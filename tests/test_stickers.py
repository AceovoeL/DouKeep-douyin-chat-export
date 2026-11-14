"""表情包同步端点测试（合成数据）：按发送者筛选、按内容去重、min_count、sample_ts 均匀分布、
missing_files、名字解码、图片端点的 404 / 路径穿越、API token 鉴权。"""
import hashlib

import pytest
from fastapi.testclient import TestClient

import backend.main as main
import backend.stickers as stickers
from tests.conftest import insert_conversation, insert_message

CONV, OTHER_CONV = "0:1:111:222", "0:1:111:333"
HER, ME = "111", "222"
T0 = 1_700_000_000
PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 64
GIF_C = b"GIF89a" + b"C" * 64
NAMED_URL = "https://p3-im.douyinpic.com/im-resource/1700000000-ts-" + "续火花.png".encode().hex() + "?x=1"


def md5(b):
    return hashlib.md5(b).hexdigest()


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    import common.paths as paths
    import extractor.models as models

    media = tmp_path / "media"
    (media / "emoji").mkdir(parents=True)
    (media / "images").mkdir()
    monkeypatch.setattr(paths, "MEDIA_DIR", str(media))
    monkeypatch.setattr(stickers, "_by_path", {})
    monkeypatch.setattr(stickers, "_by_md5", {})
    monkeypatch.setattr(main, "_get_password_hash", lambda: None)
    # 同一张图 A 存成了两个文件（CDN URL 不同）
    (media / "emoji" / "a1.png").write_bytes(PNG_A)
    (media / "emoji" / "a2.png").write_bytes(PNG_A)
    (media / "emoji" / "b.png").write_bytes(PNG_B)
    (media / "emoji" / "c.png").write_bytes(GIF_C)
    (media / "images" / "secret.png").write_bytes(PNG_B + b"x")

    conn = models.get_db()
    insert_conversation(conn, CONV, "她")
    insert_conversation(conn, OTHER_CONV, "别人")
    seq = 0

    def msg(conv, sender, path, ts, url=None, msg_type=2):
        nonlocal seq
        seq += 1
        insert_message(conn, f"m{seq}", conv, seq, sender_uid=sender, msg_type=msg_type,
                       media_url=url or f"https://cdn/{path}", media_local_path=path, timestamp=ts)

    for i in range(10):  # A：她发了 10 次（两个文件）
        msg(CONV, HER, "emoji/a1.png" if i % 2 else "emoji/a2.png", T0 + i * 100, url=NAMED_URL if i < 7 else None)
    msg(CONV, HER, "emoji/b.png", T0 + 5)  # B：她只发过 1 次
    msg(OTHER_CONV, HER, "emoji/b.png", T0 + 6)  # 在另一个会话又发了 1 次
    for i in range(5):  # C：只有"我"发过
        msg(CONV, ME, "emoji/c.png", T0 + i)
    msg(CONV, HER, "emoji/gone.png", T0 + 7)  # 本地文件不存在
    msg(CONV, HER, None, T0 + 8)  # 没下载
    msg(CONV, HER, "images/secret.png", T0 + 9)  # 不在 emoji 目录下，不算表情包文件
    msg(CONV, HER, "emoji/a1.png", T0 + 10, msg_type=3)  # 不是表情消息
    conn.commit()
    conn.close()
    return TestClient(main.app)


def test_catalog_filters_by_sender_and_dedupes_by_content(client):
    r = client.get(f"/api/stickers?sender_uid={HER}&samples=4")
    assert r.status_code == 200
    d = r.json()
    assert (d["sender_uid"], d["conv_id"], d["total_uses"], d["missing_files"]) == (HER, None, 12, 3)
    assert [(s["id"], s["count"]) for s in d["stickers"]] == [(md5(PNG_A), 10), (md5(PNG_B), 2)]
    a = d["stickers"][0]
    assert (a["first_ts"], a["last_ts"], a["name"], a["content_type"], a["size"]) == (T0, T0 + 900, "续火花", "image/png", len(PNG_A))
    assert a["sample_ts"] == [T0, T0 + 300, T0 + 600, T0 + 900]  # 均匀分布、含首尾
    assert md5(GIF_C) not in {s["id"] for s in d["stickers"]}  # 别人发的不算

    d = client.get(f"/api/stickers?sender_uid={HER}&conv_id={CONV}&min_count=2").json()
    assert [(s["id"], s["count"]) for s in d["stickers"]] == [(md5(PNG_A), 10)]
    assert d["stickers"][0]["sample_ts"] == []

    me = client.get(f"/api/stickers?sender_uid={ME}&samples=50").json()
    assert [(s["id"], s["count"], s["content_type"]) for s in me["stickers"]] == [(md5(GIF_C), 5, "image/gif")]
    assert me["stickers"][0]["sample_ts"] == [T0 + i for i in range(5)]

    assert client.get("/api/stickers").status_code == 400
    assert client.get(f"/api/stickers?sender_uid={HER}&samples=51").status_code == 422
    assert client.get(f"/api/stickers?sender_uid={HER}&min_count=0").status_code == 422


def test_sticker_file_by_content_md5(client):
    r = client.get(f"/api/stickers/{md5(GIF_C)}")  # 没先查目录：冷缓存也能找到
    assert r.status_code == 200 and r.content == GIF_C
    assert r.headers["content-type"] == "image/gif"
    assert r.headers["cache-control"] == "private, max-age=86400" and r.headers["etag"] == f'"{md5(GIF_C)}"'
    assert client.get(f"/api/stickers/{md5(PNG_A)}").content == PNG_A

    assert client.get(f"/api/stickers/{md5(b'nope')}").status_code == 404
    assert client.get(f"/api/stickers/{md5(PNG_B + b'x')}").status_code == 404  # images/ 下的文件取不到
    assert client.get("/api/stickers/" + md5(PNG_A).upper()).status_code == 404
    assert client.get("/api/stickers/a1.png").status_code == 404
    for bad in ("..%2Fimages%2Fsecret.png", "%2E%2E%2Fimages%2Fsecret.png", "..%2F..%2Fchat.db"):
        # 带 / 的路径进不了表情包路由（落到前端兜底页），无论如何拿不到 emoji 目录以外的文件
        r = client.get(f"/api/stickers/{bad}")
        assert r.content != PNG_B + b"x" and not r.headers["content-type"].startswith("image/")


def test_sticker_endpoints_need_api_token(client, monkeypatch):
    from common import config
    monkeypatch.setattr(main, "_get_password_hash", lambda: "hash")
    monkeypatch.setattr(config, "get_api_token", lambda: "tok123")
    auth = {"Authorization": "Bearer tok123"}
    assert client.get(f"/api/stickers?sender_uid={HER}").status_code == 401
    assert client.get(f"/api/stickers/{md5(PNG_A)}").status_code == 401
    assert client.get(f"/api/stickers?sender_uid={HER}", headers=auth).status_code == 200
    assert client.get(f"/api/stickers/{md5(PNG_A)}", headers=auth).content == PNG_A
