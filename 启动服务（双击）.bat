@echo off
rem ============================================================
rem  抖音聊天记录导出工具：双击本文件 = 在后台启动后端服务，并在本窗口显示启动进度。
rem
rem   · 服务已经在跑时不会再启动第二个，只把控制面板打开；
rem   · 服务就绪后浏览器会自动打开控制面板 http://127.0.0.1:8000/panel；
rem   · 要停止服务：面板左侧「日志」页 →「停止程序」（8000 端口随即释放）；
rem   · 一直没反应：看 config\logs\launcher.log（启动过程）和 config\logs\server.log（服务输出）。
rem
rem  本文件是 GBK 编码 + CRLF 换行：中文 Windows 的 cmd 按 GBK 读，
rem  存成 UTF-8 会把下面这些中文提示变成乱码。
rem  另外：chcp 65001 之后的行必须是纯 ASCII —— 那之后 cmd 按 UTF-8 解这些字节，
rem  中文会变成乱码，所以 :serve 里的中文注释一律写在 chcp 之前。
rem ============================================================

rem 写死中文代码页，免得在别的代码页下提示变乱码
chcp 936 >nul 2>nul

rem 8000 端口已经在监听 = 服务已经在跑：只打开面板，不重复启动
netstat -ano -p tcp | findstr ":8000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :open_panel

rem 被自己隐藏着拉起来的第二次：这一次真正启动服务
if /I "%~1"=="--serve" goto :serve

rem ── 第一次（用户双击的那一次）：把活交给隐藏窗口，本窗口留着显示进度 ──
rem 进度脚本（tools\launcher_progress.ps1）正常收尾会给出退出码 0 / 2 / 3
rem （就绪 / 等超时 / 启动失败），这三种它自己都已经把话说完了。
rem 别的退出码 = 这份脚本自己没跑起来 —— 这台机器上 Defender 的 AMSI 偶发崩溃就是如此
rem （屏幕上是一大段 .NET 异常）。所以先重试一次，再不行就退回纯 cmd 的简易等待：
rem 这个窗口绝不能一闪就没，那正是「以为卡死 / 以为坏了」的来源。
if not exist "%~dp0config\logs" mkdir "%~dp0config\logs" >nul 2>nul
echo.
echo  正在后台启动后端服务（服务本身没有窗口）。
echo  首次启动要建虚拟环境、装依赖、构建前端，可能要几分钟。
echo  下面会实时显示进行到哪一步；这个窗口只是进度显示，可以最小化，关掉它不影响服务。
echo.
rem 把自己隐藏着再跑一遍（--serve 那次负责真正启动）
rem 这里让 cmd.exe 去拉它：直接 Start-Process 这个 bat 会走 Windows 的「关联打开」，带下载标记
rem （Zone.Identifier）的文件会再弹一次「无法验证发布者」，而且那一次在隐藏窗口里，不点它服务起不来。
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -WorkingDirectory '%~dp0.' -FilePath $env:ComSpec -ArgumentList '/c','%~nx0 --serve'"

set "PROGRESS=%~dp0tools\launcher_progress.ps1"
set "LOGFILE=%~dp0config\logs\launcher.log"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROGRESS%" -ProjectDir "%~dp0." -LogPath "%LOGFILE%" -Port 8000
if "%errorlevel%"=="0" exit /b 0
if "%errorlevel%"=="2" exit /b 0
if "%errorlevel%"=="3" exit /b 0

echo.
echo  进度脚本没能跑起来（这台电脑的安全软件偶尔会拦住 PowerShell 脚本），重试一次……
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROGRESS%" -ProjectDir "%~dp0." -LogPath "%LOGFILE%" -Port 8000 2>nul
if "%errorlevel%"=="0" exit /b 0
if "%errorlevel%"=="2" exit /b 0
if "%errorlevel%"=="3" exit /b 0

rem ── 简易等待：纯 cmd，不依赖 PowerShell（PowerShell 被安全软件拦住时走这里）──
echo.
echo  改用简易等待：每 3 秒一个点，服务起来后照样自动打开控制面板。
echo  详细日志：config\logs\launcher.log
echo.
rem 先留几秒给隐藏那次把日志文件重建出来，免得读到上一轮留下的结束标记
timeout /t 5 /nobreak >nul 2>nul || ping -n 6 127.0.0.1 >nul
set /a TICKS=0

:wait_loop
netstat -ano -p tcp | findstr ":8000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :wait_ready
rem start.ps1 跑完（或被拦掉）时，:serve 会往日志尾补一行纯 ASCII 的结束标记
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
echo  服务已就绪，正在打开控制面板……
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process 'http://127.0.0.1:8000/panel'"
exit /b 0

:wait_failed
echo.
echo.
echo  启动脚本已经退出，但 8000 端口没起来（多半也是被安全软件拦掉的）。
echo  启动日志：config\logs\launcher.log
echo  想看完整输出就这样手动重试：powershell -ExecutionPolicy Bypass -File .\start.ps1
pause
exit /b 0

:wait_slow
echo.
echo.
echo  等了 10 分钟还没起来，先停下来。日志：config\logs\launcher.log
echo  也可能只是装依赖特别慢；重新双击会再启动一个安装过程，先看一眼日志再决定。
pause
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
rem config\logs\server.log（面板「日志」页读的就是那份）。这份 launcher.log 只记启动过程，
rem 可见窗口里的 tools\launcher_progress.ps1 就是靠读它来报进度的。
rem 跑完再往日志尾补一行结束标记（纯 ASCII），进度脚本和简易等待都靠它判断
rem 「不是卡住，是已经结束了」。
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
