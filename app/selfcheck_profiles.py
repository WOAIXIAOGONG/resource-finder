"""Frozen-app local-save workflow; only writes inside the caller's isolated home."""
import os
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEvent
from . import config
from .profiles import ProfileStore
from .ui.profiles import ProfileController


def run(app, log, shots=''):
    from .ui.mainwindow import MainWindow
    original = MainWindow._startup_checks
    old_quit = app.quitOnLastWindowClosed()
    controller = None
    MainWindow._startup_checks = lambda self: None
    try:
        store = ProfileStore(Path(os.environ['RESOURCE_FINDER_HOME'])/'profile-smoke')
        controller = ProfileController(app, store)
        controller.start()
        app.processEvents()
        assert controller.window is None and not controller.dialog.name_edit.text()
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            controller.dialog.grab().save(str(Path(shots)/'profile-login.png'))
        controller.dialog.name_edit.setText('演示用户')
        controller.dialog._enter()
        window = controller.window
        window.config.blacklist = ['测试屏蔽词']
        window.config.history = ['测试历史']
        if shots:
            app.processEvents()
            window.grab().save(str(Path(shots)/'profile-workspace.png'))
        window.logout_btn.click()
        app.processEvents()
        assert controller.window is None and controller.dialog.isVisible()
        controller.dialog.name_edit.setText('新存档')
        controller.dialog._enter()
        assert controller.window.config.blacklist == [] and controller.window.config.history == []
        controller.window.logout_btn.click()
        app.processEvents()
        controller.dialog.name_edit.setText('演示用户')
        controller.dialog._enter()
        assert controller.window.config.blacklist == ['测试屏蔽词']
        assert controller.window.config.history == ['测试历史']
        log('profiles ok: username gate, create, save, logout, new defaults, return restore, isolated paths')
    finally:
        if controller:
            if controller.window:
                controller.window.close()
            if controller.dialog:
                controller.dialog.hide()
                controller.dialog.deleteLater()
            controller.deleteLater()
        config.use_profile(None)
        MainWindow._startup_checks = original
        app.setQuitOnLastWindowClosed(old_quit)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
