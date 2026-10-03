"""Homegrown 5-field cron parser + next-run finder for the control panel.

Pure functions (no shared state) — extracted from control_panel.py so they can
be unit-tested in isolation. The panel's cron avoids a croniter dependency and
supports the common subset: '*', '*/n', 'a-b', 'a-b/n', 'a,b,c'.

中间一段（normalize_update_schedule / next_simple_rule_run / simple_rule_text）是
「定时检查更新」用的时间表：它和「定时采集」共用同一套**勾选式时间表**（见下半部分），
也就是「每天 / 每周几 / 每月哪几天」+ 好多个时间点，同一天可以检查好几次。两边只在
「保存成什么」上不同：采集拼成 cron 存起来，检查更新直接把这份时间表存进配置。

下半部分（`simple_rule_*` / `cron_to_simple_rule`）是面板「定时采集」用的
**勾选式时间表**：用户在页面上勾「每天 / 每周 / 每月」+ 星期几或日期 + 几个
时间点，后端把它拼成 cron 存起来；读回来的时候再反解成勾选状态。换算全在这里
做，前端只负责画复选框，免得同样的规则在两处各写一遍、还各错一遍。
"""
import time
from datetime import datetime, timedelta

#: 「每周几」用的编号：0=周一 … 6=周日，就是 Python 的 datetime.weekday()。
#: （cron 是 0=周日的另一套编号，别把两者混着用。）
WEEKDAY_COUNT = 7

#: 默认的检查时间点（每天 09:00 和 20:00），配置缺失或写坏时用它兜底。
DEFAULT_UPDATE_TIMES = ("09:00", "20:00")


def parse_cron(expr: str) -> list | None:
    """Parse a 5-field cron expression. Returns list of 5 sets or None."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return None
    ranges = [
        (0, 59),   # minute
        (0, 23),   # hour
        (1, 31),   # day of month
        (1, 12),   # month
        (0, 6),    # day of week (0=Sun)
    ]
    result = []
    for field, (lo, hi) in zip(fields, ranges):
        try:
            values = expand_cron_field(field, lo, hi)
            if not values:
                return None
            result.append(values)
        except Exception:
            return None
    return result


def expand_cron_field(field: str, lo: int, hi: int) -> set:
    """Expand a single cron field like '*/5', '1,3,5', '0-12', '*'."""
    values = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                start = lo
            elif "-" in base:
                start = int(base.split("-")[0])
            else:
                start = int(base)
            for v in range(start, hi + 1, step):
                if lo <= v <= hi:
                    values.add(v)
        elif "-" in part:
            a, b = part.split("-", 1)
            for v in range(int(a), int(b) + 1):
                if lo <= v <= hi:
                    values.add(v)
        elif part == "*":
            values.update(range(lo, hi + 1))
        else:
            v = int(part)
            if lo <= v <= hi:
                values.add(v)
    return values


def next_cron_run(parsed: list, now: float | None = None) -> float:
    """Find next datetime matching the cron fields.

    ``now`` 是「从哪一刻往后找」（epoch 秒），不传就按此刻算。算出来的时刻总是
    **严格晚于** ``now``，所以刚好卡在某一分钟上不会被再算一次 —— 定时任务跑完
    立刻重排下一次时，靠的就是这一点，不然会原地转圈。
    """
    base = time.time() if now is None else now
    current = datetime.fromtimestamp(base)
    cursor = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes, hours, days, months, dow = parsed
    # Search up to 366 days ahead
    for _ in range(366 * 24 * 60):
        if (cursor.month in months and cursor.day in days and
                cursor.hour in hours and cursor.minute in minutes and
                cursor.weekday() in convert_dow(dow)):
            return cursor.timestamp()
        cursor += timedelta(minutes=1)
    return base + 86400  # fallback: 1 day


def convert_dow(cron_dow: set) -> set:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon)."""
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping.get(d, d) for d in cron_dow}


# ── 定时检查更新：和「定时采集」同一套勾选式时间表 ────────────────────────

def parse_clock(text) -> tuple[int, int] | None:
    """解析 ``"HH:MM"``（``"9:05"`` 这种缺前导零的也收）。不合法返回 None。"""
    parts = str(text or "").strip().split(":")
    if len(parts) != 2:
        return None
    hour, minute = parts
    if not (hour.isdigit() and minute.isdigit()):
        return None
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def default_update_schedule() -> dict:
    """一份全新的默认时间表：关闭、每天 09:00 和 20:00、不自动更新。"""
    return {
        "enabled": False,
        "rule": {"mode": "daily", "weekdays": [], "monthdays": [],
                 "times": list(DEFAULT_UPDATE_TIMES)},
        "auto_update": False,
    }


