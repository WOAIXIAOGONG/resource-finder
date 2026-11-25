"""Premature exhaustion: real Agent HTTP and UI, deterministic search pages."""
import testenv
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from access_test import AccessTests
from app.search_evidence import SearchEvidence
from budget_test import rows


class CompletionTests(unittest.TestCase):
    def test_three_distinct_empty_pages_are_bounded_not_exhausted(self):
        evidence=SearchEvidence();evidence.reset('t');site=SimpleNamespace(id='ytb')
        for page in range(1,4):
            evidence.record('t',dict(source_id='ytb',query='nature',page=page),dict(results=[],pagination=dict(page=page,exhausted=False)))
        review=evidence.review(dict(query='nature'),[site],{})
        self.assertTrue(review['can_finish']);self.assertTrue(review['limited'])
        self.assertFalse(review['verified']);self.assertFalse(review['next_actions'])
        self.assertFalse(evidence.rows[('ytb','nature')]['exhausted'])

    def test_cache_retries_do_not_spend_budget_and_new_candidates_reset_idle(self):
        evidence=SearchEvidence();evidence.reset('t')
        def record(page,items):
            evidence.record('t',dict(source_id='ytb',query='nature',page=page),dict(results=items,pagination=dict(page=page,exhausted=False)))
        record(1,rows(0,1))
        for _ in range(10):record(1,[])
        self.assertEqual(evidence.page_count,1)
        record(2,rows(0,1));record(3,[]);record(4,rows(1,1))
        self.assertFalse(evidence.limit(dict(source_id='ytb',query='nature')))
        self.assertEqual(evidence.rows[('ytb','nature')]['idle'],0)

    def test_actual_page_and_budget_never_suggest_page_51(self):
        evidence=SearchEvidence();evidence.reset('t');site=SimpleNamespace(id='ytb')
        evidence.record('t',dict(source_id='ytb',query='nature',page=900),dict(results=rows(0,1),pagination=dict(page=50,exhausted=False)))
        self.assertEqual(evidence.rows[('ytb','nature')]['page'],50)
        review=evidence.review(dict(query='nature'),[site],{})
        self.assertTrue(review['can_finish']);self.assertFalse(review['next_actions'])
        self.assertFalse(review['verified'])

    def test_task_budget_bounds_endless_unique_queries(self):
        evidence=SearchEvidence();evidence.reset('t');site=SimpleNamespace(id='ytb')
        for index in range(200):
            evidence.record('t',dict(source_id='ytb',query=str(index),page=1),dict(results=rows(index,1),pagination=dict(exhausted=False)))
        review=evidence.review(dict(query='nature'),[site],{})
        self.assertTrue(review['can_finish']);self.assertTrue(review['limited']);self.assertFalse(review['verified'])

    def test_schedule_drains_one_query_per_platform_without_forgetting_others(self):
        evidence=SearchEvidence();evidence.reset('t');site=SimpleNamespace(id='ytb')
        for index in range(8):
            evidence.record('t',dict(source_id='ytb',query=str(index),page=1),dict(results=rows(index,1),pagination=dict(exhausted=False)))
        review=evidence.review(dict(query='nature'),[site],{})
        self.assertEqual(review['outstanding'],8);self.assertEqual(len(review['next_actions']),1)
        self.assertEqual(review['next_actions'][0]['query'],'0')

    def test_empty_irrelevant_and_unknown_page_are_not_terminal(self):
        evidence=SearchEvidence();evidence.reset('t')
        site=SimpleNamespace(id='bili')
        for exhausted in [False,None]:
            evidence.record('t',dict(source_id='bili',query='cat',page=1),dict(results=[],pagination=dict(exhausted=exhausted)))
            review=evidence.review(dict(query='cat'),[site],{})
            self.assertFalse(review['verified']);self.assertEqual(review['next_actions'][0]['page'],2)

    def test_other_query_and_unsearched_platform_cannot_be_hidden(self):
        evidence=SearchEvidence();evidence.reset('t')
        for query,end in [('cat',False),('cat animation',True)]:
            evidence.record('t',dict(source_id='bili',query=query,page=1),dict(results=[],pagination=dict(exhausted=end)))
        review=evidence.review(dict(query='cat'),[SimpleNamespace(id='bili'),SimpleNamespace(id='ytb')],{})
        self.assertFalse(review['verified']);self.assertEqual(review['outstanding'],2)
        self.assertEqual({a['reason'] for a in review['next_actions']},{'has_next_page','source_not_checked'})

    def test_old_task_and_cached_page_cannot_change_terminal_evidence(self):
        evidence=SearchEvidence();evidence.reset('t')
        args=dict(source_id='bili',query='cat',page=2)
        evidence.record('old',args,dict(pagination=dict(exhausted=False)))
        self.assertFalse(evidence.rows)
        evidence.record('t',args,dict(pagination=dict(exhausted=True)))
        evidence.record('t',dict(args,page=1),dict(pagination=dict(exhausted=False)))
        self.assertTrue(evidence.review(dict(query='cat'),[SimpleNamespace(id='bili')],{})['verified'])
        evidence.reset('new');self.assertFalse(evidence.rows)

    def test_overflow_never_forgets_open_pages_to_accept_done(self):
        evidence=SearchEvidence();evidence.reset('t')
        for i in range(257):
            evidence.record('t',dict(source_id='bili',query=str(i)),dict(pagination=dict(exhausted=False)))
        self.assertEqual(len(evidence.rows),256)
        self.assertTrue(evidence.overflow)
        self.assertFalse(evidence.review(dict(query='cat'),[SimpleNamespace(id='bili')],{})['verified'])


