"""Control panel for managing scraper, viewer, and export."""
import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from threading import Lock

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from backend import database
from common import access as _access
from common import config as _cfg, paths
from common import appearance as _appearance
from common import github_auth as _github_auth
from common import version as _version
from backend.panel import emoji_pack as _emoji_pack
from backend.panel import notify as _notify
from backend.panel.scheduler import (
    parse_cron as _parse_cron,
    next_cron_run as _next_cron_run,
    next_simple_rule_run as _next_update_run,
    normalize_update_schedule as _normalize_update_schedule,
    simple_rule_problem as _simple_rule_problem,
    simple_rule_text as _simple_rule_text,
    simple_rule_to_cron as _simple_rule_to_cron,
    cron_to_simple_rule as _cron_to_simple_rule,
)
from backend.panel.scheduler import DEFAULT_UPDATE_TIMES as _UPDATE_DEFAULT_TIMES

control_router = APIRouter(prefix="/panel")

# The panel single-page app lives in backend/panel/static/panel.html and is
# loaded once at import; panel_page() serves it verbatim.
_PANEL_HTML_PATH = os.path.join(os.path.dirname(__file__), "panel", "static", "panel.html")
with open(_PANEL_HTML_PATH, encoding="utf-8") as _f:
    PANEL_HTML = _f.read()


# ── Persistent config (config/panel_config.json) — implemented in common.config ──
def _load_config():
    return _cfg.load_config()


def _save_config(cfg):
    _cfg.save_config(cfg)


def _utf8_subprocess_env() -> dict:
    """强制被重定向的 Python 子进程输出走 UTF-8（Windows 默认 GBK 会乱码）。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _decode_log_bytes(chunk: bytes) -> str:
    """一段日志字节 → 文字：先按 UTF-8，解不开就按旧的 Windows GBK/GB18030。

    读日志的两处都用它：整份读的 ``_read_utf8_or_gbk``（按行逐段调用），以及增量跟读
    采集日志的 ``_watch_scrape_log``（每读到一段就解一段）。
    """
    try:
        return chunk.decode("utf-8")
    except UnicodeDecodeError:
        return chunk.decode("gb18030", errors="replace")


def _read_utf8_or_gbk(path: str) -> str:
    """读日志：优先 UTF-8，回退到旧的 Windows GBK/GB18030 日志。

    判断编码时**按行**来，不是整份文件挑一种：config/logs/server.log 是一直往后追加的，只要
    这台机器上先后出现过两种编码的服务，同一个文件里就会一段 UTF-8、一段 GBK。原因是
    服务的输出变成什么编码取决于它是怎么起来的 —— start.ps1 /「启动服务（双击）.bat」/
    面板的自动重启助手都会带上 PYTHONUTF8=1（写 UTF-8），但照着 README 手敲
    ``python -m uvicorn``、或者用 nssm / 计划任务托管时没人设它，Windows 默认就按 GBK 写。

    整份挑一种编码时，只要文件里有 GBK 的字节，UTF-8 那几段就会被整段读错（``→`` 变成
    ``鈫``、``—`` 变成 ``鈥``，面板上真机反馈过这种乱码）；反过来先猜 GBK 也一样会毁掉
    UTF-8 那几段。按行拆开各自判断，两种编码的段落就都能读对。

    换行符在 UTF-8 / GBK / GB18030 里都是同一个 ``0x0A`` 字节（这两个多字节编码的后续
    字节都 >= 0x30，不会撞上 0x0A），所以按 ``\\n`` 切开是安全的；切完再拼回去，结果和
    「整份都是同一种编码」时逐字节一致。
    """
    with open(path, "rb") as file:
        raw = file.read()
    if raw.startswith(b"\xef\xbb\xbf"):     # 带 BOM 的 UTF-8：和 utf-8-sig 一样先丢掉
        raw = raw[3:]
    return "\n".join(_decode_log_bytes(chunk) for chunk in raw.split(b"\n"))


# ── Scrape job state ──
_scrape_state = {
    "status": "idle",  # idle | running | completed | failed
    "kind": "scrape",  # scrape | voice_backfill
    "started_at": None,
    "finished_at": None,
    "message": "",
    "process": None,
    # Auto-pause on error (see _job_error_state below): while a dialog is open
    # for this job the child process is suspended until the user answers or the
    # countdown runs out.
    "paused": False,
    "paused_at": None,
}

# ── Export state ──
_export_state = {
    "status": "idle",
    "file_path": None,
    "message": "",
}

# Database export/import replaces a single SQLite file. Keep the file swap
# serialized with snapshots and retain a rollback copy of the previous file.
_DATABASE_FILE_LOCK = Lock()
_DATABASE_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024

# ── Scheduler state ──
_scheduler_state = {
    "enabled": False,
    "schedule": "",
    "task": None,
    "next_run": None,
}

# ── Conversation discovery (refresh conv list) state ──
_discover_state = {
    "status": "idle",  # idle | running | completed | failed
    "message": "",
    "process": None,
    "started_at": None,
    "finished_at": None,
    # 出错弹窗把子进程冻住（和采集一样），用户点「停止刷新」时置 True：
    # 这样 kill 掉进程不会被当成一次失败再弹一个窗。
    "paused": False,
    "paused_at": None,
    "stopped": False,
}

# ── Media backfill state ──
_backfill_state = {
    "status": "idle",  # idle | running | completed | failed
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "paused": False,   # set while an error dialog is open for this job
    "stop": False,     # cooperative stop requested from the panel
}

_video_backfill_state = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "skipped": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "paused": False,
    "stop": False,
}

# ── Self-update state（「关于」页的更新按钮） ──
_update_state = {
    "status": "idle",          # idle | running | completed | failed
    "message": "",
    "process": None,
    "started_at": None,
    "finished_at": None,
    "from_version": None,      # 发起更新时的本地版本 / 要更新到的远端版本
    "to_version": None,
    "requested_at": None,      # 点下「立即更新」的时间
    "allow_dirty": False,      # 是否忽略了「工作区有改动」的提示
    "auto_restart": True,      # 这次更新完成后要不要自动重启服务
    "auto_started": False,     # 这次更新是不是定时自动开始的（不是用户点的）
}

# ── 定时自动检查更新（「关于」页的设置） ──
#
# 和上面的「定时采集」是两回事：那边按 cron 采集消息，这边只是到点去远端比对
# 一下版本，不下载任何东西。设置存在 panel_config.json 的 update_schedule 里。
# 时间表用的是和「定时采集」同一套勾选式规则（rule：每天/每周几/每月几号 + 若干
# 时间点），所以同一天可以检查好几次；面板两边的勾选框长得一样，算法也同一份
# （backend/panel/scheduler.py）。
# auto_update 是「发现新版本就自己装」的开关：默认关（只检查、只通知），打开后
# 也要先过几道安全阀才会真的动手，见 _auto_update_blocker。
_update_schedule_state = {
    "enabled": False,
    # 勾选式时间表：{"mode": "daily"|"weekly"|"monthly", "weekdays": [...],
    #               "monthdays": [...], "times": ["09:00", "20:00"]}
    "rule": {"mode": "daily", "weekdays": [], "monthdays": [],
             "times": list(_UPDATE_DEFAULT_TIMES)},
    "auto_update": False,      # 发现新版本时自动下载安装（默认关）
    "task": None,              # 后台等待中的 asyncio 任务
    "next_run": None,
    "last_run": None,          # 上次自动检查的时间
    "last_result": None,       # 上次自动检查的结果（面板直接显示它）
    "running": False,
}

#: 定时设置里跟「时间表」有关、需要存进配置文件的字段（其余是运行期状态）
_UPDATE_SCHEDULE_FIELDS = ("enabled", "rule", "auto_update")

#: 跑完一次检查后先等一会儿再算下一次：时钟精度、系统时间被调回去时，同一个时间点
#: 不至于被连着触发两次。
_UPDATE_SCHEDULE_SETTLE_SECONDS = 1.0

LOG_PATH = paths.SCRAPE_LOG
VOICE_LOG_PATH = paths.VOICE_TRANSCRIPTION_LOG
DISCOVER_LOG_PATH = paths.DISCOVER_LOG
CONV_LIST_PATH = paths.CONVERSATIONS_LIST
# 「关于 → 更新」的输出：拉取代码 / 装依赖 / 构建前端，与采集日志同一份格式，
# 复用面板上的日志框。
UPDATE_LOG_PATH = paths.UPDATE_LOG


# ── Job errors: auto-pause + dialog queue + persistent corner notices ──
# Every long-running job (采集 / 语音转写补充 / 下载历史图片 / 下载历史视频)
# reports problems here. The panel shows ONE dialog at a time with a 2-minute
# countdown and keeps the rest queued; after 忽略 or the countdown the job
# continues and the error stays in the bottom-right corner until the user
# closes that notice. Repeated per-file failures (images, voices, avatars...)
# are merged into a single dialog, so a run with dozens of bad files asks once.
PAUSE_AUTO_RESUME_SECONDS = 120
# Kept as bottom-right notices until the user closes them. The panel caps the
# stack to the window height and scrolls, so older ones stay reachable.
MAX_JOB_ALERTS = 20
MAX_JOB_ERROR_QUEUE = 20
MAX_ERROR_DETAIL_LINES = 40
ERROR_DETAIL_TAIL_LINES = 60

JOB_SCRAPE = "scrape"
JOB_VOICE = "voice_backfill"
JOB_MEDIA_IMAGES = "media_images"
JOB_MEDIA_VIDEOS = "media_videos"
JOB_UPDATE = "update"
#: 「刷新会话列表」也是一份会出问题的任务：它同样走弹窗 + 暂停 + 右下角提示那一套。
JOB_REFRESH = "refresh"
#: 「更新完成」也算一种要弹给用户看、并且会留到右下角的消息（走和报错同一套队列，
#: 只是它不暂停任何任务、也没有「停止」这个选项）。
JOB_UPDATE_DONE = "update_done"

#: tools/update.py 在工作区有未提交改动时的退出码（面板据此提示「仍然更新」）
UPDATE_EXIT_DIRTY = 2

# Fatal markers / per-conversation exceptions: each one gets its own dialog.
_FATAL_ERROR_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"^\[-\]",                                        # [-] 错误 / 未找到 / 未能登录
    r"^\[!\]\s*错误[:：]",                             # 单个会话抓取异常
    r"^\[!\]\s*本次全量抓取一条消息都没拿到",
    r"^\[!\]\s*batch #\d+ 连续 \d+ 次失败",
    r"^\[!\]\s*未能获取 short_id",
    r"^Traceback \(most recent call last\)",
))

# Per-file download/transcription warnings: merged into ONE dialog per run, so
# a conversation with dozens of dead CDN links does not spam the panel.
_MEDIA_FAILURE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"^\[(media|voice|forward)\].*失败",
    r"^\[!\]\s*(下载|补全|原生语音识别|合并转发).*失败",
))

# "…已下载 12 个 (失败 0)" is the summary of that step, not a failure itself.
_MEDIA_SUMMARY_PATTERN = re.compile(r"已下载 \d+ 个 \(失败")

# PROCESS_SUSPEND_RESUME access right, used with NtSuspendProcess/NtResumeProcess.
_PROCESS_SUSPEND_RESUME = 0x0800


def classify_scrape_log_line(line: str) -> str | None:
    """Classify a scraper log line: 'fatal', 'media' or None (not an error)."""
    text = (line or "").strip()
    if not text:
        return None
    if any(pattern.search(text) for pattern in _FATAL_ERROR_PATTERNS):
        return "fatal"
    if _MEDIA_SUMMARY_PATTERN.search(text):
        return None
    if any(pattern.search(text) for pattern in _MEDIA_FAILURE_PATTERNS):
        return "media"
    return None


def _is_scrape_error_line(line: str) -> bool:
    """True for log lines that mean the scrape actually hit an error."""
    return classify_scrape_log_line(line) is not None


def _scrape_log_tail(log_path: str | None = None, lines: int = ERROR_DETAIL_TAIL_LINES) -> str:
    """Last few log lines, used as the modal's error detail."""
    path = log_path or LOG_PATH
    if not os.path.exists(path):
        return ""
    try:
        all_lines = _read_utf8_or_gbk(path).splitlines()
    except Exception:
        return ""
    return "\n".join(all_lines[-lines:]).strip()


def _descendant_pids(root_pid: int) -> list[int]:
    """Best-effort list of child PIDs (Windows only, empty elsewhere)."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    except Exception:
        return []
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []

    children: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    except Exception:
        children = {}
    finally:
        kernel32.CloseHandle(snapshot)

    found: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        found.append(pid)
        pending.extend(children.get(pid, []))
    return found


def _set_one_process_suspended(pid: int, suspend: bool) -> bool:
    """Freeze/thaw one process. Windows uses NtSuspendProcess, POSIX SIGSTOP."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            handle = kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
            if not handle:
                return False
            try:
                ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
                func = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
                func.restype = ctypes.c_long
                func.argtypes = [wintypes.HANDLE]
                return func(handle) == 0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    import signal
    try:
        os.kill(int(pid), signal.SIGSTOP if suspend else signal.SIGCONT)
        return True
    except Exception:
        return False


def _set_process_tree_suspended(root_pid: int, suspend: bool) -> int:
    """Suspend/resume the scraper and the browser processes it spawned.

    Freezing the Playwright browser too keeps the paused job from timing out
    while the dialog waits for the user. Returns how many processes reacted.
    """
    if not root_pid:
        return 0
    targets = [root_pid] + _descendant_pids(root_pid)
    return sum(1 for pid in targets if _set_one_process_suspended(pid, suspend))


_job_error_state = {
    "seq": 0,
    "active": None,   # error shown in the dialog right now
    "queue": [],      # errors waiting for their own dialog
    "alerts": [],     # resolved errors, oldest first
    "merge": {},      # job -> merged per-file error of the current run
    "resume_task": None,
}

# asyncio only keeps weak references to tasks: hold onto the fire-and-forget
# ones (progress callbacks are synchronous) so they cannot vanish mid-flight.
_background_tasks: set = set()


def _scrape_job_kind(kind: str) -> str:
    if kind == "voice_backfill":
        return JOB_VOICE
    if kind == "update":
        return JOB_UPDATE
    if kind == "refresh":
        return JOB_REFRESH
    return JOB_SCRAPE


def _job_label(kind: str) -> str:
    """任务名，给日志、状态文案和失败通知共用。"""
    if kind == "voice_backfill":
        return "语音转写补充"
    if kind == "update":
        return "更新"
    if kind == "refresh":
        return "刷新会话列表"
    if kind == JOB_UPDATE_DONE:
        return "更新完成"
    return "采集"


def _job_is_running(job: str) -> bool:
    if job in (JOB_SCRAPE, JOB_VOICE):
        proc = _scrape_state.get("process")
        return bool(proc is not None and getattr(proc, "returncode", None) is None)
    if job == JOB_REFRESH:
        return _discover_state.get("status") == "running"
    if job == JOB_UPDATE:
        proc = _update_state.get("process")
        return bool(proc is not None and getattr(proc, "returncode", None) is None)
    if job == JOB_MEDIA_IMAGES:
        return _backfill_state.get("status") == "running"
    if job == JOB_MEDIA_VIDEOS:
        return _video_backfill_state.get("status") == "running"
    return False


def _public_job_error() -> dict | None:
    """The error currently in the dialog (countdown included), or None."""
    active = _job_error_state.get("active")
    if not active:
        return None
    remaining = 0.0
    if active.get("resume_at"):
        remaining = max(0.0, active["resume_at"] - time.time())
    return {
        "id": active.get("id"),
        "job": active.get("job"),
        "message": active.get("message", ""),
        "detail": active.get("detail", ""),
        "at": active.get("at"),
        "count": int(active.get("count", 1)),
        "running": _job_is_running(active.get("job")),
        "remaining_ms": int(remaining * 1000),
        "queued": len(_job_error_state.get("queue") or []),
    }


def _public_job_alerts() -> list[dict]:
    """Resolved notices, newest first — the panel renders them top to bottom."""
    alerts = list(_job_error_state.get("alerts") or [])
    out = []
    for alert in reversed(alerts):
        out.append({
            "id": alert.get("id"),
            "job": alert.get("job"),
            "message": alert.get("message", ""),
            "detail": alert.get("detail", ""),
            "at": alert.get("at") or time.time(),
            "count": int(alert.get("count", 1)),
            "resolved_by": alert.get("resolved_by") or "auto",
        })
    return out


async def _set_job_process_suspended(state: dict, suspend: bool) -> bool:
    """Freeze/thaw a subprocess-backed job. False when there is nothing to act on.

    采集和「刷新会话列表」都是起一个子进程（外加它拉起的 Chromium），暂停方式完全一样，
    只是进程记在不同的 state 里。
    """
    proc = state.get("process")
    pid = getattr(proc, "pid", None)
    if proc is None or getattr(proc, "returncode", None) is not None or not pid:
        return False
    return bool(await asyncio.to_thread(_set_process_tree_suspended, pid, suspend))


async def _resume_scrape_process() -> bool:
    """Wake up the suspended scraper (no-op when the job already finished)."""
    return await _set_job_process_suspended(_scrape_state, False)


async def _pause_scrape_process() -> bool:
    """Suspend the running scraper. False when there is nothing to suspend."""
    return await _set_job_process_suspended(_scrape_state, True)


async def _pause_refresh_process() -> bool:
    """Suspend the running 「刷新会话列表」 child (same shape as the scraper)."""
    return await _set_job_process_suspended(_discover_state, True)


async def _resume_refresh_process() -> bool:
    """Wake up a suspended 「刷新会话列表」 child (no-op when it already finished)."""
    return await _set_job_process_suspended(_discover_state, False)


async def _pause_job(job: str) -> bool:
    """Freeze the job behind the dialog so the user can read the error."""
    if job in (JOB_SCRAPE, JOB_VOICE):
        if _scrape_state.get("paused"):
            return True  # already frozen — never stack suspensions
        paused = await _pause_scrape_process()
        _scrape_state["paused"] = paused
        _scrape_state["paused_at"] = time.time() if paused else None
        return paused
    if job == JOB_REFRESH:
        if _discover_state.get("paused"):
            return True
        paused = await _pause_refresh_process()
        _discover_state["paused"] = paused
        _discover_state["paused_at"] = time.time() if paused else None
        return paused
    if job == JOB_MEDIA_IMAGES:
        _backfill_state["paused"] = True
        _backfill_state["paused_at"] = time.time()
        return True
    if job == JOB_MEDIA_VIDEOS:
        _video_backfill_state["paused"] = True
        _video_backfill_state["paused_at"] = time.time()
        return True
    return False


async def _resume_job(job: str) -> None:
    """Let a paused job continue (no-op when it already finished)."""
    if job in (JOB_SCRAPE, JOB_VOICE):
        await _resume_scrape_process()
        _scrape_state["paused"] = False
        _scrape_state["paused_at"] = None
    elif job == JOB_REFRESH:
        await _resume_refresh_process()
        _discover_state["paused"] = False
        _discover_state["paused_at"] = None
    elif job == JOB_MEDIA_IMAGES:
        _backfill_state["paused"] = False
        _backfill_state["paused_at"] = None
    elif job == JOB_MEDIA_VIDEOS:
        _video_backfill_state["paused"] = False
        _video_backfill_state["paused_at"] = None


async def _wait_while_job_paused(state: dict) -> bool:
    """Cooperative pause for in-process jobs. False = user asked to stop."""
    while True:
        if state.get("stop"):
            return False
        if state.get("paused"):
            await asyncio.sleep(0.2)
            continue
        return True


def start_job_error_run(job: str) -> None:
    """A new run starts fresh: forget the previous run's merged per-file error."""
    _job_error_state.setdefault("merge", {}).pop(job, None)


