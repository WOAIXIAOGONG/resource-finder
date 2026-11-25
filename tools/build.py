# -*- coding: utf-8 -*-
"""打包成免安装文件夹（onedir）。

    python tools/build.py            # 打包
    python tools/build.py --dir      # 打成文件夹（启动更快，便于排查）

产物：dist/资源搜寻器/资源搜寻器.exe
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用不到的大块 Qt 模块，全部排除，能省一半体积
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtSensors", "PySide6.QtRemoteObjects", "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer", "PySide6.QtTextToSpeech", "PySide6.QtConcurrent",
    "PySide6.QtDBus", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "tkinter", "unittest", "pydoc_data", "test", "lib2to3",
    # 注意：不要排除 distutils —— setuptools 的 pre-safe-import hook 会 alias
    # setuptools._vendor.distutils 到 distutils，排掉它会让 PyInstaller 直接报
    # ValueError: Target module "distutils" already imported as ExcludedModule
    "numpy", "pandas", "matplotlib", "scipy",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="store_true", help="目录模式（非单文件）")
    ap.add_argument("--name", default="资源搜寻器")
    ap.add_argument("--debug", action="store_true", help="保留控制台，便于看报错")
    ap.add_argument('--output-dir', default=os.path.join(HERE, 'dist'), help='构建到独立目录，避免覆盖正在使用的程序和存档')
    args = ap.parse_args()
    from release_safety import release_target
    try:
        target = release_target(HERE, args.output_dir, args.name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.output_dir = str(target.parent)

    subprocess.run([sys.executable, os.path.join(HERE, "tools", "release.py"), "--check"], check=True)
    icon = os.path.join(HERE, "assets", "app.ico")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--noupx",
        "--windowed" if not args.debug else "--console",
        "--onedir",
        "--name", args.name,
        "--distpath", os.path.abspath(args.output_dir),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        "--paths", HERE,
        "--collect-submodules", "app",
        "--collect-data", "tldextract",
        "--hidden-import", "PIL.AvifImagePlugin",
        "--add-data", os.pathsep.join([os.path.join(HERE, "assets"), "assets"]),
    ]
    version_file = os.path.join(HERE, "assets", "version_info.txt")
    if os.path.isfile(version_file):
        cmd += ["--version-file", version_file]
    if os.path.isfile(icon):
        cmd += ["--icon", icon]
    else:
        print("! 没有找到 assets/app.ico，先运行 python tools/make_icon.py")
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(os.path.join(HERE, "main.py"))

    print("$", " ".join(cmd[:6]), "...")
    started = time.time()
    # Ambient tools may ship a same-named but ABI-incompatible ICU/OpenSSL DLL.
    # Resolve Qt dependencies from its wheel/Python/Windows, never from a host app.
    env = os.environ.copy()
    windows = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join(dict.fromkeys([
        os.path.dirname(sys.executable),
        os.path.dirname(getattr(sys, "_base_executable", sys.executable)),
        os.path.join(windows, "System32"), windows,
    ]))
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(cmd, cwd=HERE, env=env)
    if proc.returncode != 0:
        print("打包失败，退出码", proc.returncode)
        return proc.returncode

    source_tools = os.path.join(HERE, 'vendor', 'download-tools')
    for tool in ('yt-dlp.exe', 'ffmpeg.exe', 'ffprobe.exe', 'deno.exe', 'manifest.json'):
        if not os.path.isfile(os.path.join(source_tools, tool)):
            raise SystemExit('缺少内置下载组件，请开发者运行 tools/fetch_download_tools.py：' + tool)
    shutil.copytree(source_tools, os.path.join(args.output_dir, args.name, 'vendor', 'download-tools'), dirs_exist_ok=True)
    shutil.copytree(os.path.join(HERE,'download-login-helper'), os.path.join(args.output_dir,args.name,'download-login-helper'), dirs_exist_ok=True)
    reader = os.path.join(HERE, 'vendor', 'browser-reader')
    import hashlib, json
    try:
        with open(os.path.join(reader, 'manifest.json'), encoding='utf-8') as f:
            manifest = json.load(f)
        for filename,key in [('BrowserReader.cs','source_sha256'),('browser-reader.exe','binary_sha256')]:
            with open(os.path.join(reader,filename),'rb') as f:
                assert hashlib.sha256(f.read()).hexdigest() == manifest[key]
    except (OSError,ValueError,KeyError,AssertionError):
        subprocess.run([sys.executable, os.path.join(HERE,'tools','build_browser_reader.py')], check=True)
    if not os.path.isfile(os.path.join(reader, 'browser-reader.exe')):
        raise SystemExit('缺少内置浏览器页面读取组件，请开发者运行 tools/build_browser_reader.py')
    shutil.copytree(reader, os.path.join(args.output_dir,args.name,'vendor','browser-reader'), dirs_exist_ok=True)

    exe = os.path.join(args.output_dir, args.name + ".exe")
    if os.path.isfile(exe):
        print("\n完成：%s  （%.1f MB，用时 %.0fs）"
              % (exe, os.path.getsize(exe) / 1048576, time.time() - started))
    else:
        folder = os.path.join(args.output_dir, args.name)
        print("\n完成：目录 %s" % folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
