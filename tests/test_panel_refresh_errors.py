"""「刷新会话列表」出错时的那套弹窗。

采集出错会冻结进程、弹一个带 2 分钟倒计时的窗、右下角留一张卡片；刷新会话列表出的
故障（登录失效、页面结构变了、进程直接挂掉）走的是同一套机制，只是任务名换成
``refresh``、停止按钮写着「停止刷新」。这里钉住后端这一半和面板那一半的接线。
"""
import asyncio
import os
import sys

import pytest

from backend import control_panel as cp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")


class _FakeProc:
    """替身进程：有 pid / returncode / terminate，和 asyncio 的子进程接口一致。"""

    def __init__(self, pid=7654):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15


@pytest.fixture
def job_state(monkeypatch):
    """干净的报错队列 + 干净的刷新状态；记录进程被冻/被解冻的调用。"""
    calls = []

    def _fake_suspend(pid, suspend):
        calls.append((pid, suspend))
        return 1

    monkeypatch.setattr(cp, "_set_process_tree_suspended", _fake_suspend)
    monkeypatch.setattr(cp, "_job_error_state", {
        "seq": 0, "active": None, "queue": [], "alerts": [], "merge": {},
        "resume_task": None,
    })
    monkeypatch.setattr(cp, "_discover_state", {
        "status": "idle", "message": "", "process": None,
        "started_at": None, "finished_at": None,
        "paused": False, "paused_at": None, "stopped": False,
    })
    return calls


def _public():
    return cp._public_job_error()


def _cancel_countdown():
    task = cp._job_error_state.get("resume_task")
    if task is not None:
        task.cancel()


# ── 后端的报错链路 ────────────────────────────────────────────────────────

def test_refresh_is_a_job_of_its_own(monkeypatch):
    monkeypatch.setitem(cp._discover_state, "status", "running")
    assert cp._scrape_job_kind("refresh") == cp.JOB_REFRESH
    assert cp._job_label("refresh") == "刷新会话列表"
    assert cp._job_is_running(cp.JOB_REFRESH) is True


def test_watching_the_refresh_log_pauses_it_and_opens_the_dialog(tmp_path, job_state):
    """刷新日志里出现 [-]/[!] 这类行：冻住子进程 + 弹窗，和采集一模一样。"""
    log = tmp_path / "discover.log"
    log.write_text("[-] 未能登录，退出\n", encoding="utf-8")

    async def scenario():
        cp._discover_state["status"] = "running"
        cp._discover_state["process"] = _FakeProc()
        task = asyncio.create_task(cp._watch_scrape_log(str(log), "refresh"))
        # 等「弹窗出现」**并且**「进程真的被冻住」：登记 active 和冻进程之间隔着一次
        # to_thread，只等 active 就会抢跑 —— 任务在这中间被 cancel 掉时 paused 还没写上，
        # 在忙一点的 CI 机器上偶发失败（2026-10-03 的 v1.4.0 CI 就是这么红的）。
        for _ in range(60):
            await asyncio.sleep(0.05)
            if cp._job_error_state.get("active") and cp._discover_state.get("paused"):
                break
        task.cancel()
        info = _public()
        _cancel_countdown()
        return info

    info = asyncio.run(scenario())
    assert job_state == [(7654, True)]          # 刷新进程被冻住了
    assert cp._discover_state["paused"] is True
    assert info is not None and info["job"] == cp.JOB_REFRESH
    assert info["running"] is True              # 还活着，只是暂停着等用户回答
    assert "未能登录" in info["message"]


def test_a_dead_refresh_still_reports_the_dialog(tmp_path, monkeypatch, job_state):
    """刷新进程直接挂掉：没有东西可暂停，但原因要弹出来，并留着右下角卡片。"""
    monkeypatch.setattr(cp, "DISCOVER_LOG_PATH", str(tmp_path / "discover.log"))
    monkeypatch.setattr(cp, "PAUSE_AUTO_RESUME_SECONDS", 0.2)
    cmd = [
        sys.executable, "-u", "-c",
        "print('[-] 错误: 页面结构变了', flush=True); import time; time.sleep(0.3)"
        "; raise SystemExit(3)",
    ]

    async def scenario():
        await cp._run_discover(cmd)
        info = _public()
        await cp.resolve_job_error("ignore")
        return info

    info = asyncio.run(scenario())
    assert cp._discover_state["status"] == "failed"
    assert info is not None and info["job"] == cp.JOB_REFRESH
    assert info["running"] is False
    alerts = cp._public_job_alerts()
    assert alerts and alerts[0]["job"] == cp.JOB_REFRESH


