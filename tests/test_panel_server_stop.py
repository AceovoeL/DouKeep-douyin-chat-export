"""面板「日志」页上的「停止程序」：关掉后端自己、把 8000 端口让出来。

为什么需要它：用项目根目录的「启动服务（双击）.bat」启动的后端是**没有窗口**的，
面板上的这个按钮就是它的「关掉」入口。

测试里绝不真的退出、也绝不真的发 Ctrl+C：

* 接口层的用例把 ``_request_process_exit`` / ``_force_exit`` 都换成「只记一笔」的桩，
  顺便把两个等待时间改成 0 —— 万一退出任务真的被事件循环跑起来，也伤不到 pytest；
* 「先优雅、退不掉再硬退」那两步单独测，signal 用假的（和自动重启的用例同一套手法）。
"""
import asyncio
import json
import os
import signal as signal_module

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import control_panel as cp

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


@pytest.fixture(autouse=True)
def no_real_exit(monkeypatch):
    """退出这件事全部打桩：测试进程不能真被自己发的 Ctrl+C 打断。"""
    events: list = []
    real_request_exit = cp._request_process_exit
    monkeypatch.setattr(cp, "_request_process_exit", lambda: events.append("ctrl+c"))
    monkeypatch.setattr(cp, "_force_exit", lambda: events.append("force"))
    monkeypatch.setattr(cp, "_STOP_EXIT_DELAY", 0)
    monkeypatch.setattr(cp, "_STOP_FORCE_EXIT_DELAY", 0)
    # 「已经安排了退出」是模块级共享的：每个用例都从干净的一份开始
    monkeypatch.setattr(cp, "_server_stop_state", {"running": False})
    return {"events": events, "real_request_exit": real_request_exit}


class _FakeTask:
    """只记下「安排了退出」，不真的让协程跑（同 test_panel_update_restart.py）。"""

    def __init__(self, coro):
        self.coro = coro
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def scheduled_tasks(monkeypatch):
    created: list[_FakeTask] = []

    def fake_create_task(coro, **kwargs):
        created.append(_FakeTask(coro))
        coro.close()
        return created[-1]

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    return created


# ── 接口：当场回话，退出安排在响应之后 ─────────────────────────────────────

def test_stop_schedules_the_exit_and_answers_first(monkeypatch, scheduled_tasks):
    """先回 200 再退：不然面板只看到一个「请求失败」，不知道是自己点停的。"""
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8000))

    result = asyncio.run(cp.stop_server())

    assert result == {"ok": True, "port": 8000}
    assert len(scheduled_tasks) == 1, "应该安排一个「稍后自己退出」的任务"
    assert cp._server_stop_state["running"] is True


def test_stop_reports_the_port_the_service_actually_uses(monkeypatch, scheduled_tasks):
    """端口是从自己的命令行里读的（--port 换过就报换过的那个）。"""
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8123))

    assert asyncio.run(cp.stop_server())["port"] == 8123


def test_stop_endpoint_returns_ok(client, monkeypatch):
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8000))

    response = client.post("/panel/api/server/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["port"] == 8000
    assert cp._server_stop_state["running"] is True


def test_clicking_twice_does_not_schedule_a_second_exit(client, monkeypatch):
    """连点几下也不该安排一堆退出任务（第二下告诉它「已经在停了」）。"""
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8000))
    client.post("/panel/api/server/stop")

    body = client.post("/panel/api/server/stop").json()

    assert body["ok"] is True
    assert body["already"] is True


def test_reload_mode_is_refused_instead_of_pretending(client, monkeypatch, scheduled_tasks):
    """--reload（开发模式）下真正跑服务的是 uvicorn 的子进程：停了会被立刻拉起来。

    这种情况要**说清楚**（409 + code），不能让面板显示成「已停止」。
    """
    monkeypatch.setattr(cp, "_server_cli_address", lambda: None)

    response = client.post("/panel/api/server/stop")

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "dev_mode"          # 面板按这个 code 换当前语言的说明
    assert "Ctrl+C" in body["error"]
    assert cp._server_stop_state["running"] is False, "没停成就别记成「已经在停」"
    assert scheduled_tasks == []


# ── 退出本身：先 Ctrl+C，退不掉再硬退 ─────────────────────────────────────

class _FakeSignal:
    """只代理 raise_signal，其余（SIGINT 常量）用真的。"""

    def __init__(self, events, real):
        self._events = events
        self.SIGINT = real.SIGINT

    def raise_signal(self, sig):
        self._events.append(("raise", sig))


def test_exit_for_stop_sends_sigint_then_forces_exit(no_real_exit, monkeypatch):
    events: list = []
    # 「发 Ctrl+C」要用真实现（夹具里那个只记一笔的桩换不到 fake signal 上）
    monkeypatch.setattr(cp, "_request_process_exit", no_real_exit["real_request_exit"])
    monkeypatch.setattr(cp, "signal", _FakeSignal(events, signal_module))
    monkeypatch.setattr(cp, "_force_exit", lambda: events.append("force"))

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr(cp.asyncio, "sleep", fake_sleep)

    asyncio.run(cp._exit_for_stop())

    assert ("raise", signal_module.SIGINT) in events
    assert events[-1] == "force"


