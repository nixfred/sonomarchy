#!/usr/bin/env python
"""Sonomarchy backend: pa-dlna, made to work with real Sonos hardware.

Run by `sonomarchy-backend`; all CLI args are pa-dlna's and are passed through.
Status is reported to the shell as one JSON object per line on stdout; pa-dlna's
own logging stays on stderr.

pa-dlna (https://gitlab.com/xdegaye/pa-dlna) forwards a PipeWire/PulseAudio
stream to any DLNA renderer by creating a null-sink per device and serving the
sink's monitor as an HTTP audio stream. It is excellent and it is generic. Sonos
players, especially the pre-AirPlay generation (Play:1, Play:3, Play:5 gen 1,
Playbar, Connect), break it in several specific ways. Every patch below is a
workaround for behaviour observed on that hardware, and each one says what was
seen. Nothing here modifies pa-dlna on disk: a dependency upgrade cannot quietly
undo a fix.

--------------------------------------------------------------------------
FIX 1 - legacy Sonos cannot do HTTP chunked transfer
--------------------------------------------------------------------------
pa-dlna answers the stream GET with `Transfer-Encoding: chunked`. A legacy Sonos
reacts by opening a SECOND parallel GET ~18 ms after the first; pa-dlna only
supports one session per renderer so it answers 409, and the Sonos then resets
the original connection. Observed:

    GET  from the speaker  -> track is started
    GET  from the speaker  -> 409 "stream already running"   (+18 ms)
    ConnectionResetError   -> track is stopped                (+2 s)

Symptom: the sink exists and is selectable but no audio plays, or it dies a
couple of seconds in. `pulseaudio-dlna` hit the same wall and shipped
`--fake-http-content-length` for it, with the flag tied to its Play:1 and Play:3
compatibility rows. pa-dlna has no equivalent option.

Fix: advertise a huge Content-Length and write the body unframed. After this:
zero 409s, zero resets, exactly one GET.

--------------------------------------------------------------------------
FIX 2 - stale stream URL after any network change  (the nastiest one)
--------------------------------------------------------------------------
pa-dlna builds each renderer's stream URL from the host IP that was current
when the device was discovered and never revisits it. Move a laptop between
wifi and ethernet (dock, undock, roam) and every sink keeps advertising the
dead address. Playback goes TRANSITIONING -> STOPPED and silently produces
nothing, while the sinks are still listed and nothing logs an error.

Fix: watch the IPv4 addresses of the interfaces we discover on. If an address
we were serving on disappears, report it and exit; the shell restarts the
backend into a fresh, correct discovery pass.

--------------------------------------------------------------------------
FIX 3 - a zone parked in a Spotify Connect session refuses to stream
--------------------------------------------------------------------------
pa-dlna only sends SetAVTransportURI when the renderer's transport is STOPPED
or NO_MEDIA_PRESENT. A Sonos left in a Spotify Connect ("virtual line-in")
session sits in PAUSED_PLAYBACK with an `x-sonos-vli:` URI indefinitely, so
selecting the sink does nothing at all and logs nothing.

Fix: when about to start a stream on a zone held by a `x-sonos-vli:` session,
call Sonos' `EndDirectControlSession` first; it drops to STOPPED and the
normal path proceeds.

--------------------------------------------------------------------------
FIX 4 - the stream is torn down at every track change
--------------------------------------------------------------------------
On a track change a player's PulseAudio sink-input is destroyed and a new one
appears. pa-dlna waits `ISSUE_48_TIMER` seconds for the replacement; if the gap
is longer it tears the stream down, the Sonos re-fetches and rebuffers, and you
hear a skip between every song. Upstream's 2 s is too tight for real players.
Raised to 10 s; the cost is that a Sonos holds the stream ~10 s longer after
playback genuinely ends.

--------------------------------------------------------------------------
FIX 5 - bonded satellites must not become sinks
--------------------------------------------------------------------------
A stereo pair's second speaker, surrounds and a Sub are "bonded": part of
another zone and not independently playable. Most expose a crippled
MediaRenderer that pa-dlna disables on its own -- but not all. The ones that
slip through appear as a duplicate room in the sound menu that silently does
nothing. ZoneGroupTopology marks them Invisible="1"; ask once, cache, skip.
Note the embedded MediaRenderer UDN carries a role suffix (`..._MR`) that the
topology does not use; compare the bare player id or nothing ever matches.

--------------------------------------------------------------------------
FIX 6 - a readable name in the sound menu
--------------------------------------------------------------------------
Upstream labels the sink "<room> - <model> Media Renderer - <udn fragment>".
Recover "<room> (<model>)".

--------------------------------------------------------------------------
FIX 7 - track metadata must stay off
--------------------------------------------------------------------------
Sonos rejects `SetNextAVTransportURI` on a live stream with SOAP fault 800 and
pa-dlna then closes the renderer. pa-dlna only sends it on the track-metadata
path, so force `track_metadata` off in code rather than relying on a user
config file. Cost: no track title on the Sonos app; audio is unaffected.

--------------------------------------------------------------------------
FIX 8 - say so when a firewall is eating the stream
--------------------------------------------------------------------------
The speaker pulls the audio FROM this machine. A default-deny firewall (ufw on
a stock install) drops that connection and the result is total silence with no
error anywhere. If a speaker was told to Play and never fetched the stream
within a few seconds, report `firewall_suspected` so the shell can tell the
user which port to open.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time

from pa_dlna import pa_dlna as _pa_dlna
from pa_dlna import http_server as _http_server

VERSION = '0.1.0'

logger = logging.getLogger('sonomarchy')

# 100 GB. pulseaudio-dlna uses the same value: at 256 kbps that is ~36 years of
# audio, so a stream never reaches the advertised length.
FAKE_CONTENT_LENGTH = 100 * 1024 ** 3

# Seconds between interface checks (FIX 2).
IP_POLL_INTERVAL = 10

# Seconds pa-dlna waits for a replacement sink-input at a track change (FIX 4).
TRACK_CHANGE_GRACE = 10

# Seconds a speaker gets to fetch the stream after Play before we suspect a
# firewall (FIX 8). A healthy speaker fetches within ~100 ms.
FIREWALL_GRACE = 8


def emit(type_, **fields):
    """One JSON object per line on stdout, for Service.qml."""
    fields['type'] = type_
    try:
        sys.stdout.write(json.dumps(fields) + '\n')
        sys.stdout.flush()
    except Exception:
        pass


# ===========================================================================
# FIX 4 - do not tear the stream down across a track change
# ===========================================================================
_pa_dlna.ISSUE_48_TIMER = TRACK_CHANGE_GRACE


# ===========================================================================
# FIX 1 - Content-Length instead of chunked, and an unframed body
# ===========================================================================
async def _write_http_ok(writer, renderer):
    query = ['HTTP/1.1 200 OK',
             'Content-type: ' + renderer.mime_type,
             'Connection: close',
             f'Content-Length: {FAKE_CONTENT_LENGTH}',
             '', '']
    writer.write('\r\n'.join(query).encode('latin-1'))
    await writer.drain()


async def _write_track(self, reader):
    """Copy encoder stdout to the socket with no chunk framing.

    Mirrors upstream Track.write_track minus the chunk headers, because the
    response we send is no longer Transfer-Encoding: chunked.
    """
    while True:
        partial_data = False
        if self.writer.is_closing():
            logger.debug(f'{self.task_name}: socket is closing')
            break
        try:
            data = await reader.readexactly(_http_server.HTTP_CHUNK_SIZE)
        except asyncio.IncompleteReadError as e:
            data = e.partial
            partial_data = True
        if data:
            self.writer.write(data)
            await self.writer.drain()
        if not data or partial_data:
            logger.debug(f'EOF reading from pipe on {self.task_name}')
            break


# `Track.run` resolves `write_http_ok` from the http_server module globals at
# call time, so rebinding the module attribute is enough.
_http_server.write_http_ok = _write_http_ok
_http_server.Track.write_track = _write_track


# ===========================================================================
# FIX 7 - track metadata off, regardless of any user config file
# ===========================================================================
_orig_select_encoder = _pa_dlna.select_encoder


def _select_encoder(*args, **kwargs):
    result = _orig_select_encoder(*args, **kwargs)
    try:
        encoder = result[0] if isinstance(result, tuple) else result
        if encoder is not None:
            encoder.track_metadata = False
    except Exception:
        pass
    return result


_pa_dlna.select_encoder = _select_encoder


# ===========================================================================
# Sonos identification helpers
# ===========================================================================
def _sonos_uuid(renderer):
    """Bare player id ("RINCON_...") for a Sonos renderer, else ''."""
    import re
    udn = getattr(getattr(renderer, 'upnp_device', None), 'UDN', '') or ''
    m = re.search(r'(RINCON_[0-9A-Fa-f]+)', udn)
    return m.group(1) if m else ''


def _is_sonos(renderer):
    return bool(_sonos_uuid(renderer))


# ===========================================================================
# FIX 3 - break a Spotify Connect hold before starting a stream
# ===========================================================================
_orig_handle_action = _pa_dlna.Renderer.handle_action


async def _handle_action(self, action):
    # Only Sonos has direct-control sessions; skip the two extra SOAP round
    # trips per pulse event on every other brand of renderer.
    if isinstance(action, _pa_dlna.MetaData) and _is_sonos(self):
        try:
            state = await self.get_transport_state()
            if state not in ('STOPPED', 'NO_MEDIA_PRESENT'):
                info = await self.soap_action(_pa_dlna.AVTRANSPORT,
                                              'GetMediaInfo',
                                              {'InstanceID': 0})
                uri = (info or {}).get('CurrentURI') or ''
                if uri.startswith('x-sonos-vli:'):
                    logger.warning(
                        f'{self.name}: zone is held by a Sonos direct-control '
                        f'session; ending it so the stream can start')
                    await self.soap_action(_pa_dlna.AVTRANSPORT,
                                           'EndDirectControlSession',
                                           {'InstanceID': 0})
                    # Give the player a moment to settle into STOPPED before
                    # upstream re-reads the transport state.
                    await asyncio.sleep(1)
        except Exception as e:
            # Never let the workaround break normal playback.
            logger.debug(f'{self.name}: direct-control check skipped: {e!r}')

    return await _orig_handle_action(self, action)


_pa_dlna.Renderer.handle_action = _handle_action


# ===========================================================================
# FIX 8 - detect a firewall eating the stream
# ===========================================================================
_orig_play = _pa_dlna.Renderer.play


async def _firewall_probe(renderer):
    await asyncio.sleep(FIREWALL_GRACE)
    try:
        if renderer.nullsink is None:            # renderer closed meanwhile
            return
        if renderer.stream_sessions.is_playing:  # the speaker fetched it
            return
        port = renderer.control_point.port
        logger.warning(f'{renderer.name}: told to Play {FIREWALL_GRACE}s ago '
                       f'and never fetched the stream from TCP port {port}; '
                       f'a firewall on this machine is the usual cause')
        emit('firewall_suspected', port=port, zone=renderer.description,
             host=renderer.root_device.local_ipaddress)
    except Exception as e:
        logger.debug(f'firewall probe skipped: {e!r}')


async def _play(self, *args, **kwargs):
    result = await _orig_play(self, *args, **kwargs)
    try:
        asyncio.get_running_loop().create_task(_firewall_probe(self))
    except Exception:
        pass
    return result


_pa_dlna.Renderer.play = _play


# ===========================================================================
# FIX 2 - exit when an interface we are using loses its address
# ===========================================================================
def _nics_from_argv(argv):
    """Return the interface names given to --nics / -n, or None for 'all'."""
    for i, arg in enumerate(argv):
        if arg in ('--nics', '-n') and i + 1 < len(argv):
            return [n for n in argv[i + 1].split(',') if n]
        if arg.startswith('--nics='):
            return [n for n in arg.split('=', 1)[1].split(',') if n]
    return None


def _current_ipv4(nics):
    """IPv4 addresses currently configured on the interfaces we care about."""
    import psutil
    import socket

    addrs = psutil.net_if_addrs()
    found = set()
    for name, snics in addrs.items():
        if nics is not None and name not in nics:
            continue
        if nics is None and (name == 'lo' or name.startswith(
                ('docker', 'virbr', 'incusbr', 'br-', 'tailscale', 'veth'))):
            continue
        for snic in snics:
            if snic.family != socket.AF_INET or not snic.address:
                continue
            # A link-local 169.254.x.x can appear for a moment when a cable is
            # plugged in before DHCP answers. Speakers are never reachable on
            # it, and treating its disappearance as "address lost" would
            # trigger a pointless restart right as the real address arrives.
            if snic.address.startswith('169.254.'):
                continue
            found.add(snic.address)
    return found


def _watch_addresses(nics):
    """Exit (SIGTERM to self) when an address we were serving on goes away.

    New addresses appearing are fine -- pa-dlna picks those up on its own
    ("Start UPnP discovery on new IPs"). It is an address *disappearing* that
    strands every renderer on a dead stream URL.
    """
    # Snapshot BEFORE the first sleep. pa-dlna binds its addresses within the
    # first second; if one of them vanished during the initial poll interval
    # an empty baseline would never notice.
    try:
        seen = set(_current_ipv4(nics))
    except Exception as e:
        logger.debug(f'address watch could not take a baseline: {e!r}')
        seen = set()
    while True:
        time.sleep(IP_POLL_INTERVAL)
        # The whole body is guarded: this thread is a safety net, and a safety
        # net that dies silently on an unexpected value is worse than none.
        try:
            now = _current_ipv4(nics)
            if not isinstance(now, set):
                raise TypeError(f'_current_ipv4 returned {type(now).__name__}')

            lost = seen - now
            if lost:
                logger.warning(
                    f'address(es) {sorted(lost)} disappeared from '
                    f'{nics if nics else "the monitored interfaces"}; every '
                    f'stream URL still points at them. Exiting so the shell '
                    f'restarts discovery.')
                emit('restart', reason='address_lost', lost=sorted(lost))
                os.kill(os.getpid(), signal.SIGTERM)
                return
            seen |= now
        except Exception as e:
            logger.debug(f'address watch skipped a poll: {e!r}')


# ===========================================================================
# FIX 5 - do not create sinks for bonded satellites
# ===========================================================================
_ZGT_TTL = 300          # seconds; re-ask so re-bonding is picked up
_zgt_cache = {'uuids': frozenset(), 'ts': 0.0, 'ok': False}
# Every renderer registers at once on startup. Without a lock they all miss the
# empty cache and fire simultaneous topology requests at one speaker.
_zgt_lock = None

_ZGT_BODY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    '<u:GetZoneGroupState'
    ' xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
    '</u:GetZoneGroupState></s:Body></s:Envelope>').encode()


def _fetch_invisible_uuids(ip):
    """UUIDs of Sonos players that are bonded into another zone."""
    import html
    import re
    import urllib.request

    req = urllib.request.Request(
        f'http://{ip}:1400/ZoneGroupTopology/Control', data=_ZGT_BODY,
        headers={'Content-Type': 'text/xml; charset="utf-8"',
                 'SOAPACTION': '"urn:schemas-upnp-org:service:'
                               'ZoneGroupTopology:1#GetZoneGroupState"'})
    with urllib.request.urlopen(req, timeout=6) as resp:
        xml = resp.read().decode('utf-8', 'replace')

    # The topology arrives double-escaped inside the SOAP envelope.
    xml = html.unescape(html.unescape(xml))

    invisible = set()
    for tag in re.findall(r'<(?:ZoneGroupMember|Satellite)\b[^>]*/?>', xml):
        uuid = re.search(r'\bUUID="([^"]+)"', tag)
        if uuid and re.search(r'\bInvisible="1"', tag):
            invisible.add(uuid.group(1))
    return frozenset(invisible)


def _cache_fresh(now):
    return _zgt_cache['ok'] and now - _zgt_cache['ts'] < _ZGT_TTL


async def _invisible_uuids(ip):
    global _zgt_lock
    if _cache_fresh(time.time()):
        return _zgt_cache['uuids']
    if _zgt_lock is None:
        _zgt_lock = asyncio.Lock()
    async with _zgt_lock:
        now = time.time()
        # Re-check: whoever held the lock before us probably filled it.
        if _cache_fresh(now):
            return _zgt_cache['uuids']
        try:
            loop = asyncio.get_running_loop()
            uuids = await loop.run_in_executor(None, _fetch_invisible_uuids, ip)
            _zgt_cache.update(uuids=uuids, ts=now, ok=True)
            return uuids
        except Exception as e:
            # Fail open: if we cannot ask, show every renderer rather than
            # hide a speaker the user actually wanted. Back off for a TTL so
            # an unreachable speaker is not hammered.
            logger.debug(f'could not read Sonos zone topology from {ip}: '
                         f'{e!r}')
            _zgt_cache.update(ts=now, ok=True)
            return _zgt_cache['uuids']


_orig_register = _pa_dlna.AVControlPoint.register


async def _register(self, renderer):
    uuid = _sonos_uuid(renderer)
    if uuid:
        try:
            if uuid in await _invisible_uuids(renderer.root_device.peer_ipaddress):
                logger.info(f'skipping {uuid}: bonded into another Sonos zone '
                            f'(surround, stereo pair partner or Sub), not '
                            f'independently selectable')
                return
        except Exception as e:
            logger.debug(f'bonded-satellite check skipped: {e!r}')
    result = await _orig_register(self, renderer)
    try:
        if renderer.nullsink is not None:
            emit('zone', uuid=uuid or renderer.upnp_device.UDN,
                 name=renderer.description, sink=renderer.nullsink.sink.name)
    except Exception:
        pass
    return result


_pa_dlna.AVControlPoint.register = _register

_orig_close = _pa_dlna.Renderer.close


async def _close(self, *args, **kwargs):
    try:
        if self.nullsink is not None:
            emit('zone_gone', uuid=_sonos_uuid(self) or self.upnp_device.UDN)
    except Exception:
        pass
    return await _orig_close(self, *args, **kwargs)


_pa_dlna.Renderer.close = _close


# ===========================================================================
# FIX 6 - a readable name in the sound menu
# ===========================================================================
_SONOS_FRIENDLY = None


def _prettify(description):
    """'Kitchen - Sonos Play:1 Media Renderer - RINCO...' -> 'Kitchen (Sonos Play:1)'."""
    global _SONOS_FRIENDLY
    if _SONOS_FRIENDLY is None:
        import re
        _SONOS_FRIENDLY = re.compile(
            r'^(?P<room>.+?) - (?P<model>Sonos .+?) Media Renderer\b')
    m = _SONOS_FRIENDLY.match(description)
    return f'{m.group("room")} ({m.group("model")})' if m else description


_orig_renderer_init = _pa_dlna.Renderer.__init__


def _renderer_init(self, *args, **kwargs):
    _orig_renderer_init(self, *args, **kwargs)
    # Only `description` drives the PulseAudio device.description shown in the
    # UI. `name` is left alone: it keys encoder config lookups.
    try:
        self.description = _prettify(self.description)
    except Exception:
        pass


_pa_dlna.Renderer.__init__ = _renderer_init


def main(argv=None):
    argv = sys.argv if argv is None else argv
    emit('starting', version=VERSION)
    threading.Thread(target=_watch_addresses,
                     args=(_nics_from_argv(argv),),
                     name='address-watch',
                     daemon=True).start()
    return _pa_dlna.main()


if __name__ == '__main__':
    sys.exit(main())
