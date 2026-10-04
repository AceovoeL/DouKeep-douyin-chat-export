"""Tests for the panel's job-error flow: pause, dialog queue and notices.

Covers backend/control_panel.py (and the ``gate`` hook it passes into
extractor/video_downloader.py):

* which log lines count as fatal vs. per-file (merged) failures;
* a fatal error pauses the job and opens one dialog, extra errors queue up;
* dozens of failed images/voices merge into ONE dialog per run;
* 忽略 / 2-minute countdown continue the job and keep a corner notice;
* the 停止 button stops the job that owns the dialog;
* the video backfill gate pauses between videos and stops cleanly.
"""
import asyncio
import json
import sys

import pytest

from backend import control_panel as cp


class _FakeProc:
    """Stand-in for an asyncio subprocess: has pid/returncode/terminate."""

    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15


@pytest.fixture
def job_state(monkeypatch):
    """Fresh error registry + job states; records suspend/resume calls."""
    calls = []

    def _fake_suspend(pid, suspend):
        calls.append((pid, suspend))
        return 1

    monkeypatch.setattr(cp, "_set_process_tree_suspended", _fake_suspend)
    for key, value in (
        ("seq", 0), ("active", None), ("queue", []), ("alerts", []),
        ("merge", {}), ("resume_task", None),
    ):
        monkeypatch.setitem(cp._job_error_state, key, value)
    for key, value in (
        ("status", "running"), ("kind", "scrape"), ("message", ""),
        ("process", None), ("paused", False), ("paused_at", None),
        ("stopped", False),
    ):
        monkeypatch.setitem(cp._scrape_state, key, value)
    for state in (cp._backfill_state, cp._video_backfill_state):
        for key, value in (
            ("status", "idle"), ("total", 0), ("done", 0), ("ok", 0),
            ("failed", 0), ("message", ""), ("paused", False), ("stop", False),
        ):
            monkeypatch.setitem(state, key, value)
    monkeypatch.setitem(cp._video_backfill_state, "skipped", 0)
    return calls


def _public():
    return cp._public_job_error()


# ── log line classification ───────────────────────────────────────────────

def test_fatal_lines_are_recognised():
    assert cp.classify_scrape_log_line("  [!] 错误: TimeoutError: boom") == "fatal"
    assert cp.classify_scrape_log_line("[-] 没有匹配的会话。全部会话名称:") == "fatal"
    assert cp.classify_scrape_log_line("Traceback (most recent call last):") == "fatal"
    assert cp.classify_scrape_log_line("  [!] batch #3 连续 3 次失败，停止") == "fatal"
    assert cp.classify_scrape_log_line("  [+] 共发现 7 个会话") is None
    assert cp.classify_scrape_log_line("") is None


def test_per_file_failures_are_media_kind():
    assert cp.classify_scrape_log_line("  [media] image 失败: http 403") == "media"
    assert cp.classify_scrape_log_line("  [media] video 封面失败: boom") == "media"
    assert cp.classify_scrape_log_line("  [voice] 下载失败（空响应 0B）: srv_1") == "media"
    assert cp.classify_scrape_log_line("  [!] 下载头像失败 昵称: boom") == "media"
    assert cp.classify_scrape_log_line("  [!] 原生语音识别失败（消息已保存）: boom") == "media"
    # The step summary is not a failure itself, even when it mentions 失败 0.
    assert cp.classify_scrape_log_line("  [media] 图片/表情/视频封面 已下载 12 个 (失败 0)") is None
    # 统计行同理：带「失败」两个字，但失败数是 0 就不该弹框。
    assert cp.classify_scrape_log_line(
        "  [voice] 识别统计: 总数=47 缓存=0 请求=47 成功=47 失败=0 跳过=0"
    ) is None
    assert cp.classify_scrape_log_line(
        "  [voice] 回填会话完成: 总数=10 请求=10 成功=10 失败=0 跳过=0"
    ) is None
    assert cp.classify_scrape_log_line(
        "[voice] 历史回填完成: 总数=10 请求=10 成功=10 失败=0 跳过=0"
    ) is None
    # 失败数不为 0 仍然要报：逐条语音失败没有单独的日志行，汇总行是唯一信号。
    assert cp.classify_scrape_log_line(
        "  [voice] 识别统计: 总数=47 缓存=0 请求=47 成功=44 失败=3 跳过=0"
    ) == "media"
    # 服务器 id 恰好以 0 开头，不能被当成「失败=0」。
    assert cp.classify_scrape_log_line(
        "  [voice] 下载失败: 07692593954760574513: boom"
    ) == "media"


