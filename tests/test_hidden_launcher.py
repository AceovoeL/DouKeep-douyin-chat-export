"""隐藏启动（根目录的「启动服务（双击）.bat」）+ 可见窗口里的启动进度 + 服务日志。

约定（几个文件一起守住）：

* ``start.ps1``（Windows）和 ``start.sh``（macOS / Linux）都把服务输出写进
  ``config/logs/server.log``，所以「日志」页不管服务是怎么起来的都有内容；
* Windows 上必须用 **cmd 的重定向**：PowerShell 的 ``>`` 会把输出转成 UTF-16 写文件，
  面板按 UTF-8 读就成了乱码，而且会缓冲（日志半天不刷新）。用 ``>>`` 追加，服务重启
  几次之后还能翻到前面的记录；
* ``「启动服务（双击）.bat」`` 负责「隐藏启动」和「已经在跑就只开面板」，启动过程
  （pip / npm 的报错）单独写 ``config/logs/launcher.log``，不和服务的日志抢同一个文件 ——
  两个进程同时追加一个文件，Windows 上后一个会直接打不开；
* 双击留下的那个窗口不能是个不动的黑窗口：它跑 ``tools/launcher_progress.ps1``，读
  ``launcher.log`` 报「现在进行到哪一步、已经等了多久」。首次启动要建虚拟环境、装依赖、
  构建前端，几分钟没有动静用户就会以为卡死；起不来时也要在窗口里说清楚并把日志最后
  几行贴出来（以前的做法是等 10 分钟没反应就去打开日志文件，现在直接显示在窗口里）。
* 但 PowerShell 本身也会被拦：这台机器上 Defender 的 AMSI 偶发让 ``AmsiScanBuffer`` 崩溃
  （退出码 0xC0000005），进度脚本会带着一大段 .NET 异常直接死掉。所以 bat 里是三级兜底
  —— 进度脚本（退出码 0/2/3 = 就绪/超时/启动失败，它自己会说清楚）→ 重试一次 → 纯 cmd
  的简易等待（每 3 秒一个点）。窗口绝不能一闪而过。
* 隐藏那次启动还要绕开 Windows 的「关联打开」：带下载标记（``Zone.Identifier``）的 bat
  被 ``Start-Process`` 拉起时会再弹一次「无法验证发布者」，而且弹在隐藏窗口里 —— 不点它
  服务就起不来。所以改成让 ``cmd.exe`` 去拉它（详见 ``tests/test_download_mark.py``）。

（2026-10-04 起日志从 ``data/`` 挪到 ``config/logs/``：data/ 只放聊天记录；
同一天给双击启动补上了可见窗口的进度显示。）

这一层是脚本文件里的约定，跑不了单元测试，所以按项目的惯例把跨文件契约、编码和
换行钉住（bat 丢了 GBK/CRLF 会在中文 Windows 上乱码或让 ``goto`` 失效）。
"""
import pathlib
import re

from backend import control_panel as cp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BAT_NAME = "启动服务（双击）.bat"
BAT = REPO_ROOT / BAT_NAME
START_PS1 = REPO_ROOT / "start.ps1"
START_SH = REPO_ROOT / "start.sh"
PROGRESS = REPO_ROOT / "tools" / "launcher_progress.ps1"
README = REPO_ROOT / "README.md"

#: 服务输出落在这里，也是面板「日志」页读的那一份
SERVER_LOG = "config/logs/server.log"
#: 启动过程（pip / npm / 建虚拟环境）落在 bat 自己这份里
LAUNCHER_LOG = "config\\logs\\launcher.log"
#: 双击后那个可见窗口读的就是上面那份日志：它靠这一行判断 start.ps1 是结束还是还在跑
EXIT_MARKER = "--- start.ps1 exited with code"


def _bat_text() -> str:
    """bat 是 GBK 编码的：中文 Windows 的 cmd 按 GBK 读它。"""
    return BAT.read_bytes().decode("gbk")


# ── 根目录的 bat ──────────────────────────────────────────────────────────

