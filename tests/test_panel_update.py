"""更新检测：把远端「落后的 commit」翻译成逐版本更新内容。

这里不打网络、也不碰 git：GitHub compare 与本地 git fetch 两条路径都注入假数据，
只验证版本号换算、分组边界（同一版本号由多个 commit 组成）以及和本地 commit 数
的衔接。
"""
import asyncio
import json
import os

import pytest

from backend import control_panel as cp
from common import paths, version

#: 假装的本地 commit 数（对应版本 1.0.7）
LOCAL_COUNT = 8

#: 假装的本地 HEAD（比对基准与「查看更新内容」链接都用它）
LOCAL_SHA = "deadbeef" * 5


def _commit(sha: str, subject: str, date: str = "2026-10-02",
            parents: list[str] | None = None) -> dict:
    return {
        "sha": sha * 7,
        "parents": [{"sha": item} for item in (parents or [])],
        "commit": {
            "message": subject + "\n\nbody line",
            "author": {"date": f"{date}T12:00:00Z"},
        },
    }


def _fake_fetch(total: int, commits: list[dict]):
    """替换 _fetch_ahead_best_source 的假实现（它是个协程）。

    假数据按 compare 接口的顺序给（「旧 → 新」，实测如此），与真实链路一致。
    """
    async def fetch():
        return total, commits
    return fetch


