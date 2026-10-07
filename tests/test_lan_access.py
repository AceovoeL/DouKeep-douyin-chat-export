"""局域网访问（面板里要先用「关于」页的开发者模式打开，再看 设置 → 局域网访问）的行为测试。

覆盖这几件事：

1. ``common/access.py`` 里的地址判定与设置读写；
2. 拦截中间件：本机直通、没开局域网拒绝、对外开放但还没设访问密码时外面一样什么都拿不到
   （隧道转进来的公网访客也算「外面」，见 ``client_host``）；
3. 解锁流程：输对访问密码 → 设备被记住 → 面板里能看到 / 能移除；
4. 猜密码那本账：连着错就锁住**整个**登录入口，换网络、清 Cookie、换浏览器都躲不开；
5. 解锁页（以及「未开放局域网访问」页）的配色跟着主机上的面板主题走。

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

REMOTE_HOST = "192.168.1.9"               # 假装请求来自局域网里另一台设备
LOCAL_HOST = "127.0.0.1"
TUNNEL_HOST = "203.0.113.9"               # 假装请求是公网隧道转进来的访客


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
    # 设备错误计数在内存里、猜密码那本账在配置里，两边都要清干净，用例之间互不干扰
    from backend.panel import access_gate

    access_gate._DEVICE_FAILURES.clear()
    access.clear_guard()
    yield path
    access.invalidate_cache()
    access_gate._DEVICE_FAILURES.clear()
    access.clear_guard()


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


def login(client, password="pw", next_path="/panel", user_agent: str | None = None):
    """在「局域网里的另一台设备」上输一次访问密码（``user_agent`` 换浏览器身份）。"""
    headers = {"user-agent": user_agent} if user_agent else None
    return client.post(
        "/api/access/login",
        data={"password": password, "next": next_path},
        headers=headers,
    )


def raw_client(host: str = REMOTE_HOST):
    """一台「手上还没有 Cookie」的设备：同一个地址、同一个浏览器（UA 和 remote 一样）。

    用来模拟浏览器把刚才那次解锁提交重发了一遍 —— 第二次请求发出的时候，第一次的
    Set-Cookie 还没落到 Cookie 里（隧道抖了一下、响应丢了，就是这个样子）。
    """
    import backend.main as main

    return TestClient(_FromHost(main.app, host), base_url=f"http://{host}:8000",
                      follow_redirects=False)


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
    for host in ("192.168.1.9", "10.0.0.7", "172.16.5.4", "::ffff:192.168.1.9"):
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
    access.remember_device("192.168.1.9", "Mozilla/5.0 Chrome/120")
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
    device_id, token = access.remember_device("192.168.1.9", "Mozilla/5.0 Chrome/120")
    cookie = f"{device_id}.{token}"
    assert access.is_trusted(cookie) is True
    # 换个 token（伪造）不行
    assert access.is_trusted(f"{device_id}.{token}x") is False
    # 换台设备也不行
    assert access.is_trusted("deadbeef.{token}") is False


def test_forget_device_kills_its_cookie(cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    device_id, token = access.remember_device("192.168.1.9", "Chrome")
    assert access.is_trusted(f"{device_id}.{token}") is True
    assert access.forget_device(device_id) is True
    assert access.is_trusted(f"{device_id}.{token}") is False
    assert access.forget_device(device_id) is False   # 再删一次：没这台了


def test_token_is_not_stored_in_plain_text(cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    _, token = access.remember_device("192.168.1.9", "Chrome")
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
    # 开了但没设访问密码：局域网里直接进（这是面板上写明了的取舍），面板密码仍然照要
    write_cfg(cfg_file, {"lan_access": True})
    assert remote.get("/panel/api/status").status_code == 401
    assert remote.get("/panel").status_code == 200        # 面板页面自带登录框


def test_without_an_access_password_everyone_gets_in(remote, cfg_file, temp_db):
    """没设访问密码就是谁都能看（局域网和公网隧道都一样）—— 这是面板上写明的取舍。

    挂到公网之前面板会红字警告 + 要一次确认，但确认之后程序不替他关、也不替他拦。
    ``temp_db`` 是为了那句数据接口断言：CI 上项目里没有真实的 chat.db。
    """
    import backend.main as main

    write_cfg(cfg_file, {"lan_access": True})
    assert remote.get("/panel").status_code == 200
    assert remote.get("/api/stats").status_code == 200    # 数据接口照常（面板密码另算）
    tunnel = TestClient(_FromHost(main.app, LOCAL_HOST), base_url=f"http://{LOCAL_HOST}",
                        follow_redirects=False)
    assert tunnel.get("/panel", headers={"cf-connecting-ip": TUNNEL_HOST}).status_code == 200
    assert tunnel.get("/", headers={"cf-connecting-ip": TUNNEL_HOST}).status_code == 200


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
    """没设密码（或者干脆没开局域网）时，解锁页没有意义：直接送去该去的地方。"""
    write_cfg(cfg_file, {"lan_access": True})
    r = remote.get("/access", params={"next": "/panel"})
    assert r.status_code == 303
    assert r.headers["location"] == "/panel"


def test_access_page_redirects_when_lan_access_is_off(remote, cfg_file):
    """局域网关着时解锁页没有意义（外面本来就进不来），直接送去该去的地方。"""
    write_cfg(cfg_file, {})
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


def test_panel_orders_public_access_above_the_device_lists():
    """板块顺序：公网访问在上，然后是登录保护、信任设备，再下面是需手动确认。"""
    with open(os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html"),
              encoding="utf-8") as handle:
        html = handle.read()
    public_at = html.index('id="publicBox"')
    guard_at = html.index('id="lanGuardBox"')
    trusted_at = html.index('id="lanDevicesBox"')
    pending_at = html.index('id="lanPendingBox"')
    assert public_at < guard_at < trusted_at < pending_at


def test_panel_shows_the_login_guard_and_can_clear_it():
    """面板接线：登录保护那一块要有状态、要接到清零接口上。"""
    with open(os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html"),
              encoding="utf-8") as handle:
        html = handle.read()
    assert "function clearLanGuard(" in html
    assert "/panel/api/lan/guard/clear" in html
    assert "lanGuardLocked" in html and "lanGuardCounting" in html
    assert "function lanGuardWhen(" in html
    # 「是哪几台设备在猜」直接列在这一块里
    assert 'id="lanGuardList"' in html and "function renderLanGuardDevices(" in html
    assert "lanGuardDeviceMeta" in html and "lanGuardDevicesCount" in html


def test_panel_renders_pending_devices_one_row_each_with_a_reason():
    """「需手动确认」：一行一台设备 + 右侧原因；信任设备那边保持原样。"""
    with open(os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html"),
              encoding="utf-8") as handle:
        html = handle.read()
    assert "function renderLanPending(" in html
    assert 'id="lanPendingList"' in html and "lan-pending-list" in html
    assert "lan-pending-reason" in html
    assert "lanPendingReason_" in html                  # 原因按代码翻文案
    assert "function renderLanDevices(" in html         # 信任设备那块没被改动
    assert "function releasePendingDevice(" in html
    assert "function releaseAllPendingDevices(" in html
    # 解除的确认框要说「解除」，不能沿用默认的「不再信任设备」
    assert "'lanPendingReleaseTitle', 'lanPendingReleaseOk'" in html
    assert "'lanPendingReleaseAllTitle', 'lanPendingReleaseAllOk'" in html


#: 这几个确认框的标题和按钮就用默认那套（默认是「确认全量采集 / 继续采集」或者
#: 「不再信任设备 / 不再信任」，字面上本来就对）。
DEFAULT_CONFIRM_CALLS = (
    "openConfirmModal('confirmFullScrape')",
    "lanConfirm(t('lanForgetHint'",
    "lanConfirm(t('lanForgetAllConfirm'",
)


def _confirm_calls(html: str) -> list[str]:
    """把面板里每个确认框调用整段抠出来（括号配对到它的结尾）。"""
    calls: list[str] = []
    for name in ("openConfirmModal(", "lanConfirm("):
        start = 0
        while True:
            at = html.find(name, start)
            if at == -1:
                break
            depth = 0
            index = at + len(name) - 1
            while index < len(html):
                if html[index] == "(":
                    depth += 1
                elif html[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            calls.append(html[at:index + 1])
            start = index + 1
    return calls


def _arg_count(call: str) -> int:
    """这个调用传了几个顶层参数（``lanConfirm`` 的标题/按钮是位置参数）。"""
    inner = call[call.index("(") + 1:-1]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for char in inner:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def test_every_confirm_dialog_passes_its_own_button_text():
    """每个确认框都要说自己的话：``openConfirmModal`` 传 ``okKey``，``lanConfirm`` 传第三个参数。

    这条是被两次真实 bug 逼出来的：解除设备暂停的框顶着「不再信任」、恢复默认外观的框
    顶着「确认全量采集 / 继续采集」—— 标题和按钮都是别人的字，用户一眼就看出不对。
    """
    with open(os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html"),
              encoding="utf-8") as handle:
        html = handle.read()

    calls = [call for call in _confirm_calls(html)
             if not call.startswith("openConfirmModal(messageKey")]      # 跳过函数定义
    assert len(calls) >= 10, f"只找到 {len(calls)} 个确认框调用，抠取逻辑是不是坏了？"

    bad: list[str] = []
    for call in calls:
        if call.startswith(DEFAULT_CONFIRM_CALLS):
            continue
        if call.startswith("openConfirmModal("):
            if "okKey" not in call:
                bad.append(call)
        elif _arg_count(call) < 3:                   # lanConfirm 少传了 titleKey / okKey
            bad.append(call)
    assert not bad, "这些确认框没说自己的话，按钮会显示别人的字：\n" + "\n".join(
        call[:110] for call in bad)

    # 用户踩过的两个，明确盯着
    assert "okKey: 'apConfirmResetOk'" in html
    assert "'lanPendingReleaseTitle', 'lanPendingReleaseOk'" in html


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


def test_five_wrong_passwords_lock_the_whole_login(remote, client, cfg_file):
    """连错 5 次访问密码：**整个登录入口**锁住，不只是那台设备。

    这就是「换网络 / 清 Cookie 就当新设备接着猜」的解药：账记在服务端那本全服务共用的
    本子上，跟「谁来的」无关。
    """
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    # 前 4 次只是普通的「密码不对」
    for _ in range(access.MAX_PASSWORD_FAILURES - 1):
        assert login(remote, "wrong").headers["location"].startswith("/access?err=1")
    # 第 5 次把账点着：跳回解锁页，不带「密码不对」那句（页面会说清锁到什么时候）
    where = login(remote, "wrong").headers["location"]
    assert where == "/access?next=%2Fpanel"
    assert "blocked" not in where, "网址里不能留状态标记，否则解锁之后刷新还是那一版"
    assert access.guard()["failures"] == access.MAX_PASSWORD_FAILURES
    assert access.guard_locked_seconds() > 0

    # 换台设备、换 IP、换浏览器（等于「换网络 + 清 Cookie」）拿正确密码也一样进不来
    other = raw_client("192.168.1.77")
    r = login(other, "pw", user_agent="Mozilla/5.0 (iPad) Safari/604.1")
    assert "access_token" not in r.cookies
    assert r.headers["location"].startswith("/access?next=")

    # 这台设备看到的解锁页换成「已锁定」那一版：没有输入框，说清还要等多久
    page = other.get("/access").text
    assert "登录已暂时锁定" in page
    assert 'name="password"' not in page
    assert "立即解锁" in page

    # 面板看得到这本账（含「是哪台设备在猜」），也能一键清零
    payload = client.get("/panel/api/lan").json()
    assert payload["guard"]["failures"] == access.MAX_PASSWORD_FAILURES
    assert payload["guard"]["locked_seconds"] > 0
    assert payload["guard"]["lock_after"] == access.MAX_PASSWORD_FAILURES
    devices = payload["guard"]["devices"]
    assert len(devices) == 1
    assert devices[0]["ip"] == REMOTE_HOST
    assert REMOTE_HOST in devices[0]["name"]
    assert devices[0]["failures"] == access.MAX_PASSWORD_FAILURES
    assert "ua_hash" not in devices[0]                # 内部指纹不给界面
    assert client.post("/panel/api/lan/guard/clear", json={}).json()["ok"] is True
    assert client.get("/panel/api/lan").json()["guard"]["failures"] == 0
    assert client.get("/panel/api/lan").json()["guard"]["locked_seconds"] == 0
    # 清零之后马上就能进
    assert login(other, "pw").cookies.get("access_token")


def test_locked_login_never_lets_anyone_guess_again(remote, cfg_file):
    """锁着的时候连密码都不校验：换 IP、换浏览器、清 Cookie 也换不来一次新的猜测。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for _ in range(access.MAX_PASSWORD_FAILURES):
        login(remote, "wrong")
    before = access.guard()

    fresh = raw_client("10.1.2.3")                     # 新地址、新浏览器、手上什么都没有
    assert "access_token" not in login(fresh, "pw", user_agent="curl/8.5").cookies
    assert "access_token" not in login(fresh, "wrong2", user_agent="curl/8.5").cookies
    # 账本一个数字都没动：不是「又猜了一次」，是根本没让它猜
    assert access.guard() == before
    # 接口那边一样拿不到东西
    assert fresh.get("/api/stats").status_code == 401
    assert fresh.get("/panel/api/status").status_code == 401


