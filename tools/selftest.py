# -*- coding: utf-8 -*-
"""自检脚本：不开界面，直接把引擎跑一遍。

用法：
    python tools/selftest.py                 # 完整自检（会联网）
    python tools/selftest.py --offline       # 只测本地逻辑
    python tools/selftest.py -q 猫咪 -s bili,ytb,bing
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

FAILED = []
PASSED = []


def check(name: str, fn):
    started = time.time()
    try:
        detail = fn()
        PASSED.append(name)
        print("  [OK]   %-38s %5.2fs  %s" % (name, time.time() - started, detail or ""))
        return True
    except Exception as exc:                    # noqa: BLE001
        FAILED.append(name)
        print("  [FAIL] %-38s %5.2fs  %s: %s"
              % (name, time.time() - started, type(exc).__name__, exc))
        traceback.print_exc(limit=3)
        return False


# --------------------------------------------------------------------------- #
def test_imports():
    import app.config            # noqa: F401
    import app.models            # noqa: F401
    import app.net               # noqa: F401
    import app.engine            # noqa: F401
    import app.thumbcache        # noqa: F401
    import app.adapters          # noqa: F401
    import app.adapters.generic  # noqa: F401
    return "6 个模块"


def test_models():
    from app.models import normalize_url, title_key, SearchResult

    a = normalize_url("https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333.1007&vd_source=abc")
    assert "spm_id_from" not in a, a
    assert a.startswith("https://bilibili.com/video/BV1xx411c7mD"), a
    b = normalize_url("http://m.example.com/a/?utm_source=x")
    assert b == "http://example.com/a", b
    assert title_key("Hello World！") == title_key("hello world")
    r = SearchResult(title="t", url="u")
    assert r.cover == "" and r.extra == {}
    return a[:48]


def test_config_roundtrip():
    import json
    import tempfile

    from app.config import Config, Settings, SiteConfig

    cfg = Config()
    cfg.settings.workers = 3
    cfg.blacklist = ["暴力", "赌博"]
    cfg.add_site(SiteConfig(name="测试站", search_url="https://x.test/s?q={q}"))
    raw = json.dumps(cfg.to_dict(), ensure_ascii=False)
    back = Config.from_dict(json.loads(raw))
    assert back.settings.workers == 3
    assert back.blacklist == ["暴力", "赌博"]
    assert any(s.name == "测试站" for s in back.sites)
    assert back.match_blacklist("这是一个暴力视频") == "暴力"
    assert back.match_blacklist("正常内容") is None
    return "%d 个站点" % len(back.sites)


def test_url_template():
    from app.config import SiteConfig

    s = SiteConfig(name="x", search_url="https://s.test/find?wd={q}&p=1")
    assert s.search_url_for("猫 咪") == "https://s.test/find?wd=%E7%8C%AB+%E5%92%AA&p=1"
    assert s.validate() == ""
    bad = SiteConfig(name="x", search_url="https://s.test/find")
    assert "必须包含 {q}" in bad.validate()
    return "ok"


def test_generic_extract():
    """用一段假 HTML 验证通用抽取器能认出来卡片。"""
    from app.adapters.generic import GenericAdapter
    from app.config import SiteConfig

    html = """
    <html><body>
      <div class="nav"><a href="/login">登录</a><a href="/about">关于</a></div>
      <div class="list">
        <div class="card"><a href="/video/123"><img src="https://img.test/1.jpg" alt="第一个视频"></a>
          <h3><a href="/video/123">第一个视频的标题</a></h3></div>
        <div class="card"><a href="/video/456"><img data-src="https://img.test/2.jpg" alt=""></a>
          <h3><a href="/video/456">第二个视频的标题</a></h3></div>
        <div class="card"><a href="/video/789"><img src="//img.test/3.jpg" alt=""></a>
          <h3><a href="/video/789">第三个视频的标题</a></h3></div>
      </div>
    </body></html>
    """
    site = SiteConfig(name="t", search_url="https://t.test/s?q={q}")
    items = GenericAdapter().extract(html, "https://t.test/s?q=x", site, 10, "视频")
    assert len(items) >= 3, [i.title for i in items]
    assert items[0].url.startswith("https://t.test/video/")
    assert any(i.cover for i in items)
    return "%d 条" % len(items)


def test_thumbcache_local():
    import tempfile

    from app.config import Settings
    from app.net import HttpClient
    from app.thumbcache import ThumbCache

    with tempfile.TemporaryDirectory() as tmp:
        cache = ThumbCache(Settings(), HttpClient(Settings()), root=tmp)
        url = "https://example.com/a.jpg"
        assert cache.cached(url) is None
        p = cache.path_for(url)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 2000)
        assert cache.cached(url) == p
        assert cache.size_bytes() > 1000
        assert cache.evict(1) >= 0
        cache.shutdown()
    return "缓存命中/淘汰正常"


def test_network():
    from app.config import Settings
    from app.net import HttpClient, describe_network

    settings = Settings()
    http = HttpClient(settings)
    text = http.get_text("https://www.baidu.com/", timeout=10.0)
    assert len(text) > 500
    return describe_network(settings)


def test_engine(query: str, site_ids: str, timeout: float = 45.0):
    from app.config import Config
    from app.engine import SearchEngine
    from app.net import HttpClient

    cfg = Config()
    if site_ids:
        keep = {s.strip() for s in site_ids.split(",") if s.strip()}
        cfg.sites = [s for s in cfg.sites if s.id in keep]
        assert cfg.sites, "没有匹配的站点 id：%s" % site_ids
    events = []
    started = time.time()

    def on_event(ev):
        events.append(ev)
        if ev.kind == "done":
            print("        %-12s +%-3d %.1fs" % (ev.site_name, len(ev.results or []),
                                                ev.elapsed or 0))
        elif ev.kind == "error":
            print("        %-12s ERR %s" % (ev.site_name, ev.error))

    http = HttpClient(cfg.settings)
    engine = SearchEngine(cfg, http, log=lambda m: None)
    stats = engine.run(query, on_event)
    cost = time.time() - started
    print("        总计 %d 条 / %d 站点 / %.1fs" % (stats.results, stats.sites_total, cost))
    for ev in events:
        if ev.kind == "done" and ev.results:
            r = ev.results[0]
            print("        示例[%s] %s" % (ev.site_name, r.title[:50]))
            print("                  %s" % r.url[:90])
            print("                  封面: %s" % (r.cover[:80] or "无"))
    assert stats.sites_ok + stats.sites_error == stats.sites_total
    return "ok=%d err=%d results=%d" % (stats.sites_ok, stats.sites_error, stats.results)


def test_thumb_download():
    from app.config import Settings
    from app.net import HttpClient
    from app.thumbcache import ThumbCache

    cache = ThumbCache(Settings(), HttpClient(Settings()))
    url = "https://www.baidu.com/img/flexible/logo/pc/result.png"
    done = {}
    import threading

    ev = threading.Event()

    def cb(u, p):
        done["path"] = p
        ev.set()

    cache.request(url, referer="https://www.baidu.com/", done=cb)
    ev.wait(15)
    cache.shutdown()
    path = done.get("path")
    assert path and os.path.isfile(path), "封面下载失败"
    return "%.0f KB" % (os.path.getsize(path) / 1024)


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--query", default="风景")
    parser.add_argument("-s", "--sites", default="")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    print("=" * 78)
    print("资源搜寻器 · 自检")
    print("Python %s" % sys.version.split()[0])
    print("=" * 78)

    print("\n[本地逻辑]")
    check("模块导入", test_imports)
    check("URL 归一化 / 标题去重", test_models)
    check("配置读写与黑名单", test_config_roundtrip)
    check("搜索地址模板", test_url_template)
    check("通用 HTML 抽取", test_generic_extract)
    check("封面缓存（本地）", test_thumbcache_local)

    if not args.offline:
        print("\n[网络]")
        check("基础连通性", test_network)
        check("封面下载", test_thumb_download)
        print("\n[引擎 · 关键词=%r]" % args.query)
        check("多站点搜索", lambda: test_engine(args.query, args.sites))

    print("\n" + "=" * 78)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