def test_the_launcher_bat_exists_at_the_repo_root():
    assert BAT.is_file(), f"根目录应该有「{BAT_NAME}」：双击就能启动后端"


def test_the_bat_is_gbk_with_crlf():
    """bat 必须是 GBK + CRLF。

    * UTF-8 的中文 bat，cmd 按 GBK 解出来是乱码（提示语全废）；
    * LF-only 的 bat 会让 ``goto`` / ``if`` 这类行号跳转失效（老 cmd 的经典坑）。
    """
    data = BAT.read_bytes()

    assert not data.startswith(b"\xef\xbb\xbf"), "bat 不该有 BOM"
    text = data.decode("gbk")                    # 解不出来就说明不是 GBK
    assert "\ufffd" not in text
    assert re.search(r"(?<!\r)\n", text) is None, "每一行都要 CRLF"


def test_the_bat_keeps_the_ascii_only_part_after_chcp_65001():
    """``chcp 65001`` 之后的行必须是纯 ASCII。

    cmd 是按「当前代码页」解批处理文件里接下来这些行的，切到 UTF-8 之后那些 GBK 中文
    提示会变成乱码（并且可能把 ``if`` / ``goto`` 也一起弄坏）。:serve 这段是隐藏跑的，
    屏幕上没人看，所以把中文注释一律写在 ``chcp`` 之前就行。
    """
    lines = _bat_text().split("\r\n")
    index = lines.index("chcp 65001 >nul 2>nul")

    for line in lines[index + 1:]:
        assert line.isascii(), f"chcp 65001 之后出现了非 ASCII 内容：{line}"


def test_the_bat_only_starts_the_service_once():
    """端口在监听 = 服务已经在跑：只打开面板，不再启动第二个。"""
    text = _bat_text()

    assert 'findstr ":8000" | findstr "LISTENING"' in text
    assert "if not errorlevel 1 goto :open_panel" in text


def test_the_bat_starts_the_service_in_a_hidden_window():
    """隐藏那次启动要走 ``cmd.exe``，不能直接 ``Start-Process`` 这个 bat。

    直接 Start-Process 会经过 Windows 的「关联打开」：从网上下载来的 bat 带着
    ``Zone.Identifier`` 标记时，这一步还会再弹一次「无法验证发布者」——而且那次弹框在
    隐藏窗口里，不点它服务根本起不来（用户反馈的「明明点了运行还是卡住」）。cmd.exe
    自己不做这个检查，所以绕它一下。
    """
    text = _bat_text()

    assert 'if /I "%~1"=="--serve" goto :serve' in text, "靠 --serve 把自己再跑一遍"
    assert "-WindowStyle Hidden" in text
    assert "-FilePath $env:ComSpec" in text
    assert "-ArgumentList '/c','%~nx0 --serve'" in text


def test_the_bat_shows_progress_in_its_visible_window():
    """双击留下的窗口跑进度脚本 —— 服务和进度显示分开，各干各的。"""
    text = _bat_text()

    assert "tools\\launcher_progress.ps1" in text
    assert 'set "LOGFILE=%~dp0config\\logs\\launcher.log"' in text
    assert '-LogPath "%LOGFILE%"' in text
    # 先把隐藏的启动过程拉起来，再在当前窗口跑进度脚本
    serve = text.index("-ArgumentList '/c','%~nx0 --serve'")
    run_progress = text.index('powershell -NoProfile -ExecutionPolicy Bypass -File "%PROGRESS%"')
    assert serve < run_progress


def test_the_bat_falls_back_when_powershell_itself_dies():
    """PowerShell 被安全软件拦掉时，窗口要重试一次，再退回纯 cmd 等待。"""
    text = _bat_text()

    assert "重试一次" in text
    assert ":wait_loop" in text
    assert '<nul set /p "=."' in text, "简易等待用点告诉人它还活着"
    assert "timeout /t 3 /nobreak" in text
    # 简易等待也要认得出「start.ps1 已经退出」，别对着一个死掉的启动过程无限等
    assert 'findstr /C:"--- start.ps1 exited with code"' in text


