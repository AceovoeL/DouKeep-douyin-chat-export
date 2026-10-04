"""私有仓库凭据：只读 Token 的存取、校验与使用。

关键约束（这里都钉住）：
* Token 只落在 config/github_token，不进 panel_config.json，也不进代码仓库；
* 任何接口都不返回 Token 本身，只说「有没有、是谁」；
* **只有开发者模式开着时它才会被用到**：关着时 API 不带 Authorization 头、git 也拿不到
  凭据助手，但 Token 文件留着（重新打开开关就能接着用，不用重新粘贴）；
* Token 无效 / 没法读仓库时给出能对症的错误码，而不是笼统的「仓库不存在」。
"""
import io
import json
import urllib.error

import pytest

from backend import control_panel as cp
from common import github_auth, paths, version

TOKEN = "github_pat_" + "A" * 40


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    """把 Token 文件指到临时目录，别碰真实 config/。"""
    path = tmp_path / "github_token"
    monkeypatch.setattr(github_auth, "TOKEN_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def panel_config(tmp_path, monkeypatch):
    """把面板配置也指到临时文件，并默认**打开**开发者模式。

    这个文件里的用例验的是「Token 怎么被用」，而 Token 只在开发者模式开着时才参与
    检查更新；「关着时一律不用」那一条单独有用例（见下面「开发者模式关着时」一节）。
    """
    path = tmp_path / "panel_config.json"
    path.write_text(json.dumps({"developer_mode": True}), encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


def _set_developer_mode(panel_config, enabled: bool) -> None:
    panel_config.write_text(json.dumps({"developer_mode": bool(enabled)}), encoding="utf-8")


# ── 存取与格式 ──

def test_save_and_load_round_trip(token_file):
    github_auth.save(TOKEN)
    assert token_file.exists()
    assert github_auth.load() == TOKEN


def test_save_strips_quotes_and_whitespace(token_file):
    github_auth.save(f'  "{TOKEN}"\n')
    assert github_auth.load() == TOKEN


def test_clear_removes_file(token_file):
    github_auth.save(TOKEN)
    assert github_auth.clear() is True
    assert github_auth.load() == ""
    assert github_auth.clear() is False          # 已经不在了


def test_load_without_file_is_empty(token_file):
    assert github_auth.load() == ""


@pytest.mark.parametrize("bad", ["", "short", "with space" + "A" * 30, "line\nbreak" + "A" * 30, "A" * 300])
def test_implausible_tokens_rejected(bad):
    assert github_auth.is_plausible(bad) is False


def test_plausible_token_accepted():
    assert github_auth.is_plausible(TOKEN) is True
    assert github_auth.is_plausible("ghp_" + "b" * 36) is True


def test_token_file_is_not_the_panel_config(token_file):
    """别把 Token 混进 panel_config.json（那个文件会被整包读写、也可能被导出）。"""
    github_auth.save(TOKEN)
    assert "panel_config" not in github_auth.TOKEN_PATH
    assert github_auth.TOKEN_PATH.endswith("github_token")


# ── API 请求头 ──

class _FakeResponse:
    def __init__(self, payload, headers=None):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}          # 真实响应是 HTTPMessage，取 Link 的用法一样

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_github_json_adds_authorization_header(monkeypatch, token_file):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _FakeResponse({"login": "AceovoeL"})

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)
    github_auth.save(TOKEN)

    assert cp._github_json("https://api.github.com/user") == {"login": "AceovoeL"}
    assert seen["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_github_json_without_token_has_no_authorization(monkeypatch, token_file):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _FakeResponse({})

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)
    cp._github_json("https://api.github.com/user")
    assert "authorization" not in seen["headers"]


def test_github_read_returns_response_headers(monkeypatch, token_file):
    """响应头要跟着一起返回：远端 commit 总数是从分页 Link 头里读出来的。"""
    link = '<https://api.github.com/repositories/1/commits?sha=main&per_page=1&page=20>; rel="last"'

    def fake_urlopen(request, timeout=None):
        return _FakeResponse({}, {"Link": link})

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)

    body, head = cp._github_read("https://api.github.com/user")
    assert body == {}
    assert cp._last_page(head.get("Link")) == 20


