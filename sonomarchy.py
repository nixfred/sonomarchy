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

VERSION = '0.1.2'

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
def _range_start(headers):
    """Start byte of a `Range: bytes=N-` request header, else None.

    A speaker that lost the stream mid-way retries with a Range header asking
    to resume where it was (observed: `RANGE: bytes=208901-` on a Playbar).
    First fetches never carry one. Only that exact open-ended form is
    honoured: a bounded `N-M`, a suffix `-N` or a multi-range would be
    answered with an open-ended 206 we do not actually satisfy, so they are
    treated as a plain request instead.
    """
    try:
        value = headers.get('Range') or headers.get('RANGE') or ''
        value = value.strip().lower()
        if not value.startswith('bytes='):
            return None
        spec = value[6:].strip()
        if ',' in spec or not spec.endswith('-'):
            return None
        first = spec[:-1].strip()
        return int(first) if first.isdigit() else None
    except Exception:
        return None


def _prepare_request_state(renderer, headers):
    """Record what this GET asks for, before the track starts.

    A request without a Range is a fresh representation: the speaker's byte
    counter restarts at 0, so ours must too, or a later Range would be served
    bytes from the previous stream.
    """
    start = _range_start(headers or {})
    renderer._sonomarchy_range_start = start
    if start is None:
        _ring(renderer).reset()
    return start


def _http_ok_lines(mime_type, range_start=None):
    """Response header lines. 206 with a Content-Range when resuming."""
    if range_start is not None and 0 < range_start < FAKE_CONTENT_LENGTH:
        return ['HTTP/1.1 206 Partial Content',
                'Content-type: ' + mime_type,
                'Connection: close',
                'Accept-Ranges: bytes',
                f'Content-Range: bytes {range_start}-{FAKE_CONTENT_LENGTH - 1}'
                f'/{FAKE_CONTENT_LENGTH}',
                f'Content-Length: {FAKE_CONTENT_LENGTH - range_start}',
                '', '']
    return ['HTTP/1.1 200 OK',
            'Content-type: ' + mime_type,
            'Connection: close',
            'Accept-Ranges: bytes',
            f'Content-Length: {FAKE_CONTENT_LENGTH}',
            '', '']


# --- replay buffer: make a Range retry a REAL resume ------------------------
# A speaker that loses the connection asks for `Range: bytes=N-`, i.e. "carry
# on from where I was". Observed on a Play:1: answering 206 with fresh live
# data (which is not byte N) is rejected within 3 s with a reset. Keep the
# last RING_BYTES of what was actually sent; if N is still in that window,
# replay the missing bytes and continue live. Same bytes, same MP3 frames:
# the speaker resumes without a gap and without a reset.
RING_BYTES = 2 * 1024 * 1024        # ~65 s at 256 kbps


class _Ring:
    """Byte ring keyed by absolute stream offset."""

    def __init__(self, capacity=RING_BYTES):
        self.capacity = capacity
        self.chunks = []            # list of (start_offset, bytes)
        self.size = 0
        self.end = 0                # absolute offset of the next byte

    @property
    def start(self):
        return self.chunks[0][0] if self.chunks else self.end

    def reset(self):
        self.chunks, self.size, self.end = [], 0, 0

    def append(self, data):
        if not data:
            return
        self.chunks.append((self.end, data))
        self.size += len(data)
        self.end += len(data)
        while self.size > self.capacity and len(self.chunks) > 1:
            _, old = self.chunks.pop(0)
            self.size -= len(old)

    def since(self, offset):
        """Bytes from absolute `offset` to the end, or None if not buffered."""
        if offset < self.start or offset > self.end:
            return None
        out = []
        for start, data in self.chunks:
            if start + len(data) <= offset:
                continue
            out.append(data[max(0, offset - start):])
        return b''.join(out)


def _ring(renderer):
    ring = getattr(renderer, '_sonomarchy_ring', None)
    if ring is None:
        ring = renderer._sonomarchy_ring = _Ring()
    return ring


