"""Loopback automatic pairing, browser setup boundaries and login UI lifecycle."""
import testenv
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from app.browser_pairing import PairingAssist, running_browsers
from app.login import login_manager
from app.config import Config, Settings
from app.login_browser import LoginBrowser
from browser_bridge_test import BrowserBridgeTests
from login_test import LoginUITests

BROWSER = dict(id='edge', name='Edge', path=r'C:\Program Files\Edge\msedge.exe', settings='edge://extensions/')


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.bridge = LoginBrowser(Settings())
        self.helper = PairingAssist(self.bridge)
        self.http = requests.Session(); self.http.trust_env = False

    def tearDown(self):
        self.helper.close(); self.bridge.close(); self.http.close()

    def test_page_is_loopback_nonce_protected_no_store_and_no_external_assets(self):
        response = self.http.get(self.helper.url, timeout=2)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.bridge.token, response.text)
        self.assertIn('data-rf-pair=', response.text)
        self.assertIn('首次使用', response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertNotIn('script src=', response.text)
        self.assertEqual(self.http.get(self.helper.origin+'/rf-connect/wrong', timeout=2).status_code, 403)
        self.assertEqual(self.http.get(self.helper.url, headers={'Host': 'evil.test'}, timeout=2).status_code, 410)
        self.assertEqual(self.http.get(self.helper.url, headers={'Origin': 'https://evil.test'}, timeout=2).status_code, 403)

    def test_status_has_no_credentials_and_expired_page_is_unusable(self):
        response = self.http.get(self.helper.url+'/status', timeout=2)
        self.assertEqual(response.json(), {'connected': False})
        self.assertNotIn(self.bridge.token, response.text)
        self.helper.expires = time.monotonic()-1
        self.assertEqual(self.http.get(self.helper.url, timeout=2).status_code, 410)

    def test_open_uses_only_running_browsers_and_never_sets_debug_or_install_flags(self):
        with patch('app.browser_pairing.running_browsers', return_value=[BROWSER]), patch('app.browser_pairing.launch_page') as launch:
            self.helper.open()
        launch.assert_called_once_with(BROWSER, self.helper.url)
        with patch('app.browser_pairing.running_browsers', return_value=[]), patch('app.browser_pairing.launch_page') as launch:
            with self.assertRaisesRegex(RuntimeError, '未检测到'): self.helper.open()
            launch.assert_not_called()

    def test_extension_management_requires_same_origin_explicit_click_and_fixed_browser(self):
        self.helper.browsers = [BROWSER]
        url = self.helper.url+'/extensions'; headers={'Origin': self.helper.origin, 'X-RF-Nonce': self.helper.nonce}
        with patch('app.browser_pairing.launch_page') as launch:
            self.assertEqual(self.http.post(url, json={'browser':'edge'}, timeout=2).status_code, 403)
            self.assertEqual(self.http.post(url, json={'browser':'edge'}, headers={**headers,'Origin':'https://evil.test'}, timeout=2).status_code, 403)
            self.assertEqual(self.http.post(url, json={'browser':'evil'}, headers=headers, timeout=2).status_code, 400)
            launch.assert_not_called()
            self.assertEqual(self.http.post(url, json={'browser':'edge'}, headers=headers, timeout=2).status_code, 200)
            launch.assert_called_once_with(BROWSER, 'edge://extensions/')

    def test_inventory_only_reads_running_executable_paths_and_decodes_unicode(self):
        with patch('app.browser_pairing.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(
                [r'C:\测试\msedge.exe', r'C:\Other\evil.exe'], ensure_ascii=False).encode('utf-8'))) as run, \
                patch('app.browser_pairing.Path.is_file', return_value=True):
            found = running_browsers()
        self.assertEqual([r['id'] for r in found], ['edge'])
        command = run.call_args.args[0]
        self.assertIn('-NoProfile', command)
        self.assertNotIn('Cookie', command[-1]); self.assertNotIn('CommandLine', command[-1])

    def test_cancel_during_browser_inventory_never_opens_a_late_tab(self):
        cancel=threading.Event()
        def inventory():cancel.set();return [BROWSER]
        with patch('app.browser_pairing.running_browsers',side_effect=inventory), patch('app.browser_pairing.launch_page') as launch:
            with self.assertRaisesRegex(RuntimeError,'取消'):self.helper.open(cancel)
            launch.assert_not_called()


