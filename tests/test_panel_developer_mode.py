"""开发者模式（「关于」页最下面的开关）。

它管着三件事，这里都钉住：

* **显示**：打开后「关于」页才多出「私有仓库凭据」、「设置」页才多出「局域网访问」；
* **局域网**：关掉时如果局域网正开着，先把局域网关掉（访问密码与信任设备都保留）、
  重启一次服务把监听地址切回本机，然后才关掉开发者模式；重启没起来要能看出来；
* **私有仓库凭据**：关掉后检查更新 / 自动更新不再使用已保存的 Token，但 **Token 文件
  不删** —— 重新打开开关就能接着用，要彻底删除只能用凭据框右边的「清除」。

另外验一遍面板页面上的接线：一拨就存（没有「应用」按钮）、关之前弹确认框问一句、
关着时那两项 hidden、重启期间等它回来再刷新，以及开关下面那行小字只有一句话。
"""
import asyncio
import json
import os

import pytest
from starlette.requests import Request

from backend import control_panel as cp
from common import github_auth, paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")
TOKEN = "github_pat_" + "A" * 40


def _request() -> Request:
    """够用的假请求：_lan_status_payload 只看来源地址和 Cookie。"""
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/panel/api/config/developer-mode",
        "raw_path": b"/panel/api/config/developer-mode",
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    })


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "github_token"
    monkeypatch.setattr(github_auth, "TOKEN_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def no_real_exit(monkeypatch):
    """关开关可能顺带关局域网 → 会走重启助手：全部打桩，pytest 进程不能被自己弄退。"""
    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("0.0.0.0", 8000))
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (True, ""))
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port, **kw: True)
    monkeypatch.setattr(cp, "_request_process_exit", lambda: None)
    monkeypatch.setattr(cp, "_force_exit", lambda: None)
    monkeypatch.setattr(cp, "_lan_restart_state",
                        {"running": False, "host": "", "started_at": None})
    monkeypatch.setattr(cp.asyncio, "create_task", lambda coro, **kw: coro.close())


def _write_config(config_path, **values):
    config_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


def _saved(config_path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


def _disable() -> dict:
    return asyncio.run(cp.set_developer_mode(cp.DeveloperModeToggle(enabled=False), _request()))


# ── 设置的存取 ────────────────────────────────────────────────────────────

def test_developer_mode_is_off_when_never_configured(config_path):
    assert asyncio.run(cp.get_developer_mode())["enabled"] is False


def test_developer_mode_is_saved_and_read_back(config_path):
    saved = asyncio.run(cp.set_developer_mode(cp.DeveloperModeToggle(enabled=True), _request()))

    assert saved["status"] == "ok" and saved["enabled"] is True
    assert _saved(config_path)["developer_mode"] is True
    assert asyncio.run(cp.get_developer_mode())["enabled"] is True

    _disable()

    assert asyncio.run(cp.get_developer_mode())["enabled"] is False


def test_toggling_keeps_every_other_setting(config_path):
    """拨这个开关只动它自己和（必要时）局域网开关：别人存过的设置原样留着。"""
    _write_config(config_path, password_hash="abc", api_token="tok",
                  update_schedule={"enabled": True})

    asyncio.run(cp.set_developer_mode(cp.DeveloperModeToggle(enabled=True), _request()))
    _disable()

    saved = _saved(config_path)
    assert saved["developer_mode"] is False
    assert saved["password_hash"] == "abc"
    assert saved["api_token"] == "tok"
    assert saved["update_schedule"] == {"enabled": True}


def test_old_config_without_the_field_stays_off(tmp_path, monkeypatch):
    """老版本写下的配置里没有 developer_mode：读回来必须是关的（界面保持精简）。"""
    path = tmp_path / "panel_config.json"
    path.write_text(json.dumps({"schedule": "0 6 * * *"}), encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))

    assert asyncio.run(cp.get_developer_mode())["enabled"] is False


def test_status_tells_the_panel_what_is_still_in_use(config_path, token_file):
    """确认框要知道「局域网开着吗、公网开着吗、存过 Token 吗」，一次请求全给出来。"""
    _write_config(config_path, developer_mode=True, lan_access=True)
    github_auth.save(TOKEN)

    payload = asyncio.run(cp.get_developer_mode())

    assert payload == {"enabled": True, "lan_on": True, "public_on": False,
                       "token_set": True, "token_from_private_repo": True}


# ── 关开关时：局域网一起关掉，但设置留着 ──────────────────────────────────

