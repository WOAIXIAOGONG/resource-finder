# -*- coding: utf-8 -*-
"""网络层：自动识别代理、按站点选择通道、带降级重试的 HTTP 客户端。

代理识别顺序
    1. 设置里手动指定的代理（manual）
    2. 环境变量 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY（含小写）
    3. 系统代理注册表（WinINET ProxyEnable / ProxyServer）
    4. 都不用，直连
"""
from __future__ import annotations

import os
import queue
import socket
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ACCEPT_LANG = "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7"

# 「自动」通道下先走哪条路：国内能直连的站优先直连（走代理反而可能拿到
# 缓存错乱的内容），墙外的站优先代理。
_DIRECT_FIRST = (
    "bing.com", "baidu.com", "bilibili.com", "hdslb.com", "biliimg.com",
    "youku.com", "iqiyi.com", "qq.com", "weibo.com", "douban.com",
    "sogou.com", "so.com", "360kan.com", "acfun.cn", "zhihu.com",
    "mgtv.com", "ixigua.com", "douyin.com", "kuaishou.com",
)
_PROXY_FIRST = (
    "youtube.com", "ytimg.com", "googlevideo.com", "google.com",
    "vimeo.com", "dailymotion.com", "twimg.com", "twitter.com",
)


def _host_in(host: str, domains: Tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


class NetworkError(Exception):
    """一次请求在所有通道上都失败。"""

    def __init__(self, message: str, route: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.route = route
        self.status = status


class SearchSessionExpired(NetworkError):
    """A search page/cursor was lost; this says nothing about account login."""


# --------------------------------------------------------------------------- #
# 代理识别
# --------------------------------------------------------------------------- #
_probe_lock = threading.Lock()
_probe_cache: Dict[str, Tuple[bool, float]] = {}


def _clean_proxy(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if "://" not in v:
        v = "http://" + v
    # 只保留 scheme://host:port，去掉路径部分
    try:
        parts = urlsplit(v)
        host = parts.hostname or ""
        port = parts.port or 80
        if not host:
            return ""
        if parts.scheme not in ("http", "https"):
            return ""
        return "%s://%s" % (parts.scheme, parts.netloc)
    except Exception:
        return ""


def _env_proxy() -> str:
    # 小写优先：某些环境下小写才是实际生效的那份
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(key)
        if v:
            cleaned = _clean_proxy(v)
            if cleaned:
                return cleaned
    return ""


def _registry_proxy() -> str:
    """读取 Windows 系统代理开关。失败（非 Windows / 无权限）返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                enabled = 0
            if not int(enabled):
                return ""
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except OSError:
                return ""
        server = str(server or "").strip()
        if not server:
            return ""
        # 形式可能是 "127.0.0.1:7897" 或 "http=127.0.0.1:7897;https=127.0.0.1:7897"
        if "=" in server:
            mapping = {}
            for part in server.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    mapping[k.strip().lower()] = v.strip()
            server = mapping.get("https") or mapping.get("http") or ""
        return _clean_proxy(server)
    except Exception:
        return ""


def system_proxy_state() -> Tuple[bool, str]:
    """读 Windows「系统代理」开关。

    返回 (能否读到, 生效的代理地址)；关闭时地址为空串。
    这个函数决定「自动模式」到底走代理还是直连——用户把 VPN 的
    系统代理关掉之后，程序就该跟着走直连，而不是偷偷用别处的代理。
    """
    if os.name != "nt":
        return False, ""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
    except Exception:
        return False, ""
    with key:
        try:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except OSError:
            enabled = 0
        if not int(enabled):
            return True, ""
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            return True, ""
    server = str(server or "").strip()
    if not server:
        return True, ""
    if "=" in server:
        mapping = {}
        for part in server.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                mapping[k.strip().lower()] = v.strip()
        server = mapping.get("https") or mapping.get("http") or ""
    return True, _clean_proxy(server)


def detect_proxy(settings) -> str:
    """按优先级算出当前要用的代理地址，空串表示直连。

    自动模式**以系统代理开关为准**：用户关了系统代理（关 VPN），
    这里就返回空串走直连，跟浏览器保持一致。读不到注册表时才退回
    环境变量（有些绿色版代理只设环境变量）。
    """
    mode = getattr(settings, "proxy_mode", "auto") or "auto"
    if mode == "direct":
        return ""
    if mode == "manual":
        return _clean_proxy(getattr(settings, "proxy_url", "") or "")
    known, sys_url = system_proxy_state()
    if known:
        return sys_url
    return _env_proxy()


def proxy_alive(proxy_url: str, timeout: float = 0.45, ttl: float = 8.0) -> bool:
    """快速探测代理端口是否在监听（结果缓存，避免每个请求都探）。"""
    if not proxy_url:
        return False
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(proxy_url)
        if hit and now - hit[1] < ttl:
            return hit[0]
    ok = False
    try:
        parts = urlsplit(proxy_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        ok = True
    except Exception:
        ok = False
    with _probe_lock:
        _probe_cache[proxy_url] = (ok, now)
    return ok


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class HttpClient:
    """线程安全（每个线程独立 session）的 HTTP 客户端。"""

    def __init__(self, settings, log=None, *, track_sessions=False) -> None:
        self.settings = settings
        self.log = log or (lambda *a, **k: None)
        self._local = threading.local()
        self._sessions: Dict[Tuple[str, str], requests.Session] = {}
        self._track_sessions = track_sessions
        self._lock = threading.Lock()
        # 引擎搜索时挂上取消事件：用户点「停止」后新请求立刻放弃、重试不再等超时


    @property
    def cancel_event(self):
        return getattr(self._local, "cancel", None)

    @cancel_event.setter
    def cancel_event(self, event):
        self._local.cancel = event

    # -- session 池 ------------------------------------------------------- #
    def _session(self, proxy_url: str) -> requests.Session:
        sess = getattr(self._local, "sessions", None)
        if sess is None:
            sess = {}
            self._local.sessions = sess
        ctx_key = proxy_url
        if ctx_key in sess:
            return sess[ctx_key]
        s = requests.Session()
        s.trust_env = False
        if proxy_url:
            s.proxies = {"http": proxy_url, "https": proxy_url}
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=12, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({
            "User-Agent": DEFAULT_UA,
            "Accept-Language": _ACCEPT_LANG,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })
        sess[ctx_key] = s
        if self._track_sessions:
            with self._lock:
                self._sessions[(threading.get_ident(), ctx_key)] = s
        return s

    # -- 通道 ------------------------------------------------------------- #
    def routes_for(self, site_route: str = "auto", host: str = "") -> List[str]:
        """返回本次请求要依次尝试的通道：'proxy' / 'direct'。"""
        proxy_url = detect_proxy(self.settings)
        has_proxy = bool(proxy_url) and proxy_alive(proxy_url)
        if site_route == 'proxy_only':
            return ['proxy'] if has_proxy else []
        if site_route == "direct":
            return ["direct"]
        if not has_proxy:
            return ["direct"]
        if site_route == "proxy":
            return ["proxy", "direct"]
        if host and _host_in(host, _PROXY_FIRST):
            return ["proxy", "direct"]
        if host and _host_in(host, _DIRECT_FIRST):
            return ["direct", "proxy"]
        return ["proxy", "direct"]

    def proxy_url(self) -> str:
        p = detect_proxy(self.settings)
        return p if p and proxy_alive(p) else ""

    def _request_wait(self, request, cancel, deadline, *args, **kwargs):
        """A long IO allowance must not delay pause/disconnect or application exit."""
        finished = threading.Event()
        abandoned = threading.Event()
        outcome = []
        def run():
            try:
                response = request(*args, **kwargs)
                if abandoned.is_set():
                    response.close()
                outcome.append((True, response))
            except Exception as exc:
                outcome.append((False, exc))
            finally:
                finished.set()
        threading.Thread(target=run, name='http-request', daemon=True).start()
        while not finished.wait(.1):
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                abandoned.set()
                # A future request must not reuse the in-flight Session.
                self._local.sessions = {}
                if cancel is not None and cancel.is_set():
                    raise NetworkError('已停止')
                raise requests.exceptions.Timeout()
        ok, value = outcome[0]
        if not ok:
            raise value
        return value

    # -- 请求 ------------------------------------------------------------- #
    def get(
        self,
        url: str,
        site=None,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        route: str = "auto",
        allow_status: Tuple[int, ...] = (200,),
        stream: bool = False,
        params: Optional[Dict[str, str]] = None,
        allow_redirects: bool = True,
        _method: str = 'GET',
        json=None,
    ) -> requests.Response:
        hdrs = {"Referer": getattr(site, "referer", "") or url} if site else {}
        if site and getattr(site, "headers", None):
            hdrs.update({k: v for k, v in site.headers.items() if isinstance(v, str)})
        if headers:
            hdrs.update(headers)
        # 默认 Referer 用目标站点根地址，很多图床/接口靠这个过防盗链
        if "Referer" not in hdrs or not hdrs["Referer"]:
            hdrs.pop("Referer", None)

        if route == "auto" and site is not None:
            route = getattr(site, "route", "auto")
        to = timeout or getattr(self.settings, "timeout", 180.0)
        last_err = "未知错误"
        last_status = 0
        deadline = time.monotonic() + to
        failures = []
        base_timeout = to
        cancel = getattr(self, "cancel_event", None)
        if cancel is not None and cancel.is_set():
            base_timeout = min(base_timeout, 2.5)     # 已经点了停止，别磨蹭

        for route_name in self.routes_for(route, urlsplit(url).hostname or ""):
            proxy_url = detect_proxy(self.settings) if route_name == "proxy" else ""
            if route_name == "proxy" and not proxy_url:
                continue
            sess = self._session(proxy_url)
            for attempt in (1, 2):
                base_timeout = max(.1, deadline - time.monotonic())
                if time.monotonic() >= deadline:
                    break
                if cancel is not None and cancel.is_set():
                    raise NetworkError("已停止", route_name, 0)
                try:
                    request = sess.get if _method == 'GET' else lambda *a, **k: sess.request(_method, *a, json=json, **k)
                    resp = self._request_wait(request, cancel, deadline,
                        url, params=params, headers=hdrs,
                        timeout=(min(30.0, base_timeout), base_timeout),
                        allow_redirects=allow_redirects, stream=stream,
                    )
                    last_status = resp.status_code
                    if cancel is not None and cancel.is_set():
                        resp.close()
                        raise NetworkError("已停止", route_name, 0)
                    if resp.status_code in allow_status:
                        return resp
                    if resp.status_code in (401, 403, 404, 410, 429):
                        # 状态码类错误换通道一般也没用，直接抛出
                        raise NetworkError(
                            "HTTP %d" % resp.status_code, route_name, resp.status_code
                        )
                    last_err = "HTTP %d" % resp.status_code
                    if attempt == 1:
                        time.sleep(0.35)
                        continue
                except NetworkError:
                    raise
                except requests.exceptions.SSLError as exc:
                    if cancel is not None and cancel.is_set():
                        raise NetworkError("已停止", route_name, 0) from exc
                    last_err = "证书错误 %s" % _short(exc)
                    if getattr(self.settings, "insecure_fallback", True):
                        try:
                            resp = self._request_wait(request, cancel, deadline, url, params=params, headers=hdrs,
                                            timeout=(min(30.0, base_timeout), base_timeout),
                                            verify=False, allow_redirects=allow_redirects, stream=stream)
                            if resp.status_code in allow_status:
                                return resp
                            last_status = resp.status_code
                            last_err = "HTTP %d" % resp.status_code
                        except Exception as exc2:      # noqa: BLE001
                            last_err = "证书降级失败 %s" % _short(exc2)
                    break
                except requests.exceptions.Timeout:
                    last_err = "超时 %.0fs" % base_timeout
                    break
                except Exception as exc:               # noqa: BLE001
                    last_err = "%s %s" % (type(exc).__name__, _short(exc))
                    if attempt == 1 and _is_retryable(exc):
                        time.sleep(0.3)
                        continue
                    break
            failures.append(route_name + ": " + last_err)
        raise NetworkError("；".join(failures) or last_err, "", last_status)

    def get_text(self, url: str, site=None, **kwargs) -> str:
        resp = self.get(url, site, **kwargs)
        if getattr(site, "encoding", ""):
            resp.encoding = site.encoding          # type: ignore[union-attr]
        elif not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def post_json(self, url: str, site=None, *, json, headers=None):
        resp = self.get(url, site, _method='POST', json=json, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise NetworkError('接口未返回 JSON，可能需要验证或稍后重试') from exc
        finally:
            resp.close()

    def get_json(self, url: str, site=None, *, params: Optional[Dict[str, str]] = None,
                 headers: Optional[Dict[str, str]] = None,
                 timeout: Optional[float] = None, route: str = "auto"):
        """取 JSON。接口偶尔返回 HTML（风控页），这时抛 NetworkError。"""
        resp = self.get(url, site, params=params, headers=headers, timeout=timeout,
                        route=route, allow_status=(200,))
        text = resp.text
        try:
            return resp.json()
        except ValueError:
            head = (text or "")[:200].replace("\n", " ")
            raise NetworkError("返回的不是 JSON：%s" % head, "", resp.status_code)

    def get_bytes(self, url: str, site=None, *, timeout: float = 8.0,
                  route: str = "auto", max_bytes: int = 6 * 1024 * 1024, headers=None,
                  allow_redirects: bool = True) -> bytes:
        resp = self.get(url, site, timeout=timeout, route=route, stream=True,
                        allow_status=(200,), headers=headers, allow_redirects=allow_redirects)
        chunks: List[bytes] = []
        total = 0
        try:
            for chunk in resp.iter_content(16384):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise NetworkError("下载内容超过大小限制")
        finally:
            resp.close()
        return b"".join(chunks)

    # -- 跳转解析 --------------------------------------------------------- #
    def resolve(self, url: str, *, timeout: float = 8.0, route: str = "auto") -> str:
        """跟随一次跳转，返回最终落地地址。失败时原样返回。"""
        try:
            resp = self.get(url, timeout=timeout, route=route, stream=True,
                            allow_status=tuple(range(200, 400)) + (403, 404, 410, 429, 500))
        except NetworkError:
            return url
        try:
            final = resp.url or url
            return final if final.startswith("http") else url
        finally:
            resp.close()

    def close(self) -> None:
        sess = getattr(self._local, "sessions", None) or {}
        for s in list(sess.values()):
            try:
                s.close()
            except Exception:
                pass
        sess.clear()
        with self._lock:
            for key in [k for k in self._sessions if k[0] == threading.get_ident()]:
                self._sessions.pop(key, None)

    def close_all(self):
        """Only after all workers using this client have stopped."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[:120]


def _is_retryable(exc: Exception) -> bool:
    retryable = (
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )
    return isinstance(exc, retryable)


_REDIRECT_HINTS = (
    "baidu.com/link?", "bing.com/ck/", "google.com/url?",
    "sogou.com/link?", "so.com/link?", "link.zhihu.com/?target=",
)


def looks_redirect(url: str) -> bool:
    """搜索引擎的跳转链接：复制给用户之前最好先解成真实地址。"""
    low = (url or "").lower()
    return any(hint in low for hint in _REDIRECT_HINTS)


def describe_network(settings) -> str:
    """给「设置」界面显示一行网络诊断信息。"""
    known, sys_url = system_proxy_state()
    detected = detect_proxy(settings)
    if known and not sys_url:
        return "当前：系统代理已关闭 → 直连（和浏览器一致）"
    if not detected:
        return "当前：直连（未检测到代理）"
    ok = proxy_alive(detected)
    return "当前：%s %s" % (detected, "✅ 可用" if ok else "❌ 端口未监听，将自动直连")


def _probe_one(url: str, proxies: Optional[Dict[str, str]],
               timeout: float = 8.0) -> Tuple[bool, float]:
    """探一个 URL，返回 (是否 2xx/3xx, 耗时秒)。

    注意 trust_env=False：不加这个，requests 会自己捡 HTTP_PROXY 环境变量，
    于是「直连」其实偷偷走了代理，用户关了 VPN 也会显示能上外网。
    """
    started = time.monotonic()
    session = requests.Session()
    session.trust_env = False
    session.proxies = dict(proxies) if proxies else {}
    session.headers.update({"User-Agent": DEFAULT_UA})
    try:
        # 连接 3.5s、读 6s 封顶：探针只回答「通不通」，不值得为了一个慢端点
        # 让用户点完「刷新网络」干等十几秒（真慢的端点会被并发里的别的端点抢先）。
        r = session.get(url, timeout=(3.5, min(timeout, 6.0)), verify=False,
                        allow_redirects=False)
        ok = 200 <= r.status_code < 400
        r.close()
    except Exception:                                      # noqa: BLE001
        ok = False
    finally:
        session.close()
    return ok, time.monotonic() - started


# 境外探针：哪个先回 204 就算通（用户挂了 VPN 时这四个里总有一个能过）
# 只放「在国内一定连不上」的端点：gstatic / cloudflare 在国内有可用 CDN 节点
# （实测 gstatic 解析到 113.108.239.162 直接 204），拿它们当「能上外网」的证据是错的。
_ABROAD_PROBES = (
    "https://www.youtube.com/generate_204",
    "https://www.google.com/generate_204",
    "https://www.facebook.com/generate_204",
    "https://www.instagram.com/generate_204",
)


def _race(candidates: List[Tuple[str, str, Optional[Dict[str, str]]]],
          timeout: float = 8.0) -> Optional[Tuple[bool, str, str, float]]:
    """并发探多个 (route, url, proxies)，返回最先成功的一条 (ok, route, url, 秒)。

    并发是必须的：YouTube 走代理要 7 秒多，串行探四个端点会让「刷新网络」
    转半分钟，用户会以为卡死了。
    """
    q: "queue.Queue" = queue.Queue()
    alive = []

    def run(route: str, url: str, pm: Optional[Dict[str, str]]) -> None:
        ok, ms = _probe_one(url, pm, timeout)
        q.put((ok, route, url, ms))

    for route, url, pm in candidates:
        t = threading.Thread(target=run, args=(route, url, pm), daemon=True,
                             name="probe-%s" % route)
        t.start()
        alive.append(t)

    deadline = time.monotonic() + timeout + 0.8
    left = len(alive)
    while left > 0:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        try:
            ok, route, url, ms = q.get(timeout=remain)
        except queue.Empty:
            break
        left -= 1
        if ok:
            return ok, route, url, ms
    return None


def probe_network(settings, timeout: float = 8.0) -> Dict[str, object]:
    """实测当前网络：系统代理开没开、国内能不能上、境外能不能上、走的哪条通道。

    用户开关 VPN 之后点「刷新网络」，就知道现在能用哪些站点。
    检测完全按「程序实际会走的路」来：自动模式下系统代理是关的，这里就
    只测真·直连，绝不会用环境变量里的代理冒充「能上外网」。
    返回 {proxy, proxy_source, proxy_ok, system_proxy, domestic, domestic_ms,
          abroad, abroad_ms, route, probe_url, note}
    """
    known, sys_url = system_proxy_state()
    mode = getattr(settings, "proxy_mode", "auto") or "auto"
    proxy = detect_proxy(settings)
    proxy_ok = bool(proxy) and proxy_alive(proxy, ttl=0)
    proxy_map = {"http": proxy, "https": proxy} if proxy_ok else None
    if mode == "manual":
        source = "manual"
    elif mode == "direct":
        source = "none"
    elif known:
        source = "system" if proxy else "none"
    else:
        source = "env" if proxy else "none"
    system_proxy = "off" if (known and not sys_url) else ("on" if sys_url else "unknown")

    # 国内：直连优先（走代理反而可能被限速），直连不通再试代理
    domestic, domestic_ms = False, 0.0
    for url in ("https://www.baidu.com/", "https://www.qq.com/"):
        ok, ms = _probe_one(url, None, min(timeout, 6.0))
        if ok:
            domestic, domestic_ms = True, ms
            break
    if not domestic and proxy_map:
        ok, ms = _probe_one("https://www.baidu.com/", proxy_map, min(timeout, 6.0))
        domestic, domestic_ms = ok, ms

    # 境外：代理优先，代理全挂再直连（没挂 VPN 时直连必然失败，这是正常的）
    candidates: List[Tuple[str, str, Optional[Dict[str, str]]]] = []
    if proxy_map:
        for url in _ABROAD_PROBES:
            candidates.append(("proxy", url, proxy_map))
    for url in _ABROAD_PROBES:
        candidates.append(("direct", url, None))

    # 没有代理时，直连境外在国内容器里注定失败，4.5s 足够；有代理才给满 8s
    hit = _race(candidates, timeout if proxy_map else min(timeout, 3.5))
    abroad = hit is not None
    route = hit[1] if hit else ""
    abroad_ms = hit[3] if hit else 0.0
    probe_url = hit[2] if hit else ""

    # 说明文案（按「实际走的通道」说话，别把 manual 模式说成系统代理）
    shown_proxy = (proxy or "").replace("http://", "")
    foreign_endpoints = "youtube / google / facebook / instagram"
    if source == "manual":
        if not proxy_ok:
            note = "手动代理 %s 端口没在监听：按直连处理" % (shown_proxy or "（空）")
        elif abroad:
            note = "手动代理可用，境外站点走代理访问"
        else:
            note = "手动代理端口在监听，但境外站点全部不通：检查代理软件是否真的连上"
    elif source == "system":
        if not proxy_ok:
            note = "系统代理开着（%s），但端口没在监听：按直连处理" % shown_proxy
        elif abroad and route == "proxy":
            note = "系统代理已开启，境外站点走代理访问"
        elif abroad and route == "direct":
            note = "系统代理开着，但 %s 走代理没通，已自动改走直连" % foreign_endpoints
        else:
            note = "系统代理开着，但境外站点全部超时：VPN 可能没连上，或被安全软件拦住"
    elif system_proxy == "off":
        if abroad and route == "direct":
            note = "系统代理已关闭，但境外站点能直连：可能开着 TUN / 全局代理"
        elif abroad:
            note = "系统代理已关闭，但境外站点居然能通：可能开着 TUN / 全局代理"
        else:
            note = "系统代理已关闭（等于 VPN 关着）：境外站点访问不了，这是正常的"
    else:
        note = "没有检测到系统代理：国外站点（YouTube 等）需要先开 VPN，再点「刷新网络」"

    return {
        "proxy": proxy, "proxy_ok": proxy_ok,
        "proxy_source": source, "system_proxy": system_proxy,
        "domestic": domestic, "domestic_ms": domestic_ms,
        "abroad": abroad, "abroad_ms": abroad_ms,
        "route": route, "probe_url": probe_url,
        "note": note,
    }
