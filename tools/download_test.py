import testenv
import os,sys,json,time,tempfile,threading,unittest,shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.models import SearchResult
from app.config import Config,Settings
from app import downloads as d

class DownloadTests(unittest.TestCase):
    def test_clear_history_is_persistent_preserves_files_and_inflight_jobs(self):
        from app.download_manager import DownloadManager
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config();cfg._storage_dir=tmp;manager=DownloadManager(cfg);ids=[]
            with patch.object(manager,'_schedule'):
                for i,state in enumerate(['done','failed','cancelled','login_required','queued','running','cancelling','done']):
                    key=manager.add(SearchResult(title=str(i),url='https://www.youtube.com/watch?v='+str(i)),{},dict(mode='full'),str(Path(tmp)/'files'))
                    manager.jobs[key]['state']=state;ids.append(key)
            saved=Path(manager.jobs[ids[0]]['folder'])/'keep.txt';saved.write_text('saved content')
            manager.events[ids[-1]]=threading.Event()
            self.assertEqual(manager.clear_history(ids[4]),0)
            self.assertEqual(manager.clear_history(ids[-1]),0)
            self.assertEqual(manager.clear_history(ids[0]),1);self.assertEqual(saved.read_text(),'saved content')
            before=list(manager.jobs)
            with patch.object(manager,'save',side_effect=OSError('disk error')):
                with self.assertRaises(OSError):manager.clear_history()
            self.assertEqual(list(manager.jobs),before)
            self.assertEqual(manager.clear_history(),3);self.assertEqual(set(manager.jobs),set(ids[4:]))
            manager.save();data=json.loads(manager.file.read_text(encoding='utf-8'));self.assertEqual({j['id'] for j in data['jobs']},set(ids[4:]))
            self.assertEqual(saved.read_text(),'saved content');self.assertFalse(manager.events[ids[-1]].is_set())
            manager.events.clear();manager.close()
            loaded=DownloadManager(cfg);self.assertNotIn(ids[0],loaded.jobs);self.assertNotIn(ids[1],loaded.jobs);loaded.close()
    def test_bili_shell_retries_same_public_opus_once_only(self):
        from app import article_download as article
        from unittest.mock import MagicMock
        row=SearchResult(url='https://www.bilibili.com/read/cv123',resource_type='article')
        def response(content):
            value=MagicMock();value.__enter__.return_value=value;value.headers={'Content-Type':'text/html; charset=utf-8'};value.encoding='utf-8'
            value.url='https://www.bilibili.com/opus/123';value.iter_content.return_value=iter([content.encode()]);return value
        with patch.object(article,'HttpClient') as factory:
            client=factory.return_value;client.get.side_effect=[response('<div id="app"></div>'),response('<article>'+'正文内容。'*30+'</article>')]
            self.assertIn('正文',article.fetch_article(row,Settings(),{},threading.Event())['text'])
            self.assertEqual(client.get.call_count,2);self.assertEqual(client.get.call_args.args[0],'https://www.bilibili.com/opus/123')
        with patch.object(article,'HttpClient') as factory:
            client=factory.return_value;client.get.side_effect=[response('<div/>'),response('<div/>')]
            with self.assertRaises(article.MissingArticle):article.fetch_article(row,Settings(),{},threading.Event())
            self.assertEqual(client.get.call_count,2)
    def test_auto_access_anonymous_success_never_reads_login(self):
        from app import download_access as access
        calls=[]
        with patch.object(access,'candidates') as choices:
            result=access.automatic(SearchResult(url='https://www.youtube.com/watch?v=x'),{'browser':'edge'},threading.Event(),lambda auth:calls.append(auth) or 'ok')
        self.assertEqual(result,'ok');self.assertEqual(calls,[{}]);choices.assert_not_called()
    def test_auto_access_only_login_errors_fall_back_and_cancel_stops(self):
        from app import download_access as access
        row=SearchResult(url='https://www.youtube.com/watch?v=x');calls=[];cancel=threading.Event()
        def operation(auth):
            calls.append(auth)
            if not auth:raise d.DownloadError('Sign in to confirm you are not a bot')
            return 'ok'
        with patch.object(access,'candidates',return_value=[{'browser':'edge'}]):
            self.assertEqual(access.automatic(row,{},cancel,operation),'ok')
        self.assertEqual(calls,[{},dict(browser='edge')])
        for error in ['timeout','HTTP 403','disk full','HTTP 404']:
            with patch.object(access,'candidates') as candidates:
                with self.assertRaisesRegex(d.DownloadError,error):access.automatic(row,{},cancel,lambda _:(_ for _ in ()).throw(d.DownloadError(error)))
                candidates.assert_not_called()
        def cancelled(auth):cancel.set();raise d.DownloadError('请先登录')
        with patch.object(access,'candidates') as candidates:
            with self.assertRaises(d.DownloadCancelled):access.automatic(row,{},cancel,cancelled)
            candidates.assert_not_called()
    def test_auto_access_exhaustion_is_actionable_not_infinite(self):
        from app import download_access as access
        calls=[]
        def operation(auth):
            calls.append(auth);raise d.DownloadError('Cookie database failed' if auth else '请先登录')
        with patch.object(access,'candidates',return_value=[{'browser':'edge'},{'browser':'chrome'}]):
            with self.assertRaises(access.LoginRequired):access.automatic(SearchResult(url='https://www.youtube.com/watch?v=x'),{},threading.Event(),operation)
        self.assertEqual(len(calls),3)
    def test_article_extraction_and_safe_html_output(self):
        from app.article_download import extract_article,save_text
        body='这是可公开阅读的正文。'*20
        parsed=extract_article('<html><h1>标题</h1><nav>不要保存导航</nav><article><p>'+body+'</p><script>evil()</script></article></html>')
        self.assertEqual(parsed['title'],'标题');self.assertNotIn('evil',parsed['text']);self.assertNotIn('导航',parsed['text'])
        gbk=('<meta charset="gbk"><article>'+body+'</article>').encode('gbk')
        self.assertIn('公开阅读',extract_article(gbk)['text'])
        for bad in ['<nav>导航</nav>','<body><div>首页列表，没有正文</div></body>']:
            with self.assertRaises(d.DownloadError):extract_article(bad)
        with self.assertRaisesRegex(d.DownloadError,'请先登录'):extract_article('<main>请先登录后阅读</main>')
        with tempfile.TemporaryDirectory() as tmp:
            files=save_text('<script>title</script>','https://example.org/a','正文 <script>alert(1)</script>',tmp,'html',threading.Event())
            data=Path(files[0]).read_text(encoding='utf-8-sig');self.assertNotIn('<script>',data);self.assertIn('&lt;script&gt;',data)
    def test_article_menu_and_per_platform_credentials(self):
        from app.download_manager import DownloadManager
        self.assertEqual(d.platform(SearchResult(url='https://www.bilibili.com/read/cv123',resource_type='article')),'article')
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config();cfg._storage_dir=tmp;manager=DownloadManager(cfg)
            row=SearchResult(url='https://www.pixiv.net/novel/show.php?id=1',resource_type='article')
            manager.remember_access(row,{'file':'local.txt'})
            self.assertEqual(manager.remembered_access(row),{'file':'local.txt'})
            self.assertEqual(manager.remembered_access(SearchResult(url='https://www.youtube.com/watch?v=1')),{});manager.close()
    def test_login_wait_keeps_other_jobs_running_and_retries_same_options(self):
        from app.download_manager import DownloadManager
        from app import download_manager as module,download_access as access
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config();cfg._storage_dir=tmp;manager=DownloadManager(cfg);attempts=[]
            def transfer(row,settings,auth,options,folder,cancel,notify):
                attempts.append((row.title,dict(auth),dict(options)))
                if row.title=='gated' and not auth:raise d.DownloadError('请先登录')
                return []
            opts=dict(mode='silent',height=720,container='mkv')
            with patch.object(module,'download_video',side_effect=transfer),patch.object(access,'candidates',return_value=[]):
                gated=manager.add(SearchResult(title='gated',url='https://www.youtube.com/watch?v=a'),{},opts,str(Path(tmp)/'out'))
                other=manager.add(SearchResult(title='public',url='https://www.youtube.com/watch?v=b'),{},opts,str(Path(tmp)/'out'))
                deadline=time.monotonic()+3
                while manager.events and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(manager.jobs[gated]['state'],'login_required');self.assertEqual(manager.jobs[other]['state'],'done')
            with patch.object(module,'download_video',side_effect=transfer),patch.object(access,'candidates',return_value=[{'browser':'edge'}]):
                manager.retry(gated)
                deadline=time.monotonic()+3
                while manager.events and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(manager.jobs[gated]['state'],'done');self.assertEqual(manager.jobs[gated]['options'],opts)
            self.assertEqual([a[1] for a in attempts if a[0]=='gated'],[{}, {}, {'browser':'edge'}]);manager.close()
    def test_imported_cookies_stay_on_current_host_and_honor_secure(self):
        from http.cookiejar import MozillaCookieJar
        from requests.cookies import create_cookie
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'login.txt';jar=MozillaCookieJar(str(path))
            jar.set_cookie(create_cookie('sid','not-a-real-secret',domain='.pixiv.net',path='/novel',secure=True))
            jar.set_cookie(create_cookie('unrelated','x',domain='.example.org'))
            jar.save(ignore_discard=True,ignore_expires=True)
            http=d.HttpClient(Settings());d.apply_cookies(http,{'file':str(path)},'https://www.pixiv.net/novel/show.php?id=1')
            session=http._session('')
            for url,expected in [('https://www.pixiv.net/novel/show.php?id=1',True),('https://example.org/novel',False),('http://www.pixiv.net/novel',False),('https://www.pixiv.net/other',False)]:
                request=session.prepare_request(requests.Request('GET',url));self.assertEqual(bool(request.headers.get('Cookie')),expected)
            http.close_all()
    def test_allowlist_uses_url_not_source_label(self):
        self.assertEqual(d.platform(SearchResult(url='https://www.youtube.com/watch?v=x')),'youtube')
        self.assertFalse(d.platform(SearchResult(url='https://youtube.com.evil.test/watch?v=x',source='YouTube')))
        self.assertFalse(d.platform(SearchResult(url='https://www.pexels.com/video/1')))
        self.assertFalse(d.platform(SearchResult(url='https://i.pximg.net/1.jpg',resource_type='image')))
        self.assertEqual(d.platform(SearchResult(url='https://www.pixiv.net/artworks/123',resource_type='image')),'pixiv')
        self.assertEqual(d.platform(SearchResult(url='https://i0.hdslb.com/a.jpg',resource_type='image',extra={'page_url':'https://www.bilibili.com/read/cv1'})),'bili-image')
    def test_filename_and_format_bounds(self):
        for name in ['../bad','CON','x:y?z','中文'+('x'*500),' .. ']:
            safe=d.safe_name(name);self.assertNotIn('/',safe);self.assertNotIn(':',safe);self.assertLessEqual(len(safe),91)
        self.assertIn('[height<=720]',d.selector('full',720))
        self.assertEqual(d.selector('audio',1080),'bestaudio/best')
    def test_process_cancel_kills_owned_child(self):
        cancel=threading.Event();threading.Timer(.3,cancel.set).start();start=time.monotonic()
        with self.assertRaises(d.DownloadCancelled):d.run_process([sys.executable,'-c','import time; time.sleep(30)'],cancel)
        self.assertLess(time.monotonic()-start,8)
    def test_shutdown_stops_registered_process_immediately(self):
        cancel=threading.Event();errors=[]
        def run():
            try:d.run_process([sys.executable,'-c','import time; time.sleep(30)'],cancel)
            except d.DownloadCancelled:errors.append('cancelled')
        worker=threading.Thread(target=run);worker.start()
        deadline=time.monotonic()+5
        while cancel not in d._processes and time.monotonic()<deadline:time.sleep(.01)
        self.assertIn(cancel,d._processes)
        d.cancel_processes([cancel]);worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,['cancelled'])
        self.assertNotIn(cancel,d._processes)
    def test_queue_limits_parallelism_and_close_stops_pending(self):
        from app.download_manager import DownloadManager
        import app.download_manager as module
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config();cfg._storage_dir=tmp;manager=DownloadManager(cfg);started=[]
            def transfer(row,settings,auth,options,folder,cancel,notify):
                started.append(row.url);cancel.wait(5);raise d.DownloadCancelled('已取消')
            with patch.object(module,'download_video',side_effect=transfer):
                for i in range(4):manager.add(SearchResult(title=str(i),url='https://www.youtube.com/watch?v='+str(i)),{},dict(mode='full'),str(Path(tmp)/'files'))
                deadline=time.monotonic()+3
                while len(started)<2 and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(len(started),2)
                self.assertEqual(sum(j['state']=='queued' for j in manager.snapshot()),2)
                manager.close()
                deadline=time.monotonic()+3
                while manager.events and time.monotonic()<deadline:time.sleep(.01)
                self.assertFalse(manager.events);self.assertEqual(len(started),2)
                self.assertTrue(all(j['state']=='cancelled' for j in manager.snapshot()))
    def test_components_present_and_no_ambient_configuration(self):
        args=d.base_args(Settings(), 'bilibili',{})
        self.assertIn('--ignore-config',args);self.assertIn('--no-remote-components',args)
        self.assertIn('deno:'+d.binary('deno'),args)
        self.assertEqual(args[args.index('--proxy')+1],'')
    def test_cookie_file_is_copied_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            original=Path(tmp)/'source.txt';original.write_text('secret')
            work=Path(tmp)/'work';work.mkdir();auth=d.cookie_copy({'file':str(original)},work)
            Path(auth['file']).write_text('changed');self.assertEqual(original.read_text(),'secret')
    def test_pixiv_error_is_not_empty_success(self):
        with patch.object(d.HttpClient,'get_json',return_value={'error':True,'message':'需要登录'}):
            with self.assertRaisesRegex(d.DownloadError,'需要登录'):
                d.inspect_resource(SearchResult(url='https://www.pixiv.net/artworks/123',resource_type='image'),Settings(),{},threading.Event())
    def test_cancelled_native_transfer_does_not_commit_file(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            cancel=threading.Event();response=MagicMock();response.__enter__.return_value=response;response.headers={'Content-Length':'4'}
            def chunks(*args):yield b'ab';cancel.set();yield b'cd'
            response.iter_content=chunks
            target=Path(tmp)/'test.jpg'
            with self.assertRaises(d.DownloadCancelled):d.stream_file(SimpleNamespace(get=lambda *a,**kw:response),'https://test/a',target,'',cancel,lambda d:None,1,1)
            self.assertFalse(target.exists())
    def test_queue_cancel_duplicate_and_profile_restart(self):
        from app.download_manager import DownloadManager
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config();cfg._storage_dir=str(Path(tmp)/'a');manager=DownloadManager(cfg)
            row=SearchResult(title='one',url='https://www.youtube.com/watch?v=one')
            with patch.object(manager,'_schedule'):
                identity=manager.add(row,{},dict(mode='full'),str(Path(tmp)/'files'))
                with self.assertRaises(d.DownloadError):manager.add(row,{},dict(mode='full'),str(Path(tmp)/'files'))
                manager.cancel(identity);self.assertEqual(manager.jobs[identity]['state'],'cancelled')
                manager.retry(identity);self.assertEqual(manager.jobs[identity]['state'],'queued')
            manager.close();other=DownloadManager(cfg);self.assertEqual(other.jobs[identity]['state'],'cancelled');other.close()
            cfg2=Config();cfg2._storage_dir=str(Path(tmp)/'b');clean=DownloadManager(cfg2);self.assertFalse(clean.jobs);clean.close()

class DownloadMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.source=cls.root/'test.mp4'
        d.run_process([d.binary('ffmpeg'),'-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=320x240:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac','-shortest',str(cls.source)],threading.Event())
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_real_ffmpeg_silent_output_removes_audio_and_full_keeps_it(self):
        real=d.run_process
        for mode in ['full','silent']:
            folder=self.root/mode;folder.mkdir();cancel=threading.Event()
            def run(args,event,notify=lambda _:None,timeout=180):
                if args[0]==d.binary('yt-dlp'):
                    dest=folder/'.work/media.mp4';shutil.copyfile(self.source,dest)
                    notify('RF_FILE:'+json.dumps(str(dest)));return []
                return real(args,event,notify,timeout)
            with patch.object(d,'run_process',side_effect=run):
                files=d.download_video(SearchResult(title=mode,url='https://www.youtube.com/watch?v=test'),Settings(),{},dict(mode=mode,height=360,container='mp4'),folder,cancel,lambda _:None)
            streams=d.media_info(files[0],cancel);types={s['codec_type'] for s in streams}
            self.assertIn('video',types);self.assertEqual('audio' in types,mode=='full')
            self.assertFalse((folder/'.work').exists())
    def test_ugoira_real_mp4_gif_and_path_validation(self):
        from PIL import Image
        import io,zipfile
        for fmt in ['mp4','gif']:
            folder=self.root/('ugoira-'+fmt);folder.mkdir();archive=folder/'animation.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for i,color in enumerate(['red','blue']):
                    data=io.BytesIO();Image.new('RGB',(64,64),color).save(data,format='PNG');z.writestr('%06d.png'%i,data.getvalue())
            frames=[{'file':'000000.png','delay':100},{'file':'000001.png','delay':200}]
            output=d.convert_ugoira(archive,frames,fmt,threading.Event(),lambda _:None)
            self.assertTrue(output.is_file());self.assertGreater(output.stat().st_size,100)
            with self.assertRaises(d.DownloadError):d.convert_ugoira(archive,[{'file':'../escape.png','delay':1}],fmt,threading.Event(),lambda _:None)

import access_test
class DownloadUITests(unittest.TestCase):
    setUpClass=classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp=access_test.AccessUITests.setUp
    tearDown=access_test.AccessUITests.tearDown
    def test_download_center_minimize_hide_restore_and_clear_actions(self):
        from PySide6.QtCore import Qt
        manager=self.win.downloads;ids=[]
        with patch.object(manager,'_schedule'):
            for i,state in enumerate(['done','failed','queued']):
                key=manager.add(SearchResult(title=str(i),url='https://www.youtube.com/watch?v='+str(i)),{},dict(mode='full'),str(manager.root/'files'))
                manager.jobs[key]['state']=state;ids.append(key)
        self.win._show_download_center();popup=self.win._download_center;self.app.processEvents()
        self.assertTrue(popup.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint)
        from PySide6.QtWidgets import QFrame,QPushButton
        self.assertIsNone(popup.findChild(QFrame,'DialogFooter'))
        self.assertFalse(any(b.text() in ('最小化','隐藏到后台') for b in popup.findChildren(QPushButton)))
        popup.showMinimized();self.app.processEvents();self.assertTrue(popup.isMinimized());self.assertFalse(self.win.isMinimized())
        self.win._show_download_center();self.app.processEvents();self.assertIs(self.win._download_center,popup);self.assertFalse(popup.isMinimized())
        popup.hide();self.app.processEvents();self.assertFalse(popup.isVisible());self.assertFalse(popup._finished)
        manager.jobs[ids[2]].update(state='running',percent=37);manager.emit_changed();self.app.processEvents()
        self.win._show_download_center();self.app.processEvents();self.assertIs(self.win._download_center,popup)
        self.assertEqual(popup.cards[ids[2]].progress.value(),37)
        popup.cards[ids[0]].clear.click();self.assertNotIn(ids[0],manager.jobs);self.assertNotIn(ids[0],popup.cards)
        popup.clear_all.click();self.assertEqual(set(manager.jobs),{ids[2]});self.assertFalse(popup.clear_all.isEnabled())
        popup.close();self.app.processEvents();self.assertTrue(popup._finished)
        self.win._show_download_center();self.app.processEvents();new=self.win._download_center;self.assertIsNot(new,popup)
        manager.jobs[ids[2]]['state']='cancelled';manager.emit_changed();self.app.processEvents()
        new.clear_all.click();self.assertFalse(new.cards);self.assertTrue(new.empty.isVisible());new.close()
    def test_menu_scope_and_real_choices(self):
        from app.ui.widgets import ResultCard
        from app.ui.downloads import DownloadDialog
        supported=SearchResult(title='视频',url='https://www.youtube.com/watch?v=test')
        card=ResultCard(supported,self.win);self.assertIn('download',[a.data() for a in card._menu().actions()])
        menu=card._menu();action=menu.actions()[0];self.assertEqual(action.data(),'download')
        called=[];card.download_requested.connect(called.append)
        action.defaultWidget().click();self.assertEqual(called,[supported])
        self.assertFalse(card.download_hint.isHidden())
        card2=ResultCard(SearchResult(url='https://example.org/video'),self.win);self.assertNotIn('download',[a.data() for a in card2._menu().actions()])
        self.assertTrue(card2.download_hint.isHidden())
        with patch.object(self.win.downloads,'analyze',return_value='probe'):
            dialog=DownloadDialog(self.win.downloads,supported,self.win)
        dialog.resolved('probe',dict(kind='video',title='video',has_video=True,has_audio=True,heights=[1080,720,360],count=1),'')
        self.assertEqual(dialog.mode.count(),3);self.assertEqual(dialog.quality.count(),4)
        dialog.mode.setCurrentIndex(dialog.mode.findData('audio'));self.assertEqual(dialog.format.count(),5)
        self.assertTrue(dialog.start.isEnabled())
        self.assertFalse(hasattr(dialog,'auth'));self.assertTrue(dialog.recovery.isHidden())
        self.assertFalse(dialog.spinner._timer.isActive())
        with patch.object(self.win.downloads,'analyze',return_value='next'):
            dialog.analyze()
        self.assertFalse(dialog.start.isEnabled());self.assertTrue(dialog.spinner._timer.isActive())
        dialog.resolved('probe',{},'stale');self.assertTrue(dialog.spinner._timer.isActive())
        dialog.resolved('next',{'needs_login':True},'请先登录');self.assertFalse(dialog.spinner._timer.isActive());self.assertFalse(dialog.recovery.isHidden())
        dialog.reject();dialog.deleteLater();card.deleteLater();card2.deleteLater()
    def test_progress_keeps_cancel_button_alive(self):
        from app.ui.downloads import DownloadCenter
        with patch.object(self.win.downloads,'_schedule'):
            identity=self.win.downloads.add(SearchResult(title='one',url='https://www.youtube.com/watch?v=one'),{},dict(mode='full'),str(self.win.downloads.root/'files'))
        self.win.downloads.jobs[identity]['state']='running'
        popup=DownloadCenter(self.win.downloads,self.win);old=popup.cards[identity].action
        self.win.downloads.jobs[identity]['percent']=31;popup.refresh();self.assertIs(popup.cards[identity].action,old)
        self.assertEqual(popup.cards[identity].progress.value(),31)
        self.assertEqual(popup.stats['active'].text(),'1')
        popup.filter.setCurrentIndex(popup.filter.findData('done'));self.assertTrue(popup.cards[identity].isHidden())
        popup.filter.setCurrentIndex(popup.filter.findData('all'));self.assertFalse(popup.cards[identity].isHidden())
        self.win.downloads.events[identity]=threading.Event();old.click()
        self.assertEqual(self.win.downloads.jobs[identity]['state'],'cancelling')
        self.assertTrue(self.win.downloads.events[identity].is_set());self.assertFalse(old.isEnabled())
        self.win.downloads.events.pop(identity);self.win.downloads.jobs[identity]['state']='cancelled';popup.refresh()
        with patch.object(self.win.downloads,'_schedule'):old.click()
        self.assertEqual(self.win.downloads.jobs[identity]['state'],'queued')
        self.assertIs(popup.cards[identity].action,old)
        self.win.downloads.jobs[identity]['state']='cancelled';popup.reject();popup.deleteLater()

if __name__=='__main__':unittest.main(verbosity=2)
