"""Image and text search through public platform endpoints or scoped indexes."""
import json
import re
from urllib.parse import quote_plus, urlsplit, urljoin, parse_qs
from bs4 import BeautifulSoup
from .base import Adapter
from .bing import real_url
from .pagination import PageResults, stream_state
from .generic import GenericAdapter
from ..models import SearchResult, is_web_url, normalize_url, parse_pub_ts
from ..sources import matches
from ..net import NetworkError

TYPES = {'video': '视频', 'image': '图片', 'article': '文章', 'sound': '音效'}


class ResourceAdapter(Adapter):
    def __init__(self, resource_type):
        self.resource_type = 'article' if resource_type == 'novel' else resource_type

    def search(self, ctx, site):
        ctx.resource_type = self.resource_type
        state = stream_state(ctx, site)
        page = max(1, int(ctx.page or 1))
        with state['lock']:
            if page in state['pages']:
                return state['pages'][page]
            if page != len(state['pages']) + 1:
                raise NetworkError('续页会话已过期，请重新搜索')
            buffer = list(state['buffer'])
            seen = set(state.get('seen', ()))
            first = state.get('offset', 1)
            next_url = state.get('next_url', '')
            ended = state.get('ended', False)
            supported = state.get('supported', True)
            reason = state.get('reason', '')
            # Keep excess native results instead of throwing them away. Repeated
            # upstream pages are bounded, and never misreported as exhaustion.
            for _ in range(3):
                if buffer or ended or not supported or ctx.cancelled():
                    break
                native = self._native(ctx, site, first, next_url)
                next_url = native.next_url
                first = native.next_offset
                ended = native.exhausted is True
                supported = native.supported
                reason = native.message
                for row in native:
                    key = normalize_url(row.url)
                    if key not in seen:
                        seen.add(key)
                        buffer.append(row)
            if ctx.cancelled():
                raise NetworkError('已停止')
            size = max(1, min(50, ctx.max_results))
            out, remaining = buffer[:size], buffer[size:]
            message = ((reason or '当前来源没有更多公开检索结果') if ended and not remaining else
                       (reason or '本次未取得新增结果，可能重复或不符合类型；尚未确认末页，再次触底可重试') if not out else '')
            result = PageResults(out, exhausted=ended and not remaining, supported=supported, message=message)
            state.update(buffer=remaining, seen=seen, offset=first, next_url=next_url, ended=ended, supported=supported, reason=reason)
            state['pages'][page] = result
            return result

    def _native(self, ctx, site, first, next_url=''):
        kind = self.resource_type
        host = urlsplit(site.search_url).hostname or ''
        aggregate = site.builtin == 'bing' or matches(host, 'bing.com') or matches(host, 'baidu.com')
        domain = site.scope_domain or ('' if aggregate else host.removeprefix('www.').removeprefix('search.'))
        if matches(domain,'aigei.com'):
            if kind in ('sound','video','image'):
                from .sound import aigei
                return aigei(ctx,site,first,kind)
            from .native_resources import page_result
            return page_result([],first,message='爱给网当前支持视频、图片、音效检索，文章通道尚未接入',supported=False)
        if kind=='sound' and (matches(domain,'pixiv.net') or matches(domain,'bilibili.com') or matches(domain,'youtube.com') or site.builtin in ('youtube','bilibili')):
            from .native_resources import page_result
            return page_result([],first,message='此来源当前没有独立音效检索通道，请勾选爱给网或全网搜索来源',supported=False)
        if (domain == 'commons.wikimedia.org' and kind == 'image') or (domain == 'zh.wikisource.org' and kind == 'article'):
            from .native_resources import mediawiki
            return mediawiki(ctx, site, kind, first, next_url)
        if matches(domain, 'pixiv.net'):
            from .native_resources import pixiv
            return pixiv(ctx, site, kind, first)
        if matches(domain, 'bilibili.com') or site.builtin == 'bilibili':
            from .native_resources import bilibili
            return bilibili(ctx, site, kind, first)
        if matches(domain, 'youtube.com') or site.builtin == 'youtube':
            from .native_resources import page_result
            return page_result([], first, message='YouTube 当前仅支持视频检索，请选择视频模式或其他来源', supported=False)
        query = (('site:'+domain+' ') if domain else '') + ctx.query
        if kind=='sound':query+=' 音效 sound effect'
        size = 35 if kind == 'image' else 10
        if kind == 'image':
            url = 'https://www.bing.com/images/async?q=%s&first=%d&count=%d' % (quote_plus(query), first, size)
        else:
            url = 'https://www.bing.com/search?q=%s&first=%d&count=%d' % (quote_plus(query), first, size)
        document = ctx.http.get_text(next_url or url, site)
        soup = BeautifulSoup(document, 'html.parser')
        rows, seen = [], set()
        if kind == 'image':
            for node in soup.select('a.iusc[m]'):
                try:
                    meta = json.loads(node['m'])
                except (ValueError, TypeError):
                    continue
                if not isinstance(meta, dict):
                    continue
                image = meta.get('murl', '')
                page = meta.get('purl', '')
                page = page if is_web_url(page) else ''
                if not is_web_url(image) or (domain and not matches(urlsplit(page).hostname or '', domain)):
                    continue
                if image in seen:
                    continue
                seen.add(image)
                thumbnail = meta.get('turl') or GenericAdapter._img_src(node.select_one('img'))
                rows.append(SearchResult(title=str(meta.get('t') or node.get('aria-label') or ctx.query),
                    url=image, cover=thumbnail if is_web_url(thumbnail) else image,
                    source=site.display, source_id=site.id, resource_type='image',
                    extra={'page_url': page, 'discovery_sources': [site.id]}))
        else:
            for node in soup.select('li.b_algo'):
                link = node.select_one('h2 a[href]')
                if not link:
                    continue
                target = real_url(link['href'])
                if not is_web_url(target) or (domain and not matches(urlsplit(target).hostname or '', domain)):
                    continue
                if matches(urlsplit(target).hostname or '', 'bing.com') or re.search(r'/watch\?v=|/video/|/shorts/|youtu\.be/|\.(?:jpg|jpeg|png|gif|webp|mp4)(?:\?|$)', target, re.I):
                    continue
                if target in seen:
                    continue
                seen.add(target)
                description = node.select_one('.b_caption p') or node.select_one('p')
                title = link.get_text(' ', strip=True)
                if not title:
                    continue
                desc = description.get_text(' ', strip=True)[:300] if description else ''
                if kind=='sound' and not (re.search(r'\.(?:mp3|wav|ogg|flac|m4a)(?:\?|$)',target,re.I) or
                    (re.search(r'音效|sound.?effect|\.wav|\.mp3',title+' '+desc,re.I) and re.search(r'/item/|/sounds?/|/sound-effects/|/sfx/',target,re.I))):
                    continue
                image = node.select_one('img')
                thumbnail = GenericAdapter._img_src(image) if image else ''
                rows.append(SearchResult(title=title, url=target,
                    cover=urljoin(url, thumbnail) if thumbnail else '', resource_type=kind,
                    source=site.display, source_id=site.id,
                    desc=desc, pub_ts=parse_pub_ts(desc.split('·')[0]) if '·' in desc else 0))
        if ctx.cancelled():
            raise NetworkError('已停止')
        ended = bool(re.search(r'没有找到.*结果|没有与.*相关的结果|No results found|There are no results', soup.get_text(' ', strip=True), re.I))
        raw_count = len(soup.select('a.iusc[m]' if kind == 'image' else 'li.b_algo'))
        if not raw_count and not ended:
            raise NetworkError('未取得%s结果，也未确认末页；可能是站点限制或页面结构变化，可滚动重试' % TYPES[kind])
        result = PageResults(rows, exhausted=ended and not raw_count)
        if domain and raw_count and not rows:
            result.message = '索引未返回来自%s且符合当前类型的资源；可能未收录或忽略了站点限定，尚未确认末页' % site.display
        result.next_url = ''
        result.next_offset = first + max(size, raw_count)
        next_link = soup.select_one('a.sb_pagN[href]')
        candidate = urljoin(url, next_link['href']) if next_link else ''
        if candidate and matches(urlsplit(candidate).hostname or '', 'bing.com'):
            try:
                offset = int(parse_qs(urlsplit(candidate).query).get('first', ['0'])[0])
                if offset > first:
                    result.next_url, result.next_offset = candidate, offset
            except (ValueError, TypeError):
                pass
        return result
