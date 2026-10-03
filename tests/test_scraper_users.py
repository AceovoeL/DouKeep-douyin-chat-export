"""发送者身份补全（issue #24：群聊全部显示成群名）。

纯 API 模式不渲染历史消息，`userInfoStore` 里几乎没有群成员，users 表因此是空的，
前端只能回退成会话名。修复的做法是拿消息 protobuf field 14 的 sec_uid 去批量查
IM 用户信息接口。这里锁定"查谁、怎么批、落什么"，不碰 Playwright。
"""
import asyncio

import pytest

from common.db import connect, upsert_user
from extractor.web_scraper import BATCH_USER_INFO, WebChatScraper


class FakePage:
    """只回答两种 evaluate：批量用户信息查询、头像下载。"""

    def __init__(self, users_by_sec):
        self.users_by_sec = users_by_sec
        self.batches = []

    async def evaluate(self, script, arg=None):
        if "sec_user_ids" in script:
            _api, secs = arg
            self.batches.append(list(secs))
            return [self.users_by_sec[s] for s in secs if s in self.users_by_sec]
        return None  # 头像下载：当作失败，回退到 CDN URL


def _scraper(conn, page):
    s = WebChatScraper()
    s._db_conn = conn
    s.page = page
    return s


def _user(uid, nickname):
    return {
        "uid": uid,
        "nickname": nickname,
        "unique_id": f"id_{uid}",
        "avatar_url": f"https://p3.douyinpic.com/{uid}.jpeg",
    }


@pytest.fixture
def avatars(tmp_path, monkeypatch):
    """绝不写真实 data/media/avatars。"""
    import common.paths as paths
    d = tmp_path / "avatars"
    monkeypatch.setattr(paths, "AVATARS_DIR", str(d))
    return d


def test_resolves_unknown_group_senders(temp_db, avatars):
    conn = connect(foreign_keys=True)
    page = FakePage({
        "sec_a": _user("111", "示例昵称甲"),
        "sec_b": _user("222", "示例昵称乙"),
    })
    s = _scraper(conn, page)

    asyncio.run(s._resolve_sender_identities({"111": "sec_a", "222": "sec_b"}))

    rows = dict(conn.execute("SELECT uid, nickname FROM users").fetchall())
    assert rows == {"111": "示例昵称甲", "222": "示例昵称乙"}
    # 头像下载失败时回退到 CDN URL，而不是丢空
    avatar = conn.execute(
        "SELECT avatar_url FROM users WHERE uid = '111'").fetchone()[0]
    assert avatar == "https://p3.douyinpic.com/111.jpeg"
    assert page.batches == [["sec_a", "sec_b"]]


def test_skips_senders_already_named(temp_db, avatars):
    """已经有昵称的不再查——群聊增量抓取时别把接口打爆。"""
    conn = connect(foreign_keys=True)
    upsert_user(conn, "111", nickname="示例昵称甲")
    conn.commit()

    page = FakePage({"sec_b": _user("222", "示例昵称乙")})
    s = _scraper(conn, page)

    asyncio.run(s._resolve_sender_identities({"111": "sec_a", "222": "sec_b"}))

    assert page.batches == [["sec_b"]]  # 111 没被查


def test_empty_nickname_row_still_gets_resolved(temp_db, avatars):
    """users 里有行但没昵称 = 等同于没有，仍要补全。"""
    conn = connect(foreign_keys=True)
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('111', '')")
    conn.commit()

    page = FakePage({"sec_a": _user("111", "示例昵称甲")})
    s = _scraper(conn, page)

    asyncio.run(s._resolve_sender_identities({"111": "sec_a"}))

    assert conn.execute(
        "SELECT nickname FROM users WHERE uid = '111'").fetchone()[0] == "示例昵称甲"


def test_batches_large_groups(temp_db, avatars):
    """粉丝群成员可能上百，必须分批。"""
    n = BATCH_USER_INFO + 3
    sec_by_uid = {str(i): f"sec_{i}" for i in range(n)}
    page = FakePage({f"sec_{i}": _user(str(i), f"成员{i}") for i in range(n)})
    s = _scraper(connect(foreign_keys=True), page)

    asyncio.run(s._resolve_sender_identities(sec_by_uid))

    assert [len(b) for b in page.batches] == [BATCH_USER_INFO, 3]
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == n


def test_no_senders_no_call(temp_db, avatars):
    page = FakePage({})
    s = _scraper(connect(foreign_keys=True), page)
    asyncio.run(s._resolve_sender_identities({}))
    assert page.batches == []
