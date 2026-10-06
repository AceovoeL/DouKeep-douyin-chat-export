"""「公网访问」（backend/cloudflared.py 与面板接口）的行为测试。

真实域名、真实隧道 ID、真实账号信息都不进仓库 —— 这里用的是 example.com 这类占位域名。
"""
import base64
import json
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

import common.paths as paths
from backend import cloudflared as cf
from common import access

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把配置指到临时文件，并把下载目录也挪到临时目录（别碰真实项目）。

    ``CONFIG_FILE`` / ``PID_FILE`` 是模块加载时按 paths 算好的常量，光改 paths 挪不动它们 ——
    不一起改的话，``write_config`` 和 ``start_tunnel`` 会真的往项目 ``config/cloudflared/``
    里写（之前就留下过 config.yml 和 tunnel.pid）。
    """
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    monkeypatch.setattr(paths, "CLOUDFLARED_DIR", str(tmp_path / "cloudflared"))
    monkeypatch.setattr(paths, "CLOUDFLARED_LOG", str(tmp_path / "cloudflared.log"))
    monkeypatch.setattr(cf, "CONFIG_FILE", str(tmp_path / "cloudflared" / "config.yml"))
    monkeypatch.setattr(cf, "PID_FILE", str(tmp_path / "cloudflared" / "tunnel.pid"))
    access.invalidate_cache()
    cf.JOB.reset()
    yield path
    access.invalidate_cache()
    cf.JOB.reset()


@pytest.fixture
def client(cfg_file):
    import backend.main as main

    return TestClient(main.app)


def write_cfg(cfg_file, data: dict) -> None:
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    access.invalidate_cache()


def lan_on_with_password(cfg_file) -> None:
    write_cfg(cfg_file, {"lan_access": True,
                         "lan_password_hash": access.hash_password("pw")})


# ── 域名校验 ──

@pytest.mark.parametrize("value,expected", [
    ("example.com", "example.com"),
    ("  Chat.Example.COM  ", "chat.example.com"),
    ("https://example.com", "example.com"),
    ("http://chat.example.com/", "chat.example.com"),
    ("my-chat.example.co.uk", "my-chat.example.co.uk"),
])
def test_normalize_domain_accepts(value, expected):
    assert cf.normalize_domain(value) == expected


@pytest.mark.parametrize("value", [
    "", "example", "example.com/panel", "https://example.com/panel",
    "example.com:8000", "exa mple.com", "127.0.0.1", "-bad.example.com",
    "example..com", "http://", "*",
])
def test_normalize_domain_rejects(value):
    with pytest.raises(cf.TunnelError) as err:
        cf.normalize_domain(value)
    assert err.value.code == "bad_domain"


# ── 前置条件与访问密码 ──

def test_public_ready_only_requires_lan(cfg_file):
    """挂载只要求先开局域网；没设访问密码不再硬拦 —— 面板会红字警告 + 二次确认。"""
    write_cfg(cfg_file, {})
    assert access.public_ready() == (False, "lan_off")
    write_cfg(cfg_file, {"lan_access": True})
    assert access.public_ready() == (True, "")
    lan_on_with_password(cfg_file)
    assert access.public_ready() == (True, "")


def test_password_lock_applies_to_public_access_too(cfg_file):
    """只开公网（没开局域网）时那道锁也要生效 —— 面板上两把锁其实是同一把。"""
    write_cfg(cfg_file, {"public_access": {"enabled": True, "domain": "example.com"},
                         "lan_password_hash": access.hash_password("pw")})
    assert access.open_to_outsiders() is True
    assert access.password_required() is True


def test_public_urls_need_a_domain(cfg_file):
    write_cfg(cfg_file, {})
    assert access.public_urls() == {}
    access.set_public_access(domain="example.com")
    assert access.public_urls() == {"panel": "https://example.com/panel",
                                    "viewer": "https://example.com"}


def test_public_settings_survive_disable(cfg_file):
    write_cfg(cfg_file, {})
    access.set_public_access(enabled=True, domain="example.com", tunnel="doukeep")
    access.set_public_access(enabled=False)
    assert access.public_enabled() is False
    assert access.public_domain() == "example.com"          # 域名留着
    assert access.public_tunnel_name() == "doukeep"


# ── 程序文件 ──

def test_binary_name_follows_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cf.platform, "machine", lambda: "AMD64")
    assert cf.binary_name() == "cloudflared-windows-amd64.exe"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cf.platform, "machine", lambda: "aarch64")
    assert cf.binary_name() == "cloudflared-linux-arm64"


def test_binary_path_ignores_a_truncated_download(cfg_file, monkeypatch):
    os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
    local = os.path.join(paths.CLOUDFLARED_DIR, cf.binary_name())
    with open(local, "wb") as handle:
        handle.write(b"not really cloudflared")
    monkeypatch.setattr(cf.shutil, "which", lambda name: "/usr/bin/cloudflared")
    assert cf.binary_path() == "/usr/bin/cloudflared"       # 半截文件不当成有


def test_download_reports_progress_and_resumes(cfg_file, monkeypatch):
    payload = b"x" * (cf._MIN_BINARY_SIZE + 10)
    seen = []

    class _Response:
        status = 206
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._sent = False

        def read(self, size):
            if self._sent:
                return b""
            self._sent = True
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["range"] = request.headers.get("Range")
        return _Response()

    # 先放一个半截文件，看它是不是带着 Range 去续传
    os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
    with open(cf._partial_path(), "wb") as handle:
        handle.write(b"y" * 1024)
    monkeypatch.setattr(cf.urllib.request, "urlopen", fake_urlopen)
    result = cf.download_binary(progress=lambda got, total: seen.append((got, total)))

    assert captured["range"] == "bytes=1024-"
    assert seen and seen[-1][0] == 1024 + len(payload)
    assert os.path.isfile(result) and os.path.getsize(result) == 1024 + len(payload)
    assert not os.path.exists(cf._partial_path())           # .part 已经改名


def test_download_rejects_a_tiny_response(cfg_file, monkeypatch):
    class _Response:
        status = 200
        headers = {"Content-Length": "5"}

        def read(self, size):
            return b"" if getattr(self, "_done", False) else self._set()

        def _set(self):
            self._done = True
            return b"hello"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cf.urllib.request, "urlopen", lambda req, timeout=None: _Response())
    with pytest.raises(cf.TunnelError) as err:
        cf.download_binary()
    assert err.value.code == "download_failed"
    assert not os.path.exists(cf._partial_path())           # 坏文件不留下


def test_download_can_be_cancelled(cfg_file, monkeypatch):
    class _Response:
        status = 200
        headers = {}

        def read(self, size):
            return b"z" * size

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cf.urllib.request, "urlopen", lambda req, timeout=None: _Response())
    # 第一次调用就返回「已取消」：循环应该立刻退出
    with pytest.raises(cf.TunnelError) as err:
        cf.download_binary(cancelled=lambda: True)
    assert err.value.code == "cancelled"
    assert os.path.getsize(cf._partial_path()) <= cf._CHUNK     # 没白下一整份


# ── 挂载流程 ──

@pytest.fixture
def fake_tunnel(monkeypatch):
    """把真正会碰网络的步骤都换掉，只留流程编排本身。"""
    calls = {"dns": [], "config": [], "started": [], "stopped": 0}

    monkeypatch.setattr(cf, "ensure_binary", lambda progress=None, cancelled=None: "cf")
    monkeypatch.setattr(cf, "logged_in", lambda: True)
    monkeypatch.setattr(cf.paths, "CONFIG_DIR", paths.CONFIG_DIR)
    monkeypatch.setattr(cf, "ensure_tunnel", lambda name: "tunnel-uuid-0001")
    monkeypatch.setattr(cf, "write_config",
                        lambda uuid, domain: calls["config"].append((uuid, domain)) or "config.yml")

    def fake_route(name, domain, *, overwrite=False):
        calls["dns"].append((name, domain, overwrite))
        return True, "ok"

    monkeypatch.setattr(cf, "route_dns", fake_route)
    monkeypatch.setattr(cf, "start_tunnel", lambda name: calls["started"].append(name) or 4242)
    monkeypatch.setattr(cf, "stop_tunnel", lambda: calls.__setitem__("stopped", calls["stopped"] + 1))
    monkeypatch.setattr(cf, "tunnel_running", lambda: False)
    monkeypatch.setattr(cf, "wait_connected", lambda offset=0, timeout=0, cancelled=None: True)
    monkeypatch.setattr(cf, "verify_public_url", lambda domain, timeout=0: True)
    return calls


def _run_flow(domain="example.com"):
    cf.JOB.start(domain)
    thread = cf.JOB._thread
    if thread:
        thread.join(timeout=10)
    return cf.JOB.snapshot()


def test_mount_flow_succeeds(cfg_file, fake_tunnel):
    lan_on_with_password(cfg_file)
    snapshot = _run_flow()
    assert snapshot["state"] == "done", snapshot
    assert all(state == "done" for state in snapshot["steps"].values())
    assert fake_tunnel["config"] == [("tunnel-uuid-0001", "example.com")]
    assert fake_tunnel["started"] == [access.PUBLIC_TUNNEL_NAME]
    assert access.public_enabled() is True
    assert access.public_domain() == "example.com"


def test_mount_keeps_a_readable_live_log(cfg_file, fake_tunnel):
    """挂载过程要留一份能读的实时日志（弹窗里滚动显示的就是它）。"""
    lan_on_with_password(cfg_file)
    snapshot = _run_flow()
    log = "\n".join(snapshot["log"])
    assert "开始挂载" in log and "example.com" in log
    assert "— 创建隧道 —" in log and "隧道就绪" in log          # 步骤提示 + 结果
    assert "挂载完成" in log
    assert len(snapshot["log"]) <= cf._JOB_LOG_MAX


def test_mount_keeps_going_when_the_dns_record_already_exists(cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    monkeypatch.setattr(cf, "route_dns",
                        lambda name, domain, *, overwrite=False:
                        (False, "record with that name already exists"))
    snapshot = _run_flow()
    assert snapshot["state"] == "done"
    assert snapshot["warning"] == "dns_exists"
    assert snapshot["steps"]["dns"] == "done"


def test_mount_fails_when_the_tunnel_never_connects(cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    monkeypatch.setattr(cf, "wait_connected", lambda offset=0, timeout=0, cancelled=None: False)
    snapshot = _run_flow()
    assert snapshot["state"] == "error"
    assert snapshot["error"] == "start_failed"
    assert snapshot["steps"]["start"] == "error"
    assert access.public_enabled() is False            # 没挂上就别写「开着」


def test_mount_warns_when_the_domain_does_not_come_back(cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    monkeypatch.setattr(cf, "verify_public_url", lambda domain, timeout=0: False)
    monkeypatch.setattr(cf.time, "sleep", lambda seconds: None)
    snapshot = _run_flow()
    assert snapshot["state"] == "done"
    assert snapshot["warning"] == "verify_failed"


def test_mount_asks_for_browser_authorization_when_not_logged_in(cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    monkeypatch.setattr(cf, "logged_in", lambda: False)

    class _Session:
        url = "https://dash.cloudflare.com/authorize?token=placeholder"

        def start(self):
            pass

        def wait(self, timeout=0, cancelled=None):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(cf, "LoginSession", _Session)
    snapshot = _run_flow()
    assert snapshot["state"] == "done"
    assert snapshot["steps"]["auth"] == "done"


def test_start_mount_refuses_without_lan_access(cfg_file):
    write_cfg(cfg_file, {})
    ok, status = cf.start_mount("example.com")
    assert ok is False
    assert status["job"]["error"] == "lan_off"
    assert status["ready"] is False


def test_mount_status_shape(cfg_file, fake_tunnel):
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    status = cf.mount_status()
    assert status["enabled"] is True
    assert status["urls"]["viewer"] == "https://example.com"
    assert set(status["job"]["steps"]) == set(cf.STEPS)
    assert status["has_password"] is True        # 这个用例里已经设过访问密码
    assert status["binary"] is False             # 临时目录里没有下载好的程序


def test_disable_stops_the_tunnel_but_keeps_the_domain(cfg_file, fake_tunnel):
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    status = cf.disable_public()
    assert fake_tunnel["stopped"] >= 1
    assert status["enabled"] is False
    assert status["domain"] == "example.com"


# ── 面板接口 ──

def test_public_status_endpoint(client, cfg_file):
    lan_on_with_password(cfg_file)
    payload = client.get("/panel/api/public").json()["payload"]
    assert payload["ready"] is True
    assert payload["enabled"] is False
    assert payload["job"]["state"] == "idle"


def test_mount_endpoint_rejects_a_bad_domain(client, cfg_file):
    lan_on_with_password(cfg_file)
    r = client.post("/panel/api/public/mount", json={"domain": "http://bad domain"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_domain"


def test_mount_without_a_password_is_flagged_for_the_panel(client, cfg_file, fake_tunnel):
    """没设访问密码也能挂（面板负责红字警告 + 二次确认），状态里如实报「没有密码」。"""
    write_cfg(cfg_file, {"lan_access": True})
    body = client.post("/panel/api/public/mount", json={"domain": "example.com"}).json()
    assert body["ok"] is True
    assert body["payload"]["has_password"] is False
    cf.JOB.cancel()


def test_status_reports_whether_a_password_is_set(client, cfg_file):
    write_cfg(cfg_file, {"lan_access": True})
    assert client.get("/panel/api/public").json()["payload"]["has_password"] is False
    access.set_password("pw")
    assert client.get("/panel/api/public").json()["payload"]["has_password"] is True


def test_panel_warns_with_red_bold_before_mounting_without_a_password():
    """面板接线：没密码时红字警告 + 挂载前要过一次风险确认。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert "pubNoPasswordWarn" in html and "pubRiskConfirm" in html
    assert 'class="risk-strong"' in html and ".risk-strong" in html
    assert "messageHtmlKey" in html, "风险正文是带标签的，确认框要能按 HTML 渲染"
    assert "publicRiskConfirm({ titleKey: 'pubRiskConfirmTitle'" in html


