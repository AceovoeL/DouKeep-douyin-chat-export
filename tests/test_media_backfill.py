"""采集时（以及面板「下载历史图片」）自动补回以前漏掉的表情 / 卡片图。

用户 2026-10-04 的要求：**采集消息时自动补全以前没采集到的小火人表情、以及豆包卡
封面的图**。这两类消息的下载地址都还在旧载荷里，所以不用重新抓包：

* 小火人（aweType=519）：以前落库成 msg_type=0、media_url 为空，图在 cj.url.url_list；
* 豆包卡（aweType=6001）/ 群邀请卡：图在 cj.icon（群头像还在 aweme_invite_card.group_icon）。
"""
import asyncio
import hashlib
import json
import os

import pytest

from backend import database
from common import card_icons
from common.message_kinds import share_cover_url
from extractor import media_backfill, web_scraper
from tests.conftest import insert_conversation, insert_message

EMOJI_URL = "https://cdn.example.com/emoji/monster.webp"
COVER_URL = "https://p3-sign.douyinpic.com/obj/tos-cn-i-example/doubao-cover.jpeg"
GROUP_ICON = "https://p3-aweme-im-img.byteimg.com/tos-cn-i-example/group-icon.webp"
# 喂食卡封面的两张图（精灵素材走 dailygn.com 这个图床）
FEED_BACKGROUND = "https://lf3-creative.dailygn.com/obj/dino/pet_elf/images/feed_card/like2.png"
FEED_FOOD = "https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_icon.png"


@pytest.fixture()
def media_dirs(tmp_path, monkeypatch):
    """表情存 tmp/emoji，卡片图存 tmp/card_icons —— 别碰真实的 data/media。"""
    monkeypatch.setattr(media_backfill, "EMOJI_DIR", str(tmp_path / "emoji"))
    monkeypatch.setattr(card_icons, "MEDIA_DIR", str(tmp_path))
    (tmp_path / card_icons.CARD_ICON_DIR_NAME).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _raw(cj):
    # 用紧凑写法：真实库里的 content_json 是双层编码的紧凑 JSON
    return json.dumps({"content_json": json.dumps(cj, ensure_ascii=False, separators=(",", ":"))},
                      ensure_ascii=False)


def _monster_emoji_row():
    return _raw({"aweType": 519, "display_name": "笑死", "url": {"url_list": [EMOJI_URL]}})


def _doubao_row():
    return _raw({"aweType": 6001, "title": "《音乐公开课》", "icon": {"url_list": [COVER_URL]}})


def _invite_row():
    return _raw({"type_desc": "群聊邀请", "title": "示例群名",
                 "icon": {"url_list": [GROUP_ICON]},
                 "aweme_invite_card": {"group_name": "示例群名",
                                       "conversation_id": "1234567890123456789"}})


def test_collects_monster_emoji_and_card_rows(temp_db, media_dirs):
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_519", "c1", 1, msg_type=0, content="笑死", raw_data=_monster_emoji_row())
    insert_message(conn, "srv_music", "c1", 2, msg_type=1, content="《音乐公开课》", raw_data=_doubao_row())
    insert_message(conn, "srv_invite", "c1", 3, msg_type=0, content="群聊邀请", raw_data=_invite_row())
    # 图片 / 普通文本不该被这个回填碰
    insert_message(conn, "srv_text", "c1", 4, msg_type=1, content="普通消息")
    # 已经存过图的表情不算缺
    insert_message(conn, "srv_done", "c1", 5, msg_type=2, content="[表情]",
                   media_local_path="emoji/old.webp",
                   raw_data=_raw({"aweType": 506, "url": {"url_list": [EMOJI_URL]}}))

    rows = media_backfill.iter_media_rows(conn)
    assert [r["msg_id"] for r in rows] == ["srv_519", "srv_music", "srv_invite"]
    conn.close()


