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
    def test_concurrent_registers_fetch_once(self):
        m = load()
        calls = []

        def fake_fetch(ip):
            calls.append(ip)
            import time
            time.sleep(0.05)
            return frozenset({'RINCON_A'})
        m._fetch_invisible_uuids = fake_fetch

        async def herd():
            return await asyncio.gather(*[m._invisible_uuids('192.0.2.10') for _ in range(11)])
        results = asyncio.run(herd())
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(r == {'RINCON_A'} for r in results))

    def test_fetch_failure_fails_open(self):
        m = load()

        def boom(ip):
            raise OSError('unreachable')
        m._fetch_invisible_uuids = boom
        result = asyncio.run(m._invisible_uuids('192.0.2.10'))
        self.assertEqual(result, frozenset())

    def test_invisible_parse(self):
        m = load()
        import html
        topo = ('<ZoneGroups><ZoneGroup Coordinator="RINCON_AAA" ID="x">'
                '<ZoneGroupMember UUID="RINCON_AAA" ZoneName="Office"/>'
                '<ZoneGroupMember UUID="RINCON_BBB" ZoneName="Office" Invisible="1"/>'
                '<ZoneGroupMember UUID="RINCON_CCC" ZoneName="Den"><Satellite UUID="RINCON_DDD" Invisible="1"/></ZoneGroupMember>'
                '</ZoneGroup></ZoneGroups>')
        soap = ('<s:Envelope><s:Body><u:GetZoneGroupStateResponse><ZoneGroupState>'
                + html.escape(html.escape(topo)) + '</ZoneGroupState></u:GetZoneGroupStateResponse></s:Body></s:Envelope>')

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=0: Resp(soap.encode())
        try:
            self.assertEqual(m._fetch_invisible_uuids('192.0.2.10'), frozenset({'RINCON_BBB', 'RINCON_DDD'}))
        finally:
            urllib.request.urlopen = original


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

    def test_range_start_parsing(self):
        m = load()
        self.assertEqual(m._range_start({'RANGE': 'bytes=208901-'}), 208901)
        self.assertEqual(m._range_start({'Range': 'bytes=0-'}), 0)
        self.assertEqual(m._range_start({'Range': 'bytes=5-9,20-'}), 5)
        self.assertIsNone(m._range_start({}))
        self.assertIsNone(m._range_start({'Range': 'items=1-2'}))
        self.assertIsNone(m._range_start({'Range': 'bytes=-500'}))   # suffix range: not a resume
        self.assertIsNone(m._range_start(None))

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
        self.assertEqual(actions, ['stop_track', 'stop'])
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


if __name__ == '__main__':
    unittest.main()
