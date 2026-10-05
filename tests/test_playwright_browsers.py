"""Playwright 浏览器内核的核对规则（common/playwright_browsers.py）。

为什么值得单独钉住：pip 装的只是 playwright 这个 Python 包，浏览器本体要另外下载；
playwright 升级之后要用的内核版本号（写在目录名里）就变了，旧内核随即作废 —— 那时采集、
导入 Cookie、聊天长图都会报「Executable doesn't exist at ...\\chromium_headless_shell-1243\\...」。
所以启动脚本靠这里判断「缺不缺、缺哪个」，判断错了要么白下一遍，要么就是那条报错。
"""
import json

import pytest

from common import playwright_browsers as browsers

CHROMIUM = {"name": "chromium", "revision": "1234"}
HEADLESS = {"name": "chromium-headless-shell", "revision": "1234"}


def _write_manifest(tmp_path, entries):
    path = tmp_path / "browsers.json"
    path.write_text(json.dumps({"browsers": entries}), encoding="utf-8")
    return path


def _install(tmp_path, name, revision, exe_name):
    """造一个「装好了」的内核目录（目录 + 里面的可执行文件）。"""
    inner = tmp_path / browsers.directory_name(name, revision) / exe_name.rsplit("/", 1)[0]
    inner.mkdir(parents=True)
    (inner / exe_name.rsplit("/", 1)[1]).write_text("x", encoding="utf-8")


@pytest.fixture
def fake_install_root(tmp_path, monkeypatch):
    """把「内核装在哪」和「包要求哪些」都换成假的，不动本机真装的那份。"""
    monkeypatch.setattr(browsers, "browsers_root", lambda: tmp_path)
    monkeypatch.setattr(browsers, "browsers_json_path",
                        lambda: _write_manifest(tmp_path, [CHROMIUM, HEADLESS,
                                                           {"name": "firefox", "revision": "1538"}]))
    return tmp_path


def test_directory_name_follows_playwright():
    assert browsers.directory_name("chromium", "1234") == "chromium-1234"
    assert browsers.directory_name("chromium-headless-shell", "1234") == "chromium_headless_shell-1234"


def test_only_the_browsers_we_use_are_required(fake_install_root):
    """装了 firefox 也没用：我们只用 chromium 和它的无头内核。"""
    names = [name for name, _revision in browsers.required_browsers()]
    assert names == ["chromium", "chromium-headless-shell"]


def test_both_missing_when_nothing_is_installed(fake_install_root):
    assert browsers.missing_browsers() == ["chromium-1234", "chromium_headless_shell-1234"]
    assert browsers.installed_browsers() == []
    assert "自动安装" in browsers.summarize()


def test_ready_when_both_are_installed(fake_install_root):
    _install(fake_install_root, "chromium", "1234", "chrome-win64/chrome.exe")
    _install(fake_install_root, "chromium-headless-shell", "1234",
             "chrome-headless-shell-win64/chrome-headless-shell.exe")
    assert browsers.missing_browsers() == []
    assert browsers.installed_browsers() == ["chromium-1234", "chromium_headless_shell-1234"]
    assert "1234" in browsers.summarize()


def test_only_the_headless_shell_is_reported_when_it_is_the_one_missing(fake_install_root):
    """用户报的那条报错就是这个状态：普通内核在、无头内核没下（版本升级后最容易这样）。"""
    _install(fake_install_root, "chromium", "1234", "chrome-win64/chrome.exe")
    assert browsers.missing_browsers() == ["chromium_headless_shell-1234"]


def test_a_half_downloaded_directory_counts_as_missing(fake_install_root):
    """目录在、里面没有可执行文件（下到一半 / 被清理工具删过）：仍然算缺，要补下。"""
    empty = fake_install_root / browsers.directory_name("chromium", "1234") / "chrome-win64"
    empty.mkdir(parents=True)
    assert "chromium-1234" in browsers.missing_browsers()


def test_macos_style_app_bundle_counts_as_installed(fake_install_root):
    """macOS 里放的是 .app 包，别因为名字不是 chrome* 就每年重下一遍。"""
    inner = fake_install_root / browsers.directory_name("chromium", "1234") / "chrome-mac"
    inner.mkdir(parents=True)
    (inner / "Google Chrome for Testing.app").mkdir()
    assert "chromium-1234" not in browsers.missing_browsers()


