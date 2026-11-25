"""Public non-video searches. Keep provider pagination and real provenance."""
from urllib.parse import quote, urlencode, urlsplit
from .base import Adapter
from .pagination import PageResults
from ..models import SearchResult, parse_pub_ts, is_web_url
from ..covers import cover_url
from ..net import NetworkError


def page_result(rows, page, last=None, message='', supported=True):
    ended = page >= last if isinstance(last, int) and last >= 0 else None
    result = PageResults(rows, exhausted=ended, supported=supported, message=message)
    result.next_offset, result.next_url = page + 1, ''
    return result


def pixiv(ctx, site, kind, page):
    category, key = ('artworks', 'illustManga') if kind == 'image' else ('novels', 'novel')
    params = urlencode({'word': ctx.query, 'order': 'date_d', 'mode': 'safe', 'p': page,
                        's_mode': 's_tag', 'type': 'all', 'lang': 'zh'})
    url = 'https://www.pixiv.net/ajax/search/' + category + '/' + quote(ctx.query, safe='') + '?' + params
    data = ctx.http.get_json(url, site, headers={'Referer': 'https://www.pixiv.net/'})
    if not isinstance(data, dict) or data.get('error'):
        raise NetworkError('Pixiv 搜索未成功：' + str(data.get('message', '接口格式异常') if isinstance(data, dict) else '接口格式异常'))
    section = (data.get('body') or {}).get(key)
    if not isinstance(section, dict) or not isinstance(section.get('data'), list):
        raise NetworkError('Pixiv 未返回作品列表，不能判断已搜完')
    rows = []
    for item in section['data']:
        if ctx.cancelled():
            raise NetworkError('已停止')
        if not isinstance(item, dict) or item.get('xRestrict', 0) != 0 or item.get('isMasked') or item.get('isUnlisted'):
            continue
        identity = str(item.get('id') or '')
        if not identity.isdigit() or not item.get('title'):
            continue
        target = ('https://www.pixiv.net/artworks/' + identity if kind == 'image' else
                  'https://www.pixiv.net/novel/show.php?id=' + identity)
        rows.append(SearchResult(title=Adapter.clean_text(item['title']), url=target,
            cover=cover_url(item.get('url')), source=site.display, source_id=site.id, resource_type=kind,
            author=str(item.get('userName') or ''), desc=Adapter.clean_text(item.get('description') or '')[:300],
            pub_ts=parse_pub_ts(item.get('createDate')),
            extra={'page_url': target, 'image_page': kind == 'image', 'discovery_sources': [site.id]}))
    last = section.get('lastPage')
    if section.get('total') == 0:
        last = 0
    return page_result(rows, page, last, 'Pixiv 当前公开检索范围已结束' if isinstance(last, int) and page >= last else '')


def bilibili(ctx, site, kind, page):
    from .bilibili import BilibiliAdapter, API
    BilibiliAdapter()._warm(ctx, site)
    data = ctx.http.get_json(API, site, params={'search_type': 'article', 'keyword': ctx.query, 'page': str(page)},
                             headers={'Referer': 'https://search.bilibili.com/'})
    if not isinstance(data, dict) or data.get('code') != 0:
        raise NetworkError('B站专栏接口暂不可用：' + str(data.get('message', '响应异常') if isinstance(data, dict) else '响应异常'))
    body = data.get('data') or {}
    if not isinstance(body.get('result'), list):
        if body.get('numResults') == 0:
            return page_result([], page, 0)
        raise NetworkError('B站未返回专栏列表，不能判断已搜完')
    rows = []
    for item in body['result']:
        if ctx.cancelled():
            raise NetworkError('已停止')
        if not isinstance(item, dict) or item.get('type') not in (None, 'article'):
            continue
        identity = str(item.get('id') or '')
        title = Adapter.clean_text(item.get('title') or '')
        if not identity.isdigit() or not title:
            continue
        target = 'https://www.bilibili.com/read/cv' + identity
        covers = item.get('image_urls') or []
        if not isinstance(covers, list):
            covers = []
        covers = [u for u in (cover_url(c) for c in covers) if is_web_url(u)]
        links = covers if kind == 'image' else [target]
        for link in links:
            rows.append(SearchResult(title=title, url=link, cover=link if kind == 'image' else (covers[0] if covers else ''),
                source=site.display, source_id=site.id, resource_type=kind, author=str(item.get('author') or ''),
                desc=('专栏配图。' if kind == 'image' else '') + Adapter.clean_text(item.get('desc') or '')[:280],
                pub_ts=parse_pub_ts(item.get('pubdate') or item.get('pub_time')),
                extra={'page_url': target, 'discovery_sources': [site.id]}))
    return page_result(rows, page, body.get('numPages'))


