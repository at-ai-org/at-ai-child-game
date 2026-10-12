using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace KidGameKiosk
{
    /// <summary>一份版本描述（远端 version.json 与本地 www\version.json 共用同一格式）。</summary>
    internal sealed class VersionInfo
    {
        public string Version = "";
        public string Date = "";
        public string Notes = "";
        public string MinHost = "";

        public bool IsUsable
        {
            get { return !string.IsNullOrWhiteSpace(Version); }
        }

        public override string ToString()
        {
            return IsUsable ? Version : "(未知)";
        }
    }

    /// <summary>更新进度，回传给页面显示进度条。</summary>
    internal sealed class UpdateProgress
    {
        public string Phase = "";      // download / extract / verify
        public int Percent;            // 0-100
        public long Bytes;
        public long TotalBytes;
    }

    /// <summary>
    /// 版本比较：按数字段比大小，支持 1.2 / 1.2.3 / 1.2.3.4。
    /// 解析不出来时退化成字符串比较，绝不抛异常（版本号是外部输入）。
    /// </summary>
    internal static class VersionUtil
    {
        /// <summary>a 比 b 新返回正数，旧返回负数，相同返回 0。</summary>
        public static int Compare(string a, string b)
        {
            int[] pa = Parse(a);
            int[] pb = Parse(b);
            if (pa == null || pb == null)
                return string.CompareOrdinal(a ?? "", b ?? "");
            int n = Math.Max(pa.Length, pb.Length);
            for (int i = 0; i < n; i++)
            {
                int x = i < pa.Length ? pa[i] : 0;
                int y = i < pb.Length ? pb[i] : 0;
                if (x != y) return x > y ? 1 : -1;
            }
            return 0;
        }

        public static bool IsNewer(string candidate, string baseline)
        {
            return Compare(candidate, baseline) > 0;
        }

        private static int[] Parse(string s)
        {
            if (s == null) return null;
            s = s.Trim();
            // 空版本号按 0.0.0 处理：「本地没有任何版本信息」本来就该被任何版本取代
            if (s.Length == 0) return new int[] { 0 };
            string[] segs = s.Split('.');
            int[] parts = new int[segs.Length];
            for (int i = 0; i < segs.Length; i++)
            {
                string seg = segs[i].Trim();
                // 容忍 "1.2.0-beta" 这种后缀：只取前导数字
                int cut = 0;
                while (cut < seg.Length && char.IsDigit(seg[cut])) cut++;
                if (cut == 0) return null;
                int v;
                if (!int.TryParse(seg.Substring(0, cut), NumberStyles.None, CultureInfo.InvariantCulture, out v))
                    return null;
                parts[i] = v;
            }
            return parts;
        }
    }

    /// <summary>
    /// 版本更新：从 Gitee 仓库检查新版本、下载归档、暂存内容，并在下次启动时替换 www。
    ///
    /// 为什么「暂存 + 下次启动替换」而不是当场替换：
    ///  - 运行中 WebView2 正从 www 目录读文件，Windows 上目录被占用时改不动；
    ///  - 启动时（WebView2 还没创建）整个目录是空闲的，改名既能秒完成、又能保留回滚余地。
    /// 所以流程是：本次会话下载并解压到 update\content → 重启 → 启动时把 www 换成新内容。
    ///
    /// 这个类刻意不依赖 WinForms / WebView2，方便单独编译出来跑逻辑测试。
    /// </summary>
    internal static class UpdateService
    {
        public const string RepoUrl = "https://gitee.com/zhoul.vic/at-ai-child-game";
        public const string VersionUrl = RepoUrl + "/raw/master/version.json";
        public const string ArchiveUrl = RepoUrl + "/repository/archive/master.zip";

        /// <summary>
        /// Gitee 的两个接口对 User-Agent 的要求**刚好相反**（实测，别想着合并成一个）：
        ///  - /raw/...（version.json）：要像浏览器。用 curl 那类 UA 会被拦，
        ///    返回「The content may contain violation information」
        ///  - /repository/archive/master.zip：要像 curl。用浏览器 UA 会被当成人在浏览网页，
        ///    返回一个「预计等待时间 &lt;3s」的打包中页面，而不是 zip 文件
        ///    （实测 UA 里带 "curl" 字样就会直接给 zip；python-requests / git / 无 UA 都拿不到）
        /// </summary>
        private const string VersionUserAgent =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/120.0.0.0 Safari/537.36 KidGameKiosk";

        private const string ArchiveUserAgent = "curl/8.7.1 (KidGameKiosk)";

        /// <summary>归档下载重试次数与间隔（Gitee 按需打包，通常等几秒就好）。</summary>
        private const int ArchiveAttempts = 4;
        private const int ArchiveRetryDelayMs = 4000;

        /// <summary>打包/更新时都跳过这些顶层项（和 build.ps1 保持一致）。</summary>
        private static readonly string[] SkipTopLevel =
        {
            ".git", ".vscode", ".history", ".gitee-token", ".env",
            "windows-kiosk", "tools", "update", "_draft"
        };

        /// <summary>不发布到 www 的文件名（本地临时/备份文件）。</summary>
        private static readonly string[] SkipFileNames = { ".DS_Store", "index-3d-backup.html" };

        private static readonly HttpClient Http = CreateClient();

        private static HttpClient CreateClient()
        {
            HttpClientHandler handler = new HttpClientHandler();
            try { handler.AutomaticDecompression = System.Net.DecompressionMethods.GZip | System.Net.DecompressionMethods.Deflate; }
            catch { /* 个别环境不支持，忽略 */ }
            HttpClient c = new HttpClient(handler);
            c.Timeout = Timeout.InfiniteTimeSpan;   // 超时统一用 CancellationToken 控制
            return c;
        }

        // ------------------------------------------------------------------
        // 版本读取
        // ------------------------------------------------------------------

        /// <summary>读本地内容版本（www\version.json）。没有该文件的老版本返回 null。</summary>
        public static VersionInfo ReadLocalVersion(string wwwRoot)
        {
            if (string.IsNullOrEmpty(wwwRoot)) return null;
            try
            {
                string file = Path.Combine(wwwRoot, "version.json");
                if (!File.Exists(file)) return null;
                return Parse(File.ReadAllText(file, Encoding.UTF8));
            }
            catch { return null; }
        }

        /// <summary>读远端版本（Gitee 上的 version.json）。失败抛异常，由调用方兜住。</summary>
        public static async Task<VersionInfo> FetchRemoteVersionAsync(int timeoutMs)
        {
            return await FetchRemoteVersionAsync(VersionUrl, timeoutMs).ConfigureAwait(false);
        }

        /// <summary>同上，但可以指定地址（自测时指向本地假服务器）。</summary>
        public static async Task<VersionInfo> FetchRemoteVersionAsync(string url, int timeoutMs)
        {
            string full = url + (url.IndexOf('?') >= 0 ? "&" : "?") + "t=" +
                          DateTime.UtcNow.Ticks.ToString(CultureInfo.InvariantCulture);
            using (CancellationTokenSource cts = new CancellationTokenSource(timeoutMs))
            using (HttpRequestMessage req = new HttpRequestMessage(HttpMethod.Get, full))
            {
                req.Headers.TryAddWithoutValidation("User-Agent", VersionUserAgent);
                req.Headers.TryAddWithoutValidation("Cache-Control", "no-cache");
                using (HttpResponseMessage resp = await Http.SendAsync(req, cts.Token).ConfigureAwait(false))
                {
                    resp.EnsureSuccessStatusCode();
                    string text = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
                    VersionInfo v = Parse(text);
                    if (v == null || !v.IsUsable)
                        throw new InvalidDataException("远端 version.json 内容不合法");
                    return v;
                }
            }
        }

        /// <summary>解析 version.json；格式不对返回 null。</summary>
        public static VersionInfo Parse(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return null;
            // 去 BOM：version.json 用记事本存过、或从 HTTP 直接读回来时都可能带
            if (json.Length > 0 && json[0] == '\uFEFF') json = json.Substring(1);
            try
            {
                using (JsonDocument doc = JsonDocument.Parse(json, new JsonDocumentOptions
                {
                    AllowTrailingCommas = true,
                    CommentHandling = JsonCommentHandling.Skip
                }))
                {
                    JsonElement root = doc.RootElement;
                    if (root.ValueKind != JsonValueKind.Object) return null;
                    VersionInfo v = new VersionInfo();
                    v.Version = GetString(root, "version");
                    v.Date = GetString(root, "date");
                    v.Notes = GetString(root, "notes");
                    v.MinHost = GetString(root, "minHost");
                    return v.IsUsable ? v : null;
                }
            }
            catch { return null; }
        }

        private static string GetString(JsonElement obj, string name)
        {
            JsonElement e;
            if (!obj.TryGetProperty(name, out e)) return "";
            return e.ValueKind == JsonValueKind.String ? (e.GetString() ?? "") : "";
        }

        // ------------------------------------------------------------------
        // 暂存更新（下载 + 解压 + 校验）
        // ------------------------------------------------------------------

        /// <summary>更新目录（exe 同级的 update\）。</summary>
        public static string UpdateRoot(string baseDir)
        {
            return Path.Combine(baseDir, "update");
        }

        /// <summary>把远端内容下载并暂存到 update\content，等下次启动替换 www。</summary>
        public static async Task<VersionInfo> StageAsync(
            string baseDir, IProgress<UpdateProgress> progress, CancellationToken ct)
        {
            return await StageAsync(baseDir, ArchiveUrl, progress, ct).ConfigureAwait(false);
        }

        /// <summary>同上，但可以指定归档地址（自测时指向本地假服务器）。</summary>
        public static async Task<VersionInfo> StageAsync(
            string baseDir, string archiveUrl, IProgress<UpdateProgress> progress, CancellationToken ct)
        {
            string updateRoot = UpdateRoot(baseDir);
            string archive = Path.Combine(updateRoot, "archive.zip");
            string extractRoot = Path.Combine(updateRoot, "extract");
            string contentDir = Path.Combine(updateRoot, "content");

            // 目录不可写是最常见的失败原因，先探一下，给出人话提示
            try
            {
                Directory.CreateDirectory(updateRoot);
                string probe = Path.Combine(updateRoot, "write-test.tmp");
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
            }
            catch (Exception ex)
            {
                throw new IOException(
                    "程序目录不可写，没法自动更新。请把整个文件夹搬到可写的位置（例如桌面）再试。" +
                    "（" + ex.Message + "）", ex);
            }

            // 清掉上一次没做完的暂存，避免半成品被当成新内容
            SafeDeleteDir(extractRoot);
            SafeDeleteDir(contentDir);
            SafeDeleteFile(archive);

            // ---- 1. 下载归档 ----
            await DownloadArchiveAsync(archiveUrl, archive, progress, ct).ConfigureAwait(false);

            // ---- 2. 解压 ----
            Report(progress, "extract", 0, 0, 0);
            Directory.CreateDirectory(extractRoot);
            await Task.Run(delegate { ZipFile.ExtractToDirectory(archive, extractRoot); }, ct).ConfigureAwait(false);

            // ---- 3. 找到解压出来的顶层目录（归档里是 at-ai-child-game-master\）----
            Report(progress, "verify", 0, 0, 0);
            string srcRoot = FindSingleTopDirectory(extractRoot);
            if (srcRoot == null) throw new InvalidDataException("归档里没有找到仓库目录");

            // ---- 4. 按 build.ps1 同样的规则，把内容铺成 www 的结构 ----
            CopyContent(srcRoot, contentDir);

            // ---- 5. 校验：必须有 index.html，且版本确实比本地新 ----
            if (!File.Exists(Path.Combine(contentDir, "index.html")))
                throw new InvalidDataException("归档里缺少 index.html，可能下载不完整");
            VersionInfo staged = ReadLocalVersion(contentDir);
            if (staged == null)
                throw new InvalidDataException("归档里缺少合法的 version.json");

            string lockText = JsonSerializer.Serialize(new
            {
                version = staged.Version,
                date = staged.Date,
                notes = staged.Notes,
                minHost = staged.MinHost,
                stagedAt = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
            });
            File.WriteAllText(Path.Combine(updateRoot, "staged.json"), lockText, new UTF8Encoding(false));

            // 归档和中间目录都清掉，只留 content（省空间）
            SafeDeleteFile(archive);
            SafeDeleteDir(extractRoot);

            return staged;
        }

        /// <summary>
        /// 下载仓库归档。
        ///
        /// Gitee 的归档是「按需打包」的：刚 push 过的新提交，第一次请求
        /// /repository/archive/master.zip 拿到的不是 zip，而是一个「预计等待时间 &lt;3s」
        /// 的 HTML 网页（它同时在后台打包）；过几秒再请求才是真正的 zip。
        /// 所以这里要「看魔数 + 等几秒重试」，不能拿到什么就解压什么。
        /// </summary>
        private static async Task DownloadArchiveAsync(
            string url, string dest, IProgress<UpdateProgress> progress, CancellationToken ct)
        {
            Exception last = null;
            for (int attempt = 1; attempt <= ArchiveAttempts; attempt++)
            {
                try
                {
                    Report(progress, attempt == 1 ? "download" : "waiting", 0, 0, 0);
                    await DownloadAsync(url, dest, progress, ct).ConfigureAwait(false);
                    if (LooksLikeZip(dest)) return;
                    last = new InvalidDataException(
                        "Gitee 返回的不是压缩包（通常是它正在后台打包这个版本）");
                    SafeDeleteFile(dest);
                }
                catch (Exception ex)
                {
                    last = ex;
                    SafeDeleteFile(dest);
                }

                if (attempt < ArchiveAttempts)
                {
                    Report(progress, "waiting", 0, 0, 0);
                    await Task.Delay(ArchiveRetryDelayMs, ct).ConfigureAwait(false);
                }
            }
            throw new IOException(
                "下载归档失败（已重试 " + ArchiveAttempts + " 次）：" +
                (last != null ? last.Message : "未知原因"), last);
        }

        /// <summary>是不是一个 zip（本地文件头魔数 PK）。</summary>
        private static bool LooksLikeZip(string file)
        {
            try
            {
                using (FileStream fs = File.OpenRead(file))
                {
                    if (fs.Length < 4) return false;
                    int b0 = fs.ReadByte(), b1 = fs.ReadByte();
                    return b0 == 'P' && b1 == 'K';
                }
            }
            catch { return false; }
        }

        private static async Task DownloadAsync(string url, string dest, IProgress<UpdateProgress> progress, CancellationToken ct)
        {
            using (HttpRequestMessage req = new HttpRequestMessage(HttpMethod.Get, url))
            {
                req.Headers.TryAddWithoutValidation("User-Agent", ArchiveUserAgent);
                using (HttpResponseMessage resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false))
                {
                resp.EnsureSuccessStatusCode();
                long total = resp.Content.Headers.ContentLength ?? -1L;
                using (Stream src = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false))
                using (FileStream fs = new FileStream(dest, FileMode.Create, FileAccess.Write, FileShare.None, 81920, true))
                {
                    byte[] buf = new byte[81920];
                    long got = 0;
                    int lastPct = -1;
                    int n;
                    while ((n = await src.ReadAsync(buf, 0, buf.Length, ct).ConfigureAwait(false)) > 0)
                    {
                        await fs.WriteAsync(buf, 0, n, ct).ConfigureAwait(false);
                        got += n;
                        int pct = total > 0 ? (int)(got * 100 / total) : 0;
                        if (pct != lastPct)
                        {
                            lastPct = pct;
                            Report(progress, "download", pct, got, total);
                        }
                    }
                    await fs.FlushAsync(ct).ConfigureAwait(false);
                }
                }
            }
        }

        private static void Report(IProgress<UpdateProgress> progress, string phase, int pct, long bytes, long total)
        {
            if (progress == null) return;
            progress.Report(new UpdateProgress { Phase = phase, Percent = pct, Bytes = bytes, TotalBytes = total });
        }

        /// <summary>解压目录里应该只有一个顶层文件夹；多于一小时退回解压根目录。</summary>
        private static string FindSingleTopDirectory(string extractRoot)
        {
            string[] dirs = Directory.GetDirectories(extractRoot);
            if (dirs.Length == 1) return dirs[0];
            if (File.Exists(Path.Combine(extractRoot, "index.html"))) return extractRoot;
            if (dirs.Length > 1)
            {
                foreach (string d in dirs)
                    if (File.Exists(Path.Combine(d, "index.html"))) return d;
            }
            return null;
        }

        /// <summary>把仓库内容按 build.ps1 的规则复制成 www 结构（跳过 .git / windows-kiosk / tools 等）。</summary>
        private static void CopyContent(string srcRoot, string destRoot)
        {
            Directory.CreateDirectory(destRoot);
            foreach (string dir in Directory.GetDirectories(srcRoot))
            {
                string name = Path.GetFileName(dir);
                if (IsSkippedTopLevel(name)) continue;
                CopyDirectory(dir, Path.Combine(destRoot, name));
            }
            foreach (string file in Directory.GetFiles(srcRoot))
            {
                string name = Path.GetFileName(file);
                if (IsSkippedTopLevel(name)) continue;
                if (IsSkippedFile(name)) continue;
                File.Copy(file, Path.Combine(destRoot, name), true);
            }
        }

        private static bool IsSkippedTopLevel(string name)
        {
            foreach (string s in SkipTopLevel)
                if (string.Equals(s, name, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static bool IsSkippedFile(string name)
        {
            foreach (string s in SkipFileNames)
                if (string.Equals(s, name, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static void CopyDirectory(string src, string dest)
        {
            Directory.CreateDirectory(dest);
            foreach (string dir in Directory.GetDirectories(src))
            {
                string name = Path.GetFileName(dir);
                if (string.Equals(name, ".git", StringComparison.OrdinalIgnoreCase)) continue;
                if (string.Equals(name, ".history", StringComparison.OrdinalIgnoreCase)) continue;
                CopyDirectory(dir, Path.Combine(dest, name));
            }
            foreach (string file in Directory.GetFiles(src))
            {
                string name = Path.GetFileName(file);
                if (IsSkippedFile(name)) continue;
                File.Copy(file, Path.Combine(dest, name), true);
            }
        }

        // ------------------------------------------------------------------
        // 启动时应用暂存的更新
        // ------------------------------------------------------------------

        /// <summary>
        /// 启动时调用（必须在创建 WebView2 之前）：如果 update\content 里有暂存好的新内容，
        /// 就把 www 换成它。返回 null 表示没有待应用的更新，否则返回"从几更新到几"的描述。
        /// </summary>
        public static string TryApplyStagedUpdate(string baseDir)
        {
            string updateRoot = UpdateRoot(baseDir);
            string contentDir = Path.Combine(updateRoot, "content");
            string wwwDir = Path.Combine(baseDir, "www");
            string marker = Path.Combine(updateRoot, "staged.json");

            if (!Directory.Exists(updateRoot)) return null;
            // 不是 exe + www 这种目录结构（wwwRoot 就是程序目录本身）时换不了文件夹，
            // 但暂存内容先留着，别删（万一之后换成正常结构还能用）
            if (!Directory.Exists(wwwDir)) return null;
            // 没有暂存内容（比如上一轮下载失败）就顺手清干净
            if (!Directory.Exists(contentDir) || !File.Exists(marker))
            {
                SafeDeleteDir(updateRoot);
                return null;
            }

            try
            {
                VersionInfo staged = ReadLocalVersion(contentDir);
                VersionInfo current = ReadLocalVersion(wwwDir);
                if (staged == null || !File.Exists(Path.Combine(contentDir, "index.html")))
                {
                    SafeDeleteDir(updateRoot);
                    return null;
                }
                // 万一暂存的内容并不比本地新（重复应用 / 手动回退），别把好内容换掉
                if (current != null && !VersionUtil.IsNewer(staged.Version, current.Version))
                {
                    SafeDeleteDir(updateRoot);
                    return null;
                }

                string backup = Path.Combine(baseDir,
                    "www.bak-" + DateTime.Now.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture));

                Directory.Move(wwwDir, backup);
                try
                {
                    Directory.Move(contentDir, wwwDir);
                }
                catch
                {
                    // 换不过去就把原来的换回来，绝不留下半个 www
                    try { if (!Directory.Exists(wwwDir)) Directory.Move(backup, wwwDir); } catch { }
                    throw;
                }

                SafeDeleteDir(backup);      // 备份只是回滚用的，成功后就删
                SafeDeleteDir(updateRoot);

                string from = current != null ? current.Version : "(旧版本)";
                return from + " → " + staged.Version;
            }
            catch (Exception ex)
            {
                // 更新失败不能影响孩子玩游戏（www 原样保留）
                return "更新失败：" + ex.Message;
            }
        }

        // ------------------------------------------------------------------
        // 小工具
        // ------------------------------------------------------------------

        public static void SafeDeleteDir(string dir)
        {
            try { if (Directory.Exists(dir)) Directory.Delete(dir, true); }
            catch { /* 删不掉就算了，下次启动还会再试 */ }
        }

        public static void SafeDeleteFile(string file)
        {
            try { if (File.Exists(file)) File.Delete(file); }
            catch { /* 忽略 */ }
        }

        /// <summary>程序自身版本（来自 csproj 的 &lt;Version&gt;）。</summary>
        public static string HostVersion()
        {
            try
            {
                System.Reflection.Assembly asm = System.Reflection.Assembly.GetEntryAssembly()
                    ?? System.Reflection.Assembly.GetExecutingAssembly();
                string info = null;
                try
                {
                    object[] attrs = asm.GetCustomAttributes(typeof(System.Reflection.AssemblyInformationalVersionAttribute), false);
                    if (attrs != null && attrs.Length > 0)
                        info = ((System.Reflection.AssemblyInformationalVersionAttribute)attrs[0]).InformationalVersion;
                }
                catch { }
                if (!string.IsNullOrEmpty(info))
                {
                    int plus = info.IndexOf('+');           // 去掉 +<commit> 后缀
                    return plus > 0 ? info.Substring(0, plus) : info;
                }
                Version v = asm.GetName().Version;
                if (v != null)
                    return v.Major + "." + v.Minor + "." + (v.Build < 0 ? 0 : v.Build);
            }
            catch { }
            return "0.0.0";
        }

        /// <summary>重启自己（应用暂存更新用）。</summary>
        public static bool RestartSelf()
        {
            try
            {
                // .NET 6+ 推荐用 ProcessPath：单文件发布时它拿到的才是宿主 exe 本身
                string exe = Environment.ProcessPath;
                if (string.IsNullOrEmpty(exe))
                    exe = Process.GetCurrentProcess().MainModule.FileName;
                ProcessStartInfo psi = new ProcessStartInfo(exe);
                psi.UseShellExecute = true;
                psi.WorkingDirectory = AppContext.BaseDirectory;
                Process.Start(psi);
                return true;
            }
            catch { return false; }
        }
    }
}
