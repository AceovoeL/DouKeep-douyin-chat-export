"""「发现新版本时自动更新」：开关、安全阀、以及出错时怎么让人知道。

不打网络、不动真的代码：检测结果、git 状态、任务是否在跑、更新脚本都在用例里打桩，
只验证这个功能自己的逻辑 —— 默认关；开着时先过安全阀（有任务在跑 / 本地有改动 /
读不到 git 记录 / 更新源不对 / 已经有一次更新在跑都要跳过）；放行时走的是和面板按钮
完全相同的更新路径（含自动重启）；出问题时面板和通知都要说清楚、日志位置也要给。
"""
import asyncio
import json
import os

import pytest

from backend import control_panel as cp
from backend.panel import scheduler as sched
from common import paths, version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")

#: 真的「问 git 本地有没有改动」那个函数。夹具默认把它换成「干净」，方便验证别的
#: 逻辑；专门测它自己的用例再换回来（见 _real_worktree_check）。
REAL_WORKTREE_CHECK = cp._worktree_dirty_paths


def _real_worktree_check(monkeypatch):
    """把这几个用例里的工作区检查换回真实实现。"""
    monkeypatch.setattr(cp, "_worktree_dirty_paths", REAL_WORKTREE_CHECK)


# ── 夹具 ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """定时检查与更新状态都是模块级共享的，每个用例从干净的一份开始。"""
    monkeypatch.setattr(cp, "_update_schedule_state", {
        "enabled": True,
        "rule": {"mode": "daily", "weekdays": [], "monthdays": [], "times": ["09:00"]},
        "auto_update": True,
        "task": None, "next_run": None, "last_run": None, "last_result": None,
        "running": False,
    })
    monkeypatch.setattr(cp, "_update_state", {**cp._update_state})
    # 任务都在闲着、更新源是本项目、工作区干干净净：默认「可以自动更新」
    monkeypatch.setattr(cp, "_job_is_running", lambda job: False)
    monkeypatch.setattr(cp, "_export_state", {**cp._export_state, "status": "idle"})
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp, "_worktree_dirty_paths", lambda: [])


def _check_result(**overrides) -> dict:
    result = {
        "ok": True, "update_available": True, "current_version": version.VERSION,
        "remote_version": "9.9.9", "behind": 1,
        "versions": [{"version": "9.9.9", "subject": "某个新功能"}],
    }
    result.update(overrides)
    return result


@pytest.fixture
def notifications(monkeypatch) -> list:
    """拦下微信通知，返回 (标题, 正文) 的列表。"""
    sent: list[tuple[str, str]] = []

    async def fake_notify(title, desp):
        sent.append((title, desp))

    monkeypatch.setattr(cp, "_notify_new_version", fake_notify)
    return sent


@pytest.fixture
def started_updates(monkeypatch) -> list:
    """拦下 _start_update_task：只记下调用参数，不真的安排更新任务。"""
    seen: list[dict] = []

    def fake_start(check, *, allow_dirty, want_restart, notify):
        seen.append({"to": check["remote_version"], "allow_dirty": allow_dirty,
                     "want_restart": want_restart, "notify": notify})
        return {"status": "started", "from_version": version.VERSION,
                "to_version": check["remote_version"], "allow_dirty": allow_dirty,
                "auto_restart": want_restart}

    monkeypatch.setattr(cp, "_start_update_task", fake_start)
    return seen


# ── 安全阀 1：有任务在跑 ──────────────────────────────────────────────────

@pytest.mark.parametrize("job, label", [
    (cp.JOB_SCRAPE, "采集"),
    (cp.JOB_VOICE, "语音转写补充"),
    (cp.JOB_MEDIA_IMAGES, "下载历史图片"),
    (cp.JOB_MEDIA_VIDEOS, "下载历史视频"),
    (cp.JOB_REFRESH, "刷新会话列表"),
])
def test_busy_labels_cover_every_job_that_a_restart_would_kill(monkeypatch, job, label):
    monkeypatch.setattr(cp, "_job_is_running", lambda running: running == job)
    assert cp._busy_job_labels() == [label]


