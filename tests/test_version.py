"""版本号：发版时手写，和 commit 个数无关。

下面的换算表（序号 ↔ 三段版本号）只服务于「检查更新」时定位落后的提交，
**不再**用来推导 VERSION —— 这里只验证换算是自洽的。
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


def test_shipped_version_is_a_legal_version():
    """VERSION 必须是一个合法的三段版本号，且能原样换算回来。"""
    count = version.commit_count_for(version.VERSION)
    assert count is not None, f"VERSION 不是合法版本号: {version.VERSION}"
    assert version.version_string(count) == version.VERSION


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