def mediawiki(ctx, site, kind, page, next_url=''):
    """Use documented Wikimedia search/continuation, never infer empty from errors."""
    host = 'commons.wikimedia.org' if kind == 'image' else 'zh.wikisource.org'
    endpoint = 'https://' + host + '/w/api.php'
    params = {'action': 'query', 'format': 'json', 'formatversion': 2, 'generator': 'search',
              'gsrsearch': ctx.query, 'gsrnamespace': 6 if kind == 'image' else 0, 'gsrlimit': 24}
    if kind == 'image':
        params.update(prop='imageinfo', iiprop='url|mime', iiurlwidth=500)
    else:
        params.update(prop='info|pageimages', inprop='url', piprop='thumbnail', pithumbsize=400)
    if next_url:
        parsed = urlsplit(next_url)
        if parsed.scheme != 'https' or parsed.hostname != host or parsed.path != '/w/api.php':
            raise NetworkError('站内搜索续页地址异常，请重新搜索')
    data = ctx.http.get_json(next_url or endpoint + '?' + urlencode(params), site)
    if not isinstance(data, dict) or data.get('error'):
        error = data.get('error', {}) if isinstance(data, dict) else {}
        raise NetworkError(site.display + ' 站内搜索失败：' + str(error.get('info', '响应格式异常') if isinstance(error, dict) else error))
    query = data.get('query', {})
    pages = query.get('pages', []) if isinstance(query, dict) else None
    if not isinstance(pages, list) or (not pages and data.get('batchcomplete') is not True):
        raise NetworkError(site.display + ' 未返回有效检索响应，不能判断已搜完')
    continuation = data.get('continue')
    if continuation is not None and (not isinstance(continuation, dict) or not continuation or
            any(k not in ('continue', 'gsroffset', 'iicontinue', 'picontinue') for k in continuation)):
        raise NetworkError(site.display + ' 续页标记异常，不能判断已搜完')
    rows = []
    for item in sorted((p for p in pages if isinstance(p, dict)), key=lambda p: p.get('index', 0)):
        if ctx.cancelled():
            raise NetworkError('已停止')
        title = Adapter.clean_text(item.get('title', ''))
        cover, target, origin = '', '', ''
        if kind == 'image':
            infos = item.get('imageinfo') or []
            info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
            if not str(info.get('mime', '')).startswith('image/'):
                continue
            target, origin = info.get('url', ''), info.get('descriptionurl', '')
            cover = info.get('thumburl') or target
            title = title.removeprefix('File:')
        else:
            target = origin = item.get('fullurl', '')
            thumbnail = item.get('thumbnail') or {}
            cover = thumbnail.get('source', '') if isinstance(thumbnail, dict) else ''
        if not title or not is_web_url(target) or not is_web_url(origin):
            continue
        rows.append(SearchResult(title=title, url=target, cover=cover if is_web_url(cover) else '',
            resource_type=kind, source=site.display, source_id=site.id,
            extra={'page_url': origin, 'discovery_sources': [site.id]}))
    result = PageResults(rows, exhausted=continuation is None,
                         message='站内搜索当前匹配结果已结束' if continuation is None else '')
    result.next_offset = page + 1
    result.next_url = endpoint + '?' + urlencode({**params, **continuation}) if continuation else ''
    return result