def test_busy_labels_include_export(monkeypatch):
    """导出也是「一重启就白跑」的事，所以照样要等它跑完。"""
    monkeypatch.setattr(cp, "_export_state", {**cp._export_state, "status": "running"})
    assert cp._busy_job_labels() == ["导出"]


def test_busy_labels_lists_everything_at_once(monkeypatch):
    monkeypatch.setattr(cp, "_job_is_running", lambda job: job in (cp.JOB_SCRAPE, cp.JOB_VOICE))
    assert cp._busy_job_labels() == ["采集", "语音转写补充"]


# ── 安全阀 2：本地有未提交的改动 / 读不到 git 记录 ────────────────────────

def test_worktree_check_returns_none_without_git_history(monkeypatch, tmp_path):
    _real_worktree_check(monkeypatch)
    """没有 .git 的副本目录：判断不了本地有没有改动 → None（自动更新据此跳过）。"""
    monkeypatch.setattr(cp._version, "REPO_ROOT", str(tmp_path))
    assert cp._worktree_dirty_paths() is None


def test_worktree_check_returns_none_without_git_command(monkeypatch, tmp_path):
    _real_worktree_check(monkeypatch)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cp._version, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(cp.shutil, "which", lambda name: None)
    assert cp._worktree_dirty_paths() is None


def _fake_git(outputs: dict):
    class _Result:
        def __init__(self, stdout, returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, "", returncode

    def fake(args, **kwargs):
        key = " ".join(args)
        value = outputs.get(key)
        if isinstance(value, int):                      # 用整数表示「这条命令失败了」
            return _Result("", value)
        return _Result("" if value is None else value)

    return fake


def test_worktree_check_lists_real_changes(monkeypatch, tmp_path):
    _real_worktree_check(monkeypatch)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cp._version, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(cp.shutil, "which", lambda name: "git")
    monkeypatch.setattr(cp, "_git_run", _fake_git({
        "diff --ignore-cr-at-eol --name-only": "backend/control_panel.py\nREADME.md\n",
    }))
    assert cp._worktree_dirty_paths() == ["README.md", "backend/control_panel.py"]


def test_worktree_check_treats_a_failed_git_as_unknown(monkeypatch, tmp_path):
    _real_worktree_check(monkeypatch)
    """git 问不出来时报「不知道」，不能当成「干净」——那等于拿用户的改动冒险。"""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cp._version, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(cp.shutil, "which", lambda name: "git")
    monkeypatch.setattr(cp, "_git_run", _fake_git({
        "diff --ignore-cr-at-eol --name-only": 128,
    }))
    assert cp._worktree_dirty_paths() is None


def test_worktree_check_is_clean_when_nothing_changed(monkeypatch, tmp_path):
    _real_worktree_check(monkeypatch)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cp._version, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(cp.shutil, "which", lambda name: "git")
    monkeypatch.setattr(cp, "_git_run", _fake_git({}))
    assert cp._worktree_dirty_paths() == []


# ── 闸门汇总：什么情况放行、什么情况跳过 ──────────────────────────────────

def test_gate_passes_when_everything_is_calm():
    assert asyncio.run(cp._auto_update_gate()) == {"blocked": None, "dirty_checked": True}


def test_gate_does_not_block_without_git_history(monkeypatch):
    """没有 git 记录的电脑（只用来跑、没装 git）**不拦**：照样要能自动更新。

    只是要把「这次是下载代码包覆盖、本地改动不会保留」讲清楚，所以 dirty_checked
    记成 False，交给调用方在通知里提示。
    """
    monkeypatch.setattr(cp, "_worktree_dirty_paths", lambda: None)

    gate = asyncio.run(cp._auto_update_gate())

    assert gate["blocked"] is None
    assert gate["dirty_checked"] is False


def test_gate_reports_a_running_update():
    cp._update_state["status"] = "running"
    assert asyncio.run(cp._auto_update_gate())["blocked"] == ("updating", "")


def test_gate_reports_a_running_job(monkeypatch):
    monkeypatch.setattr(cp, "_job_is_running", lambda job: job == cp.JOB_SCRAPE)
    assert asyncio.run(cp._auto_update_gate())["blocked"] == ("busy", "采集")


def test_gate_reports_a_foreign_update_source(monkeypatch):
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.UPSTREAM_REPOSITORY_URL)
    assert asyncio.run(cp._auto_update_gate())["blocked"] == ("foreign", "")