@pytest.fixture
def local_count(monkeypatch, tmp_path):
    """固定本地 commit 数，免得测试跟着仓库的提交数变化。

    注意要打在 ``common.version`` 上，而不是 ``cp._version``：后者是同一个模块
    对象的另一个名字，但那不是 "绑定"，`_collect_update` 调用的是模块里的函数。
    同时把 Token 文件与面板配置都指到临时目录：避免碰到真实的 data/github_token，
    并顺手打开开发者模式 —— 存下的 Token 只有开发者模式开着时才会被拿来读私有仓库
    （见 github_auth.load_for_use）。
    """
    monkeypatch.setattr(version, "local_commit_count", lambda: LOCAL_COUNT)
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp._github_auth, "TOKEN_PATH", str(tmp_path / "github_token"))
    config_path = tmp_path / "panel_config.json"
    config_path.write_text(json.dumps({"developer_mode": True}), encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", str(config_path))
    return LOCAL_COUNT


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """默认把「网络」封死：预检直接放行，git 那条路返回 None，走 API 假数据。

    真实的 _order_oldest_first 会问本地 git 的祖先关系，而用例里的 sha 都是假的，
    这里统一把 git 关掉（返回 None）：祖先关系于是问不出来，排序只能靠数据里的
    parents（用例自己造）或按来源约定保持原样。本地 HEAD 也固定住：比对基准和
    「查看更新内容」链接都读它，跟着真实仓库走测试会飘。
    """
    async def reachable():
        return True

    monkeypatch.setattr(cp, "_remote_reachable", reachable)
    monkeypatch.setattr(cp, "_git_run", lambda args, **kwargs: None)
    monkeypatch.setattr(cp, "_git_head", lambda: LOCAL_SHA)
    # 每个用例都从空缓存开始，免得互相串味
    monkeypatch.setattr(cp, "_update_cache", None)


def test_order_oldest_first_keeps_compare_order(monkeypatch):
    """compare 接口给的是「旧 → 新」（实测）：父提交关系一看便知，别再翻一次。"""
    older = {"sha": "oldsha", "parents": [{"sha": "base"}]}
    newer = {"sha": "newsha", "parents": [{"sha": "oldsha"}]}
    assert [c["sha"] for c in cp._order_oldest_first([older, newer])] == ["oldsha", "newsha"]


def test_order_oldest_first_reverses_newest_first_input(monkeypatch):
    """提交列表接口 / git log 是「最新在前」：同样靠父提交关系翻过来。"""
    older = {"sha": "oldsha", "parents": [{"sha": "base"}]}
    newer = {"sha": "newsha", "parents": [{"sha": "oldsha"}]}
    assert [c["sha"] for c in cp._order_oldest_first([newer, older])] == ["oldsha", "newsha"]


def test_order_oldest_first_asks_git_when_parents_are_missing(monkeypatch):
    """数据里没有 parents（老数据）时，用本地 git 的祖先关系定序。"""
    monkeypatch.setattr(cp, "_is_ancestor", lambda old, new: (old, new) == ("oldsha", "newsha"))
    raw = [{"sha": "newsha"}, {"sha": "oldsha"}]
    assert [c["sha"] for c in cp._order_oldest_first(raw)] == ["oldsha", "newsha"]


def test_order_oldest_first_trusts_source_when_evidence_is_missing(monkeypatch):
    """两样证据都问不出来时保持原样：各来源交过来时都已经归一成「旧 → 新」。"""
    raw = [{"sha": "oldest"}, {"sha": "middle"}, {"sha": "newest"}]
    assert [c["sha"] for c in cp._order_oldest_first(raw)] == ["oldest", "middle", "newest"]


def test_is_ancestor_reads_git_exit_code(monkeypatch):
    """git merge-base --is-ancestor：0 = 是祖先，1 = 不是，其他＝问不出来。"""
    class _Result:
        def __init__(self, returncode: int):
            self.returncode = returncode

    monkeypatch.setattr(cp, "_git_run", lambda args, **kwargs: _Result(0))
    assert cp._is_ancestor("a", "b") is True
    monkeypatch.setattr(cp, "_git_run", lambda args, **kwargs: _Result(1))
    assert cp._is_ancestor("a", "b") is False
    monkeypatch.setattr(cp, "_git_run", lambda args, **kwargs: _Result(128))
    assert cp._is_ancestor("a", "b") is None
    monkeypatch.setattr(cp, "_git_run", lambda args, **kwargs: None)
    assert cp._is_ancestor("a", "b") is None


def test_git_path_normalises_order_and_parses_parents(monkeypatch):
    """本地 git 那条路：git log 也是「最新在前」，交出去前翻成「旧 → 新」并带上父提交。"""
    newest, oldest, base = "b" * 40, "a" * 40, "c" * 40
    log = (
        f"{newest}\x1f2026-10-02T12:49:58+00:00\x1f{oldest}\x1f后发布的改动\x1e"
        f"{oldest}\x1f2026-10-02T12:38:28+00:00\x1f{base}\x1f先发布的改动\x1e"
    )
    seen: list[list[str]] = []

    class _Result:
        def __init__(self, stdout: str):
            self.stdout, self.stderr, self.returncode = stdout, "", 0

    def fake_git(args, **kwargs):
        seen.append(args)
        if args[0] == "rev-parse":
            return _Result(LOCAL_SHA + "\n")
        if args[0] == "rev-list":
            return _Result("2\n")
        if args[0] == "log":
            return _Result(log)
        return _Result("")                      # fetch

    monkeypatch.setattr(cp, "_git_run", fake_git)

    total, raw = cp._fetch_ahead_commits_via_git("main")

    assert total == 2
    assert [item["commit"]["message"] for item in raw] == ["先发布的改动", "后发布的改动"]
    assert [cp._parents_of(item) for item in raw] == [[base], [oldest]]
    # 父提交信息是格式化串要来的（没有它就定不了序）
    assert any("%P" in " ".join(args) for args in seen)


def test_collect_update_falls_back_to_git_without_token(monkeypatch, local_count):
    """没有 Token 时走本地 git；git 能用就不必打 API。"""
    async def api_should_not_run():
        raise AssertionError("没有 Token 时不该走 API")

    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", lambda branch: (1, [_commit("a", "新提交")]))
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_api", api_should_not_run)
    result = asyncio.run(cp._collect_update())

    assert result["behind"] == 1
    assert result["versions"][0]["subject"] == "新提交"


def test_collect_update_reports_up_to_date(monkeypatch, local_count):
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(0, []))
    result = asyncio.run(cp._collect_update())

    assert result["ok"] is True
    assert result["update_available"] is False
    assert result["behind"] == 0
    assert result["current_version"] == version.VERSION
    assert result["remote_version"] == version.VERSION
    assert result["versions"] == []