def test_rows_without_a_url_are_not_candidates(temp_db, media_dirs):
    """没有地址的表情（旧数据只留了文字、也没 media_url）不算待补，
    否则每轮采集都把它算成一次失败。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_no_url", "c1", 1, msg_type=2, content="[表情]")
    insert_message(conn, "srv_with_url", "c1", 2, msg_type=2, content="[表情]",
                   media_url="https://cdn/e.gif")
    assert [r["msg_id"] for r in media_backfill.iter_media_rows(conn)] == ["srv_with_url"]
    conn.close()


def test_backfill_downloads_and_records_paths(temp_db, media_dirs, monkeypatch):
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_519", "c1", 1, msg_type=0, content="笑死", raw_data=_monster_emoji_row())
    insert_message(conn, "srv_music", "c1", 2, msg_type=1, content="《音乐公开课》", raw_data=_doubao_row())

    fetched = []

    def fake_save_emoji(url, directory):
        fetched.append(url)
        os.makedirs(directory, exist_ok=True)
        name = hashlib.md5(url.encode()).hexdigest()[:16] + ".webp"
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(b"RIFF....WEBP" + b"x" * 200)
        return "emoji/" + name

    monkeypatch.setattr(web_scraper, "_save_emoji", fake_save_emoji)
    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (b"RIFF....WEBP" + b"y" * 200, "image/webp"))

    stats = media_backfill.backfill_media(conn)
    assert stats["total"] == 2 and stats["ok"] == 2 and stats["failed"] == 0
    assert fetched == [EMOJI_URL]
    row = conn.execute("SELECT media_local_path, media_url FROM messages WHERE msg_id='srv_519'").fetchone()
    assert row["media_local_path"].startswith("emoji/")
    assert row["media_url"] == EMOJI_URL
    assert conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_music'").fetchone()[0].startswith("card_icons/")
    # 再跑一次：已经补齐的行不再出现
    assert media_backfill.iter_media_rows(conn) == []
    conn.close()


def test_expired_link_counts_as_failed(temp_db, media_dirs, monkeypatch):
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_music", "c1", 1, msg_type=1, content="《音乐公开课》", raw_data=_doubao_row())
    monkeypatch.setattr(card_icons, "_fetch_icon", lambda url: (None, ""))
    stats = media_backfill.backfill_media(conn)
    assert stats == {"total": 1, "ok": 0, "failed": 1, "paths": {}}
    assert conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_music'").fetchone()[0] is None
    conn.close()


def _feed_row():
    """「喂精灵」卡：封面是两张图（背景 + 食物），两张都得补下来。"""
    return _raw({
        "aweType": 110274, "description": "邀请你一起喂精灵",
        "im_dynamic_patch": {
            "card_key": "msg_fire_invitation",
            "card_type": "im_msg_fire_invitation_feeding",
            "raw_data": json.dumps({
                "top": {"content": FEED_FOOD, "type": "im-image"},
                "whole_card": {"ui_info": {"background_image@light": FEED_BACKGROUND}},
            }, ensure_ascii=False),
        },
    })


def test_feed_card_backfill_downloads_both_cover_images(temp_db, media_dirs, monkeypatch):
    """喂食卡封面的两张图都要补下来，不能只补第一张就收工。

    两张图都是带签名的临时链接，少存一张卡片封面就缺一块（背景在、辣条没了，
    或者反过来）。
    """
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_feed", "c1", 1, msg_type=0, content="", raw_data=_feed_row())
    assert [r["msg_id"] for r in media_backfill.iter_media_rows(conn)] == ["srv_feed"]

    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\x89PNG" + b"x" * 200, "image/png"

    monkeypatch.setattr(card_icons, "_fetch_icon", fake_fetch)
    stats = media_backfill.backfill_media(conn)
    assert stats["ok"] == 1
    # 两个地址都请求过，一个不落
    assert fetched == [FEED_BACKGROUND, FEED_FOOD]
    # 两张都真的存到了本地（文件名沿用抖音的官方名）
    saved = sorted(os.listdir(media_dirs / card_icons.CARD_ICON_DIR_NAME))
    assert saved == ["hotstick_icon.png", "like2.png"]
    # media_local_path 只记得下一个（背景图），另一张靠 ?url= 兜底
    path = conn.execute("SELECT media_local_path FROM messages WHERE msg_id='srv_feed'").fetchone()[0]
    assert path == "card_icons/like2.png"
    conn.close()


def test_feed_card_partial_download_still_records_what_it_got(temp_db, media_dirs, monkeypatch):
    """两张里只有一张下得下来时，补到的那张照样记上，别整条算失败。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_feed", "c1", 1, msg_type=0, content="", raw_data=_feed_row())

    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (None, "") if "hotstick" in url else (b"\x89PNG" + b"x" * 200, "image/png"))
    stats = media_backfill.backfill_media(conn)
    assert stats["ok"] == 1
    assert conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_feed'").fetchone()[0] == "card_icons/like2.png"
    conn.close()