# ── dialog + pause + countdown ────────────────────────────────────────────

def test_fatal_error_pauses_scrape_and_opens_dialog(job_state):
    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] boom", "[-] boom\ntrace")
        info = _public()
        task = cp._job_error_state["resume_task"]
        if task:
            task.cancel()
        return info

    info = asyncio.run(scenario())
    assert job_state == [(4321, True)]          # the scraper was frozen
    assert cp._scrape_state["paused"] is True
    assert info["job"] == cp.JOB_SCRAPE
    assert info["running"] is True
    assert info["count"] == 1
    assert 0 < info["remaining_ms"] <= cp.PAUSE_AUTO_RESUME_SECONDS * 1000


def test_many_fatal_errors_queue_one_dialog_at_a_time(job_state):
    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] first", "d1")
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] second", "d2")
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] third", "d3")
        first = _public()
        # 忽略 the first one: the second dialog takes its place, job stays frozen
        await cp.resolve_job_error("ignore")
        second = _public()
        paused_after_first = cp._scrape_state["paused"]
        frozen_twice = list(job_state)
        await cp.resolve_job_error("ignore")
        third = _public()
        await cp.resolve_job_error("ignore")
        return first, second, third, paused_after_first, frozen_twice

    first, second, third, paused_after_first, frozen_twice = asyncio.run(scenario())
    assert (first["id"], second["id"], third["id"]) == (1, 2, 3)
    assert first["queued"] == 2 and second["queued"] == 1 and third["queued"] == 0
    assert paused_after_first is True
    # Only the first dialog suspends the process; the queued ones reuse it.
    assert frozen_twice == [(4321, True)]
    assert [pid for pid, suspend in job_state if suspend] == [4321]
    assert job_state[-1] == (4321, False)       # thawed once the queue drained
    assert cp._scrape_state["paused"] is False


def test_many_per_file_failures_merge_into_one_dialog(job_state):
    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        await cp.raise_job_error(cp.JOB_SCRAPE, "  [media] image 失败: 403", "l1", merge=True)
        await cp.raise_job_error(cp.JOB_SCRAPE, "  [media] image 失败: 404", "l2", merge=True)
        await cp.raise_job_error(cp.JOB_SCRAPE, "  [voice] 下载失败: srv_9", "l3", merge=True)
        task = cp._job_error_state["resume_task"]
        if task:
            task.cancel()
        return _public()

    info = asyncio.run(scenario())
    assert info["id"] == 1
    assert info["count"] == 3                   # one dialog, three failures
    assert cp._job_error_state["queue"] == []   # nothing extra popped up
    # the first detail is kept and every later failure is appended to it
    assert info["detail"].startswith("l1")
    assert "[voice] 下载失败" in info["detail"]
    assert "[media] image 失败: 404" in info["detail"]
    assert job_state == [(4321, True)]          # still only one pause


def test_merge_bucket_resets_between_runs(job_state):
    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        await cp.raise_job_error(cp.JOB_SCRAPE, "  [media] image 失败: 403", "l1", merge=True)
        await cp.resolve_job_error("ignore")
        cp.start_job_error_run(cp.JOB_SCRAPE)   # what _run_scrape does
        await cp.raise_job_error(cp.JOB_SCRAPE, "  [media] image 失败: 404", "l2", merge=True)
        task = cp._job_error_state["resume_task"]
        if task:
            task.cancel()
        return _public()

    info = asyncio.run(scenario())
    assert info["id"] == 2 and info["count"] == 1


