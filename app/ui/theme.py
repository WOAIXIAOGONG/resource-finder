# -*- coding: utf-8 -*-
"""外观：配色表 + QSS 样式表 + 几个绘图小工具。"""
from __future__ import annotations

from string import Template

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap

DARK = {
    "bg": "#191b20",
    "panel": "#131519",
    "panel2": "#22252c",
    "card": "#191b20",
    "card_hover": "#232730",
    "border": "#2b2e37",
    "border_hover": "#495367",
    "text": "#e4e7ee",
    "sub": "#949ba9",
    "accent": "#779bff",
    "accent_dim": "#26334f",
    "primary_text": "#101e3e",
    "danger": "#ff6b6b",
    "ok": "#3ecf8e",
    "input": "#181b21",
    "toast_bg": "#222630",
    "toast_border": "#434d60",
}

LIGHT = {
    "bg": "#fafbfc",
    "panel": "#f0f2f5",
    "panel2": "#ffffff",
    "card": "#ffffff",
    "card_hover": "#eef2f8",
    "border": "#dfe3eb",
    "border_hover": "#a3b3ce",
    "text": "#202737",
    "sub": "#637087",
    "accent": "#3868cf",
    "accent_dim": "#e0e9fb",
    "primary_text": "#ffffff",
    "danger": "#d64545",
    "ok": "#1f9d63",
    "input": "#ffffff",
    "toast_bg": "#ffffff",
    "toast_border": "#d9e2f0",
}

