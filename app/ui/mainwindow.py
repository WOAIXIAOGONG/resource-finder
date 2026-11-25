# -*- coding: utf-8 -*-
"""主窗口。"""
from __future__ import annotations

import base64
import os
import threading
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, QEvent
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QApplication, QAbstractSlider, QCheckBox, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget, QWidgetAction)

from . import theme
from .controls import WheelSafeComboBox as QComboBox
from .icons import decorate, icon
from .dialogs import AgentWaitDialog, AgentConnectDialog, SettingsDialog, SiteDialog
from .widgets import EmptyState, FlowLayout, ResultCard, SearchComposer, Spinner, Toast
from ..config import (APP_NAME, APP_VERSION, Config, SiteConfig, load_config,
                      save_config, app_dir)
from ..engine import SearchEngine
from ..durations import DurationCache
from ..covers import video_identity
from ..models import SearchResult, SiteEvent, normalize_url
from ..net import HttpClient, describe_network, looks_redirect, probe_network
from ..thumbcache import ThumbCache
from ..agent import AgentServer, parse_intent
from ..sources import site_group, result_group, result_groups

SIDEBAR_MIN = 224
SIDEBAR_MAX = 224

EMPTY_DEFAULT = (
    "找到下一份灵感",
    "从一个关键词，到你需要的资源。\n在下方输入需求，开始搜寻。",
)


class Bridge(QObject):
    """把后台线程的事件转成 Qt 信号（自动排队到主线程）。"""

    event = Signal(int, object)
    thumb = Signal(str, str)
    duration = Signal(object, str)
    log = Signal(str)
    resolved = Signal(str, str)
    net = Signal(str, bool)
    intent = Signal(int, object, str)
    agent_results = Signal(object)
    agent_status = Signal(object)
    agent_error = Signal(object)
    login_event = Signal(object)
    login_retry = Signal(object)


