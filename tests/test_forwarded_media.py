import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.forwarded import resolve_forward
from extractor.im_media import (
    collect_cenc_jobs, download_direct_media, find_local_media, materialize_bodies,
)
from tests.conftest import insert_conversation, insert_message


KEY = "12" * 32
SID = "1000000000000000046"


def _gcm(plain: bytes) -> bytes:
    nonce = bytes(range(12))
    return nonce + AESGCM(bytes.fromhex(KEY)).encrypt(nonce, plain, None)


def _patch_media(monkeypatch, tmp_path):
    import extractor.im_media as im
    images, videos, emoji, voice = tmp_path / "images", tmp_path / "videos", tmp_path / "emoji", tmp_path / "voice"
    for d in (images, videos, emoji, voice):
        d.mkdir()
    monkeypatch.setattr(im, "IMAGES_DIR", str(images))
    monkeypatch.setattr(im, "VIDEOS_DIR", str(videos))
    monkeypatch.setattr(im, "EMOJI_DIR", str(emoji))
    monkeypatch.setattr(im, "VOICE_DIR", str(voice))
    monkeypatch.setattr(im, "MEDIA_DIR", str(tmp_path))
    return images, videos


def test_find_local_media_accepts_numeric_or_srv_name(tmp_path, monkeypatch):
    images, _ = _patch_media(monkeypatch, tmp_path)
    (images / f"{SID}.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 20)
    assert find_local_media(SID) == f"images/{SID}.jpg"
    assert find_local_media(f"srv_{SID}") == f"images/{SID}.jpg"
    assert find_local_media(f"parent/srv_{SID}") == f"images/{SID}.jpg"


def test_download_image_decrypts_origin(tmp_path, monkeypatch):
    _patch_media(monkeypatch, tmp_path)
    jpeg = b"\xff\xd8\xff" + b"JPEGDATA" * 8
    cipher = _gcm(jpeg)

    def fake_fetch(url, timeout=20):
        assert url.startswith("https://")
        return cipher

    monkeypatch.setattr("extractor.im_media._fetch", fake_fetch)
    path = download_direct_media({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": ["https://example.com/img"]},
        "inline_pic": "thumb",
    }, SID)
    # 存档名用链接里那截名字（img），不再用消息号
    assert path == "images/img.jpg"
    assert os.path.getsize(os.path.join(tmp_path, path)) == len(jpeg)


def test_image_file_name_comes_from_the_link(tmp_path, monkeypatch):
    """链接尾巴上有正经文件名时就用它（抖音的精灵素材就是这种）。"""
    from extractor.im_media import _server_image_name

    assert _server_image_name(
        "https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_card.png"
    ) == "hotstick_card.png"
    # 上传图的链接带图片处理后缀，砍掉它才是真名字
    assert _server_image_name(
        "https://p26-sign.douyinpic.com/tos-cn-o-00061/uploadv2_SrAMM999fqifpMPD~tplv-x-get:.image?lk3s=1"
    ) == "uploadv2_SrAMM999fqifpMPD"
    # 给不出安全的名字时返回空串，调用方退回老命名
    assert _server_image_name("") == ""
    assert _server_image_name("https://example.com/") == ""
    assert _server_image_name("https://example.com/..%2Fbad") == ""

    images, _ = _patch_media(monkeypatch, tmp_path)
    jpeg = b"\xff\xd8\xff" + b"JPEGDATA" * 8
    monkeypatch.setattr("extractor.im_media._fetch", lambda url, timeout=20: _gcm(jpeg))
    path = download_direct_media({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": [
            "https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_card.png"]},
    }, SID)
    assert path == "images/hotstick_card.png"
    assert (images / "hotstick_card.png").read_bytes() == jpeg
    # 再问一次不该重新下载（按链接里的文件名找得到）
    assert download_direct_media({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": [
            "https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_card.png"]},
    }, SID) == "images/hotstick_card.png"


