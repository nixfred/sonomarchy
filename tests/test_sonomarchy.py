"""Unit tests for the Sonos-specific patches in sonomarchy.py.

Run with the plugin's own environment (it needs pa-dlna importable):

    ./scripts/test-local.sh

Everything here is pure logic; nothing touches the network or PulseAudio.
"""

import asyncio
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(os.path.dirname(HERE), 'sonomarchy.py')


def load():
    spec = importlib.util.spec_from_file_location('sonomarchy', SHIM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.IP_POLL_INTERVAL = 0.02
    return module


class FakeSnic:
    def __init__(self, address):
        self.family = socket.AF_INET
        self.address = address


class NamePrettifier(unittest.TestCase):
    def test_sonos_names_are_shortened(self):
        m = load()
        self.assertEqual(
            m._prettify('Living Room - Sonos Play:1 Media Renderer - RINCO...00_MR'),
            'Living Room (Sonos Play:1)')
        self.assertEqual(
            m._prettify('Office - Sonos Playbar Media Renderer - RINCO...00_MR'),
            'Office (Sonos Playbar)')

    def test_room_names_with_dashes_survive(self):
        m = load()
        self.assertEqual(
            m._prettify('Up - Stairs - Sonos Play:3 Media Renderer - x'),
            'Up - Stairs (Sonos Play:3)')

    def test_non_sonos_untouched(self):
        m = load()
        d = 'Denon AVR-X3500H - 78962...9ef7e'
        self.assertEqual(m._prettify(d), d)


class SonosUuid(unittest.TestCase):
    def renderer(self, udn):
        return type('R', (), {'upnp_device': type('D', (), {'UDN': udn})()})()

    def test_role_suffix_is_stripped(self):
        m = load()
        self.assertEqual(m._sonos_uuid(self.renderer('uuid:RINCON_000E58A0B1C201400_MR')),
                         'RINCON_000E58A0B1C201400')

    def test_bare_udn_works(self):
        m = load()
        self.assertEqual(m._sonos_uuid(self.renderer('uuid:RINCON_000E58A0B1C201400')),
                         'RINCON_000E58A0B1C201400')

    def test_non_sonos_is_empty(self):
        m = load()
        self.assertEqual(m._sonos_uuid(self.renderer('uuid:0c4e0-not-sonos')), '')
        self.assertFalse(m._is_sonos(type('N', (), {})()))


class AddressWatch(unittest.TestCase):
    def run_watch(self, sequence, nics=('wlan0',)):
        m = load()
        killed = []
        m.os = type('OS', (), {
            'kill': staticmethod(lambda pid, sig: killed.append(sig)),
            'getpid': staticmethod(lambda: 1)})()
        it = iter(sequence)
        last = sequence[-1]

        def fake(_nics):
            value = next(it, last)
            if isinstance(value, BaseException):
                raise value
            return value
        m._current_ipv4 = fake
        m.emit = lambda *a, **k: None
        t = threading.Thread(target=m._watch_addresses, args=(list(nics),), daemon=True)
        t.start()
        t.join(timeout=0.6)
        return bool(killed), t.is_alive() or bool(killed)

    def test_address_lost_exits(self):
        restarted, alive = self.run_watch([{'10.0.0.5'}, {'10.0.0.5'}, {'10.0.0.9'}])
        self.assertTrue(restarted)

    def test_address_lost_in_first_interval_is_noticed(self):
        # baseline is taken before the first sleep
        restarted, _ = self.run_watch([{'10.0.0.5'}, {'10.0.0.9'}])
        self.assertTrue(restarted)

    def test_new_address_alongside_old_does_not_exit(self):
        restarted, _ = self.run_watch([{'10.0.0.5'}, {'10.0.0.5', '10.0.0.6'}, {'10.0.0.5', '10.0.0.6'}])
        self.assertFalse(restarted)

    def test_stable_does_not_exit(self):
        restarted, _ = self.run_watch([{'10.0.0.5'}] * 6)
        self.assertFalse(restarted)

    def test_no_address_at_boot_then_one(self):
        restarted, _ = self.run_watch([set(), set(), {'10.0.0.5'}, {'10.0.0.5'}])
        self.assertFalse(restarted)

    def test_watchdog_survives_exceptions_and_bad_values(self):
        for bad in (OSError('enumeration failed'), None, '10.0.0.5'):
            restarted, alive = self.run_watch([{'10.0.0.5'}, bad, bad, bad])
            self.assertFalse(restarted, bad)
            self.assertTrue(alive, f'watchdog died on {bad!r}')


class Ipv4Enumeration(unittest.TestCase):
    def test_link_local_and_virtual_bridges_are_ignored(self):
        m = load()
        fake = {
            'eno1': [FakeSnic('169.254.7.7')],
            'wlan0': [FakeSnic('10.0.0.5')],
            'docker0': [FakeSnic('172.17.0.1')],
            'tailscale0': [FakeSnic('100.64.0.1')],
            'lo': [FakeSnic('127.0.0.1')],
        }
        sys.modules['psutil'] = type('P', (), {'net_if_addrs': staticmethod(lambda: fake)})()
        try:
            self.assertEqual(m._current_ipv4(['eno1', 'wlan0']), {'10.0.0.5'})
            self.assertEqual(m._current_ipv4(None), {'10.0.0.5'})
        finally:
            sys.modules.pop('psutil', None)


class TopologyCache(unittest.TestCase):
    def soap(self, topo):
        """Wrap a topology the way a real Sonos does: escaped exactly once."""
        import html
        return ('<s:Envelope'
                ' xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                '<s:Body><u:GetZoneGroupStateResponse'
                ' xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
                '<ZoneGroupState>' + html.escape(topo) +
                '</ZoneGroupState></u:GetZoneGroupStateResponse>'
                '</s:Body></s:Envelope>').encode()

    def fetch(self, m, topo):
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        import urllib.request
        original = urllib.request.urlopen
        payload = self.soap(topo)
        urllib.request.urlopen = lambda req, timeout=0: Resp(payload)
        try:
            return m._fetch_topology('192.0.2.10')
        finally:
            urllib.request.urlopen = original

    def test_concurrent_registers_fetch_once(self):
        m = load()
        calls = []

        def fake_fetch(ip):
            calls.append(ip)
            import time
            time.sleep(0.05)
            return {'players': {}, 'groups': {}}
        m._fetch_topology = fake_fetch

        results = []
        threads = [threading.Thread(
            target=lambda: results.append(m._topology('192.0.2.10')))
            for _ in range(11)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 11)

    def test_fetch_failure_fails_open(self):
        m = load()

        def boom(ip):
            raise OSError('unreachable')
        m._fetch_topology = boom
        # Never having had an answer, every renderer must still register.
        self.assertEqual(m._topology('192.0.2.10'), m._EMPTY_TOPOLOGY)

    def test_fetch_failure_keeps_the_last_good_answer(self):
        m = load()
        good = {'players': {'RINCON_A': {'zone': 'Den', 'coord': 'RINCON_A',
                                         'invisible': False}},
                'groups': {'RINCON_A': ('RINCON_A',)}}
        m._fetch_topology = lambda ip: good
        self.assertEqual(m._topology('192.0.2.10'), good)
        m._ZGT_TTL = 0                      # force a refresh attempt

        def boom(ip):
            raise OSError('unreachable')
        m._fetch_topology = boom
        self.assertEqual(m._topology('192.0.2.10'), good)

    def test_parses_bonded_and_grouped_players(self):
        m = load()
        topo = self.fetch(m, TOPOLOGY_XML)
        self.assertEqual(topo['players']['RINCON_0FF1CEB0']['invisible'], True)
        self.assertEqual(topo['players']['RINCON_DECAFB0']['invisible'], True)
        self.assertEqual(topo['players']['RINCON_CAFE01']['coord'], 'RINCON_0FF1CE')
        self.assertEqual(topo['players']['RINCON_CAFE01']['zone'], 'Living Room')

    def test_coordinator_is_first_in_its_group(self):
        m = load()
        groups = self.fetch(m, TOPOLOGY_XML)['groups']
        self.assertEqual(groups['RINCON_0FF1CE'], ('RINCON_0FF1CE', 'RINCON_CAFE01'))
        # Bonded members never appear as playable group members.
        self.assertNotIn('RINCON_0FF1CEB0', groups['RINCON_0FF1CE'])

    def test_an_ampersand_in_a_room_name_survives(self):
        # The old parser unescaped the payload twice by hand and turned
        # "Bed &amp; Bath" into a broken document.
        m = load()
        topo = self.fetch(
            m, '<ZoneGroups><ZoneGroup Coordinator="RINCON_0FF1CE" ID="x">'
               '<ZoneGroupMember UUID="RINCON_0FF1CE" ZoneName="Bed &amp; Bath"/>'
               '</ZoneGroup></ZoneGroups>')
        self.assertEqual(topo['players']['RINCON_0FF1CE']['zone'], 'Bed & Bath')

    def test_a_response_without_a_payload_raises(self):
        m = load()
        import urllib.request

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=0: Resp(
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            b'<s:Body/></s:Envelope>')
        try:
            with self.assertRaises(ValueError):
                m._fetch_topology('192.0.2.10')
        finally:
            urllib.request.urlopen = original


# One household, matching the shape of real hardware: Office coordinates a
# group that Living Room has joined, Office has a bonded surround, and Den has
# a bonded satellite.
TOPOLOGY_XML = (
    '<ZoneGroups>'
    '<ZoneGroup Coordinator="RINCON_0FF1CE" ID="RINCON_0FF1CE:1">'
    '<ZoneGroupMember UUID="RINCON_0FF1CE" ZoneName="Office"/>'
    '<ZoneGroupMember UUID="RINCON_0FF1CEB0" ZoneName="Office" Invisible="1"/>'
    '<ZoneGroupMember UUID="RINCON_CAFE01" ZoneName="Living Room"/>'
    '</ZoneGroup>'
    '<ZoneGroup Coordinator="RINCON_DECAF0" ID="RINCON_DECAF0:1">'
    '<ZoneGroupMember UUID="RINCON_DECAF0" ZoneName="Den">'
    '<Satellite UUID="RINCON_DECAFB0" ZoneName="Den"/>'
    '</ZoneGroupMember>'
    '</ZoneGroup>'
    '</ZoneGroups>')

GROUPED = {
    'players': {
        'RINCON_0FF1CE': {'zone': 'Office', 'coord': 'RINCON_0FF1CE',
                       'invisible': False},
        'RINCON_0FF1CEB0': {'zone': 'Office', 'coord': 'RINCON_0FF1CE',
                       'invisible': True},
        'RINCON_CAFE01': {'zone': 'Living Room', 'coord': 'RINCON_0FF1CE',
                       'invisible': False},
        'RINCON_DECAF0': {'zone': 'Den', 'coord': 'RINCON_DECAF0',
                       'invisible': False},
    },
    'groups': {'RINCON_0FF1CE': ('RINCON_0FF1CE', 'RINCON_CAFE01'),
               'RINCON_DECAF0': ('RINCON_DECAF0',)},
}


def ungrouped():
    """The same household with Living Room pulled out of the Office group."""
    import copy
    topo = copy.deepcopy(GROUPED)
    topo['players']['RINCON_CAFE01']['coord'] = 'RINCON_CAFE01'
    topo['groups'] = {'RINCON_0FF1CE': ('RINCON_0FF1CE',),
                      'RINCON_CAFE01': ('RINCON_CAFE01',),
                      'RINCON_DECAF0': ('RINCON_DECAF0',)}
    return topo


class GroupLabel(unittest.TestCase):
    def test_a_group_is_named_after_every_room_in_it(self):
        m = load()
        self.assertEqual(m._group_label('RINCON_0FF1CE', GROUPED),
                         'Living Room + Office')

    def test_the_label_is_alphabetical_not_coordinator_first(self):
        # Sonos reassigns the coordinator on its own; a name that followed it
        # would rename the user's output device for no visible reason.
        m = load()
        self.assertEqual(m._group_label('RINCON_0FF1CE', GROUPED),
                         m._group_label('RINCON_0FF1CE', GROUPED))
        flipped = {'players': dict(GROUPED['players']),
                   'groups': {'RINCON_CAFE01': ('RINCON_CAFE01', 'RINCON_0FF1CE')}}
        self.assertEqual(m._group_label('RINCON_CAFE01', flipped),
                         'Living Room + Office')

    def test_an_empty_group_is_not_a_group(self):
        m = load()
        self.assertIsNone(m._group_label('a', {'players': {}, 'groups': {'a': ()}}))

    def test_a_lone_speaker_gets_no_group_label(self):
        m = load()
        self.assertIsNone(m._group_label('RINCON_DECAF0', GROUPED))
        self.assertIsNone(m._group_label('RINCON_0FF1CE', ungrouped()))

    def test_three_rooms_are_all_named(self):
        # The boundary: three fit, four summarise.
        m = load()
        topo = {'players': {u: {'zone': z, 'coord': 'a', 'invisible': False}
                            for u, z in zip('abc', ['Office', 'Den', 'Patio'])},
                'groups': {'a': ('a', 'b', 'c')}}
        self.assertEqual(m._group_label('a', topo), 'Den + Office + Patio')

    def test_a_big_group_is_summarised(self):
        m = load()
        topo = {'players': {f'u{i}': {'zone': z, 'coord': 'u0',
                                      'invisible': False}
                            for i, z in enumerate(['Office', 'Living Room',
                                                   'Kitchen', 'Patio'])},
                'groups': {'u0': ('u0', 'u1', 'u2', 'u3')}}
        self.assertEqual(m._group_label('u0', topo), 'Kitchen + 3 more')

    def test_quotes_cannot_break_the_pulse_module_argument(self):
        m = load()
        topo = {'players': {'a': {'zone': 'Office', 'coord': 'a',
                                  'invisible': False},
                            'b': {'zone': 'The "Den" \\ Bar', 'coord': 'a',
                                  'invisible': False}},
                'groups': {'a': ('a', 'b')}}
        label = m._group_label('a', topo)
        self.assertNotIn('"', label)
        self.assertNotIn('\\', label)


class GroupedRegistration(unittest.TestCase):
    """What the sound menu ends up offering."""

    def outputs(self, m, topo, players=None):
        registered = []
        m._topo_cache.update(topo=topo, ts=float('inf'), fresh=True)
        m._ensure_resume_loop = lambda cp: None
        m._mark_started = lambda r: None
        m.emit = lambda *a, **k: None

        async def fake_orig(self, renderer):
            registered.append(renderer.description)
        m._orig_register = fake_orig

        # A fixed set of renderers: discovery does not depend on what the
        # topology says, which is the whole point of the empty-topology case.
        for uuid, player in (players or GROUPED['players']).items():
            renderer = type('R', (), {})()
            renderer.upnp_device = type('D', (), {'UDN': f'uuid:{uuid}_MR'})()
            renderer.root_device = type('RD', (), {
                'peer_ipaddress': '192.0.2.10'})()
            renderer.description = f"{player['zone']} (Sonos Play:1)"
            renderer.nullsink = None
            asyncio.run(m._register(object(), renderer))
        return sorted(registered)

    def test_a_group_is_one_output(self):
        m = load()
        self.assertEqual(self.outputs(m, GROUPED),
                         ['Den (Sonos Play:1)', 'Living Room + Office'])

    def test_ungrouping_gives_each_room_its_own_output_again(self):
        m = load()
        self.assertEqual(
            self.outputs(m, ungrouped()),
            ['Den (Sonos Play:1)', 'Living Room (Sonos Play:1)',
             'Office (Sonos Play:1)'])

    def test_a_follower_whose_coordinator_is_missing_is_still_offered(self):
        # Hiding it would cost the user the only speaker they can play to,
        # because nothing else in the menu covers that room.
        m = load()
        topo = {'players': {'RINCON_0FF1CE': {'zone': 'Office',
                                              'coord': 'RINCON_GH0S7',
                                              'invisible': False}},
                'groups': {'RINCON_GH0S7': ('RINCON_0FF1CE',)}}
        self.assertEqual(self.outputs(m, topo, players=topo['players']),
                         ['Office (Sonos Play:1)'])

    def test_a_follower_whose_coordinator_is_bonded_is_still_offered(self):
        m = load()
        topo = {'players': {'RINCON_0FF1CE': {'zone': 'Office',
                                              'coord': 'RINCON_CAFE01',
                                              'invisible': False},
                            'RINCON_CAFE01': {'zone': 'Office',
                                              'coord': 'RINCON_CAFE01',
                                              'invisible': True}},
                'groups': {'RINCON_CAFE01': ()}}
        self.assertIn('Office (Sonos Play:1)',
                      self.outputs(m, topo, players=topo['players']))

    def test_an_unknown_topology_registers_everything(self):
        # Fail open: better a speaker that should have been hidden than a
        # missing one.
        m = load()
        self.assertEqual(len(self.outputs(m, m._EMPTY_TOPOLOGY)), 4)


class GroupingChange(unittest.TestCase):
    def test_regrouping_is_a_change(self):
        m = load()
        self.assertTrue(m._grouping_changed(GROUPED, ungrouped()))

    def test_an_identical_topology_is_not(self):
        m = load()
        self.assertFalse(m._grouping_changed(GROUPED, GROUPED))

    def test_no_known_speakers_is_not_a_topology_change(self):
        m = load()
        m._sonos_ips.clear()
        self.assertIsNone(m._refresh_topology())

    def test_every_speaker_failing_is_not_a_topology_change(self):
        m = load()
        m._sonos_ips[:] = ['192.0.2.10', '192.0.2.11']

        def boom(ip):
            raise OSError('unreachable')
        m._fetch_topology = boom
        self.assertIsNone(m._refresh_topology())

    def test_the_speaker_that_answered_is_asked_first_next_time(self):
        m = load()
        m._sonos_ips[:] = ['192.0.2.10', '192.0.2.11']
        m._fetch_topology = (lambda ip: (_ for _ in ()).throw(OSError())
                             if ip == '192.0.2.10'
                             else {'players': {}, 'groups': {}})
        m._refresh_topology()
        self.assertEqual(m._sonos_ips[0], '192.0.2.11')

    def test_a_speaker_dropping_off_wifi_is_not_a_regrouping(self):
        m = load()
        gone = {'players': {u: p for u, p in GROUPED['players'].items()
                            if u != 'RINCON_CAFE01'},
                'groups': GROUPED['groups']}
        self.assertFalse(m._grouping_changed(GROUPED, gone))

    def test_a_new_speaker_is_not_a_regrouping(self):
        m = load()
        added = {'players': dict(GROUPED['players'],
                                 RINCON_NEW={'zone': 'Deck',
                                             'coord': 'RINCON_NEW',
                                             'invisible': False}),
                 'groups': GROUPED['groups']}
        self.assertFalse(m._grouping_changed(GROUPED, added))

    def test_re_bonding_into_a_stereo_pair_is_a_change(self):
        m = load()
        bonded = {'players': dict(GROUPED['players'],
                                  RINCON_CAFE01={'zone': 'Living Room',
                                              'coord': 'RINCON_0FF1CE',
                                              'invisible': True}),
                  'groups': GROUPED['groups']}
        self.assertTrue(m._grouping_changed(GROUPED, bonded))


class GroupWatch(unittest.TestCase):
    """The watchdog that rebuilds the sinks when the grouping changes."""

    def run_watch(self, m, polls, playing=False, stop_playing_after=None):
        """Drive _watch_zone_groups over scripted _refresh_topology results."""
        exits, emitted, ticks = [], [], [0]

        class Stop(BaseException):
            """Not an Exception: the watchdog's guard must not swallow it."""

        def wait_for_change():
            # Stands in for _wait_for_group_change, which blocks on a real
            # threading.Event fed by Sonos NOTIFYs (FIX 14).
            ticks[0] += 1
            if stop_playing_after is not None and ticks[0] > stop_playing_after:
                session.is_playing = False
            return False

        readings = iter(polls)

        def refresh():
            try:
                return next(readings)
            except StopIteration:
                raise Stop

        session = type('S', (), {'is_playing': playing})()
        renderer = type('R', (), {'stream_sessions': session})()
        m._wait_for_group_change = wait_for_change
        m.os = type('O', (), {'kill': staticmethod(
            lambda *a: exits.append(a)),
            'getpid': staticmethod(lambda: 4242)})()
        m._refresh_topology = refresh
        m.emit = lambda type_, **kw: emitted.append(type_)
        m._topo_cache.update(topo=GROUPED, ts=0.0, fresh=True)
        m._control_point = type('CP', (), {
            'renderers': staticmethod(lambda: [renderer])})()
        try:
            m._watch_zone_groups()
        except Stop:
            pass
        return bool(exits), emitted

    def test_a_steady_household_is_left_alone(self):
        m = load()
        self.assertEqual(self.run_watch(m, [GROUPED] * 6)[0], False)

    def test_a_settled_change_rebuilds_the_outputs(self):
        m = load()
        exited, emitted = self.run_watch(m, [ungrouped()] * 3)
        self.assertTrue(exited)
        self.assertIn('restart', emitted)

    def test_one_poll_is_not_enough(self):
        # Sonos reports transient groupings while a regroup is in progress.
        m = load()
        self.assertEqual(self.run_watch(m, [ungrouped()])[0], False)

    def test_a_reading_that_reverts_is_ignored(self):
        m = load()
        self.assertEqual(
            self.run_watch(m, [ungrouped(), GROUPED, GROUPED])[0], False)

    def test_unreachable_speakers_are_not_a_change(self):
        m = load()
        self.assertEqual(self.run_watch(m, [None] * 6)[0], False)

    def test_the_rebuild_waits_for_playback_to_end(self):
        m = load()
        self.assertEqual(
            self.run_watch(m, [ungrouped()] * 6, playing=True)[0], False)

    def test_and_happens_once_playback_ends(self):
        m = load()
        self.assertTrue(self.run_watch(m, [ungrouped()] * 8, playing=True,
                                       stop_playing_after=4)[0])


class Emit(unittest.TestCase):
    def test_emit_writes_one_json_line(self):
        m = load()
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            m.emit('zone', uuid='RINCON_A', name='Office (Sonos Playbar)')
        finally:
            sys.stdout = real
        line = buf.getvalue()
        self.assertTrue(line.endswith('\n'))
        self.assertEqual(json.loads(line), {'type': 'zone', 'uuid': 'RINCON_A', 'name': 'Office (Sonos Playbar)'})


class StaleSinkCleanup(unittest.TestCase):
    # `pactl list short modules` is tab-separated: index, name, argument.
    MODULES = '\n'.join([
        '5\tmodule-alsa-card\tdevice_id=0',
        '536870916\tmodule-null-sink\tsink_name="Sonos Play:1-uuid:RINCON_000E58A0B1C201400_MR" sink_properties=device.description="Den (Sonos Play:1)"',
        '536870917\tmodule-null-sink\tsink_name="easyeffects_sink"',
        '536870918\tmodule-null-sink\tsink_name="Sonos Playbar-uuid:RINCON_000E58A0B1C301400_MR"',
        '536870919\tmodule-null-sink',
    ])
    CLIENTS_WITH_PA_DLNA = 'Client #80839\n\tDriver: PipeWire\n\tProperties:\n\t\tapplication.name = "pa-dlna"\n'
    CLIENTS_WITHOUT = 'Client #12\n\tProperties:\n\t\tapplication.name = "Firefox"\n'

    def fake_pactl(self, clients_text, calls):
        class FakeRun:
            def __init__(self, stdout): self.stdout = stdout

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd == ['pactl', 'list', 'clients']:
                return FakeRun(clients_text)
            if cmd == ['pactl', 'list', 'short', 'modules']:
                return FakeRun(self.MODULES)
            return FakeRun('')
        return fake_run

    def test_only_sonos_null_sinks_are_stale(self):
        m = load()
        self.assertEqual(m._stale_null_sink_modules(self.MODULES), [536870916, 536870918])
        self.assertEqual(m._stale_null_sink_modules(''), [])
        self.assertEqual(m._stale_null_sink_modules(None), [])

    def test_client_detection(self):
        m = load()
        self.assertTrue(m._pa_dlna_client_alive(self.CLIENTS_WITH_PA_DLNA))
        self.assertFalse(m._pa_dlna_client_alive(self.CLIENTS_WITHOUT))
        self.assertFalse(m._pa_dlna_client_alive(''))

    def test_cleanup_skipped_while_another_instance_runs(self):
        m = load()
        calls = []
        import subprocess
        original = subprocess.run
        subprocess.run = self.fake_pactl(self.CLIENTS_WITH_PA_DLNA, calls)
        try:
            self.assertEqual(m._unload_stale_sinks(), 0)
        finally:
            subprocess.run = original
        self.assertFalse(any(c[:2] == ['pactl', 'unload-module'] for c in calls))

    def test_cleanup_unloads_exactly_the_stale_ones(self):
        m = load()
        calls = []
        import subprocess
        original = subprocess.run
        subprocess.run = self.fake_pactl(self.CLIENTS_WITHOUT, calls)
        m.emit = lambda *a, **k: None
        try:
            self.assertEqual(m._unload_stale_sinks(), 2)
        finally:
            subprocess.run = original
        unloaded = [c[2] for c in calls if c[:2] == ['pactl', 'unload-module']]
        self.assertEqual(unloaded, ['536870916', '536870918'])


class ReplayRing(unittest.TestCase):
    """A Range retry is served with the real missing bytes, then live."""

    def test_since_returns_exact_missing_bytes(self):
        m = load()
        r = m._Ring(capacity=100)
        r.append(b'a' * 10); r.append(b'b' * 10); r.append(b'c' * 10)
        self.assertEqual(r.end, 30)
        self.assertEqual(r.since(0), b'a' * 10 + b'b' * 10 + b'c' * 10)
        self.assertEqual(r.since(15), b'b' * 5 + b'c' * 10)
        self.assertEqual(r.since(30), b'')                 # nothing missed
        self.assertIsNone(r.since(31))                      # ahead of us
        self.assertEqual(r.since(10), b'b' * 10 + b'c' * 10)

    def test_eviction_keeps_a_window_and_refuses_older_offsets(self):
        m = load()
        r = m._Ring(capacity=25)
        for ch in b'abcd':
            r.append(bytes([ch]) * 10)
        self.assertEqual(r.end, 40)
        self.assertLessEqual(r.size, 30)                    # one chunk over cap at most
        self.assertIsNone(r.since(5))                       # evicted
        self.assertEqual(r.since(r.start), b''.join(d for _, d in r.chunks))

    def test_reset_on_new_playback(self):
        m = load()
        r = m._Ring(); r.append(b'x' * 5); r.reset()
        self.assertEqual((r.start, r.end, r.size), (0, 0, 0))
        self.assertEqual(r.since(0), b'')

    def test_write_track_replays_then_records(self):
        m = load()
        sent = []

        class Writer:
            def is_closing(s): return False
            def write(s, b): sent.append(bytes(b))
            async def drain(s): pass

        class Reader:
            def __init__(s, chunks): s.chunks = list(chunks)
            async def readexactly(s, n):
                if not s.chunks: raise asyncio.IncompleteReadError(b'', n)
                return s.chunks.pop(0)

        class Renderer:
            name = 'Office'; mime_type = 'audio/mp3'
        renderer = Renderer()

        class Session:
            track = None; is_playing = False       # a deliberate end, no unlatch
        session = Session(); session.renderer = renderer

        class Track:
            task_name = 't'; writer = Writer()
        t = Track(); t.session = session
        m._http_server.HTTP_CHUNK_SIZE = 4
        # first playback: bytes 0..7 sent and recorded
        asyncio.run(m._write_track(t, Reader([b'0123', b'4567'])))
        ring = m._ring(renderer)
        self.assertEqual(ring.end, 8)
        self.assertEqual(sent, [b'0123', b'4567'])
        # speaker reconnects asking for byte 4-: the header step decides on the replay
        renderer._sonomarchy_range_start = 4
        hdr = []

        class HW:
            def write(s, b): hdr.append(bytes(b))
            async def drain(s): pass
        asyncio.run(m._write_http_ok(HW(), renderer))
        self.assertIn(b'206 Partial Content', hdr[0])
        self.assertEqual(renderer._sonomarchy_replay, (4, b'4567'))
        # ... and the track first replays "4567", then streams live
        sent.clear()
        asyncio.run(m._write_track(t, Reader([b'89ab'])))
        self.assertEqual(sent, [b'4567', b'89ab'])
        self.assertEqual(ring.end, 12)
        # a resume outside the window falls back to a fresh 200 and a reset ring
        renderer._sonomarchy_range_start = 999
        hdr.clear()
        asyncio.run(m._write_http_ok(HW(), renderer))
        self.assertIn(b'200 OK', hdr[0])
        self.assertEqual(ring.end, 0)


class RangeResume(unittest.TestCase):
    """A speaker retrying a broken stream asks to resume with a Range header."""

    def test_range_start_parsing_accepts_only_the_open_ended_form(self):
        m = load()
        self.assertEqual(m._range_start({'RANGE': 'bytes=208901-'}), 208901)
        self.assertEqual(m._range_start({'Range': 'bytes=0-'}), 0)
        self.assertEqual(m._range_start({'Range': ' Bytes=12- '}), 12)
        for bad in ('bytes=5-9', 'bytes=5-9,20-', 'bytes=-500', 'items=1-2', 'bytes=', 'bytes=a-'):
            self.assertIsNone(m._range_start({'Range': bad}), bad)
        self.assertIsNone(m._range_start({}))
        self.assertIsNone(m._range_start(None))

    def test_plain_get_resets_the_ring_and_range_keeps_it(self):
        m = load()
        r = type('R', (), {})()
        ring = m._ring(r); ring.append(b'x' * 100)
        self.assertEqual(m._prepare_request_state(r, {'Range': 'bytes=40-'}), 40)
        self.assertEqual(ring.end, 100)                      # resume: keep history
        self.assertIsNone(m._prepare_request_state(r, {}))
        self.assertEqual(ring.end, 0)                        # fresh representation: byte 0
        self.assertIsNone(m._prepare_request_state(r, None))

    def test_shutdown_writes_no_chunked_terminator(self):
        m = load()
        writes = []

        class Writer:
            closed = False
            def is_closing(s): return False
            def write(s, b): writes.append(bytes(b))
            async def drain(s): pass
            def close(s): s.closed = True
            async def wait_closed(s): pass

        class Track:
            task_name = 't'
        t = Track(); w = t.writer = Writer()
        asyncio.run(m._track_shutdown(t))
        self.assertEqual(writes, [])                         # upstream wrote b'0\r\n\r\n' here
        self.assertTrue(w.closed); self.assertIsNone(t.writer)
        asyncio.run(m._track_shutdown(t))                    # idempotent

    def test_206_headers_only_for_a_real_resume(self):
        m = load()
        full = m._http_ok_lines('audio/mp3')
        self.assertEqual(full[0], 'HTTP/1.1 200 OK')
        self.assertIn(f'Content-Length: {m.FAKE_CONTENT_LENGTH}', full)
        part = m._http_ok_lines('audio/mp3', 208901)
        self.assertEqual(part[0], 'HTTP/1.1 206 Partial Content')
        self.assertIn(f'Content-Range: bytes 208901-{m.FAKE_CONTENT_LENGTH - 1}/{m.FAKE_CONTENT_LENGTH}', part)
        self.assertIn(f'Content-Length: {m.FAKE_CONTENT_LENGTH - 208901}', part)
        # byte 0 is a plain fetch, not a resume
        self.assertEqual(m._http_ok_lines('audio/mp3', 0)[0], 'HTTP/1.1 200 OK')
        self.assertTrue(all(line.isascii() for line in part))


class ResetKeepsZone(unittest.TestCase):
    """A dropped HTTP connection closes the session, never the renderer."""

    def run_track(self, m, failure):
        events = []

        class Renderer:
            name = 'Office'
            async def close(s): events.append('renderer.close')

        class Session:
            renderer = Renderer()
            class stream_tasks:
                @staticmethod
                def create_task(coro, name=None): coro.close(); events.append('shutdown-task')
            async def close_session(s, shutdown_coro=False): events.append(('close_session', shutdown_coro))

        class Track:
            task = object(); task_name = 'Office-track-1'; writer = object()
            session = Session()
            async def write_track(s, reader):
                if failure: raise failure
            async def shutdown(s): events.append('shutdown')

        async def ok(writer, renderer): events.append('headers')
        m._http_server.write_http_ok = ok
        t = Track()
        try:
            asyncio.run(m._track_run(t, None))
        except Exception as e:
            events.append(('raised', type(e).__name__))
        return events

    def test_connection_reset_keeps_renderer(self):
        m = load()
        ev = self.run_track(m, ConnectionResetError('Connection lost'))
        self.assertIn(('close_session', True), ev)
        self.assertNotIn('renderer.close', ev)

    def test_clean_end_and_other_errors_unchanged(self):
        m = load()
        self.assertEqual(self.run_track(m, None), ['headers', 'shutdown'])
        ev = self.run_track(m, RuntimeError('boom'))
        self.assertIn(('close_session', True), ev)
        self.assertIn(('raised', 'RuntimeError'), ev)
        self.assertNotIn('renderer.close', ev)


class StreamResume(unittest.TestCase):
    """FIX 10: a dead stream must be able to come back."""

    def test_takeover_is_rate_limited_per_renderer(self):
        m = load()
        self.assertTrue(m._should_takeover('Office', 100.0))
        self.assertFalse(m._should_takeover('Office', 101.0))     # too soon
        self.assertTrue(m._should_takeover('Kitchen', 101.0))     # other zone
        self.assertTrue(m._should_takeover('Office', 100.0 + m.TAKEOVER_MIN_INTERVAL))

    def test_unlatch_only_when_track_is_current_and_playing(self):
        m = load()
        calls = []

        class Procs:
            async def close_encoder(s): calls.append('close_encoder')

        class Session:
            def __init__(s, track, playing):
                s.track = track; s.is_playing = playing; s.processes = Procs()
            async def stop_track(s):
                # Upstream's stop_track cancels the track task. Called from
                # inside that task it cancels the caller. Must never be used.
                calls.append('stop_track'); asyncio.current_task().cancel(); await asyncio.sleep(0)

        class Track:
            task_name = 't'
            def __init__(s, session): s.session = session
        t = Track(None); s = t.session = Session(t, True)
        asyncio.run(m._unlatch_after_eof(t))
        self.assertEqual(calls, ['close_encoder'])          # no self-cancel
        self.assertFalse(s.is_playing); self.assertIsNone(s.track)
        calls.clear()
        t2 = Track(None); t2.session = Session(object(), True)   # a newer track owns the session
        asyncio.run(m._unlatch_after_eof(t2))
        t3 = Track(None); t3.session = Session(t3, False)        # already stopped deliberately
        asyncio.run(m._unlatch_after_eof(t3))
        self.assertEqual(calls, [])

    def test_wait_until_stopped_polls_and_times_out(self):
        m = load()

        class R:
            def __init__(s, states): s.states = list(states)
            async def get_transport_state(s):
                return s.states.pop(0) if len(s.states) > 1 else s.states[0]
        self.assertTrue(asyncio.run(m._wait_until_stopped(R(['TRANSITIONING', 'PAUSED_PLAYBACK', 'STOPPED']), timeout=2, step=0.01)))
        self.assertFalse(asyncio.run(m._wait_until_stopped(R(['PLAYING']), timeout=0.05, step=0.01)))

    def test_processes_alive(self):
        m = load()
        P = lambda rc: type('P', (), {'returncode': rc})()
        procs = type('S', (), {'parec_proc': P(None), 'encoder_proc': P(None)})()
        self.assertTrue(m._processes_alive(procs))
        dead = type('S', (), {'parec_proc': P(None), 'encoder_proc': P(0)})()
        self.assertFalse(m._processes_alive(dead))
        self.assertFalse(m._processes_alive(None))
        # L16 has no encoder process at all: parec alone decides
        l16 = type('S', (), {'parec_proc': P(None), 'encoder_proc': None, 'no_encoder': True})()
        self.assertTrue(m._processes_alive(l16))
        l16_dead = type('S', (), {'parec_proc': P(1), 'encoder_proc': None, 'no_encoder': True})()
        self.assertFalse(m._processes_alive(l16_dead))

    def test_start_is_marked_at_handle_action_entry(self):
        m = load()
        calls = []

        async def fake_orig(self, action): calls.append('orig')
        m._orig_handle_action = fake_orig

        class R:
            name = 'Office'
            upnp_device = type('D', (), {'UDN': 'uuid:RINCON_ABC_MR'})()
            async def get_transport_state(s): return 'STOPPED'
        r = R()
        asyncio.run(m._handle_action(r, m._pa_dlna.MetaData('cliamp', '', 'x')))
        self.assertTrue(m._in_start_grace(r))
        self.assertEqual(calls, ['orig'])
        r2 = R()
        asyncio.run(m._handle_action(r2, 'Stop'))            # not a start
        self.assertFalse(m._in_start_grace(r2))

    def test_close_purges_per_zone_recovery_state(self):
        m = load()

        async def fake_close(self, *a, **k): return 'closed'
        m._orig_close = fake_close
        m.emit = lambda *a, **k: None
        r = type('R', (), {'name': 'Office', 'upnp_device': type('D', (), {'UDN': 'uuid:RINCON_ABC_MR'})(),
                           'nullsink': type('N', (), {'sink': type('K', (), {'name': 'sink-office'})()})()})()
        m._last_takeover['Office'] = 1.0; m._idle_sweeps['sink-office'] = 1
        self.assertEqual(asyncio.run(m._close(r)), 'closed')
        self.assertNotIn('Office', m._last_takeover); self.assertNotIn('sink-office', m._idle_sweeps)

    def test_resume_loop_handle_is_retained(self):
        # asyncio only weakly references tasks; a dropped handle means the
        # sweep can be garbage-collected while pending and never run.
        m = load()
        m.RESUME_INTERVAL = 0.01
        seen = []
        m._pactl_inputs_by_sink = lambda: (seen.append(1), {})[1]

        class CP:
            def renderers(self): return []

        async def go():
            m._ensure_resume_loop(CP())
            self.assertIsNotNone(m._resume_task)
            self.assertIn(m._resume_task, m._background_tasks)
            await asyncio.sleep(0.1)
            m._resume_task.cancel()
            try:
                await m._resume_task
            except asyncio.CancelledError:
                pass
        asyncio.run(go())
        self.assertGreater(len(seen), 0, 'sweep never ran')

    def make_renderer(self, m, actions, playing, alive, state='STOPPED', inputs=()):
        class Sessions:
            def __init__(s):
                s.is_playing = playing
                P = lambda rc: type('P', (), {'returncode': rc})()
                s.processes = type('S', (), {'parec_proc': P(None), 'encoder_proc': P(None if alive else 0)})()
            async def stop_track(s): actions.append('stop_track')
            async def close_session(s, shutdown_coro=False): actions.append('close_session')

        class SinkInput:
            def __init__(s, index, sink): s.index = index; s.sink = sink; s.proplist = {'application.name': 'cliamp'}

        class LibPulse:
            async def pa_context_get_sink_input_info_list(s): return [SinkInput(i, k) for i, k in inputs]

        class Renderer:
            closing = False
            description = 'Office (Sonos Playbar)'
            name = 'Office'
            control_point = type('CP', (), {'pulse': type('PU', (), {'lib_pulse': LibPulse()})()})()
            def __init__(s):
                s.stream_sessions = Sessions(); s.state = state
                s.nullsink = type('N', (), {'sink': type('K', (), {'name': 'sink-office', 'index': 42})(), 'sink_input': None})()
            async def get_transport_state(s): return s.state
            async def stop(s): actions.append('stop')
            async def handle_action(s, meta): actions.append(('start', meta.publisher))
            def sink_input_meta(s, si): return m._pa_dlna.MetaData(si.proplist['application.name'], '', 'x')
        m.emit = lambda *a, **k: None
        return Renderer()

    def test_resume_restarts_only_a_silent_zone_with_a_player(self):
        m = load(); actions = []
        # healthy zone with a player and the speaker PLAYING: untouched
        asyncio.run(m._maybe_resume(self.make_renderer(m, actions, True, True, 'PLAYING'), {'sink-office': ['cliamp']}))
        self.assertEqual(actions, [])
        # dead session, player still going, transport STOPPED: restarted, with an
        # explicit Stop first (a player that gave up will not re-fetch the same
        # URL on a bare Play), and the owning sink-input adopted for metadata
        r = self.make_renderer(m, actions, True, False, 'STOPPED', inputs=[(7, 99), (8, 42)])
        asyncio.run(m._maybe_resume(r, {'sink-office': ['cliamp']}))
        self.assertEqual(actions, ['stop_track', 'stop', ('start', 'cliamp')])
        self.assertEqual(r.nullsink.sink_input.index, 8)          # the one on our sink

    def test_sweep_leaves_a_zone_alone_while_its_start_is_in_flight(self):
        m = load(); actions = []
        r = self.make_renderer(m, actions, False, False, 'STOPPED')
        m._mark_started(r)                                   # Play just sent
        asyncio.run(m._maybe_resume(r, {'sink-office': ['cliamp']}))
        self.assertEqual(actions, [])
        r._sonomarchy_started_at -= m.RESUME_GRACE + 1       # grace elapsed
        asyncio.run(m._maybe_resume(r, {'sink-office': ['cliamp']}))
        self.assertEqual(actions[:2], ['stop_track', 'stop'])

    def test_speaker_state_is_the_truth(self):
        # session looks alive on our side but the zone reads STOPPED: restart
        m = load(); actions = []
        asyncio.run(m._maybe_resume(self.make_renderer(m, actions, True, True, 'STOPPED'), {'sink-office': ['cliamp']}))
        self.assertEqual(actions[:2], ['stop_track', 'stop'])
        self.assertEqual(actions[-1][0], 'start')

    def test_stale_session_torn_down_after_two_idle_sweeps(self):
        m = load(); actions = []
        r = self.make_renderer(m, actions, True, True, 'STOPPED')
        asyncio.run(m._maybe_resume(r, {}))          # sweep 1: could be a track gap
        self.assertEqual(actions, [])
        asyncio.run(m._maybe_resume(r, {}))          # sweep 2: stale for real
        self.assertEqual(actions, ['close_session', 'stop'])   # whole chain, parec included
        # a residual parec after stop_track (is_playing False, processes alive)
        # is exactly the leak this must catch
        actions.clear(); rr = self.make_renderer(m, actions, False, True, 'STOPPED')
        asyncio.run(m._maybe_resume(rr, {})); asyncio.run(m._maybe_resume(rr, {}))
        self.assertEqual(actions, ['close_session', 'stop'])
        # a player coming back resets the count
        actions.clear(); r2 = self.make_renderer(m, actions, True, True, 'PLAYING')
        asyncio.run(m._maybe_resume(r2, {}))
        asyncio.run(m._maybe_resume(r2, {'sink-office': ['cliamp']}))
        asyncio.run(m._maybe_resume(r2, {}))
        self.assertEqual(actions, [])
        # dead session and nobody playing: nothing to do, ever
        actions.clear(); r3 = self.make_renderer(m, actions, False, False, 'STOPPED')
        for _ in range(3): asyncio.run(m._maybe_resume(r3, {}))
        self.assertEqual(actions, [])


class ArgvParsing(unittest.TestCase):
    def test_nics_forms(self):
        m = load()
        self.assertEqual(m._nics_from_argv(['x', '--nics', 'eno1,wlan0']), ['eno1', 'wlan0'])
        self.assertEqual(m._nics_from_argv(['x', '--nics=wlan0']), ['wlan0'])
        self.assertEqual(m._nics_from_argv(['x', '-n', 'wlan0']), ['wlan0'])
        self.assertIsNone(m._nics_from_argv(['x', '--port', '8080']))


class ReplyRoute(unittest.TestCase):
    """FIX 8 has to tell a firewall drop from a VPN swallowing the reply."""

    def test_loopback_route_is_lo(self):
        m = load()
        # Exercised against real `ip route get` output rather than a fixture,
        # so the parser cannot drift from the tool it parses. The loopback
        # route exists on every Linux box.
        self.assertEqual(m._reply_route('127.0.0.1', '127.0.0.1'), 'lo')

    def test_unresolvable_destination_is_no_opinion(self):
        m = load()
        # Must return None, not raise and not guess: the caller falls back to
        # the generic advice when the route cannot be determined.
        self.assertIsNone(m._reply_route('127.0.0.1', 'not-an-ip'))

    def test_tunnel_interfaces_are_recognised(self):
        m = load()
        for dev in ('tailscale0', 'wg0', 'tun0', 'ppp0', 'nebula1', 'utun3'):
            self.assertTrue(dev.startswith(m._TUNNEL_PREFIXES), dev)

    def test_real_interfaces_are_not_flagged(self):
        m = load()
        # A false positive here would blame a VPN for a genuine firewall
        # drop, which is the exact mistake this change exists to stop.
        for dev in ('eth0', 'enp0s20f0u4c2', 'wlp2s0', 'wlo1', 'eno1',
                    'br-lan', 'lo'):
            self.assertFalse(dev.startswith(m._TUNNEL_PREFIXES), dev)


class FirewallRuleHint(unittest.TestCase):
    """FIX 11 must be exact about this machine, or say nothing at all."""

    def test_ufw_rules_name_subnet_and_both_ports(self):
        m = load()
        rules = m._firewall_rules('ufw', '10.0.0.0/24', 8083, 8081)
        self.assertEqual(len(rules), 2)
        self.assertIn('proto tcp from 10.0.0.0/24 to any port 8083', rules[0])
        self.assertIn('proto udp from 10.0.0.0/24 to any port 8081', rules[1])

    def test_firewalld_gets_firewalld_syntax(self):
        m = load()
        rules = m._firewall_rules('firewalld', '192.168.1.0/24', 8080, 8081)
        self.assertTrue(all(r.startswith('firewall-cmd') for r in rules))
        self.assertIn('source address=192.168.1.0/24', rules[0])
        self.assertEqual(rules[-1], 'firewall-cmd --reload')

    def test_unknown_subnet_or_port_yields_nothing(self):
        m = load()
        # Silence beats a rule with a placeholder in it: a half-right command
        # pasted as root is worse than no command.
        self.assertEqual(m._firewall_rules('ufw', None, 8080, 8081), [])
        self.assertEqual(m._firewall_rules('ufw', '10.0.0.0/24', None, 8081), [])

    def test_udp_port_is_optional(self):
        m = load()
        rules = m._firewall_rules('ufw', '10.0.0.0/24', 8080, None)
        self.assertEqual(len(rules), 1)

    def test_subnet_is_the_network_not_the_host(self):
        m = load()
        # A rule sourced from a /32 would let exactly one speaker in.
        self.assertEqual(m._lan_cidr('127.0.0.1'), '127.0.0.0/8')

    def test_port_parsing_handles_both_spellings(self):
        m = load()
        self.assertEqual(m._arg_value(['x', '--port', '8085'], ['--port']), 8085)
        self.assertEqual(m._arg_value(['x', '--port=8085'], ['--port']), 8085)
        self.assertEqual(m._arg_value(['x', '-p', '8081'], ['-p']), 8081)
        self.assertIsNone(m._arg_value(['x', '--port', 'wat'], ['--port']))
        self.assertIsNone(m._arg_value(['x'], ['--port']))


class TransientSinkInput(unittest.TestCase):
    """FIX 12: a brief stream ending must not close someone else's stream."""

    def make(self, m, closed, inputs, pointer_index):
        class SinkInput:
            def __init__(s, index, sink):
                s.index = index; s.sink = sink
                s.proplist = {'application.name': 'cliamp'}

        class LibPulse:
            async def pa_context_get_sink_input_info_list(s):
                return [SinkInput(i, k) for i, k in inputs]

        class Sessions:
            async def close_session(s): closed.append('closed')

        class Renderer:
            name = 'Office'
            control_point = type('CP', (), {
                'pulse': type('PU', (), {'lib_pulse': LibPulse()})()})()
            def __init__(s):
                s.stream_sessions = Sessions()
                s.nullsink = type('N', (), {
                    'sink': type('K', (), {'name': 'sink-office',
                                           'index': 42})(),
                    'sink_input': SinkInput(pointer_index, 42)})()
            def get_sink_input_index(s):
                si = s.nullsink.sink_input
                return None if si is None else si.index
            def log_pulse_event(s, *a): closed.append('ignored')
            async def stop(s): pass
        return Renderer()

    def test_transient_removal_spares_the_stream_that_is_still_playing(self):
        m = load(); m.TRACK_CHANGE_GRACE = 0
        closed = []
        # The pointer names the transient 10570, which has gone; 8210 is the
        # real stream and is still on our sink. This is the observed bug.
        r = self.make(m, closed, inputs=[(8210, 42)], pointer_index=10570)
        asyncio.run(m._maybe_stop(r, 10570, 'PLAYING'))
        self.assertEqual(closed, ['ignored'])
        self.assertEqual(r.nullsink.sink_input.index, 8210)

    def test_a_genuinely_idle_zone_is_still_torn_down(self):
        m = load(); m.TRACK_CHANGE_GRACE = 0
        closed = []
        # Nothing left on our sink: this must still close, or the encoder
        # leaks for as long as the renderer lives.
        r = self.make(m, closed, inputs=[], pointer_index=10570)
        asyncio.run(m._maybe_stop(r, 10570, 'PLAYING'))
        self.assertEqual(closed, ['closed'])

    def test_input_on_another_sink_does_not_count_as_busy(self):
        m = load(); m.TRACK_CHANGE_GRACE = 0
        closed = []
        r = self.make(m, closed, inputs=[(999, 77)], pointer_index=10570)
        asyncio.run(m._maybe_stop(r, 10570, 'PLAYING'))
        self.assertEqual(closed, ['closed'])

    def test_upstream_fast_path_still_wins(self):
        m = load(); m.TRACK_CHANGE_GRACE = 0
        closed = []
        # A replacement announced itself in time: pointer already moved on.
        r = self.make(m, closed, inputs=[(8211, 42)], pointer_index=8211)
        asyncio.run(m._maybe_stop(r, 10570, 'PLAYING'))
        self.assertEqual(closed, ['ignored'])


class TopologyEvents(unittest.TestCase):
    """FIX 14: a Sonos NOTIFY wakes the watchdog immediately."""

    def test_lease_is_parsed_from_the_timeout_header(self):
        m = load()
        self.assertEqual(m._lease_seconds('Second-1800'), 1800)
        self.assertEqual(m._lease_seconds('Second-300'), 300)

    def test_a_nonsense_lease_falls_back_to_what_we_asked_for(self):
        m = load()
        for bad in ('infinite', '', None, 'Second-', 'Second-nope'):
            self.assertEqual(m._lease_seconds(bad), m.GROUP_EVENT_LEASE)

    def test_an_absurdly_short_lease_is_floored(self):
        # Renewing at half of a 1-second lease would hammer the speaker.
        m = load()
        self.assertEqual(m._lease_seconds('Second-1'), 60)

    def test_a_lease_longer_than_we_asked_for_is_capped(self):
        # Otherwise the renewal is scheduled past the point the subscription
        # actually lapses, and events stop with nothing logged.
        m = load()
        self.assertEqual(m._lease_seconds('Second-99999999999999999999'),
                         m.GROUP_EVENT_LEASE)

    def test_subscribe_sends_a_callback_the_speaker_can_reach(self):
        m = load()
        sent = {}

        def fake_request(ip, method, headers):
            sent.update(ip=ip, method=method, headers=headers)
            return {'SID': 'uuid:abc', 'TIMEOUT': 'Second-600'}
        m._event_request = fake_request
        m._local_ip_for = lambda peer: '192.0.2.99'

        m._subscribe('192.0.2.10', 8090)
        self.assertEqual(sent['method'], 'SUBSCRIBE')
        self.assertEqual(sent['headers']['CALLBACK'],
                         '<http://192.0.2.99:8090/ZoneGroupTopology/Event>')
        self.assertEqual(sent['headers']['NT'], 'upnp:event')
        self.assertEqual(m._event_state['sid'], 'uuid:abc')
        self.assertEqual(m._event_state['ip'], '192.0.2.10')

    def test_a_subscribe_without_a_sid_is_an_error(self):
        m = load()
        m._event_request = lambda ip, method, headers: {'TIMEOUT': 'Second-600'}
        m._local_ip_for = lambda peer: '192.0.2.99'
        with self.assertRaises(ValueError):
            m._subscribe('192.0.2.10', 8090)

    def test_unsubscribe_clears_state_even_when_it_fails(self):
        m = load()
        m._event_state.update(sid='uuid:abc', ip='192.0.2.10')

        def boom(ip, method, headers):
            raise OSError('speaker gone')
        m._event_request = boom
        m._unsubscribe()                       # must not raise
        self.assertIsNone(m._event_state['sid'])
        self.assertIsNone(m._event_state['ip'])

    def test_unsubscribe_on_a_dead_subscription_does_nothing(self):
        m = load()
        called = []
        m._event_request = lambda *a, **k: called.append(a)
        m._unsubscribe()
        self.assertEqual(called, [])

    def test_an_event_wakes_the_watchdog_at_once(self):
        m = load()
        m.GROUP_POLL_INTERVAL = 30      # long enough that a poll cannot explain it
        m.GROUP_EVENT_SETTLE = 0
        m._group_wakeup.set()
        started = time.monotonic()
        self.assertTrue(m._wait_for_group_change())
        self.assertLess(time.monotonic() - started, 1)
        # The flag must be consumed, or the next turn spins.
        self.assertFalse(m._group_wakeup.is_set())

    def test_without_an_event_it_falls_back_to_the_poll_interval(self):
        m = load()
        m.GROUP_POLL_INTERVAL = 0.05
        started = time.monotonic()
        self.assertFalse(m._wait_for_group_change())
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_a_notify_over_the_wire_sets_the_wakeup(self):
        # The one test that opens a socket, on loopback only: the handler is
        # reached by an HTTP method (NOTIFY) that no client library sends by
        # default, so faking the request would prove nothing.
        import urllib.request
        m = load()
        port = m._start_event_listener()
        self.assertIsNotNone(port, 'no free port in the event range')
        try:
            m._group_wakeup.clear()
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/ZoneGroupTopology/Event',
                data=b'<propertyset/>', method='NOTIFY')
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                # One NOTIFY per connection: a body we could not drain would
                # otherwise be read as the next request on a kept-alive socket.
                self.assertEqual(resp.headers.get('Connection'), 'close')
            self.assertTrue(m._group_wakeup.wait(2))
            self.assertEqual(m._event_state['notifies'], 1)
        finally:
            m._group_wakeup.clear()
            m._event_state['server'].shutdown()
            m._event_state['server'].server_close()

    def test_the_listener_reports_failure_rather_than_raising(self):
        m = load()
        m.GROUP_EVENT_PORTS = range(1, 2)      # port 1: not ours to bind
        self.assertIsNone(m._start_event_listener())
        self.assertIsNone(m._event_state['server'])


