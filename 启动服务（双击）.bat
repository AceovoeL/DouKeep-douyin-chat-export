@echo off
rem ============================================================
rem  抖音聊天记录导出：双击本文件就能启动后端服务。
rem
rem   · 服务已经在跑：不再启动第二个，只打开控制面板；
rem   · 服务启动好之后，浏览器会自动打开控制面板 http://127.0.0.1:8000/panel；
rem   · 想停服务：面板左侧「日志」页 →「停止程序」；
rem   · 一直没动静：看 config\logs\launcher.log（启动过程）和
rem     config\logs\server.log（服务自己的输出）。
rem
rem  这个文件是 GBK 编码 + CRLF 换行：中文 Windows 的 cmd 按 GBK 读它，
rem  存成 UTF-8 会让下面这些中文提示变成乱码；
rem  而 chcp 65001 之后的行必须是纯 ASCII（那之后 cmd 按 UTF-8 解字节），
rem  所以 :serve 段里的中文注释都写在 chcp 之前。
rem ============================================================

rem 固定用中文代码页，免得在别的代码页下这些提示变成乱码
chcp 936 >nul 2>nul

rem 8000 端口有人在听 = 服务已经在跑：只打开面板，不再启动一个
netstat -ano -p tcp | findstr ":8000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :open_panel

rem 下面这次是被隐藏窗口拉起来的第二遍，真正启动服务的就是它
if /I "%~1"=="--serve" goto :serve

rem ── 第一次用：还没走过 start.html 的，先请去看一眼，这个窗口到此为止 ──
rem 有些人不会先看 README，直接双击本文件，然后卡在「没装 Python / 没装 Node.js」
rem 这种本来能提前发现的问题上。所以这里先拦一下：只要找不到「已经跑过首次检查」的
rem 痕迹，就打开 start.html 并结束这个窗口，不启动服务。下面三个痕迹有一个就算：
rem   · config\env-report.js     —— start.html 那次环境检查写出来的报告
rem   · config\panel_config.json —— 服务跑过（早就不是新装的）
rem   · config\.first-run-done   —— 这次帮用户打开 start.html 时留下的记号
rem 只拦「用户双击」这一条路：--serve 那次是真正启动服务的，不能拦。
if exist "%~dp0config\.first-run-done" goto :ready_to_start
if exist "%~dp0config\env-report.js" goto :ready_to_start
if exist "%~dp0config\panel_config.json" goto :ready_to_start

if not exist "%~dp0config" mkdir "%~dp0config" >nul 2>nul
> "%~dp0config\.first-run-done" echo first run: start.html was opened for the user
start "" "%~dp0start.html"
echo.
echo  第一次使用这个工具：已经帮你打开 start.html（首次运行检查）。
echo  它会检查这台电脑缺不缺 Python / Node.js，缺什么会告诉你怎么装；
echo  等检查通过，再双击一次本文件就会启动服务。
echo.
echo  已经用过 start.html 的话，再双击一次也是直接启动。
echo.
echo  这个窗口 15 秒后自动关闭（按任意键立刻关）。
timeout /t 15 >nul 2>nul || ping -n 16 127.0.0.1 >nul
exit /b 0

:ready_to_start

rem ── 用户双击的那一次：把启动交给隐藏窗口，本窗口留下来显示进度 ──
rem 进度脚本（tools\launcher_progress.ps1）正常收尾会返回 0 / 2 / 3
rem （已就绪 / 等超时 / 启动失败），这三种它自己都已经把话说清楚了。
rem 其它退出码说明它自己没跑起来 —— 这台机器上 Defender 的 AMSI 偶尔会崩，
rem 屏幕上是一大段 .NET 异常。所以先重试一次，再不行就退回纯 cmd 的简易等待。
rem 这个窗口不能一闪而过：用户会以为「卡死了 / 坏了」。
if not exist "%~dp0config\logs" mkdir "%~dp0config\logs" >nul 2>nul
echo.
echo  正在后台启动后端服务（服务自己没有窗口）。
echo  第一次启动要建虚拟环境、装依赖、构建前端，可能要几分钟。
echo  下面会实时显示进行到哪一步；这个窗口只是进度显示，可以最小化，关掉它不影响服务。
echo.
rem 用一个隐藏窗口把自己再跑一遍（带 --serve 的那次负责真正启动）
rem 这里特意让 cmd.exe 去拉它：直接 Start-Process 这个 bat 会走 Windows 的「关联打开」，
rem 从网上下载来的文件带着下载标记，这一步会再弹一次「无法验证发布者」，
rem 而且弹框在隐藏窗口里 —— 用户不点，服务就起不来。
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -WorkingDirectory '%~dp0.' -FilePath $env:ComSpec -ArgumentList '/c','%~nx0 --serve'"

