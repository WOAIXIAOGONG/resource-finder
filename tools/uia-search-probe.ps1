# -*- coding: utf-8 -*-
# 假定「资源搜寻器」窗口已经在运行：切直接搜索 -> 填词 -> 点开始搜索 -> 等完成 -> 截图 -> 验证卡片存在
param(
    [string]$Title = "资源搜寻器",
    [string]$Query = "风景",
    [string]$Out = "shots\exe-search.png",
    [int]$WaitSeconds = 90
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing

$src = @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
public class SearchCap {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref POINT p);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  public static void Grab(IntPtr h, string path) {
    SetProcessDPIAware(); ShowWindow(h, 9); SetForegroundWindow(h);
    System.Threading.Thread.Sleep(600);
    RECT r; GetWindowRect(h, out r);
    int w = r.Right - r.Left, ht = r.Bottom - r.Top;
    if (w < 200 || ht < 200) throw new Exception("window rect too small: " + w + "x" + ht);
    using (Bitmap b = new Bitmap(w, ht, PixelFormat.Format24bppRgb))
    using (Graphics g = Graphics.FromImage(b)) {
      IntPtr d = g.GetHdc(); PrintWindow(h, d, 2); g.ReleaseHdc(d);
      b.Save(path, ImageFormat.Png);
    }
  }
  public static void ClickClient(IntPtr h, int x, int y) {
    POINT p; p.X = x; p.Y = y; ClientToScreen(h, ref p);
    IntPtr lp = (IntPtr)((y << 16) | (x & 0xFFFF));
    PostMessage(h, 0x0200, IntPtr.Zero, lp);   // WM_MOUSEMOVE
    System.Threading.Thread.Sleep(60);
    PostMessage(h, 0x0201, (IntPtr)1, lp);     // WM_LBUTTONDOWN
    System.Threading.Thread.Sleep(60);
    PostMessage(h, 0x0202, IntPtr.Zero, lp);   // WM_LBUTTONUP
  }
}
'@
Add-Type -TypeDefinition $src -ReferencedAssemblies System.Drawing

$AE = [System.Windows.Automation.AutomationElement]
$Scope = [System.Windows.Automation.TreeScope]
$root = $AE::RootElement
$win = $null
# 只认「资源搜寻器」进程的窗口：桌面上可能还有同名文件夹的资源管理器窗口
$pids = @(Get-Process -Name "资源搜寻器" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
"pids: $($pids -join ',')"
$topAll = $root.FindAll($Scope::Children, [System.Windows.Automation.Condition]::TrueCondition)
foreach ($w in $topAll) {
    try {
        if ($w.Current.NativeWindowHandle -eq 0) { continue }
        if ($pids -contains $w.Current.ProcessId -and $w.Current.Name -like "$Title*") {
            $win = $w; break
        }
    } catch {}
}
if ($win -eq $null) { throw "找不到窗口 $Title" }
$hwnd = [IntPtr]$win.Current.NativeWindowHandle
"window: $($win.Current.Name) hwnd=$hwnd"

function Find-ByName($rootEl, $name) {
    $c = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $name)
    return $rootEl.FindFirst($Scope::Descendants, $c)
}

# 切到直接搜索
$directBtn = Find-ByName $win "直接搜索"
if ($directBtn -ne $null) { $directBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 500 }

$searchEdit = Find-ByName $win "搜索内容"
if ($searchEdit -eq $null) { throw "找不到搜索框" }
$searchEdit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($Query)
"typed: $Query"

$go = Find-ByName $win "开始搜索"
if ($go -eq $null) { throw "找不到「开始搜索」" }
$go.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
"clicked search"

$status = ""
$ok = $false
for ($i = 0; $i -lt ($WaitSeconds * 2); $i++) {
    Start-Sleep -Milliseconds 500
    # 搜索时卡片在不停增删，UIA 的 FindAll 偶尔会抛 E_FAIL(COMException)——
    # 这是 UIA 自己的时序问题，重试即可，不要当成失败退出。
    try {
        $condText = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)
        $texts = $win.FindAll($Scope::Descendants, $condText)
    } catch {
        continue
    }
    foreach ($t in $texts) {
        try { $n = $t.Current.Name } catch { continue }
        if ($n -like "完成：*") { $status = $n; $ok = $true; break }
    }
    if ($ok) { break }
}
"status: $status"
[SearchCap]::Grab($hwnd, (Join-Path (Get-Location) $Out))
"saved: $Out"

if (-not $ok) { "RESULT: FAIL（没等到「完成：」）"; exit 1 }

# 搜索完成且卡片存在才算通过；点击/复制由隔离 UI 测试覆盖。
$condCard = New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, "ResultCard")
$cards = $win.FindAll($Scope::Descendants, $condCard)
"cards in tree: $($cards.Count)"
if ($cards.Count -eq 0) { "RESULT: FAIL（没有卡片）"; exit 1 }
"RESULT: PASS"
