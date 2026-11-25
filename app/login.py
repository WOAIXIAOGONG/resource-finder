"""Per-search login requests. Credentials stay in the user's chosen browser."""
import threading


class LoginRequired(Exception):
    def __init__(self, reason='需要登录后继续搜索', code='login_required'):
        super().__init__(reason)
        self.code = code


def youtube_restriction(data):
    """Inspect explicit platform notices, never video titles or descriptions."""
    from .adapters.youtube import YouTubeAdapter
    notices = []
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('backgroundPromoRenderer', 'messageRenderer', 'alertRenderer') and isinstance(value, dict):
                    notices.append(' '.join(YouTubeAdapter._text(value.get(k)) for k in ('title', 'bodyText', 'text')))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(data)
    for text in notices:
        low = text.lower()
        if any(word in low for word in ('confirm your age', 'verify your age', '确认您的年龄', '確認你的年齡', '確認您的年齡', '验证年龄', '確認年齡', '确认年龄')):
            raise LoginRequired('YouTube 要求登录并确认年龄；请在官方页面完成验证。', 'age_verification_required')
        if any(word in low for word in ('sign in', 'log in', '登录', '登入')):
            raise LoginRequired('YouTube 要求登录或验证当前账号；请在官方页面完成。')


class LoginManager:
    def __init__(self, config):
        from .login_browser import LoginBrowser
        self.config = config
        self.browser = LoginBrowser(config.settings, config._storage_dir or None)
        config.settings._login_browser = self.browser
        self.entries = {}
        self.lock = threading.RLock()
        self.notify = lambda entry: None

    def inherit(self, previous, scope, cancel):
        with self.lock:
            for (old_scope, source), row in list(self.entries.items()):
                if old_scope == previous and row['state'] in ('skipped', 'ready'):
                    self.entries[(scope, source)] = dict(row, scope=scope, cancel=cancel)
            # Bound dead task metadata; no credentials are ever stored here.
            for key in list(self.entries):
                if len(self.entries) <= 128:
                    break
                if self.entries[key]['cancel'].is_set():
                    self.entries.pop(key, None)

    def snapshot(self, scope):
        with self.lock:
            return {site: self.public(row) for (key, site), row in self.entries.items() if key == scope and not row['cancel'].is_set()}

    @staticmethod
    def public(row):
        return {k: v for k, v in row.items() if k not in ('cancel',)}

    def require(self, scope, site, query, page, reason, cancel):
        with self.lock:
            key = (scope, site.id)
            old = self.entries.get(key)
            if old and old['state'] in ('waiting', 'verifying', 'skipped'):
                return self.public(old)
            row = dict(scope=scope, source_id=site.id, name=site.display, query=query,
                       page=page, reason=str(reason)[:900], state='waiting', cancel=cancel,
                       supported=site.builtin == 'youtube', next_page=1)
            self.entries[key] = row
        if not cancel.is_set():
            self.notify(self.public(row))
        return self.public(row)

    def skip(self, scope, source):
        with self.lock:
            row = self.entries.get((scope, source))
            if row and not row['cancel'].is_set():
                row['state'] = 'skipped'
                self.notify(self.public(row))
        if self.browser.background and self.browser.background.pending_login:
            threading.Thread(target=self.browser.background.stop_pending_login, name='login-window-close', daemon=True).start()

    def open(self, scope, source):
        with self.lock:
            row = self.entries.get((scope, source))
            if not row or row['cancel'].is_set() or row['state'] != 'waiting':
                return
            if not row['supported']:
                raise RuntimeError('当前平台暂不支持浏览器登录续搜，请本轮跳过；其他平台继续。')
        class OpenCancel:
            def is_set(inner):
                return row['cancel'].is_set() or row['state'] == 'skipped'
        self.browser.active = False
        self.browser._background().open_login(OpenCancel(), query=row['query'])

    def verify(self, scope, source, *, auto_connect=False):
        from .adapters.youtube import YouTubeAdapter
        from .adapters.base import SearchContext
        from .net import HttpClient
        with self.lock:
            row = self.entries.get((scope, source))
            if not row or row['cancel'].is_set() or row['state'] != 'waiting':
                return
            row['state'] = 'verifying'
        self.notify(self.public(row))
        http = HttpClient(self.config.settings)
        class VerificationCancel:
            def is_set(inner):
                return row['cancel'].is_set() or row['state'] == 'skipped'
        verify_cancel = VerificationCancel()
        def progress(reason):
            with self.lock:
                if verify_cancel.is_set():
                    return
                row.update(reason=reason, phase='connecting')
            self.notify(self.public(row))
        try:
            if not row['supported']:
                raise RuntimeError('当前平台暂不支持浏览器登录续搜，请本轮跳过。')
            if auto_connect:
                self.browser.ensure_connected(verify_cancel, progress)
            elif not self.browser.active:
                self.browser.discover(verify_cancel)
            with self.lock:
                row.update(reason='已连接浏览器，正在核对实际搜索页面；其他平台继续搜索…', phase='checking')
            self.notify(self.public(row))
            site = self.config.get_site(source)
            if not site or not site.enabled:
                raise RuntimeError('该平台已停用')
            # Test the actual query in the SAME browser, never infer from an avatar.
            YouTubeAdapter().search(SearchContext(row['query'], http, cancel=verify_cancel), site)
            self.browser.remember_verification(row['query'])
            state, reason = 'ready', '后台登录会话已验证，后续搜索无需保持网页打开'
        except Exception as exc:
            state, reason = 'waiting', str(exc)[:900]
        finally:
            http.close()
        with self.lock:
            if row['cancel'].is_set() or row['state'] == 'skipped':
                return
            row.update(state=state, reason=reason, phase='')
        self.notify(self.public(row))


def login_manager(config):
    manager = getattr(config, '_login_manager', None)
    if manager is None:
        manager = config._login_manager = LoginManager(config)
    return manager
