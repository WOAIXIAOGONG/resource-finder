# -*- coding: utf-8 -*-
"""数据模型：搜索结果、站点配置、事件。"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# 常见跟踪参数，去重时一律剥掉，避免同一个视频出现多条
_TRACKING_PARAMS = {
    "spm_id_from", "vd_source", "from_source", "from", "seid", "share_source",
    "share_medium", "share_plat", "share_session_id", "share_tag", "unique_k",
    "timestamp", "bbid", "ts", "msource", "csource", "vd_source",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "feature", "si", "pp", "ab_channel", "t", "start_radio", "rv",
    "suffix", "wid", "wfr", "forzc", "sfrom", "buvid", "up_id",
    # 搜索结果页带出来的跟踪参数（B站 cheese 卡/百度跳转页常见）
    "search_id", "search_query", "query_from", "track_id", "creative_id",
    "title_material_id", "from_spmid", "spm", "spm_id", "page_from",
}


def normalize_url(url: str) -> str:
    """把 URL 归一化，用于跨站/站内去重。"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        parts = urlsplit(url)
    except Exception:
        return url

    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    # 去掉常见 CDN/移动端前缀差异
    netloc = re.sub(r"^(www|m|mobile)\.", "", netloc)
    path = parts.path.rstrip("/") or "/"

    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in _TRACKING_PARAMS]
    if netloc in ("youtube.com", "youtu.be"):
        video_id = dict(query_pairs).get("v", "")
        if netloc == "youtu.be":
            video_id = path.strip("/")
        elif path.startswith(("/shorts/", "/embed/", "/live/")):
            video_id = path.split("/")[2]
        if video_id:
            return "https://youtube.com/watch?" + urlencode({"v": video_id})
    if netloc == "bilibili.com" and path.startswith("/video/"):
        scheme = "https"
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return bool(parts.scheme.lower() in ("http", "https") and parts.hostname
                    and not parts.username and not any(ord(c) < 32 for c in url))
    except (ValueError, TypeError):
        return False


def url_key(url: str) -> str:
    """URL 的稳定指纹（去重主键）。"""
    return hashlib.sha1(normalize_url(url).encode("utf-8", "ignore")).hexdigest()


def title_key(title: str) -> str:
    """标题指纹：去掉空白、标点、大小写差异后取 sha1 前 16 位。"""
    t = re.sub(r"[\s\u3000]+", "", title or "").lower()
    t = re.sub(r"[\[\]【】()（）<>《》\"'“”‘’|/\\,，.。!！?？:：;；\-—_+~`·]+", "", t)
    if len(t) < 4:
        return ""
    return hashlib.sha1(t.encode("utf-8", "ignore")).hexdigest()[:16]


_COUNT_RE = re.compile(r"(\d+(?:[\.,]\d+)?)\s*([万萬亿億KkMmBb]?)")
_UNIT = {"万": 1e4, "萬": 1e4, "亿": 1e8, "億": 1e8,
         "k": 1e3, "m": 1e6, "b": 1e9}


def parse_count(text: str) -> int:
    """把「12.3万」「1.2M」「1,234,567」这类文字转成整数，认不出返回 0。"""
    if not text:
        return 0
    if isinstance(text, (int, float)):
        return max(0, int(text)) if math.isfinite(text) else 0
    m = _COUNT_RE.search(str(text).replace(",", "").replace("，", ""))
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "").lower()
    if unit in _UNIT:
        num *= _UNIT[unit]
    return max(0, int(num)) if math.isfinite(num) else 0


_AGO_RE = re.compile(r"(\d+)\s*(秒|分钟|分|小时|时|天|日|周|星期|个月|月|年|"
                     r"second|minute|hour|day|week|month|year)s?\s*(?:前|ago)", re.I)
_AGO_UNIT = {"秒": 1, "分": 60, "分钟": 60, "时": 3600, "小时": 3600,
             "天": 86400, "日": 86400, "周": 604800, "星期": 604800,
             "个月": 2592000, "月": 2592000, "年": 31536000,
             "second": 1, "minute": 60, "hour": 3600, "day": 86400,
             "week": 604800, "month": 2592000, "year": 31536000}
