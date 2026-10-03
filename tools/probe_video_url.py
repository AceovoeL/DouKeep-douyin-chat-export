"""Intercept Blob constructions and save the JS source of any text/javascript blob
that's >100KB — that's the decryption Worker."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor.web_scraper import WebChatScraper

TARGET_TEXT = "我变成狗了"


async def main():
    s = WebChatScraper()
    await s.launch()
    await s.wait_for_login()
    page = s.page

    await page.add_init_script(
        r"""
        window.__cap__ = { worker_sources: [], errs: [] };

        // Hook Blob constructor — keep the JS source if it's text/javascript
        try {
        const _Blob = window.Blob;
        function NewBlob(parts, opts) {
            try {
                const type = (opts && opts.type) || '';
                if (type.startsWith('text/javascript') || type === '') {
                    let totalLen = 0;
                    for (const p of (parts || [])) {
                        if (typeof p === 'string') totalLen += p.length;
                        else if (p && p.byteLength) totalLen += p.byteLength;
                    }
                    if (totalLen > 10000 && totalLen < 1_000_000) {
                        // Likely a worker source
                        let source = '';
                        for (const p of (parts || [])) {
                            if (typeof p === 'string') source += p;
                            else if (p && p.byteLength) {
                                // decode bytes as text
                                source += new TextDecoder().decode(p);
                            }
                        }
                        window.__cap__.worker_sources.push({
                            total: totalLen, type,
                            head: source.slice(0, 200), tail: source.slice(-200),
                            full: source  // 不截断
                        });
                    }
                }
            } catch (e) { window.__cap__.errs.push('blob hook: ' + e); }
            return new _Blob(parts, opts);
        }
        NewBlob.prototype = _Blob.prototype;
        window.Blob = NewBlob;
        } catch (e) { window.__cap__.errs.push('blob: ' + e); }
        """
    )

    print("[*] open + switch conv ...", flush=True)
    await page.goto("https://www.douyin.com/chat", wait_until="commit", timeout=30000)
    await asyncio.sleep(8)
    await page.locator('[class*="conversationConversationItem"]').first.click(force=True, timeout=10000)
    await asyncio.sleep(8)

    print(f"[*] scroll to target ...", flush=True)
    await page.mouse.move(700, 400)
    for _ in range(300):
        ok = await page.evaluate(
            r"""(n) => {
                const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let nd;
                while (nd = tw.nextNode()) {
                    if (nd.nodeValue?.includes(n)) {
                        const el = nd.parentElement;
                        if (el && el.getBoundingClientRect().left > 350) return true;
                    }
                }
                return false;
            }""", TARGET_TEXT
        )
        if ok: break
        await page.mouse.wheel(0, -300)
        await asyncio.sleep(0.4)
    await page.evaluate(
        r"""(n) => {
            const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let nd;
            while (nd = tw.nextNode()) {
                if (nd.nodeValue?.includes(n)) {
                    nd.parentElement.scrollIntoView({block:'center'}); return;
                }
            }
        }""", TARGET_TEXT
    )
    await asyncio.sleep(3)

    box = await page.evaluate(
        r"""() => {
            const b = document.querySelector('[class*="MessageItemVideo"][class*="videoBox"]');
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return { x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) };
        }"""
    )

    # 不要 reset —— worker 是页面加载时就构造的
    pre_count = await page.evaluate("window.__cap__.worker_sources.length")
    print(f"[*] 进入下载前已捕获 {pre_count} 个候选", flush=True)
    await page.mouse.click(box["x"], box["y"], button="right")
    await asyncio.sleep(1)
    dl = await page.evaluate(
        r"""() => {
            const popup = document.querySelector('[class*="MessageOperatePopWindow"][class*="wrapper"]');
            if (!popup) return null;
            let t = null;
            popup.querySelectorAll('[class*="MessageOperatePopBody"][class*="buttonItem"]').forEach(el => {
                if (el.textContent.trim() === '下载') t = el;
            });
            if (!t) return null;
            const r = t.getBoundingClientRect();
            return { x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) };
        }"""
    )
    print(f"[*] click 下载", flush=True)
    await page.mouse.click(dl["x"], dl["y"])
    await asyncio.sleep(10)

    cap = await page.evaluate("window.__cap__")
    print(f"\n=== {len(cap['worker_sources'])} blob 候选 ===")
    for i, w in enumerate(cap['worker_sources']):
        print(f"\n--- worker[{i}] size={w['total']} type={w['type']!r} ---")
        print(f"head: {w['head'][:300]!r}")
        print(f"tail: {w['tail'][:150]!r}")
        # 保存
        with open(f"/tmp/worker_{i}.js", "w") as f:
            f.write(w['full'])
        print(f"  → /tmp/worker_{i}.js saved {len(w['full'])} bytes")

    print(f"\n=== errs: {cap['errs']} ===")
    await s.close()


if __name__ == "__main__":
    asyncio.run(main())
