<#
  少儿 AI 游戏乐园 —— Windows kiosk 版打包脚本

  用法（在 Windows 上）：
    1. 安装 .NET 8 SDK：https://dotnet.microsoft.com/download/dotnet/8.0
    2. 右键这个文件 → 「使用 PowerShell 运行」
       如果提示脚本被禁止，先执行：
       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

  产物：windows-kiosk\bin\Release\net8.0-windows\win-x64\publish\
        ├─ KidGameKiosk.exe   ← 双击即全屏运行
        └─ www\               ← 游戏文件（从仓库根目录自动复制）
#>

$ErrorActionPreference = 'Stop'

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo    = Split-Path -Parent $here
$csproj  = Join-Path $here 'KidGameKiosk.csproj'
$out     = Join-Path $here 'bin\Release\net8.0-windows\win-x64\publish'
$www     = Join-Path $out 'www'

Write-Host ''
Write-Host '=== 少儿 AI 游戏乐园 · Windows kiosk 打包 ===' -ForegroundColor Cyan
Write-Host ''

# ---------- 1. 检查环境 ----------
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Write-Host '[X] 没找到 dotnet 命令。' -ForegroundColor Red
    Write-Host '    请先安装 .NET 8 SDK： https://dotnet.microsoft.com/download/dotnet/8.0'
    Write-Host '    装完后重新打开一个 PowerShell 窗口再运行本脚本。'
    exit 1
}
Write-Host ('[1/3] 找到 ' + (dotnet --version)) -ForegroundColor Green

# ---------- 2. 编译发布 ----------
Write-Host '[2/3] 正在编译（首次运行要下载依赖，可能需要几分钟）...' -ForegroundColor Green
& dotnet publish $csproj -c Release
if ($LASTEXITCODE -ne 0) { Write-Host '[X] 编译失败。' -ForegroundColor Red; exit 1 }

# ---------- 3. 复制游戏文件 ----------
Write-Host '[3/3] 正在复制游戏文件到 www ...' -ForegroundColor Green
if (Test-Path $www) { Remove-Item $www -Recurse -Force }
New-Item -ItemType Directory -Path $www | Out-Null

# 排除：版本库、宿主程序本身、开发脚本
$skip = @('.git', 'windows-kiosk', 'tools')
Get-ChildItem -Path $repo -Force | Where-Object { $skip -notcontains $_.Name } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $www -Recurse -Force
}
# 顺带清掉不需要发布的备份文件
Get-ChildItem -Path $www -Recurse -Filter 'index-3d-backup.html' -ErrorAction SilentlyContinue |
    Remove-Item -Force

if (-not (Test-Path (Join-Path $www 'index.html'))) {
    Write-Host '[X] 复制后没找到 www\index.html，请检查仓库是否完整。' -ForegroundColor Red
    exit 1
}

# ---------- 完成 ----------
$exeSize = [math]::Round((Get-Item (Join-Path $out 'KidGameKiosk.exe')).Length / 1MB, 0)

Write-Host ''
Write-Host '=== 打包完成 ===' -ForegroundColor Cyan
Write-Host ''
Write-Host ('程序目录： ' + $out)
Write-Host ('exe 大小： ' + $exeSize + ' MB')
Write-Host ''
Write-Host '接下来：'
Write-Host '  1. 双击 KidGameKiosk.exe  →  无边框全屏启动，直接进游戏主页'
Write-Host '  2. 孩子玩的时候：Alt+F4 关不掉，Esc / F11 / Ctrl+W 都无效'
Write-Host '  3. 家长退出：Ctrl+Shift+Q'
Write-Host '  4. 想放桌面：右键 KidGameKiosk.exe → 发送到 → 桌面快捷方式'
Write-Host ''
Write-Host '提示：整个 publish 文件夹可以整体搬到别的位置，'
Write-Host '      但 exe 和 www 必须在一起（exe 靠同级的 www 找游戏）。'
Write-Host ''