def test_guard_doubles_the_wait_and_survives_a_restart(cfg_file):
    """每多错一次锁得久一倍（24 小时封顶），而且这本账写进配置，重启服务也在。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    t0 = 1_700_000_000
    for _ in range(access.MAX_PASSWORD_FAILURES):
        access.record_guard_failure(now=t0)
    assert access.guard()["locked_until"] == t0 + access.LOCK_STEP_SECONDS

    # 锁一到期他又来一次：这次要等 30 分钟
    access.record_guard_failure(now=t0 + access.LOCK_STEP_SECONDS)
    assert access.guard()["locked_until"] == t0 + access.LOCK_STEP_SECONDS + 2 * access.LOCK_STEP_SECONDS

    # 一直猜下去也不会超过 24 小时
    for _ in range(40):
        access.record_guard_failure(now=t0)
    assert access.guard()["locked_until"] == t0 + access.LOCK_MAX_SECONDS

    # 账本在配置里：重新读一次（等于换了个进程）依然认得出还没解禁
    access.invalidate_cache()
    assert access.guard_locked_seconds(now=t0) == access.LOCK_MAX_SECONDS
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["access_guard"]["failures"] == access.MAX_PASSWORD_FAILURES + 41


def test_right_password_clears_the_ledger(remote, cfg_file):
    """「连续」才算：中间输对过一次，账本就归零。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for _ in range(access.MAX_PASSWORD_FAILURES - 1):
        login(remote, "wrong")
    assert access.guard()["failures"] == access.MAX_PASSWORD_FAILURES - 1
    assert login(remote, "pw").cookies.get("access_token")
    assert access.guard() == {"failures": 0, "locked_until": 0, "devices": []}

    # 清掉 Cookie 再连错 4 次，也只是普通的「密码不对」（真的清零了）
    fresh = raw_client()
    for _ in range(access.MAX_PASSWORD_FAILURES - 1):
        assert login(fresh, "wrong").headers["location"].startswith("/access?err=1")