def test_lan_is_turned_off_with_password_and_devices_kept(config_path, monkeypatch):
    """局域网开着时关开发者模式：先关局域网（密码与信任设备保留）并重启，再关开关。"""
    _write_config(config_path, developer_mode=True, lan_access=True,
                  lan_password_hash="salt:hash",
                  access_trusted_devices=[{"id": "dev-1", "name": "我的手机"}])
    spawned: list = []
    monkeypatch.setattr(cp, "_spawn_restart_helper",
                        lambda host, port, **kw: spawned.append((host, port)) or True)

    payload = _disable()

    saved = _saved(config_path)
    assert saved["developer_mode"] is False
    assert saved["lan_access"] is False, "界面藏起来还不够，门也得关上"
    assert saved["lan_password_hash"] == "salt:hash", "访问密码要留着"
    assert saved["access_trusted_devices"] == [{"id": "dev-1", "name": "我的手机"}], \
        "信任设备要留着"
    assert spawned == [("127.0.0.1", 8000)], "监听地址要切回本机"
    assert payload["restarting"] is True
    assert payload["lan_on"] is False and payload["enabled"] is False


def test_lan_off_means_no_restart(config_path, monkeypatch):
    """局域网本来就关着：不重启，直接关掉开发者模式。"""
    _write_config(config_path, developer_mode=True)
    spawned: list = []
    monkeypatch.setattr(cp, "_spawn_restart_helper",
                        lambda host, port, **kw: spawned.append((host, port)) or True)

    payload = _disable()

    assert payload["restarting"] is False
    assert "restart_error" not in payload and "restart_code" not in payload
    assert spawned == []
    assert _saved(config_path)["developer_mode"] is False


def test_restart_that_cannot_start_is_reported_but_settings_still_apply(config_path, monkeypatch):
    """助手起不来：局域网照样关掉（配置里），但要让面板提示手动重启一次。"""
    _write_config(config_path, developer_mode=True, lan_access=True)
    monkeypatch.setattr(cp, "_spawn_restart_helper", lambda host, port, **kw: False)

    payload = _disable()

    assert payload["restarting"] is False
    assert "手动重启" in payload["restart_error"]
    saved = _saved(config_path)
    assert saved["lan_access"] is False and saved["developer_mode"] is False


def test_import_check_failure_also_only_warns(config_path, monkeypatch):
    """新代码自检没过：同样不重启，但两个开关都已经落盘。"""
    _write_config(config_path, developer_mode=True, lan_access=True)
    monkeypatch.setattr(cp, "_new_code_imports_ok", lambda: (False, "SyntaxError: bad"))

    payload = _disable()

    assert payload["restarting"] is False
    assert "自检" in payload["restart_error"]
    assert _saved(config_path)["developer_mode"] is False


def test_uvicorn_reload_mode_asks_for_a_manual_restart(config_path, monkeypatch):
    """uvicorn --reload（读不到自己的命令行）：没法自己重启，把那条提示递给面板。"""
    _write_config(config_path, developer_mode=True, lan_access=True)
    monkeypatch.setattr(cp, "_server_cli_address", lambda: None)

    payload = _disable()

    assert payload["restarting"] is False
    assert payload["restart_code"] == "dev_mode"
    assert _saved(config_path)["lan_access"] is False


def test_enabling_never_touches_the_lan_switch(config_path):
    _write_config(config_path, lan_access=False)

    asyncio.run(cp.set_developer_mode(cp.DeveloperModeToggle(enabled=True), _request()))

    assert _saved(config_path)["lan_access"] is False


# ── 关开关时：Token 只「不再使用」，不删 ──────────────────────────────────

def test_disabling_keeps_the_token_file_but_stops_using_it(config_path, token_file):
    _write_config(config_path, developer_mode=True)
    github_auth.save(TOKEN)
    assert cp._github_token() == TOKEN

    payload = _disable()

    assert github_auth.load() == TOKEN, "Token 文件要留着，用户不用重新粘贴"
    assert cp._github_token() == "", "关掉之后不再拿它读私有仓库"
    assert payload["token_set"] is True, "面板还要知道「存过 Token」"
    assert payload["token_from_private_repo"] is False


def test_re_enabling_makes_the_same_token_usable_again(config_path, token_file):
    _write_config(config_path, developer_mode=True)
    github_auth.save(TOKEN)
    _disable()

    asyncio.run(cp.set_developer_mode(cp.DeveloperModeToggle(enabled=True), _request()))

    assert cp._github_token() == TOKEN, "重新打开就能接着用，不用重新设置"
    payload = asyncio.run(cp.get_developer_mode())
    assert payload["token_from_private_repo"] is True


