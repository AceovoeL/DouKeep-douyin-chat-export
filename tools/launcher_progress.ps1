<#
「启动服务（双击）.bat」双击后那个可见窗口里跑的就是这份脚本：把后台启动的进度说出来。

为什么要它：双击 bat 时，真正干活的（建虚拟环境 → 装 Python 依赖 → 装前端依赖 →
构建前端 → 起服务）是**另一个隐藏窗口**里的 start.ps1，它的输出全写进
config\logs\launcher.log，屏幕上什么都看不到。首次启动要好几分钟，一个不动的黑窗口很
容易被当成卡死 —— 这里把那份日志读出来，实时说清楚「正在干什么、已经等了多久」。

几条约定（和 start.ps1、bat 一起守）：

 * 8000 端口一通就等于服务起来了（bat 判断「已经在跑」用的是同一个信号）；
 * launcher.log 里最新的 ``=====> xxx`` 就是 start.ps1 当前那一步（它用 Write-Step 写）；
 * 那份日志在启动过程中一直被另一个进程占着，普通读法会报「正由另一进程使用」，所以
   这里自己开 FileStream，并明确允许对方同时读写；
 * 只认修改时间晚于本脚本启动时间的内容 —— 否则会把上一次启动留下的旧步骤当成本次；
 * start.ps1 中途退出时 bat 会往日志尾补一行 ``--- start.ps1 exited with code N ---``，
   看到这一行而端口还没起来，就是启动失败：把日志最后几行贴出来，别让人对着黑窗口猜。

给 bat 看的退出码（它按这个决定还要不要兜底，是跨文件的约定）：

 * 0 —— 服务起来了（浏览器已打开，窗口等 8 秒自动关）；
 * 2 —— 等超时了，已经在这里把话说清楚，窗口停着等回车；
 * 3 —— start.ps1 退出了（启动失败），同样已经把话说清楚；
 * 其它任何退出码 —— 这份脚本自己没跑起来（例如这台机器上 Defender 的 AMSI 偶发崩溃），
   bat 会重试一次，再不行就退回纯 cmd 的简易等待，免得窗口一闪而过什么都没留下。

单独跑（默认最多等 30 分钟；-NoBrowser 只显示进度、不弹浏览器）：
    powershell -ExecutionPolicy Bypass -File .\tools\launcher_progress.ps1
#>
[CmdletBinding()]
param(
    # 项目根目录（不传就按本脚本的位置往上找一层）
    [string]$ProjectDir = "",
    # start.ps1 的输出，也是这里读的那份日志
    [string]$LogPath = "",
    [int]$Port = 8000,
    # 等这么久还没起来就停下来给人看日志（别让人对着窗口无限等）
    [int]$TimeoutSeconds = 1800,
    [switch]$NoBrowser
)

# ── 路径 ─────────────────────────────────────────────────────────────────
if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
try { $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path } catch { }
if (-not $LogPath) { $LogPath = Join-Path $ProjectDir "config\logs\launcher.log" }

$serverLog = Join-Path $ProjectDir "config\logs\server.log"
$viewerUrl = "http://127.0.0.1:$Port/"
$panelUrl = "http://127.0.0.1:$Port/panel"
$waitMinutes = [Math]::Max(1, [int][Math]::Ceiling($TimeoutSeconds / 60))

# ── 小工具 ───────────────────────────────────────────────────────────────

#: 原地刷新的那一行有多长（用来把它擦干净再写下一句）
$script:statusWidth = 0

function Clear-StatusLine {
    if ($script:statusWidth -gt 0) {
        Write-Host ("`r" + (" " * $script:statusWidth) + "`r") -NoNewline
        $script:statusWidth = 0
    }
}

function Show-StatusLine([string]$Text) {
    $pad = [Math]::Max(0, $script:statusWidth - $Text.Length)
    Write-Host ("`r" + $Text + (" " * $pad)) -NoNewline -ForegroundColor DarkGray
    $script:statusWidth = $Text.Length
}

function Format-Elapsed([TimeSpan]$Span) {
    return ('{0:00}:{1:00}' -f [int][Math]::Floor($Span.TotalMinutes), $Span.Seconds)
}

function Write-Section([string]$Text, [string]$Color = "Cyan") {
    Clear-StatusLine
    Write-Host ("  " + $Text) -ForegroundColor $Color
}

# 8000 端口通没通。和 bat 用 netstat 判断「服务已经在跑」是同一个信号，
# 只是这里用 TCP 连接，省掉每次起一个 netstat 进程的开销。
function Test-ServerPort([int]$PortNumber) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $PortNumber, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(500)) {
            $client.EndConnect($async)
            return $true
        }
        return $false
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