def test_countdown_continues_job_and_keeps_notice(job_state):
    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] boom", "detail")
        error_id = cp._job_error_state["active"]["id"]
        pending = cp._job_error_state["resume_task"]
        if pending:
            pending.cancel()
        await cp._auto_resolve_job_error(error_id, 0)   # countdown reached zero

    asyncio.run(scenario())
    assert _public() is None
    assert cp._scrape_state["paused"] is False
    assert job_state == [(4321, True), (4321, False)]
    alerts = cp._public_job_alerts()
    assert len(alerts) == 1 and alerts[0]["resolved_by"] == "auto"
    assert alerts[0]["detail"] == "detail"


def test_ignore_continues_job_and_alerts_are_newest_first(job_state):
    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] older", "d1")
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] newer", "d2")
        await cp.resolve_job_error("ignore")    # 忽略 the older one
        await cp.resolve_job_error("ignore")    # 忽略 the newer one

    asyncio.run(scenario())
    assert cp._public_job_error() is None
    alerts = cp._public_job_alerts()
    assert [a["message"] for a in alerts] == ["[-] newer", "[-] older"]
    assert [a["resolved_by"] for a in alerts] == ["ignore", "ignore"]


def test_alert_dismiss_route_drops_one_or_all(job_state):
    async def scenario():
        cp._job_error_state["alerts"] = [{"id": 1, "message": "a"}, {"id": 2, "message": "b"}]
        one = await cp.dismiss_job_alert(cp.JobAlertDismiss(id=1))
        remaining = [a["id"] for a in cp._job_error_state["alerts"]]
        every = await cp.dismiss_job_alert(None)
        return one, remaining, every

    one, remaining, every = asyncio.run(scenario())
    assert one == {"status": "ok", "remaining": 1}
    assert remaining == [2]
    assert every == {"status": "ok", "remaining": 0}


def test_stop_dialog_button_stops_the_owning_job(job_state):
    async def scenario():
        proc = _FakeProc()
        cp._scrape_state["process"] = proc
        await cp.raise_job_error(cp.JOB_SCRAPE, "[-] boom", "detail")
        result = await cp.stop_job_error()
        return proc, result

    proc, result = asyncio.run(scenario())
    assert result["status"] == "stopped" and result["job"] == cp.JOB_SCRAPE
    assert proc.terminated is True
    assert _public() is None
    assert cp._public_job_alerts()[0]["resolved_by"] == "stop"


def test_stop_dialog_button_stops_a_media_job(job_state):
    async def scenario():
        cp._video_backfill_state["status"] = "running"
        await cp.raise_job_error(cp.JOB_MEDIA_VIDEOS, "[-] 视频下载失败: boom", "d")
        return await cp.stop_job_error()

    result = asyncio.run(scenario())
    assert result["job"] == cp.JOB_MEDIA_VIDEOS
    assert cp._video_backfill_state["stop"] is True
    assert _public() is None


# ── log watcher ───────────────────────────────────────────────────────────

def test_watcher_separates_fatal_and_media_lines(tmp_path, job_state):
    log = tmp_path / "scrape.log"
    log.write_text(
        "[*] 开始采集\n"
        "  [media] image 失败: 403\n"
        "  [media] image 失败: 404\n",
        encoding="utf-8",
    )

    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        task = asyncio.create_task(cp._watch_scrape_log(str(log), "scrape"))
        # 等两条 [media] 都并进同一个弹窗（count == 2）**并且**进程被冻住：只等 active
        # 会在「登记弹窗」和「冻进程 / 合并第二条」之间抢跑，机器一忙就偶发失败。
        for _ in range(60):
            await asyncio.sleep(0.05)
            active = cp._job_error_state.get("active")
            if active and active.get("count") == 2 and cp._scrape_state.get("paused"):
                break
        task.cancel()
        pending = cp._job_error_state.get("resume_task")
        if pending:
            pending.cancel()
        return _public()

    info = asyncio.run(scenario())
    assert info is not None and info["count"] == 2   # merged, one dialog
    assert info["message"] == "[media] image 失败: 403"
    assert job_state == [(4321, True)]