def test_device_is_paused_after_lots_of_guesses_and_can_be_released(remote, client, cfg_file):
    """一直守着猜的那台设备，连着错够了会被单独停下，要在面板上点「解除」。

    这本账认的是「同一台设备」（IP + 浏览器指纹），被换网络绕过去也无所谓 —— 卡住
    「换网络接着猜」的是上面那本全服务共用的账。所以这里每错一次都把全服务的账解开，
    模拟「锁一到期他又试一次」，才走得到这台设备的暂停。
    """
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    where = ""
    for _ in range(access.MAX_DEVICE_FAILURES):
        access.clear_guard()
        where = login(remote, "wrong").headers["location"]
    assert where == "/access?next=%2Fpanel"
    assert "blocked" not in where, "网址里不能留「已暂停」的标记，否则解除之后刷新还是那一版"

    # 暂停之后连正确密码也不受理、也不下发凭据
    access.clear_guard()
    r = login(remote, "pw")
    assert r.headers["location"].startswith("/access?next=")
    assert "access_token" not in r.cookies

    # 解锁页换成「不带输入框」的那一版，并且说清楚要去哪儿解除
    page = remote.get("/access").text
    assert "被暂停" in page
    assert 'name="password"' not in page
    assert "需手动确认" in page

    # 设备进了「需手动确认」，原因写明；设备信息跟信任设备一样（名字 + IP）
    payload = client.get("/panel/api/lan").json()
    assert len(payload["pending"]) == 1
    entry = payload["pending"][0]
    assert entry["reason"] == access.PENDING_TOO_MANY_FAILURES
    assert entry["ip"] == REMOTE_HOST
    assert REMOTE_HOST in entry["name"]
    assert payload["max_password_failures"] == access.MAX_PASSWORD_FAILURES
    assert payload["max_device_failures"] == access.MAX_DEVICE_FAILURES
    assert "token_hash" not in entry

    # 面板上点「解除」之后：解锁页立刻回到「可以输密码」的那一版（刷新不会还停在暂停页）
    assert client.post("/panel/api/lan/pending/forget", json={"id": entry["id"]}).json()["removed"] is True
    assert client.get("/panel/api/lan").json()["pending"] == []
    refreshed = remote.get("/access").text
    assert 'name="password"' in refreshed
    assert "被暂停" not in refreshed

    # 而且能重新输对密码进去（带路径的地址也一样）
    r = login(remote, "pw")
    assert r.cookies.get("access_token")
    assert remote.get("/panel").status_code == 200