def test_gate_reports_a_dirty_worktree(monkeypatch):
    monkeypatch.setattr(cp, "_worktree_dirty_paths", lambda: ["README.md", "a.py"])
    assert asyncio.run(cp._auto_update_gate())["blocked"] == ("dirty", "README.md、a.py")


def test_skip_text_explains_every_code():
    for code in ("updating", "busy", "dirty", "foreign", "whatever"):
        assert cp._auto_update_skip_text(code).endswith("。"), code
    assert "采集" in cp._auto_update_skip_text("busy", "采集")


# ── 发现新版本之后到底做了什么 ────────────────────────────────────────────

def test_switch_off_only_notifies(notifications, started_updates):
    cp._update_schedule_state["auto_update"] = False

    auto = asyncio.run(cp._handle_new_version(_check_result()))

    assert auto is None, "开关关着时不写自动更新那一栏"
    assert started_updates == [], "开关关着时不许自动装"
    assert len(notifications) == 1
    assert "发现新版本" in notifications[0][0]
    assert "立即更新" in notifications[0][1]


def test_switch_on_starts_the_update(notifications, started_updates):
    auto = asyncio.run(cp._handle_new_version(_check_result()))

    assert auto == {"action": "started"}
    assert started_updates == [{"to": "9.9.9", "allow_dirty": False,
                                "want_restart": True, "notify": True}]
    assert "开始自动更新" in notifications[0][0]
    assert "v9.9.9" in notifications[0][1]
    # 本地改动检查过了（干净）：不需要那句「会直接覆盖」的提醒
    assert "不会保留" not in notifications[0][1]


def test_switch_on_without_git_history_still_updates_but_warns(monkeypatch, notifications,
                                                              started_updates):
    """没装 git / 没有 git 记录的电脑：照样自动更新，但通知里要说清「会覆盖」。"""
    monkeypatch.setattr(cp, "_worktree_dirty_paths", lambda: None)

    auto = asyncio.run(cp._handle_new_version(_check_result()))

    assert auto == {"action": "started"}, "没装 git 的电脑不许被拦下来"
    assert started_updates and started_updates[0]["notify"] is True
    desp = notifications[0][1]
    assert "读不到 git 记录" in desp
    assert "下载代码包覆盖" in desp
    assert "不会保留" in desp


@pytest.mark.parametrize("code, expected", [
    ("updating", "已经有一次更新在进行中"),
    ("dirty", "还没提交"),
])
def test_switch_on_but_blocked_only_notifies(monkeypatch, notifications, started_updates,
                                            code, expected):
    if code == "updating":
        cp._update_state["status"] = "running"
    elif code == "dirty":
        monkeypatch.setattr(cp, "_worktree_dirty_paths", lambda: ["README.md"])

    auto = asyncio.run(cp._handle_new_version(_check_result()))

    assert auto["action"] == "skipped"
    assert auto["code"] == code
    assert isinstance(auto["detail"], str)
    assert started_updates == [], "被安全阀拦下时不许动代码"
    assert len(notifications) == 1
    title, desp = notifications[0]
    assert "这次没自动更新" in title
    assert expected in desp
    assert "立即更新" in desp


def test_blocked_by_a_running_job_names_it(monkeypatch, notifications, started_updates):
    monkeypatch.setattr(cp, "_job_is_running", lambda job: job == cp.JOB_MEDIA_VIDEOS)

    auto = asyncio.run(cp._handle_new_version(_check_result()))

    assert auto == {"action": "skipped", "code": "busy", "detail": "下载历史视频"}
    assert started_updates == []
    assert "下载历史视频" in notifications[0][1]


def test_scheduled_check_records_the_auto_update_outcome(monkeypatch, notifications,
                                                         started_updates):
    async def fake_collect(*, refresh=False):
        return _check_result()

    monkeypatch.setattr(cp, "_collect_update", fake_collect)
    asyncio.run(cp._run_update_check_once())

    last = cp._update_schedule_state["last_result"]
    assert last["auto_update"] == {"action": "started"}, "面板靠这一栏显示「已开始自动更新」"


