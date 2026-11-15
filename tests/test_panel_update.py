"""更新检测：远端版本号与逐版本说明都直接读 GitHub 的 Release。

这里不打网络：仓库预检和 Release 列表都注入假数据。验证的是「版本号怎么挑、
哪些版本算新版本、更新内容取自哪里」，以及草稿 / 预发布不会被当成新版本。
"""
import asyncio
import json

import pytest

from backend import control_panel as cp
from common import paths, version

#: 真的「读 Release 列表」函数：默认夹具会把它换成空列表，想测这条路的用例
#: 得先把它放回去。
_REAL_FETCH_RELEASES = cp._fetch_remote_releases

#: 假装的本地版本号（测试里把 VERSION 换成它，免得跟着真实版本跑）
LOCAL_VERSION = "1.0.7"


def _raw_release(tag, body="", *, draft=False, prerelease=False, url=None,
                 date="2026-10-02"):
    """GitHub /releases 接口里的一条原始记录。"""
    return {
        "tag_name": tag,
        "name": "v" + tag,
        "body": body,
        "html_url": (url if url is not None
                     else "https://example.test/releases/tag/" + tag),
        "published_at": date + "T12:00:00Z",
        "draft": draft,
        "prerelease": prerelease,
    }


def _release(version_text, body="", url="", date="2026-10-02"):
    """后端整理好之后的样子（_fetch_remote_releases 的产出）。"""
    return {"version": version_text, "title": "v" + version_text,
            "body": body, "url": url, "date": date}


@pytest.fixture
def local_version(monkeypatch, tmp_path):
    """固定本地版本号，并把 Token / 面板配置指到临时目录。"""
    monkeypatch.setattr(version, "VERSION", LOCAL_VERSION)
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp._github_auth, "TOKEN_PATH", str(tmp_path / "github_token"))
    config_path = tmp_path / "panel_config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", str(config_path))
    return LOCAL_VERSION


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """网络封死：预检放行，Release 列表默认空（各用例自己给）。"""
    async def reachable():
        return True

    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_fetch_remote_releases", lambda: [])
    monkeypatch.setattr(cp, "_update_cache", None)


def _serve(monkeypatch, releases):
    monkeypatch.setattr(cp, "_fetch_remote_releases", lambda: list(releases))


# ── 一条 Release 怎么读 ───────────────────────────────────────────────────

def test_release_entry_skips_draft_prerelease_and_non_version_tags():
    assert cp._release_entry(_raw_release("1.2.0", draft=True)) is None
    assert cp._release_entry(_raw_release("1.2.0", prerelease=True)) is None
    assert cp._release_entry(_raw_release("backup")) is None
    assert cp._release_entry("这不是一条记录") is None


def test_release_entry_keeps_the_published_notes():
    entry = cp._release_entry(_raw_release("1.2.0", "## 功能新增\n1. 甲", date="2026-10-03"))

    assert entry["version"] == "1.2.0"
    assert entry["body"] == "## 功能新增\n1. 甲"
    assert entry["date"] == "2026-10-03"
    assert entry["url"].endswith("/1.2.0")


def test_fetch_remote_releases_sorts_newest_first(monkeypatch):
    monkeypatch.setattr(cp, "_fetch_remote_releases", _REAL_FETCH_RELEASES)
    monkeypatch.setattr(cp, "_github_json", lambda url, **kwargs: [
        _raw_release("1.0.9"),
        _raw_release("1.10.0"),
        _raw_release("1.9.0", draft=True),
        _raw_release("nightly"),
    ])

    releases = cp._fetch_remote_releases()

    assert [item["version"] for item in releases] == ["1.10.0", "1.0.9"]


def test_fetch_remote_releases_rejects_a_broken_response(monkeypatch):
    monkeypatch.setattr(cp, "_fetch_remote_releases", _REAL_FETCH_RELEASES)
    monkeypatch.setattr(cp, "_github_json", lambda url, **kwargs: {"message": "nope"})

    with pytest.raises(cp._UpdateCheckError) as excinfo:
        cp._fetch_remote_releases()

    assert str(excinfo.value) == "bad_response"