def _append_error_detail(error: dict, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    detail = (error.get("detail") or "").strip()
    if text in detail:
        return
    lines = [line for line in detail.splitlines() if line.strip()]
    lines.append(text)
    error["detail"] = "\n".join(lines[-MAX_ERROR_DETAIL_LINES:])


def register_job_error(job: str, message: str, detail: str = "",
                       *, merge: bool = False) -> dict | None:
    """Record a problem; the first one goes on screen, extra ones wait in line.

    ``merge=True`` folds repeated per-file failures into the one error the run
    already reported, so the panel asks once per run instead of once per file.
    """
    message = (message or "").strip()
    if not message:
        return None
    if merge:
        existing = _job_error_state.setdefault("merge", {}).get(job)
        if existing is not None:
            existing["count"] = int(existing.get("count", 1)) + 1
            _append_error_detail(existing, message)
            return existing
    _job_error_state["seq"] = int(_job_error_state.get("seq", 0)) + 1
    error = {
        "id": _job_error_state["seq"],
        "job": job,
        "message": message,
        "detail": (detail or message).strip(),
        "at": time.time(),
        "count": 1,
        "resume_at": None,
        "resolved_by": None,
        "pause_done": False,
    }
    if merge:
        _job_error_state.setdefault("merge", {})[job] = error
    if _job_error_state.get("active") is None:
        _job_error_state["active"] = error
        error["resume_at"] = time.time() + PAUSE_AUTO_RESUME_SECONDS
    else:
        queue = _job_error_state.setdefault("queue", [])
        queue.append(error)
        del queue[:-MAX_JOB_ERROR_QUEUE]
    return error


async def _ensure_job_error_active(error: dict) -> None:
    """Pause the job behind the on-screen error and start its countdown."""
    if _job_error_state.get("active") is not error or error.get("pause_done"):
        return
    error["pause_done"] = True
    await _pause_job(error.get("job"))
    _schedule_job_error_timeout(error["id"])


async def raise_job_error(job: str, message: str, detail: str = "",
                          *, merge: bool = False) -> dict | None:
    """Register a problem, pause its job and start the 2-minute countdown."""
    error = register_job_error(job, message, detail, merge=merge)
    if error is None:
        return None
    await _ensure_job_error_active(error)
    return error


def raise_job_error_soon(job: str, message: str, detail: str = "",
                         *, merge: bool = False) -> None:
    """Same as raise_job_error for synchronous callbacks (progress hooks)."""
    task = asyncio.create_task(raise_job_error(job, message, detail, merge=merge))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _push_job_alert(error: dict) -> None:
    """Keep a resolved error as a corner notice until the user closes it."""
    alerts = _job_error_state.setdefault("alerts", [])
    if any(alert is error for alert in alerts):
        return
    alerts.append(error)
    del alerts[:-MAX_JOB_ALERTS]


async def resolve_job_error(resolved_by: str, *, job: str | None = None) -> dict:
    """Close the dialog on screen: continue the job, keep a corner notice.

    ``resolved_by`` records why it closed: auto (countdown), ignore (user
    clicked 忽略) or stop (user stopped the job). ``job`` limits it to the job
    that owns the dialog, which the section's own 停止 button relies on.
    """
    active = _job_error_state.get("active")
    if active is None:
        return {"status": "no_error"}
    if job is not None and active.get("job") != job:
        return {"status": "other_job"}
    task = _job_error_state.get("resume_task")
    _job_error_state["resume_task"] = None
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    active["resolved_by"] = resolved_by
    active["resolved_at"] = time.time()
    _job_error_state["active"] = None
    _push_job_alert(active)
    queue = _job_error_state.setdefault("queue", [])
    nxt = queue.pop(0) if queue else None
    if nxt is not None:
        # More errors are waiting: show the next dialog and stay paused.
        _job_error_state["active"] = nxt
        nxt["resume_at"] = time.time() + PAUSE_AUTO_RESUME_SECONDS
        await _ensure_job_error_active(nxt)
    else:
        await _resume_job(active.get("job"))
    return {
        "status": "resolved",
        "alert_id": active.get("id"),
        "next_id": (nxt or {}).get("id"),
        "queued": len(queue),
    }


async def _auto_resolve_job_error(error_id: int, delay: float) -> None:
    """Countdown timer: nobody clicked anything, so let the job continue."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    active = _job_error_state.get("active")
    if not active or active.get("id") != error_id:
        return
    await resolve_job_error("auto")


def _schedule_job_error_timeout(error_id: int, delay: float | None = None) -> None:
    if delay is None:
        delay = PAUSE_AUTO_RESUME_SECONDS
    previous = _job_error_state.get("resume_task")
    if previous is not None and not previous.done():
        previous.cancel()
    _job_error_state["resume_task"] = asyncio.create_task(_auto_resolve_job_error(error_id, delay))


async def stop_job(job: str) -> dict:
    """Stop whichever job reported the error shown in the dialog."""
    if job in (JOB_SCRAPE, JOB_VOICE):
        return await stop_scrape()
    if job == JOB_REFRESH:
        return await _stop_discover()
    if job == JOB_MEDIA_IMAGES:
        return _stop_media_backfill()
    if job == JOB_MEDIA_VIDEOS:
        return _stop_video_backfill()
    return {"status": "unknown_job"}


def _stop_media_backfill() -> dict:
    state = _backfill_state
    if state.get("status") != "running":
        return {"status": "not_running"}
    state["stop"] = True
    state["paused"] = False   # let the loop reach its stop check
    return {"status": "stopping"}


def _stop_video_backfill() -> dict:
    state = _video_backfill_state
    if state.get("status") != "running":
        return {"status": "not_running"}
    state["stop"] = True
    state["paused"] = False   # let the gate raise JobStopped
    return {"status": "stopping"}


async def _watch_scrape_log(log_path: str, kind: str) -> None:
    """Tail the scrape log while the job runs: errors pause the job and pop up.

    Fatal lines each get their own dialog; per-file download/transcription
    warnings are merged into one dialog per run.
    """
    job = _scrape_job_kind(kind)
    offset = 0
    while True:
        await asyncio.sleep(0.6)
        try:
            if not os.path.exists(log_path):
                continue
            size = os.path.getsize(log_path)
            if size < offset:
                offset = 0  # log was rewritten
            if size <= offset:
                continue
            with open(log_path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            continue
        cut = chunk.rfind(b"\n")  # keep a half-written last line for next round
        if cut < 0:
            continue
        offset += cut + 1
        text = _decode_log_bytes(chunk[:cut])
        fatals: list[str] = []
        medias: list[str] = []
        for line in text.splitlines():
            line_kind = classify_scrape_log_line(line)
            text_line = line.strip()
            if line_kind == "fatal":
                # A traceback belongs to the error printed just before it.
                if text_line.startswith("Traceback") and fatals:
                    continue
                fatals.append(text_line)
            elif line_kind == "media":
                medias.append(text_line)
        detail = _scrape_log_tail(log_path) if (fatals or medias) else ""
        for line in fatals:
            await raise_job_error(job, line, detail)          # one dialog each
        for line in medias:
            await raise_job_error(job, line, detail, merge=True)  # merged


async def restore_schedule_on_startup():
    """从 panel_config.json 恢复定时任务（容器重启后自动恢复）。"""
    cfg = _load_config()
    cron = cfg.get("schedule", "").strip()
    if not cron:
        return
    parsed = _parse_cron(cron)
    if not parsed:
        print(f"[scheduler] 配置中的 cron 表达式无效: {cron}", flush=True)
        return
    next_run = _next_cron_run(parsed)
    _scheduler_state["enabled"] = True
    _scheduler_state["schedule"] = cron
    _scheduler_state["next_run"] = next_run
    _scheduler_state["task"] = asyncio.create_task(
        _cron_loop(parsed, incremental=True)
    )
    from datetime import datetime
    next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[scheduler] 已恢复定时任务: {cron}, 下次执行: {next_str}", flush=True)


class ScrapeRequest(BaseModel):
    incremental: bool = True
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class VoiceBackfillRequest(BaseModel):
    conversations: list[str] | None = None  # selected nicknames; empty = all DB candidates


class JobAlertDismiss(BaseModel):
    id: int | None = None  # corner-notice id; None closes every notice


class ExportRequest(BaseModel):
    format: str = "jsonl"
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class ScheduleRequest(BaseModel):
    enabled: bool
    cron: str = ""  # cron expression: "0 0 * * *" or shorthand
    #: 面板上勾选出来的时间表（见 backend/panel/scheduler.py 的 simple_rule_to_cron）。
    #: 给了它就以它为准，cron 字段忽略；用它才能让「每天 8 点和 20 点」这种规则
    #: 由后端统一换算，前端只画复选框。
    rule: dict | None = None
    incremental: bool = True
    conversations: list[str] | None = None  # selected nicknames for scheduled scrape


class CustomFilterAction(BaseModel):
    action: str  # "add" | "remove"
    value: str


class CookieImportRequest(BaseModel):
    cookies: str  # JSON array from DevTools or "key=value; key=value" string


class PasswordRequest(BaseModel):
    password: str = ""  # empty = remove password


class LanAccessRequest(BaseModel):
    enabled: bool


class LanDeviceRequest(BaseModel):
    id: str = ""


class LanPasswordRequest(BaseModel):
    password: str = ""  # empty = 取消局域网访问密码


class SelectedUpdate(BaseModel):
    section: str  # "scraper" | "export" | "schedule"
    conversations: list[str]


# ── Appearance (theme / fonts / sizes, shared by panel + viewer) ──

class AppearanceUpdate(BaseModel):
    # 只传改动的部分也接受：后端会和已保存的配置递归合并后再规范化。
    appearance: dict


def _appearance_payload() -> dict:
    return {
        "appearance": _appearance.load_appearance(),
        "defaults": _appearance.default_appearance(),
        "fonts": _appearance.font_options(),
    }


@control_router.get("/api/appearance")
async def get_panel_appearance():
    return _appearance_payload()


@control_router.post("/api/appearance")
async def set_panel_appearance(req: AppearanceUpdate):
    cfg = _load_config()
    merged = _appearance.merge_appearance(_appearance.load_appearance(cfg), req.appearance)
    warnings: list[str] = []
    normalized = _appearance.normalize_appearance(merged, warnings)
    cfg["appearance"] = normalized
    _save_config(cfg)
    return {"status": "ok", "appearance": normalized, "warnings": warnings}


@control_router.post("/api/password")
async def set_password(req: PasswordRequest):
    import hashlib
    cfg = _load_config()
    if req.password:
        cfg["password_hash"] = hashlib.sha256(req.password.encode()).hexdigest()
        _save_config(cfg)
        return {"status": "ok", "message": "密码已设置"}
    else:
        cfg.pop("password_hash", None)
        _save_config(cfg)
        return {"status": "ok", "message": "密码已清除"}


@control_router.get("/api/password/status")
async def password_status():
    cfg = _load_config()
    return {"has_password": bool(cfg.get("password_hash"))}


# ── 局域网访问（开关 / 访问密码 / 信任设备） ──
#
# 「向局域网开放」= 让服务监听 0.0.0.0，这得**重启服务**才生效（uvicorn 的监听地址是
# 启动参数）。所以打开 / 关闭这个开关的接口会先落配置、再交棒给 tools/restart_server.py，
# 和「更新完成后自动重启」走的是同一套机制，面板那边用同一套「等服务回来」的轮询。
#
#: 正在重启的状态（同一个开关连点时不重复安排）
_lan_restart_state = {"running": False, "host": "", "started_at": None}


def _lan_port() -> int:
    """当前服务监听的端口（读自己的命令行；读不出来就用默认的 8000）。"""
    address = _server_cli_address()
    return int(address[1]) if address else _access.DEFAULT_PORT


def _lan_status_payload(request: Request, expect_host: str = "") -> dict:
    """面板「局域网访问」这一块要的全部数据。"""
    cfg = _load_config()
    client_host = request.client.host if request.client else ""
    cookie_id = _access.trusted_device_id(request.cookies.get(_access.COOKIE_NAME))
    address = _server_cli_address()
    host = address[0] if address else _access.LOCAL_HOST
    port = _lan_port()
    expected = _access.normalize_host(expect_host) if expect_host else ""
    return {
        "enabled": _access.lan_enabled(),
        "require_password": _access.password_required(),
        "has_password": bool(cfg.get("lan_password_hash")),
        # 服务**当前**监听在哪儿（切换之后要等重启完成，这里才会跟着变）
        "host": host,
        "port": port,
        "expected_host": expected,
        "restarted": bool(expected) and _access.normalize_host(host) == expected,
        "client_host": client_host,
        "restarting": bool(_lan_restart_state["running"]),
        "addresses": _access.access_addresses(port, client_host),
        "devices": _access.list_devices(cookie_id),
    }


@control_router.get("/api/lan")
async def lan_status(request: Request, expect_host: str = ""):
    return _lan_status_payload(request, expect_host)


@control_router.post("/api/lan")
async def set_lan_access(req: LanAccessRequest, request: Request):
    """打开 / 关闭「向局域网开放」，然后自动重启服务让新的监听地址生效。"""
    _access.set_lan_access(req.enabled)
    target_host = _access.LAN_HOST if req.enabled else _access.LOCAL_HOST
    address = _server_cli_address()
    if address is None:                       # 开发模式（uvicorn --reload）自己会重载
        return {"ok": True, "restart_required": False, "code": "dev_mode",
                "payload": _lan_status_payload(request, target_host)}
    if _access.normalize_host(address[0]) == target_host:
        # 已经在按新的地址监听了（比如切换前刚好重启过）：不用再重启一次
        return {"ok": True, "restart_required": False,
                "payload": _lan_status_payload(request, target_host)}
    if _lan_restart_state["running"]:
        return {"ok": True, "restart_required": True, "already": True,
                "payload": _lan_status_payload(request, target_host)}
    ok, detail = await asyncio.to_thread(_new_code_imports_ok)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": f"新代码没通过启动自检，暂时不重启服务：{detail}"},
            status_code=500,
        )
    _, port = address
    if not await asyncio.to_thread(_spawn_restart_helper, target_host, port):
        return JSONResponse(
            {"ok": False, "error": "自动重启没能开始，请手动重启一次服务（start.ps1 / start.sh）"},
            status_code=500,
        )
    _lan_restart_state.update({"running": True, "host": target_host, "started_at": time.time()})
    print(f"[i] 面板切换了「向局域网开放」（{'开' if req.enabled else '关'}），"
          f"服务将重启为 {target_host}:{port}", flush=True)
    asyncio.create_task(_exit_for_restart())   # 响应发完再退出，面板拿得到正常 200
    return {"ok": True, "restart_required": True, "restart": True}


@control_router.post("/api/lan/password")
async def set_lan_password(req: LanPasswordRequest, request: Request):
    """设置 / 取消局域网访问密码（不用重启：每次请求都会读配置）。"""
    password = req.password or ""
    _access.set_password(password)
    if password:
        message = "访问密码已设置：局域网里的设备第一次访问时要输入它"
    else:
        message = "访问密码已取消：局域网里的设备都能直接访问"
    return {"ok": True, "message": message, "has_password": bool(password),
            "payload": _lan_status_payload(request)}


@control_router.post("/api/lan/devices/forget")
async def forget_lan_device(req: LanDeviceRequest, request: Request):
    """不再信任某台设备：它手上的凭据立刻失效。"""
    removed = _access.forget_device(req.id)
    return {"ok": removed, "removed": removed,
            "payload": _lan_status_payload(request)}


@control_router.post("/api/lan/devices/forget-all")
async def forget_all_lan_devices(request: Request):
    """清空信任设备（换密码、或者想把所有设备重新问一遍时用）。"""
    count = _access.forget_all_devices()
    return {"ok": True, "removed": count,
            "payload": _lan_status_payload(request)}


# ── Notifications (Server酱 / sct.ftqq.com) ──

class NotifyKeyRequest(BaseModel):
    sendkey: str = ""  # empty = remove


# Server酱 notification helpers live in backend/panel/notify.py.
_send_serverchan_sync = _notify.send_serverchan_sync
_build_failure_desp = _notify.build_failure_desp
_notify_on_failure = _notify.notify_on_failure
#: 定时检查更新发现新版本时的通知（和失败通知是同一件事，只是标题不同）
_notify_new_version = _notify.notify


@control_router.post("/api/notify/serverchan")
async def set_notify_key(req: NotifyKeyRequest):
    cfg = _load_config()
    key = req.sendkey.strip()
    if key:
        cfg["notify_serverchan_key"] = key
        _save_config(cfg)
        return {"status": "ok", "message": "SendKey 已保存"}
    cfg.pop("notify_serverchan_key", None)
    _save_config(cfg)
    return {"status": "ok", "message": "SendKey 已清除"}


@control_router.get("/api/notify/serverchan/status")
async def notify_status():
    cfg = _load_config()
    return {"has_key": bool(cfg.get("notify_serverchan_key"))}


@control_router.post("/api/notify/test")
async def notify_test():
    cfg = _load_config()
    sendkey = (cfg.get("notify_serverchan_key") or "").strip()
    if not sendkey:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "未配置 SendKey"},
        )
    ok, msg = await asyncio.to_thread(
        _send_serverchan_sync, sendkey,
        "抖音聊天导出 · 测试通知",
        "如果你收到这条消息，说明 Server酱 配置正常。",
    )
    return {"status": "ok" if ok else "error", "message": msg}


# ── Media download toggle + backfill ──

class DownloadImagesToggle(BaseModel):
    enabled: bool


@control_router.get("/api/config/download-images")
async def get_download_images():
    # 采集时默认下载媒体图片：只有用户显式关掉（配置里明确写了 false）才是不下载。
    return {"enabled": _load_config().get("download_images") is not False}


@control_router.post("/api/config/download-images")
async def set_download_images(req: DownloadImagesToggle):
    cfg = _load_config()
    cfg["download_images"] = bool(req.enabled)
    _save_config(cfg)
    return {"status": "ok", "enabled": cfg["download_images"]}


# ── 资源包（文字式表情的图片） ──
#
# 图片是字节跳动的版权素材，不进仓库（见仓库根目录 NOTICE）：仓库只带一份清单，
# 第一次进面板时问一句要不要下，问过就把 emoji_pack_prompted 记进配置，不再打扰。
# 「关于」页留着手动入口，随时能补下；下次缺的会跳过。四个接口都是薄薄一层，
# 真正的状态机在 backend/panel/emoji_pack.py。

@control_router.get("/api/emoji-pack")
async def emoji_pack_status():
    return _emoji_pack.snapshot()


@control_router.post("/api/emoji-pack/download")
async def emoji_pack_download():
    return await _emoji_pack.start()


@control_router.post("/api/emoji-pack/skip")
async def emoji_pack_skip():
    return _emoji_pack.mark_prompted()


@control_router.post("/api/emoji-pack/stop")
async def emoji_pack_stop():
    return _emoji_pack.stop()


# ── 开发者模式（「关于」页最下面的开关） ──
#
# 一个显示开关：打开后「关于」页才多出「私有仓库凭据」、「设置」页才多出「局域网访问」。
# 面板那边一拨就存（没有「应用」按钮）。它还有一层实际作用：私有仓库的 Token 只在这个
# 开关打开时才被用到（见 github_auth.load_for_use），关掉 = 不再从私有仓库读代码。
#
# 关掉它时如果「向局域网开放」正开着，要顺手把局域网也关掉：不然界面藏了、门还开着。
# 关监听地址要重启服务才生效（和设置页那个开关走同一套助手），而访问密码与信任设备
# 都留着 —— 下次打开开发者模式，那些设备不用重新输密码。

class DeveloperModeToggle(BaseModel):
    enabled: bool


def _developer_mode_flags() -> dict:
    """面板要知道的东西：开关状态、局域网开着吗、存过 Token 吗。

    顺带给出「已保存的 Token 现在真的会被用到吗」—— 开发者模式关着时不会，面板据此
    说明「关掉之后不再从私有仓库读代码」，用户也不用猜 Token 是不是被删了。
    """
    enabled = _cfg.developer_mode_enabled()
    token_set = bool(_github_auth.load())
    return {
        "enabled": enabled,
        "lan_on": _access.lan_enabled(),
        "token_set": token_set,
        "token_from_private_repo": bool(enabled and token_set),
    }


@control_router.get("/api/config/developer-mode")
async def get_developer_mode():
    # 没配过（老配置文件里没有这个字段）就是关的：默认给普通用户看精简的界面。
    return _developer_mode_flags()


@control_router.post("/api/config/developer-mode")
async def set_developer_mode(req: DeveloperModeToggle, request: Request):
    """打开 / 关闭开发者模式。

    关闭时：局域网若正开着，先关掉它（访问密码与信任设备保留），并交棒给重启助手把
    监听地址切回 127.0.0.1；随后再关开发者模式。Token 只是「不再使用」，文件不动。
    """
    if req.enabled:
        cfg = _load_config()
        cfg["developer_mode"] = True
        _save_config(cfg)
        return {**_developer_mode_flags(), "status": "ok", "restarting": False}

    lan_was_on = _access.lan_enabled()
    result: dict = {}
    if lan_was_on:
        # 复用设置页那个开关的整套逻辑（自检、起助手、退出），只是不改访问密码与设备
        result = await set_lan_access(LanAccessRequest(enabled=False), request)
        if isinstance(result, JSONResponse):        # 自检没过 / 助手起不来
            result = {"ok": False, "error": json.loads(result.body).get("error", "")}

    # 局域网那一步已经把配置重写过一次，这里重新读一遍再写自己这一格
    cfg = _load_config()
    cfg["developer_mode"] = False
    _save_config(cfg)

    payload = {**_developer_mode_flags(), "status": "ok",
               "restarting": bool(result.get("restart_required"))}
    if lan_was_on and not result.get("ok", True):
        # 局域网已经关掉（配置里），但没能自动重启：界面要提示手动重启一次
        payload["restart_error"] = result.get("error", "")
    if lan_was_on and result.get("code"):
        # uvicorn --reload（开发模式）：没法自己重启，界面照着设置页那条提示说
        payload["restart_code"] = result["code"]
    return payload


@control_router.get("/api/media/backfill/status")
async def backfill_status():
    return {
        "status": _backfill_state["status"],
        "total": _backfill_state["total"],
        "done": _backfill_state["done"],
        "ok": _backfill_state["ok"],
        "failed": _backfill_state["failed"],
        "message": _backfill_state["message"],
        "started_at": _backfill_state["started_at"],
        "finished_at": _backfill_state["finished_at"],
        "paused": bool(_backfill_state.get("paused")),
    }


@control_router.post("/api/media/backfill")
async def backfill_start():
    if _backfill_state["status"] == "running":
        return JSONResponse({"error": "Backfill already running"}, status_code=409)
    # Mark running synchronously before spawning so two rapid POSTs can't both
    # pass the 409 check (the coroutine sets it too, but that races).
    _backfill_state["status"] = "running"
    asyncio.create_task(_run_backfill())
    return {"status": "started"}


async def _run_backfill():
    """Download all historical image/emoji media that has a URL but no local file.

    - 表情 (msg_type=2): 直接下载 media_url
    - 图片 (msg_type=3): 从 raw_data 取 origin_url + skey，AES-GCM 解密后保存
    """
    _backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "message": "扫描数据库...", "started_at": time.time(), "finished_at": None,
        "paused": False, "stop": False,
    })
    start_job_error_run(JOB_MEDIA_IMAGES)
    failures: list[str] = []
    stopped = False

    async def _note_failure(text: str) -> None:
        """One dialog per run: extra failed files only extend its detail."""
        failures.append(text)
        await raise_job_error(
            JOB_MEDIA_IMAGES,
            f"[-] 图片/表情下载失败: {text}",
            "\n".join(failures[-MAX_ERROR_DETAIL_LINES:]),
            merge=True,
        )

    try:
        # Imports inside try: a failed import (e.g. playwright missing) must set
        # status='failed', not leave it stuck at 'running' (409 on every retry).
        from extractor.web_scraper import _save_emoji, _save_image
        from backend.database import get_db

        img_dir = paths.IMAGES_DIR
        emoji_dir = paths.EMOJI_DIR
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(emoji_dir, exist_ok=True)

        conn = get_db()
        rows = conn.execute(
            "SELECT msg_id, msg_type, media_url, raw_data FROM messages "
            "WHERE msg_type IN (2, 3) "
            "AND (media_local_path IS NULL OR media_local_path = '')"
        ).fetchall()
        _backfill_state["total"] = len(rows)
        _backfill_state["message"] = f"待下载 {len(rows)} 条"

        loop = asyncio.get_event_loop()
        for msg_id, msg_type, url, raw in rows:
            if not await _wait_while_job_paused(_backfill_state):
                stopped = True
                break
            rel = None
            try:
                if msg_type == 2:
                    if url:
                        rel = await loop.run_in_executor(None, _save_emoji, url, emoji_dir)
                elif msg_type == 3:
                    try:
                        data = json.loads(raw) if raw else {}
                        cj = json.loads(data.get("content_json") or "{}")
                    except Exception:
                        cj = {}
                    ru = cj.get("resource_url") or {}
                    skey = ru.get("skey")
                    origin = (ru.get("origin_url_list") or [None])[0]
                    if skey and origin:
                        rel = await loop.run_in_executor(
                            None, _save_image, origin, skey, str(msg_id), img_dir,
                        )
                if rel:
                    conn.execute(
                        "UPDATE messages SET media_local_path = ? WHERE msg_id = ?",
                        (rel, msg_id),
                    )
                    conn.commit()
                    _backfill_state["ok"] += 1
                else:
                    _backfill_state["failed"] += 1
                    await _note_failure(f"{msg_id}: 缺少可用的下载地址")
            except Exception as e:
                _backfill_state["failed"] += 1
                await _note_failure(f"{msg_id}: {e}")
            _backfill_state["done"] += 1

        from extractor.im_media import iter_message_bodies, materialize_bodies
        for (raw,) in conn.execute(
            "SELECT raw_data FROM messages WHERE raw_data LIKE '%aweType%13600%' "
            "OR raw_data LIKE '%forwarded_bodies%'"
        ):
            if not await _wait_while_job_paused(_backfill_state):
                stopped = True
                break
            # 注意别把这个局部变量叫 paths：这个函数上面还要用模块级的 common.paths
            # （img_dir/emoji_dir），一旦这里也叫 paths，上面那几句就变成读局部变量了。
            materialized = await loop.run_in_executor(
                None, lambda r=raw: materialize_bodies(list(iter_message_bodies(r))),
            )
            _backfill_state["ok"] += len(materialized)
            _backfill_state["done"] += len(materialized)

        conn.close()

        if stopped:
            _backfill_state["status"] = "idle"
            _backfill_state["message"] = (
                f"已停止: 成功 {_backfill_state['ok']}，失败 {_backfill_state['failed']}"
            )
        else:
            _backfill_state["status"] = "completed"
            _backfill_state["message"] = f"完成: 成功 {_backfill_state['ok']}，失败 {_backfill_state['failed']}"
    except Exception as e:
        _backfill_state["status"] = "failed"
        _backfill_state["message"] = f"错误: {e}"
        await raise_job_error(
            JOB_MEDIA_IMAGES,
            f"[-] 下载历史图片失败: {e}",
            _backfill_state["message"],
        )
    finally:
        _backfill_state["finished_at"] = time.time()
        _backfill_state["paused"] = False
        _backfill_state["stop"] = False


# ── 视频回填：调 batch_play_info 解析签名 URL 后落地 mp4 ──

@control_router.get("/api/media/videos/status")
async def video_backfill_status():
    return {
        "status": _video_backfill_state["status"],
        "total": _video_backfill_state["total"],
        "done": _video_backfill_state["done"],
        "ok": _video_backfill_state["ok"],
        "failed": _video_backfill_state["failed"],
        "skipped": _video_backfill_state["skipped"],
        "message": _video_backfill_state["message"],
        "started_at": _video_backfill_state["started_at"],
        "finished_at": _video_backfill_state["finished_at"],
        "paused": bool(_video_backfill_state.get("paused")),
    }


@control_router.get("/api/media/videos/pending")
async def video_backfill_pending():
    # Reuse the same Python filter as the backfill itself so the count matches
    # what will actually be processed (excludes text replies that quote a video).
    from extractor.video_downloader import pending_videos
    from backend.database import get_db
    conn = get_db()
    rows = pending_videos(conn)
    conn.close()
    return {"pending": len(rows)}


@control_router.post("/api/media/videos/backfill")
async def video_backfill_start():
    if _video_backfill_state["status"] == "running":
        return JSONResponse({"error": "video backfill already running"}, status_code=409)
    # Mark running synchronously before spawning to avoid the check-then-act race.
    _video_backfill_state["status"] = "running"
    asyncio.create_task(_run_video_backfill())
    return {"status": "started"}


async def _run_video_backfill():
    _video_backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "skipped": 0, "message": "启动浏览器解析视频 URL...",
        "started_at": time.time(), "finished_at": None,
        "paused": False, "stop": False,
    })
    start_job_error_run(JOB_MEDIA_VIDEOS)
    failures = {"count": 0}

    def _cb(p):
        _video_backfill_state["total"] = p.get("total", _video_backfill_state["total"])
        _video_backfill_state["ok"] = p.get("ok", 0)
        fail = p.get("fail", 0)
        _video_backfill_state["failed"] = fail
        _video_backfill_state["skipped"] = p.get("skipped", 0)
        _video_backfill_state["done"] = (
            _video_backfill_state["ok"] + _video_backfill_state["failed"] + _video_backfill_state["skipped"]
        )
        cur = p.get("current", "")
        _video_backfill_state["message"] = (
            f"已下载 {_video_backfill_state['ok']}，失败 {_video_backfill_state['failed']}，"
            f"跳过 {_video_backfill_state['skipped']} / {_video_backfill_state['total']}（{cur[-12:] if cur else ''}）"
        )
        # A new failed video pauses the job and reports it once per run.
        if fail > failures["count"]:
            failures["count"] = fail
            reason = p.get("last_error") or "未知原因"
            raise_job_error_soon(
                JOB_MEDIA_VIDEOS,
                f"[-] 视频下载失败: {reason}",
                _video_backfill_state["message"],
                merge=True,
            )

    async def _gate():
        """Pause between videos while the dialog is open; stop on request."""
        state = _video_backfill_state
        while True:
            if state.get("stop"):
                raise JobStopped()
            if state.get("paused"):
                await asyncio.sleep(0.2)
                continue
            return

    try:
        # Import inside try so a failed import sets status='failed', not stuck 'running'.
        from extractor.video_downloader import JobStopped, backfill as run_backfill
        result = await run_backfill(progress_cb=_cb, gate=_gate)
        if result.get("stopped") or _video_backfill_state.get("stop"):
            _video_backfill_state["status"] = "idle"
            _video_backfill_state["message"] = (
                f"已停止：成功 {result['ok']}，失败 {result['fail']}，跳过 {result['skipped']}"
            )
        else:
            _video_backfill_state["status"] = "completed"
            _video_backfill_state["message"] = (
                f"完成：成功 {result['ok']}，失败 {result['fail']}，跳过 {result['skipped']} / {result['total']}"
            )
            if result.get("total") and not (result.get("ok") or result.get("fail") or result.get("skipped")):
                # Nothing happened at all — usually "not logged in".
                await raise_job_error(
                    JOB_MEDIA_VIDEOS,
                    "[-] 视频下载没有成功任何一条（可能未登录或链接已失效）",
                    _video_backfill_state["message"],
                )
    except Exception as e:
        _video_backfill_state["status"] = "failed"
        _video_backfill_state["message"] = f"错误: {e}"
        await raise_job_error(
            JOB_MEDIA_VIDEOS,
            f"[-] 下载历史视频失败: {e}",
            _video_backfill_state["message"],
        )
    finally:
        _video_backfill_state["finished_at"] = time.time()
        _video_backfill_state["paused"] = False
        _video_backfill_state["stop"] = False


@control_router.get("", response_class=HTMLResponse)
@control_router.get("/", response_class=HTMLResponse)
async def panel_page():
    return HTMLResponse(
        content=PANEL_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@control_router.get("/api/status")
async def panel_status():
    stats = database.get_stats()
    from backend.database import get_db
    conn = get_db()
    row = conn.execute("SELECT MAX(last_message_time) FROM conversations").fetchone()
    last_time = row[0] if row and row[0] else 0
    convs = conn.execute("SELECT name FROM conversations ORDER BY last_message_time DESC").fetchall()
    conn.close()

    cfg = _load_config()

    return {
        "conversations": stats["conversations"],
        "messages": stats["messages"],
        "users": stats["users"],
        "last_message_time": last_time,
        "conversation_names": [c[0] for c in convs if c[0]],
        "custom_filters": cfg.get("custom_filters", []),
        "scrape": {
            "status": _scrape_state["status"],
            "kind": _scrape_state.get("kind", "scrape"),
            "started_at": _scrape_state["started_at"],
            "finished_at": _scrape_state["finished_at"],
            "message": _scrape_state["message"],
            "paused": bool(_scrape_state.get("paused")),
        },
        # Error dialog + bottom-right notices, shared by every long job.
        "job_error": {
            "active": _public_job_error(),
            "alerts": _public_job_alerts(),
        },
        "export": {
            "status": _export_state["status"],
            "file_path": _export_state["file_path"],
            "message": _export_state["message"],
        },
        "scheduler": {
            "enabled": _scheduler_state["enabled"],
            "schedule": _scheduler_state["schedule"],
            # 勾选式时间表：能反解就回一份（面板照着画勾选），反解不了是 None
            # （面板会把高级输入框打开，把表达式原文填进去）。
            "rule": _cron_to_simple_rule(_scheduler_state["schedule"]),
            "next_run": _scheduler_state["next_run"],
        },
    }


# ── About / self-update ──
#
# 版本号由 commit 数推出（第一个 commit = 1.0.0，之后每个 commit +0.0.1，见
# common/version.py）。检测更新时去 GitHub 读远端默认分支的提交列表：
# 远端版本 = version_string(远端 commit 数)，只取「本地版本之后」的那些 commit，
# 逐个标上它对应的版本号，于是跨多个 commit 更新时能逐版本列出更新内容。
GITHUB_API = "https://api.github.com"
# 一次检测最多读几页提交（compare 接口每页 250 条、提交列表接口每页 100 条，
# 两者上限不同），避免在很长的历史上反复请求。
_UPDATE_PAGE_SIZE = 250
_COMMITS_PAGE_SIZE = 100
_UPDATE_MAX_PAGES = 4
_UPDATE_TIMEOUT = 15.0
# 检测结果缓存：点一次「检查更新」不该因为网络慢而每次都等十几秒。
_UPDATE_CACHE_TTL = 30.0
_update_cache: tuple[float, dict] | None = None

# 「检查更新」的实时进度日志：一次检测要连 GitHub（慢的时候十几秒），面板靠它显
# 示「现在走到哪一步」，免得按钮点下去之后看起来毫无反应。日志只在内存里留最近
# 几十行，不进磁盘；面板每次点击带一个自己的 check_id —— 只有 id 对得上才返回内容，
# 于是新一次检测开始时不会把上一次的旧日志错当成这次的进度。
_CHECK_LOG_LIMIT = 40
_check_log: list[dict] = []
_check_id = ""


def _check_log_reset(check_id: str) -> None:
    """开始一次新的检测：清空日志，并记住这次检测的编号。"""
    global _check_id
    _check_id = check_id
    _check_log.clear()


def _check_log_add(text: str) -> None:
    """往进度日志里追一行（带时间戳，面板按本地时间显示）。"""
    _check_log.append({"time": time.time(), "text": text})
    if len(_check_log) > _CHECK_LOG_LIMIT:
        del _check_log[:len(_check_log) - _CHECK_LOG_LIMIT]


class _UpdateCheckError(Exception):
    """检测更新失败（网络不通 / 仓库不存在 / 凭据无效 / API 限流）。"""


class _GitFetchAuthError(Exception):
    """git 明确说「缺凭据 / 认证失败」——比「返回 None」有用，面板能给出准确提示。"""


def _github_token() -> str:
    """检查更新 / 拉代码时**真正拿去用**的 Token（没有、或开发者模式关着时为空串）。

    Token 文件由用户在「关于 → 私有仓库凭据」里保存；开发者模式关掉之后就不再拿它
    读私有仓库（文件留着，重新打开开发者模式就能接着用）。见 github_auth.load_for_use。
    """
    return _github_auth.load_for_use()


def _github_read(url: str, *, quiet_404: bool = False, token: str | None = None):
    """读一个 GitHub API，返回 (JSON 内容, 响应头)。

    带上 Token 才能看到私有仓库（未认证时 GitHub 对私有仓库一律回 404）。
    ``quiet_404`` 时 404 返回 ``(None, None)`` —— 那表示「这个 commit / 分支远端
    没有」，算不算错误交给调用方判断；其余失败都折算成 ``_UpdateCheckError``。
    """
    headers = {
        "User-Agent": "douyin-chat-export-panel",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    auth = _github_token() if token is None else token
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_UPDATE_TIMEOUT) as response:
            head = response.headers
            try:
                body = json.loads(response.read().decode("utf-8"))
            except ValueError as exc:              # 非法 JSON
                raise _UpdateCheckError("bad_response") from exc
            return body, head
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            if quiet_404:
                return None, None
            raise _UpdateCheckError("repo_missing") from exc
        if exc.code == 401:
            # Token 失效/被撤销：这和「仓库不存在」是两回事，要分开提示
            raise _UpdateCheckError("token_invalid") from exc
        if exc.code in (403, 429):
            raise _UpdateCheckError("rate_limited") from exc
        raise _UpdateCheckError(f"http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _UpdateCheckError("network") from exc


def _github_json(url: str, *, quiet_404: bool = False, token: str | None = None):
    """读一个 GitHub API 的内容，404 时按 quiet_404 决定返回 None 还是报错。"""
    return _github_read(url, quiet_404=quiet_404, token=token)[0]


def _last_page(link: str) -> int | None:
    """从 GitHub 分页用的 ``Link`` 响应头里取出最后一页的页号。

    提交列表接口带上 ``per_page=1`` 时，"last" 那一页的页号正好就是 commit 总数，
    所以「远端分支上一共有多少个 commit」只要一个请求就能问出来。
    """
    match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link or "")
    return int(match.group(1)) if match else None


def _local_commit_count() -> int:
    """本地是第几个 commit；读不到 git 历史时用 VERSION 反推。

    VERSION 是静态写在文件里的，但按版本规则它一定能反推出 commit 数，所以
    「从别人那儿整体拷贝过来、没有 .git」的副本也能参与比对。
    """
    count = _version.local_commit_count() or _version.commit_count_for(_version.VERSION)
    return int(count or 0)


def _fetch_ahead_commits(slug: str, branch: str, base: str) -> tuple[int, list[dict]]:
    """远端比 ``base`` 多出来的提交，返回 (总数, 列表)，顺序是「旧 → 新」。

    用 compare 接口而不是提交列表：它直接给出「落后的 commit」，数量与本地
    历史里的 commit 序号能对上，于是每个 commit 都能算出它对应的版本号。

    注意：compare 接口给的就是「旧 → 新」，而提交列表接口恰好相反（最新在前）
    —— 两个接口方向不一致，正是以前版本号和更新内容张冠李戴的原因，别凭印象翻。
    """
    raw: list[dict] = []
    total = 0
    for page in range(1, _UPDATE_MAX_PAGES + 1):
        url = (
            f"{GITHUB_API}/repos/{slug}/compare/{base}...{branch}"
            f"?per_page={_UPDATE_PAGE_SIZE}&page={page}"
        )
        _check_log_add(f"向 GitHub 请求远端提交（第 {page} 页）…")
        payload = _github_json(url)
        if not isinstance(payload, dict):
            raise _UpdateCheckError("bad_response")
        if page == 1:
            try:
                total = int(payload.get("total_commits") or 0)
            except (TypeError, ValueError):
                total = 0
        chunk = payload.get("commits")
        if not isinstance(chunk, list):
            break
        _check_log_add(f"第 {page} 页返回 {len(chunk)} 条提交")
        raw.extend(item for item in chunk if isinstance(item, dict))
        if len(chunk) < _UPDATE_PAGE_SIZE:
            break
    return total, raw


async def _fetch_ahead_commits_via_api() -> tuple[int, list[dict]]:
    """走 GitHub API 拿「落后的 commit」（私有仓库靠 Token 读，且不依赖 tag）。

    首选拿本地 HEAD 那个 commit 当比对基准：compare 接口一次请求就给出落后的全部
    提交，逐条说明也齐全。以前这里用的是 ``vVERSION`` 标签，但标签得有人在远端建
    出来，实际上常常没有，于是「检查更新」永远失败。本地没有 git 记录（从别人那儿
    拷来的副本、没装 git）或这个 commit 远端不认时，退回按 commit 条数比对。
    """
    slug = _version.REPOSITORY_SLUG
    branch = _version.REPOSITORY_BRANCH
    base = await asyncio.to_thread(_git_head)
    if base:
        _check_log_add(f"用 GitHub API 比对本地的 {base[:7]} 与远端 {branch}")
        # 两次 HTTP 都放线程里跑：endpoint 是 async 的，别让最长十几秒的网络等待
        # 把整个事件循环（连同面板轮询）卡住。
        payload = await asyncio.to_thread(
            _github_json,
            f"{GITHUB_API}/repos/{slug}/compare/{base}...{branch}?per_page=1",
            quiet_404=True,
        )
        if payload is not None:
            return await asyncio.to_thread(_fetch_ahead_commits, slug, branch, base)
        _check_log_add("本地这个 commit 远端不认（历史被改写过？），改成按提交条数比对")
    else:
        _check_log_add(
            f"本地没有 git 记录，按提交条数比对本地的 v{_version.VERSION} 与远端 {branch}"
        )
    return await asyncio.to_thread(
        _fetch_ahead_commits_by_count, slug, branch, _local_commit_count()
    )


def _fetch_ahead_commits_by_count(
    slug: str, branch: str, local_count: int
) -> tuple[int, list[dict]]:
    """按 commit 条数比对远端（本地没有 git 记录时的做法）。

    版本号本来就是「这是第几个 commit」，所以「远端总数 - 本地条数」就是落后的
    条数；历史是线性的，落后的那些一定是远端最新的一批。
    """
    total = _remote_commit_total(slug, branch)
    _check_log_add(f"远端 {branch} 一共 {total} 个 commit（本地 {local_count} 个）")
    if total < 1:
        raise _UpdateCheckError("no_remote_commits")
    behind = total - local_count
    if behind <= 0:
        return 0, []
    raw = _fetch_newest_commits(slug, branch, behind)
    # 提交列表接口是「最新在前」，统一翻成「旧 → 新」再往上交：只有方向一致，
    # 每条提交才能按「越新版本号越高」标出来。
    raw.reverse()
    _check_log_add(f"远端比本地多 {behind} 个 commit，取到 {len(raw)} 条说明")
    return behind, raw


def _remote_commit_total(slug: str, branch: str) -> int:
    """远端分支上一共有多少个 commit（不依赖 tag）。"""
    payload, head = _github_read(
        f"{GITHUB_API}/repos/{slug}/commits?sha={branch}&per_page=1"
    )
    last_page = _last_page((head.get("Link") if head is not None else "") or "")
    if last_page:
        return last_page
    return len(payload) if isinstance(payload, list) else 0


def _fetch_newest_commits(slug: str, branch: str, count: int) -> list[dict]:
    """远端最新的 ``count`` 个 commit（GitHub 的顺序：最新在前）。"""
    raw: list[dict] = []
    pages = min((count + _COMMITS_PAGE_SIZE - 1) // _COMMITS_PAGE_SIZE, _UPDATE_MAX_PAGES)
    for page in range(1, pages + 1):
        _check_log_add(f"向 GitHub 请求远端提交（第 {page} 页）…")
        payload = _github_json(
            f"{GITHUB_API}/repos/{slug}/commits"
            f"?sha={branch}&per_page={_COMMITS_PAGE_SIZE}&page={page}"
        )
        if not isinstance(payload, list):
            raise _UpdateCheckError("bad_response")
        chunk = [item for item in payload if isinstance(item, dict)]
        _check_log_add(f"第 {page} 页返回 {len(chunk)} 条提交")
        raw.extend(chunk)
        if len(payload) < _COMMITS_PAGE_SIZE:
            break
    return raw[:count]


async def _remote_reachable() -> bool:
    """先问一下远端仓库本身在不在。

    更新源不可达时（仓库没建、Token 没配好、网络不通）快速失败，省下 git fetch
    那十几秒等待；拿不准时返回 True，交给后面的检测流程自己判断。
    """
    try:
        payload = await asyncio.to_thread(
            _github_json, f"{GITHUB_API}/repos/{_version.REPOSITORY_SLUG}", quiet_404=True,
        )
    except _UpdateCheckError:
        return True                      # 限流/网络错误由后面统一报
    return payload is not None


async def _fetch_ahead_best_source() -> tuple[int, list[dict]]:
    """选一条路拿「落后的 commit」。

    有 Token（且开发者模式开着，见 _github_token）时直接用 API：私有仓库也能读，一条
    请求就拿到版本与逐条提交说明，不必等 git。**没有** Token 时先试本地 git（本机凭据
    可能已经能读那个私有仓库），git 也不行再用不带凭据的 API 兜一次 —— 公开仓库本来就
    允许匿名读，不该因为「没填凭据」就报「需要私有仓库凭据」。
    """
    errors: list[_UpdateCheckError] = []
    if _github_token():
        _check_log_add("检测到可用的 Token，优先走 GitHub API")
        try:
            return await _fetch_ahead_commits_via_api()
        except _UpdateCheckError as exc:
            errors.append(exc)           # Token 可能没有这个仓库的权限，再试 git
            _check_log_add("API 这条路走不通，改试本地 git")
    else:
        _check_log_add("没有可用的 Token，先用本地 git 拉取远端")
    git_needs_auth = False
    try:
        fetched = await asyncio.to_thread(_fetch_ahead_commits_via_git, _version.REPOSITORY_BRANCH)
    except _GitFetchAuthError as exc:
        # git 要登录：这个仓库多半是私有的（公开仓库 git 不会要凭据）
        git_needs_auth = True
        _check_log_add(f"git 说需要凭据：{str(exc)[:120]}")
        fetched = None
    if fetched is not None:
        return fetched
    if errors:
        raise errors[0]                  # 用过 Token 时 API 的说法更准（凭据/权限）
    if git_needs_auth:
        raise _UpdateCheckError("credentials_missing")
    # 没用 Token、git 又拿不到远端（目录里没有 .git、没装 git、网络或代理挡住）：
    # 公开仓库不带凭据也能读 API，最后再试一次，别把「读不到」说成「缺凭据」
    _check_log_add("本地 git 拿不到远端，改用不带凭据的 GitHub API 再试一次")
    return await _fetch_ahead_commits_via_api()


async def _collect_update_uncached() -> dict:
    """检测更新：返回本地版本、远端版本，以及本地缺少的逐个 commit。

    远端版本号 = 本地 commit 数 + 落后的 commit 数，再按版本规则换算。拿到的
    commit 先按旧 → 新排好序，再逐个标上 local_count + 序号 + 1，于是每一条的
    版本号都等于「它进入历史时」的版本号，最后一条就是远端最新版本。
    """
    local_count = _local_commit_count()
    if not local_count:
        raise _UpdateCheckError("bad_local_version")
    _check_log_add(f"本地版本 v{_version.VERSION}（第 {local_count} 个 commit）")

    # 先做一次便宜的「仓库在不在」预检，不可达就别去 fetch 了
    _check_log_add("先确认更新源读得到…")
    if not await _remote_reachable():
        raise _UpdateCheckError(
            "repo_missing" if not _github_token() else "repo_forbidden"
        )
    _check_log_add("更新源读得到，开始比对远端提交")

    total, raw = await _fetch_ahead_best_source()
    _check_log_add(f"比对完成：本地比远端落后 {max(total, len(raw))} 个 commit")

    ordered = await asyncio.to_thread(_order_oldest_first, raw)
    behind = max(total, len(ordered))
    versions = [
        {
            **_commit_entry(item),
            "version": _version.version_string(local_count + index + 1),
            "commit_count": local_count + index + 1,
        }
        for index, item in enumerate(ordered)
    ]
    # 面板按「最新在前」显示
    versions.reverse()
    remote_version = _version.version_string(local_count + behind) if behind else _version.VERSION
    repository = _remote_repository()
    # 「查看更新内容」的链接：用 commit 直接对比（GitHub 会列出落后的那些提交）。
    # 以前是比 vX.Y.Z 标签，但标签需要维护者手动推到远端、经常没有，链接就会指向
    # 一个打不开的页面；连本地 commit 也读不到时退回远端提交列表页，照样看得到更新。
    base_sha = await asyncio.to_thread(_git_head)
    branch = _version.REPOSITORY_BRANCH
    compare_url = (
        f"{repository}/compare/{base_sha}...{branch}"
        if behind and base_sha else f"{repository}/commits/{branch}"
    )
    return {
        "ok": True,
        "current_version": _version.VERSION,
        "remote_version": remote_version,
        "update_available": behind > 0,
        "behind": behind,
        "versions": versions,
        "repository_url": repository,
        "compare_url": compare_url,
        "updatable": _version.is_fork_repository(repository),
    }


async def _collect_update(*, refresh: bool = False) -> dict:
    """带 30 秒缓存的检测：反复点击「检查更新」不用重复等网络。"""
    global _update_cache
    now = time.time()
    if not refresh and _update_cache and now - _update_cache[0] < _UPDATE_CACHE_TTL:
        _check_log_add("命中 30 秒内的检测结果缓存，直接用上次的结果")
        return _update_cache[1]
    result = await _collect_update_uncached()
    _update_cache = (time.time(), result)
    return result


def _invalidate_update_cache() -> None:
    global _update_cache
    _update_cache = None



def _repo_slug(url: str) -> str:
    text = (url or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    text = text.replace("git@github.com:", "").replace("ssh://", "").replace("https://", "").replace("http://", "")
    for host in ("github.com/", "www.github.com/"):
        if host in text:
            text = text.split(host, 1)[1]
            break
    return text.strip("/")


def _remote_repository() -> str:
    """优先读 git 的 origin（用户可能换了 fork），读不到时用内置地址。"""
    if not os.path.isdir(os.path.join(_version.REPO_ROOT, ".git")):
        return _version.REPOSITORY_URL
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=_version.REPO_ROOT, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return _version.REPOSITORY_URL
    url = (out.stdout or "").strip()
    if out.returncode != 0 or not url:
        return _version.REPOSITORY_URL
    return url[:-4] if url.endswith(".git") else url


def _remote_branch() -> str:
    return _version.REPOSITORY_BRANCH


def _iso_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    return parsed.astimezone(timezone.utc).date().isoformat()


def _commit_entry(raw: dict) -> dict:
    commit = raw.get("commit") if isinstance(raw.get("commit"), dict) else {}
    message = str(commit.get("message") or "").splitlines()
    subject = message[0].strip() if message else ""
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    date = _iso_date(author.get("date") or "")
    sha = str(raw.get("sha") or "")
    return {"sha": sha[:7], "sha_full": sha, "subject": subject, "date": date}


def _local_version_payload() -> dict:
    count = _version.local_commit_count()
    return {
        "version": _version.VERSION,
        # 版本号理论上总能反推出 commit 数；反推失败说明文件被手改坏了。
        "commit_count": _version.commit_count_for(_version.VERSION),
        "git_commit_count": count,
        "repository_url": _remote_repository(),
        "fork_url": _version.REPOSITORY_URL,
        "upstream_url": _version.UPSTREAM_REPOSITORY_URL,
        "author": _version.AUTHOR,
        "upstream_author": _version.UPSTREAM_AUTHOR,
        "license": _version.LICENSE_NAME,
    }


def _update_error_message(code: str) -> str:
    """错误码对应的中文提示（接口和进度日志共用一份，免得两处说法不一致）。"""
    messages = {
        "repo_missing": "更新源暂时读不到：仓库可能还没公开，或私有仓库需要先在「关于」页填写只读 Token",
        "repo_forbidden": "读不到仓库：Token 可能没有这个仓库的权限，或仓库地址不对",
        "token_invalid": "GitHub Token 无效或已过期，请在「关于」页重新填写",
        "credentials_missing": "需要 GitHub 凭据才能读取私有仓库，请在「关于」页填写只读 Token",
        "rate_limited": "GitHub 接口访问次数用完了，请稍后再试",
        "network": "连不上 GitHub，请检查网络或代理",
        "bad_response": "GitHub 返回了无法解析的内容",
        "no_remote_commits": "更新源里还没有提交记录",
        "bad_local_version": "读不到本地版本号，请检查 common/version.py 里的 VERSION",
    }
    if code.startswith("http_"):
        return f"GitHub 接口返回 {code[5:]}"
    return messages.get(code, "检测更新失败")


def _raise_update_error(code: str, detail: str = ""):
    return JSONResponse(
        {"ok": False, "error_code": code, "error": _update_error_message(code), "detail": detail},
        status_code=502,
    )


def _update_error_code(exc: Exception) -> str:
    if isinstance(exc, _UpdateCheckError):
        return str(exc)
    if isinstance(exc, _GitFetchAuthError):
        return "credentials_missing"
    return "unknown"


def _update_error_response(exc: Exception) -> JSONResponse:
    """把检测过程中的异常统一变成面板能读的提示。"""
    code = _update_error_code(exc)
    detail = "" if isinstance(exc, _UpdateCheckError) else str(exc)
    return _raise_update_error(code, detail)


# ── 私有仓库的 GitHub 凭据（「关于」页里的只读 Token） ──
#
# Token 存在 config/github_token，接口只回「有没有、是谁」，从不回 Token 本身。
class _GithubTokenRequest(BaseModel):
    token: str = ""


def _github_account(token: str) -> str:
    """Token 对应的登录名；拿不到时返回空串。"""
    try:
        data = _github_json(f"{GITHUB_API}/user", token=token)
    except _UpdateCheckError:
        return ""
    return str(data.get("login") or "") if isinstance(data, dict) else ""


def _credential_scope(token: str) -> tuple[str, str]:
    """检查 Token 能用吗，返回 (状态, 登录名)。

    状态：``ok`` / ``invalid``（401，Token 无效或过期）/ ``forbidden``（读不到
    这个仓库，通常是 Token 没勾选该仓库）/ ``unknown``（限流、网络等）。
    """
    login = _github_account(token)
    try:
        payload = _github_json(
            f"{GITHUB_API}/repos/{_version.REPOSITORY_SLUG}", token=token, quiet_404=True,
        )
    except _UpdateCheckError as exc:
        code = str(exc)
        if code == "token_invalid":
            return "invalid", login
        if code == "repo_missing":
            return "forbidden", login
        return "unknown", login
    if payload is None:
        return "forbidden", login
    return "ok", login


@control_router.get("/api/update/token")
async def update_token_status():
    """Token 状态：只告诉面板「有没有、是谁」，不回 Token 本身。

    「有没有」看的是文件（存过就是存过），但**只有开发者模式开着才去问 GitHub
    「是谁」** —— 关着时那一步会拿 Token 发一次请求，而那时它已经不该被用到了。
    """
    token = _github_auth.load()
    try:                             # 跨盘符时 relpath 会抛 ValueError，退回绝对路径
        location = os.path.relpath(_github_auth.TOKEN_PATH, _version.REPO_ROOT)
    except ValueError:
        location = _github_auth.TOKEN_PATH
    usable = _github_token()
    return {
        "configured": bool(token),
        "login": (_github_account(usable) if usable else ""),
        "path": location,
    }


@control_router.post("/api/update/token")
async def update_token_save(req: _GithubTokenRequest):
    token = _github_auth.normalize(req.token)
    if not token:
        return JSONResponse({"error": "请先粘贴 Token"}, status_code=400)
    if not _github_auth.is_plausible(token):
        return JSONResponse({"error": "Token 格式不像 GitHub Token，请检查是否粘贴完整"}, status_code=400)

    status, login = await asyncio.to_thread(_credential_scope, token)
    if status != "ok":
        # 存不下就别存：存了反而让「检查更新」一直报权限错误
        if status == "invalid":
            hint = "Token 无效或已过期，请重新生成一个"
        elif status == "forbidden":
            hint = (f"Token 属于 {login}，但没有 {_version.REPOSITORY_SLUG} 的读取权限"
                    if login else f"这个 Token 没有 {_version.REPOSITORY_SLUG} 的读取权限")
        else:
            hint = "验证时连不上 GitHub，请稍后重试"
        return JSONResponse({"error": hint, "status": status}, status_code=400)

    _github_auth.save(token)
    # 换了凭据，之前缓存的检测结果可能已经不成立
    _invalidate_update_cache()
    return {"saved": True, "login": login, "login_unknown": not login}


@control_router.delete("/api/update/token")
async def update_token_clear():
    cleared = _github_auth.clear()
    _invalidate_update_cache()
    return {"cleared": cleared}


@control_router.get("/api/update/info")
async def update_info():
    """「关于」页的静态信息：版本号、仓库地址、作者。"""
    payload = _local_version_payload()
    token = _github_auth.load()
    usable = _github_token()
    payload.update({
        "repository_git_url": _version.REPOSITORY_GIT_URL,
        "token": {
            "configured": bool(token),
            "login": (_github_account(usable) if usable else ""),
        },
        "update": {
            "status": _update_state["status"],
            "message": _update_state["message"],
            "started_at": _update_state["started_at"],
            "finished_at": _update_state["finished_at"],
        },
    })
    return payload


@control_router.get("/api/env/check")
async def env_check():
    """「关于」页的环境检测按钮：检查 Python / Node / ffmpeg 等运行条件。

    与 start.html 用的是同一套检查项和返回结构（那边由 tools/env_check.ps1 提供，
    因为新电脑上 Python 装没装正是被检测的对象）。这个接口每次调用都重新检测，
    不缓存，方便"处理完再点一次"。
    """
    from backend import env_check as _env_check

    return await asyncio.to_thread(_env_check.collect)


#: 私有仓库用的凭据助手：Token 走环境变量，命令行里只有脚本路径
_CREDENTIAL_HELPER_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "git_credential.py",
)


def _git_env(*, with_token: bool = False) -> dict | None:
    """git 子进程的环境；不需要特殊设置时返回 None（继承父进程环境）。

    with_token 且有可用的 Token（开发者模式开着）、远端又是 HTTPS 时，注入一个只在
    本进程内生效的凭据助手（系统凭据助手仍然在后面兜底）。没有 Token 时 git 照旧用
    系统凭据 —— 本机本来就能 `git pull` 的机器不受影响。
    """
    env: dict | None = None
    if with_token:
        token = _github_token()
        remote = str(_remote_repository())
        if token and remote.startswith(("http://", "https://")) and os.path.exists(_CREDENTIAL_HELPER_SCRIPT):
            helper = f"!\"{sys.executable}\" \"{_CREDENTIAL_HELPER_SCRIPT}\""
            env = {
                **os.environ,
                "DSH_GIT_TOKEN": token,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": helper,
            }
    if env is not None:
        # 带 Token 时禁止任何交互式提示：凭据不对就直接失败，别挂在那里等人输入
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
    return env


def _git_run(
    args: list[str],
    *,
    timeout: float = 60.0,
    prompt_off: bool = False,
    with_token: bool = False,
) -> subprocess.CompletedProcess | None:
    """在仓库根目录跑一条 git 命令；git 不可用/超时返回 None。

    固定按 UTF-8 解码：提交说明基本都是中文，Windows 上默认的 GBK 会直接抛
    UnicodeDecodeError（读到半个多字节序列），把更新检测变成 500。
    prompt_off 时禁止 git 弹认证提示（否则要凭据的远端会把命令挂到超时）。
    with_token 时带上已保存的 Token（私有仓库的 fetch 要用）。
    """
    if not os.path.isdir(os.path.join(_version.REPO_ROOT, ".git")):
        return None
    env = _git_env(with_token=with_token)
    if prompt_off:
        # 关提示的这两个变量要**叠加**在已有环境上，绝不能另起一个只有它俩的环境：
        # ``env=`` 是整个替换子进程的环境，缺了 SystemRoot 的 git 在 Windows 上连
        # 域名都解析不了（报 "Could not resolve host"），代理变量、PATH 也一起丢。
        env = {**(env if env is not None else os.environ),
               "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    try:
        return subprocess.run(
            ["git", *args], cwd=_version.REPO_ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _is_ancestor(old: str, new: str) -> bool | None:
    """``old`` 是不是 ``new`` 的祖先（也就是更旧）；问不出来返回 None。

    以前这里问的是「本地存不存在这个 commit」，但 git fetch 之后落后的那几条也
    都躺在本地对象库里，答案往往两边都是「有」，先后还是分不出来。直接问祖先
    关系才真的能定序；有一个 sha 本地不认识（浅克隆、历史被改写过）时 git 报错，
    那种情况返回 None，交给调用方走兜底。
    """
    if not old or not new:
        return None
    result = _git_run(["merge-base", "--is-ancestor", old, new], timeout=15)
    if result is None:
        return None
    if result.returncode == 0:
        return True                      # 是祖先
    if result.returncode == 1:
        return False                     # 两者都在本地，但互不为祖先
    return None                          # 128 等：git 不认识这两个 sha


def _git_head() -> str | None:
    result = _git_run(["rev-parse", "HEAD"], timeout=15)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _fetch_ahead_commits_via_git(branch: str) -> tuple[int, list[dict]] | None:
    """用本地 git 问远端：先 fetch，再列 HEAD..origin/<branch>。

    比走 GitHub API 稳：私有仓库、限流、tag 缺失都不影响，而且返回的 sha /
    日期 / 标题一应俱全。远端连不上时返回 None，交给 API 兜底。

    fetch 加了两道刹车：速度低于 1KB/s 持续 5 秒就中止，整体最多等 20 秒 ——
    更新源不可达时（例如仓库还没公开）不至于让面板转上半分钟。
    """
    head = _git_head()
    if not head:
        return None
    _check_log_add(f"git fetch origin {branch}（最多等 20 秒）…")
    fetch = _git_run(
        ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=5",
         "fetch", "--quiet", "origin", branch],
        timeout=20, prompt_off=True, with_token=True,
    )
    if fetch is None:
        return None                      # git 不可用或超时：交给调用方兜底
    if fetch.returncode != 0:
        stderr = (fetch.stderr or "").lower()
        if any(marker in stderr for marker in (
            "could not read username",        # 没凭据
            "authentication failed",
            "invalid username or password",
            "403", "401",
            "terminal prompts disabled",
        )):
            raise _GitFetchAuthError((fetch.stderr or "").strip()[-200:])
        return None                      # 其他原因（远端不存在等）走 API 兜底
    result = _git_run(["rev-list", "--count", f"HEAD..origin/{branch}"], timeout=30)
    if result is None or result.returncode != 0:
        return None
    try:
        total = int(result.stdout.strip())
    except ValueError:
        return None

    raw: list[dict] = []
    if total:
        _check_log_add(f"远端比本地多 {total} 个 commit，正在读取它们的说明")
        # 与 GitHub 的 commit 对象保持同样的形状，上游解析逻辑只有一份
        result = _git_run([
            "log", "--no-color", "--date=iso-strict",
            "--pretty=format:%H%x1f%aI%x1f%P%x1f%s%x1e",
            f"HEAD..origin/{branch}",
        ], timeout=60)
        if result is None or result.returncode != 0:
            return None
        for record in result.stdout.split("\x1e"):
            record = record.strip()
            if not record:
                continue
            sha, _, rest = record.partition("\x1f")
            date, _, rest = rest.partition("\x1f")
            parents, _, subject = rest.partition("\x1f")
            raw.append({
                "sha": sha,
                "parents": [{"sha": item} for item in parents.split()],
                "commit": {"message": subject, "author": {"date": date}},
            })
        # git log 与提交列表接口一样是「最新在前」，翻成「旧 → 新」（理由同上）
        raw.reverse()
    return total, raw


def _parents_of(item: dict) -> list[str]:
    """这一条提交的父提交 sha（GitHub 的响应里有，本地 git 那条路也补上了）。"""
    parents = item.get("parents")
    if not isinstance(parents, list):
        return []
    shas: list[str] = []
    for parent in parents:
        sha = parent.get("sha") if isinstance(parent, dict) else parent
        if isinstance(sha, str) and sha:
            shas.append(sha)
    return shas


def _order_by_parents(raw: list[dict]) -> list[dict] | None:
    """按父提交关系定序；看不出方向时返回 None。

    落后的这些提交在历史上是连着的：最新那条的父提交也在列表里，最旧那条的父提交
    在列表外（它就是本地已有的那个 commit）。于是「谁的父提交在列表里」就等于
    「谁更靠后」，不必猜接口给的是哪个方向。
    """
    known = {str(item.get("sha") or "") for item in raw}
    known.discard("")
    if not known:
        return None
    first_newer = any(sha in known for sha in _parents_of(raw[0]))
    last_newer = any(sha in known for sha in _parents_of(raw[-1]))
    if first_newer == last_newer:
        return None                      # 只有一端能证明自己更靠后时才算数
    return list(reversed(raw)) if first_newer else list(raw)


def _order_by_ancestry(raw: list[dict]) -> list[dict] | None:
    """按本地 git 的祖先关系定序；问不出来返回 None（老数据里没有 parents 时用）。"""
    first = str(raw[0].get("sha") or "")
    last = str(raw[-1].get("sha") or "")
    if _is_ancestor(first, last) is True:
        return list(raw)                 # 第一条是最后一条的祖先 → 已经是「旧 → 新」
    if _is_ancestor(last, first) is True:
        return list(reversed(raw))
    return None


def _order_oldest_first(raw: list[dict]) -> list[dict]:
    """把 commit 列表排成「旧 → 新」。

    方向不能凭印象猜：GitHub 的提交列表接口是「最新在前」，而 compare 接口是
    「旧在前」（文档没写清，实测如此）。以前一律按「最新在前」反过来，于是 compare
    那条路上每条提交都拿到了隔壁那条的版本号 —— 面板上就表现成「v1.2.1」旁边写着
    v1.2.0 的更新内容，跨多个 commit 的更新里整体错位。

    现在按证据定序：先看父提交关系（两个 API 都给、本地 git 那条路也补了），再问
    本地 git 的祖先关系；两样都问不出来时才按调用方的约定，认为交过来的已经是
    「旧 → 新」（三个来源都各自归一过了）。
    """
    if len(raw) < 2:
        return list(raw)
    for decide in (_order_by_parents, _order_by_ancestry):
        ordered = decide(raw)
        if ordered is not None:
            return ordered
    return list(raw)


@control_router.get("/api/update/check")
async def update_check(refresh: bool = False, check_id: str = ""):
    """去远端仓库看有没有新版本，并列出本地缺少的每个 commit。

    ``check_id`` 是面板自己生成的这次检测的编号（同一个编号也用来读进度日志）：
    检测一开始就把日志清空并换成这个编号，面板于是不会读到上一次的旧进度。
    """
    _check_log_reset(check_id)
    _check_log_add("开始检查更新…")
    try:
        result = await _collect_update(refresh=refresh)
    except Exception as exc:                       # 兜底，别让面板 500
        _check_log_add("检查失败：" + _update_error_message(_update_error_code(exc)))
        return _update_error_response(exc)
    _check_log_add("检查完成")
    return result


@control_router.get("/api/update/check/log")
async def update_check_log(check_id: str = ""):
    """「检查更新」的实时进度：给面板的日志框用，只回内存里最近几十行。

    ``check_id`` 对不上（这次检测还没轮到后端处理）时回 ``pending``，面板照旧等，
    不会把上一次的日志错当成这次的进度。
    """
    if check_id and check_id != _check_id:
        return {"pending": True, "lines": []}
    return {"pending": False, "lines": list(_check_log)}


# ── 定时自动检查更新 ──────────────────────────────────────────────────────
#
# 到点后只做一件事：跑一次和「检查更新」按钮完全相同的检测（_collect_update），把
# 结果记在状态里给面板显示；发现有新版本、且用户配了 Server酱 时顺手推一条通知。
# 真正的升级仍然要用户自己在面板上点「立即更新」—— 自动换代码风险太大，不做。

def _update_schedule_payload() -> dict:
    """定时检查更新的当前状态（面板的表单与提示行都读它）。"""
    schedule = _normalize_update_schedule(_update_schedule_state)
    return {
        "schedule": {field: _update_schedule_state[field] for field in _UPDATE_SCHEDULE_FIELDS},
        # 面板的勾选框要按「收拾干净之后」的规则来画，别把它自己填的值再读回来
        "rule": schedule["rule"],
        "text": _simple_rule_text(schedule["rule"]),
        "next_run": _update_schedule_state.get("next_run"),
        "last_run": _update_schedule_state.get("last_run"),
        "last_result": _update_schedule_state.get("last_result"),
        "running": bool(_update_schedule_state.get("running")),
    }


def _update_schedule_text(schedule: dict | None = None) -> str:
    """把时间表说成一句人话：「每天 09:00、20:00」「每周一、周四 21:30」（给日志用）。"""
    if schedule is None:
        schedule = _update_schedule_state
    return _simple_rule_text(_normalize_update_schedule(schedule)["rule"])


def _update_versions_text(result: dict) -> str:
    """通知正文里的逐版本说明：同一版本号的多个 commit 合成一行（最新在前）。"""
    groups: list[tuple[str, list[str]]] = []
    for item in result.get("versions") or []:
        version = str(item.get("version") or "")
        subject = str(item.get("subject") or "")
        if groups and groups[-1][0] == version:
            groups[-1][1].append(subject)
        else:
            groups.append((version, [subject]))
    return "\n".join(
        f"- v{version}：{' ／ '.join(text for text in subjects if text)}"
        for version, subjects in groups
    )


async def _notify_update_available(result: dict) -> None:
    """有新版本时推一条通知；没配 SendKey 时 notify 自己会静默返回。"""
    remote = str(result.get("remote_version") or "")
    lines = [
        f"**当前版本**：v{result.get('current_version') or _version.VERSION}",
        f"**远端版本**：v{remote}（落后 {int(result.get('behind') or 0)} 个 commit）",
    ]
    versions = _update_versions_text(result)
    if versions:
        lines += ["", versions]
    lines += ["", "在控制面板「关于」页点「立即更新」即可升级。"]
    await _notify_new_version(f"抖音聊天导出 · 发现新版本 v{remote}", "\n".join(lines))


async def _run_update_check_once() -> dict:
    """跑一次自动检查：结果记进状态，有新版就发通知（开着自动更新时再自己装）。

    出错也不抛出去（网络不通、Token 失效都只是「这次没查到」）：循环死在一次网络
    抖动上，用户就再也不知道定时检查去哪了。
    """
    _update_schedule_state["running"] = True
    try:
        try:
            result = await _collect_update(refresh=True)
        except Exception as exc:
            code = _update_error_code(exc)
            result = {"ok": False, "error_code": code, "error": _update_error_message(code)}
    finally:
        _update_schedule_state["running"] = False
    _update_schedule_state["last_run"] = time.time()
    auto: dict | None = None
    if result.get("ok") and result.get("update_available"):
        auto = await _handle_new_version(result)
    _update_schedule_state["last_result"] = {
        "ok": bool(result.get("ok")),
        "update_available": bool(result.get("update_available")),
        "current_version": result.get("current_version") or _version.VERSION,
        "remote_version": result.get("remote_version") or "",
        "behind": int(result.get("behind") or 0),
        "error": result.get("error") or "",
        "auto_update": auto,
    }
    return result


# ── 自动更新：发现新版本就自己装（默认关，开着也要先过安全阀） ──────────────
#
# 「只检查」永远是安全的，「改代码 + 装依赖 + 重启服务」不是。所以这条路上不放行
# 任何需要人来拍板的情况：有任务在跑、本地有没提交的改动、连 git 记录都读不到时，
# 一律只发一条通知、什么都不动，等下次检查或用户自己点「立即更新」。

#: 自动更新不该打断的任务：重启会把它们正在做的事掐断，所以要等它们跑完。
_AUTO_UPDATE_BUSY_JOBS = (
    (JOB_SCRAPE, "采集"),
    (JOB_VOICE, "语音转写补充"),
    (JOB_MEDIA_IMAGES, "下载历史图片"),
    (JOB_MEDIA_VIDEOS, "下载历史视频"),
    (JOB_REFRESH, "刷新会话列表"),
)

#: 自动更新出问题时的提示里统一带上这几份日志在哪儿（用户照着一看就知道卡在哪一步）。
_UPDATE_LOG_HINT = (
    "相关日志：config/logs/update.log（更新过程）、config/logs/restart.log（重启过程）、"
    "config/logs/server.log（服务输出），面板「日志」页也能直接看。"
)


def _busy_job_labels() -> list[str]:
    """正在跑、不该被打断的任务名（自动更新遇到它们就跳过这次）。"""
    labels = [name for job, name in _AUTO_UPDATE_BUSY_JOBS if _job_is_running(job)]
    if _export_state.get("status") == "running":
        labels.append("导出")
    return labels


def _worktree_dirty_paths() -> list[str] | None:
    """工作区里的已跟踪文件有没有未提交的真实改动；判断不了时返回 None。

    判断方式和 ``tools/update.py`` 保持一致（``--ignore-cr-at-eol`` 滤掉 Windows 上
    纯换行的噪音），免得这边说「有改动」、脚本那边却认为可以更新。没有 git 记录
    （整体拷贝过来的副本）或本机没装 git 时无从判断，返回 None。

   注意 None 与 ``[]`` 在调用方那里的含义不同：``[]`` 是「问过了，干净」，None 是
    「问不出来」。**问不出来不拦自动更新**（第二台只用来跑、没装 git 的电脑要能自动
    更新），只是提示里要告诉用户：那种情况下脚本用「下载代码包覆盖」换代码，本地改动
    不会被保留。
    """
    if not os.path.exists(os.path.join(_version.REPO_ROOT, ".git")):
        return None
    if shutil.which("git") is None:
        return None
    paths: set[str] = set()
    for args in (["diff", "--ignore-cr-at-eol", "--name-only"],
                 ["diff", "--cached", "--name-only"]):
        result = _git_run(args, timeout=30)
        if result is None or result.returncode != 0:
            return None
        paths.update(line.strip() for line in (result.stdout or "").splitlines() if line.strip())
    return sorted(paths)


async def _auto_update_gate() -> dict:
    """自动更新前的闸门检查。

    返回 ``{"blocked": (原因代码, 补充说明) | None, "dirty_checked": bool}``：
    ``blocked`` 不为 None 表示这次不该自动装（只发通知）；``dirty_checked`` 为 False
    表示读不到 git 记录、本地改动无从检查 —— **这种情况不拦**，只是提示里要说清楚
    接下来会发生什么（代码文件会被直接覆盖）。
    """
    if _update_state.get("status") in ("running", "restarting"):
        return {"blocked": ("updating", ""), "dirty_checked": True}
    busy = _busy_job_labels()
    if busy:
        return {"blocked": ("busy", "、".join(busy)), "dirty_checked": True}
    if not _version.is_fork_repository(_remote_repository()):
        return {"blocked": ("foreign", ""), "dirty_checked": True}
    dirty = await asyncio.to_thread(_worktree_dirty_paths)
    if dirty:
        return {"blocked": ("dirty", "、".join(dirty[:5])), "dirty_checked": True}
    return {"blocked": None, "dirty_checked": dirty is not None}


def _auto_update_skip_text(code: str, detail: str = "") -> str:
    """「这次没自动更新，因为…」说成一句人话（面板提示与微信通知共用）。"""
    if code == "updating":
        return "已经有一次更新在进行中，这次不重复启动。"
    if code == "busy":
        return f"有任务正在跑（{detail or '未知任务'}），等它跑完再说，这次跳过。"
    if code == "dirty":
        return "本地有还没提交的代码改动，自动更新会停下来问你，所以这次跳过。"
    if code == "foreign":
        return "当前仓库地址不是本项目的更新源，没有自动更新。"
    return "这次没有自动更新。"


async def _notify_auto_update_skipped(result: dict, code: str, detail: str) -> None:
    """跳过自动更新时推一条通知，说清楚「发现了新版本」和「这次为什么没装」。"""
    remote = str(result.get("remote_version") or "")
    await _notify_new_version(
        f"抖音聊天导出 · 发现新版本 v{remote}（这次没自动更新）",
        "\n".join([
            f"**当前版本**：v{result.get('current_version') or _version.VERSION}",
            f"**远端版本**：v{remote}（落后 {int(result.get('behind') or 0)} 个 commit）",
            "",
            f"**这次没自动更新**：{_auto_update_skip_text(code, detail)}",
            "",
            "想现在就升级：在控制面板「关于」页点「立即更新」。",
        ]),
    )


async def _handle_new_version(result: dict) -> dict | None:
    """定时检查发现新版本之后做什么：开着自动更新就试着装，否则只发通知。

    返回「这次自动更新做了什么」（面板显示成一行字）；开关关着时返回 None。
    """
    if not _update_schedule_state.get("auto_update"):
        await _notify_update_available(result)
        return None
    gate = await _auto_update_gate()
    if gate["blocked"] is not None:
        code, detail = gate["blocked"]
        _check_log_add("自动更新跳过：" + _auto_update_skip_text(code, detail))
        await _notify_auto_update_skipped(result, code, detail)
        return {"action": "skipped", "code": code, "detail": detail}
    started = _start_update_task(result, allow_dirty=False, want_restart=True, notify=True)
    _check_log_add(f"自动更新已开始：v{started['from_version']} → v{started['to_version']}")
    lines = [
        f"**当前版本**：v{started['from_version']}",
        f"**目标版本**：v{started['to_version']}（落后 {int(result.get('behind') or 0)} 个 commit）",
        "",
        "正在后台拉代码、装依赖、构建前端，"
        + ("完成后会自动重启服务。" if started["auto_restart"] else "完成后需要手动重启服务。"),
        "过程中的日志：config/logs/update.log；出问题时面板也会弹提示。",
    ]
    if not gate["dirty_checked"]:
        # 本机没装 git（或目录没有 git 记录）：脚本会用「下载代码包覆盖」换代码，
        # 拦不住本地改动，那就至少先说清楚，别让用户以为改动还在。
        lines += [
            "",
            "**注意**：这个目录读不到 git 记录（副本目录，或本机没装 git），"
            "没法检查本地有没有改过代码；这次会用「下载代码包覆盖」的方式换代码，"
            "仓库里的代码文件会直接变成新版本，本地的手工修改不会保留。",
        ]
    await _notify_new_version(
        f"抖音聊天导出 · 开始自动更新到 v{started['to_version']}",
        "\n".join(lines),
    )
    return {"action": "started"}


async def _report_auto_update_problem(message: str, detail: str = "") -> None:
    """自动更新没能顺利跑完时的统一收尾：面板弹一条 + 微信推一条（都带日志位置）。

    走的是和其他任务报错同一套队列，所以不管用户当时在看哪一页都会看到，两分钟没人
    理会就收进右下角常驻，直到用户自己点掉。
    """
    await raise_job_error(JOB_UPDATE, message, "\n\n".join(
        part for part in ((detail or "").strip(), _UPDATE_LOG_HINT) if part
    ))
    reason = message + (f"；{detail.strip()}" if detail.strip() else "")
    await _notify_new_version(
        "抖音聊天导出 · 自动更新没能完成",
        "\n\n".join([_build_failure_desp(reason, UPDATE_LOG_PATH), _UPDATE_LOG_HINT]),
    )


async def _update_schedule_loop() -> None:
    """后台循环：睡到下一次该检查的时刻 → 检查一次 → 再算下一次。

    「下一次」按整份时间表算，所以同一天里的第二个、第三个时间点都会照常轮到
    （一次检查只占几秒，不会把后面的时间点顶掉）。
    """
    try:
        while True:
            next_run = _next_update_run(_update_schedule_state["rule"])
            _update_schedule_state["next_run"] = next_run
            if next_run is None:              # 设置被关掉了
                return
            await asyncio.sleep(max(0.0, next_run - time.time()))
            await _run_update_check_once()
            # 刚检查完的这个时间点不再触发（时钟精度、系统时间被调回去等情况）
            await asyncio.sleep(_UPDATE_SCHEDULE_SETTLE_SECONDS)
    except asyncio.CancelledError:
        pass


def _schedule_update_task() -> None:
    """按当前设置（重新）安排后台任务；设置关闭时只是取消掉旧任务。"""
    task = _update_schedule_state.get("task")
    if task is not None and not task.done():
        task.cancel()
    _update_schedule_state["task"] = None
    _update_schedule_state["next_run"] = _next_update_run(_update_schedule_state["rule"])
    if not _update_schedule_state["enabled"]:
        _update_schedule_state["next_run"] = None
        return
    _update_schedule_state["task"] = asyncio.create_task(_update_schedule_loop())


async def restore_update_schedule_on_startup() -> None:
    """从 panel_config.json 恢复「定时检查更新」（服务重启后接着生效）。"""
    schedule = _normalize_update_schedule(_load_config().get("update_schedule"))
    _update_schedule_state["enabled"] = schedule["enabled"]
    _update_schedule_state["rule"] = schedule["rule"]
    _update_schedule_state["auto_update"] = schedule["auto_update"]
    _schedule_update_task()
    if not _update_schedule_state["enabled"]:
        return
    next_run = _update_schedule_state.get("next_run")
    when = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S") if next_run else "?"
    print(
        f"[scheduler] 已恢复定时检查更新: {_update_schedule_text()}, 下次检查: {when}",
        flush=True,
    )


class UpdateScheduleRequest(BaseModel):
    enabled: bool = False
    #: 勾选式时间表（和「定时采集」同一套）：每天 / 每周几 / 每月哪几天 + 若干时间点。
    rule: dict | None = None
    auto_update: bool = False  # 发现新版本时自动下载安装（默认关）


@control_router.get("/api/update/schedule")
async def get_update_schedule():
    """定时检查更新的设置与最近一次结果（面板打开「关于」页时读一次）。"""
    return _update_schedule_payload()


@control_router.post("/api/update/schedule")
async def set_update_schedule(req: UpdateScheduleRequest):
    """保存定时检查更新的设置，并立刻按新设置重排后台任务。

    时间表的形状由面板决定（勾选框 → 规则），这里只做校验：说不清哪里不对的规则
    直接退回 400，免得把一个「永远不触发」的设置存进配置文件。
    """
    problem = _simple_rule_problem(req.rule)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    rule = _normalize_update_schedule({"rule": req.rule})["rule"]

    schedule = {
        "enabled": bool(req.enabled),
        "rule": rule,
        "auto_update": bool(req.auto_update),
    }
    cfg = _load_config()
    cfg["update_schedule"] = schedule
    _save_config(cfg)

    _update_schedule_state["enabled"] = schedule["enabled"]
    _update_schedule_state["rule"] = rule
    _update_schedule_state["auto_update"] = schedule["auto_update"]
    _schedule_update_task()
    payload = _update_schedule_payload()
    payload["status"] = "enabled" if schedule["enabled"] else "disabled"
    return payload


# ── 更新完成后自动重启服务 ─────────────────────────────────────────────────
#
# 「换完代码要手动重启」这一步也交给程序自己做：更新成功后，先让一个脱离父进程的
# 助手脚本等在旁边（tools/restart_server.py），确认新代码能 import 得起，再给自己
# 发 Ctrl+C 让 uvicorn 优雅退出；助手等到端口空出来就把服务重新拉起来。
# 实在退不掉就硬退（新服务已经在等端口了），不会把用户卡在「服务半死」的状态。
#
#: 助手脚本（重启流程全在它里面，见 tools/restart_server.py）
_RESTART_SCRIPT = os.path.join(_version.REPO_ROOT, "tools", "restart_server.py")
#: 重启过程的日志：助手自己的输出（只有它在写这个文件）
RESTART_LOG_PATH = paths.RESTART_LOG
#: 后端服务自己的输出（启动脚本把它重定向到这里，自动重启起来的新服务也写这里）
SERVER_LOG_PATH = paths.SERVER_LOG


def _display_path(path: str) -> str:
    """给面板显示的项目内相对路径（统一成正斜杠，Windows 上也显示成 config/logs/server.log）。"""
    try:
        return os.path.relpath(path, paths.REPO_ROOT).replace("\\", "/")
    except ValueError:                    # 跨盘符时 relpath 会抛，那就原样给
        return str(path).replace("\\", "/")


#: 重启前用新代码做一次 import 自检的超时（秒）
_IMPORT_CHECK_TIMEOUT = 90.0
#: 留给面板轮询到「正在重启」的时间，之后才真的退出
_RESTART_EXIT_DELAY = 2.5
#: 优雅退出没成功时的兜底：再等这么久就硬退（新服务已经在等端口）
_RESTART_FORCE_EXIT_DELAY = 8.0


# ── 「更新完成」这件事要跨进程传下去 ──────────────────────────────────────
#
# 更新完弹的那个「更新完成」提示，是**重启后的新服务**弹的：旧进程退出前把这次更新
# 的范围和内容写成一个文件，新进程启动时读一次就删掉，然后走和报错一样的队列 ——
# 弹窗倒计时 2 分钟（PAUSE_AUTO_RESUME_SECONDS），时间到就收进右下角常驻，直到用户
# 自己点「×」才消失。
#: 更新完成记录（读完即删，所以只会弹一次）
UPDATE_DONE_PATH = paths.UPDATE_DONE_PATH


def _update_done_payload(to_version: str) -> dict:
    """这次更新的范围与内容（写进记录文件，重启后新服务照着它弹提示）。"""
    return {
        "from_version": _update_state.get("from_version") or _version.VERSION,
        "to_version": to_version,
        "at": time.time(),
        # 逐版本变更（检查更新时拿到的，最新在前）
        "versions": list(_update_state.get("versions") or []),
    }


def _write_update_done_record(payload: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(UPDATE_DONE_PATH), exist_ok=True)
        with open(UPDATE_DONE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except (OSError, TypeError, ValueError) as exc:
        # 记不下来只是「重启后少弹一个提示」，绝不该影响更新与重启本身
        print(f"[!] 写不下更新完成记录（重启后不会弹提示）: {exc}", flush=True)
        return False
    return True


def _remove_update_done_record() -> None:
    try:
        os.remove(UPDATE_DONE_PATH)
    except OSError:
        pass


def _update_done_detail(payload: dict) -> str:
    """提示里的「更新内容」：跨了哪几个版本、每个版本改了什么。"""
    from_version = str(payload.get("from_version") or "")
    to_version = str(payload.get("to_version") or "")
    lines: list[str] = []
    if from_version and to_version:
        lines.append(f"更新范围：v{from_version} → v{to_version}")
    versions = _update_versions_text({"versions": payload.get("versions") or []})
    if versions:
        lines += ["", versions]
    if len(lines) <= 1:
        lines.append("这次更新没有单独的说明。")
    return "\n".join(lines).strip()


async def restore_update_done_notice_on_startup() -> None:
    """重启起来之后，把「更新完成」弹给用户看一次。

    记录是上一个进程留下的：**读到就删**，所以只弹一次，之后再怎么重启都不会重复弹；
    文件坏了也照样删掉，不能让一条坏记录卡住启动。提示的倒计时和右下角常驻都由现成的
    那套队列负责（``PAUSE_AUTO_RESUME_SECONDS``）。
    """
    if not os.path.exists(UPDATE_DONE_PATH):
        return
    try:
        with open(UPDATE_DONE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[!] 读不下更新完成记录（跳过这次提示）: {exc}", flush=True)
        _remove_update_done_record()
        return
    _remove_update_done_record()
    if not isinstance(payload, dict):
        return
    to_version = str(payload.get("to_version") or _version.VERSION)
    await raise_job_error(
        JOB_UPDATE_DONE,
        f"当前版本 v{to_version}",
        _update_done_detail(payload),
    )
    print(
        f"[i] 已弹出「更新完成」提示"
        f"（v{payload.get('from_version') or '?'} → v{to_version}）",
        flush=True,
    )


def _to_port(text, fallback: int) -> int:
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return fallback
    return value if 0 < value < 65536 else fallback


def _server_cli_address() -> tuple[str, int] | None:
    """从自己的命令行里读出服务监听在哪儿（uvicorn 的 ``--host`` / ``--port``）。

    读不出来就用 127.0.0.1:8000 兜底（start.ps1 / start.sh 里的默认值）。命令行里
    带 ``--reload`` 时返回 None：那是开发模式，我们一换代码 uvicorn 自己就会重载，
    不需要也不该由我们来重启。
    """
    args = list(sys.argv[1:])
    if any(item == "--reload" or item.startswith("--reload=") for item in args):
        return None
    host, port = "127.0.0.1", 8000
    for index, item in enumerate(args):
        if item == "--host" and index + 1 < len(args):
            host = args[index + 1]
        elif item.startswith("--host="):
            host = item.split("=", 1)[1]
        elif item == "--port" and index + 1 < len(args):
            port = _to_port(args[index + 1], port)
        elif item.startswith("--port="):
            port = _to_port(item.split("=", 1)[1], port)
    return host or "127.0.0.1", port


def _new_code_imports_ok() -> tuple[bool, str]:
    """更新完之后，先用磁盘上的新代码试着 import 一次后端。

    连 import 都过不了就别重启：那种代码一启动就崩，重启等于把「现在还好好跑着的
    旧服务」换成「起不来的服务」。宁可停在旧版本上，让用户还能打开面板看日志。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import backend.main"],
            cwd=_version.REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_IMPORT_CHECK_TIMEOUT,
            env=_utf8_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    lines = [line for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
    return False, " / ".join(lines[-6:])


def _detached_popen_flags() -> dict:
    """脱离父进程起子进程用的参数：这个后端马上就会退出，助手必须能自己活着。"""
    if os.name == "nt":
        # DETACHED_PROCESS：完全摆脱控制台；CREATE_NEW_PROCESS_GROUP：单独一个进程组
        return {"creationflags": 0x00000008 | 0x00000200}
    return {"start_new_session": True}


def _spawn_restart_helper(host: str, port: int, *, build_frontend: bool = False) -> bool:
    """启动「等旧服务退出 →（可选）重建前端 → 重新拉起服务」的助手脚本。成功返回 True。

    ``build_frontend=True`` 是面板「日志 → 重启服务」用的：服务停下来之后先跑一遍
    ``npm run build``，再把服务起回来（见 tools/restart_server.py）。
    """
    if not os.path.exists(_RESTART_SCRIPT):
        print(f"[!] 找不到自动重启脚本：{_RESTART_SCRIPT}", flush=True)
        return False
    command = [
        sys.executable, "-u", _RESTART_SCRIPT,
        "--wait-pid", str(os.getpid()),
        "--host", str(host),
        "--port", str(port),
        "--log", SERVER_LOG_PATH,
    ]
    if build_frontend:
        command.append("--build-frontend")
    try:
        os.makedirs(os.path.dirname(RESTART_LOG_PATH), exist_ok=True)
        handle = open(RESTART_LOG_PATH, "ab", buffering=0)
    except OSError as exc:
        print(f"[!] 打不开重启日志 {RESTART_LOG_PATH}: {exc}", flush=True)
        return False
    try:
        subprocess.Popen(
            command, cwd=_version.REPO_ROOT, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
            # 输出统一按 UTF-8：Windows 上重定向到文件时默认走 GBK，中文日志会乱码，
            # 助手打一个 GBK 里没有的字符还会直接抛 UnicodeEncodeError。
            env=_utf8_subprocess_env(),
            **_detached_popen_flags(),
        )
    except OSError as exc:
        print(f"[!] 启动自动重启脚本失败: {exc}", flush=True)
        return False
    finally:
        handle.close()
    print(f"[i] 已启动自动重启助手（等本进程退出后重新拉起 {host}:{port}）", flush=True)
    return True


def _request_process_exit() -> None:
    """让当前进程按「Ctrl+C」的方式退出（uvicorn 会自己收尾、把端口还回去）。

    自动重启和面板上的「停止程序」都用这一条：两边要做的事一模一样，区别只在
    退出之后有没有助手把服务重新拉起来。
    """
    try:
        signal.raise_signal(signal.SIGINT)
    except Exception as exc:                      # 平台少见，兜底走硬退
        print(f"[!] 通知进程退出失败（{exc}），稍后强制退出", flush=True)


def _force_exit() -> None:
    """不走常规清理直接退出：新服务正等着这个端口。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(0)


async def _exit_for_restart() -> None:
    """把控制权交给助手：发 Ctrl+C 让 uvicorn 优雅退出，退不掉就硬退。"""
    await asyncio.sleep(_RESTART_EXIT_DELAY)
    print("[i] 正在自动重启服务（新进程起来后本进程退出）", flush=True)
    _request_process_exit()
    await asyncio.sleep(_RESTART_FORCE_EXIT_DELAY)
    print("[!] 优雅退出超时，强制退出（新服务已经在等端口）", flush=True)
    _force_exit()


async def _finish_update_after_success(to_version: str, restart: dict | None,
                                      *, notify: bool = False) -> None:
    """更新成功后的收尾：能自动重启就交棒给助手，然后本进程准备退出。

    ``notify`` 是「这次是定时自动更新」：每一步没能继续下去时都多推一条微信通知，
    并且把日志在哪儿说清楚 —— 无人值守时用户看不到面板，只能靠通知知道出了什么事。
    """
    if not restart:
        _update_state["status"] = "completed"
        _update_state["message"] = f"已更新到 {to_version}，重启服务后生效"
        if notify:
            await _report_auto_update_problem(
                f"已更新到 {to_version}，但没能自动重启（现在跑的仍是旧版本）",
                "请重新运行 start.ps1 / start.sh 让新版本生效。",
            )
        return
    _update_state["message"] = f"代码已更新到 {to_version}，正在确认新代码能不能启动…"
    ok, detail = await asyncio.to_thread(_new_code_imports_ok)
    if not ok:
        _update_state["status"] = "failed"
        _update_state["message"] = (
            f"已更新到 {to_version}，但新代码没通过启动自检，"
            f"为避免服务起不来暂不重启（现在跑的还是旧版本）：{detail}"
        )
        if notify:
            await _report_auto_update_problem(
                f"已更新到 {to_version}，但新代码没通过启动自检，没有自动重启", detail,
            )
        return
    if not await asyncio.to_thread(_spawn_restart_helper, restart["host"], restart["port"]):
        _remove_update_done_record()          # 没重启起来就别留提示，免得下次重启误会
        _update_state["status"] = "completed"
        _update_state["message"] = f"已更新到 {to_version}，但自动重启没能开始，请手动重启服务"
        if notify:
            await _report_auto_update_problem(
                f"已更新到 {to_version}，但自动重启没能开始",
                "请重新运行 start.ps1 / start.sh 让新版本生效。",
            )
        return
    # 交棒成功：把「更新完成」记在磁盘上，重启后的新服务会读到它并弹给用户看
    await asyncio.to_thread(_write_update_done_record, _update_done_payload(to_version))
    _update_state["status"] = "restarting"
    _update_state["message"] = f"已更新到 {to_version}，正在自动重启服务"
    _update_state["restart_started_at"] = time.time()
    if notify:
        # 先通知再退出：通知要在线程里发 HTTP，退出任务一旦跑起来就可能把它带走
        await _notify_new_version(
            f"抖音聊天导出 · 已更新到 v{to_version}",
            "\n".join([
                f"**已更新到**：v{to_version}",
                "",
                "服务正在自动重启，重启完成后控制面板会自动弹出这次的更新内容。",
                "如果几分钟后面板还是打不开：看一眼 config/logs/restart.log 与 config/logs/server.log，"
                "或重新运行 start.ps1 / start.sh。",
            ]),
        )
    asyncio.create_task(_exit_for_restart())


class _UpdateRunRequest(BaseModel):
    allow_dirty: bool = False
    auto_restart: bool = True     # 更新完成后自动重启服务（默认开）


async def _run_update(from_version: str, to_version: str, *, allow_dirty: bool = False,
                      restart: dict | None = None, notify: bool = False) -> None:
    """跑 tools/update.py：拉代码 → 装依赖 → 构建前端，成功后自动重启服务。

    ``notify=True`` 表示这次是定时自动更新的：失败、被本地改动拦下、重启没起来时
    都额外推一条微信通知（面板里的提示走同一套文案）。
    """
    label = _job_label("update")
    os.makedirs(os.path.dirname(UPDATE_LOG_PATH), exist_ok=True)
    cmd = [sys.executable, "-u", os.path.join("tools", "update.py")]
    if allow_dirty:
        cmd.append("--allow-dirty")
    try:
        with open(UPDATE_LOG_PATH, "w", encoding="utf-8", newline="") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_version.REPO_ROOT,
                env=_utf8_subprocess_env(),
            )
            _update_state["process"] = proc
            await proc.wait()

        if proc.returncode == 0:
            await _finish_update_after_success(to_version, restart, notify=notify)
        elif proc.returncode == UPDATE_EXIT_DIRTY:
            # 工作区有改动，没动任何文件：面板据此提示「仍然更新」
            _update_state["status"] = "dirty"
            _update_state["message"] = "工作区有未提交的改动，已停止更新"
            if notify:
                # 检查那一刻还好好的、这会儿被人改了：自动更新不替用户做「强行覆盖」的决定
                await _report_auto_update_problem(
                    "自动更新已停止：本地有未提交的代码改动",
                    "请手动提交或备份这些改动，然后在「关于」页点「立即更新」。",
                )
        else:
            _update_state["status"] = "failed"
            _update_state["message"] = f"{label}失败 (exit code {proc.returncode})"
            if notify:
                await _report_auto_update_problem(
                    f"自动更新失败（{label}返回 {proc.returncode}）",
                    "更新日志的最后几行已经附在下面，可以据此判断是网络、依赖还是构建出的错。",
                )
    except Exception as exc:
        _update_state["status"] = "failed"
        _update_state["message"] = f"{label}错误: {exc}"
        if notify:
            await _report_auto_update_problem(f"自动更新出错：{exc}")
    finally:
        _update_state["finished_at"] = time.time()
        _update_state["process"] = None


def _start_update_task(check: dict, *, allow_dirty: bool, want_restart: bool,
                       notify: bool) -> dict:
    """把「开始更新」落成状态 + 后台任务。

    面板上的「立即更新」按钮和定时自动更新共用这一段，两边的行为因此完全一致
    （同一份脚本、同一套自动重启、同一份日志），差别只在要不要发微信通知。
    """
    to_version = check["remote_version"]
    from_version = _version.VERSION
    # 自动重启：默认开。开发模式（uvicorn --reload）下 uvicorn 自己会重载，跳过。
    restart: dict | None = None
    if want_restart:
        address = _server_cli_address()
        if address is None:
            print("[i] 检测到 --reload（开发模式），uvicorn 会自己重载，不自动重启", flush=True)
        else:
            restart = {"host": address[0], "port": address[1]}
    _update_state.update({
        "status": "running",
        "message": f"正在更新 {from_version} → {to_version}",
        "started_at": time.time(),
        "finished_at": None,
        "from_version": from_version,
        "to_version": to_version,
        "requested_at": time.time(),
        "allow_dirty": allow_dirty,
        "auto_restart": bool(restart),
        "auto_started": bool(notify),      # 面板据此说明「这次是自动开始的更新」
        "restart_started_at": None,
        # 逐版本变更（重启后弹「更新完成」提示时要用它显示「更新内容」）
        "versions": list(check.get("versions") or []),
    })
    asyncio.create_task(
        _run_update(from_version, to_version, allow_dirty=allow_dirty,
                    restart=restart, notify=notify)
    )
    return {
        "status": "started",
        "from_version": from_version,
        "to_version": to_version,
        "allow_dirty": allow_dirty,
        "auto_restart": bool(restart),
    }


@control_router.post("/api/update/run")
async def update_run(req: _UpdateRunRequest | None = None):
    if _update_state["status"] == "running":
        return JSONResponse({"error": "更新已在进行中"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "有采集任务在运行，请先停止再更新"}, status_code=409)

    repository = _remote_repository()
    if not _version.is_fork_repository(repository):
        return JSONResponse({
            "error": f"当前仓库地址（{repository}）不是本项目的更新源，请手动更新",
        }, status_code=400)

    try:
        check = await _collect_update(refresh=True)
    except Exception as exc:
        return _update_error_response(exc)

    if not check["update_available"]:
        return {"status": "up-to-date", "message": f"已是最新版本 {_version.VERSION}"}

    # 工作区有改动时先不拦：让 tools/update.py 判断，它会打印哪些文件被改过并
    # 返回一个专门的退出码，面板再问用户要不要「仍然更新」。
    allow_dirty = bool(req and req.allow_dirty)
    want_restart = bool(req.auto_restart) if req else True
    return _start_update_task(check, allow_dirty=allow_dirty,
                              want_restart=want_restart, notify=False)


@control_router.get("/api/update/status")
async def update_status():
    return {
        "status": _update_state["status"],
        "message": _update_state["message"],
        "started_at": _update_state["started_at"],
        "finished_at": _update_state["finished_at"],
        "from_version": _update_state["from_version"],
        "to_version": _update_state["to_version"],
        "allow_dirty": bool(_update_state.get("allow_dirty")),
        "auto_restart": bool(_update_state.get("auto_restart")),
        # 这次更新是不是定时自动开始的（面板据此把说明写成「自动更新」而不是「你点的」）
        "auto_started": bool(_update_state.get("auto_started")),
        # 定时检查更新的状态跟着这个轮询一起刷新：面板停在「关于」页时，自动检查
        # 跑完能马上显示出来，不必再开一个接口。
        "schedule": _update_schedule_payload(),
    }


@control_router.get("/api/update/log")
async def update_log(lines: int = 80):
    if not os.path.exists(UPDATE_LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(UPDATE_LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.post("/api/update/stop")
async def update_stop():
    proc = _update_state.get("process")
    if proc is not None and getattr(proc, "returncode", None) is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        _update_state["status"] = "idle"
        _update_state["message"] = "更新已取消"
    return {"status": _update_state["status"]}


@control_router.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    # Selected conversations (checkbox list) take precedence over free-text filter
    effective_filter = ",".join(req.conversations) if req.conversations else req.filter

    cmd = [sys.executable, "-u", "extract.py"]
    if req.incremental:
        cmd.append("--incremental")
    if effective_filter:
        cmd.extend(["--filter", effective_filter])
    # 媒体图片默认下载；面板里关掉时显式告诉抓取端别下载。
    if _load_config().get("download_images") is False:
        cmd.append("--no-download-images")
    else:
        cmd.append("--download-images")

    _scrape_state["status"] = "running"
    _scrape_state["kind"] = "scrape"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = f"{'增量' if req.incremental else '全量'}采集"
    if req.conversations:
        _scrape_state["message"] += f" ({len(req.conversations)} 个会话)"
    elif req.filter:
        _scrape_state["message"] += f" (过滤: {req.filter})"

    # Persist selection so it's remembered next time
    if req.conversations is not None:
        cfg = _load_config()
        cfg["scraper_selected"] = list(req.conversations)
        _save_config(cfg)

    asyncio.create_task(_run_scrape(cmd))
    return {"status": "started", "message": _scrape_state["message"]}


@control_router.post("/api/voice-transcriptions/backfill")
async def start_voice_backfill(req: VoiceBackfillRequest | None = None):
    """Start a local-DB voice backfill without fetching chat history again."""
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    conversations = list((req.conversations if req else None) or [])
    cmd = [sys.executable, "-u", "extract.py", "--transcribe-voices"]
    if conversations:
        cmd.extend(["--filter", ",".join(conversations)])

    _scrape_state["status"] = "running"
    _scrape_state["kind"] = "voice_backfill"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = "补充历史语音转写"
    if conversations:
        _scrape_state["message"] += f" ({len(conversations)} 个会话)"
    asyncio.create_task(
        _run_scrape(cmd, log_path=VOICE_LOG_PATH, job_kind="voice_backfill")
    )
    return {"status": "started", "message": _scrape_state["message"]}


async def _run_scrape(cmd, *, log_path=None, job_kind="scrape"):
    # Reset here (not in start_scrape) so BOTH the manual and cron paths clear a
    # prior manual-stop flag; otherwise a scheduled scrape after a manual Stop
    # would be mislabeled '已停止' and its failure notification suppressed.
    job = _scrape_job_kind(job_kind)
    _scrape_state["stopped"] = False
    await _resume_job(job)          # in case a stale dialog left it frozen
    start_job_error_run(job)        # per-file failures merge per run
    watcher = None
    try:
        log_path = log_path or LOG_PATH
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8", newline="") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
                env=_utf8_subprocess_env(),
            )
            _scrape_state["process"] = proc
            # Watch the log while the job runs: an error line pauses the job and
            # pops the panel dialog instead of letting it run past the problem.
            watcher = asyncio.create_task(_watch_scrape_log(log_path, job_kind))
            await proc.wait()

        if _scrape_state.get("stopped"):
            # User-initiated stop: SIGTERM makes returncode nonzero, but this is
            # not a failure — don't report failed or push a WeChat notification.
            _scrape_state["status"] = "idle"
            _scrape_state["message"] = "已停止"
        elif proc.returncode == 0:
            _scrape_state["status"] = "completed"
            _scrape_state["message"] = (
                "语音转写补充完成" if job_kind == "voice_backfill" else "采集完成"
            )
        else:
            _scrape_state["status"] = "failed"
            label = "语音转写补充" if job_kind == "voice_backfill" else "采集"
            _scrape_state["message"] = f"{label}失败 (exit code {proc.returncode})"
    except Exception as e:
        _scrape_state["status"] = "failed"
        label = "语音转写补充" if job_kind == "voice_backfill" else "采集"
        _scrape_state["message"] = f"{label}错误: {e}"
    finally:
        if watcher is not None:
            watcher.cancel()
        _scrape_state["finished_at"] = time.time()
        _scrape_state["process"] = None
        _scrape_state["paused"] = False
        _scrape_state["paused_at"] = None
        if _scrape_state["status"] == "failed" and not _scrape_state.get("stopped"):
            label = "语音转写补充" if job_kind == "voice_backfill" else "采集"
            # Show the panel dialog (and keep a corner notice) for a dead job too:
            # nothing is left to pause, the dialog just reports and the countdown
            # closes it after the usual two minutes.
            await raise_job_error(
                job,
                _scrape_state["message"],
                _scrape_log_tail(log_path) or _scrape_state["message"],
            )
            asyncio.create_task(_notify_on_failure(
                f"抖音聊天导出 · {label}失败",
                _build_failure_desp(_scrape_state["message"], log_path),
            ))


@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 50):
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.get("/api/voice-transcriptions/log")
async def voice_transcription_log(lines: int = 80):
    if not os.path.exists(VOICE_LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(VOICE_LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.get("/api/conversations/refresh/log")
async def discover_log(lines: int = 80):
    if not os.path.exists(DISCOVER_LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(DISCOVER_LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


#: 「日志」页一次最多取多少行：这两份文件都会一直往下长，整份塞给页面又慢又卡
LOG_VIEW_MAX_LINES = 2000


def _log_file(name: str) -> str | None:
    """「日志」页上的名字 → 日志文件路径；不认识的名字返回 None。

    刻意在**调用时**读这两个常量（而不是启动时算好一个字典）：测试要把路径指到临时
    文件，写死的字典改不动。名字走白名单、不让面板把路径传上来 —— 面板可能被远程
    打开，绝不能让它读这台机器上的任意文件。
    """
    return {"server": SERVER_LOG_PATH, "restart": RESTART_LOG_PATH}.get(str(name))


@control_router.get("/api/logs/{name}")
async def logs_read(name: str, lines: int = 300):
    """「日志」页：一份日志文件的最后若干行。

    目前有两份，用名字选：

    * ``server``  —— 后端服务自己的输出（``config/logs/server.log``）。不管服务是怎么起来的
      （双击「启动服务（双击）.bat」在后台起、``start.ps1`` 起，还是自动更新后由
      tools/restart_server.py 重新拉起），输出都写在这一份里；
    * ``restart`` —— 重启过程本身的记录（``config/logs/restart.log``）：什么时候等旧服务
      退出、端口多久空出来、新服务起没起来，都写在这份里。

    文件不存在是**正常情况**（还没触发过自动重启、或者日志被清理掉了），所以照样
    返回 200，只把 ``exists`` 标成 false：面板据此显示「还没有这份日志」，不必把
    一个正常的空状态当成错误处理。行数在这里统一夹到上限，免得别人手动请求一个
    很大的 ``lines`` 把服务拖住。
    """
    path = _log_file(name)
    if path is None:
        return JSONResponse({"error": f"未知的日志：{name}"}, status_code=404)
    wanted = max(1, min(lines, LOG_VIEW_MAX_LINES))
    payload = {
        "name": name,
        "log": "",
        "exists": False,
        "path": _display_path(path),
        "size": 0,
        "modified_at": None,
    }
    try:
        stat = os.stat(path)
    except OSError:
        return payload
    payload["exists"] = True
    payload["size"] = stat.st_size
    payload["modified_at"] = stat.st_mtime
    try:
        all_lines = _read_utf8_or_gbk(path).splitlines(keepends=True)
    except Exception as exc:                       # 读不了就当空文件，别把接口打挂
        print(f"[!] 读日志 {path} 失败：{exc}", flush=True)
        return payload
    tail = all_lines[-wanted:] if len(all_lines) > wanted else all_lines
    payload["log"] = "".join(tail)
    return payload


def _reveal_command(target: str | None, folder: str, *, platform: str = "") -> list[str]:
    """用系统的文件管理器打开「放日志的文件夹」要跑的命令。

    认出是哪份日志（``target``）而且它真的存在时，尽量连文件一起选中，让人一眼看到
    要看的是哪个文件。

    ``platform`` 是给测试用的：留空就按当前系统判断（windows / macos / linux）。
    """
    system = platform or ("windows" if os.name == "nt"
                          else "macos" if sys.platform == "darwin" else "linux")
    if system == "windows":
        # explorer 的 /select 要写成「/select,路径」这**一个**参数；它成功时也会返回
        # 退出码 1，所以调用处不看退出码。
        if target:
            return ["explorer", "/select," + os.path.normpath(target)]
        return ["explorer", os.path.normpath(folder)]
    if system == "macos":
        return ["open", "-R", target] if target else ["open", folder]
    return ["xdg-open", folder]


@control_router.post("/api/logs/open-folder")
async def logs_open_folder(name: str = ""):
    """在文件管理器里打开日志文件夹（认出是哪份日志就顺手选中它）。

    面板可能被远程打开，所以这里**只**打开放日志的 config/logs 目录、名字只认白名单：
    绝不接受调用方给的路径。命令发出去就算成功（发不出去才报错）—— Windows 上
    explorer 即使成功也返回退出码 1，看退出码会把好事当坏事。
    """
    folder = paths.LOG_DIR
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    path = _log_file(name)
    target = path if path and os.path.exists(path) else None
    command = _reveal_command(target, folder)
    try:
        subprocess.Popen(command, close_fds=True)
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "command": " ".join(command)},
            status_code=500,
        )
    print(f"[i] 已在文件管理器里打开日志目录（{' '.join(command)}）", flush=True)
    return {"ok": True, "folder": _display_path(folder), "command": " ".join(command)}


# ── 面板「停止程序」──────────────────────────────────────────────────────
#
# 用「启动服务（双击）.bat」启动的后端是**没有窗口**的：以前要停它，只能去任务管理器
# 里找 python.exe。所以「日志」页上给一个按钮，点了就让服务自己退出、把 8000 端口
# 让出来（释放端口这件事只有进程自己退干净才算数，所以按钮的落点是「让本进程退出」）。
#
# 做法刻意和自动重启一样：**先把响应回完，过一会儿再给自己发 Ctrl+C**。要是在收到
# 请求的当下就退，面板那边只看到一个「请求失败」，根本不知道是自己点停的。
#
# 开发模式（uvicorn --reload）不接这个按钮：那个模式下真正跑服务的是 uvicorn 拉起来的
# 子进程，把它停掉父进程会立刻再拉起来一个，看起来就像「点了没用」——不如直接说清楚。

#: 回完面板的响应之后、真的退出之前留给面板的时间（秒）
_STOP_EXIT_DELAY = 1.0
#: 优雅退出没成功时的兜底（秒）：再等这么久就硬退（进程没了端口自然就释放了）
_STOP_FORCE_EXIT_DELAY = 8.0

#: 是否已经安排了退出：连点几下不能安排一堆退出任务
_server_stop_state = {"running": False}


async def _exit_for_stop() -> None:
    """面板点了「停止程序」：先按 Ctrl+C 优雅退出，退不掉再硬退。"""
    await asyncio.sleep(_STOP_EXIT_DELAY)
    print("[i] 面板点了「停止程序」，后端服务退出中（端口马上释放）", flush=True)
    _request_process_exit()
    await asyncio.sleep(_STOP_FORCE_EXIT_DELAY)
    print("[!] 优雅退出超时，强制结束进程（端口随进程一起释放）", flush=True)
    _force_exit()


@control_router.post("/api/server/stop")
async def stop_server():
    """面板「日志」页上的「停止程序」：关掉后端自己，把端口让出来。

    响应里带上端口，面板好写一句「8000 端口正在释放」；真正的退出安排在响应之后
    （见 ``_exit_for_stop``），所以调用方总能拿到一个正常的 200，而不是「请求失败」。

    开发模式返回 409 + ``code: dev_mode``：面板按这个 code 换成当前语言的说明，
    不让用户对着一个英文/中文的报错猜发生了什么。
    """
    address = _server_cli_address()
    if address is None:                     # 命令行里有 --reload：开发模式，停了也白停
        return JSONResponse(
            {
                "ok": False,
                "code": "dev_mode",
                "error": "开发模式（uvicorn --reload）下不能从面板停止服务，"
                         "请在运行它的窗口里按 Ctrl+C。",
            },
            status_code=409,
        )
    host, port = address
    if _server_stop_state.get("running"):   # 连点了：别再安排一个退出任务
        return {"ok": True, "already": True, "port": port}
    _server_stop_state["running"] = True
    print(f"[i] 面板点了「停止程序」：准备退出（{host}:{port} 即将释放）", flush=True)
    asyncio.create_task(_exit_for_stop())
    return {"ok": True, "port": port}


# ── 面板「重启服务」──────────────────────────────────────────────────────
#
# 「停止程序」之后要自己再双击 bat 才能回来；改了前端还得手动 cd frontend && npm run
# build。这个按钮把三件事串成一条（用户点一下就等着页面自己刷新回来）：
#
#     后端退出（释放端口） → 重建前端（npm run build） → 重新拉起服务
#
# 顺序是**先停、再构建、最后启动**，由助手脚本（tools/restart_server.py
# --build-frontend）在那个已经没有服务的窗口期里执行：这样 frontend/dist 在被清空重写
# 时没有任何进程在读它，起回来的服务读到的就是新的那一份产物。
#
# 和「更新完成后自动重启」共用同一套机制：同一个助手脚本、同一条发 Ctrl+C 的优雅退出
# 路径、同一份 config/logs/restart.log。区别只是多传一个 --build-frontend（构建输出也写进
# 那份日志，所以面板「日志」页上能直接看到 npm 的报错）。
#
# 三处安全阀和更新那条路一致：
#   * 开发模式（uvicorn --reload）不接 —— 真正跑服务的是 uvicorn 的子进程，停了会被
#     立刻拉起来，看起来就像「点了没用」，不如直接说清楚（409 + code: dev_mode）；
#   * 先用磁盘上的代码做一次 import 自检（``_new_code_imports_ok``）：改坏了就别停
#     现在这个还好好的服务，把原因告诉用户，改完再点一次；
#   * 助手脚本起不来（脚本不在 / 进程创建被拒）就当场报错，绝不先退出再说。

#: 是否已经安排了这次重启：连点几下不能安排一堆
_server_restart_state = {"running": False, "started_at": None}


@control_router.post("/api/server/restart")
async def restart_server():
    """面板「日志」页上的「重启服务」：停后端 → 重建前端 → 再启动，页面自己刷新。

    真正的退出安排在响应之后（``_exit_for_restart``，同「停止程序」）：调用方总能拿到
    一个正常的 200，而不是一个「请求失败」——不然面板根本分不清是自己点的重启，还是
    服务崩了。

    响应里的 ``port`` 是服务实际监听的端口（从自己的命令行里读），面板用它写提示。
    """
    address = _server_cli_address()
    if address is None:                     # 命令行里有 --reload：开发模式，重启没意义
        return JSONResponse(
            {
                "ok": False,
                "code": "dev_mode",
                "error": "开发模式（uvicorn --reload）下不需要从面板重启服务："
                         "保存代码时 uvicorn 会自己重载，前端改动请手动跑一次 npm run build。",
            },
            status_code=409,
        )
    host, port = address
    if _server_restart_state.get("running"):    # 连点了：别再安排一次
        return {"ok": True, "already": True, "restart": True, "build": True, "port": port}

    # 改坏的代码不该把「现在还能用的服务」换成「起不来的服务」（同更新那条路）
    ok, detail = await asyncio.to_thread(_new_code_imports_ok)
    if not ok:
        return JSONResponse(
            {
                "ok": False,
                "code": "import_failed",
                # 只给技术细节：面板按 code 换成当前语言的那句话再拼上它
                "error": detail or "import backend.main 失败",
            },
            status_code=400,
        )

    if not await asyncio.to_thread(_spawn_restart_helper, host, port, build_frontend=True):
        return JSONResponse(
            {"ok": False, "error": "自动重启没能开始，请手动重启一次服务（start.ps1 / start.sh）"},
            status_code=500,
        )
    _server_restart_state.update({"running": True, "started_at": time.time()})
    print(f"[i] 面板点了「重启服务」：先停服务并重建前端，再把服务拉回 {host}:{port}", flush=True)
    asyncio.create_task(_exit_for_restart())    # 响应发完再退出，面板拿得到正常 200
    return {"ok": True, "restart": True, "build": True, "port": port}


@control_router.post("/api/scrape/stop")
async def stop_scrape_and_dialog():
    """采集 section's 停止: kill the scraper AND close its dialog (if any)."""
    result = await stop_scrape()
    active = _job_error_state.get("active")
    if active and active.get("job") in (JOB_SCRAPE, JOB_VOICE):
        await resolve_job_error("stop")
        return {"status": "stopped"}
    return result


async def stop_scrape():
    """Terminate the scraping subprocess. Returns {status}."""
    proc = _scrape_state.get("process")
    if proc and proc.returncode is None:
        _scrape_state["stopped"] = True  # tell _run_scrape this was intentional
        # A frozen job cannot react to anything until it is thawed again.
        await _resume_job(_scrape_job_kind(_scrape_state.get("kind", "scrape")))
        proc.terminate()
        _scrape_state["status"] = "idle"
        _scrape_state["message"] = "已停止"
        return {"status": "stopped"}
    return {"status": "not_running"}


# ── Dialog + corner notice endpoints (shared by every job) ──

@control_router.post("/api/job-error/resume")
async def resume_job_error():
    """Dialog button 忽略并继续: close the dialog and let the job continue."""
    return await resolve_job_error("ignore")


@control_router.post("/api/job-error/stop")
async def stop_job_error():
    """Dialog button 停止: stop whichever job reported the error."""
    active = _job_error_state.get("active")
    if active is None:
        return {"status": "no_error"}
    job = active.get("job")
    result = await stop_job(job)
    await resolve_job_error("stop", job=job)
    return {"status": "stopped", "job": job, "job_status": result.get("status")}


@control_router.post("/api/job-error/alerts/dismiss")
async def dismiss_job_alert(req: JobAlertDismiss | None = None):
    """Close one corner error notice (no id = close them all)."""
    alerts = _job_error_state.setdefault("alerts", [])
    target = req.id if req is not None else None
    if target is None:
        alerts.clear()
    else:
        alerts[:] = [a for a in alerts if a.get("id") != target]
    return {"status": "ok", "remaining": len(alerts)}


@control_router.post("/api/custom-filter")
async def manage_custom_filter(req: CustomFilterAction):
    cfg = _load_config()
    filters = cfg.get("custom_filters", [])
    if req.action == "add" and req.value and req.value not in filters:
        filters.append(req.value)
    elif req.action == "remove" and req.value in filters:
        filters.remove(req.value)
    cfg["custom_filters"] = filters
    _save_config(cfg)
    return {"custom_filters": filters}


# ── Conversation discovery / selection ────────────────────────────

def _read_conv_list():
    if not os.path.exists(CONV_LIST_PATH):
        return {"discovered_at": 0, "items": []}
    try:
        with open(CONV_LIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"discovered_at": 0, "items": []}


_login_probe_lock = asyncio.Lock()


async def _probe_login_state() -> dict:
    """Single source of truth for whether the persistent profile is logged in.

    Always launches Chromium against `_USER_DATA_DIR` and reads cookies via
    Playwright. This intentionally goes through the same code path Chromium
    uses internally so we never disagree with what the actual scraper sees
    (whatever path the cookies DB lives at, WAL checkpoints, format
    migrations — all handled by Chromium itself).

    Returns one of:
        {"status": "logged_in",  "has_cookies": True}
        {"status": "expired",    "has_cookies": False}
        {"status": "no_profile", "has_cookies": False}
        {"status": "error",      "has_cookies": False, "message": "..."}

    Serialized via a module-level lock so the badge poll and the
    refresh/scrape preconditions can't race to launch two Chromium
    instances on the same profile (which would lock-conflict).
    """
    async with _login_probe_lock:
        has_profile = os.path.isdir(_USER_DATA_DIR) and os.listdir(_USER_DATA_DIR)
        if not has_profile:
            return {"status": "no_profile", "has_cookies": False}
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    _USER_DATA_DIR, headless=True,
                    viewport={"width": 1400, "height": 900}, locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    cookies = await ctx.cookies("https://www.douyin.com")
                    has_login = any(c["name"] == "sessionid" and c["value"] for c in cookies)
                    return {
                        "status": "logged_in" if has_login else "expired",
                        "has_cookies": has_login,
                    }
                finally:
                    await ctx.close()
            finally:
                await pw.stop()
        except Exception as e:
            return {"status": "error", "has_cookies": False, "message": str(e)}


@control_router.post("/api/conversations/refresh")
async def refresh_conversations():
    """Run a lightweight scrape that only enumerates the conversation list."""
    if _discover_state["status"] == "running":
        return JSONResponse({"error": "Refresh already running"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scraper is running — stop it first"}, status_code=409)

    # Pre-check: don't spawn the 3-minute browser wait if we already know
    # there's no usable session. Uses the same Playwright probe as the
    # login badge so the two never disagree.
    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        # 登录失效是「刷新」最常出的故障，光在按钮下面写一行小字容易被忽略：
        # 和采集出错一样弹一个窗说清楚（这时没有进程可暂停，倒计时结束自己收进右下角）。
        message = "未检测到登录态，请先扫码登录或导入 Cookie"
        await raise_job_error(JOB_REFRESH, message, message)
        return JSONResponse({"error": message}, status_code=400)

    _discover_state["status"] = "running"
    _discover_state["message"] = "正在加载会话列表..."
    _discover_state["started_at"] = time.time()
    _discover_state["finished_at"] = None
    _discover_state["paused"] = False
    _discover_state["paused_at"] = None
    _discover_state["stopped"] = False

    cmd = [sys.executable, "-u", "extract.py", "--list-conversations"]
    asyncio.create_task(_run_discover(cmd))
    return {"status": "started"}


async def _run_discover(cmd):
    """跑一次「刷新会话列表」，并像采集一样盯着日志和退出码报错。

    刷新时出的故障（登录失效、页面结构变了、进程直接挂掉）和采集共用同一套弹窗：
    出错先冻结子进程、弹一个带倒计时的窗；点「忽略并继续」或等倒计时结束就接着跑，
    点「停止刷新」就把这次刷新停掉；关掉的错误留在右下角，直到用户自己关。
    """
    proc = None
    watcher = None
    job = JOB_REFRESH
    await _resume_job(job)          # 上一次的弹窗可能还把它冻着
    start_job_error_run(job)
    try:
        os.makedirs(os.path.dirname(DISCOVER_LOG_PATH), exist_ok=True)
        with open(DISCOVER_LOG_PATH, "w", encoding="utf-8", newline="") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
                env=_utf8_subprocess_env(),
            )
            _discover_state["process"] = proc
            watcher = asyncio.create_task(
                _watch_scrape_log(DISCOVER_LOG_PATH, "refresh")
            )
            await proc.wait()

        if _discover_state.get("stopped"):
            # 用户自己点的「停止刷新」：进程是被杀掉的，退出码不为 0，但这不是故障
            _discover_state["status"] = "idle"
            _discover_state["message"] = "已停止"
        elif proc.returncode == 0:
            data = _read_conv_list()
            count = len(data.get("items", []))
            _discover_state["status"] = "completed"
            _discover_state["message"] = f"发现 {count} 个会话"
        elif proc.returncode == 2:
            _discover_state["status"] = "failed"
            _discover_state["message"] = "未检测到登录态，请先扫码或导入 Cookie"
        else:
            _discover_state["status"] = "failed"
            _discover_state["message"] = f"刷新失败 (exit {proc.returncode})"
    except Exception as e:
        _discover_state["status"] = "failed"
        _discover_state["message"] = f"刷新错误: {e}"
        # Best-effort: kill any lingering subprocess so it doesn't pin the state
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
    finally:
        if watcher is not None:
            watcher.cancel()
        _discover_state["finished_at"] = time.time()
        _discover_state["process"] = None
        _discover_state["paused"] = False
        _discover_state["paused_at"] = None
        # Defensive: ensure status is never left at "running" when this coroutine exits
        if _discover_state["status"] == "running":
            _discover_state["status"] = "failed"
            _discover_state["message"] = _discover_state["message"] or "刷新中断"
        if _discover_state["status"] == "failed" and not _discover_state.get("stopped"):
            # 任务已经结束，没有东西可以暂停：弹窗只把原因说清楚，倒计时结束就收进右下角
            await raise_job_error(
                job,
                _discover_state["message"],
                _scrape_log_tail(DISCOVER_LOG_PATH) or _discover_state["message"],
            )


@control_router.get("/api/conversations/refresh/status")
async def refresh_status():
    data = _read_conv_list()
    return {
        "status": _discover_state["status"],
        "message": _discover_state["message"],
        "started_at": _discover_state["started_at"],
        "finished_at": _discover_state["finished_at"],
        # 出错弹窗把刷新冻住了（和采集一样），页面据此把状态显示成「已暂停」
        "paused": bool(_discover_state.get("paused")),
        "discovered_at": data.get("discovered_at", 0),
        "items": data.get("items", []),
    }


@control_router.post("/api/conversations/refresh/stop")
async def refresh_stop():
    """页面上的「取消」按钮：停掉这次刷新，顺手关掉它自己的报错弹窗。

    和采集那一节的「停止」一样：既然是用户主动停的，就不该再留一个错误窗挂在屏幕上。
    """
    result = await _stop_discover()
    active = _job_error_state.get("active")
    if active and active.get("job") == JOB_REFRESH:
        await resolve_job_error("stop", job=JOB_REFRESH)
        return {"status": "stopped"}
    return result


async def _stop_discover() -> dict:
    """Terminate the refresh subprocess. Returns {status}.

    弹窗上的「停止刷新」走的也是这里，所以要先解冻再杀：被冻住的进程收不到任何信号。
    """
    proc = _discover_state.get("process")
    if proc and proc.returncode is None:
        _discover_state["stopped"] = True   # 告诉 _run_discover 这是有意停的，不是失败
        await _resume_job(JOB_REFRESH)
        proc.terminate()
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已停止"
        return {"status": "stopped"}
    # No live process — if state is still "running", force-reset (was stuck)
    if _discover_state["status"] == "running":
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已重置"
        _discover_state["finished_at"] = time.time()
        return {"status": "reset"}
    return {"status": "not_running"}


@control_router.get("/api/conversations/selected")
async def get_selected():
    cfg = _load_config()
    return {
        "scraper": cfg.get("scraper_selected", []),
        "export": cfg.get("export_selected", []),
        "schedule": cfg.get("schedule_selected", []),
    }


@control_router.post("/api/conversations/selected")
async def set_selected(req: SelectedUpdate):
    if req.section not in ("scraper", "export", "schedule"):
        return JSONResponse({"error": "invalid section"}, status_code=400)
    cfg = _load_config()
    cfg[f"{req.section}_selected"] = list(req.conversations)
    _save_config(cfg)
    return {"status": "ok", "selected": cfg[f"{req.section}_selected"]}


@control_router.post("/api/schedule")
async def set_schedule(req: ScheduleRequest):
    # 面板是勾选着用的，先把勾选换算成 cron；换算不了就当面说清楚哪一项没选，
    # 别默默存个空的、让用户以为已经生效了。
    cron = req.cron
    if req.rule is not None:
        problem = _simple_rule_problem(req.rule)
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        cron = _simple_rule_to_cron(req.rule)

    # 先校验、再动正在跑的任务：写错了的一个请求不该把好好的定时任务停掉
    parsed = None
    if req.enabled:
        if not cron:
            return JSONResponse(
                {"error": "请先勾选重复方式和执行时间，或用「高级」里的 cron 表达式"},
                status_code=400,
            )
        parsed = _parse_cron(cron)
        if not parsed:
            return JSONResponse({"error": "无效的 cron 表达式（分 时 日 月 周）"}, status_code=400)

    # Cancel existing scheduled task
    if _scheduler_state["task"] and not _scheduler_state["task"].done():
        _scheduler_state["task"].cancel()
        _scheduler_state["task"] = None

    _scheduler_state["enabled"] = req.enabled
    _scheduler_state["schedule"] = cron if req.enabled else ""
    _scheduler_state["next_run"] = None

    # Always persist the schedule selection so the cron loop + UI stay in sync
    cfg = _load_config()
    if req.conversations is not None:
        cfg["schedule_selected"] = list(req.conversations)

    if req.enabled and parsed:
        next_run = _next_cron_run(parsed)
        _scheduler_state["next_run"] = next_run
        _scheduler_state["task"] = asyncio.create_task(
            _cron_loop(parsed, req.incremental)
        )
        cfg["schedule"] = cron
        _save_config(cfg)
        return {"status": "enabled", "cron": cron, "next_run": next_run}

    cfg["schedule"] = ""
    _save_config(cfg)
    return {"status": "disabled"}


# Cron parsing (_parse_cron / _next_cron_run) lives in backend/panel/scheduler.py.


async def _cron_loop(parsed: list, incremental: bool):
    """Run scrape on cron schedule."""
    try:
        while True:
            next_run = _next_cron_run(parsed)
            _scheduler_state["next_run"] = next_run
            wait_secs = next_run - time.time()
            if wait_secs > 0:
                await asyncio.sleep(wait_secs)
            if _scrape_state["status"] != "running":
                cmd = [sys.executable, "-u", "extract.py"]
                if incremental:
                    cmd.append("--incremental")
                cfg = _load_config()
                if cfg.get("download_images") is False:
                    cmd.append("--no-download-images")
                else:
                    cmd.append("--download-images")
                # Preferred: schedule_selected (checkbox picks).
                # Fallback: custom_filters (legacy).
                # Fallback: all DB conversations (scrape everything we know).
                filters = cfg.get("schedule_selected") or cfg.get("custom_filters") or []
                if not filters:
                    from backend.database import get_db
                    conn = get_db()
                    convs = conn.execute("SELECT name FROM conversations WHERE name IS NOT NULL AND name != ''").fetchall()
                    conn.close()
                    filters = [c[0] for c in convs]
                if filters:
                    cmd.extend(["--filter", ",".join(filters)])
                _scrape_state["status"] = "running"
                _scrape_state["kind"] = "scrape"
                _scrape_state["started_at"] = time.time()
                _scrape_state["finished_at"] = None
                filter_desc = f" (过滤: {','.join(filters[:5])}{'...' if len(filters) > 5 else ''})" if filters else " (全部会话)"
                _scrape_state["message"] = f"定时{'增量' if incremental else '全量'}采集{filter_desc}"
                await _run_scrape(cmd)
            # Wait at least 61 seconds to avoid re-trigger in same minute
            await asyncio.sleep(61)
    except asyncio.CancelledError:
        pass


@control_router.post("/api/export")
async def start_export(req: ExportRequest):
    if _export_state["status"] == "running":
        return JSONResponse({"error": "Export already running"}, status_code=409)

    _export_state["status"] = "running"
    _export_state["file_path"] = None
    _export_state["message"] = "正在导出..."

    # Persist selection
    if req.conversations is not None:
        cfg = _load_config()
        cfg["export_selected"] = list(req.conversations)
        _save_config(cfg)

    convs = list(req.conversations) if req.conversations else None
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_export, req.format, req.filter, convs)
    return {
        "status": _export_state["status"],
        "message": _export_state["message"],
        "file_path": _export_state["file_path"],
    }


@control_router.post("/api/database/import")
async def import_database(request: Request):
    """Validate and atomically replace the local SQLite database."""
    conflict = _database_job_conflict()
    if conflict:
        return JSONResponse({"error": conflict}, status_code=409)

    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > _DATABASE_IMPORT_MAX_BYTES:
            return JSONResponse({"error": "导入文件过大"}, status_code=413)
    except ValueError:
        pass

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    allowed_types = {"", "application/octet-stream", "application/x-sqlite3", "application/vnd.sqlite3"}
    if content_type not in allowed_types:
        return JSONResponse({"error": "请上传 SQLite 数据库文件"}, status_code=415)

    os.makedirs(paths.DATA_DIR, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".chat_database_import_", suffix=".db", dir=paths.DATA_DIR
    )
    size = 0
    try:
        with os.fdopen(fd, "wb") as staged:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > _DATABASE_IMPORT_MAX_BYTES:
                    return JSONResponse({"error": "导入文件过大"}, status_code=413)
                staged.write(chunk)

        if size == 0:
            return JSONResponse({"error": "导入文件为空"}, status_code=400)

        try:
            _validate_database_file(staged_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            return JSONResponse({"error": f"数据库校验失败: {exc}"}, status_code=400)

        try:
            backup_name = _install_database(staged_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            return JSONResponse({"error": f"数据库替换失败: {exc}"}, status_code=500)

        message = "数据库导入完成，已覆盖当前数据库"
        if backup_name:
            message += f"；旧数据库已备份为 {backup_name}"
        return {"status": "ok", "message": message, "backup_file": backup_name}
    finally:
        try:
            os.remove(staged_path)
        except FileNotFoundError:
            pass


def _database_job_conflict() -> str | None:
    """Return a user-facing reason why replacing the DB is unsafe now."""
    if _scrape_state["status"] == "running":
        return "采集任务正在运行，请完成后再导入数据库"
    if _backfill_state["status"] == "running":
        return "历史图片下载正在运行，请完成后再导入数据库"
    if _video_backfill_state["status"] == "running":
        return "历史视频下载正在运行，请完成后再导入数据库"
    if _export_state["status"] == "running":
        return "导出任务正在运行，请完成后再导入数据库"
    return None


def _database_snapshot(output_path: str) -> None:
    """Create a consistent standalone SQLite snapshot, including WAL data."""
    temp_path = f"{output_path}.tmp"
    try:
        with _DATABASE_FILE_LOCK:
            source = sqlite3.connect(paths.DB_PATH)
            target = sqlite3.connect(temp_path)
            try:
                source.execute("PRAGMA busy_timeout=10000")
                source.backup(target, pages=1000, sleep=0.05)
            finally:
                target.close()
                source.close()
            os.replace(temp_path, output_path)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise


def _do_database_export() -> str:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"chat_database_{timestamp}.db"
    output_path = os.path.join(paths.DATA_DIR, filename)
    collision_index = 2
    while os.path.exists(output_path):
        filename = f"chat_database_{timestamp}_{collision_index}.db"
        output_path = os.path.join(paths.DATA_DIR, filename)
        collision_index += 1
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    _database_snapshot(output_path)
    return output_path


def _validate_database_file(path: str) -> None:
    """Validate an uploaded SQLite file before it can replace chat.db."""
    required_columns = {
        "users": {"uid"},
        "conversations": {"conv_id", "name"},
        "messages": {"msg_id", "conv_id", "raw_data"},
    }
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("数据库完整性检查未通过")
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError("数据库外键完整性检查未通过")
        for table, columns in required_columns.items():
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not row:
                raise ValueError(f"数据库缺少表: {table}")
            actual = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
            missing = columns - actual
            if missing:
                raise ValueError(f"数据库表 {table} 缺少字段: {', '.join(sorted(missing))}")
    finally:
        conn.close()


def _move_if_exists(source: str, target: str) -> bool:
    if not os.path.exists(source):
        return False
    os.replace(source, target)
    return True


def _install_database(staged_path: str) -> str | None:
    """Atomically install a validated DB and keep the previous one recoverable."""
    db_path = paths.DB_PATH
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(paths.DATA_DIR, f"chat_database_before_import_{timestamp}.db")
    suffixes = ("-wal", "-shm")
    moved_sidecars: list[tuple[str, str]] = []
    moved_db = False
    installed = False

    if os.path.exists(backup_path):
        index = 2
        while os.path.exists(backup_path):
            backup_path = os.path.join(
                paths.DATA_DIR,
                f"chat_database_before_import_{timestamp}_{index}.db",
            )
            index += 1

    with _DATABASE_FILE_LOCK:
        try:
            if os.path.exists(db_path):
                os.replace(db_path, backup_path)
                moved_db = True
            for suffix in suffixes:
                old_sidecar = db_path + suffix
                backup_sidecar = backup_path + suffix
                if _move_if_exists(old_sidecar, backup_sidecar):
                    moved_sidecars.append((old_sidecar, backup_sidecar))
            os.replace(staged_path, db_path)
            installed = True

            from common.db import init_db
            init_db()
        except Exception:
            try:
                if os.path.exists(db_path) and installed:
                    os.remove(db_path)
                if moved_db and os.path.exists(backup_path):
                    os.replace(backup_path, db_path)
                for original, backup in reversed(moved_sidecars):
                    if os.path.exists(backup):
                        os.replace(backup, original)
            finally:
                raise

    return os.path.basename(backup_path) if moved_db else None


def _do_export(fmt: str, filter_name: str, conversations: list | None):
    try:
        from extractor.exporter import ChatLabExporter, build_export_filename
        import zipfile

        data_dir = paths.DATA_DIR

        if fmt == "database":
            output_path = _do_database_export()
            _export_state["file_path"] = os.path.basename(output_path)
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            _export_state["message"] = f"数据库导出完成 ({size_mb:.1f} MB)"
            _export_state["status"] = "completed"
            return

        # Decide targets
        if conversations:
            targets = conversations
        elif filter_name:
            targets = [filter_name]
        else:
            targets = [None]  # None = exporter picks latest

        if len(targets) <= 1:
            # Single file
            exporter = ChatLabExporter(
                conv_name=targets[0] or None,
                output_format=fmt,
                output_dir=data_dir,
            )
            output_path = exporter.export()
            if not output_path or not os.path.exists(output_path):
                raise RuntimeError(f"未找到会话: {targets[0] or '(any)'}")
            _export_state["file_path"] = os.path.basename(output_path)
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 ({size_mb:.1f} MB)"
        else:
            # Multiple → bundle into a zip
            tmp_dir = os.path.join(data_dir, "export_tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            # Clear old tmp files
            for fn in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, fn))
                except Exception:
                    pass

            produced = []
            used_filenames = set()
            exported_at = int(time.time())
            for name in targets:
                filename = build_export_filename(name, fmt, exported_at)
                collision_index = 2
                while filename in used_filenames:
                    filename = build_export_filename(
                        name, fmt, exported_at, collision_index=collision_index
                    )
                    collision_index += 1
                used_filenames.add(filename)
                path = os.path.join(tmp_dir, filename)
                try:
                    ChatLabExporter(conv_name=name, output_format=fmt).export(path)
                    if os.path.exists(path):
                        produced.append((name, path))
                except Exception as e:
                    print(f"[-] 导出 {name} 失败: {e}")

            if not produced:
                raise RuntimeError("没有成功导出的会话")

            zip_path = os.path.join(data_dir, "export.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for _, path in produced:
                    zf.write(path, arcname=os.path.basename(path))

            _export_state["file_path"] = "export.zip"
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 ({len(produced)} 个会话, {size_mb:.1f} MB)"

        _export_state["status"] = "completed"
    except Exception as e:
        _export_state["status"] = "failed"
        _export_state["message"] = f"导出失败: {e}"


@control_router.get("/api/export/download")
async def download_export():
    if not _export_state["file_path"]:
        return JSONResponse({"error": "No export file"}, status_code=404)
    path = os.path.join(paths.DATA_DIR, _export_state["file_path"])
    if not os.path.exists(path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=_export_state["file_path"])


# ── Login (in-container headless with screenshot) ──

import base64

_USER_DATA_DIR = paths.BROWSER_PROFILE

_login_state = {
    "status": "idle",  # idle | starting | waiting_scan | logged_in | failed
    "screenshot": None,  # base64 png
    "message": "",
    "countdown": 0,
    "_context": None,
    "_pw": None,
}


@control_router.get("/api/login/check")
async def login_check():
    """Check login by actually opening browser and reading cookies."""
    return await _probe_login_state()


@control_router.post("/api/login/start")
async def login_start():
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "已在登录流程中"}, status_code=409)
    # If scraper is running, reject
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "请先停止采集再登录"}, status_code=409)

    _login_state["status"] = "starting"
    _login_state["screenshot"] = None
    _login_state["message"] = "正在启动浏览器..."
    asyncio.create_task(_login_flow())
    return {"status": "started"}


