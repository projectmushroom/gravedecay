pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Effects

Item {
  id: root
  property url source
  property color color: "white"
  implicitWidth: 16
  implicitHeight: 16
  Image {
    id: mask
    anchors.fill: parent
    source: root.source
    sourceSize: Qt.size(root.width, root.height)
    fillMode: Image.PreserveAspectFit
    visible: false
    layer.enabled: true
  }
  Rectangle {
    anchors.fill: parent
    color: root.color
    layer.enabled: true
    layer.effect: MultiEffect { maskEnabled: true; maskSource: mask }
  }
}
