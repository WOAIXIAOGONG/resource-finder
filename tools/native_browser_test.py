"""Ordinary-browser conversion, session binding, reuse and cancellation."""
import testenv
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.native_browser import NativeBrowser,renderers,accessible_duration
from app.login_browser import LoginBrowser
from app.config import Settings,Config
from app.adapters.youtube import YouTubeAdapter

def record(vid='abcdefghijk'):
    return dict(id=vid,title='Nature',label='Nature 1小时2分钟3秒',text=['1234次观看','2天前'],author='Creator')

class NativeBrowserTests(unittest.TestCase):
    def test_duration_is_accessible_suffix_not_numbers_in_title(self):
        self.assertEqual(accessible_duration(record()),'1:02:03')
        self.assertEqual(accessible_duration(dict(title='10 hours music',label='10 hours music',text=[])),'')
        self.assertEqual(accessible_duration(dict(title='Title',label='Title 2 minutes 4 seconds',text=[])),'2:04')
        self.assertEqual(accessible_duration(dict(title='Title',label='Title',text=['0:42'])),'0:42')

    def test_accessible_record_retains_cover_duration_and_metadata(self):
        rows=YouTubeAdapter()._parse_results({'contents':renderers([record(),record('invalid')])},Config().get_site('ytb'))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].duration,'1:02:03');self.assertEqual(rows[0].views,1234)
        self.assertIn('i.ytimg.com/vi/abcdefghijk',rows[0].cover);self.assertEqual(rows[0].author,'Creator')

    def test_no_extension_is_needed_to_prepare_native_browser(self):
        browser=LoginBrowser(Settings())
        try:
            with patch('app.background_browser.BackgroundBrowser.prepare') as ready, patch('app.browser_pairing.PairingAssist',side_effect=AssertionError('manual install helper')):
                browser.ensure_connected();ready.assert_called_once();self.assertTrue(browser.active)
        finally:browser.close()

    def test_verified_page_is_reused_once_without_second_navigation(self):
        browser=NativeBrowser(Settings());browser.hwnd=17;browser.active=True
        with patch.object(browser,'_call',return_value={'rows':[record()]}) as call:
            first=browser.search_page('nature');browser.remember_verification('nature')
            self.assertEqual(browser.search_page('nature'),first);self.assertEqual(call.call_count,1)
            browser.search_page('nature');self.assertEqual(call.call_count,2)
        browser.close()

    def test_continuation_is_deduplicated_pinned_and_retry_stable(self):
        browser=NativeBrowser(Settings());browser.hwnd=17;browser.tab='3.1.22';browser.active=True
        with patch.object(browser,'_call',side_effect=[{'rows':[record()]},{'rows':[record(),record('12345678901')]}]) as call:
            _,cfg=browser.search_page('nature');session=cfg['_rf_browser_session'];token=browser.next_token
            result=browser.continuation({'continuation':token},session=session)
            self.assertEqual(browser.continuation({'continuation':token},session=session),result);self.assertEqual(call.call_count,2)
            self.assertEqual(call.call_args.kwargs['known'],'abcdefghijk')
            self.assertEqual((call.call_args.kwargs['hwnd'],call.call_args.kwargs['tab']),(17,'3.1.22'))
            rows=YouTubeAdapter()._parse_results(result,Config().get_site('ytb'))
            self.assertEqual([r.extra['video_id'] for r in rows],['12345678901'])
            with self.assertRaisesRegex(RuntimeError,'会话'):browser.continuation({'continuation':browser.next_token},session=['native','foreign'])
        browser.close()

    def test_scan_pins_existing_tab_and_playback_recovery_rebinds_only_search(self):
        browser=NativeBrowser(Settings())
        with patch.object(browser,'_call',side_effect=[{'hwnd':17,'tab':'3.1.22'},
                {'rows':[record()],'tab':'3.1.22'},
                {'rows':[record(),record('12345678901')],'tab':'3.1.33'},
                {'rows':[record('22222222222')],'tab':'3.1.33'}]) as call:
            _,cfg=browser.search_page('nature');session=cfg['_rf_browser_session']
            self.assertEqual(call.call_args.kwargs,{'hwnd':17,'tab':'3.1.22','query':'nature'})
            browser.continuation({'continuation':browser.next_token},session=session)
            self.assertEqual(browser.tab,'3.1.33')
            browser.continuation({'continuation':browser.next_token},session=session)
            self.assertEqual(call.call_args.kwargs['tab'],'3.1.33')
            self.assertEqual([c.args[0] for c in call.call_args_list],['scan','open','more','more'])
        browser.close()

    def test_recovery_failure_does_not_advance_session_or_change_user_tab(self):
        browser=NativeBrowser(Settings());browser.hwnd=17;browser.tab='original';browser.active=True
        with patch.object(browser,'_call',side_effect=[{'rows':[record()]},RuntimeError('page changed'),
                {'rows':[record('12345678901')],'tab':'background'}]):
            _,cfg=browser.search_page('nature');session=cfg['_rf_browser_session'];token=browser.next_token
            with self.assertRaisesRegex(RuntimeError,'page changed'):
                browser.continuation({'continuation':token},session=session)
            self.assertEqual((browser.tab,browser.next_token),('original',token))
            reply=browser.continuation({'continuation':token},session=session)
            self.assertEqual(browser.tab,'background');self.assertEqual(len(browser.seen),2)
            self.assertEqual(browser.continuation({'continuation':token},session=session),reply)
        browser.close()

    def test_empty_scroll_remains_unknown_and_failed_token_can_retry(self):
        browser=NativeBrowser(Settings());browser.hwnd=17;browser.active=True
        with patch.object(browser,'_call',side_effect=[{'rows':[record()]},RuntimeError('temporary focus'),{'rows':[record()]}]):
            _,cfg=browser.search_page('nature');session=cfg['_rf_browser_session'];token=browser.next_token
            with self.assertRaises(RuntimeError):browser.continuation({'continuation':token},session=session)
            self.assertEqual(browser.next_token,token)
            reply=browser.continuation({'continuation':token},session=session)
            self.assertTrue(YouTubeAdapter()._continuation(reply))
        browser.close()

    def test_cancel_before_ipc_does_not_launch_helper(self):
        browser=NativeBrowser(Settings());cancel=threading.Event();cancel.set()
        with patch('app.native_browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'取消'):browser._call('scan',cancel)
            launch.assert_not_called()

    def test_stop_only_kills_own_helper(self):
        browser=NativeBrowser(Settings());worker=browser.worker=Mock();worker.poll.return_value=None
        browser._stop();worker.kill.assert_called_once();self.assertIsNone(browser.worker)

    def test_closed_profile_cannot_restart_browser_reader(self):
        browser=NativeBrowser(Settings());browser.close()
        with patch('app.native_browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'已关闭'):browser.ensure_ready()
            launch.assert_not_called()

    def test_bundled_reader_selftest_needs_no_browser(self):
        path=Path(__file__).resolve().parents[1]/'vendor/browser-reader/browser-reader.exe'
        result=subprocess.run([str(path)],input='{"op":"selftest"}\n',text=True,encoding='utf-8',capture_output=True,timeout=10,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0);self.assertTrue(json.loads(result.stdout)['data']['ok'])

if __name__=='__main__':unittest.main(verbosity=2)
