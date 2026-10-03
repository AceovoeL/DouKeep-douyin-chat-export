"""面板「日志」页上的「重启服务」：停后端 → 重建前端 → 再启动，页面自己刷新。

为什么要有这个按钮：「停止程序」之后只能自己再双击 bat 才能回来，改了前端还得手动
``cd frontend && npm run build``。这个按钮把三件事串起来（见 control_panel.restart_server）。

测试分三层，全部打桩，绝不真的退出进程、也绝不真的跑 npm：

* 接口 —— 什么时候才肯重启、响应里带什么、哪种情况下**连停都不该停**；
* 助手脚本（``tools/restart_server.py --build-frontend``）—— 构建必须夹在「等旧服务
  让位」和「起新服务」中间，而且构建失败也要把服务拉回来；
* 面板接线 —— 按钮、中英文案、等服务回来再刷新。
"""
import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import control_panel as cp
from tools import restart_server

PANEL_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "panel", "static", "panel.html",
)


def _panel_html() -> str:
    with open(PANEL_HTML, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


class _FakeTask:
    """只记下「安排了退出」，不真的让协程跑（和自动重启的用例同一套手法）。"""

    def __init__(self, coro):
        self.coro = coro
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


@pytest.fixture(autouse=True)
def no_real_exit(monkeypatch):
    """退出这件事全部打桩：pytest 进程不能真被自己发的 Ctrl+C 打断。"""
    events: list = []
    monkeypatch.setattr(cp, "_request_process_exit", lambda: events.append("ctrl+c"))
    monkeypatch.setattr(cp, "_force_exit", lambda: events.append("force"))
    monkeypatch.setattr(cp, "_RESTART_EXIT_DELAY", 0)
    monkeypatch.setattr(cp, "_RESTART_FORCE_EXIT_DELAY", 0)
    # 模块级共享的「已经安排了这次重启」：每个用例都从干净的一份开始
    monkeypatch.setattr(cp, "_server_restart_state", {"running": False, "started_at": None})
    return events


@pytest.fixture
def scheduled_tasks(monkeypatch):
    created: list[_FakeTask] = []

    def fake_create_task(coro, **kwargs):
        created.append(_FakeTask(coro))
        coro.close()
        return created[-1]

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    return created


@pytest.fixture
def spawned(monkeypatch):
    """助手脚本「起得来」：只记下它被怎么调用的。"""
    seen: list = []

    def fake_spawn(host, port, *, build_frontend=False):
        seen.append({"host": host, "port": port, "build_frontend": build_frontend})
        return True

    monkeypatch.setattr(cp, "_spawn_restart_helper", fake_spawn)
    return seen


@pytest.fixture
def address(monkeypatch):
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8123))
    return ("127.0.0.1", 8123)


@pytest.fixture
def import_ok(monkeypatch):
    """新代码自检通过（不然每个用例都要真起一个 python 去 import）。"""
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))


# ── 接口：先说好，再停服务 ────────────────────────────────────────────────

def test_restart_plans_stop_then_build_then_start(address, import_ok, spawned, scheduled_tasks):
    """点一下要做三件事：停后端（安排退出）→ 让助手先构建前端 → 由助手把服务起回来。"""
    result = asyncio.run(cp.restart_server())

    assert result == {"ok": True, "restart": True, "build": True, "port": 8123}
    assert spawned == [{"host": "127.0.0.1", "port": 8123, "build_frontend": True}]
    assert len(scheduled_tasks) == 1, "应该安排一个「稍后自己退出」的任务"
    assert cp._server_restart_state["running"] is True


def test_restart_endpoint_returns_ok(client, address, import_ok, spawned, scheduled_tasks):
    response = client.post("/panel/api/server/restart")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["build"] is True
    assert body["port"] == 8123
    assert cp._server_restart_state["running"] is True


def test_restart_reports_the_port_the_service_actually_uses(monkeypatch, import_ok, spawned,
                                                            scheduled_tasks):
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("0.0.0.0", 9100))

    assert asyncio.run(cp.restart_server())["port"] == 9100


def test_clicking_twice_does_not_restart_twice(address, import_ok, spawned, scheduled_tasks):
    asyncio.run(cp.restart_server())

    body = asyncio.run(cp.restart_server())

    assert body["ok"] is True
    assert body["already"] is True
    assert len(spawned) == 1, "第二次不该再起一个助手"
    assert len(scheduled_tasks) == 1


def test_reload_mode_is_refused_instead_of_pretending(client, monkeypatch, no_real_exit,
                                                      spawned, scheduled_tasks):
    """--reload（开发模式）下 uvicorn 自己重载：说清楚，绝不停服务。"""
    monkeypatch.setattr(cp, "_server_cli_address", lambda: None)

    response = client.post("/panel/api/server/restart")

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "dev_mode"          # 面板按这个 code 换当前语言的说明
    assert cp._server_restart_state["running"] is False
    assert spawned == []
    assert scheduled_tasks == []
    assert no_real_exit == [], "开发模式下不该动这个进程"


