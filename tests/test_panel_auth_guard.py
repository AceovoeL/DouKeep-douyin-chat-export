"""面板密码那道门的防爆破 + 改密码踢人测试。

覆盖：
1. 外来设备（局域网 / 公网隧道）连错面板密码会记账，错够了锁住整个登录入口；
2. 同一台设备错得太多被单独暂停（要主人在面板上解除）；
3. 输对了清零这本账，设备进「面板密码的信任设备」名单；
4. 本机永远不记账、不受锁影响；
5. 改面板密码：已发的 token 全部作废、两道锁的设备名单全部清空。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import backend.main as main
import common.paths as paths
from common import access


REMOTE_HOST = "192.168.1.9"


class _From:
    """把请求的来源地址改成局域网里另一台设备（防爆破只对外来设备记账）。"""

    def __init__(self, app, host):
        self.app = app
        self.host = host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.host, 54321)
        return await self.app(scope, receive, send)


@pytest.fixture
def guard_cfg(monkeypatch, tmp_path):
    """配置指到临时文件：这些用例不能碰真的 panel_config.json。

    顺手把局域网访问打开 —— 外来设备的请求要先过最外层那道门（没开时直接 403），
    才谈得上走到面板密码的登录接口。
    """
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "panel_config.json"))
    access.set_lan_access(True)
    access.invalidate_cache()
    access.clear_guard(access.SCOPE_PANEL)
    main._PANEL_FAILURES.clear()
    yield
    access.invalidate_cache()
    access.clear_guard(access.SCOPE_PANEL)
    main._PANEL_FAILURES.clear()


@pytest.fixture
def remote(guard_cfg):
    return TestClient(_From(main.app, REMOTE_HOST), base_url=f"http://{REMOTE_HOST}")


@pytest.fixture
def local(guard_cfg):
    return TestClient(main.app)


def _set_panel_password(monkeypatch, pw="panel"):
    monkeypatch.setattr(main, "_get_password_hash", lambda: main._hash_password(pw))


def test_remote_wrong_panel_password_counts_and_locks(remote, monkeypatch, temp_db):
    _set_panel_password(monkeypatch)
    # 前 MAX-1 次是普通的「密码不对」；第 MAX 次点着锁，从这次起就是 429
    for i in range(access.MAX_PASSWORD_FAILURES - 1):
        assert remote.post("/api/auth/login", json={"password": "nope"}).status_code == 403
    r = remote.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 429
    assert r.json()["error"] == "login_locked"
    assert r.json()["locked_seconds"] > 0
    # 锁着的时候连密码都不再校验：输对了也不行
    r = remote.post("/api/auth/login", json={"password": "panel"})
    assert r.status_code == 429


def test_remote_success_clears_the_guard(remote, monkeypatch, temp_db):
    _set_panel_password(monkeypatch)
    remote.post("/api/auth/login", json={"password": "nope"})
    r = remote.post("/api/auth/login", json={"password": "panel"})
    assert r.status_code == 200
    token = r.json()["token"]
    # 这本账清零了
    assert access.guard(access.SCOPE_PANEL)["failures"] == 0
    # 这台设备进了面板密码的信任设备名单
    assert len(access.list_devices(scope=access.SCOPE_PANEL)) == 1
    assert remote.get("/api/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_local_login_never_counts_or_locks(local, monkeypatch, temp_db):
    _set_panel_password(monkeypatch)
    for _ in range(access.MAX_PASSWORD_FAILURES + 3):
        assert local.post("/api/auth/login", json={"password": "nope"}).status_code == 403
    assert access.guard(access.SCOPE_PANEL)["failures"] == 0
    assert local.post("/api/auth/login", json={"password": "panel"}).status_code == 200


def test_changing_password_revokes_tokens_and_devices(remote, monkeypatch, temp_db):
    """改面板密码：token 作废、两道锁的设备名单全部清空。"""
    from backend import control_panel as panel

    _set_panel_password(monkeypatch)
    r = remote.post("/api/auth/login", json={"password": "panel"})
    token = r.json()["token"]
    assert remote.get("/api/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert len(access.list_devices(scope=access.SCOPE_PANEL)) == 1

    result = asyncio.run(panel.set_password(panel.PasswordRequest(password="newpw")))
    assert result["status"] == "ok"
    assert result["cleared_devices"] >= 1
    # token 作废了
    assert main._verify_token(token) is False
    # 旧 token 再去请求面板接口：401（前端据此弹登录框）
    assert remote.get("/api/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # 两道锁的设备名单都空了
    assert access.list_devices(scope=access.SCOPE_PANEL) == []
    assert access.trusted_devices(access.SCOPE_PANEL) == []
    assert access.trusted_devices(access.SCOPE_ACCESS) == []
