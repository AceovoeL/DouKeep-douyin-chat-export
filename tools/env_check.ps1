<#
环境检测脚本 —— 只依赖 Windows 自带的 PowerShell，不依赖 Python / Node。

为什么要单独写一份：新电脑上"第一次运行"的时候，Python 和 Node 装上没有正是要检
测的对象，所以检测本身不能再用 Python / Node 来跑。这里全部用 PowerShell 内置
能力（Get-Command、.NET、注册表、TcpClient）来判断，检测完把结果写成一个
JavaScript 文件给 start.html 读取：

    config/env-report.js   ->   window.__DOUYIN_ENV_REPORT__ = {...};

start.html 用 <script src="config/env-report.js"> 这种方式读它就绕开了浏览器
「file:// 页面不能读本地文件」的限制（file:// 页面允许加载同目录/子目录的脚本）。

用法：
    powershell -ExecutionPolicy Bypass -File tools\env_check.ps1
    powershell -ExecutionPolicy Bypass -File tools\env_check.ps1 -EmitJson   # 顺便把 JSON 打到管道
#>
[CmdletBinding()]
param(
    # 报告文件（默认 config/env-report.js）
    [string]$ReportPath = "",
    # 另外把紧凑 JSON 写一份到这个路径，给 bridge.ps1 解析
    # （不能靠管道回收：这个脚本 Write-Host 的人看输出也会进 stdout，会和 JSON 混在一起）
    [string]$JsonPath = "",
    # 把紧凑 JSON 输出到管道（自己调试用，注意它会和人看的输出混在一起）
    [switch]$EmitJson
)

$ErrorActionPreference = 'Continue'
try { $ProgressPreference = 'SilentlyContinue' } catch { }
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not $ReportPath) { $ReportPath = Join-Path $ProjectDir 'config\env-report.js' }
$LogPath = Join-Path $ProjectDir 'config\logs\env-check.log'

# ── 结果收集 ──────────────────────────────────────────────────────────────
$items = New-Object System.Collections.ArrayList

function Add-Check {
    param(
        [string]$Id,
        [string]$Name,
        [bool]$Required,
        [bool]$Ok,
        [string]$Requirement = "",
        [string]$Current = "",
        [string]$Path = "",
        [string]$Detail = "",
        [string]$Hint = ""
    )
    [void]$items.Add([ordered]@{
        id          = $Id
        name        = $Name
        required    = $Required
        ok          = $Ok
        requirement = $Requirement
        current     = $Current
        path        = $Path
        detail      = $Detail
        hint        = $Hint
    })
}

function Write-Section($text) {
    Write-Host ""
    Write-Host "=====> $text" -ForegroundColor Cyan
}

# ── 小工具 ────────────────────────────────────────────────────────────────
function Invoke-Tool {
    <# 跑一个外部命令并抓回输出（找不到命令时返回 $null） #>
    param([string]$Exe, [string[]]$Arguments = @())
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    try {
        $output = & $cmd.Source @Arguments 2>&1 | Out-String
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        return [pscustomobject]@{
            Path = $cmd.Source
            Code = [int]$code
            Text = ($output -replace '\s+', ' ').Trim()
        }
    }
    catch {
        return [pscustomobject]@{ Path = $cmd.Source; Code = -1; Text = $_.Exception.Message }
    }
}

function Get-SemVer {
    <# 从任意文本里抠出 x.y.z 版本号 #>
    param([string]$Text)
    if (-not $Text) { return $null }
    $m = [regex]::Match($Text, '(\d+)\.(\d+)(?:\.(\d+))?')
    if (-not $m.Success) { return $null }
    $patch = 0
    if ($m.Groups[3].Success) { $patch = [int]$m.Groups[3].Value }
    return [pscustomobject]@{
        Major = [int]$m.Groups[1].Value
        Minor = [int]$m.Groups[2].Value
        Patch = $patch
        Text  = $m.Value
    }
}

