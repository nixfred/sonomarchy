# Changelog

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
