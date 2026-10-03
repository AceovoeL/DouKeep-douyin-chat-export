"""更新完成后自动重启：后端的编排（control_panel.py）+ 面板页面上的接线。

不打网络、不真的重启进程：助手脚本的启动、新代码的自检都在这里打桩，只验证
「什么时候该重启」「参数对不对」「什么情况下不该重启（免得把能用的服务换掉）」
以及面板那边怎么配合（等服务回来、自动刷新、刷新后报喜）。
"""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from backend import control_panel as cp
from common import version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")

#: 假装的本地 commit 数（对应版本 1.0.7）
LOCAL_COUNT = 8


class _FakeTask:
    def __init__(self, coro):
        self.coro = coro
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


def record_tasks(monkeypatch) -> list:
    """拦下 asyncio.create_task：只确认「有任务被安排」，不真的让它跑。"""
    created: list[_FakeTask] = []

    def fake_create_task(coro, **kwargs):
        created.append(_FakeTask(coro))
        coro.close()
        return created[-1]

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    return created


@pytest.fixture(autouse=True)
def clean_update_state(monkeypatch):
    """更新状态是模块级共享的，每个用例都从干净的一份开始。"""
    monkeypatch.setattr(cp, "_update_state", {**cp._update_state})


# ── 从自己的命令行里读出服务地址 ──────────────────────────────────────────

def test_server_cli_address_reads_uvicorn_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "9100"])
    assert cp._server_cli_address() == ("0.0.0.0", 9100)


def test_server_cli_address_accepts_equals_form(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app", "--host=127.0.0.1", "--port=9200"])
    assert cp._server_cli_address() == ("127.0.0.1", 9200)


def test_server_cli_address_falls_back_to_the_documented_default(monkeypatch):
    """命令行里没有 --port 时按 8000 算（start.ps1 / start.sh 里的默认值）。"""
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app"])
    assert cp._server_cli_address() == ("127.0.0.1", 8000)


def test_server_cli_address_ignores_junk_port(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app", "--port", "abc"])
    assert cp._server_cli_address() == ("127.0.0.1", 8000)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app", "--port", "99999"])
    assert cp._server_cli_address() == ("127.0.0.1", 8000)


def test_reload_mode_does_not_restart(monkeypatch):
    """--reload 是开发模式：uvicorn 自己会重载，我们不掺和。"""
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app", "--reload", "--port", "8000"])
    assert cp._server_cli_address() is None
    monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app", "--reload=True"])
    assert cp._server_cli_address() is None


# ── 新代码自检 ────────────────────────────────────────────────────────────

def test_import_check_uses_the_repo_and_the_same_python(monkeypatch):
    seen: dict = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    assert cp._new_code_imports_ok() == (True, "")
    assert seen["command"] == [sys.executable, "-c", "import backend.main"]
    assert seen["cwd"] == version.REPO_ROOT
    assert seen["env"]["PYTHONUTF8"] == "1"       # 中文报错别在 Windows 上乱码


def test_import_check_reports_the_tail_of_the_error(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "line1\nline2\nSyntaxError: invalid syntax\n"

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: Result())

    ok, detail = cp._new_code_imports_ok()

    assert ok is False
    assert "SyntaxError" in detail


def test_import_check_survives_a_failed_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("python 不见了")

    monkeypatch.setattr(cp.subprocess, "run", boom)

    ok, detail = cp._new_code_imports_ok()

    assert ok is False
    assert "python 不见了" in detail


# ── 启动助手脚本 ──────────────────────────────────────────────────────────

def test_spawn_restart_helper_passes_pid_address_and_detaches(monkeypatch, tmp_path):
    captured: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)

    monkeypatch.setattr(cp.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cp, "RESTART_LOG_PATH", str(tmp_path / "restart.log"))

    assert cp._spawn_restart_helper("127.0.0.1", 8123) is True

    command = captured["command"]
    assert command[:2] == [sys.executable, "-u"]
    assert command[2] == cp._RESTART_SCRIPT
    assert command[command.index("--wait-pid") + 1] == str(os.getpid())
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8123"
    # 新服务的输出单独一份：和助手自己的日志分开，两个进程才不会互相覆盖
    assert command[command.index("--log") + 1] == cp.SERVER_LOG_PATH
    assert cp.SERVER_LOG_PATH != cp.RESTART_LOG_PATH
    assert captured["env"]["PYTHONUTF8"] == "1"     # 中文日志别在 Windows 上乱码
    if os.name == "nt":
        # DETACHED_PROCESS：本进程马上退出，助手必须能自己活着
        assert captured["creationflags"] & 0x00000008
    else:
        assert captured["start_new_session"] is True
    assert (tmp_path / "restart.log").exists()


def test_spawn_restart_helper_without_the_script(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "_RESTART_SCRIPT", str(tmp_path / "missing.py"))
    monkeypatch.setattr(cp, "RESTART_LOG_PATH", str(tmp_path / "restart.log"))

    assert cp._spawn_restart_helper("127.0.0.1", 8123) is False


def test_spawn_restart_helper_reports_spawn_failure(monkeypatch, tmp_path):
    def boom(command, **kwargs):
        raise OSError("不允许创建进程")

    monkeypatch.setattr(cp.subprocess, "Popen", boom)
    monkeypatch.setattr(cp, "RESTART_LOG_PATH", str(tmp_path / "restart.log"))

    assert cp._spawn_restart_helper("127.0.0.1", 8123) is False


# ── 更新成功之后到底做了什么 ──────────────────────────────────────────────

def test_finish_update_hands_over_and_sets_restarting(monkeypatch):
    tasks = record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    seen: list = []
    monkeypatch.setattr(cp, "_spawn_restart_helper",
                        lambda host, port: seen.append((host, port)) or True)

    asyncio.run(cp._finish_update_after_success("1.2.6", {"host": "127.0.0.1", "port": 8000}))

    assert seen == [("127.0.0.1", 8000)]
    assert cp._update_state["status"] == "restarting"
    assert "正在自动重启" in cp._update_state["message"]
    assert len(tasks) == 1, "应该安排一个「稍后自己退出」的任务"
    assert cp._update_state["restart_started_at"] is not None


def test_finish_update_refuses_to_restart_on_broken_new_code(monkeypatch):
    """新代码连 import 都过不了时不许重启：换成「起不来的服务」比不更新更糟。"""
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (False, "SyntaxError: bad"))

    def should_not_spawn(host, port):
        raise AssertionError("自检没过就不该启动重启助手")

    monkeypatch.setattr(cp, "_spawn_restart_helper", should_not_spawn)

    asyncio.run(cp._finish_update_after_success("1.2.6", {"host": "127.0.0.1", "port": 8000}))

    assert cp._update_state["status"] == "failed"
    assert "启动自检" in cp._update_state["message"]
    assert "SyntaxError" in cp._update_state["message"]


