"""Persistent executable workers and live budgets over actual localhost HTTP."""
import testenv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from access_test import AccessTests
from app.agent_client import CONTROL_SCRIPT


class SessionTests(unittest.TestCase):
    setUp=AccessTests.setUp
    tearDown=AccessTests.tearDown
    get=AccessTests.get
    post=AccessTests.post
    ready=AccessTests.ready

    def wait_for(self,predicate,seconds=8):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.03)
        self.fail('Timed out waiting for real worker/protocol transition')

    def test_budget_update_wakes_long_poll_without_cancelling_task(self):
        self.ready();task=self.server.push_task('国内视频',want=1000);self.get();self.post('ack',task_id=task['id'])
        result=[]
        watcher=threading.Thread(target=lambda:result.append(self.get('control',task_id=task['id'],wait=20,budget_revision=1)))
        watcher.start();time.sleep(.15);started=time.monotonic()
        self.server.change_budget(task['id'],100,'limited')
        watcher.join(2);self.assertFalse(watcher.is_alive())
        self.assertLess(time.monotonic()-started,2)
        state=result[0].json()
        self.assertTrue(state['budget_changed']);self.assertEqual(state['remaining'],100)
        self.assertEqual(state['budget']['mode'],'limited');self.assertFalse(state['cancelled'])
        self.assertEqual(state['current_task_id'],task['id']);self.assertEqual(state['next'],'continue')

    def test_completion_and_duplicate_callback_explicitly_keep_polling(self):
        self.ready();task=self.server.push_task('国内视频',want=1);self.get()
        payload=dict(task_id=task['id'],results=[dict(title='one',url='https://example.com/one')])
        for reply in [self.post('results',**payload).json(), self.post('results',**payload).json(),
                      self.get('control',task_id=task['id']).json(),self.get().json()]:
            self.assertFalse(reply['stop']);self.assertFalse(reply['session_ended'])
            self.assertTrue(reply['keep_polling']);self.assertEqual(reply['next'],'poll')

    def test_context_cannot_steal_owner_or_read_replaced_task(self):
        self.ready();task=self.server.push_task('国内视频',want=100);self.get()
        reply=self.get('task/context',task_id=task['id']).json()
        self.assertEqual(reply['task']['id'],task['id'])
        self.assertEqual(self.server.task_status()['state'],'delivered')
        forbidden=self.get('task/context',task_id=task['id'],agent='other')
        self.assertEqual(forbidden.status_code,403);self.assertEqual(self.server.owner,self.who)
        self.server.push_task('new')
        self.assertTrue(self.get('task/context',task_id=task['id']).json()['stale'])

    def test_persistent_worker_runs_two_rounds_survives_idle_pause_and_disconnects(self):
        self.ready()
        self.cfg.settings.agent_search_mode='unlimited'
        with tempfile.TemporaryDirectory(prefix='rf-serve-test-') as folder:
            root=Path(folder);helper=root/'helper.py';worker=root/'worker.py';log=root/'runner.log'
            helper.write_text(CONTROL_SCRIPT,encoding='utf-8')
            worker.write_text('''import os,json,urllib.request
with open(os.environ['RF_TASK_FILE'],encoding='utf-8') as f: task=json.load(f)
body=dict(agent=os.environ['RF_AGENT'],task_id=task['id'],results=[dict(title='fixture',url='https://example.com/'+task['id']+'/'+str(n)) for n in range(task['want']+5)])
request=urllib.request.Request(os.environ['RF_BASE']+'/api/results?token='+os.environ['RF_TOKEN'],data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as r: reply=json.load(r)
assert reply['done'] and reply['keep_polling'] and not reply['session_ended']
''',encoding='utf-8')
            with log.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,str(helper),'--base',self.server.url,'--token',self.server.token,
                    '--agent',self.who,'--serve','--run',sys.executable,str(worker)],stdout=output,stderr=output)
                try:
                    first=self.server.push_task('国内视频第一轮',want=1000)
                    self.wait_for(lambda:self.server.task_status()['state']=='completed')
                    self.assertEqual(self.server.task_status()['received'],1000)
                    self.wait_for(lambda:self.server.task_status()['waiting_polls']>0)
                    self.assertIsNone(process.poll());self.assertTrue(self.server.session_ready())
                    self.server.pause();self.cfg.settings.agent_search_mode='limited'
                    second=self.server.push_task('国内视频第二轮',want=100)
                    time.sleep(.25);self.assertEqual(self.server.task_status()['state'],'queued')
                    self.assertIsNone(process.poll());self.server.resume()
                    self.wait_for(lambda:self.server.task_status()['state']=='completed')
                    self.assertEqual(self.server.current_task['id'],second['id'])
                    self.assertEqual(self.server.task_status()['received'],100)
                    self.assertNotEqual(first['id'],second['id']);self.assertEqual(len(self.rows),2)
                    self.wait_for(lambda:self.server.task_status()['waiting_polls']>0)
                    self.server.disconnect();process.wait(timeout=5)
                    self.assertEqual(process.returncode,0,log.read_text(encoding='utf-8'))
                finally:
                    if process.poll() is None:self.server.disconnect();process.wait(timeout=5)

    def test_persistent_worker_bootstraps_claimed_task_and_receives_budget_file_update(self):
        self.ready();task=self.server.push_task('国内视频',want=1000);self.get();self.post('ack',task_id=task['id'])
        with tempfile.TemporaryDirectory(prefix='rf-serve-budget-') as folder:
            root=Path(folder);helper=root/'helper.py';worker=root/'worker.py';ready=root/'ready';observed=root/'observed'
            helper.write_text(CONTROL_SCRIPT,encoding='utf-8')
            worker.write_text('''import os,json,time,sys,urllib.request
from pathlib import Path
Path(sys.argv[1]).write_text('ready')
while True:
 with open(os.environ['RF_TASK_FILE'],encoding='utf-8') as f:task=json.load(f)
 if task['budget']['revision']>1:break
 time.sleep(.05)
Path(sys.argv[2]).write_text(str(task['want']))
body=dict(agent=os.environ['RF_AGENT'],task_id=task['id'],results=[dict(title='fixture',url='https://example.com/changed')])
request=urllib.request.Request(os.environ['RF_BASE']+'/api/results?token='+os.environ['RF_TOKEN'],data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as r:assert json.load(r)['done']
''',encoding='utf-8')
            with (root/'runner.log').open('w') as output:
                process=subprocess.Popen([sys.executable,str(helper),'--base',self.server.url,'--token',self.server.token,
                    '--agent',self.who,'--serve','--task-id',task['id'],'--run',sys.executable,str(worker),str(ready),str(observed)],stdout=output,stderr=output)
                try:
                    self.wait_for(ready.exists)
                    self.server.change_budget(task['id'],1,'limited')
                    self.wait_for(lambda:self.server.task_status()['state']=='completed')
                    self.assertEqual(observed.read_text(),'1')
                    self.wait_for(lambda:self.server.task_status()['waiting_polls']>0)
                    self.assertIsNone(process.poll())
                finally:
                    self.server.disconnect();process.wait(timeout=5)


del AccessTests
if __name__=='__main__':unittest.main(verbosity=2)