def test_collect_update_maps_each_commit_to_its_version(monkeypatch, local_count):
    # 本地是第 8 个 commit（1.0.7），远端一共 11 个 commit（1.1.0）
    commits = [
        _commit("c", "第一个提交"),
        _commit("b", "第二个提交"),
        _commit("a", "第三个提交"),
    ]
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(3, commits))
    result = asyncio.run(cp._collect_update())

    assert result["behind"] == 3
    assert result["remote_version"] == "1.1.0"
    # 最新在前：第一条就是远端最新版本
    assert [(c["version"], c["subject"]) for c in result["versions"]] == [
        ("1.1.0", "第三个提交"),
        ("1.0.9", "第二个提交"),
        ("1.0.8", "第一个提交"),
    ]
    assert result["versions"][0]["sha"] == "a" * 7
    assert result["versions"][0]["date"] == "2026-10-02"
    assert result["versions"][0]["commit_count"] == 11


def test_collect_update_splits_versions_across_patch_rollover(monkeypatch, local_count):
    # 本地第 10 个 commit（1.0.9）→ 远端第 12 个（1.1.1）：跨过 1.0.9 → 1.1.0 的进位
    monkeypatch.setattr(version, "local_commit_count", lambda: 10)
    commits = [_commit("b", "进位后第一个"), _commit("a", "进位后第二个")]
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(2, commits))
    result = asyncio.run(cp._collect_update())

    assert [c["version"] for c in result["versions"]] == ["1.1.1", "1.1.0"]
    # 「查看更新内容」比的是本地 commit 与远端分支，不再是 vX.Y.Z 标签
    assert result["compare_url"].endswith(f"compare/{LOCAL_SHA}...main")


def test_collect_update_keeps_multiple_commits_of_one_version(monkeypatch, local_count):
    commits = [_commit("b", "第一个提交"), _commit("a", "第二个提交")]
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(2, commits))
    result = asyncio.run(cp._collect_update())

    assert [c["version"] for c in result["versions"]] == ["1.0.9", "1.0.8"]
    assert [c["commit_count"] for c in result["versions"]] == [10, 9]


def test_collect_update_pairs_each_version_with_its_own_commit(monkeypatch, local_count):
    """跨 2 个 commit 的更新里，版本号和更新内容不能张冠李戴。

    这正是面板上出现过的样子：compare 接口按「旧 → 新」返回两条提交，旧的那条
    对应低一档的版本号；以前一律把列表反过来，于是两条提交各自拿到了隔壁那条的
    版本号，显示成「v1.2.1 → 修复…（其实是 v1.2.0 的改动）」。
    """
    older = _commit("a", "先发布的改动", parents=["cafe" * 7])
    newer = _commit("b", "后发布的改动", parents=[older["sha"]])
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(2, [older, newer]))

    result = asyncio.run(cp._collect_update())

    # 本地是第 8 个 commit（1.0.7），落后的两条分别是第 9、第 10 个
    assert [(c["version"], c["subject"]) for c in result["versions"]] == [
        ("1.0.9", "后发布的改动"),
        ("1.0.8", "先发布的改动"),
    ]


def test_collect_update_uses_fallback_when_git_history_missing(monkeypatch):
    monkeypatch.setattr(version, "local_commit_count", lambda: None)
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(1, [_commit("a", "新提交")]))
    result = asyncio.run(cp._collect_update())

    # 没有 git 历史时退回用 VERSION 自身反推的 commit 数，仍然能算出远端版本
    expected = version.commit_count_for(version.VERSION) + 1
    assert result["remote_version"] == version.version_string(expected)


# ── GitHub API 那条路：不拿 vX.Y.Z 标签当比对基准 ──────────────────────────
#
# 标签得有人在远端建出来才算数，「检查更新」以前拿它当基准，于是维护者忘了推标签
# 时永远报「远端没有当前版本对应的标签」。现在改用本地 HEAD 的 commit 比对；
# 没有 git 记录的副本（整体拷贝、没装 git）则按 commit 条数比对。

def _link(last_page: int) -> str:
    """造一个 GitHub 风格的分页响应头（最后一页是第 ``last_page`` 页）。"""
    base = "https://api.github.com/repositories/1/commits?sha=main&per_page=1&page="
    return f'<{base}2>; rel="next", <{base}{last_page}>; rel="last"'


def _headers(last_page: int) -> dict:
    """假响应头（真实的是 ``response.headers``，取 Link 的用法一样）。"""
    return {"Link": _link(last_page)}


def test_last_page_reads_github_link_header():
    """远端 commit 总数从分页 Link 头里读（per_page=1 时最后一页页号就是总数）。"""
    assert cp._last_page(_link(20)) == 20
    assert cp._last_page("") is None


