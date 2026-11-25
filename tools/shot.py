# -*- coding: utf-8 -*-
"""开发辅助：把界面渲染成图片，方便无人值守地检查外观。

    python tools/shot.py -o shots/main.png --wait 1.2
    python tools/shot.py -o shots/search.png --query 猫咪 --settle 12

不会弹出窗口，用 QWidget.grab() 渲染，适合反复调 UI。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="shots/main.png")
    ap.add_argument("-q", "--query", default="")
    ap.add_argument("--wait", type=float, default=1.0, help="首次渲染前的等待秒数")
    ap.add_argument("--settle", type=float, default=10.0, help="搜索后等待秒数")
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--theme", default="")
    ap.add_argument("--settings", action="store_true", help="顺便截设置对话框")
    ap.add_argument("--site-dialog", action="store_true", help="顺便截站点对话框")
    ap.add_argument("--edit-builtin", action="store_true", help="截内置站点编辑对话框")
    ap.add_argument("--agent", action="store_true", help="切到 Agent 模式并截图")
    args = ap.parse_args()

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.config import load_config
    from app.ui.mainwindow import MainWindow

    w, h = (int(x) for x in args.size.lower().split("x"))
    app = QApplication(sys.argv)
    cfg = load_config()
    if args.theme:
        cfg.settings.theme = args.theme
    win = MainWindow(cfg)
    win.resize(w, h)
    win.show()

    outdir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(outdir, exist_ok=True)

    shots = []

    def snap(suffix: str, widget=None):
        path = args.out
        if suffix:
            root, ext = os.path.splitext(path)
            path = "%s-%s%s" % (root, suffix, ext or ".png")
        widget = widget or win
        widget.grab().save(path)
        shots.append(path)
        print("saved", path, widget.size().width(), "x", widget.size().height())

    def shoot_dialogs():
        if args.settings:
            from app.ui.dialogs import SettingsDialog

            dlg = SettingsDialog(win.settings, win.http, win.thumbs, win)
            dlg.show()
            app.processEvents()
            QTimer.singleShot(400, lambda: (snap("settings", dlg), dlg.close()))
        if args.site_dialog:
            from app.ui.dialogs import SiteDialog

            dlg2 = SiteDialog(None, win.http, "测试关键词", win)
            dlg2.show()
            app.processEvents()
            QTimer.singleShot(500, lambda: (snap("site", dlg2), dlg2.close()))
        if args.edit_builtin:
            from app.config import builtin_sites
            from app.ui.dialogs import SiteDialog

            dlg3 = SiteDialog(builtin_sites()[0], win.http, "测试关键词", win)
            dlg3.show()
            app.processEvents()
            QTimer.singleShot(500, lambda: (snap("site-builtin", dlg3), dlg3.close()))

    def after_search():
        snap("results")
        shoot_dialogs()
        QTimer.singleShot(1500, app.quit)

    def start():
        if args.agent:
            win._set_mode("agent")
            QTimer.singleShot(900, lambda: snap("agent-idle"))
        snap("")
        shoot_dialogs()
        if args.query:
            win.search_edit.setText(args.query)
            QTimer.singleShot(200, win._start_search)
            QTimer.singleShot(int(args.settle * 1000), after_search)
        else:
            QTimer.singleShot(1600, app.quit)

    QTimer.singleShot(int(args.wait * 1000), start)
    app.exec()
    print("done:", len(shots), "张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
