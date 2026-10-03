"""Unit tests for the extracted cron engine (backend/panel/scheduler.py)."""
from datetime import datetime, timedelta

import pytest

from backend.panel import scheduler as s


def test_parse_cron_valid_and_invalid():
    assert s.parse_cron("0 6 * * *") is not None
    assert s.parse_cron("bad") is None          # wrong field count
    assert s.parse_cron("0 6 * *") is None       # 4 fields
    assert s.parse_cron("99 6 * * *") is None     # minute out of range -> empty set


def test_expand_field_forms():
    assert s.expand_cron_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}
    assert s.expand_cron_field("*/2", 0, 6) == {0, 2, 4, 6}
    assert s.expand_cron_field("1-3", 0, 9) == {1, 2, 3}
    assert s.expand_cron_field("1,3,5", 0, 9) == {1, 3, 5}
    assert s.expand_cron_field("2-8/3", 0, 10) == {2, 5, 8}


def test_convert_dow_sunday_zero_to_python():
    # cron 0=Sunday -> python weekday 6; cron 1=Monday -> python 0
    assert s.convert_dow({0}) == {6}
    assert s.convert_dow({1}) == {0}
    assert s.convert_dow({0, 1, 6}) == {6, 0, 5}


def test_next_cron_run_every_minute_is_next_minute():
    parsed = s.parse_cron("* * * * *")
    ts = s.next_cron_run(parsed)
    expected = (datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1))
    # within a couple minutes of the next minute boundary
    assert abs(ts - expected.timestamp()) <= 120


def test_next_cron_run_specific_hour_in_future():
    parsed = s.parse_cron("0 6 * * *")  # 06:00 daily
    ts = s.next_cron_run(parsed)
    dt = datetime.fromtimestamp(ts)
    assert dt.hour == 6 and dt.minute == 0
    assert ts > datetime.now().timestamp()


# ── 勾选式时间表 ⇄ cron（面板「定时」一栏用） ─────────────────────────────
#
# 下面这 8 组就是面板上最常用的那几种说法，cron 那列是「用户勾出来的效果」。

@pytest.mark.parametrize("rule, cron", [
    # 每周一晚上八点
    ({"mode": "weekly", "weekdays": [0], "times": ["20:00"]}, "0 20 * * 1"),
    # 每月1日晚上八点
    ({"mode": "monthly", "monthdays": [1], "times": ["20:00"]}, "0 20 1 * *"),
    # 每天晚上八点
    ({"mode": "daily", "times": ["20:00"]}, "0 20 * * *"),
    # 每天早上八点和晚上八点
    ({"mode": "daily", "times": ["08:00", "20:00"]}, "0 8,20 * * *"),
    # 每月1日、15日晚上八点
    ({"mode": "monthly", "monthdays": [1, 15], "times": ["20:00"]}, "0 20 1,15 * *"),
    # 每月1日、15日早上八点和晚上八点
    ({"mode": "monthly", "monthdays": [1, 15], "times": ["08:00", "20:00"]}, "0 8,20 1,15 * *"),
    # 每周一和周四晚上八点
    ({"mode": "weekly", "weekdays": [0, 3], "times": ["20:00"]}, "0 20 * * 1,4"),
    # 每周一和周四早上八点和晚上八点
    ({"mode": "weekly", "weekdays": [0, 3], "times": ["08:00", "20:00"]}, "0 8,20 * * 1,4"),
])
def test_simple_rule_to_cron_covers_the_common_rules(rule, cron):
    assert s.simple_rule_to_cron(rule) == cron
    # 反解回来要和原来一样（面板刷新后勾选不会跳）
    assert s.cron_to_simple_rule(cron) == {
        "mode": rule["mode"],
        "weekdays": sorted(rule.get("weekdays", [])),
        "monthdays": sorted(rule.get("monthdays", [])),
        "times": sorted(rule["times"]),
    }


def test_simple_rule_to_cron_sorts_and_dedupes():
    """勾选顺序不该影响存下来的表达式；重复项要去掉。"""
    rule = {"mode": "weekly", "weekdays": [3, 0, 3], "times": ["20:00", "8:00", "08:00"]}
    assert s.simple_rule_to_cron(rule) == "0 8,20 * * 1,4"


def test_simple_rule_keeps_non_zero_minutes():
    """分钟不是 0 也要能表达（所有时间点共用同一个分钟）。"""
    rule = {"mode": "daily", "times": ["07:30", "21:30"]}
    assert s.simple_rule_to_cron(rule) == "30 7,21 * * *"
    assert s.cron_to_simple_rule("30 7,21 * * *")["times"] == ["07:30", "21:30"]


def test_cron_to_simple_rule_gives_up_on_expressions_it_cannot_draw():
    """画不成勾选框的要老实返回 None（页面会切到高级输入框，而不是瞎猜）。"""
    for cron in (
        "0 */6 * * *",     # 每 6 小时：小时是步长
        "* * * * *",       # 每分钟
        "0,30 8 * * *",    # 分钟不止一个
        "0 8 1 * 1",       # 日期和星期都有值（cron 里是「或」）
        "0 8 * 6 *",       # 限定月份
        "0 8-10 * * *",    # 小时是区间
        "0 8 * *",         # 段数不对
        "bad",             # 根本不是表达式
        "",                # 空
        None,              # 没配过
    ):
        assert s.cron_to_simple_rule(cron) is None, cron


@pytest.mark.parametrize("rule, expected", [
    ({"mode": "hourly", "times": ["08:00"]}, "重复方式"),
    ({"mode": "daily", "times": []}, "至少勾选一个执行时间"),
    ({"mode": "daily", "times": ["08:00", "20:30"]}, "分钟数要一样"),
    ({"mode": "weekly", "weekdays": [], "times": ["08:00"]}, "至少勾选一个星期几"),
    ({"mode": "monthly", "monthdays": [], "times": ["08:00"]}, "至少勾选一个日期"),
    ({"mode": "daily"}, "至少勾选一个执行时间"),
    (None, "格式不对"),
])
def test_simple_rule_problem_says_what_is_missing(rule, expected):
    """界面直接把这句话显示给用户，所以要能看懂、且指得出缺哪一项。"""
    assert expected in s.simple_rule_problem(rule)
    assert s.normalize_simple_rule(rule) is None
    assert s.simple_rule_to_cron(rule) is None


def test_simple_rule_problem_is_empty_for_good_rules():
    assert s.simple_rule_problem({"mode": "daily", "times": ["20:00"]}) == ""
