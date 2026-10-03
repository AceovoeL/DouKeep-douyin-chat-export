"""隐藏启动（根目录的「启动服务（双击）.bat」）+ 服务输出写进 data/server.log。

约定（三个文件一起守住）：

* ``start.ps1``（Windows）和 ``start.sh``（macOS / Linux）都把服务输出写进
  ``data/server.log``，所以「日志」页不管服务是怎么起来的都有内容；
* Windows 上必须用 **cmd 的重定向**：PowerShell 的 ``>`` 会把输出转成 UTF-16 写文件，
  面板按 UTF-8 读就成了乱码，而且会缓冲（日志半天不刷新）。用 ``>>`` 追加，服务重启
  几次之后还能翻到前面的记录；
* ``「启动服务（双击）.bat」`` 负责「隐藏启动」和「已经在跑就只开面板」，启动过程
  （pip / npm 的报错）单独写 ``data/launcher.log``，不和服务的日志抢同一个文件 ——
  两个进程同时追加一个文件，Windows 上后一个会直接打不开。

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
README = REPO_ROOT / "README.md"

#: 服务输出落在这里，也是面板「日志」页读的那一份
SERVER_LOG = "data/server.log"
#: 启动过程（pip / npm / 建虚拟环境）落在 bat 自己这份里
LAUNCHER_LOG = "data\\launcher.log"


def _bat_text() -> str:
    """bat 是 GBK 编码的：中文 Windows 的 cmd 按 GBK 读它。"""
    return BAT.read_bytes().decode("gbk")


# ── 根目录的 bat ──────────────────────────────────────────────────────────

def test_the_launcher_bat_exists_at_the_repo_root():
    assert BAT.is_file(), f"根目录应该有「{BAT_NAME}」：双击就能无窗口启动后端"


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


def test_the_bat_only_starts_the_service_once():
    """端口在监听 = 服务已经在跑：只打开面板，不再启动第二个。"""
    text = _bat_text()

    assert 'findstr ":8000" | findstr "LISTENING"' in text
    assert "if not errorlevel 1 goto :open_panel" in text


def test_the_bat_starts_the_service_in_a_hidden_window():
    text = _bat_text()

    assert 'if /I "%~1"=="--serve" goto :serve' in text, "靠 --serve 把自己再跑一遍"
    assert "-WindowStyle Hidden" in text
    assert "-ArgumentList '--serve'" in text


def test_the_bat_runs_start_ps1_and_keeps_its_own_log():
    text = _bat_text()

    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"' in text
    assert f'> "%~dp0{LAUNCHER_LOG}" 2>&1' in text
    # 启动过程按 UTF-8 写：别让一份日志里 UTF-8 与 GBK 混着（面板读它会两边不讨好）
    assert "chcp 65001" in text
    assert "set PYTHONUTF8=1" in text


def test_the_bat_opens_the_panel_when_the_service_is_ready():
    text = _bat_text()

    assert "Start-Process 'http://127.0.0.1:8000/panel'" in text
    assert "Start-Sleep -Seconds 2" in text
    # 一直起不来就把启动日志打开，别让人对着一个没反应的黑窗口猜
    assert "if(Test-Path $log){ Start-Process $log }" in text


# ── start.ps1：服务的输出落进 data/server.log ─────────────────────────────

def test_start_ps1_redirects_the_service_output_through_cmd():
    text = START_PS1.read_text(encoding="utf-8")

    assert "& cmd.exe /c " in text, "要用 cmd 的重定向（PowerShell 的 > 会写成 UTF-16）"
    assert "-m uvicorn backend.main:app" in text
    assert '>> `"$serverLog`" 2>&1' in text, "服务输出要追加到 data/server.log"
    assert 'Join-Path $dataDir "server.log"' in text, "日志路径跟项目目录走，不写死"
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

    assert "data\\server.log" in text
    assert "停止程序" in text, "顺带告诉他们怎么停（面板上的那个按钮）"


# ── start.sh：macOS / Linux 落到同一个文件 ────────────────────────────────

def test_start_sh_writes_the_same_log_file():
    text = START_SH.read_text(encoding="utf-8")

    assert 'mkdir -p "$DIR/data"' in text
    assert '>> "$DIR/data/server.log" 2>&1' in text
    assert ".server.log" not in text, "日志要落在 data/server.log，不是根目录"


# ── 三份东西必须指向同一个文件 ────────────────────────────────────────────

def test_the_panel_the_scripts_and_the_bat_agree_on_the_log_file():
    assert cp._display_path(cp.SERVER_LOG_PATH) == SERVER_LOG
    assert 'Join-Path $dataDir "server.log"' in START_PS1.read_text(encoding="utf-8")
    assert '"$DIR/data/server.log"' in START_SH.read_text(encoding="utf-8")
    assert "data\\server.log" in _bat_text()


def test_readme_tells_people_about_the_hidden_launcher():
    readme = README.read_text(encoding="utf-8")

    assert BAT_NAME in readme
    assert SERVER_LOG in readme
