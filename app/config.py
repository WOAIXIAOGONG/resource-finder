# -*- coding: utf-8 -*-
"""配置管理：网站白名单、内容黑名单、运行设置。

配置文件位置（与 exe 无关，卸载程序也能保留设置）：
    %APPDATA%\\ResourceFinder\\config.json
封面缓存位置：
    %LOCALAPPDATA%\\ResourceFinder\\cache\\thumbs
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

APP_ID = "ResourceFinder"
APP_NAME = "资源搜寻器"
APP_NAME_EN = "Resource Finder"
APP_VERSION = "3.38.0"
CONFIG_VERSION = 1


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
_PATHS: Dict[str, str] = {}
_PROFILE_DIR: Optional[str] = None


def use_profile(directory: Optional[str]) -> None:
    """Selected local save; legacy paths stay intact when no profile is selected."""
    global _PROFILE_DIR
    _PROFILE_DIR = os.path.abspath(directory) if directory else None


def app_dir() -> str:
    """程序目录：打包后是 exe 所在目录，开发时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    """打包进 EXE 的只读资源（图标等）路径。"""
    base = getattr(sys, "_MEIPASS", "") or app_dir()
    cand = os.path.join(base, rel)
    if os.path.exists(cand):
        return cand
    return os.path.join(app_dir(), rel)


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def _localappdata() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")


def _writable(d: str) -> bool:
    """真的写一个临时文件，不能只看目录存不存在。

    这里刻意不用 tempfile.mkstemp：Windows 上目录被安全软件/策略拒绝写入时，
    mkstemp 会认为「目录可写、只是名字撞了」，于是把 238328 个候选名全试一遍，
    表现成启动卡死好几分钟。用自己的固定探测名，失败立刻返回。
    """
    if not d:
        return False
    probe = os.path.join(d, ".probe-%s" % uuid.uuid4().hex)
    try:
        os.makedirs(d, exist_ok=True)
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return False


def _pick(name: str, candidates: List[str]) -> str:
    if name in _PATHS:
        return _PATHS[name]
    chosen = ""
    for cand in candidates:
        if cand and _writable(cand):
            chosen = cand
            break
    if not chosen:                      # 最后兜底：临时目录
        chosen = os.path.join(tempfile.gettempdir(), APP_ID)
        try:
            os.makedirs(chosen, exist_ok=True)
        except OSError:
            pass
    _PATHS[name] = chosen
    return chosen


def config_dir() -> str:
    """配置目录：%APPDATA%\\ResourceFinder；不可写时退回程序旁 data\\。"""
    if _PROFILE_DIR:
        return _PROFILE_DIR
    override = (os.environ.get("RESOURCE_FINDER_HOME") or "").strip()
    cands: List[str] = [override] if override else []
    cands += [os.path.join(_appdata(), APP_ID), os.path.join(app_dir(), "data")]
    return _pick("config", cands)


def cache_dir() -> str:
    """缓存目录：%LOCALAPPDATA%\\ResourceFinder\\cache；不可写时退回 data\\cache。"""
    if _PROFILE_DIR:
        path = os.path.join(_PROFILE_DIR, 'cache')
        os.makedirs(path, exist_ok=True)
        return path
    override = os.environ.get("RESOURCE_FINDER_HOME", "").strip()
    if override:
        return _pick("cache", [os.path.join(override, "cache")])
    cands = [os.path.join(_localappdata(), APP_ID, "cache"),
             os.path.join(config_dir(), "cache")]
    return _pick("cache", cands)


