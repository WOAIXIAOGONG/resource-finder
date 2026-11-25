"""Reach the user's 100/1000 target across prematurely finished Agent tasks."""
import testenv
import time
import unittest
from unittest.mock import patch
import requests
from budget_test import BudgetUITests, BudgetServerTests, rows


class FillUITests(unittest.TestCase):
    setUpClass = classmethod(BudgetUITests.setUpClass.__func__)
    tearDownClass = classmethod(BudgetUITests.tearDownClass.__func__)
    setUp = BudgetUITests.setUp
    tearDown = BudgetUITests.tearDown
    begin = BudgetUITests.begin
    deliver = BudgetUITests.deliver

    def next_fill(self):
        self.win._auto_agent_more()
        task = self.server.pop_task(0, 'access-test')
        self.assertIsNotNone(task)
        return task

    def test_55_then_duplicates_fills_only_45_and_next_scroll_grants_100(self):
        task = self.begin()
        self.deliver(task, 0, 55, True)
        self.assertTrue(self.win._auto_more_timer.isActive())
        task = self.next_fill()
        self.assertEqual(task['want'], 45)
        self.assertEqual(len(task['exclude']), 55)
        reply = self.deliver(task, 0, 110, True)
        self.assertEqual(reply['accepted'], 45)
        self.assertEqual(len(self.win._all_results), 100)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertEqual(self.win._agent_budget_used, 100)
        self.win._load_agent_batch()
        task = self.server.pop_task(0, 'access-test')
        self.assertEqual(task['want'], 100)
        self.assertEqual(self.win._agent_grant_end, 200)

    def test_unlimited_starts_with_1000_and_55_leaves_945(self):
        task = self.begin(True)
        self.assertEqual(task['want'], 1000)
        with patch.object(self.win, '_append_results', side_effect=lambda fresh: self.win._all_results.extend(fresh)):
            self.deliver(task, 0, 55, True)
            task = self.next_fill()
            self.assertEqual(task['want'], 945)
            self.assertEqual(self.deliver(task, 55, 1000, True)['accepted'], 945)
        self.assertEqual(self.win._agent_budget_used, 1000)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.win._load_agent_batch()
        self.assertIsNone(self.server.pop_task(0, 'access-test'))

    def test_streamed_55_plus_empty_done_still_fills_45(self):
        task = self.begin()
        self.deliver(task, 0, 55, False)
        self.deliver(task, 0, 0, True)
        self.assertEqual(self.next_fill()['want'], 45)

    def test_scroll_during_short_batch_does_not_expand_allowance(self):
        task = self.begin()
        self.deliver(task, 0, 55, True)
        self.win._load_agent_batch()
        task = self.server.pop_task(0, 'access-test')
        self.assertEqual(task['want'], 45)
        self.assertEqual(self.win._agent_grant_end, 100)

    def test_duplicate_only_completion_keeps_same_unverified_task_and_gap(self):
        task = self.begin()
        self.deliver(task, 0, 55, True)
        task = self.next_fill()
        self.assertEqual(task['want'], 45)
        for index in range(3):
            reply = self.deliver(task, 0, 55, True)  # Excluded duplicates are not end evidence.
            self.assertFalse(reply['done'])
            self.assertTrue(reply['completion_pending'])
            self.assertEqual(self.server.current_task['id'], task['id'])
            self.assertEqual(self.server.task_status()['remaining'], 45)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertFalse(self.win._no_more)
        self.assertFalse(self.server.cancellation(task['id']).is_set())

    def test_explicit_exhaustion_stops_and_reports_shortfall(self):
        task = self.begin()
        for site in self.server.access.sources():
            self.server.access.search_evidence.record(task['id'],dict(source_id=site.id,query='fixture',page=1),
                dict(results=[],pagination=dict(exhausted=True,supported=True)))
        with requests.Session() as http:
            http.trust_env = False
            reply = http.post(self.server.endpoint(), json={'agent': 'access-test', 'task_id': task['id'],
                'results': rows(0, 55), 'done': True, 'exhausted': True,
                'message': '所选站点已返回末页'}, timeout=4)
            reply.raise_for_status()
        self.app.processEvents()
        self.assertTrue(self.win._no_more)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertIn('55', self.win.pagination_notice.text())
        self.assertIn('所选站点已返回末页', self.win.pagination_notice.text())

    def test_unverified_shortfall_keeps_task_and_visible_remaining_count(self):
        task=self.begin()
        site=self.server.access.sources()[0]
        self.server.access.search_evidence.record(task['id'],dict(source_id=site.id,query='fixture',page=1),
            dict(results=[],pagination=dict(exhausted=False,supported=True)))
        self.deliver(task,0,10,True)
        self.assertFalse(self.win._no_more)
        self.assertFalse(self.server.cancellation(task['id']).is_set())
        self.assertIn('10/100',self.win.pagination_notice.text())
        resumed=self.server.pop_task(0,'access-test')
        self.assertEqual(resumed['id'],task['id']);self.assertTrue(resumed['resume_required'])
        self.assertEqual(resumed['remaining'],90)

    def test_pending_fill_is_cancelled_by_pause_new_query_and_source_change(self):
        task = self.begin()
        self.deliver(task, 0, 55, True)
        self.win._toggle_agent_pause()
        self.win._auto_agent_more()
        self.assertIsNone(self.server.current_task)
        self.win._handoff_to_agent('另一个国内视频')
        task = self.server.pop_task(0, 'access-test')
        self.assertEqual(task['want'], 100)
        self.deliver(task, 100, 55, True)
        self.win._source_all_clicked(False)
        self.win._auto_agent_more()
        self.assertFalse(self.win._auto_more_timer.isActive())

    def test_quiet_agent_preserves_missing_45_and_resumes_fill_on_poll(self):
        task = self.begin()
        self.deliver(task, 0, 55, True)
        self.server._poll_times['access-test'] = time.time() - 91
        self.win._tick_agent_bar()
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.assertNotIn('本批已完成', self.win.agent_state.text())
        self.server.pop_task(0, 'access-test')
        self.win._tick_agent_bar()
        self.assertTrue(self.win._auto_more_timer.isActive())
        self.assertEqual(self.next_fill()['want'], 45)