def test_remote_commit_total_falls_back_without_link_header(monkeypatch):
    """只有一页（没有 Link 头）时按返回条数算，空仓库就是 0。"""
    monkeypatch.setattr(cp, "_github_read", lambda url, **kw: ([{"sha": "a" * 7}], None))
    assert cp._remote_commit_total("owner/repo", "main") == 1
    monkeypatch.setattr(cp, "_github_read", lambda url, **kw: ([], None))
    assert cp._remote_commit_total("owner/repo", "main") == 0


def test_api_compares_local_commit_not_tag(monkeypatch):
    """有 git 记录时用本地 HEAD 当基准，请求里不该再出现版本标签。"""
    urls: list[str] = []

    def fake_json(url, **kwargs):
        urls.append(url)
        assert "/compare/" in url, url
        return {"total_commits": 1, "commits": [_commit("a", "新提交")]}

    monkeypatch.setattr(cp, "_github_json", fake_json)
    total, raw = asyncio.run(cp._fetch_ahead_commits_via_api())

    assert (total, len(raw)) == (1, 1)
    assert LOCAL_SHA in urls[0]
    assert all(f"v{version.VERSION}" not in url for url in urls)


def test_api_counts_commits_without_git(monkeypatch):
    """没有 git 记录时按条数比对：远端 11 个、本地 8 个 → 落后 3 个。

    提交列表接口是「最新在前」，取回来要统一翻成「旧 → 新」。
    """
    monkeypatch.setattr(cp, "_git_head", lambda: None)
    monkeypatch.setattr(cp, "_local_commit_count", lambda: LOCAL_COUNT)
    monkeypatch.setattr(cp, "_github_read", lambda url, **kw: ([{"sha": "a" * 7}], _headers(11)))
    monkeypatch.setattr(cp, "_github_json", lambda url, **kw: [
        _commit("a", "第三个提交"),
        _commit("b", "第二个提交"),
        _commit("c", "第一个提交"),
    ])

    total, raw = asyncio.run(cp._fetch_ahead_commits_via_api())

    assert total == 3
    assert [item["commit"]["message"].splitlines()[0] for item in raw] == [
        "第一个提交", "第二个提交", "第三个提交",
    ]


def test_api_counts_commits_when_base_unknown(monkeypatch):
    """本地 commit 远端不认（历史被改写过）时改按条数比对，而不是直接报错。"""
    def fake_json(url, **kwargs):
        if "/compare/" in url:
            return None                      # 远端没有这个 commit：404
        return [_commit("a", "新提交")]

    monkeypatch.setattr(cp, "_git_head", lambda: LOCAL_SHA)
    monkeypatch.setattr(cp, "_local_commit_count", lambda: LOCAL_COUNT)
    monkeypatch.setattr(cp, "_github_read", lambda url, **kw: ([{"sha": "a" * 7}], _headers(9)))
    monkeypatch.setattr(cp, "_github_json", fake_json)

    total, raw = asyncio.run(cp._fetch_ahead_commits_via_api())

    assert (total, len(raw)) == (1, 1)


def test_collect_update_without_git_still_reports_versions(monkeypatch, local_count):
    """整条链路：没有 git 记录的副本 + 已有 Token，也要能算出落后哪几个版本。"""
    cp._github_auth.save("ghp_" + "a" * 36)
    monkeypatch.setattr(cp, "_git_head", lambda: None)
    monkeypatch.setattr(cp, "_github_read", lambda url, **kw: ([{"sha": "a" * 7}], _headers(11)))
    monkeypatch.setattr(cp, "_github_json", lambda url, **kw: [
        _commit("a", "第三个提交"),
        _commit("b", "第二个提交"),
        _commit("c", "第一个提交"),
    ])

    result = asyncio.run(cp._collect_update(refresh=True))

    assert result["behind"] == 3
    assert result["remote_version"] == "1.1.0"
    assert [c["version"] for c in result["versions"]] == ["1.1.0", "1.0.9", "1.0.8"]
    # 拿不到本地 commit 时链接退回远端提交列表页，至少点得开
    assert result["compare_url"].endswith(f"/commits/{version.REPOSITORY_BRANCH}")


