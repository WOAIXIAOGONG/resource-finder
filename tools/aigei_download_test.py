import testenv
import sys,unittest,tempfile,threading,json,shutil,base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import downloads as d
from app import aigei_extract as web
from app.config import Settings,Config
from app.models import SearchResult

def listing(slug='rain',url='https://fd.aigei.com/src/vdo/mp4/preview.mp4?token=fixture'):
    content='<div fast-video-url="'+base64.b64encode(url.encode()).decode()+'" fast-video-width="128" fast-video-height="72"></div>'
    return '<div class="unit-container">'+content+'<div class="item-view-url"><a href="/item/'+slug+'.html">雨</a></div></div>'
def row(kind='video'):return SearchResult(title='雨',url='https://www.aigei.com/item/rain.html',resource_type=kind)

class SoundDownloadTests(unittest.TestCase):
    def test_one_click_uses_owned_browser_and_never_requires_extension(self):
        settings=Settings();settings._aigei_auto_start=True
        seen=[]
        settings._aigei_browser=SimpleNamespace(active=False,extract=lambda path,cancel,notify:(seen.append(path) or dict(pagePath=path,url='https://s2.aigei.com/src/aud/mp3/rain.mp3',durationSeconds=150,automatic_browser=True)))
        with patch.object(web,'HttpClient',side_effect=AssertionError('No HTML or MCP fallback')):
            info=web.inspect_resource(row('sound'),settings,{},threading.Event())
        self.assertEqual(seen,['/item/rain.html']);self.assertTrue(info['web_variants'][0]['automatic_browser'])
    def playback(self,**kwargs):
        data=dict(pagePath='/item/rain.html',url='https://s2.aigei.com/src/aud/mp3/rain.mp3?token=fixture',durationSeconds=.3,playbackLimitSeconds=0)
        data.update(kwargs);return web.playback_variant(row('sound'),data)
    def test_browser_playback_identity_limits_and_no_mcp_or_html(self):
        variant=self.playback()
        self.assertTrue(variant['browser_playback']);self.assertIn('播放版',variant['label'])
        self.assertEqual(variant['key'],self.playback(url='https://s8.aigei.com/src/aud/mp3/refreshed.mp3?token=new')['key'])
        for data in [dict(pagePath='/item/other.html'),dict(url='https://evil.test/src/aud/mp3/rain.mp3'),
                     dict(durationSeconds=float('nan')),dict(durationSeconds=float('inf')),dict(durationSeconds=-1),
                     dict(durationSeconds=150,playbackLimitSeconds=10),dict(durationSeconds=0,playbackLimitSeconds=10)]:
            with self.assertRaises(web.BrowserMediaRequired):self.playback(**data)
        browser=SimpleNamespace(active=True,aigei_media=lambda path,cancel:dict(pagePath=path,url=variant['url'],durationSeconds=.3,playbackLimitSeconds=0))
        settings=Settings();settings._login_browser=browser
        with patch.object(web,'HttpClient',side_effect=AssertionError('HTML must not be fetched')):
            info=web.inspect_resource(row('sound'),settings,{},threading.Event())
        self.assertIn('不等于原始',info['note'])
    def test_missing_browser_is_actionable_not_cookie_login(self):
        from app.download_access import automatic
        with self.assertRaises(web.BrowserMediaRequired):
            automatic(row('sound'),{},threading.Event(),lambda auth:web.browser_variants(row('sound'),Settings(),threading.Event()))
    def test_dynamic_playback_real_file_verification_and_conversion(self):
        cancel=threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audio=root/'source.mp3'
            d.run_process([d.binary('ffmpeg'),'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=0.3','-codec:a','libmp3lame','-b:a','64k',str(audio)],cancel)
            variant=self.playback();notices=[]
            for fmt in ['best','wav','flac']:
                folder=root/fmt;folder.mkdir()
                with patch.object(web,'resolve',return_value=[variant]) as resolve,patch.object(web,'stream_file',side_effect=lambda h,u,target,*a,**kw:shutil.copyfile(audio,target)):
                    files=web.download_resource(row('sound'),Settings(),{},dict(aigei_variant=variant['key'],audio_format=fmt),folder,cancel,notices.append)
                self.assertTrue(resolve.call_args.kwargs['browser_only']);self.assertIn('网页播放版',Path(files[0]).name)
                self.assertFalse(list(folder.glob('.aigei-web-*')))
                self.assertTrue(any(s['codec_type']=='audio' for s in d.media_info(files[0],cancel)))
            self.assertTrue(any('64kbps' in n.get('extraction_label','') for n in notices))
            with self.assertRaisesRegex(d.DownloadError,'时长与网页不符'):web.verify_playback(audio,dict(expected_duration=150),cancel,lambda _:None)
            garbage=root/'bad.mp3';garbage.write_bytes(b'not audio')
            with self.assertRaises(d.DownloadError):web.verify_playback(garbage,variant,cancel,lambda _:None)
    def test_expired_playback_url_has_actionable_error_and_no_saved_file(self):
        from app.net import NetworkError
        variant=self.playback()
        with tempfile.TemporaryDirectory() as tmp,patch.object(web,'resolve',return_value=[variant]),patch.object(web,'stream_file',side_effect=NetworkError('HTTP403',status=403)):
            with self.assertRaisesRegex(web.BrowserMediaRequired,'已失效'):
                web.download_resource(row('sound'),Settings(),{},dict(aigei_variant=variant['key']),Path(tmp),threading.Event(),lambda _:None)
            self.assertEqual(list(Path(tmp).iterdir()),[])
    def test_runtime_bridge_retained_without_persisting_media_url(self):
        from app.download_manager import DownloadManager
        with tempfile.TemporaryDirectory() as tmp:
            config=Config();config._storage_dir=tmp;bridge=object();config.settings._login_browser=bridge
            manager=DownloadManager(config)
            self.assertIs(manager.download_settings()._login_browser,bridge)
            self.assertNotIn('_login_browser',manager.download_settings().to_dict());manager.close()
    def test_exact_resource_match_and_preview_classification(self):
        result=web.parse_variants(listing(),row(),True)
        self.assertEqual(len(result),1);self.assertTrue(result[0]['preview']);self.assertIn('预览',result[0]['label'])
        self.assertFalse(web.parse_variants(listing(slug='other'),row(),True))
        changed=listing(url='https://fd.aigei.com/src/vdo/mp4/preview.mp4?token=renewed')
        self.assertEqual(web.parse_variants(changed,row(),True)[0]['key'],result[0]['key'])
        self.assertFalse(web.parse_variants('<video src="https://fd.aigei.com/src/vdo/other.mp4"></video>',row()))
    def test_no_mcp_in_resolution_and_old_paid_options_rejected(self):
        settings=Settings();settings._aigei_mcp=SimpleNamespace(call=lambda *a:(_ for _ in ()).throw(AssertionError('No MCP download')))
        http=SimpleNamespace(cancel_event=None,get_text=lambda *a,**kw:listing(),close_all=lambda:None)
        with patch.object(web,'HttpClient',return_value=http):info=d.inspect_resource(row(),settings,{},threading.Event())
        self.assertEqual(info['kind'],'web_video');self.assertNotIn('aigei_files',info)
        with patch.object(web,'resolve') as resolve:
            with self.assertRaises(d.DownloadError):web.download_resource(row(),settings,{},dict(aigei_file='old',aigei_fee=3),Path('.'),threading.Event(),lambda _:None)
            resolve.assert_not_called()
    def test_audio_html_extraction_and_cancellation(self):
        r=row('sound');html='<link rel="canonical" href="'+r.url+'"><audio src="https://fd.aigei.com/src/aud/mp3/rain.mp3"></audio>'
        result=web.parse_variants(html,r);self.assertEqual(len(result),1);self.assertIn('完整性未确认',result[0]['label'])
        cancelled=threading.Event();cancelled.set()
        with self.assertRaises(d.DownloadCancelled):web.resolve(r,Settings(),{},cancelled)
        self.assertFalse(web.parse_variants('<input ftype="audio_mp3" token="opaque">',r,True))
    def test_media_origin_and_endpoint_boundaries(self):
        for url in ['https://aigei.com.evil.test/src/vdo/x.mp4','https://fd.aigei.com/open/api/v1/video/download.mp4','https://user:secret@fd.aigei.com/src/vdo/x.mp4','http://fd.aigei.com/src/vdo/x.mp4','https://127.0.0.1/src/vdo/x.mp4']:
            self.assertFalse(web.media_url(url,'video'))
        for kind in ['sound','video','image']:self.assertEqual(d.platform(row(kind)),'aigei-web')
    def test_real_local_conversion_and_preview_video_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.mp4';cancel=threading.Event()
            d.run_process([d.binary('ffmpeg'),'-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=128x72:d=0.4','-c:v','libx264','-pix_fmt','yuv420p',str(source)],cancel)
            variant=web.parse_variants(listing(),row(),True)[0]
            for fmt in ['mp4','mkv']:
                folder=root/fmt;folder.mkdir()
                with patch.object(web,'resolve',return_value=[variant]),patch.object(web,'stream_file',side_effect=lambda h,u,target,*a:shutil.copyfile(source,target)):
                    files=web.download_resource(row(),Settings(),{},dict(aigei_variant=variant['key'],container=fmt),folder,cancel,lambda _:None)
                self.assertIn('网页预览',Path(files[0]).name);self.assertEqual(Path(files[0]).suffix,'.'+fmt)
                self.assertTrue(any(s['codec_type']=='video' for s in d.media_info(files[0],cancel)));self.assertFalse(list(folder.glob('.aigei-web-*')))
            audio=root/'source.wav';d.run_process([d.binary('ffmpeg'),'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=0.3',str(audio)],cancel)
            from app.sound_download import finish_audio
            for fmt in ['best','mp3','m4a','wav','flac']:
                folder=root/('audio-'+fmt);folder.mkdir();local=folder/'input.wav';shutil.copyfile(audio,local)
                files=finish_audio(local,row('sound'),dict(audio_format=fmt),folder,cancel,lambda _:None)
                self.assertTrue(all(s['codec_type']=='audio' for s in d.media_info(files[0],cancel)))
    def test_legacy_paid_jobs_cannot_resume_old_channel(self):
        from app.download_manager import DownloadManager
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=root/'keep.wav';original.write_bytes(b'keep')
            job=dict(id='old',row=row('sound').to_dict(),state='mcp_required',options=dict(aigei_file='old',aigei_fee=1,aigei_paid_confirmed=True),folder=tmp,files=[str(original)])
            (root/'downloads.json').write_text(json.dumps({'jobs':[job]}),encoding='utf-8');cfg=Config();cfg._storage_dir=tmp;manager=DownloadManager(cfg)
            self.assertEqual(manager.jobs['old']['state'],'failed');self.assertNotIn('aigei_fee',manager.jobs['old']['options'])
            self.assertEqual(manager.clear_history('old'),1);self.assertEqual(original.read_bytes(),b'keep');manager.close()

