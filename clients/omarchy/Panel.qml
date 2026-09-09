import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "projectmushroom.gravedecay"
  ipcTarget: moduleName
  manageIpc: false
  property var nodes: []
  property string selectedId: ""
  property bool refreshing: false
  property bool inventoryReady: false
  property string discoveryMessage: ""
  property string storageMessage: ""
  readonly property string inventoryDirectory: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state") + "/gravedecay"
  readonly property int refreshIntervalSec: Math.max(30, Math.min(60, Number(settings.refreshIntervalSec || 45)))
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property var current: nodes.filter(function(n) { return n.id === selectedId })[0] || null
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (refreshing || !inventoryReady) return
    refreshing = true; discovered = []; status.running = true
  }
  function saveInventory() {
    if (inventoryReady && inventory.path) inventory.setText(JSON.stringify({nodes: nodes, selectedId: selectedId}))
  }
  function finishScan(message) {
    nodes = Model.merge(nodes, discovered)
    if (!selectedId && nodes.length) selectedId = nodes[0].id
    discoveryMessage = message; refreshing = false; saveInventory()
  }
  function forgetCurrent() {
    nodes = nodes.filter(function(n) { return n.id !== selectedId })
    selectedId = nodes.length ? nodes[0].id : ""; saveInventory()
  }
  onSelectedIdChanged: saveInventory()
  function parseStatus(raw) {
    var parsed; try { parsed = JSON.parse(raw) } catch (_) { finishScan("Tailscale unavailable"); return }
    if (parsed.BackendState !== "Running") { finishScan("Connect to Tailscale to refresh plots"); return }
    pending = Model.candidates(parsed).slice(0, 64); probeNext()
  }
  property var pending: []
  property var discovered: []
  property int activeProbes: 0
  function probeNext() {
    while (pending.length && activeProbes < 8) {
      var job = probeComponent.createObject(root, {candidate: pending.shift()})
      if (job) { activeProbes++; job.running = true }
    }
    if (!pending.length && activeProbes === 0) finishScan("")
  }
  function openLink(name) { var path = current && current.summary ? Model.safePath(current.summary.links[name]) : ""; if (current && current.reachable && path) Qt.openUrlExternally("https://" + current.dns + path) }
  function fmt(value, suffix) { return value === null || value === undefined ? "—" : Number(value).toFixed(1) + suffix }

  onOpenedChanged: if (opened) { refresh(); Qt.callLater(function() { keyCatcher.forceActiveFocus() }) }
  Component.onCompleted: prepareInventory.running = true
  Process {
    id: prepareInventory; command: ["mkdir", "-p", "-m", "700", root.inventoryDirectory]
    onExited: function(exitCode) {
      if (exitCode === 0) inventory.path = root.inventoryDirectory + "/plots.json"
      else { root.storageMessage = "Could not save plots"; root.inventoryReady = true; root.refresh() }
    }
  }
  FileView {
    id: inventory; printErrors: false; atomicWrites: true
    onLoaded: {
      if (root.inventoryReady) return
      root.nodes = Model.restore(text())
      try { var value = JSON.parse(text()).selectedId; if (typeof value === "string") root.selectedId = value } catch (_) {}
      root.inventoryReady = true; root.refresh()
    }
    onLoadFailed: { if (!root.inventoryReady) { root.inventoryReady = true; root.refresh() } }
    onSaveFailed: root.storageMessage = "Could not save plots"
    onSaved: root.storageMessage = ""
  }
  Timer { interval: root.refreshIntervalSec * 1000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }
  Timer { interval: 4000; running: status.running; onTriggered: status.signal(9) }
  Process { id: status; command: ["tailscale", "status", "--json"]; stdout: StdioCollector { id: statusStdout; waitForEnd: true }; onExited: function(exitCode) { if (exitCode === 0) root.parseStatus(String(statusStdout.text || "")); else root.finishScan("Tailscale unavailable") } }
  Component {
    id: probeComponent
    Process {
      id: curl; property var candidate
      command: ["curl", "-q", "--silent", "--show-error", "--fail", "--noproxy", "*", "--connect-timeout", "2", "--max-time", "3", "--max-filesize", "65536", "https://" + candidate.dns + "/grave/api/v1/summary"]
      stdout: StdioCollector { id: curlStdout; waitForEnd: true }
      onExited: function(exitCode) {
        var value = exitCode === 0 ? Model.summary(String(curlStdout.text || "")) : null
        if (value) { var node = candidate; node.summary = value; node.reachable = true; node.lastSeen = Date.now(); root.discovered.push(node) }
        root.activeProbes--; root.probeNext(); destroy()
      }
    }
  }

  BarIconButton {
    id: button; anchors.fill: parent; bar: root.bar
    iconComponent: Component { GraveIcon { color: root.current && root.current.reachable ? (root.current.summary.health.services_failed + root.current.summary.health.containers_problem ? root.urgent : root.foreground) : root.dim } }
    tooltipText: "Graveyard: " + root.nodes.filter(function(n) { return n.reachable }).length + " online / " + root.nodes.length + " plots"
    onPressed: function(buttonCode) { if (buttonCode === Qt.RightButton || buttonCode === Qt.MiddleButton) root.refresh(); else root.toggle() }
  }
  KeyboardPanel {
    id: panel; anchorItem: button; owner: root; bar: root.bar; open: root.opened; focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(330))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(420))
    PanelKeyCatcher {
      id: keyCatcher; anchors.fill: parent; onCloseRequested: root.close(); onMoveRequested: function(dx, dy) { if (dy && root.nodes.length) { var index = root.nodes.indexOf(root.current); root.selectedId = root.nodes[(index + dy + root.nodes.length) % root.nodes.length].id } }; onTabRequested: function(direction) { root.switchPanel(direction) }; onTextKey: function(text) { if (text === "r" || text === "R") root.refresh() }
      ColumnLayout { id: content; anchors.fill: parent; spacing: Style.space(8)
        RowLayout { Layout.fillWidth: true; Text { text: "Graveyard"; color: root.foreground; font.bold: true; font.pixelSize: Style.font.title }; Item { Layout.fillWidth: true }; PanelActionButton { iconText: "󰑐"; onClicked: root.refresh() } }
        Dropdown { id: nodePicker; visible: root.nodes.length > 1; width: parent.width; showLabel: false; options: root.nodes.map(function(n) { return { value: n.id, label: n.name + (n.reachable ? "" : " · unreachable") } }); onChanged: function(value) { root.selectedId = value }; Connections { target: root; function onSelectedIdChanged() { nodePicker.value = root.selectedId } } }
        Text { visible: !root.nodes.length && !root.refreshing; text: "No plots found yet"; color: root.dim }
        Text { visible: root.refreshing; text: "Checking plots…"; color: root.dim }
        Text { visible: !!root.discoveryMessage || !!root.storageMessage; text: root.storageMessage || root.discoveryMessage; color: root.dim; wrapMode: Text.Wrap; Layout.fillWidth: true }
        ScrollView {
          visible: root.nodes.length > 1; Layout.fillWidth: true; Layout.preferredHeight: Math.min(root.nodes.length * Style.space(38), Style.space(140)); clip: true
          ColumnLayout { width: parent.width
            Repeater { model: root.nodes
              Button { required property var modelData; Layout.fillWidth: true; text: modelData.name + " · " + (modelData.reachable ? ((modelData.summary.health.services_failed + modelData.summary.health.containers_problem) ? "needs attention" : modelData.summary.activity.sessions_live + " sessions") : "unreachable"); onClicked: root.selectedId = modelData.id }
            }
          }
        }
        ColumnLayout { visible: !!root.current; Layout.fillWidth: true; spacing: Style.space(4)
          Text { text: root.current ? root.current.summary.node.host + " · " + root.current.summary.node.mode : ""; color: root.foreground; font.bold: true }
          Text { visible: root.current && !root.current.reachable; text: root.current ? "Unreachable · last seen " + new Date(root.current.lastSeen).toLocaleString() : ""; color: root.dim; wrapMode: Text.Wrap; Layout.fillWidth: true }
          Text { visible: root.current && root.current.reachable; text: root.current ? "CPU " + root.fmt(root.current.summary.resources.cpu_pct, "%") + "  RAM " + root.fmt(root.current.summary.resources.memory_pct, "%") + "  Disk " + root.fmt(root.current.summary.resources.disk_pct, "%") : ""; color: root.dim }
          Text { visible: root.current && root.current.reachable; text: root.current ? "Sessions " + root.current.summary.activity.sessions_live + " live / " + root.current.summary.activity.sessions_frozen + " frozen · Problems " + (root.current.summary.health.services_failed + root.current.summary.health.containers_problem) : ""; color: root.current && root.current.summary.health.services_failed + root.current.summary.health.containers_problem ? root.urgent : root.dim }
          Flow { visible: root.current && root.current.reachable; Layout.fillWidth: true; spacing: Style.space(4); Button { text: "Dashboard"; visible: root.current && !!root.current.summary.links.dashboard; onClicked: root.openLink("dashboard") }; Button { text: "T3"; visible: root.current && !!root.current.summary.links.t3; onClicked: root.openLink("t3") }; Button { text: "Terminal"; visible: root.current && !!root.current.summary.links.terminal; onClicked: root.openLink("terminal") }; Button { text: "Network"; visible: root.current && !!root.current.summary.links.network; onClicked: root.openLink("network") } }
          Button { text: "Forget plot"; visible: root.current && !root.current.reachable && !root.refreshing; onClicked: root.forgetCurrent() }
        }
      }
    }
  }
}
