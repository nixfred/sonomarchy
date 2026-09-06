# Changelog

## 0.1.6 — 2026-09-06

Music stuttered every few minutes: roughly 17 seconds of silence, then the
resume sweep put it back. It was not the network and not the speaker.

- Fixed: a short-lived sink-input tore down a completely different stream.
  pa-dlna decides a zone is idle from one pointer that follows whichever
  sink-input last raised an event, so any brief stream — a notification, a
  UI sound, a player reopening its PCM — captures it. When the brief stream
  ends, the idle check sees the pointer naming it and closes the encoder out
  from under the stream that never stopped. Caught in the debug log: input
  10570 lived 19 s, was removed, and took down input 8210, which had been
  playing for the previous hour. The idle check now asks the sink whether
  anything is still playing into it instead of trusting the pointer, and
  adopts whatever it finds. Raising `TRACK_CHANGE_GRACE` could not have
  fixed this — no further event was ever coming for 8210, so a longer wait
  only made the gap longer.

## 0.1.5 — 2026-09-05

- Fixed: 0.1.4's startup firewall hint never appeared. It was logged at INFO,
  and the stderr forwarder in `Service.qml` passes only WARNING and ERROR to
  the shell journal, so the one line meant to save people an hour was dropped
  before anyone could read it. The backend now also emits it as a structured
  event and the shell logs it once per backend. It stays out of WARNING on
  purpose: a running firewall is not a fault, and a warning that is not a
  fault teaches people to ignore warnings.
- Fixed: the README claimed the hint was "logged at startup" without saying
  where to find it. It now shows the line and the `journalctl` incantation.

## 0.1.4 — 2026-09-05

The firewall advice was correct and useless in the same breath: it named two
ports but left the reader to work out their own subnet, their own port (8080
is only the first choice of ten) and their own firewall's syntax — three
chances to get it wrong while the speakers sit there silent.

- Added: at startup, if a firewall service is running, the backend logs the
  exact rule for this machine's subnet and ports, ready to paste. `ufw` and
  `firewalld` syntax; log only, no notification, since a running firewall is
  not itself a fault.
- Added: FIX 8's warning now ends with that same exact command, so the
  moment a speaker fails to fetch, the fix is on the screen rather than in
  the README.
- Added: the OSD names the port and subnet to allow instead of saying
  "allow it in your firewall".
- Deliberately not done: the plugin still never reads or writes firewall
  rules and asks for no elevated rights. Reading them needs privileges we do
  not have, so it can only report that a firewall is *running* and what the
  rule would be — never that the port is actually blocked. A hint that
  overstated its evidence is what sent this project's own author hunting the
  wrong cause for an hour.

## 0.1.3 — 2026-09-05

A VPN that advertises your LAN subnet breaks playback in a way that is
indistinguishable, from the outside, from a blocked firewall port — and FIX 8
confidently blamed the firewall. Found on a laptop whose Tailscale had
accepted a subnet route for the LAN it was already sitting on: the speaker's
SYN arrived on ethernet, every SYN-ACK was routed into `tailscale0` and died
there, so the handshake never completed. Ports were open, rules were correct,
and the log pointed at the wrong thing.

- Fixed: FIX 8 now asks `ip route get <speaker> from <host>` before it
  accuses the firewall. If the reply leaves through a tunnel
  (`tailscale*`, `wg*`, `tun*`, `ppp*`, `zt*`, `utun*`, `nebula*`) the
  warning names that interface and the fix; otherwise it gives the firewall
  advice as before, now with routing named as the next thing to check. When
  the route cannot be determined the probe has no opinion and falls back.
- Fixed: the OSD and `lastError` said "a firewall is probably blocking it"
  unconditionally. They follow the same distinction via a new `reply_dev`
  field on the `firewall_suspected` event.
- Added: a README section on VPN subnet routes, with the one-line check and
  both fixes — dropping the advertisement on the subnet router (helps every
  device on the tailnet) or `--accept-routes=false` on the one machine.
- Added: tests for the route parser and the tunnel classifier, including
  that ordinary interface names are never flagged — a false positive there
  would reproduce this same bug in mirror image.

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
