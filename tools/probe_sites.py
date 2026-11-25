# -*- coding: utf-8 -*-
"""候选站点探测：给每个站点跑一次通用抽取，看能不能抓到带封面的条目。

    python tools/probe_sites.py -q 风景
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CANDIDATES = [
    ("必应视频", "https://www.bing.com/videos/search?q={q}&setlang=zh-hans"),
    ("优酷", "https://so.youku.com/search_video/q_{q}"),
    ("爱奇艺", "https://so.iqiyi.com/so/q_{q}"),
    ("腾讯视频", "https://v.qq.com/x/search/?q={q}"),
    ("搜狐视频", "https://so.tv.sohu.com/mts?wd={q}"),
    ("芒果TV", "https://so.mgtv.com/so?k={q}"),
    ("百度视频", "https://v.baidu.com/v?word={q}"),
    ("豆瓣", "https://www.douban.com/search?q={q}"),
    ("知乎", "https://www.zhihu.com/search?type=content&q={q}"),
    ("微博", "https://s.weibo.com/weibo?q={q}"),
    ("搜狗视频", "https://v.sogou.com/v?query={q}"),
    ("抖音", "https://www.douyin.com/search/{q}"),
    ("西瓜视频", "https://www.ixigua.com/search/{q}/"),
    ("好看视频", "https://haokan.baidu.com/web/search/page?query={q}"),
    ("360影视", "https://so.360kan.com/index.php?kw={q}"),
    ("1905电影网", "https://www.1905.com/search/?q={q}"),
    ("AcFun", "https://www.acfun.cn/search?keyword={q}"),
    ("Vimeo", "https://vimeo.com/search?q={q}"),
    ("Dailymotion", "https://www.dailymotion.com/search/{q}/videos"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", default="风景")
    ap.add_argument("-n", "--only", default="")
    args = ap.parse_args()

    from app.adapters.generic import GenericAdapter
    from app.config import Config, SiteConfig
    from app.net import HttpClient

    cfg = Config()
    http = HttpClient(cfg.settings)
    adapter = GenericAdapter()
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    for name, tpl in CANDIDATES:
        if only and name not in only:
            continue
        site = SiteConfig(name=name, search_url=tpl, referer="", max_results=12)
        url = site.search_url_for(args.query)
        try:
            html = http.get_text(url, site, timeout=15.0)
            items = adapter.extract(html, url, site, 12, args.query)
            with_cover = sum(1 for i in items if i.cover)
            print("\n[%s] %d 条（%d 条有封面）%s" % (name, len(items), with_cover, url[:70]))
            for it in items[:3]:
                print("   - %s" % it.title[:56])
                print("     %s" % (it.cover[:78] or "无封面"))
        except Exception as exc:            # noqa: BLE001
            print("\n[%s] 失败：%s: %s" % (name, type(exc).__name__, str(exc)[:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
