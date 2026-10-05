"""版本号与仓库信息（控制面板「关于」页的数据来源）。

版本号规则：仓库的第一个 commit 记为 1.0.0，之后每多一个 commit 就把最后一位加一，
进位和常规三段版本一致 —— 1.0.9 的下一个版本是 1.1.0，1.1.9 之后是 1.2.0。

    commit 数   版本号
    1           1.0.0
    2           1.0.1
    ...
    11          1.1.0
    12          1.1.1

所以「本地版本」和「远端版本」都能由 commit 数唯一推出来：远端最新 commit 数取自
GitHub API 的提交列表（见 backend/control_panel.py），本地版本就是下面的 VERSION。

关于"落后一点"：VERSION 必须是**静态**写在文件里的（面板「关于」页和更新检测都读它），
它没法自己跟着 commit 数跑。所以约定是：每次提交时，把它写成"这次提交是第几个 commit"
对应的版本号（第 18 个 commit → 1.1.7）；攒着几个改动一起发版时，它就**落后**于当前
commit 数，这是允许的，**但绝不允许超前**（超前说明版本号指向了还不存在的提交）。新增
commit 之后跑 `python tools/sync_version.py --apply` 重新写入即可（工具会把这个约定讲清楚）。
"""

from __future__ import annotations

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 本项目当前版本。按上面的约定，它是"包含它的这次提交"的版本号：正常状态下等于
#: version_string(commit 数)，攒着改动没发版时会落后若干位（只能落后，不能超前）。
#: 2026-10-03 迁到新仓库 DouKeep-douyin-chat-export（从空仓库重开、旧历史不带过去），
#: 版本号随新仓库从 1.0.0 重新开始。
VERSION = "1.1.8"

#: 版本号第一位默认是 1，一个「大版本」内共 100 个小版本（1.0.0 → 1.9.9）。
_BASE_MAJOR = 1
_STEPS_PER_MINOR = 10

#: 本仓库（fork 后的维护仓库，更新功能从这里拉取）。产品名仍是 douyin-chat-export，
#: 只有 GitHub 仓库名带 DouKeep 前缀。
REPOSITORY_URL = "https://github.com/AceovoeL/DouKeep-douyin-chat-export"
REPOSITORY_GIT_URL = REPOSITORY_URL + ".git"
REPOSITORY_SLUG = "AceovoeL/DouKeep-douyin-chat-export"
REPOSITORY_BRANCH = "main"

#: 改过名字的老地址。仓库先改过用户名（Ace-1016 → AceovoeL），2026-10-03 又整体迁到
#: 新仓库 DouKeep-douyin-chat-export。别的机器上 clone 下来的 remote 还指着这些旧地址，
#: GitHub 自己会 301 过去，但这里比的是字符串 —— 一并认，免得那些机器上「一键更新」
#: 突然说「不是本项目的更新源」。
LEGACY_REPOSITORY_URLS = (
    "https://github.com/AceovoeL/douyin-chat-export",
    "https://github.com/Ace-1016/douyin-chat-export",
)

#: 上游原创仓库（本项目基于它修改而来）。
UPSTREAM_REPOSITORY_URL = "https://github.com/TeamBreakerr/douyin-chat-export"
UPSTREAM_REPOSITORY_SLUG = "TeamBreakerr/douyin-chat-export"

AUTHOR = "AceovoeL"
AUTHOR_URL = "https://github.com/AceovoeL"
UPSTREAM_AUTHOR = "TeamBreakerr"
LICENSE_NAME = "MIT"


def version_string(commit_count: int) -> str:
    """第 ``commit_count`` 个 commit 对应的版本号（1 → 1.0.0）。"""
    if commit_count < 1:
        raise ValueError("commit_count must be >= 1")
    n = commit_count - 1                       # 第一个 commit 是 1.0.0
    minor, patch = divmod(n, _STEPS_PER_MINOR)  # 1.0.9 的下一位进位到 1.1.0
    return f"{_BASE_MAJOR}.{minor}.{patch}"


def commit_count_for(version: str) -> int | None:
    """版本号反推 commit 数；格式不对时返回 None。"""
    parts = str(version).strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    major, minor, patch = (int(p) for p in parts)
    if major != _BASE_MAJOR or patch >= _STEPS_PER_MINOR:
        return None
    return minor * _STEPS_PER_MINOR + patch + 1


def version_key(version: str) -> tuple[int, int, int]:
    """把版本号变成可比较的元组，非法版本当作 0.0.0。"""
    count = commit_count_for(version)
    if count is None:
        return (0, 0, 0)
    parts = str(version).strip().split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def format_version_history(labels: list[str]) -> str:
    """把版本号标签拼成「1.0.0 → 1.1.1」，给提示文案用。"""
    return " → ".join(labels)


def is_shallow_clone() -> bool:
    """是不是浅克隆（例如 CI 里 actions/checkout 默认的 fetch-depth: 1）。

    浅克隆里 ``git rev-list --count HEAD`` 只数得到已有的那几个提交，得到的
    「commit 数」和真实历史对不上，所以这种情况必须当成「读不到」。
    """
    git_dir = os.path.join(REPO_ROOT, ".git")
    if os.path.isfile(git_dir):                 # worktree / submodule：.git 是文件
        try:
            with open(git_dir, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("gitdir:"):
                        git_dir = os.path.join(REPO_ROOT, line.split(":", 1)[1].strip())
                        break
        except OSError:
            return False
    return os.path.exists(os.path.join(git_dir, "shallow"))


def local_commit_count() -> int | None:
    """本地仓库的 commit 数；.git 不在、读不到或浅克隆时返回 None。"""
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        return None
    if is_shallow_clone():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def is_fork_repository(url: str | None = None) -> bool:
    """配置里的远端是不是本项目的仓库（用来决定要不要提供一键更新）。

    改过用户名之后，老地址（见 LEGACY_REPOSITORY_URLS）也算数。
    """
    remote = (url or "").strip().rstrip("/").lower()
    if remote.endswith(".git"):
        remote = remote[:-4]
    known = {REPOSITORY_URL.lower()}
    known.update(item.lower() for item in LEGACY_REPOSITORY_URLS)
    return remote in known