class EventLoopStart(unittest.TestCase):
    """The subscription must not wait a poll interval to happen."""

    def drive(self, m, turns=1):
        subscribed, slept = [], []

        class Stop(BaseException):
            pass

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) >= turns:
                raise Stop
        m._start_event_listener = lambda nics=None: 8090
        m._subscribe = lambda ip, port: subscribed.append((ip, port))
        m.time = type('T', (), {'sleep': staticmethod(sleep),
                                'time': staticmethod(lambda: 0.0)})()
        m._sonos_ips.append('192.0.2.10')
        try:
            m._watch_zone_group_events(['wlan0'])
        except Stop:
            pass
        return subscribed, slept

    def test_it_subscribes_before_sleeping(self):
        # The bug: sleeping first left every backend unsubscribed for a whole
        # GROUP_EVENT_POLL, and a grouping change restarts the backend, so the
        # blind window landed exactly when the next change was most likely.
        m = load()
        subscribed, slept = self.drive(m)
        self.assertEqual(subscribed, [('192.0.2.10', 8090)])
        self.assertEqual(len(slept), 1, 'slept more than once before stopping')

    def test_it_retries_quickly_while_nothing_is_subscribed(self):
        # No speaker is known until the first renderer registers, long after
        # this thread starts. Waiting a full GROUP_EVENT_POLL to look again
        # left a measured 108 s with no subscription after every restart.
        m = load()
        slept = []

        class Stop(BaseException):
            pass

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) >= 2:
                raise Stop
        m._start_event_listener = lambda nics=None: 8090
        m.time = type('T', (), {'sleep': staticmethod(sleep),
                                'time': staticmethod(lambda: 0.0)})()
        m._sonos_ips.clear()            # nothing discovered yet
        try:
            m._watch_zone_group_events(['wlan0'])
        except Stop:
            pass
        self.assertEqual(slept, [m.GROUP_EVENT_RETRY] * 2)

    def test_it_backs_off_once_subscribed(self):
        m = load()
        slept = []

        class Stop(BaseException):
            pass

        def sleep(seconds):
            slept.append(seconds)
            raise Stop
        m._start_event_listener = lambda nics=None: 8090
        m._subscribe = lambda ip, port: m._event_state.update(
            sid='uuid:x', ip=ip, renew_at=1e18)
        m.time = type('T', (), {'sleep': staticmethod(sleep),
                                'time': staticmethod(lambda: 0.0)})()
        m._sonos_ips.append('192.0.2.10')
        try:
            m._watch_zone_group_events(['wlan0'])
        except Stop:
            pass
        self.assertEqual(slept, [m.GROUP_EVENT_POLL])

    def test_a_listener_that_cannot_start_gives_up_quietly(self):
        m = load()
        m._start_event_listener = lambda nics=None: None
        called = []
        m._subscribe = lambda ip, port: called.append(ip)
        m._watch_zone_group_events(['wlan0'])      # must return, not spin
        self.assertEqual(called, [])


