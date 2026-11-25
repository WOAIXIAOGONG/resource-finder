"""Value controls leave wheel gestures to the surrounding scroll area."""
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QComboBox, QSpinBox, QDoubleSpinBox


def scroll_parent(widget, event):
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            pixels = event.pixelDelta()
            if not pixels.isNull():
                horizontal = parent.horizontalScrollBar()
                vertical = parent.verticalScrollBar()
                before = (horizontal.value(), vertical.value())
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    horizontal.setValue(horizontal.value() - (pixels.x() or pixels.y()))
                else:
                    horizontal.setValue(horizontal.value() - pixels.x())
                    vertical.setValue(vertical.value() - pixels.y())
                event.setAccepted(before != (horizontal.value(), vertical.value()))
                return
            viewport = parent.viewport()
            forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                event.globalPosition(), event.pixelDelta(), event.angleDelta(), event.buttons(),
                event.modifiers(), event.phase(), event.inverted(), event.source(), event.pointingDevice())
            QCoreApplication.sendEvent(viewport, forwarded)
            event.setAccepted(forwarded.isAccepted())
            return
        parent = parent.parentWidget()
    event.ignore()


class _WheelSafe:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_editor = self.lineEdit()
        if self._wheel_editor is not None:
            self._wheel_editor.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self._wheel_editor and event.type() == QEvent.Type.Wheel:
            scroll_parent(self, event)
            return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event):
        scroll_parent(self, event)


class WheelSafeComboBox(_WheelSafe, QComboBox):
    pass


class WheelSafeSpinBox(_WheelSafe, QSpinBox):
    pass


class WheelSafeDoubleSpinBox(_WheelSafe, QDoubleSpinBox):
    pass
