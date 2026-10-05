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
from extractor import media_backfill, web_scraper
from tests.conftest import insert_conversation, insert_message

EMOJI_URL = "https://cdn.example.com/emoji/monster.webp"
COVER_URL = "https://p3-sign.douyinpic.com/obj/tos-cn-i-example/doubao-cover.jpeg"
GROUP_ICON = "https://p3-aweme-im-img.byteimg.com/tos-cn-i-example/group-icon.webp"


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


def test_emoji_url_prefers_recorded_media_url():
    cj = {"aweType": 519, "display_name": "续火花", "url": {"url_list": [EMOJI_URL]}}
    assert media_backfill.emoji_media_url(cj) == EMOJI_URL
    assert media_backfill.emoji_media_url(cj, "https://cdn/recorded.webp") == "https://cdn/recorded.webp"
    assert media_backfill.emoji_media_url({"aweType": 519}) == ""
    # 坏掉的 media_url（空串 / 不是链接）不算数，回载荷里找
    assert media_backfill.emoji_media_url(cj, "") == EMOJI_URL


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
