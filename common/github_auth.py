"""私有仓库的 GitHub 凭据（只读 Token）存取。

仓库可以是私密的：GitHub API 在未认证时对私有仓库一律返回 404，git 也会要求
登录。所以这里保存一个用户自己生成的 **只读** Personal Access Token，面板用它
读更新信息。

存哪儿：``config/github_token``（单独一个文件，不是 panel_config.json）。这样
既不会混进配置的读写逻辑，也省得万一有人在接口里整包返回配置时把 Token 带出去。
``config/`` 本来就在 .gitignore 里，不会被提交。

**什么时候用它**：只有面板打开「开发者模式」时才用（``load_for_use``）。关着时
Token 文件仍然留着，但不再拿去读私有仓库 —— 用户重新打开开发者模式就能接着用，
不用重新粘贴。

文件权限：POSIX 上设成 0600；Windows 上由用户目录的 ACL 保护（不做额外处理）。
"""
from __future__ import annotations

import os
import stat

from common import config, paths

#: 路径的单一来源在 common/paths.py（config/github_token）；留一个模块级名字，
#: 一来面板那边一直这么引用，二来测试可以把它指到临时文件、不碰真实凭据。
TOKEN_PATH = paths.TOKEN_PATH

#: Token 里不该出现的字符（粘贴时常见的换行/引号/空白）
_FORBIDDEN = set(" \t\r\n\"'`")


def normalize(token: str | None) -> str:
    """清掉粘贴时带进来的引号与空白。"""
    text = (token or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.strip()


def is_plausible(token: str) -> bool:
    """粗查格式：GitHub Token 都是较长的可见 ASCII 串。"""
    if not token or len(token) < 20 or len(token) > 255:
        return False
    if any(ch in _FORBIDDEN for ch in token):
        return False
    return all(33 <= ord(ch) <= 126 for ch in token)


def load() -> str:
    """读取已保存的 Token；没有或读不到时返回空串。"""
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return normalize(handle.read())
    except OSError:
        return ""


def load_for_use() -> str:
    """**真正拿去用**的 Token：开发者模式关着时一律当没有。

    文件本身不动（用户不用重新粘贴，重新打开开发者模式就能接着用），只是不再拿它去读
    私有仓库、也不再交给 git 和更新脚本 —— 那是「从私有仓库获取项目代码」这项功能，
    跟着开发者模式一起开关。想彻底删掉 Token 只有 ``clear()`` 一条路（面板上凭据框
    右边的「清除」按钮）。
    """
    if not config.developer_mode_enabled():
        return ""
    return load()


def save(token: str) -> None:
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalize(token) + "\n")
    _harden()


def clear() -> bool:
    """删掉 Token；本来就没有时返回 False。"""
    try:
        os.remove(TOKEN_PATH)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _harden() -> None:
    if os.name == "nt":
        return                       # Windows 交给用户目录 ACL
    try:
        os.chmod(TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass
