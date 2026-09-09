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
See also FIX 13, which reads the same topology for grouping.

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
import atexit
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pa_dlna import pa_dlna as _pa_dlna
from pa_dlna import http_server as _http_server
from pa_dlna import pulseaudio as _pulseaudio

VERSION = '1.5.3'   # kept equal to manifest.json by the validator

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

# Seconds between Sonos grouping checks (FIX 13).
GROUP_POLL_INTERVAL = 15

# Grouping-driven rebuilds allowed inside GROUP_RESTART_WINDOW seconds
# (FIX 13). A burst rather than a flat cooldown: a regroup legitimately needs
# a second rebuild now and then, when the first one sampled the topology while
# Sonos was still moving rooms between groups. What must be stopped is the
# unbounded case, not the second one.
# Five in five minutes: a person regrouping rooms in the Sonos app can
# easily make three or four changes in a couple of minutes and every one of
# them must be honoured, so the budget has to sit above human fiddling. A
# speaker flapping on bad wifi regroups every few seconds and is still capped,
# which is the only case this exists to stop.
GROUP_RESTART_WINDOW = 300
GROUP_RESTART_BURST = 5


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
# FIX 12 - a short-lived sink-input must not tear down the real one
# ===========================================================================
# Upstream decides a zone has gone idle from one pointer, nullsink.sink_input,
# which follows whichever sink-input last raised a pulse event. Any brief
# stream -- a notification, a UI sound, a player reopening its PCM -- captures
# that pointer just by existing. When the brief stream ends, `maybe_stop` sees
# the pointer still naming it, concludes the zone is idle, and closes the
# encoder out from under a completely different stream that never stopped.
#
# Observed 2026-09-06 with a player running for an hour on sink-input 8210:
#
#   23:58:12  'change' pulse event  [sink-input index 10570]   <- transient
#   23:58:31  'remove' pulse event  [sink-input index 10570]
#   23:58:41  'Closing-Stop'                                   <- 8210 killed
#   23:58:48  sweep: "cliamp is playing into the zone but the stream is not"
#
# The music stopped for ~17 s per occurrence and the sweep put it back, so the
# symptom is a stutter every few minutes rather than an outright failure.
# Raising TRACK_CHANGE_GRACE cannot fix it: no further event is coming for
# 8210, so a longer wait only makes the gap longer.
#
# The zone is busy if ANYTHING is still playing into it, so ask the sink
# rather than trusting the pointer.
_orig_maybe_stop = _pa_dlna.Renderer.maybe_stop


async def _maybe_stop(self, index, state):
    await asyncio.sleep(TRACK_CHANGE_GRACE)
    if self.nullsink is None:                    # renderer closed meanwhile
        return
    try:
        cur_index = self.get_sink_input_index()
        if cur_index is not None and cur_index != index:
            # Upstream's own check: a replacement announced itself in time.
            self.log_pulse_event('remove ignored', self.nullsink.sink_input)
            return

        adopted = await _adopt_sink_input(self)
        if adopted is not None and adopted.index != index:
            self.log_pulse_event('remove ignored', adopted)
            return

        self.nullsink.sink_input = None
        _pa_dlna.log_action(self.name, 'Closing-Stop', state)
        await self.stream_sessions.close_session()
        _pa_dlna.log_action(self.name, 'Stop', state)
        try:
            await self.stop()
        except Exception:
            pass
    except Exception as e:
        # A failure here must not leave the stream running forever: fall back
        # to upstream's behaviour rather than silently keeping the encoder.
        logger.warning(f'{self.name}: idle check failed ({e!r}); '
                       f'closing the session')
        try:
            self.nullsink.sink_input = None
            await self.stream_sessions.close_session()
        except Exception:
            pass


_pa_dlna.Renderer.maybe_stop = _maybe_stop


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


# ===========================================================================
# FIX 17 - some firmware refuses the fake-Content-Length response
# ===========================================================================
# The response below advertises a 100 GiB Content-Length and Accept-Ranges so
# that a speaker which loses the socket can resume with a Range request (see
# the replay ring). Sonos firmware 86.8 (ZPS9: Playbar, Play:1) accepts that
# happily. Firmware 92.0 (ZPS17: Move) does not -- it reads the headers, takes
# ~10 KB, and resets the connection after 2.4 s, every time, forever.
#
# Upstream pa-dlna answers `Transfer-Encoding: chunked` with no length, which
# is what a live stream honestly is. That works on the new firmware but gives
# up Range resumes, so it is not the right answer for every speaker either.
# The framing is therefore per renderer: chunked for the ones that need it,
# length-plus-ranges for the rest.
def _use_chunked(renderer):
    # Resolved once per renderer: _sonos_uuid walks the UPnP device each call
    # and this runs on every response.
    flag = getattr(renderer, '_sonomarchy_chunked', None)
    if flag is None:
        uuid = _sonos_uuid(renderer)
        flag = bool(uuid) and uuid in _chunked_uuids()
        renderer._sonomarchy_chunked = flag
        if flag:
            logger.warning(f'{renderer.name}: pinned to chunked transfer '
                           f'encoding; Range resumes are not available for '
                           f'this speaker')
    return flag


def _chunked_uuids():
    """UUIDs pinned to chunked framing by the user, one per line.

    A support switch and the way the behaviour is tested:
    `echo RINCON_x > ~/.local/state/io.github.nixfred.sonomarchy/chunked`
    """
    try:
        with open(os.path.join(_state_dir(), 'chunked')) as pinned:
            return {line.strip() for line in pinned if line.strip()}
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.debug(f'chunked pin list unreadable: {e!r}')
        return set()


# A speaker refusing the response looks distinctive: it takes a few KB, waits,
# and resets. A listener who simply stopped the music produces one drop, not a
# run of them -- so two consecutive short drops is enough to act on, and the
# decision is written to the pin file so the next start does not repeat the
# 35 seconds of silence it costs to learn.
CHUNKED_SHORT_BYTES = 64 * 1024
CHUNKED_SHORT_SECONDS = 5.0
CHUNKED_STRIKES = 2


def _pin_chunked(uuid):
    """Remember that this player needs chunked framing, across restarts."""
    if not uuid:
        return
    try:
        pinned = _chunked_uuids()
        if uuid in pinned:
            return
        os.makedirs(_state_dir(), exist_ok=True)
        with open(os.path.join(_state_dir(), 'chunked'), 'w') as out:
            out.write('\n'.join(sorted(pinned | {uuid})) + '\n')
    except Exception as e:
        logger.debug(f'could not pin {uuid} to chunked: {e!r}')


def _note_short_drop(renderer, sent, elapsed):
    """Switch a renderer to chunked framing after repeated tiny drops.

    Returns True when this drop caused the switch, for the tests.
    """
    if _use_chunked(renderer):
        return False
    if sent >= CHUNKED_SHORT_BYTES or elapsed >= CHUNKED_SHORT_SECONDS:
        renderer._sonomarchy_short_drops = 0
        return False
    strikes = getattr(renderer, '_sonomarchy_short_drops', 0) + 1
    renderer._sonomarchy_short_drops = strikes
    if strikes < CHUNKED_STRIKES:
        return False
    renderer._sonomarchy_chunked = True
    uuid = _sonos_uuid(renderer)
    logger.warning(
        f'{renderer.name}: dropped the stream after a few KB '
        f'{strikes} times running. That is firmware refusing a response with '
        f'a Content-Length it cannot use, not a network fault -- switching '
        f'this speaker to chunked transfer encoding, which costs Range '
        f'resumes and nothing else. Remove {uuid} from '
        f'{os.path.join(_state_dir(), "chunked")} to undo.')
    _pin_chunked(uuid)
    return True