def test_watcher_queues_each_fatal_line(tmp_path, job_state):
    log = tmp_path / "scrape.log"
    log.write_text("  [!] 错误: first\n  [!] 错误: second\n", encoding="utf-8")

    async def scenario():
        cp._scrape_state["process"] = _FakeProc()
        task = asyncio.create_task(cp._watch_scrape_log(str(log), "scrape"))
        for _ in range(40):
            await asyncio.sleep(0.1)
            if cp._job_error_state.get("queue"):
                break
        task.cancel()
        pending = cp._job_error_state.get("resume_task")
        if pending:
            pending.cancel()
        return _public()

    info = asyncio.run(scenario())
    # Both conversations failed: each gets its own dialog, the second queued.
    assert info is not None and info["message"] == "[!] 错误: first"
    assert info["queued"] == 1


def test_failed_scrape_job_reports_dialog(tmp_path, monkeypatch, job_state):
    """A scrape that dies still opens the dialog (nothing left to pause)."""
    monkeypatch.setattr(cp, "LOG_PATH", str(tmp_path / "scrape.log"))
    monkeypatch.setattr(cp, "PAUSE_AUTO_RESUME_SECONDS", 0.2)

    async def _no_notify(title, desp):
        return None

    monkeypatch.setattr(cp, "_notify_on_failure", _no_notify)
    cmd = [
        sys.executable, "-u", "-c",
        "print('[-] 错误: nope', flush=True); import time; time.sleep(0.4); raise SystemExit(3)",
    ]

    async def scenario():
        await cp._run_scrape(cmd)
        info = _public()
        await cp.resolve_job_error("ignore")
        return info

    info = asyncio.run(scenario())
    assert cp._scrape_state["status"] == "failed"
    assert info is not None and info["running"] is False
    assert cp._public_job_alerts()[0]["job"] == cp.JOB_SCRAPE


# ── media jobs ────────────────────────────────────────────────────────────

def test_video_backfill_pauses_on_failure_then_continues(monkeypatch, job_state):
    from extractor import video_downloader as vd

    seen = {}

    async def fake_backfill(progress_cb=None, gate=None, **kwargs):
        progress_cb({
            "ok": 0, "fail": 1, "skipped": 0, "total": 3,
            "current": "file1", "last_error": "file1: boom",
        })
        await asyncio.sleep(0.05)              # let the panel register the error
        seen["paused"] = cp._video_backfill_state.get("paused")
        seen["info"] = _public()
        await cp.resolve_job_error("ignore")   # user clicks 忽略并继续
        await gate()                           # …and the download resumes
        return {"total": 3, "ok": 1, "fail": 1, "skipped": 0, "log": [], "stopped": False}

    monkeypatch.setattr(vd, "backfill", fake_backfill)
    asyncio.run(cp._run_video_backfill())

    assert seen["paused"] is True
    assert seen["info"]["job"] == cp.JOB_MEDIA_VIDEOS
    assert seen["info"]["count"] == 1
    assert cp._video_backfill_state["status"] == "completed"
    assert cp._public_job_alerts()[0]["resolved_by"] == "ignore"


def test_video_backfill_stop_raises_through_the_gate(monkeypatch, job_state):
    from extractor import video_downloader as vd

    async def fake_backfill(progress_cb=None, gate=None, **kwargs):
        cp._video_backfill_state["status"] = "running"
        cp._video_backfill_state["stop"] = True
        try:
            await gate()
        except vd.JobStopped:
            return {"total": 1, "ok": 0, "fail": 0, "skipped": 0, "log": [], "stopped": True}
        raise AssertionError("gate should have raised JobStopped")

    monkeypatch.setattr(vd, "backfill", fake_backfill)
    asyncio.run(cp._run_video_backfill())
    assert cp._video_backfill_state["status"] == "idle"
    assert "已停止" in cp._video_backfill_state["message"]


def test_video_backfill_exception_opens_dialog(monkeypatch, job_state):
    from extractor import video_downloader as vd

    async def boom(progress_cb=None, gate=None, **kwargs):
        raise RuntimeError("browser 启动失败")

    monkeypatch.setattr(vd, "backfill", boom)

    async def scenario():
        await cp._run_video_backfill()
        info = _public()
        pending = cp._job_error_state.get("resume_task")
        if pending:
            pending.cancel()
        return info

    info = asyncio.run(scenario())
    assert cp._video_backfill_state["status"] == "failed"
    assert info is not None and info["job"] == cp.JOB_MEDIA_VIDEOS
    assert info["running"] is False