class CompletionServerTests(unittest.TestCase):
    setUp=AccessTests.setUp
    tearDown=AccessTests.tearDown
    get=AccessTests.get
    post=AccessTests.post
    ready=AccessTests.ready

    def begin(self, want=100):
        for site in self.cfg.sites:site.enabled=site.id=='bili'
        self.ready();task=self.server.push_task('只搜B站猫',want=want);self.get();self.post('ack',task_id=task['id'])
        return task

    def search(self,task,page,items,end):
        result=dict(ok=True,results=items,pagination=dict(page=page,exhausted=end,supported=True))
        with patch.object(self.server.access,'search',return_value=result):
            reply=self.post('network/search',task_id=task['id'],source_id='bili',query='cat',page=page)
        self.assertEqual(reply.status_code,200);return reply.json()

    def test_ten_of_hundred_cannot_end_then_same_task_reaches_quota(self):
        task=self.begin();token=self.server.token;event=self.server.cancellation(task['id'])
        first=self.search(task,1,rows(0,10),False)
        reply=self.post('results',task_id=task['id'],candidate_ids=[r['candidate_id'] for r in first['results']],
                        done=True,exhausted=True,message='5组关键词，第二页不相关，找不到了').json()
        self.assertEqual(reply['accepted'],10);self.assertFalse(reply['done']);self.assertFalse(reply['exhausted'])
        self.assertTrue(reply['completion_pending']);self.assertFalse(event.is_set())
        self.assertEqual(reply['completion']['next_actions'][0]['page'],2)
        self.server._poll_times[self.who]=time.time()-7200
        resumed=self.get().json()['task']
        self.assertEqual(resumed['id'],task['id']);self.assertTrue(resumed['resume_required'])
        self.assertEqual((resumed['want'],resumed['remaining'],len(resumed['exclude'])),(100,90,10))
        self.assertEqual(self.server.token,token)
        self.post('ack',task_id=task['id'])
        second=self.search(task,2,rows(10,90),False)
        final=self.post('results',task_id=task['id'],candidate_ids=[r['candidate_id'] for r in second['results']],done=False).json()
        self.assertTrue(final['done']);self.assertEqual(final['received'],100);self.assertTrue(event.is_set())
        self.assertFalse(self.server.task_status()['completion'])
        self.assertIsNone(self.get().json()['task'])

    def test_verified_terminal_can_report_real_shortfall(self):
        task=self.begin();first=self.search(task,1,rows(0,10),True)
        result=self.post('results',task_id=task['id'],candidate_ids=[r['candidate_id'] for r in first['results']],
                         done=True,exhausted=True,message='适配器确认无后续游标').json()
        self.assertTrue(result['done']);self.assertTrue(result['exhausted'])
        self.assertEqual(result['remaining'],90)

    def test_repeated_empty_report_finishes_bounded_and_keeps_connection(self):
        task=self.begin();first=self.search(task,1,rows(0,16),False)
        self.post('results',task_id=task['id'],candidate_ids=[r['candidate_id'] for r in first['results']])
        for page in range(2,5):reply=self.search(task,page,[],False)
        self.assertTrue(reply['pagination']['limited'])
        # Repeat request from a legacy client must not spend network or reset evidence.
        with patch('app.adapters.adapter_for') as adapter:
            blocked=self.post('network/search',task_id=task['id'],source_id='bili',query='cat',page=5).json()
        adapter.assert_not_called();self.assertTrue(blocked['pagination']['limited'])
        self.assertEqual(self.server.access.search_evidence.page_count,4)
        result=self.post('results',task_id=task['id'],done=True,exhausted=True,message='连续空页').json()
        self.assertTrue(result['done']);self.assertTrue(result['search_limited']);self.assertFalse(result['exhausted'])
        self.assertEqual(result['received'],16);self.assertEqual(result['remaining'],84)
        self.assertFalse(result['completion_pending']);self.assertEqual(result['next'],'poll')
        self.assertTrue(self.server.session_ready());self.assertIsNone(self.get().json()['task'])

    def test_invalid_page_is_rejected_instead_of_silently_clamped(self):
        task=self.begin()
        with patch('app.adapters.adapter_for') as adapter:
            result=self.post('network/search',task_id=task['id'],source_id='bili',query='cat',page=51)
        adapter.assert_not_called();self.assertFalse(result.json()['ok'])
        self.assertFalse(self.server.access.search_evidence.rows)

    def test_real_youtube_adapter_changing_empty_cursors_do_not_loop_forever(self):
        import json
        from app.adapters.youtube import YouTubeAdapter
        from continuation_test import video, token
        task=self.begin();http=self.server.access._search_http
        config={'INNERTUBE_CONTEXT':{'client':{'clientVersion':'test'}}}
        html='ytcfg.set(%s);var ytInitialData = %s;' % (json.dumps(config),json.dumps(
            {'contents':{'sectionListRenderer':{'contents':[video(1),token('t0')]}}}))
        counter=[0]
        def empty(*args,**kwargs):
            counter[0]+=1
            return {'onResponseReceivedCommands':[{'appendContinuationItemsAction':{
                'continuationItems':[token('t%d'%counter[0])]}}]}
        with patch('app.adapters.adapter_for',return_value=YouTubeAdapter()),patch.object(http,'get_text',return_value=html),patch.object(http,'post_json',side_effect=empty):
            for page in range(1,5):
                response=self.post('network/search',task_id=task['id'],source_id='bili',query='nature',page=page).json()
                self.assertTrue(response['ok'],response)
            self.assertTrue(response['pagination']['limited'])
            self.assertFalse(response['pagination']['exhausted'])
            self.assertEqual(counter[0],9)
            self.post('network/search',task_id=task['id'],source_id='bili',query='nature',page=5)
            self.assertEqual(counter[0],9)
        result=self.post('results',task_id=task['id'],done=True).json()
        self.assertTrue(result['done']);self.assertTrue(result['search_limited'])

    def test_delivery_report_eight_query_count_sequences_reach_bounded_finish(self):
        # Counts transcribed from the user's report; synthetic URLs, not a claim
        # that unretained historical provider responses were replayed verbatim.
        sequences=[[9,5,3,5,6,7,1,0,9,6,9,9,0,0,0,0,0,0],
                   [9,3,5,7,12,11,0,0,0,0,0,0,0],
                   [10,2,3,5,2,3,0,0,0,0,0,0,0],
                   [15,5,3,5,3,7,1,0,2,0,0,0,0],
                   [8,7,3,3,5,13,5,8,10,9,12,2,1,0,0,0,0,0],
                   [11,8,4,0,0,0,0,0,0,0],[10,4,7,1,1,0,0,0,0],[0]*7]
        task=self.begin();self.post('results',task_id=task['id'],results=rows(0,16))
        for index,counts in enumerate(sequences):
            for page,count in enumerate(counts,1):
                result=dict(ok=True,results=rows(10000+index*1000+page*50,count),pagination=dict(page=page,exhausted=False,supported=True))
                with patch.object(self.server.access,'search',return_value=result):
                    reply=self.post('network/search',task_id=task['id'],source_id='bili',query='query-%d'%index,page=page).json()
                if reply['pagination'].get('limited'):break
            else:self.fail('Report sequence failed to stop: %d'%index)
        final=self.post('results',task_id=task['id'],done=True,exhausted=True,message='报告计数重演').json()
        self.assertTrue(final['done']);self.assertTrue(final['search_limited']);self.assertFalse(final['exhausted'])
        self.assertEqual(final['received'],16);self.assertEqual(final['remaining'],84)
        self.assertFalse(final['completion_pending']);self.assertIsNone(self.get().json()['task'])

    def test_prose_without_any_observed_page_is_not_exhaustion(self):
        task=self.begin()
        result=self.post('results',task_id=task['id'],results=[],done=True,exhausted=True,message='已搜完').json()
        self.assertTrue(result['completion_pending']);self.assertFalse(result['done'])
        self.assertFalse(self.server.cancellation(task['id']).is_set())

    def test_plain_done_cannot_end_an_observed_open_cursor(self):
        task=self.begin();self.search(task,1,[],False)
        result=self.post('results',task_id=task['id'],results=[],done=True).json()
        self.assertFalse(result['done']);self.assertTrue(result['completion_pending'])

    def test_running_search_cannot_be_cancelled_by_early_done(self):
        task=self.begin();entered=threading.Event();release=threading.Event();replies=[]
        def slow(*a,**kw):
            entered.set();release.wait(3)
            return dict(ok=True,results=[],pagination=dict(page=1,exhausted=False))
        with patch.object(self.server.access,'search',side_effect=slow):
            worker=threading.Thread(target=lambda:replies.append(self.post('network/search',task_id=task['id'],source_id='bili',query='cat')))
            worker.start();self.assertTrue(entered.wait(1))
            try:
                result=self.post('results',task_id=task['id'],results=[],done=True).json()
                self.assertFalse(result['done']);self.assertTrue(result['completion']['inflight'])
                self.assertFalse(self.server.cancellation(task['id']).is_set())
            finally:release.set();worker.join(4)
        self.assertEqual(replies[0].status_code,200)

    def test_explicit_disconnect_after_rejection_still_stops(self):
        task=self.begin();self.search(task,1,[],False)
        self.post('results',task_id=task['id'],results=[],done=True,exhausted=True,message='未搜完')
        token=self.server.token
        self.server.disconnect();self.assertTrue(self.server.cancellation(task['id']).is_set())
        self.assertTrue(self.get(token=token).json()['stop'])


del AccessTests
if __name__=='__main__':unittest.main(verbosity=2)
