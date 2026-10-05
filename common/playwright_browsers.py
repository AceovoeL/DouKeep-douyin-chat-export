"""Playwright 浏览器内核：核对「包要求的版本」对应的内核装没装。

pip 装的只是 playwright 这个 Python 包，浏览器本体（下载约 300 MB、占盘约 700 MB）要另外下载；而且
playwright 升级之后要用的内核版本号会变（就写在目录名里），旧内核随即作废 ——
那时任何要开浏览器的功能都会报::

    BrowserType.launch_persistent_context: Executable doesn't exist at
    ...\\ms-playwright\\chromium_headless_shell-1243\\chrome-headless-shell-win64\\chrome-headless-shell.exe

所以启动脚本（``start.ps1`` / ``start.sh``）每次启动都对着包里的 ``browsers.json``
核一遍，缺了就下载（下载在 ``tools/ensure_playwright_browser.py``）。

这里只放「怎么算缺」这类纯读判断，不联网、不改动任何东西：

* :func:`browsers_root` —— 内核装在哪（跟 playwright 自己的规则一致）
* :func:`required_browsers` —— 包要求哪几个内核、各是哪个版本
* :func:`missing_browsers` / :func:`summarize` —— 缺哪些、给人看的一句话

我们实际用到两个内核：普通 **chromium**（有窗口，采集默认用它）和
**chromium-headless-shell**（导入 Cookie、聊天长图、``HEADLESS=true`` 时用）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: 需要的内核（名字与 playwright 的 ``browsers.json`` 一致）
NEEDED = ("chromium", "chromium-headless-shell")

#: 每个内核的目录里，可执行文件放在哪个子目录。playwright 换过布局，几种都认。
_BINARY_DIRS = {
    "chromium": ("chrome-win64", "chrome-win", "chrome-linux64", "chrome-linux",
                 "chrome-mac", "chrome-mac-arm64"),
    "chromium-headless-shell": ("chrome-headless-shell-win64", "chrome-headless-shell-linux64",
                                "chrome-headless-shell-mac", "chrome-headless-shell-mac-arm64"),
}


def browsers_root() -> Path:
    """浏览器内核的安装目录（和 playwright 自己的算法一致）。"""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom:
        return Path(custom)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    else:
        base = Path.home() / ".cache"
    return base / "ms-playwright"


def browsers_json_path() -> Path | None:
    """playwright 包里那份 ``browsers.json``（写明了要哪几个版本）；没装包时返回 None。"""
    try:
        import playwright  # noqa: PLC0415 —— 没装 playwright 就是「不知道」，不是错误
    except ImportError:
        return None
    path = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    return path if path.is_file() else None


def directory_name(name: str, revision: str) -> str:
    """内核目录名：``chromium-headless-shell`` + ``1234`` → ``chromium_headless_shell-1234``。"""
    return f"{name.replace('-', '_')}-{revision}"


def required_browsers() -> list[tuple[str, str]]:
    """包要求的 (内核名, 版本号)。读不到那份清单时返回空列表 = 「不知道」。"""
    path = browsers_json_path()
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[tuple[str, str]] = []
    for entry in data.get("browsers") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        revision = str(entry.get("revision") or "")
        if name in NEEDED and revision:
            found.append((name, revision))
    return found


def _has_binary(directory: Path, name: str) -> bool:
    """目录里真的有可执行文件吗。

    只看目录存在是不够的：目录可能在（上次下到一半、被清理工具删了文件），
    真要开浏览器时才报「Executable doesn't exist」。
    """
    if not directory.is_dir():
        return False
    known = _BINARY_DIRS.get(name, ())
    for sub in known:
        child = directory / sub
        if not child.is_dir():
            continue
        for entry in child.iterdir():
            # Windows / Linux 是可执行文件，macOS 是 .app 包
            if entry.name.lower().startswith("chrom") or entry.name.endswith(".app"):
                return True
    # 已知布局都没命中：往里再看一层，别因为 playwright 换了目录名就每年重下一遍
    # （只在「子目录名不认识」时才看，否则半下载的 chrome-win64 空目录会被当成装好了）
    for child in directory.iterdir():
        if not child.is_dir() or child.name in known:
            continue
        if any(entry.name.lower().startswith("chrom") or entry.name.endswith(".app")
               for entry in child.iterdir()):
            return True
    return False


def installed_browsers() -> list[str]:
    """已经装好的内核目录名。"""
    root = browsers_root()
    return [directory_name(name, revision) for name, revision in required_browsers()
            if _has_binary(root / directory_name(name, revision), name)]


def missing_browsers() -> list[str]:
    """缺少的内核目录名；``required_browsers()`` 读不到清单（没装包）时返回空列表。"""
    root = browsers_root()
    return [directory_name(name, revision) for name, revision in required_browsers()
            if not _has_binary(root / directory_name(name, revision), name)]


def summarize() -> str:
    """一句话说明当前状态，给面板「环境检测」的「当前」一栏用。

    缺了也照样是「已满足」：启动脚本每次启动都会把缺的补上，所以这里只说
    「启动时自动安装」，不写成故障。
    """
    required = required_browsers()
    if not required or missing_browsers():
        return "启动时自动安装（下载约 300 MB）"
    revision = required[0][1]
    return f"chromium {revision}（含无头内核）"


def manual_hint() -> str:
    """手动补装的命令，出现在检测项的提示里。"""
    if os.name == "nt":
        return "在项目目录执行：venv\\Scripts\\python.exe -m playwright install chromium"
    return "在项目目录执行：venv/bin/python -m playwright install chromium"