def test_stopping_a_refresh_is_not_reported_as_a_failure(tmp_path, monkeypatch, job_state):
    """页面上的「取消」杀掉的是进程自己：退出码非 0，但这不是故障，别弹窗。"""
    monkeypatch.setattr(cp, "DISCOVER_LOG_PATH", str(tmp_path / "discover.log"))
    cmd = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]

    async def scenario():
        task = asyncio.create_task(cp._run_discover(cmd))
        for _ in range(60):
            await asyncio.sleep(0.05)
            if cp._discover_state.get("process") is not None:
                break
        result = await cp.refresh_stop()
        await asyncio.wait_for(task, timeout=10)
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "stopped"
    assert cp._discover_state["stopped"] is True
    assert cp._discover_state["status"] == "idle"
    assert _public() is None


def test_dialog_stop_button_stops_the_refresh(job_state):
    async def scenario():
        proc = _FakeProc()
        cp._discover_state["status"] = "running"
        cp._discover_state["process"] = proc
        await cp.raise_job_error(cp.JOB_REFRESH, "[-] boom", "detail")
        result = await cp.stop_job_error()
        return proc, result

    proc, result = asyncio.run(scenario())
    assert result["job"] == cp.JOB_REFRESH
    assert proc.terminated is True
    assert cp._discover_state["stopped"] is True
    assert _public() is None
    assert cp._public_job_alerts()[0]["resolved_by"] == "stop"


def test_the_sections_own_stop_button_closes_its_dialog(job_state):
    """对话框和后端队列不能脱节：用户按「取消」时，属于刷新的那个错误窗要一起关掉。"""
    async def scenario():
        await cp.raise_job_error(cp.JOB_REFRESH, "[-] boom", "detail")
        _cancel_countdown()
        return await cp.refresh_stop()

    result = asyncio.run(scenario())
    assert result == {"status": "stopped"}
    assert _public() is None
    assert cp._public_job_alerts()[0]["resolved_by"] == "stop"


def test_status_payload_tells_the_panel_it_is_paused(job_state, monkeypatch):
    monkeypatch.setattr(cp, "_read_conv_list", lambda: {"discovered_at": 0, "items": []})
    cp._discover_state.update({"status": "running", "paused": True})

    payload = asyncio.run(cp.refresh_status())

    assert payload["status"] == "running"
    assert payload["paused"] is True


def test_expired_login_pops_the_dialog_too(job_state, monkeypatch):
    """登录失效是刷新最常出的故障：不能只在按钮下面写行小字，要和采集一样弹窗。"""
    async def no_login():
        return {"status": "expired", "has_cookies": False}

    monkeypatch.setattr(cp, "_probe_login_state", no_login)

    response = asyncio.run(cp.refresh_conversations())

    assert response.status_code == 400
    info = _public()
    assert info is not None and info["job"] == cp.JOB_REFRESH
    assert "未检测到登录态" in info["message"]
    assert info["running"] is False          # 没有进程在跑，只是把原因说清楚
    _cancel_countdown()


# ── 面板那一半 ────────────────────────────────────────────────────────────

def test_panel_wires_the_refresh_dialog():
    html = open(PANEL_HTML, encoding="utf-8").read()

    # 任务名 → 文案：弹窗标题、右下角卡片标题、停止按钮，一个都不能少
    assert "refresh: 'refreshErrorTitle'," in html
    assert "refresh: 'refreshAlertTitle'," in html
    assert "refresh: 'refreshErrorStop'," in html
    # 刷新期间把 /api/status 的轮询调快，否则弹窗要等 5 秒那次轮询才出现
    assert "setJobFastPoll('refresh', lastDiscoverStatus === 'running');" in html
    # 状态里带回 paused：被冻住时页面显示「已暂停」，不是看着像卡死
    assert "const paused = !!d.paused && lastDiscoverStatus === 'running';" in html
    # 中英文案都要有：漏了英文，切到英文界面就会直接显示 key
    for key in ("refreshErrorTitle", "refreshAlertTitle", "refreshErrorStop"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