def _tune_stream_socket(sock):
    """Per-connection TCP options for a live stream over a jittery link.

    TCP_THIN_LINEAR_TIMEOUTS: a stream this thin (a few segments in flight)
    recovers a late ACK by retransmission timeout, and by default each
    consecutive timeout doubles -- 220, 440, 880 ms -- which is what turns a
    jittery 2.4 GHz link into audible holes. Linear keeps them at the base
    RTO. TCP_NODELAY is asyncio's default for connected sockets; set anyway
    so the behaviour does not depend on that staying true.
    Returns the names applied, for the log and the tests; never raises.
    """
    import socket as _socket
    applied = []
    if sock is None:
        return applied
    for name, value in (('TCP_NODELAY', 1), ('TCP_THIN_LINEAR_TIMEOUTS', 1)):
        opt = getattr(_socket, name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(_socket.IPPROTO_TCP, opt, value)
            applied.append(name)
        except OSError as e:
            logger.debug(f'{name} not applied: {e!r}')
    return applied


def _frame_chunk(data):
    """One HTTP/1.1 chunk: size in hex, CRLF, body, CRLF."""
    return f'{len(data):x}\r\n'.encode('latin-1') + data + b'\r\n'


# ===========================================================================
# FIX 19 - prime a chunked stream with silence so the speaker has a reserve
# ===========================================================================
# A chunked response is a live radio stream to the speaker, and it starts
# playing almost as soon as bytes arrive -- with next to nothing in reserve.
# Every later byte can only arrive at real time (it is being captured live),
# so the reserve never grows, and any stall on the link is heard at once. On
# the Move's 2.4 GHz link that was a pulse roughly once a second: 694
# retransmissions in 15 minutes, each a ~200-300 ms hole.
#
# 0.1.16 built the reserve by holding the first 1.5 s of audio, which coupled
# the cushion to how fast the capture fills: with the two-second parec buffer
# the first byte arrives after the hold has expired and nothing is held at
# all. Sending pre-encoded silence instead gives the speaker its reserve in
# the first packet, before the capture has produced anything. Live audio then
# arrives behind it at real time and the lead is kept for the life of the
# stream. Cost: the music starts SILENCE_PRIME_SECONDS late, once.
#
# Sized for the capture: the first fragment comes ~2 s after parec starts
# (measured), and the speaker adds no pre-buffer of its own for this stream,
# so the standing reserve is the prime minus the fill. 3.5 s measured out at
# ~0.7 s of real reserve (delivered-minus-retransmitted bytes against the
# speaker's own RelTime), which one back-to-back pair of retransmit timeouts
# eats. 6 s leaves ~3.5-4 s on a fresh start and all 6 s on a reconnect.
SILENCE_PRIME_SECONDS = 6.0

_silence_cache = {}


def _pcm_silence(encoder, seconds):
    """Zero PCM in the format the encoder is fed."""
    width = 2                                   # s16le / s16be
    frames = int(getattr(encoder, 'rate', 44100) * seconds)
    return bytes(frames * getattr(encoder, 'channels', 2) * width)


async def _silence_prime(encoder, seconds=None):
    """Encoded silence matching the live stream, or b'' if it cannot be made.

    Runs the renderer's own encoder command on zero PCM, so the frames are
    bit-for-bit the format the speaker is about to receive. Cached per
    command: it is the same bytes every time and lame takes a moment.
    """
    seconds = SILENCE_PRIME_SECONDS if seconds is None else seconds
    if seconds <= 0:
        return b''
    command = getattr(encoder, 'command', None)
    pcm = _pcm_silence(encoder, seconds)
    if not command:
        return pcm                              # L16: raw PCM is the stream
    key = (tuple(command), len(pcm))
    cached = _silence_cache.get(key)
    if cached is not None:
        return cached
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(pcm), 20)
    except Exception as e:
        logger.warning(f'could not encode the silence prime ({e!r}); the '
                       f'stream starts with no reserve')
        return b''
    if proc.returncode != 0 or not out:
        logger.warning(f'encoder exited {proc.returncode} encoding the '
                       f'silence prime; the stream starts with no reserve')
        return b''
    _silence_cache[key] = out
    return out


def _chunked_ok_lines(mime_type):
    return ['HTTP/1.1 200 OK',
            'Content-type: ' + mime_type,
            'Connection: close',
            'Transfer-Encoding: chunked',
            '', '']


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
    if _use_chunked(renderer):
        # No length, no ranges: nothing to resume from, so the ring is dead
        # weight here and a Range request cannot be honoured anyway.
        writer.write('\r\n'.join(
            _chunked_ok_lines(renderer.mime_type)).encode('latin-1'))
        await writer.drain()
        return
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
    chunked = _use_chunked(renderer)
    ring = _ring(renderer)
    replay = getattr(renderer, '_sonomarchy_replay', None)
    renderer._sonomarchy_replay = None
    if chunked:
        replay = None
        prime = await _silence_prime(getattr(renderer, 'encoder', None))
        if prime:
            self.writer.write(_frame_chunk(prime))
            renderer._sonomarchy_sent = (
                getattr(renderer, '_sonomarchy_sent', 0) + len(prime))
            await self.writer.drain()
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
            if chunked:
                # The ring is deliberately not fed -- it only exists to answer
                # a Range request, and a chunked response never gets one.
                self.writer.write(_frame_chunk(data))
            else:
                ring.append(data)
                self.writer.write(data)
            # Counted for both framings: the ring is only fed in one of them,
            # so it cannot be what tells us how much audio the speaker got.
            renderer._sonomarchy_sent = (
                getattr(renderer, '_sonomarchy_sent', 0) + len(data))
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


async def _firewall_probe(renderer, fetches_at_play=None):
    """Blame the firewall only when the speaker never reached us at all.

    `is_playing` alone is not that test. A speaker that fetches the stream and
    then drops it -- firmware refusing our response (FIX 17) is the case that
    exposed this -- leaves is_playing False, and the probe then reported a
    firewall while the speaker's own GET sat two lines above it in the log.
    That sends the reader to ufw, which was already correct, and it cost real
    time on 2026-09-08. Comparing the request counter across the Play answers
    the actual question: did anything arrive from the speaker?
    """
    await asyncio.sleep(FIREWALL_GRACE)
    try:
        if renderer.nullsink is None:            # renderer closed meanwhile
            return
        if renderer.stream_sessions.is_playing:  # the speaker fetched it
            return
        fetches = getattr(renderer, '_sonomarchy_fetches', 0)
        if fetches_at_play is not None and fetches != fetches_at_play:
            # It reached us and then stopped; whatever is wrong, it is not a
            # blocked port. The drop is reported at WARNING where it happens.
            logger.debug(f'{renderer.name}: fetched the stream after Play and '
                         f'then stopped; not reporting a firewall')
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
            # Say it with this machine's own subnet and port rather than the
            # README's example: the reader is looking at a silent speaker and
            # should not have to translate anything.
            rules = _firewall_rules(_active_firewall(), _lan_cidr(host),
                                    port, _msearch_port())
            if rules:
                cause += '. As root: ' + ' && '.join(rules)
        logger.warning(f'{renderer.name}: told to Play {FIREWALL_GRACE}s ago '
                       f'and never fetched the stream from TCP port {port}; '
                       f'{cause}')
        # reply_dev is the shell's cue to say "a VPN is claiming your LAN";
        # sending the plain LAN interface here made it say that about wlp2s0.
        tunnel = dev if (dev and dev.startswith(_TUNNEL_PREFIXES)) else None
        emit('firewall_suspected', port=port, zone=renderer.description,
             host=host, reply_dev=tunnel, subnet=_lan_cidr(host))
    except Exception as e:
        logger.debug(f'firewall probe skipped: {e!r}')