def legacy_update_rule(raw) -> dict | None:
    """老版本配置（``{mode, time, weekday}``，只能设一个时间点）→ 勾选式时间表。

    老字段表达的是「每天 09:00」或「每周三 21:30」，正好是新时间表的一个特例。
    读不出任何有用信息时返回 None（调用方走默认值）。
    """
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode") or "").strip().lower()
    clock = parse_clock(raw.get("time"))
    if clock is None:
        # 只有 weekday 没有 time 这种残缺配置：认不出老格式，交给默认值
        return None
    time_text = f"{clock[0]:02d}:{clock[1]:02d}"
    if mode == "weekly":
        try:
            weekday = int(raw.get("weekday"))
        except (TypeError, ValueError):
            return None
        if not 0 <= weekday < WEEKDAY_COUNT:
            return None
        return {"mode": "weekly", "weekdays": [weekday], "monthdays": [],
                "times": [time_text]}
    return {"mode": "daily", "weekdays": [], "monthdays": [], "times": [time_text]}


def normalize_update_schedule(raw) -> dict:
    """把配置（或请求）里的时间表收拾成固定形状。

    拿到坏值时退回默认，不抛异常：panel_config.json 是用户可以手改的文件，写坏了
    不该让服务起不来。真正要拦用户输入错的地方是接口那层（它会先校验再调用这里）。
    老版本那种 ``{mode, time, weekday}`` 会在这里被翻译成新的勾选式时间表，升级后
    原来设的「每天 09:00」照旧生效。
    """
    schedule = default_update_schedule()
    if not isinstance(raw, dict):
        return schedule
    schedule["enabled"] = bool(raw.get("enabled"))
    schedule["auto_update"] = bool(raw.get("auto_update"))
    # 规则一律过一遍 normalize_simple_rule：老配置是 {mode, time, weekday}，新配置是
    # {mode, weekdays, monthdays, times}，认不出来的就照默认那份来（不抛异常）。
    rule = normalize_simple_rule(raw.get("rule"))
    if rule is None:
        rule = legacy_update_rule(raw)
    if rule is not None:
        schedule["rule"] = rule
    return schedule


def next_simple_rule_run(rule, now: float | None = None) -> float | None:
    """这份勾选式时间表的下一次执行时刻（epoch 秒）；算不出来时返回 None。

    就是「拼成 cron 再用既有的 cron 计算」：同一天有多个时间点时，cron 的
    「分 时」两段本来就支持写成列表，所以一次就挑出最近的那个时刻，不用自己
    逐个时间点算再比大小。返回的秒数按本机时区解释（和面板显示的时间一致）。
    """
    if normalize_simple_rule(rule) is None:
        return None
    parsed = parse_cron(simple_rule_to_cron(rule) or "")
    if parsed is None:
        return None
    return next_cron_run(parsed, now=now)


def simple_rule_text(rule) -> str:
    """把勾选式时间表说成一句人话：「每天 09:00、20:00」「每周一、周四 21:30」。

    给日志、通知和面板提示行用。规则不合法时给空串（调用方自己决定显示什么）。
    """
    schedule = normalize_simple_rule(rule)
    if schedule is None:
        return ""
    times = "、".join(schedule["times"])
    if schedule["mode"] == "weekly":
        days = "、".join(f"周{_WEEKDAY_TEXT[d]}" for d in schedule["weekdays"])
        return f"每{days} {times}"
    if schedule["mode"] == "monthly":
        days = "、".join(f"{d}日" for d in schedule["monthdays"])
        return f"每月{days} {times}"
    return f"每天 {times}"


#: simple_rule_text 里「周几」用的字（0=周一）。
_WEEKDAY_TEXT = ("一", "二", "三", "四", "五", "六", "日")


# ── 定时采集：勾选式时间表 ⇄ cron 表达式 ──────────────────────────────────
#
# 面板上的样子（一份「简单时间表」）：
#   {"mode": "weekly", "weekdays": [0, 3], "monthdays": [], "times": ["20:00"]}
#   → 「每周一、周四 20:00」→ cron "0 20 * * 1,4"
# 注意两处编号不一样：这里的 weekdays 是 0=周一（Python 的 weekday()），cron 的
# 星期字段是 0=周日，换算时 +1（模 7）。

#: 勾选式时间表的三种重复方式。
SIMPLE_MODES = ("daily", "weekly", "monthly")


def _plain_int(field, lo: int, hi: int) -> int | None:
    """``"7"`` → 7；带 ``*``、``-``、``/`` 或超范围一律 None（那是高级写法）。"""
    text = str(field).strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if lo <= value <= hi else None


def _plain_int_list(field, lo: int, hi: int) -> list[int] | None:
    """``"1,4"`` → [1, 4]；只要有一段不是纯数字或超范围就整段作废。"""
    parts = [p.strip() for p in str(field).split(",")]
    values: list[int] = []
    for part in parts:
        value = _plain_int(part, lo, hi)
        if value is None:
            return None
        if value not in values:
            values.append(value)
    return sorted(values) or None


def clean_weekdays(raw) -> list[int]:
    """星期几：0=周一 … 6=周日（和 datetime.weekday() 一致）。不合法给空列表。"""
    if not isinstance(raw, (list, tuple)):
        return []
    values: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= value < WEEKDAY_COUNT and value not in values:
            values.append(value)
    return sorted(values)


