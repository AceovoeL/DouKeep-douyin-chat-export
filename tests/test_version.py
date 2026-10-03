"""版本号规则：第一个 commit 是 1.0.0，之后每个 commit 把最后一位加一。

这些用例同时钉住进位规则（1.0.9 → 1.1.0 → 1.1.1），因为控制面板「关于」页和
更新检测都靠它把 commit 数翻译成版本号。
"""
from common import version


def test_first_commit_is_one_zero_zero():
    assert version.version_string(1) == "1.0.0"


def test_each_commit_bumps_patch():
    assert version.version_string(2) == "1.0.1"
    assert version.version_string(10) == "1.0.9"


def test_patch_rolls_over_into_minor():
    assert version.version_string(11) == "1.1.0"
    assert version.version_string(12) == "1.1.1"
    assert version.version_string(20) == "1.1.9"
    assert version.version_string(21) == "1.2.0"


def test_round_trip_commit_count():
    for count in (1, 2, 9, 10, 11, 12, 99, 100, 137):
        assert version.commit_count_for(version.version_string(count)) == count


def test_invalid_versions_are_rejected():
    for bad in ("", "1.0", "1.0.0.0", "v1.0.0", "1.0.x", "2.0.0", "1.0.10"):
        assert version.commit_count_for(bad) is None
        assert version.version_key(bad) == (0, 0, 0)


def test_shipped_version_matches_commit_count():
    """common/version.py 里的 VERSION 必须和它的 commit 序号一致。"""
    count = version.commit_count_for(version.VERSION)
    assert count is not None, f"VERSION 不是合法版本号: {version.VERSION}"
    assert version.version_string(count) == version.VERSION


def test_version_is_in_sync_with_git_history():
    """VERSION 必须是合法的版本号，且与 commit 数保持「同步」。

    为什么是「同步」而不是「相等」：VERSION 是静态写在文件里的，而每一次提交
    （包括写回版本号的那次）都会让 commit 数 +1。如果硬要求 ``VERSION ==
    version_string(commit 数)``，那就只能靠再提交一次来满足它 —— 而那次提交又把
    数字顶走一位，永远追不上。所以约定的不变量是：

        VERSION 的 commit 序号 ∈ { 当前 commit 数 - 落后次数, ..., 当前 commit 数 }

    也就是说：版本号最多落后若干个 commit（改动攒着一起发版时很正常），但**绝不
    允许超过当前 commit 数**（那意味着版本号指向了还不存在的提交，一定是手改错了）。
    ``tools/sync_version.py`` 负责在发版时把它追平。

    CI 的 actions/checkout 默认是浅克隆（fetch-depth: 1），那里数出来的 commit
    数没有意义，local_commit_count() 返回 None，这个用例直接跳过。
    """
    count = version.local_commit_count()
    if count is None:
        return

    declared = version.commit_count_for(version.VERSION)
    assert declared is not None, f"VERSION 不是合法版本号: {version.VERSION}"
    assert declared <= count, (
        f"VERSION={version.VERSION} 对应第 {declared} 个 commit，"
        f"但仓库只有 {count} 个提交 —— 版本号写过头了"
    )


def test_shallow_clone_detection(tmp_path, monkeypatch):
    """浅克隆要能被识别出来，否则 CI 里版本号会算错。

    这里把 subprocess.run 也换掉：不依赖运行环境里真有没有 git，只验证
    「.git/shallow 存在 → 不数提交」这条规则。
    """
    fake_root = tmp_path / "repo"
    (fake_root / ".git").mkdir(parents=True)
    monkeypatch.setattr(version, "REPO_ROOT", str(fake_root))

    class _Result:
        returncode = 0
        stdout = "12\n"
        stderr = ""

    monkeypatch.setattr(version.subprocess, "run", lambda *a, **k: _Result())

    assert version.is_shallow_clone() is False
    assert version.local_commit_count() == 12       # 完整克隆：正常数出来

    (fake_root / ".git" / "shallow").write_text("deadbeef\n", encoding="utf-8")
    assert version.is_shallow_clone() is True
    assert version.local_commit_count() is None, "浅克隆不能当成完整历史来数"


def test_worktree_gitdir_pointer_is_followed(tmp_path, monkeypatch):
    """worktree / submodule 里 .git 是个文件，shallow 标记在那个目录里。"""
    fake_root = tmp_path / "wt"
    real_git = tmp_path / "main" / ".git" / "worktrees" / "wt"
    real_git.mkdir(parents=True)
    (real_git / "shallow").write_text("deadbeef\n", encoding="utf-8")
    fake_root.mkdir()
    (fake_root / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    monkeypatch.setattr(version, "REPO_ROOT", str(fake_root))
    assert version.is_shallow_clone() is True


def test_repository_flags():
    assert version.is_fork_repository(version.REPOSITORY_URL)
    assert version.is_fork_repository(version.REPOSITORY_GIT_URL)
    assert version.is_fork_repository(version.REPOSITORY_URL + "/")
    assert not version.is_fork_repository(version.UPSTREAM_REPOSITORY_URL)
    assert not version.is_fork_repository("")


def test_legacy_repository_urls_still_count_as_this_project():
    """用户名改过（Ace-1016 → AceovoeL）：老机器上的 remote 还指着旧地址，
    一键更新不能因此突然说「不是本项目的更新源」。"""
    assert version.LEGACY_REPOSITORY_URLS, "改过名字就要留着旧地址"
    for url in version.LEGACY_REPOSITORY_URLS:
        assert version.is_fork_repository(url)
        assert version.is_fork_repository(url + ".git")
        assert version.is_fork_repository(url.upper())
    assert not version.is_fork_repository("https://github.com/someone-else/douyin-chat-export")
