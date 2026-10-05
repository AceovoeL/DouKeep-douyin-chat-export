"""环境检测（Python 版）——控制面板「关于 → 环境检测」按钮的数据来源。

和 ``tools/env_check.ps1`` 是同一套检查项、同一套返回结构：

    必需（缺一个都跑不起来）：操作系统、PowerShell、脚本执行策略、项目目录可写、
                             磁盘空间、Python、pip、Node.js、npm、端口 8000、
                             Playwright 浏览器内核（启动脚本会自动装，所以默认算已满足）
    可选（不阻止启动）：      Git、ffmpeg、本机 Edge/Chrome、虚拟环境、前端构建产物

为什么要两份：新电脑上"第一次运行"时 Python / Node 装没装正是被检测的对象，那一步
只能用系统自带的 PowerShell（见 tools/env_check.ps1，结果写给 start.html 读）；而这
一份是**后端已经在运行时**（说明 Python 没问题了）从面板里点按钮触发的检查，顺便能
看到 PowerShell、脚本执行策略这些只在 Windows 上才有意义的项。

两边字段一致，前端共用同一套渲染逻辑。文字给中英两份（``name`` / ``name_en`` 等），
因为控制面板可以切英文；PowerShell 那份只有中文（首次运行在中文用户的机器上）。

返回结构::

    {
      "ts": 1790927977.71, "generated_at": "2026-10-02 15:59:37", "source": "python",
      "project_dir": "...", "service_running": true,
      "summary": {"ok": true, "required_total": 10, "required_passed": 10,
                  "optional_total": 6, "optional_passed": 5},
      "items": [{"id", "name", "name_en", "required", "ok", "requirement",
                 "requirement_en", "current", "current_en", "path",
                 "detail", "detail_en", "hint", "hint_en"}, ...]
    }

检查项只做只读判断：读版本号、看目录是否存在、试写一个临时文件再删掉，不改动系统。
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from common import paths
from common import playwright_browsers as pw_browsers

#: Node.js 的要求与 frontend/package.json 的 engines 保持一致
NODE_REQUIREMENT = ">= 20.19 或 >= 22.12（Vite 7 要求）"
NODE_REQUIREMENT_EN = ">= 20.19 or >= 22.12 (required by Vite 7)"
MIN_PYTHON = (3, 10)
MIN_POWERSHELL = (5, 1)
MIN_NPM = 9
MIN_FREE_GB = 2

_SEMVER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


# ── 小工具 ────────────────────────────────────────────────────────────────
def semver(text: str | None) -> tuple[int, int, int] | None:
    """从任意文本里抠出 (major, minor, patch)。"""
    if not text:
        return None
    match = _SEMVER.search(str(text))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def node_version_ok(version: tuple[int, int, int] | None) -> bool:
    """Vite 7 的 engines 是 ``^20.19.0 || >=22.12.0``（21.x 不在支持范围）。"""
    if not version:
        return False
    major, minor, _patch = version
    if major == 20:
        return minor >= 19
    if major >= 22:
        return major > 22 or minor >= 12
    return False


def which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def run_tool(path: str | None, *arguments: str, timeout: int = 10) -> tuple[int, str] | None:
    """跑一个外部命令并抓回 (退出码, 输出)。命令不存在时返回 None。

    Windows 上 npm 是 ``npm.cmd`` 批处理，交给 ``cmd.exe /c`` 执行更稳。
    """
    if not path:
        return None
    command = [path, *arguments]
    if path.lower().endswith((".cmd", ".bat")):
        command = ["cmd.exe", "/c", *command]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=_no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (result.stdout or b"") + (result.stderr or b"")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        text = raw.decode("gb18030", errors="replace").strip()
    return result.returncode, re.sub(r"\s+", " ", text)


def _no_window_flag() -> int:
    """Windows 下别弹出黑框（其它平台没有这个常量）。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def port_busy(port: int = 8000, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def own_service_running(port: int = 8000) -> bool:
    """8000 端口上是不是已经跑着本项目的后端（面板自己就在里面）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/auth/check", timeout=3
        ) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
        return "need_password" in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _item(item_id: str, name: str, required: bool, ok: bool, requirement: str = "",
          current: str = "", path: str = "", detail: str = "", hint: str = "",
          name_en: str = "", requirement_en: str = "", current_en: str = "",
          detail_en: str = "", hint_en: str = "") -> dict:
    """一条检查结果。英文没写的退回中文（面板切英文时用 ``*_en``）。"""
    return {
        "id": item_id,
        "name": name,
        "name_en": name_en or name,
        "required": bool(required),
        "ok": bool(ok),
        "requirement": requirement,
        "requirement_en": requirement_en or requirement,
        "current": current,
        "current_en": current_en or current,
        "path": path,
        "detail": detail,
        "detail_en": detail_en or detail,
        "hint": hint,
        "hint_en": hint_en or hint,
    }


# ── 各项检查 ──────────────────────────────────────────────────────────────
def _check_os() -> dict:
    detail = "start.ps1 是 PowerShell 启动脚本，只在 Windows 上可用（macOS / Linux 请用 start.sh）"
    detail_en = "start.ps1 is a PowerShell script, so it only runs on Windows (use start.sh on macOS / Linux)"
    if os.name != "nt":
        return _item("os", "操作系统", True, True, "Windows 10 1809 或更高 / Windows 11",
                     platform.platform(),
                     detail="非 Windows 系统：本项目在这里用 start.sh 启动（本次检测按不适用处理）",
                     detail_en="Not Windows: this project starts through start.sh here (treated as not applicable)",
                     name_en="Operating system",
                     requirement_en="Windows 10 1809 or newer / Windows 11")
    current = platform.platform()
    build = 0
    parts = platform.version().split(".")
    if len(parts) >= 3 and parts[2].isdigit():
        build = int(parts[2])
    ok = build >= 17763
    return _item("os", "操作系统", True, ok, "Windows 10 1809 或更高 / Windows 11", current,
                 detail=detail, detail_en=detail_en,
                 hint="" if ok else "Windows 版本太旧，请升级系统后再运行",
                 hint_en="" if ok else "This Windows build is too old — upgrade before running",
                 name_en="Operating system",
                 requirement_en="Windows 10 1809 or newer / Windows 11")


def _check_powershell() -> dict:
    if os.name != "nt":
        return _item("powershell", "PowerShell", True, True, ">= 5.1", "不适用（非 Windows）",
                     current_en="not applicable (not Windows)",
                     detail="运行 start.ps1 用；非 Windows 系统用 start.sh",
                     detail_en="Runs start.ps1; non-Windows systems use start.sh",
                     name_en="PowerShell")
    shell = which("powershell.exe", "powershell")
    result = run_tool(shell, "-NoProfile", "-Command",
                      "$PSVersionTable.PSVersion.ToString()", timeout=20)
    current = result[1] if result else "未检测到"
    version = semver(current)
    ok = bool(version and version[:2] >= MIN_POWERSHELL)
    return _item("powershell", "PowerShell", True, ok, ">= 5.1", current, path=shell or "",
                 current_en="" if result else "not found",
                 detail="运行 start.ps1（建虚拟环境、装依赖、构建前端、起服务）",
                 detail_en="Runs start.ps1 (venv, dependencies, frontend build, server)",
                 hint="" if ok else "到微软官网安装 PowerShell 7，或修复系统自带的 Windows PowerShell",
                 hint_en="" if ok else "Install PowerShell 7 from Microsoft, or repair the built-in Windows PowerShell",
                 name_en="PowerShell")


def _check_execution_policy() -> dict:
    if os.name != "nt":
        return _item("execution_policy", "脚本执行策略", True, True,
                     "任意（启动脚本自带 Bypass）", "不适用（非 Windows）",
                     current_en="not applicable (not Windows)",
                     name_en="Script execution policy")
    shell = which("powershell.exe", "powershell")
    result = run_tool(shell, "-NoProfile", "-Command", "Get-ExecutionPolicy", timeout=20)
    current = result[1] if result else "未读到"
    return _item("execution_policy", "脚本执行策略", True, True,
                 "任意（启动脚本已用 -ExecutionPolicy Bypass）", current,
                 current_en="" if result else "could not read",
                 detail="start.html 与启动器都用 Bypass 方式调用脚本，Restricted / AllSigned 也不会挡住启动",
                 detail_en="start.html and the launcher always call scripts with Bypass, so Restricted / AllSigned cannot block the start",
                 hint="如果直接在终端敲 .\\start.ps1 报「禁止运行脚本」，用 start.html 启动，"
                      "或执行 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned",
                 hint_en="If running .\\start.ps1 by hand is blocked, start through start.html or run Set-ExecutionPolicy -Scope CurrentUser RemoteSigned",
                 name_en="Script execution policy",
                 requirement_en="any (the launcher uses -ExecutionPolicy Bypass)")


def _check_project_dir() -> dict:
    probe = os.path.join(paths.REPO_ROOT, f".env-check-{os.getpid()}.tmp")
    writable = False
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        writable = os.path.exists(probe)
    except OSError:
        writable = False
    finally:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass
    return _item("project_dir", "项目目录可写", True, writable,
                 "可写（要创建 venv、node_modules、data、config 等）",
                 "可写" if writable else "不可写", path=paths.REPO_ROOT,
                 current_en="writable" if writable else "read-only",
                 detail="启动过程要在项目目录里创建 venv/、frontend/node_modules/、frontend/dist/，"
                        "以及存聊天记录的 data/ 和存配置与日志的 config/",
                 detail_en="The start-up creates venv/, frontend/node_modules/, frontend/dist/, "
                           "plus data/ (chat records) and config/ (settings, logs) inside the project folder",
                 hint="" if writable else "把项目放到「文档 / 桌面」这类个人目录下，"
                                          "不要放在 C:\\Program Files 或只读盘里",
                 hint_en="" if writable else "Keep the project in your own folders (Documents/Desktop), not in C:\\Program Files or on a read-only drive",
                 name_en="Project folder writable",
                 requirement_en="writable (venv, node_modules, data, config live here)")


def _check_disk() -> dict:
    try:
        usage = shutil.disk_usage(paths.REPO_ROOT)
        free_gb = round(usage.free / (1024 ** 3), 1)
    except OSError:
        return _item("disk", "磁盘可用空间", True, False, f">= {MIN_FREE_GB} GB", "读取失败",
                     path=paths.REPO_ROOT, current_en="could not read",
                     hint="确认项目所在的盘符可以访问",
                     hint_en="Make sure the drive holding the project is reachable",
                     name_en="Free disk space", requirement_en=f">= {MIN_FREE_GB} GB")
    return _item("disk", "磁盘可用空间", True, free_gb >= MIN_FREE_GB, f">= {MIN_FREE_GB} GB",
                 f"{free_gb} GB 可用", path=paths.REPO_ROOT,
                 current_en=f"{free_gb} GB free",
                 detail="虚拟环境 + 前端依赖 + 构建产物 + 浏览器内核大约要 1.5 GB 上下，聊天媒体会另外占用空间",
                 detail_en="The virtualenv, frontend dependencies, build output and the Playwright "
                           "browsers take roughly 1.5 GB; chat media adds more",
                 hint="" if free_gb >= MIN_FREE_GB else "清理磁盘，或把项目移到空间更充裕的盘符",
                 hint_en="" if free_gb >= MIN_FREE_GB else "Free some space, or move the project to a drive with more room",
                 name_en="Free disk space", requirement_en=f">= {MIN_FREE_GB} GB")


def _check_python() -> dict:
    version = sys.version_info
    current = f"Python {platform.python_version()}"
    ok = (version.major, version.minor) >= MIN_PYTHON
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return _item("python", "Python", True, ok, ">= 3.10", current, path=sys.executable,
                 detail="创建虚拟环境 venv、安装 requirements.txt 里的依赖、运行后端服务"
                        + ("（当前就是项目的 venv）" if in_venv else "（当前不是项目的 venv）"),
                 detail_en="Creates the venv, installs requirements.txt and runs the backend"
                           + (" (this is the project venv)" if in_venv else " (not the project venv)"),
                 hint="" if ok else "Python 版本过低，请安装 3.10 以上版本",
                 hint_en="" if ok else "Python is too old — install 3.10 or newer",
                 name_en="Python")


def _check_pip() -> dict:
    detail = "安装 requirements.txt（fastapi、uvicorn、playwright 等）"
    detail_en = "Installs requirements.txt (fastapi, uvicorn, playwright ...)"
    hint = "执行 python -m ensurepip --upgrade 修复"
    hint_en = "Run python -m ensurepip --upgrade to repair it"
    requirement = "python -m pip 可用（>= 20.0）"
    requirement_en = "python -m pip works (>= 20.0)"
    result = run_tool(sys.executable, "-m", "pip", "--version", timeout=60)
    if not result or result[0] != 0:
        return _item("pip", "pip 包管理器", True, False, requirement,
                     (result[1] if result else "未检测到"), path=sys.executable,
                     current_en=(result[1] if result else "not found"),
                     detail=detail, detail_en=detail_en, hint=hint, hint_en=hint_en,
                     name_en="pip", requirement_en=requirement_en)
    version = semver(result[1])
    return _item("pip", "pip 包管理器", True, bool(version), requirement,
                 result[1], path=sys.executable,
                 detail=detail, detail_en=detail_en,
                 name_en="pip", requirement_en=requirement_en)


def _check_node() -> dict:
    detail = "构建前端界面（frontend 目录里执行 npm install 和 npm run build）"
    detail_en = "Builds the frontend (npm install and npm run build inside frontend/)"
    name_en = "Node.js"
    node = which("node.exe", "node")
    if not node:
        return _item("node", "Node.js", True, False, NODE_REQUIREMENT, "未检测到",
                     current_en="not found", detail=detail, detail_en=detail_en,
                     hint="到 https://nodejs.org/ 下载 LTS 版（20.19+ 或 22.12+）安装，"
                          "安装后重开一次控制台和浏览器",
                     hint_en="Install the LTS build from https://nodejs.org/ (20.19+ or 22.12+), then reopen the console and browser",
                     name_en=name_en, requirement_en=NODE_REQUIREMENT_EN)
    result = run_tool(node, "-v", timeout=20)
    current = result[1] if result else "未知"
    version = semver(current)
    ok = node_version_ok(version)
    hint = "" if ok else (
        f"当前 {current} 不满足要求：Vite 7 需要 Node.js 20.19+ 或 22.12+（21.x 也不在支持范围）。"
        "请升级 Node.js，或用 nvm-windows 切到 22 LTS")
    hint_en = "" if ok else (
        f"{current} does not satisfy Vite 7: it needs Node.js 20.19+ or 22.12+ (21.x is not supported either). "
        "Upgrade Node.js, or switch to 22 LTS with nvm-windows")
    return _item("node", "Node.js", True, ok, NODE_REQUIREMENT, current, path=node,
                 current_en="" if result else "unknown",
                 detail=detail, detail_en=detail_en, hint=hint, hint_en=hint_en,
                 name_en=name_en, requirement_en=NODE_REQUIREMENT_EN)


def _check_npm() -> dict:
    requirement = f">= {MIN_NPM}.0（随 Node.js 一起安装）"
    requirement_en = f">= {MIN_NPM}.0 (ships with Node.js)"
    detail = "安装前端依赖"
    detail_en = "Installs the frontend dependencies"
    hint = "npm 是 Node.js 自带的：重装 Node.js（LTS）即可；装完重开控制台让 PATH 生效"
    hint_en = "npm ships with Node.js: reinstall the LTS build, then reopen the console so PATH refreshes"
    npm = which("npm.cmd", "npm")
    result = run_tool(npm, "-v", timeout=60)
    if not npm or not result or not semver(result[1]):
        return _item("npm", "npm 包管理器", True, False, requirement, "未检测到",
                     current_en="not found", detail=detail, detail_en=detail_en,
                     hint=hint, hint_en=hint_en,
                     name_en="npm", requirement_en=requirement_en)
    version = semver(result[1])
    return _item("npm", "npm 包管理器", True, version[0] >= MIN_NPM,
                 requirement, result[1].split()[0], path=npm,
                 detail=detail, detail_en=detail_en,
                 hint="" if version[0] >= MIN_NPM else "npm 版本偏低，随 Node.js 一起升级",
                 hint_en="" if version[0] >= MIN_NPM else "npm is too old — upgrade it together with Node.js",
                 name_en="npm", requirement_en=requirement_en)


def _check_port() -> dict:
    busy = port_busy(8000)
    own = own_service_running(8000) if busy else False
    if not busy:
        current, current_en, ok = "空闲", "free", True
    elif own:
        current = "已被本项目的后端占用（服务正在运行）"
        current_en = "used by this project's backend (service is running)"
        ok = True
    else:
        current, current_en, ok = "已被其他程序占用", "used by another program", False
    return _item("port_8000", "端口 8000", True, ok, "未被其他程序占用", current,
                 current_en=current_en,
                 detail="后端服务固定监听 127.0.0.1:8000，聊天浏览页和控制面板都在这个端口上",
                 detail_en="The backend always listens on 127.0.0.1:8000 — the chat viewer and the control panel live there",
                 hint="" if ok else "关掉占用 8000 的程序（netstat -ano | findstr :8000 找到 PID，"
                                    "再用任务管理器结束），然后重新检测",
                 hint_en="" if ok else "Close whatever holds port 8000 (netstat -ano | findstr :8000 for the PID), then check again",
                 name_en="Port 8000", requirement_en="not used by another program")


def _check_git() -> dict:
    requirement, requirement_en = ">= 2.20（可选）", ">= 2.20 (optional)"
    detail = ("可选：有它时控制面板用 git pull 更新（只拉有变化的部分，更新前还能查出"
              "本地未提交的改动）；没有它改用下载代码包覆盖，效果一样")
    detail_en = ("Optional: with it the panel updates via git pull (pulls only what changed, "
                 "and spots local uncommitted edits first); without it the panel downloads the "
                 "code package instead — same result")
    git = which("git.exe", "git")
    result = run_tool(git, "--version", timeout=20)
    if not git or not result:
        return _item("git", "Git", False, False, requirement, "未检测到",
                     current_en="not found", detail=detail, detail_en=detail_en,
                     hint="没有 Git 也能一键更新（面板改为下载代码包覆盖，需要在「关于」页填只读 Token）；"
                          "装了更省事，到 https://git-scm.com/download/win 安装",
                     hint_en="No Git is fine — one-click updates still work by downloading the code "
                             "package (needs a read-only token on the About page); install Git from "
                             "https://git-scm.com/download/win to update via git pull instead",
                     name_en="Git", requirement_en=requirement_en)
    version = semver(result[1])
    return _item("git", "Git", False, bool(version and version[0] >= 2),
                 requirement, result[1], path=git,
                 detail=detail, detail_en=detail_en,
                 name_en="Git", requirement_en=requirement_en)


def _check_ffmpeg() -> dict:
    found = ""
    try:
        from backend.media_transcode import find_ffmpeg  # 复用后端的查找顺序
        found = find_ffmpeg() or ""
    except Exception:  # noqa: BLE001 - 查找失败就是"没找到"，不该影响整个检测
        found = which("ffmpeg.exe", "ffmpeg") or ""
    return _item("ffmpeg", "ffmpeg", False, bool(found), "任意近期版本（可选）",
                 "已找到" if found else "未找到", path=found,
                 current_en="found" if found else "not found",
                 detail="可选：抖音视频大多是 H.265，浏览器放不了时用它按需转成 H.264；"
                        "没有它只能回落到播放原文件",
                 detail_en="Optional: most Douyin videos are H.265 — ffmpeg converts them to H.264 on demand; without it the viewer falls back to the original file",
                 hint="" if found else "下载 ffmpeg 后把 bin 目录加进 PATH，"
                                       "或设置环境变量 DOUYIN_FFMPEG 指向 ffmpeg.exe",
                 hint_en="" if found else "Add ffmpeg\\bin to PATH, or point the DOUYIN_FFMPEG variable at ffmpeg.exe",
                 name_en="ffmpeg", requirement_en="any recent build (optional)")


def _check_playwright() -> dict:
    """Playwright 浏览器内核：启动脚本每次启动都会核对并补装，所以这一项默认算已满足。

    「缺了自动下载」这件事由 start.ps1 / start.sh 调用
    ``tools/ensure_playwright_browser.py`` 完成；这里只报告当前装的是哪个版本
    （见 common/playwright_browsers.py），不因此判失败 —— 否则第一次用的人会被
    一项本来不用他管的检查拦住。
    """
    manual = pw_browsers.manual_hint()
    return _item("playwright_chromium", "Playwright 浏览器内核", True, True,
                 "chromium（启动时会自动安装）", pw_browsers.summarize(),
                 path=str(pw_browsers.browsers_root()),
                 current_en="installed automatically at start-up (~300 MB download)",
                 detail="采集聊天记录、渲染聊天长图、导入 Cookie 都要用它。启动脚本每次启动都会核对"
                        "版本、缺了自动下载，所以这一项默认算已满足"
                        f"（真装不上时可以手动补：{manual.replace('在项目目录执行：', '')}）。",
                 detail_en="Chat scraping, the long chat image renderer and Cookie import all need it. "
                           "The start-up script checks the version and downloads it when missing, so this "
                           "counts as satisfied; if that ever fails, install it by hand with "
                           "`python -m playwright install chromium`.",
                 hint=manual,
                 hint_en="Run `python -m playwright install chromium` inside the project folder",
                 name_en="Playwright browser",
                 requirement_en="chromium (installed automatically at start-up)")


def _check_browser() -> dict:
    candidates = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        candidates += [
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
    found = next((path for path in candidates if os.path.exists(path)), "")
    return _item("browser", "本机 Edge / Chrome", False, bool(found), "任意近期版本（可选）",
                 os.path.splitext(os.path.basename(found))[0] if found else "未找到",
                 path=found,
                 current_en=os.path.splitext(os.path.basename(found))[0] if found else "not found",
                 detail="可选：界面回归检查脚本会优先复用本机浏览器；采集用的仍是 Playwright 自带内核",
                 detail_en="Optional: the UI regression script prefers your own browser; scraping still uses the Playwright build",
                 hint="" if found else "一般 Windows 自带 Edge，未检测到也不影响主流程",
                 hint_en="" if found else "Windows ships Edge; missing is fine for the normal flow",
                 name_en="Local Edge / Chrome", requirement_en="any recent version (optional)")


def _check_venv() -> dict:
    exists = os.path.exists(os.path.join(paths.REPO_ROOT, "venv", "Scripts", "python.exe")) or \
        os.path.exists(os.path.join(paths.REPO_ROOT, "venv", "bin", "python3"))
    return _item("venv", "虚拟环境 venv", False, True, "首次运行会自动创建",
                 "已存在（启动会跳过创建，更快）" if exists else "尚未创建（首次运行由 start.ps1 创建）",
                 path=os.path.join(paths.REPO_ROOT, "venv"),
                 current_en="exists (start-up skips creating it)" if exists
                            else "not created yet (the first run creates it)",
                 detail="Python 依赖装在项目自己的 venv 里，不污染系统 Python",
                 detail_en="Python dependencies live in the project venv, not in your system Python",
                 name_en="Virtualenv venv", requirement_en="created automatically on first run")


def _check_frontend_dist() -> dict:
    exists = os.path.exists(os.path.join(paths.FRONTEND_DIST, "index.html"))
    return _item("frontend_dist", "前端构建产物", False, True, "首次运行会自动构建",
                 "已存在（启动时仍会重新构建一次）" if exists else "尚未构建（首次运行由 start.ps1 构建）",
                 path=paths.FRONTEND_DIST,
                 current_en="exists (start-up rebuilds it once)" if exists
                            else "not built yet (the first run builds it)",
                 detail="聊天浏览界面由 Vue 构建后交给后端托管",
                 detail_en="The Vue chat viewer is built and then served by the backend",
                 name_en="Frontend build output", requirement_en="built automatically on first run")


#: 检查项顺序：必需项整段在前，可选项整段在后，最后是两条项目现状信息。
#: 面板和首次运行检测页都是照这个顺序往下画的，所以新加必需项时要放进前面那一段里，
#: 别接在可选项后面 —— 否则用户会看到「必需」的条目夹在一堆「可选」中间。
CHECKS = (
    _check_os,
    _check_powershell,
    _check_execution_policy,
    _check_project_dir,
    _check_disk,
    _check_python,
    _check_pip,
    _check_node,
    _check_npm,
    _check_port,
    _check_playwright,
    _check_git,
    _check_ffmpeg,
    _check_browser,
    _check_venv,
    _check_frontend_dist,
)


def collect() -> dict:
    """跑一遍全部检查，返回给前端渲染的完整结果。"""
    items: list[dict] = []
    for check in CHECKS:
        try:
            items.append(check())
        except Exception as exc:  # noqa: BLE001 - 单项异常不该让整个检测失败
            # 失败要"往严里算"：不知道炸掉的这项本来是不是必需的，就当必需且不通过，
            # 免得某个检查悄悄报错、启动条件却显示成通过了。
            label = check.__name__.removeprefix("_check_")
            items.append(_item(label, label, True, False, "", "检测出错",
                               current_en="check failed",
                               hint=f"{type(exc).__name__}: {exc}",
                               hint_en=f"{type(exc).__name__}: {exc}"))

    required = [item for item in items if item["required"]]
    optional = [item for item in items if not item["required"]]
    required_passed = sum(1 for item in required if item["ok"])
    optional_passed = sum(1 for item in optional if item["ok"])
    return {
        "ts": time.time(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "python",
        "project_dir": paths.REPO_ROOT,
        "service_running": True,
        "summary": {
            "ok": required_passed == len(required),
            "required_total": len(required),
            "required_passed": required_passed,
            "optional_total": len(optional),
            "optional_passed": optional_passed,
        },
        "items": items,
    }