def test_github_json_reports_invalid_token(monkeypatch, token_file):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(cp._UpdateCheckError) as excinfo:
        cp._github_json("https://api.github.com/user")
    assert str(excinfo.value) == "token_invalid"


# ── Token 接口 ──

def test_token_status_never_returns_the_token(monkeypatch, token_file):
    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_github_account", lambda token: "AceovoeL")

    payload = cp.asyncio.run(cp.update_token_status())

    assert payload["configured"] is True
    assert payload["login"] == "AceovoeL"
    assert TOKEN not in json.dumps(payload, ensure_ascii=False)


def test_update_info_never_returns_the_token(monkeypatch, token_file):
    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_github_account", lambda token: "AceovoeL")
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)

    payload = cp.asyncio.run(cp.update_info())

    assert payload["token"] == {"configured": True, "login": "AceovoeL"}
    assert TOKEN not in json.dumps(payload, ensure_ascii=False)


def test_token_save_rejects_short_token(token_file):
    response = cp.asyncio.run(cp.update_token_save(cp._GithubTokenRequest(token="abc")))
    assert response.status_code == 400
    assert "Token" in json.loads(response.body)["error"]


def test_token_save_rejects_unreadable_repo(monkeypatch, token_file):
    monkeypatch.setattr(cp, "_credential_scope", lambda token: ("forbidden", "AceovoeL"))
    response = cp.asyncio.run(cp.update_token_save(cp._GithubTokenRequest(token=TOKEN)))

    assert response.status_code == 400
    assert "AceovoeL" in json.loads(response.body)["error"]
    assert github_auth.load() == "", "校验不过就不该落盘"


def test_token_save_reports_invalid_token(monkeypatch, token_file):
    monkeypatch.setattr(cp, "_credential_scope", lambda token: ("invalid", ""))
    response = cp.asyncio.run(cp.update_token_save(cp._GithubTokenRequest(token=TOKEN)))

    payload = json.loads(response.body)
    assert response.status_code == 400
    assert payload["status"] == "invalid"
    assert "无效" in payload["error"]
    assert github_auth.load() == ""


def test_token_save_stores_and_invalidates_cache(monkeypatch, token_file):
    monkeypatch.setattr(cp, "_credential_scope", lambda token: ("ok", "AceovoeL"))
    monkeypatch.setattr(cp, "_update_cache", (0.0, {"stale": True}))

    response = cp.asyncio.run(cp.update_token_save(cp._GithubTokenRequest(token=TOKEN)))
    payload = json.loads(response.body) if hasattr(response, "body") else response

    assert payload["saved"] is True
    assert payload["login"] == "AceovoeL"
    assert github_auth.load() == TOKEN
    assert cp._update_cache is None, "换了凭据要清掉旧的检测结果"


def test_token_clear_removes_saved_token(token_file):
    github_auth.save(TOKEN)
    response = cp.asyncio.run(cp.update_token_clear())
    assert response["cleared"] is True
    assert github_auth.load() == ""


# ── 检测流程对凭据的反应 ──

def test_private_repo_without_token_reports_missing(monkeypatch, token_file):
    """没有 Token、仓库又读不到时，报「读不到仓库」并提示去设置凭据。"""
    async def unreachable():
        return False

    monkeypatch.setattr(cp, "_remote_reachable", unreachable)
    response = cp.asyncio.run(cp.update_check())

    payload = json.loads(response.body)
    assert response.status_code == 502
    assert payload["error_code"] == "repo_missing"

    # 面板上「没设置 Token」时能给出的另一条提示
    hint = json.loads(cp._raise_update_error("credentials_missing").body.decode())
    assert "Token" in hint["error"]