_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def parse_pub_ts(text: str, now: float = 0.0) -> float:
    """把「3天前」「2 weeks ago」「2024-05-01」转成 unix 时间戳，认不出返回 0。"""
    import time as _time

    if not text:
        return 0.0
    if isinstance(text, (int, float)):
        if not math.isfinite(text) or text <= 0:
            return 0.0
        value = float(text) / 1000 if text > 1e11 else float(text)
        return value if value <= 253402300799 else 0.0
    s = str(text).strip()
    now = now or _time.time()
    if re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}", s):
        return parse_pub_ts(float(s), now)
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    for word, delta in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if word in s:
            return (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
                    - timedelta(days=delta)).timestamp()
    m = _AGO_RE.search(s)
    if m:
        unit = m.group(2).lower()
        return now - int(m.group(1)) * _AGO_UNIT.get(unit, 86400)
    m = _DATE_RE.search(s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()
        except (ValueError, OverflowError):
            return 0.0
    return 0.0


def short_date(ts: float, now: float = 0.0) -> str:
    """把发布时间转成卡片上那种短文本：今天 / 昨天 / 3 天前 / 05-12 / 2024-05-12。"""
    import time as _time

    if not ts:
        return ""
    now = now or _time.time()
    if ts > now + 86400:          # 明显不对的时间，别显示
        return ""
    try:
        days = (datetime.fromtimestamp(now).date() - datetime.fromtimestamp(ts).date()).days
    except (ValueError, OverflowError, OSError):
        return ""
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return "%d 天前" % days
    t = _time.localtime(ts)
    if t.tm_year == _time.localtime(now).tm_year:
        return "%02d-%02d" % (t.tm_mon, t.tm_mday)
    return "%04d-%02d-%02d" % (t.tm_year, t.tm_mon, t.tm_mday)


@dataclass
class SearchResult:
    """一条搜索结果，对应界面上的一个小窗口（卡片）。"""

    title: str = ""
    url: str = ""
    source: str = ""            # 来源显示名，例如 "哔哩哔哩"
    source_id: str = ""         # 来源内部 id
    cover: str = ""             # 远程封面地址
    cover_path: str = ""        # 已下载到本地的封面文件
    author: str = ""            # UP 主 / 频道
    duration: str = ""          # 时长，例如 "12:34"
    play: str = ""              # 播放量等附加信息（给人看的，例如 "12.3万"）
    desc: str = ""              # 简介
    route: str = ""             # 实际使用的网络通道
    views: int = 0              # 播放量（数值，0=不知道）
    pub_ts: float = 0.0         # 发布时间（unix 秒，0=不知道）
    pub_text: str = ""          # 发布时间的显示文本（空则用 pub_ts 自动算）
    extra: Dict[str, Any] = field(default_factory=dict)
    resource_type: str = 'video'

    def __post_init__(self):
        if self.resource_type == 'novel':
            self.resource_type = 'article'

    @property
    def date_label(self) -> str:
        """卡片封面上显示的日期短文本。"""
        return self.pub_text or short_date(self.pub_ts)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        return url_key(self.url) or (self.source_id + ":" + title_key(self.title))


@dataclass
class SiteEvent:
    """引擎向界面汇报的单站点进度事件。

    kind: start 开始 / done 出结果 / error 失败 / fatal 整体拒绝 / finish 全部结束
    """

    kind: str
    site_id: str = ""
    site_name: str = ""
    results: List[SearchResult] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    exhausted: Any = None
    pagination_supported: bool = True
    pagination_message: str = ''


@dataclass
class SearchStats:
    """一次搜索的汇总数据。"""

    query: str = ""
    started: float = 0.0
    finished: float = 0.0
    sites_total: int = 0
    sites_ok: int = 0
    sites_error: int = 0
    results: int = 0
    cancelled: bool = False

    @property
    def elapsed(self) -> float:
        end = self.finished or self.started
        return max(0.0, end - self.started) if self.started else 0.0