class FillServerTests(unittest.TestCase):
    setUp = BudgetServerTests.setUp
    tearDown = BudgetServerTests.tearDown
    ready = BudgetServerTests.ready
    get = BudgetServerTests.get
    post = BudgetServerTests.post

    def test_filters_and_blacklist_do_not_spend_target_allowance(self):
        self.ready()
        self.cfg.blacklist = ['blocked']
        task = self.server.push_task('国内视频', {'min_views': 100, 'time': '7d'})
        valid = dict(rows(0, 1)[0], views=200, pub_ts=time.time()-60)
        items = [valid, dict(valid, url=rows(1, 1)[0]['url'], views=0),
                 dict(valid, url=rows(2, 1)[0]['url'], title='blocked'),
                 dict(valid, url=rows(3, 1)[0]['url'], pub_ts=time.time()-86400*30)]
        reply = self.post('results', task_id=task['id'], results=items, done=False).json()
        self.assertEqual(reply['accepted'], 1)
        self.assertEqual(reply['remaining'], 99)

    def test_instructions_do_not_tell_agent_to_stop_at_45_seconds(self):
        self.ready()
        task = self.server.push_task('国内视频', want=1000)
        self.assertNotIn('round_seconds', task['execution'])
        self.assertNotIn('45 秒', self.server.task_text(task))
        self.assertIn('exhausted', self.server.instructions())

    def test_exhaustion_without_reason_is_rejected_without_finishing_task(self):
        self.ready()
        task = self.server.push_task('国内视频')
        reply = self.post('results', task_id=task['id'], results=[], done=True, exhausted=True)
        self.assertEqual(reply.status_code, 400)
        self.assertNotEqual(self.server.task_status()['state'], 'completed')


del BudgetUITests, BudgetServerTests

if __name__ == '__main__':
    unittest.main(verbosity=2)