set "PROGRESS=%~dp0tools\launcher_progress.ps1"
set "LOGFILE=%~dp0config\logs\launcher.log"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROGRESS%" -ProjectDir "%~dp0." -LogPath "%LOGFILE%" -Port 8000
if "%errorlevel%"=="0" exit /b 0
if "%errorlevel%"=="2" exit /b 0
if "%errorlevel%"=="3" exit /b 0

echo.
echo  进度脚本没能跑起来（这台机器上的安全软件偶尔会拦住 PowerShell 脚本），重试一次……
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROGRESS%" -ProjectDir "%~dp0." -LogPath "%LOGFILE%" -Port 8000 2>nul
if "%errorlevel%"=="0" exit /b 0
if "%errorlevel%"=="2" exit /b 0
if "%errorlevel%"=="3" exit /b 0

rem ── 简易等待：不依赖 PowerShell（它被安全软件拦住时走这里）──
echo.
echo  改用简易等待：每 3 秒显示一个点，服务起来后照样自动打开控制面板。
echo  详细日志：config\logs\launcher.log
echo.
rem 先等几秒，让隐藏那次把日志重建成新的一份，免得读到上一轮留下的结束标记
timeout /t 5 /nobreak >nul 2>nul || ping -n 6 127.0.0.1 >nul
set /a TICKS=0

:wait_loop
netstat -ano -p tcp | findstr ":8000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :wait_ready
rem start.ps1 结束时，:serve 会往日志末尾补一行纯 ASCII 的结束标记
findstr /C:"--- start.ps1 exited with code" "%LOGFILE%" >nul 2>nul
if not errorlevel 1 goto :wait_failed
<nul set /p "=."
timeout /t 3 /nobreak >nul 2>nul || ping -n 4 127.0.0.1 >nul
set /a TICKS+=1
if %TICKS% LSS 200 goto :wait_loop
goto :wait_slow

:wait_ready
echo.
echo.
echo  服务已经就绪，正在打开控制面板……
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process 'http://127.0.0.1:8000/panel'"
exit /b 0

:wait_failed
echo.
echo.
echo  启动脚本已经退出，但 8000 端口没起来（多半也是被安全软件拦掉了）。
echo  启动日志：config\logs\launcher.log
echo  想看完整输出，可以这样手动重试：powershell -ExecutionPolicy Bypass -File .\start.ps1
pause
exit /b 0

:wait_slow
echo.
echo.
echo  等了 10 分钟还没起来，这里就不再等了。日志：config\logs\launcher.log
echo  也可能只是装依赖特别慢；重新双击会再走一遍安装，建议先看一眼日志再决定。
pause
exit /b 0

:open_panel
echo.
echo  后端服务已经在运行，正在打开控制面板……
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process 'http://127.0.0.1:8000/panel'"
exit /b 0

:serve
rem ── 隐藏运行的那一次：start.ps1 负责建虚拟环境 / 装依赖 / 构建前端 / 启动服务 ──
rem 这里的 > 是 cmd 的重定向（不是 PowerShell 的）：pip、npm、服务自己的输出都会
rem 原样写进文件，而且不缓冲。服务自己的输出不在这里 —— start.ps1 把它写进
rem config\logs\server.log，面板「日志」页读的就是那一份；这份 launcher.log
rem 只记启动过程，可见窗口里的 tools\launcher_progress.ps1 靠读它报进度。
rem 结束时再往日志末尾补一行结束标记（纯 ASCII），进度脚本和简易等待都靠它
rem 判断「不是卡住，是已经结束了」。
rem chcp 65001 + PYTHONUTF8：让这份日志整体是 UTF-8，不和 GBK 混在一起。
chcp 65001 >nul 2>nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "%~dp0config\logs" mkdir "%~dp0config\logs" >nul 2>nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" > "%~dp0config\logs\launcher.log" 2>&1
set "START_RC=%errorlevel%"
echo --- start.ps1 exited with code %START_RC% --- >> "%~dp0config\logs\launcher.log"
exit /b 0