def test_update_check_reports_missing_repo(monkeypatch, local_count):
    async def boom():
        raise cp._UpdateCheckError("repo_missing")
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", boom)

    response = asyncio.run(cp.update_check())
    payload = json.loads(response.body)

    assert response.status_code == 502
    assert payload["ok"] is False
    assert payload["error_code"] == "repo_missing"
    assert "读不到" in payload["error"]


def test_unreachable_remote_fails_fast_without_git_fetch(monkeypatch, local_count):
    """预检发现仓库不可达时，不该再去 fetch（否则要白等十几秒）。"""
    calls: list = []

    async def unreachable():
        return False

    monkeypatch.setattr(cp, "_remote_reachable", unreachable)
    monkeypatch.setattr(cp, "_fetch_ahead_commits_via_git", lambda branch: calls.append("git"))

    response = asyncio.run(cp.update_check())
    assert response.status_code == 502
    assert json.loads(response.body)["error_code"] == "repo_missing"
    assert calls == []


def test_update_check_is_cached(monkeypatch, local_count):
    """连续点击「检查更新」时命中缓存，不再重复请求。"""
    calls: list = []

    async def counting():
        calls.append(1)
        return 0, []

    monkeypatch.setattr(cp, "_fetch_ahead_best_source", counting)

    # 成功时 endpoint 直接返回 dict，出错才返回 JSONResponse
    first = asyncio.run(cp.update_check())
    second = asyncio.run(cp.update_check())
    assert first["ok"] is True
    assert second["ok"] is True
    assert len(calls) == 1, "第二次应该走缓存"

    # refresh=true 时绕过缓存
    third = asyncio.run(cp.update_check(refresh=True))
    assert third["ok"] is True
    assert len(calls) == 2


def test_check_log_records_progress(monkeypatch, local_count):
    """检测过程会写进度日志：面板靠它显示「正在做什么」，免得看着像卡住。"""
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(1, [_commit("a", "新提交")]))

    asyncio.run(cp.update_check(check_id="c1"))

    payload = asyncio.run(cp.update_check_log("c1"))
    texts = [item["text"] for item in payload["lines"]]
    assert payload["pending"] is False
    assert texts[0] == "开始检查更新…"
    assert texts[-1] == "检查完成"
    assert any("本地版本" in text for text in texts)
    assert any("落后 1 个 commit" in text for text in texts)
    # 每行都带时间戳（epoch 秒），面板按本地时间显示
    assert all(isinstance(item["time"], float) for item in payload["lines"])


def test_check_log_mentions_why_it_failed(monkeypatch, local_count):
    """检查失败时日志里也要有一句原因，用户直接看日志框就能明白。"""
    async def unreachable():
        return False

    monkeypatch.setattr(cp, "_remote_reachable", unreachable)

    asyncio.run(cp.update_check(check_id="c2"))

    texts = [item["text"] for item in asyncio.run(cp.update_check_log("c2"))["lines"]]
    assert any(text.startswith("检查失败：") for text in texts)


def test_check_log_is_scoped_by_check_id():
    """日志按 check_id 隔离：编号对不上时回 pending，面板不会显示上一次的旧进度。"""
    cp._check_log_reset("mine")
    cp._check_log_add("读取本地版本")

    assert asyncio.run(cp.update_check_log("mine"))["lines"][0]["text"] == "读取本地版本"
    assert asyncio.run(cp.update_check_log("older")) == {"pending": True, "lines": []}


def test_update_info_exposes_author_and_repos(monkeypatch):
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.REPOSITORY_URL)
    payload = asyncio.run(cp.update_info())

    assert payload["version"] == version.VERSION
    assert payload["commit_count"] == version.commit_count_for(version.VERSION)
    assert payload["author"] == "AceovoeL"
    assert payload["upstream_author"] == "TeamBreakerr"
    assert payload["fork_url"] == version.REPOSITORY_URL
    assert payload["upstream_url"] == version.UPSTREAM_REPOSITORY_URL
    assert payload["update"]["status"] in ("idle", "running", "completed", "failed")


def test_update_run_refuses_foreign_remote(monkeypatch):
    monkeypatch.setattr(cp, "_remote_repository", lambda: version.UPSTREAM_REPOSITORY_URL)
    response = asyncio.run(cp.update_run())

    assert response.status_code == 400
    assert "更新源" in json.loads(response.body)["error"]


