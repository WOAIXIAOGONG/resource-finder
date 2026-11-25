"""Stable display groups for configured sources and Agent results."""
from urllib.parse import urlsplit


PLATFORMS = {
    'bilibili': ('B站', ('bilibili.com', 'b23.tv'), ('b站', '哔哩哔哩', 'bilibili', 'bili')),
    'youtube': ('YouTube', ('youtube.com', 'youtu.be', 'youtube-nocookie.com'), ('youtube', '油管', 'ytb')),
    'bing': ('必应', ('bing.com',), ('bing', '必应', '必应网页')),
    'baidu': ('百度视频', ('baidu.com',), ('baidu', '百度', '百度视频')),
    'youku': ('优酷', ('youku.com',), ('youku', '优酷')),
    'iqiyi': ('爱奇艺', ('iqiyi.com',), ('iqiyi', '爱奇艺')),
    'douyin': ('抖音', ('douyin.com', 'iesdouyin.com'), ('douyin', '抖音')),
    'vimeo': ('Vimeo', ('vimeo.com',), ('vimeo',)),
}


def host_of(url):
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''


def matches(host, domain):
    return bool(host and domain and (host == domain or host.endswith('.' + domain)))


def platform(host='', name=''):
    for key, (label, domains, aliases) in PLATFORMS.items():
        if any(matches(host, d) for d in domains) or (name and name.strip().casefold() in aliases):
            return 'platform:' + key, label
    return None


def site_group(site):
    if site.scope_domain:
        return platform(host=site.scope_domain) or ('site:' + site.id, site.display)
    return (platform(name=site.builtin) or platform(host=host_of(site.search_url))
            or ('site:' + site.id, site.display))


def accepts_url(site, url):
    host = host_of(url)
    if not site.scope_domain:
        return True
    return matches(host, site.scope_domain) or bool(
        site.kind == 'builtin' and platform(host=host) and platform(host=host) == site_group(site))


def result_group(result, sites):
    host = host_of((result.extra or {}).get('page_url') or result.url) if result.resource_type == 'image' else host_of(result.url)
    known = platform(host=host)
    if known:
        return known
    for site in sites:
        if matches(host, site.scope_domain or host_of(site.search_url)):
            return site_group(site)
    for site in sites:
        if result.source_id == site.id and not site.scope_domain:
            return site_group(site)
    known = platform(name=result.source) or platform(name=result.source_id)
    if known:
        return known
    name = (result.source or '').strip()
    for site in sites:
        if name and name.casefold() == site.display.casefold() and not site.scope_domain:
            return site_group(site)
    if name and name.casefold() not in ('agent', 'unknown', '未知'):
        return 'name:' + name.casefold(), name
    return ('host:' + host, host) if host else ('unknown', '其他来源')


def result_groups(result, sites):
    """Hosting platform plus the configured aggregation channel that found it."""
    groups = dict([result_group(result, sites)])
    ids = set((result.extra or {}).get('discovery_sources', [])) | {result.source_id}
    for site in sites:
        key, label = site_group(site)
        if site.id in ids and key in ('platform:bing', 'platform:baidu'):
            groups[key] = label
    # Newly normalized whole-site boundaries are checked against the URL itself.
    # Agent-provided names or source IDs cannot admit an unrelated domain.
    for key in list(groups):
        configured = [s for s in sites if site_group(s)[0] == key]
        scope_url = (result.extra or {}).get('page_url') or result.url if result.resource_type == 'image' else result.url
        if configured and all(s.scope_domain for s in configured) and not any(accepts_url(s, scope_url) for s in configured):
            groups.pop(key)
    return groups
