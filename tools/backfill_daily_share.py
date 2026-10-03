"""回填限时日常（aweType=805）分享的正文列。

背景：抓取端还不认识 805 的时候，把它当未知载荷落库成 content =
json.dumps(cj)[:200]（一段截断的 JSON）、msg_type=0。查看器靠 raw_data 里的完整
content_json 仍能正确渲染，但搜索匹配的是 m.content / content_title 等字段，
所以这类消息搜不到「限时日常」这个关键词。

本工具把这类行的 content 改写成与抓取端一致的文案（[分享限时日常]，有标题时
接在后面），让关键词可被检索；raw_data 不动，渲染逻辑不受影响。

用法（在项目根目录执行）::

    python tools/backfill_daily_share.py            # 只列出将要修改的行
    python tools/backfill_daily_share.py --apply    # 写入，并生成回滚文件
    python tools/backfill_daily_share.py --revert   # 按回滚文件恢复

--apply 会把每行的原值写进 data/backfill_daily_share_undo.json，--revert 据此还原。
工具是幂等的：文案已经正确的行不会被再次修改。
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.message_kinds import daily_share_text, is_daily_share  # noqa: E402
from common import paths  # noqa: E402
from common.paths import DB_PATH  # noqa: E402

#: 回滚记录跟着数据库走（记的是被改写的那几行的原值）
UNDO_PATH = paths.BACKFILL_DAILY_SHARE_UNDO


def _content_json(raw_data):
    """raw_data 里的 content_json（可能是二次编码的字符串）。"""
    try:
        outer = json.loads(raw_data or "{}")
        cj = outer.get("content_json")
        return json.loads(cj) if isinstance(cj, str) else cj
    except (ValueError, TypeError):
        return None


def plan(conn):
    """返回 [(msg_id, 旧 content, 新 content)]，只含确实需要改的行。"""
    rows = []
    for msg_id, content, raw_data in conn.execute(
            "SELECT msg_id, content, raw_data FROM messages WHERE raw_data LIKE '%805%'"):
        cj = _content_json(raw_data)
        if not is_daily_share(cj):
            continue
        new = daily_share_text(cj)
        if (content or "") != new:
            rows.append((msg_id, content or "", new))
    return rows


def apply(conn, rows, undo_path=UNDO_PATH):
    conn.executemany("UPDATE messages SET content = ? WHERE msg_id = ?",
                     [(new, msg_id) for msg_id, _, new in rows])
    conn.commit()
    with open(undo_path, "w", encoding="utf-8") as fp:
        json.dump({"rows": [{"msg_id": msg_id, "content": old} for msg_id, old, _ in rows]},
                  fp, ensure_ascii=False, indent=2)
    return undo_path


def revert(conn, undo_path=UNDO_PATH):
    if not os.path.exists(undo_path):
        print(f"没有找到回滚文件：{undo_path}")
        return 0
    with open(undo_path, encoding="utf-8") as fp:
        saved = json.load(fp).get("rows", [])
    conn.executemany("UPDATE messages SET content = ? WHERE msg_id = ?",
                     [(row["content"], row["msg_id"]) for row in saved])
    conn.commit()
    print(f"已按回滚文件恢复 {len(saved)} 行：{undo_path}")
    return len(saved)


def main(argv=None):
    parser = argparse.ArgumentParser(description="回填限时日常分享的正文列")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="写入数据库并生成回滚文件")
    group.add_argument("--revert", action="store_true", help="按回滚文件恢复")
    parser.add_argument("--db", default=DB_PATH, help=f"数据库路径（默认 {DB_PATH}）")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        if args.revert:
            revert(conn)
            return 0
        rows = plan(conn)
        if not rows:
            print("没有需要回填的行（都已是 [分享限时日常] 文案）。")
            return 0
        for msg_id, old, new in rows:
            print(f"{msg_id}\n  旧: {old[:80]!r}\n  新: {new!r}")
        if args.apply:
            path = apply(conn, rows)
            print(f"\n已回填 {len(rows)} 行；原值保存在 {path}")
        else:
            print(f"\n共 {len(rows)} 行待回填（未写入，加 --apply 执行）。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
