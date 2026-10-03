"""局域网访问（面板里要先用「关于」页的开发者模式打开，再看 设置 → 局域网访问）的行为测试。

覆盖四件事：

1. ``common/access.py`` 里的地址判定与设置读写；
2. 拦截中间件：本机直通、没开局域网拒绝、开了且有访问密码时要求解锁；
3. 解锁流程：输对访问密码 → 设备被记住 → 面板里能看到 / 能移除；
4. 解锁页（以及「未开放局域网访问」页）的配色跟着主机上的面板主题走。

「访问密码」和「控制面板密码」是两道独立的锁，这里也各测一次：过了访问密码之后，
面板自己的密码该要还是要（见最后两组用例）。
"""
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

import common.paths as paths
from common import access

REMOTE_HOST = "192.168.3.9"               # 假装请求来自局域网里另一台设备
LOCAL_HOST = "127.0.0.1"


class _FromHost:
    """把请求的「来源地址」改成指定值。

    TestClient 默认把每条请求的来源都写成 ``testclient``（不是 IP），那样就没法区分
    「本机」和「局域网里另一台设备」了 —— 而这个区别正是这些用例要测的东西。这里在
    ASGI 入口把 ``scope["client"]`` 换掉，等于模拟从某个地址连进来。
    """

    def __init__(self, app, host: str):
        self.app = app
        self.host = host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.host, 54321)
            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", []) if k != b"host"
            ] + [(b"host", f"{self.host}:8000".encode())]
        return await self.app(scope, receive, send)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把配置指到临时文件：这些用例不能碰真的 data/panel_config.json。"""
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    access.invalidate_cache()
    # 错误次数计数是模块级的（内存里），用例之间互相不干扰
    from backend.panel import access_gate

    access_gate._FAILURES.clear()
    yield path
    access.invalidate_cache()
    access_gate._FAILURES.clear()


@pytest.fixture
def client(cfg_file):
    """本机（127.0.0.1）来的客户端。"""
    import backend.main as main

    # 不用 with：不触发 startup（真实配置 / 定时任务都不该在这里跑起来）。
    # follow_redirects=False：要断言的就是「跳到哪儿」，不能让它自己跟着跳。
    return TestClient(_FromHost(main.app, LOCAL_HOST), base_url=f"http://{LOCAL_HOST}",
                      follow_redirects=False)


@pytest.fixture
def remote(cfg_file):
    """局域网里另一台设备（来源地址决定走不走局域网那道锁）。"""
    import backend.main as main

    return TestClient(_FromHost(main.app, REMOTE_HOST), base_url=f"http://{REMOTE_HOST}",
                      follow_redirects=False)


def write_cfg(cfg_file, data: dict) -> None:
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    access.invalidate_cache()


def login(client, password="pw", next_path="/panel"):
    """在「局域网里的另一台设备」上输一次访问密码。"""
    return client.post(
        "/api/access/login",
        data={"password": password, "next": next_path},
    )


# ── 地址判定 ──

def test_loopback_addresses_count_as_local():
    for host in ("127.0.0.1", "127.0.0.5", "::1", "localhost", "",
                 "::ffff:127.0.0.1", "[::1]"):
        assert access.is_local_address(host), host


def test_unrecognizable_client_host_counts_as_local():
    """读不出 IP 的来源（进程内调用、测试客户端）不该被当成另一台设备挡在外面。"""
    for host in ("testclient", "unix:/tmp/x"):
        assert access.is_local_address(host), host
        assert not access.is_remote(host), host


def test_lan_addresses_are_remote():
    for host in ("192.168.3.9", "10.0.0.7", "172.16.5.4", "::ffff:192.168.3.9"):
        assert access.is_remote(host), host
        assert not access.is_local_address(host), host


def test_normalize_host():
    assert access.normalize_host("[::]") == "::"
    assert access.normalize_host("::ffff:127.0.0.1") == "127.0.0.1"
    assert access.normalize_host("") == "127.0.0.1"
    assert access.normalize_host(" 0.0.0.0 ") == "0.0.0.0"


def test_server_host_follows_the_switch(cfg_file):
    write_cfg(cfg_file, {})
    assert access.server_host() == "127.0.0.1"
    assert access.lan_enabled() is False
    access.set_lan_access(True)
    assert access.lan_enabled() is True
    assert access.server_host() == "0.0.0.0"


# ── 访问密码 ──

def test_password_is_hashed_and_verified(cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    assert access.password_required() is False      # 没设密码 = 谁都能看
    access.set_password("hello")
    assert access.password_hash() != "hello"        # 存的是摘要，不是明文
    assert access.verify_password("hello") is True
    assert access.verify_password("hell0") is False
    assert access.password_required() is True


def test_clearing_password_also_forgets_devices(cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    access.set_password("hello")
    access.remember_device("192.168.3.9", "Mozilla/5.0 Chrome/120")
    assert len(access.list_devices()) == 1
    access.set_password("")
    assert access.password_hash() is None
    assert access.list_devices() == []


def test_password_required_only_when_lan_is_on(cfg_file):
    write_cfg(cfg_file, {})
    access.set_password("hello")
    # 开关没开：局域网本来就进不来，不需要访问密码这一层
    assert access.password_required() is False


# ── 设备记忆 ──

def test_remembered_device_token_roundtrip(cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    device_id, token = access.remember_device("192.168.3.9", "Mozilla/5.0 Chrome/120")
    cookie = f"{device_id}.{token}"
    assert access.is_trusted(cookie) is True
    # 换个 token（伪造）不行
    assert access.is_trusted(f"{device_id}.{token}x") is False
    # 换台设备也不行
    assert access.is_trusted("deadbeef.{token}") is False


def test_forget_device_kills_its_cookie(cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    device_id, token = access.remember_device("192.168.3.9", "Chrome")
    assert access.is_trusted(f"{device_id}.{token}") is True
    assert access.forget_device(device_id) is True
    assert access.is_trusted(f"{device_id}.{token}") is False
    assert access.forget_device(device_id) is False   # 再删一次：没这台了


def test_token_is_not_stored_in_plain_text(cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    _, token = access.remember_device("192.168.3.9", "Chrome")
    raw = cfg_file.read_text(encoding="utf-8")
    assert token not in raw


# ── 拦截中间件 ──

def test_local_requests_never_ask_for_the_access_password(client, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    assert client.get("/panel").status_code == 200


def test_remote_denied_while_lan_access_is_off(remote, cfg_file):
    write_cfg(cfg_file, {})
    r = remote.get("/panel")
    assert r.status_code == 403
    assert "局域网" in r.text


def test_remote_denied_even_with_the_panel_password(remote, cfg_file, monkeypatch):
    """面板密码不该顺手把局域网这道门也打开。"""
    import backend.main as main

    write_cfg(cfg_file, {"lan_password_hash": access.hash_password("pw")})
    monkeypatch.setattr(main, "_get_password_hash", lambda: main._hash_password("panel"))
    # 局域网没开：连登录接口都进不来（这道门在最外层，先于面板的一切）
    assert remote.post("/api/auth/login", json={"password": "panel"}).status_code == 403
    assert remote.get("/panel", headers={"Authorization": "Bearer whatever"}).status_code == 403
    # 开了但没设访问密码：面板密码仍然照要（门开了不等于已经登录）
    write_cfg(cfg_file, {"lan_access": True})
    assert remote.get("/panel/api/status").status_code == 401
    assert remote.get("/panel").status_code == 200        # 面板页面自带登录框


def test_without_access_password_everyone_on_the_lan_gets_in(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    assert remote.get("/panel").status_code == 200


def test_remote_must_unlock_when_access_password_is_set(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = remote.get("/panel")
    assert r.status_code == 303
    assert r.headers["location"] == "/access?next=%2Fpanel"
    # 同一台设备上的接口请求不跳转（跳了前端只会拿到一页 HTML，
    # POST 还会被 303 改写成 GET），直接说「没通过」
    for path in ("/api/stats", "/panel/api/status"):
        api = remote.get(path)
        assert api.status_code == 401, path
        assert api.json()["error"] == "lan_access_password_required", path


def test_every_page_request_goes_through_the_unlock_page(remote, cfg_file):
    """没解锁的设备连静态媒体也拿不到：一律先送去输访问密码。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = remote.get("/media/nope.png")
    assert r.status_code == 303
    assert r.headers["location"] == "/access?next=%2Fmedia%2Fnope.png"
    # 解锁之后就照常（该 404 的还是 404，不是被门拦下）
    token = login(remote).cookies.get("access_token")
    assert remote.get("/media/nope.png",
                      headers={"cookie": f"access_token={token}"}).status_code == 404


