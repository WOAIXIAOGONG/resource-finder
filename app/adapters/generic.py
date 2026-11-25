# -*- coding: utf-8 -*-
"""通用 HTML 抽取器。

用户在白名单里自己加的任何站点，只要给一个搜索地址模板（含 {q}），
就用这里的启发式规则从结果页里抠出「标题 + 链接 + 封面图」：

1. JSON-LD（ItemList / VideoObject / Article）—— 结构化数据最好
2. <a> 里包着 <img> 的卡片
3. <a> 与 <img> 处在同一个父容器里的卡片（很多站点标题和图分开放）
4. 兜底：只有文字链接的结果（无封面，界面显示占位图）
"""
from __future__ import annotations

import importlib.util
import json
import re
from typing import Dict, List, Tuple
from urllib.parse import urljoin, urlsplit

from ..config import SiteConfig
from ..models import SearchResult, normalize_url, parse_pub_ts, is_web_url
from .base import Adapter, SearchContext
from .pagination import PageResults

try:                                     # bs4 是必需依赖，lxml 只是加速
    from bs4 import BeautifulSoup
except Exception:                        # pragma: no cover
    BeautifulSoup = None  # type: ignore

try:
    _HAS_LXML = importlib.util.find_spec("lxml") is not None
except Exception:                        # pragma: no cover
    _HAS_LXML = False
_PARSER = "lxml" if _HAS_LXML else "html.parser"

_BAD_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "about:", "#")
_BAD_EXT = (".css", ".js", ".ico", ".woff", ".woff2", ".ttf", ".map")
_BAD_URL_HINTS = (
    "/login", "/signin", "/signup", "/register", "/about", "/privacy",
    "/terms", "/contact", "/help", "/faq", "/feedback", "/download/",
    "/setting", "/account", "/user/", "/space/", "/search?", "/tag/",
    "/category/", "/categories/", "/archive", "/rss", "/sitemap",
)
_IMG_NOISE = (
    "logo", "icon", "avatar", "sprite", "blank", "spacer", "placeholder",
    "loading", "default", "1x1", "pixel", "qrcode", "qr_code",
)
_GOOD_URL_HINTS = ("video", "watch", "play", "detail", "view", "post", "article",
                   "item", "topic", "thread", "movie", "episode", "gallery")
_LAZY_ATTRS = (
    "data-src", "src", "data-original", "data-lazy-src", "data-lazy",
    "data-echo", "data-url", "data-image", "data-thumb", "data-thumbnail",
    "data-cover", "data-poster", "file", "original",
)

_TIME_RE = re.compile(r"^\s*\d{1,3}:\d{2}(:\d{2})?\s*$")
_COUNT_RE = re.compile(r"^\s*[\d.,]+\s*[万亿wkWK]?\s*(次|人|条)?\s*(播放|观看|浏览|点赞|评论)?\s*$")
_ONLY_SYMBOL_RE = re.compile(r"^[\s\d:：.,·\-_/|+()（）\[\]【】<>]+$")
_NAV_WORDS = {
    "首页", "登录", "注册", "更多", "下载", "客户端", "关于我们", "意见反馈",
    "播放", "全屏", "上一页", "下一页", "展开", "收起", "搜索", "筛选",
    "排序", "vip", "会员", "免费", "热播", "推荐", "全部", "电影", "电视剧",
    "综艺", "动漫", "纪录片", "直播", "音乐", "游戏", "资讯", "收藏", "分享",
    "评论", "登录/注册", "立即播放", "查看全部", "加载更多", "换一换",
}
_TITLE_CLASS_RE = re.compile(
    r"(^|[-_ ])(title|tit|name|subject|caption|heading|headline)([-_ ]|$)",
    re.I)


