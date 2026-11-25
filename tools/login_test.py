"""Login notices, per-platform continuation, and a cancellable 180-second budget."""
import testenv
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from access_test import AccessTests, AccessUITests, connect
from app.login import LoginRequired, login_manager, youtube_restriction
from app.config import Config, Settings
from app.net import HttpClient, NetworkError
from app.adapters.youtube import YouTubeAdapter
from app.adapters.base import SearchContext


AGE = {'backgroundPromoRenderer': {'title': {'simpleText': 'Confirm your age'},
       'bodyText': {'simpleText': 'These results may be inappropriate for some users.'}}}
VIDEO = {'videoRenderer': {'videoId': 'testvideo01', 'title': {'simpleText': 'Test video'}}}


def document(item):
    return {'contents': {'twoColumnSearchResultsRenderer': {'primaryContents': {
        'sectionListRenderer': {'contents': [{'itemSectionRenderer': {'contents': [item]}}]}}}}}


def html(item):
    return '<script>var ytInitialData = ' + json.dumps(document(item)) + ';</script>'


class LoginTests(unittest.TestCase):
    def test_explicit_age_notice_not_video_title(self):
        with self.assertRaises(LoginRequired):
            youtube_restriction(document(AGE))
        youtube_restriction({'videoRenderer': {'title': {'simpleText': 'Confirm your age'}}})

    def test_adapter_raises_login_instead_of_network_error(self):
        cfg = Config()
        yt = next(s for s in cfg.sites if s.builtin == 'youtube')
        with self.assertRaises(LoginRequired):
            YouTubeAdapter().search(SearchContext('test', SimpleNamespace(get_text=lambda *a: html(AGE))), yt)

    def test_browser_data_and_continuation_reuse_same_session(self):
        cfg = Config()
        browser = SimpleNamespace(active=True, search_page=lambda *a: (document(VIDEO), {}))
        cfg.settings._login_browser = browser
        client = HttpClient(cfg.settings)
        yt = next(s for s in cfg.sites if s.builtin == 'youtube')
        with patch.object(client, 'get_text', side_effect=AssertionError('anonymous request')):
            rows = YouTubeAdapter().search(SearchContext('test', client), yt)
        self.assertEqual(len(rows), 1)

    def test_actual_page_verification_not_button_alone(self):
        cfg = Config(); manager = login_manager(cfg)
        yt = next(s for s in cfg.sites if s.builtin == 'youtube')
        manager.require('test', yt, 'query', 1, 'age', threading.Event())
        manager.browser.active = True
        with patch.object(manager.browser, 'search_page', return_value=(document(AGE), {})):
            manager.verify('test', yt.id)
        self.assertEqual(manager.snapshot('test')[yt.id]['state'], 'waiting')
        with patch.object(manager.browser, 'search_page', return_value=(document(VIDEO), {})):
            manager.verify('test', yt.id)
        self.assertEqual(manager.snapshot('test')[yt.id]['state'], 'ready')

    def test_skip_during_verification_cannot_be_undone_by_late_success(self):
        cfg = Config(); manager = login_manager(cfg)
        yt = next(s for s in cfg.sites if s.builtin == 'youtube')
        manager.require('test', yt, 'query', 1, 'age', threading.Event())
        manager.browser.active = True
        def verify(*args):
            manager.skip('test', yt.id)
            return document(VIDEO), {}
        with patch.object(manager.browser, 'search_page', side_effect=verify):
            manager.verify('test', yt.id)
        self.assertEqual(manager.snapshot('test')[yt.id]['state'], 'skipped')

    def test_skip_aborts_only_verification_not_other_platforms(self):
        cfg = Config(); manager = login_manager(cfg)
        yt = next(s for s in cfg.sites if s.builtin == 'youtube')
        task_cancel = threading.Event()
        manager.require('test', yt, 'query', 1, 'age', task_cancel)
        manager.browser.active = True
        def verify(query, cancel):
            manager.skip('test', yt.id)
            self.assertTrue(cancel.is_set())
            self.assertFalse(task_cancel.is_set())
            raise RuntimeError('cancelled')
        with patch.object(manager.browser, 'search_page', side_effect=verify):
            manager.verify('test', yt.id)
        self.assertEqual(manager.snapshot('test')[yt.id]['state'], 'skipped')

    def test_cancelled_login_cannot_notify_or_resume(self):
        cfg = Config(); manager = login_manager(cfg)
        yt = next(s for s in cfg.sites if s.builtin == 'youtube')
        event = threading.Event(); event.set()
        with patch.object(manager, 'notify') as notify:
            manager.require('test', yt, 'query', 1, 'age', event)
            manager.verify('test', yt.id)
            self.assertFalse(notify.called)
        self.assertEqual(manager.snapshot('test'), {})

    def test_timeout_migration_preserves_deliberate_custom_values(self):
        self.assertEqual(Settings().timeout, 180)
        self.assertEqual(Settings.from_dict({'timeout': 12}).timeout, 180)
        self.assertEqual(Settings.from_dict({'timeout': 25}).timeout, 25)
        self.assertEqual(Settings.from_dict({'timeout': 12, 'timeout_revision': 1}).timeout, 12)
        self.assertEqual(Settings.from_dict({'timeout': 1000}).timeout, 600)

    def test_long_request_cancel_returns_promptly(self):
        client = HttpClient(Settings())
        cancel = threading.Event(); release = threading.Event()
        client.cancel_event = cancel
        outcome = []
        response = SimpleNamespace(close=lambda: outcome.append('closed'))
        def slow(*args, **kwargs):
            release.wait(2)
            return response
        timer = threading.Timer(.15, cancel.set); timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(NetworkError):
                client._request_wait(slow, cancel, time.monotonic() + 180)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set(); timer.join()


