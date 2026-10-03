"""Probe v3: dig into VMOK IM SDK module and compare URL variants."""
import asyncio
import base64
import json
import os
import sqlite3
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.web_scraper import WebChatScraper


def fetch_with_referer(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(), dict(r.headers)


async def main():
    db = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chat.db"))
    row = db.execute("SELECT msg_id, raw_data FROM messages WHERE msg_type=3 LIMIT 1").fetchone()
    data = json.loads(row[1])
    cj = json.loads(data["content_json"])
    ru = cj["resource_url"]
    skey = ru["skey"]
    print(f"[*] skey={skey} expected_md5={ru['md5']} expected_size={ru['data_size']}")

    # Compare all URL variants server-side
    print("\n[*] Comparing URL variants...")
    for key in ("large_url_list", "medium_url_list", "origin_url_list", "thumb_url_list"):
        urls = ru.get(key) or []
        if not urls:
            print(f"  {key}: empty")
            continue
        for u in urls[:1]:
            try:
                body, hdrs = fetch_with_referer(u)
                print(f"  {key}[0]: status=OK size={len(body)}  ctype={hdrs.get('Content-Type')}  first16={body[:16].hex()}")
            except Exception as e:
                print(f"  {key}[0]: error {e}")

    scraper = WebChatScraper()
    await scraper.launch()
    if not await scraper.wait_for_login():
        return

    page = scraper.page

    print("\n[*] Navigating to /chat to load IM SDK...")
    await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
    await asyncio.sleep(8)

    # Dig into VMOK / SDKRuntime
    print("\n[*] VMOK module inspection...")
    vmok_info = await page.evaluate(r"""() => {
        const out = {};
        for (const k of Object.keys(window)) {
            if (k.startsWith('__VMOK_') || k.startsWith('@pc-im')) {
                try {
                    const v = window[k];
                    out[k] = {
                        type: typeof v,
                        keys: typeof v === 'object' ? Object.keys(v).slice(0, 30) : null,
                    };
                    if (typeof v === 'object' && v.get) {
                        try {
                            const got = v.get();
                            out[k].got_keys = got && typeof got === 'object' ? Object.keys(got).slice(0, 50) : String(got).slice(0, 200);
                        } catch (e) { out[k].got_err = e.message; }
                    }
                } catch (e) { out[k] = { err: e.message }; }
            }
        }
        return out;
    }""")
    print(json.dumps(vmok_info, indent=2, ensure_ascii=False))

    # SDKRuntime module list
    print("\n[*] SDKRuntime modules...")
    sdkrt = await page.evaluate(r"""() => {
        if (!window.SDKRuntime) return null;
        const rt = window.SDKRuntime;
        const info = {};
        for (const k of Object.keys(rt)) {
            try {
                const v = rt[k];
                info[k] = typeof v === 'object' && v ? ('obj:[' + Object.keys(v).slice(0,10).join(',') + ']') : typeof v;
            } catch (e) { info[k] = 'err'; }
        }
        return info;
    }""")
    print(json.dumps(sdkrt, indent=2, ensure_ascii=False))

    # Try to introspect VMOK module further
    print("\n[*] Detailed __VMOK_@pc-im/im module inspection...")
    detail = await page.evaluate(r"""() => {
        const vmod = window['__VMOK_@pc-im/im:1.0.0.562__'];
        if (!vmod) return null;
        const out = { keys: Object.keys(vmod) };
        try {
            const g = vmod.get;
            out.getType = typeof g;
            out.getStr = String(g).slice(0, 400);
        } catch (e) { out.getErr = e.message; }
        try {
            // Try to call get without args
            const got = vmod.get && vmod.get();
            if (got) {
                out.gotKeys = Object.keys(got).slice(0, 80);
                // Look for crypto/image functions
                out.gotInteresting = Object.keys(got).filter(k => {
                    const lk = k.toLowerCase();
                    return lk.includes('decrypt') || lk.includes('crypto') ||
                           lk.includes('image') || lk.includes('pic') ||
                           lk.includes('media') || lk.includes('fetch') ||
                           lk.includes('download') || lk.includes('aes');
                });
            }
        } catch (e) { out.getCallErr = e.message; }
        return out;
    }""")
    print(json.dumps(detail, indent=2, ensure_ascii=False))

    # Inspect CryptoJS
    print("\n[*] CryptoJS algo list...")
    cryptojs_info = await page.evaluate(r"""() => {
        if (!window.CryptoJS) return null;
        const cj = window.CryptoJS;
        return {
            top: Object.keys(cj),
            algo: cj.algo ? Object.keys(cj.algo) : null,
            mode: cj.mode ? Object.keys(cj.mode) : null,
            pad: cj.pad ? Object.keys(cj.pad) : null,
        };
    }""")
    print(json.dumps(cryptojs_info, indent=2, ensure_ascii=False))

    # Search loaded JS modules' source for image/decrypt patterns
    print("\n[*] Searching loaded scripts for image-decrypt patterns...")
    scripts = await page.evaluate(r"""() => {
        const out = [];
        for (const s of document.scripts) {
            if (s.src) out.push(s.src);
        }
        return out;
    }""")
    print(f"  {len(scripts)} script tags loaded")
    for s in scripts[:5]:
        print(f"    {s[:120]}")

    # Try clicking on an existing image to see what API the SDK uses
    print("\n[*] Looking for img elements in messages, capturing their src origins...")
    img_origins = await page.evaluate(r"""() => {
        const imgs = document.querySelectorAll('img');
        const origins = {};
        for (const i of imgs) {
            try {
                const u = new URL(i.src || i.currentSrc);
                origins[u.host] = (origins[u.host] || 0) + 1;
            } catch {}
        }
        return origins;
    }""")
    print(json.dumps(img_origins, indent=2, ensure_ascii=False))

    await scraper.context.close()


if __name__ == "__main__":
    asyncio.run(main())