_QSS = Template(r"""
* { outline: none; }
QWidget {
    background: $bg;
    color: $text;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
    font-size: 13px;
}
QMainWindow, QDialog { background: $bg; }
/* 子控件不要各自刷底色，否则标签会在面板上留下深色小方块 */
QLabel, QCheckBox, QRadioButton, QGroupBox::title { background: transparent; }

/* ---------- 顶栏 ---------- */
#TopBar { background: $panel; border-bottom: 1px solid $border; }
#AppTitle { font-size: 16px; font-weight: 600; }
#AppSub { color: $sub; font-size: 11px; }
#BrandMark {
    background: $accent; border-radius: 9px; color: #ffffff;
    font-size: 15px; font-weight: 700;
}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: $input; border: 1px solid $border; border-radius: 8px;
    padding: 6px 10px; selection-background-color: $accent; min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid $accent;
}
QLineEdit:disabled { color: $sub; }
#SearchInput {
    border-radius: 12px; padding: 8px 14px; font-size: 14px; min-height: 24px;
}
#SearchInput:focus { border: 1px solid $accent; }

QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background: $panel2; border: 1px solid $border; border-radius: 8px;
    selection-background-color: $accent_dim; padding: 4px;
}

QPushButton {
    background: $panel2; border: 1px solid $border; border-radius: 8px;
    padding: 6px 14px; color: $text;
}
QPushButton:hover { background: $card_hover; border-color: $border_hover; }
QPushButton:pressed { background: $accent_dim; }
QPushButton:disabled { color: $sub; border-color: $border; background: $panel; }
QPushButton#Primary {
    background: $accent; border: none; color: #ffffff; font-weight: 600;
    padding: 8px 22px; border-radius: 10px;
}
QPushButton#Primary:hover { background: #5c99ff; }
QPushButton#Primary:disabled { background: $accent_dim; color: #c9d6ee; }
QPushButton#Danger { color: $danger; }
QPushButton#Danger:hover { border-color: $danger; }
QPushButton#Flat {
    background: transparent; border: none; padding: 4px 8px; color: $sub;
}
QPushButton#Flat:hover { color: $text; background: $panel2; border-radius: 6px; }
QPushButton#RefreshBtn {
    background: $panel; color: $sub; border: 1px solid $border;
    border-radius: 8px; font-size: 15px; padding: 0px;
}
QPushButton#RefreshBtn:hover { color: $accent; border-color: $accent; background: $panel2; }
QPushButton#RefreshBtn:pressed { background: $accent_dim; color: #ffffff; }

/* ---------- 侧栏 ---------- */
#Sidebar { background: $panel; border-right: 1px solid $border; }
#SideSection { color: $sub; font-size: 11px; font-weight: 600; letter-spacing: 1px; }

QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab {
    background: transparent; color: $sub; padding: 7px 4px; margin-right: 12px;
    border: none; border-bottom: 2px solid transparent; font-size: 13px;
}
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QTabBar::tab:hover { color: $text; }

QListWidget {
    background: transparent; border: none; padding: 2px;
}
QListWidget::item {
    background: $panel2; border: 1px solid $border; border-radius: 8px;
    padding: 7px 8px; margin-bottom: 5px; color: $text;
}
QListWidget::item:hover { border-color: $border_hover; }
QListWidget::item:selected { background: $accent_dim; border-color: $accent; color: $text; }
QListWidget::indicator {
    width: 14px; height: 14px; border-radius: 4px;
    border: 1px solid $border; background: $input;
}
QListWidget::indicator:checked { background: $accent; border-color: $accent; }

#NetBadge { color: $sub; font-size: 11px; }
#NetBadge[ok="true"] { color: $ok; }
#NetBadge[ok="false"] { color: $danger; }

/* ---------- 轻提示 ---------- */
#Toast {
    background: $toast_bg; border: 1px solid $toast_border; border-radius: 10px;
}
#ToastIcon { color: $ok; font-size: 15px; font-weight: 700; background: transparent; }
#ToastText { color: $text; font-size: 12px; background: transparent; }

/* ---------- 「正在搜索」遮罩 ---------- */
#BusyOverlay {
    background: $panel; border: 1px solid $border; border-radius: 12px;
}
#BusyTitle { color: $text; font-size: 14px; font-weight: 600; background: transparent; }
#BusySub { color: $sub; font-size: 11px; background: transparent; }

/* 侧栏里的「Agent 正在搜索」——不挡结果区 */
#SideBusy {
    background: $panel2; border: 1px solid $border; border-radius: 10px;
}
#SideBusyTitle { color: $text; font-size: 11px; font-weight: 600; background: transparent; }
#SideBusySub { color: $sub; font-size: 10px; background: transparent; }

/* ---------- 结果区 ---------- */
QScrollArea, #ResultArea { background: $bg; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $border; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $sub; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QScrollBar:horizontal { height: 10px; background: transparent; }
QScrollBar::handle:horizontal { background: $border; border-radius: 4px; min-width: 30px; }

#Card {
    background: $card; border: 1px solid $border; border-radius: 12px;
}
#Card:hover { border: 1px solid $border_hover; background: $card_hover; }
#CardTitle { font-size: 13px; font-weight: 600; color: $text; background: transparent; }
#CardMeta { font-size: 11px; color: $sub; background: transparent; }
#CardBadge {
    background: rgba(76, 141, 255, 0.16); color: $accent;
    border-radius: 5px; padding: 1px 6px; font-size: 10px;
}
#CoverLabel { background: $panel2; border-radius: 8px; color: $sub; font-size: 11px; }
#CoverBadge {
    background: rgba(6, 8, 12, 0.78); color: #f2f6ff;
    border-radius: 5px; padding: 1px 5px; font-size: 10px; font-weight: 600;
}
#CoverDate {
    background: rgba(6, 8, 12, 0.72); color: #e9eefc;
    border-radius: 5px; padding: 1px 5px; font-size: 10px;
}
#EmptyTitle { font-size: 17px; font-weight: 600; color: $text; }
#EmptyHint { font-size: 12px; color: $sub; }
#StatusText { color: $sub; font-size: 12px; }

QProgressBar {
    background: $panel2; border: none; border-radius: 2px; max-height: 3px; min-height: 3px;
}
QProgressBar::chunk { background: $accent; border-radius: 2px; }

QMenu {
    background: $panel2; border: 1px solid $border; border-radius: 8px; padding: 5px;
}
QMenu::item { padding: 6px 22px 6px 12px; border-radius: 6px; }
QMenu::item:selected { background: $accent_dim; }

QToolTip {
    background: $panel2; color: $text; border: 1px solid $border;
    padding: 5px 8px; border-radius: 6px;
}

QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid $border; background: $input;
}
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }

QGroupBox {
    border: 1px solid $border; border-radius: 10px; margin-top: 14px;
    padding: 12px 10px 10px 10px; background: $panel;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 5px; color: $sub;
}

#Hint { color: $sub; font-size: 11px; }
#Divider { background: $border; max-height: 1px; min-height: 1px; border: none; }

/* ---- 模式切换 ---- */
#ModeSwitch {
    background: $input; border: 1px solid $border; border-radius: 10px;
}
QPushButton#ModeBtn {
    background: transparent; border: none; color: $sub;
    padding: 4px 12px; border-radius: 8px; font-size: 12px;
}
QPushButton#ModeBtn:hover { color: $text; background: $panel2; }
QPushButton#ModeBtn:checked {
    background: $accent; color: #ffffff; font-weight: 600;
}

/* ---- Agent 条 ---- */
#AgentBar {
    background: $panel; border-top: 1px solid $border; border-bottom: 1px solid $border;
}
#AgentDot { color: $sub; font-size: 13px; background: transparent; }
#AgentDot[on="true"] { color: $ok; }
#AgentText { color: $text; font-size: 12px; font-weight: 600; background: transparent; }
#AgentHint { color: $sub; font-size: 11px; background: transparent; }
#AgentUrl {
    background: $input; border: 1px solid $border; border-radius: 7px;
    color: $sub; font-size: 11px; padding: 3px 8px; min-height: 20px;
}
#FilterBar { background: $bg; }
#SourcePopup, #SourceOptions { background: $panel; }
#SourcePopup QLabel { color: $sub; font-size: 11px; }
QPushButton#SourceFilterButton { background: transparent; border-color: $border; border-radius: 5px; padding: 4px 18px 4px 8px; font-size: 11px; min-height: 22px; }
QCheckBox#SourceChip { background: $panel; border: 1px solid $border; border-radius: 6px; padding: 5px 9px; font-size: 11px; }
QCheckBox#SourceChip:checked { background: $accent_dim; border-color: $accent; color: $text; }
#SourcePopup QCheckBox::indicator:indeterminate { background: $accent_dim; border-color: $accent; }
#FilterEmptyHint { color: $sub; padding: 8px 24px; }
#MoreLoadingText { color: $sub; font-size: 12px; padding: 0; background: transparent; }
#FilterBar QLabel { color: $sub; font-size: 11px; }

/* ---- 「还没接入 Agent」提示框 ---- */
#DlgTitle { font-size: 16px; font-weight: 600; color: $text; background: transparent; }
#DlgHint { color: $sub; font-size: 12px; background: transparent; }
#DlgSteps { color: $text; font-size: 12px; background: transparent; }
#DlgPreview {
    background: $input; border: 1px solid $border; border-radius: 8px;
    color: $sub; font-size: 11px; padding: 6px;
}
#DlgState { color: $danger; font-size: 11px; background: transparent; }
#DlgState[ok="true"] { color: $ok; }
""")


