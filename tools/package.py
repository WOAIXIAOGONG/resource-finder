# -*- coding: utf-8 -*-
"""把成品、文档、源码打成一个交付包。

    python tools/package.py                # 用当前版本号打 dist\\资源搜寻器-vX.Y.Z-win64.zip
    python tools/package.py --no-zip       # 只整理 dist\\资源搜寻器\\，不打 zip

做的事：
  dist\\资源搜寻器\\
      ├─ 资源搜寻器.exe + _internal\\      （build.py --dir 的产物，必须已存在）
      ├─ 使用说明.txt / 详情介绍.md / CHANGELOG.md / AGENTS.md
      └─ app\\、tools\\、assets\\、shots\\、main.py、requirements.txt、README.md
                                          （与 exe 同版本的源码，直接放顶层，方便接手）

只对独立构建产物整理；含存档、旧配置或下载目录时拒绝打包，避免覆盖数据。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(HERE, "dist")
APP_DIR = os.path.join(DIST, "资源搜寻器")

DOC_FILES = ("使用说明.txt", "详情介绍.md", "CHANGELOG.md", "AGENTS.md")
SRC_ITEMS = ("app", "tools", "assets", "browser-extension", "download-login-helper", "vendor", "main.py", "requirements.txt", "README.md", ".gitignore")

# 只有这些工具进交付包（够接手方改代码 + 测试 + 打包 + 验证）。
# 其余 diag_*.py / check_*.py / uia-*-probe.ps1 都是一次性排查脚本，留在开发仓库里，别塞进交付包。
TOOLS_KEEP = {
    "build.py",            # 打包出 onedir
    "package.py",          # 本脚本：整理交付包
    "release.py",          # 升版本号 + 写 CHANGELOG
    "release_safety.py",
    "release_safety_test.py",
    "regression.py",
    "liveness_test.py",
    "qa_test.py",
    "fill_test.py",
    "completion_test.py",
    "session_test.py",
    "shutdown_test.py",
    "update_test.py",
    "publish_release.py",
    "worker_test.py",
    "contract_test.py",
    "login_test.py",
    "browser_bridge_test.py",
    "pairing_test.py",
    "native_browser_test.py",
    "background_browser_test.py",
    "build_browser_reader.py",
    "pairing_extension_test.js",
    "browser_extension_test.js",
    "access_test.py",
    "interrupt_test.py",
    "routing_test.py",
    "budget_test.py",
    "media_test.py",
    "duration_test.py",
    "thumbnail_test.py",
    "source_test.py",
    "site_intake_test.py",
    "profile_test.py",
    "resource_test.py",
    "sound_test.py",
    "aigei_download_test.py",
    "aigei_browser_test.py",
    "aigei_mcp_test.py",
    "search_matrix.py",
    "fetch_download_tools.py",
    "download_test.py",
    "download_helper_test.js",
    "testenv.py",
    "paging_test.py",
    "continuation_test.py",
    "continuation_probe.py",
    "uitest.py",           # 界面自动化测试
    "selftest.py",         # 引擎/网络自检
    "make_icon.py",        # 生成 app.ico
    "probe_sites.py",      # 探测候选站点，加新搜索源时用
    "shot.py",             # 开发期截图
    "uia-search-probe.ps1",  # 真机冒烟：搜一次并点卡片
}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "profiles"}
SKIP_SUFFIX = (".pyc", ".pyo")


def version() -> str:
    text = open(os.path.join(HERE, "app", "config.py"), encoding="utf-8").read()
    m = re.search(r'APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    if not m:
        raise SystemExit("读不到 app/config.py 里的 APP_VERSION")
    return m.group(1)


def ignored(_dir: str, names):
    return [n for n in names if n in SKIP_DIRS]


def clean_app_dir() -> None:
    # Data directories are rejected by release_target, never deleted here.
    if os.path.isdir(APP_DIR):
        for name in os.listdir(APP_DIR):
            if name.startswith("selftest") and name.endswith(".txt"):
                os.remove(os.path.join(APP_DIR, name))


def copy_docs() -> None:
    for name in DOC_FILES:
        src = os.path.join(HERE, name)
        if not os.path.isfile(src):
            raise SystemExit("缺少文档：%s" % name)
        shutil.copy2(src, os.path.join(APP_DIR, name))
    shutil.copy2(os.path.join(HERE, "使用说明.txt"), os.path.join(DIST, "使用说明.txt"))


def copy_source() -> None:
    dst_root = APP_DIR

    for item in SRC_ITEMS:
        src = os.path.join(HERE, item)
        dst = os.path.join(dst_root, item)
        if not os.path.exists(src):
            print("跳过（不存在）：", item)
            continue
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=ignored)
        else:
            shutil.copy2(src, dst)

    # 源码里不需要的体积/临时文件
    for root, dirs, files in os.walk(dst_root):
        for name in list(dirs):
            if name in SKIP_DIRS:
                if name != 'profiles':
                    shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)
        for name in files:
            if name.endswith(SKIP_SUFFIX):
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass

    # tools\ 只留维护必需的那几个
    tools_dir = os.path.join(dst_root, "tools")
    if os.path.isdir(tools_dir):
        removed = []
        for name in sorted(os.listdir(tools_dir)):
            if name in TOOLS_KEEP:
                continue
            path = os.path.join(tools_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed.append(name)
        if removed:
            print("交付包里略过的开发脚本（%d 个）：%s" % (len(removed), "、".join(removed)))


def referenced_shots() -> None:
    """把 README 里引用到的截图留在 源码\\shots\\，其余截图不必进交付包。"""
    readme = os.path.join(HERE, "README.md")
    if not os.path.isfile(readme):
        return
    text = open(readme, encoding="utf-8").read()
    names = sorted(set(re.findall(r"shots/([A-Za-z0-9._-]+)", text)))
    shots_dir = os.path.join(APP_DIR, "shots")
    if not os.path.isdir(shots_dir):
        return
    for name in os.listdir(shots_dir):
        if name not in names:
            path = os.path.join(shots_dir, name)
            os.remove(path) if os.path.isfile(path) else shutil.rmtree(path, ignore_errors=True)
    print("源码里保留的截图：", ", ".join(names) or "（无）")


def make_zip(ver: str) -> str:
    out = os.path.join(DIST, "资源搜寻器-v%s-win64.zip" % ver)
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(APP_DIR):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, DIST)          # 以 dist 为根，保留“资源搜寻器\”这一层
                zf.write(full, rel)
    return out


def main() -> int:
    global DIST, APP_DIR
    ap = argparse.ArgumentParser(description="成品 + 文档 + 源码，打成一个交付包")
    ap.add_argument('--dist-dir', default=DIST, help='独立构建产物目录')
    ap.add_argument("--no-zip", action="store_true", help="只整理文件夹，不打 zip")
    args = ap.parse_args()
    DIST = os.path.abspath(args.dist_dir)
    from release_safety import release_target
    try:
        APP_DIR = str(release_target(HERE, DIST))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    subprocess.run([sys.executable, os.path.join(HERE, "tools", "release.py"), "--check"], check=True)

    ver = version()
    if not os.path.isdir(APP_DIR):
        raise SystemExit("找不到 %s，先跑 python tools\\build.py --dir" % APP_DIR)
    exe = os.path.join(APP_DIR, "资源搜寻器.exe")
    if not os.path.isfile(exe) or not os.path.isdir(os.path.join(APP_DIR, "_internal")):
        raise SystemExit("缺少 exe 或 _internal，请先完整构建 onedir")
    import pefile
    with pefile.PE(exe, fast_load=True) as pe:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        info = pe.VS_FIXEDFILEINFO[0]
        actual = (info.FileVersionMS >> 16, info.FileVersionMS & 65535, info.FileVersionLS >> 16)
        if actual != tuple(map(int, ver.split("."))):
            raise SystemExit("EXE 版本与源码不一致，请重新构建后再打包")

    clean_app_dir()
    copy_docs()
    copy_source()
    referenced_shots()
    sys.path.insert(0,HERE)
    from app.updates import write_manifest
    write_manifest(APP_DIR,ver)

    total = 0
    for root, _dirs, files in os.walk(APP_DIR):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    print("整理完成：%s（%d 个文件，%.1f MB）" % (APP_DIR, sum(len(f) for _, _, f in os.walk(APP_DIR)),
                                          total / 1024 / 1024))
    print("  ├─ 使用说明.txt / 详情介绍.md / CHANGELOG.md / AGENTS.md")
    print("  └─ 源码：app、tools、assets、shots、main.py、requirements.txt、README.md（与 exe 同版本）")

    if not args.no_zip:
        out = make_zip(ver)
        print("已打包：%s（%.1f MB）" % (out, os.path.getsize(out) / 1024 / 1024))
        print("下一步：解压 zip 跑 `资源搜寻器.exe --selftest --selfout selftest.txt`，末行应为 RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