def test_same_name_different_image_does_not_overwrite(tmp_path, monkeypatch):
    """两条链接名字撞车但图不一样时，第二张加一小截哈希区分，别把第一张盖掉。"""
    images, _ = _patch_media(monkeypatch, tmp_path)
    first = b"\xff\xd8\xff" + b"FIRST" * 4
    second = b"\xff\xd8\xff" + b"SECOND" * 4
    monkeypatch.setattr("extractor.im_media._fetch", lambda url, timeout=20: _gcm(first))
    a = download_direct_media({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": ["https://cdn.example.com/a/same.jpg"]},
    }, "1000000000000000001")
    assert a == "images/same.jpg"

    monkeypatch.setattr("extractor.im_media._fetch", lambda url, timeout=20: _gcm(second))
    b = download_direct_media({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": ["https://cdn.example.com/b/same.jpg"]},
    }, "1000000000000000002")
    assert b != a and b.startswith("images/same-") and b.endswith(".jpg")
    assert (images / "same.jpg").read_bytes() == first

    # 同一张图再下一次时按内容认出已有文件，不再另存一份
    monkeypatch.setattr("extractor.im_media._fetch", lambda url, timeout=20: _gcm(first))
    assert download_direct_media({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": ["https://cdn.example.com/a/same.jpg"]},
    }, "1000000000000000001") == "images/same.jpg"


def test_resolve_forward_attaches_decrypted_image(temp_db, tmp_path, monkeypatch):
    images, _ = _patch_media(monkeypatch, tmp_path)
    jpeg = b"\xff\xd8\xff" + b"JPEGDATA" * 8
    monkeypatch.setattr("extractor.im_media._fetch", lambda url, timeout=20: _gcm(jpeg))
    from backend import database
    conn = database.get_db()
    message = {
        "msg_id": "srv_parent",
        "raw_data": json.dumps({"content_json": json.dumps({
            "aweType": 13600,
            "msg_ids": [{"msg_id": int(SID)}],
            "inline_content": [{
                "server_message_id": int(SID),
                "sender": 1,
                "create_time": 1700000000,
                "content": json.dumps({
                    "aweType": 2702,
                    "resource_url": {"skey": KEY, "origin_url_list": ["https://example.com/img"]},
                    "inline_pic": "thumb",
                }),
            }],
        })}),
    }
    result = resolve_forward(message, conn)
    assert result["complete"]
    assert result["items"][0]["media_local_path"] == "images/img.jpg"
    conn.close()


def test_failed_fetch_leaves_row_without_local_file(temp_db, tmp_path, monkeypatch):
    _patch_media(monkeypatch, tmp_path)
    monkeypatch.setattr("extractor.im_media._fetch", lambda url, timeout=20: (_ for _ in ()).throw(OSError("offline")))
    from backend import database
    conn = database.get_db()
    message = {
        "msg_id": "srv_parent",
        "raw_data": json.dumps({"content_json": json.dumps({
            "aweType": 13600,
            "msg_ids": [{"msg_id": 42}],
            "inline_content": [{
                "server_message_id": 42, "content": json.dumps({
                    "aweType": 2702,
                    "resource_url": {"skey": KEY, "origin_url_list": ["https://example.com/img"]},
                    "inline_pic": "thumb",
                }),
            }],
        })}),
    }
    item = resolve_forward(message, conn)["items"][0]
    assert item["msg_type"] == 3
    assert not item.get("media_local_path")
    conn.close()


def test_collect_cenc_jobs_and_skip_existing_mp4(tmp_path, monkeypatch):
    _, videos = _patch_media(monkeypatch, tmp_path)
    body = {
        "server_message_id": int(SID),
        "content": json.dumps({"video": {"tkey": "tk", "skey": "sk", "vid": "v"}}),
    }
    assert collect_cenc_jobs(bodies=[body]) == [{
        "file_id": SID, "tkey": "tk", "skey": "sk", "msg_id": f"srv_{SID}",
    }]
    (videos / f"{SID}.mp4").write_bytes(b"mp4")
    assert collect_cenc_jobs(bodies=[body]) == []