def test_access_page_renders_and_carries_the_next_target(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = remote.get("/access", params={"next": "/panel"})
    assert r.status_code == 200
    assert 'name="next" value="/panel"' in r.text
    assert "访问密码" in r.text
    # 英文浏览器看到英文（同一页，文案按 Accept-Language 选）
    en = remote.get("/access", params={"next": "/panel"},
                    headers={"accept-language": "en-US,en;q=0.9"})
    assert "Access password" in en.text


def test_access_page_redirects_when_no_password_is_needed(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    r = remote.get("/access", params={"next": "/panel"})
    assert r.status_code == 303
    assert r.headers["location"] == "/panel"


def test_next_target_cannot_point_off_site(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = remote.get("/access", params={"next": "//evil.example.com"})
    assert 'name="next" value="/"' in r.text
    ok = login(remote, "pw", "//evil.example.com")
    assert ok.headers["location"] == "/"


# ── 解锁页的配色（跟主机上的面板保持一致） ──

def open_unlock_page(remote, cfg_file, appearance: dict) -> str:
    """以「局域网里另一台设备」的身份打开解锁页，返回 HTML。"""
    write_cfg(cfg_file, {
        "lan_access": True,
        "lan_password_hash": access.hash_password("pw"),
        "appearance": appearance,
    })
    r = remote.get("/access")
    assert r.status_code == 200
    return r.text


def test_unlock_page_follows_the_host_panel_theme(remote, cfg_file):
    """主机把面板主题设成 Light，别的设备看到的解锁页也该是浅色，不能只换一半。

    配置里的 ``colors`` 永远是一份完整快照（没自定义过就是暗色那一套），所以它只能
    在「自定义」主题下生效；否则背景、输入框会被拉回暗色，而 ``surface``（卡片）
    又没有对应字段可覆盖，页面就成了「黑底白卡片」。
    """
    page = open_unlock_page(remote, cfg_file, {"panel": {"theme": "light"}})
    assert "--bg: #f7f7f9;" in page
    assert "--bg3: #eef0f3;" in page          # 输入框底色
    assert "--surface: #ffffff;" in page      # 卡片底色
    assert "--text: #1a1c22;" in page
    assert "#0f1014" not in page              # 暗色主题的一个都不该留下
    assert "#21242e" not in page
    # 派生色的混色比例也跟着主题（面板 Light 用的是 9% / 10%）
    assert "--accent-bg: color-mix(in srgb, var(--accent) 9%, transparent);" in page
    assert "--red-bg: color-mix(in srgb, var(--red) 10%, transparent);" in page


def test_custom_theme_uses_the_saved_colors(remote, cfg_file):
    """「自定义」主题才用配置里存的那几个颜色，卡片和悬停色补成不突兀的值。"""
    page = open_unlock_page(remote, cfg_file, {
        "panel": {
            "theme": "custom",
            "colors": {"bg": "#101418", "bg2": "#1b2026", "bg3": "#252b33",
                       "accent": "#12ab34", "text": "#f2f3f5"},
        },
    })
    assert "--bg: #101418;" in page
    assert "--bg3: #252b33;" in page
    assert "--text: #f2f3f5;" in page
    assert "--surface: #1b2026;" in page      # 卡片跟着卡片底色
    assert "--accent2: #12ab34;" in page      # 悬停色＝主色


def test_theme_tokens_stay_in_sync_with_the_panel_page():
    """解锁页的配色是面板页面的拷贝：面板里改了主题颜色，这里必须跟着改。"""
    from backend.panel import access_gate

    with open(os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html"),
              encoding="utf-8") as handle:
        css = handle.read()
    for theme, expected in access_gate._THEME_TOKENS.items():
        block = re.search(r'\[data-theme="%s"\]\s*\{(.*?)\}' % theme, css, re.S)
        assert block, f"面板页面里找不到 {theme} 主题块"
        found = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block.group(1)))
        values = [found.get(name, "").strip().lower()
                  for name in access_gate._THEME_VAR_NAMES]
        assert values == expected.split(), theme


def test_lan_off_page_follows_the_host_panel_theme(remote, cfg_file):
    """局域网没开时那张「未开放局域网访问」的说明页，配色同样跟随面板。"""
    write_cfg(cfg_file, {"appearance": {"panel": {"theme": "light"}}})
    r = remote.get("/panel")
    assert r.status_code == 403
    assert "未开放局域网访问" in r.text
    assert "--bg: #f7f7f9;" in r.text
    assert "#0f1014" not in r.text


# ── 解锁流程 ──

def test_wrong_password_keeps_the_device_out(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = login(remote, "nope")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/access?err=1")
    assert "access_token" not in r.cookies
    assert remote.get("/panel").status_code == 303


def test_login_remembers_the_device_for_both_panel_and_viewer(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = login(remote)
    assert r.status_code == 303
    assert r.headers["location"] == "/panel"
    token = r.cookies.get("access_token")
    assert token
    cookie = {"cookie": f"access_token={token}"}
    # 面板、查看器（前端外壳）都不用再输访问密码
    assert remote.get("/panel", headers=cookie).status_code == 200
    assert remote.get("/", headers=cookie).status_code == 200
    # 状态接口也认得这台设备
    assert remote.get("/api/access/status", headers=cookie).json()["trusted"] is True


def test_viewer_shell_is_gated_on_the_lan(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = remote.get("/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/access?next=%2F")


def test_logout_drops_the_device(remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    token = login(remote).cookies.get("access_token")
    cookie = {"cookie": f"access_token={token}"}
    assert remote.post("/api/access/logout", headers=cookie).json()["removed"] is True
    assert remote.get("/panel", headers=cookie).status_code == 303
    assert access.list_devices() == []


def test_trailing_slash_login_is_accepted(remote, cfg_file):
    """解锁页提交到带斜杠的地址也要能登录（不然用户会看到一个 404）。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    r = remote.post("/api/access/login/", data={"password": "pw"})
    assert r.status_code == 303
    assert r.cookies.get("access_token")


def test_repeated_wrong_passwords_are_slowed_down(remote, cfg_file):
    """局域网里谁都能来猜密码，同一台设备连错多次之后要暂时不受理。"""
    from backend.panel import access_gate

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for _ in range(access_gate._MAX_FAILURES):
        assert login(remote, "wrong").headers["location"].startswith("/access?err=1")
    # 到量之后，连正确的密码也先不受理（等窗口过了再说）
    r = login(remote, "pw")
    assert r.headers["location"].startswith("/access?err=1")
    assert "access_token" not in r.cookies

    # 换个地址不受影响：这是「别一直猜」的刹车，不是全局封禁
    access_gate._FAILURES.clear()
    assert login(remote, "pw").cookies.get("access_token")


# ── 面板接口 ──

def test_panel_status_lists_trusted_devices(client, remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    login(remote)
    payload = client.get("/panel/api/lan").json()
    assert payload["enabled"] is True
    assert payload["require_password"] is True
    assert len(payload["devices"]) == 1
    device = payload["devices"][0]
    assert "token_hash" not in device            # 凭据摘要绝不出现在接口里
    assert device["ip"] == REMOTE_HOST
    assert REMOTE_HOST in device["name"]
    # 面板也告诉用户「用哪个地址访问」
    assert payload["addresses"]["panel"].endswith("/panel")


def test_panel_can_forget_one_device(client, remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    first = login(remote).cookies.get("access_token")
    second = login(remote, next_path="/").cookies.get("access_token")
    device_id = access.parse_cookie(first)[0]
    r = client.post("/panel/api/lan/devices/forget", json={"id": device_id})
    assert r.json()["removed"] is True
    assert len(r.json()["payload"]["devices"]) == 1
    # 被移除的那台立刻失效，另一台不受影响
    assert remote.get("/panel", headers={"cookie": f"access_token={first}"}).status_code == 303
    assert remote.get("/panel", headers={"cookie": f"access_token={second}"}).status_code == 200


def test_panel_can_forget_every_device(client, remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    login(remote)
    login(remote, next_path="/")
    r = client.post("/panel/api/lan/devices/forget-all")
    assert r.json()["removed"] == 2
    assert r.json()["payload"]["devices"] == []


def test_panel_sets_and_clears_the_access_password(client, remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    r = client.post("/panel/api/lan/password", json={"password": "new-pw"})
    assert r.json()["has_password"] is True
    assert access.verify_password("new-pw") is True
    assert remote.get("/panel").status_code == 303
    r = client.post("/panel/api/lan/password", json={"password": ""})
    assert r.json()["has_password"] is False
    assert remote.get("/panel").status_code == 200


def test_panel_lan_toggle_reports_the_current_listen_address(client, cfg_file):
    write_cfg(cfg_file, {})
    payload = client.get("/panel/api/lan").json()
    assert payload["enabled"] is False
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 8000


# ── 两道锁各自独立 ──

def test_tracked_device_still_needs_the_panel_password(remote, cfg_file, monkeypatch, temp_db):
    """先过局域网访问密码，再输面板密码：两道锁互不代劳。"""
    import backend.main as main

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    monkeypatch.setattr(main, "_get_password_hash", lambda: main._hash_password("panel"))
    token = login(remote).cookies.get("access_token")
    cookie = {"cookie": f"access_token={token}"}
    # 访问密码那层过了，面板的数据接口仍然要面板密码
    assert remote.get("/panel/api/status", headers=cookie).status_code == 401
    panel_token = remote.post("/api/auth/login", json={"password": "panel"},
                              headers=cookie).json()["token"]
    ok = remote.get("/panel/api/status",
                    headers={**cookie, "Authorization": f"Bearer {panel_token}"})
    assert ok.status_code == 200


def test_tracked_device_still_needs_the_panel_password_for_the_viewer(remote, cfg_file, monkeypatch,
                                                                     temp_db):
    import backend.main as main

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    monkeypatch.setattr(main, "_get_password_hash", lambda: main._hash_password("panel"))
    token = login(remote, next_path="/").cookies.get("access_token")
    cookie = {"cookie": f"access_token={token}"}
    # 外壳能打开（查看器里自己会弹登录框），但数据要面板密码
    assert remote.get("/", headers=cookie).status_code == 200
    assert remote.get("/api/stats", headers=cookie).status_code == 401
    check = remote.get("/api/auth/check", headers=cookie).json()
    assert check == {"need_password": True, "authenticated": False}