def test_video_backfill_no_op_run_is_reported(monkeypatch, job_state):
    from extractor import video_downloader as vd

    async def fake_backfill(progress_cb=None, gate=None, **kwargs):
        return {"total": 5, "ok": 0, "fail": 0, "skipped": 0,
                "log": ["[-] login required"], "stopped": False}

    monkeypatch.setattr(vd, "backfill", fake_backfill)

    async def scenario():
        await cp._run_video_backfill()
        info = _public()
        pending = cp._job_error_state.get("resume_task")
        if pending:
            pending.cancel()
        return info

    info = asyncio.run(scenario())
    assert info is not None and "没有成功" in info["message"]


def test_save_cenc_jobs_gate_pauses_and_stops(tmp_path, monkeypatch):
    from extractor import video_downloader as vd

    monkeypatch.setattr(vd, "VIDEOS_DIR", str(tmp_path))

    async def fake_resolve(page, tkeys):
        return {}

    monkeypatch.setattr(vd, "_resolve_batch", fake_resolve)
    progress = []
    jobs = [
        {"msg_id": "m1", "file_id": "f1", "tkey": "t1", "skey": "00" * 16},
        {"msg_id": "m2", "file_id": "f2", "tkey": "t2", "skey": "00" * 16},
    ]

    async def scenario():
        calls = []

        async def gate():
            calls.append(len(calls))
            if len(calls) == 2:
                raise vd.JobStopped()

        result = await vd.save_cenc_jobs(None, jobs, progress_cb=progress.append, gate=gate)
        return result, calls

    result, calls = asyncio.run(scenario())
    assert len(calls) == 2                    # checked before each video
    assert result["stopped"] is True
    assert result["fail"] == 1 and result["ok"] == 0
    assert progress and progress[0]["last_error"].endswith("no URL from resolver")


def test_image_backfill_reports_failure_and_pauses(temp_db, monkeypatch, job_state):
    from extractor import web_scraper
    from backend import database
    from tests.conftest import insert_conversation, insert_message

    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_img", "c1", 1, msg_type=2, media_url="https://example.com/a.gif")
    insert_message(conn, "srv_img2", "c1", 2, msg_type=2, media_url="https://example.com/b.gif")
    conn.commit()
    conn.close()

    def boom(url, out_dir):
        raise RuntimeError("cdn 403")

    monkeypatch.setattr(web_scraper, "_save_emoji", boom)
    monkeypatch.setattr(web_scraper, "_save_image", boom)

    async def scenario():
        task = asyncio.create_task(cp._run_backfill())
        info = None
        for _ in range(60):
            await asyncio.sleep(0.05)
            info = _public()
            if info:
                break
        paused = cp._backfill_state.get("paused")
        await cp.resolve_job_error("ignore")
        await asyncio.wait_for(task, timeout=10)
        return info, paused

    info, paused = asyncio.run(scenario())
    assert info is not None and info["job"] == cp.JOB_MEDIA_IMAGES
    # The job really stopped between the two failed files…
    assert paused is True
    assert cp._backfill_state["failed"] == 2
    assert cp._backfill_state["status"] == "completed"
    # …and the second failure folded into the same notice instead of popping up
    assert _public() is None
    alerts = cp._public_job_alerts()
    assert alerts[0]["job"] == cp.JOB_MEDIA_IMAGES
    assert alerts[0]["count"] == 2


# ── /api/status contract ──────────────────────────────────────────────────

def test_status_endpoint_exposes_job_error_and_alerts(temp_db, job_state, monkeypatch):
    monkeypatch.setitem(cp._video_backfill_state, "status", "running")

    async def scenario():
        await cp.raise_job_error(cp.JOB_MEDIA_VIDEOS, "[-] 视频下载失败: boom", "d")
        status = await cp.panel_status()
        pending = cp._job_error_state["resume_task"]
        if pending:
            pending.cancel()
        return status

    status = asyncio.run(scenario())
    assert status["job_error"]["active"]["job"] == cp.JOB_MEDIA_VIDEOS
    assert status["job_error"]["active"]["running"] is True
    assert status["job_error"]["alerts"] == []
    assert "error" not in status["scrape"]      # moved to job_error
