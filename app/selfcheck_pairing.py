"""Frozen background login and terminal continuation, with browser fixtures."""
import json
from pathlib import Path
import subprocess
import threading
import time


def run(app, win, log, shot=''):
    from .background_browser import BackgroundBrowser
    from .config import app_dir
    from PySide6.QtWidgets import QPushButton
    reader=Path(app_dir())/'vendor/browser-reader/browser-reader.exe'
    checked=subprocess.run([str(reader)],input='{"op":"selftest"}\n',text=True,encoding='utf-8',capture_output=True,timeout=10,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert checked.returncode==0 and json.loads(checked.stdout)['data']['ok']
    original_prepare=BackgroundBrowser.prepare;original_search=BackgroundBrowser.search_page
    original_remember=BackgroundBrowser.remember_verification
    original_more=BackgroundBrowser.continuation
    old_thumbnails=win.settings.show_thumbnails;old_sites=[(site,site.enabled) for site in win.config.sites];calls=[]
    def prepare(self,cancel=None,progress=None):calls.append('prepare')
    def fixture(self,query,cancel=None):
        if self.prepared:
            result,self.prepared=self.prepared,None
            return result
        calls.append('query')
        result=({'contents':{'videoRenderer':dict(videoId='abcdefghijk',title={'simpleText':'后台登录续搜验收'},lengthText={'simpleText':'0:12'}),
                            'continuationItemRenderer':{'continuationEndpoint':{'continuationCommand':{'token':'fixture-next'}}}}},
                {'LOGGED_IN':True,'INNERTUBE_CONTEXT':{'client':{'clientName':'WEB','clientVersion':'fixture'}},'_rf_browser_session':['background','fixture']})
        self._fixture=result
        return result
    def remember(self,query):self.prepared=self._fixture
    def more(self,body,cancel=None,session=None):
        calls.append('more')
        assert body['continuation']=='fixture-next'
        return {'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':[
            {'itemSectionRenderer':{'contents':[{'messageRenderer':{'text':{'runs':[{'text':'无更多结果'}]}}}]}}]}}]}
    try:
        BackgroundBrowser.prepare=prepare;BackgroundBrowser.search_page=fixture;BackgroundBrowser.remember_verification=remember
        BackgroundBrowser.continuation=more
        win._set_mode('direct');win._clear_results();win._query='fixture';win._cancel=threading.Event()
        win.settings.show_thumbnails=False
        manager=win.logins;site=win.config.get_site('ytb');site.enabled=True
        for configured in win.config.sites:configured.enabled=configured.id==site.id
        scope='direct:'+str(id(win._cancel))
        manager.require(scope,site,'fixture',1,'请在专用浏览器登录后点已登录；无需扩展。',win._cancel)
        app.processEvents();dialog=win._login_dialogs[(scope,site.id)]
        assert sorted(b.text() for b in dialog.findChildren(QPushButton))==sorted(['本轮跳过该平台','已登录，继续搜索','打开专用登录窗口'])
        dialog.verify_button.click();deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            time.sleep(.03);app.processEvents()
            if win._cards and not win._paging and (scope,site.id) not in win._login_dialogs:break
        assert manager.snapshot(scope)[site.id]['state']=='ready'
        assert win._cards[0].time_badge.text()=='0:12'
        assert calls==['prepare','query'],calls
        win._load_more();deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            time.sleep(.03);app.processEvents()
            if not win._paging and site.id in win._site_exhausted:break
        assert site.id in win._site_exhausted
        assert not win._login_dialogs
        assert '没有更多资源' in win.pagination_notice.text()
        win._load_more();win._load_more();app.processEvents()
        assert calls==['prepare','query','more'],calls
        if shot:win.grab().save(shot)
        log('background browser login ok: bundled owner-helper selftest PASS; confirm -> prepare -> query -> verified-page reuse -> dialog closes -> results; browser records are fixtures, no real account login or extension installation claimed')
        log('YouTube terminal continuation ok: localized no-more notice ends paging; repeated bottom requests neither re-fetch nor reopen login')
    finally:
        BackgroundBrowser.prepare=original_prepare;BackgroundBrowser.search_page=original_search;BackgroundBrowser.remember_verification=original_remember
        BackgroundBrowser.continuation=original_more
        if win.logins.browser.background:
            win.logins.browser.background.close();win.logins.browser.background=None
            win.logins.browser._background_selected=False
        win.logins.browser.active=False
        win.settings.show_thumbnails=old_thumbnails;win._clear_results()
        for site,enabled in old_sites:site.enabled=enabled
