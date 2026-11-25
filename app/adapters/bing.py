# -*- coding: utf-8 -*-
"""必应：先把「网页结果」当兜底搜索源，结果太少时再补一轮「视频结果」。

必应网页结果的列表结构很稳定（li.b_algo > h2 > a），所以直接按结构取；
视频结果页结构经常变，交给通用抽取器处理。
"""
from __future__ import annotations

import base64
import json
import importlib.util
import re
from typing import List
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from ..config import SiteConfig
from ..models import SearchResult, normalize_url, parse_pub_ts
from ..net import NetworkError
from .base import Adapter, SearchContext
from .pagination import PageResults, stream_state
from .generic import GenericAdapter

try:
    from bs4 import BeautifulSoup
except Exception:                        # pragma: no cover
    BeautifulSoup = None  # type: ignore

try:
    _HAS_LXML = importlib.util.find_spec("lxml") is not None
except Exception:                        # pragma: no cover
    _HAS_LXML = False
_PARSER = "lxml" if _HAS_LXML else "html.parser"


def real_url(href: str) -> str:
    """必应的搜索结果链接常常是 /ck/a?...&u=a1<base64> 的跳转地址，
    这里解出真正的网址，用户复制走才能直接打开。"""
    if "/ck/a" not in href:
        return href
    try:
        q = parse_qs(urlparse(href).query)
        u = (q.get("u") or [""])[0]
        if u.startswith("a1"):
            u = u[2:]
        u += "=" * (-len(u) % 4)
        real = base64.urlsafe_b64decode(u).decode("utf-8", "ignore")
        if real.startswith("http"):
            return real
    except Exception:
        pass
    return href