function Test-NodeOk {
    <# Vite 7 的 engines 要求：^20.19.0 || >=22.12.0 #>
    param($Version)
    if (-not $Version) { return $false }
    if ($Version.Major -eq 20) { return $Version.Minor -ge 19 }
    if ($Version.Major -ge 22) {
        if ($Version.Major -gt 22) { return $true }
        return $Version.Minor -ge 12
    }
    return $false
}

function Test-PortBusy {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(600, $false)) {
            try { $client.EndConnect($iar); return $true } catch { return $false }
        }
        return $false
    }
    catch { return $false }
    finally { try { $client.Close() } catch { } }
}

function Test-OwnService {
    <# 8000 端口上是不是已经跑着本项目的后端 #>
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/auth/check' -TimeoutSec 3 -UseBasicParsing
        if ($resp.StatusCode -eq 200 -and $resp.Content -match 'need_password') { return $true }
    }
    catch { }
    return $false
}

# ── 1. 操作系统 ───────────────────────────────────────────────────────────
Write-Section "检测操作系统"
$osOk = $false
$osCurrent = ""
$osDetail = ""
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $osCurrent = ("{0} ({1})" -f $os.Caption, $os.Version) -replace '\s+', ' '
    $build = 0
    try { $build = [int]($os.Version.Split('.')[2]) } catch { }
    $osOk = ($build -ge 17763)                      # Windows 10 1809
    $osDetail = "start.ps1 是 PowerShell 启动脚本，只在 Windows 上可用（macOS / Linux 请用 start.sh）"
}
catch {
    $osCurrent = [System.Environment]::OSVersion.VersionString
    $osOk = ($env:OS -eq 'Windows_NT')
    $osDetail = "读取系统信息失败，按当前系统版本判断"
}
if (-not $osOk -and $osCurrent -eq "") { $osCurrent = [System.Environment]::OSVersion.VersionString }
Add-Check -Id 'os' -Name '操作系统' -Required $true -Ok $osOk `
    -Requirement 'Windows 10 1809 或更高 / Windows 11' -Current $osCurrent `
    -Detail $osDetail `
    -Hint '本项目 Windows 端依赖 PowerShell 脚本，请换用 Windows 10 1809+ 的机器；macOS / Linux 请改用 start.sh'

# ── 2. PowerShell ─────────────────────────────────────────────────────────
Write-Section "检测 PowerShell"
$psOk = $false
$psCurrent = ""
if ($PSVersionTable -and $PSVersionTable.PSVersion) {
    $psCurrent = $PSVersionTable.PSVersion.ToString()
    $psOk = ($PSVersionTable.PSVersion.Major -gt 5) -or
            ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -ge 1)
}
Add-Check -Id 'powershell' -Name 'PowerShell' -Required $true -Ok $psOk `
    -Requirement '>= 5.1' -Current $psCurrent -Path '' `
    -Detail '运行 start.ps1（建虚拟环境、装依赖、构建前端、起服务）' `
    -Hint 'Windows 10 / 11 自带 PowerShell 5.1，缺失说明系统组件被裁剪过，可到微软官网装 PowerShell 7'

# ── 3. 脚本执行策略 ───────────────────────────────────────────────────────
Write-Section "检测脚本执行策略"
$policy = ''
$policyScope = ''
try {
    $policy = (Get-ExecutionPolicy -Scope LocalMachine).ToString()
    $policyScope = 'LocalMachine'
}
catch { }
try {
    $userPolicy = (Get-ExecutionPolicy -Scope CurrentUser).ToString()
    if ($userPolicy -and $userPolicy -ne 'Undefined') { $policy = $userPolicy; $policyScope = 'CurrentUser' }
}
catch { }
if (-not $policy) { $policy = 'Undefined' }
Add-Check -Id 'execution_policy' -Name '脚本执行策略' -Required $true -Ok $true `
    -Requirement '任意（启动脚本已用 -ExecutionPolicy Bypass）' `
    -Current ("{0}{1}" -f $policy, $(if ($policyScope) { "（$policyScope）" } else { "" })) `
    -Detail 'start.html 与启动器都用 Bypass 方式调用脚本，所以 Restricted / AllSigned 也不会挡住启动' `
    -Hint '如果习惯直接在终端敲 .\start.ps1 报「禁止运行脚本」，用 start.html 启动，或执行 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned'