def test_panel_asks_before_clearing_the_password_under_public_access():
    """面板接线：公网开着时清密码，先问一句（正文是红字加粗的风险说明）。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert "pubClearPasswordTitle" in html and "pubClearPassword" in html
    assert "if (lanAccess && lanAccess.public_on)" in html
    assert "clearLanPassword" in html


def test_panel_does_not_leave_the_mount_button_dead():
    """挂载中关掉弹窗之后，按钮还得能用 —— 这里钉住三处接线：

    1. 挂载中不把「挂载到公网」变灰，改成能点的「查看挂载进度」；
    2. 关弹窗时若后台还在挂，**继续轮询**（停了页面里那份状态会永远停在「进行中」，
       按钮跟着一直是灰的，只能刷新页面）；
    3. 进度那一屏的「取消挂载」要真的取消，不是单纯关弹窗。
    """
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert "btn.disabled = busy ? false : !d.ready;" in html
    assert "pubViewProgress" in html
    assert "const running = ((publicState || {}).job || {}).state === 'running';" in html
    assert "? cancelPublicMount() : closePublicModal()" in html


def test_panel_shows_the_mount_log_inside_the_modal():
    """挂载弹窗里也要有实时日志（和「日志 → 公网挂载」同一批输出）。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert 'id="publicJobLog"' in html and "publicLogHint" in html
    assert "function renderPublicJobLog" in html
    assert "(job && job.log) || []" in html
    assert "box.scrollTop = box.scrollHeight" in html            # 跟着滚到最新一行
    assert "state === 'error'" in html                           # 失败时也把日志留着


