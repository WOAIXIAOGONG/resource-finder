# -*- coding: utf-8 -*-
"""版本号 + 更新日志的一键维护工具。

    python tools/release.py --check
    python tools/release.py --version 1.6.2 --added "新增 XXX" --fixed "修了 YYY"
    python tools/release.py --version 1.7.0 --changed "把 A 改成 B" --dry-run

`--version` 会同时改三处：
    app/config.py                 APP_VERSION = "x.y.z"
    assets/version_info.txt       filevers/prodvers=(x, y, z, 0)、FileVersion/ProductVersion
    CHANGELOG.md                  在 [Unreleased] 下面插入一条新版本记录

`--check` 只核对这三处是否一致（打包前必须 PASS）。
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PY = os.path.join(HERE, "app", "config.py")
VERSION_INFO = os.path.join(HERE, "assets", "version_info.txt")
CHANGELOG = os.path.join(HERE, "CHANGELOG.md")

SECTION_TITLES = (
    ("added", "Added"),
    ("changed", "Changed"),
    ("fixed", "Fixed"),
    ("removed", "Removed"),
)


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def parse_version(text: str) -> tuple:
    parts = [int(p) for p in text.strip().split(".")]
    if len(parts) != 3:
        raise ValueError("版本号必须形如 1.2.3：%r" % text)
    return tuple(parts)


def fmt_version(ver: tuple) -> str:
    return "%d.%d.%d" % ver


def current_config_version() -> str:
    m = re.search(r'APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', read(CONFIG_PY))
    if not m:
        raise SystemExit("在 app/config.py 里找不到 APP_VERSION")
    return m.group(1)


def current_info_version() -> str:
    text = read(VERSION_INFO)
    m = re.search(r"filevers\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\s*\)", text)
    if not m:
        raise SystemExit("在 assets/version_info.txt 里找不到 filevers")
    return "%s.%s.%s" % m.groups()


def changelog_latest() -> str:
    text = read(CHANGELOG)
    m = re.search(r"^##\s*\[(\d+\.\d+\.\d+)\]", text, re.M)
    return m.group(1) if m else ""


def do_check() -> int:
    cfg = current_config_version()
    info = current_info_version()
    log = changelog_latest()
    print("app/config.py          APP_VERSION = %s" % cfg)
    print("assets/version_info.txt filevers    = %s" % info)
    print("CHANGELOG.md          最新条目      = %s" % (log or "（没有）"))
    ok = cfg == info == log and bool(cfg)
    print("CHECK: %s" % ("PASS" if ok else "FAIL（三处版本号不一致，先用 --version 修）"))
    return 0 if ok else 1


def bump_version(version: str) -> None:
    text = read(CONFIG_PY)
    text, n = re.subn(r'(APP_VERSION\s*=\s*")[0-9]+\.[0-9]+\.[0-9]+(")',
                      r"\g<1>%s\g<2>" % version, text)
    if n != 1:
        raise SystemExit("改 app/config.py 的 APP_VERSION 失败")
    write(CONFIG_PY, text)

    parts = [int(p) for p in version.split(".")]
    text = read(VERSION_INFO)
    text = re.sub(r"filevers\s*=\s*\([^)]*\)",
                  "filevers=(%d, %d, %d, 0)" % tuple(parts), text)
    text = re.sub(r"prodvers\s*=\s*\([^)]*\)",
                  "prodvers=(%d, %d, %d, 0)" % tuple(parts), text)
    text = re.sub(r"StringStruct\('FileVersion',\s*'[^']*'\)",
                  "StringStruct('FileVersion', '%s.0')" % version, text)
    text = re.sub(r"StringStruct\('ProductVersion',\s*'[^']*'\)",
                  "StringStruct('ProductVersion', '%s.0')" % version, text)
    write(VERSION_INFO, text)


def make_block(version: str, date: str, sections) -> str:
    lines = ["## [%s] - %s" % (version, date), ""]
    wrote = False
    for key, title in SECTION_TITLES:
        items = sections.get(key) or []
        if not items:
            continue
        wrote = True
        lines.append("### %s" % title)
        for item in items:
            lines.append("- %s" % item)
        lines.append("")
    if not wrote:
        lines.append("### Changed")
        lines.append("- 文档/维护性更新。")
        lines.append("")
    return "\n".join(lines)


def add_changelog(version: str, date: str, sections) -> None:
    text = read(CHANGELOG)
    anchor = "## [Unreleased]"
    idx = text.find(anchor)
    if idx < 0:
        raise SystemExit("CHANGELOG.md 里找不到 `## [Unreleased]`")
    head_end = idx + len(anchor)
    rest = text[head_end:].lstrip("\n")
    block = make_block(version, date, sections)
    write(CHANGELOG, text[:head_end] + "\n\n" + block + "\n" + rest)


def main() -> int:
    ap = argparse.ArgumentParser(description="版本号 + 更新日志一键维护")
    ap.add_argument("--version", help="新版本号，如 1.6.2（必须大于当前版本）")
    ap.add_argument("--date", default=datetime.date.today().isoformat(),
                    help="日志日期，默认今天（YYYY-MM-DD）")
    for key, _ in SECTION_TITLES:
        ap.add_argument("--" + key, action="append", default=[],
                        help="%s 小节的一条说明，可重复" % key)
    ap.add_argument("--check", action="store_true", help="只核对三处版本号是否一致")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    if args.check:
        return do_check()
    if not args.version:
        ap.print_help()
        return 1

    new = parse_version(args.version)
    old = parse_version(current_config_version())
    if new <= old:
        raise SystemExit("新版本 %s 必须大于当前 %s" % (args.version, fmt_version(old)))

    sections = {key: getattr(args, key) for key, _ in SECTION_TITLES}
    block = make_block(args.version, args.date, sections)

    if args.dry_run:
        print("将把版本改为 %s，并写入：\n" % args.version)
        print(block)
        return 0

    bump_version(args.version)
    add_changelog(args.version, args.date, sections)
    print("版本已更新：%s → %s" % (fmt_version(old), args.version))
    print("已改：app/config.py、assets/version_info.txt、CHANGELOG.md")
    print()
    print("接下来记得：")
    print("  1) 同步 使用说明.txt / README.md（用户可见行为有变化时）")
    print("  2) .\\.venv\\Scripts\\python.exe tools\\uitest.py --offline")
    print("  3) python tools\\build.py --dir")
    print("  4) 打包 zip + 解压跑 --selftest")
    return do_check()


if __name__ == "__main__":
    sys.exit(main())