def test_emoji_url_prefers_recorded_media_url():
    cj = {"aweType": 519, "display_name": "续火花", "url": {"url_list": [EMOJI_URL]}}
    assert media_backfill.emoji_media_url(cj) == EMOJI_URL
    assert media_backfill.emoji_media_url(cj, "https://cdn/recorded.webp") == "https://cdn/recorded.webp"
    assert media_backfill.emoji_media_url({"aweType": 519}) == ""
    # 坏掉的 media_url（空串 / 不是链接）不算数，回载荷里找
    assert media_backfill.emoji_media_url(cj, "") == EMOJI_URL


# 精灵自己发的表情（aweType=520）：阅读端一早就当图片画，采集端却不在名单里，
# 于是落库成了普通消息（msg_type=0）、图从没下过（用户 2026-10-11 反馈）。
SPIRIT_EMOJI_URL = "https://p3-im-emoticon-sign.byteimg.com/obj/tos-cn-o-example/spirit.webp"


def _spirit_emoji_row():
    return _raw({
        "aweType": 520, "display_name": "示例表情",
        "ai_business_ext": {"petai_style": "示例性格"},
        "url": {"url_list": [SPIRIT_EMOJI_URL]},
    })


def test_loose_emoji_list_stays_the_same_as_the_reader():
    """「会落成普通消息」的表情就是这四个，和阅读端 LOOSE_EMOJI_AWES 对齐。

    这几个的图只能靠载荷里的 aweType 粗筛出来（它们不是 msg_type=2），
    漏掉哪一个，那种表情就会在链接过期后变成碎图。
    """
    assert card_icons.LOOSE_EMOJI_AWE_TYPES == frozenset({"515", "517", "519", "520"})
    assert card_icons.LOOSE_EMOJI_AWE_TYPES <= card_icons.EMOJI_AWE_TYPES


def test_spirit_emoji_is_a_backfill_candidate_even_as_a_plain_message(temp_db, media_dirs):
    """精灵表情落库时是普通消息（msg_type=0），也要进待补清单。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_520", "c1", 1, msg_type=0, content="示例表情",
                   raw_data=_spirit_emoji_row())
    assert [r["msg_id"] for r in media_backfill.iter_media_rows(conn)] == ["srv_520"]
    conn.close()


def test_spirit_emoji_backfill_records_the_local_copy(temp_db, media_dirs, monkeypatch):
    """补下来之后要记上路径和地址，下次不再当成缺图。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_520", "c1", 1, msg_type=0, content="示例表情",
                   raw_data=_spirit_emoji_row())

    fetched = []

    def fake_save_emoji(url, directory):
        fetched.append(url)
        return _write_emoji(directory, url)

    monkeypatch.setattr(web_scraper, "_save_emoji", fake_save_emoji)
    stats = media_backfill.backfill_media(conn)
    assert stats["total"] == 1 and stats["ok"] == 1
    assert fetched == [SPIRIT_EMOJI_URL]
    row = conn.execute(
        "SELECT media_local_path, media_url FROM messages WHERE msg_id='srv_520'").fetchone()
    assert row["media_local_path"].startswith("emoji/")
    assert row["media_url"] == SPIRIT_EMOJI_URL
    # 再跑一次不再出现
    assert media_backfill.iter_media_rows(conn) == []
    conn.close()


