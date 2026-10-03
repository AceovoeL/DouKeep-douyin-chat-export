"""按 commit 数核算版本号，并写回 common/version.py。

版本规则：第一个 commit 是 1.0.0，之后每多一个 commit 就把最后一位加一
（1.0.9 的下一个是 1.1.0）。所以新增 commit 之后跑一次这个脚本即可：

    python tools/sync_version.py            # 只打印当前 commit 数对应的版本号
    python tools/sync_version.py --apply    # 不一致时改写 common/version.py

关于「永远差一个 commit」：写回版本号这个动作本身也会产生一个新的 commit，于是
VERSION 永远等于「上一次提交」的版本号。这是正常的，脚本对此有专门判断 ——
只要 VERSION 是 HEAD 或 HEAD 父提交的版本号，就认为已经同步。

`--apply` 写完后建议提交这次改动。
"""
import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO_ROOT, "common", "version.py")

sys.path.insert(0, REPO_ROOT)

from common.version import commit_count_for, local_commit_count, version_string  # noqa: E402


def current_version() -> str:
    pattern = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
    with open(VERSION_FILE, encoding="utf-8") as handle:
        match = pattern.search(handle.read())
    return match.group(1) if match else ""


def write_version(version: str) -> None:
    with open(VERSION_FILE, encoding="utf-8") as handle:
        text = handle.read()
    updated = re.sub(
        r'^(VERSION\s*=\s*")[^"]+(")',
        lambda m: m.group(1) + version + m.group(2),
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        raise SystemExit("[-] 没能在 common/version.py 里找到 VERSION 定义")
    with open(VERSION_FILE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)


def main() -> int:
    parser = argparse.ArgumentParser(description="按 commit 数核算版本号")
    parser.add_argument("--apply", action="store_true", help="把版本号写回 common/version.py")
    args = parser.parse_args()

    count = local_commit_count()
    if not count:
        print("[-] 读不到 git 历史（不是 git 仓库、浅克隆或 git 不可用），无法核算版本号")
        return 1

    expected = version_string(count)
    actual = current_version()
    declared = commit_count_for(actual)
    print(f"commit 数: {count}")
    print(f"应有版本: {expected}")
    print(f"当前版本: {actual or '(未找到)'}" + (f"（第 {declared} 个 commit）" if declared else ""))
    if declared is not None:
        lag = count - declared
        if lag > 0:
            print(f"        落后 {lag} 个 commit")
        elif lag < 0:
            print(f"        [!] 版本号写过头了 {-lag} 个 commit")

    # 发版时把它追平即可；已经追平（或本来就落后）都算可用，只有「写过头」要拦
    if declared is not None and 0 <= declared <= count:
        print("[+] 版本号可用（发版前想追平最新 commit，加 --apply）")
        if not args.apply:
            return 0
        if declared == count:
            return 0
    if not args.apply:
        print("[!] 版本号与 commit 数不一致，加 --apply 写回")
        return 1

    write_version(expected)
    print(f"[+] 已把 common/version.py 的 VERSION 改为 {expected}，记得提交这次改动")
    print(f"    （这次提交会让 commit 数变成 {count + 1}，版本号因此又落后一位，属正常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
