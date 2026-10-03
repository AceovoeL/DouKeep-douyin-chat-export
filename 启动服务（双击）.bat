@echo off
rem ============================================================
rem  抖音聊天记录导出工具：双击本文件 = 在后台（没有窗口）启动后端服务。
rem
rem   · 服务已经在跑时不会再启动第二个，只把控制面板打开；
rem   · 服务就绪后浏览器会自动打开控制面板 http://127.0.0.1:8000/panel；
rem   · 要停止服务：面板左侧「日志」页 →「停止程序」（8000 端口随即释放）；
rem   · 一直没反应：看 data\launcher.log（启动过程）和 data\server.log（服务输出）。
rem
rem  本文件是 GBK 编码 + CRLF 换行：中文 Windows 的 cmd 按 GBK 读，
rem  存成 UTF-8 会把下面这些中文提示变成乱码。
rem ============================================================

rem 写死中文代码页，免得在别的代码页下提示变乱码
chcp 936 >nul 2>nul

rem 8000 端口已经在监听 = 服务已经在跑：只打开面板，不重复启动
netstat -ano -p tcp | findstr ":8000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :open_panel

rem 被自己隐藏着拉起来的第二次：这一次真正启动服务
if /I "%~1"=="--serve" goto :serve

rem ── 第一次（用户双击的那一次）：交代一句，然后把活交给隐藏窗口 ──
if not exist "%~dp0data" mkdir "%~dp0data" >nul 2>nul
echo.
echo  正在后台启动后端服务（不会有窗口）……
echo  首次启动要建虚拟环境、装依赖、构建前端，可能要几分钟。
echo  服务就绪后浏览器会自动打开控制面板。
echo  这个黑窗口马上就关掉，不用管它；一直没反应时看 data\launcher.log。
echo.
rem 把自己隐藏着再跑一遍（--serve 那次负责真正启动），并等端口起来后打开面板；
rem 10 分钟还没起来，就把日志文件直接打开给人看。
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%~f0' -ArgumentList '--serve'; for($i=0;$i -lt 300;$i++){ if(netstat -ano -p tcp | Select-String ':8000\s+.*LISTENING'){ Start-Process 'http://127.0.0.1:8000/panel'; exit }; Start-Sleep -Seconds 2 }; $log='%~dp0data\launcher.log'; if(-not (Test-Path $log)){ $log='%~dp0data\server.log' }; if(Test-Path $log){ Start-Process $log }"
exit /b 0

:open_panel
echo.
echo  后端服务已经在运行，正在打开控制面板……
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process 'http://127.0.0.1:8000/panel'"
exit /b 0

:serve
rem ── 隐藏着跑的那一次：start.ps1 负责建虚拟环境 / 装依赖 / 构建前端 / 起服务 ──
rem 这里的 > 是 cmd 的重定向（不是 PowerShell 的）：子进程（pip / npm / 服务）的字节
rem 原样落进文件，而且不缓冲。服务的输出自己不在这里 —— start.ps1 把它写进
rem data\server.log（面板「日志」页读的就是那份）。这份 launcher.log 只记启动过程。
rem chcp 65001 + PYTHONUTF8：让这份日志整体是 UTF-8，不和 GBK 混在一起。
chcp 65001 >nul 2>nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "%~dp0data" mkdir "%~dp0data" >nul 2>nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" > "%~dp0data\launcher.log" 2>&1
exit /b 0
