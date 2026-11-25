"""Explicit QA: replay observed playback metadata over real local bridge transport.

The supplied fixture contains an already observed normal playback URL. It does
not exercise Chrome's live player inspection and must not be described as such.
"""
import hashlib,json,threading,time
from pathlib import Path
from websockets.sync.client import connect
from .models import SearchResult

def run(app,win,fixture_path,log):
    fixture=json.loads(Path(fixture_path).read_text(encoding='utf-8'))
    if fixture.get('identity_verified') is False:raise RuntimeError('QA fixture item identity has not been verified')
    row=SearchResult(title=fixture['title'],url=fixture['page_url'],resource_type='sound')
    from .aigei_extract import page_path,playback_variant
    metadata=dict(pagePath=page_path(row.url),url=fixture['url'],durationSeconds=fixture['expected_duration'],playbackLimitSeconds=0)
    playback_variant(row,metadata) # Reject invalid endpoints before any transport.
    browser=win.logins.browser
    socket=connect(browser.connection_info(),origin='chrome-extension://'+'a'*32,proxy=None)
    socket.recv();operations=[]
    def serve_fixture():
        try:
            for raw in socket:
                msg=json.loads(raw)
                if msg.get('type')!='job':continue
                operations.append(msg['op'])
                ok=msg['op']=='aigei_media' and msg.get('pagePath')==metadata['pagePath']
                socket.send(json.dumps(dict(type='result',id=msg['id'],ok=ok,data=metadata if ok else None,error='Unexpected QA operation')))
        except Exception:pass
    threading.Thread(target=serve_fixture,daemon=True).start()
    output={};finished=threading.Event()
    def resolved(identity,info,error):
        output.update(identity=identity,info=info,error=error);finished.set()
    win.downloads.resolved.connect(resolved)
    try:
        identity=win.downloads.analyze(row);deadline=time.monotonic()+30
        while not finished.is_set() and time.monotonic()<deadline:time.sleep(.05);app.processEvents()
        if not finished.is_set() or output['identity']!=identity or output['error']:raise RuntimeError('Playback QA inspection failed: '+output.get('error','timeout'))
        variant=output['info']['web_variants'][0]
        options=dict(mode='web_sound',kind='web_sound',aigei_variant=variant['key'],audio_format=fixture.get('format','best'),extraction_label=variant['label'])
        job_id=win.downloads.add(row,{},options,fixture['output_directory']);deadline=time.monotonic()+180
        while win.downloads.jobs[job_id]['state'] in ('queued','running','cancelling') and time.monotonic()<deadline:time.sleep(.05);app.processEvents()
        job=win.downloads.jobs[job_id]
        if job['state']!='done':raise RuntimeError('Playback QA transfer failed: '+job.get('error',job['state']))
        if len(operations)<2 or any(op!='aigei_media' for op in operations):raise RuntimeError('Playback QA did not re-resolve through fixed bridge operation')
        if not job.get('extraction_label') or 'kbps' not in job['extraction_label']:raise RuntimeError('Playback QA missing measured quality')
        if metadata['url'] in win.downloads.file.read_text(encoding='utf-8'):raise RuntimeError('Playback QA signed URL persisted')
        target=Path(job['files'][0]);digest=hashlib.sha256(target.read_bytes()).hexdigest()
        if fixture.get('sha256') and fixture.get('format','best')=='best' and digest!=fixture['sha256']:raise RuntimeError('Playback QA changed original playback bytes')
        log('Aigei playback QA: real bridge transport + replayed observed metadata; not live extension inspection')
        log('Aigei playback saved: '+job['extraction_label']+'; bytes='+str(target.stat().st_size)+'; sha256='+digest)
    finally:
        win.downloads.resolved.disconnect(resolved);socket.close()
