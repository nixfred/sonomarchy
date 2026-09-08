# Changelog

## 0.1.12 — 2026-09-08

0.1.11 did not load. It added a second `Component.onDestruction` to a file that
already had one, which is a compile error — `Property value set multiple
times` — so the shell logged `service plugin load failed` and every Sonos
output disappeared until it was fixed. The validator was green the whole time.

- Fixed: the duplicate handler. The teardown warning now lives in the existing
  `Component.onDestruction`, next to the `expectedStop` it already set.
- Added: **the validator now compiles the QML.** `qmlcachegen` is the real QML
  compiler and fails on exactly the error that shipped; `qmllint` returns 0 for
  it, so it can only ever be the style pass. Proven against a copy of the
  broken file before being trusted.
- Fixed: both Qt tools were looked up with a bare `command -v`, and Arch keeps
  them in `/usr/lib/qt6/`, which is not on `PATH`. The `qmllint` step had
  therefore been silently skipped since the day it was added.
- Verified live this time, rather than by inspection: the backend was SIGKILLed
  and the journal answered
  `backend stopped unexpectedly (killed by a signal); restarting in 2000 ms,
  attempt 1`, followed by the resume sweep putting the stream back.

## 0.1.11 — 2026-09-08

Diagnosis only: no behaviour change. Answering "is the music skipping a bug or
the network?" took an hour of comparing file mtimes against process start
times, because a backend that is torn down and restarted looks, in the journal,
exactly like a stream that stopped and came back.

- Added: **the backend now says why it restarted.** `onExited` logs the exit
  code (and whether a signal ended it), whether the restart was deliberate and
  for what reason, and how long the backoff will be. The `restart` message from
  the backend logs its `reason` — `address_lost` or `grouping_changed` — where
  previously it set a property nothing ever printed.
- Added: a line when the shell unloads this plugin's service with the backend
  still running. Any local plugin changing on disk makes the shell unload
  *every* plugin service, so editing an unrelated plugin restarts Sonomarchy
  and cuts the stream for the ~6 s the replacement needs to rediscover the
  household. That is the shell's design, not a fault, and it is out of this
  plugin's hands — but it should not be invisible.
- Note: a plugin reload re-instantiates the *cached* compiled QML, so an edit
  to `Service.qml` does not take effect until `omarchy restart shell`. Measured
  on 2026-09-08 with a marker in `Component.onCompleted`: the reload spawned a
  fresh backend and reset the once-per-instance firewall hint, but ran the
  pre-edit code. Only `sonomarchy.py` is genuinely hot-reloaded.

## 0.1.10 — 2026-09-07

Music kept hopping from the Sonos to the laptop speakers and back. Two causes,
both ours, found by instrumenting rather than guessing.

- Fixed: **the backend wrote Python bytecode into its own plugin directory,
  and the shell reloads a plugin when that directory changes.** The reload
  tore down the backend, and the replacement wrote the `.pyc` again. Caught
  with inotify: the reload fired in the same second as the `MOVED_TO` of
  `sonomarchy.cpython-314.pyc`. The backend, the test runner and the validator
  now all run Python with `-B`, so nothing lands in the watched directory.
- Fixed: **a backend restart dropped every Sonos output.** Unloading the
  null-sinks makes the zones vanish from PipeWire, so anything playing is
  moved to the built-in speakers, and moved back when the sinks reappear —
  audible as the output device flipping. Sinks are now left loaded when the
  control point shuts down and adopted by the replacement, so playback stays
  where it is. Verified on hardware: the backend was killed mid-playback, the
  three sinks survived, the stream never left the Sonos, and both rooms of the
  group kept playing.
  - A renderer that genuinely goes away still unloads its sink; only a
    shutdown of the whole control point keeps them, which is the case where
    something is coming back.
  - A sink whose label no longer matches is reloaded rather than adopted —
    that is the regroup path from 0.1.7, where the sound menu *must* change.
- Changed: FIX 9's startup sweep of every Sonos null-sink is gone; with sinks
  now adopted, clearing them at startup would throw away the one playback is
  sitting on. Whatever no zone claims is swept 90 s in, once discovery has
  settled and a stale sink can be told from one about to be claimed.

## 0.1.9 — 2026-09-07

Hardening pass over the grouping work, driven by attacking it rather than by
re-reading it. Everything here was found by fuzzing the parser, timing the
event path, or watching a real regroup on real speakers with the debug log on.