def thumbs_dir() -> str:
    d = os.path.join(cache_dir(), "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def log_path() -> str:
    return os.path.join(config_dir(), "app.log")


# --------------------------------------------------------------------------- #
# 站点
# --------------------------------------------------------------------------- #
@dataclass
class SiteConfig:
    """一个搜索来源（白名单里的一行）。"""

    id: str = ""
    name: str = ""
    kind: str = "generic"          # generic | builtin | domain (indexed whole-site search)
    builtin: str = ""              # bilibili / youtube / bing ...
    enabled: bool = True
    search_url: str = ""           # 通用站点：搜索地址模板，用 {q} 代表关键词
    route: str = "auto"            # auto | proxy | direct
    referer: str = ""              # 抓取时带的 Referer
    encoding: str = ""             # 强制页面编码，留空自动判断
    headers: Dict[str, str] = field(default_factory=dict)
    css: Dict[str, str] = field(default_factory=dict)   # 可选：item/link/title/image 选择器
    max_results: int = 24          # 该站点最多取多少条
    note: str = ""
    scope_domain: str = ""        # Optional whole-site boundary; includes subdomains.

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_site_id(self.name or self.builtin or "site")
        self.route = self.route if self.route in ("auto", "proxy", "direct") else "auto"
        try:
            self.max_results = max(1, min(60, int(self.max_results)))
        except Exception:
            self.max_results = 24

    @property
    def display(self) -> str:
        return self.name or self.builtin or self.id

    def is_valid(self) -> bool:
        if self.kind == 'domain' and not self.scope_domain:
            return False
        if self.kind == "builtin":
            return bool(self.builtin)
        return bool(self.search_url and "{q}" in self.search_url)

    def validate(self) -> str:
        """返回错误说明，空串表示通过。"""
        if not self.name.strip():
            return "站点名称不能为空"
        if self.kind == 'domain' and not self.scope_domain:
            return "限定网站检索缺少域名，请重新粘贴网站链接"
        if self.kind == "builtin":
            if not self.builtin:
                return "内置站点缺少类型标识"
            return ""
        if not self.search_url.strip():
            return "搜索地址不能为空"
        if "{q}" not in self.search_url:
            return "搜索地址里必须包含 {q}（代表搜索关键词的位置）"
        if not re.match(r"^https?://", self.search_url.strip()):
            return "搜索地址必须以 http:// 或 https:// 开头"
        return ""

    def search_url_for(self, query: str) -> str:
        from urllib.parse import quote_plus

        q = quote_plus(query)
        url = self.search_url.replace("{q}", q).replace("{query}", q)
        return url

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SiteConfig":
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        clean = {k: v for k, v in (data or {}).items() if k in known}
        if not isinstance(clean.get("headers"), dict):
            clean["headers"] = {}
        if not isinstance(clean.get("css"), dict):
            clean["css"] = {}
        return cls(**clean)


def new_site_id(seed: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (seed or "").lower()).strip("-")[:16]
    return (slug + "-" if slug else "") + uuid.uuid4().hex[:6]


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    workers: int = 6               # 同时抓取的站点数
    max_results_per_site: int = 24
    timeout: float = 180.0
    thumb_workers: int = 12
    theme: str = "dark"            # dark | light
    proxy_mode: str = "auto"       # auto（跟随系统/环境变量）| direct | manual
    proxy_url: str = ""
    timeout_revision: int = 1       # Migrate the old 12-second default once.
    insecure_fallback: bool = True  # 证书异常时降级重试一次
    open_in_browser: bool = True  # Legacy field; 2.3+ always opens on left-click.
    cache_mb: int = 300
    window_geometry: str = ""
    sidebar_width: int = 250
    show_thumbnails: bool = True
    polite_delay: float = 0.0      # 站点之间的礼貌间隔
    agent_search_mode: str = "limited"  # limited: manual batches; unlimited: 1000 per query
    resource_type: str = 'video'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        if not isinstance(data, dict):
            data = {}
        data = dict(data)
        defaults = cls()
        if not data.get("timeout_revision") and data.get("timeout", 12) in (12, 12.0, "12", "12.0"):
            data["timeout"] = 180.0
        data["timeout_revision"] = 1
        for key, cast in (("workers", int), ("thumb_workers", int),
                          ("max_results_per_site", int), ("timeout", float),
                          ("cache_mb", int), ("polite_delay", float)):
            try:
                data[key] = cast(data.get(key, getattr(defaults, key)))
                if not __import__("math").isfinite(data[key]):
                    raise ValueError("non-finite setting")
            except (TypeError, ValueError, OverflowError):
                data[key] = getattr(defaults, key)
        inst = cls(**{k: v for k, v in (data or {}).items() if k in known})
        inst.workers = max(1, min(16, int(inst.workers or 6)))
        inst.thumb_workers = max(1, min(24, int(inst.thumb_workers or 8)))
        inst.max_results_per_site = max(1, min(60, int(inst.max_results_per_site or 24)))
        inst.timeout = max(3.0, min(600.0, float(inst.timeout or 180.0)))
        inst.cache_mb = max(50, min(5000, int(inst.cache_mb or 300)))
        inst.polite_delay = max(0, min(10, inst.polite_delay))
        if inst.theme not in ("dark", "light"):
            inst.theme = "dark"
        if inst.proxy_mode not in ("auto", "direct", "manual"):
            inst.proxy_mode = "auto"
        if inst.agent_search_mode not in ('limited', 'unlimited'):
            inst.agent_search_mode = 'limited'
        if inst.resource_type == 'novel':
            inst.resource_type = 'article'
        if inst.resource_type not in ('video', 'image', 'article', 'sound'):
            inst.resource_type = 'video'
        return inst


# --------------------------------------------------------------------------- #
# 内置站点
# --------------------------------------------------------------------------- #
#: 内置站点的「标准搜索地址」：编辑器里预填这个地址；
#: 用户把地址改掉（或填了自定义 CSS）时，就退化成通用网页抓取。
BUILTIN_SEARCH_URLS = {
    "bilibili": "https://search.bilibili.com/all?keyword={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "bing": "https://www.bing.com/search?q={q}",
}


def default_search_url(builtin: str) -> str:
    return BUILTIN_SEARCH_URLS.get(builtin, "")


def builtin_sites() -> List[SiteConfig]:
    return [
        SiteConfig(
            id="bili", name="哔哩哔哩", kind="builtin", builtin="bilibili",
            route="auto", referer="https://www.bilibili.com/", max_results=24,
            search_url=default_search_url("bilibili"),
            note="B 站视频搜索（官方搜索接口）",
        ),
        SiteConfig(
            id="ytb", name="YouTube", kind="builtin", builtin="youtube",
            route="auto", referer="https://www.youtube.com/", max_results=24,
            search_url=default_search_url("youtube"),
            note="YouTube 视频搜索（网页结果解析）",
        ),
        SiteConfig(
            id="bing", name="必应网页", kind="builtin", builtin="bing",
            route="auto", max_results=20,
            search_url=default_search_url("bing"),
            note="必应网页搜索，用于兜底覆盖名单之外的站点",
        ),
        SiteConfig(
            id="baidu", name="百度视频", kind="generic",
            search_url="https://v.baidu.com/v?word={q}",
            route="auto", referer="https://v.baidu.com/", max_results=16,
            note="百度视频聚合搜索（结果是跳转链接，复制时会自动解析成真实地址）",
        ),
    ]


def default_sites() -> List[SiteConfig]:
    """Original enabled sources plus optional public-site presets."""
    from .site_catalog import catalog_sites
    return builtin_sites() + catalog_sites()


# --------------------------------------------------------------------------- #
# 总配置
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self) -> None:
        self.version: int = CONFIG_VERSION
        self.settings: Settings = Settings()
        self.sites: List[SiteConfig] = default_sites()
        from .site_catalog import CATALOG_REVISION
        self.site_catalog_revision = CATALOG_REVISION
        self.blacklist: List[str] = []
        self.history: List[str] = []
        self._dirty = False
        self._storage_dir: Optional[str] = None
        self._profile_name = ''

    # --- 序列化 ---------------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "site_catalog_revision": self.site_catalog_revision,
            "settings": self.settings.to_dict(),
            "sites": [s.to_dict() for s in self.sites],
            "blacklist": list(self.blacklist),
            "history": list(self.history[:50]),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        try:
            cfg.version = int(data.get("version") or CONFIG_VERSION)
        except (ValueError, TypeError):
            cfg.version = CONFIG_VERSION
        cfg.settings = Settings.from_dict(data.get("settings") or {})
        sites = []
        for raw in data.get("sites") or []:
            if not isinstance(raw, dict):
                continue
            try:
                sites.append(SiteConfig.from_dict(raw))
            except Exception:
                continue
        cfg.sites = sites if data.get("sites") == [] or sites else default_sites()
        from .site_catalog import CATALOG_REVISION, merge_catalog
        try:
            revision = int(data.get('site_catalog_revision') or 0)
        except (TypeError, ValueError):
            revision = 0
        if revision < CATALOG_REVISION and cfg.sites:
            cfg._dirty = bool(merge_catalog(cfg.sites, since_revision=revision))
        cfg.site_catalog_revision = max(revision, CATALOG_REVISION)
        bl = data.get("blacklist")
        cfg.blacklist = [str(x).strip() for x in bl if str(x).strip()] if isinstance(bl, list) else []
        hist = data.get("history")
        cfg.history = [str(x) for x in hist][:50] if isinstance(hist, list) else []
        return cfg

    # --- 站点操作 -------------------------------------------------------- #
    def get_site(self, site_id: str) -> Optional[SiteConfig]:
        for s in self.sites:
            if s.id == site_id:
                return s
        return None

    def enabled_sites(self) -> List[SiteConfig]:
        return [s for s in self.sites if s.enabled and s.is_valid()]

    def add_site(self, site: SiteConfig) -> None:
        self.sites.append(site)
        self._dirty = True

    def remove_site(self, site_id: str) -> None:
        self.sites = [s for s in self.sites if s.id != site_id]
        self._dirty = True

    def move_site(self, site_id: str, delta: int) -> None:
        idx = next((i for i, s in enumerate(self.sites) if s.id == site_id), -1)
        if idx < 0:
            return
        new = max(0, min(len(self.sites) - 1, idx + delta))
        if new != idx:
            self.sites.insert(new, self.sites.pop(idx))
            self._dirty = True

    # --- 黑名单 ---------------------------------------------------------- #
    def add_blackword(self, word: str) -> bool:
        w = (word or "").strip()
        if not w:
            return False
        if any(w.lower() == x.lower() for x in self.blacklist):
            return False
        self.blacklist.append(w)
        self._dirty = True
        return True

    def remove_blackword(self, word: str) -> None:
        self.blacklist = [x for x in self.blacklist if x != word]
        self._dirty = True

    def match_blacklist(self, text: str) -> Optional[str]:
        """返回命中的黑名单词，未命中返回 None。"""
        if not text:
            return None
        low = text.lower()
        for w in self.blacklist:
            wl = w.strip().lower()
            if wl and wl in low:
                return w
        return None

    def add_history(self, query: str) -> None:
        q = (query or "").strip()
        if not q:
            return
        self.history = [q] + [x for x in self.history if x != q]
        self.history = self.history[:50]
        self._dirty = True


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def load_config() -> Config:
    path = config_path()
    if not os.path.exists(path):
        cfg = Config()
        cfg._storage_dir = os.path.dirname(path)
        save_config(cfg)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = Config.from_dict(data)
        cfg._storage_dir = os.path.dirname(path)
        return cfg
    except Exception:
        # 配置损坏：备份后回到默认值，绝不因为一个坏文件启动不了
        try:
            shutil.copy2(path, path + ".corrupt-%d" % int(time.time()))
        except Exception:
            pass
        cfg = Config()
        cfg._storage_dir = os.path.dirname(path)
        save_config(cfg)
        return cfg


def save_config(cfg: Config) -> bool:
    """原子写入，避免写一半掉电导致配置损坏。"""
    directory = cfg._storage_dir or config_dir()
    path = os.path.join(directory, 'config.json')
    # 同样不用 mkstemp：目录不可写时它会空转 238328 次。
    tmp_path = os.path.join(directory, ".cfg-%s.part" % uuid.uuid4().hex)
    try:
        tmp_fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        cfg._dirty = False
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False
