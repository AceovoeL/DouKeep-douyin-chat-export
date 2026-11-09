"""聊天数据洞察统计的单元测试。"""

import sqlite3

from backend.insights import emoji_stats, friendship, heatmap, keywords, talkativeness


def _row(msg_id="1", uid="a", name="小明", content="文本", msg_type=1, ts=1730000000):
    return {
        "msg_id": msg_id, "sender_uid": uid, "sender_name": name,
        "content": content, "msg_type": msg_type, "timestamp": ts,
    }


def test_heatmap_counts_by_weekday_and_hour():
    # 1730000000 = 2024-10-27 08:53 UTC（周日）；同小时放两条，peak 应为 2
    msgs = [
        _row(ts=1730000000),
        _row(msg_id="2", ts=1730000000 + 60),
        _row(msg_id="3", ts=1730000000 + 86400),
    ]
    hm = heatmap(msgs, tz_hours=0)
    assert hm["total"] == 3
    assert sum(sum(r) for r in hm["grid"]) == 3
    assert hm["peak"]["count"] == 2
    assert hm["night_ratio"] + hm["morning_ratio"] >= 0


def test_talkativeness_excludes_system_rows():
    msgs = [_row(uid="a", content="hello"), _row(uid="", content="[系统消息]")]
    talkers = talkativeness(msgs, {}, "", "我", 1, "会话")
    assert len(talkers) == 1
    assert talkers[0]["uid"] == "a"


def test_emoji_stats_only_official_tokens():
    msgs = [
        _row(content="好[憨笑]"),
        _row(msg_id="2", content="[憨笑][憨笑]"),
        # 非官方记号（不在名单里）不统计
        _row(msg_id="3", content="[自定义表情]"),
        # 表情图（msg_type=2）不参与
        _row(msg_id="4", content="{}", msg_type=2),
    ]
    emojis = emoji_stats(msgs)
    assert emojis == [{"name": "憨笑", "count": 3}]


def test_friendship_counts_longest_mutual_streak():
    import time

    base = 1730000000  # 2024-10-27
    day = 86400

    def on_day(prefix, ts, uids):
        return [_row(msg_id=f"{prefix}{i}", uid=uid, ts=ts)
                for i, uid in enumerate(uids)]

    # 连续 3 天都是两个人各发过一条（互聊），中间断一天，第 5 天只有一个人说话。
    msgs = (on_day("1", base, ["a", "b"])
            + on_day("2", base + day, ["a", "b"])
            + on_day("3", base + 2 * day, ["a", "b"])
            + on_day("4", base + 4 * day, ["a"]))
    fr = friendship(msgs, tz_hours=0)
    assert fr["longest_mutual_streak"] == 3
    # 「聊过天的天数」仍然按有消息的日子算，单方说话的日子也算
    assert fr["total_chat_days"] == 4
    assert fr["first_chat"] == time.strftime("%Y-%m-%d", time.gmtime(base))
    # 认识天数按「算上头一天」数，任何时候都不该比聊过天的天数还少
    assert fr["days_known"] >= fr["total_chat_days"]


def test_friendship_single_side_day_is_not_mutual():
    base = 1730000000
    day = 86400
    # 第 1、3 天互聊，中间那天只有 a 一个人说话：连续互聊最长只有 1 天
    msgs = [
        _row(msg_id="1a", uid="a", ts=base),
        _row(msg_id="1b", uid="b", ts=base + 60),
        _row(msg_id="2a", uid="a", ts=base + day),
        _row(msg_id="3a", uid="a", ts=base + 2 * day),
        _row(msg_id="3b", uid="b", ts=base + 2 * day + 60),
    ]
    fr = friendship(msgs, tz_hours=0)
    assert fr["longest_mutual_streak"] == 1
    assert fr["total_chat_days"] == 3


def test_friendship_ignores_system_rows_for_mutual_days():
    # 只有 a 说话，另一条是系统通知（uid 为空）：这天不算互聊
    msgs = [
        _row(msg_id="1", uid="a", ts=1730000000),
        _row(msg_id="2", uid="", name="", content="[系统消息]", ts=1730000000 + 60),
    ]
    assert friendship(msgs, tz_hours=0)["longest_mutual_streak"] == 0


def test_friendship_days_known_includes_first_day():
    # 只有一天、且只有一方发过消息：认识天数和聊过天的天数都是 1，
    # 但没形成互聊，连续互聊天数是 0。
    # 用未来时间戳，这样不依赖「今天」，断言结果稳定。
    fr = friendship([_row(ts=4102444800)], tz_hours=8)  # 2100-01-01 UTC
    assert fr["days_known"] == 1
    assert fr["total_chat_days"] == 1
    assert fr["longest_mutual_streak"] == 0


def test_keywords_drop_stopwords_and_emoji_tokens():
    msgs = [_row(content="笑死我了[憨笑] 真的笑死"), _row(msg_id="2", content="火锅好吃火锅好吃")]
    kws = keywords(msgs)
    words = [k["word"] for k in kws]
    assert "笑死" in words
    assert "火锅" in words
    # 停用词不该出现
    assert "我们" not in words