def test_update_run_reports_already_latest(monkeypatch, local_count):
    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(0, []))
    result = asyncio.run(cp.update_run())

    assert result["status"] == "up-to-date"


def test_update_run_starts_job(monkeypatch, local_count):
    scheduled: list = []

    async def fake_run_update(from_version, to_version, *, allow_dirty=False, restart=None,
                              notify=False):
        scheduled.append((from_version, to_version, allow_dirty, restart))

    def fake_create_task(coro, **kwargs):
        # 不真的跑协程：确认它被调度即可，顺手关掉免得留 unawaited 警告
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(1, [_commit("a", "新提交")]))
    monkeypatch.setattr(cp, "_run_update", fake_run_update)
    monkeypatch.setattr(cp, "_scrape_state", {**cp._scrape_state, "status": "idle"})
    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)

    try:
        result = asyncio.run(cp.update_run())

        assert result["status"] == "started"
        assert result["from_version"] == version.VERSION
        assert result["to_version"] == "1.0.8"     # 本地 8 个 commit 之后的第 9 个
        assert result["allow_dirty"] is False
        assert cp._update_state["status"] == "running"
        assert scheduled, "更新任务应该被调度"
    finally:
        # 收尾：别把全局状态留给别的测试
        cp._update_state.update({
            "status": "idle", "message": "", "process": None,
            "from_version": None, "to_version": None, "allow_dirty": False,
        })


def test_update_run_passes_allow_dirty(monkeypatch, local_count):
    """带 allow_dirty 的请求要把标记一路传到更新脚本。"""
    seen: list = []

    async def fake_run_update(from_version, to_version, *, allow_dirty=False, restart=None,
                              notify=False):
        seen.append(allow_dirty)

    monkeypatch.setattr(cp, "_fetch_ahead_best_source", _fake_fetch(1, [_commit("a", "新提交")]))
    monkeypatch.setattr(cp, "_run_update", fake_run_update)
    monkeypatch.setattr(cp, "_scrape_state", {**cp._scrape_state, "status": "idle"})
    monkeypatch.setattr(cp.asyncio, "create_task", lambda coro, **kw: coro.close())

    try:
        result = asyncio.run(cp.update_run(cp._UpdateRunRequest(allow_dirty=True)))

        assert result["allow_dirty"] is True
        assert cp._update_state["allow_dirty"] is True
    finally:
        cp._update_state.update({
            "status": "idle", "message": "", "process": None,
            "from_version": None, "to_version": None, "allow_dirty": False,
        })


def test_update_run_refuses_while_scraping(monkeypatch, local_count):
    monkeypatch.setattr(cp, "_scrape_state", {**cp._scrape_state, "status": "running"})
    response = asyncio.run(cp.update_run())

    assert response.status_code == 409
    assert "采集" in json.loads(response.body)["error"]


# ── 面板：大版本号旁边的「新版本 vX.Y.Z 可用」 ────────────────────────────

def test_panel_shows_the_available_version_next_to_the_current_one():
    """有新版本时，在大版本号的右下方挂一个小号的新版本号 +「可用」。"""
    panel = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend", "panel", "static", "panel.html",
    )
    html = open(panel, encoding="utf-8").read()

    # 元素：默认藏着，检测到才显示
    assert 'id="aboutVersionNew" hidden' in html
    assert 'id="aboutVersionNewNum"' in html
    assert 'data-i18n="aboutVersionAvailable">可用<' in html
    # 样式：贴在大版本号的右下方（flex 行里靠底对齐）、字号比大版本号小一档
    assert ".ab-version-new {" in html
    assert "align-self: flex-end" in html
    assert "font-size: calc(15px * var(--font-scale))" in html
    # 手动检查与定时检查两个来源都要喂给它，并且只有真的更新了才显示
    assert "const updateAvailableSources = { check: '', schedule: '' };" in html
    assert "updateAvailableSources.check = data.update_available ? (data.remote_version || '') : '';" in html
    assert "updateAvailableSources.schedule =" in html
    assert ".filter(v => v && compareVersions(v, current) > 0)" in html
    # 中英文案都要有
    assert html.count("aboutVersionAvailable:") == 2