def test_broken_code_keeps_the_running_service(client, address, monkeypatch, no_real_exit,
                                               spawned, scheduled_tasks):
    """新代码连 import 都过不了时不许停服务：换成「起不来的服务」比不重启更糟。"""
    monkeypatch.setattr(cp, "_new_code_imports_ok",
                        lambda: (False, "SyntaxError: invalid syntax"))

    response = client.post("/panel/api/server/restart")

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "import_failed"
    assert "SyntaxError" in body["error"]      # 面板把它拼进当前语言的那句话里
    assert spawned == []
    assert scheduled_tasks == []
    assert cp._server_restart_state["running"] is False
    assert no_real_exit == [], "自检没过就不该退出"


def test_helper_that_cannot_start_does_not_stop_the_service(address, import_ok, monkeypatch,
                                                            no_real_exit, scheduled_tasks):
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port, **kwargs: False)

    response = asyncio.run(cp.restart_server())

    assert response.status_code == 500
    assert "手动重启" in json.loads(response.body.decode("utf-8"))["error"]
    assert cp._server_restart_state["running"] is False
    assert scheduled_tasks == []
    assert no_real_exit == [], "助手没起来就别退出 —— 不然服务就真没了"


def test_restart_uses_the_same_ctrl_c_path_as_the_update_flow(address, import_ok, spawned,
                                                              monkeypatch, scheduled_tasks):
    """重启也是「先把响应回完、再走那条发 Ctrl+C 的退出路径」（和自动重启同一条）。"""
    seen: list = []
    monkeypatch.setattr(cp, "_request_process_exit", lambda: seen.append("ctrl+c"))
    monkeypatch.setattr(cp, "_force_exit", lambda: seen.append("force"))

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(cp.asyncio, "sleep", fake_sleep)

    asyncio.run(cp.restart_server())
    assert seen == [], "响应还没回完，不能先退出"

    asyncio.run(cp._exit_for_restart())
    assert seen == ["ctrl+c", "force"]


# ── 助手脚本的启动参数 ────────────────────────────────────────────────────

def _capture_spawn(monkeypatch, tmp_path) -> dict:
    captured: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)

    monkeypatch.setattr(cp.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cp, "RESTART_LOG_PATH", str(tmp_path / "restart.log"))
    return captured


def test_spawn_helper_asks_for_the_frontend_build(monkeypatch, tmp_path):
    captured = _capture_spawn(monkeypatch, tmp_path)

    assert cp._spawn_restart_helper("127.0.0.1", 8123, build_frontend=True) is True

    assert "--build-frontend" in captured["command"]


def test_spawn_helper_for_an_update_does_not_build(monkeypatch, tmp_path):
    """「更新后自动重启」那条路已经自己构建过了，不该再多跑一次 npm。"""
    captured = _capture_spawn(monkeypatch, tmp_path)

    assert cp._spawn_restart_helper("127.0.0.1", 8123) is True

    assert "--build-frontend" not in captured["command"]


# ── 助手脚本自己：构建夹在「等服务让位」和「起服务」中间 ─────────────────

class _FakeProc:
    pid = 9999

    def poll(self):
        return None


def _record_helper_calls(monkeypatch, build_code: int = 0) -> list:
    calls: list = []

    monkeypatch.setattr(restart_server, "wait_for_old_service",
                        lambda pid, host, port, timeout: calls.append("wait") or True)
    monkeypatch.setattr(restart_server, "build_frontend",
                        lambda: calls.append("build") or build_code)
    monkeypatch.setattr(restart_server, "write_pid_file", lambda pid: calls.append("pid"))
    monkeypatch.setattr(restart_server, "start_server",
                        lambda host, port, log: calls.append("start") or _FakeProc())
    monkeypatch.setattr(restart_server, "wait_for_server",
                        lambda host, port, timeout: calls.append("up") or True)
    return calls


def test_helper_builds_the_frontend_before_starting_the_service(monkeypatch, tmp_path):
    calls = _record_helper_calls(monkeypatch)

    code = restart_server.main(["--port", "8123", "--build-frontend",
                                "--log", str(tmp_path / "restart.log")])

    assert code == 0
    assert calls == ["wait", "build", "start", "up", "pid"], \
        "构建必须在旧服务让位之后、起服务之前"


def test_helper_skips_the_build_without_the_flag(monkeypatch, tmp_path):
    calls = _record_helper_calls(monkeypatch)

    restart_server.main(["--port", "8123", "--log", str(tmp_path / "restart.log")])

    assert calls == ["wait", "start", "up", "pid"]


