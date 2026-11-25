"""An app-profile-owned Chromium login window and persistent headless session.

The website login runs in a plain, non-debug browser. Once the user confirms it,
that owned window closes normally; the same private profile reopens headless.
No default browser profile, password input, cookie export or extension install.
"""
import json
from collections import OrderedDict
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import quote_plus, urlsplit, parse_qs

from websockets.sync.client import connect

from .config import Settings, SiteConfig
from .login import LoginRequired, youtube_restriction
from .native_browser import NativeBrowser
from .net import NetworkError, SearchSessionExpired


class BackgroundBrowser:
    def __init__(self, settings, root, *, auth_required=True):
        self.settings = settings
        self.root = Path(root).resolve() / 'youtube-browser'
        self.auth_required = auth_required
        self.owner = ''
        self.address = ''
        self.target = None
        self.process = None
        self.pending_login = False
        self.closed = False
        self.lock = threading.RLock()
        self.helper = NativeBrowser(Settings(timeout=5))
        self.session = ''
        self.cache = {}
        self.query = ''
        self.items_seen = set()
        self.prepared = self.last_page = None
        self.sessions = OrderedDict()
        self.session_limit = 4

    def _state(self):
        try:
            data = json.loads((self.root / '.rf-login.json').read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def can_resume(self):
        return not self.closed and not self.pending_login and self._state().get('authenticated') is True

    def _profile(self):
        if self.root.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(self.root)):
            raise RuntimeError('专用会话目录不能使用链接，请检查本地存档')
        marker = self.root / '.resourcefinder-session'
        if marker.is_symlink():
            raise RuntimeError('专用会话归属文件不能使用链接')
        if not marker.exists():
            if self.root.exists() and any(self.root.iterdir()):
                raise RuntimeError('专用会话目录包含未识别的数据，不会覆盖；请使用其他本地存档')
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                with marker.open('x', encoding='utf-8') as stream:
                    stream.write(secrets.token_hex(32))
            except FileExistsError:
                pass
        self.owner = marker.read_text(encoding='utf-8')
        if not re.fullmatch('[0-9a-f]{64}', self.owner):
            raise RuntimeError('专用会话归属信息损坏，请保留存档后检查')

    def _save(self, **updates):
        state = dict(self._state(), **updates)
        path = self.root / ('.rf-login-' + secrets.token_hex(8) + '.part')
        path.write_text(json.dumps(state), encoding='utf-8')
        os.replace(path, self.root / '.rf-login.json')

    def _executable(self):
        choices = []
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')):
            if base:
                choices.extend((('edge', Path(base) / 'Microsoft/Edge/Application/msedge.exe'),
                                ('chrome', Path(base) / 'Google/Chrome/Application/chrome.exe')))
        selected = self._state().get('browser')
        choices.sort(key=lambda row: row[0] != (selected or 'edge'))
        for browser, path in choices:
            if path.is_file() and (not selected or browser == selected):
                if not selected:
                    self._save(browser=browser, authenticated=False)
                return path
        raise RuntimeError('未找到专用会话使用的 Edge / Chrome 浏览器，请安装对应浏览器后重试')

    def _owned(self, *, close=False, force=False, show=False):
        return self.helper._call('managed_profile', profile=str(self.root), owner=self.owner, close=close, force=force, show=show)

    def _check(self, cancel, deadline):
        if self.closed or (cancel and cancel.is_set()):
            raise RuntimeError('搜索已取消')
        if time.monotonic() >= deadline:
            raise RuntimeError('后台会话响应超时，请稍后重试或重新登录')

    def _acquire(self, cancel, deadline):
        while not self.lock.acquire(timeout=.1):
            self._check(cancel, deadline)
        try:
            self._check(cancel, deadline)
        except Exception:
            self.lock.release()
            raise

    def _endpoint(self):
        try:
            port, path = (self.root / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()[:2]
            if 0 < int(port) < 65536 and re.fullmatch('/devtools/browser/[A-Za-z0-9-]+', path):
                return 'ws://127.0.0.1:' + str(int(port)) + path
        except (OSError, ValueError):
            pass
        return ''

    def _connection(self, address=None):
        return connect(address or self.address, proxy=None, open_timeout=2, close_timeout=.3, max_size=12*1024*1024)

    def _rpc(self, ws, method, params, cancel, deadline, session=None):
        self._check(cancel, deadline)
        key = secrets.randbits(28)
        message = dict(id=key, method=method, params=params)
        if session:
            message['sessionId'] = session
        ws.send(json.dumps(message))
        while True:
            self._check(cancel, deadline)
            try:
                result = json.loads(ws.recv(timeout=.1))
            except TimeoutError:
                continue
            if result.get('id') != key:
                continue
            if result.get('error'):
                raise RuntimeError('后台浏览器操作失败：' + str(result['error'].get('message', ''))[:300])
            return result.get('result', {})

    def _evaluate(self, ws, script, cancel, deadline, session, *, promise=False):
        result = self._rpc(ws, 'Runtime.evaluate', dict(expression=script, returnByValue=True, awaitPromise=promise), cancel, deadline, session)
        if result.get('exceptionDetails'):
            raise RuntimeError('后台网页暂未返回有效搜索数据，请稍后重试')
        return result.get('result', {}).get('value')

    def _stop_browser(self, cancel, deadline):
        address = self.address or self._endpoint()
        if address:
            try:
                with self._connection(address) as ws:
                    ws.send(json.dumps(dict(id=1, method='Browser.close', params={})))
            except Exception:
                pass
        self.address = ''; self.target = None
        self.sessions.clear()
        self.session = ''; self.prepared = self.last_page = None
        state = self._owned(close=True)
        while state['processes']:
            self._check(cancel, deadline)
            time.sleep(.2)
            state = self._owned()

    def open_login(self, cancel=None, query=''):
        if not isinstance(query,str) or len(query)>300:
            raise RuntimeError('搜索词无效')
        deadline = time.monotonic() + self.settings.timeout
        self._acquire(cancel, deadline)
        try:
            self._profile()
            state = self._owned()
            if self.pending_login and state['processes'] and not state['headless']:
                self._owned(show=True)
                return
            self._stop_browser(cancel, deadline)
            executable = self._executable()
            self.pending_login = True
            self.prepared = None
            self.process = subprocess.Popen([str(executable), '--user-data-dir=' + str(self.root),
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                '--new-window', 'https://www.youtube.com/results?search_query='+quote_plus(query) if query else 'https://www.youtube.com/'], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            while True:
                self._check(cancel, deadline)
                state = self._owned()
                if state['windows'] and not state['headless']:
                    return
                time.sleep(.2)
        except Exception:
            if cancel and cancel.is_set() and self.pending_login:
                self.pending_login = False
                try:
                    self._stop_browser(None, time.monotonic()+5)
                except Exception:
                    pass
            raise
        finally:
            self.lock.release()

    def stop_pending_login(self):
        if not self.pending_login or self.closed:
            return
        with self.lock:
            if not self.pending_login or self.closed:
                return
            self.pending_login = False
            try:
                self._stop_browser(None, time.monotonic()+5)
            except Exception:
                pass

    def prepare(self, cancel=None, progress=None):
        deadline = time.monotonic() + self.settings.timeout
        self._acquire(cancel, deadline)
        try:
            self._profile()
            if progress:
                progress('正在保存登录状态并关闭专用登录窗口，随后切换到后台搜索…')
            state = self._owned()
            if state['processes'] and not state['headless']:
                self._stop_browser(cancel, deadline)
            self.pending_login = False
            self._start(cancel, deadline)
        finally:
            self.lock.release()

    def _start(self, cancel, deadline):
        self._check(cancel, deadline)
        self._profile()
        if self.pending_login:
            raise LoginRequired('请先在专用登录窗口完成登录，然后点击“已登录”')
        state = self._owned()
        if state['processes'] and not state['headless']:
            raise LoginRequired('专用登录窗口仍在使用，请完成登录后点击“已登录”')
        candidate = self.address or self._endpoint()
        if state['headless'] and candidate:
            try:
                with self._connection(candidate):
                    self.address = candidate
                    return
            except Exception:
                pass
        if not state['processes']:
            executable = self._executable()
            self.process = subprocess.Popen([str(executable), '--user-data-dir=' + str(self.root),
                '--headless=new', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                '--disk-cache-size=33554432', 'https://www.youtube.com/'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        while True:
            self._check(cancel, deadline)
            candidate = self._endpoint()
            if candidate:
                try:
                    with self._connection(candidate):
                        self.address = candidate
                        return
                except Exception:
                    pass
            time.sleep(.1)

    def _attach(self, ws, cancel, deadline, *, existing=False, fresh=False):
        targets = self._rpc(ws, 'Target.getTargets', {}, cancel, deadline).get('targetInfos', [])
        if not any(t.get('targetId') == self.target and t.get('type') == 'page' for t in targets):
            if existing:
                raise SearchSessionExpired('后台搜索页面已关闭，正在恢复搜索；无需重新登录')
            reserved = {row['target'] for row in self.sessions.values()}
            self.target = None if fresh else next((t['targetId'] for t in targets if t.get('type') == 'page' and
                                t['targetId'] not in reserved and
                                urlsplit(t.get('url', '')).hostname == 'www.youtube.com'), None)
        if not self.target:
            self.target = self._rpc(ws, 'Target.createTarget', {'url':'https://www.youtube.com/'}, cancel, deadline)['targetId']
        attached = self._rpc(ws, 'Target.attachToTarget', dict(targetId=self.target, flatten=True), cancel, deadline)['sessionId']
        # Only our headless browser is controlled here. Its selected tab still
        # needs foreground lifecycle to run native scrolling/visibility loaders;
        # this does not open or focus a visible desktop browser window.
        self._rpc(ws, 'Page.bringToFront', {}, cancel, deadline, attached)
        return attached

    def _select_session(self, session):
        key = session[1] if isinstance(session, (list, tuple)) and len(session) == 2 and session[0] == 'background' else None
        row = self.sessions.get(key) if isinstance(key, str) else None
        if row is None:
            raise SearchSessionExpired('后台续页已释放，正在恢复搜索；无需重新登录')
        self.sessions.move_to_end(key)
        self.session = key
        self.target, self.query = row['target'], row['query']
        self.cache, self.items_seen = row['cache'], row['seen']

    def search_page(self, query, cancel=None):
        if not isinstance(query, str) or not 0 < len(query) <= 300:
            raise RuntimeError('搜索词无效')
        deadline = time.monotonic() + self.settings.timeout
        self._acquire(cancel, deadline)
        try:
            if self.prepared:
                saved, self.prepared = self.prepared, None
                if (saved[0] == query and time.monotonic() - saved[3] < 30 and
                        saved[2]['_rf_browser_session'][1] in self.sessions):
                    self._select_session(saved[2]['_rf_browser_session'])
                    return saved[1], saved[2]
            self._start(cancel, deadline)
            with self._connection() as ws:
                # Each live search owns its page. A different keyword must not
                # navigate away from an older keyword's native continuation.
                while len(self.sessions) >= self.session_limit:
                    _, old = self.sessions.popitem(last=False)
                    self._rpc(ws, 'Target.closeTarget', {'targetId':old['target']}, cancel, deadline)
                self.target = None
                # closeTarget acknowledges before the target disappears from
                # getTargets. Never recycle a just-closed tab into a new query.
                session = self._attach(ws, cancel, deadline, fresh=bool(self.session))
                url = 'https://www.youtube.com/results?search_query=' + quote_plus(query) + '&hl=zh-CN'
                self._rpc(ws, 'Page.navigate', {'url':url}, cancel, deadline, session)
                auth_deadline = None
                while True:
                    self._check(cancel, deadline)
                    result = self._evaluate(ws, """(()=>{if(location.origin!=='https://www.youtube.com')return {url:location.href};
const get=k=>window.ytcfg?.get?.(k)??window.ytcfg?.data_?.[k];
return {url:location.href,ready:document.readyState,data:window.ytInitialData,
config:{LOGGED_IN:get('LOGGED_IN'),INNERTUBE_CONTEXT:get('INNERTUBE_CONTEXT'),INNERTUBE_CONTEXT_CLIENT_NAME:get('INNERTUBE_CONTEXT_CLIENT_NAME')}};})()""", cancel, deadline, session) or {}
                    actual = urlsplit(result.get('url', ''))
                    if actual.hostname == 'accounts.google.com':
                        raise LoginRequired('网站要求重新登录，请在专用登录窗口完成后继续')
                    if (actual.hostname == 'www.youtube.com' and actual.path == '/results' and
                            parse_qs(actual.query).get('search_query') == [query] and
                            result.get('ready') in ('interactive', 'complete') and result.get('data')):
                        data, config = result['data'], result.get('config') or {}
                        if self.auth_required and config.get('LOGGED_IN') is not True:
                            if auth_deadline is None:
                                auth_deadline = min(deadline-.1, time.monotonic()+min(15, self.settings.timeout/3))
                            if time.monotonic() < auth_deadline:
                                time.sleep(.2)
                                continue
                            raise LoginRequired('尚未确认专用会话已登录。请在工具打开的登录窗口完成登录，再点击“已登录”；日常浏览器的登录不会自动复制到此存档。')
                        if not isinstance(config.get('INNERTUBE_CONTEXT'), dict):
                            time.sleep(.2)
                            continue
                        youtube_restriction(data)
                        break
                    time.sleep(.2)
            self.session = secrets.token_hex(16)
            self.cache = {}
            self.query = query
            self.items_seen = {key for key, _ in self._snapshot_items(data['contents'])}
            self.sessions[self.session] = dict(target=self.target, query=query, cache=self.cache, seen=self.items_seen)
            config['_rf_browser_session'] = ['background', self.session]
            self.last_page = (query, data, config, time.monotonic())
            return data, config
        finally:
            self.lock.release()

    def remember_verification(self, query):
        if self.last_page and self.last_page[0] == query and self.last_page[2].get('LOGGED_IN') is True:
            self.prepared = self.last_page
            self._save(authenticated=True)

    @staticmethod
    def _snapshot_items(data):
        """Read actual main-search renderer records, excluding ads and duplicates."""
        from .adapters.youtube import YouTubeAdapter
        def organic(node):
            if isinstance(node, dict):
                return {k:organic(v) for k,v in node.items() if k not in (
                    'searchPyvRenderer','adSlotRenderer','inFeedAdLayoutRenderer',
                    'promotedSparklesWebRenderer','promotedVideoRenderer','displayAdRenderer')}
            if isinstance(node, list):return [organic(v) for v in node]
            return node
        adapter = YouTubeAdapter(); site = SiteConfig(id='ytb', name='YouTube')
        data = organic(data); records = []; seen = set()
        groups = [(adapter._walk_videos(data), adapter._to_result, lambda v:'videoRenderer'),
                  (adapter._walk_lockups(data), adapter._from_lockup, lambda v:'lockupViewModel'),
                  (adapter._walk_shorts(data), adapter._from_short, lambda v:'shortsLockupViewModel' if 'onTap' in v else 'reelItemRenderer')]
        for values, convert, renderer in groups:
            for value in values:
                row = convert(value, site)
                if row and row.url not in seen:
                    seen.add(row.url);records.append((row.url, {renderer(value):value}))
        return records

    def continuation(self, body, cancel=None, session=None):
        deadline = time.monotonic() + self.settings.timeout
        self._acquire(cancel, deadline)
        try:
            self._select_session(session)
            token = body.get('continuation')
            if not isinstance(token, str) or not isinstance(body.get('context'), dict):
                raise RuntimeError('后台续页参数无效')
            if token in self.cache:
                return self.cache[token]
            from .adapters.youtube import YouTubeAdapter
            adapter = YouTubeAdapter()
            with self._connection() as ws:
                attached = self._attach(ws, cancel, deadline, existing=True)
                expected = json.dumps(self.query)
                read_script = """(()=>{ /* __rfReadSearchResults */
const url=location.href;
if(location.origin!=='https://www.youtube.com'||new URL(url).searchParams.get('search_query')!==QUERY)return {url};
const list=document.querySelector('ytd-search ytd-section-list-renderer');
const model=list?.data;
const data=model&&Array.isArray(model.contents)?{contents:model.contents.slice(-8),continuations:model.continuations}:undefined;
const continuation=list?.querySelector('ytd-continuation-item-renderer');
return {url,data,pending:!!continuation&&continuation.getClientRects().length>0,
loading:!!document.querySelector('ytd-search ytd-continuation-item-renderer[loading]')};})()""".replace('QUERY', expected)
                scroll_script = """(()=>{ /* __rfScrollSearchResults */
const matches=()=>location.origin==='https://www.youtube.com'&&new URL(location.href).searchParams.get('search_query')===QUERY;
if(!matches())return false;
const item=document.querySelector('ytd-search ytd-section-list-renderer ytd-continuation-item-renderer');
if(item)item.scrollIntoView({block:'end'});
window.scrollBy(0,-250);
requestAnimationFrame(()=>{if(matches())window.scrollTo(0,document.documentElement.scrollHeight);});
return true;})()""".replace('QUERY', expected)
                last_scroll = 0; terminal_at = None
                while True:
                    self._check(cancel, deadline)
                    snapshot = self._evaluate(ws, read_script, cancel, deadline, attached) or {}
                    actual = urlsplit(snapshot.get('url', ''))
                    if actual.hostname == 'accounts.google.com':
                        raise LoginRequired('网站要求重新登录，请在专用登录窗口完成后继续')
                    if (actual.hostname!='www.youtube.com' or actual.path!='/results' or
                            parse_qs(actual.query).get('search_query')!=[self.query]):
                        raise SearchSessionExpired('后台搜索页面已变化，正在恢复搜索；无需重新登录')
                    current = snapshot.get('data')
                    if isinstance(current, dict):
                        youtube_restriction(current)
                        fresh = [(key, item) for key, item in self._snapshot_items(current) if key not in self.items_seen]
                        next_token = adapter._continuation(current)
                        # Let YouTube itself assemble authenticated requests. Its
                        # real result model advances even when a hand-built API
                        # request would return only ads or a false empty page.
                        changed = next_token is not None and next_token != token
                        terminal = (not next_token and not snapshot.get('loading') and bool(current.get('contents')) and
                                    (adapter._valid_empty(current) or snapshot.get('pending') is False))
                        def unknown_notice(node):
                            if isinstance(node, dict):
                                if 'messageRenderer' in node:return not adapter._valid_empty(node)
                                return any(unknown_notice(v) for k,v in node.items() if k not in
                                    ('videoRenderer','lockupViewModel','reelItemRenderer','shortsLockupViewModel','adSlotRenderer'))
                            if isinstance(node, list):return any(unknown_notice(v) for v in node)
                            return False
                        if terminal and unknown_notice(current):
                            raise NetworkError('YouTube网页返回了临时提示，已保留当前结果')
                        if terminal:
                            terminal_at = terminal_at or time.monotonic()
                        else:
                            terminal_at = None
                        if changed or (terminal and time.monotonic()-terminal_at>=1):
                            items = [item for _, item in fresh]
                            if next_token:
                                items.append({'continuationItemRenderer':{'continuationEndpoint':{'continuationCommand':{'token':next_token}}}})
                            data = {'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':items}}]}
                            self._check(cancel, deadline)
                            self.items_seen.update(key for key, _ in fresh)
                            break
                    else:
                        terminal_at = None
                    if time.monotonic()-last_scroll>=1:
                        self._evaluate(ws, scroll_script, cancel, deadline, attached)
                        last_scroll = time.monotonic()
                    time.sleep(.15)
            # Cache only a successfully read native page, never unknown/failed
            # raw replies; retries must still be able to obtain fresh results.
            self.cache[token] = data
            while len(self.cache) > 16:
                self.cache.pop(next(iter(self.cache)))
            return data
        finally:
            self.lock.release()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if (self.root / '.resourcefinder-session').is_file():
            try:
                self._profile()
                address = self.address or self._endpoint()
                if address:
                    try:
                        with self._connection(address) as ws:
                            ws.send(json.dumps(dict(id=1, method='Browser.close', params={})))
                    except Exception:
                        pass
                self._owned(close=True)
                end = time.monotonic()+3
                while time.monotonic()<end:
                    if not self._owned()['processes']:
                        break
                    time.sleep(.15)
                else:
                    self._owned(force=True)  # Only owned headless roots, never login/user browsers.
            except Exception:
                pass
        self.helper.close()
