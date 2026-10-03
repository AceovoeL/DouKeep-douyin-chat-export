# 启动脚本：准备 venv 与依赖 → 构建前端 → 启动后端服务。
# 目录跟随本脚本所在位置，项目整体移动或改名后都无需修改路径。
#
# 用法（在项目根目录执行）：
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
# 直接运行报「禁止运行脚本」时，用上面这条命令即可。

# ============ 工具函数 ============
function Write-Step($msg) {
    Write-Host ""
    Write-Host "=====> $msg" -ForegroundColor Cyan
}

# ============ 进入项目目录（= 本脚本所在目录） ============
$projectDir = $PSScriptRoot
Write-Step "切换到项目目录: $projectDir"
Set-Location $projectDir

# ============ 创建虚拟环境（仅在不存在时） ============
$venvPython = Join-Path $projectDir "venv\Scripts\python.exe"
$venvActivate = Join-Path $projectDir "venv\Scripts\Activate.ps1"

if (Test-Path $venvActivate) {
    Write-Step "已存在虚拟环境 venv，跳过创建"
}
else {
    Write-Step "未检测到虚拟环境，正在创建: python -m venv venv"
    python -m venv venv
}

# ============ 激活虚拟环境 ============
Write-Step "激活虚拟环境: .\venv\Scripts\Activate.ps1"
try {
    & $venvActivate
}
catch {
    # 执行策略可能禁止运行 .ps1；后续步骤统一用 venv 的绝对路径，不影响启动。
    Write-Host "（激活失败，继续使用: $venvPython）" -ForegroundColor Yellow
}

# ============ 检查并修复 pip 启动器 ============
# 场景：venv 曾从别的目录（如 xxx-repair）整体移动过来，
# pip.exe 内部 launcher 仍指向旧路径，导致 "Fatal error in launcher"
Write-Step "检查 pip 是否可用"
& $venvPython -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Step "pip 不可用，正在重建: python -m ensurepip --upgrade"
    & $venvPython -m ensurepip --upgrade
    Write-Step "升级 pip: python -m pip install --upgrade --force-reinstall pip"
    & $venvPython -m pip install --upgrade --force-reinstall pip
}
else {
    Write-Step "pip 可用，跳过修复"
}

# ============ 安装 Python 依赖（仅在未安装时） ============
Write-Step "检查 Python 依赖是否已安装"
& $venvPython -c "import fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Step "Python 依赖已安装，跳过 pip install"
}
else {
    Write-Step "未检测到 Python 依赖，正在安装: python -m pip install -r requirements.txt"
    & $venvPython -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
}

# ============ 进入前端目录 ============
Write-Step "切换到前端目录: .\frontend"
Set-Location (Join-Path $projectDir "frontend")

# npm 在 PowerShell 里可能解析到被安全策略拦截的 npm.ps1，优先用 npm.cmd。
$npm = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { "npm.cmd" } else { "npm" }

# 国内直连 npm 官方源经常超时（表现为装到一半就断），默认用国内镜像。
$npmRegistry = "https://registry.npmmirror.com"

# ============ 检查 Node.js 版本 ============
# 前端用的 vite 7 要求 Node 20.19+ 或 22.12+，版本太低时报错信息很费解，先拦住。
$nodeVersion = (& node -v 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $nodeVersion) {
    Write-Host "未检测到 Node.js，请先安装 Node.js 20.19+ 或 22.12+：https://nodejs.org/" -ForegroundColor Red
    exit 1
}
$nodeOk = $false
if ($nodeVersion -match '^v(\d+)\.(\d+)') {
    $nodeMajor = [int]$Matches[1]
    $nodeMinor = [int]$Matches[2]
    if ($nodeMajor -gt 22) { $nodeOk = $true }
    elseif ($nodeMajor -eq 22 -and $nodeMinor -ge 12) { $nodeOk = $true }
    elseif ($nodeMajor -eq 20 -and $nodeMinor -ge 19) { $nodeOk = $true }
}
if (-not $nodeOk) {
    Write-Host "Node.js 版本过低（当前 $nodeVersion），前端构建需要 20.19+ 或 22.12+，请升级后重试。" -ForegroundColor Red
    exit 1
}

# ============ 安装前端依赖（以 vite 是否真的可用为准） ============
# 只看 node_modules 目录是否存在是不够的：目录可能残缺（从别的电脑整体复制过来、
# 或者上次 npm install 因为网络中断没装完），那时目录在、可执行文件却没有，
# 构建就会报「'vite' 不是内部或外部命令」。
$viteBin = Join-Path (Get-Location) "node_modules\.bin\vite.cmd"
$depsReady = (Test-Path ".\node_modules") -and (Test-Path $viteBin)