def test_finish_update_without_restart_keeps_the_manual_message(monkeypatch):
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok",
                        lambda: pytest.fail("不需要重启时不必做自检"))

    asyncio.run(cp._finish_update_after_success("1.2.6", None))

    assert cp._update_state["status"] == "completed"
    assert cp._update_state["message"] == "已更新到 1.2.6，重启服务后生效"


def test_finish_update_reports_when_the_helper_cannot_start(monkeypatch):
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port: False)

    asyncio.run(cp._finish_update_after_success("1.2.6", {"host": "127.0.0.1", "port": 8000}))

    assert cp._update_state["status"] == "completed"
    assert "请手动重启" in cp._update_state["message"]


def test_exit_for_restart_sends_sigint_then_forces_exit(monkeypatch):
    """优雅退出（Ctrl+C）优先；退不掉就硬退，不能让新服务一直干等。"""
    import signal as signal_module

    events: list = []
    monkeypatch.setattr(cp, "_RESTART_EXIT_DELAY", 0)
    monkeypatch.setattr(cp, "_RESTART_FORCE_EXIT_DELAY", 0)
    monkeypatch.setattr(cp, "signal", _FakeSignal(events, signal_module))
    monkeypatch.setattr(cp, "_force_exit", lambda: events.append("force"))

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr(cp.asyncio, "sleep", fake_sleep)

    asyncio.run(cp._exit_for_restart())

    assert ("raise", signal_module.SIGINT) in events
    assert events[-1] == "force"


class _FakeSignal:
    """只代理 raise_signal，其余（SIGINT 常量）用真的。"""

    def __init__(self, events, real):
        self._events = events
        self.SIGINT = real.SIGINT

    def raise_signal(self, sig):
        self._events.append(("raise", sig))


# ── 接口：谁来触发、响应里带了什么 ────────────────────────────────────────

def _commit(sha: str, subject: str) -> dict:
    return {
        "sha": sha * 7,
        "parents": [],
        "commit": {"message": subject, "author": {"date": "2026-10-02T12:00:00Z"}},
    }