def test_pending_can_be_released_all_at_once(remote, client, cfg_file):
    """「全部解除」一次清空待确认列表。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for agent in ("Mozilla/5.0 (Windows NT 10.0) Chrome/120.0",
                  "Mozilla/5.0 (iPad) Safari/604.1"):
        for _ in range(access.MAX_DEVICE_FAILURES):
            access.clear_guard()
            login(remote, "wrong", user_agent=agent)
    assert len(client.get("/panel/api/lan").json()["pending"]) == 2

    body = client.post("/panel/api/lan/pending/forget-all", json={}).json()
    assert body["ok"] is True and body["removed"] == 2
    assert client.get("/panel/api/lan").json()["pending"] == []


def test_clearing_the_password_also_clears_the_ledger_and_pending_list(remote, client, cfg_file):
    """取消访问密码之后，猜密码那本账和「需手动确认」一起清掉。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for _ in range(access.MAX_DEVICE_FAILURES):
        access.clear_guard()
        login(remote, "wrong")
    assert len(client.get("/panel/api/lan").json()["pending"]) == 1
    assert access.guard()["failures"] == 1          # 最后一次错误留在账本上

    client.post("/panel/api/lan/password", json={"password": ""})
    payload = client.get("/panel/api/lan").json()
    assert payload["pending"] == []
    assert payload["guard"]["failures"] == 0


