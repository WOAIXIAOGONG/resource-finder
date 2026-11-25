# 接手这个项目前必读

> 你是接手继续迭代「资源搜寻器」的开发者/Agent。先读 `详情介绍.md`（架构与坑），
> 再看 `CHANGELOG.md`（改到哪一版了），然后按下面的规矩干活。
> 这份文件优先级高于你的一般习惯：**每次改动都必须留下版本号和日志**。

---

## 1. 每次改动必须做的四件事

1. **改代码/文档**；
2. **升版本号 + 写日志**（一条命令搞定）：

   ```powershell
   python tools\release.py --version 1.7.0 ^
     --added "新增 XXX" ^
     --changed "把 YYY 改成 ZZZ" ^
     --fixed "修了 NNN"
   ```

   它会同时改：`app/config.py` 的 `APP_VERSION`、`assets/version_info.txt` 的
   `filevers/prodvers/FileVersion/ProductVersion`、`CHANGELOG.md` 顶部新增条目。

3. **同步用户文档**（只要用户能看到行为变化）：
   - `使用说明.txt`：给最终用户看的操作说明（会进交付包）；
   - `README.md`：项目说明（功能清单、截图、快速开始）；
   - `详情介绍.md`：架构/接口/坑有变化时才改。
4. **跑测试 + 重新打包**（见第 3 节）：`python tools\package.py` 会自动把最新的
   `使用说明.txt` / `详情介绍.md` / `CHANGELOG.md` / `AGENTS.md` 和完整源码（`app\`、`tools\`…）
   复制进 `dist\资源搜寻器\`，再打成一个 zip。

版本号规则：

| 情况 | 升法 | 例子 |
| --- | --- | --- |
| 只改文档 / 小修 | patch | 1.6.0 → 1.6.1 |
| 新功能 / 界面变化 | minor | 1.6.1 → 1.7.0 |
| 配置格式或 Agent 协议不兼容 | major | 1.7.0 → 2.0.0 |

`python tools\release.py --check` 会核对三处版本号与日志是否一致——**打包前必须跑，必须 PASS**。

---

## 2. 不能动的东西

- `APP_ID = "ResourceFinder"` 不变。3.16.0起按用户明确要求，正常运行使用程序旁 `profiles` 本地用户名存档；旧配置 `%APPDATA%\ResourceFinder` 与缓存 `%LOCALAPPDATA%\ResourceFinder\cache` 保留供兼容/可选导入，不得删除或自动覆盖。
- 用户的 `profiles` 不得进交付ZIP，不得在升级、清理、构建时删除。优先 `tools/build.py --dir --output-dir .validation/版本/staged-dist` 独立构建，再 `tools/package.py --dist-dir 同目录`；更新运行文件时保留 profiles。
- 3.34.0起构建、打包共同使用 `tools/release_safety.py`：输出不能覆盖源码/运行组件/开发环境，也不能含 `profiles`、旧配置 `data` 或 `downloads`。不得为了通过检查先删除这些目录；改用空的独立构建目录，再保留存档更新运行组件。
- 交付形态保持 **onedir**（`资源搜寻器.exe` + `_internal`）。**不要改回 onefile**：
  单文件版要先解压到 `%TEMP%`，在受限账户/安全软件下会弹
  `Could not create temporary directory!` 直接打不开（1.0.0 的真实事故）。
- 不要引入需要额外系统组件的新依赖（WebView2、Electron、数据库服务等）；保持"解压即用"。
- Agent 收件口只监听 `127.0.0.1` 且必须带 token；不要改成 `0.0.0.0`。

---

## 3. 命令速查

```powershell
# 运行
.\.venv\Scripts\python.exe main.py

# 测试（离线快；全量含联网搜索约 1 分钟）
.\.venv\Scripts\python.exe tools\uitest.py --offline
.\.venv\Scripts\python.exe tools\uitest.py
.\.venv\Scripts\python.exe tools\selftest.py --offline

# 打包前先关掉正在运行的实例，否则 _internal 的 dll 被占用会 PermissionError
Get-Process -Name "资源搜寻器" -ErrorAction SilentlyContinue | Stop-Process -Force

python tools\build.py --dir --output-dir .validation\版本\staged-dist

# 整理交付包 + 打 zip：会把 使用说明/详情介绍/CHANGELOG/AGENTS 和完整源码（app、tools…）
# 复制进独立产物目录，生成 ZIP；含存档/旧配置/下载目录时拒绝运行，不清理用户数据
python tools\package.py --dist-dir .validation\版本\staged-dist