_WORKSPACE_QSS = Template(r"""
/* Spacedrive-inspired library: quiet chrome, compact navigation, image-first grid. */
#TopBar { background: $bg; border-bottom: 1px solid $border; }
#BreadcrumbRoot { color: $sub; font-size: 12px; }
#BreadcrumbCurrent { color: $text; font-size: 12px; font-weight: 500; }
#AppTitle { font-size: 14px; font-weight: 600; }
#AppSub { color: $sub; font-size: 8px; letter-spacing: 1px; }
#BrandMark { background: $accent_dim; border-radius: 7px; }
QPushButton { padding: 5px 10px; border-radius: 6px; min-height: 18px; }
QPushButton:focus { border: 1px solid $accent; }
QPushButton#Primary { background: $accent; color: $primary_text; padding: 5px 14px; border-radius: 7px; }
QPushButton#Primary:hover { background: $accent; border: 1px solid $text; }
QPushButton#Flat { padding: 5px 8px; border: 1px solid transparent; border-radius: 6px; }
QPushButton#Flat:focus { border: 1px solid $accent; }
QPushButton#Secondary { background: $accent_dim; color: $text; border-color: $accent_dim; font-weight: 500; }
QPushButton#Secondary:hover { border-color: $accent; }
#ModeSwitch { background: transparent; border: none; }
QPushButton#ModeBtn { text-align: left; padding: 4px 12px; border: 1px solid transparent; border-radius: 6px; }
QPushButton#ModeBtn:checked { background: $accent_dim; color: $text; border-color: $accent_dim; font-weight: 500; }
QPushButton#ModeBtn:focus { border-color: $accent; }
#Sidebar { background: $panel; }
#SidePage, #SidePage QListWidget, #SidePage QListWidget QWidget { background: $panel; }
#SideSection { color: $sub; font-size: 11px; font-weight: 500; letter-spacing: 0px; }
QTabWidget::pane, QTabWidget > QWidget > QWidget { background: transparent; }
QTabBar { background: transparent; border: none; }
QTabBar::tab { padding: 7px 12px; margin-right: 4px; font-size: 11px; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QListWidget { padding: 2px 0px; }
QListWidget::item { background: transparent; border: 1px solid transparent; border-radius: 5px; padding: 7px 6px; margin-bottom: 2px; }
QListWidget::item:hover { background: $panel2; border-color: transparent; }
QListWidget::item:selected { background: $accent_dim; border-color: transparent; }
QListWidget::indicator { width: 13px; height: 13px; margin-right: 6px; }
#NetworkPanel { background: transparent; border: none; }
#NetBadge { font-size: 10px; }
#NetBadge[ok="true"] { color: $sub; }
#ResultHeader { background: transparent; }
#ResultsTitle { font-size: 15px; font-weight: 600; }
#ResultCount { background: transparent; color: $sub; padding: 4px 8px; font-size: 11px; }
#FilterBar QComboBox { background: transparent; border-color: $border; border-radius: 5px; padding: 4px 8px; font-size: 11px; }
#FilterBar QCheckBox { font-size: 11px; color: $sub; }
QComboBox::drop-down { width: 20px; }
#AgentBar { background: $panel; border: 1px solid $border; border-radius: 7px; }
#AgentBar QPushButton { padding: 4px 6px; font-size: 11px; }
#AgentText { font-size: 11px; font-weight: 500; }
#AgentDetails { background: transparent; }
#AgentHint { font-size: 11px; }
#AgentUrl { border: none; background: $input; font-family: "Consolas"; font-size: 11px; }
#SideBusy { border: 1px solid $border; border-radius: 7px; background: $input; }
#SideBusyTitle { font-size: 11px; }
#SideBusySub { font-size: 10px; }
#Card { background: transparent; border: 1px solid transparent; border-radius: 7px; }
#Card:hover { background: $card_hover; border: 1px solid $border_hover; }
#CardTitle { font-size: 12px; font-weight: 400; }
#CardMeta { font-size: 10px; }
#CardBadge { background: transparent; color: $sub; padding: 0px; font-size: 10px; }
#CoverLabel { border-radius: 6px; }
#CoverDate, #CoverBadge { border-radius: 4px; font-size: 9px; }
#CoverPlay { background: rgba(15, 20, 32, 0.8); border: 1px solid rgba(255,255,255,0.3); border-radius: 19px; }
#EmptyTitle { font-size: 24px; font-weight: 500; }
#EmptyHint { font-size: 12px; color: $sub; }
#EmptyEyebrow { color: $sub; font-size: 10px; letter-spacing: 1px; }
#EmptyIcon { background: $panel2; border: 1px solid $border; border-radius: 16px; }
#EmptyFooter { color: $sub; font-size: 10px; padding-top: 12px; }
QStatusBar { background: $panel; border-top: 1px solid $border; padding: 2px 10px; }
QStatusBar::item { border: none; }
#StatusText { font-size: 10px; }
QDialog { background: $bg; }
QDialogButtonBox QPushButton { min-width: 68px; }
QDialogButtonBox QPushButton:default { background: $accent; color: $primary_text; border-color: $accent; }
QGroupBox { border-radius: 8px; margin-top: 16px; padding: 12px 12px 10px; }
QGroupBox::title { font-weight: 500; color: $text; }
#ComposerDock { background: $bg; border-top: 1px solid $border; }
#ComposerPanel { background: $panel2; border: 1px solid $border; border-radius: 9px; }
#ComposerPanel[focused="true"] { border-color: $border_hover; }
QPlainTextEdit#SearchInput { background: transparent; border: none; padding: 3px 2px; font-size: 13px; selection-background-color: $accent_dim; }
QPlainTextEdit#SearchInput:focus { border: none; }
QPlainTextEdit#SearchInput QWidget { background: transparent; }
#ComposerHint { color: $sub; font-size: 10px; }
#ComposerPanel QPushButton#RefreshBtn { border: none; background: transparent; border-radius: 6px; }
#ComposerPanel QPushButton#RefreshBtn:hover { background: $card_hover; }
""")


