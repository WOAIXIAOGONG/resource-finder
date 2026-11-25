"""Agent handshake and per-target connectivity, independent of the host's badge."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urljoin

from .net import HttpClient, detect_proxy, _DIRECT_FIRST, _host_in
from .sources import site_group


PROXY_PUBLIC_DOMAINS = ('youtube.com', 'google.com', 'bing.com', 'bilibili.com',
                        'baidu.com', 'vimeo.com', 'dailymotion.com', 'github.com')


def public_url(url, remote_dns=False):
    p = urlsplit(str(url))
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('只支持不带凭据的公开 HTTP/HTTPS 网页')
    if p.port not in (None, 80, 443) or p.hostname.lower() in ('localhost', 'localhost.localdomain'):
        raise ValueError('不允许访问本地服务或非网页端口')
    # A configured proxy resolves these known public domains itself. Local DNS
    # may be poisoned/Fake-IP; the caller MUST force proxy_only in this case.
    if remote_dns and _host_in(p.hostname, PROXY_PUBLIC_DOMAINS):
        return url
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('不允许访问私有、回环或保留网络地址')
    return url


def search_scope(query):
    """Infer explicit source scope; ambiguous/mixed requests keep all sources."""
    text = re.sub(r'(?:不要|不搜|不找|不看|排除|不包括|不含|不用|别搜)\s*(?:搜索|搜|找|访问)?\s*'
                  r'(?:国内|国外|海外|外网|境外|YouTube|油管|B站|哔哩哔哩|bilibili)', '', query or '', flags=re.I)
    urls = re.findall(r'https?://[^\s<>"，。]+', text)
    domestic = bool(re.search(r'国内|中国大陆|大陆网站|B站|bilibili|哔哩|优酷|爱奇艺|腾讯视频|抖音|快手|百度视频', text, re.I))
    overseas = bool(re.search(r'国外|海外|外网|境外|YouTube|油管|vimeo|dailymotion', text, re.I))
    for url in urls:
        host = urlsplit(url).hostname or ''
        domestic |= _host_in(host, _DIRECT_FIRST)
        overseas |= bool(host and not _host_in(host, _DIRECT_FIRST))
    if re.search(r'国内外|境内外|全球', text):
        return 'all'
    return 'domestic' if domestic and not overseas else 'overseas' if overseas and not domestic else 'all'


def scoped_sources(query, sites):
    scope = search_scope(query)
    return [s for s in sites if s.enabled and (scope == 'all' or
            (_host_in(s.scope_domain or urlsplit(s.search_url).hostname or '', _DIRECT_FIRST) == (scope == 'domestic')))]


def external_targets(query, sites):
    """Conservative first-use check; Agent can add an unanticipated target explicitly."""
    urls = re.findall(r'https?://[^\s<>"，。]+', query or '')
    if re.search(r'youtube|油管', query or '', re.I):
        urls.append('https://www.youtube.com/')
    if re.search(r'vimeo', query or '', re.I):
        urls.append('https://vimeo.com/')
    if re.search(r'外网|海外|国外|境外', query or '') and not urls:
        urls.append('https://www.youtube.com/')
    domestic_only = search_scope(query) == 'domestic'
    if domestic_only:
        # Explicit external URLs would have made this a mixed scope above.
        return []
    urls += [s.search_url for s in scoped_sources(query, sites) if s.route != 'direct']
    targets = []
    for url in urls:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        if host in ('localhost', 'localhost.localdomain') or host.endswith('.local'):
            continue
        try:
            if not ipaddress.ip_address(host).is_global:
                continue
        except ValueError:
            pass
        if host and not _host_in(host, _DIRECT_FIRST):
            target = 'https://' + host + '/'
            if target not in targets:
                targets.append(target)
    return targets[:6]


class AgentAccess:
    TTL = 900

    def __init__(self, server, config):
        self.server, self.config = server, config
        from .login import login_manager
        self.logins = login_manager(config)
        self.owner = ''
        self.connection = 'waiting'
        self.challenge = secrets.token_hex(16)
        self.cache = {}
        self.plan = {}
        self.receipts = {}
        self.last_report = ''
        self.started = time.monotonic()
        self.io_slots = threading.BoundedSemaphore(3)
        self._search_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='agent-search')
        self._search_http = HttpClient(config.settings, track_sessions=True)
        self._candidates = {}
        self._candidate_task = ''
        from .search_evidence import SearchEvidence
        self.search_evidence = SearchEvidence()

    def remember_candidates(self, task_id, rows):
        # Called under the server task lock after stale-response validation.
        if self._candidate_task != task_id:
            self._candidates.clear()
            self._candidate_task = task_id
        for row in rows:
            cid = secrets.token_hex(8)
            self._candidates[cid] = dict(row)
            row['candidate_id'] = cid
        while len(self._candidates) > 2000:
            self._candidates.pop(next(iter(self._candidates)))

    def resolve_candidates(self, task_id, ids):
        if not isinstance(ids, list) or len(ids) > 1000 or any(
                not isinstance(cid, str) or cid not in self._candidates for cid in ids):
            raise ValueError('candidate_ids 必须是本任务有效候选编号数组（最多 1000 个）')
        if self._candidate_task != task_id:
            raise ValueError('候选编号已过期，请使用本任务的搜索结果')
        return [dict(self._candidates[cid]) for cid in ids]

    def candidate_allows_fetch(self, task_id, url, keys):
        from .sources import result_groups
        from .models import SearchResult, normalize_url
        if task_id != self._candidate_task:
            return False
        for row in self._candidates.values():
            if normalize_url(row.get('url', '')) == normalize_url(url):
                result = SearchResult(title=row.get('title', ''), url=row.get('url', ''),
                                      source_id=row.get('source_id', ''), resource_type=row.get('resource_type', 'video'),
                                      extra=row.get('extra') or {})
                if set(result_groups(result, self.config.sites)) & set(keys):
                    return True
        return False

    def close(self):
        if self._search_pool._shutdown:
            return
        pool, http = self._search_pool, self._search_http
        pool.shutdown(wait=False, cancel_futures=True)
        def finish():
            pool.shutdown(wait=True, cancel_futures=True)
            http.close_all()
        threading.Thread(target=finish, name='agent-network-cleanup', daemon=True).start()

    def fingerprint(self):
        settings = self.config.settings
        return hashlib.sha256((detect_proxy(settings) + str(settings.proxy_mode)).encode()).hexdigest()

    def sync_owner(self):
        if self.owner != self.server.owner:
            self.owner = self.server.owner
            self.challenge = secrets.token_hex(16)
            self.connection = 'waiting'
            self.cache.clear()
            self.plan = {}
            self.receipts.clear()
            self.started = time.monotonic()

    def ready(self):
        return (self.owner == self.server.owner and self.connection == 'ready'
                and self.server.connected() and not self.server._resume_pending)

    def poll(self):
        self.sync_owner()
        if self.connection == 'confirmed':
            self.connection = 'ready'
        if self.connection != 'ready':
            self.connection = 'confirming'
            return {'id': self.challenge, 'kind': 'connect', 'challenge': self.challenge,
                    'instruction': 'POST /api/connect 同时带 agent、challenge、ok:true；然后继续 GET /api/task。不要对握手调用 /api/ack。'}
        return None

    def confirm(self, data):
        self.sync_owner()
        if data.get('challenge') != self.challenge or self.connection not in ('confirming', 'confirmed', 'ready'):
            raise ValueError('握手已失效，请重新取任务')
        if data.get('ok') is not True:
            self.connection = 'failed'
            self.last_report = str(data.get('error') or 'Agent 拒绝接管或无法持续轮询')[:4000]
        elif self.connection != 'ready':
            self.connection = 'confirmed'
        return {'ok': True, 'confirmed': self.connection in ('confirmed', 'ready'), 'next': '继续 GET /api/task 才完成接管'}

    def reset(self):
        self.connection = 'waiting'
        self.challenge = secrets.token_hex(16)
        self.started = time.monotonic()
        self.last_report = ''
        self.plan = {}
        self.cache.clear()
        self.receipts.clear()

    def prepare(self, task, targets=None):
        self.search_evidence.reset(task['id'])
        if self._candidate_task != task['id']:
            self._candidates.clear()
            self._candidate_task = task['id']
        keys = task.get('filters', {}).get('source_keys')
        sites = self.config.enabled_sites()
        if keys is not None:
            sites = [s for s in sites if site_group(s)[0] in keys]
        targets = targets if targets is not None else external_targets('' if keys is not None else task['query'], sites)
        fp = self.fingerprint()
        cached = {url: self.cache.get((url, fp)) for url in targets}
        required = any(not hit or time.monotonic() - hit[1] > self.TTL for hit in cached.values())
        self.plan = {'task_id': task['id'], 'check_id': secrets.token_hex(12),
                     'scope': search_scope(' '.join('https://' + s.scope_domain if s.scope_domain else s.search_url for s in sites) if keys is not None else task['query']),
                     'targets': targets, 'required': required, 'state': 'pending' if required else 'ready',
                     'fingerprint': fp, 'routes': {u: h[0] for u, h in cached.items() if h}}
        self.receipts.clear()
        return self.snapshot()

    def snapshot(self):
        return {k: v for k, v in self.plan.items() if k not in ('fingerprint', 'started')}

    def for_task(self, task):
        if self.plan.get('task_id') != task['id'] or self.plan.get('fingerprint') != self.fingerprint():
            self.prepare(task)
        return self.snapshot()

    def pending(self):
        task = self.server.current_task
        if not task:
            return False
        return self.for_task(task).get('state') != 'ready'

    def config_payload(self):
        proxy = detect_proxy(self.config.settings)
        return {'ok': True, 'network': self.snapshot(), 'proxy_url': proxy,
                'environment': {'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy,
                                'NO_PROXY': '127.0.0.1,localhost,::1'},
                'scope': '仅当前 Agent 子进程；不修改系统代理，不承诺内置联网工具使用该代理',
                'helper': self.server.url + '/api/network/helper?token=' + self.server.token,
                'fetch': '/api/network/fetch', 'search': '/api/network/search',
                'request_timeout_seconds': self.config.settings.timeout, 'client_timeout_seconds': self.config.settings.timeout + 30,
                'login': self.logins.snapshot((self.server.current_task or {}).get('id', ''))}

    def sources(self):
        task = self.server.current_task or {}
        keys = task.get('filters', {}).get('source_keys')
        if keys is not None:
            return [s for s in self.config.enabled_sites() if site_group(s)[0] in keys]
        query = task.get('query', '')
        return scoped_sources(query, self.config.enabled_sites())

    def report(self, data):
        if data.get('check_id') != self.plan.get('check_id'):
            raise ValueError('网络检测已失效，请重新取配置')
        attempts = data.get('attempts')
        if not isinstance(attempts, list) or len(attempts) > 24:
            raise ValueError('attempts 必须是最多 24 项的实测报告')
        passed, lines = {}, []
        for row in attempts:
            if not isinstance(row, dict):
                raise ValueError('检测记录必须是对象')
            url, route = str(row.get('url', '')), str(row.get('route', ''))
            if url not in self.plan['targets'] or route not in ('direct', 'proxy', 'relay', 'native'):
                raise ValueError('未知检测目标或通道')
            code = row.get('status')
            good = row.get('ok') is True and type(code) is int and 200 <= code < 300
            if route == 'relay' and good:
                good = self.receipts.get(str(row.get('receipt'))) == (self.plan['check_id'], url)
            if good:
                passed[url] = route
            reason = str(row.get('error') or ('HTTP %s' % code))[:900]
            proxy = detect_proxy(self.config.settings)
            if proxy:
                reason = reason.replace(proxy, '[代理地址]')
            lines.append('%s | %s | %s' % (url, route, '通过' if good else reason))
        ok = all(url in passed for url in self.plan['targets'])
        self.last_report = '\n'.join(lines) or 'Agent 未提供任何检测记录'
        if ok:
            self.plan.update(state='ready', required=False, routes=passed)
            for url, route in passed.items():
                self.cache[(url, self.plan['fingerprint'])] = (route, time.monotonic())
        else:
            self.plan['state'] = 'failed'
            for url in self.plan['targets']:
                self.cache.pop((url, self.plan['fingerprint']), None)
        return ok

    def fetch(self, url, probe=False, cancel=None):
        """Bounded public web relay. No client headers/cookies, redirects revalidated."""
        if not self.io_slots.acquire(False):
            raise ValueError('网络通道繁忙，请稍后重试')
        http = HttpClient(self.config.settings)
        http.cancel_event = cancel
        def checkpoint():
            if cancel is not None and cancel.is_set():
                raise ValueError('任务已取消')
        started = time.monotonic()
        try:
            current = str(url)
            for _ in range(5):
                checkpoint()
                host = urlsplit(current).hostname or ''
                domestic = _host_in(host, _DIRECT_FIRST)
                site = next((s for s in self.config.enabled_sites()
                             if (s.scope_domain and _host_in(host, (s.scope_domain,))) or urlsplit(s.search_url).hostname == host or any(
                                 _host_in(host, (d,)) and _host_in(urlsplit(s.search_url).hostname or '', (d,))
                                 for d in PROXY_PUBLIC_DOMAINS)), None)
                route = getattr(site, 'route', 'auto')
                proxy_dns = (route != 'direct' and bool(http.proxy_url()) and
                             _host_in(host, PROXY_PUBLIC_DOMAINS) and (not domestic or route == 'proxy'))
                public_url(current, remote_dns=proxy_dns)
                checkpoint()
                response = http.get(current, timeout=self.config.settings.timeout, stream=True, allow_redirects=False,
                                    route='proxy_only' if proxy_dns else route,
                                    allow_status=tuple(range(200, 400)))
                with response:
                    if 300 <= response.status_code < 400 and response.headers.get('Location'):
                        current = urljoin(current, response.headers['Location'])
                        continue
                    response.raise_for_status()
                    chunks, total = [], 0
                    for chunk in response.iter_content(16384):
                        checkpoint()
                        total += len(chunk)
                        if total > 2 * 1024 * 1024 or time.monotonic() - started > self.config.settings.timeout:
                            raise ValueError('网页超出 2 MB 或请求超时限制')
                        chunks.append(chunk)
                        if probe:
                            break
                    body = b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace')
                    return {'ok': True, 'url': current, 'status': response.status_code, 'body': body}
            raise ValueError('网页重定向次数过多')
        finally:
            http.close()
            self.io_slots.release()

    def search(self, data, cancel=None):
        if not self.io_slots.acquire(False):
            raise ValueError('网络通道繁忙，请稍后重试')
        try:
            return self._search_pool.submit(self._search, data, cancel).result()
        finally:
            self.io_slots.release()

    def _search(self, data, cancel=None):
        from .adapters import adapter_for
        from .adapters.base import SearchContext
        query = str(data.get('query') or '').strip()[:300]
        if not query or self.config.match_blacklist(query):
            raise ValueError('搜索词为空或命中黑名单')
        site = next((s for s in self.sources() if s.id == data.get('source_id')), None)
        if not site:
            raise ValueError('source_id 必须是本次搜索范围内的站点 ID；GET /api/network/config 查看 sources')
        http = self._search_http
        task_id = str(data.get('task_id') or '')
        task = self.server.current_task or {}
        kind = task.get('filters', {}).get('resource_type', 'video')
        cancel = cancel if cancel is not None else self.server.cancellation(task_id)
        http.cancel_event = cancel
        def watch():
            if not self.server.accepts(task_id) or self.server.paused:
                cancel.set()
            return cancel.is_set()
        try:
            if watch():
                raise ValueError('任务已取消')
            from .search_evidence import MAX_SEARCH_PAGE
            page = int(data.get('page') or 1)
            if not 1 <= page <= MAX_SEARCH_PAGE:
                raise ValueError('page 必须在 1 到 %d 之间；不得重复读取上限页来冒充新页' % MAX_SEARCH_PAGE)
            with self.server._task_lock:
                limited = self.search_evidence.limit(data)
            if limited:
                return {'ok': True, 'results': [], 'pagination': {'page': page,
                    'exhausted': False, 'supported': True, 'limited': True, 'limit_reason': limited},
                    'note': '本轮该检索已停止推进，不再发起网页请求。处理其他未检查来源后提交done:true；这不代表网站没有资源。'}
            ctx = SearchContext(query, http, max_results=50, cancel=cancel, page=page, resource_type=kind)
            ctx.cancelled = watch
            rows = (adapter_for(site) if kind == 'video' else adapter_for(site, kind)).search(ctx, site)
            pagination = {'page': page, 'exhausted': getattr(rows, 'exhausted', None),
                          'supported': getattr(rows, 'supported', True),
                          'message': getattr(rows, 'message', '')}
            if watch():
                raise ValueError('任务已取消')
            rows = [r for r in rows if not self.config.match_blacklist(' '.join((r.title, r.desc, r.url, r.author)))]
            return {'ok': True, 'results': [asdict(r) for r in rows[:50]], 'pagination': pagination,
                    'note': '筛选候选后 POST results 的 candidate_ids 数组。limited=true 表示本轮停止该检索，处理其他来源后done:true，不再翻页；exhausted=true才表示末页。其余false可继续，null表示未知；supported=false不重试。'}
        finally:
            http.cancel_event = None

    def instructions(self):
        base = self.server.url + '/api/'
        token = '?token=' + self.server.token
        return (
            '\n【3.0 接管与网络：优先执行】\n'
            '任务 network.scope 与 network/config.sources 给出本次范围；国内任务只搜国内来源，'
            '不要因为界面也勾选 YouTube 就检测或查询它。国内来源走工具默认直连，无需设置全局代理。\n'
            'GET task 若返回 kind=connect：先 POST ' + base + 'connect' + token +
            '，JSON 为 {"agent":"同一个名字","challenge":"task.challenge","ok":true}，然后继续 GET task。'
            '只有确认后再次轮询才算接管成功；不要 ACK connect 任务。无法接管时 ok:false 并附 error。\n'
            '搜索任务含 network：required=true 时先测试所有 targets，在确认前不要搜或推结果。'
            '可以用自己的实际联网工具逐项实测，或使用下方标准库 Python 助手；助手最多并行检测 3 个目标，有代理时优先代理，然后直连和中转。\n'
            'GET ' + base + 'network/config' + token + '&agent=你的名字&task_id=当前任务号 获取配置。\n'
            'GET ' + base + 'network/helper' + token + ' 返回 JSON 中的 script：保存为临时 rf_agent_net.py（UTF-8），先审阅再执行。\n'
            '运行：python rf_agent_net.py --base ' + self.server.url + ' --token ' + self.server.token +
            ' --agent "你的名字" --task-id "当前任务号"。它自动实测并上报，不安装依赖、不改系统配置。'
            '没有 Python 时用自己的 HTTP 能力按同一协议测试并报告，不要假装运行成功。\n'
            'POST network/report 带 agent, task_id, check_id, attempts 数组：'
            '[{"url":"目标完整地址","route":"native|direct|proxy|relay","ok":true,"status":200,"error":"失败时原样原因"}]。'
            'relay 成功需先 POST network/probe {agent,task_id,url} 并在报告带返回 receipt。\n'
            '为缩短首屏时间，网络通过后优先复用工具搜索通道（也可使用自己的联网搜索）：POST network/fetch {agent,task_id,url} 获取公开网页；'
            'POST network/search {agent,task_id,source_id,query,page:1} 使用工具网络搜索，拿到候选后自行核实、去重和回推。'
            'network/config 的顶层sources提供站点列表，network只提供连通信息；所有 POST 都带 URL token。'
            '此搜索通道仅提供候选，不理解用户原话，也不审核主题。query使用Agent确定的检索词，候选须由Agent审核相关性后选择candidate_ids回推。\n'
            '若使用代理，显式传请求级代理或只设置当前子进程 HTTP_PROXY/HTTPS_PROXY，同时 NO_PROXY=127.0.0.1,localhost,::1。'
            '这不会自动改变你内置联网工具；若它仍不可用，请改用命令 HTTP 或本地中转。'
            '若你的执行环境禁止网络或禁止使用这些通道，报告限制，不尝试绕过。\n'
            '检测中每 30 秒 POST progress 说明当前尝试。所有通道失败时上报 network/report 失败记录；'
            '无法执行检测/配置则 POST error，写明原始原因，界面会弹出报告。不要无限重试。\n'
            '若临时要访问未列出的外网站点，先 POST network/require {agent,task_id,targets:["https://目标域名/"]}，再检测。'
            '助手仅测试通道，不会替你永久修改网络：后续请求必须遵循返回的 routes；proxy 路径需 GET network/config 后显式使用代理，'
            'relay 路径需使用本地 fetch/search。连接通过仅代表该目标当前可访问，不代表能绕过站点登录、验证码或任意网站可用。\n'
            '以上新接口都以 ' + base + ' 为前缀并带 ' + token + '。\n'
        )