# ── 面板页面上的接线 ──────────────────────────────────────────────────────

def _about_page(html: str) -> str:
    """「关于」页那一整块（开发者模式开关就在它的最下面）。"""
    return html.split('id="page-about"', 1)[1].split("</main>", 1)[0]


def _dev_mode_section(html: str) -> str:
    """开发者模式那一段（开关 + 小字），一直到页面内容结束。"""
    return html.split('id="devModeSection"', 1)[1].split("</main>", 1)[0]


def test_switch_lives_at_the_bottom_of_the_about_page():
    """开关在「关于」页最下面：它前面是环境检测，后面就是页面结束。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    about = _about_page(html)
    settings = html.split('id="page-settings"', 1)[1].split('id="page-logs"', 1)[0]

    assert 'id="devModeSection"' in about
    assert 'id="devModeSection"' not in settings, "这个开关已经不在设置页了"
    assert about.index('id="envCheckBtn"') < about.index('id="devModeSection"')


def test_panel_html_wires_the_developer_mode_switch():
    html = open(PANEL_HTML, encoding="utf-8").read()
    section = _dev_mode_section(html)

    assert 'id="devModeEnabled"' in section
    assert 'onchange="setDeveloperMode(this.checked)"' in section, "勾选框自己带事件 = 一拨就存"
    assert "<button" not in section, "这个开关没有「应用」按钮，点了就生效"
    assert "async function setDeveloperMode(enabled)" in html
    assert "await fetch('/panel/api/config/developer-mode'" in html      # 读回来
    assert "body: JSON.stringify({ enabled: wanted })" in html           # 存下去
    assert "applyDeveloperMode(!wanted);" in html, "没存上要把开关拨回原位，不骗人"
    assert "setDevModeStatus" in html, "存好 / 存不上都要说一声"


def test_the_switch_asks_before_turning_off_what_is_still_in_use():
    """关之前弹确认框：局域网 / 公网访问开着、存过 Token 都要讲清楚会发生什么。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert "devModeState.lan_on || devModeState.public_on || devModeState.token_set" in html
    assert "async function confirmDevModeOff(state)" in html
    assert "devModeOffLanWarn" in html and "devModeOffTokenWarn" in html
    assert "devModeOffPublicWarn" in html
    assert "box.checked = true; return;" in html, "用户取消就把开关拨回去"


def test_the_switch_waits_for_the_restart_before_reloading():
    """顺带关了局域网：监听地址要重启才切回本机，页面等它回来再刷新。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert "if (d.restarting) { beginDevModeRestart(); return; }" in html
    assert "function beginDevModeRestart()" in html
    assert "/panel/api/lan?expect_host=127.0.0.1" in html
    assert "devModeRestartWait" in html
    assert "devModeRestartFailed" in html


def test_the_switch_gates_the_credential_box_and_lan_access():
    """开关管着两项：「关于」页的凭据框、「设置」页的局域网访问，默认都是 hidden。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert '<div class="ab-token" id="abTokenBox" hidden>' in html
    assert '<div class="section" id="lanSection" hidden>' in html
    assert "tokenBox.hidden = !enabled;" in html
    assert "lanSection.hidden = !enabled;" in html
    assert "loadDeveloperMode();" in html          # 进「关于」/「设置」页与登录后各读一次
    # 摘掉的只是这两项：检查更新、定时检查那些照旧显示
    assert 'id="updateCheckBtn"' in html
    assert 'id="abAutoCheck"' in html


def test_the_note_under_the_switch_is_one_sentence():
    """开关下面那行小字只说「会多出哪两项」，一句话讲完，不铺开介绍这个功能。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    zh = "开启后，「关于」页会显示「私有仓库凭据」，「设置」页会显示「局域网访问」。"
    assert zh in html
    assert ("When on, the About page shows the private-repo credential and "
            "Settings shows LAN access.") in html
    assert zh.count("。") == 1, "小字只要一句话"


def test_developer_mode_copy_has_both_languages():
    html = open(PANEL_HTML, encoding="utf-8").read()

    for key in ("devModeTitle", "devModeLabel", "devModeNote", "devModeSaved",
                "devModeSaveFailed", "devModeOffConfirmTitle", "devModeOffConfirmOk",
                "devModeOffLanWarn", "devModeOffTokenWarn", "devModeRestartWait",
                "devModeRestartFailed"):
        assert html.count(key + ":") == 2, f"{key} 需要中英文各一份"