@control_router.get("/api/login/status")
async def login_status():
    return {
        "status": _login_state["status"],
        "screenshot": _login_state["screenshot"],
        "message": _login_state["message"],
        "countdown": _login_state["countdown"],
    }


class MouseAction(BaseModel):
    action: str  # click, mousedown, mousemove, mouseup
    x: float
    y: float


class KeyAction(BaseModel):
    action: str  # press, type
    key: str = ""
    text: str = ""


@control_router.post("/api/login/mouse")
async def login_mouse(req: MouseAction):
    """Forward mouse events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        mouse = page.mouse
        if req.action == "click":
            await mouse.click(req.x, req.y)
        elif req.action == "mousedown":
            await mouse.move(req.x, req.y)
            await mouse.down()
        elif req.action == "mousemove":
            await mouse.move(req.x, req.y)
        elif req.action == "mouseup":
            await mouse.up()
        else:
            return JSONResponse({"error": f"Unknown action: {req.action}"}, status_code=400)

        # Take a fresh screenshot after interaction
        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()

        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/keyboard")
async def login_keyboard(req: KeyAction):
    """Forward keyboard events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        kb = page.keyboard
        if req.action == "type" and req.text:
            await kb.type(req.text)
        elif req.action == "press" and req.key:
            await kb.press(req.key)
        else:
            return JSONResponse({"error": "Invalid keyboard action"}, status_code=400)

        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/cancel")