async def _write_http_ok(writer, renderer):
    # The request's Range (if any) is stashed on the renderer by the
    # connection handler; Track.run calls us without request context.
    range_start = getattr(renderer, '_sonomarchy_range_start', None)
    renderer._sonomarchy_range_start = None
    renderer._sonomarchy_replay = None
    if range_start is not None:
        ring = _ring(renderer)
        replay = ring.since(range_start)
        if replay is not None:
            renderer._sonomarchy_replay = (range_start, replay)
            logger.warning(f'{renderer.name}: speaker is resuming at byte '
                           f'{range_start}; replaying {len(replay)} buffered '
                           f'bytes then continuing live (206)')
        else:
            # Outside the window: we cannot honour it. Start a fresh
            # representation from byte 0 rather than lie about the offset.
            logger.warning(f'{renderer.name}: speaker asked to resume at byte '
                           f'{range_start} but only {ring.start}-{ring.end} is '
                           f'buffered; starting a fresh stream (200)')
            range_start = None
            ring.reset()
    query = _http_ok_lines(renderer.mime_type, range_start)
    writer.write('\r\n'.join(query).encode('latin-1'))
    await writer.drain()


async def _write_track(self, reader):
    """Copy encoder stdout to the socket with no chunk framing.

    Mirrors upstream Track.write_track minus the chunk headers, because the
    response we send is no longer Transfer-Encoding: chunked. Every byte sent
    is also recorded in the renderer's replay ring; a resume request first
    gets the buffered bytes it missed.
    """
    renderer = self.session.renderer
    ring = _ring(renderer)
    replay = getattr(renderer, '_sonomarchy_replay', None)
    renderer._sonomarchy_replay = None
    if replay is not None:
        _, missed = replay
        if missed:
            self.writer.write(missed)
            await self.writer.drain()
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
            ring.append(data)
            self.writer.write(data)
            await self.writer.drain()
        if not data or partial_data:
            logger.debug(f'EOF reading from pipe on {self.task_name}')
            # FIX 10a: an EOF that was not preceded by a deliberate stop means
            # the encoder died; clear the session so the speaker's retry is
            # served instead of answered 409.
            await _unlatch_after_eof(self)
            break


# `Track.run` resolves `write_http_ok` from the http_server module globals at
# call time, so rebinding the module attribute is enough.
_http_server.write_http_ok = _write_http_ok
_http_server.Track.write_track = _write_track

# A new playback (SetAVTransportURI) starts a new byte stream at 0: the
# speaker's offsets restart, so must ours.
_orig_set_avtransporturi = _pa_dlna.Renderer.set_avtransporturi


async def _set_avtransporturi(self, *args, **kwargs):
    _ring(self).reset()
    self._sonomarchy_replay = None
    return await _orig_set_avtransporturi(self, *args, **kwargs)


_pa_dlna.Renderer.set_avtransporturi = _set_avtransporturi


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
    if isinstance(action, _pa_dlna.MetaData):
        # A start is in flight from this moment, not from when Play returns:
        # the direct-control check, the wait for STOPPED and SetAVTransportURI
        # all come first, and the sweep must not double-start meanwhile.
        _mark_started(self)
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
                    # Wait for the player to actually reach STOPPED. A fixed
                    # 1 s was not enough on a Play:1: its own post-session
                    # transition fetched our URL while upstream's Play fetched
                    # it again, and the collision ended in a 409 and a reset.
                    if not await _wait_until_stopped(self):
                        logger.warning(f'{self.name}: still not STOPPED after '
                                       f'ending the direct-control session')
        except Exception as e:
            # Never let the workaround break normal playback.
            logger.debug(f'{self.name}: direct-control check skipped: {e!r}')

    return await _orig_handle_action(self, action)


_pa_dlna.Renderer.handle_action = _handle_action


# ===========================================================================
# FIX 8 - detect a firewall (or a VPN route) eating the stream
# ===========================================================================
_orig_play = _pa_dlna.Renderer.play

# Interfaces that can never carry the reply to a speaker on the LAN. If the
# route back to the speaker leaves through one of these, our SYN-ACK goes
# into a tunnel and the speaker never sees it: the symptom is identical to a
# firewall drop, so the two have to be told apart or the log sends people
# hunting the wrong one. Observed on 2026-09-05 on a laptop whose Tailscale
# had accepted a subnet route for the very LAN it was sitting on -- inbound
# SYNs arrived on ethernet, every SYN-ACK left via tailscale0 and died.
_TUNNEL_PREFIXES = ('tailscale', 'wg', 'tun', 'ppp', 'zt', 'utun', 'nebula')


