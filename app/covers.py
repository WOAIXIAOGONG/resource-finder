"""Normalize supplied covers and recover metadata from known video providers."""
import html
import re
from urllib.parse import urlsplit, parse_qs


def cover_url(raw):
    if isinstance(raw, dict):
        raw = raw.get('url') or ''
    if not isinstance(raw, str):
        return ''
    url = html.unescape(raw).strip()
    if url.startswith('//'):
        url = 'https:' + url
    try:
        p = urlsplit(url)
    except ValueError:
        return ''
    return url if p.scheme in ('http', 'https') and p.hostname else ''


def video_identity(url):
    try:
        p = urlsplit(url)
    except ValueError:
        return '', ''
    host = (p.hostname or '').lower()
    if host == 'bilibili.com' or host.endswith('.bilibili.com'):
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]+|av[0-9]+)/*', p.path)
        if match:
            return 'bilibili', match[1]
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'):
        vid = p.path.strip('/') if host == 'youtu.be' else parse_qs(p.query).get('v', [''])[0]
        if not vid and p.path.startswith(('/shorts/', '/embed/')):
            vid = p.path.split('/')[2]
        if re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            return 'youtube', vid
    return '', ''


def youtube_cover(vid, supplied=''):
    """Known public thumbnail address when a YouTube page omits its image field."""
    explicit = cover_url(supplied)
    if explicit:
        return explicit
    if isinstance(vid, str) and re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        return 'https://i.ytimg.com/vi/' + vid + '/hqdefault.jpg'
    return ''


def resolve_cover(url, http):
    provider, vid = video_identity(url)
    if provider == 'youtube':
        return youtube_cover(vid)
    if provider == 'bilibili':
        data = http.get_json('https://api.bilibili.com/x/web-interface/view',
                             params={'bvid': vid} if vid.startswith('BV') else {'aid': vid[2:]},
                             headers={'Referer': 'https://www.bilibili.com/'}, timeout=6)
        if data.get('code') == 0:
            return cover_url((data.get('data') or {}).get('pic'))
    return ''