_DIALOG_QSS = Template(r"""
/* Dialogs share the workspace palette, with clearer content and action hierarchy. */
QDialog#StyledDialog, QMessageBox#StyledMessage { background: $bg; }
#DialogHeader, #DialogBody, #DialogScroll, #DialogScroll > QWidget,
#DialogFooter, #DialogHeader QWidget { background: transparent; }
#DialogTitle { font-size: 21px; font-weight: 600; color: $text; }
#DialogHint, #StyledDialog #Hint, #StyledDialog #DlgHint {
    color: $sub; font-size: 12px; background: transparent;
}
#DialogBadge { background: $accent_dim; border: 1px solid $border; border-radius: 14px; }
#DialogCard { background: $panel2; border: 1px solid $border; border-radius: 14px; }
#DialogStepNumber { background: $accent_dim; color: $accent; border-radius: 8px; font-size: 12px; font-weight: 600; }
#DialogStepTitle { font-size: 13px; font-weight: 600; color: $text; }
#DialogNotice {
    color: $text; background: $accent_dim; border: 1px solid $border;
    border-radius: 10px; padding: 12px 14px; font-size: 12px;
}
#DialogLinks { padding: 2px 0; font-size: 12px; }
#DialogFooter { border: none; border-top: 1px solid $border; }
#StyledDialog QPushButton, #StyledMessage QPushButton {
    min-height: 22px; min-width: 68px; padding: 7px 16px; border-radius: 9px;
    font-size: 12px; background: $panel2; color: $text; border: 1px solid $border;
}
#StyledDialog QPushButton:hover, #StyledMessage QPushButton:hover { background: $card_hover; border-color: $border_hover; }
#StyledDialog QPushButton:focus, #StyledMessage QPushButton:focus { border: 1px solid $accent; }
#StyledDialog QPushButton#DialogPrimary, #StyledMessage QPushButton#DialogPrimary {
    background: $accent; color: $primary_text; border: 1px solid $accent; font-weight: 600;
}
#StyledDialog QPushButton#DialogPrimary:hover, #StyledMessage QPushButton#DialogPrimary:hover { border-color: $text; }
#StyledDialog QPushButton:disabled, #StyledDialog QPushButton#DialogPrimary:disabled {
    background: $panel; color: $sub; border-color: $border;
}
#StyledDialog QPushButton#Flat { background: transparent; color: $sub; border-color: transparent; min-width: 30px; padding: 6px 8px; }
#StyledDialog QPushButton#Flat:hover { background: $panel2; color: $text; }
#StyledDialog QGroupBox { background: $panel2; border: 1px solid $border; border-radius: 12px; padding: 18px 14px 14px; margin-top: 14px; }
#StyledDialog QGroupBox::title { subcontrol-origin: margin; left: 16px; color: $text; font-size: 12px; font-weight: 600; padding: 0 6px; }
#StyledDialog QGroupBox QWidget { background: transparent; }
#StyledDialog QLineEdit, #StyledDialog QComboBox, #StyledDialog QSpinBox, #StyledDialog QDoubleSpinBox {
    background: $input; border: 1px solid $border; border-radius: 8px; padding: 7px 10px;
    min-height: 20px; font-size: 12px;
}
#StyledDialog QLineEdit:focus, #StyledDialog QComboBox:focus, #StyledDialog QSpinBox:focus, #StyledDialog QDoubleSpinBox:focus { border-color: $accent; }
#StyledDialog QLineEdit:disabled { background: $panel; color: $sub; }
#StyledDialog QTabWidget::pane { border: none; background: transparent; }
#StyledDialog QTabBar::tab { background: $panel; color: $sub; border: 1px solid transparent; border-radius: 8px; padding: 9px 20px; margin: 0 8px 8px 0; }
#StyledDialog QTabBar::tab:selected { background: $accent_dim; color: $accent; border-color: $border; }
#StyledDialog QTabBar::tab:hover { color: $text; }
#StyledDialog QPlainTextEdit { background: $panel2; border: 1px solid $border; border-radius: 10px; padding: 12px; color: $sub; font-size: 12px; }
#StyledDialog QPlainTextEdit QWidget { background: transparent; }
#StyledDialog #DialogStatus { border: none; background: $panel2; color: $text; }
#StyledDialog #DlgState { color: $accent; font-size: 12px; }
#StyledMessage #qt_msgbox_label { color: $text; font-size: 14px; background: transparent; }
#StyledMessage #qt_msgbox_informativelabel { color: $sub; font-size: 12px; }
#StyledMessage QTextEdit { background: $panel2; color: $sub; border: 1px solid $border; border-radius: 8px; padding: 10px; font-size: 12px; }
#StyledMessage QTextEdit QWidget { background: transparent; }
""")