def test_cloudflared_never_gets_a_visible_window():
    """cloudflared 一律以无可见窗口的形式跑。

    Windows 上：一次性命令不给它控制台（CREATE_NO_WINDOW）、常驻隧道用
    DETACHED_PROCESS（照样没有窗口，而且面板重启不会把它带走）；两种都再叠一层
    STARTUPINFO 的 SW_HIDE 兜底。POSIX 上不弹窗是天然行为。
    """
    flags = cf._hidden_flags()
    detached = cf._hidden_flags(detached=True)
    if os.name == "nt":
        assert flags["creationflags"] == cf.subprocess.CREATE_NO_WINDOW
        assert detached["creationflags"] & cf.subprocess.DETACHED_PROCESS
        assert detached["creationflags"] & cf.subprocess.CREATE_NEW_PROCESS_GROUP
        assert flags["startupinfo"].wShowWindow == cf.subprocess.SW_HIDE
        assert detached["startupinfo"].wShowWindow == cf.subprocess.SW_HIDE
        assert flags["startupinfo"].dwFlags & cf.subprocess.STARTF_USESHOWWINDOW
    else:
        assert flags == {}
        assert detached == {"start_new_session": True}


def test_tunnel_log_gets_a_timezone_header(cfg_file, tmp_path, monkeypatch):
    """那份日志的行首是**世界时**（cloudflared 的规矩）：文件开头写一句说明。

    不写的话用户拿它跟自己的钟一对，会以为时间差了 8 小时、日志坏了。说明只在文件还空着
    的时候写一次，之后一直往后追加。
    """
    logs = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_DIR", str(logs))
    log = logs / "cloudflared.log"
    monkeypatch.setattr(paths, "CLOUDFLARED_LOG", str(log))

    cf._append_tunnel_log("[panel]", "第一行")
    text = log.read_text(encoding="utf-8")
    assert "世界时" in text.splitlines()[0]
    assert "第一行" in text

    cf._append_tunnel_log("[panel]", "第二行")
    assert log.read_text(encoding="utf-8").count("世界时") == 1     # 不重复写


