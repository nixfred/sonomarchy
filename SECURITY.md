# Security

## What this plugin does on your network

- **Listens on TCP port 8080** (or the next free port up to 8089) on your LAN
  addresses. This is the audio stream the speakers fetch. pa-dlna only accepts
  connections from the IP addresses of renderers it discovered; anything else
  is dropped and logged ("Discarded TCP connection ... not allowed").
- **Sends SSDP discovery** to the multicast group 239.255.255.250:1900 and
  listens for replies on UDP 8081.
- **Talks UPnP/SOAP over HTTP to your Sonos players on port 1400**: transport
  control (set stream URL, play, stop), the zone topology (to hide bonded
  satellites), and `EndDirectControlSession` when a zone is held by Spotify
  Connect.
- Never contacts the Internet after the first start. The first start installs
  the hash-locked Python dependencies from PyPI into a private venv under
  `~/.local/share/io.github.nixfred.sonomarchy/`.

## What it does not do

- No Sonos account, no cloud, no telemetry.
- No privilege escalation; it never calls sudo. Firewall rules are yours to
  add (see README).
- Never writes inside the plugin directory.

## Dependencies

Runtime Python dependencies are pinned with hashes in `requirements.lock`
(`pa-dlna`, `libpulse`, `psutil`). Regenerate with
`uv pip compile requirements.in --generate-hashes -o requirements.lock` and
review the diff.

## Reporting

Open a GitHub issue. If it's sensitive, say so in the title and we'll take it
to email.
