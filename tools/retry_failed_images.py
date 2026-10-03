"""Retry: re-fetch URLs for image messages whose URL arrays were empty.

Strategy:
1. Load IM SDK by going to /chat
2. For each conv with failed image messages, navigate to that conv
3. Wait for SDK to load messages around the target msg
4. Read messageStore/conversationStore to get fresh URLs
5. If origin_url_list is now populated, decrypt + save
"""
import asyncio
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.web_scraper import WebChatScraper, _save_image


async def main():
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chat.db")
    db = sqlite3.connect(db_path)

    # Failed messages grouped by conv
    rows = db.execute("""
        SELECT msg_id, conv_id, raw_data FROM messages
        WHERE msg_type=3 AND (media_local_path IS NULL OR media_local_path = '')
    """).fetchall()
    by_conv = defaultdict(list)
    for msg_id, conv_id, raw in rows:
        data = json.loads(raw)
        cj = json.loads(data["content_json"])
        ru = cj.get("resource_url", {})
        by_conv[conv_id].append({
            "msg_id": msg_id,
            "server_id": msg_id.replace("srv_", ""),
            "oid": ru.get("oid"),
            "skey": ru.get("skey"),
        })
    print(f"[*] {sum(len(v) for v in by_conv.values())} failed images across {len(by_conv)} conversations")

    scraper = WebChatScraper()
    await scraper.launch()
    if not await scraper.wait_for_login():
        return

    page = scraper.page
    print("[*] Opening /chat ...")
    await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    img_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "media", "images")
    os.makedirs(img_dir, exist_ok=True)

    total_ok = 0
    total_fail = 0
    for conv_id, targets in by_conv.items():
        print(f"\n[*] Conv {conv_id[:40]}... ({len(targets)} targets)")
        try:
            # Use conversationStore to switch to this conversation
            ok = await page.evaluate(f"""(convId) => {{
                const cs = window.conversationStore;
                if (!cs) return 'no store';
                if (typeof cs.setCurConversation === 'function') {{
                    cs.setCurConversation(convId);
                    return 'switched';
                }}
                return 'no setCurConversation';
            }}""", conv_id)
            print(f"   switch: {ok}")
            await asyncio.sleep(4)  # let SDK load messages

            # Scroll up a bit to load more messages from the relevant time range
            # (We can't easily target a specific time, so just load enough)
            await page.evaluate("""() => {
                const sel = 'div[class*="messageMessageListlist"]';
                const list = document.querySelector(sel);
                if (list) {
                    const scroller = list.querySelector('[style*="overflow"]') || list;
                    for (let i = 0; i < 30; i++) scroller.scrollTop = 0;
                }
            }""")
            await asyncio.sleep(3)

            # For each target msg, find fresh URLs from messageStore
            for t in targets:
                fresh = await page.evaluate(f"""(serverId) => {{
                    // Try multiple stores
                    const stores = ['messageStore', 'conversationStore'];
                    for (const sn of stores) {{
                        const s = window[sn];
                        if (!s) continue;
                        // Walk all maps
                        for (const k of Object.keys(s)) {{
                            const v = s[k];
                            if (!v || typeof v !== 'object') continue;
                            const dataMap = v.data_ || v;
                            if (!dataMap || typeof dataMap.entries !== 'function') continue;
                            try {{
                                for (const [, boxed] of dataMap.entries()) {{
                                    const val = boxed?.value_ ?? boxed;
                                    if (!val) continue;
                                    // Direct match
                                    if (val.serverId === serverId || val.server_id === serverId) {{
                                        const cj = typeof val.content === 'string' ?
                                            (() => {{ try {{ return JSON.parse(val.content); }} catch {{ return null; }} }})() : null;
                                        if (cj?.resource_url) return {{ found: sn + '.' + k, resource_url: cj.resource_url }};
                                    }}
                                    // Sub-list match (conversations contain messages)
                                    if (Array.isArray(val.messageList)) {{
                                        for (const m of val.messageList) {{
                                            if (m.serverId === serverId || m.server_id === serverId) {{
                                                try {{ const cj = JSON.parse(m.content); if (cj?.resource_url) return {{ found: sn+'.'+k+'.list', resource_url: cj.resource_url }}; }} catch {{}}
                                            }}
                                        }}
                                    }}
                                }}
                            }} catch {{}}
                        }}
                    }}
                    return null;
                }}""", t["server_id"])

                if not fresh:
                    print(f"   [-] {t['msg_id']}: SDK 仍未加载")
                    total_fail += 1
                    continue

                ru = fresh.get("resource_url") or {}
                origin = (ru.get("origin_url_list") or [None])[0]
                if not origin:
                    print(f"   [-] {t['msg_id']}: SDK 也没给 URL（{fresh.get('found')}）")
                    total_fail += 1
                    continue

                print(f"   [+] {t['msg_id']}: 拿到新 URL，开始解密...")
                try:
                    rel = _save_image(origin, t["skey"], t["msg_id"], img_dir)
                    if rel:
                        db.execute("UPDATE messages SET media_local_path = ? WHERE msg_id = ?", (rel, t["msg_id"]))
                        db.commit()
                        print(f"       saved: {rel}")
                        total_ok += 1
                    else:
                        print(f"       save 失败")
                        total_fail += 1
                except Exception as e:
                    print(f"       error: {e}")
                    total_fail += 1
        except Exception as e:
            print(f"   会话切换失败: {e}")

    print(f"\n[✓] Total: {total_ok} 成功，{total_fail} 失败")
    await scraper.context.close()


if __name__ == "__main__":
    asyncio.run(main())
