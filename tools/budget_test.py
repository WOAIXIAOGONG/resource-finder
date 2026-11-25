"""Agent budgets: server caps and user-triggered versus automatic continuation."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import access_test as access
from app.config import Settings


def rows(start, count):
    return [{'title': 'Video %d' % n, 'url': 'https://example.com/video/%d' % n}
            for n in range(start, start + count)]


class BudgetServerTests(unittest.TestCase):
    setUp = access.AccessTests.setUp
    tearDown = access.AccessTests.tearDown
    get = access.AccessTests.get
    post = access.AccessTests.post
    ready = access.AccessTests.ready
    task = access.AccessTests.task

    def test_oversized_batch_is_trimmed_and_cancels_without_done(self):
        task = self.task('国内视频')
        self.assertEqual(task['want'], 100)
        event = self.server.cancellation(task['id'])
        reply = self.post('results', task_id=task['id'], results=rows(0, 125), done=False).json()
        self.assertEqual(reply['accepted'], 100)
        self.assertTrue(reply['limit_reached'] and reply['done'] and event.is_set())
        self.assertEqual(len(self.rows[0][0]), 100)
        self.assertEqual(self.server.task_status()['state'], 'completed')
        late = self.post('results', task_id=task['id'], results=rows(125, 100), done=False).json()
        self.assertEqual(late['accepted'], 0)
        self.assertEqual(len(self.rows), 1)
        self.post('error', task_id=task['id'], error='late failure')
        self.assertFalse(self.errors)

    def test_stream_duplicates_invalid_urls_and_exclusions_do_not_consume_extra(self):
        self.ready()
        task = self.server.push_task('国内视频', kind='more', want=3, exclude=[rows(0, 1)[0]['url']])
        self.get()
        reply = self.post('results', task_id=task['id'], results=rows(0, 2)*2 + [{'title':'x','url':'file:///no'}]).json()
        self.assertEqual(reply['received'], 1)
        reply = self.post('results', task_id=task['id'], results=rows(1, 5), done=False).json()
        self.assertEqual(reply['received'], 3)
        self.assertEqual(reply['accepted'], 2)
        self.assertTrue(reply['limit_reached'])

    def test_retry_preserves_remaining_budget_and_exclusions(self):
        task = self.task('国内视频')
        self.post('results', task_id=task['id'], results=rows(0, 40), done=False)
        new = self.server.retry_task()
        self.assertEqual(new['want'], 60)
        reply = self.post('results', task_id=new['id'], results=rows(0, 110)).json()
        self.assertEqual(reply['accepted'], 60)
        self.assertTrue(reply['limit_reached'])
        self.assertIsNone(self.server.retry_task())

    def test_server_never_accepts_more_than_thousand_for_one_task(self):
        self.ready()
        task = self.server.push_task('国内视频', want=100000)
        self.assertEqual(task['want'], 1000)
        reply = self.post('results', task_id=task['id'], results=rows(0, 1100)).json()
        self.assertEqual(reply['accepted'], 1000)

    def test_settings_default_validation_and_round_trip(self):
        self.assertEqual(Settings.from_dict({}).agent_search_mode, 'limited')
        self.assertEqual(Settings.from_dict({'agent_search_mode': 'bad'}).agent_search_mode, 'limited')
        setting = Settings(agent_search_mode='unlimited')
        self.assertEqual(Settings.from_dict(setting.to_dict()).agent_search_mode, 'unlimited')


class BudgetUITests(unittest.TestCase):
    def test_bounded_search_keeps_results_stops_refill_and_requires_new_scroll(self):
        task=self.begin()
        self.deliver(task,0,16,False)
        self.pump()
        bar=self.win.scroll.verticalScrollBar();bar.setValue(bar.maximum())
        self.wheel()
        self.assertTrue(self.win._agent_scroll_pending)
        self.win._on_agent_results(dict(results=[], query='', meta=dict(task_id=task['id'],done=True,
            received=16,search_limited=True,exhausted=False,message='bounded')))
        self.pump()
        self.assertEqual(len(self.win._all_results),16)
        self.assertFalse(self.win._agent_busy)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertFalse(self.win._agent_scroll_pending)
        self.assertIn('不代表网站没有更多资源',self.win.pagination_notice.text())
        self.assertIn('本轮已停止',self.win.agent_budget_label.text())
        self.assertNotIn('继续补足本批',self.win.agent_budget_label.text())
        self.assertEqual(self.server.current_task['id'],task['id'])
        self.wheel()
        next_task=self.server.pop_task(0,'access-test')
        self.assertIsNotNone(next_task);self.assertEqual(next_task['want'],84)

    setUpClass = classmethod(access.AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(access.AccessUITests.tearDownClass.__func__)
    setUp = access.AccessUITests.setUp
    tearDown = access.AccessUITests.tearDown

    def pump(self):
        for _ in range(3):
            self.app.processEvents()
            time.sleep(.005)

    def wheel(self, angle=-120, pixels=0, target=None):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        viewport = target if target is not None else self.win.scroll.viewport()
        point = viewport.rect().center()
        event = QWheelEvent(QPointF(point), QPointF(viewport.mapToGlobal(point)),
                            QPoint(0, pixels), QPoint(0, angle), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
        self.app.sendEvent(viewport, event)
        self.pump()

    def begin(self, unlimited=False):
        access.connect(self.server)
        self.win._tick_agent_bar()
        if unlimited:
            self.win.agent_budget_combo.setCurrentIndex(1)
        self.win._handoff_to_agent('国内视频')
        return self.server.pop_task(0, 'access-test')

    def deliver(self, task, start, count, done=False):
        import requests
        with requests.Session() as http:
            http.trust_env = False
            response = http.post(self.server.endpoint(), json={'agent':'access-test', 'task_id':task['id'],
                'results': rows(start, count), 'done': done}, timeout=4)
        self.app.processEvents()
        return response.json()

    def test_limited_stops_after_batch_and_real_end_key_grants_next_hundred(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        task = self.begin()
        self.deliver(task, 0, 120)
        self.assertEqual(len(self.win._all_results), 100)
        self.assertFalse(self.win._agent_busy or self.win._auto_more_timer.isActive())
        self.win._auto_agent_more()
        bar = self.win.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())  # Programmatic changes are not user permission.
        self.win._maybe_load_more(); self.app.processEvents()
        self.assertEqual(self.server.current_task['id'], task['id'])
        QTest.keyClick(self.win.scroll.viewport(), Qt.Key.Key_End)
        self.app.processEvents()
        new = self.server.pop_task(0, 'access-test')
        self.assertIsNotNone(new)
        self.assertEqual(new['want'], 100)
        self.deliver(new, 100, 120)
        self.assertEqual(len(self.win._all_results), 200)
        self.assertFalse(self.win._auto_more_timer.isActive())

    def test_full_limited_filtered_batch_waits_and_wheel_works_without_scrollbar(self):
        task = self.begin()
        self.deliver(task, 0, 100, True)
        self.win._all_results[0].views = 1000000000
        self.win.views_combo.setCurrentIndex(1)  # One visible card from a complete batch.
        self.pump()
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertFalse(hasattr(self.win, 'agent_load_btn'))
        self.assertEqual(self.win.scroll.verticalScrollBar().maximum(), 0)
        self.wheel()
        new = self.server.pop_task(0, 'access-test')
        self.assertEqual(new['want'], 100)
        self.assertEqual(self.win._agent_grant_end, 200)

    def test_more_indicator_tracks_queue_work_silence_finish_and_pause(self):
        task = self.begin()
        self.assertTrue(self.win.more_loading.isHidden())
        self.deliver(task, 0, 1, True)
        self.wheel()
        self.assertTrue(self.win.more_loading.isVisible())
        self.assertIn('等待 Agent 领取', self.win.more_label.text())
        more = self.server.pop_task(0, 'access-test')
        self.server.acknowledge(more['id'])
        self.app.processEvents()
        self.win._tick_agent_bar()
        self.assertEqual(self.win.more_label.text(), '正在加载更多资源…')
        self.assertTrue(self.win.more_spinner._timer.isActive())
        self.server._poll_times['access-test'] = time.time() - 91
        self.win._tick_agent_bar()
        self.assertIn('任务已保留', self.win.more_label.text())
        self.assertFalse(self.win.more_spinner._timer.isActive())
        self.deliver(more, 1, 99, True)
        self.assertTrue(self.win.more_loading.isHidden())
        self.assertFalse(self.win.more_spinner._timer.isActive())
        self.win.scroll.verticalScrollBar().setValue(self.win.scroll.verticalScrollBar().maximum())
        self.wheel()
        self.assertFalse(self.win.more_loading.isHidden())
        self.win._toggle_agent_pause()
        self.assertTrue(self.win.more_loading.isHidden())
        self.assertFalse(self.win.more_spinner._timer.isActive())

    def test_pixel_only_touchpad_triggers_at_bottom(self):
        task = self.begin()
        self.deliver(task, 0, 1, True)
        self.wheel(angle=0, pixels=-25)
        new = self.server.pop_task(0, 'access-test')
        self.assertIsNotNone(new)
        self.assertEqual(new['want'], 99)  # Finish the current batch before granting another.

    def test_wheel_over_video_card_reaches_scroll_trigger(self):
        task = self.begin()
        self.deliver(task, 0, 1, True)
        self.wheel(target=self.win._cards[0])
        self.assertIsNotNone(self.server.pop_task(0, 'access-test'))

    def test_slider_action_checks_position_after_qt_updates_it(self):
        from PySide6.QtWidgets import QAbstractSlider
        task = self.begin()
        self.deliver(task, 0, 100)
        bar = self.win.scroll.verticalScrollBar()
        self.pump()
        bar.setValue(0)
        self.assertGreater(bar.maximum(), 100)
        # actionTriggered is emitted while value() still points to the old position.
        bar.triggerAction(QAbstractSlider.SliderAction.SliderToMaximum)
        self.pump()
        new = self.server.pop_task(0, 'access-test')
        self.assertIsNotNone(new)
        self.assertEqual(new['want'], 100)

    def test_drag_to_bottom_triggers_once(self):
        task = self.begin()
        self.deliver(task, 0, 100)
        self.pump()
        bar = self.win.scroll.verticalScrollBar()
        bar.setValue(0)
        bar.setSliderDown(True)
        bar.setSliderPosition(bar.maximum())
        bar.setSliderDown(False)
        self.pump()
        self.assertEqual(self.server.current_task['kind'], 'more')
        self.assertEqual(self.server._task_seq, 2)

    def test_bottom_during_stream_waits_then_grants_exactly_one_batch(self):
        task = self.begin()
        self.deliver(task, 0, 1, False)
        for _ in range(3):
            self.wheel()
        self.assertTrue(self.win._agent_scroll_pending)
        self.assertEqual(self.server.current_task['id'], task['id'])
        self.deliver(task, 0, 0, True)
        self.pump()
        new = self.server.pop_task(0, 'access-test')
        self.assertIsNotNone(new)
        self.assertEqual(self.server._task_seq, 2)
        self.deliver(new, 1, 1, True)
        self.pump()
        self.assertFalse(self.win._agent_busy)
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_upward_scroll_cancels_pending_bottom_request(self):
        task = self.begin()
        self.deliver(task, 0, 1, False)
        self.wheel()
        self.wheel(angle=120)
        self.deliver(task, 0, 0, True)
        self.pump()
        self.assertFalse(self.win._agent_scroll_pending)
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_new_cards_move_bottom_away_and_cancel_pending_request(self):
        task = self.begin()
        self.deliver(task, 0, 1, False)
        self.wheel()
        self.deliver(task, 1, 60, False)
        self.pump()
        self.assertGreater(self.win.scroll.verticalScrollBar().maximum(), 100)
        self.deliver(task, 0, 0, True)
        self.pump()
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_pending_scroll_never_leaks_to_replacement_query(self):
        task = self.begin()
        self.deliver(task, 0, 1, False)
        self.wheel()
        self.win._handoff_to_agent('另一个国内关键词')
        new = self.server.pop_task(0, 'access-test')
        self.deliver(new, 100, 1, True)
        self.pump()
        self.assertIsNone(self.server.pop_task(0, 'access-test'))
        self.assertFalse(self.win._agent_scroll_pending)

    def test_scroll_away_from_bottom_does_not_load(self):
        task = self.begin()
        self.deliver(task, 0, 100)
        self.pump()
        self.win.scroll.verticalScrollBar().setValue(0)
        self.wheel()
        self.assertEqual(self.server.current_task['id'], task['id'])
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_failed_batch_cancels_pending_scroll_instead_of_retrying(self):
        task = self.begin()
        self.deliver(task, 0, 1, False)
        self.wheel()
        self.win._on_agent_error({'task_id': task['id'], 'error': 'transport failed'})
        self.pump()
        self.assertFalse(self.win._agent_scroll_pending)
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_unlimited_stops_at_thousand_including_scroll_and_retry(self):
        task = self.begin(True)
        # Exercise real HTTP/accounting while avoiding 1000 cover widgets in this loop.
        with patch.object(self.win, '_append_results', side_effect=lambda fresh: self.win._all_results.extend(fresh)):
            for index in range(10):
                self.deliver(task, index*100, 100 if index < 9 else 120, True)
                if index < 9:
                    self.assertTrue(self.win._auto_more_timer.isActive())
                    self.win._auto_agent_more()
                    task = self.server.pop_task(0, 'access-test')
                    self.assertIsNotNone(task)
        self.assertEqual(self.win._agent_budget_used, 1000)
        self.assertEqual(len(self.win._all_results), 1000)
        self.assertFalse(self.win._agent_busy or self.win._auto_more_timer.isActive())
        last = self.server.current_task['id']
        self.win._load_agent_batch(); self.win._auto_agent_more()
        self.assertEqual(self.server.current_task['id'], last)
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_switch_to_limited_updates_current_task_without_losing_results(self):
        task = self.begin(True)
        self.deliver(task, 0, 10)
        event = self.server.cancellation(task['id'])
        self.win.agent_budget_combo.setCurrentIndex(0)
        self.assertFalse(event.is_set())
        self.assertTrue(self.win._agent_busy)
        self.assertEqual(len(self.win._all_results), 10)
        self.assertEqual(self.server.current_task['id'],task['id'])
        self.assertEqual(self.server.current_task['want'],100)
        self.assertEqual(self.win._agent_grant_end,100)
        reply=self.deliver(task,10,120)
        self.assertEqual(reply['accepted'],90);self.assertTrue(reply['done'])

    def test_switch_to_unlimited_preserves_keyword_count_and_new_search_resets(self):
        task = self.begin()
        self.deliver(task, 0, 20, True)
        self.win.agent_budget_combo.setCurrentIndex(1)
        self.assertEqual(self.win._agent_budget_used, 20)
        self.assertEqual(self.win._agent_grant_end, 1000)
        self.assertTrue(self.win._agent_busy)
        self.win._handoff_to_agent('另一个国内关键词')
        self.assertEqual(self.win._agent_budget_used, 0)
        self.assertEqual(self.win._budget_remaining(), 1000)

    def test_pending_queued_results_are_accounted_when_changing_mode(self):
        task = self.begin(True)
        # Server accepted results whose Qt delivery has not yet been processed.
        self.server._task_received = 25
        self.win.agent_budget_combo.setCurrentIndex(0)
        self.assertEqual(self.win._agent_budget_used, 25)
        self.assertEqual(self.server.current_task['id'],task['id'])
        self.assertEqual(self.server.task_status()['remaining'],75)

    def test_lower_mode_after_150_stops_only_task_and_preserves_delayed_cards(self):
        import requests
        task=self.begin(True)
        with requests.Session() as http:
            http.trust_env=False
            reply=http.post(self.server.endpoint(),json=dict(agent='access-test',task_id=task['id'],results=rows(0,150)),timeout=4).json()
        self.assertEqual(reply['accepted'],150)
        self.win.agent_budget_combo.setCurrentIndex(0)  # Qt result callback is still queued.
        self.pump()
        self.assertEqual(len(self.win._all_results),150)
        self.assertEqual(self.win._agent_budget_used,150)
        self.assertEqual(self.server.task_status()['state'],'completed')
        self.assertTrue(self.server.session_ready());self.assertFalse(self.win._agent_busy)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertIsNone(self.server.pop_task(0,'access-test'))
        self.win._load_agent_batch()
        new=self.server.pop_task(0,'access-test')
        self.assertEqual(new['want'],100);self.assertEqual(len(new['exclude']),150)

    def test_raise_active_mode_keeps_task_cursor_count_and_updates_remaining(self):
        task=self.begin();self.deliver(task,0,20)
        event=self.server.cancellation(task['id'])
        self.win.agent_budget_combo.setCurrentIndex(1)
        self.assertEqual(self.server.current_task['id'],task['id'])
        self.assertEqual(self.server.task_status()['remaining'],980)
        self.assertFalse(event.is_set());self.assertEqual(len(self.win._all_results),20)
        self.win.agent_budget_combo.setCurrentIndex(0)
        self.assertEqual(self.server.task_status()['remaining'],80)
        self.assertFalse(event.is_set())

    def test_raise_completed_hundred_creates_remaining_900_without_new_handshake(self):
        task=self.begin();self.deliver(task,0,100)
        token=self.server.token
        self.win.agent_budget_combo.setCurrentIndex(1)
        new=self.server.pop_task(0,'access-test')
        self.assertNotEqual(new['id'],task['id']);self.assertEqual(new['want'],900)
        self.assertEqual(len(new['exclude']),100);self.assertEqual(self.server.token,token)

    def test_mode_change_while_paused_does_not_resume_and_can_finish_new_cap(self):
        task=self.begin(True);self.deliver(task,0,150)
        self.win._toggle_agent_pause()
        self.win.agent_budget_combo.setCurrentIndex(0)
        self.assertTrue(self.server.paused);self.assertEqual(self.win._budget_remaining(),0)
        self.assertEqual(self.server.task_status()['state'],'idle')  # UI pause already revoked the prior task.
        self.assertFalse(self.win._agent_busy);self.assertEqual(len(self.win._all_results),150)
        self.win.agent_budget_combo.setCurrentIndex(1)
        self.assertTrue(self.server.paused);self.assertEqual(self.win._budget_remaining(),850)
        self.assertIsNone(self.server.pop_task(0,'access-test'))
        self.win._toggle_agent_pause();self.server.pop_task(0,'access-test');self.win._tick_agent_bar()
        next_task=self.server.pop_task(0,'access-test')
        self.assertIsNotNone(next_task);self.assertEqual(next_task['want'],850)

    def test_unlimited_last_task_only_gets_remaining_allowance(self):
        task = self.begin(True)
        self.deliver(task, 0, 1, True)
        self.win._agent_budget_used = 975
        self.win._auto_agent_more()
        new = self.server.pop_task(0, 'access-test')
        self.assertEqual(new['want'], 25)
        self.deliver(new, 100, 40)
        self.assertEqual(self.win._agent_budget_used, 1000)
        self.assertFalse(self.win._auto_more_timer.isActive())


if __name__ == '__main__':
    os.environ['RESOURCE_FINDER_HOME'] = tempfile.mkdtemp(prefix='rf-budget-test-')
    unittest.main(verbosity=2)
