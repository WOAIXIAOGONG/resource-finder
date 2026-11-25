"""Bounded background queue owned by one local profile."""
import json,os,time,threading,uuid
from pathlib import Path
from dataclasses import asdict
from PySide6.QtCore import QObject,Signal
from .config import config_dir,Settings
from .models import SearchResult
from .downloads import (inspect_resource,download_video,download_native,platform,safe_name,
                         DownloadError,DownloadCancelled,redact,cancel_processes)
from .download_access import automatic,LoginRequired,access_key

class DownloadManager(QObject):
    changed=Signal()
    resolved=Signal(str,object,str)
    inspecting=Signal(str,str)
    browser_state=Signal(bool)
    def __init__(self,config,parent=None):
        super().__init__(parent);self.config=config
        self.root=Path(config._storage_dir or config_dir());self.file=self.root/'downloads.json'
        self.jobs={};self.events={};self.probes={};self.lock=threading.RLock();self.closed=False
        from .aigei_browser import AigeiBrowser
        self.aigei_browser=AigeiBrowser(self.root,self.config.settings)
        self._browser_check_pending=False
        self.preferences={'directory':str(self.root/'downloads'),'browser':'','file':''}
        try:
            data=json.loads(self.file.read_text(encoding='utf-8'))
            self.preferences.update(data.get('preferences',{}))
            for job in data.get('jobs',[]):
                if job['state'] in ('queued','running','cancelling'):job.update(state='cancelled',phase='上次退出已停止，可重试')
                legacy=any(key in job.get('options',{}) for key in ('aigei_file','aigei_fee','aigei_paid_confirmed'))
                if legacy or job['state']=='mcp_required':
                    for key in ('aigei_file','aigei_fee','aigei_paid_confirmed'):job.get('options',{}).pop(key,None)
                    if job['state'] not in ('done','cancelled'):job.update(state='failed',phase='请重新解析网页',error='旧版爱给下载通道已移除，请从资源封面重新解析网页文件')
                self.jobs[job['id']]=job
        except (OSError,ValueError,KeyError,TypeError):pass
    def snapshot(self):
        with self.lock:return [dict(j) for j in self.jobs.values()]
    def emit_changed(self):
        if not self.closed:
            try:self.changed.emit()
            except RuntimeError:pass
    def save(self):
        with self.lock:
            self.root.mkdir(parents=True,exist_ok=True)
            tmp=self.file.with_suffix('.json.tmp')
            active=[j for j in self.jobs.values() if j['state'] in ('queued','running','cancelling')]
            finished=[j for j in self.jobs.values() if j['state'] not in ('queued','running','cancelling')][-200:]
            tmp.write_text(json.dumps({'preferences':self.preferences,'jobs':active+finished},ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(tmp,self.file)
    def remembered_access(self,row):
        with self.lock:return dict(self.preferences.get('access',{}).get(access_key(row),{}))
    def remember_access(self,row,auth):
        with self.lock:
            self.preferences.setdefault('access',{})[access_key(row)]=dict(auth);self.save()
    def download_settings(self):
        settings=Settings.from_dict(self.config.settings.to_dict())
        # Runtime bridge belongs to this profile; never serialize it into a task.
        settings._login_browser=getattr(self.config.settings,'_login_browser',None)
        settings._aigei_browser=self.aigei_browser
        return settings
    def check_browser_state(self):
        """GUI callers receive a bounded asynchronous health check, never a launch."""
        with self.lock:
            if self.closed or self._browser_check_pending:return
            self._browser_check_pending=True
        def work():
            ready=self.aigei_browser.refresh_ready()
            with self.lock:self._browser_check_pending=False
            if not self.closed:
                try:self.browser_state.emit(ready)
                except RuntimeError:pass
        threading.Thread(target=work,daemon=True,name='download-browser-state').start()
    def analyze(self,row,auth=None,*,auto_browser=False):
        identity=uuid.uuid4().hex;cancel=threading.Event();self.probes[identity]=cancel
        settings=self.download_settings()
        settings._aigei_auto_start=auto_browser
        def work():
            result={};error=''
            def progress(message):
                if not cancel.is_set() and not self.closed:
                    try:self.inspecting.emit(identity,message)
                    except RuntimeError:pass
            settings._download_progress=progress
            try:result=automatic(row,auth or self.remembered_access(row),cancel,
                lambda login:inspect_resource(row,settings,login,cancel),progress)
            except Exception as exc:error=redact(str(exc));result={'needs_login':isinstance(exc,LoginRequired),'needs_browser':bool(getattr(exc,'needs_browser',False))}
            self.probes.pop(identity,None)
            if not cancel.is_set() and not self.closed:
                try:self.resolved.emit(identity,result,error)
                except RuntimeError:pass
        threading.Thread(target=work,daemon=True,name='download-inspect').start()
        return identity
    def cancel_probe(self,identity):
        event=self.probes.get(identity)
        if event:event.set()
    def add(self,row,auth,options,directory):
        if not platform(row):raise DownloadError('此来源尚未支持下载')
        parent=Path(directory).expanduser().resolve();parent.mkdir(parents=True,exist_ok=True)
        with self.lock:
            for j in self.jobs.values():
                if j['state'] in ('queued','running','cancelling') and j['row']['url']==row.url and (j['options']==options):
                    raise DownloadError('相同资源与下载选项已在队列中')
            identity=uuid.uuid4().hex
            folder=parent/(safe_name(row.title)+'-'+identity[:8]);folder.mkdir()
            job={'id':identity,'row':row.to_dict(),'auth':dict(auth),'options':dict(options),'folder':str(folder),
                 'state':'queued','phase':'等待下载','percent':0,'speed':0,'files':[],'error':'','created':time.time()}
            self.jobs[identity]=job;self.preferences.update(directory=str(parent),**auth)
            try:self.save()
            except Exception:
                self.jobs.pop(identity,None);folder.rmdir();raise
        self.emit_changed();self._schedule();return identity
    def _schedule(self):
        with self.lock:
            if self.closed:return
            available=2-sum(j['state'] in ('running','cancelling') for j in self.jobs.values())
            for job in self.jobs.values():
                if available<=0:break
                if job['state']!='queued':continue
                job.update(state='running',phase='准备下载');event=threading.Event();self.events[job['id']]=event;available-=1
                threading.Thread(target=self._work,args=(job,event),daemon=True,name='download-job').start()
        self.emit_changed()
    def _work(self,job,cancel):
        tick=[0.0]
        def progress(data):
            with self.lock:
                if cancel.is_set() or self.closed:return
                job.update(data)
            if time.monotonic()-tick[0]>.15:tick[0]=time.monotonic();self.emit_changed()
        try:
            row=SearchResult(**{k:v for k,v in job['row'].items() if k in SearchResult.__dataclass_fields__});settings=self.download_settings()
            settings._aigei_auto_start=bool(job['options'].get('aigei_automatic'))
            settings._download_progress=lambda message:progress({'phase':message,'percent':-1})
            fn=download_video if platform(row) in ('bilibili','youtube') else download_native
            files=automatic(row,self.remembered_access(row) or job.get('auth',{}),cancel,
                lambda login:fn(row,settings,login,job['options'],Path(job['folder']),cancel,progress),
                lambda message:progress({'phase':message,'percent':-1}))
            with self.lock:
                if cancel.is_set():raise DownloadCancelled('已取消')
                job.update(state='done',phase='已完成',percent=100,files=files,speed=0)
        except DownloadCancelled:
            with self.lock:job.update(state='cancelled',phase='已取消',speed=0)
        except LoginRequired as exc:
            with self.lock:job.update(state='login_required',phase='需要登录后继续',error=redact(str(exc)),speed=0)
        except Exception as exc:
            with self.lock:
                job.update(state='cancelled' if cancel.is_set() else 'failed',phase='已取消' if cancel.is_set() else '下载失败',error='' if cancel.is_set() else redact(str(exc)),speed=0)
        finally:
            with self.lock:
                self.events.pop(job['id'],None)
                try:self.save()
                except OSError as exc:job['error']+='\n下载记录保存失败：'+str(exc)
            self.emit_changed();self._schedule()
    def cancel(self,identity):
        with self.lock:
            j=self.jobs.get(identity)
            if not j:return
            if j['state']=='queued':j.update(state='cancelled',phase='已取消')
            elif j['state']=='running':j.update(state='cancelling',phase='正在取消');self.events[identity].set()
            self.save()
        self.emit_changed()
    def retry(self,identity):
        with self.lock:
            j=self.jobs.get(identity)
            if not j or j['state'] not in ('failed','cancelled','login_required'):return
            j.update(state='queued',phase='等待重试',error='',percent=0,speed=0)
            self.save()
        self.emit_changed();self._schedule()
    def clearable_ids(self):
        with self.lock:
            return {k for k,j in self.jobs.items() if j['state'] in ('done','failed','cancelled','login_required') and k not in self.events}
    def clear_history(self,identity=None):
        """Forget ended jobs only. Never touch downloads, folders or active workers."""
        with self.lock:
            if self.closed:return 0
            keys=self.clearable_ids()
            if identity is not None:keys.intersection_update({identity})
            if not keys:return 0
            original=dict(self.jobs)
            for key in keys:self.jobs.pop(key)
            try:self.save()
            except Exception:
                self.jobs=original;raise
        self.emit_changed();return len(keys)
    def close(self):
        with self.lock:
            self.closed=True
            events=list(self.probes.values())+list(self.events.values())
            for event in events:event.set()
            for j in self.jobs.values():
                if j['state'] in ('queued','running','cancelling'):j.update(state='cancelled',phase='已退出，下载停止')
            if self.jobs:
                try:self.save()
                except OSError:pass
        cancel_processes(events)
        self.aigei_browser.close()
