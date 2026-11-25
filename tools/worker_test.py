"""Full supplied dispatcher/worker protocol with deterministic remote pages."""
import testenv
import json
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from access_test import AccessTests


class WorkerTests(unittest.TestCase):
    setUp = AccessTests.setUp
    tearDown = AccessTests.tearDown
    get = AccessTests.get
    post = AccessTests.post
    ready = AccessTests.ready

    def wait_for(self, predicate, seconds=12):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.03)
        self.fail('Worker timed out: '+self.log.read_text(encoding='utf-8'))

    @contextmanager
    def dispatcher(self):
        # These tests exercise collector transport, NOT model understanding.
        original_push = self.server.push_task
        def transport_task(*args, **kwargs):
            with self.server._task_lock:
                task = original_push(*args, **kwargs)
                task['execution']['requires_agent_reasoning'] = False
                return task
        self.server.push_task = transport_task
        self.cfg.sites = [s for s in self.cfg.sites if s.id == 'bili']
        with tempfile.TemporaryDirectory(prefix='rf-supplied-') as folder:
            root=Path(folder); helper=root/'runner.py'; self.log=root/'run.log'
            response=self.get('control/helper').json()
            self.assertTrue(response['requires_model_capable_worker'])
            helper.write_text(response['script'],encoding='utf-8')
            worker=root/'worker.py'
            worker.write_text(self.get('worker/helper').json()['script'],encoding='utf-8')
            with self.log.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,str(helper),'--base',self.server.url,
                    '--token',self.server.token,'--agent',self.who,'--serve','--run',sys.executable,str(worker)],stdout=output,stderr=output)
                try:
                    self.wait_for(self.server.session_ready)
                    yield process
                finally:
                    self.server.disconnect()
                    process.wait(timeout=8)

    @staticmethod
    def page(data, **kwargs):
        page=int(data['page'])
        return dict(ok=True, results=[dict(title='fixture '+str(page)+' '+str(n),
            url='https://www.bilibili.com/video/BVfixture'+str(page)+str(n),
            source='哔哩哔哩', cover='https://example.com/cover.jpg',duration='1:23',
            resource_type='video', views=100) for n in range(2)],
            pagination=dict(page=page,exhausted=page>=3,supported=True))

    def test_supplied_worker_sources_paging_metadata_and_next_round(self):
        with patch.object(self.server.access,'search',side_effect=self.page), self.dispatcher() as process:
            first=self.server.push_task('风景',want=3)
            self.wait_for(lambda:self.server.task_status()['state']=='completed')
            self.assertEqual(self.server.task_status()['received'],3)
            rows=list(self.server._task_result_rows)
            self.assertEqual(rows[0].cover,'https://example.com/cover.jpg')
            self.assertEqual(rows[0].duration,'1:23')
            self.wait_for(lambda:self.server.task_status()['waiting_polls']>0)
            second=self.server.push_task('风景',kind='more',want=2,exclude=[r.url for r in rows])
            self.wait_for(lambda:self.server.task_status()['state']=='completed')
            self.assertEqual(self.server.task_status()['received'],2)
            self.assertNotEqual(first['id'],second['id'])
            self.assertFalse(set(r.url for r in rows)&set(r.url for r in self.server._task_result_rows))
            self.assertIsNone(process.poll())
            log=self.log.read_text(encoding='utf-8')
            self.assertIn('"sources": ["bili"]',log)
            self.assertIn('"page": 2',log)

    def test_worker_uses_live_raised_quota_not_initial_want(self):
        calls=[]
        def page(data,**kwargs):
            calls.append(data['page'])
            if len(calls)==1:self.server.change_budget(data['task_id'],5,'unlimited')
            return self.page(data)
        with patch.object(self.server.access,'search',side_effect=page), self.dispatcher():
            self.server.push_task('风景',want=2)
            self.wait_for(lambda:self.server.task_status()['state']=='completed')
            self.assertEqual(self.server.task_status()['received'],5)
            self.assertEqual(calls,[1,2,3])

    def test_worker_failure_is_not_zero_result_success_and_dispatcher_survives(self):
        with patch.object(self.server.access,'search',side_effect=RuntimeError('fixture transport failure')), self.dispatcher() as process:
            self.server.push_task('风景',want=100)
            self.wait_for(lambda:self.server.task_status()['state']=='failed')
            self.assertEqual(self.server.task_status()['received'],0)
            self.assertTrue(self.errors)
            self.assertIn('fixture transport failure',str(self.errors))
            self.wait_for(lambda:self.server.task_status()['waiting_polls']>0)
            self.assertIsNone(process.poll())

    def test_zero_done_without_search_evidence_is_pending(self):
        self.ready(); task=self.server.push_task('国内视频');self.get()
        reply=self.post('results',task_id=task['id'],done=True,results=[]).json()
        self.assertFalse(reply['done']); self.assertTrue(reply['completion_pending'])
        self.assertTrue(reply['completion']['next_actions'])

    def test_replaced_inflight_search_starts_new_task_without_stale_results(self):
        entered=threading.Event()
        def page(data,cancel=None):
            if data['query']=='old':
                entered.set()
                self.assertTrue(cancel.wait(5))
            return self.page(data)
        with patch.object(self.server.access,'search',side_effect=page), self.dispatcher() as process:
            old=self.server.push_task('old',want=100)
            self.wait_for(entered.is_set)
            new=self.server.push_task('new',want=2)
            self.wait_for(lambda:self.server.task_status()['state']=='completed')
            self.assertNotEqual(old['id'],new['id'])
            self.assertEqual(self.server.task_status()['received'],2)
            self.assertTrue(all(meta['task_id']==new['id'] for _,_,meta in self.rows))
            self.assertFalse(self.errors)
            self.assertIsNone(process.poll())

    def test_config_contract_sources_are_siblings_of_network(self):
        self.ready();task=self.server.push_task('国内视频');self.get()
        cfg=self.get('network/config',task_id=task['id']).json()
        self.assertEqual(cfg['schema_version'],1)
        self.assertTrue(cfg['sources'])
        self.assertNotIn('sources',cfg['network'])
        namespace={}
        from app.agent_worker import WORKER_SCRIPT
        exec(compile(WORKER_SCRIPT,'worker','exec'),namespace)
        # Original DSH call discarded cfg['sources'] before selection.
        self.assertFalse((cfg.get('network') or cfg).get('sources'))


del AccessTests
if __name__=='__main__':unittest.main(verbosity=2)
