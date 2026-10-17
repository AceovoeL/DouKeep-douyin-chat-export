"""卡片自带的图（群头像 / 豆包卡封面 / 火花图标）的落地缓存。

抖音给这些图的是一条带签名的临时链接（约 180 天过期）。用户 2026-10-04 报的豆包卡
封面就是这样碎的：图没存下来、链接过期后再也拉不到。这里锁住三件事：

1. 存档文件名和前端 ``iconSrc()``（frontend/src/lib/media.js）算得一模一样；
2. 只允许抖音图床的 http(s) 链接（这个地址会被后端拿去请求）；
3. 卡片识别（哪张卡的图要存）和前端 ``cardKinds.js`` / ``fireInviteCard.js`` 对齐。
"""
import json
import os

import pytest

from common import card_icons

# 示例链接（形状和真实链接一致：群邀请卡的群头像、豆包卡封面）
INVITE_ICON = "http://p3-aweme-im-img.byteimg.com/tos-cn-i-example/group-icon.webp"
DOUBAO_COVER = (
    "http://p26-sign.douyinpic.com/large/tos-cn-i-example/demo-cover.jpeg"
    "?lk3s=138a59ce&x-expires=1781013600&x-signature=1frPMEUL%2BEuHBSTaVue2XUrPwM4%3D"
    "&from=327834062_large&s=PackSourceEnum_FEED&se=false&sc=cover&biz_tag=aweme_video"
    "&l=20260526224605C571133BC44DED8B0679"
)


@pytest.fixture()
def media_dir(tmp_path, monkeypatch):
    """把 media 目录指到 tmp，别把测试图写进真实的 data/media。"""
    monkeypatch.setattr(card_icons, "MEDIA_DIR", str(tmp_path))
    (tmp_path / card_icons.CARD_ICON_DIR_NAME).mkdir()
    return tmp_path


def test_filename_matches_frontend_hash():
    """这两个哈希是前端 media.test.js 里断言过的同一对值。"""
    assert card_icons.fnv1a_32(INVITE_ICON) == "e5e49455"
    assert card_icons.fnv1a_32(DOUBAO_COVER) == "15f52813"
    assert card_icons.icon_filename(INVITE_ICON) == "e5e49455.webp"
    assert card_icons.icon_filename(DOUBAO_COVER) == "15f52813.jpg"


def test_extension_follows_the_link_path():
    assert card_icons.icon_filename("https://x.douyinpic.com/a/b.PNG?sig=1") == "".join(
        [card_icons.fnv1a_32("https://x.douyinpic.com/a/b.PNG?sig=1"), ".png"])
    # 路径里没有扩展名（抖音有时给 .image）时按响应头猜，猜不到就按 webp 存
    weird = "https://p3-webcast.douyinpic.com/img/x.image?biz_tag=a"
    assert card_icons.icon_filename(weird).endswith(".webp")
    assert card_icons.icon_extension(weird, "image/png") == ".png"


def test_only_douyin_image_hosts_are_allowed():
    assert card_icons.is_allowed_icon_url(INVITE_ICON) is True
    assert card_icons.is_allowed_icon_url("https://p3-sign.douyinpic.com/x.webp") is True
    # 这个地址会变成服务端请求，所以本机/任意主机/伪装的域名都不许
    assert card_icons.is_allowed_icon_url("http://127.0.0.1:8000/api/stats") is False
    assert card_icons.is_allowed_icon_url("http://localhost/x.webp") is False
    assert card_icons.is_allowed_icon_url("http://evil.example.com/x.webp") is False
    assert card_icons.is_allowed_icon_url("https://douyinpic.com.evil.com/x.webp") is False
    assert card_icons.is_allowed_icon_url("file:///etc/passwd") is False
    assert card_icons.is_allowed_icon_url(None) is False
    assert card_icons.icon_proxy_url("http://127.0.0.1/x") == ""


