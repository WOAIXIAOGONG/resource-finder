import testenv
import sys,json,threading,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.aigei_browser import AigeiBrowser
from app.config import Settings
from app.downloads import DownloadCancelled,DownloadError

class AigeiBrowserTests(unittest.TestCase):
    def test_successful_launcher_exit_waits_for_delayed_endpoint_in_same_start(self):
        from websockets.sync.server import serve
        from unittest.mock import Mock
        def handler(ws):
            try:
                for _ in ws:pass
            except Exception:pass
        server=serve(handler,'127.0.0.1',0);threading.Thread(target=server.serve_forever,daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());b.root.mkdir();port=server.socket.getsockname()[1]
            timer=threading.Timer(.2,lambda:(b.root/'DevToolsActivePort').write_text(str(port)+'\n/devtools/browser/startup',encoding='utf-8'));timer.start()
            process=Mock();process.poll.return_value=0
            try:
                with patch.object(b,'executable',return_value=Path('fixture-edge.exe')),patch('subprocess.Popen',return_value=process) as launch:
                    b.start(threading.Event(),time.monotonic()+3)
                self.assertIn('127.0.0.1',b.address);self.assertEqual(launch.call_count,1)
                self.assertNotEqual(launch.call_args.args[0][-1],'about:blank')
            finally:timer.join();b.close();server.shutdown()
    def test_port_file_is_retained_until_browser_websocket_becomes_ready(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());b.root.mkdir();portfile=b.root/'DevToolsActivePort';portfile.write_text('12345\n/devtools/browser/startup',encoding='utf-8')
            process=Mock();process.poll.return_value=0;calls=[]
            def connecting(*args,**kwargs):
                calls.append(1)
                if len(calls)<3:raise ConnectionRefusedError()
                return Mock()
            with patch.object(b,'executable',return_value=Path('fixture-edge.exe')),patch('subprocess.Popen',return_value=process) as launch,patch('app.aigei_browser.connect',side_effect=connecting):
                b.start(threading.Event(),time.monotonic()+3)
            self.assertEqual(launch.call_count,1);self.assertTrue(portfile.exists());self.assertEqual(len(calls),3);b.address='';b.close()
    def test_cancelled_health_probe_does_not_revoke_a_live_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());b.address='ws://127.0.0.1:9';b.target='owned';b._page_ready=True
            cancel=threading.Event()
            def interrupted(*args):cancel.set();raise DownloadCancelled('已取消')
            with patch('app.aigei_browser.connect'),patch.object(b,'rpc',side_effect=interrupted):
                with self.assertRaises(DownloadCancelled):b.refresh_ready(cancel)
            self.assertTrue(b.active);b.address='';b.close()
    def test_private_profile_and_loopback_endpoint_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());self.assertEqual(b.root,Path(tmp)/'aigei-browser');b.root.mkdir()
            for value in ['9222\n/devtools/browser/fixture','70000\n/devtools/browser/fixture','9222\nhttps://evil.test/private']:
                (b.root/'DevToolsActivePort').write_text(value,encoding='utf-8')
                self.assertEqual(bool(b.endpoint()),value.startswith('9222\n/devtools/'))
            b.close()
    def test_invalid_target_and_cancel_never_launch_browser(self):
        with tempfile.TemporaryDirectory() as tmp,patch('subprocess.Popen') as launch:
            b=AigeiBrowser(tmp,Settings());c=threading.Event()
            for path in ['https://evil.test','/open/download','/item/../a.html']:
                with self.assertRaises(DownloadError):b.extract(path,c)
            c.set()
            with self.assertRaises(DownloadCancelled):b.extract('/item/rain.html',c)
            launch.assert_not_called();self.assertFalse(b.root.exists());b.close()
    def test_cancel_while_another_job_owns_browser_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());b.lock.acquire();c=threading.Event();timer=threading.Timer(.15,c.set);timer.start();start=time.monotonic()
            try:
                with self.assertRaises(DownloadCancelled):b.extract('/item/rain.html',c)
                self.assertLess(time.monotonic()-start,1)
            finally:b.lock.release();b.close()
    def test_protocol_uses_bounded_integer_id_and_ignores_events(self):
        class Socket:
            def send(self,s):self.message=json.loads(s);self.count=0
            def recv(self,timeout):
                self.count+=1
                return json.dumps({'method':'Page.event'}) if self.count==1 else json.dumps({'id':self.message['id'],'result':{'ok':True}})
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());socket=Socket()
            self.assertEqual(b.rpc(socket,'Target.getTargets',{},threading.Event(),time.monotonic()+1),{'ok':True})
            self.assertIs(type(socket.message['id']),int);self.assertLess(socket.message['id'],2**31);b.close()
    def test_only_numeric_resource_play_button_can_be_activated(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings());c=threading.Event()
            with patch.object(b,'evaluate') as evaluate:
                b.click_play(None,{'itemId':'123','button':{'x':2,'y':3}},c,time.monotonic()+1,None)
                code=evaluate.call_args.args[1];self.assertIn('audioItemBox_123',code);self.assertIn(".audio-player-btn",code)
                self.assertNotIn('fileGet',code);self.assertNotIn('download',code)
                evaluate.reset_mock();b.click_play(None,{'itemId':'123','button':{'obscured':True}},c,time.monotonic()+1,None);evaluate.assert_not_called()
                with self.assertRaises(DownloadError):b.click_play(None,{'itemId':'x);evil()','button':{'x':1}},c,time.monotonic()+1,None)
            b.close()
    def test_close_only_uses_owned_browser_endpoint_not_taskkill(self):
        with tempfile.TemporaryDirectory() as tmp,patch('subprocess.run') as shell:
            b=AigeiBrowser(tmp,Settings());b.close();shell.assert_not_called();self.assertTrue(b.closed)
    def test_initial_blank_tab_is_not_reused_as_resource_target(self):
        from websockets.sync.server import serve
        operations=[];targets=[{'type':'page','targetId':'browser-startup','url':'about:blank'}]
        def handler(ws):
            try:
                for raw in ws:
                    msg=json.loads(raw);method=msg['method'];operations.append((method,msg.get('params',{})))
                    data={}
                    if method=='Target.getTargets':data={'targetInfos':list(targets)}
                    elif method=='Target.createTarget':
                        targets.append({'type':'page','targetId':'owned-resource','url':'https://www.aigei.com/item/rain.html'});data={'targetId':'owned-resource'}
                    elif method=='Target.attachToTarget':
                        if msg['params']['targetId']!='owned-resource':raise AssertionError('Unsafe startup-target reuse')
                        data={'sessionId':'owned-session'}
                    elif method=='Runtime.evaluate':data={'result':{'value':{'state':'ready','pagePath':'/item/rain.html','url':'https://s2.aigei.com/src/aud/mp3/rain.mp3','durationSeconds':10}}}
                    ws.send(json.dumps({'id':msg['id'],'result':data}))
            except Exception:pass
        server=serve(handler,'127.0.0.1',0);threading.Thread(target=server.serve_forever,daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            b=AigeiBrowser(tmp,Settings(timeout=3));b.address='ws://127.0.0.1:%d'%server.socket.getsockname()[1]
            try:
                data=b.extract('/item/rain.html',threading.Event());self.assertTrue(data['automatic_browser'])
                self.assertTrue(any(m=='Target.createTarget' for m,_ in operations));self.assertFalse(any(m=='Page.navigate' for m,_ in operations))
                self.assertTrue(b.active);self.assertTrue(b.refresh_ready())
                targets.pop();self.assertFalse(b.refresh_ready());self.assertFalse(b.active)
                self.assertEqual(sum(m=='Target.createTarget' for m,_ in operations),1)
            finally:b.close();server.shutdown()
    def test_dead_browser_address_does_not_count_as_ready_or_launch_window(self):
        with tempfile.TemporaryDirectory() as tmp,patch('subprocess.Popen') as launch:
            b=AigeiBrowser(tmp,Settings());b.address='ws://127.0.0.1:9';b.target='owned-resource';b._page_ready=True
            self.assertFalse(b.refresh_ready());self.assertFalse(b.active);launch.assert_not_called();b.address='';b.close()

if __name__=='__main__':unittest.main(verbosity=2)