def test_spaced_json_is_still_found(temp_db, media_dirs, monkeypatch):
    """别的写入路径会把 JSON 存成带空格的写法，粗筛也得能命中。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_519", "c1", 1, msg_type=0, content="笑死",
                   raw_data=json.dumps({"content_json": json.dumps(
                       {"aweType": 519, "display_name": "笑死",
                        "url": {"url_list": [EMOJI_URL]}}, ensure_ascii=False)}, ensure_ascii=False))
    assert [r["msg_id"] for r in media_backfill.iter_media_rows(conn)] == ["srv_519"]
    monkeypatch.setattr(card_icons, "_fetch_icon", lambda url: (b"x" * 200, "image/webp"))
    monkeypatch.setattr(web_scraper, "_save_emoji",
                        lambda url, directory: _write_emoji(directory, url))
    assert media_backfill.backfill_media(conn)["ok"] == 1
    conn.close()


def test_scrape_end_pass_downloads_missing_media(temp_db, media_dirs, monkeypatch):
    """采集收尾那一步真的能补下表情/卡片图。

    回归用例：以前这步挂在「每个会话抓完」的位置上，还把主线程开的 sqlite 连接丢进
    线程池（sqlite 不允许跨线程用连接），于是每次都 ProgrammingError 被吞掉 —— 等于
    从来没补过。这里要求它真的把图下下来。
    """
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_519", "c1", 1, msg_type=0, content="笑死",
                   raw_data=_monster_emoji_row())
    conn.commit()

    monkeypatch.setattr(web_scraper, "_save_emoji",
                        lambda url, directory: _write_emoji(directory, url))
    scraper = web_scraper.WebChatScraper()
    scraper._db_conn = conn          # 主线程的连接，和真实采集一样
    asyncio.run(scraper._backfill_missing_media())

    row = conn.execute("SELECT media_local_path FROM messages WHERE msg_id='srv_519'").fetchone()
    assert row["media_local_path"].startswith("emoji/")
    conn.close()


def test_extract_all_runs_the_media_pass_once(temp_db, monkeypatch):
    """整轮采集收尾只补一次（以前是每个会话补一次，白扫全库）。"""
    from extractor import web_scraper

    calls = []

    async def fake_nav(self):
        return None

    async def fake_convs(self):
        return [{"name": "会话A", "nickname": "", "time": "-"},
                {"name": "会话B", "nickname": "", "time": "-"}]

    async def fake_conversation(self, index, conv, refresh=False):
        return None

    async def fake_media(self):
        calls.append("media")

    async def fake_live(self):
        calls.append("live")

    monkeypatch.setattr(web_scraper.WebChatScraper, "navigate_to_chat", fake_nav)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_load_all_conversations", fake_convs)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_extract_conversation", fake_conversation)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_backfill_missing_media", fake_media)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_backfill_live_photo_videos", fake_live)

    scraper = web_scraper.WebChatScraper()
    scraper._db_conn = database.get_db()
    asyncio.run(scraper.extract_all())
    assert calls == ["media", "live"]      # 两个会话也只各跑一次
    scraper._db_conn.close()


def _write_emoji(directory, url):
    os.makedirs(directory, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest()[:16] + ".gif"
    with open(os.path.join(directory, name), "wb") as handle:
        handle.write(b"GIF89a" + b"x" * 200)
    return "emoji/" + name


# 「彩蛋福袋」卡（aweType=110360 · card_key=msg_easter_redpack）：老库里缺的插图
# 也要靠「回填媒体」补下来（上半部分那张图是带签名的临时链接，过期就没了）。
EASTER_COVER = (
    "https://p26-infras.byteimg.com/static-assets/"
    "example-cover.png~tplv-example-75compress.image")


def _easter_row():
    return _raw({
        "aweType": 110360, "description": "[彩蛋福袋]",
        "im_dynamic_patch": {
            "card_key": "msg_easter_redpack",
            "card_type": "pay_msg_easter_redpack_card",
            "raw_data": json.dumps({
                "top": {"cover_image": EASTER_COVER, "type": "im-image"},
                "content_content": {"content": "拆福袋", "type": "im-button"},
            }, ensure_ascii=False),
        },
    })


def test_easter_redpack_card_is_a_backfill_candidate(temp_db, media_dirs, monkeypatch):
    """只给了 card_key 的老行也要被粗筛捞出来，并把插图存进 card_icons/。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_easter", "c1", 1, msg_type=0, content="[彩蛋福袋]",
                   raw_data=_easter_row())
    assert [r["msg_id"] for r in media_backfill.iter_media_rows(conn)] == ["srv_easter"]

    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (b"\x89PNG" + b"x" * 200, "image/png"))
    stats = media_backfill.backfill_media(conn)
    assert stats["ok"] == 1
    path = conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_easter'").fetchone()[0]
    assert path.startswith("card_icons/")
    assert card_icons.find_card_icon(EASTER_COVER) == path
    conn.close()

# ── 分享卡封面（视频 / 图文 / 动图 / 文章）────────────────────────────────
# 用户 2026-11-08 报的问题：分享卡显示「封面已失效」，点进去抖音官网视频却能播。
# 原因是封面只是一条**带签名的临时链接**（约一年过期），以前只记链接不下载；
# 视频靠作品 id 现场换新地址，所以还能播。现在封面也存进 card_icons/。
SHARE_COVER = (
    "https://p3-sign.douyinpic.com/obj/tos-cn-i-example/share-cover.jpeg"
    "?lk3s=example&x-expires=1799999999&x-signature=example"
)


