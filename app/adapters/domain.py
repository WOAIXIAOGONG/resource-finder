"""Indexed video search restricted to a site's entire domain, with native paging."""
from dataclasses import replace
from urllib.parse import urlsplit
from .bing import BingAdapter
from .pagination import PageResults
from ..sources import matches


class DomainAdapter(BingAdapter):
    name = 'domain'

    def search(self, ctx, site):
        scoped = replace(ctx, query='site:' + site.scope_domain + ' ' + ctx.query)
        scoped.cancelled = ctx.cancelled
        rows = super().search(scoped, site)
        kept = [r for r in rows if matches(urlsplit(r.url).hostname or '', site.scope_domain)]
        message = rows.message
        if not kept:
            message = ('搜索引擎已无更多已收录结果；这不代表网站全部内容已搜尽。' if rows.exhausted
                       else '本页没有该网站的新结果；可继续触底检索。')
        return PageResults(kept, exhausted=rows.exhausted, supported=rows.supported, message=message)