def test_scheduled_check_without_auto_update_keeps_the_old_behaviour(monkeypatch, notifications):
    cp._update_schedule_state["auto_update"] = False

    async def fake_collect(*, refresh=False):
        return _check_result()

    monkeypatch.setattr(cp, "_collect_update", fake_collect)
    asyncio.run(cp._run_update_check_once())

    assert cp._update_schedule_state["last_result"]["auto_update"] is None
    assert len(notifications) == 1


# ── 自动更新跑起来之后：出错要看得见 ──────────────────────────────────────

@pytest.fixture
def update_log(tmp_path, monkeypatch):
    path = tmp_path / "update.log"
    path.write_text("=====> 拉取最新代码 git pull --ff-only\n[-] git pull 失败：本地与远端分叉\n",
                    encoding="utf-8")
    monkeypatch.setattr(cp, "UPDATE_LOG_PATH", str(path))
    return path


def test_auto_update_failure_shows_up_on_the_panel_and_in_the_notification(
        monkeypatch, notifications, update_log):
    """自动更新失败：面板弹一条（含日志位置）+ 微信推一条（含日志末尾）。"""
    dialogs: list = []

    async def fake_raise(job, message, detail=""):
        dialogs.append({"job": job, "message": message, "detail": detail})

    monkeypatch.setattr(cp, "raise_job_error", fake_raise)

    asyncio.run(cp._report_auto_update_problem("自动更新失败（更新返回 1）", "看日志"))

    assert dialogs[0]["job"] == cp.JOB_UPDATE
    assert "自动更新失败" in dialogs[0]["message"]
    assert "config/logs/update.log" in dialogs[0]["detail"]
    assert "config/logs/restart.log" in dialogs[0]["detail"]
    assert "日志" in dialogs[0]["detail"]
    title, desp = notifications[0]
    assert "自动更新没能完成" in title
    assert "自动更新失败" in desp
    assert "git pull 失败" in desp, "通知里要带上日志末尾，否则用户不知道错在哪"
    assert "config/logs/update.log" in desp


def test_run_update_notifies_when_the_script_fails(monkeypatch, update_log):
    """tools/update.py 非零退出：状态记为失败，并且推一条带日志的通知。"""
    reported: list = []

    async def fake_report(message, detail=""):
        reported.append((message, detail))

    class _Proc:
        returncode = 3

        async def wait(self):
            return 3

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cp, "_report_auto_update_problem", fake_report)

    asyncio.run(cp._run_update("1.3.1", "1.3.2", restart=None, notify=True))

    assert cp._update_state["status"] == "failed"
    assert reported and "自动更新失败" in reported[0][0]
    assert cp._update_state["process"] is None


def test_run_update_keeps_the_manual_path_quiet(monkeypatch, update_log):
    """用户自己点「立即更新」时失败：只更新状态，不额外推微信（他正在面板上看）。"""
    reported: list = []

    async def fake_report(message, detail=""):
        reported.append(message)

    class _Proc:
        returncode = 3

        async def wait(self):
            return 3

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cp, "_report_auto_update_problem", fake_report)

    asyncio.run(cp._run_update("1.3.1", "1.3.2", restart=None, notify=False))

    assert cp._update_state["status"] == "failed"
    assert reported == []


def test_run_update_notifies_when_the_worktree_became_dirty(monkeypatch, update_log):
    """检查时还干净、装的时候被人改了：停下来并说清楚，不替用户做覆盖的决定。"""
    reported: list = []

    async def fake_report(message, detail=""):
        reported.append(message)

    class _Proc:
        returncode = cp.UPDATE_EXIT_DIRTY

        async def wait(self):
            return self.returncode

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cp, "_report_auto_update_problem", fake_report)

    asyncio.run(cp._run_update("1.3.1", "1.3.2", restart=None, notify=True))

    assert cp._update_state["status"] == "dirty"
    assert reported and "未提交" in reported[0]


