"""卡片自带的图（群头像 / 豆包卡封面）的落地缓存。

抖音给这些图的是一条带签名的临时链接（约 180 天过期）。用户 2026-10-04 报的豆包卡
封面就是这样碎的：图没存下来、链接过期后再也拉不到。这里锁住三件事：

1. 存档文件名和前端 ``iconSrc()``（frontend/src/lib/media.js）算得一模一样；
2. 只允许抖音图床的 http(s) 链接（这个地址会被后端拿去请求）；
3. 卡片识别（哪张卡的图要存）和前端 ``cardKinds.js`` 对齐。
"""
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
