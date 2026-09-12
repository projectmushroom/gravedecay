import QtQuick
import Quickshell.Io

// Tint the bundled vector before Qt rasterizes it. This also works with
// software rendering, where shader-based color overlays are unavailable.
Item {
  id: root
  property string iconName: "tux"
  property color color: "white"
  property string artwork: ""
  implicitWidth: 16
  implicitHeight: 16
  Image {
    anchors.fill: parent
    sourceSize: Qt.size(root.width, root.height)
    fillMode: Image.PreserveAspectFit
    source: root.artwork ? "data:image/svg+xml," + encodeURIComponent(root.artwork.replace("<svg ", '<svg fill="' + root.color + '" ')) : ""
  }
  FileView {
    path: decodeURIComponent(Qt.resolvedUrl("os-logos/" + root.iconName + ".svg").toString().replace(/^file:\/\//, ""))
    onLoaded: root.artwork = text()
    onLoadFailed: root.artwork = ""
  }
}
