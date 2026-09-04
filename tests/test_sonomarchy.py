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


class ArgvParsing(unittest.TestCase):
    def test_nics_forms(self):
        m = load()
        self.assertEqual(m._nics_from_argv(['x', '--nics', 'eno1,wlan0']), ['eno1', 'wlan0'])
        self.assertEqual(m._nics_from_argv(['x', '--nics=wlan0']), ['wlan0'])
        self.assertEqual(m._nics_from_argv(['x', '-n', 'wlan0']), ['wlan0'])
        self.assertIsNone(m._nics_from_argv(['x', '--port', '8080']))


if __name__ == '__main__':
    unittest.main()