def test_one_shot_commands_are_recorded_in_the_tunnel_log(cfg_file, monkeypatch, tmp_path):
    """面板跑的那些命令（建隧道 / 建解析 / 列隧道）也要留痕：事后就看它了。"""
    logs = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_DIR", str(logs))
    log = str(logs / "cloudflared.log")
    monkeypatch.setattr(paths, "CLOUDFLARED_LOG", log)
    monkeypatch.setattr(cf, "binary_path", lambda: "cloudflared-fake")

    class _Proc:
        returncode = 0
        stdout = "Created tunnel doukeep with id 00000000-0000-0000-0000-000000000009\n"
        stderr = ""

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **kw: _Proc())
    code, out = cf._run(["tunnel", "create", "doukeep"])

    assert code == 0 and "Created tunnel" in out
    written = open(log, encoding="utf-8").read()
    assert "cloudflared tunnel create doukeep" in written
    assert "[panel]" in written and "Created tunnel" in written


def test_bad_api_resolution_is_flagged(cfg_file, monkeypatch):
    """域名被解析到 Cloudflare 之外时要点出来（用户再怎么点授权都没用）。"""
    monkeypatch.setattr(cf.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("182.16.62.14", 443)),
        (2, 1, 6, "", ("104.19.192.29", 443)),
    ])
    assert cf.bad_api_addresses() == ["182.16.62.14"]


def test_clean_api_resolution_is_not_flagged(cfg_file, monkeypatch):
    monkeypatch.setattr(cf.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("104.19.192.29", 443)),
        (10, 1, 6, "", ("2606:4700:300a::6813:c0b1", 443, 0, 0)),
    ])
    assert cf.bad_api_addresses() == []


