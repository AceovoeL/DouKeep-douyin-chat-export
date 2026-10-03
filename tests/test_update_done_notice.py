"""「更新完成」那条提示：跨进程的记录文件 + 只弹一次 + 走报错同一套队列。

弹窗和右下角常驻都是现成的机制（panel.html 里的 job error dialog / alert card）。这里
只钉住属于「更新完成」的部分：谁写记录、重启后的新服务怎么读它并弹出来、倒计时是不是
2 分钟、以及这条「好消息」不会暂停任何任务、要人点了「×」才从右下角消失。
"""
import asyncio
import json
import os

import pytest

from backend import control_panel as cp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")

#: 一条真实的「更新完成」记录长什么样
RECORD = {
    "from_version": "1.2.5",
    "to_version": "1.2.6",
    "at": 1760000000.0,
    "versions": [
        {"version": "1.2.6", "subject": "更新完自动重启", "date": "2026-10-02"},
        {"version": "1.2.6", "subject": "顺带修一处笔误", "date": "2026-10-02"},
        {"version": "1.2.5", "subject": "定时自动检查更新", "date": "2026-10-02"},
    ],
}


@pytest.fixture
def record_path(tmp_path, monkeypatch):
    """把记录文件指到临时目录。"""
    path = tmp_path / "update-done.json"
    monkeypatch.setattr(cp, "UPDATE_DONE_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """两条全局状态都从干净的开始：更新状态、以及报错/提示队列。"""
    monkeypatch.setattr(cp, "_update_state", {**cp._update_state})
    monkeypatch.setattr(cp, "_job_error_state", {
        "seq": 0, "active": None, "queue": [], "alerts": [], "merge": {}, "resume_task": None,
    })


def record_tasks(monkeypatch) -> list:
    created: list = []

    def fake_create_task(coro, **kwargs):
        created.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    return created


# ── 记录文件：交棒前写下、读一次就删 ──────────────────────────────────────

def test_handover_writes_the_record(record_path, monkeypatch):
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port: True)
    cp._update_state.update({
        "from_version": "1.2.5",
        "versions": RECORD["versions"],
    })

    asyncio.run(cp._finish_update_after_success("1.2.6", {"host": "127.0.0.1", "port": 8000}))

    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload["from_version"] == "1.2.5"
    assert payload["to_version"] == "1.2.6"
    assert payload["versions"] == RECORD["versions"]
    assert payload["at"] > 0
    assert cp._update_state["status"] == "restarting"


def test_no_record_when_the_restart_cannot_start(record_path, monkeypatch):
    """自动重启没起来就别留提示：否则下次手动重启会莫名其妙弹一条出来。"""
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port: False)

    asyncio.run(cp._finish_update_after_success("1.2.6", {"host": "127.0.0.1", "port": 8000}))

    assert not record_path.exists()
    assert cp._update_state["status"] == "completed"


def test_no_record_without_a_restart(record_path, monkeypatch):
    """没勾自动重启时这条提示也不该出现（用户自己会重启）。"""
    record_tasks(monkeypatch)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: pytest.fail("不该做自检"))

    asyncio.run(cp._finish_update_after_success("1.2.6", None))

    assert not record_path.exists()


# ── 新服务启动时把它弹出来 ────────────────────────────────────────────────

def test_startup_raises_the_notice_once(record_path):
    record_path.write_text(json.dumps(RECORD), encoding="utf-8")

    asyncio.run(cp.restore_update_done_notice_on_startup())

    active = cp._job_error_state["active"]
    assert active is not None
    assert active["job"] == "update_done"
    assert "当前版本 v1.2.6" in active["message"]
    # 弹窗里的「更新内容」：更新范围 + 逐版本说明（同一版本号合成一行）
    assert "更新范围：v1.2.5 → v1.2.6" in active["detail"]
    assert "- v1.2.6：更新完自动重启 ／ 顺带修一处笔误" in active["detail"]
    assert "- v1.2.5：定时自动检查更新" in active["detail"]
    # 记录读完就删：再重启也不会重复弹
    assert not record_path.exists()
    first_id = active["id"]

    asyncio.run(cp.restore_update_done_notice_on_startup())

    assert cp._job_error_state["active"]["id"] == first_id
    assert len(cp._job_error_state["alerts"]) == 0


def test_startup_without_a_record_does_nothing(record_path):
    asyncio.run(cp.restore_update_done_notice_on_startup())

    assert cp._job_error_state["active"] is None


def test_startup_survives_a_corrupt_record(record_path):
    """记录坏了也要能启动：跳过这次提示，并把坏文件清掉。"""
    record_path.write_text("{ 这不是 JSON", encoding="utf-8")

    asyncio.run(cp.restore_update_done_notice_on_startup())

    assert cp._job_error_state["active"] is None
    assert not record_path.exists()


def test_startup_ignores_a_record_without_versions(record_path):
    """没有逐版本说明时也得有话可说，不能弹一个空框。"""
    record_path.write_text(json.dumps({"to_version": "1.2.6"}), encoding="utf-8")

    asyncio.run(cp.restore_update_done_notice_on_startup())

    detail = cp._job_error_state["active"]["detail"]
    assert "这次更新没有单独的说明" in detail


def test_startup_takes_over_the_broken_countdown(monkeypatch, record_path):
    """倒计时是 2 分钟（用户要求），也就是报错弹窗用的那个常量。"""
    assert cp.PAUSE_AUTO_RESUME_SECONDS == 120
    record_path.write_text(json.dumps(RECORD), encoding="utf-8")

    asyncio.run(cp.restore_update_done_notice_on_startup())

    active = cp._job_error_state["active"]
    # 剩余时间就是那 2 分钟（面板照着它画倒计时条）
    assert cp._public_job_error()["remaining_ms"] > 0
    assert active["pause_done"] is True


# ── 不影响任何任务，也不会自己消失 ────────────────────────────────────────

def test_notice_pauses_nothing(record_path):
    record_path.write_text(json.dumps(RECORD), encoding="utf-8")
    monkeypatch_state = dict(cp._scrape_state)

    asyncio.run(cp.restore_update_done_notice_on_startup())

    assert cp._scrape_state["paused"] == monkeypatch_state["paused"]
    assert cp._backfill_state["paused"] is False
    assert cp._video_backfill_state["paused"] is False
    # 也没有「停止」这个动作可做（面板会把那个按钮收起来）
    assert asyncio.run(cp.stop_job(cp.JOB_UPDATE_DONE)) == {"status": "unknown_job"}
    assert cp._job_is_running(cp.JOB_UPDATE_DONE) is False


def test_countdown_end_moves_it_to_the_corner(record_path):
    """2 分钟一到（后端自动 resolve）就收进右下角常驻，不再是弹窗。"""
    record_path.write_text(json.dumps(RECORD), encoding="utf-8")
    asyncio.run(cp.restore_update_done_notice_on_startup())

    result = asyncio.run(cp.resolve_job_error("auto"))

    assert result["status"] == "resolved"
    assert cp._job_error_state["active"] is None
    alerts = cp._public_job_alerts()
    assert len(alerts) == 1
    assert alerts[0]["job"] == "update_done"
    assert alerts[0]["resolved_by"] == "auto"
    assert "当前版本 v1.2.6" in alerts[0]["message"]
    assert "更新范围" in alerts[0]["detail"]


def test_corner_notice_stays_until_dismissed(record_path):
    """右下角这条要留到用户自己点「×」：轮询多少次都还在，关掉才没有。"""
    record_path.write_text(json.dumps(RECORD), encoding="utf-8")
    asyncio.run(cp.restore_update_done_notice_on_startup())
    asyncio.run(cp.resolve_job_error("auto"))
    alert_id = cp._public_job_alerts()[0]["id"]

    for _ in range(3):                      # 面板每 5 秒轮询一次，读多少遍都还在
        assert [a["id"] for a in cp._public_job_alerts()] == [alert_id]

    asyncio.run(cp.dismiss_job_alert(cp.JobAlertDismiss(id=alert_id)))

    assert cp._public_job_alerts() == []


def test_user_click_keeps_the_same_information(record_path):
    """用户点「知道了」提前关掉弹窗时，右下角那张卡片的信息要和弹窗一致。"""
    record_path.write_text(json.dumps(RECORD), encoding="utf-8")
    asyncio.run(cp.restore_update_done_notice_on_startup())

    asyncio.run(cp.resolve_job_error("ignore"))

    alert = cp._public_job_alerts()[0]
    assert alert["resolved_by"] == "ignore"
    assert "当前版本 v1.2.6" in alert["message"]
    assert "- v1.2.6：更新完自动重启 ／ 顺带修一处笔误" in alert["detail"]


# ── 面板那一半 ────────────────────────────────────────────────────────────

def test_panel_dialog_and_card_speak_update_done():
    """面板要认得出这条不是故障：换文案、换图标、没有「停止」，卡片也是绿的一套。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert 'id="scrapeErrorIcon"' in html
    assert "function isUpdateDoneNotice(" in html
    assert "update_done: 'updateDoneTitle'," in html
    assert "update_done: 'updateDoneAlertTitle'," in html
    assert "update_done: {\n    auto: 'updateDoneTagAuto'," in html
    # 弹窗：详情标题、倒计时文案、主按钮、图标、绿色样式都跟着分岔
    assert "done ? 'updateDoneDetailTitle' : 'scrapeErrorDetailTitle'" in html
    assert "isUpdateDoneNotice(jobErrorView) ? 'updateDoneAutoIn' : 'scrapeErrorAutoIn'" in html
    assert "setText(okBtn, done ? 'updateDoneOk' : 'scrapeErrorIgnore')" in html
    assert "stopBtn.hidden = done;" in html
    assert "icon.textContent = done ? '⇪' : '!';" in html
    assert "document.querySelector('.error-dialog')?.classList.toggle('info', done);" in html
    assert "card.classList.toggle('info', done);" in html
    # 右下角卡片的标签要按任务换：不然会显示成「已自动继续」
    assert "const byJob = JOB_ALERT_TAGS_BY_JOB[alert.job];" in html
    # 中英文案都要有
    for key in ("updateDoneTitle", "updateDoneAlertTitle", "updateDoneDetailTitle",
                "updateDoneTimerNote", "updateDoneAutoIn", "updateDoneOk",
                "updateDoneTagAuto", "updateDoneTagSeen"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