async def login_cancel():
    await _login_cleanup()
    _login_state["status"] = "idle"
    _login_state["message"] = "已取消"
    _login_state["screenshot"] = None
    return {"status": "cancelled"}


@control_router.post("/api/login/clear")
async def login_clear():
    """Clear browser profile to force re-login."""
    import shutil
    if os.path.isdir(_USER_DATA_DIR):
        shutil.rmtree(_USER_DATA_DIR, ignore_errors=True)
    return {"status": "cleared"}


def _validate_cookie_entries(parsed: list[dict]) -> tuple[list[str], list[str]]:
    """Pre-flight check on parsed cookies. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    sids = [c for c in parsed if c["name"] == "sessionid"]
    if not sids:
        errors.append("Cookie 中未包含 sessionid，请确保已登录后再导出（cookie-editor 需全选导出）")
        return errors, warnings

    sid = sids[0]
    value = (sid.get("value") or "").strip()
    if not value:
        errors.append("sessionid 的值为空")
    elif len(value) < 16:
        warnings.append(f"sessionid 长度异常 ({len(value)} 字节)，可能被截断")

    domain = (sid.get("domain") or "").lstrip(".")
    if domain and domain != "douyin.com" and not domain.endswith(".douyin.com"):
        errors.append(
            f"sessionid 的 domain 是 .{domain}（应为 .douyin.com）"
            "—— 可能在子站点（iesdouyin.com 等）导出了，请回到 www.douyin.com 重导"
        )

    exp = sid.get("expires")
    if exp and exp > 0 and exp < time.time():
        errors.append("sessionid 已过期（expirationDate 在过去），请重新登录后再导出")

    if len(parsed) < 3:
        warnings.append(
            f"只解析出 {len(parsed)} 个 cookie，抖音通常需要 10+ 个才能完整工作，"
            "建议在 cookie-editor 里全选后再导出"
        )
    return errors, warnings


@control_router.post("/api/login/cookie-import")
async def login_cookie_import(req: CookieImportRequest):
    """Import cookies from browser DevTools or document.cookie string."""
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止"}, status_code=409)
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "登录流程进行中，请先取消"}, status_code=409)

    raw = req.cookies.strip()
    if not raw:
        return JSONResponse({"error": "Cookie 数据为空"}, status_code=400)

    parsed: list[dict] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for c in data:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                entry: dict = {
                    "name": c["name"],
                    "value": str(c.get("value", "")),
                    "domain": c.get("domain", ".douyin.com"),
                    "path": c.get("path", "/"),
                }
                exp = c.get("expirationDate") or c.get("expires")
                if exp:
                    entry["expires"] = float(exp)
                if c.get("httpOnly") is not None:
                    entry["httpOnly"] = bool(c["httpOnly"])
                if c.get("secure") is not None:
                    entry["secure"] = bool(c["secure"])
                # cookie-editor exports sameSite as lowercase enum.
                # Map "no_restriction" → "None" (cross-site allowed) — must NOT downgrade to Lax,
                # since some Douyin auth cookies require cross-site delivery for IM API calls.
                ss = (c.get("sameSite") or "").strip().lower()
                ss_map = {"no_restriction": "None", "none": "None",
                          "lax": "Lax", "strict": "Strict"}
                if ss in ss_map:
                    entry["sameSite"] = ss_map[ss]
                    # Playwright requires Secure=true when SameSite=None
                    if entry["sameSite"] == "None":
                        entry["secure"] = True
                parsed.append(entry)
        else:
            return JSONResponse({"error": "JSON 格式需为数组"}, status_code=400)
    except (json.JSONDecodeError, ValueError):
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            parsed.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })

    if not parsed:
        return JSONResponse({"error": "未能解析出任何 Cookie"}, status_code=400)

    errors, warnings = _validate_cookie_entries(parsed)
    if errors:
        return JSONResponse({"error": "；".join(errors)}, status_code=400)

    # Session cookies (no expirationDate) get dropped on browser restart,
    # so the next login probe wouldn't see them. Pin a 30-day default.
    default_exp = time.time() + 30 * 86400
    for c in parsed:
        if "expires" not in c:
            c["expires"] = default_exp

    try:
        from playwright.async_api import async_playwright
        os.makedirs(_USER_DATA_DIR, exist_ok=True)
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            _USER_DATA_DIR,
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(1)
        await ctx.add_cookies(parsed)
        cookies = await ctx.cookies("https://www.douyin.com")
        ok = "sessionid" in {c["name"] for c in cookies}
        all_cookies = await ctx.cookies()  # everything regardless of url, for diagnostics
        await ctx.close()
        await pw.stop()
        if ok:
            msg = f"成功导入 {len(parsed)} 个 Cookie"
            if warnings:
                msg += "（注意：" + "；".join(warnings) + "）"
            return {"status": "ok", "message": msg, "count": len(parsed),
                    "warnings": warnings}
        # Verification failed — diagnose why so the user knows what to fix.
        sid_other = [c for c in all_cookies if c["name"] == "sessionid"]
        if sid_other:
            wrong_domain = sid_other[0].get("domain", "?")
            return JSONResponse(
                {"error": f"sessionid 被加载到 domain={wrong_domain}，"
                          f"对 www.douyin.com 不生效。请确认 cookie 的 domain 是 .douyin.com"},
                status_code=400,
            )
        return JSONResponse(
            {"error": "sessionid 导入后无法在 douyin.com 读取到，"
                      "可能已被服务端注销，请重新登录后再导出"},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse({"error": f"导入失败: {e}"}, status_code=500)


async def _login_cleanup():
    try:
        if _login_state["_context"]:
            await _login_state["_context"].close()
    except Exception:
        pass
    try:
        if _login_state["_pw"]:
            await _login_state["_pw"].stop()
    except Exception:
        pass
    _login_state["_context"] = None
    _login_state["_pw"] = None


async def _login_flow():
    """In-container: open headless browser, screenshot the page for QR scanning."""
    try:
        from playwright.async_api import async_playwright

        os.makedirs(_USER_DATA_DIR, exist_ok=True)
        pw = await async_playwright().start()
        _login_state["_pw"] = pw

        ctx = await pw.chromium.launch_persistent_context(
            _USER_DATA_DIR,
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        _login_state["_context"] = ctx
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Navigate to Douyin
        _login_state["message"] = "正在打开抖音..."
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Check if already logged in
        cookies = await ctx.cookies("https://www.douyin.com")
        if any(c["name"] == "sessionid" and c["value"] for c in cookies):
            _login_state["status"] = "logged_in"
            _login_state["message"] = "已登录，无需扫码"
            await _login_cleanup()
            return

        # Try to click login button
        _login_state["status"] = "waiting_scan"
        _login_state["message"] = "正在获取二维码..."
        try:
            login_btn = await page.wait_for_selector(
                'button:has-text("登录")', timeout=5000
            )
            if login_btn:
                await login_btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        # Poll: take screenshots and check cookies
        timeout_secs = 180
        for i in range(timeout_secs):
            if _login_state["status"] != "waiting_scan":
                break  # cancelled

            _login_state["countdown"] = timeout_secs - i

            # Screenshot
            png = await page.screenshot(type="png")
            _login_state["screenshot"] = base64.b64encode(png).decode()
            _login_state["message"] = f"请用抖音 APP 扫码 ({timeout_secs - i}s)"

            # Check login
            cookies = await ctx.cookies("https://www.douyin.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                _login_state["status"] = "logged_in"
                _login_state["message"] = "登录成功！"
                _login_state["screenshot"] = None
                await _login_cleanup()
                return

            await asyncio.sleep(1)

        if _login_state["status"] == "waiting_scan":
            _login_state["status"] = "failed"
            _login_state["message"] = "扫码超时（3 分钟）"

    except Exception as e:
        _login_state["status"] = "failed"
        _login_state["message"] = f"登录错误: {e}"
    finally:
        await _login_cleanup()
