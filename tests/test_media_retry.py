"""补不下来的图别再每轮重试：失败记进库，下次跳过（见 common/media_retry.py）。

用户反馈：每轮采集收尾都要为早就过期的那批图（实测 1876 条）各发一次请求，
采集结束前白白多等好几分钟。这里锁住新行为：链接过期记成永久失败，下次一次
请求都不发；网络抽风只冷却，过几天还会再试。
"""
import json
import os
import urllib.error

import pytest

from backend import database
from common import card_icons, media_retry
from extractor import media_backfill
from tests.conftest import insert_conversation, insert_message

COVER_URL = "https://p3-sign.douyinpic.com/obj/tos-cn-i-example/expired.jpeg"


@pytest.fixture()
def media_dirs(tmp_path, monkeypatch):
    """表情存 tmp/emoji，卡片图存 tmp/card_icons —— 别碰真实的 data/media。"""
    monkeypatch.setattr(media_backfill, "EMOJI_DIR", str(tmp_path / "emoji"))
    monkeypatch.setattr(card_icons, "MEDIA_DIR", str(tmp_path))
    (tmp_path / card_icons.CARD_ICON_DIR_NAME).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _doubao_row():
    cj = {"aweType": 6001, "title": "《音乐公开课》", "icon": {"url_list": [COVER_URL]}}
    return json.dumps(
        {"content_json": json.dumps(cj, ensure_ascii=False, separators=(",", ":"))},
        ensure_ascii=False,
    )


def _seed_missing_cover(conn):
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_music", "c1", 1, msg_type=1,
                   content="《音乐公开课》", raw_data=_doubao_row())


def _expired_403():
    return urllib.error.HTTPError(COVER_URL, 403, "Forbidden", {}, None)


def test_classify_treats_403_as_permanent():
    permanent, reason = media_retry.classify(_expired_403())
    assert permanent is True
    assert "403" in reason
    permanent, _reason = media_retry.classify(TimeoutError("timed out"))
    assert permanent is False


def test_expired_link_is_recorded_and_skipped_next_time(temp_db, media_dirs, monkeypatch):
    conn = database.get_db()
    _seed_missing_cover(conn)

    def fail(url, error_out=None):
        if error_out is not None:
            error_out.append(_expired_403())
        return None

    monkeypatch.setattr(card_icons, "save_card_icon", fail)
    first = media_backfill.backfill_media(conn)
    assert first["total"] == 1 and first["failed"] == 1 and first["skipped"] == 0
    assert media_retry.should_skip(conn, "srv_music", "media", COVER_URL)

    calls = []

    def unexpected(url, error_out=None):
        calls.append(url)
        return None

    monkeypatch.setattr(card_icons, "save_card_icon", unexpected)
    second = media_backfill.backfill_media(conn)
    # 一次请求都没发：直接按记录跳过
    assert second["total"] == 0 and second["skipped"] == 1
    assert calls == []
    conn.close()


def test_force_retries_the_ones_that_were_skipped(temp_db, media_dirs, monkeypatch):
    conn = database.get_db()
    _seed_missing_cover(conn)

    def fail(url, error_out=None):
        if error_out is not None:
            error_out.append(_expired_403())
        return None

    monkeypatch.setattr(card_icons, "save_card_icon", fail)
    assert media_backfill.backfill_media(conn)["failed"] == 1

    calls = []

    def ok(url, error_out=None):
        calls.append(url)
        rel = "card_icons/brought-back.jpg"
        target = os.path.join(card_icons.MEDIA_DIR, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"x" * 200)
        return rel

    monkeypatch.setattr(card_icons, "save_card_icon", ok)
    stats = media_backfill.backfill_media(conn, force=True)
    assert calls == [COVER_URL]
    assert stats["ok"] == 1
    # 补成功了，失败记录也跟着清掉
    assert not media_retry.should_skip(conn, "srv_music", "media", COVER_URL)
    conn.close()


def test_transient_failure_only_cools_down_for_a_while(temp_db, media_dirs):
    conn = database.get_db()
    media_retry.note_failure(conn, "srv_x", "media", signature=COVER_URL,
                             exc=TimeoutError("timed out"))
    conn.commit()
    reason = media_retry.should_skip(conn, "srv_x", "media", COVER_URL)
    assert reason and "天" in reason
    # 地址换了（重新拿到新签名的链接）：重新试
    assert media_retry.should_skip(conn, "srv_x", "media", "https://cdn.example.com/new.jpg") is None
    # 冷却时间过了：也重新试
    conn.execute("UPDATE media_retry SET last_at=0 WHERE msg_id='srv_x'")
    conn.commit()
    assert media_retry.should_skip(conn, "srv_x", "media", COVER_URL) is None
    conn.close()


def test_clear_forgets_recorded_failures(temp_db, media_dirs):
    conn = database.get_db()
    media_retry.note_failure(conn, "srv_live", "live", reason="没有地址")
    conn.commit()
    assert media_retry.count(conn, "live") == 1
    assert media_retry.clear(conn, kinds=("live",)) == 1
    assert media_retry.should_skip(conn, "srv_live", "live") is None
    conn.close()