def test_unknown_manifest_is_not_treated_as_missing(tmp_path, monkeypatch):
    """读不到包里的清单（没装 playwright）时不能瞎报「缺」，也不能装作已经装好。"""
    monkeypatch.setattr(browsers, "browsers_root", lambda: tmp_path)
    monkeypatch.setattr(browsers, "browsers_json_path", lambda: None)
    assert browsers.required_browsers() == []
    assert browsers.missing_browsers() == []
    assert "自动安装" in browsers.summarize()


def test_broken_manifest_is_survived(tmp_path, monkeypatch):
    broken = tmp_path / "browsers.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(browsers, "browsers_json_path", lambda: broken)
    assert browsers.required_browsers() == []


def test_manual_hint_names_the_install_command():
    hint = browsers.manual_hint()
    assert "playwright install chromium" in hint
    assert "venv" in hint


# ── 启动脚本调用的那个补装脚本（tools/ensure_playwright_browser.py） ──────
def test_ensure_does_not_download_when_everything_is_there(fake_install_root, monkeypatch):
    from tools import ensure_playwright_browser as ensure

    _install(fake_install_root, "chromium", "1234", "chrome-win64/chrome.exe")
    _install(fake_install_root, "chromium-headless-shell", "1234",
             "chrome-headless-shell-win64/chrome-headless-shell.exe")
    monkeypatch.setattr(ensure, "playwright_installed", lambda: True)
    monkeypatch.setattr(ensure, "download", lambda host=None: pytest.fail("不该下载"))

    assert ensure.ensure() == 0


def test_ensure_check_only_never_touches_the_network(fake_install_root, monkeypatch):
    from tools import ensure_playwright_browser as ensure

    monkeypatch.setattr(ensure, "playwright_installed", lambda: True)
    monkeypatch.setattr(ensure, "download", lambda host=None: pytest.fail("--check-only 不该下载"))

    assert ensure.ensure(check_only=True) == 1


def test_ensure_downloads_from_the_mirror_first(fake_install_root, monkeypatch):
    """镜像源下载成功后就不再走官方 CDN（国内直连官方经常超时）。"""
    from tools import ensure_playwright_browser as ensure

    hosts = []

    def fake_download(host=None):
        hosts.append(host)
        _install(fake_install_root, "chromium", "1234", "chrome-win64/chrome.exe")
        _install(fake_install_root, "chromium-headless-shell", "1234",
                 "chrome-headless-shell-win64/chrome-headless-shell.exe")
        return True

    monkeypatch.setattr(ensure, "playwright_installed", lambda: True)
    monkeypatch.setattr(ensure, "download", fake_download)
    monkeypatch.delenv(ensure.DOWNLOAD_ENV, raising=False)

    assert ensure.ensure() == 0
    assert hosts == [ensure.MIRROR_HOST]


def test_ensure_falls_back_to_the_official_cdn(fake_install_root, monkeypatch):
    """镜像没成（或没下全）时，要再试一次官方源。"""
    from tools import ensure_playwright_browser as ensure

    hosts = []

    def fake_download(host=None):
        hosts.append(host)
        if host is None:                      # 官方这次成功
            _install(fake_install_root, "chromium", "1234", "chrome-win64/chrome.exe")
            _install(fake_install_root, "chromium-headless-shell", "1234",
                     "chrome-headless-shell-win64/chrome-headless-shell.exe")
            return True
        return False

    monkeypatch.setattr(ensure, "playwright_installed", lambda: True)
    monkeypatch.setattr(ensure, "download", fake_download)
    monkeypatch.delenv(ensure.DOWNLOAD_ENV, raising=False)

    assert ensure.ensure() == 0
    assert hosts == [ensure.MIRROR_HOST, None]


def test_ensure_respects_a_user_chosen_download_host(fake_install_root, monkeypatch):
    """用户自己设了 PLAYWRIGHT_DOWNLOAD_HOST 就只按他说的下，不覆盖。"""
    from tools import ensure_playwright_browser as ensure

    hosts = []
    monkeypatch.setattr(ensure, "playwright_installed", lambda: True)
    monkeypatch.setattr(ensure, "download", lambda host=None: hosts.append(host) or False)
    monkeypatch.setenv(ensure.DOWNLOAD_ENV, "https://my.mirror/playwright")

    assert ensure.ensure() == 1
    assert hosts == ["https://my.mirror/playwright"]


def test_ensure_tells_you_to_install_dependencies_first(monkeypatch):
    from tools import ensure_playwright_browser as ensure

    monkeypatch.setattr(ensure, "playwright_installed", lambda: False)
    assert ensure.ensure() == 1