def test_a_failed_signal_still_ends_up_forcing_the_exit(no_real_exit, monkeypatch):
    """发信号失败（平台少见）不能就这么算了：进程得走，端口才回得来。"""
    events: list = []
    # 把「发 Ctrl+C」换回真实现，再让 signal 在它里面炸掉
    monkeypatch.setattr(cp, "_request_process_exit", no_real_exit["real_request_exit"])

    class _BrokenSignal:
        SIGINT = signal_module.SIGINT

        def raise_signal(self, sig):
            raise OSError("没有控制台")

    monkeypatch.setattr(cp, "signal", _BrokenSignal())
    monkeypatch.setattr(cp, "_force_exit", lambda: events.append("force"))

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(cp.asyncio, "sleep", fake_sleep)

    asyncio.run(cp._exit_for_stop())

    assert events == ["force"]


def test_restart_reuses_the_same_ctrl_c_helper(monkeypatch):
    """自动重启和「停止程序」走的是同一条发 Ctrl+C 的路（少一份重复实现）。"""
    seen: list = []
    monkeypatch.setattr(cp, "_request_process_exit", lambda: seen.append("ctrl+c"))
    monkeypatch.setattr(cp, "_force_exit", lambda: seen.append("force"))

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(cp.asyncio, "sleep", fake_sleep)

    asyncio.run(cp._exit_for_restart())

    assert seen == ["ctrl+c", "force"]


# ── 面板接线 ──────────────────────────────────────────────────────────────

def test_panel_has_a_stop_button_on_the_logs_page():
    html = _panel_html()

    assert 'id="logsStopBtn" onclick="stopServer()"' in html
    assert 'data-i18n="logsStop">停止程序<' in html
    assert 'class="btn btn-danger" id="logsStopBtn"' in html, "停止是不可逆操作，用醒目的危险色"
    assert "'/panel/api/server/stop'" in html
    assert "method: 'POST'" in html
    # 结果（停没停成、怎么重新启动）显示在日志框上面那一行
    assert 'id="logsStopHint"' in html


def test_panel_asks_before_stopping():
    """点一下就退出服务太危险：要有一个确认框，而且标题/按钮要换成「停止」那一套。"""
    html = _panel_html()

    assert "await openConfirmModal('logsStopConfirm'" in html
    assert "titleKey: 'logsStopConfirmTitle'" in html
    assert "okKey: 'logsStopConfirmOk'" in html
    # 确认框的标题和按钮要能按用途换（默认仍是「全量采集」那一套）
    assert "function syncConfirmModalTexts()" in html
    assert "confirmModalTitleKey = options.titleKey || 'fullScrapeConfirmTitle';" in html
    assert "confirmModalTitleKey = 'fullScrapeConfirmTitle';" in html, "用完要还回默认那一套"


def test_panel_stops_talking_to_a_service_it_just_stopped():
    """服务没了就别再轮询、刷新、开文件夹了：否则页面上一直冒失败，像卡住了。"""
    html = _panel_html()

    assert "let serverStopped = false;" in html
    assert "if (serverStopped) return;" in html
    assert "serverStopped = true;" in html
    # 自动刷新关掉、按钮禁用，并给一句「怎么重新启动」
    assert "if (auto) auto.checked = false;" in html
    assert "stopHint.textContent = t('logsStopDone');" in html


def test_panel_shows_the_dev_mode_refusal_in_the_current_language():
    html = _panel_html()

    assert "if (err.code === 'dev_mode')" in html
    assert "logsStopDevMode" in html
    assert "logsStopFailed" in html


def test_stop_texts_exist_in_both_languages():
    html = _panel_html()

    for key in ("logsStop", "logsStopConfirmTitle", "logsStopConfirm", "logsStopConfirmOk",
                "logsStopDone", "logsStopFailed", "logsStopDevMode"):
        assert html.count(key + ":") == 2, f"{key} 的中英文案要各有一份"


def test_logs_page_no_longer_claims_only_a_restart_writes_the_log():
    """服务日志现在是「启动脚本也写、自动重启也写」，页面上的说明得跟上。"""
    html = _panel_html()

    assert "启动服务（双击）.bat" in html
    assert html.count("logsNoteServer:") == 2
    assert html.count("logsEmptyServer:") == 2


def test_route_baseline_knows_the_stop_endpoint():
    """路由契约（tests/baseline/routes.json）里要有这一个，否则接口加得偷偷摸摸。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline", "routes.json")
    routes = json.load(open(path, encoding="utf-8"))

    assert "POST /panel/api/server/stop" in routes