class MainWindow(QMainWindow):
    session_closed = Signal(bool)

    def __init__(self, config: Optional[Config] = None) -> None:
        super().__init__()
        self._closing = False
        self.config = config or load_config()
        self._logout_requested = False
        from ..config import config_dir
        self._log_file = os.path.join(self.config._storage_dir or config_dir(), 'app.log')
        self.settings = self.config.settings
        from ..aigei_mcp import AigeiMCP
        self.settings._aigei_mcp = AigeiMCP(self.settings, self.config._storage_dir or config_dir())
        from ..download_manager import DownloadManager
        self.downloads = DownloadManager(self.config, self)
        self._download_center = None
        self._update_dialog = None
        from .dialog_design import install_message_style
        install_message_style()
        self.bridge = Bridge(self)
        self.http = HttpClient(self.settings, log=self._log)
        self.thumbs = ThumbCache(self.settings, self.http, log=self._log)
        self.durations = DurationCache(self.http)
        self.engine = SearchEngine(self.config, self.http, log=self._log)
        self.logins = self.engine.logins
        self.logins.notify = self.bridge.login_event.emit
        self._login_dialogs = {}
        self._login_retry_rows = {}
        self.bridge.login_event.connect(self._on_login_event)
        self.bridge.login_retry.connect(self._on_login_retry)

        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._cards: List[ResultCard] = []
        self._waiting: Dict[str, List[ResultCard]] = {}
        self._results_all: Dict[str, List[SearchResult]] = {}
        self._all_results: List[SearchResult] = []
        self._referers: Dict[str, str] = {}
        self._source_checks = {}
        self._source_rows = {}
        self._source_only_buttons = {}
        self._source_selection = None  # None = all, empty set = none.
        self._source_scroll_hold = False
        self._loading = False
        self._searching = False
        self._gen = 0
        self._done_sites = 0
        self._ok_sites = 0
        self._total_sites = 0
        self._query = ""
        self._restart_after_stop = False
        self._net_silent = False
        # 无限滚动
        self._page = 1
        self._site_pages = {}
        self._site_exhausted = set()
        self._site_unavailable = {}
        self._site_page_notes = {}
        self._newly_exhausted = []
        self._paging_pages = {}
        self._no_more = False
        self._paging = False
        self._paging_new = 0
        self._agent_budget_used = 0
        self._agent_batch_start = 0
        self._agent_fill_pending = False
        self._agent_empty_rounds = 0
        self._agent_grant_end = 1000 if self.settings.agent_search_mode == 'unlimited' else 100
        self._agent_accounted = 0
        self._agent_seen_urls = set()
        self._agent_scroll_requested = False
        self._agent_scroll_pending = False
        self._agent_scroll_task = ''
        self._agent_drag_position = 0
        self._agent_scroll_timer = QTimer(self)
        self._agent_scroll_timer.setSingleShot(True)
        self._agent_scroll_timer.timeout.connect(self._check_agent_scroll)

        self.setWindowTitle("%s %s" % (APP_NAME, APP_VERSION))
        self.setMinimumSize(1000, 660)
        self.resize(1280, 820)

        self._build_ui()
        self._apply_theme()
        self._toast = Toast(self)
        self._agent_busy = False
        self._agent_more_task = ""
        self._agent_task_id = ""
        self._agent_round_added = 0
        self._agent_finished_id = ""
        self._agent_busy_timer = QTimer(self)
        self._agent_busy_timer.setSingleShot(True)
        self._agent_busy_timer.setInterval(30000)
        self._agent_busy_timer.timeout.connect(self._agent_busy_timeout)
        self._auto_more_timer = QTimer(self)
        self._auto_more_timer.setSingleShot(True)
        self._auto_more_timer.setInterval(1500)
        self._auto_more_timer.timeout.connect(self._auto_agent_more)
        self._reload_sites()
        self._reload_blacklist()
        self._update_net_badge()
        self._restore_geometry()

        self.bridge.event.connect(self._on_event)
        self.bridge.thumb.connect(self._on_thumb)
        self.bridge.duration.connect(self._on_duration, Qt.ConnectionType.QueuedConnection)
        self.bridge.log.connect(self._log)
        self.bridge.resolved.connect(self._on_resolved)
        self.bridge.net.connect(self._on_net_probe)
        self.bridge.intent.connect(self._on_intent)
        self.bridge.agent_results.connect(self._on_agent_results)
        self.bridge.agent_status.connect(self._on_agent_status)
        self.bridge.agent_error.connect(self._on_agent_error)

        self.agent_mode = False
        self._ai_pending = False
        self._ai_serial = 0
        self._agent_server: Optional[AgentServer] = None
        self._connect_dialog = None
        self._agent_inbox: List[SearchResult] = []

        # 封面兜底：25 秒还没回来的，先显示占位图，别一直「加载中」
        self._wait_started: Dict[str, float] = {}
        self._pump_timer = QTimer(self)
        self._pump_timer.setSingleShot(True)
        self._pump_timer.setInterval(60)
        self._pump_timer.timeout.connect(self._pump_thumbs)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(5000)
        self._thumb_timer.timeout.connect(self._sweep_thumbs)
        self._thumb_timer.start()

        # Agent 状态：每 2 秒看一下 Agent 有没有在轮询取任务
        self._agent_timer = QTimer(self)
        self._agent_timer.setInterval(2000)
        self._agent_timer.timeout.connect(self._tick_agent_bar)

        QTimer.singleShot(400, self._startup_checks)

    # ================================================================== #
    # 界面搭建
    # ================================================================== #
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        workspace = QVBoxLayout()
        workspace.setContentsMargins(0, 0, 0, 0)
        workspace.setSpacing(0)
        workspace.addWidget(self._build_topbar())
        workspace.addWidget(self._build_results(), 1)
        workspace.addWidget(self._build_composer())
        body.addLayout(workspace, 1)
        outer.addLayout(body, 1)

        # 底部状态栏
        bar = self.statusBar()
        bar.setSizeGripEnabled(True)
        self.status_label = QLabel("就绪", self)
        self.status_label.setObjectName("StatusText")
        bar.addWidget(self.status_label, 1)
        self.progress = QProgressBar(self)
        self.progress.setFixedWidth(150)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        bar.addPermanentWidget(self.progress)

    def _build_topbar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("TopBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(24, 10, 24, 10)
        lay.setSpacing(10)
        self.library_mark = QLabel(bar)
        self.library_mark.setFixedSize(18, 18)
        lay.addWidget(self.library_mark)
        label = QLabel("资源库", bar)
        label.setObjectName("BreadcrumbRoot")
        lay.addWidget(label)
        separator = QLabel("/", bar)
        separator.setObjectName("Hint")
        lay.addWidget(separator)
        self.workspace_mode = QLabel("直接搜索", bar)
        self.workspace_mode.setObjectName("BreadcrumbCurrent")
        lay.addWidget(self.workspace_mode)
        lay.addStretch(1)

        view_hint = QLabel("封面视图", bar)
        view_hint.setObjectName("Hint")
        lay.addWidget(view_hint)
        return bar

    def _build_navigation(self, side, lay) -> None:
        brand = QHBoxLayout()
        brand.setContentsMargins(6, 6, 0, 10)
        brand.setSpacing(10)
        self.brand_mark = QLabel(side)
        self.brand_mark.setObjectName("BrandMark")
        self.brand_mark.setFixedSize(30, 30)
        self.brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(self.brand_mark)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        title = QLabel(APP_NAME, side)
        title.setObjectName("AppTitle")
        sub = QLabel("RESOURCE FINDER", side)
        sub.setObjectName("AppSub")
        labels.addWidget(title)
        labels.addWidget(sub)
        brand.addLayout(labels, 1)
        lay.addLayout(brand)

        # ---- 模式切换：直接搜索 / Agent 模式 ----
        mode_box = QFrame(side)
        mode_box.setObjectName("ModeSwitch")
        ml = QVBoxLayout(mode_box)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(4)
        self.direct_btn = QPushButton("直接搜索", mode_box)
        self.direct_btn.setObjectName("ModeBtn")
        self.direct_btn.setCheckable(True)
        self.direct_btn.setChecked(True)
        self.direct_btn.setFixedHeight(36)
        decorate(self.direct_btn, "grid")
        self.direct_btn.setToolTip("输入关键词，直接去白名单站点抓取")
        self.direct_btn.clicked.connect(lambda: self._set_mode("direct"))
        ml.addWidget(self.direct_btn)
        self.agent_btn = QPushButton("Agent 模式", mode_box)
        self.agent_btn.setObjectName("ModeBtn")
        self.agent_btn.setCheckable(True)
        self.agent_btn.setFixedHeight(36)
        decorate(self.agent_btn, "agent")
        self.agent_btn.setToolTip("用大白话描述，交给本机 Agent / 本地 AI 理解；\n"
                                  "也可以把搜好的结果从 Agent 推进来展示")
        self.agent_btn.clicked.connect(lambda: self._set_mode("agent"))
        ml.addWidget(self.agent_btn)
        lay.addWidget(mode_box)

    def _build_composer(self) -> QWidget:
        dock = QWidget(self)
        dock.setObjectName("ComposerDock")
        dock_layout = QHBoxLayout(dock)
        dock_layout.setContentsMargins(24, 10, 24, 14)
        self.composer = QFrame(dock)
        self.composer.setObjectName("ComposerPanel")
        dock_layout.addWidget(self.composer, 1)
        root = QVBoxLayout(self.composer)
        root.setContentsMargins(14, 8, 10, 8)
        root.setSpacing(2)
        self.search_edit = SearchComposer(self.composer)
        self.search_edit.setObjectName("SearchInput")
        self.search_edit.setFixedHeight(46)
        self.search_edit.setPlaceholderText("描述你想找的资源，或输入关键词…")
        self.search_edit.returnPressed.connect(self._start_search)
        root.addWidget(self.search_edit)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.resource_combo = QComboBox(self.composer)
        for label, value in [('视频', 'video'), ('图片', 'image'), ('文章', 'article'), ('音效', 'sound')]:
            self.resource_combo.addItem(label, value)
        self.resource_combo.setCurrentIndex(max(0, self.resource_combo.findData(self.settings.resource_type)))
        self.resource_combo.setToolTip('选择资源类型，文章包含小说，音效可勾选爱给网。B站、Pixiv、Commons图片与维基文库文章已有站内搜索通道；其他来源可能依赖索引，空结果不代表全网没有资源。')
        search_row.addWidget(self.resource_combo)
        self.resource_combo.currentIndexChanged.connect(self._resource_changed)
        root.addLayout(search_row)
        self.composer_hint = QLabel("Enter 搜索   ·   Shift + Enter 换行", self.composer)
        self.composer_hint.setObjectName("ComposerHint")
        search_row.addWidget(self.composer_hint, 1)
        clear_input = QPushButton("", self.composer)
        clear_input.setObjectName("RefreshBtn")
        clear_input.setFixedSize(36, 36)
        clear_input.setAccessibleName("清空输入")
        clear_input.setToolTip("清空输入内容")
        decorate(clear_input, "close")
        clear_input.clicked.connect(self.search_edit.clear)
        clear_input.setEnabled(False)
        self.search_edit.textChanged.connect(lambda: clear_input.setEnabled(bool(self.search_edit.text())))
        search_row.addWidget(clear_input)

        self.search_btn = QPushButton("开始搜索", self.composer)
        self.search_btn.setObjectName("Primary")
        self.search_btn.setFixedHeight(36)
        decorate(self.search_btn, "send")
        self.search_btn.clicked.connect(self._start_search)

        # 刷新搜索：按当前关键词和筛选条件再搜一次（搜索结果会更新）
        self.refresh_btn = QPushButton("", self.composer)
        self.refresh_btn.setObjectName("RefreshBtn")
        self.refresh_btn.setFixedSize(36, 36)
        self.refresh_btn.setAccessibleName("刷新搜索")
        decorate(self.refresh_btn, "refresh")
        self.refresh_btn.setToolTip("刷新搜索：用当前关键词和筛选条件重新搜一次")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._refresh_search)
        search_row.addWidget(self.refresh_btn)
        search_row.addWidget(self.search_btn)

        self.stop_btn = QPushButton("停止", self.composer)
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setFixedHeight(36)
        decorate(self.stop_btn, "stop")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._stop_search)
        search_row.addWidget(self.stop_btn)

        return dock

    def _build_sidebar(self) -> QWidget:
        side = QFrame(self)
        side.setObjectName("Sidebar")
        side.setMinimumWidth(SIDEBAR_MIN)
        side.setMaximumWidth(SIDEBAR_MAX)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(10)
        self._build_navigation(side, lay)
        lay.addSpacing(8)

        from .dialog_design import StyledDialog, primary
        self.site_manager = StyledDialog(self)
        self.site_manager.settings = self.settings
        self.site_manager.setWindowTitle('站点与黑名单')
        manager_root = self.site_manager.make_root('站点与黑名单',
            '仅在此窗口查看列表。勾选、添加和删除后立即保存。', 'settings', 680)
        self.manage_sites_btn = QPushButton('站点管理', side)
        self.manage_blacklist_btn = QPushButton('黑名单管理', side)
        for button, symbol, index in ((self.manage_sites_btn, 'globe', 0), (self.manage_blacklist_btn, 'library', 1)):
            decorate(button, symbol)
            button.setMinimumHeight(36)
            button.clicked.connect(lambda checked=False, tab=index: self._open_site_manager(tab))
            lay.addWidget(button)
        hint = QLabel('站点和屏蔽词已收起\n需要时再打开管理', side)
        hint.setObjectName('Hint')
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        if self.config._profile_name:
            account = QLabel('本地存档 · ' + self.config._profile_name, side)
            account.setTextFormat(Qt.TextFormat.PlainText)
            account.setWordWrap(True)
            account.setObjectName('Hint')
            lay.addWidget(account)
            self.logout_btn = QPushButton('退出账号 / 切换存档', side)
            self.logout_btn.clicked.connect(self._logout)
            lay.addWidget(self.logout_btn)

        self.tabs = QTabWidget(self.site_manager)
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName("SourceTabs")
        self.tabs.tabBar().setDrawBase(False)

        # -- 站点 -- #
        site_page = QWidget(self.site_manager)
        site_page.setObjectName("SidePage")
        sp = QVBoxLayout(site_page)
        sp.setContentsMargins(0, 6, 0, 0)
        sp.setSpacing(6)
        self.site_list = QListWidget(site_page)
        self.site_list.setToolTip("勾选=参与搜索；双击=编辑")
        self.site_list.itemChanged.connect(self._on_site_toggled)
        self.site_list.itemDoubleClicked.connect(lambda *_: self._edit_site())
        sp.addWidget(self.site_list, 1)

        site_btns = QHBoxLayout()
        site_btns.setSpacing(6)
        add_btn = QPushButton('添加站点', site_page)
        decorate(add_btn, 'plus')
        add_btn.clicked.connect(self._add_site)
        site_btns.addWidget(add_btn)
        edit_btn = QPushButton("编辑", site_page)
        decorate(edit_btn, "edit")
        edit_btn.clicked.connect(self._edit_site)
        del_btn = QPushButton("删除", site_page)
        del_btn.setObjectName("Danger")
        del_btn.clicked.connect(self._delete_site)
        reset_btn = QPushButton("", site_page)
        decorate(reset_btn, "refresh")
        reset_btn.setFixedWidth(36)
        reset_btn.setAccessibleName("恢复默认站点")
        reset_btn.setToolTip("恢复默认站点")
        reset_btn.setObjectName("Flat")
        reset_btn.clicked.connect(self._reset_sites)
        site_btns.addWidget(edit_btn)
        site_btns.addWidget(del_btn)
        site_btns.addStretch(1)
        site_btns.addWidget(reset_btn)
        sp.addLayout(site_btns)
        self.tabs.addTab(site_page, "站点")

        # -- 黑名单 -- #
        bl_page = QWidget(self.site_manager)
        bl_page.setObjectName("SidePage")
        bp = QVBoxLayout(bl_page)
        bp.setContentsMargins(0, 6, 0, 0)
        bp.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.black_edit = QLineEdit(bl_page)
        self.black_edit.setPlaceholderText("添加禁止搜索的词…")
        self.black_edit.returnPressed.connect(self._add_blackword)
        add_black = QPushButton("添加", bl_page)
        add_black.clicked.connect(self._add_blackword)
        row.addWidget(self.black_edit, 1)
        row.addWidget(add_black)
        bp.addLayout(row)
        self.black_list = QListWidget(bl_page)
        self.black_list.setToolTip("标题/描述/链接里出现这些词的结果会被过滤掉")
        bp.addWidget(self.black_list, 1)
        bl_row = QHBoxLayout()
        del_black = QPushButton("删除选中", bl_page)
        del_black.setObjectName("Danger")
        del_black.clicked.connect(self._delete_blackword)
        bl_row.addWidget(del_black)
        bl_row.addStretch(1)
        bp.addLayout(bl_row)
        self.tabs.addTab(bl_page, "黑名单")

        self.site_list.setMinimumHeight(240)
        self.black_list.setMinimumHeight(240)
        manager_root.addWidget(self.tabs, 1)
        finish = QPushButton('完成', self.site_manager)
        primary(finish)
        finish.clicked.connect(self.site_manager.accept)
        manager_actions = QHBoxLayout()
        manager_actions.addStretch(1)
        manager_actions.addWidget(finish)
        self.site_manager.finish_layout(manager_root, manager_actions)
        self.site_manager.resize(680, 570)

        div = QFrame(side)
        div.setObjectName("Divider")
        div.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(div)

        # Agent 正在忙的时候，提示放侧栏（不遮结果区）
        self.side_busy = QFrame(side)
        self.side_busy.setObjectName("SideBusy")
        sb = QHBoxLayout(self.side_busy)
        sb.setContentsMargins(10, 8, 10, 8)
        sb.setSpacing(8)
        self.side_busy_spinner = Spinner(self.side_busy)
        sb.addWidget(self.side_busy_spinner, 0, Qt.AlignmentFlag.AlignTop)
        sb_col = QVBoxLayout()
        sb_col.setSpacing(2)
        self.side_busy_title = QLabel("", self.side_busy)
        self.side_busy_title.setObjectName("SideBusyTitle")
        self.side_busy_title.setWordWrap(True)
        self.side_busy_sub = QLabel("", self.side_busy)
        self.side_busy_sub.setObjectName("SideBusySub")
        self.side_busy_sub.setWordWrap(True)
        sb_col.addWidget(self.side_busy_title)
        sb_col.addWidget(self.side_busy_sub)
        sb.addLayout(sb_col, 1)
        self.side_busy.setVisible(False)
        lay.addWidget(self.side_busy)

        network_panel = QFrame(side)
        network_panel.setObjectName("NetworkPanel")
        network_layout = QVBoxLayout(network_panel)
        network_layout.setContentsMargins(8, 6, 8, 0)
        network_layout.setSpacing(6)
        network_title = QLabel("连接状态", network_panel)
        network_title.setObjectName("SideSection")
        network_layout.addWidget(network_title)
        self.net_badge = QLabel("网络：检测中…", network_panel)
        self.net_badge.setObjectName("NetBadge")
        self.net_badge.setWordWrap(True)
        network_layout.addWidget(self.net_badge)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.net_btn = QPushButton("检测", side)
        decorate(self.net_btn, "refresh")
        self.net_btn.setObjectName("Flat")
        self.net_btn.setToolTip("重新检测本机代理 / VPN 状态，并按当前网络自动配置访问方式")
        self.net_btn.clicked.connect(self._refresh_network)
        bottom.addWidget(self.net_btn)
        cache_btn = QPushButton("清缓存", side)
        cache_btn.setObjectName("Flat")
        cache_btn.clicked.connect(self._clear_cache)
        bottom.addWidget(cache_btn)
        bottom.addStretch(1)
        network_layout.addLayout(bottom)
        lay.addWidget(network_panel)
        utility_row = QHBoxLayout()
        utility_row.setSpacing(4)
        self.history_btn = QPushButton("历史", side)
        self.history_btn.setObjectName("Flat")
        decorate(self.history_btn, "history")
        self.history_btn.clicked.connect(self._show_history)
        utility_row.addWidget(self.history_btn)
        settings_btn = QPushButton("设置", side)
        settings_btn.setObjectName("Flat")
        decorate(settings_btn, "settings")
        settings_btn.clicked.connect(self._open_settings)
        utility_row.addWidget(settings_btn)
        lay.addLayout(utility_row)
        self.download_btn = QPushButton('下载中心', side)
        decorate(self.download_btn, 'download')
        self.download_btn.clicked.connect(self._show_download_center)
        lay.addWidget(self.download_btn)
        self.update_btn = QPushButton('检查更新 · '+APP_VERSION, side)
        self.update_btn.setObjectName('Flat')
        decorate(self.update_btn, 'refresh')
        self.update_btn.clicked.connect(lambda:self._check_updates(False))
        lay.addWidget(self.update_btn)
        self.downloads.changed.connect(self._update_download_button)
        return side

    def _update_download_button(self):
        active = sum(j['state'] in ('queued', 'running', 'cancelling') for j in self.downloads.snapshot())
        self.download_btn.setText('下载中心' + (' · %d 项进行中' % active if active else ''))

    def _show_download_center(self):
        from .downloads import DownloadCenter
        if self._download_center is None or self._download_center._finished:
            if self._download_center is not None:
                self._download_center.deleteLater()
            self._download_center = DownloadCenter(self.downloads, self)
        self._download_center.refresh()
        self._download_center.showNormal()
        self._download_center.raise_()
        self._download_center.activateWindow()

    def _download_resource(self, row):
        from .downloads import DownloadDialog
        dialog = DownloadDialog(self.downloads, row, self)
        if dialog.exec():
            self._show_download_center()
        dialog.deleteLater()

    def _build_results(self) -> QWidget:
        wrap = QWidget(self)
        wrap.setObjectName("ResultArea")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget(wrap)
        header.setObjectName("ResultHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 14, 24, 6)
        hl.setSpacing(8)
        self.result_title = QLabel("搜索结果", header)
        self.result_title.setObjectName("ResultsTitle")
        hl.addWidget(self.result_title)
        self.result_count = QLabel("就绪", header)
        self.result_count.setObjectName("ResultCount")
        hl.addWidget(self.result_count)
        hl.addStretch(1)

        clear_btn = QPushButton("清空", header)
        decorate(clear_btn, "trash")
        clear_btn.setObjectName("Flat")
        clear_btn.clicked.connect(self._clear_results)
        hl.addWidget(clear_btn)
        lay.addWidget(header)

        # ---- 筛选栏：排序 / 时间 / 播放量 / 分组 ----
        fbar = QWidget(wrap)
        fbar.setObjectName("FilterBar")
        fl = QHBoxLayout(fbar)
        fl.setContentsMargins(24, 0, 24, 10)
        fl.setSpacing(10)
        fl.addWidget(QLabel("排序", fbar))
        self.sort_combo = QComboBox(fbar)
        self.sort_combo.addItem("相关度", "relevance")
        self.sort_combo.addItem("最新发布", "latest")
        self.sort_combo.addItem("播放最多", "views")
        self.sort_combo.model().item(2).setEnabled(self.settings.resource_type == 'video')
        self.sort_combo.setFixedWidth(100)
        self.sort_combo.currentIndexChanged.connect(self._apply_view)
        fl.addWidget(self.sort_combo)

        fl.addWidget(QLabel("时间", fbar))
        self.time_combo = QComboBox(fbar)
        self.time_combo.addItem("不限", 0)
        self.time_combo.addItem("24 小时内", 86400)
        self.time_combo.addItem("本周", 7 * 86400)
        self.time_combo.addItem("本月", 30 * 86400)
        self.time_combo.addItem("今年", 365 * 86400)
        self.time_combo.setFixedWidth(104)
        self.time_combo.currentIndexChanged.connect(self._apply_view)
        fl.addWidget(self.time_combo)

        fl.addWidget(QLabel("播放量", fbar))
        self.views_combo = QComboBox(fbar)
        self.views_combo.setEnabled(self.settings.resource_type == 'video')
        self.views_combo.addItem("不限", 0)
        self.views_combo.addItem("1 万以上", 10000)
        self.views_combo.addItem("10 万以上", 100000)
        self.views_combo.addItem("100 万以上", 1000000)
        self.views_combo.setFixedWidth(104)
        self.views_combo.currentIndexChanged.connect(self._apply_view)
        fl.addWidget(self.views_combo)

        self.source_button = QPushButton('来源：全部', fbar)
        self.source_button.setObjectName('SourceFilterButton')
        self.source_button.setFixedWidth(154)
        self.source_menu = QMenu(self.source_button)
        self.source_menu.setObjectName('SourceMenu')
        self.source_button.setMenu(self.source_menu)
        fl.addWidget(self.source_button)
        fl.addStretch(1)
        lay.addWidget(fbar)
        self.cover_notice = QPushButton('封面已关闭 · 点击开启', wrap)
        self.cover_notice.setObjectName('Flat')
        self.cover_notice.setVisible(not self.settings.show_thumbnails)
        self.cover_notice.clicked.connect(self._enable_thumbnails)
        lay.addWidget(self.cover_notice)

        source_panel = QWidget(self.source_menu)
        source_panel.setObjectName('SourcePopup')
        source_panel.setFixedWidth(360)
        sl = QVBoxLayout(source_panel)
        sl.setContentsMargins(12, 10, 12, 10)
        sl.setSpacing(8)
        self.source_search = QLineEdit(source_panel)
        self.source_search.setPlaceholderText('搜索平台')
        self.source_search.setClearButtonEnabled(True)
        self.source_search.textChanged.connect(self._filter_source_options)
        sl.addWidget(self.source_search)
        self.source_all = QCheckBox('全部来源', source_panel)
        self.source_all.setTristate(True)
        self.source_all.setToolTip('全选或取消所有来源；只筛选已收到的结果，不重新搜索')
        self.source_all.clicked.connect(self._source_all_clicked)
        sl.addWidget(self.source_all)
        source_scroll = QScrollArea(source_panel)
        self.source_scroll = source_scroll
        source_scroll.setWidgetResizable(True)
        source_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        source_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.source_options = QWidget(source_scroll)
        self.source_options.setObjectName('SourceOptions')
        self.source_layout = QVBoxLayout(self.source_options)
        self.source_layout.setContentsMargins(0, 0, 0, 0)
        self.source_layout.setSpacing(4)
        self.source_layout.addStretch(1)
        source_scroll.setWidget(self.source_options)
        sl.addWidget(source_scroll)
        self.source_search_empty = QLabel('没有匹配的平台', source_panel)
        self.source_search_empty.hide()
        sl.addWidget(self.source_search_empty)
        source_action = QWidgetAction(self.source_menu)
        source_action.setDefaultWidget(source_panel)
        self.source_menu.addAction(source_action)
        self.source_menu.aboutToShow.connect(self.source_search.clear)
        self.filter_empty_hint = QLabel('没有符合当前筛选条件的资源，请调整来源、时间或播放量。', wrap)
        self.filter_empty_hint.setObjectName('FilterEmptyHint')
        self.filter_empty_hint.setWordWrap(True)
        self.filter_empty_hint.hide()
        lay.addWidget(self.filter_empty_hint)

        # ---- Agent 模式提示条（平时隐藏） ----
        self.agent_bar = QWidget(wrap)
        self.agent_bar.setObjectName("AgentBar")
        al = QVBoxLayout(self.agent_bar)
        al.setContentsMargins(10, 8, 10, 8)
        al.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.agent_dot = QLabel("●", self.agent_bar)
        self.agent_dot.setObjectName("AgentDot")
        row1.addWidget(self.agent_dot)
        self.agent_state = QLabel("收件口未开启", self.agent_bar)
        self.agent_state.setWordWrap(True)
        self.agent_state.setObjectName("AgentText")
        row1.addWidget(self.agent_state)
        self.agent_endpoint = QLineEdit(self.agent_bar)
        self.agent_endpoint.setReadOnly(True)
        self.agent_endpoint.setObjectName("AgentUrl")
        row1.addStretch(1)
        self.agent_copy_btn = QPushButton("复制接入指令", self.agent_bar)
        self.agent_copy_btn.setObjectName("Secondary")
        decorate(self.agent_copy_btn, "copy")
        self.agent_copy_btn.setToolTip(
            "点一下，把「怎么接管这个工具」的完整说明复制到剪贴板；\n"
            "粘给你本机开着的 Agent（Claude / ChatGPT / Codex / DSH …）就行。")
        self.agent_copy_btn.clicked.connect(self._copy_agent_help)
        row1.addWidget(self.agent_copy_btn)
        self.agent_toggle_btn = QPushButton("开启", self.agent_bar)
        self.agent_toggle_btn.setObjectName("Flat")
        self.agent_toggle_btn.clicked.connect(self._toggle_agent_server)
        row1.addWidget(self.agent_toggle_btn)
        self.agent_pause_btn = QPushButton("暂停", self.agent_bar)
        self.agent_pause_btn.setObjectName("Flat")
        self.agent_pause_btn.setToolTip(
            "暂停 / 继续 Agent 的搜索：暂停后不再派任务，Agent 推来的结果也不再入库；\n"
            "点「继续」会接着让它搜下一轮。换关键词随时可以直接换，会自动中断当前这轮。")
        decorate(self.agent_pause_btn, "pause")
        self.agent_pause_btn.clicked.connect(self._toggle_agent_pause)
        row1.addWidget(self.agent_pause_btn)
        self.agent_disconnect_btn = QPushButton("断开 Agent", self.agent_bar)
        self.agent_disconnect_btn.setObjectName("Danger")
        decorate(self.agent_disconnect_btn, "stop")
        self.agent_disconnect_btn.setToolTip("中断本轮任务并断开当前 Agent；保留结果和收件口。\n重新接入需复制新的接入指令。")
        self.agent_disconnect_btn.clicked.connect(self._disconnect_agent)
        row1.addWidget(self.agent_disconnect_btn)
        details_btn = QPushButton("连接详情", self.agent_bar)
        details_btn.setObjectName("Flat")
        details_btn.setCheckable(True)
        details_btn.setToolTip("展开收件地址和人工接入说明")
        row1.addWidget(details_btn)
        al.addLayout(row1)
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel('搜索额度', self.agent_bar))
        self.agent_budget_combo = QComboBox(self.agent_bar)
        self.agent_budget_combo.addItem('限制模式 · 每批 100 条', 'limited')
        self.agent_budget_combo.addItem('无限模式 · 最多 1000 条', 'unlimited')
        self.agent_budget_combo.setCurrentIndex(1 if self.settings.agent_search_mode == 'unlimited' else 0)
        self.agent_budget_combo.setToolTip('限制模式搜索一批后等待滚动；无限模式自动续搜至本关键词累计 1000 条。\n切换模式实时调整额度，已达新上限则结束本轮，保留已有结果和接管。')
        self.agent_budget_combo.currentIndexChanged.connect(self._change_agent_budget)
        budget_row.addWidget(self.agent_budget_combo)
        self.agent_budget_label = QLabel('每批最多 100 条，滚动到底部再加载', self.agent_bar)
        self.agent_budget_label.setWordWrap(True)
        self.agent_budget_label.setToolTip('按任务筛选和黑名单过滤后去重的有效链接计数，不足时自动补足。\n之后调整显示筛选，可见卡片可能少于已接收数；新搜索重新计数。')
        budget_row.addWidget(self.agent_budget_label, 1)
        al.addLayout(budget_row)
        self.agent_details = QWidget(self.agent_bar)
        self.agent_details.setObjectName("AgentDetails")
        details_layout = QVBoxLayout(self.agent_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.addWidget(self.agent_endpoint)

        row_tip = QHBoxLayout()
        row_tip.setSpacing(8)
        self.agent_tip = QLabel(
            "复制指令 → 粘贴给本机 Agent → 保持轮询，即可接收搜索任务。", self.agent_bar)
        self.agent_tip.setToolTip("支持 Codex、Claude Code、DSH 等能执行命令的 Agent。工具无法自动唤醒已结束的对话。")
        self.agent_tip.setObjectName("AgentHint")
        self.agent_tip.setWordWrap(True)
        row_tip.addWidget(self.agent_tip, 1)
        details_layout.addLayout(row_tip)
        self.agent_network_state = QLabel('Agent 外网：首次需要时单独实测', self.agent_details)
        self.agent_network_state.setObjectName('AgentHint')
        self.agent_network_state.setTextFormat(Qt.TextFormat.PlainText)
        self.agent_network_state.setWordWrap(True)
        details_layout.addWidget(self.agent_network_state)
        self.agent_timing_state = QLabel('耗时：等待任务', self.agent_details)
        self.agent_timing_state.setObjectName('AgentHint')
        self.agent_timing_state.setWordWrap(True)
        self.agent_timing_state.setToolTip('网络累计只统计工具中转，含失败请求；并发请求的耗时会相加。\n首条回推从 Agent 确认领取开始计时，包含检测、搜索和处理，不能单独代表模型思考时间。')
        details_layout.addWidget(self.agent_timing_state)
        self.agent_details.hide()
        details_btn.toggled.connect(self.agent_details.setVisible)
        al.addWidget(self.agent_details)
        self.agent_bar.setVisible(False)
        agent_frame = QHBoxLayout()
        agent_frame.setContentsMargins(24, 0, 24, 12)
        agent_frame.addWidget(self.agent_bar)
        lay.addLayout(agent_frame)

        self.scroll = QScrollArea(wrap)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.result_host = QWidget()
        self.result_host.setObjectName("ResultArea")
        self.flow = FlowLayout(self.result_host, margin=16, spacing=12)
        self.scroll.setWidget(self.result_host)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.scroll.verticalScrollBar().actionTriggered.connect(self._agent_scroll_action)
        self.scroll.verticalScrollBar().sliderMoved.connect(self._agent_slider_moved)
        self.scroll.verticalScrollBar().sliderPressed.connect(self._agent_slider_pressed)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._queue_agent_scroll_check)
        for target in (self.scroll, self.scroll.viewport(), self.scroll.verticalScrollBar(), self.result_host):
            target.installEventFilter(self)

        self.empty = EmptyState(wrap)
        self.stack = QStackedWidget(wrap)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.scroll)
        lay.addWidget(self.stack, 1)
        # Fixed below the viewport: visible while cards stream in, and never a
        # scroll sentinel that could accidentally authorize another batch.
        self.more_loading = QWidget(wrap)
        self.more_loading.setObjectName('MoreLoading')
        more_row = QHBoxLayout(self.more_loading)
        more_row.setContentsMargins(24, 9, 24, 9)
        more_row.setSpacing(9)
        more_row.addStretch()
        self.more_spinner = Spinner(self.more_loading, size=18,
                                    color=theme.palette(self.settings.theme)['accent'])
        self.more_label = QLabel('', self.more_loading)
        self.more_label.setObjectName('MoreLoadingText')
        self.more_label.setWordWrap(True)
        more_row.addWidget(self.more_spinner)
        more_row.addWidget(self.more_label)
        more_row.addStretch()
        self.more_loading.hide()
        lay.addWidget(self.more_loading)
        self.pagination_notice = QLabel('', wrap)
        self.pagination_notice.setObjectName('FilterEmptyHint')
        self.pagination_notice.setWordWrap(True)
        self.pagination_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.pagination_notice.setContentsMargins(24, 8, 24, 8)
        self.pagination_notice.hide()
        notice_row = QHBoxLayout()
        notice_row.addWidget(self.pagination_notice, 1)
        self.notice_details = QPushButton('查看详情', wrap)
        self.notice_details.setObjectName('Flat')
        self.notice_details.clicked.connect(self._show_search_details)
        self.notice_details.hide()
        notice_row.addWidget(self.notice_details)
        self.global_search_btn = QPushButton('搜索全网', wrap)
        self.global_search_btn.setToolTip('启用“必应网页”全网来源，保留其他站点，再搜索当前关键词')
        self.global_search_btn.clicked.connect(self._enable_global_search)
        self.global_search_btn.hide()
        notice_row.addWidget(self.global_search_btn)
        lay.addLayout(notice_row)
        return wrap

    # ================================================================== #
    # 主题
    # ================================================================== #
    def _apply_theme(self) -> None:
        self.setStyleSheet(theme.stylesheet(self.settings.theme))
        colors = theme.palette(self.settings.theme)
        self.more_spinner._color = colors['accent']
        self.more_spinner.update()
        self.brand_mark.setPixmap(icon("library", colors["accent"], 20).pixmap(20, 20))
        self.library_mark.setPixmap(icon("folder", colors["sub"], 18).pixmap(18, 18))
        for button in self.findChildren(QPushButton):
            name = button.property("lineIcon")
            if name:
                color = colors["primary_text"] if button.objectName() == "Primary" else colors["sub"]
                button.setIcon(icon(name, color))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.empty.icon.setPixmap(icon("search", colors["accent"], 38).pixmap(38, 38))
        for card in self._cards:
            if not card.has_cover:
                card.set_cover(None)

    def _update_net_badge(self) -> None:
        text = describe_network(self.settings)
        self.net_badge.setText(text)
        self.net_badge.setMinimumHeight(max(18, self.net_badge.heightForWidth(SIDEBAR_MIN - 58)))
        ok = "✅" in text
        self.net_badge.setProperty("ok", "true" if ok else "false")
        self.net_badge.style().unpolish(self.net_badge)
        self.net_badge.style().polish(self.net_badge)

    # ================================================================== #
    # 站点 / 黑名单
    # ================================================================== #
    def _reload_sites(self) -> None:
        self._loading = True
        self.site_list.clear()
        for site in self.config.sites:
            item = QListWidgetItem(site.display)
            item.setData(Qt.ItemDataRole.UserRole, site.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if site.enabled else Qt.CheckState.Unchecked)
            tip = site.note or ""
            if site.search_url:
                tip = (tip + "\n" + site.search_url).strip()
            tip = (tip + "\n双击这一行可以改站点").strip()
            item.setToolTip(tip)
            if not site.is_valid():
                item.setForeground(QColor("#ff9f43"))
                item.setToolTip("配置不完整，已跳过：" + (tip or site.search_url))
            self.site_list.addItem(item)
        self._loading = False

        self._sync_source_filters()

    def _on_site_toggled(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        site_id = item.data(Qt.ItemDataRole.UserRole)
        site = self.config.get_site(site_id)
        if site is None:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        if site.enabled == enabled:
            return  # itemChanged also fires for result counts, colors and tooltips.
        site.enabled = enabled
        self._save_config()
        self._sites_changed()

    def _sites_changed(self, reset_ids=()) -> None:
        """Invalidate work captured before a whitelist edit, retaining visible cards."""
        self._restart_after_stop = False
        if self._searching:
            self._cancel.set()
            self._gen += 1
            self._finish_search()
        for site_id in reset_ids:
            self._site_pages.pop(site_id, None)
            self._site_exhausted.discard(site_id)
            self._site_unavailable.pop(site_id, None)
            self._site_page_notes.pop(site_id, None)
        self._newly_exhausted.clear()
        self._source_scope_changed()
        self._reset_agent_scroll()
        self._apply_view()
        if self._query:
            self._status('站点已更新，旧任务已停止；向下滚动按当前站点继续，或刷新重新搜索')

    def _selected_site(self) -> Optional[SiteConfig]:
        item = self.site_list.currentItem()
        if item is None:
            return None
        return self.config.get_site(item.data(Qt.ItemDataRole.UserRole))

    def _open_site_manager(self, tab=0):
        self.tabs.setCurrentIndex(tab)
        self.site_manager.show()
        self.site_manager.raise_()
        self.site_manager.activateWindow()

    def _site_dialog_parent(self):
        return self.site_manager if self.site_manager.isVisible() else self

    def _add_site(self) -> None:
        dlg = SiteDialog(None, self.http, self._query, self._site_dialog_parent())
        if dlg.exec() and dlg.result_site:
            from ..site_intake import same_site
            existing = next((site for site in self.config.sites if same_site(dlg.result_site, site)), None)
            if existing:
                existing.enabled = True
                self._save_config()
                self._reload_sites()
                self._sites_changed()
                for index in range(self.site_list.count()):
                    item = self.site_list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == existing.id:
                        self.site_list.setCurrentItem(item)
                self._status('该站点已在列表中，已启用并定位；保留原有配置。')
                return
            self.config.add_site(dlg.result_site)
            self._save_config()
            self._reload_sites()
            self._sites_changed()
            self._status("已添加站点：%s" % dlg.result_site.display)

    def _edit_site(self) -> None:
        site = self._selected_site()
        if site is None:
            return
        dlg = SiteDialog(site, self.http, self._query, self._site_dialog_parent())
        if dlg.exec() and dlg.result_site:
            updated = dlg.result_site
            for i, s in enumerate(self.config.sites):
                if s.id == site.id:
                    self.config.sites[i] = updated
                    break
            self._save_config()
            self._reload_sites()
            self._sites_changed([site.id])

    def _delete_site(self) -> None:
        site = self._selected_site()
        if site is None:
            return
        if QMessageBox.question(self._site_dialog_parent(), "删除站点", "确定从白名单里删除「%s」？" % site.display) \
                != QMessageBox.StandardButton.Yes:
            return
        self.config.remove_site(site.id)
        self._save_config()
        self._reload_sites()
        self._sites_changed([site.id])

    def _reset_sites(self) -> None:
        from ..config import default_sites

        if QMessageBox.question(self._site_dialog_parent(), "恢复默认",
                                "用默认站点名单覆盖当前白名单？自己添加的站点会丢失。") \
                != QMessageBox.StandardButton.Yes:
            return
        reset_ids = {s.id for s in self.config.sites}
        self.config.sites = default_sites()
        reset_ids.update(s.id for s in self.config.sites)
        self._save_config()
        self._reload_sites()
        self._sites_changed(reset_ids)

    def _reload_blacklist(self) -> None:
        self.black_list.clear()
        for word in self.config.blacklist:
            self.black_list.addItem(word)

    def _add_blackword(self) -> None:
        if self.config.add_blackword(self.black_edit.text()):
            self.black_edit.clear()
            self._save_config()
            self._reload_blacklist()
        else:
            self.black_edit.clear()

    def _delete_blackword(self) -> None:
        item = self.black_list.currentItem()
        if item is None:
            return
        self.config.remove_blackword(item.text())
        self._save_config()
        self._reload_blacklist()

    def _history_menu(self) -> QMenu:
        """构建「历史」下拉菜单（单独拆出来，方便测试直接检查菜单项）。"""
        menu = QMenu(self)
        if not self.config.history:
            act = menu.addAction("（还没有搜索记录）")
            act.setEnabled(False)
        for q in self.config.history[:20]:
            act = menu.addAction(q)
            act.triggered.connect(lambda _=False, text=q: self._use_history(text))
        menu.addSeparator()
        clear = menu.addAction("清空历史")
        clear.triggered.connect(self._clear_history)
        return menu

    def _show_history(self) -> None:
        menu = self._history_menu()
        menu.exec(self.history_btn.mapToGlobal(self.history_btn.rect().bottomLeft()))

    def _use_history(self, text: str) -> None:
        self.search_edit.setText(text)
        self.search_edit.setFocus()
        self._start_search()

    def _clear_history(self) -> None:
        self.config.history = []
        self._save_config()

    # ================================================================== #
    # 搜索
    # ================================================================== #
    def _resource_changed(self):
        kind = self.resource_combo.currentData()
        self._clear_results()
        self.settings.resource_type = kind
        self.views_combo.setCurrentIndex(0)
        self.views_combo.setEnabled(kind == 'video')
        self.sort_combo.model().item(2).setEnabled(kind == 'video')
        if self.sort_combo.currentData() == 'views':
            self.sort_combo.setCurrentIndex(0)
        self._save_config()
        self._status('已切换到%s搜索，请输入关键词搜索' % self.resource_combo.currentText())

    def _start_search(self) -> None:
        if self._searching:
            return
        # Agent 模式：先把大白话交给 AI 理解，再按解析出的关键词去搜
        if self.agent_mode and not self._ai_pending:
            query = self.search_edit.text().strip()
            if not query:
                self._status("先用一句话说说你想找什么")
                self.search_edit.setFocus()
                return
            hit = self.config.match_blacklist(query)
            if hit:
                QMessageBox.warning(self, "已被黑名单拦截",
                                    "这句话里含有黑名单词「%s」，已拒绝搜索。" % hit)
                return
            if self._agent_busy:
                if query.strip() == (self._query or "").strip():
                    server = self._agent_server
                    if server and server.task_status()["state"] in ("queued", "delivered"):
                        self._toast_show('任务已在等待领取，Agent 恢复轮询后会自动收到', 2600)
                    else:
                        self._toast_show("当前任务已领取；可以换关键词或断开 Agent", 2200)
                    return
                # 换了搜索内容：立刻中断当前这轮，按新内容重新派任务
                server = self._agent_server
                if server is not None:
                    server.clear_tasks()
                self._agent_busy_done()
                self._status("旧任务已撤销并通知 Agent，正在按「%s」派发新任务" % query)
            self._agent_search(query)
            return
        if self._agent_busy:
            self._toast_show("Agent 正在搜索，结果到了会自动出现；不用反复点", 2200)
            return
        query = self.search_edit.text().strip()
        if not query:
            self._status("先输入要搜索的内容")
            self.search_edit.setFocus()
            return
        hit = self.config.match_blacklist(query)
        if hit:
            QMessageBox.warning(self, "已被黑名单拦截",
                                "关键词「%s」命中了黑名单里的「%s」，已拒绝搜索。\n"
                                "如果这是误伤，可以打开「黑名单管理」删掉它。" % (query, hit))
            return

        enabled = self.config.enabled_sites()
        if not enabled:
            QMessageBox.warning(self, "没有可用站点",
                                "白名单里没有勾选任何站点。\n请打开「站点管理」勾选，或点击「添加站点」。")
            return

        self._cancel.set()
        self._query = query
        self._page = 1
        self._no_more = False
        self._paging = False
        self._paging_new = 0
        self._gen += 1
        self._clear_results(keep_empty=True)
        self._query = query
        self.config.add_history(query)
        self._save_config()

        self._searching = True
        self._cancel = threading.Event()
        self._done_sites = 0
        self._ok_sites = 0
        self._total_sites = len(enabled)
        self._reload_sites()          # 清掉上一轮的 +N / ✕ 标记
        self.progress.setRange(0, self._total_sites)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.search_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self.search_edit.setEnabled(False)
        self.empty.set_text("正在搜索…", "正在从 %d 个站点抓取「%s」" % (self._total_sites, query))
        self._status("搜索中：%s" % query)
        self.result_count.setText("搜索中…")
        self._loading = True
        for i in range(self.site_list.count()):
            item = self.site_list.item(i)
            site = self.config.get_site(item.data(Qt.ItemDataRole.UserRole))
            if site and site.enabled and site.is_valid():
                item.setForeground(QColor(theme.palette(self.settings.theme)["accent"]))
        self._loading = False

        self._worker = threading.Thread(
            target=self._run_engine, args=(query, self._gen, self._cancel), name="engine", daemon=True)
        self._worker.start()

    def _run_engine(self, query: str, gen: int, cancel) -> None:
        try:
            self.engine.run(query, lambda ev: self.bridge.event.emit(gen, ev), cancel)
        except Exception as exc:                     # noqa: BLE001
            self.bridge.event.emit(gen, SiteEvent(kind="fatal", error="搜索线程异常：%s" % exc))

    def _stop_search(self) -> None:
        if not self._searching:
            return
        self._cancel.set()
        self._status("正在停止…（已发出的请求会自然结束）")
        self.stop_btn.setEnabled(False)

    def _on_event(self, gen: int, ev: SiteEvent) -> None:
        if gen != self._gen:
            return                                    # 上一轮搜索的迟到事件，丢掉
        if self._paging:
            self._on_paging_event(ev)
            return
        if ev.kind == "start":
            self._status("搜索中：%s 正在抓取 %s" % (self._query, ev.site_name))
        elif ev.kind == "done":
            self._site_pages[ev.site_id] = 1
            self._record_page_status(ev)
            self._done_sites += 1
            self._ok_sites += 1
            self.progress.setValue(self._done_sites)
            self._mark_site(ev.site_id, len(ev.results or []), ok=True)
            self._add_cards(ev.site_id, ev.results or [])
        elif ev.kind == "error":
            self._done_sites += 1
            self._site_page_notes[ev.site_id] = ev.error or '请求失败，可重试'
            self.progress.setValue(self._done_sites)
            self._mark_site(ev.site_id, 0, ok=False, message=ev.error)
            self._status("%s 失败：%s" % (ev.site_name, (ev.error or "")[:90]))
        elif ev.kind == "fatal":
            self._finish_search()
            QMessageBox.warning(self, "无法搜索", ev.error or "未知错误")
            self._status(ev.error or "")
        elif ev.kind == "finish":
            self._finish_search()

    def _finish_search(self) -> None:
        if not self._searching:
            return
        self._searching = False
        self._queue_agent_scroll_check()
        self.search_btn.setVisible(True)
        self.search_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.stop_btn.setEnabled(True)
        self.search_edit.setEnabled(True)
        self.progress.setVisible(False)
        self.progress.setValue(0)
        n = len(self._cards)
        if n:
            self.result_count.setText("共 %d 个资源" % n)
            self._status("完成：%d 个资源（来自 %d/%d 个站点）"
                         % (n, self._ok_sites, self._total_sites))
        else:
            self.empty.set_text("当前搜索范围没有匹配结果",
                                "这不代表全网没有资源。可检查来源与筛选，或启用全网搜索。")
            self._status("没有找到结果")
            self.result_count.setText("0 个资源")
        if getattr(self, "_restart_after_stop", False):
            # 用户点了「刷新」：上一轮已经停稳，马上按同样的条件再搜一次
            QTimer.singleShot(0, self._restart_search_if_requested)
        self._show_page_status()

    def _restart_search_if_requested(self):
        if not self._closing and self._restart_after_stop:
            self._restart_after_stop = False
            self._start_search()

    def _mark_site(self, site_id: str, count: int, ok: bool, message: str = "") -> None:
        for i in range(self.site_list.count()):
            item = self.site_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) != site_id:
                continue
            site = self.config.get_site(site_id)
            name = site.display if site else item.text()
            pal = theme.palette(self.settings.theme)
            if ok:
                item.setText("%s  +%d" % (name, count))
                item.setForeground(QColor(pal["ok"]))
            else:
                item.setText("%s  ✕" % name)
                item.setForeground(QColor(pal["danger"]))
                item.setToolTip((site.note + "\n" if site and site.note else "") +
                                (message or "抓取失败"))
            break

    # ================================================================== #
    # 卡片
    # ================================================================== #
    def _add_cards(self, site_id: str, results: List[SearchResult]) -> None:
        if not results:
            return
        self._results_all.setdefault(site_id, []).extend(results)
        self._all_results.extend(results)
        site = self.config.get_site(site_id)
        if site is not None:
            self._referers[site_id] = site.referer or (
                site.search_url.split("{")[0] if site.search_url else "")
        if self.stack.currentIndex() != 1:
            self.stack.setCurrentIndex(1)
        self._apply_view()

    def _source_key(self, result):
        return result_group(result, self.config.sites)[0]

    def _sync_source_filters(self):
        groups = dict(site_group(s) for s in self.config.enabled_sites())
        counts = {}
        for row in self._all_results:
            for key, label in result_groups(row, self.config.sites).items():
                groups.setdefault(key, label)
                counts[key] = counts.get(key, 0) + 1
        for key in list(self._source_checks):
            if key not in groups:
                check = self._source_checks.pop(key)
                row = self._source_rows.pop(key)
                self._source_only_buttons.pop(key)
                self.source_layout.removeWidget(row)
                row.deleteLater()
        for key, label in groups.items():
            check = self._source_checks.get(key)
            if check is None:
                row = QWidget(self.source_options)
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(6)
                check = QCheckBox(row)
                check.setObjectName('SourceChip')
                check.setFixedHeight(34)
                check.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                check.clicked.connect(lambda checked, k=key: self._source_clicked(k, checked))
                self._source_checks[key] = check
                only = QPushButton('仅看', row)
                only.setObjectName('Flat')
                only.setFixedSize(52, 30)
                only.clicked.connect(lambda _checked=False, k=key: self._source_only(k))
                line.addWidget(check, 1)
                line.addWidget(only)
                self._source_rows[key] = row
                self._source_only_buttons[key] = only
                self.source_layout.insertWidget(self.source_layout.count() - 1, row)
            check.setText('%s  %d' % (label, counts.get(key, 0)))
            check.setAccessibleName(label + '来源筛选')
            self._source_only_buttons[key].setAccessibleName('仅看' + label + '来源')
            self._source_only_buttons[key].setToolTip('只显示%s；下次触底也只搜索该来源，其他站点配置保持不变' % label)
            check.setToolTip('%s：本轮已收到 %d 条，数量不受时间和播放量筛选影响' % (label, counts.get(key, 0)))
            if key in ('platform:bing', 'platform:baidu'):
                check.setText('%s（搜索来源）  %d' % (label, counts.get(key, 0)))
                check.setToolTip('显示通过%s找到的资源；同一资源也可属于其实际来源平台' % label)
            check.setChecked(self._source_selection is None or key in self._source_selection)
        selected = sum(c.isChecked() for c in self._source_checks.values())
        state = (Qt.CheckState.Checked if self._source_selection is None or selected == len(groups) and selected
                 else Qt.CheckState.PartiallyChecked if selected else Qt.CheckState.Unchecked)
        self.source_all.setCheckState(state)
        summary = ('全部' if self._source_selection is None else
                   '未选择' if not selected else
                   next(c.accessibleName().removesuffix('来源筛选') for c in self._source_checks.values() if c.isChecked())
                   if selected == 1 else '已选 %d 项' % selected)
        self.source_button.setText('来源：' + (summary if len(summary) <= 9 else summary[:8] + '…'))
        self.source_button.setToolTip('展开选择平台来源（可多选）。当前：' + summary)
        self._filter_source_options()

    def _filter_source_options(self, _text=''):
        query = self.source_search.text().strip().casefold()
        visible = 0
        for key, check in self._source_checks.items():
            match = not query or query in (check.text() + ' ' + key).casefold()
            self._source_rows[key].setVisible(match)
            check.setVisible(match)
            visible += int(match)
        self.source_scroll.setFixedHeight(min(280, max(38, visible * 38)))
        self.source_search_empty.setVisible(not visible)

    def _source_clicked(self, key, checked):
        self._source_scroll_hold = True
        if self._source_selection is None:
            self._source_selection = set(self._source_checks)
        if checked:
            self._source_selection.add(key)
        else:
            self._source_selection.discard(key)
        if set(self._source_checks).issubset(self._source_selection):
            self._source_selection = None
        self._source_scope_changed()
        self._reset_agent_scroll()
        self._apply_view()

    def _source_all_clicked(self, checked):
        self._source_scroll_hold = True
        self._source_selection = None if checked else set()
        self._source_scope_changed()
        self._reset_agent_scroll()
        self._apply_view()

    def _source_only(self, key):
        if key not in self._source_checks or self._source_selection == {key}:
            return
        self._source_scroll_hold = True
        self._source_selection = {key}
        self._source_scope_changed()
        self._reset_agent_scroll()
        self._apply_view()

    def _source_scope_changed(self):
        self._agent_fill_pending = False
        self._agent_empty_rounds = 0
        self._auto_more_timer.stop()
        self._no_more = False
        self._page_notice('')
        self._show_page_status()
        if self._paging:
            self._cancel.set()
            self._gen += 1
            self._paging = False
        if self.agent_mode and self._agent_busy:
            self._account_agent_budget()
            self._agent_server.clear_tasks()
            self._agent_task_id = self._agent_more_task = ''
            self._agent_busy_done()
            self._auto_more_timer.stop()
            self._status('来源已更改，旧批次已停止；向下滚动到底部按所选平台继续')
        self._sync_more_indicator()

    def _login_current(self, entry):
        if self._closing:
            return False
        if entry['scope'].startswith('direct:'):
            return not self.agent_mode and entry['scope'] == 'direct:' + str(id(self._cancel)) and not self._cancel.is_set()
        server = self._agent_server
        return bool(self.agent_mode and server and server.accepts(entry['scope']) and not server.paused and not server.cancellation(entry['scope']).is_set())

    def _on_login_event(self, entry):
        if not self._login_current(entry):
            return
        from .dialogs import PlatformLoginDialog
        key = (entry['scope'], entry['source_id'])
        live = self.logins.snapshot(entry['scope']).get(entry['source_id'])
        if live and live['state'] in ('skipped', 'ready') and entry['state'] != live['state']:
            return  # A queued connecting/checking update must not reopen a finished dialog.
        dialog = self._login_dialogs.get(key)
        state = entry['state']
        if state in ('ready', 'skipped'):
            if dialog:
                dialog.finish()
                self._login_dialogs.pop(key, None)
            if state == 'skipped':
                self._page_notice(entry['name'] + '：本轮已跳过；其他平台继续')
                return
            source = entry['source_id']
            if entry['scope'].startswith('direct:'):
                self._site_pages[source] = 0
                self._site_exhausted.discard(source)
                self._site_unavailable.pop(source, None)
                self._no_more = False
                self._login_retry_rows[key] = entry
                self._retry_login_source(entry)
            else:
                access = self._agent_server.access
                cache = getattr(access._search_http, '_pagination_cache', {})
                for cache_key in list(cache):
                    if cache_key[0] == source:
                        cache.pop(cache_key, None)
                self._status(entry['name'] + ' 登录验证通过，已通知 Agent 从该来源第一页继续')
            return
        if dialog is None:
            dialog = PlatformLoginDialog(entry, self)
            self._login_dialogs[key] = dialog
            dialog.verify_requested.connect(lambda e=dict(entry): self._verify_login(e))
            dialog.open_requested.connect(lambda e=dict(entry): self._open_login_window(e))
            dialog.skip_requested.connect(lambda e=dict(entry): self.logins.skip(e['scope'], e['source_id']))
            dialog.show()
            if entry.get('supported') and self.config._profile_name:
                self._open_login_window(dict(entry))
        dialog.set_message(entry['reason'])
        dialog.set_verifying(state == 'verifying', entry.get('phase', ''))
        dialog.verify_button.setEnabled(state == 'waiting' and entry.get('supported', False))

    def _verify_login(self, entry):
        if self._login_current(entry):
            threading.Thread(target=lambda: self.logins.verify(entry['scope'], entry['source_id'], auto_connect=True),
                             name='login-verify', daemon=True).start()

    def _open_login_window(self, entry):
        if not self._login_current(entry):
            return
        def open_window():
            try:
                self.logins.open(entry['scope'], entry['source_id'])
            except Exception as exc:
                if self._login_current(entry):
                    self.bridge.login_event.emit(dict(entry, reason='专用登录窗口未能打开：' + str(exc)[:600]))
        threading.Thread(target=open_window, name='login-open-window', daemon=True).start()

    def _retry_login_source(self, entry):
        key = (entry['scope'], entry['source_id'])
        if not self._login_current(entry) or key not in self._login_retry_rows:
            self._login_retry_rows.pop(key, None)
            return
        if self._searching or self._paging:
            QTimer.singleShot(200, lambda: self._retry_login_source(entry))
            return
        self._login_retry_rows.pop(key, None)
        site = self.config.get_site(entry['source_id'])
        if not site or not site.enabled:
            return
        self._status(site.display + '：登录验证通过，正在恢复搜索')
        self._paging = True
        self._sync_more_indicator()
        cancel = self._cancel
        def run():
            events = []
            self.engine.run(entry['query'], events.append, cancel, sites=[site], page=1)
            self.bridge.login_retry.emit((entry, events))
        threading.Thread(target=run, name='login-resume-search', daemon=True).start()

    def _on_login_retry(self, payload):
        entry, events = payload
        if not self._login_current(entry):
            return
        self._paging = False
        for event in events:
            if event.kind == 'done':
                self._site_pages[event.site_id] = 1
                self._record_page_status(event)
                self._append_results(event.results or [])
                self._mark_site(event.site_id, len(event.results or []), True)
            elif event.kind == 'error':
                self._page_notice(event.error)
        self._sync_more_indicator()

    def _close_login_dialogs(self):
        for dialog in list(self._login_dialogs.values()):
            dialog.finish()
        self._login_dialogs.clear()
        self._login_retry_rows.clear()

    def _selected_search_sites(self):
        blocked = self.logins.snapshot('direct:' + str(id(self._cancel))) if not self.agent_mode else {}
        return [s for s in self.config.enabled_sites() if
                blocked.get(s.id, {}).get('state') not in ('waiting', 'verifying', 'skipped') and
                (self._source_selection is None or site_group(s)[0] in self._source_selection)]

    def _filtered_results(self) -> List[SearchResult]:
        items = list(self._all_results)
        if self._source_selection is not None:
            items = [r for r in items if self._matches_source(r, self._source_selection)]
        now = time.time()
        span = self.time_combo.currentData() or 0
        if span:
            items = [r for r in items if r.pub_ts and 0 <= now - r.pub_ts <= span]
        min_views = self.views_combo.currentData() or 0
        if min_views:
            items = [r for r in items if r.resource_type != 'video' or r.views >= min_views]
        mode = self.sort_combo.currentData()
        if mode == "latest":
            items.sort(key=lambda r: -(r.pub_ts or 0))
        elif mode == "views":
            items.sort(key=lambda r: -(r.views or 0))
        return items

    def _apply_view(self) -> None:
        """按当前筛选条件重建卡片列表。"""
        self.cover_notice.setVisible(not self.settings.show_thumbnails)
        self._sync_source_filters()
        items = self._filtered_results()
        hidden = len(self._all_results) - len(items)

        existing = {card.result.key: card for card in self._cards}
        append_only = (len(items) >= len(self._cards) and
                       all(card.result.key == res.key for card, res in zip(self._cards, items)))
        old_count = len(self._cards)
        if not append_only:
            while self.flow.count():
                self.flow.takeAt(0)
        cards = []
        for index, res in enumerate(items):
            card = existing.pop(res.key, None) or self._make_card(res)
            cards.append(card)
            if not append_only or index >= old_count:
                self.flow.addWidget(card)
                card.show()
        for card in existing.values():
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self._cards = cards
        self.filter_empty_hint.setVisible(bool(self._all_results) and not cards)
        self.stack.setCurrentIndex(1 if self._all_results else 0)
        if not cards:
            self.result_host.setMinimumHeight(0)

        total = len(items)
        if self._searching:
            self.result_count.setText("已找到 %d 个…" % total)
        else:
            txt = "共 %d 个资源" % total
            if hidden:
                txt += "（筛选掉了 %d 条）"
                txt = txt % hidden
            self.result_count.setText(txt)
        self.flow.invalidate()
        self._relayout_results()
        self.result_host.update()
        QTimer.singleShot(0, self._pump_thumbs)

    def _relayout_results(self) -> None:
        """按可视宽度重排卡片，并把容器高度撑够（不然右侧会空一大块）。"""
        w = self.scroll.viewport().width()
        if w <= 0 or self.flow.count() <= 0:
            return
        height = self.flow.heightForWidth(w)
        self.result_host.setMinimumWidth(0)
        self.result_host.setMinimumHeight(max(0, height))
        # 光 invalidate 不够：容器尺寸没变时 Qt 不会再跑一遍布局，
        # 新加进来的卡片会停在 (0,0) 叠在一起。手动激活一次。
        self.flow.invalidate()
        self.flow.activate()

    def _pump_thumbs(self) -> None:
        """只给「看得见 + 屏幕外一屏」的卡片下封面，滚动到哪里就下到哪里。"""
        if self._closing or not self._cards:
            return
        vp_h = self.scroll.viewport().height() or 700
        top = self.scroll.verticalScrollBar().value()
        low = top - vp_h
        high = top + vp_h * 2                    # 往下预取一屏
        for card in self._cards:
            y = card.y()
            if y + card.height() < low or y > high or card.isHidden():
                continue
            row = card.result
            if (row.resource_type == 'video' and not row.duration and video_identity(row.url)[0]
                    and time.monotonic() >= getattr(card, '_duration_retry_at', 0)
                    and not getattr(card, '_duration_requested', False)):
                card._duration_requested = self.durations.request(row.url,
                    lambda text, row=row: self.bridge.duration.emit(row, text))
            if not self.settings.show_thumbnails:
                continue
            if getattr(card, "_thumb_requested", True):
                continue
            if time.monotonic() < getattr(card, '_thumb_retry_at', 0):
                continue
            card._thumb_requested = True         # type: ignore[attr-defined]
            self._request_thumb(card, getattr(card, "_referer", ""))

    def _on_duration(self, row, text):
        # Rebuilt filters may have new cards for the same row; a new search has new rows.
        if self._closing or not any(row is r for r in self._all_results):
            return
        if text and not row.duration:
            row.duration = text
        for card in self._cards:
            if card.result is row:
                card.set_duration(row.duration)
                card._duration_requested = False
                card._duration_retry_at = time.monotonic() + 60
                if not row.duration:
                    card.cover.setToolTip('暂未获取到可靠时长；可能受网络、平台限制或直播状态影响')

    def _on_scroll(self, _value: int = 0) -> None:
        # 滚动时别每个像素都算一遍，攒 60ms 再跑
        self._pump_timer.start()
        if self.agent_mode:
            self._queue_agent_scroll_check()
        else:
            self._queue_agent_scroll_check()

    def eventFilter(self, obj, event):
        # Ignore destruction/layout events before accessing child Qt wrappers.
        if event.type() in (QEvent.Type.Wheel, QEvent.Type.KeyPress) and hasattr(self, 'scroll'):
            if obj is self.scroll or self.scroll.isAncestorOf(obj):
                if event.type() == QEvent.Type.Wheel:
                    self._source_scroll_hold = False
                    delta = event.pixelDelta().y() or event.angleDelta().y()
                    if delta < 0:
                        self._agent_user_scroll()
                    elif delta > 0:
                        self._reset_agent_scroll()
                else:
                    key = event.key()
                    if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                               Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_Space):
                        self._source_scroll_hold = False
                    backwards = key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Home) or (
                        key == Qt.Key.Key_Space and event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    if backwards:
                        self._reset_agent_scroll()
                    elif key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_End, Qt.Key.Key_Space):
                        self._agent_user_scroll()
        return super().eventFilter(obj, event)

    def _reset_agent_scroll(self):
        self._agent_scroll_requested = self._agent_scroll_pending = False
        self._agent_scroll_task = ''
        self._agent_scroll_timer.stop()

    def _queue_agent_scroll_check(self, *_):
        if self._agent_scroll_requested or self._agent_scroll_pending:
            self._agent_scroll_timer.start(0)

    def _agent_user_scroll(self, *_):
        if not self._query:
            return
        self._agent_scroll_requested = True
        self._agent_scroll_task = self._agent_task_id if self.agent_mode else str(self._gen)
        # Slider signals are emitted BEFORE Qt updates its value. Coalesce them
        # and inspect the final position only on the next event-loop turn.
        self._queue_agent_scroll_check()

    def _agent_slider_pressed(self):
        self._source_scroll_hold = False
        self._agent_drag_position = self.scroll.verticalScrollBar().value()

    def _agent_slider_moved(self, position):
        if position > self._agent_drag_position:
            self._agent_user_scroll()
        elif position < self._agent_drag_position:
            self._reset_agent_scroll()
        self._agent_drag_position = position

    def _agent_scroll_action(self, action):
        self._source_scroll_hold = False
        action = QAbstractSlider.SliderAction(action)
        A = QAbstractSlider.SliderAction
        if action in (A.SliderSingleStepAdd, A.SliderPageStepAdd, A.SliderToMaximum):
            self._agent_user_scroll()
        elif action in (A.SliderSingleStepSub, A.SliderPageStepSub, A.SliderToMinimum):
            self._reset_agent_scroll()
        elif action == A.SliderMove:
            bar = self.scroll.verticalScrollBar()
            if bar.sliderPosition() > bar.value():
                self._agent_user_scroll()
            elif bar.sliderPosition() < bar.value():
                self._reset_agent_scroll()

    def _check_agent_scroll(self):
        if not self._agent_scroll_requested and not self._agent_scroll_pending:
            return
        if not self.agent_mode:
            self._check_direct_scroll()
            return
        server = self._agent_server
        if (not self.agent_mode or not server or not self._query or self._no_more or server.paused or
                self._agent_scroll_task != self._agent_task_id or not server.session_ready()):
            self._reset_agent_scroll()
            return
        requested = self._agent_scroll_requested
        self._agent_scroll_requested = False
        bar = self.scroll.verticalScrollBar()
        if bar.value() < bar.maximum() - 8:
            self._agent_scroll_pending = False
            return
        self._agent_scroll_pending = self._agent_scroll_pending or requested
        if self._agent_busy:
            return  # Remember one genuine bottom gesture until this batch ends.
        self._reset_agent_scroll()  # Consume once; a new batch needs a new gesture.
        self._load_agent_batch()

    def _check_direct_scroll(self):
        if not self._query or self._agent_scroll_task != str(self._gen):
            self._reset_agent_scroll()
            return
        bar = self.scroll.verticalScrollBar()
        if bar.value() < bar.maximum() - 8:
            self._reset_agent_scroll()
            return
        self._agent_scroll_pending = True
        self._agent_scroll_requested = False
        if self._searching or self._paging:
            return
        self._reset_agent_scroll()
        self._load_more()

    # ---- 无限滚动 --------------------------------------------------- #
    def _card_click(self, url):
        # Old configs may still contain open_in_browser=False.
        self._open_link(url)

    def _make_card(self, res: SearchResult) -> ResultCard:
        card = ResultCard(res, self.result_host)
        card.installEventFilter(self)
        card.copy_requested.connect(self._copy_link)
        card.open_requested.connect(self._open_link)
        card.click_requested.connect(self._card_click)
        card.copied.connect(lambda text: self._toast_show(text, 1800))
        card.download_requested.connect(self._download_resource)
        site = self.config.get_site(res.source_id)
        if site:
            referer = site.referer or (site.search_url.split('{')[0] if site.search_url else '')
        else:
            referer = self._referers.get(res.source_id, '')
        card._referer = referer or res.url
        if res.resource_type == 'image':
            card._referer = res.extra.get('page_url') or res.url
        elif res.resource_type == 'article' and not (site and site.referer):
            card._referer = res.extra.get('page_url') or res.url
        card._thumb_requested = False                           # type: ignore[attr-defined]
        if not self.settings.show_thumbnails or not card.cover_url:
            card.set_cover(None)
        return card

    def _filters_active(self) -> bool:
        return bool(self.time_combo.currentData() or self.views_combo.currentData()
                    or (self.sort_combo.currentData() or "relevance") != "relevance"
                    or self._source_selection is not None)

    def _maybe_load_more(self) -> None:
        if self.agent_mode:
            self._queue_agent_scroll_check()
            return
        if self._agent_busy or self._searching or self._paging or self._no_more:
            return
        if not self._query or not self._all_results:
            return
        bar = self.scroll.verticalScrollBar()
        if bar.value() < bar.maximum() - 240:
            return
        if self.agent_mode:
            self._ask_agent_more()
        else:
            self._load_more()

    def _load_agent_batch(self):
        server = self._agent_server
        if not self.agent_mode or not server or server.paused or self._agent_busy or not self._query or self._no_more:
            return
        if not server.session_ready():
            return
        self._agent_empty_rounds = 0
        self._page_notice('')
        if self.settings.agent_search_mode == 'limited' and self._budget_remaining() <= 0:
            self._agent_batch_start = self._agent_budget_used
            self._agent_grant_end = self._agent_budget_used + 100
        self._ask_agent_more()

    def _budget_remaining(self):
        return max(0, self._agent_grant_end - self._agent_budget_used)

    def _account_agent_budget(self, received=None):
        if received is None:
            server = self._agent_server
            if not server or not server.accepts(self._agent_task_id):
                return
            received = server.task_status().get('received', 0)
        count = max(self._agent_accounted, int(received or 0))
        self._agent_budget_used += count - self._agent_accounted
        self._agent_accounted = count

    def _update_agent_budget(self):
        unlimited = self.settings.agent_search_mode == 'unlimited'
        if unlimited:
            text = '本关键词 %d / 1000 条%s' % (self._agent_budget_used, ' · 已达上限' if not self._budget_remaining() else ' · 自动续搜')
        else:
            count = self._agent_budget_used - self._agent_batch_start
            suffix = ' · 继续补足本批'
            if not self._budget_remaining():
                suffix = ' · 本批已满，触底再找 100 条' if count >= 100 else ' · 触底开始下一批 100 条'
            if not self._query:
                suffix = ' · 满 100 条后等待滚动'
            text = '本批 %d / 100 条 · 累计 %d 条%s' % (count, self._agent_budget_used, suffix)
        if self._no_more:
            text += ' · 资源不足，详见底部说明'
        elif getattr(self, '_agent_search_limited', False):
            text = text.replace(' · 自动续搜', '').replace(' · 继续补足本批', '')
            text += ' · 本轮已停止，向下滚动可重试'
        elif self._agent_empty_rounds >= 3:
            text += ' · 无新增，补足已暂停'
        server = self._agent_server
        if self._query and self._budget_remaining() and server:
            state = server.task_status()['state']
            if state in ('queued', 'delivered'):
                text = text.replace(' · 自动续搜', '').replace(' · 继续补足本批', '')
                text += ' · 等待 Agent 领取' if state == 'queued' else ' · 等待 Agent 确认领取'
            elif state == 'working' and not server.connected() and not server.liveness()['registered_searches']:
                text = text.replace(' · 自动续搜', '').replace(' · 继续补足本批', '')
                text += ' · 等待 Agent 新进度'
        self.agent_budget_label.setText(text)

    def _change_agent_budget(self, *_):
        from contextlib import nullcontext
        server = self._agent_server
        self.settings.agent_search_mode = self.agent_budget_combo.currentData()
        self._save_config()
        self._auto_more_timer.stop()
        self._agent_fill_pending = False
        self._reset_agent_scroll()
        update = None
        with server._task_lock if server else nullcontext():
            if server and server.accepts(self._agent_task_id) and server._task_result_rows:
                self._on_agent_results(dict(results=list(server._task_result_rows),query='',
                    meta=dict(task_id=self._agent_task_id,received=server._task_received,done=False,budget_sync=True)))
            self._account_agent_budget()
            self._agent_grant_end = 1000 if self.settings.agent_search_mode == 'unlimited' else self._agent_batch_start + 100
            if server and server.accepts(self._agent_task_id):
                # Total includes this task's received count, including callbacks
                # still waiting in Qt's queue. Preserve the batch origin.
                want = self._agent_accounted + self._agent_grant_end - self._agent_budget_used
                update = server.change_budget(self._agent_task_id, want, self.settings.agent_search_mode)
        self._agent_empty_rounds = 0
        self._agent_search_limited = False
        if update and update['done']:
            self._on_agent_results(dict(results=[], query='', meta=update))
        elif update:
            self._status('额度已实时调整：本任务已收到 %d 条，还需 %d 条；继续当前搜索' % (update['received'], update['remaining']))
        elif self.agent_mode and self._query and self._budget_remaining() > 0:
            self._agent_busy_done()
            self._no_more = False
            self._ask_agent_more()
        self._update_agent_bar()
        self._update_agent_budget()
        self._toast_show('模式已切换，已按现有结果重新计算额度；接管保留', 2500)

    def _ask_agent_more(self) -> None:
        """Agent 模式滚到底：请 Agent 再找一批，并告诉它别重复已给过的链接。"""
        server = self._agent_server
        if server is None or not server.running:
            self._toast_show("收件口没开着，没法让 Agent 继续加载", 2400)
            return
        if not server.session_ready():
            self._toast_show("还没有 Agent 接管，先点「复制接入指令」粘给它", 3000)
            return
        if server.paused or self._agent_busy or not self.agent_mode:
            return
        if self._source_selection == set():
            self._status('请至少勾选一个来源，再向下滚动续搜')
            return
        remaining = self._budget_remaining()
        if remaining <= 0:
            self._update_agent_budget()
            return
        exclude = list(self._agent_seen_urls | {r.url for r in self._all_results if r.url})
        filters = {
            'resource_type': self.settings.resource_type,
            "sort": self.sort_combo.currentData() or "relevance",
            "time": {86400: "24h", 604800: "7d", 2592000: "30d", 31536000: "1y"}.get(self.time_combo.currentData(), "any"),
            "min_views": int(self.views_combo.currentData() or 0),
        }
        if self._source_selection is not None:
            filters['source_keys'] = sorted(self._source_selection)
            filters['source_names'] = [c.accessibleName().removesuffix('来源筛选')
                                       for k, c in self._source_checks.items() if k in self._source_selection]
        task = server.push_task(self._query, filters, kind="more",
                                want=remaining, exclude=exclude)
        self._agent_more_task = str(task.get("id") or "")
        self._track_agent_task(task)
        info = server.connected_agent()
        who = str(info.get("name") or "Agent")
        self._agent_busy_show("%s 正在找更多…" % who,
                              "第 %d 轮：再找最多 %d 条，边搜边显示；跳过已有的 %d 条"
                              % (int(task.get("round") or 1), task['want'], len(exclude)))
        self._update_agent_budget()
        self._status("续搜任务已入队，等待 Agent 领取（%s）" % task.get("id"))

    def _auto_agent_more(self) -> None:
        """Fill the remaining user-authorized allowance without granting a new batch."""
        server = self._agent_server
        if not self._agent_fill_pending or self._budget_remaining() <= 0:
            return
        if not self.agent_mode or server is None or not server.running:
            return
        if self._no_more or self._agent_busy or self._searching or self._ai_pending:
            return
        if server.paused or not server.connected():
            return
        self._ask_agent_more()

    def _load_more(self) -> None:
        if self._paging or self._searching or not self._query:
            return
        sites = self._selected_search_sites()
        if not sites:
            self._status('请勾选来源；若所选平台没有对应站点，请先在「站点管理」中启用或配置该平台')
            return
        sites = [s for s in sites if s.id not in self._site_exhausted and s.id not in self._site_unavailable]
        if not sites:
            self._no_more = True
            self._show_page_status()
            return
        self._paging = True
        self._paging_new = 0
        self._paging_failed = False
        pages = {s.id: self._site_pages.get(s.id, 0) + 1 for s in sites}
        self._paging_pages = pages
        self._paging_source_keys = None if self._source_selection is None else set(self._source_selection)
        self._page = max(pages.values())
        if self._cancel.is_set():
            self._cancel = threading.Event()
        self.result_count.setText("加载第 %d 页…" % self._page)
        self._status("正在加载更多（第 %d 页）…" % self._page)
        self._sync_more_indicator()
        gen = self._gen
        page = self._page
        query, cancel = self._query, self._cancel

        def work() -> None:
            try:
                self.engine.run(query,
                                lambda ev: self.bridge.event.emit(gen, ev),
                                cancel, sites=sites, page=page, pages=pages)
            except Exception as exc:                 # noqa: BLE001
                self.bridge.event.emit(gen, SiteEvent(kind="fatal", error="翻页失败：%s" % exc))

        threading.Thread(target=work, name="paging", daemon=True).start()

    def _on_paging_event(self, ev: SiteEvent) -> None:
        if ev.kind == "done":
            self._site_pages[ev.site_id] = self._paging_pages.get(ev.site_id, self._page)
            scope = getattr(self, '_paging_source_keys', None)
            rows = [r for r in (ev.results or []) if scope is None or self._matches_source(r, scope)]
            added = self._append_results(rows)
            self._paging_new += added
            self._record_page_status(ev)
            if not added and ev.exhausted is not True and ev.pagination_supported and not ev.pagination_message:
                self._site_page_notes[ev.site_id] = '本页没有新增的可显示资源，再次触底可继续'
        elif ev.kind == "error":
            self._paging_failed = True
            self._site_page_notes[ev.site_id] = (ev.error or '加载失败') + '；再次触底可重试'
            self._status(ev.error or "此页部分站点失败，滚动后可重试")
            self._page_notice('%s：%s；再次触底可重试' % (ev.site_name or ev.site_id, ev.error))
        elif ev.kind == "finish":
            self._finish_paging()
        elif ev.kind == "fatal":
            self._paging = False
            self._status(ev.error or "翻页失败")
            self._page_notice((ev.error or '加载失败') + '；再次触底可重试')
            self._sync_more_indicator()

    def _finish_paging(self) -> None:
        self._paging = False
        self._sync_more_indicator()
        if getattr(self, "_paging_failed", False):
            self._page = max(self._site_pages.values(), default=max(1, self._page - 1))
            self._status("部分站点加载失败，保留已有结果；再次滚到底可重试")
        elif self._paging_new <= 0:
            self._no_more = False
            self._status('本页没有新增的可显示资源（可能重复或被筛选），再次触底可继续')
            self._page_notice('本页没有新增的可显示资源；再次向下滚动可继续搜索')
        else:
            self._status("又加载了 %d 条，共 %d 个资源"
                         % (self._paging_new, len(self._all_results)))
        visible = len(self._cards)
        hidden = len(self._all_results) - visible
        self.result_count.setText('共 %d 个资源%s' % (visible, '（筛选掉了 %d 条）' % hidden if hidden else ''))
        self._paging_new = 0
        self._show_page_status()
        self._queue_agent_scroll_check()

    def _matches_source(self, row, scope):
        return bool(set(result_groups(row, self.config.sites)) & scope)

    def _sync_more_indicator(self):
        if not hasattr(self, 'more_loading'):
            return
        message, spinning = '', False
        if not self.agent_mode and self._paging:
            message, spinning = '正在加载更多资源…', True
        elif self.agent_mode and self._agent_fill_pending:
            server = self._agent_server
            if server and server.running and not server.paused:
                spinning = server.connected()
                message = ('本批尚差 %d 条，正在准备补足…' % self._budget_remaining() if spinning
                           else '等待 Agent 响应，剩余数量待补足')
        elif self.agent_mode and self._agent_busy:
            server = self._agent_server
            if (server and server.running and not server.paused and self._agent_more_task
                    and self._agent_more_task == self._agent_task_id
                    and server.accepts(self._agent_task_id)):
                state = server.task_status()['state']
                if state not in ('completed', 'failed'):
                    if not server.connected():
                        message = '等待 Agent 响应，续搜任务已保留'
                    elif state in ('queued', 'delivered'):
                        message, spinning = '等待 Agent 领取续搜任务…', True
                    elif server.access and server.access.pending():
                        message, spinning = '正在检查网络，随后加载更多资源…', True
                    else:
                        message, spinning = '正在加载更多资源…', True
        self.more_label.setText(message)
        self.more_loading.setVisible(bool(message))
        if spinning:
            self.more_spinner.start()
        else:
            self.more_spinner.stop()

    def _page_notice(self, message, summary=None):
        self._search_details_text = message
        compact = len(message) > 180
        self.pagination_notice.setText((summary or '部分来源未返回匹配结果或暂不可用；这不代表全网没有资源。') if compact else message)
        self.pagination_notice.setVisible(bool(message))
        self.notice_details.setVisible(compact)
        self.global_search_btn.setVisible(bool(self._query) and not self.agent_mode and not self._searching
            and not self._all_results and not self._has_global_source())

    def _has_global_source(self):
        from ..sources import host_of, matches
        return any(not s.scope_domain and (s.builtin == 'bing' or
            matches(host_of(s.search_url), 'bing.com') or matches(host_of(s.search_url), 'baidu.com'))
            for s in self.config.enabled_sites())

    def _enable_global_search(self):
        if self._searching or self.agent_mode:
            return
        from ..config import builtin_sites, new_site_id
        site = next((s for s in self.config.sites if s.builtin == 'bing' and not s.scope_domain), None)
        if site is None:
            site = next(s for s in builtin_sites() if s.builtin == 'bing')
            if self.config.get_site(site.id):
                site.id = new_site_id('global-bing')
            self.config.add_site(site)
        site.enabled = True
        self._save_config()
        self._reload_sites()
        self._start_search()

    def _show_search_details(self):
        from .dialog_design import StyledDialog
        from PySide6.QtWidgets import QPlainTextEdit
        dialog = StyledDialog(self)
        dialog.setWindowTitle('搜索来源详情')
        root = dialog.make_root('搜索来源详情', '这些提示描述本次查询，不代表全网资源是否存在。', 'globe', 680)
        content = QPlainTextEdit(dialog)
        content.setReadOnly(True)
        content.setPlainText(getattr(self, '_search_details_text', '').replace('；', '\n\n'))
        content.setMinimumHeight(260)
        root.addWidget(content)
        close = QPushButton('完成', dialog)
        close.clicked.connect(dialog.accept)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close)
        dialog.finish_layout(root, footer)
        dialog.exec()

    def _record_page_status(self, ev):
        self._site_page_notes.pop(ev.site_id, None)
        if ev.pagination_message and ev.pagination_supported:
            self._site_page_notes[ev.site_id] = ev.pagination_message
        if ev.exhausted is True and ev.site_id not in self._site_exhausted:
            self._site_exhausted.add(ev.site_id)
            site = self.config.get_site(ev.site_id)
            self._newly_exhausted.append(ev.site_name or (site.display if site else ev.site_id))
        if not ev.pagination_supported:
            self._site_unavailable[ev.site_id] = ev.pagination_message or '暂不支持续页'

    def _show_page_status(self):
        if self.agent_mode:
            return
        selected = self._selected_search_sites()
        ended = [s.display for s in selected if s.id in self._site_exhausted]
        unavailable = [s.display + '：' + self._site_unavailable[s.id] for s in selected if s.id in self._site_unavailable]
        parts = []
        if ended:
            parts.append('、'.join(ended) + '：当前关键词下没有更多资源了')
        parts.extend(unavailable)
        parts.extend(s.display + '：' + self._site_page_notes[s.id] for s in selected
                     if s.id in self._site_page_notes and s.id not in self._site_exhausted)
        all_ended = bool(selected) and all(s.id in self._site_exhausted for s in selected)
        self._page_notice('；'.join(parts), '所选来源的当前检索范围已结束；可查看各来源详情。' if all_ended else None)
        if self._newly_exhausted:
            self._toast_show('、'.join(self._newly_exhausted) + '：没有更多资源了', 4500)
            self._newly_exhausted.clear()
        if selected and all(s.id in self._site_exhausted or s.id in self._site_unavailable for s in selected):
            self._no_more = True
            self._status('；'.join(parts))

    def _append_results(self, results: List[SearchResult]) -> int:
        existing = {normalize_url(r.url) or r.url: r for r in self._all_results}
        seen = set(existing)
        fresh: List[SearchResult] = []
        for r in results:
            key = normalize_url(r.url) or r.url
            if key in seen:
                old = existing.get(key)
                if old is not None:
                    ids = set(old.extra.get('discovery_sources', [])) | {old.source_id, r.source_id}
                    old.extra['discovery_sources'] = sorted(ids)
                    if r.duration and not old.duration:
                        self._on_duration(old, r.duration)
                continue
            seen.add(key)
            fresh.append(r)
        if not fresh:
            self._apply_view()
            return 0
        self._all_results.extend(fresh)
        pos = self.scroll.verticalScrollBar().value()
        self._apply_view()
        self.scroll.verticalScrollBar().setValue(pos)
        return len(fresh)

    def _request_thumb(self, card: ResultCard, referer: str) -> None:
        url = card.cover_url or 'video:' + card.result.url
        video_url = card.result.url
        path = self.thumbs.cached(url)
        if path:
            card.set_cover_from_file(path)
            return
        self._waiting.setdefault(url, []).append(card)
        self._wait_started.setdefault(url, time.time())
        def complete(_url, path):
            self.bridge.thumb.emit(url, path or '')
        def downloaded(_url, path):
            if not path and card.result.resource_type == 'image' and url != video_url and not card.result.extra.get('image_page'):
                self.thumbs.request(video_url, referer=referer, done=complete)
            elif path or card.result.resource_type != 'video':
                complete(_url, path)
            else:
                self.thumbs.request_video(video_url, done=complete)
        if card.cover_url:
            self.thumbs.request(url, referer=referer, done=downloaded)
        else:
            if card.result.resource_type == 'video':
                self.thumbs.request_video(video_url, done=complete)
            else:
                complete(url, '')

    def _sweep_thumbs(self) -> None:
        """兜底：等太久还没回来的封面，直接用占位图，别一直显示「加载中」。"""
        now = time.time()
        stale = [u for u, t in self._wait_started.items() if now - t > 25.0]
        for url in stale:
            self._wait_started.pop(url, None)
            for card in self._waiting.get(url, []):
                try:
                    card.set_cover(None)
                except RuntimeError:
                    continue

    def _on_thumb(self, url: str, path: str) -> None:
        self._wait_started.pop(url, None)
        cards = self._waiting.pop(url, [])
        for card in cards:
            try:
                if path and self.settings.show_thumbnails:
                    card.set_cover_from_file(path)
                else:
                    card.set_cover(None)
                    card.cover.setToolTip('封面暂不可用：地址缺失、站点限制或图片格式不支持')
                    attempts = getattr(card, '_thumb_attempts', 0) + 1
                    card._thumb_attempts = attempts
                    if attempts < 2:
                        card._thumb_requested = False
                        card._thumb_retry_at = time.monotonic() + 60
            except RuntimeError:          # 卡片已经被清掉了
                continue

    def _clear_results(self, keep_empty: bool = False) -> None:
        self._cancel_intent()
        self.durations.reset()
        self._close_login_dialogs()
        self._cancel.set()
        self._restart_after_stop = False
        # 清空 = 停止这一轮搜索 + 把界面恢复到「还没搜过」的状态
        if self._searching or self._paging:
            self._cancel.set()
            self._searching = False
            self._gen += 1                      # 让还在路上的事件全部失效
        self._auto_more_timer.stop()
        self._agent_busy_done()
        if self._agent_server:
            self._agent_server.clear_tasks()
        self._agent_task_id = ""
        self._agent_more_task = ""
        self._agent_round_added = 0
        self._agent_inbox.clear()
        self._agent_budget_used = self._agent_accounted = 0
        self._agent_batch_start = self._agent_empty_rounds = 0
        self._agent_search_limited = False
        self._agent_fill_pending = False
        self._agent_seen_urls.clear()
        self._reset_agent_scroll()
        self._agent_grant_end = 1000 if self.settings.agent_search_mode == 'unlimited' else 100
        self._query = ""
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._waiting.clear()
        self._wait_started.clear()
        self._results_all = {}
        self._all_results = []
        self._site_pages = {}
        self._site_exhausted = set()
        self._site_unavailable = {}
        self._site_page_notes = {}
        self._newly_exhausted = []
        self._page_notice('')
        self._paging_pages = {}
        self._source_selection = None
        self._source_scroll_hold = False
        self._sync_source_filters()
        self.filter_empty_hint.hide()
        self._page = 1
        self._no_more = False
        self._paging = False
        self._paging_new = 0
        self._referers = {}
        self._sync_more_indicator()
        while self.flow.count():
            item = self.flow.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.setParent(None)
        self.stack.setCurrentIndex(0)
        self.result_count.setText("就绪")
        self.result_host.setMinimumHeight(0)
        self.search_btn.setVisible(True)
        self.search_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.stop_btn.setEnabled(True)
        self.search_edit.setEnabled(True)
        self.progress.setVisible(False)
        self.progress.setValue(0)
        self._done_sites = 0
        self._ok_sites = 0
        self._total_sites = 0
        if not keep_empty:
            self.empty.set_text(*EMPTY_DEFAULT)
            self._status("就绪")

    # ================================================================== #
    # 交互
    # ================================================================== #
    def _copy_link(self, url: str) -> None:
        QGuiApplication.clipboard().setText(url)
        if looks_redirect(url):
            # 百度/必应这类跳转链接，先复制，再解析成真实地址（不阻塞界面）
            self._toast_show("已复制链接，正在解析真实地址…")
            self._status("已复制，正在解析真实地址…")
            threading.Thread(target=self._resolve_link, args=(url,), daemon=True).start()
            return
        self._toast_show("已复制链接")
        self._status("已复制链接：%s" % url[:110])

    def _toast_show(self, text: str, ms: int = 2000) -> None:
        try:
            self._toast.show_toast(text, ms)
        except Exception:                    # noqa: BLE001
            pass

    def _resolve_link(self, url: str) -> None:
        try:
            final = self.http.resolve(url, timeout=8.0)
        except Exception:                 # noqa: BLE001
            final = url
        if final and final != url:
            self.bridge.resolved.emit(url, final)
        else:
            self.bridge.resolved.emit(url, "")

    def _on_resolved(self, raw: str, final: str) -> None:
        if not final or looks_redirect(final):
            self._status("已复制链接：%s" % raw[:110])
            return
        # 只有剪贴板里还是原始链接时才替换，避免覆盖用户新复制的内容
        if QGuiApplication.clipboard().text() != raw:
            return
        QGuiApplication.clipboard().setText(final)
        self._toast_show("已复制真实地址（自动解开了跳转）", 2600)
        self._status("已复制真实地址：%s" % final[:110])

    def _refresh_network(self, silent: bool = False) -> None:
        """重新探测代理 / VPN 状态（用户开关 VPN 后点这个；启动时也会自动跑一次）。"""
        self.net_btn.setEnabled(False)
        self.net_badge.setText("网络：检测中…")
        self.net_badge.setMinimumHeight(18)
        self._net_silent = bool(silent)
        if not silent:
            self._status("正在检测网络连接…")

        def work() -> None:
            try:
                info = probe_network(self.settings)
            except Exception as exc:              # noqa: BLE001
                self.bridge.net.emit("网络检测失败：%s" % exc, False)
                return
            proxy = (info.get("proxy") or "").replace("http://", "")
            source = str(info.get("proxy_source") or "none")
            system_proxy = str(info.get("system_proxy") or "unknown")
            if source == "manual":
                sys_txt = "手动代理：%s%s" % (proxy or "（空）", "" if info.get("proxy_ok") else "（未监听）")
            elif source == "system":
                sys_txt = "系统代理：开 → %s" % proxy
            elif source == "env":
                sys_txt = "环境变量代理：%s" % proxy
            elif system_proxy == "off":
                sys_txt = "系统代理：关（直连，和浏览器一致）"
            else:
                sys_txt = "直连（未检测到代理）"
            route = info.get("route") or ""
            route_txt = "代理" if route == "proxy" else ("直连" if route == "direct" else "")
            abroad_extra = ""
            if info.get("abroad"):
                abroad_extra = "（%s %.1fs）" % (route_txt, float(info.get("abroad_ms") or 0.0))
            text = "%s\n国内 %s · 境外 %s%s" % (
                sys_txt,
                "✅" if info.get("domestic") else "❌",
                "✅" if info.get("abroad") else "❌",
                abroad_extra,
            )
            if info.get("note"):
                text += "\n" + str(info.get("note"))
            self.bridge.net.emit(text, bool(info.get("domestic") and info.get("abroad")))

        threading.Thread(target=work, name="netprobe", daemon=True).start()

    def _on_net_probe(self, text: str, ok: bool) -> None:
        self.net_btn.setEnabled(True)
        self.net_badge.setText(text)
        self.net_badge.setMinimumHeight(max(18, self.net_badge.heightForWidth(SIDEBAR_MIN - 58)))
        self.net_badge.setProperty("ok", "true" if ok else "false")
        self.net_badge.style().unpolish(self.net_badge)
        self.net_badge.style().polish(self.net_badge)
        silent = getattr(self, "_net_silent", False)
        self._net_silent = False
        if silent:
            # 启动时自动检测：只在状态栏留一句话，不弹 toast 打扰
            self._status("网络检测完成：%s" % ("国内/境外都可用" if ok else "境外不可用"))
            return
        self._status("网络已就绪：国内和境外都能访问" if ok else "网络状态已刷新（境外不可用）")
        self._toast_show("网络已就绪" if ok else "网络已刷新：境外不可用", 1800)

    def _refresh_search(self) -> None:
        """按当前关键词和筛选条件重新搜一次（搜索中则先停掉，结束后自动重搜）。"""
        text = self.search_edit.text().strip()
        if not text:
            self._toast_show("先输入要搜的内容，再点刷新", 2000)
            self.search_edit.setFocus()
            return
        if self._searching:
            self._restart_after_stop = True
            self._stop_search()
            self._status("正在刷新：先停掉当前搜索，随后用「%s」重新搜" % text)
            return
        if self.agent_mode:
            self._clear_results(keep_empty=True)
        self._start_search()

    def _open_link(self, url: str) -> None:
        if not QUrl(url).isValid() or QUrl(url).scheme().lower() not in ("http", "https"):
            self._toast_show("链接格式无效，无法打开")
            return
        ok = QDesktopServices.openUrl(QUrl(url))
        if ok:
            self._toast_show("已在浏览器中打开", 1400)
        else:
            self._toast_show("未能打开默认浏览器，请检查系统设置；也可右键复制网址", 4000)

    # ================================================================== #
    # Agent 模式
    # ================================================================== #
    def _set_mode(self, mode: str) -> None:
        self._cancel_intent()
        self._restart_after_stop = False
        self._agent_resume_requested = False
        self._reset_agent_scroll()
        if self._searching or self._paging:
            self._cancel.set()
            self._gen += 1
            self._searching = False
            self._paging = False
            self.search_edit.setEnabled(True)
            self.search_btn.setVisible(True)
            self.stop_btn.setVisible(False)
            self.progress.setVisible(False)
        self.agent_mode = (mode == "agent")
        self.workspace_mode.setText("Agent 模式" if self.agent_mode else "直接搜索")
        self.direct_btn.setChecked(not self.agent_mode)
        self.agent_btn.setChecked(self.agent_mode)
        self.agent_bar.setVisible(self.agent_mode)
        if self.agent_mode:
            self.search_edit.setPlaceholderText(
                "用大白话说：找几个最近一周的 4K 风景延时，播放量高一点")
            self.search_btn.setText("AI 搜索")
            self.empty.set_text("把想找的，交给 Agent",
                                "用一句话描述你的需求。\n先复制上方接入指令给 Agent，连接后即可开始。")
            self._start_agent_server()
            self._agent_timer.start()
            self._status("Agent 模式：可以用大白话搜，也可以让 Agent 把结果推进来")
        else:
            self._agent_timer.stop()
            self._auto_more_timer.stop()
            if self._agent_server:
                self._agent_server.clear_tasks()
            self._agent_task_id = ""
            self._agent_busy_done()
            self._agent_more_task = ""
            self.search_edit.setPlaceholderText("描述你想找的资源，或输入关键词…")
            self.search_btn.setText("开始搜索")
            if not self._cards:
                self.empty.set_text(*EMPTY_DEFAULT)
            self._status("直接搜索模式")
        self._sync_agent_input()

    def _toggle_agent_server(self) -> None:
        self._agent_resume_requested = False
        if self._agent_server is not None and self._agent_server.running:
            self._auto_more_timer.stop()
            self._agent_busy_done()
            self._agent_task_id = ""
            self._agent_server.clear_tasks()
            self._agent_server.stop()
            self._agent_server = None
            self._status("Agent 收件口已关闭")
        else:
            self._start_agent_server()
        self._update_agent_bar()

    def _start_agent_server(self) -> None:
        if self._agent_server is not None and self._agent_server.running:
            self._update_agent_bar()
            return
        server = AgentServer(
            lambda results, query, meta=None: self.bridge.agent_results.emit(
                {"results": results, "query": query, "meta": meta or {}}),
            on_clear=lambda meta: self.bridge.agent_results.emit({"clear": True, "meta": meta}),
            on_status=lambda info: self.bridge.agent_status.emit(info),
            on_error=lambda info: self.bridge.agent_error.emit(info),
            config=self.config,
        )
        self._agent_server = server if server.start() else None
        if self._agent_server is None:
            self._status("Agent 收件口启动失败：端口都被占用了")
        self._update_agent_bar()

    def _toggle_agent_pause(self) -> None:
        server = self._agent_server
        if server is None or not server.running:
            self._toast_show("Agent 收件口还没开，先点「开启」")
            return
        if server.paused:
            server.resume()
            self._agent_resume_requested = True
            self.agent_pause_btn.setText("暂停")
            self.side_busy_title.setText('正在确认 Agent 是否仍在运行…')
            self._set_agent_detail('收到新的取任务请求后才继续搜索；若 Agent 已结束，请重新发送接入指令。')
            self.side_busy.setVisible(True)
            self._toast_show('正在等待 Agent 恢复轮询')
        else:
            self._agent_resume_requested = False
            server.pause()
            self._reset_agent_scroll()
            self._account_agent_budget()
            self.agent_pause_btn.setText("继续")
            self._auto_more_timer.stop()
            self._agent_busy_done()
            server.clear_tasks()
            self._agent_task_id = ""
            if hasattr(self, "side_busy_spinner"):
                self.side_busy_spinner.stop()
            self.side_busy_spinner.hide()
            self.side_busy_title.setText("已暂停 Agent 搜索")
            self._set_agent_detail("点「继续」接着搜；也可以直接换个词重新搜")
            self.side_busy.setVisible(True)
            self.search_btn.setEnabled(True)
            self._status("已暂停 Agent 搜索（Agent 取任务会收到 paused=true）")
            self._toast_show("已暂停：Agent 会停手，结果也不再入库")
        self._update_agent_bar()

    def _disconnect_agent(self):
        self._agent_resume_requested = False
        self._reset_agent_scroll()
        server = self._agent_server
        if not server or not server.running or not server.owner:
            return
        self._account_agent_budget()
        server.disconnect()
        self._auto_more_timer.stop()
        self._agent_busy_done()
        self._agent_task_id = self._agent_more_task = self._agent_finished_id = ''
        self._delivery_view_key = None
        self.side_busy.hide()
        self._update_agent_bar()
        self._status('Agent 已断开，本轮任务已撤销；重新接入请复制新指令')
        self._toast_show('已断开 Agent，现有结果已保留', 2800)

    def _agent_pending_view(self, info):
        """Delivery evidence, independent of retained session authorization."""
        title = ('搜索未开始 · Agent 尚未领取' if info['state'] == 'queued'
                 else '任务已送达 · 待 Agent 确认')
        poll = ('尚未收到取任务请求' if info['last_poll_age'] < 0 else
                '上次取任务请求在 %d 秒前' % int(info['last_poll_age']))
        detail = ('任务 %s · 已发送 %d 次 · %s。\n'
                  '收到领取确认后才开始搜索。若 Agent 已回复完成或待命，请点击“复制继续指令”，'
                  '发送给 Agent 继续执行；工具无法自行唤醒已经结束的对话。') % (
                      info['task_id'], info['attempts'], poll)
        return title, detail

    def _update_agent_bar(self) -> None:
        server = self._agent_server
        on = bool(server and server.running)
        self.agent_disconnect_btn.setEnabled(bool(on and server.owner))
        self.agent_pause_btn.setEnabled(bool(on and server.owner))
        ready = on and server.connected_agent().get('ready')
        self.agent_dot.setProperty("on", "true" if ready else "false")
        self.agent_dot.style().unpolish(self.agent_dot)
        self.agent_dot.style().polish(self.agent_dot)
        if on and server is not None:
            info = server.connected_agent()
            task_info = server.task_status()
            live = task_info['liveness']
            pending = server.session_ready() and task_info['state'] in ('queued', 'delivered') and not server.paused
            self.agent_copy_btn.setText('复制继续指令' if pending else '复制接入指令')
            if pending:
                state = self._agent_pending_view(task_info)[0]
            elif server.session_ready() and task_info['state'] == 'working' and not server.paused:
                age = max(0,int(info.get('age') or 0))
                state = ('%s 已接管 · 搜索子程序运行中' % server.owner if live['registered_searches'] else
                         '%s 已接管 · 正在执行' % server.owner if info.get('connected') else
                         '%s 接管保持 · %d 秒未收到新活动' % (server.owner,age))
            elif server.owner and not info.get('connected'):
                state = ('搜索已暂停 · 暂未收到 Agent 响应' if server.paused else
                         ('本批已完成 · 等待 Agent 轮询' if not self._budget_remaining() else '本批未满 · 等待 Agent 轮询')
                         if server.task_status()['state'] == 'completed' else
                         '等待 Agent 响应 · 连接状态待确认')
            elif server._resume_pending:
                state = '等待 Agent 恢复轮询 · 尚未确认继续搜索'
            elif server.paused:
                state = "收件口已开启 · 搜索已暂停，Agent 在线等待"
            elif info.get("ready"):
                age = max(0, int(info.get("age") or 0))
                who = str(info.get("name") or "Agent")
                state = "收件口已开启 · %s 已接管（%d 秒前有活动）" % (who, age)
            elif info.get('connected'):
                state = '%s 正在接管 · 等待握手确认和持续轮询' % info.get('name', 'Agent')
            elif server.disconnected:
                state = "Agent 已断开 · 复制新指令可重新接入"
            else:
                state = "收件口已开启 · 等待 Agent 接入"
            self.agent_state.setText(state)
            self.agent_endpoint.setText(server.endpoint())
            self.agent_toggle_btn.setText("关闭")
            self.agent_pause_btn.setText("继续" if server.paused else "暂停")
            decorate(self.agent_pause_btn, "play" if server.paused else "pause", theme.palette(self.settings.theme)["sub"])
            if server.access:
                plan = server.access.snapshot()
                summary = {'pending': '待实测 / 检测中', 'failed': '检测失败，可重新搜索后重试',
                           'ready': '无需外网检测' if not plan.get('targets') else '已实测通过'}
                routes = {'native': 'Agent 自带工具', 'direct': '直连', 'proxy': '当前代理', 'relay': '工具中转'}
                detail = '；'.join('%s：%s' % (url, routes.get(route, route))
                                  for url, route in plan.get('routes', {}).items())
                self.agent_network_state.setText('Agent 外网：' + summary.get(plan.get('state'), '首次需要时单独实测')
                                                + ('\n' + detail if detail else ''))
                self.agent_network_state.setToolTip(server.access.last_report)
            timings = server.timings()
            parts = []
            if timings['queue_ms'] is not None:
                parts.append('领取等待 %.1f 秒' % (timings['queue_ms'] / 1000))
            if timings['network_requests']:
                parts.append('工具网络累计 %.1f 秒 / %d 次' % (timings['network_ms'] / 1000, timings['network_requests']))
            if timings['work_ms']:
                parts.append('工具外等待/执行 %.1f 秒' % (timings['outside_tool_ms']/1000))
            if timings['first_result_ms'] is not None:
                parts.append('首条回推 %.1f 秒' % (timings['first_result_ms'] / 1000))
            self.agent_timing_state.setText('耗时：' + (' · '.join(parts) if parts else '等待任务'))
        else:
            self.agent_copy_btn.setText('复制接入指令')
            self.agent_state.setText("收件口未开启")
            self.agent_endpoint.setText("（点右边「开启」，Agent 就能接活）")
            self.agent_toggle_btn.setText("开启")
            self.agent_pause_btn.setText("暂停")
        self._sync_agent_input()
        self._update_agent_budget()

    def _sync_agent_input(self):
        self._sync_more_indicator()
        server = self._agent_server
        gated = bool(self.agent_mode and (not server or not server.running or
                     not server.session_ready()))
        self.search_edit.setEnabled(not gated and not self._searching)
        self.search_btn.setEnabled(not gated and not self._ai_pending)
        self.refresh_btn.setEnabled(not gated)
        if gated:
            self.search_edit.setPlaceholderText('先点击「复制接入指令」，完成 Agent 接管后即可输入…')
            if server and server.disconnected and not self._cards:
                self.empty.set_text('Agent 已断开', '本轮任务已撤销，收件口仍然开启。\n复制新接入指令后，可连接同一个或其他 Agent。')
            elif server and server.owner and not server.connected() and not self._cards:
                self.empty.set_text('等待 Agent 响应', '暂未收到新的活动，尚不能确认 Agent 是否退出。\n若对方已经结束运行，请重新发送接入指令。')
            elif server and server._resume_pending and not self._cards:
                self.empty.set_text('等待 Agent 恢复轮询', '收到新的取任务请求后才继续搜索。\n如果 Agent 已结束，请重新发送接入指令。')
        elif self.agent_mode:
            self.search_edit.setPlaceholderText('描述你想找的资源；首次访问外网会先验证 Agent 网络…')
            if server and server.access and not self._cards:
                task_info = server.task_status()
                state = task_info['state']
                if server.paused:
                    title, hint = '搜索已暂停', '点击“继续”后等待 Agent 恢复执行。'
                elif state in ('queued', 'delivered'):
                    title, hint = self._agent_pending_view(task_info)
                elif not server.connected():
                    title, hint = '等待 Agent 响应', '可以输入关键词排队，Agent 恢复轮询后领取。\n若对方已结束运行，请重新发送接入指令。'
                elif not self._agent_busy:
                    title, hint = 'Agent 已接管', '在下方输入你想找的内容。\n首次访问外网时，会先验证 Agent 的网络通道。'
                elif server.access.plan.get('state') == 'pending':
                    title, hint = '正在验证 Agent 网络', '检测目标连接，并在需要时尝试代理或工具中转。\n通过后自动开始搜索。'
                else:
                    title, hint = '正在搜寻资源', 'Agent 会边搜边推，结果将在这里出现。'
                self.empty.set_text(title, hint)

    def _show_connect_dialog(self):
        server = self._agent_server
        if not server or not server.access or server.session_ready():
            return
        if self._connect_dialog is not None:
            self._connect_dialog.raise_()
            return
        dlg = AgentConnectDialog(server, self)
        self._connect_dialog = dlg
        def finished(result):
            self._connect_dialog = None
            if result:
                self._update_agent_bar()
                self.search_edit.setFocus()
                self._toast_show('Agent 已完成接管，现在可以输入搜索内容')
            else:
                self._set_mode('direct')
            dlg.deleteLater()
        dlg.finished.connect(finished)
        dlg.open()

    def _set_agent_detail(self, text):
        self.side_busy_sub.setText(text)
        width = max(140, self.side_busy.width() - 24 - (34 if not self.side_busy_spinner.isHidden() else 0))
        self.side_busy_sub.setMinimumHeight(max(16, self.side_busy_sub.heightForWidth(width)))

    def _sync_agent_delivery(self):
        self._sync_more_indicator()
        server = self._agent_server
        if not self.agent_mode or not self._agent_busy or not server:
            return
        info = server.task_status()
        if info["task_id"] != self._agent_task_id or server.paused:
            return
        if not server.connected() and not (server.session_ready() and info['state'] in ('queued', 'delivered', 'working')):
            self._agent_busy_timer.stop()
            self.side_busy_spinner.stop()
            self.side_busy_spinner.hide()
            self.side_busy_title.setText('等待 Agent 响应')
            self._set_agent_detail('任务已保留，收到领取确认或新的进度后更新。若 Agent 已结束，请重新发送接入指令。')
            self._delivery_view_key = None
            return
        state = info["state"]
        if state == 'working' and server.access and server.access.pending():
            started = server.access.plan.get('started')
            self._delivery_view_key = None
            self.side_busy_title.setText('正在检测 Agent 外网连接…')
            self._set_agent_detail('正在等待网络检测报告；任务保留，不会因检测耗时较长而撤销。' if started and time.monotonic()-started>=300
                                   else '依次验证 Agent 直连、当前代理和工具中转；通过后自动开始搜索。')
            self.side_busy_spinner.show()
            self.side_busy_spinner.start()
            if not self._agent_busy_timer.isActive():
                self._agent_busy_timer.start()
            return
        delayed = info["age"] >= 30
        live = info['liveness']
        evidence = 'runner' if live['registered_searches'] else 'recent' if server.connected() else 'quiet'
        key = (info["task_id"], state, delayed if state != "working" else evidence)
        changed = key != getattr(self, "_delivery_view_key", None)
        self._delivery_view_key = key
        if state in ("queued", "delivered"):
            self._agent_busy_timer.stop()
            self.side_busy_spinner.stop()
            self.side_busy_spinner.hide()
            title, detail = self._agent_pending_view(info)
            self.side_busy_title.setText(title)
            self._set_agent_detail(detail)
            if changed:
                self._status("%s（%s）" % (self.side_busy_title.text(), info["task_id"]))
        elif state == "working" and changed:
            self.side_busy_title.setText('搜索子程序运行中…' if evidence=='runner' else
                                         'Agent 接管保持，等待执行进度…' if evidence=='quiet' else
                                         "%s 已确认，正在搜索…" % self._agent_who())
            self._set_agent_detail('已检测到运行助手及其搜索子程序仍活跃，每2秒检查；任务和结果保留。' if evidence=='runner' else
                                   '暂未收到新的活动，不能据此判断Agent已退出；任务保持有效，可继续等待、换词或断开。' if evidence=='quiet' else
                                   "任务 %s 已被领取，等待结果或进度" % info["task_id"])
            self.side_busy_spinner.show()
            self.side_busy_spinner.start()
            self._agent_busy_timer.start()

    def _copy_agent_help(self) -> None:
        server = self._agent_server
        if server is None or not server.running:
            self._toast_show("先点「开启」，才有接入地址")
            return
        task = server.current_task
        QGuiApplication.clipboard().setText(server.task_text(task))
        if task and server.session_ready():
            self._toast_show('继续指令已复制，请发送给 Agent 恢复领取当前任务；无需重新接管')
        elif task:
            self._toast_show("接入指令 + 当前任务已复制，粘给你的 Agent 就行")
        else:
            self._toast_show("接入指令已复制，粘给你的 Agent 就行")
        self._status("已复制 Agent 接入指令（含任务接口和格式）")
        self._show_connect_dialog()

    def _agent_search(self, query: str) -> None:
        """Agent 模式下点「搜索」：接通了就把活派给 Agent，没接通就弹窗说明。

        刻意不在没接入时默默用关键词搜一份——那样用户会以为看到的是 AI 搜索
        的结果。这里把「接入指令 + 这条任务」放进剪贴板，让用户手动粘给 Agent。
        """
        server = self._agent_server
        if server and server.access and not server.session_ready():
            self._copy_agent_help()
            return
        connected = server is not None and server.running and server.session_ready()
        if connected:
            self._handoff_to_agent(query)
            return

        filters = {
            'resource_type': self.settings.resource_type,
            "sort": self.sort_combo.currentData() or "relevance",
            "time": {86400: "24h", 604800: "7d", 2592000: "30d", 31536000: "1y"}.get(self.time_combo.currentData(), "any"),
            "min_views": int(self.views_combo.currentData() or 0),
        }
        if server is None or not server.running:
            self._toast_show("Agent 收件口没开着，先点提示条上的「开启」", 2600)
            self._status("Agent 收件口未开启，无法派任务")
            return

        self._clear_results(keep_empty=True)
        server.resume()
        self._query = query
        self.config.add_history(query)
        self._save_config()
        task = server.push_task(query, filters)
        self._track_agent_task(task)
        self._agent_busy_show("等待 Agent 接入…", "任务已就绪，请复制接入指令")
        text = server.task_text(task)
        QGuiApplication.clipboard().setText(text)
        self._update_agent_bar()

        dlg = AgentWaitDialog(
            text,
            check_connected=server.connected,
            task_id=str(task.get("id") or ""),
            parent=self,
        )
        dlg.exec()

        if dlg.choice == AgentWaitDialog.HANDOFF:
            self._sync_agent_delivery()
            self._toast_show("Agent 已接入；领取状态以左侧任务提示为准", 2200)
        elif dlg.choice == AgentWaitDialog.PASTE:
            self._status("任务 %s 已就绪，等 Agent 取走；接入指令已复制到剪贴板"
                         % task.get("id"))
            self._toast_show("把剪贴板内容粘给 Agent，它就能接手这条任务", 3000)
        elif dlg.choice == AgentWaitDialog.LOCAL:
            server.clear_tasks()
            self._agent_task_id = ""
            self._agent_busy_done()
            self._update_agent_bar()
            self._toast_show("按你的选择：用本地关键词搜索（不是 Agent 结果）", 2600)
            self._ai_search()
        else:                                   # 取消
            server.clear_tasks()
            self._agent_task_id = ""
            self._agent_busy_done()
            self._update_agent_bar()
            self._status("已取消：没有搜索，也没有把任务留给 Agent")

    def _handoff_to_agent(self, query: str) -> None:
        server = self._agent_server
        if server and server.access and not server.session_ready():
            self._copy_agent_help()
            return
        if server is None or not server.running:
            self._ai_search()
            return
        new_query = (query or "").strip()
        self._clear_results(keep_empty=True)
        server.resume()
        self.config.add_history(new_query)
        self._save_config()
        filters = {
            'resource_type': self.settings.resource_type,
            "sort": self.sort_combo.currentData() or "relevance",
            "time": {86400: "24h", 604800: "7d", 2592000: "30d", 31536000: "1y"}.get(self.time_combo.currentData(), "any"),
            "min_views": int(self.views_combo.currentData() or 0),
        }
        task = server.push_task(new_query, filters, want=self._budget_remaining())
        self._track_agent_task(task)
        self._query = new_query                    # 滚到底要更多时要用到它
        self._page = 1
        self._no_more = False
        # 同时把「接入指令 + 这条任务」放进剪贴板：Agent 要是没在轮询，
        # 用户直接粘过去也能立刻开工。
        QGuiApplication.clipboard().setText(server.task_text(task))
        info = server.connected_agent()
        who = str(info.get("name") or "Agent")
        self._agent_busy_show("搜索未开始 · Agent 尚未领取",
                              "任务 %s 已排队，本批目标 %d 条；等待 Agent 领取确认，可随时换词搜索"
                              % (task.get("id"), task['want']))
        self._status("任务已入队，等待 Agent 领取（%s）：%s" % (task.get("id"), query))
        self._toast_show("任务已排队；Agent 持续轮询本窗口地址才能收到", 2800)
        self._update_agent_bar()


    def _track_agent_task(self, task):
        self._agent_fill_pending = False
        self._agent_resume_requested = False
        self._auto_more_timer.stop()
        self._agent_task_id = str(task["id"])
        self._agent_round_added = 0
        self._agent_accounted = 0
        self._reset_agent_scroll()
        self._agent_finished_id = ""
        self._delivery_view_key = None

    def _valid_agent_message(self, info, query=""):
        server = self._agent_server
        task_id = str(info.get("task_id") or "")
        return bool(self.agent_mode and server and not server.paused
                    and task_id and task_id == self._agent_task_id
                    and task_id != self._agent_finished_id
                    and server.accepts(task_id, query))

    def _on_agent_status(self, info) -> None:
        if not isinstance(info, dict):
            return
        stage = str(info.get("stage") or "").strip()
        if stage == "takeover":
            server = self._agent_server
            if (not self.agent_mode or not server or info.get("epoch") != server.epoch
                    or info.get("agent") != server.owner):
                return  # A queued takeover notification must not cancel a newer search.
            task = server.current_task
            if task and server.task_status()["state"] in ("queued", "delivered", "working"):
                if task["id"] != self._agent_task_id:
                    self._track_agent_task(task)
                self._query = str(task["query"])
                self._agent_more_task = task["id"] if task["kind"] == "more" else ""
                self._agent_busy_show("等待新 Agent 领取任务")
            else:
                self._agent_busy_done()
            self._toast_show("%s 开始接入，当前任务已保留" % server.owner, 2800)
            self._update_agent_bar()
            self._show_connect_dialog()
            return
        if info.get("task_id") and not self._valid_agent_message(info):
            return
        active = stage not in ("queued", "delivered")
        if info.get("task_id") and self._agent_busy and active:
            self._agent_server.acknowledge(str(info["task_id"]), activity=stage != "acknowledged")
            self._agent_busy_timer.start()
        msg = str(info.get("message") or "").strip()
        self._sync_agent_delivery()
        if msg:
            self.agent_state.setText("Agent：%s" % msg)
            self._status("Agent：%s" % msg)
            if self._agent_busy:
                self._set_agent_detail(msg)
        elif stage:
            self.agent_state.setText("Agent：%s" % stage)
        self._update_agent_bar()

    def _on_agent_error(self, info) -> None:
        """Agent 说「搜不了」：必须让用户看见，并且结束「搜索中」。"""
        if not isinstance(info, dict):
            return
        if not self._valid_agent_message(info):
            return
        self._reset_agent_scroll()
        self._agent_server.finish_task(self._agent_task_id, failed=True)
        self._agent_finished_id = self._agent_task_id
        self._auto_more_timer.stop()
        who = str(info.get("agent") or "Agent")
        reason = str(info.get("error") or "没有说明原因")
        self._agent_busy_done()
        self._status("Agent 搜索失败：%s" % reason)
        self.agent_state.setText("Agent 失败：%s" % reason)
        self._warn("Agent 搜索失败",
                   "%s 报告：\n\n%s\n\n可以换个说法/换一个 Agent 再试。"
                   % (who, reason))

    def _warn(self, title: str, text: str) -> None:
        """统一的警告弹窗（测试里可以替换掉，避免真的弹模态框）。"""
        QMessageBox.warning(self, title, text)

    def _cancel_intent(self) -> None:
        self._ai_serial += 1
        self._ai_pending = False
        if not self._searching:
            self.search_btn.setEnabled(True)

    def _ai_search(self) -> None:
        text = self.search_edit.text().strip()
        if not text:
            self._toast_show("先说一句你想找什么")
            return
        self._ai_pending = True
        self._ai_serial += 1
        serial = self._ai_serial
        self.search_btn.setEnabled(False)
        self._status("正在理解这句话…")

        def work() -> None:
            try:
                intent = parse_intent(text)
            except Exception as exc:              # noqa: BLE001
                intent = {"keywords": [text], "why": "解析失败：%s" % exc}
            try:
                self.bridge.intent.emit(serial, intent, text)
            except RuntimeError:  # The profile window may already be destroyed.
                pass

        threading.Thread(target=work, name="agent-intent", daemon=True).start()

    def _on_intent(self, serial: int, intent, text: str) -> None:
        if self._closing or serial != self._ai_serial or not self._ai_pending:
            return
        self._ai_pending = False
        self.search_btn.setEnabled(True)
        if text != self.search_edit.text().strip():
            self._status('输入已更改，请按新内容重新搜索')
            return
        kws = [k for k in (intent.get("keywords") or []) if k] or [text]
        span_idx = {"24h": 1, "7d": 2, "30d": 3, "1y": 4}.get(str(intent.get("time")), 0)
        self.time_combo.setCurrentIndex(span_idx)
        min_views = int(intent.get("min_views") or 0) if self.settings.resource_type == 'video' else 0
        idx = 0
        for i, threshold in ((1, 10000), (2, 100000), (3, 1000000)):
            if min_views >= threshold:
                idx = i
        self.views_combo.setCurrentIndex(idx)
        query = " ".join(kws)
        self.search_edit.setText(query)
        self._status("AI 理解：%s（%s）" % (query, intent.get("why") or ""))
        # 这里要直接按解析结果搜，不能再走一遍 AI 解析
        self._ai_pending = True
        try:
            self._start_search()
        finally:
            self._ai_pending = False

    def _on_agent_results(self, payload) -> None:
        if isinstance(payload, dict):
            if payload.get("clear"):
                if self._valid_agent_message(payload.get("meta") or {}):
                    self._clear_results()
                return
            results = payload.get("results") or []
            query = str(payload.get("query") or "")
            meta = payload.get("meta") or {}
        else:                                   # 兼容旧的两参数形式
            results, query = payload, ""
            meta = {}
        syncing = bool(meta.get('budget_sync'))
        if not self._valid_agent_message(meta, query) and not ((syncing or meta.get('budget_changed')) and self.agent_mode and self._agent_server
                and meta.get('task_id') == self._agent_task_id and self._agent_server.accepts(self._agent_task_id)):
            return
        task_id = str(meta.get("task_id") or "")
        if not syncing:
            self._agent_server.acknowledge(task_id, activity=True)
            self._sync_agent_delivery()
            self._agent_busy_timer.start()
        done_raw = meta.get("done")
        done = True if done_raw is None else bool(done_raw)
        was_more = bool(self._agent_more_task) and task_id == self._agent_more_task
        if query and not self._query:
            self._query = query

        incoming = [r for r in (results or []) if r is not None and r.url]
        task = self._agent_server.current_task or {}
        # Server receipts were already capped atomically. A budget change must
        # not discard accepted rows still waiting in Qt's event queue.
        room = len(incoming) if 'received' in meta else min(self._budget_remaining(), max(0, int(task.get('want', 100)) - self._agent_accounted))
        fresh = []
        for r in incoming:
            key = normalize_url(r.url) or r.url
            if key not in self._agent_seen_urls and len(fresh) < room:
                self._agent_seen_urls.add(key)
                fresh.append(r)
        self._account_agent_budget(meta.get('received', self._agent_accounted + len(fresh)))
        done = not syncing and (done or self._budget_remaining() <= 0 or self._agent_accounted >= int(task.get('want', 100)))
        # 黑名单对所有模式都生效：Agent 推来的也要过一遍
        fresh, blocked = self._split_blacklisted(fresh)
        seen = {normalize_url(r.url) or r.url for r in self._all_results}
        unique = []
        for r in fresh:
            key = normalize_url(r.url) or r.url
            if key not in seen:
                unique.append(r)
                seen.add(key)
        fresh = unique
        self._agent_round_added += len(fresh)

        if fresh:
            self._agent_inbox.extend(fresh)
            self._results_all.setdefault("agent", []).extend(fresh)
            self._append_results(fresh)
            extra = "，另拦下 %d 条黑名单" % blocked if blocked else ""
            self._status("Agent 送来 %d 条新资源%s%s"
                         % (len(fresh), extra, ("：「%s」" % query) if query else ""))
            if done:
                self._toast_show("Agent 送来 %d 条新资源%s" % (len(fresh), extra))
        elif blocked:
            self._toast_show("Agent 推来的 %d 条都在黑名单里，已拦下" % blocked)
            self._status("Agent 推来的 %d 条命中黑名单，已拦下" % blocked)

        if syncing:
            return
        if not done:
            # 边搜边推：这一批只是过程，继续等它
            self._agent_busy = True
            completion = meta.get('completion') or {}
            if completion.get('pending'):
                message = completion['message']
                self._page_notice(message, summary=message[:160])
                self._set_agent_detail(message)
                self._status(message)
                self._update_agent_budget()
                return
            if getattr(self, '_search_details_text', '').startswith('本批尚未完成：'):
                self._page_notice('')
            total = len(self._all_results)
            self.side_busy_title.setText("正在让 %s 搜索…" % self._agent_who())
            self._set_agent_detail(
                "已收到 %d 条，Agent 还在继续搜（每搜到一条就会立刻出现在下面）"
                % total)
            self._status("已收到 %d 条，Agent 还在继续搜…" % total)
            self._update_agent_budget()
            return

        self._agent_finished_id = task_id
        self._agent_server.finish_task(task_id)
        self._agent_busy_done()
        if was_more:
            self._agent_more_task = ""
        remaining = self._budget_remaining()
        self._agent_empty_rounds = 0 if self._agent_accounted else self._agent_empty_rounds + 1
        if remaining <= 0:
            self._page_notice('')
            self._status('已达到本关键词 1000 条上限，Agent 已停止搜索' if self.settings.agent_search_mode == 'unlimited'
                         else '本批已满 100 条；向下滚动到底部再搜索下一批 100 条')
            self._queue_agent_scroll_check()
        elif meta.get('search_limited') is True:
            # A bounded search is not proof of website exhaustion. No automatic
            # refill or pending wheel may restart the just-stopped request loop.
            self._no_more = False
            self._agent_search_limited = True
            message = '本轮检索已停止：连续多页无新候选或已达检索上限。已收到 %d 条，还差 %d 条；不代表网站没有更多资源。向下滚动可重试，或更换关键词/来源。' % (
                self._agent_budget_used - self._agent_batch_start, remaining)
            self._page_notice(message)
            self._status(message)
            self._reset_agent_scroll()
        elif meta.get('exhausted') is True:
            self._no_more = True
            message = '资源不足：已收到 %d 条，距本批目标还差 %d 条。%s' % (
                self._agent_budget_used - self._agent_batch_start, remaining, meta.get('message') or 'Agent 报告没有更多资源')
            self._page_notice(message, summary='本批资源不足：已收到 %d 条，还差 %d 条；已核验本轮检索情况，查看详情。' % (
                self._agent_budget_used-self._agent_batch_start, remaining))
            self._status(message)
            self._reset_agent_scroll()
        elif self._agent_empty_rounds >= 3:
            message = '连续 3 轮无新增，已暂停补足；还差 %d 条。向下滚动可重试，或更换关键词/来源。' % remaining
            self._page_notice(message)
            self._status(message)
            self._reset_agent_scroll()
        else:
            self._agent_fill_pending = True
            self._status('本批尚差 %d 条，将自动继续搜索补足' % remaining)
            self._auto_more_timer.start()
            self._queue_agent_scroll_check()
        self._update_agent_budget()
        self._sync_more_indicator()

    def _agent_who(self) -> str:
        server = self._agent_server
        if server is None:
            return "Agent"
        try:
            info = server.connected_agent() or {}
            return str(info.get("name") or "Agent")
        except Exception:                 # noqa: BLE001
            return "Agent"

    def _agent_busy_show(self, title: str, sub: str = "") -> None:
        self._agent_search_limited = False
        self._agent_busy = True
        self.search_btn.setEnabled(True)
        self.side_busy_title.setText(title)
        self._set_agent_detail(sub)
        self.side_busy.setVisible(True)
        self.side_busy_spinner.show()
        self.side_busy_spinner.start()
        self._agent_busy_timer.start()
        self._sync_agent_delivery()

    def _agent_busy_done(self) -> None:
        self._agent_fill_pending = False
        self._auto_more_timer.stop()
        self._agent_busy = False
        self._agent_busy_timer.stop()
        self.side_busy_spinner.stop()
        self.side_busy.setVisible(False)
        if not self._searching and not self._ai_pending:
            self.search_btn.setEnabled(True)
        self._sync_agent_input()

    def _agent_busy_timeout(self) -> None:
        # This is a periodic observation, not a lease expiry. A slow external
        # command, model call or login cannot prove that the Agent disconnected.
        if not self._agent_busy:
            return
        server = self._agent_server
        if server and server.accepts(self._agent_task_id) and not server.paused:
            self._delivery_view_key = None
            self._sync_agent_delivery()
            self._agent_busy_timer.start()


    def _split_blacklisted(self, results):
        """把命黑名单的结果挑出去，返回 (保留的, 被拦下的条数)。"""
        keep = []
        blocked = 0
        for r in results or []:
            text = " ".join(x for x in (r.title, r.desc, r.url, r.author) if x)
            if self.config.match_blacklist(text):
                blocked += 1
                continue
            keep.append(r)
        return keep, blocked

    def _tick_agent_bar(self) -> None:
        for key, dialog in list(self._login_dialogs.items()):
            entry = dict(scope=key[0], source_id=key[1])
            if not self._login_current(entry):
                dialog.finish()
                self._login_dialogs.pop(key, None)
        if not self.agent_mode:
            return
        server = self._agent_server
        retained_work = bool(server and server.session_ready() and not server.paused and
                             server.task_status()['state']=='working')
        lost = bool(server and server.running and server.owner and not server.connected() and not retained_work)
        if lost and not getattr(self, '_agent_loss_notified', False):
            self._agent_loss_notified = True
            self._auto_more_timer.stop()
            self._agent_busy_timer.stop()
            self._delivery_view_key = None
            self.side_busy_spinner.stop()
            self.side_busy_spinner.hide()
            self.side_busy_title.setText('等待 Agent 响应')
            self._set_agent_detail('暂未收到新的活动，不能据此判断 Agent 已退出。任务与结果已保留；若对方已结束，请重新发送接入指令。')
            self.side_busy.show()
            self._status('等待 Agent 响应，任务与结果已保留')
        elif not lost:
            if getattr(self, '_agent_loss_notified', False) and self._agent_fill_pending:
                self._auto_more_timer.start()
            if getattr(self, '_agent_loss_notified', False) and not self._agent_busy:
                self.side_busy.hide()
            self._agent_loss_notified = False
        if (server and getattr(self, '_agent_resume_requested', False)
                and not server.paused and server.connected_agent().get('ready')):
            self._agent_resume_requested = False
            self.side_busy.hide()
            if self._query and not self._no_more and not self._agent_busy:
                self._ask_agent_more()
        self._update_agent_bar()
        self._sync_agent_delivery()

    def _clear_cache(self) -> None:
        n = self.thumbs.clear()
        self._status("已清空封面缓存（%d 个文件）" % n)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self.http, self.thumbs, self)
        if not dlg.exec():
            return
        old_theme = self.settings.theme
        old_thumbnails = self.settings.show_thumbnails
        dlg.apply_to(self.settings)
        self._save_config()
        if self.settings.theme != old_theme:
            self._apply_theme()
        self._update_net_badge()
        self.thumbs.reconfigure()
        if old_thumbnails != self.settings.show_thumbnails:
            for card in self._cards:
                card._thumb_requested = False
                if not self.settings.show_thumbnails:
                    card.set_cover(None)
            self._pump_thumbs()
        self.thumbs.evict(self.settings.cache_mb)
        self.cover_notice.setVisible(not self.settings.show_thumbnails)
        self._status("设置已保存")

    def _enable_thumbnails(self):
        self.settings.show_thumbnails = True
        self.cover_notice.hide()
        self._save_config()
        for card in self._cards:
            card._thumb_requested = False
            card._thumb_retry_at = 0
            card._thumb_attempts = 0
        self._pump_thumbs()
        self._status('已开启封面，正在加载可见资源的预览')

    # ================================================================== #
    # 杂项
    # ================================================================== #
    def _status(self, text: str) -> None:
        self.status_label.setText(text)
        self._log(text)

    def _log(self, text: str) -> None:
        if not text:
            return
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(text.replace("\n", " ") + "\n")
        except Exception:
            pass

    def _save_config(self) -> None:
        self.config.settings.window_geometry = self._geometry_b64()
        save_config(self.config)

    def _geometry_b64(self) -> str:
        try:
            return base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        except Exception:
            return ""

    def _restore_geometry(self) -> None:
        raw = (self.settings.window_geometry or "").strip()
        if not raw:
            return
        try:
            from PySide6.QtCore import QByteArray

            self.restoreGeometry(QByteArray(base64.b64decode(raw)))
        except Exception:
            pass

    def _startup_checks(self) -> None:
        if self._closing:
            return
        self._update_net_badge()
        self.search_edit.setFocus()
        # 启动就自动测一次网络，省得用户每次手动点「刷新网络」
        self._refresh_network(silent=True)
        import sys
        if getattr(sys,'frozen',False) and not os.environ.get('RESOURCE_FINDER_HOME'):
            self._check_updates(True)

    def _check_updates(self, automatic=False):
        if self._closing:return
        from ..updates import UpdateSettings
        if automatic and not UpdateSettings().automatic:return
        if self._update_dialog is None:
            from .updates import UpdateDialog
            self._update_dialog=UpdateDialog(self,automatic)
        dialog=self._update_dialog
        if not automatic:
            dialog.automatic=False
            dialog.show();dialog.raise_()
        dialog.check()

    # ================================================================== #
    def resizeEvent(self, event):                  # noqa: N802
        super().resizeEvent(event)
        toast = getattr(self, "_toast", None)
        if toast is not None and toast.isVisible():
            toast._reposition()
        if getattr(self, "_cards", None):
            self._relayout_results()
            QTimer.singleShot(0, self._pump_thumbs)

    def _logout(self):
        self._logout_requested = True
        if not self.close():
            self._logout_requested = False

    def closeEvent(self, event):                   # noqa: N802
        if self.config._profile_name:
            self.config.settings.window_geometry = self._geometry_b64()
            if not save_config(self.config):
                QMessageBox.warning(self, '存档未保存', '无法保存当前配置，请检查存档目录的写入权限后重试。')
                event.ignore()
                return
        self._closing = True
        if self._update_dialog is not None:self._update_dialog.cancel.set()
        # Revoke the Agent before any potentially slow/failing subsystem cleanup.
        server, self._agent_server = self._agent_server, None
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass  # Clients also fail closed when the process/listener disappears.
        self._cancel_intent()
        self.durations.shutdown()
        self.downloads.close()
        self.site_manager.close()
        self._close_login_dialogs()
        self._cancel.set()
        self._restart_after_stop = False
        self._reset_agent_scroll()
        self._cancel.set()
        self._gen += 1
        self._agent_task_id = ""
        for timer in (self._agent_busy_timer, self._auto_more_timer,
                      self._agent_timer, self._pump_timer, self._thumb_timer):
            timer.stop()
        try:
            self.logins.browser.close()
            self.config.settings.window_geometry = self._geometry_b64()
            save_config(self.config)
            self.thumbs.evict(self.config.settings.cache_mb)
            self.thumbs.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
        self.session_closed.emit(self._logout_requested)


def run() -> int:
    import sys

    from PySide6.QtGui import QIcon

    from ..config import resource_path

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setFont(theme.app_font())
    icon = resource_path(os.path.join("assets", "app.ico"))
    if os.path.isfile(icon):
        app.setWindowIcon(QIcon(icon))
    from .profiles import ProfileController
    controller = ProfileController(app)
    app._profile_controller = controller
    controller.start()
    return app.exec()
