"""Shared dialog composition; native window controls and existing actions stay intact."""
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QTextEdit,
                               QVBoxLayout, QWidget, QLayout)
from . import theme
from .icons import icon


def label(text, name='DialogHint', parent=None):
    result = QLabel(text, parent)
    result.setObjectName(name)
    result.setWordWrap(True)
    result.setTextFormat(Qt.TextFormat.PlainText)
    return result


class StyledDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('StyledDialog')
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        if parent is None:
            self.setStyleSheet(theme.stylesheet('dark'))

    def make_root(self, title, subtitle, symbol='settings', width=600):
        self.setMinimumWidth(460)
        self._preferred_width = width
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(20)
        header = QWidget(self)
        header.setObjectName('DialogHeader')
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        badge = QLabel(header)
        badge.setObjectName('DialogBadge')
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mode = getattr(getattr(self.parent(), 'settings', None), 'theme', 'dark')
        badge.setPixmap(icon(symbol, theme.palette(mode)['accent'], 23).pixmap(QSize(23, 23)))
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setSpacing(5)
        self.heading = label(title, 'DialogTitle', header)
        copy.addWidget(self.heading)
        if subtitle:
            copy.addWidget(label(subtitle, parent=header))
        row.addLayout(copy, 1)
        root.addWidget(header)
        return root

    def finish_layout(self, root, footer=None):
        """Scrollable body with a stable header/footer, including on high-DPI screens."""
        body = QWidget(self)
        body.setObjectName('DialogBody')
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 6, 0)
        content.setSpacing(14)
        content.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        while root.count() > 1:
            item = root.takeAt(1)
            if item.widget():
                content.addWidget(item.widget())
            elif item.layout():
                item.layout().setParent(None)
                content.addLayout(item.layout())
            else:
                content.addItem(item)
        content.addStretch(1)
        area = self.body_scroll = QScrollArea(self)
        area.setObjectName('DialogScroll')
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setMinimumHeight(70)
        area.setWidget(body)
        root.addWidget(area, 1)
        if footer is not None:
            bar = QFrame(self)
            bar.setObjectName('DialogFooter')
            actions = QHBoxLayout(bar)
            actions.setContentsMargins(0, 16, 0, 0)
            if isinstance(footer, QWidget):
                actions.addWidget(footer)
            else:
                actions.addLayout(footer)
            root.addWidget(bar)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if button.objectName() not in ('DialogPrimary', 'Primary'):
                button.setAutoDefault(False)
        # Leave enough room for the natural content while keeping it scrollable.
        self.resize(self._preferred_width, min(720, max(330, body.sizeHint().height() + 170)))

    def showEvent(self, event):
        screen = self.screen()
        if screen:
            available = screen.availableGeometry()
            width = min(self.width(), available.width() - 40)
            height = min(self.height(), available.height() - 72)
            self.setMinimumWidth(min(460, max(300, available.width() - 40)))
            self.resize(width, height)
        super().showEvent(event)


def primary(button):
    button.setObjectName('DialogPrimary')
    button.setDefault(True)


def step_card(parent, steps):
    card = QFrame(parent)
    card.setObjectName('DialogCard')
    rows = QVBoxLayout(card)
    rows.setContentsMargins(18, 16, 18, 16)
    rows.setSpacing(18)
    for index, (title, detail) in enumerate(steps, 1):
        row = QHBoxLayout()
        row.setSpacing(12)
        number = QLabel(str(index), card)
        number.setObjectName('DialogStepNumber')
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number.setFixedSize(26, 26)
        row.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        copy.addWidget(label(title, 'DialogStepTitle', card))
        copy.addWidget(label(detail, parent=card))
        row.addLayout(copy, 1)
        rows.addLayout(row)
    return card


class MessageStyler(QObject):
    """Style Qt message boxes without replacing static APIs or button semantics."""
    def eventFilter(self, obj, event):
        if isinstance(obj, QMessageBox) and event.type() == QEvent.Type.Show:
            # QMessageBox's own showEvent recalculates wrapping; style afterwards.
            QTimer.singleShot(0, lambda: self.polish_message(obj))
        return super().eventFilter(obj, event)

    def polish_message(self, obj):
        from shiboken6 import isValid
        if not isValid(obj) or not obj.isVisible():
            return
        obj.setObjectName('StyledMessage')
        obj.setTextFormat(Qt.TextFormat.PlainText)
        layout = obj.layout()
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(18)
        body = obj.findChild(QLabel, 'qt_msgbox_label')
        if body:
            body.setWordWrap(True)
            width = min(420, max(240, obj.screen().availableGeometry().width() - 180))
            body.setFixedWidth(width)
            if body.heightForWidth(width) > 220 and not obj.detailedText():
                original = obj.text()
                obj.setDetailedText(original)
                preview = '\n'.join(original.splitlines()[:3])[:160]
                obj.setText(preview + '…\n展开详情可查看完整信息。')
                body.setWordWrap(True)
            body.setMinimumHeight(body.heightForWidth(width))
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        parent = obj.parent()
        while parent is not None and not hasattr(parent, 'settings'):
            parent = parent.parent()
        mode = getattr(getattr(parent, 'settings', None), 'theme', 'dark')
        colors = theme.palette(mode)
        pix = QPixmap(88, 88)
        pix.setDevicePixelRatio(2)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors['accent_dim']))
        painter.drawRoundedRect(0, 0, 44, 44, 13, 13)
        painter.setPen(QColor(colors['accent']))
        font = painter.font(); font.setPixelSize(22); font.setBold(True); painter.setFont(font)
        painter.drawText(0, 0, 44, 44, Qt.AlignmentFlag.AlignCenter,
                         '?' if obj.icon() == QMessageBox.Icon.Question else '!')
        painter.end()
        obj.setIconPixmap(pix)
        for button in obj.buttons():
            standard = obj.standardButton(button)
            translations = {QMessageBox.StandardButton.Ok: '知道了', QMessageBox.StandardButton.Yes: '确认',
                            QMessageBox.StandardButton.No: '返回', QMessageBox.StandardButton.Cancel: '取消'}
            if standard in translations:
                button.setText(translations[standard])
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if standard in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Ok):
                button.setObjectName('DialogPrimary')
            elif standard == QMessageBox.StandardButton.NoButton and obj.detailedText():
                import weakref
                box_ref = weakref.ref(obj)
                button.setText('查看详情')
                if not button.property('detailsStyled'):
                    button.setProperty('detailsStyled', True)
                    button.clicked.connect(lambda checked=False, a=box_ref:
                                           QTimer.singleShot(0, lambda: self.detail_label(a)))
        obj.style().unpolish(obj); obj.style().polish(obj)
        layout = obj.layout()
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(18)
        if body:
            body.setWordWrap(True)
            body.setMinimumHeight(body.heightForWidth(body.width()))
        obj.adjustSize()

    @staticmethod
    def detail_label(box_ref):
        from shiboken6 import isValid
        box = box_ref()
        if box is None or not isValid(box):
            return
        details = box.findChild(QTextEdit)
        for button in box.buttons():
            if button.property('detailsStyled'):
                button.setText('收起详情' if details and details.isVisible() else '查看详情')


def install_message_style():
    app = QApplication.instance()
    if app and not hasattr(app, '_resource_dialog_styler'):
        app._resource_dialog_styler = MessageStyler(app)
        app.installEventFilter(app._resource_dialog_styler)