def test_finish_update_reports_when_the_helper_cannot_start(monkeypatch, update_log):
    """更新成功但重启没起来：面板提示「请手动重启」，自动更新还要推一条通知。"""
    reported: list = []

    async def fake_report(message, detail=""):
        reported.append((message, detail))

    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port: False)
    monkeypatch.setattr(cp, "_report_auto_update_problem", fake_report)

    asyncio.run(cp._finish_update_after_success(
        "1.3.2", {"host": "127.0.0.1", "port": 8000}, notify=True))

    assert cp._update_state["status"] == "completed"
    assert "请手动重启" in cp._update_state["message"]
    assert reported and "自动重启没能开始" in reported[0][0]
    assert "start.ps1" in reported[0][1]


def test_finish_update_notifies_before_handing_over_to_the_restart(monkeypatch, tmp_path,
                                                                   notifications):
    """重启交棒成功时也要说一声：通知先发出去，然后才安排「自己退出」。"""
    tasks: list = []

    def fake_create_task(coro, **kwargs):
        tasks.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port: True)
    monkeypatch.setattr(cp, "_write_update_done_record", lambda payload: True)
    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)

    asyncio.run(cp._finish_update_after_success(
        "1.3.2", {"host": "127.0.0.1", "port": 8000}, notify=True))

    assert cp._update_state["status"] == "restarting"
    assert len(tasks) == 1, "通知发完之后仍然要安排退出任务"
    title, desp = notifications[0]
    assert "已更新到 v1.3.2" in title
    assert "自动重启" in desp
    assert "config/logs/restart.log" in desp, "重启失败时用户要知道去哪里看日志"


def test_finish_update_stays_quiet_without_notify(monkeypatch, notifications):
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port: False)

    asyncio.run(cp._finish_update_after_success(
        "1.3.2", {"host": "127.0.0.1", "port": 8000}))

    assert notifications == []


# ── 设置的存取：开关要跟着配置文件活下来 ──────────────────────────────────

@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


def test_auto_update_switch_is_saved_and_restored(config_path, monkeypatch):
    monkeypatch.setattr(cp.asyncio, "create_task", lambda coro, **kw: coro.close())

    saved = asyncio.run(cp.set_update_schedule(cp.UpdateScheduleRequest(
        enabled=True, rule={"mode": "daily", "weekdays": [], "monthdays": [],
                            "times": ["03:00"]}, auto_update=True)))
    assert saved["schedule"]["auto_update"] is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["update_schedule"]["auto_update"] is True

    # 重启服务：从配置文件恢复，开关还得是开的
    cp._update_schedule_state["auto_update"] = False
    asyncio.run(cp.restore_update_schedule_on_startup())
    assert cp._update_schedule_state["auto_update"] is True


def test_update_status_exposes_the_auto_update_flag(config_path):
    cp._update_schedule_state["auto_update"] = True
    payload = asyncio.run(cp.update_status())
    assert payload["schedule"]["schedule"]["auto_update"] is True


def test_toggling_the_switch_alone_keeps_the_time_table(config_path, monkeypatch):
    """面板上「只拨一下开关」送的就是这一组请求（见 saveAutoUpdateSwitch）。

    总开关和时间表照抄「上一次真的存下来的那一份」，只有 auto_update 是刚拨到的值：
    存完之后时间表必须原样不动，不能因为拨了个开关就把时间表重置成默认。
    """
    monkeypatch.setattr(cp.asyncio, "create_task", lambda coro, **kw: coro.close())
    rule = {"mode": "weekly", "weekdays": [0, 3], "monthdays": [], "times": ["21:30"]}
    asyncio.run(cp.set_update_schedule(cp.UpdateScheduleRequest(
        enabled=True, rule=rule, auto_update=False)))

    saved = cp._update_schedule_payload()          # 面板重新读回来的那份
    result = asyncio.run(cp.set_update_schedule(cp.UpdateScheduleRequest(
        enabled=bool(saved["schedule"]["enabled"]), rule=saved["rule"], auto_update=True)))

    assert result["schedule"]["auto_update"] is True
    assert result["rule"] == rule, "只拨开关不该动时间表"
    assert result["text"] == "每周一、周四 21:30"
    saved_config = json.loads(config_path.read_text(encoding="utf-8"))["update_schedule"]
    assert saved_config == {"enabled": True, "rule": rule, "auto_update": True}


