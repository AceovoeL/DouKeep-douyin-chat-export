"""去掉 Windows 盖在「下载来的文件」上的标记（Mark-of-the-Web）。

从浏览器下载的 ZIP 解压出来的文件，Windows 会给每个文件挂一条附加数据流
``Zone.Identifier``（内容是 ``[ZoneTransfer]`` + ``ZoneId=3``，意思是「来自 Internet」）。
带这个标记的 ``.bat`` / ``.exe`` 被打开时，Windows 会先弹一个框：

    打开文件 - 安全警告
    无法验证发布者。你确定要运行此软件吗？

这不是项目在要权限，也不是「程序没签名」，而是系统对下载来源的提醒。麻烦的地方在于：
**这个框在我们的代码跑起来之前就弹出来了**，所以脚本拦不住第一次；能做的是启动之后把标记
清掉 —— 工具自己跑过一次，这个文件就不再带标记，以后双击不会再问。（重新下载一个 ZIP 解压
出来又会有，那就再清一次。）

为什么放在 Python 这边：``start.ps1`` / ``start.sh`` / 双击 bat / 面板里起的服务，最后都会进
后端，一次覆盖所有入口；而且它有单元测试（``tests/test_download_mark.py``）。``start.ps1``
里还顺手清了一下项目根目录那一层（见那边的注释）—— 双击的对象就在那一层，早点清掉能少弹一次。
双击 bat 后**隐藏**启动的那一次不经过 Windows 的关联打开流程（改由 ``cmd.exe`` 拉起），
所以那种弹窗只会出现在用户自己的那一次双击上。

只清会触发弹窗的扩展名（``RISKY_EXTENSIONS``）：``.py`` / ``.md`` / ``.png`` 这些带着标记也
不会弹窗，没必要动。非 Windows 系统没有附加数据流这回事，所有函数都是空操作。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator

from common.paths import REPO_ROOT

#: 下载标记那条附加数据流（NTFS Alternate Data Stream）的名字
STREAM_NAME = "Zone.Identifier"

#: 会被 Windows 弹「无法验证发布者」拦一下的扩展名（系统默认清单里挑常见的）。
#: 其它扩展名带着标记也不会弹窗，不用去动它们。
RISKY_EXTENSIONS = frozenset({
    ".bat", ".cmd", ".com", ".cpl", ".exe", ".hta", ".inf", ".ins", ".isp", ".jar",
    ".js", ".jse", ".lnk", ".msi", ".msp", ".ps1", ".ps1xml", ".ps2", ".ps2xml",
    ".psc1", ".psc2", ".reg", ".scf", ".scr", ".sct", ".url", ".vb", ".vbe", ".vbs",
    ".ws", ".wsc", ".wsf", ".wsh",
})

#: 这些目录里不会有「用户会双击的启动脚本」，扫它们纯属浪费时间
#: （venv 里上万个文件；data / config 是用户数据；几个 pytest 目录本身可能不许读）。
SKIP_DIRS = frozenset({
    ".git", ".idea", ".pytest_cache", ".pytest-cache", ".pytest-run", ".pytest-tmp",
    ".venv", ".vscode", "__pycache__", "build", "config", "data", "dist", "node_modules", "venv",
})


def is_supported() -> bool:
    """只有 Windows（NTFS 附加数据流）才有这回事，别的系统一律空操作。"""
    return os.name == "nt"


def _stream_path(path: str | os.PathLike[str]) -> str:
    return f"{os.fspath(path)}:{STREAM_NAME}"


def has_download_mark(path: str | os.PathLike[str]) -> bool:
    """这个文件带不带下载标记。

    不能拿 ``os.path.exists("xx:Zone.Identifier")`` 判断：Windows 上那种路径 .NET 与
    ``GetFileAttributes`` 这些走「普通路径」的接口一律认不出来，只有真正去开这个流才准。
    """
    if not is_supported():
        return False
    try:
        with open(_stream_path(path), "rb"):
            return True
    except OSError:
        return False


def strip_download_mark(path: str | os.PathLike[str]) -> bool:
    """删掉一个文件的下载标记；本来就没有（或者删不掉）就返回 ``False``。"""
    if not is_supported():
        return False
    try:
        os.remove(_stream_path(path))
        return True
    except OSError:
        return False


def iter_files(root: str | os.PathLike[str], skip_dirs: Iterable[str] = SKIP_DIRS) -> Iterator[Path]:
    """遍历项目里的文件（跳过 ``skip_dirs`` 里的目录；读不了的目录直接跳过）。"""
    for dirpath, dirnames, filenames in os.walk(root, onerror=None):
        dirnames[:] = [name for name in dirnames if name not in skip_dirs]
        for name in filenames:
            yield Path(dirpath) / name


def strip_project_download_marks(
    root: str | os.PathLike[str] | None = None,
    *,
    extensions: Iterable[str] = RISKY_EXTENSIONS,
    skip_dirs: Iterable[str] = SKIP_DIRS,
) -> list[Path]:
    """把项目里带下载标记的「可执行类」文件清一遍，返回被清掉的文件（绝对路径）。

    非 Windows、或本来就没有标记时返回空列表。
    """
    if not is_supported():
        return []
    base = Path(root) if root is not None else Path(REPO_ROOT)
    wanted = {str(item).lower() for item in extensions}
    stripped: list[Path] = []
    for path in iter_files(base, skip_dirs=skip_dirs):
        if path.suffix.lower() not in wanted:
            continue
        if strip_download_mark(path):
            stripped.append(path)
    return stripped
