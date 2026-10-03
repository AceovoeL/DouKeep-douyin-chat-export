"""Probe: inspect conversationMap entries and test loadMoreConversations()."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.web_scraper import WebChatScraper


async def main():
    scraper = WebChatScraper()
    await scraper.launch()
    await scraper.wait_for_login()
    await scraper.navigate_to_chat()
    page = scraper.page

    await asyncio.sleep(4)

    # Step 1: sample one entry from conversationMap
    sample = await page.evaluate("""() => {
        const cs = window.conversationStore;
        if (!cs || !cs.conversationMap) return null;
        const m = cs.conversationMap;
        const raw = m.data_;
        if (!raw) return { _note: 'no data_', keys: Object.keys(m) };
        const entries = Array.from(raw.entries());
        if (entries.length === 0) return { _note: 'empty' };

        const [firstKey, firstValBox] = entries[0];
        const firstVal = firstValBox && firstValBox.value_ !== undefined ? firstValBox.value_ : firstValBox;

        // Enumerate fields with types
        const fields = {};
        for (const k of Object.keys(firstVal || {})) {
            try {
                const v = firstVal[k];
                if (v === null || v === undefined) { fields[k] = 'null'; continue; }
                const t = typeof v;
                if (t === 'function') { fields[k] = 'function'; continue; }
                if (t !== 'object') { fields[k] = `${t}: ${JSON.stringify(v).substring(0,150)}`; continue; }
                if (Array.isArray(v)) { fields[k] = `array[${v.length}] first=${JSON.stringify(v[0]).substring(0,100)}`; continue; }
                fields[k] = `obj keys=[${Object.keys(v).slice(0,10).join(',')}]`;
            } catch(e) { fields[k] = 'err'; }
        }
        return { key: String(firstKey), totalCount: entries.length, fields };
    }""")
    print("=== Sample entry from conversationMap ===")
    print(json.dumps(sample, ensure_ascii=False, indent=2))

    # Step 2: check hasMore + try loadMoreConversations
    initial = await page.evaluate("""() => ({
        hasMore: window.conversationStore?.hasMore,
        isLoading: window.conversationStore?.isLoading,
        size: window.conversationStore?.conversationMap?.data_?.size,
    })""")
    print(f"\n=== Before loadMore: {initial}")

    for i in range(30):
        status = await page.evaluate("""async () => {
            const cs = window.conversationStore;
            if (!cs) return { err: 'no store' };
            if (!cs.hasMore) return { done: true, size: cs.conversationMap?.data_?.size };
            if (cs.isLoading) return { loading: true, size: cs.conversationMap?.data_?.size };
            try {
                const ret = await cs.loadMoreConversations();
                return { called: true, ret: typeof ret, size: cs.conversationMap?.data_?.size, hasMore: cs.hasMore };
            } catch(e) {
                return { err: e.message };
            }
        }""")
        print(f"  [loop {i}] {status}")
        if status.get("done"):
            break
        await asyncio.sleep(1.2)

    # Step 3: dump final size + first 3 entries (key fields only)
    final = await page.evaluate("""() => {
        const cs = window.conversationStore;
        const m = cs?.conversationMap?.data_;
        if (!m) return null;
        const out = { size: m.size, samples: [] };
        let i = 0;
        for (const [k, boxed] of m.entries()) {
            if (i++ >= 3) break;
            const v = boxed?.value_ !== undefined ? boxed.value_ : boxed;
            out.samples.push({
                key: String(k),
                conversationId: v?.conversationId || v?.conversation_id,
                conversationShortId: v?.conversationShortId || v?.conversation_short_id,
                conversationType: v?.conversationType,
                lastMessageTime: v?.lastMessageTime || v?.updatedTime,
                memberCount: v?.memberCount,
                unreadCount: v?.unreadCount,
                name: v?.name,
            });
        }
        return out;
    }""")
    print(f"\n=== Final conversationMap ===")
    print(json.dumps(final, ensure_ascii=False, indent=2))

    # Step 4: probe participants map shape
    parts = await page.evaluate("""() => {
        const cs = window.conversationStore;
        const pm = cs?.participantMapWithConversationId;
        if (!pm || !pm.data_) return null;
        const entries = Array.from(pm.data_.entries());
        if (!entries.length) return { empty: true };
        const [k, boxed] = entries[0];
        const v = boxed?.value_ !== undefined ? boxed.value_ : boxed;
        return { size: entries.length, sampleKey: String(k), sampleValKeys: Object.keys(v || {}).slice(0,20), sampleFirstVal: Array.isArray(v) && v.length ? Object.keys(v[0]).slice(0,20) : null };
    }""")
    print(f"\n=== participantMapWithConversationId sample ===")
    print(json.dumps(parts, ensure_ascii=False, indent=2))

    await scraper.context.close()


if __name__ == "__main__":
    asyncio.run(main())
