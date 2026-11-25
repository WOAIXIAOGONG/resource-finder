// Windows accessibility reader for ordinary YouTube tabs. No extension, CDP,
// credential export, authentication input, browser policy or security changes.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

class BrowserReader {
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr value);
    delegate bool EnumProc(IntPtr hwnd, IntPtr value);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr address);
    static JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 4000000 };

    static string Value(AutomationElement element) {
        try {
            object pattern;
            if (element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) return ((ValuePattern)pattern).Current.Value;
            object raw = element.GetCurrentPropertyValue(ValuePattern.ValueProperty, true);
            if (raw is string) return (string)raw;
        } catch { }
        return "";
    }
    static bool YouTube(string value) {
        Uri uri;
        return Uri.TryCreate(value, UriKind.Absolute, out uri) && uri.Scheme == "https" && uri.Host == "www.youtube.com";
    }
    static bool ValidTitle(string name, string id) {
        return name.Length > 0 && (id == "video-title" || (name != "观看" && name != "Watch"));
    }
    static bool SignedIn(AutomationElement doc) {
        var buttons = doc.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
        foreach (AutomationElement button in buttons) {
            string name = button.Current.Name;
            if (button.Current.AutomationId == "avatar-btn" || name.Contains("账号菜单") || name.Contains("帳戶選單") ||
                name.IndexOf("account menu", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        }
        return false;
    }
    static string VideoId(string value) {
        if (!YouTube(value)) return "";
        Uri uri = new Uri(value);
        string id = uri.AbsolutePath == "/watch" ? HttpUtility.ParseQueryString(uri.Query)["v"] :
                    uri.AbsolutePath.StartsWith("/shorts/") ? uri.AbsolutePath.Substring(8).TrimEnd('/') : "";
        return id != null && System.Text.RegularExpressions.Regex.IsMatch(id, "^[A-Za-z0-9_-]{11}$") ? id : "";
    }
    static bool Browser(IntPtr hwnd) {
        try {
            uint pid; GetWindowThreadProcessId(hwnd, out pid);
            string name = Process.GetProcessById((int)pid).ProcessName.ToLowerInvariant();
            return name == "msedge" || name == "chrome" || name == "brave" || name == "vivaldi";
        } catch { return false; }
    }
    static AutomationElement Root(IntPtr hwnd) {
        if (!Browser(hwnd)) throw new Exception("目标浏览器窗口已关闭");
        return AutomationElement.FromHandle(hwnd);
    }
    static AutomationElement Document(IntPtr hwnd) {
        var docs = Root(hwnd).FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document));
        foreach (AutomationElement doc in docs) if (YouTube(Value(doc))) return doc;
        return null;
    }
    static AutomationElementCollection Tabs(IntPtr hwnd) {
        return Root(hwnd).FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.TabItem));
    }
    static AutomationElement Selected(IntPtr hwnd) {
        foreach (AutomationElement tab in Tabs(hwnd)) {
            object pattern;
            if (tab.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern) && ((SelectionItemPattern)pattern).Current.IsSelected) return tab;
        }
        return null;
    }
    static void Select(AutomationElement tab) {
        object pattern;
        if (tab == null || !tab.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern)) throw new Exception("工作标签页已关闭，请重新搜索");
        ((SelectionItemPattern)pattern).Select();
    }
    static string TabKey(AutomationElement tab) {
        return tab == null ? "" : String.Join(".", Array.ConvertAll(tab.GetRuntimeId(), value => value.ToString(System.Globalization.CultureInfo.InvariantCulture)));
    }
    static void SelectTarget(IntPtr hwnd, string key) {
        if (key.Length == 0) throw new Exception("浏览器标签标记缺失，请重新点击“已登录”");
        foreach (AutomationElement tab in Tabs(hwnd)) {
            if (TabKey(tab) != key) continue;
            if (TabKey(Selected(hwnd)) != key) { Select(tab); Thread.Sleep(250); }
            return;
        }
        throw new Exception("原 YouTube 标签页已关闭或移动，请重新点击“已登录”；不会另开网页");
    }
    static AutomationElement TargetDocument(IntPtr hwnd, string key) {
        if (TabKey(Selected(hwnd)) != key) throw new Exception("浏览器标签已切换，已停止读取；请保持 YouTube 页面打开后重试");
        return Document(hwnd);
    }
    static bool MatchingQuery(string url, string query) {
        return YouTube(url) && new Uri(url).AbsolutePath == "/results" && HttpUtility.ParseQueryString(new Uri(url).Query)["search_query"] == query;
    }
    static void RestoreTab(IntPtr hwnd, AutomationElement previous, string key) {
        try { if (previous != null && TabKey(previous) != key && TabKey(Selected(hwnd)) == key) Select(previous); } catch { }
    }
    static string FindSearchTab(IntPtr hwnd, string query) {
        AutomationElement previous = Selected(hwnd); string selected = "";
        try {
            foreach (AutomationElement candidate in Tabs(hwnd)) {
                if (candidate.Current.Name.IndexOf("YouTube", StringComparison.OrdinalIgnoreCase) < 0) continue;
                selected = TabKey(candidate); SelectTarget(hwnd, selected);
                AutomationElement doc = TargetDocument(hwnd, selected);
                if (doc != null && MatchingQuery(Value(doc), query)) return selected;
            }
            return "";
        } finally { RestoreTab(hwnd, previous, selected); }
    }
    static void NavigateSearch(IntPtr hwnd, string tab, string query) {
        Focus(hwnd); TargetDocument(hwnd, tab);
        Address(hwnd, "https://www.youtube.com/results?search_query=" + Uri.EscapeDataString(query));
    }
    static string PrepareSearch(IntPtr hwnd, string tab, string query, bool initial) {
        SelectTarget(hwnd, tab);
        AutomationElement doc = TargetDocument(hwnd, tab);
        string url = doc == null ? "" : Value(doc);
        if (MatchingQuery(url, query)) return tab;
        // A home/channel page may be reused for the first search. A playing video
        // or a page the user navigated to during pagination must remain intact.
        bool playing = YouTube(url) && (new Uri(url).AbsolutePath == "/watch" || new Uri(url).AbsolutePath.StartsWith("/shorts/"));
        string existing = FindSearchTab(hwnd, query);
        if (existing != "") { SelectTarget(hwnd, existing); return existing; }
        SelectTarget(hwnd, tab);
        if (initial && doc != null && !playing) {
            NavigateSearch(hwnd, tab, query); return tab;
        }
        // Only when playback/navigation displaced the search: one tab in the
        // same browser window. Never create another browser window.
        var before = new HashSet<string>();
        foreach (AutomationElement item in Tabs(hwnd)) before.Add(TabKey(item));
        Focus(hwnd); TargetDocument(hwnd, tab); Keys(hwnd, "^t");
        DateTime end = DateTime.UtcNow.AddSeconds(4);
        string created = "";
        do {
            Thread.Sleep(100); created = TabKey(Selected(hwnd));
            if (created != "" && !before.Contains(created)) break;
        } while (DateTime.UtcNow < end);
        if (created == "" || before.Contains(created)) throw new Exception("无法准备后台搜索标签页，请重试；当前视频页面已保留");
        NavigateSearch(hwnd, created, query);
        return created;
    }
    static List<object> WaitRows(IntPtr hwnd, string tab, string query) {
        DateTime end = DateTime.UtcNow.AddSeconds(35);
        int lastCount = 0, stable = 0;
        do {
            Thread.Sleep(350);
            AutomationElement doc = TargetDocument(hwnd, tab);
            if (doc != null && MatchingQuery(Value(doc), query)) {
                var rows = ReadRows(doc);
                stable = rows.Count == lastCount ? stable+1 : 0; lastCount = rows.Count;
                if (rows.Count >= 8 || (rows.Count > 0 && stable >= 3)) return rows;
            }
        } while (DateTime.UtcNow < end);
        throw new Exception("浏览器未显示可读取的视频结果。请在官网完成登录、年龄或其他验证后重试；工具不会代填这些信息。");
    }
    static void Focus(IntPtr hwnd) {
        SetForegroundWindow(hwnd); Thread.Sleep(80);
        if (GetForegroundWindow() != hwnd) throw new Exception("无法激活浏览器工作窗口，请关闭遮挡窗口后重试");
    }
    static void Keys(IntPtr hwnd, string keys) {
        if (GetForegroundWindow() != hwnd) throw new Exception("浏览器焦点已改变，已停止操作以避免影响其他窗口");
        SendKeys.SendWait(keys);
    }
    static object Scan() {
        IntPtr previousFocus = GetForegroundWindow(); IntPtr touched = IntPtr.Zero;
        IntPtr available = IntPtr.Zero; string availableTab = "";
        var windows = new List<IntPtr>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr unused) { if (IsWindowVisible(hwnd) && Browser(hwnd)) windows.Add(hwnd); return true; }, IntPtr.Zero);
        try { foreach (IntPtr hwnd in windows) {
            try {
                AutomationElement currentDoc = Document(hwnd);
                if (currentDoc != null) {
                    string key = TabKey(Selected(hwnd));
                    if (key != "" && SignedIn(currentDoc)) return new { hwnd = hwnd.ToInt64(), tab = key };
                    if (key != "" && available == IntPtr.Zero) { available = hwnd; availableTab = key; }
                }
                AutomationElement previous = Selected(hwnd);
                foreach (AutomationElement tab in Tabs(hwnd)) {
                    if (tab.Current.Name.IndexOf("YouTube", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    touched = hwnd; Select(tab); Thread.Sleep(250);
                    AutomationElement doc = Document(hwnd);
                    if (doc != null && SignedIn(doc)) {
                        if (previous != null) Select(previous);
                        return new { hwnd = hwnd.ToInt64(), tab = TabKey(tab) };
                    }
                    if (doc != null && available == IntPtr.Zero) { available = hwnd; availableTab = TabKey(tab); }
                }
                if (previous != null) Select(previous);
            } catch { }
        } } finally { if (touched != IntPtr.Zero && GetForegroundWindow() == touched && previousFocus != touched) SetForegroundWindow(previousFocus); }
        if (available != IntPtr.Zero) return new { hwnd = available.ToInt64(), tab = availableTab };
        throw new Exception("未检测到可读取的 YouTube 页面。请在 Chrome / Edge 等浏览器登录 YouTube，保持页面打开，再点击“已登录”。无需安装扩展。");
    }
    static void Address(IntPtr hwnd, string url) {
        var fields = Root(hwnd).FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
        foreach (AutomationElement field in fields) {
            string name = field.Current.Name;
            if (!(name.IndexOf("address", StringComparison.OrdinalIgnoreCase) >= 0 || name.Contains("地址") || name.Contains("網址"))) continue;
            // The native browser address bar must be outside the web document.
            AutomationElement ancestor = field; bool inDocument = false;
            for (int n = 0; n < 12 && ancestor != null; n++) {
                if (ancestor.Current.ControlType == ControlType.Document) { inDocument = true; break; }
                ancestor = TreeWalker.ControlViewWalker.GetParent(ancestor);
            }
            if (inDocument) continue;
            object pattern;
            if (!field.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) continue;
            field.SetFocus(); ((ValuePattern)pattern).SetValue(url); Keys(hwnd, "{ENTER}"); return;
        }
        throw new Exception("无法读取浏览器地址栏；当前浏览器可能不支持 Windows 页面访问接口");
    }
    static object Open(IntPtr hwnd, string tab, string query) {
        if (query.Length == 0 || query.Length > 300) throw new Exception("搜索词无效");
        IntPtr previous = GetForegroundWindow();
        AutomationElement previousTab = Selected(hwnd);
        try {
            tab = PrepareSearch(hwnd, tab, query, true);
            var rows = WaitRows(hwnd, tab, query);
            return new { rows = rows, url = Value(TargetDocument(hwnd, tab)), tab = tab, ended = false };
        } finally {
            RestoreTab(hwnd, previousTab, tab);
            if (GetForegroundWindow() == hwnd && previous != hwnd) SetForegroundWindow(previous);
        }
    }
    static List<object> ReadRows(AutomationElement doc) {
        var rows = new List<object>(); var seen = new HashSet<string>();
        var links = doc.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink));
        foreach (AutomationElement link in links) {
            string id = VideoId(Value(link)); string name = link.Current.Name;
            if (id == "" || !ValidTitle(name, link.Current.AutomationId) || seen.Contains(id)) continue;
            seen.Add(id);
            string title = link.Current.HelpText;
            if (String.IsNullOrEmpty(title)) title = name;
            var parts = new List<string>(); string author = "";
            AutomationElement group = link;
            for (int level = 0; level < 5; level++) {
                group = TreeWalker.ControlViewWalker.GetParent(group);
                if (group == null || group.Current.ControlType == ControlType.Document) break;
                var nearby = group.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink));
                bool different = false;
                foreach (AutomationElement other in nearby) {
                    string otherId = VideoId(Value(other));
                    if (otherId != "" && otherId != id) { different = true; break; }
                }
                if (different) break;
                foreach (AutomationElement other in nearby) {
                    string url = Value(other);
                    if (YouTube(url) && (url.Contains("/@") || url.Contains("/channel/"))) author = other.Current.Name;
                }
                var texts = group.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text));
                if (texts.Count > 100) break;
                var candidate = new List<string>();
                foreach (AutomationElement text in texts) if (text.Current.Name.Length < 500) candidate.Add(text.Current.Name);
                if (candidate.Count > parts.Count) parts = candidate;
            }
            rows.Add(new { id = id, title = title, label = name, text = parts, author = author });
            if (rows.Count > 500) rows.RemoveAt(0); // Keep the moving tail on very long pages.
        }
        return rows;
    }
    static object More(IntPtr hwnd, string tab, string query, string knownIds) {
        if (query.Length == 0 || query.Length > 300) throw new Exception("搜索词无效");
        var known = new HashSet<string>(knownIds.Split(','));
        IntPtr previous = GetForegroundWindow();
        AutomationElement previousTab = Selected(hwnd);
        try {
            string original = tab;
            tab = PrepareSearch(hwnd, tab, query, false);
            if (original != tab) WaitRows(hwnd, tab, query);
            AutomationElement doc = TargetDocument(hwnd, tab);
            object scrolling; ScrollPattern scroll = null;
            if (doc.TryGetCurrentPattern(ScrollPattern.Pattern, out scrolling)) scroll = (ScrollPattern)scrolling;
            if (scroll == null) {
                var containers = doc.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.IsScrollPatternAvailableProperty, true));
                foreach (AutomationElement container in containers) {
                    if (container.TryGetCurrentPattern(ScrollPattern.Pattern, out scrolling) && ((ScrollPattern)scrolling).Current.VerticallyScrollable) { scroll = (ScrollPattern)scrolling; break; }
                }
            }
            if (scroll != null && scroll.Current.VerticallyScrollable) scroll.SetScrollPercent(ScrollPattern.NoScroll, 100);
            else {
                Focus(hwnd);
                try { doc.SetFocus(); } catch {
                    AutomationElement first = doc.FindFirst(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink));
                    if (first != null) first.SetFocus();
                }
                Keys(hwnd, "^{END}");
            }
            DateTime end = DateTime.UtcNow.AddSeconds(12);
            List<object> rows;
            do {
                Thread.Sleep(450); doc = TargetDocument(hwnd, tab);
                if (doc == null || !MatchingQuery(Value(doc), query)) throw new Exception("原搜索页已改变或正在要求验证，请重新登录后继续");
                rows = ReadRows(doc);
                foreach (object row in rows) if (!known.Contains((string)row.GetType().GetProperty("id").GetValue(row, null)))
                    return new { rows = rows, url = Value(doc), tab = tab, ended = false };
            } while (DateTime.UtcNow < end);
            return new { rows = rows, url = Value(doc), tab = tab, ended = false };
        } finally {
            RestoreTab(hwnd, previousTab, tab);
            if (GetForegroundWindow() == hwnd && previous != hwnd) SetForegroundWindow(previous);
        }
    }
    static string[] Arguments(string command) {
        int count; IntPtr address=CommandLineToArgvW(command,out count);
        if (address==IntPtr.Zero) return new string[0];
        try {
            var result=new string[count];
            for (int n=0;n<count;n++) result[n]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(address,n*IntPtr.Size));
            return result;
        } finally { LocalFree(address); }
    }
    static object ManagedProfile(string directory, string owner, bool close, bool force, bool show) {
        string profile=Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar);
        if (Path.GetFileName(profile)!="youtube-browser" || owner.Length!=64 ||
            (File.GetAttributes(profile)&FileAttributes.ReparsePoint)!=0 ||
            File.ReadAllText(Path.Combine(profile,".resourcefinder-session"))!=owner)
            throw new Exception("专用浏览器会话归属不匹配，不会操作日常浏览器");
        var pids=new HashSet<uint>(); var headless=new HashSet<uint>(); var starts=new Dictionary<uint,long>(); int windows=0;
        using (var search=new ManagementObjectSearcher("SELECT ProcessId,CommandLine,CreationDate FROM Win32_Process WHERE Name='msedge.exe' OR Name='chrome.exe'")) {
            foreach (ManagementObject process in search.Get()) {
                string command=Convert.ToString(process["CommandLine"]); if (command.Length==0) continue;
                string[] arguments=Arguments(command); string value=""; bool child=false, hidden=false;
                for (int n=0;n<arguments.Length;n++) {
                    if (arguments[n].StartsWith("--type=")) child=true;
                    if (arguments[n].StartsWith("--headless")) hidden=true;
                    if (arguments[n].StartsWith("--user-data-dir=")) value=arguments[n].Substring(16);
                    else if (arguments[n]=="--user-data-dir" && n+1<arguments.Length) value=arguments[n+1];
                }
                if (child || value.Length==0) continue;
                try { if (!String.Equals(Path.GetFullPath(value).TrimEnd(Path.DirectorySeparatorChar),profile,StringComparison.OrdinalIgnoreCase)) continue; } catch { continue; }
                uint pid=Convert.ToUInt32(process["ProcessId"]);
                try {
                    long started=Process.GetProcessById((int)pid).StartTime.ToUniversalTime().Ticks;
                    long reported=ManagementDateTimeConverter.ToDateTime(Convert.ToString(process["CreationDate"])).ToUniversalTime().Ticks;
                    if (Math.Abs(started-reported)>10000) continue;
                    starts[pid]=started;
                } catch { continue; }
                pids.Add(pid); if (hidden) headless.Add(pid);
            }
        }
        EnumWindows(delegate(IntPtr hwnd,IntPtr unused) {
            uint pid;GetWindowThreadProcessId(hwnd,out pid);
            try { if (pids.Contains(pid) && IsWindowVisible(hwnd) && Browser(hwnd) && Process.GetProcessById((int)pid).StartTime.ToUniversalTime().Ticks==starts[pid]) {
                if (close) PostMessage(hwnd,0x10,IntPtr.Zero,IntPtr.Zero); windows++;
                if (show && !close && !headless.Contains(pid)) { ShowWindow(hwnd,9); SetForegroundWindow(hwnd); }
            } } catch { }
            return true;
        },IntPtr.Zero);
        if (force) foreach (uint pid in headless) {
            try { var process=Process.GetProcessById((int)pid); if (process.StartTime.ToUniversalTime().Ticks==starts[pid]) process.Kill(); } catch (ArgumentException) { }
        }
        return new { processes=pids.Count, headless=headless.Count, windows=windows };
    }
    [STAThread] static void Main(string[] args) {
        Console.InputEncoding = new System.Text.UTF8Encoding(false);
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        string line;
        while ((line = Console.ReadLine()) != null) {
            try {
                var input = json.Deserialize<Dictionary<string, object>>(line); string op = Convert.ToString(input["op"]); object data;
                if (op == "scan") data = Scan();
                else if (op == "managed_profile") data = ManagedProfile(Convert.ToString(input["profile"]),Convert.ToString(input["owner"]),input.ContainsKey("close") && Convert.ToBoolean(input["close"]),input.ContainsKey("force") && Convert.ToBoolean(input["force"]),input.ContainsKey("show") && Convert.ToBoolean(input["show"]));
                else if (op == "open") data = Open(new IntPtr(Convert.ToInt64(input["hwnd"])), Convert.ToString(input["tab"]), Convert.ToString(input["query"]));
                else if (op == "more") data = More(new IntPtr(Convert.ToInt64(input["hwnd"])), Convert.ToString(input["tab"]), Convert.ToString(input["query"]), Convert.ToString(input["known"]));
                else if (op == "selftest") data = new { ok = YouTube("https://www.youtube.com/") && !YouTube("https://www.youtube.com.evil/") && VideoId("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk" && ValidTitle("风景", "") && ValidTitle("观看", "video-title") && !ValidTitle("Watch", "") && MatchingQuery("https://www.youtube.com/results?search_query=nature+landscape", "nature landscape") && !MatchingQuery("https://www.youtube.com/watch?search_query=nature", "nature") && !MatchingQuery("https://www.youtube.com/results?search_query=other", "nature") };
                else if (op == "shutdown") data = new { closed = true }; // User-owned tabs must remain open.
                else throw new Exception("不支持的浏览器操作");
                Console.WriteLine(json.Serialize(new { ok = true, data = data }));
            } catch (Exception error) { Console.WriteLine(json.Serialize(new { ok = false, error = error.GetType().Name+": "+error.Message })); }
        }
    }
}
