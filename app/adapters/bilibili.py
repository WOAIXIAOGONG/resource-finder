# -*- coding: utf-8 -*-
"""哔哩哔哩：走官方网页搜索接口。

接口：https://api.bilibili.com/x/web-interface/search/type?search_type=video
      &keyword=...&page=1&page_size=30
不带 cookie 直接请求会返回 -412（被风控），所以先访问一次首页拿 buvid3。
cookie 由同一个线程的 session 持有，所以「预热」和「查询」必须在同一个
线程里连续执行 —— 引擎保证一个站点的抓取在同一个 worker 线程里完成。
"""
from __future__ import annotations

import time
from typing import List, Optional
from urllib.parse import quote

from ..config import SiteConfig
from ..models import SearchResult, parse_count, parse_pub_ts
from ..net import NetworkError
from .base import Adapter, SearchContext
from .pagination import PageResults

HOME = "https://www.bilibili.com/"
API = "https://api.bilibili.com/x/web-interface/search/type"

# 每个 HttpClient 工作线程保存预热标记，随连接复用。
_WARM_TTL = 20 * 60


class BilibiliAdapter(Adapter):
    name = "bilibili"

    def search(self, ctx: SearchContext, site: SiteConfig) -> List[SearchResult]:
        query = ctx.query.strip()
        if not query:
            return []

        data = self._call(ctx, site, query)
        if data is None:                       # 被风控，换 HTML 兜底
            return self._fallback(ctx, site, query)

        rows = (data or {}).get("result") or []
        out: List[SearchResult] = []
        for row in rows:
            if ctx.cancelled():
                break
            if not isinstance(row, dict):
                continue
            url = row.get("arcurl") or ""
            bvid = row.get("bvid") or ""
            if not bvid:
                # 课程卡/广告卡没有 bvid（arcurl 会指向 cheese/play 付费课程），
                # 用户想要的是视频，这类结果直接丢掉。
                continue
            # 一律用 BV 号拼地址：arcurl 常带 search_id / search_query 之类的跟踪参数，
            # 而且旧视频可能是会跳转的 av 号地址。
            url = "https://www.bilibili.com/video/%s" % bvid
            url = url.replace("http://", "https://")
            if not url:
                continue
            title = self.clean_text(row.get("title") or "")
            if not title:
                continue
            pic = (row.get("pic") or "").strip()
            if pic.startswith("//"):
                pic = "https:" + pic
            duration = self._fix_duration(row.get("duration"))
            out.append(SearchResult(
                title=title,
                url=url,
                source=site.display,
                source_id=site.id,
                cover=pic,
                author=row.get("author") or "",
                duration=str(duration),
                play=self.human_count(row.get("play")),
                desc=self.clean_text(row.get("description") or "")[:300],
                views=parse_count(row.get("play")),
                pub_ts=parse_pub_ts(row.get("pubdate")),
                extra={"bvid": bvid, "aid": row.get("aid")},
            ))
            if len(out) >= ctx.max_results:
                break
        ctx.log("%s → %d 条" % (site.display, len(out)))
        pages = data.get('numPages')
        ended = max(1, int(ctx.page or 1)) >= int(pages) if pages is not None else None
        return PageResults(out, exhausted=ended)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fix_duration(raw) -> str:
        """B站接口的 duration 是「分钟:秒」（1056:39 = 17 小时 36 分 39 秒），
        还可能出现 20:7 这种秒不补零的写法，这里统一成 H:MM:SS / M:SS。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        parts = text.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return text
        if len(nums) == 2:
            total = nums[0] * 60 + nums[1]
        elif len(nums) == 3:
            total = nums[0] * 3600 + nums[1] * 60 + nums[2]
        else:
            return text
        if total <= 0:
            return ""
        if total >= 3600:
            return "%d:%02d:%02d" % (total // 3600, total % 3600 // 60, total % 60)
        return "%d:%02d" % (total // 60, total % 60)

    def _call(self, ctx: SearchContext, site: SiteConfig, query: str) -> Optional[dict]:
        params = {
            "search_type": "video",
            "keyword": query,
            "page": str(max(1, int(ctx.page or 1))),
            "page_size": str(max(10, min(50, ctx.max_results + 6))),
            "order": "totalrank",
        }
        headers = {
            # 注意：Referer 只能是 ASCII，关键词必须转义，否则 requests 会报 latin-1 错误
            "Referer": "https://search.bilibili.com/all?keyword=" + quote(query),
            "Origin": "https://search.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        try:
            self._warm(ctx, site)
            data = ctx.http.get_json(API, site, params=params, headers=headers)
        except NetworkError:
            return None
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        code = data.get("code")
        if code in (-412, -352, -401):
            # 风控：丢掉预热标记，下次重新拿 cookie
            ctx.http._local.bilibili_warm_at = 0.0
            ctx.log("%s 触发风控(code=%s)，改用网页兜底" % (site.display, code))
            return None
        if code != 0:
            ctx.log("%s 接口返回 code=%s %s" % (site.display, code, data.get("message", "")))
            return None
        return data.get("data") or {}

    def _warm(self, ctx: SearchContext, site: SiteConfig) -> None:
        # The warm-up belongs to this HTTP client/thread, not unrelated clients.
        state = ctx.http._local
        if time.time() - getattr(state, 'bilibili_warm_at', 0) < _WARM_TTL:
            return
        try:
            ctx.http.get(HOME, site, timeout=8.0)
            state.bilibili_warm_at = time.time()
        except Exception:
            pass

    def _fallback(self, ctx: SearchContext, site: SiteConfig, query: str) -> List[SearchResult]:
        """接口被风控时退回通用 HTML 抽取（页面是 JS 渲染的，通常抓不到，
        但至少不会让整个搜索报错）。翻页时不兜底，避免把第一页重复刷一遍。"""
        if int(ctx.page or 1) > 1:
            raise NetworkError("B站接口暂时不可用，请稍后重试此页")
        from .generic import GenericAdapter

        fake = SiteConfig(
            id=site.id, name=site.name, kind="generic",
            search_url="https://search.bilibili.com/all?keyword={q}",
            referer="https://www.bilibili.com/", max_results=site.max_results,
        )
        try:
            rows = GenericAdapter().search(ctx, fake)
            if not rows:
                raise NetworkError("B站接口和网页均未返回可用结果，可能触发风控")
            return rows
        except Exception as exc:               # noqa: BLE001
            ctx.log("%s 兜底失败：%s" % (site.display, exc))
            raise