def _reply_route(host, peer):
    """Name of the interface a reply from `host` to `peer` would leave by.

    Returns None when it cannot be determined; the caller must treat that as
    "no opinion" and fall back to the generic advice.
    """
    import subprocess
    try:
        out = subprocess.run(['ip', 'route', 'get', peer, 'from', host],
                             capture_output=True, text=True,
                             timeout=2).stdout.split()
    except Exception as e:
        logger.debug(f'reply route lookup failed: {e!r}')
        return None
    for i, word in enumerate(out):
        if word == 'dev' and i + 1 < len(out):
            return out[i + 1]
    return None


async def _firewall_probe(renderer):
    await asyncio.sleep(FIREWALL_GRACE)
    try:
        if renderer.nullsink is None:            # renderer closed meanwhile
            return
        if renderer.stream_sessions.is_playing:  # the speaker fetched it
            return
        port = renderer.control_point.port
        host = renderer.root_device.local_ipaddress
        peer = renderer.root_device.peer_ipaddress
        dev = _reply_route(host, peer)
        if dev and dev.startswith(_TUNNEL_PREFIXES):
            cause = (f'the reply to {peer} is routed out {dev}, which the '
                     f'speaker cannot be reached through -- a VPN is '
                     f'claiming your LAN subnet. Check `ip route get {peer} '
                     f'from {host}`; for Tailscale, a subnet router is '
                     f'advertising this LAN and `tailscale set '
                     f'--accept-routes=false` drops it')
        else:
            cause = (f'a firewall on this machine is the usual cause; a VPN '
                     f'claiming your LAN subnet is the next one -- check '
                     f'`ip route get {peer} from {host}` points at the '
                     f'interface facing the speaker')
        logger.warning(f'{renderer.name}: told to Play {FIREWALL_GRACE}s ago '
                       f'and never fetched the stream from TCP port {port}; '
                       f'{cause}')
        emit('firewall_suspected', port=port, zone=renderer.description,
             host=host, reply_dev=dev)
    except Exception as e:
        logger.debug(f'firewall probe skipped: {e!r}')


async def _play(self, *args, **kwargs):
    result = await _orig_play(self, *args, **kwargs)
    _mark_started(self)
    try:
        _keep(asyncio.get_running_loop().create_task(_firewall_probe(self)))
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
    _ensure_resume_loop(self)          # FIX 10c, needs the running loop
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
    _mark_started(renderer)
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
            # Per-zone recovery state must not outlive the renderer: a
            # rediscovered zone would inherit a takeover throttle or an idle
            # count it never earned.
            _idle_sweeps.pop(self.nullsink.sink.name, None)
        _last_takeover.pop(self.name, None)
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


# ===========================================================================
# FIX 9 - clean up sinks a previous backend left behind
# ===========================================================================
# `omarchy plugin update` makes the shell reload the plugin; the old backend is
# torn down before pa-dlna can unload its null-sink modules. A crash does the
# same. The leftovers then sit in the sound menu as duplicate, dead zones next
# to the live ones. pa-dlna's own single-instance check counts live PulseAudio
# clients, so it neither notices nor removes them.
#
# Only done when no pa-dlna client is alive: a running instance's sinks are
# in use, and it will refuse to let us start anyway.
def _stale_null_sink_modules(short_modules):
    """Indices of pa-dlna Sonos null-sinks in `pactl list short modules` text.

    Text on purpose: PipeWire's pactl reports every module's index as null in
    `-f json` output, while the tab-separated short listing has been stable
    across PulseAudio and PipeWire for years.
    """
    stale = []
    for line in (short_modules or '').splitlines():
        parts = line.split('\t')
        if len(parts) < 2 or parts[1] != 'module-null-sink':
            continue
        argument = parts[2] if len(parts) > 2 else ''
        if 'uuid:RINCON_' in argument and parts[0].strip().isdigit():
            stale.append(int(parts[0]))
    return stale


def _pa_dlna_client_alive(clients_text):
    """True if `pactl list clients` shows a client named pa-dlna."""
    return 'application.name = "pa-dlna"' in (clients_text or '')


