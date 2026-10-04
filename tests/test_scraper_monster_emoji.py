"""小火人表情（aweType=519）在采集端也当表情处理。

用户报的样本：msg_type=other、content="笑死"、awe_type=519，
content_json 里 display_name="笑死"、image_id=1010、url.url_list[0] 才是那张动图。
以前 519 不在表情名单里，于是落库成了普通消息（msg_type=0、media_url 为空），
阅读端只剩一行「笑死」。
"""
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


def test_monster_emoji_is_in_the_emoji_list():
    assert "519" in _EMOJI_AWE_TYPES
    # 别的表情不能被挤掉
    assert {"500", "501", "507", "508", "510", "514", "516"} <= set(_EMOJI_AWE_TYPES)


def test_monster_emoji_payload_gives_text_and_image():
    assert _emoji_payload(REAL_MONSTER_EMOJI) == (
        "笑死",
        "https://cdn.example.com/obj/emoji-1010",
    )


def test_emoji_payload_tolerates_missing_url():
    assert _emoji_payload({"aweType": 519, "display_name": "续火花"}) == ("续火花", None)
    assert _emoji_payload({"aweType": 519, "url": {"uri": "tos-cn/xx"}}) == ("", None)
    assert _emoji_payload({"aweType": 519, "url": {"url_list": [None, "https://x/y.webp"]}}) == ("", None)
