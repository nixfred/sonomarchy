import QtQuick
import Quickshell
import Quickshell.Io

// Sonomarchy — Sonos zones as system audio outputs.
//
// This service owns one long-lived backend process (sonomarchy-backend →
// sonomarchy.py). The backend discovers Sonos zones over UPnP and registers a
// PipeWire null-sink per zone; the stock Omarchy audio panel then lists them
// like any other output. There is no widget: the audio panel is the UI.
//
// The backend deliberately exits when the machine's network address changes
// (see sonomarchy.py, "address lost"), because every stream URL it handed to
// the speakers would otherwise point at a dead address. This file restarts it
// with exponential backoff. A setup failure (missing dependency, another
// pa-dlna already running) is NOT retried in a loop: it is surfaced and waits
// for `restart()` over IPC.
Item {
  id: root

  property var settings: ({})

  readonly property string moduleName: "io.github.nixfred.sonomarchy"
  readonly property string backendPath: localPath(Qt.resolvedUrl("sonomarchy-backend"))

  property string state: "starting"           // starting | ready | restarting | setup_error
  property string lastError: ""
  property string setupError: ""
  property var zones: ({})                    // uuid -> { name, sink }
  property int restartAttempt: 0
  property bool expectedStop: false
  property bool healthyThisRun: false
  property bool firewallWarned: false
  property string restartReason: ""

  readonly property int zoneCount: Object.keys(zones).length

  function localPath(url) {
    var text = String(url || "")
    if (text.indexOf("file://") === 0) text = text.substring(7)
    return decodeURIComponent(text)
  }

  function plainText(value, limit) {
    return String(value || "").slice(0, limit).replace(/[\x00-\x1f\x7f]/g, " ").trim()
  }

  function zoneNames() {
    var names = []
    for (var uuid in zones) names.push(zones[uuid].name)
    names.sort()
    return names
  }

  function status() {
    var text = "Sonomarchy: " + state + ", " + zoneCount + " zone" + (zoneCount === 1 ? "" : "s")
    if (zoneCount > 0) text += " (" + zoneNames().join(", ") + ")"
    else if (state === "ready") text += " (no Sonos found on this network yet; discovery keeps running)"
    if (lastError !== "") text += ", error=" + lastError
    return text
  }

  function restart() {
    setupError = ""
    lastError = ""
    restartAttempt = 0
    firewallWarned = false
    if (backend.running) {
      // The backend stops cleanly on SIGTERM and onExited brings it back.
      restartReason = "ipc"
      backend.signal(15)
    } else {
      state = "starting"
      backend.running = true
    }
  }

  function notify(icon, message) {
    osd.command = ["omarchy-osd", "-i", icon, "-m", message]
    osd.running = true
  }

  // One JSON object per line on stdout. Anything that is not JSON is ignored so
  // a stray print in a dependency can never wedge the parser.
  function handleLine(line) {
    var text = String(line || "").trim()
    if (text.charAt(0) !== "{") return
    var message
    try { message = JSON.parse(text) } catch (e) { return }
    switch (String(message.type || "")) {
      case "starting":
        state = "starting"
        break
      case "zone": {
        var next = {}
        for (var key in zones) next[key] = zones[key]
        next[String(message.uuid)] = { name: plainText(message.name, 64), sink: plainText(message.sink, 128) }
        zones = next
        state = "ready"
        healthyThisRun = true
        restartAttempt = 0
        break
      }
      case "zone_gone": {
        var remaining = {}
        for (var uuid in zones) if (uuid !== String(message.uuid)) remaining[uuid] = zones[uuid]
        zones = remaining
        break
      }
      case "firewall_suspected":
        lastError = "Sonos never fetched the stream on port " + message.port + " — a firewall is probably blocking it"
        if (!firewallWarned) {
          firewallWarned = true
          notify("volume-muted", "Sonomarchy: " + plainText(message.zone, 40)
            + " can't reach port " + message.port + " — allow it in your firewall")
        }
        break
      case "restart":
        restartReason = plainText(message.reason, 64)
        state = "restarting"
        break
      case "cleanup":
        // Runs before pa-dlna configures its logging, so this is the only
        // place the event is visible.
        console.warn("Sonomarchy: unloaded " + parseInt(message.unloaded, 10)
          + " stale sink(s) left behind by a previous backend")
        break
    }
  }

  Process {
    id: backend
    command: [root.backendPath]

    stdout: SplitParser {
      onRead: function(line) { root.handleLine(line) }
    }

    stderr: SplitParser {
      onRead: function(line) {
        var text = root.plainText(line, 512)
        if (text === "") return
        var marker = "SONOMARCHY_SETUP_ERROR:"
        if (text.indexOf(marker) === 0) {
          root.setupError = text.substring(marker.length).trim()
          console.warn("Sonomarchy:", text)
          return
        }
        // pa-dlna's own log lines land here. Every discovery pass makes it
        // complain about unrelated UPnP devices on the LAN (TVs, routers) whose
        // descriptions it cannot parse; that is not our problem and would spam
        // the shell log once a minute. Forward only what concerns a Sonos or us.
        var severe = text.indexOf(" ERROR ") >= 0 || text.indexOf(" WARNING ") >= 0
        // pa-dlna abbreviates ids ("RINCO...01400"), so match the prefix.
        var foreignDevice = text.indexOf("UPnPRootDevice") >= 0 && text.indexOf("RINCO") < 0
        if (severe && !foreignDevice) console.warn("Sonomarchy:", text)
      }
    }

    onStarted: {
      root.healthyThisRun = false
      root.setupError = ""
      root.zones = ({})
      settleTimer.restart()
    }

    onExited: function(exitCode) {
      root.zones = ({})
      if (root.expectedStop) return

      if (!root.healthyThisRun && root.setupError !== "") {
        // Nothing will change until the user fixes it; do not spin.
        root.state = "setup_error"
        root.lastError = root.setupError
        root.notify("volume-muted", "Sonomarchy can't start: " + root.setupError)
        return
      }

      // A deliberate exit (address change, IPC restart) comes back fast; a
      // crash backs off up to 30 s.
      var deliberate = root.restartReason !== ""
      root.restartReason = ""
      root.state = "restarting"
      if (!deliberate) {
        root.lastError = "backend stopped (" + exitCode + ")"
        root.restartAttempt = Math.min(root.restartAttempt + 1, 6)
      }
      restartTimer.interval = deliberate ? 1000 : Math.min(30000, 1000 * Math.pow(2, root.restartAttempt))
      restartTimer.restart()
    }
  }

  Process {
    id: osd
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: if (!root.expectedStop && !backend.running) backend.running = true
  }

  // "ready" is normally set by the first zone. On a network with no Sonos
  // that never happens, and "starting" forever reads like a failure. If the
  // backend has been up this long without a setup error, it is simply
  // waiting for speakers to answer discovery.
  Timer {
    id: settleTimer
    interval: 15000
    repeat: false
    onTriggered: if (root.state === "starting" && backend.running && root.setupError === "") root.state = "ready"
  }

  IpcHandler {
    target: "io.github.nixfred.sonomarchy"
    function status(): string { return root.status() }
    function zones(): string { return JSON.stringify(root.zones) }
    function restart(): void { root.restart() }
  }

  Component.onCompleted: backend.running = true
  Component.onDestruction: {
    expectedStop = true
    backend.running = false
  }
}
