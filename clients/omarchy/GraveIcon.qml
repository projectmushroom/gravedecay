import QtQuick

Item {
  id: root
  property color color: "white"
  implicitWidth: 18
  implicitHeight: 18
  Rectangle { x: 7; y: 1; width: 4; height: 8; color: root.color }
  Rectangle { x: 4; y: 4; width: 10; height: 3; color: root.color }
  Rectangle { x: 3; y: 9; width: 12; height: 8; radius: 2; color: root.color }
  Rectangle { x: 6; y: 12; width: 6; height: 1; color: "transparent"; border.color: Qt.rgba(0, 0, 0, .45); border.width: 1 }
}
