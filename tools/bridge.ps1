<#
启动器（浏览器与 PowerShell 之间的桥）

start.html 是浏览器页面，浏览器出于安全限制不能直接跑本地命令，所以由这个脚本来干
活：检测环境、把结果写成 data/env-report.js、在需要时运行 start.ps1。页面通过下面两条
路调用它：

  1. 首次运行：页面把命令复制到剪贴板，用户按 Win+R 粘贴回车（手动，但只需一次）；
  2. 之后：页面跳到自定义协议 douyin-chat-export://check 或 ://start，
     系统根据注册表里的记录调用这个脚本（注册也由本脚本完成，写在 HKCU，不需要管理员）。

启动前先看 8000 端口和正在运行的 start.ps1 进程，已经在跑就直接返回，只把状态写进
data/launcher-state.js；start.html 读这个文件，看到「启动器正在启动」就只等结果，
自己不启动第二份 start.ps1。

用法：
    powershell -ExecutionPolicy Bypass -File tools\bridge.ps1 -Action check
    powershell -ExecutionPolicy Bypass -File tools\bridge.ps1 -Action start
    powershell -ExecutionPolicy Bypass -File tools\bridge.ps1 -Action install
    powershell -ExecutionPolicy Bypass -File tools\bridge.ps1 -Uri "douyin-chat-export://check/"
#>
[CmdletBinding()]
param(
    # 自定义协议回传的完整 URI，形如 douyin-chat-export://check/
    [string]$Uri = "",
    # 直接指定动作：check / start / install / uninstall / open
    [ValidateSet('', 'check', 'start', 'install', 'uninstall', 'open')]
    [string]$Action = "",
    # 检测通过后不自动启动后端（只写报告）
    [switch]$NoStart
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ProjectDir = Split-Path -Parent $PSScriptRoot
$ProtocolName = 'douyin-chat-export'
$LogPath = Join-Path $ProjectDir 'data\env-check.log'
$StartScript = Join-Path $ProjectDir 'start.ps1'
$StartHtml = Join-Path $ProjectDir 'start.html'
# 启动状态文件（JSONP 形式，和 env-report.js 一样是给 start.html 用 <script src> 读的）。
# 作用：记录「start.ps1 已经由启动器拉起来了」，避免 start.html 再启动一遍。
$StatePath = Join-Path $ProjectDir 'data\launcher-state.js'

function Write-Msg($text, $color = 'Gray') {
    Write-Host $text -ForegroundColor $color
}

function Write-Log($text) {
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogPath -Value ("{0}  [bridge] {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $text) -Encoding UTF8
    }
    catch { }
}

function Get-LauncherState {
    <# 读上一次「启动器把 start.ps1 拉起来」的记录（数据文件同时也给 start.html 读） #>
    if (-not (Test-Path $StatePath)) { return $null }
    try {
        $text = [System.IO.File]::ReadAllText($StatePath, [System.Text.Encoding]::UTF8)
        $json = (($text -replace '^[^=]*=', '') -replace ';\s*$', '').Trim()
        if (-not $json) { return $null }
        return ($json | ConvertFrom-Json)
    }
    catch { return $null }
}

function Save-LauncherState {
    <# 把启动状态写成 JSONP 文件：bridge.ps1 用它认自己拉起的进程，start.html 读它避免重复启动 #>
    param(
        [string]$State = 'starting',
        [int]$ProcessId = 0,
        [string]$Note = ''
    )
    try {
        $dir = Split-Path -Parent $StatePath
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $payload = [ordered]@{
            ts           = [double]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000)
            generated_at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
            state        = $State
            pid          = $ProcessId
            script       = $StartScript
            note         = $Note
        } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($StatePath, "window.__DOUYIN_LAUNCHER_STATE__ = $payload;`r`n",
            (New-Object System.Text.UTF8Encoding($false)))
    }
    catch {
        Write-Log "写启动状态文件失败: $($_.Exception.Message)"
    }
}