class LoginServerTests(unittest.TestCase):
    setUp = AccessTests.setUp
    tearDown = AccessTests.tearDown
    get = AccessTests.get
    post = AccessTests.post
    ready = AccessTests.ready
    task = AccessTests.task
    report = AccessTests.report
    successes = AccessTests.successes

    def test_age_gate_returns_structured_state_other_source_keeps_working(self):
        task = self.task(); self.report(task, self.successes(task))
        yt = next(s for s in self.cfg.sites if s.builtin == 'youtube')
        with patch.object(HttpClient, 'get_text', return_value=html(AGE)):
            reply = self.post('network/search', task_id=task['id'], source_id=yt.id, query='test')
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json()['login_required'])
        self.assertEqual(reply.json()['code'], 'age_verification_required')
        self.assertTrue(self.server.accepts(task['id']))
        self.assertFalse(self.server.cancellation(task['id']).is_set())
        control = self.get('control', task_id=task['id']).json()
        self.assertEqual(control['login'][yt.id]['state'], 'waiting')
        self.assertEqual(self.post('results', task_id=task['id'], results=[], done=True).json()['done'], False)
        self.server.access.logins.skip(task['id'], yt.id)
        self.assertTrue(self.post('network/search', task_id=task['id'], source_id=yt.id, query='test').json()['skipped'])
        self.assertTrue(self.post('results', task_id=task['id'], results=[], done=True).json()['done'])

    def test_inflight_long_search_counts_as_connected_but_cancel_does_not(self):
        task = self.task()
        self.server._poll_times[self.server.owner] = time.time() - 140
        self.assertFalse(self.server.connected())
        self.server._inflight_network[(task['id'], 1)] = time.monotonic() + 40
        self.assertTrue(self.server.connected())
        self.server.clear_tasks()
        self.assertFalse(self.server.connected())

    def test_skipped_platform_is_carried_to_more_and_rejected_before_count(self):
        task = self.task(); self.report(task, self.successes(task))
        yt = next(s for s in self.cfg.sites if s.builtin == 'youtube')
        self.post('network/login', task_id=task['id'], source_id=yt.id, code='login_required')
        self.server.access.logins.skip(task['id'], yt.id)
        reply = self.post('results', task_id=task['id'], results=[{'title':'late', 'url':'https://www.youtube.com/watch?v=testvideo01'}])
        self.assertEqual(reply.json()['accepted'], 0)
        new = self.server.push_task(task['query'], kind='more')
        self.assertEqual(self.server.access.logins.snapshot(new['id'])[yt.id]['state'], 'skipped')

    def test_agent_can_report_login_but_cannot_choose_arbitrary_login_url(self):
        task = self.task()
        reply = self.post('network/login', task_id=task['id'], source_id='missing', code='login_required', url='http://localhost')
        self.assertEqual(reply.status_code, 400)
        yt = next(s for s in self.cfg.sites if s.builtin == 'youtube')
        reply = self.post('network/login', task_id=task['id'], source_id=yt.id, code='login_required')
        self.assertTrue(reply.json()['login_required'])
        self.server.disconnect()
        self.assertEqual(self.server.access.logins.snapshot(task['id']), {})