def _unload_stale_sinks():
    import subprocess

    def pactl(*args):
        return subprocess.run(['pactl', *args], capture_output=True,
                              text=True, timeout=5).stdout or ''

    try:
        if _pa_dlna_client_alive(pactl('list', 'clients')):
            return 0
        stale = _stale_null_sink_modules(pactl('list', 'short', 'modules'))
        for index in stale:
            subprocess.run(['pactl', 'unload-module', str(index)],
                           capture_output=True, timeout=5)
        if stale:
            logger.warning(f'unloaded {len(stale)} stale Sonos null-sink(s) '
                           f'left behind by a previous backend')
            emit('cleanup', unloaded=len(stale))
        return len(stale)
    except Exception as e:
        logger.debug(f'stale sink cleanup skipped: {e!r}')
        return 0


# ===========================================================================
# FIX 10 - a stream that dies must be able to come back
# ===========================================================================
# Observed 2026-09-04, first in the wild after a Spotify Connect takeover and
# then reproduced on demand by killing the encoder mid-stream: the speaker
# sees the early EOF on a stream that promised 100 GB and does the right thing
# -- it retries the GET. pa-dlna's StreamSessions.is_playing is still True
# from the dead track (only stop_track/close_session clear it, and those run
# only on a PulseAudio 'remove' event), so the retry is answered 409, the
# speaker gives up, and because the application's sink-input never changed no
# event ever restarts the stream. Result: a sink that is RUNNING and silent,
# indefinitely, while the audio panel says everything is fine.
#
# Three layers, each proven separately:
#   a) unlatch  - an unexpected EOF on the encoder pipe clears the session, so
#                 the speaker's own retry is simply served (see _write_track);
#   b) takeover - a GET that arrives while a track is nominally running stops
#                 that track and serves the new connection instead of 409.
#                 Rate-limited per renderer so two connections cannot
#                 ping-pong;
#   c) resume   - every few seconds, a zone into which an application is still
#                 playing but that has no stream running is restarted.

TAKEOVER_MIN_INTERVAL = 3.0     # seconds between takeovers, per renderer
RESUME_INTERVAL = 8             # seconds between resume sweeps
_last_takeover = {}
_resume_started = False


async def _unlatch_after_eof(track):
    """Clear a session whose track ended without a deliberate stop.

    Runs INSIDE the track's own task, so it must not call
    StreamSessions.stop_track(): that calls Track.stop(), which cancels the
    track task -- i.e. cancels us, mid-cleanup, and leaves the session
    half-closed. Mirror stop_track() minus the self-cancel: the track is
    ending on its own anyway.
    """
    session = track.session
    try:
        if session.track is track and session.is_playing:
            logger.warning(f'{track.task_name}: stream ended unexpectedly; '
                           f'clearing the session so the speaker can '
                           f'reconnect')
            session.is_playing = False
            session.track = None
            if session.processes is not None:
                await session.processes.close_encoder()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f'{track.task_name}: unlatch after EOF failed: {e!r}')


def _should_takeover(name, now):
    last = _last_takeover.get(name, 0.0)
    if now - last < TAKEOVER_MIN_INTERVAL:
        return False
    _last_takeover[name] = now
    return True