def test_pending_videos_includes_forwarded_bodies(temp_db, tmp_path, monkeypatch):
    _patch_media(monkeypatch, tmp_path)
    from backend import database
    from extractor.video_downloader import pending_videos
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "srv_parent", "c1", 1, raw_data=json.dumps({
        "content_json": json.dumps({"aweType": 13600}),
        "forwarded_bodies": [{
            "server_message_id": int(SID),
            "content": json.dumps({"video": {"tkey": "tk", "skey": "sk", "vid": "v"}, "poster": {}}),
        }],
    }))
    pending = pending_videos(conn)
    assert any(p["msg_id"] == f"srv_{SID}" for p in pending)
    conn.close()


def test_forward_media_queues_live_photo_clip(tmp_path, monkeypatch):
    """合并转发里的实况图：封面照旧解密，会动的那段进 CENC 任务队列（kind=live）。"""
    import asyncio
    import extractor.video_downloader as vd
    from extractor.forwarded import _materialize_forward_media

    images, _ = _patch_media(monkeypatch, tmp_path)
    monkeypatch.setattr("extractor.im_media._fetch",
                        lambda url, timeout=20: _gcm(b"\xff\xd8\xff" + b"J" * 8))
    seen = {}

    async def fake_save(page, jobs, **kwargs):
        seen["jobs"] = jobs
        return {"total": len(jobs), "ok": 0, "fail": 0, "skipped": 0, "log": []}

    monkeypatch.setattr(vd, "save_cenc_jobs", fake_save)
    bodies = [{"server_message_id": int(SID), "content": json.dumps({
        "aweType": 2704,
        "resource_url": {"skey": KEY, "origin_url_list": ["https://example.com/img"]},
        "live_photo_video": {"tkey": "vid-live", "skey": "00" * 16, "vid": "v"},
    })}]

    asyncio.run(_materialize_forward_media("PAGE", bodies, "srv_parent"))
    assert seen["jobs"] == [{"file_id": SID, "kind": "live", "tkey": "vid-live",
                             "skey": "00" * 16, "msg_id": f"srv_{SID}"}]
    assert os.path.isfile(images / "img.jpg")     # 静态封面也解密了（名字取链接里那截）


def test_materialize_bodies_respects_budget(tmp_path, monkeypatch):
    _patch_media(monkeypatch, tmp_path)
    calls = []

    def fake_fetch(url, timeout=20):
        calls.append(url)
        return _gcm(b"\xff\xd8\xff" + b"JPEGDATA" * 8)

    monkeypatch.setattr("extractor.im_media._fetch", fake_fetch)
    bodies = [{"server_message_id": 100 + i, "content": json.dumps({
        "aweType": 2702,
        "resource_url": {"skey": KEY, "origin_url_list": [f"https://example.com/{i}"]},
    })} for i in range(3)]
    paths = materialize_bodies(bodies, budget=[2])
    assert len(paths) == 2
    assert len(calls) == 2


def test_downloads_monster_emoji_519(tmp_path, monkeypatch):
    """小火人表情（aweType=519）也走表情下载：url.url_list[0] 直接存，不用解密。"""
    _patch_media(monkeypatch, tmp_path)
    fetched = []
    webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * 200

    def fake_fetch(url, timeout=20):
        fetched.append(url)
        return webp

    monkeypatch.setattr("extractor.im_media._fetch", fake_fetch)
    path = download_direct_media({
        "aweType": 519,
        "display_name": "笑死",
        "url": {"url_list": ["https://p26-sign.douyinpic.com/obj/tos-cn-i/1010.webp"]},
    }, SID)
    # 表情也用链接里那截名字存进 emoji/（这里是 1010.webp），不按 server_id 命名。
    assert path == "emoji/1010.webp"
    assert os.path.getsize(os.path.join(tmp_path, path)) == len(webp)
    assert fetched == ["https://p26-sign.douyinpic.com/obj/tos-cn-i/1010.webp"]
    # 同一张表情再遇到（另一个 server_id）直接用已存的那份，不再下载。
    assert download_direct_media({
        "aweType": 519,
        "url": {"url_list": ["https://p26-sign.douyinpic.com/obj/tos-cn-i/1010.webp"]},
    }, "1000000000000000047") == path
    assert len(fetched) == 1
