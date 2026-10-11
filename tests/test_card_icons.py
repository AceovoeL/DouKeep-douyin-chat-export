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
    # 文件名沿用抖音的官方原名，和前端 assets 里那张对得上
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