async def _play(self, *args, **kwargs):
    result = await _orig_play(self, *args, **kwargs)
    _mark_started(self)
    try:
        _keep(asyncio.get_running_loop().create_task(
            _firewall_probe(self, getattr(self, '_sonomarchy_fetches', 0))))
    except Exception:
        pass
    return result


_pa_dlna.Renderer.play = _play


# ===========================================================================
# FIX 11 - name the exact firewall rule, for this machine and this subnet
# ===========================================================================
# The README has always told people to open two ports, and FIX 8 tells them
# again once a speaker has failed to fetch. Both said it in the abstract:
# the reader still had to work out their own subnet, their own port (8080 is
# only the first choice of ten) and their own firewall's syntax. That is
# three chances to get it wrong while the speakers sit there silent.
#
# What we can establish without root, and what we deliberately do not claim:
# `systemctl is-active` answers for anyone, so we can see that a firewall is
# RUNNING. Reading its rules needs privileges we do not have and will not
# ask for, so we can never say the port IS blocked -- only that something is
# in a position to block it, and exactly what to type if it is.
_FIREWALL_UNITS = ('ufw', 'firewalld', 'nftables', 'iptables')


def _active_firewall():
    """Name of a running firewall service, or None. Unprivileged."""
    import subprocess
    for unit in _FIREWALL_UNITS:
        try:
            done = subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                                  capture_output=True, timeout=2)
        except Exception as e:
            logger.debug(f'firewall unit check skipped ({unit}): {e!r}')
            continue
        if done.returncode == 0:
            return unit
    return None


def _lan_cidr(host):
    """The CIDR of the interface that holds `host`, e.g. '10.0.0.0/24'.

    Returned as the network address, never the host address: it is going
    into a firewall rule as a source range.
    """
    import ipaddress
    import psutil
    import socket

    try:
        for _name, snics in psutil.net_if_addrs().items():
            for snic in snics:
                if (snic.family == socket.AF_INET
                        and snic.address == host and snic.netmask):
                    return str(ipaddress.ip_network(
                        f'{host}/{snic.netmask}', strict=False))
    except Exception as e:
        logger.debug(f'subnet lookup failed for {host}: {e!r}')
    return None


# Mirrors HTTP_PORT_RANGE in sonomarchy-backend; the validator keeps them equal.
# The wrapper takes the first free port in this range, so a firewall rule for
# the port in use today is a rule that silently stops matching the day
# something else sits on that port first (2026-09-09: signal-cli on 8080).
STREAM_PORT_RANGE = (8080, 8089)


def _tcp_port_spec(tcp_port, sep):
    """`8080:8089` (ufw) / `8080-8089` (firewalld) when the port is one the
    wrapper picks from; the single port when the user pinned one elsewhere."""
    lo, hi = STREAM_PORT_RANGE
    if lo <= int(tcp_port) <= hi:
        return f'{lo}{sep}{hi}'
    return str(tcp_port)


def _firewall_rules(firewall, cidr, tcp_port, udp_port):
    """The exact commands this machine needs, or [] if we cannot be exact.

    ufw syntax is the fallback for unknown firewalls because it is what the
    README documents; a wrong-syntax hint is worse than none, so anything we
    cannot render precisely returns nothing at all.
    """
    if not cidr or not tcp_port:
        return []
    if firewall == 'firewalld':
        rules = [f"firewall-cmd --permanent --add-rich-rule='rule family=ipv4"
                 f" source address={cidr} port port={_tcp_port_spec(tcp_port, '-')}"
                 f" protocol=tcp accept'"]
        if udp_port:
            rules.append(f"firewall-cmd --permanent --add-rich-rule='rule"
                         f" family=ipv4 source address={cidr} port"
                         f" port={udp_port} protocol=udp accept'")
        rules.append('firewall-cmd --reload')
        return rules
    # Everything else gets ufw syntax: it is what the README documents, and
    # an nftables user reading a ufw line still learns the port, protocol and
    # source range they need, which is the part that is machine-specific.
    rules = [f'ufw allow proto tcp from {cidr} to any port '
             f"{_tcp_port_spec(tcp_port, ':')} comment 'Sonomarchy stream'"]
    if udp_port:
        rules.append(f'ufw allow proto udp from {cidr} to any port'
                     f" {udp_port} comment 'Sonomarchy discovery'")
    return rules


def _arg_value(argv, names):
    """Value of the first of `names` present in argv, as an int, or None."""
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                candidate = argv[i + 1]
            elif arg.startswith(name + '='):
                candidate = arg.split('=', 1)[1]
            else:
                continue
            try:
                return int(candidate)
            except (TypeError, ValueError):
                return None
    return None


def _msearch_port():
    """The UDP port speakers answer discovery on, read from our own argv.

    Read on demand rather than cached in a global: it is needed only on the
    failure path, and state that exists solely to serve an error message is
    state that can go stale without anyone noticing.
    """
    return _arg_value(sys.argv, ['--msearch-port', '-p'])


def _firewall_advice(argv):
    """(rules, firewall, cidr) for this machine right now; rules may be []."""
    nics = _nics_from_argv(argv)
    try:
        addresses = sorted(_current_ipv4(nics))
    except Exception as e:
        logger.debug(f'firewall advice found no address: {e!r}')
        addresses = []
    host = addresses[0] if addresses else None
    cidr = _lan_cidr(host) if host else None
    firewall = _active_firewall()
    tcp_port = _arg_value(argv, ['--port'])
    udp_port = _arg_value(argv, ['--msearch-port', '-p'])
    return _firewall_rules(firewall, cidr, tcp_port, udp_port), firewall, cidr


def _announce_firewall_rules(argv):
    """Log the rule this machine needs, once, at startup.

    Log only -- no OSD. A running firewall is not itself a fault, and most
    people have already allowed the ports. The notification belongs to FIX 8,
    which fires only when a speaker has actually failed to fetch.
    """
    try:
        rules, firewall, cidr = _firewall_advice(argv)
        if not rules or not firewall:
            return
        logger.info(
            f'{firewall} is running. If a zone is selectable but silent, '
            f'the speakers need to reach this machine on {cidr}: '
            + ' && '.join(rules))
        emit('firewall_rules', firewall=firewall, subnet=cidr, rules=rules)
    except Exception as e:
        logger.debug(f'firewall rule announcement skipped: {e!r}')


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
# FIX 5  - do not create sinks for bonded satellites
# FIX 13 - a Sonos group is ONE output
# ===========================================================================
# Two kinds of Sonos player must not get a sink of their own, and
# ZoneGroupTopology is the single place that tells us about both.
#
# BONDED (FIX 5). A stereo pair's second speaker, a surround and a Sub belong
# to another player's zone and are not independently playable. The topology
# marks them Invisible="1"; satellites are additionally nested as <Satellite>.
#
# GROUPED (FIX 13). Rooms grouped in the Sonos app play in lockstep, and only
# the group's Coordinator owns the transport. Every other member sits at
# CurrentURI "x-rincon:RINCON_<coordinator>" and follows it. Observed on
# 2026-09-06 with "Living Room" grouped to "Office" (player ids and addresses
# below are stand-ins -- a real RINCON id contains the speaker's MAC):
#
#   ZoneGroup Coordinator="RINCON_0FF1CE01400"
#     ZoneGroupMember ZoneName="Office"       UUID="RINCON_0FF1CE01400"
#     ZoneGroupMember ZoneName="Living Room"  UUID="RINCON_CAFE0101400"
#
#   Office       CurrentURI = http://192.0.2.10:8080/audio-content/uuid:...
#   Living Room  CurrentURI = x-rincon:RINCON_0FF1CE01400
#
# yet the sound menu still offered both as separate outputs. Selecting the
# follower does NOT play to the group: SetAVTransportURI on a grouped member
# makes it leave the group and play alone, which is the opposite of what
# someone who grouped the rooms asked for.
#
# Fix: register only each group's coordinator, and label its sink with every
# room in the group ("Living Room + Office"). One sink then drives them all --
# Sonos relays the stream to the followers itself. Ungroup in the Sonos app
# and the rooms become separate sinks again; _watch_zone_groups() below
# notices and rebuilds.
#
# The label is sorted alphabetically, NOT coordinator-first: Sonos reassigns
# the coordinator on its own (when one drops out, or on a regroup), and a name
# that follows it would rename the user's output device for no visible reason.
#
# Volume is not part of this: pa-dlna never issues RenderingControl actions,
# so each speaker keeps the volume the Sonos app gave it and the group's own
# volume rules apply.