import access_test
class SoundDownloadUITests(unittest.TestCase):
    setUpClass=classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp=access_test.AccessUITests.setUp
    tearDown=access_test.AccessUITests.tearDown
    def test_existing_page_hides_one_click_and_closed_page_restores_it(self):
        from app.ui.downloads import DownloadDialog
        browser=self.win.downloads.aigei_browser;browser.address='ws://127.0.0.1:9';browser.target='owned';browser._page_ready=True
        with patch.object(self.win.downloads,'analyze',return_value='p'),patch.object(self.win.downloads,'check_browser_state'):
            dialog=DownloadDialog(self.win.downloads,row('sound'),self.win)
        dialog.browser_timer.stop();self.assertTrue(dialog.auto_extract.isHidden())
        variant=web.playback_variant(row('sound'),dict(pagePath='/item/rain.html',url='https://s2.aigei.com/src/aud/mp3/rain.mp3',durationSeconds=150,automatic_browser=True))
        dialog.resolved('p',{'kind':'web_sound','count':1,'web_variants':[variant]},'')
        self.assertTrue(dialog.auto_extract.isHidden());self.assertTrue(dialog.start.isEnabled())
        dialog.browser_state_changed(False) # Discard a stale queued negative check.
        self.assertTrue(dialog.auto_extract.isHidden());self.assertTrue(dialog.start.isEnabled())
        browser._page_ready=False;dialog.browser_state_changed(False)
        self.assertFalse(dialog.auto_extract.isHidden());self.assertFalse(dialog.start.isEnabled())
        browser.address='';dialog.reject();dialog.deleteLater()
    def test_aigei_exposes_one_recovery_button_without_extension_controls(self):
        from app.ui.downloads import DownloadDialog
        from PySide6.QtWidgets import QPushButton
        with patch.object(self.win.downloads,'analyze',return_value='p'):dialog=DownloadDialog(self.win.downloads,row('sound'),self.win)
        dialog.browser_timer.stop();dialog.resolved('p',{'needs_browser':True},'尚未连接')
        self.assertTrue(dialog.details.isHidden());self.assertTrue(dialog.parse.isHidden());self.assertFalse(dialog.auto_extract.isHidden())
        self.assertTrue(dialog.subtitles.isHidden())
        self.assertFalse(any(any(word in b.text() for word in ['扩展','复制浏览器','打开资源网页']) for b in dialog.findChildren(QPushButton)))
        self.assertNotIn('失败',dialog.status.text());self.assertFalse(dialog.start.isEnabled());dialog.reject();dialog.deleteLater()
    def test_one_click_button_starts_browser_work_and_submits_automatic_job(self):
        from app.ui.downloads import DownloadDialog
        with patch.object(self.win.downloads,'analyze',return_value='p'):dialog=DownloadDialog(self.win.downloads,row('sound'),self.win)
        self.assertTrue(dialog.auto_extract.isEnabled())
        dialog.resolved('p',{'needs_browser':True},'点击一键提取')
        self.assertTrue(dialog.auto_extract.isEnabled());self.assertTrue(dialog.parse.isHidden())
        with patch.object(self.win.downloads,'analyze',return_value='auto') as analyze:
            dialog.auto_extract.click();self.assertTrue(analyze.call_args.kwargs['auto_browser'])
        self.assertTrue(dialog.spinner._timer.isActive());self.assertFalse(dialog.auto_extract.isEnabled())
        variant=web.playback_variant(row('sound'),dict(pagePath='/item/rain.html',url='https://s2.aigei.com/src/aud/mp3/rain.mp3',durationSeconds=150,automatic_browser=True))
        dialog.resolved('auto',{'kind':'web_sound','count':1,'web_variants':[variant]},'')
        with patch.object(self.win.downloads,'add') as add:dialog.submit()
        self.assertTrue(add.call_args.args[2]['aigei_automatic']);dialog.deleteLater()
    def test_same_button_retries_when_an_open_browser_cannot_extract(self):
        from app.ui.downloads import DownloadDialog
        with patch.object(self.win.downloads,'analyze',return_value='p'):dialog=DownloadDialog(self.win.downloads,row('sound'),self.win)
        dialog.browser_timer.stop();dialog.resolved('p',{'needs_browser':True},'尚未连接')
        with patch.object(self.win.downloads,'analyze',return_value='auto'):dialog.auto_extract.click()
        browser=self.win.downloads.aigei_browser;browser.address='ws://127.0.0.1:9';browser.target='owned';browser._page_ready=True
        dialog.resolved('auto',{'needs_browser':True},'网页暂时未返回音频地址')
        self.assertFalse(dialog.auto_extract.isHidden());self.assertEqual(dialog.auto_extract.text(),'一键重试')
        self.assertTrue(dialog.details.isHidden());self.assertTrue(dialog.parse.isHidden());self.assertIn('网页暂时',dialog.status.toolTip())
        with patch.object(self.win.downloads,'analyze',return_value='retry') as analyze:
            dialog.auto_extract.click();self.assertTrue(analyze.call_args.kwargs['auto_browser'])
        browser.address='';dialog.reject();dialog.deleteLater()
    def test_web_panel_has_preview_choice_and_no_payment_controls(self):
        from app.ui.downloads import DownloadDialog
        with patch.object(self.win.downloads,'analyze',return_value='p'):dialog=DownloadDialog(self.win.downloads,row(),self.win)
        variants=web.parse_variants(listing(),row(),True)
        dialog.resolved('p',{'kind':'web_video','count':1,'web_variants':variants,'note':'网页预览'},'')
        self.assertEqual(dialog.mode.currentData(),'web_video');self.assertIn('预览',dialog.quality.currentText());self.assertTrue(dialog.subtitles.isHidden())
        from PySide6.QtWidgets import QMessageBox,QPushButton
        self.assertFalse(any('MCP' in w.text() or '铜币' in w.text() for w in dialog.findChildren(QPushButton)))
        with patch.object(self.win.downloads,'add') as add,patch.object(QMessageBox,'question') as confirm:
            dialog.submit();confirm.assert_not_called();options=add.call_args.args[2]
            self.assertEqual(options['aigei_variant'],variants[0]['key']);self.assertNotIn('aigei_fee',options)
        dialog.deleteLater()

if __name__=='__main__':unittest.main(verbosity=2)
