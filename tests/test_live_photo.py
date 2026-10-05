"""实况图（抖音的「会动的图」）里的那段小视频。

用户 2026-10-05 的要求：采集时自动补全以前没下过小视频的实况图，查看器里悬停即播放。
实况图 = 静态封面 + 一段两三秒的小视频：

* 封面：``resource_url``（AES-GCM，本来就是按图片下载的，不动）；
* 小视频：``live_photo_video``，字段形状和普通聊天视频的 ``video`` 一样
  （tkey/skey/vid），所以复用 ``video_downloader`` 那套 batch_play_info + CENC 解密。

关键是**两列分开**：小视频写 ``live_video_path``，不能覆盖封面的 ``media_local_path``。
"""
import asyncio
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import database
from extractor import video_downloader
from tests.conftest import insert_conversation, insert_message

# 全是假值：消息 id 只要形状对（雪花数字），tkey/skey 只要能挑出来即可，
# 真实聊天里的 id 与密钥一律不进仓库。
SID = "7600000000000000123"
LIVE_TKEY = "vid-v0000fake00000000000000000000ab"
LIVE_SKEY = "00112233445566778899aabbccddeeff"
VIDEO_TKEY = "vid-v0000fake00000000000000000000cd"
_COVER_SKEY = "1a" * 32      # resource_url.skey（AES-256-GCM，静态封面用）


def _gcm(plain, key_hex):
    nonce = bytes(range(12))
    return nonce + AESGCM(bytes.fromhex(key_hex)).encrypt(nonce, plain, None)


def _raw(cj):
    return json.dumps({"content_json": json.dumps(cj, separators=(",", ":"))})


def _live_photo_cj():
    return {
        "aweType": 2704,
        "resource_url": {"skey": _COVER_SKEY, "origin_url_list": ["https://cdn.example.com/img"]},
        "live_photo_video": {"is_new_encrypt": 1, "skey": LIVE_SKEY, "tkey": LIVE_TKEY,
                             "vid": "v0000fake00000000000000000000ab"},
    }


def _video_cj():
    return {"aweType": 0, "poster": {},
            "video": {"skey": "00" * 16, "tkey": VIDEO_TKEY, "vid": "v1"}}


def _seed(conn):
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, f"srv_{SID}", "c1", 1, msg_type=3, content="[图片]",
                   media_local_path=f"images/{SID}.jpg",
                   raw_data=_raw(_live_photo_cj()))
    insert_message(conn, "srv_video", "c1", 2, msg_type=5, content="[视频]",
                   raw_data=_raw(_video_cj()))


@pytest.fixture()
def media_dirs(tmp_path, monkeypatch):
    """images/videos 指到临时目录，别碰真实的 data/media。"""
    import extractor.im_media as im

    images, videos = tmp_path / "images", tmp_path / "videos"
    for path in (images, videos):
        path.mkdir()
    monkeypatch.setattr(im, "IMAGES_DIR", str(images))
    monkeypatch.setattr(im, "VIDEOS_DIR", str(videos))
    monkeypatch.setattr(im, "MEDIA_DIR", str(tmp_path))
    return images, videos


def test_live_photo_cenc_reads_the_video_payload():
    from extractor.im_media import live_photo_cenc

    assert live_photo_cenc(_live_photo_cj()) == {"tkey": LIVE_TKEY, "skey": LIVE_SKEY}
    # 普通视频没有 live_photo_video；半截载荷（缺 skey）也不算数
    assert live_photo_cenc(_video_cj()) is None
    assert live_photo_cenc({"live_photo_video": {"tkey": "t"}}) is None
    assert live_photo_cenc(None) is None