if ($depsReady) {
    Write-Step "已存在 node_modules 且 vite 可用，跳过 npm install"
}
else {
    if (Test-Path ".\node_modules") {
        Write-Step "node_modules 存在但缺少 vite（上次安装不完整），重新安装补齐"
    }
    else {
        Write-Step "未检测到 node_modules，正在安装前端依赖: npm install"
    }
    Write-Step "使用镜像源: $npmRegistry"
    & $npm install "--registry=$npmRegistry"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "镜像源安装失败，换官方源再试一次: npm install" -ForegroundColor Yellow
        & $npm install
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "npm install 失败，请检查上面的错误输出（网络或 Node.js 版本问题）" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $viteBin)) {
        Write-Host "依赖安装完成，但仍找不到 vite。请检查 frontend\package.json 的 devDependencies 里是否有 vite。" -ForegroundColor Red
        exit 1
    }
}

# ============ 构建前端 ============
Write-Step "构建前端: npm run build"
& $npm run build
if ($LASTEXITCODE -ne 0) {
    Write-Host "前端构建失败，请检查上面的错误输出" -ForegroundColor Red
    exit 1
}

# ============ 返回项目根目录 ============
Write-Step "返回项目根目录"
Set-Location $projectDir

# ============ 启动后端服务 ============
# data\ 只放聊天记录（数据库、下载的媒体），config\ 放设置、登录态和日志。
$dataDir = Join-Path $projectDir "data"
$configDir = Join-Path $projectDir "config"
$logDir = Join-Path $configDir "logs"
$serverLog = Join-Path $logDir "server.log"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 监听地址由面板「设置 → 局域网访问」决定（配置在 config\panel_config.json）：
#   打开 = 监听 0.0.0.0（局域网里的设备也能连），关闭 = 只监听 127.0.0.1。
# 这里只读文件、不引 JSON 解析（不同 PowerShell 版本上 ConvertFrom-Json 的严格程度不同），
# 因此失败一律退回「只监听本机」这个安全默认值。
$lanConfig = Join-Path $configDir "panel_config.json"
$listenHost = "127.0.0.1"
if (Test-Path $lanConfig) {
    $lanText = Get-Content $lanConfig -Raw -ErrorAction SilentlyContinue
    if ($lanText -match '"lan_access"\s*:\s*("?true"?)') { $listenHost = "0.0.0.0" }
}

Write-Step "启动后端服务: ${listenHost}:8000"
Write-Host "聊天浏览: http://127.0.0.1:8000"
Write-Host "控制面板: http://127.0.0.1:8000/panel"
if ($listenHost -eq "0.0.0.0") {
    Write-Host "已向局域网开放（监听 0.0.0.0）：同一个局域网里的设备可以用这台电脑的 IP 加端口访问" -ForegroundColor Yellow
}
Write-Host "服务输出: config\logs\server.log（面板左侧「日志」页可以直接看）" -ForegroundColor Cyan
Write-Host "停止服务: 面板「日志」页 →「停止程序」（也可以在这个窗口里按 Ctrl+C）" -ForegroundColor Cyan
Write-Host ""

# 服务的输出写进 config\logs\server.log，而不是只打在屏幕上：
#   · 用「启动服务（双击）.bat」启动时根本没有窗口，不落文件就等于什么都没留下；
#   · 面板「日志」页读的正是这份文件（旁边还有「停止程序」按钮）。
# 这里必须用 cmd 的重定向（不是 PowerShell 的 >）：PowerShell 会把输出转成 UTF-16
# 再写文件，面板按 UTF-8 读就成了乱码；cmd 是把子进程的字节原样写进文件，不缓冲、
# 实时可见。用 >> 追加，服务重启几次之后还能翻到前面的记录。
$env:PYTHONUTF8 = "1"                 # 让 python 按 UTF-8 写这个文件（Windows 默认 GBK）
$env:PYTHONIOENCODING = "utf-8"
& cmd.exe /c "`"$venvPython`" -m uvicorn backend.main:app --host $listenHost --port 8000 >> `"$serverLog`" 2>&1"
$serverExitCode = $LASTEXITCODE

Write-Host ""
Write-Host "后端服务已退出（退出码 $serverExitCode），它的完整输出在: config\logs\server.log" -ForegroundColor Yellow
exit $serverExitCode
