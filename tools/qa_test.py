"""Cross-feature lifecycle regressions discovered during the multi-round QA audit."""
import testenv
import sys
import os
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from PySide6.QtCore import Qt
from app.agent import AgentServer
from app.models import SearchResult, SiteEvent
from access_test import connect
from paging_test import SelectedPagingTests


class WorkflowQATests(unittest.TestCase):
    def test_editing_text_during_local_intent_does_not_lock_search_button(self):
        self.win.search_edit.setText('old')
        release, entered = threading.Event(), threading.Event()
        def parse(text):
            entered.set(); release.wait(3)
            return {'keywords': ['old'], 'time': '7d'}
        try:
            with patch('app.ui.mainwindow.parse_intent', side_effect=parse), patch.object(self.win, '_start_search') as start:
                self.win._ai_search(); self.wait(entered.is_set)
                self.win.search_edit.setText('new')
                release.set(); self.wait(lambda: not self.win._ai_pending)
                self.assertTrue(self.win.search_btn.isEnabled())
                self.assertEqual(self.win.search_edit.text(), 'new')
                start.assert_not_called()
        finally: release.set()

    def test_clear_and_mode_switch_ignore_late_local_intent(self):
        for action in (lambda: self.win._clear_results(), lambda: self.win._set_mode('direct')):
            self.win.search_edit.setText('old')
            release, entered, emitted = threading.Event(), threading.Event(), threading.Event()
            def parse(text):
                entered.set(); release.wait(3); emitted.set()
                return {'keywords': ['stale']}
            try:
                with patch('app.ui.mainwindow.parse_intent', side_effect=parse), patch.object(self.win, '_start_search') as start:
                    self.win._ai_search(); self.wait(entered.is_set)
                    action(); self.win.search_edit.setText('new')
                    release.set(); self.wait(emitted.is_set)
                    self.app.processEvents()
                    self.assertFalse(self.win._ai_pending)
                    self.assertTrue(self.win.search_btn.isEnabled())
                    self.assertEqual(self.win.search_edit.text(), 'new')
                    start.assert_not_called()
            finally: release.set()

    setUpClass = classmethod(SelectedPagingTests.setUpClass.__func__)
    tearDownClass = classmethod(SelectedPagingTests.tearDownClass.__func__)
    setUp = SelectedPagingTests.setUp
    tearDown = SelectedPagingTests.tearDown
    seed = SelectedPagingTests.seed
    wait = SelectedPagingTests.wait
    wheel = SelectedPagingTests.wheel

    def toggle(self, site_id, enabled):
        for i in range(self.win.site_list.count()):
            item = self.win.site_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == site_id:
                item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
                return
        self.fail('missing site')

    def test_sidebar_change_cancels_old_page_and_rejects_late_callbacks(self):
        self.seed()
        with patch.object(self.win.engine, 'run'):
            self.win._load_more()
            generation, cancel = self.win._gen, self.win._cancel
            self.toggle('bili', False)
            self.assertTrue(cancel.is_set())
            self.assertFalse(self.win._paging)
            self.assertTrue(self.win.more_loading.isHidden())
            self.win._on_event(generation, SiteEvent(kind='done', site_id='bili', results=[
                SearchResult(title='late', url='https://www.bilibili.com/video/BVlate')]))
            self.assertEqual(len(self.win._all_results), 1)
            self.assertEqual(self.win._site_pages['bili'], 1)

    def test_sidebar_change_cancels_agent_batch_without_losing_cards(self):
        connect(self.server)
        self.win.settings.show_thumbnails = False
        self.win._handoff_to_agent('国内视频')
        task_id = self.win._agent_task_id
        self.win._append_results([SearchResult(title='keep', url='https://www.bilibili.com/video/BVkeep')])
        self.toggle('bili', False)
        self.assertFalse(self.server.accepts(task_id))
        self.assertFalse(self.win._agent_busy)
        self.assertEqual(len(self.win._all_results), 1)

    def test_sidebar_enable_clears_obsolete_end_notice(self):
        self.seed()
        self.win._source_all_clicked(True)
        for site in self.win.config.sites:
            self.toggle(site.id, site.id == 'bili')
        self.win._site_exhausted.add('bili')
        self.win._show_page_status()
        self.assertTrue(self.win._no_more)
        self.toggle('bili', False)
        self.toggle('ytb', True)
        self.assertFalse(self.win._no_more)
        self.assertTrue(self.win.pagination_notice.isHidden())
        with patch.object(self.win.engine, 'run', side_effect=lambda q, cb, c, **kw: cb(SiteEvent(kind='finish'))) as run:
            self.wheel()
            self.wait(lambda: run.called and not self.win._paging)
        self.assertEqual([s.id for s in run.call_args.kwargs['sites']], ['ytb'])

    def test_edit_exhausted_site_restarts_its_cursor_only(self):
        from dataclasses import replace
        self.seed()
        self.win._site_exhausted.add('bili')
        self.win._site_unavailable['bili'] = 'old endpoint'
        self.win._site_page_notes['bili'] = 'old note'
        self.win.site_list.setCurrentRow(0)
        self.assertEqual(self.win._selected_site().id, 'bili')
        updated = replace(self.win.config.get_site('bili'), max_results=12)
        with patch('app.ui.mainwindow.SiteDialog') as dialog:
            dialog.return_value.exec.return_value = True
            dialog.return_value.result_site = updated
            self.win._edit_site()
        self.assertNotIn('bili', self.win._site_pages)
        self.assertNotIn('bili', self.win._site_exhausted)
        self.assertNotIn('bili', self.win._site_unavailable)
        self.assertNotIn('bili', self.win._site_page_notes)
        self.assertEqual(self.win._site_pages['ytb'], 1)

    def test_clear_and_mode_change_cancel_pending_refresh(self):
        for action in (self.win._clear_results, lambda: self.win._set_mode('agent')):
            self.win._set_mode('direct')
            self.win._searching = True
            self.win._restart_after_stop = True
            action()
            self.assertFalse(self.win._restart_after_stop)

    def test_clear_after_finish_does_not_resurrect_scheduled_refresh(self):
        self.seed()
        self.win._searching = True
        self.win._restart_after_stop = True
        with patch.object(self.win, '_start_search') as start:
            self.win._finish_search()
            self.win._clear_results()
            self.app.processEvents()
            start.assert_not_called()

    def test_sidebar_change_interrupts_first_search_and_preserves_existing_cards(self):
        self.seed()
        self.win._searching = True
        self.win.search_edit.setEnabled(False)
        old, cancel = self.win._gen, self.win._cancel
        self.toggle('bili', False)
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.win._searching)
        self.assertTrue(self.win.search_edit.isEnabled())
        self.win._on_event(old, SiteEvent(kind='done', site_id='bili', results=[
            SearchResult(title='old', url='https://www.bilibili.com/video/BVold')]))
        self.assertEqual(len(self.win._all_results), 1)

    def test_site_count_and_error_decoration_does_not_cancel_or_save_search(self):
        self.seed()
        self.win._searching = True
        old, cancel = self.win._gen, self.win._cancel
        with patch.object(self.win, '_save_config') as save:
            self.win._mark_site('bili', 24, True)
            self.win._mark_site('ytb', 0, False, 'network timeout')
            self.assertTrue(self.win._searching)
            self.assertFalse(cancel.is_set())
            self.assertEqual(self.win._gen, old)
            save.assert_not_called()

    def test_new_cards_use_current_site_cover_referer_after_config_edit(self):
        self.seed()
        self.win._referers['bili'] = 'https://old.example/'
        self.win.config.get_site('bili').referer = 'https://new.example/'
        row = SearchResult(title='new cover', url='https://www.bilibili.com/video/BVnew',
                           source_id='bili', cover='https://image.example/new.jpg')
        self.win._append_results([row])
        card = next(c for c in self.win._cards if c.result.url == row.url)
        with patch.object(self.win.thumbs, 'cached', return_value=''), \
                patch.object(self.win.thumbs, 'request') as request:
            self.win._request_thumb(card, card._referer)
        self.assertEqual(request.call_args.kwargs['referer'], 'https://new.example/')

    def test_queued_thumbnail_callback_after_window_destroy_is_ignored(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from app.config import Config
        from app.ui.mainwindow import MainWindow
        popup = MainWindow(Config())
        popup.settings.show_thumbnails = True
        popup._append_results([SearchResult(title='pending', url='https://example.com/video',
                                           cover='https://example.com/image.png')])
        popup.close()
        popup.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        errors = []
        with patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args)):
            popup._pump_thumbs()
            self.app.processEvents()
        self.assertEqual(errors, [])

    def test_refresh_still_restarts_once_when_not_cancelled(self):
        self.seed()
        self.win._searching = True
        self.win._restart_after_stop = True
        with patch.object(self.win, '_start_search') as start:
            self.win._finish_search()
            self.app.processEvents()
            self.app.processEvents()
            start.assert_called_once()
        self.assertFalse(self.win._restart_after_stop)

    def test_repeated_mode_pause_disconnect_and_reconnect_cycles(self):
        for cycle in range(4):
            with self.subTest(cycle=cycle):
                self.win._set_mode('agent')
                connect(self.server)
                self.win._tick_agent_bar()
                self.win._handoff_to_agent('只搜 B站 猫 %d' % cycle)
                old_task = self.win._agent_task_id
                self.win._toggle_agent_pause()
                self.assertTrue(self.server.paused)
                self.assertFalse(self.server.accepts(old_task))
                self.assertFalse(self.win._agent_busy)
                self.assertTrue(self.win.more_loading.isHidden())
                self.win._toggle_agent_pause()
                self.assertFalse(self.server.paused)
                self.server.pop_task(0, 'access-test')
                self.win._tick_agent_bar()
                self.win._disconnect_agent()
                self.assertFalse(self.win.search_edit.isEnabled())
                self.assertFalse(self.server.owner)
                connect(self.server)
                self.win._tick_agent_bar()
                self.assertTrue(self.win.search_edit.isEnabled())
                self.win._set_mode('direct')
                self.assertFalse(self.win._agent_busy)
                self.assertTrue(self.win.search_edit.isEnabled())
                self.assertTrue(self.win.more_loading.isHidden())
                self.win._clear_results()

    def test_agent_status_readable_at_small_window_in_both_themes(self):
        connect(self.server, 'Codex QA long agent name')
        for palette in ('dark', 'light'):
            self.win.settings.theme = palette
            self.win._apply_theme()
            for width, height in ((1000, 660), (1280, 820)):
                self.win.resize(width, height)
                self.win._update_agent_bar()
                for _ in range(3):
                    self.app.processEvents()
                label = self.win.agent_state
                shots = os.environ.get('RF_QA_SHOTS')
                if shots:
                    Path(shots).mkdir(parents=True, exist_ok=True)
                    self.win.grab().save(str(Path(shots) / ('agent-%s-%d.png' % (palette, width))))
                with self.subTest(theme=palette, width=width):
                    self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()),
                                            'Agent status is vertically clipped')


