"""Owned persistent sessions, real local CDP framing, login and cancellation."""
import testenv
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from websockets.sync.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.background_browser import BackgroundBrowser
from app.config import Settings, SiteConfig
from app.login import LoginRequired


class BackgroundBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rf-background-test-')
        self.browser = BackgroundBrowser(Settings(timeout=3), self.temp.name)

    def tearDown(self):
        self.browser.close()
        self.temp.cleanup()

    def test_unknown_existing_directory_is_not_adopted_or_overwritten(self):
        self.browser.root.mkdir();keep=self.browser.root/'keep.txt';keep.write_text('do not touch')
        with self.assertRaisesRegex(RuntimeError,'未识别'):
            self.browser._profile()
        self.assertEqual(keep.read_text(),'do not touch')

    def test_linked_profile_is_rejected_before_opening_browser(self):
        with patch.object(Path,'is_symlink',return_value=True), patch('app.background_browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'链接'):self.browser.open_login()
            launch.assert_not_called()

    def test_saved_session_is_isolated_and_detection_does_not_launch(self):
        self.browser._profile();self.browser._save(authenticated=True,browser='edge')
        fresh=BackgroundBrowser(Settings(),self.temp.name)
        other=BackgroundBrowser(Settings(),Path(self.temp.name)/'other-user')
        try:
            with patch('app.background_browser.subprocess.Popen') as launch:
                self.assertTrue(fresh.can_resume());self.assertFalse(other.can_resume());launch.assert_not_called()
        finally:
            fresh.helper.close();other.close()
        # Close metadata is mocked for this fixture, not a real browser.
        self.browser._owned=lambda **kw:dict(processes=0,headless=0,windows=0)

    def test_login_launch_is_plain_and_only_uses_owned_profile(self):
        self.browser._profile()
        states=[dict(processes=0,headless=0,windows=0),dict(processes=0,headless=0,windows=0),dict(processes=1,headless=0,windows=1)]
        with patch.object(self.browser,'_owned',side_effect=states), patch.object(self.browser,'_executable',return_value=Path('C:/browser/msedge.exe')), patch('app.background_browser.subprocess.Popen') as launch:
            self.browser.open_login()
            args=launch.call_args.args[0]
            self.assertIn('--user-data-dir='+str(self.browser.root),args)
            self.assertFalse(any('headless' in arg or 'remote-debugging' in arg or 'enable-automation' in arg for arg in args))
            self.assertTrue(self.browser.pending_login)
        self.browser._owned=lambda **kw:dict(processes=0,headless=0,windows=0)

    def test_prepare_preserves_profile_files_and_closes_only_owned_login(self):
        self.browser._profile();cookie=self.browser.root/'cookie-fixture';cookie.write_bytes(b'kept')
        self.browser.pending_login=True
        with patch.object(self.browser,'_owned',return_value=dict(processes=1,headless=0,windows=1)), patch.object(self.browser,'_stop_browser') as stop, patch.object(self.browser,'_start') as start:
            self.browser.prepare()
            stop.assert_called_once();start.assert_called_once();self.assertFalse(self.browser.pending_login)
        self.assertEqual(cookie.read_bytes(),b'kept')
        self.browser._owned=lambda **kw:dict(processes=0,headless=0,windows=0)

    def test_closed_session_and_cancelled_calls_do_not_launch(self):
        self.browser.close()
        with patch('app.background_browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'取消'):self.browser.search_page('cat')
            launch.assert_not_called()

    def test_wrong_ownership_marker_never_requests_browser_control(self):
        self.browser.root.mkdir();(self.browser.root/'.resourcefinder-session').write_text('not a valid owner')
        with patch.object(self.browser,'_owned') as owned:
            with self.assertRaisesRegex(RuntimeError,'归属'):self.browser.prepare()
            owned.assert_not_called()


class BackgroundTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='rf-background-cdp-')
        self.browser=BackgroundBrowser(Settings(timeout=3),self.temp.name)
        self.browser._profile();self.calls=[];self.url='https://www.youtube.com/';self.logged=True;self.logged_reads=[];self.scrolls=0
        self.initial={'contents':[{'videoRenderer':{'videoId':'abcdefghijk','title':{'simpleText':'fixture'}}},
            {'continuationItemRenderer':{'continuationEndpoint':{'continuationCommand':{'token':'opaque'}}}}]}
        self.dom=self.initial;self.advance=True;self.snapshots=[];self.on_scroll=lambda:None
        self.targets={'owned-page':(self.url,self.dom)};self.selected_target='owned-page';self.created_targets=0;self.delayed_close=False
        self.next_dom={'contents':self.initial['contents'][:1]+[
            {'videoRenderer':{'videoId':'12345678901','title':{'simpleText':'next'}}},
            {'continuationItemRenderer':{'continuationEndpoint':{'continuationCommand':{'token':'later'}}}}]}
        def connected(ws):
            try:
                for line in ws:
                    msg=json.loads(line);self.calls.append(msg)
                    if not isinstance(msg.get('id'),int) or not 0<msg['id']<2**31:
                        raise AssertionError('CDP IDs must fit Chromium integer range')
                    method=msg['method'];result={}
                    if method=='Target.getTargets':
                        self.targets[self.selected_target]=(self.url,self.dom)
                        result={'targetInfos':[{'targetId':key,'type':'page','url':value[0]} for key,value in self.targets.items()]}
                    elif method=='Target.createTarget':
                        self.created_targets+=1;key='created-'+str(self.created_targets)
                        self.targets[key]=(msg['params']['url'],self.initial);result={'targetId':key}
                    elif method=='Target.closeTarget':
                        if not self.delayed_close:self.targets.pop(msg['params']['targetId'],None)
                        result={'success':True}
                    elif method=='Target.attachToTarget':
                        self.targets[self.selected_target]=(self.url,self.dom)
                        self.selected_target=msg['params']['targetId'];self.url,self.dom=self.targets[self.selected_target]
                        result={'sessionId':'attached'}
                    elif method=='Page.navigate':self.url=msg['params']['url'];self.dom=self.initial
                    elif method=='Runtime.evaluate':
                        script=msg['params']['expression']
                        if '__rfReadSearchResults' in script:
                            value=self.snapshots.pop(0) if self.snapshots else dict(url=self.url,data=self.dom,loading=False,pending=False)
                        elif '__rfScrollSearchResults' in script:
                            self.scrolls+=1;self.on_scroll()
                            if self.advance:self.dom=self.next_dom
                            value=True
                        else:
                            value={'url':self.url,'ready':'complete','data':{'contents':self.initial},
                                   'config':{'LOGGED_IN':self.logged_reads.pop(0) if self.logged_reads else self.logged,'INNERTUBE_CONTEXT':{'client':{'clientName':'WEB','clientVersion':'fixture'}}}}
                        result={'result':{'value':value}}
                    ws.send(json.dumps({'method':'Page.lifecycleEvent','params':{}}))
                    ws.send(json.dumps({'id':msg['id'],'result':result}))
            except Exception:
                pass
        self.server=serve(connected,'127.0.0.1',0,compression=None,close_timeout=.1)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.browser.address='ws://127.0.0.1:'+str(self.server.socket.getsockname()[1])
        self.browser._start=lambda *args:None
        self.browser._owned=lambda **kw:dict(processes=0,headless=0,windows=0)

    def tearDown(self):
        self.browser.close();self.server.shutdown();self.thread.join(2);self.temp.cleanup()

    def test_authenticated_query_saves_then_reuses_verified_page_once(self):
        first=self.browser.search_page('cat');self.browser.remember_verification('cat')
        self.assertTrue(self.browser.can_resume())
        self.assertEqual(self.browser.search_page('cat'),first)
        self.assertEqual(sum(m['method']=='Page.navigate' for m in self.calls),1)
        self.browser.search_page('cat')
        self.assertEqual(sum(m['method']=='Page.navigate' for m in self.calls),2)
        self.assertFalse(any(m['method'] in ('Network.getAllCookies','Network.getCookies') for m in self.calls))
        self.assertTrue(any(m['method']=='Page.bringToFront' for m in self.calls))

    def test_signed_out_page_is_not_misreported_as_verified(self):
        self.logged=False
        with self.assertRaises(LoginRequired):self.browser.search_page('cat')
        self.assertFalse(self.browser.can_resume());self.assertIsNone(self.browser.last_page)

    def test_login_bootstrap_waits_for_authenticated_config_without_second_login(self):
        self.logged_reads=[False,False,True]
        _,cfg=self.browser.search_page('cat')
        self.assertTrue(cfg['LOGGED_IN'])
        self.assertEqual(sum(m['method']=='Page.navigate' for m in self.calls),1)
        self.browser.remember_verification('cat');self.assertTrue(self.browser.can_resume())

    def test_query_is_url_encoded_and_foreign_continuation_never_runs(self):
        query='猫 & " ; location.href="evil"'
        _,cfg=self.browser.search_page(query)
        from urllib.parse import parse_qs,urlsplit
        self.assertEqual(parse_qs(urlsplit(self.url).query)['search_query'],[query])
        from app.net import NetworkError
        with self.assertRaises(NetworkError):self.browser.continuation({'continuation':'x','context':{}},session=['background','foreign'])
        self.assertEqual(self.scrolls,0)

    def test_continuation_is_same_origin_idempotent_and_pinned_to_query(self):
        _,cfg=self.browser.search_page('cat');body={'continuation':'opaque','context':cfg['INNERTUBE_CONTEXT']};session=cfg['_rf_browser_session']
        first=self.browser.continuation(body,session=session)
        self.assertEqual(self.browser.continuation(body,session=session),first);self.assertEqual(self.scrolls,1)
        script=next(m['params']['expression'] for m in self.calls if m['method']=='Runtime.evaluate' and '__rfScrollSearchResults' in m['params']['expression'])
        self.assertIn("location.origin==='https://www.youtube.com'",script)
        self.assertNotIn('document.cookie',script)
        self.assertFalse(any('fetch(' in m.get('params',{}).get('expression','') for m in self.calls))
        self.browser.search_page('coffee')
        self.assertEqual(self.browser.continuation(body,session=session),first)
        self.assertEqual(self.scrolls,1)

    def test_interleaved_keywords_keep_independent_native_pages(self):
        _,cat=self.browser.search_page('cat');cat_target=self.browser.target
        _,dog=self.browser.search_page('dog');dog_target=self.browser.target
        self.assertNotEqual(cat_target,dog_target)
        body={'continuation':'opaque','context':{}}
        a=self.browser.continuation(body,session=cat['_rf_browser_session'])
        self.assertEqual(self.url.split('search_query=')[1].split('&')[0],'cat')
        b=self.browser.continuation(body,session=dog['_rf_browser_session'])
        self.assertEqual(self.url.split('search_query=')[1].split('&')[0],'dog')
        self.assertEqual(self.scrolls,2)
        self.assertEqual(self.browser.continuation(body,session=cat['_rf_browser_session']),a)
        self.assertEqual(self.browser.continuation(body,session=dog['_rf_browser_session']),b)
        self.assertEqual(self.scrolls,2)

    def test_evicted_or_closed_search_page_is_not_a_login_failure(self):
        from app.net import NetworkError
        _,cat=self.browser.search_page('cat');target=self.browser.target
        self.targets.pop(target)
        # The fake current page has been closed; do not resurrect it on getTargets.
        self.selected_target='unrelated';self.url='https://example.com/';self.dom={}
        with self.assertRaises(NetworkError):
            self.browser.continuation({'continuation':'opaque','context':{}},session=cat['_rf_browser_session'])
        self.assertEqual(self.scrolls,0)

    def test_page_limit_closes_only_old_search_and_keeps_saved_login(self):
        from app.net import SearchSessionExpired
        self.browser._save(authenticated=True)
        self.browser.session_limit=2
        _,cat=self.browser.search_page('cat');old_target=self.browser.target
        _,dog=self.browser.search_page('dog')
        self.browser.search_page('bird')
        self.assertEqual(len(self.browser.sessions),2)
        closed=[m['params']['targetId'] for m in self.calls if m['method']=='Target.closeTarget']
        self.assertEqual(closed,[old_target]);self.assertTrue(self.browser.can_resume())
        with self.assertRaises(SearchSessionExpired):
            self.browser.continuation({'continuation':'opaque','context':{}},session=cat['_rf_browser_session'])
        self.browser.continuation({'continuation':'opaque','context':{}},session=dog['_rf_browser_session'])
        self.assertEqual(self.scrolls,1)

    def test_async_closed_target_is_never_recycled_for_new_query(self):
        self.browser.session_limit=1;self.delayed_close=True
        self.browser.search_page('cat');old=self.browser.target
        self.browser.search_page('dog')
        self.assertNotEqual(self.browser.target,old)
        self.assertTrue(any(m['method']=='Target.createTarget' for m in self.calls))

    def test_failed_new_query_does_not_destroy_previous_continuation(self):
        _,cat=self.browser.search_page('cat')
        self.logged=False
        with self.assertRaises(LoginRequired):self.browser.search_page('dog')
        self.logged=True
        reply=self.browser.continuation({'continuation':'opaque','context':{}},session=cat['_rf_browser_session'])
        self.assertEqual(self.url.split('search_query=')[1].split('&')[0],'cat')
        self.assertEqual(self.scrolls,1)

    def test_native_delta_excludes_previous_cards_and_ad_only_records(self):
        _,cfg=self.browser.search_page('cat')
        self.next_dom['contents'].append({'adSlotRenderer':{'videoRenderer':{'videoId':'adadadadada','title':{'simpleText':'advert'}}}})
        reply=self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session'])
        from app.adapters.youtube import YouTubeAdapter
        rows=YouTubeAdapter()._parse_results(reply,SiteConfig(id='ytb',name='YouTube'))
        self.assertEqual([r.extra['video_id'] for r in rows],['12345678901'])

    def test_stalled_page_does_not_cache_failure_and_same_cursor_can_recover(self):
        _,cfg=self.browser.search_page('cat');body={'continuation':'opaque','context':{}}
        self.advance=False;self.browser.settings.timeout=.35
        with self.assertRaisesRegex(RuntimeError,'超时'):self.browser.continuation(body,session=cfg['_rf_browser_session'])
        self.assertNotIn('opaque',self.browser.cache);self.assertEqual(len(self.browser.items_seen),1)
        self.advance=True;self.browser.settings.timeout=3
        reply=self.browser.continuation(body,session=cfg['_rf_browser_session'])
        self.assertIn('opaque',self.browser.cache);self.assertEqual(len(self.browser.items_seen),2)

    def test_foreign_query_snapshot_never_reused(self):
        _,cfg=self.browser.search_page('cat')
        self.snapshots=[dict(url='https://www.youtube.com/results?search_query=other',data=self.next_dom)]
        from app.net import NetworkError
        with self.assertRaises(NetworkError):self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session'])
        self.assertFalse(self.browser.cache);self.assertEqual(self.scrolls,0)

    def test_cancellation_during_scroll_does_not_commit_cursor(self):
        _,cfg=self.browser.search_page('cat');cancel=threading.Event();self.on_scroll=cancel.set
        with self.assertRaisesRegex(RuntimeError,'取消'):
            self.browser.continuation({'continuation':'opaque','context':{}},cancel,session=cfg['_rf_browser_session'])
        self.assertFalse(self.browser.cache);self.assertEqual(len(self.browser.items_seen),1)

    def test_native_terminal_is_confirmed_and_cached_without_repeating_scroll(self):
        _,cfg=self.browser.search_page('cat')
        self.dom={'contents':self.initial['contents'][:1]+[{'messageRenderer':{'text':{'simpleText':'无更多结果'}}}]}
        self.advance=False
        reply=self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session'])
        self.assertEqual(reply['onResponseReceivedCommands'][0]['appendContinuationItemsAction']['continuationItems'],[])
        count=self.scrolls
        self.assertEqual(self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session']),reply)
        self.assertEqual(self.scrolls,count)

    def test_missing_native_result_model_is_not_false_end(self):
        _,cfg=self.browser.search_page('cat');self.dom={};self.advance=False;self.browser.settings.timeout=.35
        with self.assertRaisesRegex(RuntimeError,'超时'):
            self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session'])
        self.assertFalse(self.browser.cache)

    def test_native_error_notice_is_not_cached_or_mistaken_for_end(self):
        from app.net import NetworkError
        _,cfg=self.browser.search_page('cat')
        self.dom={'contents':[{'messageRenderer':{'text':{'simpleText':'Temporary error, retry'}}}]}
        with self.assertRaises(NetworkError):
            self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session'])
        self.assertFalse(self.browser.cache);self.assertEqual(len(self.browser.items_seen),1)

    def test_pending_native_loader_without_cursor_is_not_false_end(self):
        _,cfg=self.browser.search_page('cat')
        self.snapshots=[dict(url=self.url,data={'contents':self.initial['contents'][:1]},loading=False,pending=True)]
        reply=self.browser.continuation({'continuation':'opaque','context':{}},session=cfg['_rf_browser_session'])
        self.assertEqual(self.scrolls,1)
        from app.adapters.youtube import YouTubeAdapter
        self.assertEqual(YouTubeAdapter()._continuation(reply),'later')

    def test_cancellation_before_query_does_not_navigate(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaisesRegex(RuntimeError,'取消'):self.browser.search_page('cat',cancel)
        self.assertFalse(self.calls)


if __name__=='__main__':unittest.main(verbosity=2)