def test_a_failed_build_still_starts_the_service(monkeypatch, tmp_path):
    """构建失败也要把服务拉回来：先把面板还给人，界面对不对可以看着日志再修。"""
    calls = _record_helper_calls(monkeypatch, build_code=1)

    code = restart_server.main(["--port", "8123", "--build-frontend",
                                "--log", str(tmp_path / "restart.log")])

    assert code == 0
    assert calls == ["wait", "build", "start", "up", "pid"]


def test_a_build_that_blows_up_still_starts_the_service(monkeypatch, tmp_path):
    """构建脚本本身抛异常（例如找不到 npm）也不能把重启流程卡住。"""
    calls: list = []
    monkeypatch.setattr(restart_server, "wait_for_old_service",
                        lambda pid, host, port, timeout: calls.append("wait") or True)

    def boom():
        calls.append("build")
        raise OSError("找不到 npm")

    monkeypatch.setattr(restart_server, "build_frontend", boom)
    monkeypatch.setattr(restart_server, "write_pid_file", lambda pid: None)
    monkeypatch.setattr(restart_server, "start_server",
                        lambda host, port, log: calls.append("start") or _FakeProc())
    monkeypatch.setattr(restart_server, "wait_for_server",
                        lambda host, port, timeout: calls.append("up") or True)

    code = restart_server.main(["--port", "8123", "--build-frontend",
                                "--log", str(tmp_path / "restart.log")])

    assert code == 0
    assert calls == ["wait", "build", "start", "up"]


def test_helper_reuses_the_updater_build(monkeypatch):
    """构建逻辑只有一份：tools/update.py 里的那个，「一键更新」和这里共用。"""
    seen: list = []
    monkeypatch.setattr(restart_server.update_tool, "build_frontend",
                        lambda: seen.append("build") or 0)

    assert restart_server.build_frontend() == 0
    assert seen == ["build"]


def test_build_frontend_flag_is_documented_in_the_help():
    args = restart_server.parse_args(["--build-frontend"])

    assert args.build_frontend is True
    assert restart_server.parse_args([]).build_frontend is False


# ── 面板接线 ──────────────────────────────────────────────────────────────

def test_panel_has_a_restart_button_on_the_logs_page():
    html = _panel_html()

    assert 'id="logsRestartBtn" onclick="restartServer()"' in html
    assert 'data-i18n="logsRestart">重启服务<' in html
    assert "'/panel/api/server/restart'" in html
    assert "method: 'POST'" in html
    # 结果（进行中 / 没重启成 / 已重启完成）显示在日志框上面那一行
    assert 'id="logsRestartHint"' in html


def test_panel_asks_before_restarting():
    """点一下就把服务停掉太危险（还夹着一次前端构建）：要有确认框，换掉标题与按钮文案。"""
    html = _panel_html()

    assert "await openConfirmModal('logsRestartConfirm'" in html
    assert "titleKey: 'logsRestartConfirmTitle'" in html
    assert "okKey: 'logsRestartConfirmOk'" in html


def test_panel_waits_for_the_service_to_come_back_then_reloads():
    """服务断了又回来才算重启完：不看断没断过就会当场刷新，刷完还是旧服务。"""
    html = _panel_html()

    assert "function beginLogsRestartWait()" in html
    assert "function checkLogsRestart()" in html
    assert "logsRestartTimer = setInterval(checkLogsRestart, 2000);" in html
    assert "if (!answers) logsRestartSawDown = true;" in html
    assert "if (answers && logsRestartSawDown) {" in html
    assert "location.reload();" in html
    # 等服务期间不再跟后端要日志，按钮也灰掉
    assert "function setLogsBusy(busy)" in html
    assert "setLogsBusy(true);" in html
    # 刷新回来报一句「已重启完成」（刷新前先记下这件事）
    assert "const LOGS_RESTART_KEY = 'panel-logs-restart';" in html
    assert "if (readLogsRestartPending()) {" in html
    assert "logsRestartDone = true;" in html


def test_panel_explains_why_it_could_not_restart():
    html = _panel_html()

    assert "if (err.code === 'dev_mode')" in html
    assert "else if (err.code === 'import_failed')" in html
    assert "logsRestartImportFailed" in html
    assert "logsRestartFailed" in html
    assert "logsRestartStuck" in html
    assert "logsRestartTimeout" in html


def test_restart_texts_exist_in_both_languages():
    html = _panel_html()

    for key in ("logsRestart", "logsRestartConfirmTitle", "logsRestartConfirm",
                "logsRestartConfirmOk", "logsRestartPending", "logsRestartDone",
                "logsRestartFailed", "logsRestartImportFailed", "logsRestartDevMode",
                "logsRestartStuck", "logsRestartTimeout"):
        assert html.count(key + ":") == 2, f"{key} 的中英文案要各有一份"


def test_route_baseline_knows_the_restart_endpoint():
    """路由契约（tests/baseline/routes.json）里要有这一个，否则接口加得偷偷摸摸。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline", "routes.json")
    routes = json.load(open(path, encoding="utf-8"))

    assert "POST /panel/api/server/restart" in routes
