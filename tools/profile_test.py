"""Portable local saves and real login / logout lifecycle without external services."""
import testenv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config
from app.profiles import ProfileStore


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rf-profiles-')
        self.root = Path(self.temp.name)
        self.store = ProfileStore(self.root)
        self.sessions = []

    def open(self, *a, **kw):
        session = self.store.open(*a, **kw)
        self.sessions.append(session)
        return session

    def tearDown(self):
        for session in self.sessions:
            session.release()
        config.use_profile(None)
        self.temp.cleanup()

    def test_new_saves_default_independent_and_reload(self):
        first = self.open('小明')
        first.config.blacklist = ['甲']
        first.config.history = ['甲的查询']
        first.config.sites.pop()
        first.config.settings.timeout = 300
        first.config.settings.resource_type = 'novel'
        self.assertTrue(config.save_config(first.config))
        second = self.open('小红')
        self.assertEqual(second.config.to_dict(), config.Config().to_dict())
        config.use_profile(str(second.directory))
        first.config.blacklist.append('旧回调')
        config.save_config(first.config)
        self.assertEqual(json.loads((second.directory/'config.json').read_text(encoding='utf-8'))['blacklist'], [])
        first.release()
        again = self.open('小明')
        self.assertEqual(again.config.blacklist, ['甲', '旧回调'])
        self.assertEqual(again.config.settings.timeout, 300)
        self.assertEqual(again.config.settings.resource_type, 'article')
        self.assertEqual(again.config.history, ['甲的查询'])
        self.assertEqual(len(again.config.sites), len(config.default_sites()) - 1)

    def test_case_alias_and_lock(self):
        first = self.open(' Alice ')
        with self.assertRaisesRegex(ValueError, '另一个窗口'):
            self.open('alice')
        first.release()
        again = self.open('ALICE')
        self.assertEqual(again.name, 'Alice')
        self.assertEqual(self.store.names(), ['Alice'])

    def test_names_cannot_escape_root(self):
        for name in ['', ' ', '../abc', 'a/b', 'a\\b', 'a\nb', 'x'*33, '..']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.open(name)
        self.assertFalse(self.store.root.exists())

    def test_portable_copy_keeps_saves(self):
        first = self.open('便携存档')
        first.config.blacklist = ['保存的数据']
        config.save_config(first.config)
        first.release()
        destination = self.root/'another-tool'
        shutil.copytree(self.store.root, destination/'profiles')
        moved = ProfileStore(destination).open('便携存档')
        try:
            self.assertEqual(moved.config.blacklist, ['保存的数据'])
        finally:
            moved.release()

    def test_corrupt_save_does_not_reset_and_releases_lock(self):
        first = self.open('损坏测试')
        first.release()
        path = first.directory/'config.json'
        path.write_text('{broken', encoding='utf-8')
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.store.open('损坏测试')
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_failed_new_save_not_listed(self):
        with patch('app.profiles.config.save_config', return_value=False), self.assertRaises(OSError):
            self.store.open('不能写入')
        self.assertEqual(self.store.names(), [])
        self.assertFalse(self.store.exists('不能写入'))

    def test_import_is_optional_and_never_writes_legacy(self):
        legacy = self.root/'legacy.json'
        cfg = config.Config()
        cfg.blacklist, cfg.history = ['旧黑名单'], ['旧历史']
        original = json.dumps(cfg.to_dict()).encode('utf-8')
        legacy.write_bytes(original)
        with patch.object(self.store, 'legacy_path', return_value=legacy):
            fresh = self.open('默认')
            imported = self.open('导入', import_legacy=True)
        self.assertEqual(fresh.config.blacklist, [])
        self.assertEqual(imported.config.blacklist, ['旧黑名单'])
        imported.config.add_blackword('新增')
        config.save_config(imported.config)
        self.assertEqual(legacy.read_bytes(), original)

    def test_cache_and_config_paths_are_separate(self):
        first, second = self.open('一'), self.open('二')
        config.use_profile(str(first.directory))
        cache_a = config.thumbs_dir()
        config.use_profile(str(second.directory))
        self.assertNotEqual(cache_a, config.thumbs_dir())
        self.assertEqual(config.config_path(), str(second.directory/'config.json'))

    def test_packaging_preserves_saves_but_excludes_them_from_zip(self):
        import zipfile
        from tools import package
        distribution = self.root/'dist'
        app = distribution/'资源搜寻器'
        (app/'profiles'/'alice').mkdir(parents=True)
        private = app/'profiles'/'alice'/'config.json'
        private.write_text('private', encoding='utf-8')
        (app/'keep.txt').write_text('product', encoding='utf-8')
        with patch.object(package, 'DIST', str(distribution)), patch.object(package, 'APP_DIR', str(app)), patch.object(package, 'SRC_ITEMS', ()):
            package.copy_source()
            archive = package.make_zip('test')
        self.assertEqual(private.read_text(encoding='utf-8'), 'private')
        with zipfile.ZipFile(archive) as saved:
            self.assertFalse(any('profiles' in path.split('/') for path in saved.namelist()))
            self.assertIn('资源搜寻器/keep.txt', saved.namelist())


class ProfileUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ProfileTests.setUp(self)
        from app.ui.mainwindow import MainWindow
        self.startup = patch.object(MainWindow, '_startup_checks', lambda self: None)
        self.startup.start()
        self.controller = None
        self.quit_mode = self.app.quitOnLastWindowClosed()

    def tearDown(self):
        if self.controller:
            if self.controller.window:
                self.controller.window.close()
            if self.controller.dialog:
                self.controller.dialog.hide()
                self.controller.dialog.deleteLater()
            self.controller.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.app.setQuitOnLastWindowClosed(self.quit_mode)
        self.startup.stop()
        ProfileTests.tearDown(self)

    def test_logout_revokes_agent_and_old_callbacks_stay_in_old_save(self):
        from app.ui.profiles import ProfileController
        from app.agent import AgentServer
        import requests
        self.controller = ProfileController(self.app, self.store)
        self.controller.start()
        self.controller.dialog.name_edit.setText('旧会话')
        self.controller.dialog._enter()
        old_window = self.controller.window
        old_cfg = old_window.config
        listener = AgentServer(lambda *args: None, port=0, config=old_cfg)
        self.assertTrue(listener.start())
        old_window._agent_server = listener
        token = listener.token
        old_window.logout_btn.click()
        self.app.processEvents()
        self.assertFalse(listener.running)
        self.controller.dialog.name_edit.setText('新会话')
        self.controller.dialog._enter()
        old_cfg.blacklist.append('旧回调')
        config.save_config(old_cfg)
        self.assertEqual(self.controller.window.config.blacklist, [])
        self.assertNotEqual(old_cfg._storage_dir, self.controller.window.config._storage_dir)

    def test_enter_logout_new_save_and_return_restore(self):
        from app.ui.profiles import ProfileController
        from PySide6.QtCore import QCoreApplication, QEvent
        self.controller = ProfileController(self.app, self.store)
        self.controller.start()
        self.assertIsNone(self.controller.window)
        self.assertEqual(self.controller.dialog.name_edit.text(), '')
        self.controller.dialog.name_edit.setText('第一位')
        self.controller.dialog._enter()
        win = self.controller.window
        self.assertTrue(win.isVisible())
        win.config.blacklist = ['私有配置']
        win.config.history = ['历史词']
        win.logout_btn.click()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(self.controller.window)
        self.assertTrue(self.controller.dialog.isVisible())
        self.assertEqual(self.controller.dialog.name_edit.text(), '')
        self.controller.dialog.name_edit.setText('第二位')
        self.controller.dialog._enter()
        self.assertEqual(self.controller.window.config.blacklist, [])
        self.assertEqual(self.controller.window.config.history, [])
        self.controller.window.logout_btn.click()
        self.app.processEvents()
        self.controller.dialog.name_edit.setText('第一位')
        self.controller.dialog._enter()
        self.assertEqual(self.controller.window.config.blacklist, ['私有配置'])
        self.assertEqual(self.controller.window.config.history, ['历史词'])

    def test_save_failure_keeps_user_window_and_session(self):
        from app.ui.profiles import ProfileController
        self.controller = ProfileController(self.app, self.store)
        self.controller.start()
        self.controller.dialog.name_edit.setText('保存失败')
        self.controller.dialog._enter()
        win = self.controller.window
        with patch('app.ui.mainwindow.save_config', return_value=False), patch('app.ui.mainwindow.QMessageBox.warning'):
            win.logout_btn.click()
        self.assertIs(self.controller.window, win)
        self.assertTrue(win.isVisible())
        self.assertFalse(win._closing)
        self.assertIsNotNone(self.controller.session)


if __name__ == '__main__':
    unittest.main(verbosity=2)
