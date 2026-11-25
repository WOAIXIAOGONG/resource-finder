# -*- coding: utf-8 -*-
"""结果卡片、流式布局、空状态。"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation, QRect,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFontMetrics, QGuiApplication, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QLayout, QMenu, QSizePolicy, QPlainTextEdit,
                               QVBoxLayout, QWidget, QWidgetAction, QPushButton)

from ..models import SearchResult
from . import theme
from .icons import icon

CARD_WIDTH = 224
COVER_W = 208
COVER_H = 117


class SearchComposer(QPlainTextEdit):
    """Multiline input: Enter submits, Shift+Enter inserts a line."""
    returnPressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preediting = False
        self.setTabChangesFocus(True)
        self.setAcceptDrops(False)
        self.setAccessibleName("搜索内容")

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)

    def inputMethodEvent(self, event):
        self._preediting = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._preediting:
                event.accept()
                return
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.returnPressed.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        self._focus_panel(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._preediting = False
        self._focus_panel(False)
        super().focusOutEvent(event)

    def _focus_panel(self, focused):
        panel = self.parentWidget()
        if panel:
            panel.setProperty("focused", focused)
            panel.style().unpolish(panel)
            panel.style().polish(panel)


class FlowLayout(QLayout):
    """自动换行的流式布局，窗口变宽时卡片会自动多排一列。"""

    def __init__(self, parent: Optional[QWidget] = None, margin: int = 0, spacing: int = 12):
        super().__init__(parent)
        self._items: List = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):                      # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):                 # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):                 # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):                # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:          # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):                  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:                  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:               # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        # Center the grid as a block; incomplete rows keep the same column positions.
        widths = {item.sizeHint().width() for item in self._items
                  if item.widget() is None or not item.widget().isHidden()}
        if len(widths) == 1:
            item_width = next(iter(widths))
            columns = max(1, (effective.width() + self._spacing) // (item_width + self._spacing))
            used = columns * item_width + (columns - 1) * self._spacing
            offset = max(0, (effective.width() - used) // 2)
            effective.adjust(offset, 0, -offset, 0)
        x, y = effective.x(), effective.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            # 用 isHidden() 而不是 isVisible()：新加的卡片还没走完 show 流程时
            # isVisible() 也是 False，会被当成「隐藏项」跳过，结果是第二张卡
            # 停在 (0,0) 和第一张叠在一起。
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class ResultCard(QFrame):
    """封面 + 标题 + 来源/时长；左键打开，右键复制。"""

    copy_requested = Signal(str)
    click_requested = Signal(str)
    open_requested = Signal(str)
    copied = Signal(str)
    download_requested = Signal(object)

    def __init__(self, result: SearchResult, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.result = result
        self._cover_url = ""
        self._pix: Optional[QPixmap] = None
        self._has_cover = False
        self._hover = False

        self.setObjectName("Card")
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("%s\n\n单击打开网址；右键可以复制网址 / 标题 / 封面图" % result.title)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.cover = QLabel(self)
        self.cover.setObjectName("CoverLabel")
        self.cover.setFixedSize(COVER_W, COVER_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setText("加载中…")
        root.addWidget(self.cover)

        # 时长角标（videos 网站那种右下角小黑条）
        self.time_badge = QLabel(result.duration or "", self.cover)
        self.time_badge.setObjectName("CoverBadge")
        self.time_badge.setVisible(bool(result.duration))
        self.time_badge.adjustSize()

        # 发布日期角标（左上角小药丸；不知道日期就不显示）
        date_text = result.date_label
        self.date_badge = QLabel(date_text, self.cover)
        self.date_badge.setObjectName("CoverDate")
        self.date_badge.setVisible(bool(date_text))
        self.date_badge.adjustSize()
        self._place_badge()

        self.play_overlay = QLabel(self.cover)
        self.play_overlay.setObjectName("CoverPlay")
        self.play_overlay.setFixedSize(38, 38)
        self.play_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.play_overlay.setPixmap(icon('sound' if result.resource_type=='sound' else "play" if result.resource_type == "video" else "globe", "#ffffff", 22).pixmap(22, 22))
        self.play_overlay.move((COVER_W - 38) // 2, (COVER_H - 38) // 2)
        self.play_overlay.hide()
        from ..downloads import platform
        self.download_hint=QLabel('↓  右键下载',self.cover)
        self.download_hint.setObjectName('CoverDownloadHint');self.download_hint.adjustSize()
        self.download_hint.move(8,COVER_H-self.download_hint.height()-8)
        self.download_hint.setVisible(bool(platform(result)))

        self.title = QLabel(self)
        self.title.setObjectName("CardTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setFixedHeight(36)
        self.title.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.title.ensurePolished()
        self.title.setText(self._two_lines(result.title, COVER_W, self.title.font()))
        root.addWidget(self.title)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(6)
        source = result.source or "网页"
        badge = QLabel(source, self)
        badge.setObjectName("CardBadge")
        badge.ensurePolished()
        badge.setText(QFontMetrics(badge.font()).elidedText(source, Qt.TextElideMode.ElideRight, 70))
        badge.setToolTip(source)
        foot.addWidget(badge)

        meta_bits = [] if result.resource_type == 'video' else [{'image': '图片', 'article': '文章', 'sound':'音效'}.get(result.resource_type, '资源')]
        if result.author:
            meta_bits.append(result.author)
        if result.play and result.resource_type=='video':
            meta_bits.append("▶ " + result.play)
        self.meta = QLabel(" · ".join(meta_bits[:2]), self)
        self.meta.setObjectName("CardMeta")
        self.meta.ensurePolished()
        fm = QFontMetrics(self.meta.font())
        self.meta.setText(fm.elidedText(self.meta.text(), Qt.TextElideMode.ElideRight,
                                     COVER_W - badge.sizeHint().width() - 6))
        foot.addWidget(self.meta, 1, Qt.AlignmentFlag.AlignRight)
        root.addLayout(foot)

        self.setFixedHeight(COVER_H + 36 + 20 + 16 + 12)

        if result.cover:
            self._cover_url = result.cover
        else:
            self.set_cover(None)

        # 卡片里的文字/角标都别吃鼠标事件，保证点封面也能触发卡片的单击
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------------ #
    @property
    def cover_url(self) -> str:
        return self._cover_url

    @property
    def has_cover(self) -> bool:
        """是否真的加载到了封面（不是占位图）。"""
        return self._has_cover

    def _place_badge(self) -> None:
        badge = getattr(self, "time_badge", None)
        if badge is not None and badge.text():
            badge.adjustSize()
            badge.move(COVER_W - badge.width() - 6, COVER_H - badge.height() - 6)
            badge.raise_()
        date = getattr(self, "date_badge", None)
        if date is not None and date.text():
            date.adjustSize()
            date.move(6, 6)
            date.raise_()

    def set_duration(self, text: str) -> None:
        self.result.duration = text
        self.time_badge.setText(text)
        self.time_badge.setVisible(bool(text))
        self._place_badge()

    def set_cover(self, pixmap: Optional[QPixmap]) -> None:
        if pixmap is None or pixmap.isNull():
            settings = getattr(self.window(), "settings", None)
            label = '暂无预览' if getattr(settings, 'show_thumbnails', True) else '封面已关闭'
            pixmap = theme.placeholder_cover(COVER_W, COVER_H, label,
                                            getattr(settings, "theme", "dark"))
            if self.result.resource_type=='sound' and getattr(settings,'show_thumbnails',True):
                pixmap=theme.sound_cover(COVER_W,COVER_H,getattr(settings,'theme','dark'))
            self._has_cover = False
        rounded = theme.rounded_pixmap(pixmap, 8)
        self.cover.setPixmap(rounded)
        self.cover.setText("")
        self._pix = rounded

    def set_cover_from_file(self, path: str) -> bool:
        pm = theme.thumbnail_pixmap(path, COVER_W, COVER_H)
        if pm.isNull():
            self.set_cover(None)
            return False
        self.set_cover(pm)
        self._has_cover = True
        return True

    # ------------------------------------------------------------------ #
    def _two_lines(self, text: str, width: int, font) -> str:
        """把标题按字符硬折成两行，超出的部分用省略号收掉。"""
        text = (text or "").strip()
        fm = QFontMetrics(font)
        lines: List[str] = []
        buf = ""
        for ch in text:
            if fm.horizontalAdvance(buf + ch) > width:
                lines.append(buf)
                buf = ch
                if len(lines) == 2:
                    break
            else:
                buf += ch
        if len(lines) < 2 and buf:
            lines.append(buf)
        if len(lines) == 2 and (len("".join(lines)) < len(text)):
            second = lines[1]
            while second and fm.horizontalAdvance(second + "…") > width:
                second = second[:-1]
            lines[1] = second + "…"
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def enterEvent(self, event):                  # noqa: N802
        self._hover = True
        self.play_overlay.show()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):                  # noqa: N802
        self._hover = False
        self.play_overlay.hide()
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):           # noqa: N802
        # 单击卡片 = 直接用浏览器打开这个资源（复制链接在右键菜单里）
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            if not getattr(self, "_double_release", False):
                self.click_requested.emit(self.result.url)
            self._double_release = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):       # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._double_release = True
        super().mouseDoubleClickEvent(event)

    def _menu(self) -> QMenu:
        """右键菜单（单独抽出来，测试里可以直接检查菜单项）。"""
        menu = QMenu(self)
        from ..downloads import platform
        if platform(self.result):
            action=QWidgetAction(menu);action.setData('download');action.setText('下载资源…')
            button=QPushButton('下载资源…\n选择格式与保存位置',menu);button.setObjectName('MenuDownload')
            mode=getattr(getattr(self.window(),'settings',None),'theme','dark')
            button.setIcon(icon('download',theme.palette(mode)['accent'],24));button.setIconSize(QSize(24,24))
            button.setAccessibleName('下载资源，选择格式与保存位置')
            action.setDefaultWidget(button);menu.addAction(action)
            action.triggered.connect(lambda:self.download_requested.emit(self.result))
            button.clicked.connect(lambda:(action.trigger(),menu.close()))
            menu.addSeparator()
        menu.addAction("在浏览器中打开").setData("open")
        menu.addSeparator()
        menu.addAction("复制网址").setData("link")
        menu.addAction("复制标题").setData("title")
        act_cover = menu.addAction("复制封面图")
        act_cover.setData("cover")
        act_cover.setEnabled(bool(self._has_cover and self._pix))
        return menu

    def contextMenuEvent(self, event):            # noqa: N802
        menu = self._menu()
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        action = str(chosen.data() or "")
        if action == "open":
            self.open_requested.emit(self.result.url)
        elif action == "link":
            self.copy_requested.emit(self.result.url)
        elif action == "title":
            QGuiApplication.clipboard().setText(self.result.title)
            self.copied.emit("已复制标题")
        elif action == "cover" and self._pix:
            QGuiApplication.clipboard().setPixmap(self._pix)
            self.copied.emit("已复制封面图")


class EmptyState(QWidget):
    """结果区为空时的提示。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 40, 40, 40)
        lay.setSpacing(14)
        lay.addStretch(1)

        eyebrow = self.eyebrow = QLabel("你的资源工作空间", self)
        eyebrow.setObjectName("EmptyEyebrow")
        eyebrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon = QLabel(self)
        self.icon.setObjectName("EmptyIcon")
        self.icon.setFixedSize(68, 68)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title = QLabel("找到下一份灵感", self)
        self.title.setObjectName("EmptyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title.setWordWrap(True)
        self.hint = QLabel("从一个关键词，到你需要的视频。\n在下方输入需求，开始搜寻。", self)
        self.hint.setObjectName("EmptyHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        self.hint.setMaximumWidth(560)

        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(8)
        lay.addWidget(eyebrow)
        lay.addWidget(self.title)
        lay.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignHCenter)
        footer = self.footer = QLabel("点击封面打开视频   ·   右键复制资源链接", self)
        footer.setObjectName("EmptyFooter")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(footer)
        lay.addStretch(2)

    def set_text(self, title: str, hint: str = "") -> None:
        self.title.setText(title)
        self.hint.setText(hint)
        self._fit_text()

    def _fit_text(self):
        width = max(180, min(560, self.width() - 48))
        self.hint.setFixedWidth(width)
        self.hint.setMinimumHeight(max(36, self.hint.heightForWidth(width)))
        self.title.setMinimumHeight(max(36, self.title.heightForWidth(max(180, self.width() - 48))))

    def resizeEvent(self, event):
        # Decoration yields to the actionable message on short windows.
        compact = self.height() < 360
        self.icon.setVisible(not compact)
        self.eyebrow.setVisible(not compact)
        self.footer.setVisible(not compact)
        self.layout().setContentsMargins(24, 16 if compact else 40, 24, 16 if compact else 40)
        self._fit_text()
        super().resizeEvent(event)


class Toast(QFrame):
    """浮在窗口底部中间的轻提示：复制成功之类，2 秒后淡出。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 9, 18, 9)
        lay.setSpacing(8)
        self.icon = QLabel("✓", self)
        self.icon.setObjectName("ToastIcon")
        lay.addWidget(self.icon)
        self.label = QLabel("", self)
        self.label.setObjectName("ToastText")
        lay.addWidget(self.label)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_done)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._fading = False
        self.hide()

    def show_toast(self, text: str, ms: int = 2000) -> None:
        self._anim.stop()
        self._fading = False
        self.icon.setText("✓")
        self.label.setText(text)
        self.adjustSize()
        self._reposition()
        self._effect.setOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start(ms)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        x = max(12, (parent.width() - self.width()) // 2)
        y = max(12, parent.height() - self.height() - 46)
        self.move(x, y)

    def _fade_out(self) -> None:
        self._fading = True
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_done(self) -> None:
        if self._fading:
            self.hide()
            self._effect.setOpacity(1.0)

class Spinner(QWidget):
    """转圈的加载指示（纯 QPainter 画弧，不依赖任何图片）。"""

    def __init__(self, parent: Optional[QWidget] = None, size: int = 26,
                 color: str = "") -> None:
        super().__init__(parent)
        self._size = size
        self._angle = 0
        self._color = color or "#5b8cff"
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event) -> None:          # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(self._color), 2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        rect = self.rect().adjusted(3, 3, -3, -3)
        painter.drawArc(rect, int(self._angle * 16), int(270 * 16))
        painter.end()


class BusyOverlay(QFrame):
    """盖在结果区中间的「正在搜索」遮罩，让用户知道 Agent 正在干活。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("BusyOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 22, 14)
        lay.setSpacing(14)
        self.spinner = Spinner(self)
        lay.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.title = QLabel("Agent 正在搜索…", self)
        self.title.setObjectName("BusyTitle")
        self.sub = QLabel("", self)
        self.sub.setObjectName("BusySub")
        self.sub.setWordWrap(True)
        col.addWidget(self.title)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.hide()

    def show_busy(self, title: str, sub: str = "") -> None:
        self.title.setText(title)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.spinner.start()
        self.adjustSize()
        self.show()
        self.raise_()

    def hide_busy(self) -> None:
        self.spinner.stop()
        self.hide()
