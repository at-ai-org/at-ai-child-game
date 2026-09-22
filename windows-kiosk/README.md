# Windows kiosk 版（WebView2 打包）

把整套游戏打包成一个 **Windows exe**，双击就**无边框全屏**运行，孩子按 `Alt+F4`、`Esc`、`F11`、`Ctrl+W` 都出不去。

专门为**一台 Windows 机器**设计，不需要安装程序、不需要代码签名。

---

## 为什么用这个方案（而不是浏览器全屏）

浏览器有三条硬限制，网页代码绕不过去：

1. `requestFullscreen()` 必须由用户手势触发 → 页面加载时自动全屏一定被拒
2. **页面跳转会重置全屏** → 从主页点进游戏就会掉出全屏
3. `Esc` 退出全屏拦不住

这个 exe 的全屏是**窗口级**的（不是网页的 Fullscreen API），所以：

- ✅ 游戏里主页 ↔ 游戏页来回跳转，**全程保持全屏**
- ✅ `Alt+F4` 被拦住，`Esc` / `F11` / `Ctrl+W` / 右键菜单 / 缩放全部失效
- ✅ 完全离线运行（整个游戏只有 2.1 MB，已全部打进 `www`）
- ✅ `localStorage` 正常（图形提示开关、英语进度都能记住）

---

## 快速开始

### 前提

- 那台 Windows 是 **Windows 10 或 11**
- 一般**已经自带** WebView2 运行时（跟着 Edge 一起装的）；万一没有，exe 启动时会弹窗提示，装一下
  [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) 即可

### 方式 A：在 Windows 上自己编译（推荐）

1. 把这整个仓库拷到 Windows（压缩包解压即可，约 2.1 MB）
2. 安装 **.NET 8 SDK**：<https://dotnet.microsoft.com/download/dotnet/8.0>
3. 进入 `windows-kiosk` 文件夹，右键 `build.ps1` → **使用 PowerShell 运行**
   （若提示脚本被禁止，先执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force`）
4. 脚本会自动编译 + 复制游戏文件，完成后告诉你目录

产物长这样：

```
publish\
├─ KidGameKiosk.exe     ← 双击运行（约 66 MB，已内含 .NET 运行时）
└─ www\                 ← 游戏文件（24 个游戏）
```

> `exe` 和 `www` **必须放在一起**，exe 靠同级的 `www` 找游戏。

### 方式 B：直接用已编译好的 exe

仓库里 `windows-kiosk/bin/Release/net8.0-windows/win-x64/publish/KidGameKiosk.exe`
是我在 macOS 上**交叉编译**出来的（**没有在 Windows 上实测过**）。

如果想省掉装 SDK 这一步，可以把这个 exe 拷到 Windows，然后在它旁边建一个 `www` 文件夹，
把仓库里除 `.git`、`windows-kiosk`、`tools` 之外的内容拷进去。

---

## 使用

| 操作 | 效果 |
|---|---|
| 双击 `KidGameKiosk.exe` | 无边框全屏启动，直接进游戏主页 |
| 孩子在里面玩 | `Alt+F4` / `Esc` / `F11` / `Ctrl+W` / 右键 / 缩放 **全部无效** |
| **家长退出** | **`Ctrl+Shift+Q`** |
| 放到桌面 | 右键 exe → 发送到 → 桌面快捷方式 |
| 开机自启 | `Win+R` → `shell:startup` → 把快捷方式丢进去 |

---

## 需要改游戏代码吗？

**不需要。** 游戏本体（HTML/CSS/JS）**一行都没改**，因为：

| 检查项 | 结果 |
|---|---|
| 绝对路径引用 | 无（全是相对路径） |
| `window.open` / `target="_blank"` | 无 |
| 外部 http 链接 | 无 |
| `fetch` / `XMLHttpRequest` | 无 |
| 本地 js 依赖 | 只有 `data.js`、`three.min.js`，都是相对路径 |

宿主程序用**虚拟主机映射**把本地文件夹映射成 `http://game.local/`，而不是用 `file://`。
这一点很关键：`file://` 下 `localStorage` 是 opaque origin，`hanzi-quest` 的图形提示开关和
`mba-english` 的进度**会失效**；映射成 http 源之后行为就和浏览器完全一致。

---

## 想改点什么

| 想改 | 改哪里 |
|---|---|
| 家长退出热键 | `Program.cs` 里的 `VK_Q`（改成别的键码） |
| 换成加载远程网址 | `Program.cs` 里 `core.Navigate("http://...")`，并去掉 `SetVirtualHostNameToFolderMapping` |
| 允许 F11 / 右键 | `Program.cs` 里对应的 `Set(() => s.XXX = false)` 改成 `true` |
| 窗口标题 | `Program.cs` 里 `Text = "..."` |
| 图标 | 加一个 `.ico`，在 `csproj` 里加 `<ApplicationIcon>app.ico</ApplicationIcon>` |

---

## 已知限制

- **中文语音必须实测**：23/24 个游戏靠 `speechSynthesis` 播报。WebView2 用的是 Edge 引擎，
  理论上和 Edge 里表现一致（现在能正常播），但**第一次跑务必听一下有没有声音**。
  如果没声音，检查 Windows 是否装了中文语音包：
  设置 → 时间和语言 → 语言 → 中文（简体）→ 语音
- 交叉编译出的那个 exe **我没有在 Windows 上跑过**（手边只有 macOS），
  建议优先用「方式 A」在 Windows 上自己编译
- 首次启动会稍慢（单文件 exe 要先把运行时解压到临时目录），之后就快了
