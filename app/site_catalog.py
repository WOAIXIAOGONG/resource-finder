"""Optional public-site presets, merged once without replacing user choices."""
from urllib.parse import urlsplit

CATALOG_REVISION = 2
# Stable IDs survive renamed sites and future catalog updates.
ENTRIES = (
    ('pixiv', 'Pixiv', 'pixiv.net', '图片、文章（含小说）'),
    ('unsplash', 'Unsplash', 'unsplash.com', '图片'),
    ('pexels', 'Pexels', 'pexels.com', '图片、视频'),
    ('pixabay', 'Pixabay', 'pixabay.com', '图片、视频'),
    ('commons', 'Wikimedia Commons', 'commons.wikimedia.org', '图片'),
    ('zhihu', '知乎', 'zhihu.com', '文章'),
    ('jianshu', '简书', 'jianshu.com', '文章'),
    ('qidian', '起点中文网', 'qidian.com', '文章（小说）'),
    ('zongheng', '纵横中文网', 'zongheng.com', '文章（小说）'),
    ('jinjiang', '晋江文学城', 'jjwxc.net', '文章（小说）'),
    ('gutenberg', 'Project Gutenberg', 'gutenberg.org', '文章（图书、小说）'),
    ('wikisource', '维基文库', 'zh.wikisource.org', '文章（文学、典籍）'),
    ('aigei', '爱给网', 'aigei.com', '视频、图片、音效'),
)


def catalog_sites():
    from .config import SiteConfig
    return [SiteConfig(id='preset-'+key, name=name, kind='domain', enabled=False,
                       scope_domain=domain,
                       search_url='https://www.aigei.com/s?q={q}&type=sound' if key=='aigei' else 'https://www.bing.com/search?q=site%3A'+domain+'+{q}',
                       note='适合：'+types+'。'+('已接入公开站内搜索；' if key in ('pixiv','commons','wikisource','aigei') else '公开索引检索（非专用站内搜索）；')+'勾选后参与搜索，访问与收录情况因站点而异。')
            for key, name, domain, types in ENTRIES]


def merge_catalog(sites, since_revision=0):
    """Keep custom definitions, enabled flags and domains; add missing presets only."""
    def domain(site):
        try:
            return (site.scope_domain or urlsplit(site.search_url).hostname or '').lower().removeprefix('www.')
        except ValueError:
            return ''
    existing = {domain(site) for site in sites} - {''}
    ids = {site.id for site in sites}
    added = 0
    for preset in catalog_sites():
        introduced=2 if preset.id=='preset-aigei' else 1
        if introduced<=since_revision:continue
        target = preset.scope_domain
        if preset.id in ids or any(target == d or target.endswith('.'+d) or d.endswith('.'+target) for d in existing):
            continue
        sites.append(preset)
        existing.add(target)
        ids.add(preset.id)
        added += 1
    return added
