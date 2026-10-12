# Windows kiosk 版（WebView2 打包）

把整套游戏打包成一个 **Windows exe**，双击就**无边框全屏**运行，孩子按 `Alt+F4`、`Esc`、`F11`、`Ctrl+W` 都出不去。

专门为**一台 Windows 机器**设计，不需要安装程序、不需要代码签名。

![图标预览](icon-preview.png)

图标是**手柄造型**（`app.ico`，含 16/24/32/48/64/128/256 七个尺寸），由 `make-icon.py` 生成，
不依赖任何第三方库；16/24px 用小尺寸专用简化版，任务栏里也看得清。

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
| 图标 | 改 `make-icon.py` 里的配色（`GRAD_A/GRAD_B/PAD/DPAD/BTN`）或几何（`GEO`），重跑 `python3 make-icon.py` 重新生成 `app.ico` |
| 换图标造型 | `make-icon.py --variants` 会把多种握把造型并排渲染成 `variants.png` 方便挑 |

---

## 版本更新（以后不用每次重新打包）

程序**每次启动会自动去 Gitee 上比对一次版本**，发现新版就在游戏主页弹窗问要不要更新：

- 比对仓库根目录的 `version.json`（`https://gitee.com/zhoul.vic/at-ai-child-game/raw/master/version.json`）
  和本地 `www\version.json`
- 家长点「立即更新」→ 程序下载仓库归档（`/repository/archive/master.zip`）→ 解压校验 → 暂存
- 再点「立即重启」→ 重启时把 `www` 整个换成新内容（**`data\` 目录不动，孩子的存档不丢**）

所以以后改了游戏**只要推代码 + 改一下 `version.json` 的版本号**，那台机器下次开机就自己更新了，
不用再打包 exe。只有**改宿主程序本身**（`Program.cs` / `Updater.cs` / `.csproj`）才需要重新打包。

### 发布新版本怎么做

1. 改完游戏代码，把仓库根目录 `version.json` 的版本号往上加（例如 `1.2.0` → `1.3.0`），顺手写上这次的说明：
   ```json
   { "version": "1.3.0", "date": "2026-11-01", "notes": "新增 3 个数学游戏", "minHost": "1.2.0" }
   ```
2. `git push` 到 Gitee 的 `master`
3. 完事 —— 客户端下次启动会自己看到

> `minHost` 是「这份内容最低需要哪个版本的客户端」。将来新内容如果用到宿主的新能力，
> 就把 `minHost` 提到对应版本，老客户端会提示"需要新的客户端程序"，
> 而不是更新成一份跑不起来的内容。

### 为什么是「先下载、重启才生效」

运行中 WebView2 正从 `www` 目录读文件，Windows 上目录被占用时改不动。
而程序刚启动、WebView2 还没创建的那一瞬间整个目录是空闲的，改名替换既快又能留回滚余地。
所以更新拆成两步：**本次下载暂存 → 下次启动替换**，界面上也是这么引导的。

出错时的表现（都不会影响孩子继续玩）：

| 情况 | 表现 |
|---|---|
| 没网 / Gitee 连不上 | 静默跳过（只有家长手动点「检查更新」才会看到提示） |
| 程序目录不可写（比如放在 Program Files） | 提示「程序目录不可写，请把整个文件夹搬到可写的位置」 |
| 下载到一半断了 | 报错，`www` 原样不动；`update\` 里的半成品会在下次检查时清掉 |
| 替换目录失败 | 自动把原来的 `www` 换回来 |
| 点了「以后再说」 | 本次不打扰，下次启动还会提醒 |
| 点了「跳过这个版本」 | 这个版本不再提醒，等更新的版本出来再提醒 |

解压出来的内容会按和 `build.ps1` **完全一样的排除规则**铺进 `www`：
跳过 `.git` / `windows-kiosk` / `tools` / `.gitee-token` / `.env` / `_draft`，
以及 `index-3d-backup.html` 这类备份文件。

### 相关文件

| 文件 | 作用 |
|---|---|
| `version.json`（仓库根目录） | 内容版本号，客户端比对这个 |
| `windows-kiosk/Updater.cs` | 检查 / 下载 / 解压 / 校验 / 暂存 / 启动时替换的全部逻辑（不依赖 WinForms，可单独编译测试） |
| `windows-kiosk/Program.cs` | 页面 ↔ 宿主的消息桥：`get-state` / `check` / `update` / `restart` |
| `index.html`（游戏主页） | 版本号、更新弹窗、进度条。普通浏览器里只显示版本号，不弹窗 |

### 踩过的坑（改 Updater.cs 前一定要看）

**Gitee 的两个接口对 User-Agent 的要求刚好相反**，实测记录：

| 接口 | 要什么 UA | 用错会怎样 |
|---|---|---|
| `/raw/master/version.json` | 像浏览器 | 用 curl 那类 UA 会被拦，返回 `The content may contain violation information` |
| `/repository/archive/master.zip` | 像 curl（UA 里带 `curl` 字样） | 用浏览器 UA 会被当成人在浏览网页，返回「预计等待时间 <3s」的**打包中网页**，而不是 zip。实测 `python-requests` / `git/2.40` / 无 UA / 浏览器 UA 都拿不到，只有 `curl`、`wget` 能直接拿到 |

所以 `Updater.cs` 里给两类请求准备了两个 UA，别合并。另外归档是**按需打包**的：
刚 push 完新提交时，即使 UA 对了也可能先拿到那个等待页，所以下载后要检查文件头是不是
`PK`，不是就等几秒重试（最多 4 次）。Gitee 那个 `check_archive` 内部接口需要会话令牌，
试过走不通（返回「验证信息过期」），别去逆向它。

---

## 已知限制

- **中文语音必须实测**：23/24 个游戏靠 `speechSynthesis` 播报。WebView2 用的是 Edge 引擎，
  理论上和 Edge 里表现一致（现在能正常播），但**第一次跑务必听一下有没有声音**。
  如果没声音，检查 Windows 是否装了中文语音包：
  设置 → 时间和语言 → 语言 → 中文（简体）→ 语音
- 交叉编译出的那个 exe **我没有在 Windows 上跑过**（手边只有 macOS），
  建议优先用「方式 A」在 Windows 上自己编译
- 首次启动会稍慢（单文件 exe 要先把运行时解压到临时目录），之后就快了
