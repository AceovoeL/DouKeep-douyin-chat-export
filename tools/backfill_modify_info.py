"""把「撤回 / 编辑 / 表情快捷回复 / 仅看一次」的判定结果写进 messages.raw_data。

背景：抖音把四种行为的「时间」都写在同一个 protobuf 第 11 号字段上，只有整包
字段才能分出到底是哪一种（见 ``common/message_modify.py``）。抓取端落库时会
顺手写 ``raw_data.modify``；本工具给「回填整包之前就已经入库、或整包在别的
地方」的行补上同一个结构，这样查看器只认一套判断。

用法（在项目根目录执行）::

    python tools/backfill_modify_info.py            # 只统计，不写库
    python tools/backfill_modify_info.py --apply    # 写入，并生成回滚文件
    python tools/backfill_modify_info.py --revert   # 按回滚文件恢复

``--apply`` 只增改 raw_data 里的 ``modify`` 一个键，回滚文件只记录这个键的旧值，
所以很小。工具幂等：已经写过且一致的行不会再改。
"""
import argparse
import base64
import json
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.message_modify import modify_from_raw, parse_modify  # noqa: E402
from common.paths import DATA_DIR, DB_PATH  # noqa: E402

UNDO_PATH = os.path.join(DATA_DIR, "backfill_modify_undo.json")


def _load_raw(raw_data):
    try:
        outer = json.loads(raw_data or "{}")
    except (ValueError, TypeError):
        return None
    return outer if isinstance(outer, dict) else None


def _same(a, b):
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def plan(conn):
    """返回 (需要写入的行, 分类统计)。"""
    changes = []
    stats = Counter()
    packets = dict(conn.execute("select msg_id, packet from message_packets where packet is not null"))
    empty_packets = set(mid for (mid,) in conn.execute("select msg_id from message_packets where packet is null"))
    for msg_id, raw_data in conn.execute("select msg_id, raw_data from messages where raw_data is not null"):
        packet = packets.get(msg_id)
        if packet:
            try:
                modify = parse_modify(base64.b64decode(packet))
            except Exception:
                modify = None
            source = "packet"
        elif msg_id in empty_packets:
            modify = modify_from_raw(_load_raw(raw_data))
            source = "fields"
        else:
            modify = modify_from_raw(_load_raw(raw_data))
            source = "raw"
        if not modify:
            continue
        kinds = tuple(modify.get("kinds") or ())
        stats["/".join(kinds) or "?"] += 1
        stats["来源:" + source] += 1
        raw = _load_raw(raw_data)
        if raw is None:
            continue
        if _same(raw.get("modify"), modify):
            stats["已是最新"] += 1
            continue
        changes.append((msg_id, json.dumps(raw, ensure_ascii=False), raw.get("modify"), modify, raw_data))
    return changes, stats


def apply_changes(conn, changes):
    undo = []
    for msg_id, _, previous, modify, old_raw in changes:
        raw = _load_raw(old_raw) or {}
        raw["modify"] = modify
        conn.execute(
            "UPDATE messages SET raw_data = ? WHERE msg_id = ?",
            (json.dumps(raw, ensure_ascii=False), msg_id),
        )
        undo.append({"msg_id": msg_id, "previous": previous})
    conn.commit()
    with open(UNDO_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": undo}, f, ensure_ascii=False)
    return len(undo)


def revert(conn):
    if not os.path.exists(UNDO_PATH):
        print(f"[-] 找不到回滚文件: {UNDO_PATH}")
        return 0
    with open(UNDO_PATH, encoding="utf-8") as f:
        items = json.load(f).get("items", [])
    done = 0
    for item in items:
        row = conn.execute(
            "SELECT raw_data FROM messages WHERE msg_id = ?", (item["msg_id"],)
        ).fetchone()
        if not row:
            continue
        raw = _load_raw(row[0])
        if raw is None:
            continue
        if item.get("previous") is None:
            raw.pop("modify", None)
        else:
            raw["modify"] = item["previous"]
        conn.execute(
            "UPDATE messages SET raw_data = ? WHERE msg_id = ?",
            (json.dumps(raw, ensure_ascii=False), item["msg_id"]),
        )
        done += 1
    conn.commit()
    return done


def main():
    parser = argparse.ArgumentParser(description="回填消息操作标记（撤回/编辑/表情快捷回复/仅看一次）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="写入数据库并生成回滚文件")
    group.add_argument("--revert", action="store_true", help="按回滚文件恢复")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if args.revert:
        print(f"[+] 已恢复 {revert(conn)} 行")
        return

    changes, stats = plan(conn)
    print("[*] 分类统计:")
    for key, count in stats.most_common():
        print(f"    {key}: {count}")
    print(f"[*] 需要写入的行: {len(changes)}")
    if not args.apply:
        print("[*] 这是预演（不写库）。确认后加 --apply 执行。")
        return
    print(f"[+] 已写入 {apply_changes(conn, changes)} 行，回滚文件: {UNDO_PATH}")


if __name__ == "__main__":
    main()