# ── 4. 项目目录可写 ───────────────────────────────────────────────────────
Write-Section "检测项目目录是否可写"
$writable = $false
$probe = Join-Path $ProjectDir ('.env-check-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    [System.IO.File]::WriteAllText($probe, 'ok')
    if (Test-Path $probe) { $writable = $true }
}
catch { $writable = $false }
finally { if (Test-Path $probe) { Remove-Item $probe -Force -ErrorAction SilentlyContinue } }
Add-Check -Id 'project_dir' -Name '项目目录可写' -Required $true -Ok $writable `
    -Requirement '可写（要创建 venv、node_modules、data、config 等）' `
    -Current $(if ($writable) { '可写' } else { '不可写' }) -Path $ProjectDir `
    -Detail '启动过程要在项目目录里创建 venv/、frontend/node_modules/、frontend/dist/，以及存聊天记录的 data/ 和存配置与日志的 config/' `
    -Hint '把项目放到「文档 / 桌面」这类个人目录下（不要放在 C:\Program Files 或只读盘里），或右键属性取消只读'

# ── 5. 磁盘可用空间 ───────────────────────────────────────────────────────
Write-Section "检测磁盘可用空间"
$freeGb = 0
$freeText = ''
$diskOk = $false
try {
    $root = [System.IO.Path]::GetPathRoot($ProjectDir)
    $drive = New-Object System.IO.DriveInfo($root)
    $freeGb = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
    $freeText = "$freeGb GB 可用（$($drive.Name.TrimEnd('\'))）"
    $diskOk = ($freeGb -ge 2)
}
catch { $freeText = '读取失败' }
Add-Check -Id 'disk' -Name '磁盘可用空间' -Required $true -Ok $diskOk `
    -Requirement '>= 2 GB' -Current $freeText -Path $ProjectDir `
    -Detail '虚拟环境 + 前端依赖 + 构建产物 + 浏览器内核大约要 1.5 GB 上下，聊天媒体会另外占用空间' `
    -Hint '清理一下磁盘，或把项目移到空间更充裕的盘符再运行'

# ── 6. Python ─────────────────────────────────────────────────────────────
Write-Section "检测 Python"
$pyInfo = Invoke-Tool -Exe 'python' -Arguments @('--version')
$pyVersion = $null
$pyCurrent = ''
$pyOk = $false
$pyPath = ''
$pyHint = '到 https://www.python.org/downloads/ 下载安装，安装时务必勾选「Add python.exe to PATH」（或使用 Microsoft Store 版 Python）'
if ($pyInfo) {
    $pyPath = $pyInfo.Path
    $pyCurrent = $pyInfo.Text
    if ($pyCurrent -match 'was not found|不是内部或外部命令|Microsoft Store') {
        # Windows 的「应用执行别名」空壳，看起来有 python 其实没装
        $pyCurrent = '未安装（只有应用商店别名）'
        $pyHint = '系统里的 python 只是 Microsoft Store 的别名占位。请安装真正的 Python，或到「设置 → 应用 → 高级应用设置 → 应用执行别名」里关掉 python.exe 的别名'
    }
    else {
        $pyVersion = Get-SemVer -Text $pyCurrent
        if ($pyVersion) {
            $pyOk = ($pyVersion.Major -gt 3) -or ($pyVersion.Major -eq 3 -and $pyVersion.Minor -ge 10)
        }
    }
}
else {
    $pyCurrent = '未检测到'
}
Add-Check -Id 'python' -Name 'Python' -Required $true -Ok $pyOk `
    -Requirement '>= 3.10' -Current $pyCurrent -Path $pyPath `
    -Detail '创建虚拟环境 venv、安装 requirements.txt 里的依赖、运行后端服务' `
    -Hint $pyHint

