"""Real owned-browser playback and transfer check, explicitly requested by QA."""
import threading,time
from .models import SearchResult

def run(app,win,log,url='https://www.aigei.com/item/xia_yu_sheng_1052.html'):
    row=SearchResult(title='自动播放提取验收',url=url,resource_type='sound')
    response={};ready=threading.Event()
    def resolved(identity,info,error):response.update(identity=identity,info=info,error=error);ready.set()
    win.downloads.resolved.connect(resolved);started=time.monotonic()
    try:
        identity=win.downloads.analyze(row,auto_browser=True);deadline=time.monotonic()+190
        while not ready.is_set() and time.monotonic()<deadline:time.sleep(.05);app.processEvents()
        if not ready.is_set() or response['identity']!=identity or response['error']:raise RuntimeError('自动播放解析失败：'+response.get('error','timeout'))
        chosen=response['info']['web_variants'][0]
        if not chosen.get('automatic_browser'):raise RuntimeError('自动提取未使用独立浏览器')
        key=win.downloads.add(row,{},dict(mode='web_sound',aigei_variant=chosen['key'],aigei_automatic=True,audio_format='best'),str(win.downloads.root/'auto-output'))
        deadline=time.monotonic()+190
        while win.downloads.jobs[key]['state'] in ('running','queued','cancelling') and time.monotonic()<deadline:time.sleep(.05);app.processEvents()
        job=win.downloads.jobs[key]
        if job['state']!='done':raise RuntimeError('自动提取下载失败：'+job.get('error',job['state']))
        if 'kbps' not in job.get('extraction_label',''):raise RuntimeError('下载未校验音质')
        from pathlib import Path
        log('Aigei automatic real browser PASS: no extension, no pairing, no manual playback; '+job['extraction_label'])
        log('Aigei automatic file: bytes='+str(Path(job['files'][0]).stat().st_size)+'; elapsed=%.2fs'%(time.monotonic()-started))
        # Open another download panel through its normal constructor: this must
        # reuse the page automatically, with no second one-click action.
        from .ui.downloads import DownloadDialog
        next_url=('https://www.aigei.com/item/chi_fan_de_shen_1.html' if 'xia_yu' in url else 'https://www.aigei.com/item/xia_yu_sheng_1052.html')
        dialog=DownloadDialog(win.downloads,SearchResult(title='复用浏览器验收',url=next_url,resource_type='sound'),win)
        try:
            assert dialog.auto_extract.isHidden()
            deadline=time.monotonic()+190
            while dialog.probe and time.monotonic()<deadline:time.sleep(.05);app.processEvents()
            if not dialog.info or not dialog.start.isEnabled():raise RuntimeError('已有浏览器时下一条音效未自动解析：'+dialog.status.text())
            assert dialog.auto_extract.isHidden()
            log('Aigei reuse PASS: next resource resolved without one-click; button hidden')
            from websockets.sync.client import connect
            browser=win.downloads.aigei_browser
            with connect(browser.address,proxy=None,open_timeout=2,close_timeout=1) as ws:
                browser.rpc(ws,'Target.closeTarget',dict(targetId=browser.target),threading.Event(),time.monotonic()+3)
            win.downloads.check_browser_state();deadline=time.monotonic()+6
            while (browser.active or dialog.auto_extract.isHidden()) and time.monotonic()<deadline:time.sleep(.05);app.processEvents()
            if browser.active or dialog.auto_extract.isHidden() or dialog.start.isEnabled():raise RuntimeError('提取页面关闭后按钮未恢复或仍可提交旧解析')
            log('Aigei page-close PASS: actual target closed; one-click restored, stale format submission disabled')
        finally:dialog.reject();dialog.deleteLater();app.processEvents()
    finally:win.downloads.resolved.disconnect(resolved)
