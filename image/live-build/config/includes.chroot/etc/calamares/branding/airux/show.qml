import QtQuick 2.0;
import calamares.slideshow 1.0;

Presentation {
    id: presentation
    Slide {
        Rectangle { anchors.fill: parent; color: "#0d1117" }
        Text {
            anchors.centerIn: parent
            color: "#e6e9ee"; font.pixelSize: 22; horizontalAlignment: Text.AlignHCenter
            text: "Installing AIRUX.<br/><br/>When it finishes, AIRA -- your Adaptive Intelligent Research<br/>" +
                  "Assistant -- will be waiting on the desktop."
        }
    }
}