class BingAdapter(Adapter):
    name = "bing"

    def search(self, ctx: SearchContext, site: SiteConfig) -> List[SearchResult]:
        """视频结果优先（有封面，适合卡片墙），不够再补网页结果。"""
        query = ctx.query.strip()
        if not query:
            return []

        # Video pages carry actual video URLs; web fallback mixed articles into
        # the video feed and silently hid video-fetch failures.
        state = stream_state(ctx, site)
        page = max(1, int(ctx.page or 1))
        with state['lock']:
            if page in state['pages']:
                return state['pages'][page]
            if page != len(state['pages']) + 1:
                raise NetworkError('必应续页会话已过期，请重新搜索')
            buffer = list(state['buffer'])
            offset = state.get('offset', 1)
            seen = set(state.get('seen', set()))
            ended = state.get('ended', False)
            message = ''
            # A duplicate page is not exhaustion. Look ahead at most three
            # native pages per user gesture, then explain instead of spinning.
            for _ in range(3):
                if buffer or ended:
                    break
                native = self._videos(ctx, site, query, first=offset)
                next_offset = getattr(native, 'next_offset', None)
                offset = next_offset if next_offset and next_offset > offset else offset + max(1, len(native), 35)
                ended = native.exhausted is True
                for row in native:
                    key = normalize_url(row.url)
                    if key not in seen:
                        seen.add(key)
                        buffer.append(row)
            if not buffer and not ended:
                message = '必应连续返回重复结果，尚未确认末页；再次触底可继续尝试'
            if ctx.cancelled():
                raise NetworkError('已停止')
            out, remaining = buffer[:ctx.max_results], buffer[ctx.max_results:]
            result = PageResults(out, exhausted=ended and not remaining, message=message)
            state.update(buffer=remaining, offset=offset, seen=seen, ended=ended)
            state['pages'][page] = result
            return result

    # ------------------------------------------------------------------ #
    def _web(self, ctx: SearchContext, site: SiteConfig, query: str) -> List[SearchResult]:
        if BeautifulSoup is None:
            return []
        first = 1 + (max(1, int(ctx.page or 1)) - 1) * 10
        url = ("https://www.bing.com/search?q=%s&count=%d&first=%d&setlang=zh-hans"
               % (quote_plus(query), max(10, min(40, ctx.max_results + 5)), first))
        html = ctx.http.get_text(url, site)
        soup = BeautifulSoup(html, _PARSER)
        out: List[SearchResult] = []
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a[href]") or li.select_one("a[href]")
            if a is None:
                continue
            href = (a.get("href") or "").strip()
            href = real_url(href)
            if not href.startswith("http"):
                continue
            title = self.clean_text(a.get_text(" ", strip=True))
            if not title:
                continue
            desc = ""
            p = li.select_one(".b_caption p") or li.select_one("p")
            if p is not None:
                desc = self.clean_text(p.get_text(" ", strip=True))
            cover = ""
            img = li.select_one("img")
            if img is not None:
                cover = GenericAdapter._img_src(img)
                if cover:
                    cover = urljoin(url, cover)
            out.append(SearchResult(
                title=title, url=href, source=site.display, source_id=site.id,
                cover=cover, desc=desc[:300],
            ))
        return out

    def _videos(self, ctx: SearchContext, site: SiteConfig, query: str, first=1) -> List[SearchResult]:
        size = 35  # native infinite-scroll count from the site's acph-url
        url = ("https://www.bing.com/videos/search?q=%s&count=%d&first=%d"
               % (quote_plus(query), size, first))
        if first > 1:
            url = ('https://www.bing.com/videos/async/rankedans?q=%s&mmasync=1'
                   '&varh=VideoResultInfiniteScroll&vdpp=VideoResultAsync&count=%d&first=%d'
                   % (quote_plus(query), size, first))
        html = ctx.http.get_text(url, site)
        soup = BeautifulSoup(html, _PARSER)
        rows, seen = [], set()
        for card in soup.select('[mmeta]'):
            try:
                meta = json.loads(card['mmeta'])
            except (ValueError, TypeError):
                continue
            link = real_url(meta.get('pgurl') or meta.get('murl') or '')
            title_el = card.select_one('.mc_vtvc_title')
            title = title_el.get('title') or title_el.get_text(' ', strip=True) if title_el else ''
            key = normalize_url(link)
            if not title or not link.startswith(('http://', 'https://')) or key in seen:
                continue
            seen.add(key)
            img = card.select_one('img')
            duration = card.select_one('.mc_bc_rc.items')
            author = card.select_one('.mc_vtvc_meta_row_channel')
            published = card.select_one('.meta_pd_content')
            rows.append(SearchResult(title=title, url=link, source=site.display, source_id=site.id,
                cover=GenericAdapter._img_src(img) or meta.get('turl', ''),
                duration=duration.get_text(strip=True) if duration else '',
                author=author.get_text(strip=True) if author else '',
                pub_ts=parse_pub_ts(published.get_text(strip=True) if published else '')))
        if rows:
            result = PageResults(rows, exhausted=False)
            for more in soup.select('[acph-url]'):
                try:
                    next_offset = int(parse_qs(urlparse(more['acph-url']).query).get('first', ['0'])[0])
                    if next_offset > first:
                        result.next_offset = next_offset
                        break
                except (ValueError, TypeError):
                    pass
            return result
        text = soup.get_text(' ', strip=True)
        if re.search(r'没有与.*相关的结果|没有找到.*结果|No results found|There are no results', text, re.I):
            return PageResults(exhausted=True)
        fake = SiteConfig(id=site.id, name="必应视频", kind="generic",
                          search_url=url, referer="https://www.bing.com/",
                          max_results=site.max_results)
        results = GenericAdapter().extract(html, url, fake, ctx.max_results, query)
        # 必应在「搜不到」时会给一页与关键词无关的兜底视频，这里按关键词过一遍，
        # 免得卡片墙里冒出一堆风马牛不相及的东西。
        tokens = [t for t in re.split(r"\s+", query.casefold()) if len(t) >= 2]
        if tokens:
            results = [r for r in results
                       if any(t in (r.title + " " + r.url).casefold() for t in tokens)]
        for r in results:
            r.url = real_url(r.url)
            r.source = site.display
            r.source_id = site.id
        if not results:
            raise NetworkError('必应未返回视频或明确的结束标记，不能判断已搜完；滚动可重试')
        return PageResults(results, exhausted=None)

