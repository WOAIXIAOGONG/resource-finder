# -*- coding: utf-8 -*-
"""搜索引擎：把关键词并发分发到白名单里的每个站点，边搜边回调。

线程模型
    引擎自己跑在一个后台线程里；它内部再用线程池并发访问各站点，
    每个站点的工作全部落在同一个 worker 线程上（B 站需要 cookie 预热）。
    所有回调都从 worker 线程发出，GUI 侧通过 Qt 信号切回主线程。
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional

from .adapters import adapter_for
from .adapters.base import SearchContext
from .config import Config, SiteConfig
from .models import SearchResult, SearchStats, SiteEvent, normalize_url, title_key
from .net import HttpClient, NetworkError
from .login import LoginRequired, login_manager

EventCallback = Callable[[SiteEvent], None]
LogCallback = Callable[[str], None]


class SearchEngine:
    def __init__(self, config: Config, http: HttpClient,
                 log: Optional[LogCallback] = None) -> None:
        self.config = config
        self.http = http
        self.logins = login_manager(config)
        self.log = log or (lambda msg: None)

    # ------------------------------------------------------------------ #
    def run(self, query: str, on_event: EventCallback,
            cancel: Optional[threading.Event] = None,
            sites: Optional[List[SiteConfig]] = None,
            page: int = 1, pages: Optional[Dict[str, int]] = None) -> SearchStats:
        query = (query or "").strip()
        started = time.time()
        cancel = cancel or threading.Event()
        stats = SearchStats(query=query, started=started)

        if not query:
            on_event(SiteEvent(kind="fatal", error="请输入要搜索的内容"))
            stats.finished = time.time()
            return stats

        hit = self.config.match_blacklist(query)
        if hit:
            on_event(SiteEvent(kind="fatal",
                               error="关键词命中黑名单「%s」，已拒绝搜索" % hit))
            stats.finished = time.time()
            return stats

        targets = [s for s in (sites if sites is not None else self.config.enabled_sites())
                   if s.enabled and s.is_valid()]
        stats.sites_total = len(targets)
        if not targets:
            on_event(SiteEvent(kind="fatal", error="白名单里没有启用的站点，请先添加"))
            stats.finished = time.time()
            return stats

        for site in targets:
            on_event(SiteEvent(kind="start", site_id=site.id, site_name=site.display))

        results: List[SearchResult] = []
        seen_urls: Dict[str, SearchResult] = {}
        seen_titles: Dict[str, SearchResult] = {}
        lock = threading.Lock()

        workers = max(1, min(len(targets), int(self.config.settings.workers or 6)))
        # 让 HttpClient 也知道「用户点了停止」，否则它会一直等到 12s 超时
        self.http.cancel_event = cancel
        try:
            return self._run_pool(targets, query, cancel, stats, started, on_event,
                                  results, seen_urls, seen_titles, lock, workers,
                                  page=max(1, int(page or 1)), pages=pages,
                                  resource_type=self.config.settings.resource_type)
        finally:
            self.http.cancel_event = None

    def _run_pool(self, targets, query, cancel, stats, started, on_event,
                  results, seen_urls, seen_titles, lock, workers, page=1, pages=None, resource_type='video'):
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search")
        futures = {
            pool.submit(self._one_site, site, query, cancel, stats, (pages or {}).get(site.id, page), resource_type): site
            for site in targets
        }
        pending = set(futures)
        try:
            while pending:
                if cancel.is_set():
                    # 用户点了「停止」：不再等已经发出的请求，立刻收尾。
                    # 工作线程还会在自己的超时里退出，但它们只返回数据、不发事件，
                    # 所以这里直接返回是安全的。
                    for fut in pending:
                        fut.cancel()
                    break
                done, pending = wait(pending, timeout=0.25,
                                     return_when=FIRST_COMPLETED)
                for fut in done:
                    self._handle_future(fut, futures[fut], cancel, stats, started,
                                        on_event, results, seen_urls, seen_titles, lock)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        stats.finished = time.time()
        stats.results = len(results)
        stats.cancelled = cancel.is_set()
        on_event(SiteEvent(kind="finish", elapsed=stats.finished - started))
        return stats

    def _handle_future(self, fut, site, cancel, stats, started, on_event,
                       results, seen_urls, seen_titles, lock):
        """处理一个已完成的站点任务：失败上报，成功则去重后发给界面。"""
        try:
            items, elapsed = fut.result()
        except Exception as exc:                             # noqa: BLE001
            if cancel.is_set():                              # 停止时后端在主动断开，不算站点错误
                return
            elapsed = time.time() - started
            if isinstance(exc, LoginRequired):
                self.logins.require("direct:" + str(id(cancel)), site, stats.query, 1, exc, cancel)
            msg = self._explain(exc)
            with lock:
                stats.sites_error += 1
            self.log("[%s] 失败：%s" % (site.display, msg))
            on_event(SiteEvent(kind="error", site_id=site.id,
                               site_name=site.display, error=msg,
                               elapsed=elapsed))
            return

        with lock:
            stats.sites_ok += 1
        fresh: List[SearchResult] = []
        for item in items:
            if cancel.is_set():
                break
            if self.config.match_blacklist(item.title) or \
                    self.config.match_blacklist(item.desc) or \
                    self.config.match_blacklist(item.url) or \
                    self.config.match_blacklist(item.author):
                continue
            uk = normalize_url(item.url)
            tk = title_key(item.title)
            if uk and uk in seen_urls:
                old = seen_urls[uk]
                old.extra['discovery_sources'] = sorted(set(old.extra.get('discovery_sources', []))
                                                       | {old.source_id, item.source_id})
                continue
            if uk:
                seen_urls[uk] = item
            if tk:
                seen_titles[tk] = item
            fresh.append(item)
        results.extend(fresh)
        stats.results = len(results)
        elapsed = time.time() - started
        self.log("[%s] %d 条，用时 %.1fs" % (site.display, len(fresh), elapsed))
        on_event(SiteEvent(kind="done", site_id=site.id, site_name=site.display,
                           results=fresh, elapsed=elapsed,
                           exhausted=getattr(items, 'exhausted', None),
                           pagination_supported=getattr(items, 'supported', True),
                           pagination_message=getattr(items, 'message', '')))

    # ------------------------------------------------------------------ #
    def _one_site(self, site: SiteConfig, query: str, cancel: threading.Event,
                  stats: SearchStats, page: int = 1, resource_type: str = 'video'):
        started = time.time()
        if cancel.is_set():
            return [], 0.0
        delay = float(getattr(self.config.settings, "polite_delay", 0.0) or 0.0)
        if delay > 0:
            time.sleep(random.uniform(0, delay))
        ctx = SearchContext(
            query=query,
            http=self.http,
            max_results=min(site.max_results,
                            int(self.config.settings.max_results_per_site or 24)),
            cancel=cancel,
            log=self.log,
            page=max(1, int(page or 1)),
            reset=page == 1,
            resource_type=resource_type,
        )
        kind = resource_type
        adapter = adapter_for(site) if kind == 'video' else adapter_for(site, kind)
        self.http.cancel_event = cancel
        try:
            items = adapter.search(ctx, site)
            if items is None:
                items = []
            return items, time.time() - started
        finally:
            self.http.cancel_event = None
            self.http.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _explain(exc: Exception) -> str:
        if isinstance(exc, NetworkError):
            extra = ""
            if exc.status:
                extra = "（HTTP %d）" % exc.status
            return "网络请求失败：%s%s" % (exc, extra)
        text = str(exc) or exc.__class__.__name__
        low = text.lower()
        if "getaddrinfo" in low or "name or service" in low:
            return "域名解析失败，检查网络或代理"
        if "timed out" in low or "timeout" in low:
            return "连接超时（可能被墙或站点不可达）"
        if "ssl" in low:
            return "证书校验失败"
        return text[:160]