# ── 检测结果 ─────────────────────────────────────────────────────────────

def test_newer_releases_become_the_update_list(monkeypatch, local_version):
    _serve(monkeypatch, [
        _release("1.0.9", "## 问题修复\n1. 旧的修复", url="https://example.test/1.0.9"),
        _release("1.1.0", "## 功能新增\n1. 新的功能", url="https://example.test/1.1.0"),
        _release("1.0.7", "## 问题修复\n1. 当前这一版"),
    ])

    result = asyncio.run(cp._collect_update())

    assert result["update_available"] is True
    assert result["remote_version"] == "1.1.0"
    assert [item["version"] for item in result["versions"]] == ["1.1.0", "1.0.9"]
    assert result["versions"][0]["body"].startswith("## 功能新增")
    assert result["release_url"] == "https://example.test/1.1.0"
    assert "behind" not in result
    assert "compare_url" not in result


def test_up_to_date_has_no_versions(monkeypatch, local_version):
    _serve(monkeypatch, [_release("1.0.7", url="https://example.test/1.0.7")])

    result = asyncio.run(cp._collect_update())

    assert result["update_available"] is False
    assert result["versions"] == []
    assert result["release_url"] == version.REPOSITORY_URL + "/releases"


def test_missing_release_page_falls_back_to_the_release_list(monkeypatch, local_version):
    _serve(monkeypatch, [_release("1.0.8")])

    result = asyncio.run(cp._collect_update())

    assert result["update_available"] is True
    assert result["release_url"] == version.REPOSITORY_URL + "/releases"


def test_no_release_at_all_is_reported(monkeypatch, local_version):
    _serve(monkeypatch, [])

    with pytest.raises(cp._UpdateCheckError) as excinfo:
        asyncio.run(cp._collect_update())

    assert str(excinfo.value) == "no_remote_version"
    assert "Release" in cp._update_error_message("no_remote_version")


def test_network_failure_keeps_its_own_code(monkeypatch, local_version):
    def boom():
        raise cp._UpdateCheckError("network")

    monkeypatch.setattr(cp, "_fetch_remote_releases", boom)

    with pytest.raises(cp._UpdateCheckError) as excinfo:
        asyncio.run(cp._collect_update())

    assert cp._update_error_code(excinfo.value) == "network"


def test_repeated_checks_use_the_short_cache(monkeypatch, local_version):
    calls = []

    def fetch():
        calls.append(1)
        return [_release("1.0.7")]

    monkeypatch.setattr(cp, "_fetch_remote_releases", fetch)

    asyncio.run(cp._collect_update())
    asyncio.run(cp._collect_update())
    asyncio.run(cp._collect_update(refresh=True))

    assert len(calls) == 2, "30 秒内的重复点击不该再问一次，refresh 才强制重查"


# ── 通知里的逐版本说明 ───────────────────────────────────────────────────

def test_release_summary_strips_markdown():
    body = "## 问题修复\n\\-\n\n## 功能新增\n1. 甲\n2. 乙\n3. 丙\n4. 丁"

    assert cp._release_summary(body) == "甲；乙；丙"
    assert cp._release_summary("") == ""


def test_update_versions_text_uses_the_release_body():
    text = cp._update_versions_text({"versions": [
        _release("1.1.0", "## 功能新增\n1. 第二件事\n2. 第一件事"),
        _release("1.0.9", "## 问题修复\n1. 更早的事"),
    ]})

    assert text.splitlines() == ["- v1.1.0：第二件事；第一件事", "- v1.0.9：更早的事"]


def test_update_versions_text_without_a_body_only_names_the_version():
    text = cp._update_versions_text({"versions": [_release("1.1.0")]})

    assert text == "- v1.1.0"
