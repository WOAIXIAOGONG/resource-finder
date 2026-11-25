# -*- coding: utf-8 -*-
"""对话框：站点编辑、设置。"""
from __future__ import annotations

import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialogButtonBox,
                               QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from .dialog_design import StyledDialog, label, primary, step_card
from .controls import (WheelSafeComboBox as QComboBox, WheelSafeSpinBox as QSpinBox,
                       WheelSafeDoubleSpinBox as QDoubleSpinBox)

from ..adapters import adapter_for
from ..adapters.base import SearchContext
from ..config import SiteConfig, default_search_url
from ..net import NetworkError, describe_network, probe_network
from ..thumbcache import ThumbCache


class PlatformLoginDialog(StyledDialog):
    """Modeless: one platform's login must never block the other platforms."""
    verify_requested = Signal()
    skip_requested = Signal()
    open_requested = Signal()

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.setWindowTitle(entry['name'] + ' · 需要登录')
        self.setModal(False)
        self.completed = False
        layout = self.make_root('登录一次，后台继续', entry['name'] + ' · 登录状态保存在当前本地存档', 'globe', 580)
        self.message = label(entry['reason'], 'DialogNotice', self)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self._sync_message_height)
        layout.addWidget(self.message)
        self.steps = step_card(self, [
            ('在专用窗口中登录', '使用工具打开的 Edge / Chrome 窗口登录 YouTube。首次需在此窗口登录，日常浏览器的账号不会自动复制过来。'),
            ('点击下方“已登录”', '工具自动关闭专用登录窗口、验证后台会话并继续搜索。后续无需保持网页打开，也无需安装扩展。'),
        ])
        layout.addWidget(self.steps)
        self.steps.setVisible(entry.get('supported', False))
        self.open_button = QPushButton('打开专用登录窗口', self)
        self.open_button.setObjectName('Flat')
        self.open_button.setVisible(entry.get('supported', False))
        self.open_button.clicked.connect(self.open_requested.emit)
        layout.addWidget(self.open_button)
        self.note = label(('日常浏览器和正在播放的视频不会被接管。登录失效或网站要求验证时，才需要再次打开专用窗口。\n不代填密码或验证码，不导出 Cookie。其他平台继续搜索。'
                           if entry.get('supported', False) else '当前平台暂不支持浏览器登录续搜，可跳过此平台；其他平台继续搜索。'), parent=self)
        layout.addWidget(self.note)
        from .widgets import Spinner
        self.busy_row = QWidget(self)
        busy = QHBoxLayout(self.busy_row)
        busy.setContentsMargins(0, 2, 0, 2)
        self.spinner = Spinner(self.busy_row)
        busy.addWidget(self.spinner)
        busy.addWidget(label('正在切换后台会话并验证搜索，请稍候', parent=self.busy_row), 1)
        layout.addWidget(self.busy_row)
        self.busy_row.hide()
        buttons = QHBoxLayout()
        self.verify_button = QPushButton('已登录，继续搜索', self)
        self.skip_button = QPushButton('本轮跳过该平台', self)
        primary(self.verify_button)
        self.verify_button.clicked.connect(self.verify_requested.emit)
        self.skip_button.clicked.connect(self.reject)
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        buttons.addWidget(self.verify_button)
        self.finish_layout(layout, buttons)
        self.resize(580, 530)

    def set_message(self, text):
        self.message.setText(text)
        self.message.setToolTip(text)
        self._message_timer.start(0)

    def _sync_message_height(self):
        self.message.setMinimumHeight(max(0, self.message.heightForWidth(self.message.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_message_timer'):
            self._message_timer.start(0)

    def set_verifying(self, active, phase=''):
        self.open_button.setEnabled(not active)
        self.busy_row.setVisible(active)
        if active:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.verify_button.setText(('正在连接浏览器…' if phase == 'connecting' else '正在验证登录…')
                                   if active else '已登录，继续搜索')

    def reject(self):
        self._message_timer.stop()
        self.spinner.stop()
        if not self.completed:
            self.completed = True
            self.skip_requested.emit()
        super().reject()

    def finish(self):
        self._message_timer.stop()
        self.spinner.stop()
        self.completed = True
        self.accept()


class AgentConnectDialog(StyledDialog):
    """Non-blocking window-modal handshake: other apps remain available for pasting."""
    def __init__(self, server, parent=None):
        super().__init__(parent)
        from .widgets import Spinner
        self.server = server
        self.setWindowTitle('正在接管资源搜寻器')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        root = self.make_root('等待 Agent 接管', '连接完成后将自动返回，你就可以开始搜索。', 'agent', 560)
        self.title = self.heading
        root.addWidget(step_card(self, [
            ('指令已经复制', '切换到 Agent 对话，粘贴接入指令并发送。'),
            ('等待连接确认', 'Agent 完成握手并持续接收任务后，此窗口自动关闭。'),
        ]))
        row = QHBoxLayout()
        self.spinner = Spinner(self)
        self.spinner.start()
        row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignTop)
        self.state = QPlainTextEdit(self)
        self.state.setReadOnly(True)
        self.state.setObjectName('DialogStatus')
        self.state.setMinimumHeight(88)
        self.state.setMaximumHeight(112)
        row.addWidget(self.state, 1)
        root.addLayout(row)
        actions = QHBoxLayout()
        cancel = QPushButton('返回直接搜索', self)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        actions.addStretch()
        self.retry_btn = QPushButton('重新复制指令', self)
        primary(self.retry_btn)
        self.retry_btn.clicked.connect(self.retry)
        actions.addWidget(self.retry_btn)
        self.finish_layout(root, actions)
        self.started = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def retry(self):
        from PySide6.QtGui import QGuiApplication
        with self.server._task_lock:
            self.server.access.reset()
        QGuiApplication.clipboard().setText(self.server.instructions())
        self.started = time.monotonic()
        self.spinner.start()
        self.title.setText('等待 Agent 接管…')
        self.refresh()

    def refresh(self):
        access = self.server.access
        if not self.server.running:
            self.state.setPlainText('收件口已关闭。请返回后重新开启。')
            self.spinner.stop()
            return
        if access.ready():
            self.accept()
            return
        elapsed = int(time.monotonic() - self.started)
        messages = {'waiting': '① 等待 Agent 发起请求',
                    'confirming': '② 已收到连接，等待 Agent 回传握手确认',
                    'confirmed': '③ 已确认，等待 Agent 再次轮询以证明能够接收任务',
                    'ready': '连接活动已中断，等待 Agent 恢复轮询',
                    'failed': 'Agent 接管失败：' + access.last_report}
        self.state.setPlainText(messages.get(access.connection, '等待接管') + '\n已等待 %d 秒' % elapsed)
        if elapsed >= 120 or access.connection == 'failed':
            self.spinner.stop()
            self.title.setText('尚未完成接管')
            self.state.setPlainText(self.state.toPlainText() + '\n请确认 Agent 能访问本机接口并持续轮询；可重新复制指令或返回直接搜索。')

    def done(self, result):
        self.timer.stop()
        self.spinner.stop()
        super().done(result)


class _TestSignals(QObject):
    done = Signal(bool, str)


class _TestThread(threading.Thread):
    def __init__(self, site: SiteConfig, http, query: str, signals: _TestSignals):
        super().__init__(daemon=True)
        self.site = site
        self.http = http
        self.query = query
        self.signals = signals

    def run(self) -> None:  # noqa: D401
        import time

        started = time.time()
        try:
            ctx = SearchContext(query=self.query, http=self.http, max_results=5,
                                log=lambda m: None)
            items = adapter_for(self.site).search(ctx, self.site) or []
            cost = time.time() - started
            if items:
                sample = items[0].title[:40]
                self.signals.done.emit(True, "抓取成功：%d 条，用时 %.1fs\n首条：%s"
                                       % (len(items), cost, sample))
            else:
                self.signals.done.emit(False, "页面打开了，但没解析出结果（%.1fs）。\n"
                                              "可以试试在「高级」里填 CSS 选择器，或换一个搜索地址。"
                                       % cost)
        except NetworkError as exc:
            self.signals.done.emit(False, "网络失败：%s" % exc)
        except Exception as exc:                      # noqa: BLE001
            self.signals.done.emit(False, "失败：%s" % exc)


class SiteDialog(StyledDialog):
    """添加 / 编辑一个搜索来源。"""
    site_resolved = Signal(int, str, object, str)

    def __init__(self, site: Optional[SiteConfig] = None, http=None,
                 default_query: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑站点" if site else "添加站点")
        self.setMinimumWidth(520)
        self.http = http
        self.default_query = default_query or "风景"
        self._site = site
        self.result_site: Optional[SiteConfig] = None

        root = self.make_root("编辑搜索来源" if site else "添加搜索来源",
                              "粘贴首页或任意页面链接，自动识别网站与搜索范围。", 'globe', 640)
        form = QFormLayout()
        form.setSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("例如：哔哩哔哩 / 某个资源站")
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("粘贴网站首页、视频或文章链接…")
        form.addRow("网站链接", self.url_edit)
        form.addRow("站点名称", self.name_edit)
        self.name_edit.setPlaceholderText("自动识别，也可以自己命名")
        url_hint = self.intake_hint = QLabel("无需手动填写 {q}。也支持已有的自定义搜索地址模板。", self)
        url_hint.setObjectName("Hint")
        url_hint.setWordWrap(True)
        form.addRow("", url_hint)

        self._builtin_hint = QLabel(
            "已识别平台会使用专用搜索方式；高级 CSS 可用于手动配置的站点。", self)
        self._builtin_hint.setObjectName("Hint")
        self._builtin_hint.setWordWrap(True)
        self._builtin_hint.hide()
        form.addRow("", self._builtin_hint)

        self.route_combo = QComboBox(self)
        self.route_combo.addItem("自动（按站点选择通道）", "auto")
        self.route_combo.addItem("代理优先（失败再直连）", "proxy")
        self.route_combo.addItem("只走直连（国内站点）", "direct")
        form.addRow("访问通道", self.route_combo)

        self.limit_spin = QSpinBox(self)
        self.limit_spin.setRange(1, 60)
        self.limit_spin.setValue(24)
        self.limit_spin.setSuffix(" 条")
        form.addRow("最多显示", self.limit_spin)

        root.addLayout(form)

        # -- 高级 -------------------------------------------------------- #
        self.advanced_toggle = QPushButton("▸  高级（可选）", self)
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_toggle.setAccessibleName("展开高级选项")
        self.advanced_toggle.setToolTip("展开或收起 CSS 选择器设置")
        root.addWidget(self.advanced_toggle)
        self.advanced = QGroupBox(self)
        adv = QFormLayout(self.advanced)
        adv.setSpacing(8)
        self.css_item = QLineEdit(self)
        self.css_item.setPlaceholderText("li.result, div.card, article")
        self.css_link = QLineEdit(self)
        self.css_link.setPlaceholderText("a")
        self.css_title = QLineEdit(self)
        self.css_title.setPlaceholderText("h3, .title")
        self.css_image = QLineEdit(self)
        self.css_image.setPlaceholderText("img")
        adv.addRow("结果条目选择器", self.css_item)
        adv.addRow("链接选择器", self.css_link)
        adv.addRow("标题选择器", self.css_title)
        adv.addRow("封面选择器", self.css_image)
        css_hint = QLabel("留空则自动识别。只有自动识别不准的站点才需要填。", self)
        css_hint.setObjectName("Hint")
        css_hint.setWordWrap(True)
        adv.addRow("", css_hint)
        root.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self._toggle_advanced)

        # -- 测试 -------------------------------------------------------- #
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试抓取", self)
        self.test_btn.setToolTip("用上面填的地址试抓一次，看看能拿到多少条结果")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.clicked.connect(self._on_test)
        self.test_label = QLabel("", self)
        self.test_label.setObjectName("Hint")
        self.test_label.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_label, 1)
        root.addLayout(test_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        primary(buttons.button(QDialogButtonBox.StandardButton.Ok))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        self.finish_layout(root, buttons)

        self._signals = _TestSignals(self)
        self._signals.done.connect(self._on_test_done)
        self._thread: Optional[_TestThread] = None

        self._intake_revision = 0
        self._intake_cancel = threading.Event()
        self._resolution = None
        self._pending_action = ''
        self._automatic_name = ''
        self._intake_timer = QTimer(self)
        self._intake_timer.setSingleShot(True)
        self._intake_timer.timeout.connect(self._begin_resolve)
        self.site_resolved.connect(self._apply_resolution)
        if site:
            self._load(site)
            self.intake_hint.setText(site.note or '已保存的搜索入口，可粘贴新网址重新识别。')
        self.url_edit.textChanged.connect(self._input_changed)

    # ------------------------------------------------------------------ #
    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced.setVisible(expanded)
        self.advanced_toggle.setText(("▾" if expanded else "▸") + "  高级（可选）")
        self.advanced_toggle.setAccessibleName("收起高级选项" if expanded else "展开高级选项")
        if expanded:
            QTimer.singleShot(0, self._reveal_advanced)

    def _reveal_advanced(self) -> None:
        if self.advanced_toggle.isChecked() and self.isVisible():
            self.body_scroll.ensureWidgetVisible(self.css_item, 0, 24)

    def _load(self, site: SiteConfig) -> None:
        self.name_edit.setText(site.name)
        url = site.search_url or default_search_url(site.builtin)
        self.url_edit.setText(url)
        idx = self.route_combo.findData(site.route)
        self.route_combo.setCurrentIndex(max(0, idx))
        self.limit_spin.setValue(site.max_results)
        self.css_item.setText(site.css.get("item", ""))
        self.css_link.setText(site.css.get("link", ""))
        self.css_title.setText(site.css.get("title", ""))
        self.css_image.setText(site.css.get("image", ""))
        if site.kind == "builtin":
            # 内置站点也允许改：改地址/填 CSS 后自动按普通网页抓取
            self.test_label.setText(
                "内置站点：只改名字不影响搜索方式；改地址或填了高级 CSS，就按普通网页抓取。")
            self._builtin_hint.show()

    # ------------------------------------------------------------------ #
    def _input_changed(self):
        self._intake_revision += 1
        self._intake_cancel.set()
        self._intake_cancel = threading.Event()
        self._resolution = None
        self._pending_action = ''
        self.save_button.setEnabled(True)
        self.test_btn.setEnabled(True)
        self.intake_hint.setText('正在识别网站…' if self.url_edit.text().strip() else '粘贴一个网站链接即可。')
        self._intake_timer.start(500)

    def _begin_resolve(self, action=''):
        from ..site_intake import infer_site, discover_site
        raw = self.url_edit.text().strip()
        self._intake_timer.stop()
        self._pending_action = action
        try:
            site, message = infer_site(raw)
        except ValueError as exc:
            self.intake_hint.setText(str(exc))
            return
        revision, cancel = self._intake_revision, self._intake_cancel
        if site.kind != 'domain' or self.http is None:
            self._apply_resolution(revision, raw, site, message)
            return
        self.save_button.setEnabled(False)
        self.test_btn.setEnabled(False)
        self.intake_hint.setText('已识别 %s，正在检查公开搜索入口…' % site.scope_domain)
        settings = self.http.settings
        def work():
            try:
                resolved, note = discover_site(raw, settings, cancel)
            except Exception:
                resolved, note = site, message + '（暂时无法检查公开搜索入口，已使用限定域名检索。）'
            try:
                self.site_resolved.emit(revision, raw, resolved, note)
            except RuntimeError:
                pass
        threading.Thread(target=work, name='site-intake', daemon=True).start()

    def _apply_resolution(self, revision, raw, site, message):
        if revision != self._intake_revision or raw != self.url_edit.text().strip() or self._intake_cancel.is_set():
            return
        self._resolution = (raw, site)
        if not self.name_edit.text().strip() or self.name_edit.text() == self._automatic_name:
            self.name_edit.setText(site.display)
            self._automatic_name = site.display
        self.intake_hint.setText(message + '\n搜索地址：' + site.search_url)
        self.save_button.setEnabled(True)
        self.test_btn.setEnabled(True)
        action, self._pending_action = self._pending_action, ''
        if action == 'save':
            self._on_accept()
        elif action == 'test':
            self._on_test()

    def _ensure_resolved(self, action):
        raw = self.url_edit.text().strip()
        if '{q}' in raw or (self._resolution and self._resolution[0] == raw):
            return True
        self._begin_resolve(action)
        return False

    def done(self, result):
        self._intake_revision += 1
        self._intake_timer.stop()
        self._intake_cancel.set()
        super().done(result)

    def _collect(self) -> SiteConfig:
        from ..site_intake import infer_site
        raw = self.url_edit.text().strip()
        css = {k: w.text().strip() for k, w in [('item', self.css_item), ('link', self.css_link),
                                               ('title', self.css_title), ('image', self.css_image)] if w.text().strip()}
        base = self._site
        if base and raw == base.search_url:
            site = SiteConfig.from_dict(base.to_dict())
        elif self._resolution and self._resolution[0] == raw:
            site = SiteConfig.from_dict(self._resolution[1].to_dict())
        else:
            site, _ = infer_site(raw)
        if base:
            site.id, site.enabled = base.id, base.enabled
            if raw == base.search_url:
                site.headers, site.encoding = dict(base.headers), base.encoding
        site.name = self.name_edit.text().strip() or site.display
        site.route = self.route_combo.currentData() or 'auto'
        site.max_results = self.limit_spin.value()
        if css and site.kind == 'builtin':
            site.kind, site.builtin = 'generic', ''
        site.css = css
        return site

    def _on_accept(self) -> None:
        if not self._ensure_resolved('save'):
            return
        try:
            site = self._collect()
        except ValueError as exc:
            self.intake_hint.setText(str(exc))
            return
        err = site.validate()
        if err:
            QMessageBox.warning(self, "还差一点", err)
            return
        self.result_site = site
        self.accept()

    # ------------------------------------------------------------------ #
    def _on_test(self) -> None:
        if self.http is None:
            self.test_label.setText("测试不可用（没有初始化网络）")
            return
        if not self._ensure_resolved('test'):
            return
        try:
            site = self._collect()
        except ValueError as exc:
            self.test_label.setText(str(exc))
            return
        err = site.validate()
        if err:
            self.test_label.setText("先补全：" + err)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self.test_btn.setEnabled(False)
        self.test_label.setText("正在测试抓取，请稍候…")
        self._thread = _TestThread(site, self.http, self.default_query, self._signals)
        self._thread.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_label.setText(message.replace("\n", "  "))
        from . import theme
        mode = getattr(getattr(self.parent(), 'settings', None), 'theme', 'dark')
        color = theme.palette(mode)['ok' if ok else 'danger']
        self.test_label.setStyleSheet("color: %s;" % color)


class SettingsDialog(StyledDialog):
    """设置：网络 / 抓取 / 显示 / 缓存。"""
    network_done = Signal(int, object)

    def __init__(self, settings, http, thumbs: Optional[ThumbCache] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self.settings = settings
        self.http = http
        self.thumbs = thumbs
        self._network_revision = 0
        self._network_running = False
        self.network_done.connect(self._on_network_done)

        root = self.make_root("偏好设置", "调整连接、搜索与外观，保存后应用更改。", 'settings', 660)
        self.pages = QTabWidget(self)
        root.addWidget(self.pages, 1)
        network_page = QWidget(self.pages)
        network_layout = QVBoxLayout(network_page)
        network_layout.setContentsMargins(0, 8, 0, 0)
        display_page = QWidget(self.pages)
        display_layout = QVBoxLayout(display_page)
        display_layout.setContentsMargins(0, 8, 0, 0)
        self.pages.addTab(network_page, "网络与抓取")
        self.pages.addTab(display_page, "外观与缓存")
        service_page = QWidget(self.pages)
        service_layout = QVBoxLayout(service_page)
        service_note = QLabel('连接官方素材服务。爱给账号授权仅在当前本地存档使用，无需安装额外环境。', service_page)
        service_note.setWordWrap(True)
        service_layout.addWidget(service_note)
        aigei_button = QPushButton('爱给官方 MCP · 账号连接', service_page)
        def connect_aigei():
            from .aigei import AigeiDialog
            AigeiDialog(getattr(settings, '_aigei_mcp', None), self).exec()
        aigei_button.clicked.connect(connect_aigei)
        service_layout.addWidget(aigei_button)
        service_layout.addStretch()
        self.pages.addTab(service_page, '素材连接')

        # -- 网络 -------------------------------------------------------- #
        net = QGroupBox("网络", self)
        nf = QFormLayout(net)
        nf.setSpacing(9)
        self.proxy_combo = QComboBox(net)
        self.proxy_combo.addItem("自动（跟随系统代理 / 环境变量）", "auto")
        self.proxy_combo.addItem("直连（不走代理）", "direct")
        self.proxy_combo.addItem("手动指定代理", "manual")
        idx = self.proxy_combo.findData(settings.proxy_mode)
        self.proxy_combo.setCurrentIndex(max(0, idx))
        nf.addRow("代理模式", self.proxy_combo)

        self.proxy_edit = QLineEdit(net)
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7897")
        self.proxy_edit.setText(settings.proxy_url or "")
        nf.addRow("代理地址", self.proxy_edit)

        diag_row = QHBoxLayout()
        self.diag_label = QLabel(describe_network(settings), net)
        self.diag_label.setObjectName("Hint")
        self.diag_label.setWordWrap(True)
        diag_btn = self.diag_btn = QPushButton("重新检测", net)
        diag_btn.setObjectName("Flat")
        diag_btn.clicked.connect(self._refresh_net)
        diag_row.addWidget(self.diag_label, 1)
        diag_row.addWidget(diag_btn)
        holder = QWidget(net)
        holder.setLayout(diag_row)
        nf.addRow("当前状态", holder)
        network_layout.addWidget(net)

        # -- 抓取 -------------------------------------------------------- #
        crawl = QGroupBox("抓取", self)
        cf = QFormLayout(crawl)
        cf.setSpacing(9)
        self.workers_spin = QSpinBox(crawl)
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(int(settings.workers))
        self.workers_spin.setSuffix(" 个站点同时搜")
        cf.addRow("并发", self.workers_spin)

        self.limit_spin = QSpinBox(crawl)
        self.limit_spin.setRange(1, 60)
        self.limit_spin.setValue(int(settings.max_results_per_site))
        self.limit_spin.setSuffix(" 条 / 站点")
        cf.addRow("每个站点最多", self.limit_spin)

        self.timeout_spin = QDoubleSpinBox(crawl)
        self.timeout_spin.setRange(3, 600)
        self.timeout_spin.setSingleStep(1)
        self.timeout_spin.setValue(float(settings.timeout))
        self.timeout_spin.setSuffix(" 秒")
        cf.addRow("请求超时", self.timeout_spin)

        self.delay_spin = QDoubleSpinBox(crawl)
        self.delay_spin.setRange(0, 3)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setValue(float(settings.polite_delay))
        self.delay_spin.setSuffix(" 秒（随机延迟，避免被封）")
        cf.addRow("礼貌间隔", self.delay_spin)
        network_layout.addWidget(crawl)
        network_layout.addStretch(1)

        # -- 显示 -------------------------------------------------------- #
        disp = QGroupBox("显示", self)
        df = QFormLayout(disp)
        df.setSpacing(9)
        self.theme_combo = QComboBox(disp)
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        idx = self.theme_combo.findData(settings.theme)
        self.theme_combo.setCurrentIndex(max(0, idx))
        df.addRow("主题", self.theme_combo)

        self.thumbs_check = QCheckBox("显示封面图（关掉可以省流量）", disp)
        self.thumbs_check.setChecked(bool(settings.show_thumbnails))
        df.addRow("", self.thumbs_check)

        click_hint = QLabel("左键打开视频网址；右键复制链接、标题或封面。", disp)
        click_hint.setObjectName("Hint")
        click_hint.setWordWrap(True)
        df.addRow("卡片操作", click_hint)
        display_layout.addWidget(disp)

        # -- 缓存 -------------------------------------------------------- #
        cache = QGroupBox("封面缓存", self)
        cl = QHBoxLayout(cache)
        self.cache_spin = QSpinBox(cache)
        self.cache_spin.setRange(50, 5000)
        self.cache_spin.setSingleStep(50)
        self.cache_spin.setValue(int(settings.cache_mb))
        self.cache_spin.setSuffix(" MB")
        self.size_label = QLabel(self._cache_text(), cache)
        self.size_label.setObjectName("Hint")
        clear_btn = QPushButton("清空缓存", cache)
        clear_btn.setObjectName("Flat")
        clear_btn.clicked.connect(self._clear_cache)
        cl.addWidget(QLabel("上限", cache))
        cl.addWidget(self.cache_spin)
        cl.addWidget(self.size_label, 1)
        cl.addWidget(clear_btn)
        display_layout.addWidget(cache)
        display_layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        primary(buttons.button(QDialogButtonBox.StandardButton.Ok))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.finish_layout(root, buttons)

        self.proxy_combo.currentIndexChanged.connect(self._sync_proxy_enabled)
        self.proxy_combo.currentIndexChanged.connect(self._network_changed)
        self.proxy_edit.textChanged.connect(self._network_changed)
        self._sync_proxy_enabled()

    # ------------------------------------------------------------------ #
    def _sync_proxy_enabled(self) -> None:
        self.proxy_edit.setEnabled(self.proxy_combo.currentData() == "manual")

    def _cache_text(self) -> str:
        if not self.thumbs:
            return ""
        mb = self.thumbs.size_bytes() / (1024 * 1024)
        return "已用 %.1f MB" % mb

    def _clear_cache(self) -> None:
        if not self.thumbs:
            return
        n = self.thumbs.clear()
        self.size_label.setText("已清空 %d 个文件" % n)

    def _refresh_net(self) -> None:
        from dataclasses import replace
        if self._network_running:
            return
        probe = replace(self.settings, proxy_mode=self.proxy_combo.currentData(),
                        proxy_url=self.proxy_edit.text().strip())
        revision = self._network_revision
        self._network_running = True
        self.diag_btn.setEnabled(False)
        self.diag_label.setText('正在实测国内与境外连接…（使用当前填写的配置）')

        def work():
            try:
                info = probe_network(probe)
            except Exception as exc:
                info = {'error': str(exc)}
            try:
                self.network_done.emit(revision, info)
            except RuntimeError:
                pass  # Dialog may have been closed and destroyed during the probe.
        threading.Thread(target=work, name='settings-netprobe', daemon=True).start()

    def _network_changed(self, *_):
        self._network_revision += 1
        self.diag_label.setText('配置已更改，请重新检测；保存后才应用到搜索')

    def _on_network_done(self, revision, info):
        self._network_running = False
        self.diag_btn.setEnabled(True)
        if revision != self._network_revision:
            return
        if info.get('error'):
            self.diag_label.setText('网络检测失败：' + info['error'])
            return
        text = '国内 %s · 境外 %s' % (
            '可用' if info.get('domestic') else '不可用',
            '可用' if info.get('abroad') else '不可用')
        if info.get('note'):
            text += '\n' + str(info['note'])
        self.diag_label.setText(text)

    # ------------------------------------------------------------------ #
    def apply_to(self, settings):
        """把界面上的值写回 settings（原地修改）。"""
        settings.proxy_mode = self.proxy_combo.currentData()
        settings.proxy_url = self.proxy_edit.text().strip()
        settings.workers = self.workers_spin.value()
        settings.max_results_per_site = self.limit_spin.value()
        settings.timeout = float(self.timeout_spin.value())
        settings.polite_delay = float(self.delay_spin.value())
        settings.theme = self.theme_combo.currentData()
        settings.show_thumbnails = self.thumbs_check.isChecked()
        settings.open_in_browser = True  # Retained for reading older config files.
        settings.cache_mb = self.cache_spin.value()
        return settings


class AgentWaitDialog(StyledDialog):
    """Agent 模式下点了搜索、但本机 Agent 还没接管时的提示。

    设计要点：不做「默默用关键词搜一份」的兜底——那样用户会以为这是 AI 的
    理解结果。这里明确弹窗，并把「接入指令 + 这条任务」放进剪贴板。
    """

    PASTE = 1       # 保留任务，用户去粘贴
    HANDOFF = 2     # 复查时发现已接入，直接派
    LOCAL = 3       # 用户坚持用本地规则搜
    CANCEL = 0      # 取消，撤掉任务

    def __init__(self, task_text: str, check_connected=None,
                 task_id: str = "", parent=None):
        super().__init__(parent)
        self.choice = AgentWaitDialog.CANCEL
        self._check = check_connected
        self._task_text = task_text or ""

        self.setWindowTitle("还没有 Agent 接管")
        self.setModal(True)
        root = self.make_root('准备交给 Agent', '当前还没有 Agent 接收这条搜索，请先完成连接。', 'agent', 640)
        root.addWidget(step_card(self, [
            ('打开 Agent 对话', '接入指令与本次任务已经复制，可粘贴到 Codex、Claude Code 等 Agent 中。'),
            ('发送指令，再回到这里', 'Agent 开始接收任务后，点击下方主按钮继续；也可以使用普通搜索。'),
        ]))
        self.preview = QPlainTextEdit(self._task_text)
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(105)
        self.preview.setMaximumHeight(150)
        self.preview.setObjectName("DlgPreview")
        root.addWidget(self.preview)

        self.state = QLabel("当前状态：等待 Agent 接入（最近 90 秒没有取过任务）")
        self.state.setObjectName("DlgState")
        root.addWidget(self.state)

        row = QHBoxLayout()
        self.recheck = QPushButton("⟳  重新检测")
        self.recheck.setObjectName("Flat")
        self.recheck.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recheck.clicked.connect(self._recheck)
        row.addWidget(self.recheck)
        row.addStretch(1)

        self.local_btn = QPushButton("仍用本地搜索")
        self.local_btn.setObjectName("Flat")
        self.local_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.local_btn.setToolTip("不依赖 Agent，用关键词直接搜一轮（结果会标注为普通搜索）")
        self.local_btn.clicked.connect(self._use_local)
        row.addWidget(self.local_btn)

        self.main_btn = QPushButton("我已粘贴，交给 Agent")
        primary(self.main_btn)
        self.main_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.main_btn.setDefault(True)
        self.main_btn.clicked.connect(self._use_agent)
        row.addWidget(self.main_btn)
        self.finish_layout(root, row)

    # ------------------------------------------------------------------ #
    def _recheck(self) -> None:
        ok = False
        try:
            ok = bool(self._check()) if self._check else False
        except Exception:
            ok = False
        if ok:
            self.state.setText("当前状态：Agent 已接入 ✓  可以直接把任务派给它")
            self.state.setProperty("ok", True)
            self.main_btn.setText("直接派给 Agent")
        else:
            self.state.setText("当前状态：仍然没有 Agent 取任务，先把剪贴板内容粘给它")
            self.state.setProperty("ok", False)
            self.main_btn.setText("我已粘贴，交给 Agent")
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def _use_agent(self) -> None:
        ok = False
        try:
            ok = bool(self._check()) if self._check else False
        except Exception:
            ok = False
        self.choice = AgentWaitDialog.HANDOFF if ok else AgentWaitDialog.PASTE
        self.accept()

    def _use_local(self) -> None:
        self.choice = AgentWaitDialog.LOCAL
        self.accept()