def test_setting_a_new_password_clears_the_ledger(remote, client, cfg_file):
    """改密码的人是主人（只有本机面板能改）：顺手把账清零，不用再找一次「立即解锁」。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for _ in range(access.MAX_PASSWORD_FAILURES):
        login(remote, "wrong")
    assert access.guard_locked_seconds() > 0

    client.post("/panel/api/lan/password", json={"password": "brand-new"})
    assert access.guard() == {"failures": 0, "locked_until": 0, "devices": []}
    assert login(remote, "brand-new").cookies.get("access_token")


# ── 隧道转进来的请求（公网）──

def test_login_guard_lists_which_devices_are_guessing(remote, client, cfg_file):
    """「登录保护」要说清是哪几台设备在猜：同一台设备猜几次算一台，换地址才是另一台。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    login(remote, "wrong", user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0")
    login(raw_client(), "wrong", user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0")
    login(raw_client("192.168.1.30"), "wrong", user_agent="Mozilla/5.0 (iPad) Safari/604.1")

    devices = client.get("/panel/api/lan").json()["guard"]["devices"]
    assert len(devices) == 2, "同一台设备的两次猜测要并成一台，换地址才是另一台"
    by_ip = {row["ip"]: row for row in devices}
    assert by_ip[REMOTE_HOST]["failures"] == 2
    assert REMOTE_HOST in by_ip[REMOTE_HOST]["name"]
    assert by_ip["192.168.1.30"]["failures"] == 1
    assert "iPad" in by_ip["192.168.1.30"]["name"]
    assert all(row["last_at"] > 0 for row in devices)

    # 清零之后这份清单也空了
    client.post("/panel/api/lan/guard/clear", json={})
    assert client.get("/panel/api/lan").json()["guard"]["devices"] == []


def test_guard_device_list_is_capped(cfg_file):
    """猜密码的设备太多时只留最近的那几台，别让配置无限长。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    for index in range(access.MAX_GUARD_DEVICES + 5):
        access.record_guard_failure(ip=f"192.168.1.{index + 1}", user_agent=f"agent-{index}")
    devices = access.guard_devices()
    assert len(devices) == access.MAX_GUARD_DEVICES
    assert devices[0]["ip"] == f"192.168.1.{access.MAX_GUARD_DEVICES + 5}"   # 最近的排最前


def test_tunnel_requests_count_as_outsiders(cfg_file):
    """公网隧道连的是本机回环地址：要把隧道写的真实访客地址认出来。

    不认的话，公网访客在服务看来全是「本机」，访问密码那道门等于没关。
    """
    import backend.main as main

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    tunnel = TestClient(_FromHost(main.app, LOCAL_HOST), base_url=f"http://{LOCAL_HOST}",
                        follow_redirects=False)
    r = tunnel.get("/panel", headers={"cf-connecting-ip": TUNNEL_HOST})
    assert r.status_code == 303
    assert r.headers["location"] == "/access?next=%2Fpanel"
    r = tunnel.get("/panel", headers={"x-forwarded-for": f"{TUNNEL_HOST}, 172.68.1.1"})
    assert r.status_code == 303
    # 没有这些头（真的本机浏览器）：直接放行
    assert tunnel.get("/panel").status_code == 200


def test_a_lan_device_cannot_spoof_its_way_in(cfg_file):
    """局域网里的设备自己编「我是本机」的头没用：对端不是回环，一个头都不看。"""
    import backend.main as main

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    remote = TestClient(_FromHost(main.app, REMOTE_HOST), base_url=f"http://{REMOTE_HOST}",
                        follow_redirects=False)
    r = remote.get("/panel", headers={"cf-connecting-ip": "127.0.0.1",
                                      "x-forwarded-for": "127.0.0.1"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/access?next=")


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
    other = raw_client("192.168.1.10")                 # 另一台设备（换了个地址）
    second = login(other).cookies.get("access_token")
    device_id = access.parse_cookie(first)[0]
    r = client.post("/panel/api/lan/devices/forget", json={"id": device_id})
    assert r.json()["removed"] is True
    assert len(r.json()["payload"]["devices"]) == 1
    # 被移除的那台立刻失效，另一台不受影响
    assert remote.get("/panel", headers={"cookie": f"access_token={first}"}).status_code == 303
    assert access.is_trusted(second) is True


def test_same_browser_logging_in_twice_is_one_device(remote, cfg_file):
    """同一个浏览器把解锁请求提交了两次：面板里该只有一台设备，点一次就都清掉。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    first = login(remote).cookies.get("access_token")
    second = login(raw_client()).cookies.get("access_token")     # 提交时还没带上 Cookie
    assert first and second and first != second
    assert len(access.trusted_devices()) == 2                    # 配置里确实是两条记录
    devices = access.list_devices()
    assert len(devices) == 1                                     # 界面上只显示一台
    assert devices[0]["ip"] == REMOTE_HOST
    # 「不再信任」把同一台设备的记录一起删掉：两条 Cookie 都失效
    assert access.forget_device(devices[0]["id"]) is True
    assert access.trusted_devices() == []
    for token in (first, second):
        assert remote.get("/panel",
                          headers={"cookie": f"access_token={token}"}).status_code == 303


def test_already_trusted_device_is_not_registered_again(remote, cfg_file):
    """已经通过过的设备再提交一次密码，不该在名单里凭空多出一台。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    login(remote)
    again = login(remote, next_path="/")
    assert again.status_code == 303
    assert again.headers["location"] == "/"
    assert len(access.trusted_devices()) == 1
    assert len(access.list_devices()) == 1


def test_different_devices_stay_separate(cfg_file):
    """地址不同、或浏览器不同，就是两台设备，不能并成一行。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    access.remember_device("192.168.1.9", "Mozilla/5.0 Chrome/120")
    access.remember_device("192.168.1.9", "Mozilla/5.0 Firefox/121")        # 换浏览器
    access.remember_device("192.168.1.10", "Mozilla/5.0 Chrome/120")        # 换设备
    assert len(access.list_devices()) == 3


def test_legacy_records_without_a_fingerprint_still_merge(cfg_file):
    """升级前写下的记录没有 ua_hash，按显示名也能认出「这是同一台」。"""
    write_cfg(cfg_file, {
        "lan_access": True,
        "lan_password_hash": access.hash_password("pw"),
        "access_trusted_devices": [
            {"id": "old1", "token_hash": "a", "name": "Edge · Windows（192.168.1.9）",
             "ip": "192.168.1.9", "trusted_at": 100},
            {"id": "old2", "token_hash": "b", "name": "Edge · Windows（192.168.1.9）",
             "ip": "192.168.1.9", "trusted_at": 200},
        ],
    })
    devices = access.list_devices("old2")
    assert len(devices) == 1
    assert devices[0]["id"] == "old2"          # 代表 id 取最近登录的那条
    assert devices[0]["trusted_at"] == 200
    assert devices[0]["current"] is True
    assert access.forget_device("old1") is True
    assert access.trusted_devices() == []


def test_panel_can_forget_every_device(client, remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    login(remote)
    login(raw_client("192.168.1.10"))          # 第二台设备
    r = client.post("/panel/api/lan/devices/forget-all")
    assert r.json()["removed"] == 2
    assert r.json()["payload"]["devices"] == []


def test_forget_all_counts_devices_not_records(client, remote, cfg_file):
    """同一台设备遗留多条记录时，「全部不再信任」报的数字要跟列表里的行数一致。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    login(remote)
    login(raw_client())                        # 同一台设备的第二条记录
    assert len(access.trusted_devices()) == 2
    assert len(client.get("/panel/api/lan").json()["devices"]) == 1
    assert client.post("/panel/api/lan/devices/forget-all").json()["removed"] == 1


def test_panel_sets_and_clears_the_access_password(client, remote, cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    r = client.post("/panel/api/lan/password", json={"password": "new-pw"})
    assert r.json()["has_password"] is True
    assert access.verify_password("new-pw") is True
    assert remote.get("/panel").status_code == 303
    r = client.post("/panel/api/lan/password", json={"password": ""})
    assert r.json()["has_password"] is False
    # 密码一清，局域网里又回到「不用输密码直接进」
    assert remote.get("/panel").status_code == 200


def test_changing_the_password_signs_every_device_out(cfg_file):
    """换访问密码 = 换锁：所有设备的登录态立刻作废（局域网 + 公网都要用新密码重来）。

    主人改密码往往正是因为「有别人知道旧密码」—— 那时候光换哈希还不够，对方手上那块
    Cookie 还认不认才是关键。这里把「认不认」钉死。
    """
    import backend.main as main

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("old")})
    lan = TestClient(_FromHost(main.app, REMOTE_HOST), base_url=f"http://{REMOTE_HOST}",
                     follow_redirects=False)
    tunnel = TestClient(_FromHost(main.app, LOCAL_HOST), base_url=f"http://{LOCAL_HOST}",
                        follow_redirects=False)
    host = TestClient(_FromHost(main.app, LOCAL_HOST), base_url=f"http://{LOCAL_HOST}",
                      follow_redirects=False)
    tunnel_headers = {"cf-connecting-ip": TUNNEL_HOST}

    lan_token = login(lan, "old").cookies.get("access_token")
    tunnel_token = tunnel.post("/api/access/login", data={"password": "old", "next": "/panel"},
                               headers=tunnel_headers).cookies.get("access_token")
    assert lan_token and tunnel_token
    assert len(access.list_devices()) == 2
    assert lan.get("/panel", headers={"cookie": f"access_token={lan_token}"}).status_code == 200
    assert tunnel.get("/panel", headers={**tunnel_headers,
                                         "cookie": f"access_token={tunnel_token}"}).status_code == 200

    body = host.post("/panel/api/lan/password", json={"password": "new"}).json()

    assert body["has_password"] is True
    assert body["cleared_devices"] == 2
    assert access.trusted_devices() == []
    assert access.list_devices() == []

    # 两台设备手上的通行证立刻失效：页面请求又被送去登录页，接口回 401
    assert lan.get("/panel", headers={"cookie": f"access_token={lan_token}"}).status_code == 303
    assert tunnel.get("/panel", headers={**tunnel_headers,
                                         "cookie": f"access_token={tunnel_token}"}).status_code == 303
    assert tunnel.get("/api/stats", headers={**tunnel_headers,
                                             "cookie": f"access_token={tunnel_token}"}).status_code == 401

    # 旧密码不认了，新密码能进
    assert "access_token" not in login(lan, "old").cookies
    assert login(lan, "new").cookies.get("access_token")


def test_changing_the_password_also_clears_the_pending_list(client, cfg_file):
    """「需手动确认」也是设备登录态的一部分：换密码时一起清掉。"""
    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("old")})
    for _ in range(access.MAX_DEVICE_FAILURES):
        access.clear_guard()
        login(raw_client(), "wrong")
    assert len(access.list_pending_devices()) == 1

    client.post("/panel/api/lan/password", json={"password": "new"})
    assert access.list_pending_devices() == []


def test_clearing_the_password_keeps_public_access(client, cfg_file, monkeypatch):
    """公网开着的时候清掉访问密码：会警告、要确认，但**不会**顺手关掉公网访问。

    这条钉住「不替用户关」这半边（警告那半边由 test_public_access 钉住）：清密码只改回
    密码本身，公网访问保持原来的开关和域名，隧道进程也不去动它。
    """
    from backend import cloudflared as cf

    write_cfg(cfg_file, {"lan_access": True, "lan_password_hash": access.hash_password("pw")})
    access.set_public_access(enabled=True, domain="example.com")
    stopped = []
    monkeypatch.setattr(cf, "stop_tunnel", lambda: stopped.append(True) or True)

    body = client.post("/panel/api/lan/password", json={"password": ""}).json()

    assert body["has_password"] is False
    assert "public_closed" not in body
    assert stopped == [], "清密码不该去动隧道"
    assert access.public_enabled() is True
    assert access.public_domain() == "example.com"


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