def test_save_card_icon_writes_once_and_reuses(media_dir, monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"RIFF....WEBP" + b"x" * 200, "image/webp"

    monkeypatch.setattr(card_icons, "_fetch_icon", fake_fetch)
    rel = card_icons.save_card_icon(INVITE_ICON)
    assert rel == "card_icons/e5e49455.webp"
    assert os.path.getsize(os.path.join(media_dir, rel)) > 0
    assert card_icons.find_card_icon(INVITE_ICON) == rel
    # 再遇到同一个链接（另一个群里也拉了一次）直接用存好的那份
    assert card_icons.save_card_icon(INVITE_ICON) == rel
    assert calls == [INVITE_ICON]


def test_expired_link_is_left_alone(media_dir, monkeypatch):
    """签名过期时抖音返回 403，抓不到就算了，卡片照样画（只是没有图）。"""
    def fake_fetch(url):
        return None, ""

    monkeypatch.setattr(card_icons, "_fetch_icon", fake_fetch)
    assert card_icons.save_card_icon(DOUBAO_COVER) is None
    assert card_icons.find_card_icon(DOUBAO_COVER) is None


def test_card_icon_url_picks_the_right_field():
    invite = {
        "title": "示例群名", "desc": "某某 添加你进群", "type_desc": "群聊邀请",
        "icon": {"url_list": [INVITE_ICON]},
        "aweme_invite_card": {"group_name": "示例群名", "conversation_id": "1234567890123456789"},
    }
    music = {"aweType": 6001, "source_title": "豆包", "title": "《音乐公开课》",
             "icon": {"url_list": [DOUBAO_COVER]}}
    assert card_icons.card_icon_url(invite) == INVITE_ICON
    assert card_icons.card_icon_url(music) == DOUBAO_COVER
    # 群头像只写在 aweme_invite_card.group_icon 里时也能取到
    nested = {"type_desc": "群聊邀请",
              "event": {"param": {"conversation_id": "42"}},
              "aweme_invite_card": {"group_icon": {"url_list": [INVITE_ICON]}}}
    assert card_icons.card_icon_url(nested) == INVITE_ICON
    # 别的卡片/普通消息不该被顺手存图
    assert card_icons.card_icon_url({"aweType": 800, "itemId": "42",
                                     "cover_url": {"url_list": [INVITE_ICON]}}) == ""
    assert card_icons.card_icon_url({"type_desc": "群公告",
                                     "icon": {"url_list": [INVITE_ICON]}}) == ""
    assert card_icons.card_icon_url(None) == ""


# 「聊天火花」关系卡（更改为姐妹 / 兄弟 / 伴侣 / 死党火花）上的那颗火花图标。
# 姐妹 / 兄弟 / 伴侣 / 死党**共用同一套 card_type**，图标才是区分它们的唯一东西，
# 所以采集时必须把这张图存下来（否则阅读端只能画个空壳，或者认成别的火花）。
_SPARK = "https://p11.douyinpic.com/obj/im-resource/1734337175292-ts-626f736f6d313231362e706e67?biz_tag=aweme_im"


def _fire_payload(card_type, layout):
    return {
        "aweType": 110170,
        "im_dynamic_patch": {
            "card_key": "msg_fire_invitation",
            "card_type": card_type,
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    }


def test_fire_card_icon_comes_from_the_payload():
    """邀请卡在 top.content，接受纪念卡在 top@1.content.flame_icon。"""
    invite = _fire_payload("im_msg_fire_invitation_send",
                           {"top": {"content": _SPARK, "type": "im-image"}})
    assert card_icons.is_fire_card(invite) is True
    assert card_icons.card_icon_url(invite) == _SPARK

    accept = _fire_payload("im_msg_fire_invitation_accept_memory", {
        "top": {"content": ["https://p3.douyinpic.com/a.webp"], "type": "im-avatar"},
        "top@1": {"content": {"flame_icon": _SPARK}, "type": "im-avatar-animax"},
    })
    assert card_icons.card_icon_url(accept) == _SPARK


def test_fire_card_icon_rejects_other_card_families():
    """合养小火人 / 别家的卡不在这里 —— 它们的图标字段形状都不一样。"""
    joint = _fire_payload("im_msg_fire_invitation_joint_send",
                          {"top": {"content": _SPARK, "type": "im-image"}})
    assert card_icons.is_fire_card(joint) is False
    assert card_icons.card_icon_url(joint) == ""
    # card_key 不对（别的分享卡）也不算
    assert card_icons.is_fire_card({
        "im_dynamic_patch": {"card_key": "video_share",
                             "card_type": "im_msg_fire_invitation_send",
                             "raw_data": "{}"}}) is False


def test_fire_card_icon_missing_is_not_an_error():
    """载荷里没有图标（或排版坏掉）时返回空串，不抛异常。"""
    no_icon = _fire_payload("im_msg_fire_invitation_send", {"top": {"type": "im-image"}})
    assert card_icons.card_icon_url(no_icon) == ""
    broken = {"im_dynamic_patch": {"card_key": "msg_fire_invitation",
                                   "card_type": "im_msg_fire_invitation_send",
                                   "raw_data": "{不是 JSON"}}
    assert card_icons.card_icon_url(broken) == ""


# 「喂精灵」卡（card_type=im_msg_fire_invitation_feeding）：封面是**两张图**拼的 ——
# 背景（小火人 + 爱心光晕）在 whole_card.ui_info，那根辣条在 top.content。
# 两张都是带签名的临时链接，缺一张卡片就残了，所以两张都要存。
_FEED_BACKGROUND = (
    "https://lf3-creative.dailygn.com/obj/dino/pet_elf/images/feed_card/like2.png")
_FEED_FOOD = (
    "https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_icon.png")


def _feed_payload(layout):
    return {
        "aweType": 110274,
        "im_dynamic_patch": {
            "card_key": "msg_fire_invitation",
            "card_type": "im_msg_fire_invitation_feeding",
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    }


def _feed_layout():
    return {
        "top": {"content": _FEED_FOOD, "type": "im-image"},
        "whole_card": {"ui_info": {
            "background_image@light": _FEED_BACKGROUND,
            "background_image@dark": _FEED_BACKGROUND.replace("like2", "dark"),
        }},
    }


def test_feed_card_has_two_images():
    """喂食卡要存两张图：背景（@light 那张）+ 食物。"""
    feed = _feed_payload(_feed_layout())
    assert card_icons.is_feed_card(feed) is True
    assert card_icons.feed_card_image_urls(feed) == (_FEED_BACKGROUND, _FEED_FOOD)
    assert card_icons.card_icon_urls(feed) == [_FEED_BACKGROUND, _FEED_FOOD]
    # card_icon_url 是「单张」那个老接口，给喂食卡返回背景图
    assert card_icons.card_icon_url(feed) == _FEED_BACKGROUND


def test_feed_card_prefers_light_background():
    """深色模式那张只在没有 @light 时才用。"""
    layout = _feed_layout()
    ui = layout["whole_card"]["ui_info"]
    ui["background_image@light"] = ""
    feed = _feed_payload(layout)
    assert card_icons.feed_card_image_urls(feed)[0] == _FEED_BACKGROUND.replace("like2", "dark")


def test_feed_card_rejects_other_card_families():
    """同族的姐妹火花卡 / 合养小火人卡都不是喂食卡。"""
    for card_type in ("im_msg_fire_invitation_send",
                      "im_msg_fire_invitation_joint_send"):
        other = {
            "im_dynamic_patch": {"card_key": "msg_fire_invitation", "card_type": card_type,
                                 "raw_data": json.dumps(_feed_layout(), ensure_ascii=False)},
        }
        assert card_icons.is_feed_card(other) is False
        assert card_icons.feed_card_image_urls(other) == ("", "")
    # 排版坏掉 / 卡片本身不完整时也不炸
    broken = {"im_dynamic_patch": {"card_key": "msg_fire_invitation",
                                   "card_type": "im_msg_fire_invitation_feeding",
                                   "raw_data": "{不是 JSON"}}
    assert card_icons.feed_card_image_urls(broken) == ("", "")
    assert card_icons.card_icon_urls(broken) == []


# 老版版式（2026 年 7 月那批真实样本）：载荷里**没有**背景图 —— whole_card 里只有一段
# 日志信息，ui_info 是空的；卡片上那张精灵图在 top.content（文件名 hotstick_card.png）。
# 前端改用项目自带的 assets/invite-feed.png 打底，所以采集端只需要存 top.content 这一张。
_OLD_FEED_SPIRIT = (
    "https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_card.png")


def _old_feed_layout():
    return {
        "content_top": {"content": "你还没有喂精灵~", "type": "im-text"},
        "content_content": {"content": "一起喂解锁彩蛋效果", "type": "im-text"},
        "top": {"content": _OLD_FEED_SPIRIT, "type": "im-image"},
        "whole_card": {"extra_info": {"log_info": {
            "card_type": "im_msg_fire_invitation_feeding"}}},
    }


def test_old_feed_card_still_saves_its_spirit_image():
    """老版喂食卡（载荷没有背景图）也要存图，而且就存 top.content 那一张。"""
    feed = _feed_payload(_old_feed_layout())
    assert card_icons.is_feed_card(feed) is True
    assert card_icons.feed_card_image_urls(feed) == ("", _OLD_FEED_SPIRIT)
    assert card_icons.card_icon_urls(feed) == [_OLD_FEED_SPIRIT]
    # 文件名沿用抖音的官方原名（存在 data/media/card_icons/ 下，前端按链接取本地这份）
    assert card_icons.icon_filename(_OLD_FEED_SPIRIT) == "hotstick_card.png"


def test_feed_card_images_use_the_official_names():
    """精灵那套素材沿用抖音自己的文件名（用户 2026-10-11 要求）。"""
    assert card_icons.icon_filename(_FEED_BACKGROUND) == "like2.png"
    assert card_icons.icon_filename(_FEED_FOOD) == "hotstick_icon.png"
    # 别的图床照旧按哈希命名，不会被这套规则影响
    assert card_icons.icon_filename(INVITE_ICON) == "e5e49455.webp"
    # 不是这套图床的链接（哪怕文件名一样）也走哈希，别把名字借给别家
    assert card_icons.official_asset_name("https://p3.douyinpic.com/like2.png") == ""
    # 路径里带奇怪字符的不要当文件名写进磁盘
    assert card_icons.official_asset_name(
        "https://lf3-creative.dailygn.com/obj/dino/x/..%2Fbad.png") == ""


# 「陪伴天数」卡（aweType=110270 · card_key=msg_daily · card_type=im_msg_daily_fire）：
# 整张卡由前端自己画，载荷里唯一一张图是中间那颗**火花图标**（content_top 组件列表里的
# im-image）。项目自带 assets/fire.png，所以这张图不是非存不可，但存下来更贴近原样。
_DAILY_FLAME = (
    "http://p3-aweme-im-img.byteimg.com/tos-cn-i-7lppr0tkux/fire-story/normal/"
    "efabf5f6fbef6b1d50d066190ec54698.png~tplv-7lppr0tkux-original-png.png")


def _daily_payload(layout, awe_type=110270, card_type="im_msg_daily_fire"):
    return {
        "aweType": awe_type,
        "im_dynamic_patch": {
            "card_key": "msg_daily",
            "card_type": card_type,
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    }


def _daily_layout():
    return {
        "content_top": {
            "type": "im-component-list",
            "content": [
                {"type": "im-text", "content": "一起度过了人生的"},
                {"type": "im-image", "content": _DAILY_FLAME},
                {"type": "im-number-roller", "content": {"start": 364, "end": 365}},
                {"type": "im-text", "content": "天"},
            ],
        },
        "bottom_right": {"content": "限时日常"},
    }


def test_daily_fire_card_saves_its_flame_icon():
    """陪伴天数卡要存的是 content_top 里那颗火花图（不是别的那几张）。"""
    card = _daily_payload(_daily_layout())
    assert card_icons.is_daily_fire_card(card) is True
    assert card_icons.daily_fire_icon_url(card) == _DAILY_FLAME
    assert card_icons.card_icon_urls(card) == [_DAILY_FLAME]
    assert card_icons.card_icon_url(card) == _DAILY_FLAME


def test_daily_fire_card_rejects_the_gift_sibling():
    """同一个 card_key 下的「获得火花见面礼」（aweType=110408）不是这张卡。"""
    gift = _daily_payload(_daily_layout(), awe_type=110408, card_type="im_msg_daily_flame_gift")
    assert card_icons.is_daily_fire_card(gift) is False
    assert card_icons.daily_fire_icon_url(gift) == ""
    # 反过来：别的卡片家族也不认这张卡
    feed = _feed_payload(_feed_layout())
    assert card_icons.is_daily_fire_card(feed) is False
    # 排版坏掉 / 没有火花图时也不炸（前端会退回项目自带的 assets/fire.png）
    broken = {"im_dynamic_patch": {"card_key": "msg_daily",
                                   "card_type": "im_msg_daily_fire",
                                   "raw_data": "{不是 JSON"}}
    assert card_icons.daily_fire_icon_url(broken) == ""
    no_image = _daily_payload({"content_top": {"content": [{"type": "im-text", "content": "x"}]}})
    assert card_icons.daily_fire_icon_url(no_image) == ""
    assert card_icons.card_icon_urls(no_image) == []


# 「彩蛋福袋」卡（aweType=110360 · card_key=msg_easter_redpack ·
# card_type=pay_msg_easter_redpack_card）：上半部分那张插图在排版 top.cover_image，
# 是带签名的临时链接。下半部分底图（含蝴蝶结和文字位置）用项目自带的 assets/gift.png，
# 所以采集端只需要把这张插图存下来。
_EASTER_COVER = (
    "https://p26-infras.byteimg.com/static-assets/"
    "example-cover.png~tplv-example-75compress.image")


def _easter_payload(layout, *, awe_type=110360, card_key="msg_easter_redpack",
                    card_type="pay_msg_easter_redpack_card"):
    return {
        "aweType": awe_type,
        "description": "[彩蛋福袋]",
        "im_dynamic_patch": {
            "card_key": card_key,
            "card_type": card_type,
            "raw_data": json.dumps(layout, ensure_ascii=False),
        },
    }


def _easter_layout():
    return {
        "top": {"cover_image": _EASTER_COVER, "type": "im-image"},
        "bottom": {"content": "彩蛋福袋", "type": "im-text"},
        "content_content": {"content": "拆福袋", "type": "im-button"},
    }


def test_easter_redpack_card_saves_its_cover():
    """彩蛋福袋卡要存的是上半部分那张插图（top.cover_image）。"""
    card = _easter_payload(_easter_layout())
    assert card_icons.is_easter_redpack_card(card) is True
    assert card_icons.easter_redpack_cover_url(card) == _EASTER_COVER
    assert card_icons.card_icon_urls(card) == [_EASTER_COVER]
    assert card_icons.card_icon_url(card) == _EASTER_COVER
    # 路径以 .image 结尾时按响应头猜扩展名（这里 PNG），哈希名和前端一致
    assert card_icons.icon_filename(_EASTER_COVER) == (
        card_icons.fnv1a_32(_EASTER_COVER) + ".webp")
    assert card_icons.icon_extension(_EASTER_COVER, "image/png") == ".png"


def test_easter_redpack_card_recognized_by_any_marker():
    """card_key / card_type / aweType 任一命中都能认出来（老样本偶尔缺记号）。"""
    layout = _easter_layout()
    by_type = _easter_payload(layout, card_key="", awe_type=0)
    assert card_icons.is_easter_redpack_card(by_type) is True
    assert card_icons.easter_redpack_cover_url(by_type) == _EASTER_COVER

    by_awe = _easter_payload(layout, card_key="", card_type="", awe_type=110360)
    assert card_icons.is_easter_redpack_card(by_awe) is True

    no_marker = _easter_payload(layout, card_key="", card_type="", awe_type=0)
    assert card_icons.is_easter_redpack_card(no_marker) is False
    assert card_icons.easter_redpack_cover_url(no_marker) == ""


def test_easter_redpack_card_rejects_other_cards():
    """商品卡、喂食卡等别的卡片不会被顺手存成彩蛋福袋。"""
    goods = {
        "aweType": 11029,
        "im_dynamic_patch": {
            "card_key": "msg_big_product",
            "card_type": "",
            "raw_data": json.dumps(_easter_layout(), ensure_ascii=False),
        },
    }
    assert card_icons.is_easter_redpack_card(goods) is False
    assert card_icons.easter_redpack_cover_url(goods) == ""
    assert card_icons.card_icon_url(goods) == ""


def test_easter_redpack_card_without_cover_still_works():
    """排版坏掉或没有插图时返回空串，不抛异常（前端仍有底图可画）。"""
    no_cover = _easter_payload({"bottom": {"content": "彩蛋福袋"}})
    assert card_icons.easter_redpack_cover_url(no_cover) == ""
    broken = _easter_payload({})
    broken["im_dynamic_patch"]["raw_data"] = "{不是 JSON"
    assert card_icons.is_easter_redpack_card(broken) is True
    assert card_icons.easter_redpack_cover_url(broken) == ""

    # 个别老载荷把图放在顶层 cover_url / icon 上，也能兜住
    top_level = {"aweType": 110360, "cover_url": {"url_list": [_EASTER_COVER]}}
    assert card_icons.easter_redpack_cover_url(top_level) == _EASTER_COVER