@pytest.fixture
def updatable(monkeypatch, tmp_path):
    """把「检查更新」固定成「远端有一个新版本」，网络一律封死。"""
    async def fetch():
        return 1, [_commit("a", "新提交")]

    async def reachable():
        return True

    monkeypatch.setattr(cp, "_fetch_ahead_best_source", fetch)
    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_git_run", lambda args, **kwargs: None)
    monkeypatch.setattr(cp, "_git_head", lambda: None)
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp._github_auth, "TOKEN_PATH", str(tmp_path / "github_token"))
    monkeypatch.setattr(cp, "_update_cache", None)
    monkeypatch.setattr(version, "local_commit_count", lambda: LOCAL_COUNT)
    monkeypatch.setattr(cp, "_scrape_state", {**cp._scrape_state, "status": "idle"})
    return LOCAL_COUNT


@pytest.fixture
def capture_run_update(monkeypatch):
    """把 _run_update 换成一个「只记参数、不干活」的东西。

    ``asyncio.create_task`` 那边用的是「记下协程就关掉」的桩（不真的跑），所以这里
    返回一个空的协程对象就够了。
    """
    seen: list = []

    async def _nothing():
        return None

    def fake_run_update(from_version, to_version, *, allow_dirty=False, restart=None, notify=False):
        seen.append({"from": from_version, "to": to_version,
                     "allow_dirty": allow_dirty, "restart": restart})
        return _nothing()

    monkeypatch.setattr(cp, "_run_update", fake_run_update)
    return seen


def test_update_run_plans_the_restart(monkeypatch, updatable, capture_run_update):
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8123))

    result = asyncio.run(cp.update_run(cp._UpdateRunRequest()))

    assert result["status"] == "started"
    assert result["auto_restart"] is True
    assert capture_run_update == [{
        "from": version.VERSION, "to": "1.0.8", "allow_dirty": False,
        "restart": {"host": "127.0.0.1", "port": 8123},
    }]
    # 逐版本变更要先记在更新状态里：重启后弹「更新完成」提示要用它显示更新内容
    assert cp._update_state["versions"]
    assert cp._update_state["versions"][0]["subject"] == "新提交"


def test_update_run_can_skip_the_restart(monkeypatch, updatable, capture_run_update):
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8123))

    result = asyncio.run(cp.update_run(cp._UpdateRunRequest(auto_restart=False)))

    assert result["auto_restart"] is False
    assert capture_run_update[0]["restart"] is None
    assert cp._update_state["auto_restart"] is False


def test_update_run_skips_the_restart_in_reload_mode(monkeypatch, updatable, capture_run_update):
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_server_cli_address", lambda: None)      # 命令行里有 --reload

    result = asyncio.run(cp.update_run(cp._UpdateRunRequest()))

    assert result["auto_restart"] is False
    assert capture_run_update[0]["restart"] is None


def test_update_status_exposes_auto_restart(monkeypatch, updatable):
    record_tasks(monkeypatch)

    async def fake_run_update(*args, **kwargs):
        pass

    monkeypatch.setattr(cp, "_run_update", fake_run_update)
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8123))
    asyncio.run(cp.update_run(cp._UpdateRunRequest()))

    payload = asyncio.run(cp.update_status())

    assert payload["auto_restart"] is True
    assert payload["status"] == "running"


# ── 面板页面上的接线 ──────────────────────────────────────────────────────

def test_panel_html_wires_the_automatic_restart():
    """面板那一半也钉住：勾选框、交棒、等服务回来、刷新后报喜，缺一个都不成立。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert 'id="updateAutoRestart"' in html
    assert "auto_restart: updateAutoRestart" in html
    assert "data.auto_restart !== false" in html          # 后端否掉时以它为准
    assert "if (updateAutoRestart) setUpdatePending(target);" in html
    # 状态变成 restarting 就交棒；fetch 失败也可能是服务在重启
    assert "beginUpdateHandoff();" in html
    assert "if (updateInFlight && updateAutoRestart) beginUpdateHandoff();" in html
    # 等待期间问的是 update/info（新进程里必定存在的接口），回来就刷新
    assert "fetch('/panel/api/update/info', { cache: 'no-store' })" in html
    assert "location.reload();" in html
    assert "confirmPendingUpdate();" in html
    # 中英文案都要有
    for key in ("aboutUpdateAutoRestart", "aboutUpdateRestarting", "aboutUpdateRestartWait",
                "aboutUpdateRestartDone", "aboutUpdateRestartTimeout", "aboutUpdateStale"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
    # 弹窗里的说明要提到「自动重启」，别让用户以为还要自己动手
    assert html.count("aboutUpdateStepsNote:") == 2
    assert "自动重启后端服务" in html
    assert "restarts the service automatically" in html
