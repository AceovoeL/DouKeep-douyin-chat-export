"""聊天数据洞察统计（查看器「数据洞察」弹窗的数据来源）。

所有统计都在本地 SQLite 上现算：单聊与群聊都能用，不写缓存：
查询都走现有索引，几万条消息秒级返回，也用不着缓存。

汇总内容（昵称规则与导出保持一致）：
- 热力图：星期几 x 几点 的消息量
- 消息排行：谁发的条数多、谁写的字多
- 文字表情榜：只统计抖音官方文字表情（"[钱]" 这种记号，名单与
  frontend/src/lib/emojiAssets.js 保持一致，这里存一份镜像）
- 友谊数据：第一次聊天日期、认识天数、最长连续互聊天数、聊天总天数
  （「互聊」按天算：这一天双方都发过消息才算 1 天，只有一方说话不断档）
- 年度关键词：正文分词后的高频词 top 榜（纯本地词库，不上传任何内容）
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone, timedelta

# 抖音官方文字表情名单 —— 与 frontend/src/lib/emojiAssets.js 的 EMOJI_NAMES 保持
# 一致（那份是前端渲染用的；这里只需要名单本身，用来把 [xx] 记号挑出来）。
OFFICIAL_EMOJI_NAMES = (
    '微笑、色、发呆、酷拽、抠鼻、流泪、捂脸、发怒、呲牙、尬笑、害羞、调皮、'
    '舔屏、看、爱心、比心、赞、鼓掌、感谢、抱抱你、玫瑰、尴尬流汗、戳手手、'
    '星星眼、杀马特、黄脸干杯、抱紧自己、拜拜、热化了、黄脸祈祷、懵、举手、'
    '加功德、摊手、无语流汗、续火花吧、点火、哭哭、吐舌小狗、送花、爱心手、'
    '贴贴、灵机一动、耶、打脸、大笑、机智、送心、666、闭嘴、来看我、'
    '一起加油、哈欠、震惊、晕、衰、困、疑问、泣不成声、小鼓掌、大金牙、偷笑、'
    '石化、思考、吐血、可怜、嘘、撇嘴、笑哭、奸笑、得意、憨笑、坏笑、抓狂、'
    '泪奔、钱、恐惧、愉快、快哭了、翻白眼、互粉、我想静静、委屈、鄙视、飞吻、'
    '再见、紫薇别走、听歌、求抱抱、绝望的凝视、不失礼貌的微笑、不看、裂开、'
    '干饭人、庆祝、吐舌、呆无辜、白眼、猪头、冷漠、暗中观察、二哈、菜狗、黑脸、'
    '展开说说、蜜蜂狗、柴犬、摸头、皱眉、擦汗、红脸、做鬼脸、强、如花、吐、'
    '惊喜、敲打、奋斗、吐彩虹、大哭、嘿哈、惊恐、囧、难过、斜眼、阴险、悠闲、'
    '咒骂、吃瓜群众、绿帽子、敢怒不敢言、求求了、眼含热泪、叹气、好开心、'
    '不是吧、鞠躬、躺平、九转大肠、不你不想、一头乱麻、kisskiss、'
    '你不大行、噢买尬、宕机、苦涩、逞强落泪、求机位-黄脸、求机位3、点赞、'
    '精选、强壮、碰拳、OK、击掌、左上、握手、抱拳、勾引、拳头、弱、胜利、'
    '右边、左边、嘴唇、心碎、凋谢、愤怒、垃圾、啤酒、咖啡、蛋糕、礼物、撒花、'
    '加一、减一、okk、V5、绝、给力、红包、屎、发、18禁、炸弹、西瓜、'
    '加鸡腿、握爪、太阳、月亮、给跪了、蕉绿、扎心、胡瓜、打call、栓Q、'
    '雪花、圣诞树、平安果、圣诞帽、气球、烟花、福、candy、糖葫芦、鞭炮、'
    '元宝、灯笼、锦鲤、巧克力、戒指、棒棒糖、纸飞机、粽子'
).split('、')

_OFFICIAL_EMOJI = frozenset(OFFICIAL_EMOJI_NAMES)

# 与前端 highlight.js 相同的记号规则：[ 名字 ]，名字 1~20 个字符、不含换行。
_EMOJI_TOKEN_RE = re.compile(r"\[([^\[\]\n]{1,20})\]")

# 中文正文里抽词：连续的汉字串 + 英文/数字词。
_CN_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,6}")
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,15}")

# 停用词：太常见、没有信息量的词不出现在关键词榜里（称呼、语气词等）。
_STOPWORDS = frozenset("""
我们 你们 他们 自己 现在 时候 什么 这个 那个 这些 那些 没有 就是 但是 所以
因为 如果 还是 然后 已经 可以 觉得 知道 其实 应该 可能 不是 怎么 这样 那样
一个 一下 不过 以后 以前 今天 昨天 明天 上午 下午 晚上 早上 中午 东西
哈哈 哈哈哈 哈哈哈哈哈 嗯嗯 好的 好吧 好嘞 好啦 行吧 好的好的 对对对 对呀
嗯嗯嗯 唔 额 恩 噢 哦 哇 唉 哎 哎呀 嘛 呀 啦 啧 咯 咧 老铁 家人们
""".split())

_TOP_N = 8
_TALKER_TOP_N = 10
_TZ_CN = timezone(timedelta(hours=8))


def _conv_messages(conn, conv_id: str):
    """该会话全部消息的轻量视图（只取统计需要的列）。"""
    return conn.execute(
        "SELECT msg_id, sender_uid, sender_name, content, msg_type, timestamp "
        "FROM messages WHERE conv_id = ? AND timestamp > 0 ORDER BY seq ASC",
        (conv_id,),
    ).fetchall()


def _text_of(msg) -> str:
    """文本消息的正文。表情图、图片、视频与 JSON 载荷不参与文本统计。"""
    content = msg["content"] or ""
    if not content or content.startswith("{"):
        return ""
    return content


def _collect_names(messages) -> dict:
    """消息里出现过的昵称（uid -> 名字），users 表没记的人用这个兜底。"""
    by_uid: dict[str, str] = {}
    for msg in messages:
        uid = (msg["sender_uid"] or "").strip()
        if uid and uid not in by_uid and msg["sender_name"]:
            by_uid[uid] = msg["sender_name"]
    return by_uid


def _display_name(uid, users_map, fallback, owner_uid, owner_name,
                  conv_type, conv_name) -> str:
    """昵称规则与导出一致：users 表 -> 消息自带昵称 -> 本机账号名 -> 兜底。"""
    uid = uid or ""
    name = users_map.get(uid, "")
    if name:
        return name
    if uid and uid == owner_uid:
        return owner_name
    if uid and fallback.get(uid):
        return fallback[uid]
    return ("用户" + uid) if conv_type == 2 else (conv_name or "未知")


def heatmap(messages, tz_hours: int = 8) -> dict:
    """星期几 x 小时 的消息量。grid[weekday][hour]，weekday 0=周一。"""
    grid = [[0] * 24 for _ in range(7)]
    offset = tz_hours * 3600
    for msg in messages:
        ts = (msg["timestamp"] or 0) + offset
        if ts <= 0:
            continue
        dt = datetime.utcfromtimestamp(ts)
        grid[dt.weekday()][dt.hour] += 1
    total = sum(sum(row) for row in grid)
    best = None
    for wd in range(7):
        for hour in range(24):
            v = grid[wd][hour]
            if v and (best is None or v > best[2]):
                best = (wd, hour, v)
    night = sum(grid[wd][h] for wd in range(7) for h in (0, 1, 2, 22, 23))
    morning = sum(grid[wd][h] for wd in range(7) for h in (6, 7, 8, 9, 10, 11))
    return {
        "grid": grid,
        "total": total,
        "peak": {"weekday": best[0], "hour": best[1], "count": best[2]} if best else None,
        "night_ratio": round(night / total, 3) if total else 0,
        "morning_ratio": round(morning / total, 3) if total else 0,
    }


def talkativeness(messages, users_map: dict, owner_uid: str, owner_name: str,
                  conv_type: int, conv_name: str | None) -> list[dict]:
    """消息排行：按发送者聚合条数与字数。系统通知（uid 为空）不计入。"""
    fallback = _collect_names(messages)
    stats: dict[str, dict] = {}
    for msg in messages:
        uid = (msg["sender_uid"] or "").strip()
        if not uid:
            continue
        row = stats.setdefault(uid, {"uid": uid, "count": 0, "chars": 0})
        row["count"] += 1
        row["chars"] += len(_text_of(msg))
    result = []
    for uid, row in stats.items():
        result.append({
            "uid": uid,
            "name": _display_name(uid, users_map, fallback, owner_uid,
                                  owner_name, conv_type, conv_name),
            "count": row["count"],
            "chars": row["chars"],
        })
    result.sort(key=lambda r: (-r["count"], r["name"]))
    return result[:_TALKER_TOP_N]


def emoji_stats(messages) -> list[dict]:
    """文字表情榜：只认名单里的官方表情（"[憨笑]"）。返回 top 若干。"""
    counter: Counter[str] = Counter()
    for msg in messages:
        if msg["msg_type"] not in (0, 1, 4):
            continue
        text = _text_of(msg)
        if not text:
            continue
        for match in _EMOJI_TOKEN_RE.finditer(text):
            name = match.group(1)
            if name in _OFFICIAL_EMOJI:
                counter[name] += 1
    return [{"name": name, "count": count} for name, count in counter.most_common(_TOP_N)]


def _mutual_days(messages, tz_hours: int = 8) -> set:
    """按天找出「双方都发过消息」的日子。

    同一天里至少两个人各发过至少一条消息，才算这天互聊：单聊时就是两个人
    你一句我一句；群聊里则是至少两位成员开了口。系统通知、撤回提示这类
    sender_uid 为空的行不算谁说的话，也不会把某天变成互聊。
    """
    speakers: dict = {}
    offset = tz_hours * 3600
    for msg in messages:
        uid = (msg["sender_uid"] or "").strip()
        ts = (msg["timestamp"] or 0) + offset
        if not uid or ts <= 0:
            continue
        speakers.setdefault(datetime.utcfromtimestamp(ts).date(), set()).add(uid)
    return {day for day, uids in speakers.items() if len(uids) >= 2}


def _longest_consecutive(days) -> int:
    """升序日期里最长的一段「相邻两天正好差一天」，没有就返回 0。"""
    longest = run = 0
    prev = None
    for day in days:
        run = run + 1 if prev is not None and (day - prev).days == 1 else 1
        longest = max(longest, run)
        prev = day
    return longest


def friendship(messages, tz_hours: int = 8) -> dict:
    """友谊数据：第一次聊天、认识天数、最长连续互聊天数、聊天总天数。"""
    offset = tz_hours * 3600
    days = sorted({datetime.utcfromtimestamp((m["timestamp"] or 0) + offset).date()
                   for m in messages})
    if not days:
        return {}
    first, last = days[0], days[-1]
    # 互聊的连续天数只看「双方都开口」的日子：只有一方连发再多条也不算一天，
    # 还会把前后的连续段打断 —— 这样这个数才代表两个人都在聊的频率。
    longest = _longest_consecutive(sorted(_mutual_days(messages, tz_hours)))
    # 认识天数按「算上头一天」数：8 月 6 日认识、当天就聊天，显示 1 天而不是 0 天。
    # 这样它和下面的 total_chat_days（也是数日期格子）口径一致，不会出现「聊过天的
    # 天数 96 > 认识天数 95」这种看着像矛盾的数字。
    days_known = max((datetime.now(_TZ_CN).date() - first).days,
                     (last - first).days) + 1
    return {
        "first_chat": first.isoformat(),
        "last_chat": last.isoformat(),
        "days_known": days_known,
        "total_chat_days": len(days),
        "longest_mutual_streak": longest,
        # days ⊆ [first, last] 且 days_known 覆盖到 last，所以这个比例天然 ≤ 1。
        "active_ratio": round(len(days) / max(days_known, 1), 3),
    }


def keywords(messages) -> list[dict]:
    """关键词 top 榜：中文按连续汉字串滑窗切词 + 英文单词，停用词表过滤。

    切词策略（不上分词库、不上传）：短句整句保留；长句按 2 字步长滑窗，
    让反复出现的说法自然浮上来。全库中文滑窗在几万条消息上要算几秒，
    够用 —— 这个弹窗本来就是点开才算一次的。
    """
    counter: Counter[str] = Counter()
    for msg in messages:
        text = _text_of(msg)
        if len(text) < 2:
            continue
        # 去掉表情记号，避免 "[憨笑]" 里的字被算进词频
        text = _EMOJI_TOKEN_RE.sub(" ", text)
        for chunk in _CN_WORD_RE.findall(text):
            if len(chunk) <= 4:
                if chunk not in _STOPWORDS:
                    counter[chunk] += 1
                # 4 字整句再补一个 2 字滑窗，覆盖「今天天气」里的「天气」
                if len(chunk) == 4:
                    for i in range(0, 3):
                        pair = chunk[i:i + 2]
                        if pair not in _STOPWORDS:
                            counter[pair] += 1
                continue
            # 长句：2 字步长滑窗
            for i in range(0, len(chunk) - 1, 2):
                for size in (4, 3, 2):
                    if i + size > len(chunk):
                        continue
                    word = chunk[i:i + size]
                    if word in _STOPWORDS:
                        continue
                    counter[word] += 1
        for word in _EN_WORD_RE.findall(text):
            low = word.lower()
            if low not in _STOPWORDS and len(low) >= 2:
                counter[low] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(_TOP_N)]


def build_insights(conn, conv_id: str, conv_type: int, conv_name: str | None,
                   tz_hours: int = 8) -> dict:
    """一次取数、五项统计全部算完。"""
    from common.owner import detect_owner

    messages = _conv_messages(conn, conv_id)
    owner_uid, owner_name = detect_owner(conn)
    users_map = {
        row["uid"]: row["nickname"]
        for row in conn.execute("SELECT uid, nickname FROM users").fetchall()
        if row["uid"] and row["nickname"]
    }
    return {
        "heatmap": heatmap(messages, tz_hours),
        "talkers": talkativeness(messages, users_map, owner_uid, owner_name,
                                 conv_type, conv_name),
        "emojis": emoji_stats(messages),
        "friendship": friendship(messages, tz_hours),
        "keywords": keywords(messages),
        "meta": {
            "total": len(messages),
            "owner": owner_name,
            "owner_uid": owner_uid,
        },
    }
