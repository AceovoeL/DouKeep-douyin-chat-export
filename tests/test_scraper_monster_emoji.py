"""「会落成普通消息」的表情在采集端也当表情处理（519 小火人 / 520 精灵发的表情等）。

用户报的样本：msg_type=other、content="笑死"、awe_type=519，
content_json 里 display_name="笑死"、image_id=1010、url.url_list[0] 才是那张动图。
以前 519 不在表情名单里，于是落库成了普通消息（msg_type=0、media_url 为空），
阅读端只剩一行「笑死」。520（精灵自己发的表情）同理 —— 阅读端早就当图片画，
采集端一直没认，图也就一直没被下载（用户 2026-10-11 反馈）。
"""
from common import card_icons
from extractor.web_scraper import _EMOJI_AWE_TYPES, _emoji_payload

REAL_MONSTER_EMOJI = {
    "aweType": 519,
    "display_name": "笑死",
    "emoji_from": "monster",
    "emoji_source": "unpersonalized_monster",
    "image_id": 1010,
    "image_type": "webp",
    "sticker_type": 23,
    "url": {
        "uri": "tos-cn-i-example/emoji-1010",
        "url_list": ["https://cdn.example.com/obj/emoji-1010"],
    },
}

# 精灵（小火人 AI）自己发的表情：载荷形状与 monster emoji 一致，
# 只是多了 ai_business_ext（性格设定）。
REAL_SPIRIT_EMOJI = {
    "aweType": 520,
    "display_name": "示例表情",
    "ai_business_ext": {"petai_style": "示例性格"},
    "url": {
        "uri": "tos-cn-o-example/spirit",
        "url_list": ["https://cdn.example.com/obj/spirit.webp"],
    },
}


def test_monster_emoji_is_in_the_emoji_list():
    assert "519" in _EMOJI_AWE_TYPES
    # 别的表情不能被挤掉
    assert {"500", "501", "507", "508", "510", "514", "516"} <= set(_EMOJI_AWE_TYPES)


def test_spirit_emoji_is_in_the_emoji_list():
    """精灵发的表情（520）也要按表情落库，否则图不会被下载。"""
    assert {"515", "517", "520"} <= set(_EMOJI_AWE_TYPES)
    assert card_icons.LOOSE_EMOJI_AWE_TYPES <= set(_EMOJI_AWE_TYPES)
    assert card_icons.LOOSE_EMOJI_AWE_TYPES <= card_icons.EMOJI_AWE_TYPES


def test_monster_emoji_payload_gives_text_and_image():
    assert _emoji_payload(REAL_MONSTER_EMOJI) == (
        "笑死",
        "https://cdn.example.com/obj/emoji-1010",
    )


def test_spirit_emoji_payload_gives_text_and_image():
    assert _emoji_payload(REAL_SPIRIT_EMOJI) == (
        "示例表情",
        "https://cdn.example.com/obj/spirit.webp",
    )


def test_emoji_payload_tolerates_missing_url():
    assert _emoji_payload({"aweType": 519, "display_name": "续火花"}) == ("续火花", None)
    assert _emoji_payload({"aweType": 519, "url": {"uri": "tos-cn/xx"}}) == ("", None)
    assert _emoji_payload({"aweType": 519, "url": {"url_list": [None, "https://x/y.webp"]}}) == ("", None)