# 解压自检
Expand-Archive "dist\资源搜寻器-vX.Y.Z-win64.zip" .zipcheck -Force
& ".zipcheck\资源搜寻器\资源搜寻器.exe" --selftest --selfout zipcheck-selftest.txt
# 10 秒后看末行；必须是 RESULT: PASS
```

---

## 4. 环境坑（都是踩过的，别再踩）

1. **PowerShell 脚本必须 UTF-8 with BOM**，否则 `powershell.exe` 5.1 按 GBK 解析中文直接报
   `Unexpected token`。用 edit 工具改完 `.ps1` 记得补 BOM：
   ```powershell
   $c = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
   [IO.File]::WriteAllText($p, $c, [Text.UTF8Encoding]::new($true))
   ```
2. **不要用 `tempfile.mkstemp` 探测目录可写性**（Windows 上会重试 23 万个名字 → 启动假死）。
   用一次性 `os.open(path, O_CREAT|O_EXCL|O_WRONLY, 0o600)`。
3. **不要用 `QTest.qWait` 等后台搜索**：它会抢 GIL 把后台线程饿死（7 秒变 37 秒）。
   用 `time.sleep(step)` + `QApplication.processEvents()`。
4. **UIA 找窗口按进程 PID 过滤**：桌面上可能有同名文件夹窗口，只按标题会抓到资源管理器。
5. **带 parent 的 Qt 模态对话框在 UIA 里是主窗口的子元素**，用 `ClassName=AgentWaitDialog` 在
   Descendants 里找；点按钮用 `InvokePattern`，别用 PostMessage（会顺手点掉默认按钮）。
6. **右键菜单**要发 `WM_CONTEXTMENU (0x007B)`，`lParam=(screenY<<16)|(screenX&0xFFFF)`；
   Qt 不响应 `WM_RBUTTONUP`。
7. **`Start-Process` 启动 GUI 子进程会卡死 pwsh**（管道句柄继承）。用 `&`（后台 job）或 `Start-Job`。
8. **打包失败 `PermissionError: [WinError 5]`** = 旧实例还在跑，先杀进程。
9. 本机 DSH 沙箱会隔离进程命名空间（`tasklist` 看不到用户开的 Agent），
   所以"本机 Agent 检测"在沙箱里不可靠 —— 1.5.0 起已不做自动检测，别加回来。

---

## 5. 交付清单（每次交付前对照）

```
dist\
├─ 资源搜寻器\                     ← 唯一的成品文件夹，整个给用户/接手方
│  ├─ 资源搜寻器.exe + _internal\
│  ├─ 使用说明.txt / 详情介绍.md / CHANGELOG.md / AGENTS.md
│  └─ app\ tools\ assets\ shots\ main.py requirements.txt README.md   ← 与 exe 同版本的源码
├─ 使用说明.txt
└─ 资源搜寻器-vX.Y.Z-win64.zip     ← 唯一的交付 zip（成品 + 文档 + 源码都在里面）
```

- [ ] `python tools\release.py --check` PASS
- [ ] `tools\uitest.py --offline` 全过
- [ ] `tools\uitest.py` 全过（联网）
- [ ] `tools\build.py --dir` 成功，VersionInfo 是新版本号
- [ ] 真机启动一次（`tools\uia-search-probe.ps1 -Query 风景` 或手动）能搜出卡片
- [ ] 解压 zip 跑 `--selftest` → `RESULT: PASS`
- [ ] 清理 `.zipcheck`、`build`、`data`、日志、`__pycache__`
- [ ] 上一项的 `data` 仅指隔离测试产生的数据，不能删除用户旧配置。历史验证、备份、旧报告与无引用截图移至工具目录外相邻的 `资源搜寻器_开发归档`，不要在工具目录累积多版完整运行文件。`profiles` 与实际下载文件始终保留，ZIP不带开发环境或测试资料。
- [ ] `present` 交付 zip + 使用说明，并在回复里逐条说明改了什么、怎么验证的

---

## 6. 出问题时先看哪里

| 症状 | 先查 |
| --- | --- |
| 搜不到结果 / 条数少 | `app/adapters/*.py`、`python tools\probe_sites.py` |
| 卡片空白/错位 | `app/ui/widgets.py` 的 `FlowLayout`、`_relayout_results` |
| Agent 不接管 / 403 | `app/agent.py` 的 token、`allow_reuse_address`、是否旧实例占用 8765 |
| 网络检测撒谎 | `app/net.py` 的 `system_proxy_state` / `trust_env` / `_ABROAD_PROBES` |
| 打包后打不开 | 是否 onedir、`_internal` 是否同目录、是否被杀软拦截 |
| 配置丢了 | `%APPDATA%\ResourceFinder\config.json` 与 exe 旁 `data\config.json` |
