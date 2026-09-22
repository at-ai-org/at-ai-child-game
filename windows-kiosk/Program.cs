using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
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

        internal KioskForm()
        {
            Text = "少儿 AI 游戏乐园";
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