def _share_row(cj=None):
    return _raw(cj or {
        "aweType": 800,
        "content_title": "示例作品标题",
        "content_name": "示例作者",
        "itemId": "1000000000000000009",
        "cover_width": 576,
        "cover_height": 1128,
        "cover_url": {"url_list": [SHARE_COVER]},
    })


def test_share_cover_url_reads_every_payload_shape():
    """封面可能藏在四个字段里，外加 11054 这类排版卡的 top.content。"""
    assert share_cover_url({"aweType": 800, "cover_url": {"url_list": [SHARE_COVER]}}) == SHARE_COVER
    # cover_info.resource_url
    assert share_cover_url({
        "aweType": 801, "cover_info": {"resource_url": {"url_list": [SHARE_COVER]}},
    }) == SHARE_COVER
    # content_thumb
    assert share_cover_url({
        "aweType": 803, "content_thumb": {"url_list": [SHARE_COVER]},
    }) == SHARE_COVER
    # 动态排版卡（11054）：封面在 im_dynamic_patch.raw_data 的 top.content 里
    assert share_cover_url({
        "aweType": 11054,
        "im_dynamic_patch": {"raw_data": json.dumps({"top": {"content": SHARE_COVER}})},
    }) == SHARE_COVER


def test_share_cover_url_ignores_non_work_shares():
    """商品卡 / 评论卡 / 群邀请卡不是「大封面」卡，不该被这里认领。"""
    payload = {"cover_url": {"url_list": [SHARE_COVER]}}
    assert share_cover_url({"aweType": 11029, **payload}) == ""      # 商品
    assert share_cover_url({"aweType": 10500, **payload}) == ""      # 分享评论
    assert share_cover_url({"aweType": 6001, **payload}) == ""       # 豆包卡（另有通道）
    assert share_cover_url({"aweType": 800}) == ""                   # 没有封面
    assert share_cover_url(None) == ""


def test_share_cover_is_a_backfill_candidate(temp_db, media_dirs):
    """库里缺封面的分享消息要进待补清单（老库里的那些）。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_share", "c1", 1, msg_type=4, content="",
                   media_url=SHARE_COVER, raw_data=_share_row())
    assert [r["msg_id"] for r in media_backfill.iter_media_rows(conn)] == ["srv_share"]
    conn.close()


def test_share_cover_backfill_saves_into_card_icons(temp_db, media_dirs, monkeypatch):
    """补下来之后存进 card_icons/，并记上路径，下次不再当成缺图。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_share", "c1", 1, msg_type=4, content="",
                   raw_data=_share_row())

    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (b"\xff\xd8\xff" + b"x" * 200, "image/jpeg"))
    stats = media_backfill.backfill_media(conn)
    assert stats["total"] == 1 and stats["ok"] == 1

    path = conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_share'").fetchone()[0]
    assert path.startswith("card_icons/")
    # .jpeg 统一规范成 .jpg（见 card_icons.icon_extension）
    assert path.endswith(".jpg")
    # 文件真的落地了，并且 find_card_icon 认得同一个链接
    assert card_icons.find_card_icon(SHARE_COVER) == path
    # 再跑一次不再出现（不会每轮采集都白扫）
    assert media_backfill.iter_media_rows(conn) == []
    conn.close()


def test_expired_share_cover_link_counts_as_failed(temp_db, media_dirs, monkeypatch):
    """链接已经过期（403）的补不回来：算一次失败，但别把整轮打断。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_share", "c1", 1, msg_type=4, content="",
                   raw_data=_share_row())
    monkeypatch.setattr(card_icons, "_fetch_icon", lambda url: (None, ""))
    stats = media_backfill.backfill_media(conn)
    assert stats["total"] == 1 and stats["ok"] == 0 and stats["failed"] == 1
    conn.close()


def test_scrape_downloads_the_share_cover_inline(temp_db, media_dirs, monkeypatch):
    """采集转换时就把封面存下来（不等收尾），并写进 media_local_path。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (b"\xff\xd8\xff" + b"x" * 200, "image/jpeg"))

    scraper = object.__new__(web_scraper.WebChatScraper)
    scraper._db_conn = conn
    msg = {
        "server_id": "1000000000000000051",
        "content": "",
        "msg_type": "share",
        "sender_uid": "u1",
        "sender_name": "",
        "is_self": False,
        "created_at": "2026-11-08T12:00:00Z",
        "order_high": 1,
        "order_low": 1,
        "content_json": json.dumps({"aweType": 800, "cover_url": {"url_list": [SHARE_COVER]}}),
        # 真实采集里这两项都由转换循环填：image_src 记原始链接（进 media_url），
        # share_cover_dir 是 _download_share_cover 存下来的本地路径。
        "image_src": SHARE_COVER,
        "share_cover_dir": card_icons.save_card_icon(SHARE_COVER),
    }
    inserted, ids, _ = scraper._store_messages([msg], "c1")
    assert inserted == 1
    row = conn.execute(
        "SELECT media_local_path, media_url FROM messages WHERE msg_id = ?", (ids[0],)
    ).fetchone()
    assert row["media_local_path"].startswith("card_icons/")
    assert row["media_url"] == SHARE_COVER
    conn.close()