class GroupingRebuildBurst(unittest.TestCase):
    """A flapping group must not restart the backend forever."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get('XDG_STATE_HOME')
        os.environ['XDG_STATE_HOME'] = self.tmp

    def tearDown(self):
        import shutil
        if self.old is None:
            os.environ.pop('XDG_STATE_HOME', None)
        else:
            os.environ['XDG_STATE_HOME'] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nothing_recorded_means_nothing_held_off(self):
        m = load()
        self.assertFalse(m._grouping_rebuilds_exhausted())

    def test_a_regroup_needing_a_second_rebuild_still_gets_one(self):
        # The case that made a flat cooldown wrong: the first rebuild can
        # sample the topology while Sonos is still moving rooms, and the
        # correction must not be suppressed.
        m = load()
        m._record_grouping_restart()
        self.assertFalse(m._grouping_rebuilds_exhausted())
        m._record_grouping_restart()
        self.assertFalse(m._grouping_rebuilds_exhausted())

    def test_a_flapping_group_is_eventually_held_off(self):
        m = load()
        for _ in range(m.GROUP_RESTART_BURST):
            m._record_grouping_restart()
        self.assertTrue(m._grouping_rebuilds_exhausted())

    def test_the_budget_refills_once_the_window_passes(self):
        m = load()
        for _ in range(m.GROUP_RESTART_BURST):
            m._record_grouping_restart()
        self.assertTrue(m._grouping_rebuilds_exhausted())
        stale = time.time() - m.GROUP_RESTART_WINDOW - 1
        with open(m._grouping_restart_stamp(), 'w') as stamp:
            json.dump([stale] * m.GROUP_RESTART_BURST, stamp)
        self.assertFalse(m._grouping_rebuilds_exhausted())

    def test_the_stamp_file_does_not_grow_without_bound(self):
        m = load()
        for _ in range(50):
            m._record_grouping_restart()
        with open(m._grouping_restart_stamp()) as stamp:
            self.assertLessEqual(len(json.load(stamp)), m.GROUP_RESTART_BURST)

    def test_a_corrupt_stamp_fails_open(self):
        m = load()
        os.makedirs(m._state_dir(), exist_ok=True)
        for junk in ('', 'not json', '{}', '[1, "x"]', '[null]'):
            with open(m._grouping_restart_stamp(), 'w') as stamp:
                stamp.write(junk)
            self.assertFalse(m._grouping_rebuilds_exhausted())

    def test_stamps_from_the_future_are_dropped(self):
        # A clock jump must not wedge rebuilds until the clock catches up.
        m = load()
        os.makedirs(m._state_dir(), exist_ok=True)
        with open(m._grouping_restart_stamp(), 'w') as stamp:
            json.dump([time.time() + 86400] * m.GROUP_RESTART_BURST, stamp)
        self.assertFalse(m._grouping_rebuilds_exhausted())

    def test_an_unwritable_state_dir_does_not_raise(self):
        m = load()
        m._state_dir = lambda: '/proc/nope/nowhere'
        m._record_grouping_restart()               # must not raise
        self.assertFalse(m._grouping_rebuilds_exhausted())


if __name__ == '__main__':
    unittest.main()
