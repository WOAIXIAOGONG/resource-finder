# -*- coding: utf-8 -*-
"""适配器基类与公共工具。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from ..config import SiteConfig
from ..models import SearchResult
from ..net import HttpClient


@dataclass
class SearchContext:
    """一次搜索里传给适配器的上下文。"""

    query: str
    http: HttpClient
    max_results: int = 24
    cancel: Optional[threading.Event] = None
    log: Callable[[str], None] = lambda msg: None
    page: int = 1
    reset: bool = False
    resource_type: str = 'video'

    def cancelled(self) -> bool:
        return bool(self.cancel and self.cancel.is_set())


class Adapter:
    """所有站点的抓取器都实现 search()。"""

    name = "base"

    def search(self, ctx: SearchContext, site: SiteConfig) -> List[SearchResult]:
        raise NotImplementedError

    # -- 小工具 ----------------------------------------------------------- #
    @staticmethod
    def clean_text(text: str) -> str:
        import re

        if not text:
            return ""
        t = str(text)
        t = re.sub(r"<[^>]+>", "", t)                 # 去掉 <em class="keyword">
        t = t.replace("\u200b", "").replace("\ufeff", "")
        t = re.sub(r"[\r\n\t]+", " ", t)
        t = re.sub(r"[\u3000]+", " ", t)
        t = re.sub(r" {2,}", " ", t)
        return t.strip()

    @staticmethod
    def human_count(value) -> str:
        """把 1234567 变成 123.5万 / 1.2亿。"""
        try:
            n = float(value)
        except (TypeError, ValueError):
            return ""
        if n <= 0:
            return ""
        if n >= 1e8:
            return "%.1f亿" % (n / 1e8)
        if n >= 1e4:
            return "%.1f万" % (n / 1e4)
        return "%d" % int(n)

    @staticmethod
    def human_duration(seconds) -> str:
        try:
            s = int(float(seconds))
        except (TypeError, ValueError):
            return ""
        if s <= 0:
            return ""
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return "%d:%02d:%02d" % (h, m, sec)
        return "%d:%02d" % (m, sec)