def test_the_progress_script_exit_codes_match_what_the_bat_expects():
    """0/2/3 是跨文件约定：三种都表示「进度脚本已经把话说完了」，照它给的结论收工。"""
    text = PROGRESS.read_text(encoding="utf-8")

    assert "exit 0" in text, "就绪"
    assert "exit 2" in text, "等超时（已报）"
    assert "exit 3" in text, "start.ps1 退出（已报）"
    for code in ("0", "2", "3"):
        assert f'if "%errorlevel%"=="{code}" exit /b 0' in _bat_text()


def test_the_progress_script_recognises_the_amsi_crash():
    """这台机器上 Defender 的 AMSI 偶发崩溃会把 start.ps1 一起弄死，得给人一句人话。"""
    text = PROGRESS.read_text(encoding="utf-8")

    assert "-1073741819" in text, "0xC0000005（AMSI 崩溃）的退出码"
    assert "AmsiScanBuffer" in text, "日志里留下的痕迹也认"


def test_the_bat_runs_start_ps1_and_keeps_its_own_log():
    text = _bat_text()

    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"' in text
    assert f'> "%~dp0{LAUNCHER_LOG}" 2>&1' in text
    # 启动过程按 UTF-8 写：别让一份日志里 UTF-8 与 GBK 混着（面板读它会两边不讨好）
    assert "chcp 65001" in text
    assert "set PYTHONUTF8=1" in text


def test_the_bat_opens_the_panel_when_the_service_is_already_running():
    text = _bat_text()

    assert "Start-Process 'http://127.0.0.1:8000/panel'" in text
    assert ":open_panel" in text


def test_the_bat_sends_first_time_users_to_start_html():
    """第一次双击（还没走过 start.html）先送去看它，然后结束本窗口，不启动服务。

    有些人 README 也不看就双击这个文件，然后卡在「缺 Python / 缺 Node」这种本来能提前
    发现的问题上。痕迹认三个，有任何一个就不再拦：``start.html`` 那次环境检测写的
    ``config/env-report.js``、服务跑过留下的 ``config/panel_config.json``、以及这次打开
    ``start.html`` 时写下的 ``config/.first-run-done``。
    """
    text = _bat_text()

    assert 'if exist "%~dp0config\\.first-run-done" goto :ready_to_start' in text
    assert 'if exist "%~dp0config\\env-report.js" goto :ready_to_start' in text
    assert 'if exist "%~dp0config\\panel_config.json" goto :ready_to_start' in text
    assert 'start "" "%~dp0start.html"' in text
    assert ":ready_to_start" in text
    # 拦下来之后是**结束**这个窗口，不是继续往下把服务拉起来
    guide_at = text.index('start "" "%~dp0start.html"')
    assert "exit /b 0" in text[guide_at:guide_at + 600]


def test_the_bat_marks_the_guide_before_opening_the_page():
    """先落标记、再打开页面：浏览器没弹出来或者用户立刻关掉，再双击也不会被反复拦。"""
    text = _bat_text()

    write_at = text.index('> "%~dp0config\\.first-run-done" echo')
    open_at = text.index('start "" "%~dp0start.html"')
    assert write_at < open_at


def test_the_bat_guides_only_the_double_click_path():
    """隐藏那次（``--serve``）不能被拦 —— 它才是真正启动服务的那个，拦了就永远起不来。"""
    text = _bat_text()

    serve_at = text.index('if /I "%~1"=="--serve" goto :serve')
    guide_at = text.index('if exist "%~dp0config\\.first-run-done"')
    assert serve_at < guide_at


# ── 可见窗口里的进度显示（tools/launcher_progress.ps1） ────────────────────

def test_the_progress_script_reports_steps_and_waits_for_the_port():
    text = PROGRESS.read_text(encoding="utf-8")

    assert "=====>" in text, "start.ps1 写的步骤行是它报进度的依据"
    assert "Test-ServerPort" in text, "8000 端口通了 = 服务起来了"
    assert "[System.IO.FileShare]::ReadWrite" in text, "日志被启动过程占着，读它得允许对方同时写"
    assert "LastWriteTime" in text, "别把上一次启动留下的旧步骤当成本次"
    assert "http://127.0.0.1:$Port/panel" in text, "就绪后打开控制面板"