def test_share_cover_backfill_runs_only_once(temp_db, media_dirs, monkeypatch):
    """全库回填**只做一次**：标记文件写下去之后，下一轮采集直接跳过。"""
    from common import paths as common_paths

    marker = tmp_path_marker = media_dirs / "share_cover_backfill_done"
    monkeypatch.setattr(common_paths, "SHARE_COVER_BACKFILL_DONE", str(tmp_path_marker))

    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_share", "c1", 1, msg_type=4, content="",
                   raw_data=_share_row())
    conn.commit()

    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (b"\xff\xd8\xff" + b"x" * 200, "image/jpeg"))

    scraper = web_scraper.WebChatScraper()
    scraper._db_conn = conn
    asyncio.run(scraper._backfill_share_covers_once())
    # 第一次：补下来了，标记也写了
    assert marker.exists()
    assert conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_share'"
    ).fetchone()[0].startswith("card_icons/")

    # 第二次：新加一条缺封面的，回填不该再跑（标记文件在）
    insert_message(conn, "srv_share2", "c1", 2, msg_type=4, content="",
                   media_url=SHARE_COVER,
                   raw_data=_raw({"aweType": 800, "cover_url": {"url_list": [SHARE_COVER + "&n=2"]}}))
    conn.commit()
    asyncio.run(scraper._backfill_share_covers_once())
    assert conn.execute(
        "SELECT media_local_path FROM messages WHERE msg_id='srv_share2'"
    ).fetchone()[0] is None      # 没被回填（只做一次）
    conn.close()

def test_share_cover_logs_are_not_scrape_errors():
    """新加的日志不能被面板当成采集出错。

    面板把「[media] … 失败」当错误弹框、并把这轮采集暂停两分钟
    （backend/control_panel.py 的 _MEDIA_FAILURE_PATTERNS）。补不上的封面
    多半是链接早就过期，属于正常情况，日志里不能出现会命中规则的字样。
    """
    from backend.control_panel import classify_scrape_log_line

    lines = [
        "  [media] 分享卡封面回填完成: 待补 5 条，已补 4 条，另有 1 张链接已过期没能补",
        "  [media] 分享卡封面回填完成: 老库里没有缺封面的消息",
        "  [media] 已存到本地: 卡片图 2 张，分享卡封面 3 张",
        "  [media] 首次升级：开始全库补齐分享卡封面（只补这一次）...",
    ]
    for line in lines:
        assert classify_scrape_log_line(line) is None, line


def test_share_cover_backfill_prints_progress(temp_db, media_dirs, monkeypatch, capsys):
    """回填时日志里要能看见「开始」和「完成」，用户才知道它在干活。"""
    from common import paths as common_paths

    marker = media_dirs / "share_cover_backfill_done"
    monkeypatch.setattr(common_paths, "SHARE_COVER_BACKFILL_DONE", str(marker))

    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_share", "c1", 1, msg_type=4, content="",
                   raw_data=_share_row())
    conn.commit()

    monkeypatch.setattr(card_icons, "_fetch_icon",
                        lambda url: (b"\xff\xd8\xff" + b"x" * 200, "image/jpeg"))

    scraper = web_scraper.WebChatScraper()
    scraper._db_conn = conn
    asyncio.run(scraper._backfill_share_covers_once())

    out = capsys.readouterr().out
    assert "开始全库补齐分享卡封面" in out
    assert "分享卡封面回填完成" in out
    conn.close()