class AutoConnectionTests(unittest.TestCase):
    setUp = BrowserBridgeTests.setUp
    tearDown = BrowserBridgeTests.tearDown
    client = BrowserBridgeTests.client

    def test_already_connected_signed_in_page_needs_no_helper(self):
        self.client()
        with patch('app.browser_pairing.PairingAssist', side_effect=AssertionError('unnecessary setup')):
            self.browser.ensure_extension_connected()

    def test_automatic_helper_connection_uses_real_websocket_and_same_browser_search(self):
        pages = []
        def launched(browser, url):
            local = requests.Session(); local.trust_env = False
            try: pages.append(local.get(url, timeout=2).text)
            finally: local.close()
            self.client(tab=17)
        with patch('app.browser_pairing.running_browsers', return_value=[BROWSER]), patch('app.browser_pairing.launch_page', side_effect=launched):
            self.browser.ensure_extension_connected()
        self.assertEqual(len(pages), 1)
        self.assertIn('data-rf-pair=', pages[0])
        _, cfg = self.browser.search_page('fixture')
        self.assertEqual(cfg['_rf_browser_session'][1], 17)

    def test_no_browser_is_actionable_and_closes_pairing_server(self):
        with patch('app.browser_pairing.running_browsers', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, '未检测到'): self.browser.ensure_extension_connected()
        self.assertIsNone(self.browser.pairing)

    def test_cancel_while_waiting_for_extension_stops_promptly_and_does_not_reconnect(self):
        cancel = threading.Event(); timer=threading.Timer(.15,cancel.set);timer.start()
        try:
            started=time.monotonic()
            with patch('app.browser_pairing.running_browsers', return_value=[BROWSER]), patch('app.browser_pairing.launch_page') as launch:
                with self.assertRaisesRegex(RuntimeError, '取消'): self.browser.ensure_extension_connected(cancel)
                self.assertEqual(launch.call_count, 1)
            self.assertLess(time.monotonic()-started, 1.5)
            self.assertIsNone(self.browser.pairing)
        finally: timer.cancel()

    def test_setup_is_only_requested_by_explicit_auto_connect_flag(self):
        cfg=Config();manager=login_manager(cfg);site=cfg.get_site('ytb');cancel=threading.Event()
        manager.require('test',site,'fixture',1,'login',cancel)
        try:
            with patch.object(manager.browser,'ensure_connected') as auto, patch.object(manager.browser,'discover',side_effect=RuntimeError('not paired')), \
                    patch('app.adapters.youtube.YouTubeAdapter.search',return_value=[]):
                manager.verify('test',site.id);auto.assert_not_called()
                manager.verify('test',site.id,auto_connect=True);auto.assert_called_once()
        finally:manager.browser.close()


class PairingUITests(unittest.TestCase):
    setUpClass=classmethod(LoginUITests.setUpClass.__func__)
    tearDownClass=classmethod(LoginUITests.tearDownClass.__func__)
    setUp=LoginUITests.setUp
    tearDown=LoginUITests.tearDown
    request=LoginUITests.request

    def test_login_dialog_has_no_manual_pairing_links_and_spinner_covers_connection(self):
        row,dialog=self.request()
        from PySide6.QtWidgets import QLabel
        self.assertFalse(any('href=' in label.text() for label in dialog.findChildren(QLabel)))
        busy=dict(row,state='verifying',phase='connecting',reason='正在自动连接浏览器')
        self.win._on_login_event(busy);self.app.processEvents()
        self.assertFalse(dialog.verify_button.isEnabled())
        self.assertTrue(dialog.spinner._timer.isActive())
        self.assertIn('连接浏览器',dialog.verify_button.text())
        self.win._on_login_event(dict(row,reason='扩展待确认'));self.app.processEvents()
        self.assertTrue(dialog.verify_button.isEnabled());self.assertFalse(dialog.spinner._timer.isActive())

    def test_button_dispatches_automatic_connect_and_skip_only_stops_this_platform(self):
        row,dialog=self.request();called=threading.Event();calls=[]
        def verify(*args,**kwargs): calls.append((args,kwargs));called.set()
        with patch.object(self.win.logins,'verify',side_effect=verify):
            dialog.verify_button.click();self.assertTrue(called.wait(1))
        self.assertTrue(calls[0][1]['auto_connect'])
        dialog.reject();self.app.processEvents()
        self.assertFalse(self.server.cancellation(row['scope']).is_set())

    def test_queued_connecting_update_cannot_reopen_skipped_platform(self):
        row,dialog=self.request();dialog.reject();self.app.processEvents()
        self.win._on_login_event(dict(row,state='verifying',phase='connecting',reason='late'))
        self.app.processEvents()
        self.assertNotIn((row['scope'],row['source_id']),self.win._login_dialogs)

    def test_long_connection_notice_is_not_clipped_after_dynamic_change(self):
        row,dialog=self.request()
        dialog.resize(470,520)
        dialog.set_message('正在连接你已打开的浏览器；如首次使用或扩展需要更新，请在浏览器连接助手中完成一次确认。无需复制连接信息。')
        for _ in range(4): self.app.processEvents();time.sleep(.01)
        self.assertGreaterEqual(dialog.message.height(),dialog.message.heightForWidth(dialog.message.width()))


del BrowserBridgeTests,LoginUITests

if __name__=='__main__':unittest.main(verbosity=2)