async def _wait_until_stopped(renderer, timeout=5.0, step=0.5):
    """Poll the transport until it reports STOPPED; True if it did in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(step)
        try:
            state = await renderer.get_transport_state()
        except Exception:
            continue
        if state in ('STOPPED', 'NO_MEDIA_PRESENT'):
            return True
    return False


def _processes_alive(processes):
    """True if the parec -> encoder chain of a session is still running.

    L16 has no encoder process (raw PCM straight from parec); judge it on
    parec alone or every L16 zone would be called dead and restarted.
    """
    if processes is None:
        return False
    procs = [getattr(processes, 'parec_proc', None)]
    if not getattr(processes, 'no_encoder', False):
        procs.append(getattr(processes, 'encoder_proc', None))
    for proc in procs:
        if proc is None or proc.returncode is not None:
            return False
    return True


# --- b) takeover: upstream HTTPServer.client_connected with one branch changed
async def _client_connected(self, reader, writer):
    """Handle an HTTP GET request from a DLNA device.

    Mirrors pa-dlna 1.2 HTTPServer.client_connected (pinned in
    requirements.lock). The only change is the `is_playing` branch: instead
    of refusing a second connection with 409, hand the stream to it.
    """
    H = _http_server
    peername = writer.get_extra_info('peername')
    ip_source = peername[0]
    if ip_source not in self.allowed_ips:
        sockname = writer.get_extra_info('sockname')
        H.logger.warning(f'Discarded TCP connection from {ip_source} (not'
                         f' allowed) received on {sockname[0]}')
        writer.close()
        return

    do_close = True
    try:
        handler = H.HTTPRequestHandler(reader, writer, peername)
        await handler.set_rfile()
        handler.handle_one_request()

        if not hasattr(handler, 'path'):
            content = handler.rfile.getvalue().decode()
            request = content.splitlines()[0] if content else ''
            H.logger.error(f'Invalid path in HTTP request from {ip_source}:'
                           f' {request}')
            return

        uri_path = H.urllib.parse.unquote(handler.path)

        for renderer in self.control_point.renderers():
            if not renderer.match(uri_path):
                continue

            if handler.request_version != 'HTTP/1.1':
                handler.send_error(H.HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
                await renderer.disable_root_device()
                break
            if renderer.nullsink is None:
                handler.send_error(H.HTTPStatus.CONFLICT,
                                   f'{renderer.name} temporarily disabled')
                break

            # HEAD is answered from static knowledge and must never touch the
            # stream: a HEAD during playback used to fall into the takeover
            # branch below and silence the zone.
            if handler.command == 'HEAD':
                lines = _http_ok_lines(renderer.mime_type)
                writer.write('\r\n'.join(lines).encode('latin-1'))
                await writer.drain()
                return
            if handler.command != 'GET':
                handler.send_error(H.HTTPStatus.METHOD_NOT_ALLOWED)
                break

            if renderer.stream_sessions.is_playing:
                if _should_takeover(renderer.name, time.monotonic()):
                    logger.warning(f'{renderer.name}: new stream request '
                                   f'while a track is running; handing the '
                                   f'stream to the new connection')
                    await renderer.stream_sessions.stop_track()
                else:
                    handler.send_error(H.HTTPStatus.CONFLICT,
                                       f'Cannot start {renderer.name} stream'
                                       f' (already running)')
                    break

            # Resume (Range) or fresh representation (ring reset).
            _prepare_request_state(renderer,
                                   getattr(handler, 'headers', None))
            await renderer.start_track(writer)
            do_close = False
            return

        else:
            handler.send_error(H.HTTPStatus.NOT_FOUND,
                               'Cannot find a matching renderer')

        await writer.drain()

    finally:
        if do_close:
            try:
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass


_http_server.HTTPServer.client_connected = _client_connected


# --- d) a connection reset must not remove the zone
async def _track_run(self, reader):
    """Mirror of pa-dlna 1.2 Track.run with one change.

    Upstream answers a ConnectionError by closing the session AND the whole
    renderer: the null-sink is unloaded and the zone vanishes from the sound
    menu until the next discovery pass -- for a speaker that merely dropped
    one HTTP connection. Observed on a Playbar retrying a broken stream:
    reset after 2.4 s, "Closing renderer", zone gone. Close only the session;
    the renderer, its sink and the application's stream stay, and the
    speaker's retry or the resume sweep re-establishes the stream.
    """
    assert self.task is not None
    renderer = self.session.renderer
    try:
        await _http_server.write_http_ok(self.writer, renderer)
        _http_server.logger.debug(f'{self.task_name}: track is started')
        await self.write_track(reader)
        await self.shutdown()
    except asyncio.CancelledError:
        self.session.stream_tasks.create_task(self.shutdown(),
                                              name='shutdown')
    except ConnectionError as e:
        logger.warning(f'{self.task_name}: speaker dropped the connection '
                       f'({e!r}); keeping the zone, the stream will be '
                       f're-established')
        await self.session.close_session(shutdown_coro=True)
    except Exception:
        await self.session.close_session(shutdown_coro=True)
        raise


_wrap = getattr(_http_server, 'log_unhandled_exception', None)
_http_server.Track.run = (_wrap(_http_server.logger)(_track_run)
                          if callable(_wrap) else _track_run)


# --- e) no chunked terminator in a fixed-length body
async def _track_shutdown(self):
    """Mirror of pa-dlna 1.2 Track.shutdown with one line removed.

    Upstream ends every track by writing `0\\r\\n\\r\\n` -- the chunked-transfer
    terminator. Our responses carry a Content-Length instead (FIX 1), so those
    five bytes are audio payload to the speaker: a few corrupt bytes in the
    MP3, and, worse, five bytes the replay ring never saw, so a speaker that
    consumed them asks to resume five bytes past our window and is refused.
    """
    if self.writer is None:
        return
    writer = self.writer
    self.writer = None
    try:
        try:
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except ConnectionError:
            pass
        _http_server.logger.debug(f'{self.task_name}: track is stopped')
    except asyncio.CancelledError:
        _http_server.logger.debug(f'{self.task_name}: Got CancelledError at '
                                  f'Track shutdown')


_http_server.Track.shutdown = (_wrap(_http_server.logger)(_track_shutdown)
                               if callable(_wrap) else _track_shutdown)


# --- c) resume sweep
def _pactl_inputs_by_sink():
    """{sink_name: [application names]} for uncorked sink-inputs, via pactl."""
    import subprocess

    def pactl(*args):
        return json.loads(subprocess.run(['pactl', '-f', 'json', 'list', *args],
                                         capture_output=True, text=True,
                                         timeout=5).stdout or '[]')

    sinks = {s.get('index'): s.get('name') for s in pactl('sinks')}
    result = {}
    for si in pactl('sink-inputs'):
        if si.get('corked'):
            continue
        name = sinks.get(si.get('sink'))
        if name:
            app = (si.get('properties') or {}).get('application.name') or 'audio'
            result.setdefault(name, []).append(app)
    return result


_ACTIVE_STATES = ('PLAYING', 'TRANSITIONING')
# Seconds after a zone registers or is told to Play during which the sweep
# leaves it alone: the normal start sequence (SetAVTransportURI, Play, the
# speaker's GET) takes a few seconds, and a sweep landing inside it would
# issue a second, redundant restart -- an audible hiccup for nothing.
RESUME_GRACE = 15


def _mark_started(renderer):
    try:
        renderer._sonomarchy_started_at = time.monotonic()
    except Exception:
        pass


def _in_start_grace(renderer, now=None):
    started = getattr(renderer, '_sonomarchy_started_at', None)
    if started is None:
        return False
    return (time.monotonic() if now is None else now) - started < RESUME_GRACE
# A zone whose session is alive but whose sink has had no player for this many
# consecutive sweeps is torn down. Two sweeps (16 s) outlasts the 10 s
# track-change grace, so a player between songs is never cut off.
STALE_SWEEPS = 2
_idle_sweeps = {}


async def _adopt_sink_input(renderer):
    """Point pa-dlna at the sink-input that owns the stream we are restarting.

    Upstream sets nullsink.sink_input from the pulse event that starts a
    session. A sweep-started session has no such event, so without this the
    later 'remove' event for that application is not recognised as ours and
    the stream keeps running after the player stops -- observed as a live
    parec/encoder pair on a STOPPED zone with nothing playing into it.
    """
    try:
        sink_index = renderer.nullsink.sink.index
        lib_pulse = renderer.control_point.pulse.lib_pulse
        for sink_input in await lib_pulse.pa_context_get_sink_input_info_list():
            if getattr(sink_input, 'sink', None) == sink_index:
                renderer.nullsink.sink_input = sink_input
                return sink_input
    except Exception as e:
        logger.debug(f'{renderer.name}: could not adopt a sink-input: {e!r}')
    return None


async def _maybe_resume(renderer, inputs_by_sink):
    if renderer.nullsink is None or getattr(renderer, 'closing', False):
        return
    sink_name = renderer.nullsink.sink.name
    apps = inputs_by_sink.get(sink_name)
    sessions = renderer.stream_sessions
    session_alive = sessions.is_playing and _processes_alive(sessions.processes)

    if not apps:
        # Nobody is playing into it. A session still running here is stale
        # (its player went away without pa-dlna noticing) -- give it two
        # sweeps in case it is just a gap between tracks, then tear it down.
        # "alive" here means anything still running: stop_track() keeps parec
        # by upstream design, so a residual parec with is_playing False is
        # exactly the leak this branch must catch.
        residual = session_alive or _processes_alive(sessions.processes)
        if residual:
            _idle_sweeps[sink_name] = _idle_sweeps.get(sink_name, 0) + 1
            if _idle_sweeps[sink_name] >= STALE_SWEEPS:
                logger.warning(f'{renderer.name}: stream still running with '
                               f'nothing playing into the zone for '
                               f'{STALE_SWEEPS} sweeps; stopping it')
                _idle_sweeps.pop(sink_name, None)
                # close_session, not stop_track: the whole chain goes.
                await sessions.close_session()
                try:
                    await renderer.stop()
                except Exception:
                    pass
        else:
            _idle_sweeps.pop(sink_name, None)
        return
    _idle_sweeps.pop(sink_name, None)
    if _in_start_grace(renderer):
        return                                   # a start is in flight

    # The speaker is the truth about whether audio is actually playing: a
    # session can look alive on our side while the zone reads STOPPED.
    state = await renderer.get_transport_state()
    if session_alive and state in _ACTIVE_STATES:
        return                                   # healthy

    logger.warning(f'{renderer.name}: {apps[0]} is playing into the zone but '
                   f'the stream is not (session '
                   f'{"alive" if session_alive else "dead"}, transport '
                   f'{state}); restarting the stream')
    await sessions.stop_track()
    await _adopt_sink_input(renderer)
    # Always send an explicit Stop, even when the transport already reads
    # STOPPED: after a broken stream the player will not re-fetch the same
    # URL on a bare Play, but it does after Stop -> SetAVTransportURI -> Play,
    # which is exactly the sequence the manual "bounce" workaround produces.
    try:
        await renderer.stop()
    except Exception as e:
        logger.debug(f'{renderer.name}: Stop before resume: {e!r}')
    await _wait_until_stopped(renderer, timeout=3.0)
    sink_input = renderer.nullsink.sink_input
    meta = renderer.sink_input_meta(sink_input) if sink_input is not None \
        else _pa_dlna.MetaData(apps[0], '', apps[0])
    emit('resumed', zone=renderer.description, app=apps[0])
    await renderer.handle_action(meta)


async def _resume_loop(control_point):
    while True:
        await asyncio.sleep(RESUME_INTERVAL)
        try:
            loop = asyncio.get_running_loop()
            inputs = await loop.run_in_executor(None, _pactl_inputs_by_sink)
            renderers = list(control_point.renderers())
            # Report only when there is something to look at: zones that have
            # a player but no live stream. Silent sweeps stay silent.
            candidates = []
            for r in renderers:
                try:
                    if (r.nullsink is not None
                            and inputs.get(r.nullsink.sink.name)
                            and not (r.stream_sessions.is_playing
                                     and _processes_alive(
                                         r.stream_sessions.processes))):
                        candidates.append(r.description)
                except Exception as e:
                    candidates.append(f'{getattr(r, "name", "?")}: {e!r}')
            if candidates:
                emit('sweep',
                     zones=sum(1 for r in renderers if r.nullsink is not None),
                     inputs=len(inputs), candidates=candidates)
            for renderer in renderers:
                try:
                    await _maybe_resume(renderer, inputs)
                except Exception as e:
                    # WARNING on purpose: a silent failure here is exactly
                    # the "sink is running but nothing plays" bug this
                    # sweep exists to catch, so it must be visible.
                    logger.warning(f'resume check for {renderer.name} '
                                   f'skipped: {e!r}')
        except Exception as e:
            logger.warning(f'resume sweep skipped: {e!r}')


# asyncio keeps only WEAK references to tasks. A task whose handle is dropped
# can be garbage-collected while still pending -- which is exactly how the
# first version of this sweep silently never ran. Hold the handles.
_resume_task = None
_background_tasks = set()


def _keep(task):
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _ensure_resume_loop(control_point):
    """Start the sweep on pa-dlna's own event loop, once."""
    global _resume_started, _resume_task
    if _resume_started:
        return
    try:
        _resume_task = _keep(asyncio.get_running_loop().create_task(
            _resume_loop(control_point), name='sonomarchy-resume'))
        _resume_started = True
        # WARNING so it reaches the shell journal once per backend start;
        # the shell only forwards severe lines.
        logger.warning('resume sweep started')
    except Exception as e:
        logger.warning(f'resume loop not started: {e!r}')


def main(argv=None):
    argv = sys.argv if argv is None else argv
    emit('starting', version=VERSION)
    _unload_stale_sinks()
    threading.Thread(target=_watch_addresses,
                     args=(_nics_from_argv(argv),),
                     name='address-watch',
                     daemon=True).start()
    return _pa_dlna.main()


if __name__ == '__main__':
    sys.exit(main())
