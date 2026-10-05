"""核对 Playwright 的浏览器内核，缺了就下载 —— 启动脚本调用，也可以手动跑。

    venv\\Scripts\\python.exe tools\\ensure_playwright_browser.py    # 缺啥补啥
    python tools/ensure_playwright_browser.py --check-only          # 只看，不联网

为什么要有这一步：pip 装的只是 playwright 这个 Python 包，浏览器本体要另外下载；
playwright 升级后要用的内核版本会变，旧内核随即作废，那时采集、导入 Cookie、聊天长图
都会报「Executable doesn't exist at ...\\chromium_headless_shell-<版本>\\...」。
所以 `start.ps1`（双击「启动服务（双击）.bat」走的就是它）和 `start.sh` 每次启动
都跑一遍：已经装好时只是核对一下版本，很快；缺了才下载。

下载先走国内镜像（和 pip / npm 那两处一个思路），失败再回官方 CDN。用户自己设了
``PLAYWRIGHT_DOWNLOAD_HOST`` 就听用户的，不覆盖。装不上不拦启动，只返回 1 并说清楚
怎么手动补 —— 服务照样能起来看已有记录，只是要用浏览器的功能会失败。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# 直接跑 `python tools/ensure_playwright_browser.py` 时 sys.path[0] 是 tools/，
# 项目自己的模块要手动挂上。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common import playwright_browsers as browsers  # noqa: E402（必须在 sys.path 调整之后）

#: 国内镜像（cdn.npmmirror.com 镜像了 playwright 的整个构建目录）
MIRROR_HOST = "https://cdn.npmmirror.com/binaries/playwright"

DOWNLOAD_ENV = "PLAYWRIGHT_DOWNLOAD_HOST"


def playwright_installed() -> bool:
    """venv 里有没有 playwright 这个包（没有的话先装 Python 依赖）。"""
    try:
        import playwright  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


def download(host: str | None) -> bool:
    """跑一次 ``playwright install chromium``；返回是否成功。"""
    env = dict(os.environ)
    if host:
        env[DOWNLOAD_ENV] = host
    try:
        return subprocess.call(
            [sys.executable, "-m", "playwright", "install", "chromium"], env=env
        ) == 0
    except OSError as error:
        print(f"[!] 调不动 playwright：{error}")
        return False


def ensure(check_only: bool = False) -> int:
    """0 = 内核齐了，1 = 还缺（没下载或下载失败）。"""
    if not playwright_installed():
        print("[!] 还没装 playwright 这个包，先装 Python 依赖：pip install -r requirements.txt")
        return 1

    required = browsers.required_browsers()
    if not required:
        print("[!] 读不到 playwright 的浏览器清单（browsers.json），跳过核对")
        return 1

    missing = browsers.missing_browsers()
    if not missing:
        print(f"[+] Playwright 浏览器内核已就绪：{'、'.join(browsers.installed_browsers())}")
        return 0

    print(f"[*] 缺少 Playwright 浏览器内核：{'、'.join(missing)}")
    if check_only:
        print(f"[*] 手动补装：{browsers.manual_hint()}")
        return 1

    print("[*] 正在下载（约 300 MB，占盘约 700 MB，第一次会慢一些）……")
    host = os.environ.get(DOWNLOAD_ENV)          # 用户自己指定了就只按他说的下
    if host:
        download(host)
    else:
        print(f"[*] 先用国内镜像：{MIRROR_HOST}")
        if not download(MIRROR_HOST) or browsers.missing_browsers():
            print("[*] 镜像没成，换官方 CDN 再试一次……")
            download(None)

    if browsers.missing_browsers():
        print(f"[!] 还是缺：{'、'.join(browsers.missing_browsers())}")
        print(f"[!] 可以稍后手动补装：{browsers.manual_hint()}")
        return 1
    print(f"[+] Playwright 浏览器内核已装好：{'、'.join(browsers.installed_browsers())}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="缺了就下载 Playwright 浏览器内核")
    parser.add_argument("--check-only", action="store_true", help="只报告缺什么，不下载")
    args = parser.parse_args(argv)
    return ensure(check_only=args.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