def test_live_photo_jobs_only_picks_live_photos(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(video_downloader, "VIDEOS_DIR", str(tmp_path / "videos"))
    conn = database.get_db()
    _seed(conn)

    jobs = video_downloader.live_photo_jobs(conn)
    assert [j["msg_id"] for j in jobs] == [f"srv_{SID}"]
    assert jobs[0]["kind"] == "live" and jobs[0]["file_id"] == SID
    # 这条老数据已经有静态封面了，只需要补小视频
    assert jobs[0]["needs_cover"] is False

    # 两份都缺，普通视频那条只出现在总清单里
    all_jobs = video_downloader.pending_videos(conn)
    assert {j["kind"] for j in all_jobs} == {"video", "live"}

    # 视频下好之后就不再是待办（实况图的静态封面还在 media_local_path 里，
    # 不会因为「已经有 mp4」被当成下过）
    conn.execute("UPDATE messages SET live_video_path=? WHERE msg_id=?",
                 (f"videos/{SID}.mp4", f"srv_{SID}"))
    conn.commit()
    assert video_downloader.live_photo_jobs(conn) == []
    conn.close()


def test_save_cenc_jobs_writes_live_video_path_without_touching_the_cover(
        temp_db, tmp_path, monkeypatch):
    videos = tmp_path / "videos"
    monkeypatch.setattr(video_downloader, "VIDEOS_DIR", str(videos))
    conn = database.get_db()
    conn.execute("PRAGMA foreign_keys=ON")
    _seed(conn)

    async def fake_resolve(page, tkeys):
        return {t: f"https://cdn.example.com/{t}" for t in tkeys}

    def fake_download(url, timeout=60):
        return b"x" * 2048

    def fake_process(enc_bytes, skey_hex, out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(b"plain-mp4")
        return os.path.getsize(out_path)

    monkeypatch.setattr(video_downloader, "_resolve_batch", fake_resolve)
    monkeypatch.setattr(video_downloader, "_download", fake_download)
    monkeypatch.setattr(video_downloader, "_process_one", fake_process)

    jobs = video_downloader.pending_videos(conn)
    result = asyncio.run(video_downloader.save_cenc_jobs(None, jobs, conn=conn))
    assert result["ok"] == 2 and result["fail"] == 0

    live = conn.execute("SELECT media_local_path, live_video_path FROM messages "
                        "WHERE msg_id=?", (f"srv_{SID}",)).fetchone()
    assert live["live_video_path"] == f"videos/{SID}.mp4"
    assert live["media_local_path"] == f"images/{SID}.jpg"     # 封面没被顶掉

    video = conn.execute("SELECT media_local_path, live_video_path FROM messages "
                         "WHERE msg_id='srv_video'").fetchone()
    assert video["media_local_path"] == "videos/srv_video.mp4"
    assert video["live_video_path"] is None
    conn.close()


def test_missing_cover_is_filled_by_the_clip_pass(temp_db, media_dirs, monkeypatch):
    """开关关着时新收的实况图：封面还没下 → 补小视频那一轮顺手把封面补上。"""
    images, _ = media_dirs
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, f"srv_{SID}", "c1", 1, msg_type=3, content="[图片]",
                   raw_data=_raw(_live_photo_cj()))

    jobs = video_downloader.live_photo_jobs(conn)
    assert len(jobs) == 1 and jobs[0]["needs_cover"] is True

    jpeg = b"\xff\xd8\xff" + b"JPEGDATA" * 8
    monkeypatch.setattr("extractor.im_media._fetch",
                        lambda url, timeout=20: _gcm(jpeg, _COVER_SKEY))
    assert video_downloader.ensure_live_photo_covers(conn, jobs) == 1

    row = conn.execute("SELECT media_local_path FROM messages WHERE msg_id=?",
                       (f"srv_{SID}",)).fetchone()
    assert row["media_local_path"] == f"images/{SID}.jpg"
    assert (images / f"{SID}.jpg").read_bytes() == jpeg
    # 封面补上之后这一轮就只剩小视频要补了
    assert video_downloader.live_photo_jobs(conn)[0]["needs_cover"] is False
    conn.close()


def test_cover_only_jobs_enter_the_queue(temp_db):
    """小视频下好了但封面还缺（比如封面链接当时是坏的）也要留在队列里补封面。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, f"srv_{SID}", "c1", 1, msg_type=3, content="[图片]",
                   live_video_path=f"videos/{SID}.mp4",
                   raw_data=_raw(_live_photo_cj()))

    jobs = video_downloader.live_photo_jobs(conn)
    assert len(jobs) == 1 and jobs[0]["needs_cover"] is True
    conn.close()


def test_scraper_backfills_live_photos_after_scrape(temp_db, tmp_path, monkeypatch):
    """采集收尾那一步：拿到实况图任务就交给同一套下载流程（用页面里的登录态）。"""
    from extractor import web_scraper

    monkeypatch.setattr(video_downloader, "VIDEOS_DIR", str(tmp_path / "videos"))
    seen = {}

    def fake_jobs(conn, conv_id=None, limit=None):
        return [{"msg_id": f"srv_{SID}", "file_id": SID, "kind": "live",
                 "tkey": LIVE_TKEY, "skey": LIVE_SKEY, "needs_cover": True}]

    def fake_covers(conn, jobs, limit=None):
        seen["covers"] = jobs
        return 1

    async def fake_save(page, jobs, **kwargs):
        seen["page"] = page
        seen["jobs"] = jobs
        seen["conn"] = kwargs.get("conn")
        return {"total": len(jobs), "ok": len(jobs), "fail": 0, "skipped": 0, "log": []}

    monkeypatch.setattr(video_downloader, "live_photo_jobs", fake_jobs)
    monkeypatch.setattr(video_downloader, "ensure_live_photo_covers", fake_covers)
    monkeypatch.setattr(video_downloader, "save_cenc_jobs", fake_save)

    scraper = web_scraper.WebChatScraper()
    scraper.page = "PAGE"
    scraper._db_conn = database.get_db()
    asyncio.run(scraper._backfill_live_photo_videos())

    assert seen["page"] == "PAGE" and seen["conn"] is scraper._db_conn
    assert seen["jobs"][0]["kind"] == "live"
    assert seen["covers"] is seen["jobs"]      # 补小视频的同一轮里也补封面
    scraper._db_conn.close()


def test_extract_all_runs_the_live_photo_pass(temp_db, monkeypatch):
    """整轮采集结束前一定会补实况图（老消息漏下的也在这时候补上）。"""
    from extractor import web_scraper

    called = []

    async def fake_nav(self):
        return None

    async def fake_convs(self):
        return [{"name": "会话", "nickname": "", "time": "-"}]

    async def fake_conversation(self, index, conv, refresh=False):
        return None

    async def fake_live(self):
        called.append(True)

    monkeypatch.setattr(web_scraper.WebChatScraper, "navigate_to_chat", fake_nav)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_load_all_conversations", fake_convs)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_extract_conversation", fake_conversation)
    monkeypatch.setattr(web_scraper.WebChatScraper, "_backfill_live_photo_videos", fake_live)

    scraper = web_scraper.WebChatScraper()
    scraper._db_conn = database.get_db()
    asyncio.run(scraper.extract_all())
    assert called == [True]
    scraper._db_conn.close()


# ── 合并转发里的内嵌实况图 ────────────────────────────────────────────────
# 内嵌消息没有自己的数据库行（live_video_path 那列写不上），所以小视频按文件名
# 约定认：videos/<消息id>.mp4。

def test_collect_live_photo_jobs_from_forwarded_bodies(media_dirs):
    from extractor.im_media import collect_live_photo_jobs

    images, videos = media_dirs
    bodies = [
        {"server_message_id": int(SID), "content": json.dumps(_live_photo_cj())},
        {"server_message_id": 999, "content": json.dumps(_video_cj())},
    ]
    assert collect_live_photo_jobs(bodies=bodies) == [{
        "file_id": SID, "kind": "live", "tkey": LIVE_TKEY, "skey": LIVE_SKEY,
        "msg_id": f"srv_{SID}",
    }]
    # 只有静态封面（图片先到）不算下过，要的是那段小视频
    (images / f"{SID}.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 20)
    assert len(collect_live_photo_jobs(bodies=bodies)) == 1
    (videos / f"{SID}.mp4").write_bytes(b"mp4")
    assert collect_live_photo_jobs(bodies=bodies) == []


def test_reader_exposes_forwarded_live_photo_clip(temp_db, media_dirs):
    from backend import database
    from backend.forwarded import resolve_forward

    _, videos = media_dirs
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    message = {
        "msg_id": "srv_parent",
        "raw_data": json.dumps({"content_json": json.dumps({
            "aweType": 13600,
            "msg_ids": [{"msg_id": int(SID)}],
            "inline_content": [{
                "server_message_id": int(SID), "sender": 1, "create_time": 1700000000,
                "content": json.dumps(_live_photo_cj(), ensure_ascii=False),
            }],
        }, ensure_ascii=False)}),
    }

    item = resolve_forward(message, conn, fetch_media=False)["items"][0]
    assert item["msg_type"] == 3 and item["live_video_path"] is None

    (videos / f"{SID}.mp4").write_bytes(b"mp4")
    item = resolve_forward(message, conn, fetch_media=False)["items"][0]
    assert item["live_video_path"] == f"videos/{SID}.mp4"
    conn.close()
