"""Diagnostic: enumerate ALL network traffic on /chat page load.

Reuses WebChatScraper.launch() and wait_for_login() so auth state matches
the real scrape. Attaches request/response/websocket listeners BEFORE
navigate_to_chat() fires any SDK calls.
"""
import asyncio
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.web_scraper import WebChatScraper

DUMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "api_dumps")
CONV_HINTS = ("conversation", "inbox", "recent", "session", "dialog", "participants", "get_by_", "get_msg", "message_list")


async def main():
    os.makedirs(DUMP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = os.path.join(DUMP_DIR, f"summary_{stamp}.txt")
    summary = open(summary_path, "w", encoding="utf-8")

    def log(msg):
        print(msg, flush=True)
        summary.write(msg + "\n")
        summary.flush()

    all_requests = []
    ws_frames = []

    scraper = WebChatScraper()
    await scraper.launch()
    await scraper.wait_for_login()

    page = scraper.page

    async def on_response(resp):
        url = resp.url
        if not ("douyin.com" in url or "bytedance" in url or "zijieapi" in url):
            return
        rtype = resp.request.resource_type
        if rtype in ("image", "font", "stylesheet", "media"):
            return
        try:
            body = await resp.body()
        except Exception:
            body = b""

        u = urlparse(url.split("?", 1)[0])
        tail = "/".join(u.path.rsplit("/", 3)[-3:])
        short = f"{u.hostname}/{tail}"

        entry = {
            "short": short,
            "url": url,
            "method": resp.request.method,
            "status": resp.status,
            "resp_size": len(body),
            "rtype": rtype,
        }
        all_requests.append(entry)

        hint_match = any(h in url.lower() for h in CONV_HINTS)
        # Always log anything >500B with HINT marker
        if hint_match or len(body) > 2000:
            marker = " [HINT]" if hint_match else ""
            log(f"  {resp.request.method} {short}{marker}  resp={len(body)}B  type={rtype}  status={resp.status}")

        if hint_match:
            safe = short.replace("/", "_")[:100]
            out = os.path.join(DUMP_DIR, f"{stamp}__{safe}__resp.bin")
            with open(out, "wb") as f:
                f.write(body)
            try:
                req_body = resp.request.post_data_buffer
                if req_body:
                    out_req = os.path.join(DUMP_DIR, f"{stamp}__{safe}__req.bin")
                    with open(out_req, "wb") as f:
                        f.write(req_body)
            except Exception:
                pass

    def on_websocket(ws):
        log(f"  [WS] connected: {ws.url[:120]}")
        def on_frame_recv(payload):
            ws_frames.append({"dir": "recv", "url": ws.url, "payload": payload})
        def on_frame_sent(payload):
            ws_frames.append({"dir": "send", "url": ws.url, "payload": payload})
        ws.on("framereceived", on_frame_recv)
        ws.on("framesent", on_frame_sent)

    page.on("response", lambda r: asyncio.create_task(on_response(r)))
    page.on("websocket", on_websocket)

    log(f"[*] Listeners attached. Navigating to chat ...")
    await scraper.navigate_to_chat()
    await asyncio.sleep(5)

    # Check login state now
    login_info = await page.evaluate("""() => {
        const uis = window.userInfoStore;
        const cs = window.conversationStore;
        return {
            hasUserStore: !!uis,
            loginUid: uis?.curLoginUserInfo?.uid || null,
            loginNickname: uis?.curLoginUserInfo?.nickname || null,
            hasConvStore: !!cs,
            convStoreTopLevelKeys: cs ? Object.keys(cs).slice(0, 60) : [],
        };
    }""")
    log(f"[*] Login: {login_info.get('loginNickname')} (uid={login_info.get('loginUid')})")
    log(f"[*] conversationStore keys: {login_info.get('convStoreTopLevelKeys')}")

    # Probe conversationStore shape
    cs_probe = await page.evaluate("""() => {
        const cs = window.conversationStore;
        if (!cs) return null;
        const out = {};
        for (const key of Object.keys(cs)) {
            try {
                const v = cs[key];
                if (v === null || v === undefined) { out[key] = 'null'; continue; }
                const t = typeof v;
                if (t === 'function') { out[key] = 'function'; continue; }
                if (t !== 'object') { out[key] = `${t}: ${String(v).substring(0, 80)}`; continue; }
                if (Array.isArray(v)) { out[key] = `array[${v.length}]`; continue; }
                // MobX observable map?
                if (v.data_ && typeof v.data_.size === 'number') {
                    out[key] = `MobXMap(size=${v.data_.size})`;
                    continue;
                }
                if (typeof v.size === 'number' && typeof v.entries === 'function') {
                    out[key] = `Map(size=${v.size})`;
                    continue;
                }
                out[key] = `object(keys=${Object.keys(v).slice(0,10).join(',')})`;
            } catch(e) { out[key] = 'err'; }
        }
        return out;
    }""")
    log(f"[*] conversationStore shape:")
    for k, v in (cs_probe or {}).items():
        log(f"    {k}: {v}")

    log(f"[*] Waiting 20s for SDK traffic ...")
    await asyncio.sleep(20)

    log(f"[*] Scrolling conv list ...")
    try:
        await page.evaluate("""() => {
            const list = document.querySelector('div[class*="conversationConversationListwrapper"]');
            if (!list) return;
            const scrollable = list.querySelector('[style*="overflow"]') || list;
            for (let i = 0; i < 15; i++) scrollable.scrollTop += 500;
        }""")
    except Exception as e:
        log(f"  [!] scroll failed: {e}")
    await asyncio.sleep(8)

    # Dump all unique endpoints >500B
    log(f"\n[+] Total non-static responses from douyin/bytedance/zijieapi: {len(all_requests)}")
    log(f"[+] WebSocket frames: {len(ws_frames)}")

    distinct = {}
    for e in all_requests:
        distinct.setdefault(e["short"], []).append(e)
    log(f"[+] Distinct endpoints: {len(distinct)}")
    for short, entries in sorted(distinct.items(), key=lambda kv: -max(e["resp_size"] for e in kv[1])):
        sizes = [e["resp_size"] for e in entries]
        biggest = max(sizes)
        if biggest < 500:
            continue
        log(f"    {entries[0]['method']:4}  {short}  count={len(entries)}  sizes={sizes}")

    # Save WebSocket payloads
    if ws_frames:
        log(f"\n[+] WebSocket frame sizes:")
        by_url_dir = {}
        for f in ws_frames:
            key = (f["url"][:80], f["dir"])
            by_url_dir.setdefault(key, []).append(f["payload"])
        for (url, direction), payloads in by_url_dir.items():
            sizes = [len(p) if isinstance(p, (bytes, str)) else 0 for p in payloads]
            log(f"    {direction} {url}  count={len(payloads)}  sizes={sizes[:30]}")
        # Save biggest few frames
        big = sorted([f for f in ws_frames], key=lambda f: -(len(f["payload"]) if isinstance(f["payload"], (bytes, str)) else 0))[:10]
        for i, f in enumerate(big):
            payload = f["payload"]
            if isinstance(payload, str):
                data = payload.encode("utf-8", errors="replace")
            elif isinstance(payload, bytes):
                data = payload
            else:
                continue
            if len(data) < 200:
                continue
            out = os.path.join(DUMP_DIR, f"{stamp}__ws_{f['dir']}_{i}.bin")
            with open(out, "wb") as fp:
                fp.write(data)
            log(f"    saved {out} ({len(data)}B)")

    await scraper.context.close()
    summary.close()
    print(f"\n[✓] Summary written to {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