# 装了但没进 PATH 时给个更准的提示（start.ps1 直接调用 python）
if (-not $pyOk) {
    $pyLauncher = Invoke-Tool -Exe 'py' -Arguments @('-3', '--version')
    if ($pyLauncher -and $pyLauncher.Code -eq 0 -and $pyLauncher.Text -match '\d+\.\d+') {
        Add-Check -Id 'python_path' -Name 'python 命令在 PATH 里' -Required $true -Ok $false `
            -Requirement 'python 能被直接调用' -Current ("py -3 可用：{0}" -f $pyLauncher.Text) -Path $pyLauncher.Path `
            -Detail '系统里装了 Python（py 启动器能找到），但 python 这个命令不在 PATH 中，start.ps1 调不到' `
            -Hint '重新运行 Python 安装程序 → Modify → 勾选「Add python.exe to PATH」，或把 Python 安装目录加进系统环境变量 Path'
    }
}

# ── 7. pip ────────────────────────────────────────────────────────────────
Write-Section "检测 pip"
$pipInfo = $null
$pipCurrent = '未检测到'
$pipOk = $false
$pipVersion = $null
if ($pyOk) {
    $pipInfo = Invoke-Tool -Exe 'python' -Arguments @('-m', 'pip', '--version')
    if ($pipInfo -and $pipInfo.Code -eq 0) {
        $pipCurrent = $pipInfo.Text
        $pipVersion = Get-SemVer -Text $pipCurrent
        $pipOk = $true
    }
    elseif ($pipInfo) { $pipCurrent = $pipInfo.Text }
}
Add-Check -Id 'pip' -Name 'pip 包管理器' -Required $true -Ok $pipOk `
    -Requirement 'python -m pip 可用（>= 20.0）' -Current $pipCurrent `
    -Path $(if ($pipInfo) { $pipInfo.Path } else { '' }) `
    -Detail '安装 requirements.txt（fastapi、uvicorn、playwright 等）' `
    -Hint '执行 python -m ensurepip --upgrade 修复；如果 Python 装在系统盘受保护目录，用管理员终端跑一次'

# ── 8. Node.js ────────────────────────────────────────────────────────────
Write-Section "检测 Node.js"
$nodeInfo = Invoke-Tool -Exe 'node' -Arguments @('-v')
$nodeVersion = $null
$nodeCurrent = '未检测到'
$nodeOk = $false
$nodePath = ''
if ($nodeInfo) {
    $nodePath = $nodeInfo.Path
    $nodeVersion = Get-SemVer -Text $nodeInfo.Text
    $nodeCurrent = if ($nodeInfo.Text) { $nodeInfo.Text } else { '未知' }
    $nodeOk = Test-NodeOk -Version $nodeVersion
}
$nodeHint = '到 https://nodejs.org/ 下载 LTS 版（20.19+ 或 22.12+）安装，安装后重开一次浏览器/终端；已经装过旧版本的用 nvm-windows 切换'
if ($nodeInfo -and -not $nodeOk -and $nodeVersion) {
    $nodeHint = "当前 $($nodeVersion.Text) 不满足要求：Vite 7 需要 Node.js 20.19+ 或 22.12+（21.x 也不在支持范围）。请升级 Node.js，或用 nvm-windows 切到 22 LTS"
}
Add-Check -Id 'node' -Name 'Node.js' -Required $true -Ok $nodeOk `
    -Requirement '>= 20.19 或 >= 22.12（Vite 7 要求）' -Current $nodeCurrent -Path $nodePath `
    -Detail '构建前端界面（frontend 目录里执行 npm install 和 npm run build）' `
    -Hint $nodeHint

# ── 9. npm ────────────────────────────────────────────────────────────────
Write-Section "检测 npm"
$npmInfo = Invoke-Tool -Exe 'npm.cmd' -Arguments @('-v')
if (-not $npmInfo) { $npmInfo = Invoke-Tool -Exe 'npm' -Arguments @('-v') }
$npmCurrent = '未检测到'
$npmOk = $false
$npmVersion = $null
if ($npmInfo -and $npmInfo.Text -match '\d+\.\d+') {
    $npmVersion = Get-SemVer -Text $npmInfo.Text
    $npmCurrent = ($npmInfo.Text -split '\s+')[0]
    $npmOk = ($npmVersion.Major -ge 9)
}
Add-Check -Id 'npm' -Name 'npm 包管理器' -Required $true -Ok $npmOk `
    -Requirement '>= 9.0（随 Node.js 一起安装）' -Current $npmCurrent `
    -Path $(if ($npmInfo) { $npmInfo.Path } else { '' }) `
    -Detail '安装前端依赖' `
    -Hint 'npm 是 Node.js 自带的：重装 Node.js（LTS）即可；装完重开终端让 PATH 生效'

# ── 10. 端口 8000 ─────────────────────────────────────────────────────────
Write-Section "检测端口 8000"
$portBusy = Test-PortBusy -Port 8000
$ownService = $false
if ($portBusy) { $ownService = Test-OwnService }
$portOk = (-not $portBusy) -or $ownService
$portCurrent = '空闲'
if ($portBusy -and $ownService) { $portCurrent = '已被本项目的后端占用（说明服务已在运行）' }
elseif ($portBusy) { $portCurrent = '已被其他程序占用' }
Add-Check -Id 'port_8000' -Name '端口 8000' -Required $true -Ok $portOk `
    -Requirement '未被其他程序占用' -Current $portCurrent `
    -Detail '后端服务固定监听 127.0.0.1:8000，聊天浏览页和控制面板都在这个端口上' `
    -Hint '关掉占用 8000 的程序（在命令行执行 netstat -ano | findstr :8000 找到 PID，再用任务管理器结束），然后重新检测'

# ── 11. Git（可选） ────────────────────────────────────────────────────────
Write-Section "检测 Git（可选）"
$gitInfo = Invoke-Tool -Exe 'git' -Arguments @('--version')
$gitVersion = $null
$gitCurrent = '未检测到'
$gitOk = $false
if ($gitInfo) {
    $gitCurrent = $gitInfo.Text
    $gitVersion = Get-SemVer -Text $gitCurrent
    $gitOk = ($gitVersion -and $gitVersion.Major -ge 2)
}
Add-Check -Id 'git' -Name 'Git' -Required $false -Ok $gitOk `
    -Requirement '>= 2.20（可选）' -Current $gitCurrent `
    -Path $(if ($gitInfo) { $gitInfo.Path } else { '' }) `
    -Detail '可选：有它时控制面板用 git pull 更新（只拉有变化的部分，更新前还能查出本地未提交的改动）；没有它改用下载代码包覆盖，效果一样' `
    -Hint '没有 Git 也能一键更新（面板改为下载代码包覆盖，需要在「关于」页填只读 Token）；装了更省事，到 https://git-scm.com/download/win 安装'

# ── 12. ffmpeg（可选） ────────────────────────────────────────────────────
Write-Section "检测 ffmpeg（可选）"
$ffmpegPath = ''
$ffmpegSource = ''
if ($env:DOUYIN_FFMPEG -and (Test-Path $env:DOUYIN_FFMPEG)) {
    $ffmpegPath = $env:DOUYIN_FFMPEG
    $ffmpegSource = '环境变量 DOUYIN_FFMPEG'
}
if (-not $ffmpegPath) {
    $ffmpegCmd = Get-Command 'ffmpeg' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ffmpegCmd) { $ffmpegPath = $ffmpegCmd.Source; $ffmpegSource = 'PATH' }
}
if (-not $ffmpegPath) {
    $bundled = @(Get-ChildItem -Path (Join-Path $ProjectDir 'tools\ffmpeg') -Recurse -Filter 'ffmpeg.exe' -ErrorAction SilentlyContinue)
    if ($bundled.Count -gt 0) { $ffmpegPath = $bundled[0].FullName; $ffmpegSource = '项目 tools\ffmpeg' }
}
if (-not $ffmpegPath) {
    $jianying = @(
        'D:\Program Files (x86)\JianyingPro\11.2.0.14339\ffmpeg.exe',
        'C:\Program Files (x86)\JianyingPro\11.2.0.14339\ffmpeg.exe'
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($jianying) { $ffmpegPath = $jianying; $ffmpegSource = '剪映自带' }
}
Add-Check -Id 'ffmpeg' -Name 'ffmpeg' -Required $false -Ok ([bool]$ffmpegPath) `
    -Requirement '任意近期版本（可选）' `
    -Current $(if ($ffmpegPath) { "已找到（$ffmpegSource）" } else { '未找到' }) `
    -Path $ffmpegPath `
    -Detail '可选：抖音视频大多是 H.265，浏览器放不了时用它按需转成 H.264；没有它只能回落到播放原文件' `
    -Hint '需要的话下载 ffmpeg 后把 bin 目录加进 PATH，或设置环境变量 DOUYIN_FFMPEG 指向 ffmpeg.exe'

# ── 13. Playwright 浏览器内核（必需：启动脚本会在启动时自动下载） ─────────
# 这里永远算「已满足」：首次运行这一刻还什么都没有，而 start.ps1 一启动就会核对版本、
# 缺了自动下载（tools/ensure_playwright_browser.py）。所以这一项只报告现状，
# 不用它拦住启动 —— 否则用户会被一项本来不用他管的检查挡在门外。
Write-Section "检测 Playwright 浏览器内核"
$pwRoot = $env:PLAYWRIGHT_BROWSERS_PATH
if (-not $pwRoot) { $pwRoot = Join-Path $env:LOCALAPPDATA 'ms-playwright' }
$pwDirs = @()
if (Test-Path $pwRoot) {
    $pwDirs = @(Get-ChildItem -Path $pwRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'chromium-*' -or $_.Name -like 'chromium_headless_shell-*' })
}
$pwCurrent = '启动时自动安装（下载约 300 MB）'
if ($pwDirs.Count -gt 0) { $pwCurrent = ($pwDirs | ForEach-Object { $_.Name }) -join '、' }
Add-Check -Id 'playwright_chromium' -Name 'Playwright 浏览器内核' -Required $true -Ok $true `
    -Requirement 'chromium（启动时自动安装）' -Current $pwCurrent -Path $pwRoot `
    -Detail '采集聊天记录、渲染聊天长图、导入 Cookie 都要用它。启动脚本每次启动都会核对版本、缺了自动下载，所以这一项默认算已满足' `
    -Hint '真装不上时可以手动补：venv\Scripts\python.exe -m playwright install chromium'

# ── 14. 本机 Edge / Chrome（可选） ────────────────────────────────────────
Write-Section "检测本机浏览器（可选）"
$browserPath = ''
foreach ($candidate in @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe")) {
    if ($candidate -and (Test-Path $candidate)) { $browserPath = $candidate; break }
}
$browserName = ''
if ($browserPath) { $browserName = [System.IO.Path]::GetFileNameWithoutExtension($browserPath) }
Add-Check -Id 'browser' -Name '本机 Edge / Chrome' -Required $false -Ok ([bool]$browserPath) `
    -Requirement '任意近期版本（可选）' -Current $(if ($browserPath) { $browserName } else { '未找到' }) `
    -Path $browserPath `
    -Detail '可选：界面回归检查脚本会优先复用本机浏览器；采集用的仍是 Playwright 自带内核' `
    -Hint '一般 Windows 自带 Edge，未检测到也不影响主流程'

# ── 15. 项目当前状态（信息项） ────────────────────────────────────────────
Write-Section "检查项目现状"
$venvOk = Test-Path (Join-Path $ProjectDir 'venv\Scripts\python.exe')
Add-Check -Id 'venv' -Name '虚拟环境 venv' -Required $false -Ok $true `
    -Requirement '首次运行会自动创建' `
    -Current $(if ($venvOk) { '已存在（启动会跳过创建，更快）' } else { '尚未创建（首次运行由 start.ps1 创建）' }) `
    -Path (Join-Path $ProjectDir 'venv') -Detail 'Python 依赖装在项目自己的 venv 里，不污染系统 Python'

$distOk = Test-Path (Join-Path $ProjectDir 'frontend\dist\index.html')
Add-Check -Id 'frontend_dist' -Name '前端构建产物' -Required $false -Ok $true `
    -Requirement '首次运行会自动构建' `
    -Current $(if ($distOk) { '已存在（启动时仍会重新构建一次）' } else { '尚未构建（首次运行由 start.ps1 构建）' }) `
    -Path (Join-Path $ProjectDir 'frontend\dist') -Detail '聊天浏览界面由 Vue 构建后交给后端托管'

# ── 汇总 ──────────────────────────────────────────────────────────────────
$requiredItems = @($items | Where-Object { $_.required })
$optionalItems = @($items | Where-Object { -not $_.required })
$requiredPassed = @($requiredItems | Where-Object { $_.ok }).Count
$optionalPassed = @($optionalItems | Where-Object { $_.ok }).Count
$allRequiredOk = ($requiredPassed -eq $requiredItems.Count)

$report = [ordered]@{
    ts               = [double]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000)
    generated_at     = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    source           = 'powershell'
    project_dir      = $ProjectDir
    service_running  = $ownService
    summary          = [ordered]@{
        ok               = $allRequiredOk
        required_total   = $requiredItems.Count
        required_passed  = $requiredPassed
        optional_total   = $optionalItems.Count
        optional_passed  = $optionalPassed
    }
    items            = $items
}

$json = $report | ConvertTo-Json -Depth 8 -Compress

# 给启动器用的纯 JSON 副本（bridge.ps1 读它来判断"要不要顺手启动后端"）
if ($JsonPath) {
    try {
        [System.IO.File]::WriteAllText($JsonPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
    catch {
        Write-Host "写入 JSON 副本失败: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# 写报告（UTF-8 无 BOM；start.html 用 <script src> 直接读）
try {
    $dir = Split-Path -Parent $ReportPath
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $payload = "window.__DOUYIN_ENV_REPORT__ = " + $json + ";`r`n"
    [System.IO.File]::WriteAllText($ReportPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ""
    Write-Host "检测报告已写入: $ReportPath" -ForegroundColor Green
}
catch {
    Write-Host "写入检测报告失败: $($_.Exception.Message)" -ForegroundColor Yellow
}

# 追加一行日志，方便排查"点了检测但页面没反应"
try {
    $logDir = Split-Path -Parent $LogPath
    if ($logDir -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    $line = "{0}  必需 {1}/{2}  可选 {3}/{4}  结论 {5}" -f `
        (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $requiredPassed, $requiredItems.Count, `
        $optionalPassed, $optionalItems.Count, $(if ($allRequiredOk) { '通过' } else { '未通过' })
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}
catch { }

Write-Host ""
if ($allRequiredOk) {
    Write-Host "=====> 必要条件全部通过（$requiredPassed/$($requiredItems.Count)），可以启动" -ForegroundColor Green
}
else {
    Write-Host "=====> 必要条件有 $($requiredItems.Count - $requiredPassed) 项未通过，请先按提示处理" -ForegroundColor Red
    foreach ($item in ($requiredItems | Where-Object { -not $_.ok })) {
        Write-Host ("  [x] {0}：需要 {1}，当前 {2}" -f $item.name, $item.requirement, $item.current) -ForegroundColor Red
        if ($item.hint) { Write-Host ("      -> {0}" -f $item.hint) -ForegroundColor Yellow }
    }
}

if ($EmitJson) { Write-Output $json }
exit 0