_ZGT_TTL = 60           # seconds a registration may reuse a cached topology
_GROUP_SETTLE_POLLS = 2  # consecutive identical polls before acting (FIX 13)

_EMPTY_TOPOLOGY = {'players': {}, 'groups': {}}

# Every renderer registers at once on startup. Without a lock they all miss
# the empty cache and fire simultaneous topology requests at one speaker.
# A plain threading lock, not an asyncio one: the FIX 13 watcher thread
# shares this cache with the registering coroutines.
_topo_lock = threading.Lock()
_topo_cache = {'topo': _EMPTY_TOPOLOGY, 'ts': 0.0, 'fresh': False}

# Any Sonos answers for the whole household, so remember the ones we have seen
# and let the watcher ask whichever is still up.
_ips_lock = threading.Lock()
_sonos_ips = []

# Set on the first register(); the watcher uses it to avoid restarting in the
# middle of playback.
_control_point = None

_ZGT_BODY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    '<u:GetZoneGroupState'
    ' xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
    '</u:GetZoneGroupState></s:Body></s:Envelope>').encode()


def _fetch_topology(ip):
    """Read the household's zone topology from one Sonos.

    Returns {'players': {uuid: {'zone', 'coord', 'invisible'}},
             'groups':  {coordinator uuid: (visible member uuids,)}}
    with each group's coordinator first in its member tuple.
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    req = urllib.request.Request(
        f'http://{ip}:1400/ZoneGroupTopology/Control', data=_ZGT_BODY,
        headers={'Content-Type': 'text/xml; charset="utf-8"',
                 'SOAPACTION': '"urn:schemas-upnp-org:service:'
                               'ZoneGroupTopology:1#GetZoneGroupState"'})
    with urllib.request.urlopen(req, timeout=6) as resp:
        envelope = resp.read()

    # The topology is an escaped XML document inside the SOAP response. Let
    # the parser unescape it exactly once -- unescaping the text by hand
    # twice, as this did before, mangles a room name containing an '&'.
    payload = None
    for elem in ET.fromstring(envelope).iter('ZoneGroupState'):
        if elem.text and 'ZoneGroup' in elem.text:
            payload = elem.text
            break
    if payload is None:
        raise ValueError('no ZoneGroupState payload in the SOAP response')

    players, groups = {}, {}
    for group in ET.fromstring(payload).iter('ZoneGroup'):
        coord = group.get('Coordinator') or ''
        visible = []
        for member in group.iter():
            uuid = member.get('UUID')
            if not uuid or member.tag not in ('ZoneGroupMember', 'Satellite'):
                continue
            invisible = (member.get('Invisible') == '1' or
                         member.tag == 'Satellite')
            players[uuid] = {'zone': member.get('ZoneName') or '',
                             'coord': coord, 'invisible': invisible}
            if not invisible:
                visible.append(uuid)
        if coord:
            visible.sort(key=lambda uuid: uuid != coord)
            groups[coord] = tuple(visible)
    return {'players': players, 'groups': groups}


def _topology(ip):
    """The cached topology, refreshed at most once per _ZGT_TTL.

    Never raises. If we cannot ask, keep the last good answer -- or, having
    never had one, an empty topology, which registers every renderer. Failing
    open shows a speaker that should have been hidden; failing closed would
    hide one the user actually wanted.
    """
    with _topo_lock:
        now = time.time()
        if _topo_cache['fresh'] and now - _topo_cache['ts'] < _ZGT_TTL:
            return _topo_cache['topo']
        try:
            topo = _fetch_topology(ip)
        except Exception as e:
            logger.debug(f'could not read Sonos zone topology from {ip}: '
                         f'{e!r}')
            # Back off for a TTL so an unreachable speaker is not hammered.
            _topo_cache['ts'] = now
            return _topo_cache['topo']
        _topo_cache.update(topo=topo, ts=now, fresh=True)
        return topo


def _refresh_topology():
    """Force a refresh, asking each known Sonos until one answers.

    Returns None when none of them did -- which is not a topology change and
    must never be mistaken for one.
    """
    with _ips_lock:
        ips = list(_sonos_ips)
    for ip in ips:
        try:
            topo = _fetch_topology(ip)
        except Exception as e:
            logger.debug(f'zone topology: {ip} did not answer: {e!r}')
            continue
        with _topo_lock:
            _topo_cache.update(topo=topo, ts=time.time(), fresh=True)
        with _ips_lock:
            # Ask the speaker that just worked first next time.
            if ip in _sonos_ips:
                _sonos_ips.remove(ip)
                _sonos_ips.insert(0, ip)
        return topo
    return None


def _remember_sonos_ip(ip):
    if not ip:
        return
    with _ips_lock:
        if ip not in _sonos_ips:
            _sonos_ips.append(ip)


def _group_rooms(uuid, topo):
    """Room names in this coordinator's group, alphabetical, deduplicated."""
    names = set()
    for member in topo['groups'].get(uuid, ()):
        zone = topo['players'].get(member, {}).get('zone')
        if zone:
            names.add(zone)
    return sorted(names, key=str.casefold)


def _group_label(uuid, topo):
    """Sink label for a group coordinator, or None when it plays alone."""
    names = _group_rooms(uuid, topo)
    if len(names) < 2:
        return None
    label = (' + '.join(names) if len(names) <= 3 else
             f'{names[0]} + {len(names) - 1} more')
    # The label is interpolated into a quoted pulseaudio module argument.
    return label.replace('"', '').replace('\\', '')


_orig_register = _pa_dlna.AVControlPoint.register


async def _register(self, renderer):
    global _control_point
    _ensure_resume_loop(self)          # FIX 10c, needs the running loop
    _control_point = self

    uuid = _sonos_uuid(renderer)
    if uuid:
        ip = getattr(renderer.root_device, 'peer_ipaddress', '')
        _remember_sonos_ip(ip)
        try:
            loop = asyncio.get_running_loop()
            topo = await loop.run_in_executor(None, _topology, ip)
            player = topo['players'].get(uuid)
            if player is not None:
                zone = player['zone'] or uuid
                if player['invisible']:
                    logger.info(f'skipping {zone} ({uuid}): bonded into '
                                f'another Sonos zone (surround, stereo pair '
                                f'partner or Sub), not independently '
                                f'selectable')
                    return
                coord = player['coord']
                if coord and coord != uuid:
                    # Hide a follower only when its coordinator is a player we
                    # would actually register, so something takes its place in
                    # the menu. A topology that names a coordinator we cannot
                    # see -- malformed, or mid-change -- must not cost the user
                    # the one speaker they can really play to.
                    leader = topo['players'].get(coord)
                    if leader is not None and not leader['invisible']:
                        logger.info(f'skipping {zone} ({uuid}): grouped with '
                                    f'{leader["zone"] or coord}; the whole '
                                    f'group plays through a single sink')
                        return
                    logger.info(f'{zone} ({uuid}) claims coordinator {coord}, '
                                f'which is not a playable zone here; offering '
                                f'{zone} on its own rather than hiding it')
                label = _group_label(uuid, topo)
                if label:
                    # Set before pulse_register(): the null-sink module takes
                    # device.description from here.
                    logger.info(f'{zone} coordinates a Sonos group; its sink '
                                f'plays to all of "{label}"')
                    renderer.description = label
        except Exception as e:
            logger.debug(f'zone group check skipped: {e!r}')
    result = await _orig_register(self, renderer)
    _mark_started(renderer)
    try:
        if renderer.nullsink is not None:
            emit('zone', uuid=uuid or renderer.upnp_device.UDN,
                 name=renderer.description, sink=renderer.nullsink.sink.name,
                 rooms=_group_rooms(uuid, _topo_cache['topo']))
    except Exception:
        pass
    return result


