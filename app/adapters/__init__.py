# -*- coding: utf-8 -*-
"""适配器注册表：按站点的 builtin 字段挑选抓取器。"""
from __future__ import annotations

from typing import Dict, Type

from ..config import SiteConfig
from .base import Adapter, SearchContext
from .generic import GenericAdapter
from .bilibili import BilibiliAdapter
from .youtube import YouTubeAdapter
from .bing import BingAdapter
from .baidu import BaiduAdapter

_BUILTINS: Dict[str, Type[Adapter]] = {
    "bilibili": BilibiliAdapter,
    "youtube": YouTubeAdapter,
    "bing": BingAdapter,
    "baidu": BaiduAdapter,
}


def adapter_for(site: SiteConfig, resource_type='video') -> Adapter:
    """内置站点用自己的适配器，其余一律走通用抽取。"""
    if resource_type in ('image', 'article', 'novel', 'sound') or site.scope_domain=='aigei.com':
        from .resources import ResourceAdapter
        return ResourceAdapter(resource_type)
    if site.kind == 'domain' and site.scope_domain:
        from .domain import DomainAdapter
        return DomainAdapter()
    if site.kind == "builtin" and site.builtin in _BUILTINS:
        return _BUILTINS[site.builtin]()
    # Upgrade existing default configurations without overwriting user settings.
    if site.search_url == 'https://v.baidu.com/v?word={q}' and not any(site.css.values()):
        return BaiduAdapter()
    return GenericAdapter()


__all__ = ["Adapter", "SearchContext", "GenericAdapter", "adapter_for",
           "BilibiliAdapter", "YouTubeAdapter", "BingAdapter"]