function Get-StartScriptProcess {
    <#
    找出正在跑本项目 start.ps1 的 PowerShell 进程。

    为什么要这个：只看 8000 端口不够 —— start.ps1 前面还要建虚拟环境、装依赖、
    构建前端，这段时间里服务还没起来，只看端口会以为「还没人启动」而重复拉起一份。
    #>
    $procs = $null
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process `
                -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" -ErrorAction Stop)
    }
    catch {
        Write-Log "读进程列表失败（跳过重复启动检查）: $($_.Exception.Message)"
        return $null
    }

    $recordedPid = 0
    $state = Get-LauncherState
    if ($state -and $state.pid) { $recordedPid = [int]$state.pid }

    foreach ($p in $procs) {
        $cmd = ''
        try { $cmd = [string]$p.CommandLine } catch { $cmd = '' }
        if ($cmd -notmatch 'start\.ps1') { continue }
        # 命令行里带本项目目录的才算我们的（别的项目也可能有个同名脚本）
        if ($cmd.IndexOf($ProjectDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $p }
        # 兜底：命令行没写全路径时，认启动状态文件里记下的那个进程号
        if ($recordedPid -gt 0 -and $p.ProcessId -eq $recordedPid) { return $p }
    }
    return $null
}

function Get-ActionFromUri {
    param([string]$Value)
    if (-not $Value) { return '' }
    $text = $Value.Trim()
    $idx = $text.IndexOf('://')
    if ($idx -ge 0) { $text = $text.Substring($idx + 3) }
    $text = $text.Trim('/')
    if (-not $text) { return 'check' }
    return ($text.Split('/')[0]).ToLowerInvariant()
}

function Test-OwnService {
    <# 8000 端口上是不是已经跑着本项目的后端 #>
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/auth/check' -TimeoutSec 3 -UseBasicParsing
        return ($resp.StatusCode -eq 200 -and $resp.Content -match 'need_password')
    }
    catch { return $false }
}

function Install-Protocol {
    <# 注册 douyin-chat-export:// 协议（只写当前用户 HKCU，不需要管理员） #>
    try {
        $bridge = Join-Path $PSScriptRoot 'bridge.ps1'
        $command = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
                   $bridge + '" -Uri "%1"'
        $root = "HKCU:\Software\Classes\$ProtocolName"
        New-Item -Path $root -Force | Out-Null
        New-ItemProperty -Path $root -Name '(default)' -Value 'URL:Douyin Chat Export 启动器' -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $root -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
        New-Item -Path "$root\shell\open\command" -Force | Out-Null
        New-ItemProperty -Path "$root\shell\open\command" -Name '(default)' -Value $command -PropertyType String -Force | Out-Null
        Write-Log "已注册协议 $ProtocolName"
        Write-Msg "已注册一键启动协议: ${ProtocolName}://" Green
        return $true
    }
    catch {
        Write-Log "注册协议失败: $($_.Exception.Message)"
        Write-Msg "注册一键启动协议失败（不影响手动启动）：$($_.Exception.Message)" Yellow
        return $false
    }
}

function Start-Backend {
    <#
    启动 start.ps1（它负责建环境、装依赖、构建前端、起服务）。

    启动之前先看 8000 端口上有没有本项目的服务、有没有正在跑的 start.ps1 进程；
    已经在跑就只更新状态文件，让 start.html 知道「后端正在准备中」。
    #>
    if (Test-OwnService) {
        Write-Msg "后端服务已经在运行（127.0.0.1:8000），不重复启动" Green
        Write-Log "后端已在运行，跳过启动"
        Save-LauncherState -State 'running' -Note '后端服务已经在运行'
        return $true
    }
    $existing = Get-StartScriptProcess
    if ($existing) {
        Write-Msg "start.ps1 已经在运行（进程号 $($existing.ProcessId)），不重复启动" Green
        Write-Msg "等它跑完即可，8000 端口能连上就表示就绪（服务输出在 data\server.log）" Gray
        Write-Log "start.ps1 已在运行（PID $($existing.ProcessId)），跳过启动"
        Save-LauncherState -State 'starting' -ProcessId $existing.ProcessId -Note 'start.ps1 已经在运行'
        return $true
    }
    if (-not (Test-Path $StartScript)) {
        Write-Msg "找不到启动脚本: $StartScript" Red
        Write-Log "找不到 $StartScript"
        return $false
    }
    try {
        # -NoExit 让窗口在脚本结束后保留，出错时能看到报错信息
        $psArgs = '-NoExit -ExecutionPolicy Bypass -File "' + $StartScript + '"'
        $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $psArgs -WorkingDirectory $ProjectDir `
            -WindowStyle Normal -PassThru
        # 先记状态再打印，网页那边一读到就知道「启动器正在启动」，不必自己再启动一次
        Save-LauncherState -State 'starting' -ProcessId $proc.Id -Note '已由启动器拉起 start.ps1'
        Write-Msg "已启动 start.ps1（建环境 / 装依赖 / 构建前端的进度在这里显示；服务的输出写进 data\server.log，面板「日志」页可以看）" Green
        Write-Log "已启动 start.ps1（PID $($proc.Id)）"
        return $true
    }
    catch {
        Write-Msg "启动失败：$($_.Exception.Message)" Red
        Write-Log "启动 start.ps1 失败: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-EnvCheck {
    <# 跑环境检测，写成报告；通过则按需自动启动后端 #>
    $script = Join-Path $PSScriptRoot 'env_check.ps1'
    if (-not (Test-Path $script)) {
        Write-Msg "找不到环境检测脚本: $script" Red
        Write-Log "找不到 $script"
        return $false
    }
    # 检测脚本的输出直接让它打到控制台（用户能看见进度），JSON 通过临时文件回收：
    # 子进程 stdout 里混着 Write-Host 的人看输出，从管道取 JSON 会解析失败。
    $jsonPath = Join-Path $env:TEMP ('douyin-env-' + [guid]::NewGuid().ToString('N') + '.json')
    $report = $null
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -JsonPath $jsonPath
        if (Test-Path $jsonPath) {
            try {
                $report = (Get-Content -Path $jsonPath -Raw -Encoding UTF8) | ConvertFrom-Json
            }
            catch { $report = $null }
        }
    }
    finally {
        if (Test-Path $jsonPath) { Remove-Item $jsonPath -Force -ErrorAction SilentlyContinue }
    }
    if (-not $report) {
        Write-Msg "环境检测没有返回结果，请看上面的输出" Red
        Write-Log "环境检测未返回可解析的结果"
        return $false
    }
    if ($report.summary.ok) {
        if ($NoStart) {
            Write-Msg "必要条件全部通过（未要求自动启动）" Green
        }
        else {
            Write-Msg "必要条件全部通过，准备启动后端服务..." Green
            Start-Backend | Out-Null
        }
        return $true
    }
    Write-Msg "必要条件未全部通过，已停止启动；请在 start.html 页面里按提示处理" Red
    Write-Log "必要条件未通过，未启动后端"
    return $false
}