class LoginUITests(unittest.TestCase):
    setUpClass = classmethod(AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(AccessUITests.tearDownClass.__func__)
    setUp = AccessUITests.setUp
    tearDown = AccessUITests.tearDown

    def request(self):
        connect(self.server)
        self.win._handoff_to_agent('YouTube test')
        task = self.server.pop_task(0, 'access-test')
        yt = next(s for s in self.win.config.sites if s.builtin == 'youtube')
        with patch.object(self.win.logins, 'open'):
            row = self.win.logins.require(task['id'], yt, 'test', 1, 'Confirm your age', self.server.cancellation(task['id']))
            self.app.processEvents()
            time.sleep(.02)
            self.app.processEvents()
        return row, self.win._login_dialogs[(task['id'], yt.id)]

    def test_dialog_has_verify_skip_and_login_launcher_is_modeless(self):
        from PySide6.QtWidgets import QPushButton
        row, dlg = self.request()
        self.assertCountEqual([b.text() for b in dlg.findChildren(QPushButton)], ['已登录，继续搜索', '本轮跳过该平台', '打开专用登录窗口'])
        self.assertFalse(dlg.isModal())
        dlg.close(); self.app.processEvents()
        self.assertEqual(self.win.logins.snapshot(row['scope'])[row['source_id']]['state'], 'skipped')
        self.assertFalse(self.server.cancellation(row['scope']).is_set())

    def test_waiting_login_does_not_cancel_whole_task_on_silence(self):
        row, dlg = self.request()
        self.win._agent_busy_timeout()
        self.assertTrue(self.win._agent_busy)
        self.assertFalse(self.server.cancellation(row['scope']).is_set())

    def test_new_query_dismisses_old_dialog(self):
        row, dlg = self.request()
        self.win._handoff_to_agent('replacement')
        self.app.processEvents()
        self.assertFalse(dlg.isVisible())
        self.assertFalse(self.win._login_dialogs)

    def test_direct_login_retry_keeps_other_platform_results(self):
        from app.models import SearchResult
        self.win._set_mode('direct')
        self.win._query = 'nature'
        self.win._cancel = threading.Event()
        self.win.settings.show_thumbnails = False
        self.win._append_results([SearchResult(title='kept', url='https://bilibili.com/video/BVtest', source_id='bili')])
        yt = next(s for s in self.win.config.sites if s.builtin == 'youtube')
        scope = 'direct:' + str(id(self.win._cancel))
        with patch.object(self.win.logins, 'open'):
            self.win.logins.require(scope, yt, 'nature', 1, 'age', self.win._cancel)
            self.app.processEvents(); time.sleep(.02); self.app.processEvents()
        self.assertNotIn(yt, self.win._selected_search_sites())
        self.win.logins.browser.active = True
        with patch.object(self.win.logins.browser, 'search_page', return_value=(document(VIDEO), {})):
            self.win.logins.verify(scope, yt.id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.02)
                if len(self.win._all_results) == 2 and not self.win._paging:
                    break
        self.assertEqual(len(self.win._all_results), 2)
        self.assertEqual(self.win._all_results[0].title, 'kept')
        self.assertEqual(self.win._site_pages[yt.id], 1)


del AccessTests, AccessUITests

if __name__ == '__main__':
    unittest.main(verbosity=2)
