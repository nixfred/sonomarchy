# Changelog

## 0.1.2 — 2026-09-04

Fixes from an independent adversarial review (Codex), each confirmed in the
code before changing it.

- Fixed: every track ended with pa-dlna's chunked-transfer terminator
  (`0\r\n\r\n`) written into what is a fixed-length body — five garbage
  bytes in the MP3 that the replay ring never saw, so a speaker that had
  consumed them asked to resume five bytes past our window and was refused.
- Fixed: a `HEAD` request during playback silenced the zone (it fell into
  the takeover path). `HEAD` is answered statically; other methods get 405.
- Fixed: tearing down a stale stream used `stop_track`, which by design
  keeps the capture process; a leftover `parec` could linger. The whole
  chain is closed now, and a residual capture process is itself detected.
- Fixed: the sweep's start grace began only after `Play` returned; the
  normal start does several SOAP round-trips first. Marked at entry.
- Fixed: a reconnect without `Range` inherited the previous stream's
  offsets; a later `Range` could be served the wrong bytes. Reset on any
  plain request. Only the exact `bytes=N-` form is treated as a resume.
- Fixed: the launcher had no instance lock across `exec`; a plugin reload
  could race the previous backend and land in a setup error the shell never
  retries. A lock is now held for the backend's life with a 20 s wait; the
  running-instance check no longer relies on a `pipefail`-sensitive pipe;
  `pactl`, `flock` and `sha256sum` are checked like the other dependencies;
  `SONOMARCHY_HTTP_PORT` is honoured exactly; `SONOMARCHY_DRY_RUN=1`
  prints the resolved arguments for tests and support.
- Fixed: the status kept `error=…` from a previous run after a healthy
  restart; a healthy settle or a zone arriving now clears it and the backoff.
- Fixed: raw-PCM (L16) renderers, which have no encoder process, were judged
  permanently dead by the sweep.
- Fixed: per-zone takeover/idle state is purged when a zone closes.

## 0.1.1 — 2026-09-04

- Fixed: a zone could go silent for good while the application kept playing
  into it, and could then vanish from the output list. Verified on real
  hardware: a Playbar retries a broken stream with a `Range` request; the
  backend answered `200` and a fresh stream, the speaker reset the
  connection, and pa-dlna's reaction to any reset was to close the whole
  renderer — sink unloaded, zone gone. Now: an unexpected EOF clears the
  session so the retry is served instead of refused with 409; a `Range`
  retry gets a proper `206 Partial Content`; a reset closes only the session
  and keeps the zone; a new connection takes over a stale one; and an 8 s
  sweep restarts any zone that has a player but no stream (for speakers
  that give up without retrying). Measured: resume in 150 ms via the retry,
  within 10 s via the sweep, zones stable throughout.
- Fixed: a `Range` retry is now a real resume. The last ~65 s of encoded
  audio is kept per zone; a speaker reconnecting with `Range: bytes=N-` is
  sent the bytes it actually missed, then the live stream — no reset, no
  gap (a Play:1 rejected a 206 that did not really continue the stream).
- Fixed: the resume sweep leaves a zone alone for 15 s after it starts, so it
  never issues a redundant restart while the normal start is in flight.
- Fixed: after ending a Spotify Connect session the backend waits for the
  player to report STOPPED instead of sleeping a fixed second, which on a
  Play:1 collided with the player's own transition and produced the same 409.
- Added: a support switch — `touch ~/.local/state/io.github.nixfred.sonomarchy/debug`
  and restart the plugin to get a full backend log at `backend.log` next to
  it; remove the file to turn it off.

## 0.1.0 — 2026-09-04

First release.

- One PipeWire output per Sonos zone, listed in the Omarchy audio panel.
- Bonded stereo partners, surrounds and Subs are hidden (ZoneGroupTopology).
- Readable output names: "Office (Sonos Playbar)".
- Works on pre-AirPlay Sonos (Play:1/3/5, Playbar, Connect) via
  Content-Length streaming instead of chunked transfer.
- Survives wifi↔ethernet changes by restarting discovery when an address
  disappears.
- Takes over a zone held by Spotify Connect instead of silently doing nothing.
- Seamless track changes (10 s grace instead of pa-dlna's 2 s).
- Detects a firewall blocking the stream and says which port to open.
- Removes null-sinks a previous backend left behind (plugin update, crash).
- Hash-locked Python backend in a private venv; picks a free port; finds the
  right network interfaces on any machine.