def test_the_progress_script_stops_when_the_startup_script_dies():
    """start.ps1 退出（失败）或等太久，窗口都要停下来把日志给人看，不能干等。"""
    text = PROGRESS.read_text(encoding="utf-8")

    assert "[失败]" in text
    assert "[超时]" in text
    assert "Get-LogTail" in text, "把日志最后几行直接显示在窗口里"
    assert "Read-Host" in text, "出问题时窗口停住，别一闪而过"


def test_bat_and_progress_script_agree_on_the_exit_marker():
    """两边靠一行纯 ASCII 的标记通信：bat 写，进度脚本按同一个格式认。"""
    assert f"{EXIT_MARKER} %START_RC% ---" in _bat_text()
    assert r"start\.ps1 exited with code" in PROGRESS.read_text(encoding="utf-8")


# ── start.ps1：服务的输出落进 config/logs/server.log ──────────────────────

def test_start_ps1_redirects_the_service_output_through_cmd():
    text = START_PS1.read_text(encoding="utf-8")

    assert "& cmd.exe /c " in text, "要用 cmd 的重定向（PowerShell 的 > 会写成 UTF-16）"
    assert "-m uvicorn backend.main:app" in text
    assert '>> `"$serverLog`" 2>&1' in text, "服务输出要追加到 config/logs/server.log"
    assert 'Join-Path $logDir "server.log"' in text, "日志路径跟项目目录走，不写死"
    assert '`"$venvPython`"' in text, "用 venv 里的 python 起服务"


def test_start_ps1_asks_for_utf8_log_output():
    """不设 PYTHONUTF8，Windows 上 python 会按 GBK 写文件，和别处的 UTF-8 混在一起。"""
    text = START_PS1.read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in text
    assert '$env:PYTHONIOENCODING = "utf-8"' in text


def test_start_ps1_does_not_use_powershell_redirection_or_tee():
    """PowerShell 的 ``>`` 写出来是 UTF-16（还带缓冲），Tee-Object 也会拖后腿。"""
    text = START_PS1.read_text(encoding="utf-8")

    assert '> "$serverLog"' not in text
    assert "Out-File" not in text
    assert "Tee-Object" not in text


def test_start_ps1_tells_people_where_the_log_is():
    text = START_PS1.read_text(encoding="utf-8")

    assert "config\\logs\\server.log" in text
    assert "停止程序" in text, "顺带告诉他们怎么停（面板上的那个按钮）"


def test_start_ps1_keeps_the_step_prefix_the_progress_window_reads():
    """``=====>`` 是进度窗口认步骤的锚点，改提示语可以，去掉前缀不行。"""
    text = START_PS1.read_text(encoding="utf-8")

    assert 'Write-Host "=====> $msg"' in text


# ── start.sh：macOS / Linux 落到同一个文件 ────────────────────────────────

def test_start_sh_writes_the_same_log_file():
    text = START_SH.read_text(encoding="utf-8")

    assert 'mkdir -p "$DIR/data" "$DIR/config/logs"' in text
    assert '>> "$DIR/config/logs/server.log" 2>&1' in text
    assert ".server.log" not in text, "日志要落在 config/logs/server.log，不是根目录"


# ── 三份东西必须指向同一个文件 ────────────────────────────────────────────

def test_the_panel_the_scripts_and_the_bat_agree_on_the_log_file():
    assert cp._display_path(cp.SERVER_LOG_PATH) == SERVER_LOG
    assert 'Join-Path $logDir "server.log"' in START_PS1.read_text(encoding="utf-8")
    assert '"$DIR/config/logs/server.log"' in START_SH.read_text(encoding="utf-8")
    assert "config\\logs\\server.log" in _bat_text()


def test_readme_tells_people_about_the_hidden_launcher():
    readme = README.read_text(encoding="utf-8")

    assert BAT_NAME in readme
    assert SERVER_LOG in readme
