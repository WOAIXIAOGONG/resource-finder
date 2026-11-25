"""App-owned, isolated Chromium session for an explicit one-click playback job."""
import json,os,re,subprocess,threading,time,uuid
from pathlib import Path
from urllib.parse import quote
from websockets.sync.client import connect
from .downloads import DownloadError,DownloadCancelled

class AigeiBrowser:
    def __init__(self,root,settings):
        self.root=Path(root)/'aigei-browser';self.settings=settings
        self.process=None;self.address='';self.target=None;self.lock=threading.Lock();self.closed=False
        self._page_ready=False
        self._startup_url='data:text/html;charset=utf-8,'+quote('<!doctype html><meta charset="utf-8"><title>正在连接资源页面</title><style>body{background:#181b21;color:#eef0f6;font:18px system-ui;padding:12vh 10vw}p{color:#b6bdcd;font-size:15px}</style><h2>正在连接资源页面…</h2><p>工具正在准备浏览器，连接成功后会自动打开资源。请稍候，无需再次点击。</p>')

    @property
    def active(self):return bool(self.address) and self._page_ready and not self.closed

    def refresh_ready(self,cancel=None):
        """Read the owned target only; never open a window during a health check."""
        if self.closed or not self.address or not self.target:
            self._page_ready=False;return False
        address,target=self.address,self.target;cancel=cancel or threading.Event()
        if cancel.is_set():raise DownloadCancelled('已取消')
        try:
            with connect(address,proxy=None,open_timeout=1,close_timeout=.2,max_size=1024*1024) as ws:
                targets=self.rpc(ws,'Target.getTargets',{},cancel,time.monotonic()+1.5).get('targetInfos',[])
                ready=any(t.get('targetId')==target and t.get('type')=='page' for t in targets)
        except DownloadCancelled:
            if cancel.is_set():raise
            ready=False
        except Exception:ready=False
        # A concurrently created replacement target must not inherit an old check.
        if address==self.address and target==self.target and not self.closed:self._page_ready=ready
        return self.active

    def check(self,cancel,deadline):
        if self.closed or cancel.is_set():raise DownloadCancelled('已取消')
        if time.monotonic()>=deadline:raise DownloadError('等待资源网页超时，请在打开的窗口检查登录或验证提示，再点击一键提取重试')

    @staticmethod
    def executable():
        choices=[]
        for base in [os.environ.get('ProgramFiles(x86)',''),os.environ.get('ProgramFiles',''),os.environ.get('LOCALAPPDATA','')]:
            if base:
                choices.extend(Path(base)/part for part in ['Microsoft/Edge/Application/msedge.exe','Google/Chrome/Application/chrome.exe'])
        # Prefer Edge, regardless of whether Chrome's installation folder is first.
        choices.sort(key=lambda p:p.name!='msedge.exe')
        return next((p for p in choices if p.is_file()),None)

    def endpoint(self):
        try:
            lines=(self.root/'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
            port=int(lines[0]);path=lines[1]
            if not 0<port<65536 or not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+',path):return ''
            return 'ws://127.0.0.1:%d%s'%(port,path)
        except (OSError,ValueError,IndexError):return ''

    def start(self,cancel,deadline):
        self.check(cancel,deadline)
        if self.address:
            try:
                ws=connect(self.address,proxy=None,open_timeout=2,close_timeout=1,max_size=8*1024*1024)
                ws.close();return
            except Exception:self.address='';self.target=None;self._page_ready=False
        executable=self.executable()
        if executable is None:raise DownloadError('未找到Edge或Chrome；一键提取需要本机已安装其中一种浏览器')
        self.root.mkdir(parents=True,exist_ok=True)
        # This directory is private to the app profile, never the user's default browser profile.
        old=self.endpoint()
        if old:
            try:
                ws=connect(old,proxy=None,open_timeout=1,close_timeout=1,max_size=8*1024*1024);ws.close();self.address=old;return
            except Exception:pass
        # Preserve a port file written by a still-starting browser. A new
        # browser replaces a stale file; a forwarded launcher may not rewrite it.
        self.process=subprocess.Popen([str(executable),'--user-data-dir='+str(self.root.resolve()),
            '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--no-first-run',
            '--no-default-browser-check',self._startup_url],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        while not self.address:
            self.check(cancel,deadline)
            candidate=self.endpoint()
            if candidate:
                try:
                    ws=connect(candidate,proxy=None,open_timeout=min(1,max(.1,deadline-time.monotonic())),close_timeout=.2,max_size=8*1024*1024)
                    ws.close();self.address=candidate;break
                except Exception:pass
            # Edge/Chrome's launcher may exit successfully after handing off to
            # its browser process. Readiness is the endpoint, not launcher.poll().
            cancel.wait(.1)

    def rpc(self,ws,method,params,cancel,deadline,session=None):
        self.check(cancel,deadline);key=uuid.uuid4().hex
        # CDP command IDs are integers, not JSON-RPC string IDs.
        identity=int(key[:7],16);message=dict(id=identity,method=method,params=params)
        if session:message['sessionId']=session
        ws.send(json.dumps(message))
        while True:
            self.check(cancel,deadline)
            try:data=json.loads(ws.recv(timeout=.1))
            except TimeoutError:continue
            if data.get('id')!=identity:continue
            if data.get('error'):raise DownloadError('浏览器操作失败：'+str(data['error'].get('message',''))[:300])
            return data.get('result',{})

    def evaluate(self,ws,expression,cancel,deadline,session):
        data=self.rpc(ws,'Runtime.evaluate',dict(expression=expression,returnByValue=True),cancel,deadline,session)
        if data.get('exceptionDetails'):raise DownloadError('资源网页结构暂不支持自动识别，请重试或查看网页提示')
        return data.get('result',{}).get('value')

    def extract(self,page_path,cancel,notify=lambda _:None):
        from .config import app_dir
        from .aigei_extract import page_path as valid_path
        if not valid_path('https://www.aigei.com'+page_path):raise DownloadError('爱给资源路径无效')
        deadline=time.monotonic()+self.settings.timeout
        while not self.lock.acquire(timeout=.1):self.check(cancel,deadline)
        try:
            notify('正在连接浏览器，首次启动请稍候…');self.start(cancel,deadline);notify('正在打开资源网页…')
            script=(Path(app_dir())/'browser-extension/worker.js').read_text(encoding='utf-8')
            try:script=script.split('/* RF_AIGEI_PLAYER_BEGIN */',1)[1].split('/* RF_AIGEI_PLAYER_END */',1)[0]
            except IndexError:raise DownloadError('缺少新版网页识别组件，请使用完整解压的新版工具')
            expression='('+script.strip()+')('+json.dumps(page_path)+')'
            with connect(self.address,proxy=None,open_timeout=3,close_timeout=1,max_size=8*1024*1024) as ws:
                targets=self.rpc(ws,'Target.getTargets',{},cancel,deadline).get('targetInfos',[])
                if self.target and any(t.get('targetId')==self.target and t.get('type')=='page' for t in targets):
                    session=self.rpc(ws,'Target.attachToTarget',dict(targetId=self.target,flatten=True),cancel,deadline)['sessionId']
                    self.rpc(ws,'Page.navigate',dict(url='https://www.aigei.com'+page_path),cancel,deadline,session)
                else:
                    self.target=self.rpc(ws,'Target.createTarget',dict(url='https://www.aigei.com'+page_path),cancel,deadline)['targetId']
                    session=self.rpc(ws,'Target.attachToTarget',dict(targetId=self.target,flatten=True),cancel,deadline)['sessionId']
                self._page_ready=True
                for tab in targets:
                    if tab.get('type')=='page' and tab.get('url')==self._startup_url and tab.get('targetId')!=self.target:
                        self.rpc(ws,'Target.closeTarget',dict(targetId=tab['targetId']),cancel,deadline)
                self.rpc(ws,'Page.bringToFront',{},cancel,deadline,session)
                clicked=False;attempts=0;last_click=0;last_state='';started=time.monotonic()
                while True:
                    self.check(cancel,deadline)
                    state=self.evaluate(ws,expression,cancel,deadline,session) or {}
                    phase=state.get('state','loading')
                    if phase=='verification':message='网页要求验证，请在独立浏览器窗口完成；工具会继续等待…'
                    elif phase=='ready':
                        if clicked and state.get('playing') and state.get('button'):self.click_play(ws,state,cancel,deadline,session)
                        return dict(state,automatic_browser=True)
                    elif phase=='play' and state.get('button') and attempts<3 and time.monotonic()-last_click>2 and state.get('requestStatus') in ('','no','fail'):
                        # Activate only the precisely identified normal play control;
                        # never call the site's download/signature routines directly.
                        if not re.fullmatch(r'\d+',state.get('itemId','')):raise DownloadError('目标音频播放器编号无效')
                        scroll="document.getElementById("+json.dumps('audioItemBox_'+state['itemId'])+")?.querySelector('.audio-player-btn')?.scrollIntoView({block:'center'})"
                        self.evaluate(ws,scroll,cancel,deadline,session)
                        cancel.wait(.3)
                        state=self.evaluate(ws,expression,cancel,deadline,session) or {}
                        if state.get('button',{}).get('obscured'):message='播放按钮被页面提示遮挡，请在独立窗口处理后继续…'
                        else:
                            self.click_play(ws,state,cancel,deadline,session);clicked=True;attempts+=1;last_click=time.monotonic();message='正在正常播放并识别音频地址…'
                    elif phase=='ambiguous':raise DownloadError('页面有多个无法区分的目标播放器，已停止，避免提取其他资源')
                    elif phase=='unmatched':message='正在等待详情页主播放器初始化，请保持独立窗口打开…'
                    else:message='正在等待资源页面和播放器加载…'
                    if message!=last_state:notify(message);last_state=message
                    cancel.wait(.2)
        except DownloadError:raise
        except Exception as exc:
            self._page_ready=False
            if cancel.is_set() or self.closed:raise DownloadCancelled('已取消') from exc
            raise DownloadError('自动提取未完成，浏览器可能已关闭或连接中断；可点击一键提取重试') from exc
        finally:self.lock.release()

    def click_play(self,ws,state,cancel,deadline,session):
        point=state.get('button')
        if not point:raise DownloadError('目标资源的播放按钮暂不可用')
        if point.get('obscured'):return
        item=state.get('itemId','')
        if not re.fullmatch(r'\d+',item):raise DownloadError('目标播放器编号无效')
        expression="document.getElementById("+json.dumps('audioItemBox_'+item)+")?.querySelector('.audio-player-btn')?.click()"
        self.evaluate(ws,expression,cancel,deadline,session)

    def close(self):
        self.closed=True
        address=self.address or (self.endpoint() if self.process is not None else '')
        if address:
            try:
                with connect(address,proxy=None,open_timeout=1,close_timeout=1) as ws:
                    ws.send(json.dumps(dict(id=1,method='Browser.close')))
            except Exception:pass
        self.address='';self.target=None;self._page_ready=False
        if self.process:
            try:self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:pass
