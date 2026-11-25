"""Copy/help/task contract tests; these do not claim to test an external LLM."""
import testenv
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from access_test import AccessTests, AccessUITests, connect
from app.agent_contract import AGENT_RULES
from app.agent_client import CONTROL_SCRIPT
from app.agent_worker import WORKER_SCRIPT
from app.config import APP_VERSION


class ContractTests(unittest.TestCase):
    setUp=AccessTests.setUp
    tearDown=AccessTests.tearDown
    get=AccessTests.get
    post=AccessTests.post
    ready=AccessTests.ready

    def assert_contract(self,text):
        self.assertIn(AGENT_RULES,text)
        self.assertIn(APP_VERSION,text)
        self.assertIn('Agent审核相关性',text)
        self.assertNotIn('它默认下载并执行工具自带的已测试搜索脚本',text)
        self.assertNotIn('--serve 。',text)

    def test_idle_copy_and_http_help_share_current_rules_and_credentials(self):
        self.assert_contract(self.server.task_text())
        self.assert_contract(self.get('help').json()['how'])
        old=self.server.token;self.server.disconnect()
        current=self.server.task_text()
        self.assertNotIn(old,current)
        self.assertIn(self.server.token,current)

    def test_search_and_more_keep_request_as_data_and_always_include_contract(self):
        self.ready()
        raw='帮我找 Blender 布料模拟的视频\n"不要改变原话"'
        for kind,want in [('search',100),('more',900)]:
            task=self.server.push_task(raw,kind=kind,want=want,filters={
                'resource_type':'video','source_keys':['platform:youtube']},exclude=['https://example.com/old'])
            received=self.get().json()['task']
            self.assertEqual(received['query'],raw)
            self.assertEqual(received['agent_contract']['instructions'],AGENT_RULES)
            self.assertTrue(received['execution']['requires_agent_reasoning'])
            text=self.server.task_text(task)
            self.assert_contract(text)
            self.assertIn(json.dumps(raw,ensure_ascii=False),text)
            self.assertIn('platform:youtube',text)
            self.assertIn('本轮目标：%d 条'%want,text)

    def test_copy_after_budget_change_preserves_zero_and_current_state(self):
        self.ready();task=self.server.push_task('Blender',want=1000);self.get()
        self.server.change_budget(task['id'],0,'limited')
        text=self.server.task_text()
        self.assertIn('本轮目标：0 条',text)
        self.assertIn('状态：completed',text)
        self.assertIn('剩余0条',text)

    def test_collector_refuses_reasoning_task_before_any_network_or_push(self):
        namespace={'__name__':'worker_test_module'}
        exec(compile(WORKER_SCRIPT,'worker','exec'),namespace)
        worker=object.__new__(namespace['Worker'])
        worker.task=self.server.push_task('帮我找Blender教程')
        with self.assertRaisesRegex(RuntimeError,'没有模型能力'):
            worker.run()

    def test_bare_serve_fails_before_claiming_or_marking_connection_ready(self):
        task=self.server.push_task('Blender')
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'runner.py';script.write_text(CONTROL_SCRIPT,encoding='utf-8')
            result=subprocess.run([sys.executable,str(script),'--base',self.server.url,
                '--token',self.server.token,'--agent','bare-test','--serve'],capture_output=True,timeout=8)
        self.assertEqual(result.returncode,2)
        self.assertIn(b'interpretation and candidate review',result.stderr)
        self.assertEqual(self.server.task_status()['state'],'queued')
        self.assertEqual(self.server.current_task['id'],task['id'])
        self.assertFalse(self.server.session_ready())


