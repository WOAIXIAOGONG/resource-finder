"""Real service-process death, owned process trees and fail-closed client output."""
import testenv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
from access_test import AccessUITests
from app.agent_client import CONTROL_SCRIPT, HELPER_SCRIPT
from app.agent_liveness import process_identity


class ShutdownTests(unittest.TestCase):
    def wait_for(self, condition, timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if condition():return
            time.sleep(.03)
        self.fail('Timed out waiting for process evidence')

    def lifecycle(self, crash=True, idle=False):
        with tempfile.TemporaryDirectory(prefix='rf-shutdown-') as folder:
            root=Path(folder);info=root/'info.json';close=root/'close';pids=root/'pids.json'
            server_script=root/'server.py';helper=root/'helper.py';worker=root/'worker.py'
            helper.write_text(CONTROL_SCRIPT,encoding='utf-8')
            server_script.write_text('''import sys,time,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app.agent import AgentServer
s=AgentServer(lambda *a:None,port=0);assert s.start();s.touch('shutdown-test')
task=None if sys.argv[4]=='idle' else s.push_task('diagnostic fixture')
Path(sys.argv[2]).write_text(json.dumps(dict(base=s.url,token=s.token,task=task)),encoding='utf-8')
while not Path(sys.argv[3]).exists():time.sleep(.03)
s.stop()
''',encoding='utf-8')
            worker.write_text('''import subprocess,sys,os,time,json
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(120)
''',encoding='utf-8')
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            service=subprocess.Popen([sys.executable,str(server_script),str(Path(__file__).resolve().parents[1]),str(info),str(close),'idle' if idle else 'active'],**options)
            runner=None;identities=[]
            try:
                self.wait_for(info.exists);data=json.loads(info.read_text(encoding='utf-8'))
                command=[sys.executable,str(helper),'--base',data['base'],'--token',data['token'],'--agent','shutdown-test','--serve','--run',sys.executable,str(worker),str(pids)]
                runner=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**options)
                if idle:
                    time.sleep(.4);self.assertIsNone(runner.poll())
                else:
                    self.wait_for(pids.exists)
                    identities=[process_identity(pid) for pid in json.loads(pids.read_text())]
                    self.assertTrue(all(identities))
                started=time.monotonic()
                if crash:service.kill()
                else:close.write_text('close')
                service.wait(timeout=5)
                stdout,stderr=runner.communicate(timeout=22)
                elapsed=time.monotonic()-started
                self.assertEqual(runner.returncode,0,stderr)
                replies=[json.loads(line) for line in stdout.decode().splitlines() if line.startswith('{')]
                terminal=[r for r in replies if r.get('session_ended')]
                self.assertTrue(terminal,stdout)
                self.assertEqual(terminal[-1]['next'],'stop')
                if crash:
                    self.assertTrue(any(r.get('reason')=='service_unavailable' for r in terminal))
                    self.assertFalse(terminal[-1].get('retryable',False))
                for identity in identities:
                    self.assertNotEqual(process_identity(identity[0]),identity,'Owned descendant survived service death')
                print('shutdown evidence:', 'crash' if crash else 'close', 'idle' if idle else 'active tree',round(elapsed,2),'seconds',flush=True)
            finally:
                if service.poll() is None:service.kill();service.wait(timeout=5)
                if runner and runner.poll() is None:
                    if os.name=='nt':subprocess.run(['taskkill','/PID',str(runner.pid),'/T','/F'],capture_output=True,**options)
                    else:runner.kill()
                    runner.communicate(timeout=5)
                for identity in identities:
                    if identity and process_identity(identity[0])==identity:
                        if os.name=='nt':subprocess.run(['taskkill','/PID',str(identity[0]),'/T','/F'],capture_output=True,**options)

    def test_real_crash_stops_active_worker_and_grandchild(self):self.lifecycle()
    def test_real_crash_stops_idle_dispatcher(self):self.lifecycle(idle=True)
    def test_normal_close_stops_active_worker_and_grandchild(self):self.lifecycle(crash=False)

    def test_read_transport_recovers_and_never_replays_post(self):
        namespace={'__name__':'test_module'};exec(CONTROL_SCRIPT,namespace)
        from urllib.request import Request
        opener=unittest.mock.Mock()
        opener.open.side_effect=[urllib.error.URLError('brief outage'),io.BytesIO(b'{"ok":true}')]
        with patch('time.sleep'):
            self.assertTrue(namespace['read_local'](opener,Request('http://127.0.0.1/'),4)['ok'])
        self.assertEqual(opener.open.call_count,2)
        opener.reset_mock();opener.open.side_effect=urllib.error.URLError('offline')
        with self.assertRaises(namespace['ServiceUnavailable']):
            namespace['read_local'](opener,Request('http://127.0.0.1/',data=b'{}'),4)
        self.assertEqual(opener.open.call_count,1)

    def test_network_helper_missing_service_emits_terminal_signal(self):
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as folder:
            helper=Path(folder)/'net.py';helper.write_text(HELPER_SCRIPT,encoding='utf-8')
            result=subprocess.run([sys.executable,str(helper),'--base','http://127.0.0.1:%d'%port,'--token','fixture','--agent','test','--task-id','fixture'],capture_output=True,timeout=18)
            self.assertEqual(result.returncode,0,result.stderr)
            reply=json.loads(result.stdout)
            self.assertTrue(reply['stop']);self.assertFalse(reply['retryable'])
            self.assertEqual(reply['reason'],'service_unavailable')


class ShutdownUITests(unittest.TestCase):
    setUpClass=classmethod(AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(AccessUITests.tearDownClass.__func__)
    setUp=AccessUITests.setUp
    tearDown=AccessUITests.tearDown

    def test_revoke_precedes_failing_download_cleanup(self):
        from PySide6.QtGui import QCloseEvent
        server=self.server;old_token=server.token
        with patch.object(self.win.downloads,'close',side_effect=RuntimeError('cleanup failure')):
            with self.assertRaisesRegex(RuntimeError,'cleanup failure'):
                self.win.closeEvent(QCloseEvent())
        self.assertFalse(server.running)
        self.assertNotEqual(server.token,old_token)
        self.assertIsNone(self.win._agent_server)


del AccessUITests
if __name__=='__main__':unittest.main(verbosity=2)