_pa_dlna.AVControlPoint.register = _register


def _state_dir():
    base = (os.environ.get('XDG_STATE_HOME')
            or os.path.join(os.path.expanduser('~'), '.local', 'state'))
    return os.path.join(base, 'io.github.nixfred.sonomarchy')


def _grouping_restart_stamp():
    return os.path.join(_state_dir(), 'last-grouping-restart')


def _recent_grouping_restarts():
    """Timestamps of grouping rebuilds inside the window, oldest first.

    The rebuild discards this process, so an in-memory count would be
    forgotten by the process that needs it; it lives on disk instead.
    Anything unreadable returns [] -- failing open costs a rebuild, failing
    closed would wedge rebuilds forever on one bad file.
    """
    try:
        with open(_grouping_restart_stamp()) as stamp:
            stamps = json.load(stamp)
        now = time.time()
        # A stamp in the future means the clock jumped; drop it rather than
        # letting it hold rebuilds off until the clock catches up.
        return sorted(float(t) for t in stamps
                      if 0 <= now - float(t) < GROUP_RESTART_WINDOW)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.debug(f'grouping restart stamp unreadable: {e!r}')
        return []


def _grouping_rebuilds_exhausted():
    """True when we have already rebuilt GROUP_RESTART_BURST times lately.

    A group that keeps forming and dissolving -- a member on failing wifi is
    the realistic case -- would otherwise exit the backend every few seconds
    indefinitely: Service.qml treats a deliberate restart as fast and does NOT
    apply its crash backoff, so nothing else would slow the loop down.
    """
    return len(_recent_grouping_restarts()) >= GROUP_RESTART_BURST


def _record_grouping_restart():
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        stamps = _recent_grouping_restarts() + [time.time()]
        with open(_grouping_restart_stamp(), 'w') as stamp:
            json.dump(stamps[-GROUP_RESTART_BURST:], stamp)
    except Exception as e:
        logger.debug(f'could not record the grouping rebuild: {e!r}')


def _grouping_changed(before, after):
    """True when a player we can still see has been re-grouped or re-bonded.

    Only players present in BOTH snapshots are compared. A speaker dropping
    off wifi for a poll, or a new one appearing, is pa-dlna's own business
    (SSDP handles it) and must not cost a restart.
    """
    for uuid in before['players'].keys() & after['players'].keys():
        was, now = before['players'][uuid], after['players'][uuid]
        if (was['coord'], was['invisible']) != (now['coord'],
                                                now['invisible']):
            return True
    return False


def _is_streaming():
    """True if any renderer is playing -- or if we could not tell."""
    cp = _control_point
    if cp is None:
        return False
    try:
        return any(r.stream_sessions.is_playing for r in cp.renderers())
    except Exception as e:
        # Iterating the control point's renderers from this thread can race
        # with the event loop mutating them. Unsure means "do not interrupt".
        logger.debug(f'could not check for active streams: {e!r}')
        return True


def _watch_zone_groups():
    """Exit (SIGTERM to self) when the Sonos grouping changes.

    Grouping is only read at discovery, so a group formed or dissolved while
    we run leaves the sink list wrong: a follower keeps a sink that would tear
    it out of its group, or an ungrouped room has no sink at all. There is no
    supported way to add or drop a null-sink for an already-registered
    renderer, so do what FIX 2 does -- exit and let the shell restart us into
    a fresh discovery pass.
    """
    baseline = None
    pending = None
    pending_polls = 0
    deferred = False
    throttled = False
    while True:
        # Returns as soon as a Sonos tells us the household changed (FIX 14),
        # or after GROUP_POLL_INTERVAL if nothing does.
        _wait_for_group_change()
        # The whole body is guarded: a watchdog that dies silently on an
        # unexpected value is worse than no watchdog.
        try:
            if baseline is None:
                # Prefer the snapshot the registrations actually used, so a
                # regrouping between discovery and this first poll is not
                # baked into the baseline and missed.
                with _topo_lock:
                    if _topo_cache['fresh']:
                        baseline = _topo_cache['topo']
                if baseline is None:
                    baseline = _refresh_topology()
                continue

            current = _refresh_topology()
            if current is None:
                continue

            if not _grouping_changed(baseline, current):
                pending, pending_polls = None, 0
                deferred = throttled = False
                continue

            # Sonos reports transient groupings while a regroup is in
            # progress; wait for the new one to hold still.
            if pending is not None and not _grouping_changed(pending, current):
                pending_polls += 1
            else:
                pending, pending_polls = current, 1
            if pending_polls < _GROUP_SETTLE_POLLS:
                continue

            if _is_streaming():
                # Sonos keeps a group in sync with its coordinator, so audio
                # in flight is unaffected; only the sink list is stale.
                # Rebuild once playback ends rather than cutting the music.
                if not deferred:
                    deferred = True
                    logger.info('Sonos grouping changed while a stream is '
                                'running; rebuilding the outputs once '
                                'playback ends')
                continue

            if _grouping_rebuilds_exhausted():
                if not throttled:
                    throttled = True
                    logger.warning(
                        f'Sonos grouping has changed {GROUP_RESTART_BURST} '
                        f'times in the last {GROUP_RESTART_WINDOW}s; holding '
                        f'off on further rebuilds. A group that keeps forming '
                        f'and dissolving usually means a speaker with an '
                        f'unstable connection. The outputs may be stale until '
                        f'it settles.')
                continue

            logger.warning('Sonos grouping changed; exiting so the shell '
                           'rebuilds the outputs and each group is offered '
                           'as one sink.')
            _record_grouping_restart()
            emit('restart', reason='grouping_changed')
            os.kill(os.getpid(), signal.SIGTERM)
            return
        except Exception as e:
            logger.debug(f'zone group watch skipped a poll: {e!r}')


# ===========================================================================
# FIX 14 - notice a regroup the moment it happens
# ===========================================================================
# FIX 13's watchdog polls the topology every GROUP_POLL_INTERVAL seconds, so a
# regroup can take half a minute to show up in the sound menu. Sonos will tell
# us instead: ZoneGroupTopology is an evented UPnP service, so SUBSCRIBE to it
# and the speaker POSTs a NOTIFY the instant the household changes.
#
# The NOTIFY body is deliberately ignored. It carries the new topology in yet
# another encoding, and trusting it would mean a second parser and an
# assumption that events arrive in order. All we take from it is "something
# changed"; the watchdog then re-reads the topology over SOAP as it always
# did, which is authoritative and already tested.
#
# Eventing is a fast path, never a dependency. The speaker connects back to
# us, so a firewall that drops the callback port makes NOTIFY silently
# disappear -- the same failure FIX 8 exists for on the audio port. The poll
# is therefore kept exactly as it was: if events never arrive, the worst case
# is the behaviour of 0.1.7. Nothing here can make grouping detection worse
# than not having it.
#
# The callback port is taken from a range above the audio one (the backend
# hands pa-dlna something in 8080-8089), chosen at startup and logged, so a
# firewall rule can name it.

