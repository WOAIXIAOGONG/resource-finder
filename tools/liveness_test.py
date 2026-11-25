"""Retained Agent sessions, scoped process evidence, runners and timing attribution."""
import testenv
import os,sys,subprocess,tempfile,time,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import access_test as access
from app.agent_liveness import RunnerTracker,process_identity
from app.agent_client import CONTROL_SCRIPT

class LivenessTests(unittest.TestCase):
    setUp=access.AccessTests.setUp
    tearDown=access.AccessTests.tearDown
    task=access.AccessTests.task
    ready=access.AccessTests.ready
    get=access.AccessTests.get
    post=access.AccessTests.post

    def test_process_identity_handles_current_missing_and_invalid_pids(self):
        self.assertEqual(process_identity(os.getpid())[0],os.getpid())
        self.assertIsNone(process_identity(-1));self.assertIsNone(process_identity('bad'))

    def test_tracker_rejects_dead_or_tool_processes_and_pid_reuse(self):
        tracker=RunnerTracker()
        with self.assertRaises(ValueError):tracker.register('t',os.getpid(),os.getpid())
        with patch('app.agent_liveness.process_identity',side_effect=lambda pid:(pid,'start')):
            key=tracker.register('t',12345,12346);self.assertEqual(tracker.active('t'),1)
        with patch('app.agent_liveness.process_identity',side_effect=lambda pid:(pid,'reused')):
            self.assertEqual(tracker.active('t'),0)
        self.assertNotIn(key,tracker.rows)

    def test_readonly_status_and_observer_cannot_keep_dead_agent_online(self):
        task=self.task('只搜B站 猫');self.server.acknowledge(task['id'])
        self.server._poll_times[self.who]=time.time()-7200
        self.get('status');self.get('control',task_id=task['id'],observe=1)
        self.assertFalse(self.server.connected());self.assertTrue(self.server.session_ready())
        self.assertEqual(self.server.liveness()['evidence'],'unknown')
        self.assertFalse(self.server.cancellation(task['id']).is_set())

    def test_network_request_after_silence_reuses_handshake_authorization(self):
        task=self.task('只搜B站 猫')
        self.server.access.report({'check_id':task['network']['check_id'],'attempts':[
            {'url':url,'route':'native','ok':True,'status':200} for url in task['network']['targets']]})
        self.server._poll_times[self.who]=time.time()-7200
        self.assertFalse(self.server.connected());self.assertFalse(self.server.access.ready())
        self.assertTrue(self.server.session_ready())
        with patch.object(self.server.access,'search',return_value={'ok':True,'results':[]}):
            reply=self.post('network/search',task_id=task['id'],source_id='bili',query='猫')
        self.assertEqual(reply.status_code,200);self.assertTrue(reply.json()['ok'])
        self.server.disconnect()
        self.assertFalse(self.server.access.ready())

    def test_registered_process_evidence_retains_active_status_without_heartbeat(self):
        task=self.task('只搜B站 猫')
        with patch('app.agent_liveness.process_identity',side_effect=lambda pid:(pid,'start')):
            result=self.post('activity/runner',task_id=task['id'],runner_pid=12345,child_pid=12346)
            self.assertEqual(result.status_code,200)
            self.server._poll_times[self.who]=time.time()-7200
            self.assertTrue(self.server.connected())
            self.assertEqual(self.server.liveness()['registered_searches'],1)
            self.assertEqual(self.server.liveness()['evidence'],'registered_process')
            self.assertEqual(self.server.current_task['id'],task['id'])
        self.assertFalse(self.server.connected())
        self.assertTrue(self.server.session_ready())

    def test_new_task_pause_and_disconnect_invalidate_old_runner_proof(self):
        task=self.task('只搜B站 猫')
        with patch('app.agent_liveness.process_identity',side_effect=lambda pid:(pid,'start')):
            self.post('activity/runner',task_id=task['id'],runner_pid=12345,child_pid=12346)
            replacement=self.server.push_task('只搜B站 狗')
            self.assertEqual(self.server.liveness()['registered_searches'],0)
            self.assertEqual(self.post('activity/runner',task_id=task['id'],runner_pid=12345,child_pid=12346).status_code,409)
            self.server.pause();self.assertEqual(self.server.liveness()['registered_searches'],0)
            self.server.disconnect();self.assertFalse(self.server.session_ready())

    def test_nonowner_runner_cannot_take_over_or_change_task(self):
        task=self.task('只搜B站 猫');self.who='intruder'
        self.assertEqual(self.post('activity/runner',task_id=task['id'],runner_pid=12345,child_pid=12346).status_code,403)
        self.assertEqual(self.server.owner,'access-test');self.assertEqual(self.server.current_task['id'],task['id'])

    def test_foreign_namespace_registration_falls_back_without_false_process_proof(self):
        task=self.task('只搜B站 猫')
        with patch('app.agent_liveness.process_identity',side_effect=lambda pid:(pid,'host-created')):
            result=self.post('activity/runner',task_id=task['id'],runner_pid=12345,child_pid=12346,
                platform=os.name,runner_created='other-namespace',child_created='other-namespace')
            self.assertEqual(result.status_code,400)
            self.assertEqual(self.server.liveness()['registered_searches'],0)
            self.assertTrue(self.server.session_ready())
            result=self.post('activity/runner',task_id=task['id'],runner_pid=12345,child_pid=12346,platform=os.name)
            self.assertEqual(result.status_code,400)

    def test_network_time_uses_union_and_finished_metrics_stop_growing(self):
        task=self.task('只搜B站 猫');self.server.acknowledge(task['id'])
        now=time.monotonic();self.server._metrics.update(ack_at=now-10,
            network_intervals=[(now-9,now-5),(now-7,now-3)])
        metrics=self.server.timings()
        self.assertAlmostEqual(metrics['network_wall_ms'],6000,delta=20)
        self.assertAlmostEqual(metrics['outside_tool_ms'],4000,delta=20)
        self.server.finish_task(task['id']);first=self.server.timings()['work_ms'];time.sleep(.03)
        self.assertEqual(self.server.timings()['work_ms'],first)

    def test_real_runner_registration_observation_and_explicit_stop(self):
        task=self.task('只搜B站 猫')
        with tempfile.TemporaryDirectory() as directory:
            helper=Path(directory)/'runner.py';helper.write_text(CONTROL_SCRIPT,encoding='utf-8')
            proc=subprocess.Popen([sys.executable,str(helper),'--base',self.server.url,'--token',self.server.token,
                '--agent',self.who,'--task-id',task['id'],'--run',sys.executable,'-c','import time;time.sleep(40)'],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and not self.server.liveness()['registered_searches']:time.sleep(.03)
                self.assertEqual(self.server.liveness()['registered_searches'],1)
                self.server._poll_times[self.who]=time.time()-7200;old=self.server._poll_times[self.who]
                time.sleep(.1)
                self.assertTrue(self.server.connected());self.assertEqual(self.server._poll_times[self.who],old)
                self.server.disconnect();stdout,stderr=proc.communicate(timeout=5)
                self.assertEqual(proc.returncode,0,stderr);self.assertIn(b'"session_ended": true',stdout)
                self.assertFalse(self.server.runners.rows)
            finally:
                if proc.poll() is None:self.server.clear_tasks();proc.communicate(timeout=6)

class LivenessUITests(unittest.TestCase):
    setUpClass=classmethod(access.AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(access.AccessUITests.tearDownClass.__func__)
    setUp=access.AccessUITests.setUp
    tearDown=access.AccessUITests.tearDown

    def test_repeated_long_silence_checks_never_revoke_work_or_request_takeover(self):
        access.connect(self.server)
        self.win._handoff_to_agent('只搜B站 猫');task=self.server.pop_task(0,'access-test');self.server.acknowledge(task['id'])
        token=self.server.token
        for age in (100,211,301,3600,7200):
            self.server._poll_times['access-test']=time.time()-age
            self.server._activity_at=time.monotonic()-age
            self.win._agent_busy_timeout();self.win._tick_agent_bar()
            self.assertEqual(self.server.current_task['id'],task['id']);self.assertEqual(self.server.token,token)
            self.assertTrue(self.win._agent_busy);self.assertFalse(self.server.cancellation(task['id']).is_set())
            self.assertTrue(self.win.search_edit.isEnabled());self.assertFalse(self.errors)
            self.assertIn('接管保持',self.win.agent_state.text())
            self.assertTrue(self.win.side_busy_spinner._timer.isActive())

    def test_manual_disconnect_still_stops_retained_work(self):
        access.connect(self.server)
        self.win._handoff_to_agent('只搜B站 猫');task=self.server.pop_task(0,'access-test');self.server.acknowledge(task['id'])
        self.server._poll_times['access-test']=time.time()-7200
        self.win._agent_busy_timeout();self.win._disconnect_agent()
        self.assertFalse(self.server.session_ready());self.assertFalse(self.win._agent_busy)
        self.assertTrue(self.server.control('access-test',task['id'])['stop'])

    def test_external_execution_time_is_visible_without_tool_network_requests(self):
        access.connect(self.server)
        self.win._handoff_to_agent('只搜B站 猫');task=self.server.pop_task(0,'access-test');self.server.acknowledge(task['id'])
        self.server._metrics['ack_at']=time.monotonic()-10
        self.win._tick_agent_bar()
        self.assertEqual(self.server.timings()['network_requests'],0)
        self.assertIn('工具外等待/执行',self.win.agent_timing_state.text())

if __name__=='__main__':unittest.main(verbosity=2)
