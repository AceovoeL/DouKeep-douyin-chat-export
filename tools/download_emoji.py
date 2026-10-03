#!/usr/bin/env python3
"""下载文字式表情的图片到 assets/emoji/（控制面板里那个「资源包」的同一件事）。

图片为什么不放在仓库里：那是字节跳动的版权素材，公开分发不合适（见仓库根目录的
NOTICE）。仓库只带 ``assets/emoji_manifest.json``（名字 → 官方地址），第一次运行时
按需下载。控制面板第一次打开时会问一句要不要下，这个脚本是命令行版本，也能用它补下
或者修掉坏文件。

    python tools/download_emoji.py            # 只下缺的（已有就跳过，可以中断了再来）
    python tools/download_emoji.py --force    # 全部重下（下坏了、想覆盖时用）
    python tools/download_emoji.py --list     # 只看看还缺哪些，不下载

进度会实时打印：``[ 12/214] 微笑``。全部成功返回 0，有下不来的返回 1（再跑一次即可，
已下好的会跳过）。
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 直接跑 `python tools/download_emoji.py` 时 sys.path[0] 是 tools/，项目模块要手动挂上。
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common import emoji_pack  # noqa: E402  (必须在 sys.path 调整之后)


def _print_progress(result: dict) -> None:
    total = result["total"]
    done = result["done"]
    name = result.get("current", "")
    failed = f"  失败 {result['failed']}" if result["failed"] else ""
    print(f"[{done:>4}/{total}] {name}{failed}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="下载文字式表情图片（资源包）")
    parser.add_argument("--force", action="store_true", help="已经存在的也重下")
    parser.add_argument("--list", action="store_true", help="只列出还缺哪些，不下载")
    args = parser.parse_args(argv)

    items = emoji_pack.load_manifest()
    if not items:
        print(f"[!] 读不到清单：{emoji_pack.MANIFEST_PATH}", file=sys.stderr)
        return 1

    before = emoji_pack.status(items)
    print(f"清单 {before['total']} 项，本机已有 {before['installed']} 项，"
          f"还缺 {before['missing']} 项 → {emoji_pack.EMOJI_DIR}")

    if args.list:
        for item in items:
            if not emoji_pack.is_installed(item["name"]):
                print(item["name"])
        return 0

    result = emoji_pack.download(items, on_progress=_print_progress, force=args.force)

    print(f"\n完成 {result['done']}/{result['total']}"
          f"（跳过已有 {result['skipped']}，失败 {result['failed']}，"
          f"用时 {result['elapsed']} 秒）")
    if result["failed_names"]:
        print("没下下来的：" + "、".join(result["failed_names"][:20])
              + ("…" if len(result["failed_names"]) > 20 else ""))
        print("再跑一次这个脚本就会只补这些（已经下好的不会重下）。")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
