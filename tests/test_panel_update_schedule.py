"""定时自动检查更新：时间表算法 + 后端接口 + 面板页面的接线。

时间表和「定时采集」共用同一套勾选规则（每天 / 每周几 / 每月哪几天 + 好多个时间点），
所以这里的用例既钉住「同一天能检查好几次」，也钉住老配置读回来不丢设置。

不打网络、也不真的等时间：「到点」这件事由 backend/panel/scheduler.py 里的纯函数
算出来，检测本身（``_collect_update``）在用例里换成假函数，只验证属于本功能的部分
—— 到点算什么时间、跑完有没有记住结果、有没有按设置发通知、设置重启后还在不在。
"""
import asyncio
import json
import os
import time
from datetime import datetime

import pytest

from backend import control_panel as cp
from backend.panel import scheduler as sched
from common import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")


# ── 夹具 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """把 panel_config.json 指到临时目录：定时设置要真的写盘、真的读回来。"""
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


def daily_rule(*times, **overrides) -> dict:
    """一份「每天这几个点」的时间表（用例里最常用的形状）。"""
    rule = {"mode": "daily", "weekdays": [], "monthdays": [], "times": list(times)}
    rule.update(overrides)
    return rule


def weekly_rule(weekdays, *times) -> dict:
    return {"mode": "weekly", "weekdays": list(weekdays), "monthdays": [],
            "times": list(times)}


def monthly_rule(monthdays, *times) -> dict:
    return {"mode": "monthly", "weekdays": [], "monthdays": list(monthdays),
            "times": list(times)}


@pytest.fixture(autouse=True)
def clean_update_schedule_state(monkeypatch):
    """每个用例都从「刚开机、还没排任务」的状态开始，结束自动还原。"""
    monkeypatch.setattr(cp, "_update_schedule_state", {
        "enabled": False,
        "rule": {"mode": "daily", "weekdays": [], "monthdays": [], "times": ["09:00"]},
        "auto_update": False,
        "task": None, "next_run": None, "last_run": None, "last_result": None,
        "running": False,
    })


class _FakeTask:
    """假的后台任务：只记下协程和有没有被取消，不真的跑那个无限循环。"""

    def __init__(self, coro):
        self.coro = coro
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


def record_tasks(monkeypatch) -> list:
    """拦下 asyncio.create_task，返回所有被安排的任务（协程随手关掉）。"""
    created: list[_FakeTask] = []

    def fake_create_task(coro, **kwargs):
        created.append(_FakeTask(coro))
        coro.close()               # 别真跑无限循环，也别留 unawaited 警告
        return created[-1]

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    return created


# ── 时间表算法（backend/panel/scheduler.py） ───────────────────────────────

def test_parse_clock_accepts_common_forms():
    assert sched.parse_clock("09:30") == (9, 30)
    assert sched.parse_clock("9:05") == (9, 5)
    assert sched.parse_clock(" 23:59 ") == (23, 59)


@pytest.mark.parametrize("bad", ["", None, "24:00", "09:60", "0930", "09:30:00", "九点", "9-30"])
def test_parse_clock_rejects_bad_input(bad):
    assert sched.parse_clock(bad) is None


def test_normalize_update_schedule_keeps_good_values():
    got = sched.normalize_update_schedule(
        {"enabled": True, "rule": weekly_rule([6], "7:5"), "auto_update": True}
    )
    assert got == {
        "enabled": True, "auto_update": True,
        "rule": {"mode": "weekly", "weekdays": [6], "monthdays": [], "times": ["07:05"]},
    }


def test_normalize_update_schedule_falls_back_on_bad_values():
    got = sched.normalize_update_schedule(
        {"enabled": "yes", "rule": {"mode": "hourly", "times": []}, "auto_update": "yes"}
    )
    assert got == {
        "enabled": True, "auto_update": True,
        "rule": sched.default_update_schedule()["rule"],
    }
    assert sched.normalize_update_schedule(None) == sched.default_update_schedule()