def test_old_config_without_the_switch_keeps_auto_update_off(config_path, monkeypatch):
    """老版本写下的配置里没有 auto_update：读回来必须是关的（不会突然自己更新）。"""
    config_path.write_text(json.dumps({"update_schedule": {
        "enabled": True, "mode": "daily", "time": "09:00", "weekday": 0,
    }}), encoding="utf-8")
    monkeypatch.setattr(cp.asyncio, "create_task", lambda coro, **kw: coro.close())

    asyncio.run(cp.restore_update_schedule_on_startup())

    assert cp._update_schedule_state["auto_update"] is False
    assert sched.normalize_update_schedule(
        {"enabled": True, "mode": "daily", "time": "09:00", "weekday": 0}
    )["auto_update"] is False


# ── 面板页面上的接线 ──────────────────────────────────────────────────────

def test_panel_html_wires_the_auto_update_switch():
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert 'id="updateSchAuto"' in html
    assert "auto_update: autoUpdate" in html              # 保存时带上开关
    assert "auto.checked = !!s.auto_update;" in html      # 读回来时填上开关
    assert "AUTO_UPDATE_SKIP_KEYS" in html                # 跳过原因翻成当前语言
    for code in ("updating", "busy", "dirty", "foreign"):
        assert f"{code}:" in html, code
    assert "no_git:" not in html, "没有 git 记录不再是一条「跳过」理由"
    # 这次更新是自动开始的：说明文字跟手动点的不一样
    assert "aboutUpdateRunningAuto" in html

    for key in ("aboutAutoCheckAutoUpdate", "aboutAutoCheckAutoNote",
                "aboutAutoCheckAutoStarted", "aboutAutoCheckAutoSkippedUpdating",
                "aboutAutoCheckAutoSkippedBusy", "aboutAutoCheckAutoSkippedDirty",
                "aboutAutoCheckAutoSkippedForeign",
                "aboutAutoCheckAutoSkippedUnknown", "aboutUpdateRunningAuto"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
    assert "aboutAutoCheckAutoSkippedNoGit" not in html, "这条文案应该已经删掉"


def test_auto_update_switch_saves_right_away():
    """这个开关勾一下就存（不用点「应用」）：动的是「已经存下来的」时间表。

    用户 2026-10-03 的要求：只有这一个开关要立刻生效，「应用」留给上面那张时间表。
    连带存下去的总开关与规则必须来自「上一次真的存下来的那份」（updateScheduleData），
    不能拿界面上正改着、还没点「应用」的预览值 —— 否则顺手拨一下开关就把改了一半的
    时间表一起存了。
    """
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert 'onchange="saveAutoUpdateSwitch()"' in html        # 勾选框自己带事件
    assert "async function saveAutoUpdateSwitch()" in html
    assert "updateScheduleRule(saved)" in html                # 用存过的那份规则
    assert "auto_update: wanted" in html                      # 存的是刚拨到的状态
    assert "box.checked = !wanted;" in html                   # 存不上就拨回去，不骗人
    assert "setUpdateScheduleSaved" in html                   # 存好了给一行绿的反馈
    # 绿色那行要留住几秒：更新状态的轮询 1.5 秒一次，不留就会被立刻冲掉、用户看不到反馈
    assert "UPDATE_SCHED_SAVED_MS" in html
    assert "updateSchedSavedUntil" in html
    assert "const keepSaved = Date.now() < updateSchedSavedUntil;" in html
    assert "updateSchedSavedUntil = 0;" in html               # 出错时立刻收起绿字
    # 提示「不用点应用」的文案，中英文各一份
    for key in ("aboutAutoCheckAutoInstant", "aboutAutoCheckAutoSaved",
                "aboutAutoCheckAutoNeedApply"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"


def test_panel_note_lists_the_safety_valves():
    """说明文字要把「什么时候只发通知」和「没有 git 记录会覆盖代码」都讲出来。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert "有采集、语音转写、下载任务在跑" in html
    assert "本地有未提交的改动" in html
    assert "下载代码包覆盖" in html
    assert "a scrape / transcription / download job is running" in html
    assert "downloading and unpacking the code package" in html
