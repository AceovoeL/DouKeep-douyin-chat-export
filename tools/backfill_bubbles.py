#!/usr/bin/env python3
"""补下载聊天气泡装饰，或分析一张气泡图。

用法：
    python tools/backfill_bubbles.py --scan
    python tools/backfill_bubbles.py --download
    python tools/backfill_bubbles.py --bubble-id 1234567890123456789 --download
    python tools/backfill_bubbles.py --analyze data/media/bubbles/xxx/self.png

说明：
- 气泡图里的 FlexData 自带九宫格切片和文字安全区，优先读它；
- 没有 FlexData 的图才按透明通道估算，并标 estimated（可能不准）；
- 素材只写 data/media/bubbles/，不修改聊天消息。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass

from common import bubble_assets, paths  # noqa: E402


def _scan_ids(limit: int = 0) -> dict[str, int]:
    """从 message_packets 里统计出现过哪些 bubble_id（只读，不联网）。"""
    from common.db import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT fields FROM message_packets WHERE fields LIKE '%a:chat_bubble%'"
        ).fetchall()
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for row in rows:
        try:
            fields = json.loads(row[0] or "{}")
        except (ValueError, TypeError):
            continue
        bubble_id = bubble_assets.bubble_id_from_fields(fields)
        if bubble_id:
            counts[bubble_id] = counts.get(bubble_id, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit:
        items = items[:limit]
    return dict(items)


def _print_scan(counts: dict[str, int]) -> None:
    if not counts:
        print("没有在 message_packets 里找到气泡装饰。")
        return
    print(f"共发现 {len(counts)} 种气泡装饰（括号里是消息条数）：")
    for bubble_id, count in counts.items():
        print(f"  {bubble_id}  ({count})")


def _analyze(path: str, scale: float) -> int:
    if not os.path.isfile(path):
        print(f"找不到图片：{path}")
        return 1
    meta = bubble_assets.analyze_local_image(path, scale=scale)
    if not meta:
        print("这张图分析不了（格式不认识、尺寸异常，或者完全没有不透明像素）。")
        return 1
    print("分析结果（可直接填进 data/media/bubbles/index.json 的 self/guest）：")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if meta.get("estimated"):
        print("\n注意：这张图没有 FlexData，上面是透明通道估算值，可能压到装饰。")
    return 0


async def _download(bubble_ids: list[str]) -> int:
    from extractor.web_scraper import WebChatScraper

    scraper = WebChatScraper()
    await scraper.launch()
    try:
        await scraper.navigate_to_chat()
        catalog = bubble_assets.load_catalog()
        pending = [item for item in bubble_ids if item not in catalog["bubbles"]]
        if not pending:
            print("这些气泡都已经下载过了。")
            return 0
        print(f"准备补 {len(pending)} 个气泡装饰...")
        await scraper._ensure_bubble_assets(pending)
        return 0
    finally:
        await scraper.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="聊天气泡装饰回填 / 分析")
    parser.add_argument("--scan", action="store_true", help="只扫描数据库，不联网")
    parser.add_argument("--download", action="store_true", help="下载并生成配置")
    parser.add_argument("--bubble-id", action="append", default=[], help="只处理指定气泡，可重复")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个，0 表示全部")
    parser.add_argument("--analyze", metavar="图片路径", help="分析一张本地气泡图")
    parser.add_argument("--scale", type=float, default=bubble_assets.BUBBLE_RENDER_SCALE,
                        help="原图到显示尺寸的倍数，默认 3")
    args = parser.parse_args()

    if args.analyze:
        return _analyze(args.analyze, args.scale)

    requested = [item for item in args.bubble_id if bubble_assets.is_valid_bubble_id(item)]
    invalid = [item for item in args.bubble_id if item not in requested]
    if invalid:
        print("这些 bubble_id 不合法，已忽略：" + "、".join(invalid))

    counts = _scan_ids(limit=args.limit)
    _print_scan(counts)

    if not args.download:
        if not args.scan:
            print("\n加 --download 才会联网下载；加 --analyze 可分析单张图片。")
        return 0

    ids = requested or list(counts)
    if args.limit and len(ids) > args.limit:
        ids = ids[:args.limit]
    if not ids:
        print("没有需要下载的气泡。")
        return 0
    return asyncio.run(_download(ids))


if __name__ == "__main__":
    raise SystemExit(main())
