"""Turn pasted web links into explicit, reviewable site-wide search configurations."""
import html
import ipaddress
import json
import re
import time
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import tldextract
from bs4 import BeautifulSoup
from .config import SiteConfig, builtin_sites
from .sources import matches

# Bundled PSL, including private suffixes: never fetch a list or merge tenants.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
ALIASES = {'bilibili.com': 'bilibili', 'b23.tv': 'bilibili', 'youtube.com': 'youtube',
           'youtu.be': 'youtube', 'youtube-nocookie.com': 'youtube', 'bing.com': 'bing', 'baidu.com': 'baidu'}


def pasted_url(value):
    value = html.unescape((value or '').strip()).replace('\\&', '&')
    match = re.search(r'https?://[^\s<>"\u3002\uff0c]+', value, re.I)
    if match:
        value = match.group().rstrip(').,;，。；）]》')
    elif not re.match(r'^[a-z][a-z0-9+.-]*:', value, re.I):
        value = 'https://' + value
    try:
        p = urlsplit(value)
        host = (p.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        if not host or any(not part or not re.fullmatch(r'[a-z0-9-]+', part) for part in host.split('.')):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        if not _extract(host).suffix or not _extract(host).domain:
            raise ValueError()
        return urlunsplit((p.scheme, host, p.path or '/', p.query, ''))
    except (ValueError, UnicodeError):
        raise ValueError('请粘贴一个有效的公开网站网址，例如网站首页或视频链接。') from None


def root_domain(url):
    return _extract(urlsplit(pasted_url(url)).hostname).top_domain_under_public_suffix


def infer_site(value):
    url = pasted_url(value)
    host = urlsplit(url).hostname
    # An explicit template is an advanced user choice, never silently rewrite it.
    if '{q}' in url:
        return SiteConfig(name=host, search_url=url), '已保留自定义搜索地址。'
    domain = root_domain(url)
    for alias, builtin in ALIASES.items():
        if matches(host, alias):
            site = next(s for s in builtin_sites() if s.builtin == builtin or s.id == builtin)
            canonical = {'bilibili': 'bilibili.com', 'youtube': 'youtube.com', 'bing': '', 'baidu': ''}[builtin]
            site.id = ''
            site.__post_init__()
            site.scope_domain = canonical
            return site, '已识别 %s，使用平台搜索入口，覆盖该平台的不同路径。' % site.display
    template = 'https://www.bing.com/videos/search?q=site%3A' + domain + '+{q}'
    return SiteConfig(name=domain, kind='domain', scope_domain=domain, search_url=template,
                      note='限定域名检索：包含该域名及子域名的全部路径；结果取决于搜索引擎收录'), \
        '搜索范围：%s 及其子域名的全部路径。未识别原生入口时使用必应限定域名检索。' % domain


def search_template(document, home, domain):
    """Use declared SearchAction or an unambiguous GET search form only."""
    soup = BeautifulSoup(document[:2 * 1024 * 1024], 'html.parser')
    def valid(candidate):
        try:
            p = urlsplit(candidate)
            return p.scheme in ('http', 'https') and not p.username and not p.password and p.port in (None, 80, 443) and matches(p.hostname or '', domain) and candidate.count('{q}') == 1
        except ValueError:
            return False
    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, dict):
            if node.get('@type') == 'SearchAction':
                target = node.get('target', '')
                if isinstance(target, dict):
                    target = target.get('urlTemplate', '')
                if isinstance(target, str):
                    candidate = urljoin(home, re.sub(r'\{(?:search_term_string|query|q)\}', '{q}', target))
                    if valid(candidate):
                        yield candidate
            for item in node.values():
                yield from walk(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item, depth + 1)
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            found = next(walk(json.loads(script.string or script.get_text())), None)
            if found:
                return found
        except (ValueError, TypeError):
            pass
    for form in soup.find_all('form'):
        if str(form.get('method', 'get')).lower() != 'get' or form.find('input', type='password'):
            continue
        field = next((i for i in form.find_all('input') if i.get('name') and
                      (i.get('type') == 'search' or str(i.get('name')).lower() in ('q', 's', 'query', 'keyword', 'search', 'search_query'))), None)
        if field is None:
            continue
        action = urljoin(home, form.get('action') or home)
        p = urlsplit(action)
        params = [(k, v) for k, v in parse_qsl(p.query) if k != field['name']]
        params += [(i.get('name'), i.get('value', '')) for i in form.find_all('input', type='hidden')
                   if i.get('name') and not re.search(r'token|csrf|auth|session', i.get('name'), re.I)]
        params.append((field['name'], '{q}'))
        candidate = urlunsplit((p.scheme, p.netloc, p.path, urlencode(params).replace('%7Bq%7D', '{q}'), ''))
        if valid(candidate):
            return candidate
    return ''


def discover_site(value, settings, cancel):
    """Bounded homepage GET; never submit forms or visit the pasted tracking URL."""
    site, message = infer_site(value)
    if site.kind != 'domain' or cancel.is_set():
        return site, message
    from .agent_access import public_url
    from .net import HttpClient
    http = HttpClient(replace(settings, timeout=8))
    http.cancel_event = cancel
    current = 'https://' + site.scope_domain + '/'
    deadline = time.monotonic() + 8
    try:
        for _ in range(3):
            if cancel.is_set() or time.monotonic() >= deadline:
                break
            public_url(current)
            response = http.get(current, timeout=max(.2, deadline - time.monotonic()), stream=True,
                                allow_redirects=False, allow_status=tuple(range(200, 400)))
            with response:
                if 300 <= response.status_code < 400:
                    target = urljoin(current, response.headers.get('Location', ''))
                    if not matches(urlsplit(target).hostname or '', site.scope_domain):
                        break
                    current = target
                    continue
                chunks, total = [], 0
                for chunk in response.iter_content(16384):
                    total += len(chunk)
                    if cancel.is_set() or time.monotonic() >= deadline or total > 2 * 1024 * 1024:
                        raise ValueError('首页读取超过限制')
                    chunks.append(chunk)
                document = b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace')
                template = search_template(document, current, site.scope_domain)
                if template:
                    site.kind, site.search_url = 'generic', template
                    site.note = '由网站公开搜索入口识别；仅接收该域名及子域名的结果'
                    return site, '已识别网站公开搜索入口；范围为 %s 及其子域名的全部路径。' % site.scope_domain
                break
    except Exception:
        pass
    finally:
        http.close()
    return site, message + '（未发现可用的公开搜索入口，或首页暂不可访问。）'


def same_site(a, b):
    if a.kind == b.kind == 'builtin' and a.builtin == b.builtin:
        return True
    if a.scope_domain:
        try:
            return a.scope_domain == (b.scope_domain or root_domain(b.search_url))
        except ValueError:
            pass
    return a.search_url == b.search_url
