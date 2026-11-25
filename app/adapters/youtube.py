# -*- coding: utf-8 -*-
"""YouTube：抓结果页 HTML，解析内嵌的 ytInitialData JSON。

YouTube 的结果页是服务端渲染的，搜索结果都在 <script>var ytInitialData = {...}
里，不需要浏览器也不需要 API key。
"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Dict, List
from urllib.parse import quote_plus

from ..config import SiteConfig
from ..models import SearchResult, parse_count, parse_pub_ts
from ..net import NetworkError, SearchSessionExpired
from ..login import youtube_restriction
from ..durations import youtube_result_duration
from ..covers import youtube_cover
from .base import Adapter, SearchContext
from .pagination import PageResults, stream_state

_MARKERS = ("ytInitialData", "window[\"ytInitialData\"]", "window['ytInitialData']")


class YouTubeAdapter(Adapter):
    name = "youtube"

    def search(self, ctx: SearchContext, site: SiteConfig) -> List[SearchResult]:
        query = ctx.query.strip()
        if not query:
            return []
        state = stream_state(ctx, site)
        page = max(1, int(ctx.page or 1))
        with state['lock']:
            if page in state['pages']:
                return state['pages'][page]
            if page != len(state['pages']) + 1:
                raise NetworkError('YouTube 续页会话已过期或页码不连续，请重新搜索')
            buffer, token, config = list(state['buffer']), state['token'], state['config']
            initialized, attempts, browser_no_new = state['initialized'], 0, False
            recovered = False
            recovering = state.get('recovering', False)
            delivered = {r.url for rows in state['pages'].values() for r in rows}
            while not buffer and (not initialized or token) and attempts < 3:
                attempts += 1
                if not initialized:
                    url = 'https://www.youtube.com/results?search_query=%s&hl=zh-CN' % quote_plus(query)
                    browser = getattr(getattr(ctx.http, 'settings', None), '_login_browser', None)
                    if browser and (browser.active is True or getattr(browser, 'can_resume', lambda:False)() is True):
                        data, config = browser.search_page(query, ctx.cancel)
                    else:
                        html = ctx.http.get_text(url, site)
                        data = self._find_initial_data(html)
                        config = self._config(html)
                    if not data or 'contents' not in data:
                        raise NetworkError('YouTube 未返回可解析的搜索页，可能需要验证或页面结构已改变')
                    # Only search results, never tokens from sidebar recommendations.
                    data = self._primary(data['contents'])
                    initialized = True
                else:
                    context = config.get('INNERTUBE_CONTEXT')
                    if not context:
                        raise NetworkError('YouTube 缺少续页会话参数，请重新搜索')
                    browser = getattr(getattr(ctx.http, 'settings', None), '_login_browser', None)
                    session = config.get('_rf_browser_session')
                    post = (lambda url, site, json, headers: browser.continuation(json, ctx.cancel, session=session)) if session else ctx.http.post_json
                    try:
                        if session and (not browser or browser.active is not True):
                            raise SearchSessionExpired('后台搜索连接已变化')
                        data = post('https://www.youtube.com/youtubei/v1/search', site,
                            json={'context': context, 'continuation': token},
                            headers={'Origin': 'https://www.youtube.com',
                                     'Referer': 'https://www.youtube.com/',
                                     'X-Youtube-Client-Name': str(config.get('INNERTUBE_CONTEXT_CLIENT_NAME', 1)),
                                     'X-Youtube-Client-Version': str(context['client']['clientVersion'])})
                    except SearchSessionExpired:
                        if recovered:
                            raise
                        # Reopen only this query using the same saved login. Old
                        # delivered URLs are excluded while advancing the fresh
                        # cursor; a lost page never requests another login.
                        initialized, token, config, recovered = False, None, {}, True
                        attempts -= 1
                        continue
                    browser_no_new = bool(session and session[0] == 'native' and data.get('_rf_no_new'))
                    data = self._response_items(data)
                youtube_restriction(data)
                parsed = self._parse_results(data, site)
                buffer = [r for r in parsed if not (recovering or recovered) or r.url not in delivered]
                next_token = self._continuation(data)
                if token and token == next_token:
                    raise NetworkError('YouTube 返回了重复续页标记，请稍后重试')
                token = next_token
                if not parsed and token is None and not self._valid_empty(data):
                    # An unknown/changed response is not evidence of expired login.
                    # Explicit account/age notices are handled above; leave this
                    # page uncommitted so a later scroll can retry its same cursor.
                    raise NetworkError('YouTube 续页暂未返回可识别的资源，尚不能确认搜索结束；已保留当前结果，再次触底可重试')
                if browser_no_new:
                    break
            if ctx.cancelled():
                raise NetworkError('已停止')
            out, remaining = buffer[:ctx.max_results], buffer[ctx.max_results:]
            result = PageResults(out, exhausted=not remaining and not token,
                message=('浏览器本轮未加载到新视频，尚未确认结束；可稍后再次触底重试。' if browser_no_new else
                         '本段尚无新视频，仍有续页；继续触底可检索下一段。') if not out and token else '')
            state.update(buffer=remaining, token=token, config=config, initialized=True,
                         recovering=recovering or recovered)
            state['pages'][page] = result
            ctx.log('%s 第 %d 页 → %d 条%s' % (site.display, page, len(result), '，已到末页' if result.exhausted else ''))
            return result

    @staticmethod
    def _primary(data):
        if isinstance(data, dict):
            primary = (data.get('twoColumnSearchResultsRenderer') or {}).get('primaryContents')
            if primary is not None:
                return primary
        return data

    @staticmethod
    def _response_items(data):
        actions, present = [], False
        for name in ('onResponseReceivedCommands', 'onResponseReceivedActions', 'onResponseReceivedEndpoints'):
            if isinstance(data.get(name), list):
                present = True
                actions.extend(data[name])
        if present:
            return actions
        legacy = data.get('continuationContents')
        if isinstance(legacy, dict) and any(k in legacy for k in ('sectionListContinuation', 'itemSectionContinuation', 'gridContinuation', 'richGridContinuation')):
            return legacy
        raise NetworkError('YouTube 续页响应异常，保留当前页码，滚动可重试')

    @staticmethod
    def _config(html):
        config = {}
        for match in re.finditer(r'ytcfg\.set\s*\(\s*', html):
            try:
                data, _ = json.JSONDecoder().raw_decode(html[match.end():])
                if isinstance(data, dict):
                    config.update(data)
            except ValueError:
                pass
        return config

    def _continuation(self, node):
        # Breadth-first: a Shorts/shelf token must not replace the main list's.
        pending = deque([node])
        while pending:
            current = pending.popleft()
            if isinstance(current, dict):
                renderer = current.get('continuationItemRenderer')
                if isinstance(renderer, dict):
                    endpoints = deque([renderer.get('continuationEndpoint') or {}])
                    while endpoints:
                        endpoint = endpoints.popleft()
                        if not isinstance(endpoint, dict):
                            continue
                        cmd = endpoint.get('continuationCommand') or {}
                        if cmd.get('token'):
                            return cmd['token']
                        endpoints.extend((endpoint.get('commandExecutorCommand') or {}).get('commands') or [])
                for continuation in current.get('continuations') or []:
                    if not isinstance(continuation, dict):
                        continue
                    cmd = continuation.get('nextContinuationData') or continuation.get('reloadContinuationData') or {}
                    if cmd.get('continuation'):
                        return cmd['continuation']
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
        return None

    @staticmethod
    def _valid_empty(data):
        if isinstance(data, list):
            if not data:
                return True  # An explicitly empty continuation command list.
            for command in data:
                if not isinstance(command, dict):
                    continue
                for key in ('appendContinuationItemsAction', 'reloadContinuationItemsCommand'):
                    if (command.get(key) or {}).get('continuationItems') == []:
                        return True
        if isinstance(data, dict):
            for key in ('sectionListContinuation', 'itemSectionContinuation', 'gridContinuation', 'richGridContinuation'):
                container = data.get(key)
                if isinstance(container, dict) and (container.get('contents') == [] or container.get('items') == []) and not container.get('continuations'):
                    return True
        def notice(node):
            if isinstance(node, dict):
                message = node.get('messageRenderer')
                text = YouTubeAdapter._text(message.get('text')).strip(' \t\r\n.。!！') if isinstance(message, dict) else ''
                if text and re.fullmatch(
                        r'No (?:more )?results(?: found)?|'
                        r'没有(?:找到)?(?:任何|更多|其他)?(?:搜索)?结果|无更多结果|'
                        r'(?:未找到|找不到)(?:任何)?结果|'
                        r'沒有(?:找到)?(?:任何|更多|其他)?(?:搜尋)?結果|無更多結果|'
                        r'(?:未找到|找不到)(?:任何)?結果', text, re.I):
                    return True
                return any(notice(v) for k,v in node.items() if k not in ('videoRenderer','lockupViewModel','reelItemRenderer'))
            if isinstance(node, list):
                return any(notice(v) for v in node)
            return False
        return notice(data)

    def _parse_results(self, data, site):
        out: List[SearchResult] = []
        seen = set()
        for video in self._walk_videos(data):
            item = self._to_result(video, site)
            if item and item.url not in seen:
                seen.add(item.url)
                out.append(item)
        for lockup in self._walk_lockups(data):
            item = self._from_lockup(lockup, site)
            if item and item.url not in seen:
                seen.add(item.url)
                out.append(item)
        for short in self._walk_shorts(data):
            item = self._from_short(short, site)
            if item and item.url not in seen:
                seen.add(item.url)
                out.append(item)
        return out

    def _walk_shorts(self, node, depth=0):
        if depth > 35:
            return
        if isinstance(node, dict):
            for key in ('reelItemRenderer', 'shortsLockupViewModel'):
                if isinstance(node.get(key), dict):
                    yield node[key]
                    return
            for child in node.values():
                yield from self._walk_shorts(child, depth+1)
        elif isinstance(node, list):
            for child in node[:160]:
                yield from self._walk_shorts(child, depth+1)

    def _from_short(self, node, site):
        endpoint = ((node.get('onTap') or {}).get('innertubeCommand') or {}).get('reelWatchEndpoint') or {}
        vid = node.get('videoId') or endpoint.get('videoId')
        if not vid:
            return None
        metadata = node.get('overlayMetadata') or {}
        title = self._text(node.get('headline')) or self._text(metadata.get('primaryText'))
        if not title:
            return None
        thumbnail = node.get('thumbnail') or {}
        sources = thumbnail.get('thumbnails') or thumbnail.get('sources') or []
        cover = max(sources, key=lambda s: (s.get('width') or 0)*(s.get('height') or 0)).get('url', '') if sources else ''
        views = self._text(node.get('viewCountText')) or self._text(metadata.get('secondaryText'))
        return SearchResult(title=title, url='https://www.youtube.com/watch?v='+str(vid),
            source=site.display, source_id=site.id, cover=youtube_cover(str(vid), cover),
            duration=youtube_result_duration(node),
            views=parse_count(self._view_count(views)), play=self._view_count(views), extra={'video_id': str(vid)})

    # ------------------------------------------------------------------ #
    def _find_initial_data(self, html: str) -> Dict[str, Any]:
        if not html:
            return {}
        for marker in _MARKERS:
            idx = html.find(marker)
            if idx < 0:
                continue
            start = html.find("{", idx)
            if start < 0:
                continue
            try:
                obj, _ = json.JSONDecoder().raw_decode(html[start:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
        # 退化路径：正则截取（对超长页面更稳）
        m = re.search(r"ytInitialData\s*=\s*(\{.+?\})\s*;\s*</script>", html, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return {}
        return {}

    def _walk_videos(self, node: Any, depth: int = 0):
        """深度优先找出所有 videoRenderer。"""
        if depth > 30:
            return
        if isinstance(node, dict):
            for key in ('videoRenderer', 'gridVideoRenderer', 'compactVideoRenderer', 'videoWithContextRenderer'):
                if isinstance(node.get(key), dict):
                    yield node[key]
                    return
            for value in node.values():
                yield from self._walk_videos(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:80]:
                yield from self._walk_videos(item, depth + 1)

    def _walk_lockups(self, node: Any, depth: int = 0):
        """新版结果页用 lockupViewModel 代替 videoRenderer。"""
        if depth > 30:
            return
        if isinstance(node, dict):
            vm = node.get("lockupViewModel")
            if isinstance(vm, dict):
                yield vm
                return
            for value in node.values():
                yield from self._walk_lockups(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:80]:
                yield from self._walk_lockups(item, depth + 1)

    def _from_lockup(self, vm: Dict[str, Any], site: SiteConfig):
        vid = str(vm.get("contentId") or "")
        ctype = str(vm.get("contentType") or "").upper()
        if not vid or (ctype and "VIDEO" not in ctype):
            return None
        meta = ((vm.get("metadata") or {}).get("lockupMetadataViewModel") or {})
        title = self._text(meta.get("title"))
        if not title:
            return None

        thumb = ((vm.get("contentImage") or {}).get("thumbnailViewModel") or {})
        sources = ((thumb.get("image") or {}).get("sources") or [])
        cover = ""
        if sources:
            best = max(sources, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
            cover = best.get("url") or ""

        rows = ((((meta.get("metadata") or {}).get("contentMetadataViewModel") or {}))
                .get("metadataRows") or [])
        parts: List[str] = []
        for row in rows:
            for part in row.get("metadataParts") or []:
                text = self._text(part.get("text"))
                if text:
                    parts.append(text)
        author = parts[0] if parts else ""
        views = next((p for p in parts if re.search(r"view|观看|次播放", p, re.I)), "")
        published = next((p for p in parts if re.search(r"ago|前$|\d+\s*天前", p, re.I)), "")

        duration = youtube_result_duration(vm)

        return SearchResult(
            title=title,
            url="https://www.youtube.com/watch?v=%s" % vid,
            source=site.display,
            source_id=site.id,
            cover=youtube_cover(vid, cover),
            author=author,
            duration=duration,
            play=self._view_count(views),
            views=parse_count(self._view_count(views)),
            pub_ts=parse_pub_ts(published),
            extra={"video_id": vid, "published": published},
        )

    def _to_result(self, v: Dict[str, Any], site: SiteConfig):
        vid = v.get("videoId") or ""
        if not vid:
            return None
        title = self._text(v.get("title"))
        if not title:
            return None

        cover = ""
        thumbs = ((v.get("thumbnail") or {}).get("thumbnails") or [])
        if thumbs:
            best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            cover = best.get("url") or ""

        author = ""
        owner = v.get("ownerText") or v.get("longBylineText") or {}
        runs = owner.get("runs") or []
        if runs:
            author = runs[0].get("text") or ""
            nav = (runs[0].get("navigationEndpoint") or {}).get("browseEndpoint") or {}
            if not author:
                author = nav.get("canonicalBaseUrl") or ""

        length = youtube_result_duration(v)
        views = (v.get("viewCountText") or {}).get("simpleText") or ""
        published = (v.get("publishedTimeText") or {}).get("simpleText") or ""
        desc = self._text(v.get("descriptionSnippet")) or self._text(v.get("detailedMetadataSnippets"))
        if isinstance(desc, dict):
            desc = ""

        return SearchResult(
            title=title,
            url="https://www.youtube.com/watch?v=%s" % vid,
            source=site.display,
            source_id=site.id,
            cover=youtube_cover(vid, cover),
            author=author,
            duration=length,
            play=self._view_count(views),
            desc=(desc or "")[:300],
            views=parse_count(self._view_count(views)),
            pub_ts=parse_pub_ts(published),
            extra={"video_id": vid, "published": published},
        )

    @staticmethod
    def _text(node: Any) -> str:
        if not node:
            return ""
        if isinstance(node, str):
            return node.strip()
        if isinstance(node, dict):
            if "simpleText" in node:
                return str(node["simpleText"]).strip()
            if isinstance(node.get("content"), str):
                return node["content"].strip()
            runs = node.get("runs") or []
            if runs:
                return "".join(str(r.get("text") or "") for r in runs).strip()
        if isinstance(node, list):
            for item in node:
                t = YouTubeAdapter._text(item)
                if t:
                    return t
        return ""

    @staticmethod
    def _view_count(text: str) -> str:
        """'1,234,567 views' → '123.5万'。"""
        m = re.search(r"([\d,\.]+)\s*([KMB万亿]?)", text or "")
        if not m:
            return ""
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            return text
        unit = m.group(2)
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "万": 1e4, "亿": 1e8}.get(unit, 1)
        return Adapter.human_count(n * mult)
