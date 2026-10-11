"""「N 条消息」显示几：Python 端与前端逐条对齐。

样例文件放在前端目录（`frontend/src/lib/displayCountCases.json`），
前端 `displayCount.test.js` 用的是同一份，两边规则不一致就会各自报错。
"""
import json
import os

from common import display_rules

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_PATH = os.path.join(_REPO_ROOT, "frontend", "src", "lib", "displayCountCases.json")


def load_cases():
    with open(CASES_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def test_display_rules_match_the_frontend_cases():
    for sample in load_cases():
        hidden = display_rules.hidden_message_ids(sample["messages"])
        assert sorted(hidden) == sorted(sample["hidden"]), sample["name"]
        assert display_rules.display_count(sample["messages"]) == sample["display"], sample["name"]


def test_sender_display_counts_split_by_sender():
    conn_hidden = {
        "msg_id": "x1", "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": 0,
        "content": "{}", "timestamp": 10, "seq": 1, "raw_data": None,
    }
    rows = [
        {"msg_id": "x2", "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": 1,
         "content": "你好", "timestamp": 11, "seq": 2, "raw_data": None},
        conn_hidden,
        {"msg_id": "x3", "conv_id": "0:1:1:2", "sender_uid": "u2", "msg_type": 1,
         "content": "你好呀", "timestamp": 12, "seq": 3, "raw_data": None},
    ]
    assert display_rules.sender_display_counts(rows) == {"u1": 1, "u2": 1}
    assert display_rules.display_count(rows) == 2


def test_empty_conversation_has_no_displayed_messages():
    assert display_rules.display_count([]) == 0
    assert display_rules.hidden_message_ids([]) == set()


def test_group_notice_locale_template_counts_as_displayed():
    """群通知落库时 content 只是 "[系统消息]" 占位，句子在 locale_resources 里，
    仍然算「会显示出来」，不能因为占位符而漏算。"""
    row = {
        "msg_id": "n1", "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": 0,
        "content": "[系统消息]", "timestamp": 10, "seq": 1,
        "raw_data": json.dumps({"content_json": json.dumps({
            "aweType": 100115,
            "active_users": [{"uid": 1, "nickname": "示例昵称"}],
            "locale_resources": [{"lang": "zh-Hans", "text": "{0}修改了群头像"}],
        }, ensure_ascii=False)}, ensure_ascii=False),
    }
    assert display_rules.render_system_msg_text(row) == "示例昵称修改了群头像"
    assert display_rules.hidden_message_ids([row]) == set()
    assert display_rules.display_count([row]) == 1


def _row(msg_id, cj, msg_type=0, content=""):
    return {
        "msg_id": msg_id, "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": msg_type,
        "content": content, "timestamp": 10, "seq": 1,
        "raw_data": json.dumps({"content_json": json.dumps(cj, ensure_ascii=False)}, ensure_ascii=False),
    }


def test_monster_emoji_519_counts_as_emoji_not_as_a_system_line():
    """小火人（aweType=519）在阅读端是表情图片，正文「笑死」不是提示文字。
    镜像要跟 frontend/src/lib/douyinMessage.js 的 LOOSE_EMOJI_AWES 一致。"""
    row = _row("e1", {
        "aweType": 519, "display_name": "笑死", "image_id": 1010,
        "url": {"url_list": ["https://cdn/1010.webp"]},
    }, content="笑死")
    assert display_rules.is_loose_emoji(row) is True
    assert display_rules.should_show(row) is True
    assert display_rules.hidden_message_ids([row]) == set()


def test_group_invite_card_is_always_displayed():
    """群邀请卡：阅读端画成卡片（群名 + 谁添加你进群），不该被当成空系统提示藏掉。"""
    row = _row("i1", {
        "title": "示例群名", "desc": "某某 添加你进群", "type_desc": "群聊邀请",
        "icon": {"url_list": ["https://cdn/group.webp"]},
        "event": {"param": {"conversation_id": "1234567890123456789"}},
        "aweme_invite_card": {"group_name": "示例群名", "conversation_id": "1234567890123456789"},
    })
    card = display_rules.get_invite_card(row)
    assert card == {
        "groupName": "示例群名",
        "convId": "1234567890123456789",
        "icon": "https://cdn/group.webp",
    }
    assert display_rules.should_show(row) is True
    assert display_rules.display_count([row]) == 1


def test_doubao_music_card_is_always_displayed():
    """豆包卡（aweType=6001）：画成卡片（封面 + 标题 + 豆包），不靠正文。"""
    row = _row("m1", {
        "aweType": 6001, "source_title": "豆包", "title": "《音乐公开课》",
        "icon": {"url_list": ["https://cdn/doubao.jpeg"]},
        "open_url": "https://v.douyin.com/xxx/",
    })
    assert display_rules.get_music_card(row) == {
        "title": "《音乐公开课》", "cover": "https://cdn/doubao.jpeg", "source": "豆包",
    }
    assert display_rules.should_show(row) is True


def test_card_parsers_do_not_claim_other_payloads():
    """邀请卡要有群会话 id，豆包卡要有标题或封面 —— 别的卡片一律不认。"""
    assert display_rules.get_invite_card(_row("x1", {"type_desc": "群聊邀请", "title": "群"})) is None
    assert display_rules.get_music_card(_row("x2", {"aweType": 6001})) is None
    assert display_rules.get_music_card(_row("x3", {"aweType": 805, "itemId": "9"})) is None


# 关系卡上那颗火花的两张图：姐妹火花（文件名带 bosom1216 的十六进制）与兄弟火花。
_SPARK = ("https://p11.douyinpic.com/obj/im-resource/1734337175292-ts-"
          "626f736f6d313231362e706e67?biz_tag=aweme_im")
_BROTHER_SPARK = "https://p11.douyinpic.com/obj/im-resource/1734337200000-ts-62726f74686572.png"


def _fire_card(card_type, layout):
    """「聊天火花」关系卡：content_json 里嵌 im_dynamic_patch，raw_data 是再编码的排版 JSON。"""
    return _row("f1", {
        "aweType": 110170,
        "description": "姐妹火花",
        "im_dynamic_patch": {
            "card_key": "msg_fire_invitation",
            "card_type": card_type,
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    })


def test_fire_card_is_always_displayed():
    """「聊天火花」关系卡（邀请 / 接受纪念）：文字在卡片载荷里，不该被当成空系统提示藏掉。

    这两种卡落库后 content 往往是空的，界面按卡片画 —— 认不出来的话「N 条消息」
    会比实际少算两条。
    """
    invite = _fire_card("im_msg_fire_invitation_send",
                        {"top": {"content": _SPARK, "type": "im-image"}})
    assert display_rules.get_fire_invite_card(invite) == {"card_type": "im_msg_fire_invitation_send"}
    assert display_rules.should_show(invite) is True
    assert display_rules.display_count([invite]) == 1

    accept = _fire_card("im_msg_fire_invitation_accept_memory", {
        "top@1": {"content": {"flame_icon": _SPARK, "relation_type": "bosom"},
                  "type": "im-avatar-animax"},
    })
    assert display_rules.get_fire_invite_card(accept) == {
        "card_type": "im_msg_fire_invitation_accept_memory"}
    assert display_rules.should_show(accept) is True


def test_fire_card_accepts_every_relation_kind():
    """姐妹 / 兄弟 / 伴侣 / 死党火花共用同一套 card_type，只靠图标区分 —— 都该认出来。"""
    brother = _fire_card("im_msg_fire_invitation_send",
                         {"top": {"content": _BROTHER_SPARK, "type": "im-image"}})
    assert display_rules.get_fire_invite_card(brother) == {"card_type": "im_msg_fire_invitation_send"}
    # 图标缺失也不影响「认卡片」（认的是类型号）
    iconless = _fire_card("im_msg_fire_invitation_send", {"top": {"type": "im-image"}})
    assert display_rules.get_fire_invite_card(iconless) == {"card_type": "im_msg_fire_invitation_send"}


def test_fire_card_parser_ignores_other_card_families():
    """别的小火人 / 合养精灵卡是另一套外观、另一个 card_type，不认。"""
    joint = _fire_card("im_msg_fire_invitation_joint_send",
                       {"top": {"content": _BROTHER_SPARK, "type": "im-image"}})
    assert display_rules.get_fire_invite_card(joint) is None
    # 别的卡片（商品卡）也不认
    assert display_rules.get_fire_invite_card(_row("x4", {"aweType": 11029})) is None


# 「合养小火人」卡：同一个 card_key（msg_fire_invitation），但 card_type 是 joint_* 三种。
# 它和「聊天火花」是各自独立的一族，解析函数也各自独立（前端对应 fireBabyCard.js）。
_PETELF = "https://lf3-static.bytednsdoc.com/obj/eden-cn/petelf.png"


def _baby_card(card_type, layout):
    return _row("b1", {
        "aweType": 110182,
        "description": "邀请你一起养小火人",
        "im_dynamic_patch": {
            "card_key": "msg_fire_invitation",
            "card_type": card_type,
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    })


def _baby_layout(title, subtitle, button):
    return {
        "top": {"content": _PETELF, "type": "im-image"},
        "content_top": title,
        "content_content": subtitle,
        "bottom": {"content": button, "type": "im-button"},
        "whole_card": {"extra_info": {"status": "1"}},
    }


def test_fire_baby_card_is_always_displayed():
    """「合养小火人」三种卡（邀请 / 开始合养 / 纪念）不该被当成空系统提示藏掉。

    这三种卡落库后 content 往往是空字符串或很短，文字都在卡片载荷里，界面按卡片画 ——
    认不出来的话「N 条消息」会比实际少算几条。
    """
    for card_type in ("im_msg_fire_invitation_joint_send",
                      "im_msg_fire_invitation_joint_accept",
                      "im_msg_fire_invitation_joint_accept_memory"):
        card = _baby_card(card_type, _baby_layout(
            {"content@guest": "对方邀请你一起养小火人", "content@sender": "邀请对方一起养小火人"},
            {"content": "查看合养小火人"},
            "接受邀请"))
        assert display_rules.get_fire_baby_card(card) == {"card_type": card_type}, card_type
        assert display_rules.should_show(card) is True, card_type
        assert display_rules.display_count([card]) == 1, card_type


def test_fire_baby_card_ignores_the_spark_family():
    """「聊天火花」（send / accept_memory）是另一族，两边不互相认领。"""
    spark = _fire_card("im_msg_fire_invitation_send",
                       {"top": {"content": _SPARK, "type": "im-image"}})
    assert display_rules.get_fire_baby_card(spark) is None
    assert display_rules.get_fire_invite_card(spark) is not None

    baby = _baby_card("im_msg_fire_invitation_joint_send",
                      _baby_layout({"content": "对方邀请你一起养小火人"},
                                   {"content": "查看合养小火人"}, "接受邀请"))
    assert display_rules.get_fire_invite_card(baby) is None
    assert display_rules.get_fire_baby_card(baby) is not None


def test_fire_baby_card_ignores_unrelated_joint_cards():
    """同族的喂精灵 / AI 精灵切换卡不是「合养小火人」，不认（各自的外观都不一样）。"""
    for card_type in ("im_msg_fire_invitation_feeding",
                      "im_msg_fire_invitation_ai_petelf_switch_send",
                      "im_msg_fire_invitation_pre_flame_send",
                      "im_msg_fire_invitation_ds_send"):
        card = _baby_card(card_type, _baby_layout({"content": "x"}, {"content": "y"}, "z"))
        assert display_rules.get_fire_baby_card(card) is None, card_type
    # 排版坏掉时也不认（前端会照样返回 null，两边一致）
    broken = {"im_dynamic_patch": {"card_key": "msg_fire_invitation",
                                   "card_type": "im_msg_fire_invitation_joint_send",
                                   "raw_data": "{不是 JSON"}}
    assert display_rules.get_fire_baby_card(_row("b2", broken)) is None


def _feed_card(layout):
    return _row("f1", {
        "aweType": 110274,
        "description": "邀请你一起喂精灵",
        "im_dynamic_patch": {
            "card_key": "msg_fire_invitation",
            "card_type": "im_msg_fire_invitation_feeding",
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    })


def test_feed_card_is_always_displayed():
    """「喂精灵」卡也不该被当成空系统提示藏掉。

    它落库后 content 往往是空字符串（文字都在卡片载荷里、界面按卡片画），
    认不出来的话「N 条消息」会比实际少算几条。
    """
    card = _feed_card({
        "top": {"content": _PETELF, "type": "im-image"},
        "content_top": {"content": "邀请你一起喂精灵"},
        "content_content": {"content": "精灵还在慢慢品味中~"},
        "bottom": {"content": "去喂食", "type": "im-button"},
    })
    assert display_rules.get_feed_card(card) == {"card_type": "im_msg_fire_invitation_feeding"}
    assert display_rules.should_show(card) is True
    assert display_rules.display_count([card]) == 1


def test_feed_card_does_not_steal_sibling_cards():
    """喂食卡和同族的火花卡 / 合养小火人卡各认各的，谁也不抢谁的。"""
    spark = _fire_card("im_msg_fire_invitation_send", {"top": {"content": _SPARK}})
    baby = _baby_card("im_msg_fire_invitation_joint_send",
                      _baby_layout({"content": "x"}, {"content": "y"}, "z"))
    for other in (spark, baby):
        assert display_rules.get_feed_card(other) is None
    # 反过来：喂食卡不被那两支认领
    feed = _feed_card({"top": {"content": _PETELF}})
    assert display_rules.get_fire_invite_card(feed) is None
    assert display_rules.get_fire_baby_card(feed) is None
    # 排版坏掉时不认（前端同样返回 null，两边一致）
    broken = {"im_dynamic_patch": {"card_key": "msg_fire_invitation",
                                   "card_type": "im_msg_fire_invitation_feeding",
                                   "raw_data": "{不是 JSON"}}
    assert display_rules.get_feed_card(_row("f2", broken)) is None