class SettingsQATests(unittest.TestCase):
    setUpClass = classmethod(SelectedPagingTests.setUpClass.__func__)
    tearDownClass = classmethod(SelectedPagingTests.tearDownClass.__func__)
    wait = SelectedPagingTests.wait

    def setUp(self):
        from app.config import Settings
        from app.net import HttpClient
        from app.ui.dialogs import SettingsDialog
        self.settings = Settings(proxy_mode='direct')
        self.http = HttpClient(self.settings)
        self.dialog = SettingsDialog(self.settings, self.http)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        self.dialog.close()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.http.close()

    def test_network_uses_unsaved_values_without_blocking_or_saving(self):
        entered, release = threading.Event(), threading.Event()
        def probe(settings):
            self.assertEqual(settings.proxy_mode, 'manual')
            self.assertEqual(settings.proxy_url, 'http://127.0.0.1:9999')
            entered.set()
            release.wait(3)
            return {'domestic': True, 'abroad': False, 'note': 'test route'}
        self.dialog.proxy_combo.setCurrentIndex(self.dialog.proxy_combo.findData('manual'))
        self.dialog.proxy_edit.setText('http://127.0.0.1:9999')
        try:
            with patch('app.ui.dialogs.probe_network', side_effect=probe) as check:
                self.dialog.diag_btn.click()
                self.wait(entered.is_set)
                self.assertFalse(self.dialog.diag_btn.isEnabled())
                self.assertEqual(self.settings.proxy_mode, 'direct')
                self.dialog._refresh_net()
                self.assertEqual(check.call_count, 1)
                release.set()
                self.wait(lambda: self.dialog.diag_btn.isEnabled())
            self.assertIn('国内 可用 · 境外 不可用', self.dialog.diag_label.text())
            self.dialog.reject()
            self.assertEqual(self.settings.proxy_mode, 'direct')
        finally:
            release.set()

    def test_old_network_result_cannot_overwrite_changed_proxy_and_errors_retry(self):
        entered, release = threading.Event(), threading.Event()
        def probe(settings):
            entered.set()
            release.wait(3)
            return {'domestic': True, 'abroad': True}
        try:
            with patch('app.ui.dialogs.probe_network', side_effect=probe):
                self.dialog.diag_btn.click()
                self.wait(entered.is_set)
                self.dialog.proxy_combo.setCurrentIndex(self.dialog.proxy_combo.findData('manual'))
                release.set()
                self.wait(lambda: self.dialog.diag_btn.isEnabled())
            self.assertIn('配置已更改', self.dialog.diag_label.text())
            with patch('app.ui.dialogs.probe_network', side_effect=RuntimeError('probe failed')):
                self.dialog.diag_btn.click()
                self.wait(lambda: self.dialog.diag_btn.isEnabled())
            self.assertIn('probe failed', self.dialog.diag_label.text())
        finally:
            release.set()


