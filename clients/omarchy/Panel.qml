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
  readonly property int refreshIntervalSec: Math.max(30, Math.min(60, Number(settings.refreshIntervalSec || 45)))
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property var current: nodes.filter(function(n) { return n.id === selectedId })[0] || nodes[0] || null
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (refreshing) return
    refreshing = true; discovered = []; status.running = true
  }
  function parseStatus(raw) {
    var parsed; try { parsed = JSON.parse(raw) } catch (_) { nodes = []; refreshing = false; return }
    pending = Model.candidates(parsed); probeNext()
  }
  property var pending: []
  property var discovered: []
  function probeNext() {
    if (!pending.length) { nodes = discovered; if (nodes.length && !nodes.filter(function(n) { return n.id === selectedId }).length) selectedId = nodes[0].id; refreshing = false; return }
    probing = pending.shift(); curl.command = ["curl", "--silent", "--show-error", "--fail", "--connect-timeout", "2", "--max-time", "3", "--max-filesize", "65536", "https://" + probing.dns + "/grave/api/v1/summary"]; curl.running = true
  }
  property var probing: null
  function openLink(name) { var path = current && current.summary ? Model.safePath(current.summary.links[name]) : ""; if (current && path) Qt.openUrlExternally("https://" + current.dns + path) }
  function fmt(value, suffix) { return value === null || value === undefined ? "—" : Number(value).toFixed(1) + suffix }

  onOpenedChanged: if (opened) { refresh(); Qt.callLater(function() { keyCatcher.forceActiveFocus() }) }
  Timer { interval: root.refreshIntervalSec * 1000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }
  Process { id: status; command: ["tailscale", "status", "--json"]; stdout: StdioCollector { id: statusStdout; waitForEnd: true }; onExited: function(exitCode) { if (exitCode === 0) root.parseStatus(String(statusStdout.text || "")); else { root.nodes = []; root.refreshing = false } } }
  Process { id: curl; stdout: StdioCollector { id: curlStdout; waitForEnd: true }; onExited: function(exitCode) { var value = exitCode === 0 ? Model.summary(String(curlStdout.text || "")) : null; if (value) { var node = root.probing; node.summary = value; root.discovered.push(node) }; root.probeNext() } }

  BarIconButton {
    id: button; anchors.fill: parent; bar: root.bar
    iconComponent: Component { GraveIcon { color: root.nodes.length ? (root.current && root.current.summary.health.services_failed + root.current.summary.health.containers_problem ? root.urgent : root.foreground) : root.dim } }
    tooltipText: "Gravedecay: " + root.nodes.length + " reachable appliances"
    onPressed: function(buttonCode) { if (buttonCode === Qt.RightButton || buttonCode === Qt.MiddleButton) root.refresh(); else root.toggle() }
  }
  KeyboardPanel {
    id: panel; anchorItem: button; owner: root; bar: root.bar; open: root.opened; focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(330))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(420))
    PanelKeyCatcher {
      id: keyCatcher; anchors.fill: parent; onCloseRequested: root.close(); onMoveRequested: function(dx, dy) { if (dy && root.nodes.length) { var index = root.nodes.indexOf(root.current); root.selectedId = root.nodes[(index + dy + root.nodes.length) % root.nodes.length].id } }; onTabRequested: function(direction) { root.switchPanel(direction) }; onTextKey: function(text) { if (text === "r" || text === "R") root.refresh() }
      ColumnLayout { id: content; anchors.fill: parent; spacing: Style.space(8)
        RowLayout { Layout.fillWidth: true; Text { text: "Gravedecay"; color: root.foreground; font.bold: true; font.pixelSize: Style.font.title }; Item { Layout.fillWidth: true }; PanelActionButton { iconText: "󰑐"; onClicked: root.refresh() } }
        Dropdown { id: nodePicker; visible: root.nodes.length > 1; width: parent.width; showLabel: false; options: root.nodes.map(function(n) { return { value: n.id, label: n.name } }); onChanged: function(value) { root.selectedId = value }; Connections { target: root; function onSelectedIdChanged() { nodePicker.value = root.selectedId } } }
        Text { visible: !root.nodes.length && !root.refreshing; text: "No reachable Gravedecay appliances"; color: root.dim }
        Text { visible: root.refreshing; text: "Discovering tailnet appliances…"; color: root.dim }
        ColumnLayout { visible: !!root.current; Layout.fillWidth: true; spacing: Style.space(4)
          Text { text: root.current ? root.current.summary.node.host + " · " + root.current.summary.node.mode : ""; color: root.foreground; font.bold: true }
          Text { text: root.current ? "CPU " + root.fmt(root.current.summary.resources.cpu_pct, "%") + "  RAM " + root.fmt(root.current.summary.resources.memory_pct, "%") + "  Disk " + root.fmt(root.current.summary.resources.disk_pct, "%") : ""; color: root.dim }
          Text { text: root.current ? "Sessions " + root.current.summary.activity.sessions_live + " live / " + root.current.summary.activity.sessions_frozen + " frozen · Problems " + (root.current.summary.health.services_failed + root.current.summary.health.containers_problem) : ""; color: root.current && root.current.summary.health.services_failed + root.current.summary.health.containers_problem ? root.urgent : root.dim }
          RowLayout { Button { text: "Dashboard"; onClicked: root.openLink("dashboard") }; Button { text: "T3"; visible: root.current && root.current.summary.links.t3; onClicked: root.openLink("t3") }; Button { text: "Terminal"; visible: root.current && root.current.summary.links.terminal; onClicked: root.openLink("terminal") }; Button { text: "Network"; onClicked: root.openLink("network") } }
        }
      }
    }
  }
}