def test_auth_failure_reports_what_cloudflared_said(cfg_file, fake_tunnel, monkeypatch):
    """cloudflared 自己在授权中途退出 → 报 auth_failed，并把它的原话带上。"""
    lan_on_with_password(cfg_file)
    monkeypatch.setattr(cf, "logged_in", lambda: False)
    monkeypatch.setattr(cf, "bad_api_addresses", lambda host="api.cloudflare.com": [])

    class DeadSession:
        url = "https://dash.cloudflare.com/argotunnel?aud=x"
        finished = True
        output = ["ERR Failed to login: dial tcp 182.16.62.14:443: connectex: refused"]

        def start(self):
            pass

        def wait(self, timeout=0, cancelled=None):
            return False            # 进程退出、没拿到凭据

        def stop(self):
            pass

        def tail(self, lines=6):
            return "\n".join(self.output[-lines:])

    monkeypatch.setattr(cf, "LoginSession", DeadSession)
    snapshot = _run_flow()

    assert snapshot["state"] == "error"
    assert snapshot["error"] == "auth_failed"
    assert "connectex: refused" in snapshot["error_detail"]
    assert any("失败：auth_failed" in line for line in snapshot["log"])


def test_hijacked_api_resolution_is_reported_as_such(cfg_file, fake_tunnel, monkeypatch):
    """域名被解析坏的那次，错误码要是 auth_dns（面板据此给「换 DNS」的人话提示）。"""
    lan_on_with_password(cfg_file)
    monkeypatch.setattr(cf, "logged_in", lambda: False)
    monkeypatch.setattr(cf, "bad_api_addresses", lambda host="api.cloudflare.com": ["182.16.62.14"])

    class DeadSession:
        url = "https://dash.cloudflare.com/argotunnel?aud=x"    # 有链接就不用在等它上面耗时间
        finished = True
        output = ["ERR Failed to login"]

        def start(self):
            pass

        def wait(self, timeout=0, cancelled=None):
            return False

        def stop(self):
            pass

        def tail(self, lines=6):
            return "\n".join(self.output[-lines:])

    monkeypatch.setattr(cf, "LoginSession", DeadSession)
    snapshot = _run_flow()

    assert snapshot["error"] == "auth_dns"
    assert any("被解析到：182.16.62.14" in line for line in snapshot["log"])


def test_credentials_live_in_the_project_folder(cfg_file, tmp_path, monkeypatch):
    """凭据默认放项目目录 —— 有些机器按程序路径挡住了 cloudflared 往 ~/.cloudflared 写。"""
    monkeypatch.setattr(cf, "CRED_DIR", str(tmp_path / "legacy"))
    project_cert = os.path.join(paths.CLOUDFLARED_DIR, "cert.pem")

    # 都还没有：就用项目目录（登录会写这里）
    assert cf.cert_path() == project_cert
    assert cf.logged_in() is False

    # 空文件不算登录（cloudflared 写了一半就断了的情况）
    os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
    open(project_cert, "wb").close()
    assert cf.logged_in() is False

    # 写进内容就算登录，而且环境变量指的就是它
    with open(project_cert, "w", encoding="utf-8") as handle:
        handle.write("-----BEGIN CERTIFICATE-----")
    assert cf.cert_path() == project_cert
    assert cf.logged_in() is True
    assert cf._env()["TUNNEL_ORIGIN_CERT"] == project_cert


def test_legacy_home_certificate_still_counts(cfg_file, tmp_path, monkeypatch):
    """以前手工跑过 cloudflared 的机器，~/.cloudflared/cert.pem 继续认，不用重新授权。"""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(cf, "CRED_DIR", str(legacy))
    (legacy / "cert.pem").write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")

    assert cf.cert_path() == str(legacy / "cert.pem")
    assert cf.logged_in() is True
    assert cf._env()["TUNNEL_ORIGIN_CERT"] == str(legacy / "cert.pem")


def test_every_cloudflared_call_gets_the_cert_variable(cfg_file, monkeypatch):
    """login / 一次性命令 / 常驻隧道三种启动都要带上 TUNNEL_ORIGIN_CERT。"""
    seen = {}

    def fake_run(args, **kwargs):
        seen["run"] = kwargs

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    class _Proc:
        pid = 123
        stdout = None            # 假的 Popen：没有管道，读输出那个线程会直接收工

    def fake_popen(args, **kwargs):
        seen.setdefault("popen", []).append(kwargs)
        return _Proc()

    monkeypatch.setattr(cf, "binary_path", lambda: "cloudflared-fake")
    monkeypatch.setattr(cf, "_can_write_with", lambda runner, folder: True)
    monkeypatch.setattr(cf.subprocess, "run", fake_run)
    monkeypatch.setattr(cf.subprocess, "Popen", fake_popen)

    cf._run(["tunnel", "list"])
    cf.LoginSession().start()
    cf.start_tunnel("doukeep")

    assert seen["run"]["env"]["TUNNEL_ORIGIN_CERT"] == cf.cert_path()
    assert all(kw["env"]["TUNNEL_ORIGIN_CERT"] == cf.cert_path() for kw in seen["popen"])


