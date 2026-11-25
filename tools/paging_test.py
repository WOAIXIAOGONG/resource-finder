"""Source-scoped continuation and regression against real settings pollution."""
import testenv
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import SearchResult, SiteEvent
from app.config import config_dir
from source_test import SourceUITests
from media_test import CandidateTests
from access_test import connect


class SelectedPagingTests(unittest.TestCase):
    def test_bottom_loading_visible_until_finish_without_extra_requests(self):
        import threading
        release, entered = threading.Event(), threading.Event()
        self.seed()
        def run(q, cb, c, **kw):
            entered.set()
            release.wait(5)
            cb(SiteEvent(kind='finish'))
        try:
            with patch.object(self.win.engine, 'run', side_effect=run) as search:
                self.wheel()
                self.wait(entered.is_set)
                self.assertTrue(self.win.more_loading.isVisible())
                self.assertTrue(self.win.more_spinner._timer.isActive())
                self.assertEqual(self.win.more_label.text(), '正在加载更多资源…')
                self.assertEqual(len(self.win._cards), 1)
                self.app.processEvents()
                self.assertEqual(search.call_count, 1)
                release.set()
                self.wait(lambda: not self.win._paging)
                self.assertTrue(self.win.more_loading.isHidden())
                self.assertFalse(self.win.more_spinner._timer.isActive())
                self.assertEqual(search.call_count, 1)
        finally:
            release.set()

    def test_loading_stops_on_source_change_fatal_and_clear(self):
        self.seed()
        with patch.object(self.win.engine, 'run'):
            self.win._load_more()
            self.assertFalse(self.win.more_loading.isHidden())
            self.win._source_all_clicked(False)
            self.assertTrue(self.win.more_loading.isHidden())
            self.win._source_checks['platform:bilibili'].click()
            self.win._load_more()
            self.win._on_paging_event(SiteEvent(kind='fatal', error='timeout'))
            self.assertTrue(self.win.more_loading.isHidden())
            self.assertFalse(self.win.more_spinner._timer.isActive())
            self.assertIn('再次触底', self.win.pagination_notice.text())
            self.win._load_more()
            self.win._clear_results(keep_empty=True)
            self.assertTrue(self.win.more_loading.isHidden())
            self.assertFalse(self.win.more_spinner._timer.isActive())

    setUpClass = classmethod(SourceUITests.setUpClass.__func__)
    tearDownClass = classmethod(SourceUITests.tearDownClass.__func__)
    setUp = SourceUITests.setUp
    tearDown = SourceUITests.tearDown

    def seed(self):
        self.win._set_mode('direct')
        self.win.settings.show_thumbnails = False
        self.win._append_results([SearchResult(title='first', url='https://www.bilibili.com/video/BV1', source_id='bili')])
        self.win._query = '宵宫'
        self.win._site_pages = {'bili': 1, 'ytb': 1}
        self.win._source_all_clicked(False)
        self.win._source_checks['platform:bilibili'].click()

    def wheel(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, -20), QPoint(),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
        self.app.sendEvent(self.win.scroll.viewport(), event)

    def wait(self, predicate):
        until = time.monotonic()+3
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('event did not complete')

    def test_short_list_pixel_wheel_searches_only_selected_and_keeps_independent_pages(self):
        self.seed()
        calls = []
        def run(query, callback, cancel, sites=None, page=1, pages=None):
            calls.append(([s.id for s in sites], dict(pages)))
            for site in sites:
                callback(SiteEvent(kind='done', site_id=site.id, results=[SearchResult(title='next',
                    url=('https://www.bilibili.com/video/BV%d' if site.id == 'bili' else 'https://www.youtube.com/watch?v=abcdefghij%d') % pages[site.id])]))
            callback(SiteEvent(kind='finish'))
        with patch.object(self.win.engine, 'run', side_effect=run):
            self.wheel()
            self.wait(lambda: len(calls) == 1 and not self.win._paging)
            self.assertEqual(calls[0], (['bili'], {'bili': 2}))
            self.win._source_all_clicked(False)
            self.win._source_checks['platform:youtube'].click()
            self.wheel()
            self.wait(lambda: len(calls) == 2 and not self.win._paging)
        self.assertEqual(calls[1], (['ytb'], {'ytb': 2}))
        self.assertEqual(self.win._site_pages, {'bili': 2, 'ytb': 2})
        self.assertEqual(len(self.win.config.enabled_sites()), 4)
        self.assertIn('共 1 个资源', self.win.result_count.text())

    def test_exhausted_platform_does_not_block_other_platform(self):
        self.seed()
        self.win._site_exhausted.add('bili')
        self.win._load_more()
        self.assertTrue(self.win._no_more)
        self.win._source_all_clicked(False)
        self.win._source_checks['platform:youtube'].click()
        self.assertFalse(self.win._no_more)
        with patch.object(self.win.engine, 'run', side_effect=lambda q, cb, c, **kw: cb(SiteEvent(kind='finish'))) as run:
            self.wheel()
            self.wait(lambda: run.called and not self.win._paging)
        self.assertEqual([s.id for s in run.call_args.kwargs['sites']], ['ytb'])

    def test_no_selection_and_layout_changes_do_not_request_pages(self):
        self.seed()
        self.win._source_all_clicked(False)
        with patch.object(self.win.engine, 'run') as run:
            self.win._on_scroll()
            self.wheel()
            for _ in range(4):
                self.app.processEvents(); time.sleep(.01)
            run.assert_not_called()

    def test_failed_site_retries_same_page(self):
        self.seed()
        def fail(q, cb, c, **kw):
            cb(SiteEvent(kind='error', site_id='bili', error='temporary'))
            cb(SiteEvent(kind='finish'))
        with patch.object(self.win.engine, 'run', side_effect=fail) as run:
            self.wheel()
            self.wait(lambda: run.call_count == 1 and not self.win._paging)
            self.wheel()
            self.wait(lambda: run.call_count == 2 and not self.win._paging)
        self.assertEqual([c.kwargs['pages'] for c in run.call_args_list], [{'bili': 2}, {'bili': 2}])

    def test_cover_toggle_restores_requests_and_config_is_isolated(self):
        self.seed()
        self.assertTrue(Path(config_dir()).is_relative_to(Path(os.environ['RESOURCE_FINDER_HOME'])))
        self.assertTrue(self.win.cover_notice.isVisible())
        with patch.object(self.win.thumbs, 'request_video') as request:
            self.win.cover_notice.click()
            request.assert_called_once()
        self.assertTrue(self.win.settings.show_thumbnails)
        self.assertFalse(self.win.cover_notice.isVisible())

    def test_scope_change_cancels_paging_and_rejects_late_results(self):
        self.seed()
        old_gen = self.win._gen
        old_cancel = self.win._cancel
        self.win._paging = True
        self.win._source_all_clicked(False)
        self.assertTrue(old_cancel.is_set())
        self.assertFalse(self.win._paging)
        self.win._on_event(old_gen, SiteEvent(kind='done', site_id='bili', results=[
            SearchResult(title='late', url='https://www.bilibili.com/video/BV999')]))
        self.assertEqual(len(self.win._all_results), 1)
        self.assertEqual(self.win._site_pages['bili'], 1)

    def test_agent_more_contains_selection_and_changing_scope_cancels_old_work(self):
        self.win.settings.show_thumbnails = False
        connect(self.server)
        self.win._tick_agent_bar()
        self.win._handoff_to_agent('国内视频')
        old_id = self.win._agent_task_id
        self.win._source_all_clicked(False)
        self.assertFalse(self.server.accepts(old_id))
        self.win._source_checks['platform:bilibili'].click()
        self.wheel()
        self.wait(lambda: self.win._agent_busy)
        task = self.server.current_task
        self.assertEqual(task['filters']['source_keys'], ['platform:bilibili'])
        self.assertEqual([s.id for s in self.server.access.sources()], ['bili'])


class ScopedAgentTests(unittest.TestCase):
    setUp = CandidateTests.setUp
    tearDown = CandidateTests.tearDown
    get = CandidateTests.get
    post = CandidateTests.post
    ready = CandidateTests.ready

    def test_out_of_scope_results_and_network_search_are_rejected(self):
        self.ready()
        task = self.server.push_task('视频', {'source_keys': ['platform:bilibili']})
        reply = self.post('results', task_id=task['id'], results=[
            {'title':'b', 'url':'https://www.bilibili.com/video/BV1'},
            {'title':'y', 'url':'https://www.youtube.com/watch?v=abcdefghijk'}], done=False)
        self.assertEqual(reply.json()['accepted'], 1)
        self.assertEqual(self.server.task_status()['received'], 1)
        reply = self.post('network/search', task_id=task['id'], source_id='ytb', query='test')
        self.assertNotEqual(reply.status_code, 200)
        self.assertEqual(self.post('network/fetch', task_id=task['id'], url='https://www.youtube.com/').status_code, 400)


del SourceUITests, CandidateTests

if __name__ == '__main__':
    unittest.main(verbosity=2)