class ServerShutdownQATests(unittest.TestCase):
    def test_close_listener_wakes_poll_with_terminal_stop_and_revokes_credentials(self):
        server = AgentServer(lambda *a: None, port=0)
        server.start()
        token, replies, failures = server.token, [], []
        def poll():
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    replies.append(session.get(server.url + '/api/task', params={
                        'token': token, 'agent': 'qa-close', 'wait': 20}, timeout=3).json())
            except Exception as exc:
                failures.append(exc)
        worker = threading.Thread(target=poll, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while not server._waiting_polls.get('qa-close') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(server._waiting_polls.get('qa-close'))
            started = time.monotonic()
            server.stop()
            worker.join(1)
            self.assertFalse(worker.is_alive(), 'closing inbox left long poll running')
            self.assertFalse(failures)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(replies[0]['stop'])
            self.assertEqual(replies[0]['next'], 'stop')
            self.assertEqual(replies[0]['task']['kind'], 'stop')
            self.assertNotEqual(server.token, token)
            self.assertTrue(server.start())
            with requests.Session() as session:
                session.trust_env = False
                reply = session.get(server.url + '/api/task', params={
                    'token': token, 'agent': 'qa-close', 'wait': 0}, timeout=2).json()
            self.assertTrue(reply['stop'])
            self.assertFalse(server.owner)
        finally:
            server.disconnect()
            server.stop()
            worker.join(4)


del SelectedPagingTests

if __name__ == '__main__':
    unittest.main(verbosity=2)