def test_tunnel_config_points_credentials_next_to_the_certificate(cfg_file, tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "CRED_DIR", str(tmp_path / "legacy"))
    config_path = cf.write_config("00000000-0000-0000-0000-000000000001", "example.com")

    text = open(config_path, encoding="utf-8").read()
    expected = os.path.join(paths.CLOUDFLARED_DIR, "00000000-0000-0000-0000-000000000001.json")
    assert f"credentials-file: {expected}" in text


def test_tunnel_config_uses_http2(cfg_file, tmp_path, monkeypatch):
    """固定走 http2（TCP 443）。

    默认的 QUIC 走 UDP 7844，有的网络拦 UDP 或 IPv6 出不去，日志里就是一片
    ``handshake did not complete in time``，四条连接掉到只剩一条。
    """
    monkeypatch.setattr(cf, "CRED_DIR", str(tmp_path / "legacy"))
    config_path = cf.write_config("00000000-0000-0000-0000-000000000001", "example.com")

    text = open(config_path, encoding="utf-8").read()
    assert f"protocol: {cf.PROTOCOL}" in text
    assert cf.PROTOCOL == "http2"


def test_login_falls_back_to_a_temp_copy_when_writing_is_blocked(cfg_file, tmp_path, monkeypatch):
    """有的机器按程序路径挡住写入：确认原地写不了时，登录要改用一份临时副本（用完删掉）。"""
    monkeypatch.setattr(cf, "binary_path", lambda: sys.executable)
    monkeypatch.setattr(cf, "CRED_DIR", str(tmp_path / "legacy"))
    monkeypatch.setattr(cf, "_can_write_with", lambda runner, folder: False)

    session = cf.LoginSession()
    runner = session._runner()

    assert runner != sys.executable and os.path.isfile(runner)
    assert tmp_path not in pathlib.Path(runner).parents          # 在临时目录里
    session.stop()                                               # 收拾掉
    assert not os.path.exists(os.path.dirname(runner))


def test_login_uses_the_project_binary_when_writing_works(cfg_file, tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "binary_path", lambda: sys.executable)
    monkeypatch.setattr(cf, "CRED_DIR", str(tmp_path / "legacy"))
    monkeypatch.setattr(cf, "_can_write_with", lambda runner, folder: True)

    assert cf.LoginSession()._runner() == sys.executable


def test_login_falls_back_only_when_the_default_folder_is_blocked(cfg_file, tmp_path, monkeypatch):
    """判据是"能不能写默认凭据目录"：写得进去就不折腾，写不进才换副本。"""
    calls = []

    def probe(runner, folder):
        calls.append(folder)
        return folder == paths.CLOUDFLARED_DIR     # 只有项目目录写得进去

    monkeypatch.setattr(cf, "binary_path", lambda: sys.executable)
    monkeypatch.setattr(cf, "CRED_DIR", str(tmp_path / "legacy"))
    monkeypatch.setattr(cf, "_can_write_with", probe)

    session = cf.LoginSession()
    runner = session._runner()

    assert calls == [str(tmp_path / "legacy")]      # 只探默认位置，不浪费一次探测
    assert runner != sys.executable                 # 默认位置写不了 → 换副本
    session.stop()
    assert not os.path.exists(os.path.dirname(runner))


def test_legacy_certificate_is_moved_into_the_project(cfg_file, tmp_path, monkeypatch):
    """万一 cloudflared 还是写到了 ~/.cloudflared，也要搬进项目目录，后面命令才从一处读。"""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(cf, "CRED_DIR", str(legacy))
    (legacy / "cert.pem").write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")

    cf._adopt_legacy_cert()

    project = pathlib.Path(paths.CLOUDFLARED_DIR) / "cert.pem"
    assert project.read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")
    assert not (legacy / "cert.pem").exists()