def clean_monthdays(raw) -> list[int]:
    """每月哪几天：1-31。不合法给空列表。"""
    if not isinstance(raw, (list, tuple)):
        return []
    values: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 31 and value not in values:
            values.append(value)
    return sorted(values)


def clean_simple_times(raw) -> list[str]:
    """``["8:00", "20:00"]`` → ``["08:00", "20:00"]``。不合法给空列表。"""
    if not isinstance(raw, (list, tuple)):
        return []
    times: list[str] = []
    for item in raw:
        clock = parse_clock(item)
        if clock is None:
            continue
        text = f"{clock[0]:02d}:{clock[1]:02d}"
        if text not in times:
            times.append(text)
    return sorted(times)


def simple_rule_problem(rule) -> str:
    """用一句人话说明这份「简单时间表」哪里不对；没问题返回空串。

    面板拿这句话直接给用户看，所以别写英文、别写代码里的字段名。
    """
    if not isinstance(rule, dict):
        return "时间表格式不对"
    mode = str(rule.get("mode") or "").strip().lower()
    if mode not in SIMPLE_MODES:
        return "重复方式只能选「每天 / 每周 / 每月」"
    times = clean_simple_times(rule.get("times"))
    if not times:
        return "请至少勾选一个执行时间"
    if len({text[3:] for text in times}) > 1:
        return "多个执行时间的分钟数要一样（整点就都选 0 分）"
    if mode == "weekly" and not clean_weekdays(rule.get("weekdays")):
        return "选「每周」时请至少勾选一个星期几"
    if mode == "monthly" and not clean_monthdays(rule.get("monthdays")):
        return "选「每月」时请至少勾选一个日期"
    return ""


def normalize_simple_rule(rule) -> dict | None:
    """把「简单时间表」收拾成固定形状；不合法返回 None。"""
    if simple_rule_problem(rule):
        return None
    mode = str(rule.get("mode")).strip().lower()
    weekdays = clean_weekdays(rule.get("weekdays")) if mode == "weekly" else []
    monthdays = clean_monthdays(rule.get("monthdays")) if mode == "monthly" else []
    return {
        "mode": mode,
        "weekdays": weekdays,
        "monthdays": monthdays,
        "times": clean_simple_times(rule.get("times")),
    }


def simple_rule_to_cron(rule) -> str | None:
    """把「简单时间表」拼成 5 段 cron 表达式；不合法返回 None。"""
    schedule = normalize_simple_rule(rule)
    if schedule is None:
        return None
    minute = int(schedule["times"][0][3:])
    hours = sorted({int(text[:2]) for text in schedule["times"]})
    hour_field = ",".join(str(h) for h in hours)
    if schedule["mode"] == "weekly":
        # cron 的星期是 0=周日，比这里的 0=周一 早一天
        day_field = "*"
        dow_field = ",".join(str((d + 1) % 7) for d in schedule["weekdays"])
    elif schedule["mode"] == "monthly":
        day_field = ",".join(str(d) for d in schedule["monthdays"])
        dow_field = "*"
    else:
        day_field = dow_field = "*"
    return f"{minute} {hour_field} {day_field} * {dow_field}"


def cron_to_simple_rule(cron: str) -> dict | None:
    """把 cron 反解成「简单时间表」；表达不了就返回 None（页面切到高级输入框）。

    能反解的样子只有一种：分钟是单个数字、小时是纯数字列表、日期和星期最多只有
    一边是纯数字列表、月份是 ``*``。所以 ``0 */6 * * *``、``* * * * *``、
    ``0 20 1 * 1`` 这类都返回 None —— 它们仍然能用（走高级输入框），只是画不成
    勾选框，硬猜会给用户看一个错的界面。
    """
    fields = str(cron or "").split()
    if len(fields) != 5:
        return None
    minute_field, hour_field, day_field, month_field, dow_field = fields
    if month_field != "*":
        return None
    minute = _plain_int(minute_field, 0, 59)
    hours = _plain_int_list(hour_field, 0, 23)
    if minute is None or hours is None:
        return None
    weekdays: list[int] = []
    monthdays: list[int] = []
    if day_field == "*" and dow_field == "*":
        mode = "daily"
    elif day_field == "*":
        cron_dows = _plain_int_list(dow_field, 0, 6)
        if not cron_dows:
            return None
        mode = "weekly"
        weekdays = sorted((d + 6) % 7 for d in cron_dows)
    elif dow_field == "*":
        monthdays = _plain_int_list(day_field, 1, 31) or []
        if not monthdays:
            return None
        mode = "monthly"
    else:
        # 日期和星期都有值：cron 里是「或」的关系，勾选框表达不了
        return None
    return {
        "mode": mode,
        "weekdays": weekdays,
        "monthdays": monthdays,
        "times": [f"{h:02d}:{minute:02d}" for h in hours],
    }
