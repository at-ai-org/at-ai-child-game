using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace KidGameKiosk
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try { Application.SetHighDpiMode(HighDpiMode.PerMonitorV2); } catch { /* 老系统忽略 */ }
            Application.Run(new KioskForm());
        }
    }

    /// <summary>
    /// 儿童游戏 kiosk 宿主：无边框全屏窗口 + WebView2 加载本地游戏文件。
    ///
    /// 设计要点：
    ///  - 全屏是「窗口级」的（不是网页的 Fullscreen API），所以游戏里主页↔游戏页
    ///    来回跳转都不会掉出全屏 —— 这正是浏览器方案解决不了的问题。
    ///  - 用虚拟主机映射（http://game.local/）而不是 file://，这样 localStorage
    ///    才可用（hanzi-quest 的图形提示开关、mba-english 的进度都依赖它）。
    ///  - Alt+F4 被拦住；家长用 Ctrl+Shift+Q 退出。
    /// </summary>
    internal sealed class KioskForm : Form
    {
        // ---- 家长退出热键：Ctrl+Shift+Q（全局热键，不受 WebView2 焦点影响）----
        private const int WM_HOTKEY = 0x0312;
        private const int HOTKEY_ID = 0x4B47;
        private const uint MOD_CONTROL = 0x0002;
        private const uint MOD_SHIFT = 0x0004;
        private const uint VK_Q = 0x51;

        [DllImport("user32.dll")]
        private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

        [DllImport("user32.dll")]
        private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

        /// <summary>虚拟主机名：给本地文件一个正常的 http 源。</summary>
        private const string VHost = "game.local";

        private readonly WebView2 _web = new WebView2();
        private bool _allowExit;          // 只有家长热键能置 true

        // ---- 版本更新 ----
        private CoreWebView2 _core;
        private string _wwwRoot;
        private VersionInfo _localVer;    // 本地内容版本（www\version.json）
        private VersionInfo _remoteVer;   // Gitee 上的版本
        private VersionInfo _stagedVer;   // 已下载待重启生效的版本
        private string _appliedNote;      // 本次启动刚应用了暂存更新时记一句
        private string _stateName = "idle";
        private string _stateMsg = "";
        private int _percent;
        private bool _busy;               // 正在检查/下载，避免重复触发
        private bool _autoChecked;        // 每次启动只自动检查一次

        internal KioskForm()
        {
            Text = "少儿 AI 游戏乐园";
            Icon = LoadAppIcon();                     // 任务栏 / Alt-Tab 用的就是它
            FormBorderStyle = FormBorderStyle.None;   // 无标题栏、无边框
            WindowState = FormWindowState.Maximized;  // 铺满屏幕
            TopMost = true;                           // 不被打扰
            KeyPreview = true;
            BackColor = Color.FromArgb(20, 26, 46);
            StartPosition = FormStartPosition.CenterScreen;

            _web.Dock = DockStyle.Fill;
            _web.DefaultBackgroundColor = Color.FromArgb(20, 26, 46);
            Controls.Add(_web);
        }

        /// <summary>
        /// 窗口图标（任务栏按钮、Alt-Tab 切换器、对话框标题栏都用它）。
        ///
        /// 为什么还要单独来这一手：csproj 里的 &lt;ApplicationIcon&gt; 只决定「exe 文件在
        /// 资源管理器里长什么样」，管不到运行起来的窗口——WinForms 窗体如果没显式设 Icon，
        /// 任务栏和 Alt-Tab 显示的是 .NET 自带的那个通用窗体图标。
        /// 这里把 app.ico 作为嵌入资源读出来直接给窗体，顺便保留 16/24/32/48/128/256
        /// 多个尺寸，任务栏小图标和高 DPI 下的 Alt-Tab 大图标都清晰。
        /// </summary>
        private static Icon LoadAppIcon()
        {
            try
            {
                using (Stream s = typeof(KioskForm).Assembly
                    .GetManifestResourceStream("KidGameKiosk.app.ico"))
                {
                    if (s != null) return new Icon(s);
                }
            }
            catch { /* 读不到就试下面的兜底 */ }

            try
            {
                // 兜底：直接从 exe 文件里抠一个（单文件发布时 ProcessPath 就是 exe 本身）
                string exe = Environment.ProcessPath;
                if (!string.IsNullOrEmpty(exe)) return Icon.ExtractAssociatedIcon(exe);
            }
            catch { }

            return null;   // 实在拿不到就用系统默认图标，不影响运行
        }

        // ------------------------------------------------------------------
        // 启动
        // ------------------------------------------------------------------
        protected override async void OnLoad(EventArgs e)
        {
            base.OnLoad(e);

            string wwwRoot = ResolveWwwRoot();
            if (wwwRoot == null)
            {
                MessageBox.Show(
                    "找不到游戏文件。\n\n请把游戏文件放在本程序同级的 www 文件夹里，\n" +
                    "也就是要让这个文件存在：\n<程序目录>\\www\\index.html",
                    "启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                _allowExit = true;
                Close();
                return;
            }

            _wwwRoot = wwwRoot;

            // 上次会话下载好的更新，趁 WebView2 还没起来（www 里的文件没被占用）先落地。
            // 这一步必须在创建 WebView2 之前做，否则目录被占用就换不动了。
            try { _appliedNote = UpdateService.TryApplyStagedUpdate(AppContext.BaseDirectory); }
            catch (Exception ex) { _appliedNote = "更新失败：" + ex.Message; }

            try
            {
                // 用户数据放在程序目录下，存档（localStorage）跟着程序走
                string userData = Path.Combine(AppContext.BaseDirectory, "data");
                Directory.CreateDirectory(userData);

                CoreWebView2Environment env =
                    await CoreWebView2Environment.CreateAsync(null, userData);
                await _web.EnsureCoreWebView2Async(env);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "WebView2 运行时初始化失败。\n\n" +
                    "Windows 10/11 一般自带 WebView2 运行时；如果这台机器没有，\n" +
                    "请安装「Microsoft Edge WebView2 Runtime」后重试。\n\n" +
                    "错误信息：" + ex.Message,
                    "启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                _allowExit = true;
                Close();
                return;
            }

            CoreWebView2 core = _web.CoreWebView2;
            // 下面这段如果抛异常（比如目录不可读），async void 里会静默崩溃，所以统一兜住
            try
            {

                // ---- 关掉一切可能「跑出去」或让画面变乱的功能 ----
                CoreWebView2Settings s = core.Settings;
                Set(() => s.AreDevToolsEnabled = false);                 // F12 开发者工具
                Set(() => s.AreDefaultContextMenusEnabled = false);      // 右键菜单
                Set(() => s.AreBrowserAcceleratorKeysEnabled = false);   // Ctrl+W / F5 / Ctrl+P 等
                Set(() => s.IsZoomControlEnabled = false);               // Ctrl+滚轮缩放
                Set(() => s.IsStatusBarEnabled = false);                 // 左下角状态条
                Set(() => s.IsSwipeNavigationEnabled = false);           // 触屏侧滑前进后退
                Set(() => s.IsPasswordAutosaveEnabled = false);
                Set(() => s.IsGeneralAutofillEnabled = false);

                // ---- 虚拟主机映射：把本地文件夹映射成 http://game.local/ ----
                core.SetVirtualHostNameToFolderMapping(
                    VHost, wwwRoot, CoreWebView2HostResourceAccessKind.Allow);

                // 不允许弹新窗口（即使将来页面里加了 window.open 也留在应用内）
                core.NewWindowRequested += (_, ev) =>
                {
                    ev.Handled = true;
                    if (!string.IsNullOrEmpty(ev.Uri)) core.Navigate(ev.Uri);
                };

                // 只允许在本机上跑，导航到外部地址一律拦下（防误触跳出）
                core.NavigationStarting += (_, ev) =>
                {
                    if (!IsInternal(ev.Uri)) ev.Cancel = true;
                };

                // 渲染进程崩溃时自动恢复，避免孩子看到白屏
                core.ProcessFailed += (_, ev) =>
                {
                    if (ev.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
                    {
                        _allowExit = true;
                        MessageBox.Show("游戏进程意外退出，请重新打开。", "提示",
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
                        Close();
                    }
                    else
                    {
                        try { core.Reload(); } catch { /* 忽略 */ }
                    }
                };

                // ---- 版本更新：本地版本 + 已暂存的更新 ----
                _core = core;
                _localVer = UpdateService.ReadLocalVersion(wwwRoot);
                _stagedVer = ReadStagedVersion(AppContext.BaseDirectory);

                // 页面通过 window.chrome.webview.postMessage({cmd:'...'}) 发指令过来
                core.WebMessageReceived += (_, ev) => OnPageMessage(ev.WebMessageAsJson);

                // 页面（含游戏页往返）每次加载完都把状态推一遍，顺便做一次启动自动检查
                core.NavigationCompleted += (_, __) =>
                {
                    SendInitialState();
                    if (!_autoChecked)
                    {
                        _autoChecked = true;
                        _ = CheckForUpdateAsync();
                    }
                };

                core.Navigate("http://" + VHost + "/index.html");
                Activate();
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "初始化游戏界面时出错。\n\n" + ex.Message,
                    "启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                _allowExit = true;
                Close();
            }
        }

        /// <summary>把 URL 限制在虚拟主机内（about:blank 放行，WebView2 内部会用到）。</summary>
        private static bool IsInternal(string uri)
        {
            if (string.IsNullOrEmpty(uri)) return true;
            if (uri.StartsWith("about:", StringComparison.OrdinalIgnoreCase)) return true;
            if (uri.StartsWith("data:", StringComparison.OrdinalIgnoreCase)) return true;
            if (uri.StartsWith("blob:", StringComparison.OrdinalIgnoreCase)) return true;
            return uri.StartsWith("http://" + VHost + "/", StringComparison.OrdinalIgnoreCase)
                || uri.Equals("http://" + VHost, StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>兼容不同版本的 WebView2 设置项，个别项不存在时忽略即可。</summary>
        private static void Set(Action apply)
        {
            try { apply(); } catch { /* 该版本没有这个设置项，跳过 */ }
        }

        /// <summary>找游戏文件：优先程序目录下的 www\，其次程序目录本身。</summary>
        private static string ResolveWwwRoot()
        {
            string baseDir = AppContext.BaseDirectory;
            string www = Path.Combine(baseDir, "www");
            if (File.Exists(Path.Combine(www, "index.html"))) return www;
            if (File.Exists(Path.Combine(baseDir, "index.html"))) return baseDir;
            return null;
        }

        // ------------------------------------------------------------------
        // 版本更新（页面 ↔ 宿主）
        //
        // 页面发过来的指令（window.chrome.webview.postMessage）：
        //   {cmd:'get-state'}  取当前状态
        //   {cmd:'check'}      立即检查（家长点了"检查更新"）
        //   {cmd:'update'}     开始下载并暂存新版本
        //   {cmd:'restart'}    立刻重启（让暂存的更新落地）
        // 宿主推给页面的消息统一是 {type:'update', state:..., ...}：
        //   state = idle / checking / uptodate / available / downloading
        //           / staged / need-host / error / just-updated
        // ------------------------------------------------------------------
        private void OnPageMessage(string json)
        {
            string cmd = "";
            try
            {
                using (JsonDocument doc = JsonDocument.Parse(json))
                {
                    JsonElement e;
                    if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                        doc.RootElement.TryGetProperty("cmd", out e) &&
                        e.ValueKind == JsonValueKind.String)
                    {
                        cmd = e.GetString() ?? "";
                    }
                }
            }
            catch { /* 不是我们认识的消息，忽略 */ }

            try
            {
                switch (cmd)
                {
                    case "get-state": PostUpdateState(); break;
                    case "check": _ = CheckForUpdateAsync(); break;
                    case "update": _ = StartUpdateAsync(); break;
                    case "restart": RestartNow(); break;
                }
            }
            catch { /* 任何异常都不能影响孩子玩游戏 */ }
        }

        /// <summary>把状态推给页面。页面还没加载好时 WebView2 会直接丢弃，页面加载后会主动来取。</summary>
        private void PostUpdateState()
        {
            if (_core == null) return;
            try
            {
                string json = JsonSerializer.Serialize(new
                {
                    type = "update",
                    state = _stateName,
                    message = _stateMsg,
                    host = UpdateService.HostVersion(),
                    local = _localVer != null ? _localVer.Version : "",
                    remote = _remoteVer != null ? _remoteVer.Version : "",
                    notes = _remoteVer != null ? _remoteVer.Notes : "",
                    date = _remoteVer != null ? _remoteVer.Date : "",
                    minHost = _remoteVer != null ? _remoteVer.MinHost : "",
                    staged = _stagedVer != null ? _stagedVer.Version : "",
                    applied = _appliedNote ?? "",
                    percent = _percent
                });
                _core.PostWebMessageAsJson(json);
            }
            catch { /* 忽略 */ }
        }

        private void SetState(string state, string message, int percent)
        {
            _stateName = state;
            _stateMsg = message ?? "";
            _percent = percent;
            PostUpdateState();
        }

        /// <summary>页面每次加载完先报一次状态（含"刚更新完"和"有新版本待重启"）。</summary>
        private void SendInitialState()
        {
            if (!string.IsNullOrEmpty(_appliedNote))
            {
                string note = _appliedNote;
                _appliedNote = null;      // 只提示一次，之后从游戏页返回主页不该再弹
                if (note.StartsWith("更新失败", StringComparison.Ordinal))
                    SetState("error", note, 0);
                else
                    SetState("just-updated", "已更新到 " + (_localVer != null ? _localVer.Version : "") +
                                             "（" + note + "）", 0);
                return;
            }
            if (_stagedVer != null)
            {
                SetState("staged", "新版本 " + _stagedVer.Version + " 已下载好，重启后生效", 100);
                return;
            }
            PostUpdateState();
        }

        /// <summary>检查 Gitee 上的版本。启动时自动跑一次，家长也可以手动点。</summary>
        private async Task CheckForUpdateAsync()
        {
            if (_busy || _core == null) return;
            _busy = true;
            try
            {
                SetState("checking", "正在检查更新…", 0);

                if (_localVer == null) _localVer = UpdateService.ReadLocalVersion(_wwwRoot);
                VersionInfo remote = await UpdateService.FetchRemoteVersionAsync(15000);
                _remoteVer = remote;

                string localVer = _localVer != null ? _localVer.Version : "0.0.0";
                if (!VersionUtil.IsNewer(remote.Version, localVer))
                {
                    SetState("uptodate", "已经是最新版本（" + localVer + "）", 0);
                    return;
                }
                if (!string.IsNullOrEmpty(remote.MinHost) &&
                    VersionUtil.IsNewer(remote.MinHost, UpdateService.HostVersion()))
                {
                    SetState("need-host",
                        "新内容需要更新的客户端程序（需要 " + remote.MinHost +
                        "，当前 " + UpdateService.HostVersion() + "），请到仓库下载新的 exe", 0);
                    return;
                }
                if (_stagedVer != null)
                {
                    SetState("staged", "新版本 " + _stagedVer.Version + " 已下载好，重启后生效", 100);
                    return;
                }
                SetState("available", "发现新版本 " + remote.Version, 0);
            }
            catch (Exception ex)
            {
                SetState("error", "检查更新失败：" + ex.Message, 0);
            }
            finally { _busy = false; }
        }

        /// <summary>下载并暂存新版本；替换 www 留到下次启动（那时目录没被占用）。</summary>
        private async Task StartUpdateAsync()
        {
            if (_busy || _core == null) return;
            _busy = true;
            try
            {
                SetState("downloading", "正在下载新版本…", 0);

                // Progress 在 UI 线程构造，回调会自动回到 UI 线程
                Progress<UpdateProgress> prog = new Progress<UpdateProgress>(p =>
                {
                    string phase = p.Phase == "download" ? "正在下载新版本…"
                                 : p.Phase == "waiting" ? "Gitee 正在后台打包这个版本，稍等几秒…"
                                 : p.Phase == "extract" ? "正在解压…" : "正在校验…";
                    int pct = p.Phase == "download" ? p.Percent : (p.Phase == "waiting" ? 0 : 100);
                    SetState("downloading", phase, pct);
                });

                VersionInfo staged = await UpdateService.StageAsync(
                    AppContext.BaseDirectory, prog, CancellationToken.None);

                _stagedVer = staged;
                SetState("staged", "新版本 " + staged.Version + " 已下载好，重启后生效", 100);
            }
            catch (Exception ex)
            {
                SetState("error", "更新失败：" + ex.Message, 0);
            }
            finally { _busy = false; }
        }

        /// <summary>立刻重启程序，让暂存的更新在启动时落地。</summary>
        private void RestartNow()
        {
            if (UpdateService.RestartSelf())
            {
                _allowExit = true;
                Close();
            }
            else
            {
                SetState("staged", "自动重启失败，请按 Ctrl+Shift+Q 退出后重新打开", 100);
            }
        }

        /// <summary>读 update\staged.json（上次下载好、还没重启生效的版本）。</summary>
        private static VersionInfo ReadStagedVersion(string baseDir)
        {
            try
            {
                string marker = Path.Combine(UpdateService.UpdateRoot(baseDir), "staged.json");
                if (!File.Exists(marker)) return null;
                return UpdateService.Parse(File.ReadAllText(marker, Encoding.UTF8));
            }
            catch { return null; }
        }

        // ------------------------------------------------------------------
        // 防退出：Alt+F4 / 关闭按钮 一律拦住，只有家长热键能退
        // ------------------------------------------------------------------
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!_allowExit && e.CloseReason != CloseReason.WindowsShutDown
                             && e.CloseReason != CloseReason.TaskManagerClosing)
            {
                e.Cancel = true;   // 孩子按 Alt+F4 关不掉
                return;
            }
            base.OnFormClosing(e);
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            try { RegisterHotKey(Handle, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT, VK_Q); }
            catch { /* 注册失败不影响游戏，只是少了家长退出热键 */ }
        }

        protected override void OnHandleDestroyed(EventArgs e)
        {
            try { UnregisterHotKey(Handle, HOTKEY_ID); } catch { /* 忽略 */ }
            base.OnHandleDestroyed(e);
        }

        /// <summary>Ctrl+Shift+Q：家长退出。</summary>
        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_HOTKEY && m.WParam.ToInt32() == HOTKEY_ID)
            {
                _allowExit = true;
                Close();
                return;
            }
            base.WndProc(ref m);
        }

        /// <summary>兜底：窗口获得焦点时把 Esc / F11 吞掉。</summary>
        protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
        {
            if (keyData == Keys.Escape || keyData == Keys.F11) return true;
            return base.ProcessCmdKey(ref msg, keyData);
        }
    }
}