_DOWNLOAD_QSS = Template(r"""
#DownloadTask { background: $panel2; border: 1px solid $border; border-radius: 14px; }
#DownloadTask:hover { border-color: $border_hover; }
#DownloadTitle { color: $text; font-size: 13px; font-weight: 600; }
#DownloadSymbol { background: $accent_dim; border-radius: 10px; }
#DownloadStat { background: $panel2; border: 1px solid $border; border-radius: 12px; padding: 8px; }
#DownloadCount { font-size: 25px; font-weight: 600; color: $text; }
#DownloadState { padding: 4px 10px; border-radius: 9px; font-size: 11px; }
#DownloadState[tone="success"] { color: #49b88a; background: rgba(73,184,138,0.12); }
#DownloadState[tone="error"] { color: #df9871; background: rgba(223,152,113,0.12); }
#DownloadState[tone="active"] { color: $accent; background: $accent_dim; }
#DownloadState[tone="muted"] { color: $sub; background: $panel; }
#DownloadTask QProgressBar { border: none; border-radius: 2px; background: $border; }
#DownloadTask QProgressBar::chunk { background: $accent; border-radius: 2px; }
#DownloadEmpty { color: $sub; padding: 42px 16px; border: 1px dashed $border; border-radius: 14px; }
QPushButton#MenuDownload { text-align: left; padding: 12px 18px; margin: 3px; min-width: 210px;
    border-radius: 9px; background: $accent_dim; border: 1px solid $accent; color: $text; font-size: 12px; font-weight: 600; }
QPushButton#MenuDownload:hover, QPushButton#MenuDownload:focus { background: $accent; color: $primary_text; }
#CoverDownloadHint { color: #ffffff; background: rgba(18,24,40,0.86); border: 1px solid rgba(255,255,255,0.22);
    border-radius: 5px; padding: 3px 7px; font-size: 10px; }
""")

