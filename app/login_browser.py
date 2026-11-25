"""Explicitly paired browser extensions; never launch unrelated browsers."""
import json
import re
import secrets
import threading
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit
from websockets.sync.server import serve


class LoginBrowser:
    def __init__(self, settings, root=None):
        self.settings = settings
        self.active = False
        self.lock = threading.RLock()
        self.work_lock = threading.RLock()
        self.server = None
        self.token = secrets.token_urlsafe(32)
        self.clients = {}
        self.pending = {}
        self.selected = None
        self.closed = False
        self.pairing = None
        self.pair_lock = threading.Lock()
        self.native = None
        self.background = None
        self._background_selected = False
        from .config import config_dir
        self.profile_root = root or config_dir()

    def _background(self):
        if self.closed:
            raise RuntimeError('工具已关闭')
        if self.background is None:
            from .background_browser import BackgroundBrowser
            self.background = BackgroundBrowser(self.settings, self.profile_root)
        return self.background

    def can_resume(self):
        ready = not self.closed and self._background().can_resume()
        if ready:
            self._background_selected = True
        return ready

    def ensure_connected(self, cancel=None, progress=None):
        """Close the owned plain login window and prepare a headless session."""
        progress = progress or (lambda text: None)
        if self.closed:
            raise RuntimeError('工具已关闭')
        self._background().prepare(cancel, progress)
        self._background_selected = True
        self.active = True

    def ensure_extension_connected(self, cancel=None, progress=None):
        """Only called after the user's explicit '已登录' action."""
        from .browser_pairing import PairingAssist
        progress = progress or (lambda text: None)
        # A connected, signed-in page needs no extra tab or browser launch.
        if self.active:
            try:
                self.discover(cancel)
                return
            except RuntimeError:
                self.checkpoint(cancel, time.monotonic() + self.settings.timeout)
        deadline = time.monotonic() + self.settings.timeout
        while not self.pair_lock.acquire(timeout=.1):
            self.checkpoint(cancel, deadline)
        try:
            self.checkpoint(cancel, deadline)
            if self.pairing:
                self.pairing.close()
                self.pairing = None
            helper = self.pairing = PairingAssist(self, lifetime=self.settings.timeout)
            with self.lock:
                before = set(self.clients)
            progress('正在连接你已打开的浏览器；如首次使用或扩展需要更新，请在浏览器连接助手中完成一次确认。无需复制连接信息。')
            helper.open(cancel)
            while True:
                self.checkpoint(cancel, deadline)
                if self.closed:
                    raise RuntimeError('工具已关闭')
                with self.lock:
                    if set(self.clients) - before:
                        return
                time.sleep(.1)
        except Exception:
            if self.pairing:
                self.pairing.close()
                self.pairing = None
            raise
        finally:
            self.pair_lock.release()

    def remember_verification(self, query):
        if self.background:
            self.background.remember_verification(query)
        elif self.native:
            self.native.remember_verification(query)

    def connection_info(self):
        with self.lock:
            if self.closed:
                raise RuntimeError('工具已关闭')
            if self.server is None:
                self.server = serve(self._connected, '127.0.0.1', 0,
                    origins=[re.compile(r'chrome-extension://[a-p]{32}')],
                    process_request=self._authorize, max_size=8 * 1024 * 1024,
                    open_timeout=5, close_timeout=1, compression=None)
                threading.Thread(target=self.server.serve_forever,
                                 name='browser-bridge', daemon=True).start()
            port = self.server.socket.getsockname()[1]
            return f'ws://127.0.0.1:{port}/browser?token={self.token}'

    def _authorize(self, connection, request):
        url = urlsplit(request.path)
        token = parse_qs(url.query).get('token', [''])[0]
        origin = request.headers.get('Origin', '')
        if (not re.fullmatch(r'chrome-extension://[a-p]{32}', origin) or url.path != '/browser'
                or not secrets.compare_digest(token, self.token) or self.closed):
            return connection.respond(HTTPStatus.FORBIDDEN, 'Pairing required')
        with self.lock:
            if len(self.clients) >= 8:
                return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, 'Too many browsers')

    def _connected(self, ws):
        client = secrets.token_hex(8)
        with self.lock:
            self.clients[client] = ws
            self.active = True
        try:
            ws.send(json.dumps({'type': 'connected', 'version': 1}))
            while not self.closed:
                try:
                    raw = ws.recv(timeout=20)
                except TimeoutError:
                    ws.send('{"type":"ping"}')
                    continue
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict) or msg.get('type') != 'result':
                    continue
                with self.lock:
                    job = self.pending.get(str(msg.get('id', '')))
                    if job and job['client'] == client:
                        job['result'] = msg
                        job['event'].set()
        except Exception:
            pass
        finally:
            with self.lock:
                self.clients.pop(client, None)
                self.active = not self.closed and (bool(self.clients) or bool(self.native and self.native.active))
                for job in self.pending.values():
                    if job['client'] == client:
                        job['result'] = {'ok': False, 'error': '浏览器连接已断开，请重新连接扩展后重试。'}
                        job['event'].set()

    @staticmethod
    def checkpoint(cancel, deadline):
        if cancel and cancel.is_set():
            raise RuntimeError('搜索已取消')
        if time.monotonic() >= deadline:
            raise RuntimeError('浏览器响应超时；请保持目标标签页打开后重试。')

    def _send_job(self, client, operation, **payload):
        key = secrets.token_hex(16)
        job = dict(client=client, event=threading.Event(), result={})
        with self.lock:
            ws = self.clients.get(client)
            if ws is None:
                raise RuntimeError('浏览器已断开，请重新连接扩展')
            self.pending[key] = job
        try:
            ws.send(json.dumps(dict(type='job', id=key, op=operation, **payload)))
        except Exception:
            with self.lock:
                self.pending.pop(key, None)
            raise RuntimeError('无法联系浏览器，请重新连接扩展') from None
        return key, job

    def _wait_job(self, key, job, cancel, deadline):
        try:
            while not job['event'].wait(.1):
                self.checkpoint(cancel, deadline)
            self.checkpoint(cancel, deadline)
            msg = job['result']
            if msg.get('ok') is not True:
                raise RuntimeError(str(msg.get('error', '浏览器请求失败'))[:900])
            return msg.get('data')
        finally:
            with self.lock:
                self.pending.pop(key, None)
                ws = self.clients.get(job['client'])
            if not job['event'].is_set() and ws:
                try:
                    ws.send(json.dumps({'type': 'cancel', 'id': key}))
                except Exception:
                    pass

    def _call(self, client, operation, cancel, deadline, **payload):
        self.checkpoint(cancel, deadline)
        key, job = self._send_job(client, operation, **payload)
        return self._wait_job(key, job, cancel, deadline)

    def discover(self, cancel):
        self.connection_info()
        with self.lock:
            clients = list(reversed(self.clients))
        if not clients:
            raise RuntimeError('尚未连接浏览器扩展。请打开扩展文件夹完成安装，复制连接信息到扩展，并在常用浏览器中登录 YouTube。')
        deadline = time.monotonic() + 10
        jobs, tabs = [], []
        try:
            for client in clients:
                try:
                    key, job = self._send_job(client, 'discover')
                    jobs.append((client, key, job))
                except RuntimeError:
                    pass
            for client, key, job in jobs:
                try:
                    data = self._wait_job(key, job, cancel, deadline)
                    if isinstance(data, list):
                        tabs.extend((client, tab['id']) for tab in data
                                    if isinstance(tab, dict) and type(tab.get('id')) is int
                                    and tab.get('signedIn') is True)
                except RuntimeError:
                    if cancel and cancel.is_set():
                        raise
        finally:
            with self.lock:
                for _, key, _ in jobs:
                    self.pending.pop(key, None)
        if not tabs:
            raise RuntimeError('已连接的浏览器中未检测到已登录的 YouTube 页面。请完成登录后回到 YouTube 页面并刷新，保持标签页打开，再点击已登录。')
        return tabs

    def open_login(self, cancel=None):
        return self.connection_info()

    def aigei_media(self, page_path, cancel=None):
        """Read only a precisely matched, already loaded public playback source."""
        if not re.fullmatch(r'/item/[A-Za-z0-9_-]+\.html', page_path):
            raise RuntimeError('爱给资源路径无效')
        self.connection_info()
        with self.lock:
            clients = list(reversed(self.clients))
        if not clients:
            raise RuntimeError('尚未连接浏览器扩展，请复制连接信息到扩展。')
        deadline = time.monotonic() + min(15, self.settings.timeout)
        jobs = []; errors = []
        try:
            for client in clients:
                try:
                    key, job = self._send_job(client, 'aigei_media', pagePath=page_path)
                    jobs.append((key, job))
                except RuntimeError as exc:
                    errors.append(str(exc))
            while jobs:
                self.checkpoint(cancel, deadline)
                ready = [(key, job) for key, job in jobs if job['event'].is_set()]
                for key, job in ready:
                    jobs.remove((key, job))
                    try:
                        result = self._wait_job(key, job, cancel, deadline)
                        if isinstance(result, dict) and result.get('pagePath') == page_path:
                            return result
                        errors.append('浏览器未返回目标资源的播放信息')
                    except RuntimeError as exc:
                        errors.append(str(exc))
                if jobs:
                    jobs[0][1]['event'].wait(.05)
            raise RuntimeError('；'.join(dict.fromkeys(errors))[:900] or '未识别到目标资源的播放地址')
        finally:
            with self.lock:
                for key, _ in jobs:
                    self.pending.pop(key, None)

    def search_page(self, query, cancel=None):
        if self.closed:
            raise RuntimeError('工具已关闭')
        from .adapters.youtube import YouTubeAdapter
        from .login import LoginRequired, youtube_restriction
        if self.background is not None and (self._background_selected or self.background.can_resume()):
            result = self.background.search_page(query, cancel)
            self.active = True
            return result
        if self.native is not None:
            try:
                return self.native.search_page(query, cancel)
            except RuntimeError as exc:
                if cancel and cancel.is_set():
                    raise
                raise LoginRequired(str(exc), 'browser_page_required') from exc
        with self.work_lock:
            try:
                candidates = self.discover(cancel)
            except RuntimeError as exc:
                if cancel and cancel.is_set():
                    raise
                raise LoginRequired(str(exc)) from exc
            deadline = time.monotonic() + self.settings.timeout
            error = None
            for client, tab in candidates:
                try:
                    result = self._call(client, 'search', cancel, deadline, tabId=tab, query=query[:300],
                                        timeoutMs=int(self.settings.timeout * 1000))
                    html = result['html']
                    data = YouTubeAdapter()._find_initial_data(html)
                    if not data:
                        raise RuntimeError('当前浏览器未返回有效 YouTube 搜索页面，请检查该页面是否需要验证。')
                    youtube_restriction(data)
                    config = YouTubeAdapter._config(html)
                    if config.get('LOGGED_IN') is False:
                        raise LoginRequired('搜索页面显示登录已失效，请在 YouTube 页面重新登录后验证。')
                    self.selected = (client, tab)
                    config['_rf_browser_session'] = [client, tab]
                    return data, config
                except Exception as exc:
                    self.checkpoint(cancel, deadline)
                    error = exc
            raise error or RuntimeError('未找到可用的登录页面')

    def continuation(self, body, cancel=None, session=None):
        if self.closed:
            raise RuntimeError('工具已关闭')
        if session and session[0] == 'background':
            if self.background is None:
                from .net import SearchSessionExpired
                raise SearchSessionExpired('后台搜索连接已中断，正在恢复搜索')
            return self.background.continuation(body, cancel, session)
        if session and session[0] == 'native':
            if self.native is None:
                raise RuntimeError('浏览器读取会话已中断，请重新搜索')
            return self.native.continuation(body, cancel, session)
        client, tab = session or self.selected or (None, None)
        if not client:
            raise RuntimeError('浏览器分页会话已失效，请重新搜索')
        return self._call(client, 'continuation', cancel, time.monotonic() + self.settings.timeout,
                          tabId=tab, body=body, timeoutMs=int(self.settings.timeout * 1000))

    def close(self):
        with self.lock:
            self.closed = True
            sockets = list(self.clients.values())
            self.active = False
        if self.background:
            self.background.close()
        if self.native:
            self.native.close()
        if self.pairing:
            self.pairing.close()
            self.pairing = None
        for ws in sockets:
            ws.close()
        if self.server:
            self.server.shutdown()
