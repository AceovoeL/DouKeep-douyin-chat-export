#!/usr/bin/env python3
"""打开本机浏览器扫码登录抖音，把登录态存进 config/browser_profile/。"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from common import paths  # noqa: E402  (必须在 sys.path 调整之后)

PROFILE_DIR = paths.BROWSER_PROFILE
DOUYIN_URL = "https://www.douyin.com/"


async def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    print("[*] 启动浏览器，请在弹出的窗口中扫码登录...")

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    page = context.pages[0] if context.pages else await context.new_page()
    # Douyin's heavy JS / bot-detection can stall "domcontentloaded" well past
    # 30s even though the page is already usable for scanning. Wait only for the
    # navigation to commit, and don't let a slow load abort the login flow — the
    # QR UI renders client-side and the cookie poll below gives it 5 minutes.
    try:
        await page.goto(DOUYIN_URL, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[!] 页面加载较慢，继续等待登录: {e}")

    print("[*] 等待登录... (登录成功后自动关闭)")
    for _ in range(300):  # 5 min timeout
        # Read the cookie jar instead of document.cookie: sessionid is httpOnly
        # (invisible to document.cookie) and context.cookies() also survives the
        # page navigations that destroy the JS execution context mid-poll.
        try:
            cookies = await context.cookies()
        except Exception:
            await asyncio.sleep(1)
            continue
        if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
            print("[+] 登录成功！")
            break
        await asyncio.sleep(1)
    else:
        print("[-] 登录超时")
        await context.close()
        await pw.stop()
        return

    await context.close()
    await pw.stop()
    print(f"[+] 登录态已保存到 {PROFILE_DIR}")
    print("[*] 回到控制面板继续采集即可。")


if __name__ == "__main__":
    asyncio.run(main())
