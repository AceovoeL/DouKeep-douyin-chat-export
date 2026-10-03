"""面板「定时」一栏：勾选式时间表的接口 + 页面接线。

用户在页面上勾的是「每天 / 每周 / 每月 + 星期几或日期 + 几个时间点」，换算成
cron 的纯函数在 backend/panel/scheduler.py（用例在 test_panel_scheduler.py）。
这里管两件事：接口收不收这份「勾选」、面板页面上那些复选框和入口函数在不在。
"""
import asyncio
import json
import os

import pytest

from backend import control_panel as cp
from common import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")


# ── 夹具 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """把 panel_config.json 指到临时目录：cron 要真的写盘、真的读回来。"""
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def clean_scheduler_state(monkeypatch):
    """每个用例都从「刚开机、还没排任务」的状态开始，结束自动还原。"""
    monkeypatch.setattr(cp, "_scheduler_state", {
        "enabled": False, "schedule": "", "task": None, "next_run": None,
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
    created: list[_FakeTask] = []

    def fake_create_task(coro, **kwargs):
        created.append(_FakeTask(coro))
        coro.close()               # 别真跑无限循环，也别留 unawaited 警告
        return created[-1]

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    return created


class _Cursor:
    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


class _Conn:
    def execute(self, *args, **kwargs):
        return _Cursor()

    def close(self):
        pass


def stub_database(monkeypatch):
    """面板状态接口要读数据库，这里给个空库。"""
    monkeypatch.setattr(cp.database, "get_stats",
                        lambda: {"conversations": 0, "messages": 0, "users": 0})
    monkeypatch.setattr("backend.database.get_db", lambda: _Conn())


# ── 接口：勾选 → cron ─────────────────────────────────────────────────────

def test_set_schedule_turns_picks_into_cron(config_path, monkeypatch):
    tasks = record_tasks(monkeypatch)

    result = asyncio.run(cp.set_schedule(cp.ScheduleRequest(
        enabled=True,
        rule={"mode": "weekly", "weekdays": [0, 3], "monthdays": [], "times": ["08:00", "20:00"]},
    )))

    assert result["status"] == "enabled"
    assert result["cron"] == "0 8,20 * * 1,4", "「每周一、周四早 8 点 + 晚 8 点」"
    assert result["next_run"] is not None
    assert cp._scheduler_state["enabled"] is True
    assert len(tasks) == 1, "开启后应该有后台任务在等"
    # 落盘：重启服务靠它恢复
    assert json.loads(config_path.read_text(encoding="utf-8"))["schedule"] == "0 8,20 * * 1,4"


def test_set_schedule_ignores_cron_when_the_panel_sends_picks(config_path, monkeypatch):
    """面板同时带了 rule 和 cron 时以勾选为准，别让两条路打架。"""
    record_tasks(monkeypatch)

    result = asyncio.run(cp.set_schedule(cp.ScheduleRequest(
        enabled=True, cron="0 3 * * *",
        rule={"mode": "daily", "weekdays": [], "monthdays": [], "times": ["20:00"]},
    )))

    assert result["cron"] == "0 20 * * *"


@pytest.mark.parametrize("rule, expected", [
    ({"mode": "weekly", "weekdays": [], "times": ["20:00"]}, "至少勾选一个星期几"),
    ({"mode": "monthly", "monthdays": [], "times": ["20:00"]}, "至少勾选一个日期"),
    ({"mode": "daily", "times": []}, "至少勾选一个执行时间"),
    ({"mode": "hourly", "times": ["08:00"]}, "重复方式"),
])
def test_set_schedule_rejects_incomplete_picks(config_path, monkeypatch, rule, expected):
    record_tasks(monkeypatch)

    response = asyncio.run(cp.set_schedule(cp.ScheduleRequest(enabled=True, rule=rule)))

    assert response.status_code == 400
    assert expected in json.loads(response.body)["error"]
    assert cp._scheduler_state["enabled"] is False, "拦下来的请求不许改状态"
    assert not config_path.exists(), "拦下来的请求不许写配置文件"


def test_a_bad_edit_does_not_stop_a_running_schedule(config_path, monkeypatch):
    """勾错一项不该把正在跑的定时采集弄停：校验要在动状态之前。"""
    tasks = record_tasks(monkeypatch)
    asyncio.run(cp.set_schedule(cp.ScheduleRequest(
        enabled=True, rule={"mode": "daily", "times": ["20:00"]})))

    response = asyncio.run(cp.set_schedule(cp.ScheduleRequest(
        enabled=True, rule={"mode": "weekly", "weekdays": [], "times": ["20:00"]})))

    assert response.status_code == 400
    assert tasks[0].cancelled is False, "旧任务还在跑"
    assert cp._scheduler_state["enabled"] is True
    assert cp._scheduler_state["schedule"] == "0 20 * * *"


def test_advanced_cron_path_still_works(config_path, monkeypatch):
    """勾选框表达不了的（每 6 小时之类）还能从高级输入框进来。"""
    record_tasks(monkeypatch)

    result = asyncio.run(cp.set_schedule(cp.ScheduleRequest(enabled=True, cron="0 */6 * * *")))

    assert result["cron"] == "0 */6 * * *"
    assert cp._scheduler_state["schedule"] == "0 */6 * * *"


def test_enabled_without_any_schedule_is_rejected(config_path, monkeypatch):
    """开了开关却什么都没选：当面说清楚，别默默存个空的。"""
    record_tasks(monkeypatch)

    response = asyncio.run(cp.set_schedule(cp.ScheduleRequest(enabled=True, cron="")))

    assert response.status_code == 400
    assert "cron" in json.loads(response.body)["error"]
    assert not config_path.exists()


def test_invalid_cron_in_advanced_box_is_rejected(config_path, monkeypatch):
    record_tasks(monkeypatch)

    response = asyncio.run(cp.set_schedule(cp.ScheduleRequest(enabled=True, cron="0 8 * *")))

    assert response.status_code == 400
    assert "cron" in json.loads(response.body)["error"]
    assert not config_path.exists()


def test_disabling_clears_the_saved_cron(config_path, monkeypatch):
    tasks = record_tasks(monkeypatch)
    asyncio.run(cp.set_schedule(cp.ScheduleRequest(
        enabled=True, rule={"mode": "daily", "times": ["20:00"]})))

    result = asyncio.run(cp.set_schedule(cp.ScheduleRequest(
        enabled=False, rule={"mode": "daily", "times": ["20:00"]})))

    assert result == {"status": "disabled"}
    assert tasks[0].cancelled is True
    assert cp._scheduler_state["schedule"] == ""
    assert json.loads(config_path.read_text(encoding="utf-8"))["schedule"] == ""


# ── 接口：状态里回一份能画的勾选 ──────────────────────────────────────────

def test_panel_status_gives_the_panel_picks_to_draw(config_path, monkeypatch):
    stub_database(monkeypatch)
    cp._scheduler_state.update({"enabled": True, "schedule": "0 20 1,15 * *"})

    payload = asyncio.run(cp.panel_status())

    assert payload["scheduler"]["schedule"] == "0 20 1,15 * *"
    assert payload["scheduler"]["rule"] == {
        "mode": "monthly", "weekdays": [], "monthdays": [1, 15], "times": ["20:00"],
    }


def test_panel_status_marks_a_custom_cron_as_undrawable(config_path, monkeypatch):
    """画不出来的表达式给 None，页面会打开高级输入框并说明情况。"""
    stub_database(monkeypatch)
    cp._scheduler_state.update({"enabled": True, "schedule": "0 */6 * * *"})

    payload = asyncio.run(cp.panel_status())

    assert payload["scheduler"]["schedule"] == "0 */6 * * *"
    assert payload["scheduler"]["rule"] is None


# ── 面板页面上的接线 ──────────────────────────────────────────────────────

def test_panel_html_has_the_checkbox_schedule_ui():
    """面板那一半也钉住：元素、入口函数、中英文案缺一个都点不成。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    for element in ('id="schedWeekdays"', 'id="schedMonthdays"', 'id="schedHours"',
                    'id="schedMinutePicks"', 'id="schedMinuteBtn"', 'id="schedSummary"',
                    'id="schedDirtyTag"',
                    'id="schedWeekdayLine"', 'id="schedMonthdayLine"',
                    'id="schedAdvancedBody"', 'id="schedAdvancedNote"',
                    'id="schedModeBtn"', 'id="schedPickModal"', 'id="schedPickGrid"'):
        assert element in html, element

    # 「重复方式」是按钮点开弹窗；「分钟」是摊在页面上的常用分钟勾选框 + 一个「自定义」弹窗
    assert 'onclick="openSchedPick(\'mode\', \'collect\')"' in html
    assert 'onclick="openSchedPick(\'minute\', \'collect\')"' in html
    assert "const SCHED_MINUTE_COMMON = [0, 5, 10, 15, 20, 30, 45, 50];" in html
    assert "buildSchedMinutePicks(ids)" in html
    # 分钟只能有一个值：那排勾选框必须是单选（radio），点第二个会把第一个的勾去掉
    assert "input.type = 'radio';" in html
    assert 'id="schedModeDaily"' not in html
    assert 'id="schedMinute"' not in html.replace('id="schedMinuteBtn"', '')

    # 主入口是「应用勾选」，原来那五个手写输入框挪进「高级」里，不再是页面主体
    assert 'onclick="applySchedule()"' in html
    assert 'onclick="applyCronExpr()"' in html
    assert 'onclick="updateSchedule()"' not in html
    assert 'cron-preset" onclick="setCron(' not in html
    assert 'id="schedAdvancedBody" hidden' in html, "高级输入框默认要收起来"

    # 常用规则一键填好（对应用户最常说的那几种）
    for preset in ('applySchedPreset(\'dailyNight\')', 'applySchedPreset(\'dailyTwice\')',
                   'applySchedPreset(\'mondayNight\')', 'applySchedPreset(\'monThuNight\')',
                   'applySchedPreset(\'day1Night\')', 'applySchedPreset(\'day1and15\')'):
        assert preset in html, preset

    # 前端只提交勾选，不自己拼表达式；状态回来时按服务端的 rule 重画
    assert "rule: schedUIToRule()" in html
    assert "renderScheduleFromServer(sch)" in html
    assert "onSchedPickChange" in html
    # 用户正在勾选时，5 秒一次的状态轮询不许把勾好的东西冲掉
    assert "if (!schedDirty)" in html

    # 中英文案都要有：漏了英文，切到英文界面就会直接显示 key
    for key in ("schedEnabledLabel", "schedDirty", "scheduleDesc", "schedRepeat", "schedDaily",
                "schedWeekly", "schedMonthly", "schedWeekdays", "schedMonthdays", "schedTimes",
                "schedMinute", "schedMinuteHint", "schedMinuteOption", "schedHourLabel",
                "schedMinutePick", "schedMinuteCustom", "schedMinuteCustomValue",
                "schedRepeatPick", "schedPickNow",
                "schedTimeWithMinute", "schedDayLabel", "schedWorkdays", "schedWeekend",
                "schedDay1", "schedDay1and15", "schedPresets", "schedPresetDailyNight",
                "schedPresetDailyTwice", "schedPresetMondayNight", "schedPresetMonThuNight",
                "schedPresetDay1Night", "schedPresetDay1and15", "schedSummaryDaily",
                "schedSummaryWeekly", "schedSummaryMonthly", "schedNeedHours",
                "schedNeedWeekdays", "schedNeedMonthdays", "schedAdvanced", "schedApplyCron",
                "schedCustomCron", "schedSavedOff"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