- Fixed: a follower whose coordinator was not itself a playable zone got
  hidden with nothing taking its place, costing the user the one speaker they
  could actually play to. Found by fuzzing a topology whose Coordinator is not
  among its own members. A follower is now hidden only when its coordinator is
  a zone we would really register.
- Fixed: the event thread slept before its first turn, so nothing was
  subscribed for a full poll interval after every start — and since a grouping
  change restarts the backend, that blind window landed exactly when the next
  change was likeliest. It now acts first and sleeps after, and retries every
  5 s while unsubscribed instead of every 30 s. Measured on hardware: 108 s
  from start to subscribed, now 5.0 s.
- Fixed: a NOTIFY reply left the connection open, so a body we could not drain
  — a chunked one carries no Content-Length — would have been read as the start
  of the next request. One NOTIFY per connection now.
- Fixed: a speaker granting a subscription lease longer than we asked for
  would have had us schedule the renewal past the point the lease actually
  lapsed, stopping events with nothing logged. Leases are clamped both ways.
- Added: a cap on grouping-driven rebuilds — five per five minutes. A group
  that keeps forming and dissolving (a member on failing wifi) would otherwise
  exit the backend every few seconds forever, because Service.qml treats a
  deliberate restart as fast and does not apply its crash backoff. The budget
  sits above human fiddling on purpose: someone regrouping rooms in the Sonos
  app can easily make three or four changes in a couple of minutes and every
  one must be honoured. It survives restarts by living on disk, and anything
  unreadable, corrupt, or stamped in the future fails open.

## 0.1.8 — 2026-09-07

Regrouping shows up at once instead of within half a minute — where the
network lets it.

- Added: Sonomarchy subscribes to ZoneGroupTopology events, so a Sonos tells
  us the moment rooms are grouped or ungrouped rather than waiting to be
  asked. The NOTIFY body is ignored on purpose: it only wakes the watchdog,
  which then re-reads the topology over SOAP exactly as before, so there is
  no second parser and no dependence on event ordering.
- **Eventing is a fast path, never a dependency.** The speaker connects back
  to this machine, so a default-deny firewall drops the callback and events
  silently never arrive — measured on the development machine, where ufw
  allowed only the audio and discovery ports. The 15 s poll from 0.1.7 is
  therefore untouched and still catches every regroup; the worst case is
  exactly 0.1.7's behaviour. The startup log now names the port and prints
  the precise `ufw`/`firewalld` rule that makes regrouping instant, and a
  subscription that has produced no events after several minutes says so
  once rather than leaving you to wonder.
- Fixed: `HTTPServer.server_bind()` resolves its bind address with
  `socket.getfqdn()`, a reverse DNS lookup with nothing to answer it for
  0.0.0.0. It blocked the event listener's startup for a full resolver
  timeout, measured at 5.0 s. Skipped; startup is now instant.

## 0.1.7 — 2026-09-06

Grouped rooms are one output now, instead of one broken sink per room.

- Added: a Sonos group appears as a single output named after every room in
  it ("Living Room + Office"). Only the group's coordinator gets a sink;
  Sonos relays the stream to the other rooms itself. Previously every member
  got its own sink, and selecting a follower did not play to the group — a
  `SetAVTransportURI` to a grouped member pulls it *out* of the group and
  plays it alone, the opposite of what grouping the rooms asked for.
  The label is alphabetical rather than coordinator-first, because Sonos
  reassigns the coordinator on its own and the output would otherwise be
  renamed for no visible reason.
- Added: the grouping is watched while running (every 15 s). Group or
  ungroup in the Sonos app and the outputs are rebuilt to match, by exiting
  for the shell to restart — the same mechanism an address change uses. A
  change has to hold for two polls before it counts, because Sonos reports
  transient groupings mid-regroup, and the rebuild waits for playback to end
  rather than cutting the music: Sonos keeps a group in sync with its
  coordinator, so audio in flight is unaffected either way. Speakers that
  stop answering are never mistaken for a regrouping.
- Fixed: the zone topology was HTML-unescaped twice by hand, which mangled
  any room name containing an "&". It is now unescaped exactly once, by the
  XML parser.
- Fixed: `VERSION` in the backend still said 0.1.2 while the manifest said
  0.1.6, so the `starting` event reported a version four releases old.

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