# 读日志最后一段。日志被 start.ps1（严格说是 bat 的重定向）占着，必须自己开流并允许
# 对方同时读写；读不到就返回 $null，让调用方下一轮再试。
function Read-LogLines([string]$Path, [int]$MaxBytes = 16384) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }

    $buffer = $null
    $read = 0
    $start = 0
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    }
    catch {
        return $null
    }
    try {
        $length = $stream.Length
        $take = [Math]::Min($MaxBytes, [int]$length)
        $start = [int]($length - $take)
        if ($start -gt 0) { $stream.Seek($start, [System.IO.SeekOrigin]::Begin) | Out-Null }
        $buffer = New-Object byte[] $take
        $read = $stream.Read($buffer, 0, $take)
    }
    catch {
        return $null
    }
    finally {
        $stream.Dispose()
    }

    $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
    if ($start -gt 0) {
        # 从中间截断时头一行多半是半截，丢掉
        $cut = $text.IndexOf("`n")
        $text = if ($cut -ge 0) { $text.Substring($cut + 1) } else { "" }
    }
    return ($text.TrimStart([char]0xFEFF) -split "`r?`n")
}

# start.ps1 每做一件事就写一行「=====> xxx」，最后一行就是它现在在干什么。
function Get-CurrentStep([string[]]$Lines) {
    if (-not $Lines) { return "" }
    $step = ""
    foreach ($line in $Lines) {
        if ($line -match '^\s*=====>\s*(.+?)\s*$') { $step = $Matches[1] }
    }
    return $step
}

# bat 在 start.ps1 退出后补的那一行（纯 ASCII，见 bat 里的注释）。
function Get-FinishedCode([string[]]$Lines) {
    if (-not $Lines) { return $null }
    $code = $null
    foreach ($line in $Lines) {
        if ($line -match '^---\s*start\.ps1 exited with code (-?\d+)\s*---\s*$') {
            $code = [int]$Matches[1]
        }
    }
    return $code
}

# 这一步大概要多久、为什么慢 —— 有预期才不会被当成卡死。
function Get-StepHint([string]$Step) {
    if (-not $Step) { return "" }
    if ($Step -match '虚拟环境') { return '首次要下载 Python 运行环境，通常 1~2 分钟' }
    if ($Step -match 'pip') { return '检查（必要时修复）pip，通常几十秒' }
    if ($Step -match 'Python 依赖') { return '要下载 FastAPI、Playwright 等包，首次通常 1~5 分钟' }
    if ($Step -match 'Playwright') { return '核对浏览器内核，缺了自动下载，首次通常 1~3 分钟' }
    if ($Step -match 'Node|node') { return '检查 Node.js 版本' }
    if ($Step -match 'npm install|前端依赖') { return '正在下载前端依赖包，首次通常 1~5 分钟（网速慢会更久）' }
    if ($Step -match '构建前端') { return '正在打包前端界面，通常 10~60 秒' }
    if ($Step -match '启动后端服务') { return '服务正在起来，马上就好' }
    if ($Step -match '目录') { return '在切目录 / 收尾，下一步马上开始' }
    return ""
}

# 出问题时把日志最后几行贴到窗口里 —— 比让人自己去翻文件快得多。
function Get-LogTail([string]$Path, [int]$Count = 12) {
    $lines = Read-LogLines -Path $Path -MaxBytes 32768
    if (-not $lines) { return @() }
    $kept = @()
    foreach ($line in $lines) {
        if (-not [string]::IsNullOrWhiteSpace($line)) { $kept += $line.TrimEnd() }
    }
    if ($kept.Count -le $Count) { return $kept }
    return $kept[($kept.Count - $Count)..($kept.Count - 1)]
}

# ── 开场白 ───────────────────────────────────────────────────────────────
$startedAt = Get-Date
$deadline = $startedAt.AddSeconds($TimeoutSeconds)

Write-Host ""
Write-Host "  抖音聊天记录导出工具 · 正在后台启动服务" -ForegroundColor White
Write-Host "  这个窗口只显示启动进度；服务本身跑在后台（没有窗口），关掉这个窗口不影响它。" -ForegroundColor DarkGray
Write-Host "  首次启动要建虚拟环境、装依赖、构建前端，通常几分钟，请耐心等。" -ForegroundColor DarkGray
Write-Host "  最多等 $waitMinutes 分钟；详细输出在 config\logs\launcher.log。" -ForegroundColor DarkGray
Write-Host ""

# ── 盯着端口和日志 ───────────────────────────────────────────────────────
$spinChars = "|/-\"
$spinIndex = 0
$lastStep = ""
$logFresh = $false
$exitCode = $null
$isReady = $false
$isTimeout = $false

