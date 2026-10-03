"""首次运行启动链路：启动器 `tools/bridge.ps1` 和网页 `start.html` 的分工。

约定（两个文件必须一起守住）：

* ``tools/bridge.ps1`` 在启动 ``start.ps1`` 之前，先看有没有已经在跑的 ``start.ps1``
  进程；有就只更新 ``data/launcher-state.js`` 然后返回，不再启动第二个；
* ``start.html`` 读到这份状态（``__DOUYIN_LAUNCHER_STATE__``）就只等着，自己不启动。

真正的判定逻辑横跨 PowerShell 和浏览器，跑不了单元测试，所以这里钉住跨文件的契约、
"先检查再启动" 的顺序，以及 PowerShell 脚本的 UTF-8 BOM（丢了 BOM，PS 5.1 会按 GBK
读文件，中文注释一坏整个脚本就解析失败）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE = REPO_ROOT / "tools" / "bridge.ps1"
PAGE = REPO_ROOT / "start.html"

# 两边共用的状态文件与全局变量名（改动必须同步，否则网页永远读不到启动器状态）
STATE_FILE = "launcher-state.js"
STATE_GLOBAL = "__DOUYIN_LAUNCHER_STATE__"

BOM = b"\xef\xbb\xbf"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _body(text: str, name: str) -> str:
    """抠出 ``function <name>`` 的函数体（到下一个顶层 function 之前）。"""
    start = text.index(f"function {name}")
    nxt = re.search(r"^\s*function ", text[start + 1:], re.M)
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


# ── 编码：PowerShell 脚本必须有 BOM ──────────────────────────────────────
def test_powershell_scripts_keep_their_utf8_bom():
    """PS 5.1 读没有 BOM 的 .ps1 时按系统 ANSI（中文机器上是 GBK）解码。

    这些脚本里全是中文注释和提示语，一旦丢了 BOM，轻则输出乱码，重则直接报
    「语句块或类型定义中缺少右 }」。编辑工具经常把 BOM 弄丢，所以钉在这里。
    """
    for name in ("start.ps1", "tools/bridge.ps1", "tools/env_check.ps1"):
        data = (REPO_ROOT / name).read_bytes()
        assert data.startswith(BOM), f"{name} 丢了 UTF-8 BOM（改完记得补回来）"


def test_shell_scripts_must_not_have_a_bom():
    """bash 会因为 BOM 认不出 ``#!/bin/sh``，所以 shell 脚本反过来不能有 BOM。"""
    for name in ("start.sh", "stop.sh"):
        assert not (REPO_ROOT / name).read_bytes().startswith(BOM), f"{name} 不该有 BOM"


# ── 跨文件契约：状态文件名 / 全局变量名 ──────────────────────────────────
def test_bridge_and_page_agree_on_the_launcher_state_file():
    bridge = _read(BRIDGE)
    page = _read(PAGE)
    for label, text in (("bridge.ps1", bridge), ("start.html", page)):
        assert STATE_FILE in text, f"{label} 里没有引用 {STATE_FILE}"
        assert STATE_GLOBAL in text, f"{label} 里没有引用 {STATE_GLOBAL}"


# ── bridge.ps1：先检查，再启动 ───────────────────────────────────────────
def test_bridge_checks_for_a_running_start_script_before_spawning_one():
    body = _body(_read(BRIDGE), "Start-Backend")
    check = body.index("Get-StartScriptProcess")
    spawn = body.index("Start-Process")
    assert check < spawn, "启动 start.ps1 之前必须先检查有没有已经在跑的"

    # 检查到「已经在跑」时，要写状态文件告诉网页，而不是默默返回
    skip_branch = body[check:spawn]
    assert "Save-LauncherState" in skip_branch, "跳过启动时也要写启动状态文件"

    # 自己拉起来的进程号要记下来（网页据此判断「启动器已经动手了」）
    assert "-PassThru" in body[spawn:], "启动时要拿到进程号，写进状态文件"


def test_bridge_looks_for_start_script_of_this_project_only():
    """命令行里要出现本项目目录才算我们的 start.ps1，别误判别的项目。"""
    body = _body(_read(BRIDGE), "Get-StartScriptProcess")
    assert "start\\.ps1" in body
    assert "ProjectDir" in body


# ── start.html：启动器在启动时，页面只等 ─────────────────────────────────
def test_page_waits_when_the_launcher_is_already_starting():
    page = _read(PAGE)
    # 自动启动那一段：先问 launcherStarting()，确认为假才自己启动
    section = page[page.index("试着自动启动一次"):]
    assert section.index("launcherStarting()") < section.index("fireProtocol('start')")


def test_manual_start_button_defers_to_the_launcher():
    body = _body(_read(PAGE), "startBackend")
    assert body.index("launcherStarting()") < body.index("fireProtocol('start')")


def test_page_treats_old_launcher_state_as_expired():
    """启动器启动失败留下的旧记录不能一直挡着，宽限期过后页面要能自己重试。"""
    page = _read(PAGE)
    assert "LAUNCHER_GRACE_MS" in page
    body = _body(page, "launcherStarting")
    assert "LAUNCHER_GRACE_MS" in body, "launcherStarting() 必须按宽限期判断新旧"