def test_token_present_but_no_repo_access(monkeypatch, token_file):
    async def unreachable():
        return False

    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_remote_reachable", unreachable)
    response = cp.asyncio.run(cp.update_check())

    payload = json.loads(response.body)
    assert payload["error_code"] == "repo_forbidden"
    assert "Token" in payload["error"]


def test_git_auth_failure_maps_to_credentials_missing(monkeypatch, token_file):
    """git fetch 说缺凭据、API 也用不了时，给出「需要凭据」而不是网络错误。"""
    async def reachable():
        return True

    def git_needs_auth(branch):
        raise cp._GitFetchAuthError("could not read Username")

    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", git_needs_auth)

    response = cp.asyncio.run(cp.update_check())
    payload = json.loads(response.body)

    assert response.status_code == 502
    assert payload["error_code"] == "credentials_missing"
    assert "Token" in payload["error"]


def test_public_repo_without_token_falls_back_to_api(monkeypatch, token_file):
    """公开仓库没填凭据时：本地 git 拿不到就改用**不带凭据**的 API，而不是报缺凭据。

    仓库公开之后谁都能匿名读 API，只有私有仓库才真的需要 Token；以前没 Token 时
    只走本地 git，git 一旦失败（目录里没有 .git、没装 git、Windows 上环境被换掉）
    就被笼统地说成「需要私有仓库凭据」。
    """
    calls: list = []

    async def reachable():
        return True

    def via_git(branch):
        calls.append("git")
        return None

    async def via_api():
        calls.append("api")
        return 1, [{"sha": "c" * 40,
                    "commit": {"message": "公开仓库的新提交", "author": {"date": "2026-10-02T00:00:00Z"}}}]

    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", via_git)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_api", via_api)
    monkeypatch.setattr(cp, "_order_oldest_first", lambda raw: list(raw))
    monkeypatch.setattr(cp._version, "local_commit_count", lambda: 8)
    monkeypatch.setattr(cp, "_update_cache", None)

    result = cp.asyncio.run(cp.update_check())

    assert result["update_available"] is True
    assert result["versions"][0]["subject"] == "公开仓库的新提交"
    assert calls == ["git", "api"], "git 走不通时才轮到匿名 API"


def test_git_needing_auth_does_not_try_the_anonymous_api(monkeypatch, token_file):
    """git 明确说要凭据（私有仓库）时，别再把匿名 API 试一遍 —— 白等一轮。"""
    async def reachable():
        return True

    def git_needs_auth(branch):
        raise cp._GitFetchAuthError("Authentication failed")

    def api_must_not_run():
        pytest.fail("git 已经说要凭据了，不该再试匿名 API")

    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", git_needs_auth)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_api", api_must_not_run)

    with pytest.raises(cp._UpdateCheckError) as excinfo:
        cp.asyncio.run(cp._fetch_ahead_best_source())

    assert str(excinfo.value) == "credentials_missing"


def test_api_is_used_first_when_token_present(monkeypatch, token_file):
    """有 Token 时优先走 API（私有仓库也能读，而且快）。"""
    calls: list = []

    async def reachable():
        return True

    async def via_api():
        calls.append("api")
        return 1, [{"sha": "a" * 40, "commit": {"message": "新提交", "author": {"date": "2026-10-02T00:00:00Z"}}}]

    def via_git(branch):
        calls.append("git")
        return None

    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_api", via_api)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", via_git)
    monkeypatch.setattr(cp, "_order_oldest_first", lambda raw: list(raw))
    monkeypatch.setattr(cp._version, "local_commit_count", lambda: 8)
    monkeypatch.setattr(cp, "_update_cache", None)

    result = cp.asyncio.run(cp.update_check())

    assert result["update_available"] is True
    assert result["versions"][0]["subject"] == "新提交"
    assert calls == ["api"], "有 Token 时不该再去 fetch"