class ContractUITests(unittest.TestCase):
    setUpClass=classmethod(AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(AccessUITests.tearDownClass.__func__)
    _base_setup=AccessUITests.setUp
    _base_teardown=AccessUITests.tearDown
    def setUp(self):
        from PySide6.QtGui import QGuiApplication
        self.clipboard_before=QGuiApplication.clipboard().text()
        self._base_setup()
    def tearDown(self):
        from PySide6.QtGui import QGuiApplication
        self._base_teardown()
        QGuiApplication.clipboard().setText(self.clipboard_before)

    def test_main_and_dialog_copy_use_same_live_contract(self):
        from PySide6.QtGui import QGuiApplication
        self.win._copy_agent_help()
        self.assertIn(AGENT_RULES,QGuiApplication.clipboard().text())
        self.win._connect_dialog.retry()
        self.assertIn(AGENT_RULES,QGuiApplication.clipboard().text())
        connect(self.server);self.win._tick_agent_bar()
        self.win._handoff_to_agent('帮我找 Blender 布料模拟的视频')
        self.win.agent_budget_combo.setCurrentIndex(1)
        self.win._copy_agent_help()
        text=QGuiApplication.clipboard().text()
        self.assertIn('本轮目标：1000 条',text)
        self.assertIn(AGENT_RULES,text)

    def test_direct_mode_passes_user_query_unchanged(self):
        self.win._set_mode('direct')
        query='帮我找 Blender 布料模拟的视频'
        with patch.object(self.win,'_run_engine') as engine:
            self.win.search_edit.setText(query)
            self.win._start_search()
            self.win._worker.join(2)
            self.assertEqual(engine.call_args.args[0],query)

    def test_finished_external_turn_switch_query_and_resume_over_real_http(self):
        import requests
        connect(self.server)
        self.win._handoff_to_agent('old query')
        old = self.server.pop_task(0, 'access-test')
        self.server.acknowledge(old['id'])
        # A real one-shot client exits after reporting; it has no persistent poller.
        script = '''import sys,json,urllib.request
base,token,tid=sys.argv[1:]
body=json.dumps({'agent':'access-test','task_id':tid,'stage':'searching','message':'one shot client ending'}).encode()
req=urllib.request.Request(base+'/api/progress?token='+token,data=body,headers={'Content-Type':'application/json'})
print(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5).status)
'''
        process = subprocess.run([sys.executable,'-c',script,self.server.url,self.server.token,old['id']],capture_output=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        self.app.processEvents()
        self.server._poll_times[self.server.owner] = time.time()-180
        self.server._get_times[self.server.owner] = time.time()-180
        self.win.agent_budget_combo.setCurrentIndex(1)
        self.win.search_edit.setText('new query')
        self.win._start_search()
        self.win._tick_agent_bar()
        current = self.server.current_task
        self.assertNotEqual(current['id'],old['id'])
        self.assertEqual(self.server.task_status()['state'],'queued')
        for label in (self.win.agent_state,self.win.empty.title,self.win.side_busy_title):
            self.assertIn('搜索未开始',label.text())
        self.assertIn('上次取任务请求',self.win.side_busy_sub.text())
        self.assertFalse(self.win.side_busy_spinner._timer.isActive())
        token=self.server.token
        with patch('app.ui.mainwindow.AgentConnectDialog') as dialog:
            self.win.agent_copy_btn.click()
            dialog.assert_not_called()
        self.assertEqual(self.server.token,token)
        self.assertEqual(self.server.current_task['id'],current['id'])
        text=self.app.clipboard().text()
        self.assertIn('继续执行当前任务',text)
        self.assertIn('new query',text)
        self.assertIn('本轮目标：1000 条',text)
        with requests.Session() as http:
            http.trust_env=False
            params={'token':token,'agent':'access-test','wait':0}
            task=http.get(self.server.url+'/api/task',params=params,timeout=5).json()['task']
            self.assertEqual(task['id'],current['id'])
            self.win._tick_agent_bar()
            self.assertIn('待 Agent 确认',self.win.agent_state.text())
            stale=http.post(self.server.endpoint(),json={'agent':'access-test','task_id':old['id'],'results':[{'title':'late','url':'https://example.com/late'}]},timeout=5)
            self.assertEqual(stale.status_code,409)
            ack=http.post(self.server.url+'/api/ack',params={'token':token},json={'agent':'access-test','task_id':task['id']},timeout=5)
            self.assertTrue(ack.json()['ok'])
        self.app.processEvents()
        self.win._tick_agent_bar()
        self.assertEqual(self.server.task_status()['state'],'working')
        self.assertNotIn('尚未领取',self.win.agent_state.text())
        self.assertFalse(any(r.url.endswith('/late') for r in self.win._all_results))


del AccessTests,AccessUITests
if __name__=='__main__':unittest.main(verbosity=2)