def stylesheet(theme: str) -> str:
    from pathlib import Path
    import sys
    colors = LIGHT if theme == "light" else DARK
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets"
    check = (assets / ("check.svg" if theme == "light" else "check-dark.svg")).as_posix()
    chevron = (assets / "chevron.svg").as_posix()
    return (_QSS.substitute(**colors) + _WORKSPACE_QSS.substitute(**colors) + _DIALOG_QSS.substitute(**colors) + _DOWNLOAD_QSS.substitute(**colors)
            + f'QCheckBox::indicator:checked, QListWidget::indicator:checked {{ image: url("{check}"); }}'
            + f'QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}')


def palette(theme: str) -> dict:
    return dict(LIGHT if theme == "light" else DARK)


def app_font() -> QFont:
    f = QFont("Microsoft YaHei UI", 9)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def elide(text: str, width: int, font: QFont, mode=Qt.TextElideMode.ElideRight) -> str:
    fm = QFontMetrics(font)
    return fm.elidedText(text or "", mode, max(10, width))


def rounded_pixmap(pix: QPixmap, radius: int = 8) -> QPixmap:
    """把封面图裁成圆角，省得用遮罩控件。"""
    if pix.isNull():
        return pix
    size = pix.size()
    out = QPixmap(size)
    out.setDevicePixelRatio(pix.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size.width(), size.height()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


def thumbnail_pixmap(path: str, width: int, height: int) -> QPixmap:
    """加载封面并按「填满 + 居中裁切」缩放，避免变形。"""
    src = QPixmap(path)
    if src.isNull():
        return src
    scaled = src.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, (scaled.height() - height) // 2)
    return scaled.copy(x, y, width, height)


def placeholder_cover(width: int, height: int, text: str = "无封面",
                      theme: str = "dark") -> QPixmap:
    c = palette(theme)
    pm = QPixmap(width, height)
    pm.fill(QColor(c["panel2"]))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(c["sub"]))
    f = QFont("Microsoft YaHei UI", 8)
    p.setFont(f)
    p.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm

def sound_cover(width,height,theme='dark'):
    c=palette(theme);pm=QPixmap(width,height);pm.fill(QColor(c['panel2']))
    p=QPainter(pm);p.setRenderHint(QPainter.RenderHint.Antialiasing);p.setPen(QColor(c['accent']))
    # Decorative sound glyph; this is not a measured waveform.
    for i,h in enumerate([12,26,18,42,56,34,18,40,24,12]):
        p.drawRoundedRect(QRectF(width/2-48+i*10,height/2-12-h/2,3,h),1.5,1.5)
    p.setPen(QColor(c['sub']));p.setFont(QFont('Microsoft YaHei UI',8))
    p.drawText(QRectF(0,height-31,width,20),Qt.AlignmentFlag.AlignCenter,'音效 · 点击前往试听');p.end();return pm