while ($true) {
    if (Test-ServerPort -PortNumber $Port) { $isReady = $true; break }

    # 日志的修改时间晚于本次启动时间，才认里面的内容（否则是上一次启动留下的）
    if (-not $logFresh) {
        try {
            $logFresh = (Get-Item -LiteralPath $LogPath -ErrorAction Stop).LastWriteTime -ge $startedAt
        }
        catch {
            $logFresh = $false
        }
    }

    $lines = if ($logFresh) { Read-LogLines -Path $LogPath } else { $null }

    if ($lines) {
        $step = Get-CurrentStep -Lines $lines
        if ($step -and $step -ne $lastStep) {
            $lastStep = $step
            $stamp = Format-Elapsed ((Get-Date) - $startedAt)
            Write-Section "[$stamp] 正在进行：$step" "Cyan"
            $hint = Get-StepHint -Step $step
            if ($hint) { Write-Host "           提示：$hint" -ForegroundColor DarkGray }
        }
        $exitCode = Get-FinishedCode -Lines $lines
        if ($null -ne $exitCode) { break }
    }

    if ((Get-Date) -ge $deadline) { $isTimeout = $true; break }

    $spinIndex = ($spinIndex + 1) % $spinChars.Length
    $waited = Format-Elapsed ((Get-Date) - $startedAt)
    Show-StatusLine ("      " + $spinChars[$spinIndex] + " 已等待 $waited ……（这个窗口只显示进度，可以最小化）")
    Start-Sleep -Seconds 2
}

Clear-StatusLine

# ── 三种收尾：起来了 / 启动脚本退出了 / 等超时了 ──────────────────────────
if ($isReady) {
    $used = Format-Elapsed ((Get-Date) - $startedAt)
    Clear-StatusLine
    Write-Host ""
    Write-Host "  [就绪] 服务已经起来了，用时 $used" -ForegroundColor Green
    Write-Host "  聊天浏览：$viewerUrl" -ForegroundColor Cyan
    Write-Host "  控制面板：$panelUrl" -ForegroundColor Cyan
    Write-Host "  服务输出：config\logs\server.log（面板「日志」页也能直接看）" -ForegroundColor DarkGray
    Write-Host "  停止服务：面板「日志」页 →「停止程序」" -ForegroundColor DarkGray
    if (-not $NoBrowser) {
        Write-Host "  正在用浏览器打开控制面板……" -ForegroundColor DarkGray
        Start-Process $panelUrl
    }
    Write-Host ""
    Write-Host "  这个窗口 8 秒后自动关闭（按任意键立刻关闭；关掉窗口不影响后台服务）。" -ForegroundColor DarkGray
    for ($i = 0; $i -lt 8; $i++) {
        Start-Sleep -Seconds 1
        try {
            if ([Console]::KeyAvailable) { [void][Console]::ReadKey($true); break }
        }
        catch {
            # 输入被重定向（不是真的控制台）时 KeyAvailable 会报错：那就老实等满 8 秒
            continue
        }
    }
    exit 0
}

Write-Host ""
# 这台机器上 PowerShell 编译脚本时偶发 AccessViolation（Defender 的 AMSI 扫描崩了），
# start.ps1 因此干不了活、退出码是 0xC0000005 的十进制形式。报清楚比让人猜强。
$amsiCrash = ($exitCode -eq -1073741819)
if ($null -ne $exitCode) {
    Write-Host "  [失败] 启动没有成功：start.ps1 已经退出（退出码 $exitCode），但 $Port 端口一直没起来。" -ForegroundColor Red
}
else {
    Write-Host "  [超时] 等了 $waitMinutes 分钟还没起来，先停下来把日志给你看。" -ForegroundColor Red
    Write-Host "  也可能只是特别慢（比如网速不好装依赖）：先别急着重开 —— 重新双击会再启动一个安装过程。" -ForegroundColor DarkGray
}

$tail = Get-LogTail -Path $LogPath
if (-not $amsiCrash -and $tail) {
    foreach ($line in $tail) {
        if ($line -like "*AmsiScanBuffer*") { $amsiCrash = $true }
    }
}
if ($tail.Count -gt 0) {
    Write-Host ""
    Write-Host "  日志里的最后几行：" -ForegroundColor Yellow
    foreach ($line in $tail) { Write-Host "      $line" -ForegroundColor Gray }
}
else {
    Write-Host ""
    Write-Host "  （启动日志还没有内容或暂时读不到）" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  完整启动日志：$LogPath" -ForegroundColor DarkGray
Write-Host "  服务日志：    $serverLog" -ForegroundColor DarkGray
Write-Host "  常见原因：没装 Node.js 20.19+ / 22.12+、Python 低于 3.10、网络装不上依赖。" -ForegroundColor DarkGray
if ($amsiCrash) {
    Write-Host "  这次的退出码/日志看着是这台电脑的安全软件（Defender）扫描 PowerShell 脚本时崩了，" -ForegroundColor Yellow
    Write-Host "  和项目本身无关：直接再双击一次「启动服务（双击）.bat」重试通常就好。" -ForegroundColor Yellow
}
Write-Host "  手动重试（能看到完整输出）：powershell -ExecutionPolicy Bypass -File .\start.ps1" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  按回车键关闭这个窗口。" -ForegroundColor Yellow
try { Read-Host | Out-Null } catch { Start-Sleep -Seconds 5 }

# 给 bat 的退出码（见文件头的约定）：3 = start.ps1 退出了，2 = 等超时；两种情况上面都报过了
if ($null -ne $exitCode) { exit 3 } else { exit 2 }