GROUP_EVENT_PORTS = range(8090, 8100)

# Seconds to let a regroup finish after an event wakes us. Sonos emits
# several NOTIFYs while rooms are moving between groups; without this the
# settle rule in FIX 13 would be satisfied by two readings microseconds apart
# and could act on a half-finished regroup.
GROUP_EVENT_SETTLE = 3

# Seconds of subscription lease to ask for, and the fraction of it at which
# we renew.
GROUP_EVENT_LEASE = 1800

# Seconds between checks that the subscription is still alive.
GROUP_EVENT_POLL = 30

# Seconds between attempts while we have NO subscription. Shorter on purpose:
# no speaker is known until the first renderer registers, which is well after
# this thread starts, and a grouping change restarts the backend -- so a slow
# retry leaves the blind window open exactly when another change is likeliest.
# Measured at 108 s from start to subscribed before this was split out.
GROUP_EVENT_RETRY = 5

_ZGT_EVENT_PATH = '/ZoneGroupTopology/Event'

_group_wakeup = threading.Event()
_event_state = {'port': None, 'sid': None, 'ip': None, 'renew_at': 0.0,
                'notifies': 0, 'warned': False, 'server': None}


class _ZgtEventServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() resolves the bind address with
        # socket.getfqdn(), a reverse DNS lookup that blocks for a full
        # resolver timeout -- measured at 5.0 s here -- because nothing
        # answers for 0.0.0.0. server_name is only ever used to fill in CGI
        # variables, which this server does not have, so skip the lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _ZgtEventHandler(BaseHTTPRequestHandler):
    """Answers the NOTIFY a Sonos sends when the household changes."""

    protocol_version = 'HTTP/1.1'

    def do_NOTIFY(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length:
                self.rfile.read(length)     # drained, then discarded on purpose
            self.send_response(200)
            self.send_header('Content-Length', '0')
            # One NOTIFY per connection. Without this, a body we could not
            # drain -- a chunked one carries no Content-Length -- would be
            # read as the start of the next request on a kept-alive socket.
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
        except Exception as e:
            logger.debug(f'malformed zone topology NOTIFY: {e!r}')
            return
        _event_state['notifies'] += 1
        _group_wakeup.set()

    def log_message(self, *args):
        pass            # BaseHTTPRequestHandler logs every request to stderr


def _start_event_listener(nics=None):
    """Listen for topology NOTIFYs. Returns the port, or None if we cannot."""
    override = os.environ.get('SONOMARCHY_EVENT_PORT')
    ports = [int(override)] if (override or '').isdigit() else GROUP_EVENT_PORTS
    for port in ports:
        try:
            server = _ZgtEventServer(('0.0.0.0', port), _ZgtEventHandler)
        except OSError as e:
            logger.debug(f'zone topology event port {port} unusable: {e!r}')
            continue
        threading.Thread(target=server.serve_forever, name='zgt-events',
                         daemon=True).start()
        _event_state.update(port=port, server=server)
        rule = _event_port_rule(nics, port)
        logger.info(
            f'listening for Sonos zone topology events on port {port}. A '
            f'firewall that blocks it costs speed, not correctness: the '
            f'{GROUP_POLL_INTERVAL}s poll still catches every regroup.'
            + (f' To make regrouping instant: {rule}' if rule else ''))
        return port
    logger.info(f'no free port for Sonos topology events in '
                f'{ports[0]}-{ports[-1]}; falling back to the '
                f'{GROUP_POLL_INTERVAL}s poll')
    return None


def _event_port_rule(nics, port):
    """The firewall rule that makes the callback reachable, or '' if unsure.

    Same reasoning as FIX 11: a hint we cannot render exactly is worse than
    no hint, so anything uncertain returns nothing.
    """
    try:
        addresses = sorted(_current_ipv4(nics))
        cidr = _lan_cidr(addresses[0]) if addresses else None
        if not cidr:
            return ''
        if _active_firewall() == 'firewalld':
            return (f"firewall-cmd --permanent --add-rich-rule='rule"
                    f" family=ipv4 source address={cidr} port port={port}"
                    f" protocol=tcp accept' && firewall-cmd --reload")
        return (f'ufw allow proto tcp from {cidr} to any port {port}'
                f" comment 'Sonomarchy zone events'")
    except Exception as e:
        logger.debug(f'event port rule hint skipped: {e!r}')
        return ''


def _event_request(ip, method, headers):
    import urllib.request
    req = urllib.request.Request(f'http://{ip}:1400{_ZGT_EVENT_PATH}',
                                 method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=6) as resp:
        return dict(resp.headers)


def _lease_seconds(header):
    """'Second-1800' -> 1800. Anything else -> the lease we asked for.

    Clamped both ways. A lease of 0 would have us renewing continuously; one
    larger than we asked for (seen from fuzzing, not from hardware) would push
    the renewal past the moment the subscription actually lapses, and events
    would stop with nothing logged.
    """
    try:
        return min(GROUP_EVENT_LEASE,
                   max(60, int((header or '').split('-', 1)[1])))
    except Exception:
        return GROUP_EVENT_LEASE


def _local_ip_for(peer):
    """The address a packet to `peer` would leave from."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((peer, 1400))      # UDP connect: sets a route, sends none
        return sock.getsockname()[0]


def _subscribe(ip, port):
    callback = f'http://{_local_ip_for(ip)}:{port}{_ZGT_EVENT_PATH}'
    headers = _event_request(ip, 'SUBSCRIBE', {
        'CALLBACK': f'<{callback}>',
        'NT': 'upnp:event',
        'TIMEOUT': f'Second-{GROUP_EVENT_LEASE}'})
    sid = headers.get('SID')
    if not sid:
        raise ValueError('SUBSCRIBE returned no SID')
    lease = _lease_seconds(headers.get('TIMEOUT'))
    _event_state.update(sid=sid, ip=ip, renew_at=time.time() + lease / 2)
    logger.info(f'subscribed to zone topology events on {ip} '
                f'(lease {lease}s); regroups now show up at once')
    return sid


def _renew():
    ip, sid = _event_state['ip'], _event_state['sid']
    headers = _event_request(ip, 'SUBSCRIBE', {'SID': sid,
                                               'TIMEOUT': f'Second-'
                                                          f'{GROUP_EVENT_LEASE}'})
    lease = _lease_seconds(headers.get('TIMEOUT'))
    _event_state['renew_at'] = time.time() + lease / 2
    logger.debug(f'renewed zone topology subscription on {ip} ({lease}s)')


def _unsubscribe():
    ip, sid = _event_state['ip'], _event_state['sid']
    if not (ip and sid):
        return
    try:
        _event_request(ip, 'UNSUBSCRIBE', {'SID': sid})
        logger.debug(f'unsubscribed from zone topology events on {ip}')
    except Exception as e:
        # The lease expires on its own; a speaker POSTing to a closed port
        # gets a refused connection and drops us.
        logger.debug(f'unsubscribe failed, letting the lease lapse: {e!r}')
    finally:
        _event_state.update(sid=None, ip=None, renew_at=0.0)


def _watch_zone_group_events(nics=None):
    """Keep one live topology subscription, on whichever speaker answers.

    Never raises: every failure degrades to FIX 13's poll.
    """
    port = _start_event_listener(nics)
    if port is None:
        return
    atexit.register(_unsubscribe)
    first = True
    while True:
        # Sleep at the END of the turn. Sleeping first left every backend
        # unsubscribed for GROUP_EVENT_POLL seconds after start -- and a
        # grouping change restarts the backend, so that blind window landed
        # exactly when the next change was most likely.
        if not first:
            time.sleep(GROUP_EVENT_POLL if _event_state['sid']
                       else GROUP_EVENT_RETRY)
        first = False
        try:
            if _event_state['sid'] is not None:
                if time.time() < _event_state['renew_at']:
                    _warn_once_if_events_are_not_arriving()
                    continue
                try:
                    _renew()
                    continue
                except Exception as e:
                    logger.info(f'zone topology subscription lost '
                                f'({e!r}); resubscribing')
                    _event_state.update(sid=None, ip=None, renew_at=0.0)

            with _ips_lock:
                candidates = list(_sonos_ips)
            for ip in candidates:
                try:
                    _subscribe(ip, port)
                    break
                except Exception as e:
                    logger.debug(f'could not subscribe on {ip}: {e!r}')
        except Exception as e:
            logger.debug(f'zone topology event watch skipped a turn: {e!r}')


def _warn_once_if_events_are_not_arriving():
    """Say so, once, if a live subscription has never produced a NOTIFY.

    A silent subscription means the speaker cannot reach our callback port --
    almost always a firewall. Worth one line, because the symptom otherwise is
    just "grouping takes a while to show up".
    """
    if _event_state['warned'] or _event_state['notifies']:
        return
    if time.time() < _event_state['renew_at'] - GROUP_EVENT_LEASE / 4:
        return
    _event_state['warned'] = True
    logger.info(f'subscribed to zone topology events on '
                f'{_event_state["ip"]} but none have arrived; the speaker '
                f'may be unable to reach port {_event_state["port"]} on this '
                f'machine. Regroups are still picked up by the '
                f'{GROUP_POLL_INTERVAL}s poll.')


def _wait_for_group_change():
    """Block until a topology event arrives, or the poll interval elapses.

    Returns True when an event woke us. The extra settle pause is what keeps
    FIX 13's "hold for two readings" rule meaningful: without it a burst of
    NOTIFYs mid-regroup would satisfy it in microseconds.
    """
    woken = _group_wakeup.wait(GROUP_POLL_INTERVAL)
    if woken:
        _group_wakeup.clear()
        time.sleep(GROUP_EVENT_SETTLE)
    return woken

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
# FIX 9 - clean up sinks a previous backend left behind  (SUPERSEDED by FIX 15)
# ===========================================================================
# A reloaded or crashed backend used to leave its null-sink modules loaded,
# and they sat in the sound menu as duplicate, dead zones. FIX 9 cleared every
# Sonos null-sink at startup.
#
# FIX 15 makes that wrong: sinks are now deliberately left loaded across a
# restart and adopted by the replacement, so clearing them at startup would
# throw away the one playback is sitting on -- which is the whole bug FIX 15
# exists to stop. The leftovers FIX 9 was written for are now swept by
# _sweep_unadopted_sinks() once discovery has settled and it is possible to
# tell a stale sink from one a zone is about to claim.
#
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

            # The speaker reached us. Counted before the takeover branch on
            # purpose: even a request we answer 409 proves the network path
            # works, which is the only question FIX 8's probe is asking.
            renderer._sonomarchy_fetches = (
                getattr(renderer, '_sonomarchy_fetches', 0) + 1)

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

            _tune_stream_socket(writer.get_extra_info('socket'))

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


# ===========================================================================
# FIX 18 - ask parec for a small buffer, or the audio arrives in lumps
# ===========================================================================
# `parec` with no latency request gets whatever the server thinks is generous:
# measured here, a 88200-frame quantum at 44100 Hz -- a two second buffer. The
# capture then reaches the encoder in ~0.37 s lumps after an initial 2 s stall,
# and delivers only ~120 KB/s of the 176 KB/s the stream needs. A speaker with
# a shallow jitter buffer plays that as a stutter roughly once a second, which
# is exactly how a new Sonos Move sounded on 2026-09-08 while every session
# metric said the stream was healthy: no drops, no restarts, no xruns.
#
# Measured over 6 s on the same monitor source:
#     default            724992 bytes, 12 stalls > 50 ms (~0.37 s apart)
#     --latency-msec=50 1052672 bytes, steady 53 ms cadence
# (The default's byte count is simply 4 s of real time after its 2 s fill,
# not a slower rate -- an earlier reading of it as "68% of real time" was
# wrong.)
#
# 50 ms turned out to be too small in the other direction: it makes the TCP
# stream a dribble of ~1.6 KB every 50 ms, and a "thin" stream like that can
# only recover a late ACK by retransmission timeout. 250 ms then failed the
# moment the machine got busy: at load average 10 parec missed its 250 ms
# deadline 183 times in 27 minutes, and every miss is a hole in the audio
# before it reaches TCP. The two-second default was never the problem -- it
# is the slack that hides scheduling stalls, and the ~0.37 s fragments it
# delivers in are a fine TCP burst size. What was missing was a reserve on
# the speaker's side, which FIX 19 provides without any waiting. So: no
# latency request at all. The knob stays for a machine that needs it.
PAREC_LATENCY_MSEC = None

_orig_run_parec = _http_server.StreamProcesses.run_parec


async def _run_parec(self, encoder, parec_cmd, stdout=None):
    """Add a latency request to the parec argv unless one is already there."""
    if PAREC_LATENCY_MSEC is not None and not any(
            str(arg).startswith('--latency') for arg in parec_cmd):
        # A new list: upstream extends the one it is given, and mutating the
        # caller's would double the flag on a restarted track.
        parec_cmd = list(parec_cmd) + [f'--latency-msec={PAREC_LATENCY_MSEC}']
    return await _orig_run_parec(self, encoder, parec_cmd, stdout)


_http_server.StreamProcesses.run_parec = _run_parec


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
    # How much audio actually reached the speaker before it hung up is the
    # one fact that separates the two causes of a drop: a starved stream
    # (nothing playing into the sink, encoder dead) sends ~nothing, while a
    # speaker rejecting the response sends a couple of seconds of perfectly
    # good MP3 first. Without this the log looks identical either way.
    started = time.monotonic()
    sent_at_start = getattr(renderer, '_sonomarchy_sent', 0)
    try:
        await _http_server.write_http_ok(self.writer, renderer)
        _http_server.logger.debug(f'{self.task_name}: track is started')
        await self.write_track(reader)
        await self.shutdown()
    except asyncio.CancelledError:
        self.session.stream_tasks.create_task(self.shutdown(),
                                              name='shutdown')
    except ConnectionError as e:
        sent = getattr(renderer, '_sonomarchy_sent', 0) - sent_at_start
        elapsed = time.monotonic() - started
        logger.warning(f'{self.task_name}: speaker dropped the connection '
                       f'({e!r}) after {sent} bytes in {elapsed:.1f}s; '
                       f'keeping the zone, the stream will be re-established')
        _note_short_drop(renderer, sent, elapsed)
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
    # A renderer pinned to chunked framing (FIX 17) is the one case where the
    # terminator upstream sends IS correct, so it comes back for those.
    # Defensively: shutdown also runs on half-built tracks, where reaching
    # through to the renderer would raise and lose the socket cleanup.
    session = getattr(self, 'session', None)
    renderer = getattr(session, 'renderer', None)
    chunked = renderer is not None and _use_chunked(renderer)
    try:
        try:
            if chunked and not writer.is_closing():
                writer.write(b'0' + b'\r\n' + b'\r\n')
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



# ===========================================================================
# FIX 15 - a backend restart must not move the user's audio
# ===========================================================================
# The backend does not decide when it restarts. The shell owns the process and
# recreates it whenever it reloads its plugin tree, which on this machine it
# did 16 times between 09:59 and 11:19 on 2026-09-07 with nothing in
# Sonomarchy asking for it (the grouping watchdog recorded no rebuilds all
# day, the address watch logged nothing, and SIGTERM shutdown measures 0.10 s
# with no sinks left behind).
#
# That was harmless until you were listening. Unloading the null-sinks makes
# every zone vanish from PipeWire, so anything playing is moved to the
# built-in speakers; when the replacement backend loads the sinks again the
# stream is restored to the Sonos. Audible result: the music hops to the
# laptop and back every time the shell reloads.
#
# Fix: stop treating a null-sink as owned by one backend process. On the way
# out, leave the sinks loaded -- the sink never disappears, so nothing moves.
# On the way in, adopt the sink that is already there instead of loading a
# second one. What the user hears is a short gap while the encoder restarts,
# not their output device changing underneath them.
#
# Two things this must not break:
#
#   * A renderer that goes away for real (a speaker switched off, a 'byebye')
#     still unloads its sink. Only a shutdown of the whole control point keeps
#     them, which is the case where something is coming back.
#   * A sink whose label no longer matches is reloaded rather than adopted.
#     That is the FIX 13 regroup path: "Office (Sonos Playbar)" becoming
#     "Living Room + Office" is exactly the moment the sound menu MUST change,
#     and moving the stream is the correct cost there.
#
# Anything left over -- a zone that never came back, a sink from an older
# version -- is swept once discovery has settled, so a stale entry cannot sit
# in the sound menu doing nothing.

# Seconds after start before unclaimed Sonos sinks are swept. Must clear
# discovery: registration fills _adopted_sinks as each zone is found.
SINK_SWEEP_DELAY = 90

# Null-sink names this backend is answering for.
_adopted_sinks = set()


def _sink_description(sink):
    """The device.description currently on a libpulse Sink, or ''."""
    try:
        proplist = getattr(sink, 'proplist', None) or {}
        return proplist.get('device.description') or ''
    except Exception:
        return ''


def _null_sink_name(renderer):
    """The sink name pa-dlna uses for a renderer (see Pulse.register)."""
    return f'{renderer.getattr("modelName")}-{renderer.upnp_device.UDN}'


_orig_pulse_register = _pulseaudio.Pulse.register


async def _pulse_register(self, renderer):
    if self.lib_pulse is not None:
        try:
            name = _null_sink_name(renderer)
            for sink in await self.lib_pulse.pa_context_get_sink_info_list():
                if sink.name != name:
                    continue
                if _sink_description(sink) == renderer.description:
                    logger.info(f'adopting the null-sink already loaded for '
                                f'{renderer.description}: anything playing '
                                f'stays on the speaker')
                    _adopted_sinks.add(name)
                    return _pulseaudio.NullSink(sink)
                # The label changed -- a regroup (FIX 13). The sound menu has
                # to change, so reload rather than adopt.
                logger.info(f'null-sink {name} is labelled '
                            f'{_sink_description(sink)!r} but should be '
                            f'{renderer.description!r}; reloading it')
                await self.lib_pulse.pa_context_unload_module(
                                                        sink.owner_module)
                break
        except Exception as e:
            # Fail back to upstream's behaviour: load a fresh sink.
            logger.debug(f'could not adopt an existing null-sink: {e!r}')

    nullsink = await _orig_pulse_register(self, renderer)
    if nullsink is not None:
        _adopted_sinks.add(nullsink.sink.name)
    return nullsink


_pulseaudio.Pulse.register = _pulse_register

_orig_pulse_unregister = _pulseaudio.Pulse.unregister


async def _pulse_unregister(self, nullsink):
    control_point = getattr(self, 'av_control_point', None)
    if control_point is not None and getattr(control_point, 'closing', False):
        # The whole control point is going down, which on this machine almost
        # always means the shell is about to start a replacement. Leave the
        # sink loaded so PipeWire has no reason to move anything.
        logger.info(f'leaving null-sink {nullsink.sink.name} loaded so '
                    f'playback stays put across the restart')
        return
    _adopted_sinks.discard(nullsink.sink.name)
    return await _orig_pulse_unregister(self, nullsink)


_pulseaudio.Pulse.unregister = _pulse_unregister


def _sonos_null_sinks(short_modules):
    """[(module index, sink name)] for Sonos null-sinks in `pactl` output."""
    import re
    found = []
    for line in (short_modules or '').splitlines():
        parts = line.split('\t')
        if len(parts) < 3 or parts[1] != 'module-null-sink':
            continue
        if 'uuid:RINCON_' not in parts[2] or not parts[0].strip().isdigit():
            continue
        name = re.search(r'sink_name="([^"]+)"', parts[2])
        found.append((int(parts[0]), name.group(1) if name else ''))
    return found


def _sweep_unadopted_sinks():
    """Unload Sonos null-sinks no zone claimed, once discovery has settled.

    FIX 15 keeps sinks across a restart, so they can no longer be cleared
    wholesale at startup -- that would throw away the very thing we mean to
    adopt. Instead, whatever is still unclaimed after discovery is stale for
    real: a speaker that never came back, or a leftover from an older version.
    """
    time.sleep(SINK_SWEEP_DELAY)
    try:
        import subprocess
        listing = subprocess.run(['pactl', 'list', 'short', 'modules'],
                                 capture_output=True, text=True,
                                 timeout=5).stdout or ''
        unloaded = 0
        for index, name in _sonos_null_sinks(listing):
            if name and name in _adopted_sinks:
                continue
            subprocess.run(['pactl', 'unload-module', str(index)],
                           capture_output=True, timeout=5)
            unloaded += 1
        if unloaded:
            logger.warning(f'unloaded {unloaded} Sonos null-sink(s) that no '
                           f'zone claimed')
            emit('cleanup', unloaded=unloaded)
    except Exception as e:
        logger.debug(f'unadopted sink sweep skipped: {e!r}')


def _die_with_the_shell():
    """Ask the kernel for SIGTERM when the shell that started us is gone.

    The service stops its backend when it unloads, but a shell that dies hard
    (the ``omarchy restart shell`` kill, a crash) never gets to. The backend
    it leaves behind keeps the instance lock and the speakers for as long as
    it lives; on 2026-09-09 one outlived nine shell restarts and every new
    shell failed with "still running after 20 s". PR_SET_PDEATHSIG (Linux
    only, the only platform Omarchy runs on) closes that gap: the shell going
    away becomes the same clean SIGTERM shutdown an unload sends.
    """
    if not sys.platform.startswith('linux'):
        return
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        pr_set_pdeathsig = 1
        if libc.prctl(pr_set_pdeathsig, int(signal.SIGTERM), 0, 0, 0) != 0:
            return
    except (OSError, AttributeError):
        return
    # The signal is only armed for a parent that dies AFTER the call; one
    # that died in between would leave us orphaned exactly as before.
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    _die_with_the_shell()
    emit('starting', version=VERSION)
    # FIX 15: sinks are adopted, not recreated, so they must NOT be cleared
    # here -- that would throw away the ones playback is sitting on. Whatever
    # no zone claims is swept once discovery has settled.
    _announce_firewall_rules(argv)
    threading.Thread(target=_sweep_unadopted_sinks,
                     name='sink-sweep',
                     daemon=True).start()
    threading.Thread(target=_watch_addresses,
                     args=(_nics_from_argv(argv),),
                     name='address-watch',
                     daemon=True).start()
    threading.Thread(target=_watch_zone_groups,
                     name='zone-group-watch',
                     daemon=True).start()
    threading.Thread(target=_watch_zone_group_events,
                     args=(_nics_from_argv(argv),),
                     name='zone-group-events',
                     daemon=True).start()
    return _pa_dlna.main()


if __name__ == '__main__':
    sys.exit(main())