def test_panel_offers_a_copyable_authorization_link():
    """授权那一步要把链接铺成能选中的文字 —— 自动开浏览器有时会被权限/弹窗拦下。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert 'id="publicAuthUrlText"' in html
    assert "copyPublicAuthUrl" in html
    assert "pubAuthUrlWaiting" in html
    assert "提升的权限" in html          # 浏览器提示权限不一致时怎么办，界面上要有一句


def test_elevated_service_is_reported_and_hinted(cfg_file):
    """服务以管理员身份跑时要能看出来（面板据此提前提示浏览器的权限提示）。"""
    assert isinstance(cf.is_elevated(), bool)
    assert isinstance(cf.mount_status()["elevated"], bool)
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert "pubAuthNoteElevated" in html and "d.elevated" in html


def test_disable_endpoint_keeps_the_domain(client, cfg_file, fake_tunnel):
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    payload = client.post("/panel/api/public/disable").json()["payload"]
    assert payload["enabled"] is False
    assert payload["domain"] == "example.com"


def test_remove_endpoint_clears_everything_local(client, cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    monkeypatch.setattr(cf, "delete_tunnel", lambda name: True)
    monkeypatch.setattr(cf, "delete_dns", lambda name, domain: True)
    body = client.post("/panel/api/public/remove").json()
    assert body["removed_tunnel"] is True and body["removed_dns"] is True
    assert body["payload"]["domain"] == ""
    assert access.public_enabled() is False


def test_remove_endpoint_says_so_when_dns_stays_behind(client, cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    monkeypatch.setattr(cf, "delete_tunnel", lambda name: True)
    monkeypatch.setattr(cf, "delete_dns", lambda name, domain: False)
    body = client.post("/panel/api/public/remove").json()
    assert body["removed_tunnel"] is True and body["removed_dns"] is False


def test_developer_mode_off_also_closes_public_access(client, cfg_file, fake_tunnel):
    """公网访问挂在开发者模式下面：关掉开发者模式就顺手把公网入口收起来（域名留着）。"""
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    body = client.post("/panel/api/config/developer-mode", json={"enabled": False}).json()
    assert body["public_on"] is False
    assert body["public_was_on"] is True
    assert access.public_enabled() is False
    assert access.public_domain() == "example.com"


def test_turning_off_lan_also_closes_public_access(client, cfg_file, fake_tunnel, monkeypatch):
    """公网访问搭在局域网访问上：关掉局域网就把公网入口一起收起来，域名留着。"""
    import backend.control_panel as cp

    monkeypatch.setattr(cp, "_server_cli_address", lambda: ("127.0.0.1", 8000))
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")

    body = client.post("/panel/api/lan", json={"enabled": False}).json()

    assert body["public_closed"] is True
    assert body["payload"]["public_on"] is False
    assert fake_tunnel["stopped"] >= 1                 # 隧道进程被停了
    assert access.public_enabled() is False
    assert access.public_domain() == "example.com"     # 域名照旧留着


def test_lan_status_tells_the_panel_whether_public_is_on(client, cfg_file, fake_tunnel):
    """面板关开关前要知道公网是不是开着（好先弹那句确认）。"""
    lan_on_with_password(cfg_file)
    assert client.get("/panel/api/lan").json()["public_on"] is False
    access.set_public_access(enabled=True, domain="example.com")
    assert client.get("/panel/api/lan").json()["public_on"] is True


def test_panel_asks_before_lan_off_closes_public_access():
    """面板上的接线：公网开着时关局域网，先弹确认框把后果说清楚。"""
    html = open(PANEL_HTML, encoding="utf-8").read()
    assert "lanAccess.public_on" in html
    assert "lanOffClosesPublic" in html and "lanOffClosesPublicTitle" in html
    assert "lanPublicClosed" in html
    assert "public_closed" in html


# ── 删解析记录：走 Cloudflare 接口，并且不许被「假成功」骗过 ──

def _cert_pem_text(zone: str = "zone-1", token: str = "tok-1", body: str | None = None) -> str:
    """造一份 cert.pem（ARGO TUNNEL TOKEN 里是 base64 的 JSON）。"""
    payload = body if body is not None else json.dumps(
        {"zoneID": zone, "accountID": "acct-1", "apiToken": token})
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return ("-----BEGIN ARGO TUNNEL TOKEN-----\n"
            + encoded + "\n-----END ARGO TUNNEL TOKEN-----\n")


def test_argo_credentials_reads_the_token_from_cert_pem(tmp_path, monkeypatch):
    cert = tmp_path / "cert.pem"
    cert.write_text(_cert_pem_text(), encoding="utf-8")
    monkeypatch.setattr(cf, "cert_path", lambda: str(cert))

    creds = cf.argo_credentials()

    assert creds["zoneID"] == "zone-1" and creds["apiToken"] == "tok-1"


def test_argo_credentials_gives_up_on_a_broken_file(tmp_path, monkeypatch):
    cert = tmp_path / "cert.pem"
    cert.write_text(_cert_pem_text(body="hello"), encoding="utf-8")   # 解出来不是 JSON
    monkeypatch.setattr(cf, "cert_path", lambda: str(cert))

    assert cf.argo_credentials() == {}


def _fake_api(records, calls, *, delete_ok=True):
    """假的 Cloudflare 接口：查询返回固定记录，删除按 ``delete_ok``。"""
    def fake(path, *, method="GET", timeout=30):
        calls.append((method, path))
        if method == "DELETE":
            return delete_ok, {"success": delete_ok, "result": {}}
        return True, {"success": True, "result": records}
    return fake


def test_delete_dns_removes_the_tunnel_record_through_the_api(cfg_file, monkeypatch):
    """「彻底移除」要真的删掉那条解析 —— 走接口，不指望 cloudflared 的命令行。"""
    monkeypatch.setattr(cf, "argo_credentials",
                        lambda: {"zoneID": "zone-1", "apiToken": "tok-1"})
    calls = []
    monkeypatch.setattr(cf, "api_call", _fake_api(
        [{"id": "rec-1", "name": "chat.example.com", "type": "CNAME",
          "content": "00000000-0000-0000-0000-000000000001.cfargotunnel.com"}], calls))
    monkeypatch.setattr(cf, "_run", lambda *a, **k: pytest.fail("不该退回命令行"))

    assert cf.delete_dns("doukeep", "chat.example.com") is True
    assert ("DELETE", "/zones/zone-1/dns_records/rec-1") in calls


def test_delete_dns_keeps_its_hands_off_a_record_that_is_not_a_tunnel(cfg_file, monkeypatch):
    """域名上那条记录不是隧道（用户自己指的别处）时：绝不删，但也不用喊「没删掉」。"""
    monkeypatch.setattr(cf, "argo_credentials",
                        lambda: {"zoneID": "zone-1", "apiToken": "tok-1"})
    calls = []
    monkeypatch.setattr(cf, "api_call", _fake_api(
        [{"id": "rec-9", "name": "chat.example.com", "type": "A",
          "content": "203.0.113.10"}], calls))
    monkeypatch.setattr(cf, "_run", lambda *a, **k: pytest.fail("不该退回命令行"))

    assert cf.delete_dns("doukeep", "chat.example.com") is True
    assert all(method != "DELETE" for method, _ in calls)


def test_delete_dns_is_not_fooled_by_a_fake_success(cfg_file, monkeypatch):
    """cloudflared 对不认识的开关打完 Incorrect Usage **还是退出 0**。

    这正是老代码的病根：以为删掉了、其实没删，面板也就不提醒用户。这里必须判成失败。
    """
    monkeypatch.setattr(cf, "argo_credentials", lambda: {})
    monkeypatch.setattr(cf, "_run", lambda *a, **k: (
        0, "Incorrect Usage: flag provided but not defined: -delete"))

    assert cf.delete_dns("doukeep", "chat.example.com") is False


def test_delete_dns_still_trusts_an_honest_cli_success(cfg_file, monkeypatch):
    monkeypatch.setattr(cf, "argo_credentials", lambda: {})
    monkeypatch.setattr(cf, "_run", lambda *a, **k: (0, "Deleted record chat.example.com"))

    assert cf.delete_dns("doukeep", "chat.example.com") is True


# ── 「把解析改到这台电脑」：这一路才允许覆盖已有记录 ──

def test_overwrite_endpoint_clears_the_dns_exists_warning(client, cfg_file, fake_tunnel, monkeypatch):
    """挂载时撞上「记录已存在」→ 点一下按钮，覆盖过去并把那条提示收掉。"""
    lan_on_with_password(cfg_file)

    def fake_route(name, domain, *, overwrite=False):
        return (True, "ok") if overwrite else (False, "record with that name already exists")

    monkeypatch.setattr(cf, "route_dns", fake_route)
    assert _run_flow()["warning"] == "dns_exists"

    body = client.post("/panel/api/public/dns/overwrite").json()

    assert body["ok"] is True
    assert body["payload"]["job"]["warning"] == ""


def test_overwrite_endpoint_reports_a_failure(client, cfg_file, fake_tunnel, monkeypatch):
    lan_on_with_password(cfg_file)
    access.set_public_access(enabled=True, domain="example.com")
    monkeypatch.setattr(cf, "route_dns",
                        lambda name, domain, *, overwrite=False:
                        (False, "Failed to update record example.com"))

    body = client.post("/panel/api/public/dns/overwrite").json()

    assert body["ok"] is False and body["error"] == "overwrite_failed"
    assert "Failed to update" in body["detail"]


def test_overwrite_endpoint_needs_a_mounted_domain(client, cfg_file, fake_tunnel):
    lan_on_with_password(cfg_file)

    body = client.post("/panel/api/public/dns/overwrite").json()

    assert body["ok"] is False and body["error"] == "no_domain"


def test_panel_offers_the_point_dns_here_button():
    """面板要有这个出口：设置页一个按钮、挂载结果页一个，都接到新接口上。"""
    html = open(PANEL_HTML, encoding="utf-8").read()

    assert "publicFixDnsBtn" in html and "publicResultFixBtn" in html
    assert "fixPublicDns" in html
    assert "/panel/api/public/dns/overwrite" in html
    assert html.count('data-i18n="pubFixDnsBtn"') == 2


def test_route_dns_puts_the_overwrite_flag_before_the_positional_arguments(cfg_file, monkeypatch):
    """``--overwrite-dns`` 必须排在隧道名/域名**前面**。

    cloudflared 只认位置参数前面的开关；放到后面它会报
    「This command expects the format "cloudflared tunnel route dns <tunnel name/id> <hostname>"」
    并且失败 —— 2026-10-06 那次「把解析改到这台电脑」就是栽在参数顺序上。
    """
    seen = []
    monkeypatch.setattr(cf, "_run", lambda args, timeout=120: seen.append(args) or (0, "ok"))

    assert cf.route_dns("doukeep", "chat.example.com")[0] is True
    assert cf.route_dns("doukeep", "chat.example.com", overwrite=True)[0] is True

    assert seen[0] == ["tunnel", "route", "dns", "doukeep", "chat.example.com"]
    assert seen[1] == ["tunnel", "route", "dns", "--overwrite-dns", "doukeep", "chat.example.com"]

