"""「图片与视频」查找的界面回归检查（浏览器端到端）。

会自己拉起一个后端（随机端口，不影响正在运行的实例），用无头浏览器打开真实界面，
操作「会话 → 查找聊天记录 → 图片与视频」，然后核对这几条行为：

  1. 打开就停在最新处（滚动条在底部）
  2. 列表顶部有「加载更早」入口
  3. 日期分组升序（旧在上、新在下）
  4. 每个日期分组内部也是升序
  5. 点「加载更早」后条数增加，且新增的是更早的消息
  6. 加载更早时阅读位置不跳（同一张图仍在屏幕原处）
  7. 滚到顶部自动加载更早一页（不用点按钮），位置同样不跳

用法（在项目根目录）：

    .\\venv\\Scripts\\python.exe tools\\verify_media_search_ui.py

可选参数：

    --conv <conv_id>   指定会话（默认挑消息最多的那个有媒体的会话）
    --keep-open        检查完不关闭浏览器窗口（默认无头，关掉）
    --headed           显示浏览器窗口，方便肉眼确认

依赖：本机装有 Edge 或 Chrome（脚本会依次尝试 msedge / chrome / 内置 chromium）。
返回码：0 全部通过；1 有失败项；2 后端启动失败；3 面板设置了密码，需要先登录。
"""
import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台默认 GBK，中文会乱码：切到 UTF-8 并允许替换无法编码的字符。
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 页面里读一份状态：日期分组、每个格子上的时间戳、滚动位置、是否有自动加载入口。
READ_STATE = """() => {
  const sc = document.querySelector('.search-scroll')
  const tiles = [...document.querySelectorAll('.media-tile')]
  const stamp = t => (t.getAttribute('aria-label') || '').match(/\\d{4}年\\d{2}月\\d{2}日 \\d{2}:\\d{2}/)?.[0] || ''
  return {
    dates: [...document.querySelectorAll('.media-group h4')].map(h => h.textContent.trim()),
    stamps: tiles.map(stamp).filter(Boolean),
    tiles: tiles.length,
    scrollTop: Math.round(sc.scrollTop),
    scrollHeight: Math.round(sc.scrollHeight),
    clientHeight: Math.round(sc.clientHeight),
    topButton: !!document.querySelector('.search-more-top'),
    loading: (document.querySelector('.search-status')?.textContent || '').includes('加载中'),
  }
}"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for_server(base: str, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/", timeout=3).read(64)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return True  # 服务起来了，只是需要登录
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return False


def pick_conversation(base: str, conv_id: str | None):
    """挑一个有媒体数据的会话；返回 (conv_id, 名称, 媒体总数)。"""
    conversations = get_json(base + "/api/conversations")["items"]
    if conv_id:
        name = next((c["name"] for c in conversations if c["conv_id"] == conv_id), conv_id)
        total = get_json(f"{base}/api/search?conv_id={urllib.parse.quote(conv_id)}&media_type=media&page=1&page_size=1")["total"]
        return conv_id, name, total
    for conv in conversations[:8]:
        query = urllib.parse.quote(conv["conv_id"])
        total = get_json(f"{base}/api/search?conv_id={query}&media_type=media&page=1&page_size=1")["total"]
        if total >= 60:  # 至少两页，才能测「加载更早」
            return conv["conv_id"], conv["name"], total
    return None, None, 0


async def run_checks(base: str, conv: str, headed: bool, keep_open: bool):
    from playwright.async_api import async_playwright

    results = []

    def check(name, passed, detail=""):
        results.append((name, bool(passed), detail))

    async with async_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = await pw.chromium.launch(channel=channel, headless=not headed)
                break
            except Exception as exc:  # 换下一个浏览器
                last_error = exc
        if browser is None:
            raise RuntimeError(f"找不到可用的浏览器（Edge / Chrome / chromium）: {last_error}")

        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(base + "/", wait_until="domcontentloaded")
        await page.wait_for_selector(".conv-item", timeout=30000)
        await page.click(".conv-item")
        await page.wait_for_selector(".msg-item", timeout=30000)
        await page.click(".search-toggle")
        await page.wait_for_selector(".search-shortcuts", timeout=10000)
        await page.click(".search-shortcuts button:has-text('图片与视频')")
        await page.wait_for_selector(".media-group", timeout=60000)
        await page.wait_for_timeout(1500)

        first = await page.evaluate(READ_STATE)
        dates, stamps = first["dates"], first["stamps"]
        check("打开就停在最新处", first["scrollTop"] + first["clientHeight"] >= first["scrollHeight"] - 2,
              f"scrollTop={first['scrollTop']} + 视口={first['clientHeight']} / 内容高={first['scrollHeight']}")
        check("顶部有「加载更早」入口", first["topButton"], f"已加载 {first['tiles']} 张，{len(dates)} 个日期分组")
        check("日期分组升序（旧在上）", dates == sorted(dates), " → ".join(dates[:3]) + " …")
        check("分组内时间戳升序", stamps == sorted(stamps), f"最上 {stamps[0] if stamps else '?'} → 最下 {stamps[-1] if stamps else '?'}")

        # 手动点「加载更早」：条数变多、新增的更早、阅读位置不跳
        before = await page.evaluate(READ_STATE)
        await page.evaluate("""() => {
          const tile = document.querySelector('.media-tile')
          tile.dataset.probe = '1'
          window.__probeTop = tile.getBoundingClientRect().top
          document.querySelector('.search-more-top').click()
        }""")
        await page.wait_for_function(
            "() => !(document.querySelector('.search-status')?.textContent || '').includes('加载中')",
            timeout=60000,
        )
        await page.wait_for_timeout(900)
        after = await page.evaluate(READ_STATE)
        probe = await page.evaluate("""() => {
          const tile = document.querySelector('[data-probe]')
          return tile ? { top: tile.getBoundingClientRect().top, before: window.__probeTop } : null
        }""")
        drift = abs(probe["top"] - probe["before"]) if probe else 999
        check("点「加载更早」后条数增加", after["tiles"] > before["tiles"], f"{before['tiles']} → {after['tiles']} 张")
        check("新增的是更早的消息", after["stamps"][0] <= before["stamps"][0],
              f"最上方 {before['stamps'][0]} → {after['stamps'][0]}")
        check("加载更早时阅读位置不跳", drift <= 2, f"位移 {drift:.1f}px")

        # 滚到顶部：应当自动续拉更早的一页，且位置不跳
        before_auto = await page.evaluate(READ_STATE)
        await page.evaluate("""() => {
          const scroller = document.querySelector('.search-scroll')
          const tile = document.querySelector('.media-tile')
          scroller.scrollTop = 0            // 先滚到顶，再取基准位置
          tile.dataset.probe2 = '1'
          window.__probe2Top = tile.getBoundingClientRect().top
        }""")
        try:
            await page.wait_for_function(
                f"() => document.querySelectorAll('.media-tile').length > {before_auto['tiles']}",
                timeout=30000,
            )
            auto_loaded = True
        except Exception:
            auto_loaded = False
        await page.wait_for_function(
            "() => !(document.querySelector('.search-status')?.textContent || '').includes('加载中')",
            timeout=60000,
        )
        await page.wait_for_timeout(900)
        after_auto = await page.evaluate(READ_STATE)
        probe2 = await page.evaluate("""() => {
          const tile = document.querySelector('[data-probe2]')
          return tile ? { top: tile.getBoundingClientRect().top, before: window.__probe2Top } : null
        }""")
        drift2 = abs(probe2["top"] - probe2["before"]) if probe2 else 999
        check("滚到顶部自动加载更早一页", auto_loaded and after_auto["tiles"] > before_auto["tiles"],
              f"{before_auto['tiles']} → {after_auto['tiles']} 张")
        check("自动加载后阅读位置不跳", drift2 <= 2, f"位移 {drift2:.1f}px")

        if keep_open or headed:
            await page.wait_for_timeout(4000)
        await browser.close()

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="「图片与视频」查找的界面回归检查")
    parser.add_argument("--conv", help="指定会话 conv_id（默认自动挑消息最多的）")
    parser.add_argument("--keep-open", action="store_true", help="检查完保留浏览器窗口几秒")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[*] 启动后端 {base}（项目目录 {PROJECT_ROOT}）")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_server(base):
            print("[!] 后端启动失败")
            return 2
        try:
            conv, name, total = pick_conversation(base, args.conv)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                print("[!] 控制面板已设置密码：浏览器里需要登录后才有权限。")
                print("    可在面板里临时取消密码，或只用未设密码的实例跑这个检查。")
                return 3
            raise
        if not conv:
            print("[=] 跳过：没有找到媒体消息不少于 60 条的会话")
            return 0
        print(f"[*] 使用会话「{name}」，媒体消息 {total} 条")

        results = asyncio.run(run_checks(base, conv, args.headed, args.keep_open))
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        print("[*] 后端已停止")

    print("\n================ 「图片与视频」界面检查 ================")
    for title, passed, detail in results:
        print(f"{'通过' if passed else '失败'} | {title} | {detail}")
    failed = [r for r in results if not r[1]]
    print("======================================================")
    print(f"共 {len(results)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