class GenericAdapter(Adapter):
    name = "generic"

    def search(self, ctx: SearchContext, site: SiteConfig) -> List[SearchResult]:
        if BeautifulSoup is None:
            raise RuntimeError("缺少 beautifulsoup4 依赖")
        page = max(1, int(ctx.page or 1))
        url = site.search_url_for(ctx.query)
        if page > 1:
            # 只有地址里写了 {page} 的站点才能翻页；没写的直接认输，别重复抓第一页
            if "{page}" not in (site.search_url or "") and "{p}" not in (site.search_url or ""):
                return PageResults(supported=False, message='该自定义站点尚未配置分页地址，不能据此判断资源已搜完')
        url = url.replace("{page}", str(page)).replace("{p}", str(page))
        html = ctx.http.get_text(url, site)
        results = self.extract(html, url, site, ctx.max_results, ctx.query)
        ctx.log("%s → %d 条" % (site.display, len(results)))
        return results

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def extract(self, html: str, base_url: str, site: SiteConfig,
                limit: int = 24, query: str = "") -> List[SearchResult]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, _PARSER)
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            # script 里可能有 JSON-LD，稍后单独处理，这里先留着 ld+json
            if tag.name == "script" and (tag.get("type") or "").lower() == "application/ld+json":
                continue
            tag.decompose()

        candidates: List[Dict] = []

        # 1) 用户自定义 CSS 选择器优先
        if site.css.get("item"):
            candidates.extend(self._from_css(soup, base_url, site))

        # 2) JSON-LD
        if not candidates:
            candidates.extend(self._from_jsonld(soup, base_url))

        # 3) DOM 启发式
        if not candidates:
            candidates.extend(self._from_dom(soup, base_url))

        ordered = self._rank(candidates, base_url, query)
        if site.scope_domain:
            from ..sources import accepts_url
            ordered = [row for row in ordered if accepts_url(site, row.get('url', ''))]
        return self._to_results(ordered, site, limit)

    # ------------------------------------------------------------------ #
    # 三种抽取
    # ------------------------------------------------------------------ #
    def _from_css(self, soup, base_url: str, site: SiteConfig) -> List[Dict]:
        out: List[Dict] = []
        css = site.css
        try:
            nodes = soup.select(css["item"])[:200]
        except Exception:
            return []
        for node in nodes:
            link_el = title_el = img_el = None
            try:
                link_el = node.select_one(css["link"]) if css.get("link") else node.find("a")
            except Exception:
                link_el = node.find("a")
            try:
                title_el = node.select_one(css["title"]) if css.get("title") else None
            except Exception:
                title_el = None
            try:
                img_el = node.select_one(css["image"]) if css.get("image") else node.find("img")
            except Exception:
                img_el = node.find("img")

            href = ""
            if link_el is not None:
                href = urljoin(base_url, (link_el.get("href") or "").strip())
            if not href:
                continue
            title = ""
            if title_el is not None:
                title = title_el.get("title") or title_el.get_text(" ", strip=True)
            if not title and img_el is not None:
                title = img_el.get("alt") or img_el.get("title") or ""
            if not title and link_el is not None:
                title = link_el.get("title") or link_el.get_text(" ", strip=True)
            out.append({
                "url": href,
                "title": title,
                "image": self._img_src(img_el) if img_el is not None else "",
                "source": "css",
            })
        return out

    def _from_jsonld(self, soup, base_url: str) -> List[Dict]:
        out: List[Dict] = []

        def walk(node, depth=0):
            if depth > 8 or len(out) > 200:
                return
            if isinstance(node, dict):
                types = node.get("@type") or ""
                tlist = types if isinstance(types, list) else [types]
                tlist = [str(t).lower() for t in tlist]
                if any(t in ("itemlist", "collectionpage", "searchresultspage") for t in tlist):
                    for it in node.get("itemListElement") or []:
                        walk(it, depth + 1)
                if any(t in ("videoobject", "article", "newsarticle", "blogposting",
                             "product", "creativework", "movie", "tvseries", "episode")
                       for t in tlist):
                    url = self._first_str(node.get("url") or node.get("contentUrl")
                                          or node.get("embedUrl") or node.get("@id"))
                    title = self._first_str(node.get("name") or node.get("headline"))
                    img = ""
                    for key in ("thumbnailUrl", "image", "thumbnail", "screenshot"):
                        img = self._first_str(node.get(key))
                        if img:
                            break
                    if url and title:
                        out.append({
                            "url": urljoin(base_url, url),
                            "title": title,
                            "image": urljoin(base_url, img) if img else "",
                            "source": "jsonld",
                            "desc": self._first_str(node.get("description")),
                        })
                    return
                if "item" in node:
                    walk(node["item"], depth + 1)
                for k in ("itemListElement", "mainEntity", "hasPart"):
                    if k in node:
                        walk(node[k], depth + 1)
            elif isinstance(node, list):
                for it in node[:40]:
                    walk(it, depth + 1)

        for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
            raw = tag.string or tag.get_text() or ""
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                try:                          # 有些站外层裹了注释
                    data = json.loads(re.sub(r"^\s*<!--|-->\s*$", "", raw))
                except Exception:
                    continue
            walk(data)
        return out

    def _from_dom(self, soup, base_url: str) -> List[Dict]:
        """DOM 启发式：先把「图 + 链接」归到同一张卡片里，再统一选标题。

        只在单个 <a> 上找文字很容易抓到时长（"02:52"）或播放量，
        所以这里先向上找 3 层定位卡片容器，再从容器里挑最像标题的文字。
        """
        out: List[Dict] = []
        seen_anchor: set = set()

        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not self._usable_href(href):
                continue
            if id(a) in seen_anchor:
                continue
            seen_anchor.add(id(a))

            img = a.find("img")
            container = self._card_container(a, img)
            image = self._card_image(a if img is not None else container)
            title = self._card_title(container, a)

            # 只有纯文字时，要求链接看起来像内容页，避免满屏导航
            if not image and not title:
                continue
            if not image:
                if len(title) < 8 or not any(h in href.lower() for h in _GOOD_URL_HINTS):
                    continue
            if not title:
                img_alt = ""
                if img is not None:
                    img_alt = self._title_like(img.get("alt") or img.get("title") or "")
                title = img_alt
            if not title:
                continue

            card_text = (container or a).get_text(" ", strip=True)[:400]
            out.append({
                "url": urljoin(base_url, href),
                "title": title,
                "image": urljoin(base_url, image) if image else "",
                "source": "card",
                "parent": self._parent_sig(container or a),
                "pub_ts": parse_pub_ts(card_text),
            })

        # 兜底：整页都没卡片时，退化成纯文字链接
        if len(out) < 3:
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not self._usable_href(href):
                    continue
                title = self._title_like(a.get_text(" ", strip=True), 8, 120)
                if not title:
                    continue
                if not any(h in href.lower() for h in _GOOD_URL_HINTS):
                    continue
                out.append({
                    "url": urljoin(base_url, href),
                    "title": title,
                    "image": "",
                    "source": "text",
                    "parent": self._parent_sig(a),
                })
        return out

    # ------------------------------------------------------------------ #
    # 卡片定位
    # ------------------------------------------------------------------ #
    def _card_container(self, a, img):
        """从链接向上找「卡片容器」：优先含标题元素的那一层。"""
        start = a if img is not None else a.parent
        if start is None:
            return a
        node = start
        fallback = start
        for _ in range(3):
            if node is None:
                break
            has_heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None
            try:
                has_title_class = node.find(True, class_=_TITLE_CLASS_RE) is not None
            except Exception:
                has_title_class = False
            if has_heading or has_title_class:
                return node
            if node.find("img") is not None:
                fallback = node
            node = node.parent
        return fallback

    def _card_title(self, node, link=None) -> str:
        if node is None:
            node = link
        if node is None:
            return ""
        # 1) 链接自身的 title / aria-label
        if link is not None:
            for attr in ("title", "aria-label"):
                t = self._title_like(link.get(attr) or "")
                if t:
                    return t
        # 2) 标题标签
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            for el in node.find_all(tag, limit=2):
                t = self._title_like(el.get_text(" ", strip=True))
                if t:
                    return t
        # 3) class 里带 title/name/caption 的元素
        try:
            for el in node.find_all(True, class_=_TITLE_CLASS_RE, limit=6):
                t = self._title_like(el.get("title") or el.get_text(" ", strip=True))
                if t:
                    return t
        except Exception:
            pass
        # 4) 容器里最长的有效链接文字
        best = ""
        for el in node.find_all("a", href=True, limit=30):
            t = self._title_like(el.get("title") or el.get_text(" ", strip=True))
            if len(t) > len(best):
                best = t
        if best:
            return best
        # 5) 图片 alt
        for el in node.find_all("img", limit=6):
            t = self._title_like(el.get("alt") or "")
            if t:
                return t
        return ""

    def _card_image(self, node) -> str:
        if node is None:
            return ""
        for img in node.find_all("img", limit=12):
            w = str(img.get("width") or "")
            h = str(img.get("height") or "")
            if w.isdigit() and h.isdigit() and (int(w) < 60 or int(h) < 40):
                continue
            src = self._img_src(img)
            if src and self._usable_image(src):
                return src
        return ""

    @staticmethod
    def _title_like(text: str, min_len: int = 4, max_len: int = 140) -> str:
        """判断一段文字像不像标题：太短、纯时间、纯数字、导航词都扔掉。"""
        t = Adapter.clean_text(text or "")
        # 卡片里常把「来源 / 时长 / 播放量」拼在标题后面，切掉
        t = re.sub(r"\s*(来源[:：]|时长[:：]|已浏览|上传时间|发布时间|播放量[:：]).*$", "", t)
        t = t.strip(" ·-—|｜,，")
        if not t or len(t) < min_len or len(t) > max_len:
            return ""
        if _TIME_RE.match(t) or _COUNT_RE.match(t) or _ONLY_SYMBOL_RE.match(t):
            return ""
        if t.lower() in _NAV_WORDS or t in _NAV_WORDS:
            return ""
        # 至少要有两个汉字或 4 个字母，过滤 "HD"、"4K" 这类标签
        zh = len(re.findall(r"[\u4e00-\u9fff]", t))
        en = len(re.findall(r"[A-Za-z]", t))
        if zh < 2 and en < 4:
            return ""
        return t

    @staticmethod
    def _usable_image(src: str) -> bool:
        low = (src or "").lower()
        if not low or low.startswith("data:"):
            return False
        if any(noise in low for noise in _IMG_NOISE):
            return False
        return True

    # ------------------------------------------------------------------ #
    # 排序 & 过滤
    # ------------------------------------------------------------------ #
    def _rank(self, candidates: List[Dict], base_url: str, query: str) -> List[Dict]:
        if not candidates:
            return []

        # 同一父容器签名出现最多的那一簇，通常就是结果列表
        cluster: Dict[str, int] = {}
        for c in candidates:
            sig = c.get("parent") or ""
            if sig:
                cluster[sig] = cluster.get(sig, 0) + 1
        best_sig = ""
        best_n = 0
        for sig, n in cluster.items():
            if n > best_n:
                best_sig, best_n = sig, n
        if best_n < 3:
            best_sig = ""

        base_host = urlsplit(base_url).netloc
        q_norm = (query or "").lower()
        scored: List[Tuple[float, int, Dict]] = []
        for idx, c in enumerate(candidates):
            url = c.get("url") or ""
            if not is_web_url(url):
                continue
            title = self._title_like(c.get("title") or "", 2, 160)
            image = c.get("image") or ""
            if not url or not title:
                continue
            if title.lower() in ("更多", "查看全部", "more", "next", "上一页", "下一页"):
                continue
            score = 0.0
            if image:
                score += 3.0
            if 6 <= len(title) <= 90:
                score += 1.0
            path = urlsplit(url).path
            if path.count("/") >= 2:
                score += 0.6
            if any(h in url.lower() for h in _GOOD_URL_HINTS):
                score += 1.2
            if best_sig and c.get("parent") == best_sig:
                score += 2.0
            if q_norm and q_norm in (title).lower():
                score += 0.8
            host = urlsplit(url).netloc
            if host and base_host and host != base_host:
                score += 0.3
            scored.append((score, idx, {**c, "title": title, "url": url,
                                        "image": image}))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [x[2] for x in scored]

    def _to_results(self, cards: List[Dict], site: SiteConfig, limit: int) -> List[SearchResult]:
        out: List[SearchResult] = []
        seen_url: set = set()
        seen_title: set = set()
        for c in cards:
            if len(out) >= limit:
                break
            url = c.get("url") or ""
            if not is_web_url(url):
                continue
            title = (c.get("title") or "").strip()
            if not url or not title:
                continue
            key = normalize_url(url)
            if key in seen_url:
                continue
            tkey = re.sub(r"[\s\W_]+", "", title.lower())[:40]
            if tkey and tkey in seen_title:
                continue
            seen_url.add(key)
            if tkey:
                seen_title.add(tkey)
            out.append(SearchResult(
                title=title[:200],
                url=url,
                source=site.display,
                source_id=site.id,
                cover=c.get("image") or "",
                desc=(c.get("desc") or "")[:300],
                pub_ts=float(c.get("pub_ts") or 0.0),
            ))
        return out

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _usable_href(href: str) -> bool:
        h = (href or "").strip()
        if not h or h.startswith("#"):
            return False
        low = h.lower()
        if low.startswith(_BAD_SCHEMES):
            return False
        if low.endswith(_BAD_EXT):
            return False
        if any(b in low for b in _BAD_URL_HINTS):
            return False
        return True

    @staticmethod
    def _parent_sig(el) -> str:
        node = el.parent
        hops = 0
        while node is not None and hops < 3:
            classes = node.get("class") or []
            if isinstance(classes, str):
                classes = [classes]
            if classes:
                return "%s.%s" % (node.name, ".".join(sorted(classes)[:2])[:60])
            node = node.parent
            hops += 1
        return ""

    @staticmethod
    def _img_src(img) -> str:
        """从 <img> / <source> 上尽量抠出真正的图片地址（含懒加载）。"""
        if img is None:
            return ""
        if img.name == "source":
            srcset = img.get("srcset") or ""
            if srcset:
                return GenericAdapter._best_from_srcset(srcset)
            return (img.get("src") or "").strip()
        for attr in _LAZY_ATTRS:
            val = img.get(attr)
            if val and isinstance(val, str):
                val = val.strip()
                if val.startswith("<"):
                    continue
                if val and not val.startswith("data:"):
                    return val
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        if srcset:
            return GenericAdapter._best_from_srcset(srcset)
        style = img.get("style") or ""
        m = re.search(r"url\((['\"]?)(.*?)\1\)", style)
        if m:
            return m.group(2).strip()
        # <picture><source>
        parent = getattr(img, "parent", None)
        if parent is not None and getattr(parent, "name", "") == "picture":
            source = parent.find("source")
            if source is not None:
                return GenericAdapter._img_src(source)
        return ""

    @staticmethod
    def _best_from_srcset(srcset: str) -> str:
        best = ""
        best_w = -1
        for part in str(srcset).split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split()
            url = bits[0]
            width = 0
            for b in bits[1:]:
                m = re.match(r"(\d+)[wx]$", b)
                if m:
                    width = int(m.group(1))
                    break
            if width > best_w:
                best, best_w = url, width
        return best

    @staticmethod
    def _first_str(value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            for v in value:
                s = GenericAdapter._first_str(v)
                if s:
                    return s
            return ""
        if isinstance(value, dict):
            for key in ("url", "contentUrl", "@id", "name"):
                s = GenericAdapter._first_str(value.get(key))
                if s:
                    return s
        return ""