def test_old_config_turns_into_the_new_time_table():
    """老版本只存了 {mode, time, weekday}：读回来要照旧是「每天 09:00」这种设置。"""
    got = sched.normalize_update_schedule({"enabled": True, "mode": "daily", "time": "7:5"})
    assert got["rule"] == daily_rule("07:05")

    got = sched.normalize_update_schedule(
        {"enabled": True, "mode": "WEEKLY", "time": "21:30", "weekday": 4}
    )
    assert got["rule"] == weekly_rule([4], "21:30")


def test_auto_update_is_off_by_default():
    """自动更新默认关：老配置里没有这个字段时，行为必须和以前一模一样。"""
    assert sched.default_update_schedule()["auto_update"] is False
    assert sched.normalize_update_schedule({"enabled": True, "rule": daily_rule("09:00")})[
        "auto_update"] is False
    raw = sched.normalize_update_schedule({"mode": "daily", "time": "09:00"})
    assert raw["auto_update"] is False and raw["rule"] == daily_rule("09:00")
    assert sched.normalize_update_schedule({"auto_update": "yes"})["auto_update"] is True


def test_next_run_daily_picks_the_nearest_time_left_today():
    """同一天两个时间点：挑最近的那个，不是「今天第一个」也不是「明天第一个」。"""
    now = datetime(2026, 10, 2, 12, 0)
    got = sched.next_simple_rule_run(daily_rule("08:00", "20:00"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 2, 20, 0)


def test_next_run_daily_rolls_over_to_tomorrow():
    now = datetime(2026, 10, 2, 21, 0)
    got = sched.next_simple_rule_run(daily_rule("08:00", "20:00"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 3, 8, 0)


def test_next_run_skips_the_moment_it_just_ran():
    """正好是那一分钟：算出来的是「下一次」，不然循环会原地转圈。"""
    now = datetime(2026, 10, 2, 9, 0)
    got = sched.next_simple_rule_run(daily_rule("09:00", "20:00"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 2, 20, 0)


def test_next_run_supports_four_checks_a_day():
    now = datetime(2026, 10, 2, 9, 30)
    got = sched.next_simple_rule_run(daily_rule("00:00", "06:00", "12:00", "18:00"),
                                     now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 2, 12, 0)


def test_next_run_weekly_picks_the_chosen_weekday():
    # 2026-10-02 是周五；设的是每周一 07:30 → 下周一
    now = datetime(2026, 10, 2, 12, 0)
    got = sched.next_simple_rule_run(weekly_rule([0], "07:30"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 5, 7, 30)


def test_next_run_weekly_can_be_today():
    now = datetime(2026, 10, 2, 6, 0)          # 周五早上，当天 21:00 还没到
    got = sched.next_simple_rule_run(weekly_rule([4], "21:00"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 2, 21, 0)


def test_next_run_weekly_takes_the_first_of_several_days():
    # 周五下午设的「每周一、周三 09:00」→ 下周一（周三在周一前面，但更远的那次排在后面）
    now = datetime(2026, 10, 2, 15, 0)
    got = sched.next_simple_rule_run(weekly_rule([0, 2], "09:00"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 5, 9, 0)


def test_next_run_monthly_supports_two_days_a_month():
    now = datetime(2026, 10, 2, 12, 0)
    got = sched.next_simple_rule_run(monthly_rule([1, 15], "09:00"), now=now.timestamp())
    assert datetime.fromtimestamp(got) == datetime(2026, 10, 15, 9, 0)


def test_next_run_is_none_for_a_broken_rule():
    """规则读不出来（比如时间点全丢了）时不给时间，否则会排到「很久以后」这种怪时刻。"""
    assert sched.next_simple_rule_run({"mode": "daily", "times": []}) is None
    assert sched.next_simple_rule_run({"mode": "weekly", "weekdays": [], "times": ["09:00"]}) is None
    assert sched.next_simple_rule_run(None) is None


def test_simple_rule_text_says_it_in_words():
    assert sched.simple_rule_text(daily_rule("09:00")) == "每天 09:00"
    assert sched.simple_rule_text(daily_rule("08:00", "20:00")) == "每天 08:00、20:00"
    assert sched.simple_rule_text(weekly_rule([0, 3], "21:30")) == "每周一、周四 21:30"
    assert sched.simple_rule_text(monthly_rule([1], "06:30")) == "每月1日 06:30"
    assert sched.simple_rule_text({"mode": "daily", "times": []}) == ""


# ── 接口：保存设置 ────────────────────────────────────────────────────────

def test_set_update_schedule_persists_and_schedules(config_path, monkeypatch):
    tasks = record_tasks(monkeypatch)

    result = asyncio.run(cp.set_update_schedule(cp.UpdateScheduleRequest(
        enabled=True, rule=weekly_rule([0, 3], "21:30", "23:30"), auto_update=True,
    )))

    assert result["status"] == "enabled"
    assert result["schedule"] == {
        "enabled": True, "rule": weekly_rule([0, 3], "21:30", "23:30"), "auto_update": True,
    }
    assert result["rule"] == weekly_rule([0, 3], "21:30", "23:30")
    assert result["text"] == "每周一、周四 21:30、23:30"
    assert result["next_run"] is not None
    assert cp._update_schedule_state["task"] is not None
    assert len(tasks) == 1, "开启后应该有后台任务在等"
    # 落盘：重启服务靠它恢复
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["update_schedule"] == result["schedule"]


def test_set_update_schedule_normalises_the_rule(config_path, monkeypatch):
    """勾选框送来的值可能带脏东西（顺序、重复、缺前导零），存之前要收拾干净。"""
    record_tasks(monkeypatch)
    result = asyncio.run(cp.set_update_schedule(cp.UpdateScheduleRequest(
        enabled=True,
        rule={"mode": "DAILY", "weekdays": [3, 1], "monthdays": [],
              "times": ["20:00", "8:00", "20:00"]},
    )))
    assert result["rule"] == daily_rule("08:00", "20:00")
    assert result["text"] == "每天 08:00、20:00"


def test_set_update_schedule_disable_cancels_the_task(config_path, monkeypatch):
    tasks = record_tasks(monkeypatch)
    asyncio.run(cp.set_update_schedule(
        cp.UpdateScheduleRequest(enabled=True, rule=daily_rule("09:00"))))
    result = asyncio.run(cp.set_update_schedule(
        cp.UpdateScheduleRequest(enabled=False, rule=daily_rule("09:00"))))

    assert result["status"] == "disabled"
    assert tasks[0].cancelled is True
    assert cp._update_schedule_state["enabled"] is False
    assert cp._update_schedule_state["task"] is None
    assert cp._update_schedule_state["next_run"] is None
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["update_schedule"]["enabled"] is False


@pytest.mark.parametrize("rule, expected", [
    # 一个时间点都没勾：存下去就成了「永远不检查」，必须拦下来
    ({"mode": "daily", "times": []}, "执行时间"),
    # 同一天多个时间点的分钟数不一样：cron 表达不了，只能请用户统一
    ({"mode": "daily", "times": ["09:00", "09:30"]}, "分钟数"),
    ({"mode": "weekly", "weekdays": [], "times": ["09:00"]}, "星期几"),
    ({"mode": "monthly", "monthdays": [], "times": ["09:00"]}, "日期"),
    ({"mode": "hourly", "times": ["09:00"]}, "重复方式"),
])
def test_set_update_schedule_rejects_bad_rule(config_path, monkeypatch, rule, expected):
    record_tasks(monkeypatch)

    response = asyncio.run(cp.set_update_schedule(
        cp.UpdateScheduleRequest(enabled=True, rule=rule)))

    assert response.status_code == 400
    assert expected in json.loads(response.body)["error"]
    assert cp._update_schedule_state["enabled"] is False, "拦下来的请求不许改状态"
    assert not config_path.exists(), "拦下来的请求不许写配置文件"


def test_get_update_schedule_reports_state(config_path):
    cp._update_schedule_state.update(
        {"enabled": True, "rule": weekly_rule([1, 4], "07:15"), "auto_update": True}
    )

    payload = asyncio.run(cp.get_update_schedule())

    assert payload["schedule"] == {
        "enabled": True, "rule": weekly_rule([1, 4], "07:15"), "auto_update": True,
    }
    assert payload["rule"] == weekly_rule([1, 4], "07:15")
    assert payload["text"] == "每周二、周五 07:15"
    assert payload["next_run"] is None       # 没排任务时没有下次时间
    assert payload["last_result"] is None
    assert payload["running"] is False


def test_update_status_exposes_schedule(config_path, monkeypatch):
    """面板每 4 秒读一次更新状态，定时检查的情况跟着它一起刷新。"""
    class _Cursor:
        def fetchone(self): return (0,)
        def fetchall(self): return []

    class _Conn:
        def execute(self, *args, **kwargs): return _Cursor()
        def close(self): pass

    monkeypatch.setattr(cp.database, "get_stats", lambda: {"conversations": 0, "messages": 0, "users": 0})
    monkeypatch.setattr("backend.database.get_db", lambda: _Conn())
    cp._update_schedule_state.update({"enabled": True, "rule": daily_rule("06:00", "18:00")})

    payload = asyncio.run(cp.update_status())

    assert payload["schedule"]["schedule"]["enabled"] is True
    assert payload["schedule"]["rule"] == daily_rule("06:00", "18:00")
    assert payload["schedule"]["text"] == "每天 06:00、18:00"


# ── 启动恢复 ──────────────────────────────────────────────────────────────

def test_restore_update_schedule_on_startup(config_path, monkeypatch):
    tasks = record_tasks(monkeypatch)
    config_path.write_text(json.dumps({"update_schedule": {
        "enabled": True, "rule": daily_rule("07:30", "19:30"),
    }}), encoding="utf-8")

    asyncio.run(cp.restore_update_schedule_on_startup())

    assert cp._update_schedule_state["enabled"] is True
    assert cp._update_schedule_state["rule"] == daily_rule("07:30", "19:30")
    assert cp._update_schedule_state["next_run"] is not None
    assert len(tasks) == 1


def test_restore_update_schedule_accepts_an_old_config(config_path, monkeypatch):
    """升级前存的「每周三 09:00」也要能恢复，不能因为认不出新字段就变成「没设置」。"""
    tasks = record_tasks(monkeypatch)
    config_path.write_text(json.dumps({"update_schedule": {
        "enabled": True, "mode": "weekly", "time": "07:30", "weekday": 2,
    }}), encoding="utf-8")

    asyncio.run(cp.restore_update_schedule_on_startup())

    assert cp._update_schedule_state["enabled"] is True
    assert cp._update_schedule_state["rule"] == weekly_rule([2], "07:30")
    assert cp._update_schedule_state["next_run"] is not None
    assert len(tasks) == 1


def test_restore_update_schedule_does_nothing_without_config(config_path, monkeypatch):
    tasks = record_tasks(monkeypatch)

    asyncio.run(cp.restore_update_schedule_on_startup())

    assert tasks == []
    assert cp._update_schedule_state["enabled"] is False
    assert cp._update_schedule_state["task"] is None


# ── 到点之后到底做了什么 ──────────────────────────────────────────────────

def test_scheduled_check_records_result_and_notifies(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_collect(*, refresh=False):
        assert refresh is True, "定时检查要绕过 30 秒缓存，否则永远看到旧结果"
        return {
            "ok": True, "update_available": True, "current_version": "1.2.4",
            "remote_version": "1.2.5", "behind": 1,
            "versions": [
                {"version": "1.2.5", "subject": "定时检查更新"},
                {"version": "1.2.5", "subject": "顺带修一处笔误"},
            ],
        }

    async def fake_notify(title, desp):
        sent.append((title, desp))

    monkeypatch.setattr(cp, "_collect_update", fake_collect)
    monkeypatch.setattr(cp, "_notify_new_version", fake_notify)

    result = asyncio.run(cp._run_update_check_once())

    assert result["ok"] is True
    assert cp._update_schedule_state["last_run"] is not None
    assert cp._update_schedule_state["last_result"] == {
        "ok": True, "update_available": True, "current_version": "1.2.4",
        "remote_version": "1.2.5", "behind": 1, "error": "",
        # 自动更新的开关关着：这一栏是空的，行为跟以前一样（只通知）
        "auto_update": None,
    }
    assert cp._update_schedule_state["running"] is False
    assert len(sent) == 1
    title, desp = sent[0]
    assert "发现新版本 v1.2.5" in title
    assert "**当前版本**：v1.2.4" in desp
    assert "- v1.2.5：定时检查更新 ／ 顺带修一处笔误" in desp
    assert "立即更新" in desp


def test_scheduled_check_is_quiet_when_up_to_date(monkeypatch):
    sent: list = []

    async def fake_collect(*, refresh=False):
        return {"ok": True, "update_available": False, "current_version": "1.2.4",
                "remote_version": "1.2.4", "behind": 0, "versions": []}

    async def fake_notify(title, desp):
        sent.append(title)

    monkeypatch.setattr(cp, "_collect_update", fake_collect)
    monkeypatch.setattr(cp, "_notify_new_version", fake_notify)

    asyncio.run(cp._run_update_check_once())

    assert sent == [], "没有新版本就不要打扰用户"
    assert cp._update_schedule_state["last_result"]["update_available"] is False


def test_scheduled_check_records_failure_instead_of_raising(monkeypatch):
    """网络不通、Token 失效都只是「这次没查到」：记下来给面板看，循环不能死。"""
    async def fake_collect(*, refresh=False):
        raise cp._UpdateCheckError("network")

    monkeypatch.setattr(cp, "_collect_update", fake_collect)

    result = asyncio.run(cp._run_update_check_once())

    assert result["ok"] is False
    last = cp._update_schedule_state["last_result"]
    assert last["ok"] is False
    assert "连不上 GitHub" in last["error"]


def test_update_schedule_loop_runs_once_then_stops(monkeypatch):
    ran: list = []
    # 第一次算出「马上要跑」，跑完再算时说明设置已经关掉 → 循环退出
    plan = [time.time() + 0.01, None]

    async def fake_once():
        ran.append(1)

    monkeypatch.setattr(cp, "_next_update_run", lambda schedule: plan.pop(0) if plan else None)
    monkeypatch.setattr(cp, "_run_update_check_once", fake_once)
    monkeypatch.setattr(cp, "_UPDATE_SCHEDULE_SETTLE_SECONDS", 0)

    asyncio.run(cp._update_schedule_loop())

    assert ran == [1]
    assert cp._update_schedule_state["next_run"] is None


def test_update_versions_text_groups_one_version_per_line():
    text = cp._update_versions_text({"versions": [
        {"version": "1.2.5", "subject": "第二件事"},
        {"version": "1.2.5", "subject": "第一件事"},
        {"version": "1.2.4", "subject": "更早的事"},
    ]})
    assert text.splitlines() == ["- v1.2.5：第二件事 ／ 第一件事", "- v1.2.4：更早的事"]


def test_update_schedule_text_says_it_in_words():
    assert cp._update_schedule_text({"enabled": True, "rule": daily_rule("09:00")}) == "每天 09:00"
    assert cp._update_schedule_text(
        {"enabled": True, "mode": "weekly", "time": "21:30", "weekday": 2}
    ) == "每周三 21:30"
    assert cp._update_schedule_text(
        {"enabled": True, "rule": daily_rule("08:00", "12:00", "20:00")}
    ) == "每天 08:00、12:00、20:00"


# ── 面板页面上的接线 ──────────────────────────────────────────────────────

def test_panel_html_wires_the_update_schedule():
    """面板那一半也钉住：元素、入口函数、中英文案缺一个都点不成。

    时间是「勾选式」的：一天里能勾好几个整点（.sched-picks），重复方式跟「定时」页
    一样有每天 / 每周 / 每月三档（用按钮点开弹窗来选），分钟是常用分钟那排勾选框 +
    「自定义」弹窗，所以面板上必须真的长出这些元素。
    """
    html = open(PANEL_HTML, encoding="utf-8").read()

    for element in ('id="updateSchEnabled"', 'id="updateSchModeBtn"', 'id="updateSchSaveBtn"',
                    'id="updateSchMeta"', 'id="updateSchSummary"', 'id="updateSchHours"',
                    'id="updateSchMinutePicks"', 'id="updateSchMinuteBtn"',
                    'id="updateSchWeekdays"', 'id="updateSchMonthdays"',
                    'id="updateSchWeekdayLine"', 'id="updateSchMonthdayLine"',
                    'id="schedPickModal"', 'id="schedPickGrid"'):
        assert element in html, element
    # 重复方式：按钮 + 弹窗；分钟：常用分钟勾选框 + 「自定义」弹窗（两处时间表各一套）
    assert 'onclick="openSchedPick(\'mode\', \'update\')"' in html
    assert 'onclick="openSchedPick(\'minute\', \'update\')"' in html
    assert 'id="updateSchMode"' not in html.replace('id="updateSchModeBtn"', '')
    assert 'id="updateSchMinute"' not in html.replace('id="updateSchMinuteBtn"', '')
    assert "onSchedMinutePickChange(m, ids)" in html
    assert 'onclick="saveUpdateSchedule()"' in html
    assert "/panel/api/update/schedule" in html
    # 保存时送的是整理好的规则，不再是一个 "HH:MM"
    assert "rule: schedUIToRule(updateSchUI)" in html
    assert "auto_update: autoUpdate" in html
    # 进入「关于」页读一次设置；状态轮询里刷新提示行（不覆盖用户正在改的值）
    assert "loadUpdateSchedule();" in html
    assert "updateScheduleData = data.schedule;" in html
    assert "renderUpdateScheduleInfo(updateScheduleData)" in html
    # 两处时间表共用同一批画勾选的零件（一份规则不写两遍）
    assert "buildSchedulePicks(SCHED_UPDATE_IDS);" in html
    assert "renderSchedUI(updateSchUI, { ...SCHED_UPDATE_IDS, summaryKind: 'check' }, dirty);" in html
    assert "SCHED_UPDATE_IDS" in html
    # 中英文案都要有：漏了英文，切到英文界面就会直接显示 key
    for key in ("aboutAutoCheckTitle", "aboutAutoCheckNote", "aboutAutoCheckOff",
                "aboutAutoCheckNext", "aboutAutoCheckFound", "aboutAutoCheckLatest",
                "aboutAutoCheckFailed", "aboutAutoCheckSaveFailed",
                "aboutAutoCheckSummaryDaily", "aboutAutoCheckSummaryWeekly",
                "aboutAutoCheckSummaryMonthly", "aboutAutoCheckPresetDailyThree",
                "weekday0", "weekday6"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
    # 「重复方式」那三档现在借用「定时」页的文案，两边都得有中英文各一份
    for key in ("schedDaily", "schedWeekly", "schedMonthly"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"


def test_panel_html_has_no_leftover_single_time_picker():
    """老的单点时间选择（隐藏输入 + 弹窗）已经拆掉：留着会让人以为还能设一个时间。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    for gone in ('id="updateSchTime"', 'id="updateTimeModal"', "openUpdateTimePicker",
                 "renderUpdateScheduleTimeField", "aboutAutoCheckDailyAt"):
        assert gone not in html, gone