def test_git_fallback_when_token_lacks_permission(monkeypatch, token_file):
    """Token 没这个仓库的权限时，退回本地 git（本机凭据可能已经能读）。"""
    async def reachable():
        return True

    async def api_forbidden():
        raise cp._UpdateCheckError("repo_forbidden")

    def via_git(branch):
        return 1, [{"sha": "b" * 40, "commit": {"message": "来自 git", "author": {"date": "2026-10-02T00:00:00Z"}}}]

    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_api", api_forbidden)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", via_git)
    monkeypatch.setattr(cp, "_order_oldest_first", lambda raw: list(raw))
    monkeypatch.setattr(cp._version, "local_commit_count", lambda: 8)
    monkeypatch.setattr(cp, "_update_cache", None)

    result = cp.asyncio.run(cp.update_check())
    assert result["versions"][0]["subject"] == "来自 git"


# ── git 侧的凭据注入 ──

def test_git_env_injects_credential_helper(monkeypatch, token_file):
    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp, "_CREDENTIAL_HELPER_SCRIPT", __file__)   # 存在即可

    env = cp._git_env(with_token=True)

    assert env is not None
    assert env["DSH_GIT_TOKEN"] == TOKEN
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert "git_credential" in env["GIT_CONFIG_VALUE_0"] or __file__ in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_env_without_token_is_none(token_file):
    assert cp._git_env(with_token=True) is None


def test_git_env_skips_token_for_ssh_remote(monkeypatch, token_file):
    github_auth.save(TOKEN)
    monkeypatch.setattr(cp, "_remote_repository", lambda: "git@github.com:AceovoeL/x.git")
    assert cp._git_env(with_token=True) is None


def test_git_run_keeps_the_parent_environment_when_prompts_are_off(monkeypatch, tmp_path):
    """关 git 交互提示的两个变量要叠加在父进程环境上，不能整个替换掉它。

    以前这里给子进程的环境只有 ``GIT_TERMINAL_PROMPT`` / ``GIT_ASKPASS``（子进程的
    环境是被整个替换的），Windows 上 git 因此丢掉 SystemRoot，连 github.com 都解析
    不了（"Could not resolve host: github.com"），检查更新就被误报成「需要私有仓库
    凭据」—— 仓库其实早就公开了。
    """
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cp._version, "REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("DSH_ENV_PROBE", "kept")
    seen: dict = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    assert cp._git_run(["status"], prompt_off=True) is not None
    assert seen["env"]["DSH_ENV_PROBE"] == "kept"        # 父进程的环境还在
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"     # 提示仍然关着
    assert seen["env"]["GIT_ASKPASS"] == ""


# ── 开发者模式关着时：一律不用它 ──────────────────────────────────────────
#
# 「关掉开发者模式」= 关掉「从私有仓库获取项目代码」这项功能：检查更新不再带 Token、
# git 不再拿凭据助手。Token 文件留着 —— 重新打开开关就接着用（见上面 re-enable 的用例
# 在 test_panel_developer_mode.py 里），要删只能用凭据框右边的「清除」。

def test_token_is_not_used_while_developer_mode_is_off(monkeypatch, token_file, panel_config):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _FakeResponse({})

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp, "_CREDENTIAL_HELPER_SCRIPT", __file__)
    github_auth.save(TOKEN)
    _set_developer_mode(panel_config, False)

    cp._github_json("https://api.github.com/user")

    assert "authorization" not in seen["headers"]
    assert github_auth.load() == TOKEN, "文件要留着，用户不用重新粘贴"
    assert cp._github_token() == ""
    assert cp._git_env(with_token=True) is None, "git 也不该拿到凭据助手"


def test_token_status_does_not_ask_github_while_developer_mode_is_off(monkeypatch, token_file,
                                                                     panel_config):
    """关着时连「这个 Token 是谁」都不去问：那一步会拿 Token 发请求。"""
    github_auth.save(TOKEN)
    _set_developer_mode(panel_config, False)
    monkeypatch.setattr(cp, "_github_account",
                        lambda token: pytest.fail("开发者模式关着时不该拿 Token 去问 GitHub"))

    payload = cp.asyncio.run(cp.update_token_status())

    assert payload["configured"] is True, "存过就是存过，面板要知道"
    assert payload["login"] == ""