# ── 决定要做什么 ──────────────────────────────────────────────────────────
$target = $Action
if (-not $target) { $target = Get-ActionFromUri -Value $Uri }
if (-not $target) { $target = 'check' }

Write-Msg ""
Write-Msg "抖音聊天记录导出 · 启动器（$target）" Cyan
Write-Msg "项目目录: $ProjectDir" Gray
Write-Log "动作: $target"

switch ($target) {
    'check' {
        # 检测顺带把协议注册好，之后网页里点按钮就能直接调用
        Install-Protocol | Out-Null
        Invoke-EnvCheck | Out-Null
    }
    'start' {
        Start-Backend | Out-Null
    }
    'install' {
        Install-Protocol | Out-Null
    }
    'uninstall' {
        try {
            Remove-Item -Path "HKCU:\Software\Classes\$ProtocolName" -Recurse -Force -ErrorAction Stop
            Write-Msg "已取消注册协议 $ProtocolName" Green
            Write-Log "已取消注册协议"
        }
        catch {
            Write-Msg "取消注册失败：$($_.Exception.Message)" Yellow
        }
    }
    'open' {
        if (Test-Path $StartHtml) {
            Start-Process -FilePath $StartHtml
            Write-Log "已打开 start.html"
        }
        else {
            Write-Msg "找不到 $StartHtml" Red
        }
    }
    default {
        Write-Msg "未知动作：$target（可用：check / start / install / uninstall / open）" Yellow
    }
}

Write-Msg ""
exit 0
