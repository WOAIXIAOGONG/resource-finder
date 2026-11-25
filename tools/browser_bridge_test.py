"""Real localhost transport tests: pairing, isolation, cancellation, browser sessions."""
import testenv
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import threading
import time
import unittest
from unittest.mock import patch

from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus
from app.config import Settings, Config
from app.login_browser import LoginBrowser
from app.login import login_manager, LoginRequired

ORIGIN = 'chrome-extension://' + 'a' * 32
DATA = {'contents': {'videoRenderer': {'videoId': 'testvideo01', 'title': {'simpleText': 'Fixture'}}}}
HTML = '<script>ytcfg.set({"LOGGED_IN":true});var ytInitialData = ' + json.dumps(DATA) + ';</script>'


class BrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        self.browser = LoginBrowser(Settings())
        self.url = self.browser.connection_info()
        self.sockets = []
        self.jobs = []

    def tearDown(self):
        for ws in self.sockets:
            ws.close()
        self.browser.close()

    def client(self, tab=1, signed=True, silent=False, media=None):
        ws = connect(self.url, origin=ORIGIN, proxy=None)
        self.sockets.append(ws)
        self.assertEqual(json.loads(ws.recv())['type'], 'connected')
        def loop():
            try:
                for raw in ws:
                    msg = json.loads(raw)
                    if msg['type'] != 'job':
                        continue
                    self.jobs.append(msg)
                    if silent:
                        continue
                    data = (media if msg['op'] == 'aigei_media' else [{'id': tab, 'signedIn': signed}] if msg['op'] == 'discover'
                            else {'html': HTML} if msg['op'] == 'search' else {'tab': tab})
                    ws.send(json.dumps(dict(type='result', id=msg['id'], ok=True, data=data)))
            except Exception:
                pass
        threading.Thread(target=loop, daemon=True).start()
        return ws

    def test_no_browser_launch_and_missing_extension_actionable(self):
        with patch('subprocess.Popen', side_effect=AssertionError('browser launched')):
            self.assertEqual(self.browser.open_login(), self.url)
            with self.assertRaisesRegex(RuntimeError, '尚未连接浏览器扩展'):
                self.browser.discover(None)

    def test_pairing_requires_token_and_extension_origin(self):
        for url, origin in ((self.url.replace('token=', 'wrong='), ORIGIN),
                            (self.url, 'https://www.youtube.com'), (self.url, None)):
            with self.assertRaises(InvalidStatus):
                connect(url, origin=origin, proxy=None)
        self.assertFalse(self.browser.active)

    def test_scan_connected_profiles_and_reuse_exact_session(self):
        self.client(tab=7)
        data, config = self.browser.search_page('nature')
        self.assertEqual(data, DATA)
        session = config['_rf_browser_session']
        self.client(tab=9)
        candidates = self.browser.discover(None)
        self.assertEqual({tab for _, tab in candidates}, {7, 9})
        self.assertEqual(self.browser.continuation({'continuation': 'token'}, session=session), {'tab': 7})

    def test_connected_but_signed_out_does_not_mark_login_ready(self):
        self.client(signed=False)
        with self.assertRaisesRegex(LoginRequired, '未检测到已登录'):
            self.browser.search_page('nature')
        self.assertFalse(any(job['op'] == 'search' for job in self.jobs))

    def test_stale_tab_avatar_cannot_mask_signed_out_search_page(self):
        self.client()
        with patch(__name__ + '.HTML', HTML.replace('"LOGGED_IN":true', '"LOGGED_IN":false')):
            with self.assertRaisesRegex(LoginRequired, '登录已失效'):
                self.browser.search_page('nature')

    def test_cancel_pending_request_promptly_and_discard_late_result(self):
        self.client(silent=True)
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set); timer.start()
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, '取消'):
            self.browser.discover(cancel)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.browser.pending, {})

    def test_disconnected_tab_never_sends_continuation_to_other_browser(self):
        first = self.client(tab=7)
        _, config = self.browser.search_page('nature')
        self.client(tab=9)
        first.close()
        with self.assertRaises(RuntimeError):
            self.browser.continuation({}, session=config['_rf_browser_session'])

    def test_response_cannot_be_forged_by_another_connected_browser(self):
        first = connect(self.url, origin=ORIGIN, proxy=None); self.sockets.append(first); first.recv()
        first_id = next(iter(self.browser.clients))
        second = connect(self.url, origin=ORIGIN, proxy=None); self.sockets.append(second); second.recv()
        key, job = self.browser._send_job(first_id, 'discover')
        first.recv()
        second.send(json.dumps({'type': 'result', 'id': key, 'ok': True, 'data': 'wrong'}))
        self.assertFalse(job['event'].wait(.15))
        first.send(json.dumps({'type': 'result', 'id': key, 'ok': True, 'data': 'right'}))
        self.assertEqual(self.browser._wait_job(key, job, None, time.monotonic()+2), 'right')

    def test_verify_without_extension_never_opens_browser(self):
        cfg = Config(); manager = login_manager(cfg)
        yt = next(site for site in cfg.sites if site.builtin == 'youtube')
        manager.require('task', yt, 'query', 1, 'Sign in', threading.Event())
        try:
            with patch('subprocess.Popen', side_effect=AssertionError('browser launched')):
                manager.verify('task', yt.id)
            row = manager.snapshot('task')[yt.id]
            self.assertEqual(row['state'], 'waiting')
            self.assertIn('尚未连接浏览器扩展', row['reason'])
            self.assertNotIn(manager.browser.token, json.dumps(manager.snapshot('task')))
        finally:
            manager.browser.close()

    def test_aigei_exact_item_and_fast_browser_not_blocked_by_silent_client(self):
        self.client(silent=True)
        media={'pagePath':'/item/rain.html','url':'https://s2.aigei.com/src/aud/mp3/rain.mp3','durationSeconds':150}
        self.client(media=media)
        started=time.monotonic()
        self.assertEqual(self.browser.aigei_media('/item/rain.html'),media)
        self.assertLess(time.monotonic()-started,1.5)
        self.assertFalse(self.browser.pending)
        self.assertTrue(all(j['op']=='aigei_media' for j in self.jobs))

    def test_aigei_cancel_and_invalid_item(self):
        self.client(silent=True)
        cancel=threading.Event();timer=threading.Timer(.15,cancel.set);timer.start()
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'取消'):self.browser.aigei_media('/item/rain.html',cancel)
        self.assertLess(time.monotonic()-started,1.5);self.assertFalse(self.browser.pending)
        for path in ['https://evil.test','/open/download','/item/../private.html']:
            with self.assertRaisesRegex(RuntimeError,'路径无效'):self.browser.aigei_media(path)

    def test_aigei_mismatched_reply_cannot_become_download_source(self):
        self.client(media={'pagePath':'/item/other.html','url':'https://s2.aigei.com/src/aud/mp3/other.mp3'})
        with self.assertRaisesRegex(RuntimeError,'目标资源'):self.browser.aigei_media('/item/rain.html')


if __name__ == '__main__':
    unittest.main(verbosity=2)
