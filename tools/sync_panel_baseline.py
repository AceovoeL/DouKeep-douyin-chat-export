#!/usr/bin/env python3
"""把 tests/baseline/panel.html 同步到当前的面板页面。

背景：``tests/test_routes_contract.py::test_panel_html_byte_identical`` 会把控制
面板页面 **逐字节** 和 ``tests/baseline/panel.html`` 比对。这份快照是故意冻结
的契约 —— 面板页面的任何改动都必须显式同步到基线，否则 CI 一定红。但「显式
同步」这一步很容易忘（2026-09-29 那次 CI 挂掉就是因为漏了它），所以这里把它
变成一条命令。

基线里存的必须是「后端真正发出去的字节」，而不是磁盘上的原始字节：后端用
``open(..., encoding="utf-8")`` 读文本（换行在此统一成 LF），HTMLResponse 再
按 utf-8 编码。所以即使 Windows 上把 panel.html 存成了 CRLF，只要按这个读法
算出来的字节一致，测试就是绿的。

用法（在项目根目录执行）::

    python tools/sync_panel_baseline.py                     # 只对比并打印差异
    python tools/sync_panel_baseline.py --apply             # 写入快照，再跑一次 pytest
    python tools/sync_panel_baseline.py --apply --no-test   # 只写快照

退出码：0 = 一致或已同步；1 = 不一致（默认的空跑模式）；2 = 出错。
"""
import argparse
import difflib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(ROOT, "backend", "panel", "static", "panel.html")
BASELINE_HTML = os.path.join(ROOT, "tests", "baseline", "panel.html")

# 差异太长时只打印开头这么多行，避免刷屏
MAX_DIFF_LINES = 200


def served_bytes() -> bytes:
    """面板路由实际发给浏览器的字节（与 control_panel.py 的读法一致）。"""
    with open(PANEL_HTML, encoding="utf-8") as handle:
        return handle.read().encode("utf-8")


def read_baseline() -> bytes:
    with open(BASELINE_HTML, "rb") as handle:
        return handle.read()


def describe_diff(expected: bytes, actual: bytes) -> None:
    """打印可读的差异；两侧都按 UTF-8 文本处理。"""
    old = actual.decode("utf-8", errors="replace").splitlines()
    new = expected.decode("utf-8", errors="replace").splitlines()
    lines = list(difflib.unified_diff(
        old, new,
        fromfile="tests/baseline/panel.html（现在的基线）",
        tofile="backend/panel/static/panel.html（面板页面）",
        lineterm="", n=1,
    ))
    for line in lines[:MAX_DIFF_LINES]:
        print(line)
    if len(lines) > MAX_DIFF_LINES:
        print(f"...（还有 {len(lines) - MAX_DIFF_LINES} 行差异，已省略）")

    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    print(f"\n差异：新增 {added} 行，删除 {removed} 行。")


def warn_if_crlf() -> None:
    """面板页面的换行：源文件用 CRLF 会被服务端转成 LF，工作区里最好直接是 LF。"""
    with open(PANEL_HTML, "rb") as handle:
        raw = handle.read()
    if b"\r\n" in raw or b"\r" in raw:
        print("[提醒] backend/panel/static/panel.html 里是 CRLF 换行；"
              "后端发送前会转成 LF，快照已按 LF 写入，但这个源文件建议改回 LF。")


def apply(expected: bytes) -> None:
    with open(BASELINE_HTML, "wb") as handle:
        handle.write(expected)
    print(f"[+] 已同步：{os.path.relpath(BASELINE_HTML, ROOT)}")


def run_tests() -> int:
    cmd = [sys.executable, "-m", "pytest", "-q"]
    print("\n$ " + " ".join(cmd))
    code = subprocess.call(cmd, cwd=ROOT)
    if code != 0:
        print("\n[!] 测试没有全绿。如果失败的不是 test_panel_html_byte_identical，"
              "那说明是别的问题，这个脚本管不到。")
    return code


def main(argv=None) -> int:
    # 差异里可能有 GBK 打不出来的字符（面板上的 ⇪、各种箭头），Windows 控制台默认用
    # GBK 编码，print 会直接抛 UnicodeEncodeError —— 那会让「同步」在写文件之前就崩掉，
    # 看起来像同步成功了、其实基线没更新。这里统一按 UTF-8 输出，
    # 编不出来的字符用替代符顶上，绝不能因为「打印不了」而中断。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="同步 tests/baseline/panel.html 快照")
    parser.add_argument("--apply", action="store_true",
                        help="把当前面板页面写入基线快照（默认只对比不写入）")
    parser.add_argument("--no-test", action="store_true",
                        help="--apply 之后不跑 pytest")
    args = parser.parse_args(argv)

    for path in (PANEL_HTML, BASELINE_HTML):
        if not os.path.exists(path):
            print(f"[x] 找不到文件：{path}")
            return 2

    expected = served_bytes()
    current = read_baseline()

    if expected == current:
        print("面板页面与基线一致，无需同步。")
        if not args.apply:
            return 0
        warn_if_crlf()
        return 0 if args.no_test else run_tests()

    print(f"面板页面与基线不一致（基线 {len(current)} 字节，"
          f"面板页面 {len(expected)} 字节）。\n")
    describe_diff(expected, current)

    if not args.apply:
        print("\n未写入。确认上面的改动是有意的之后，加 --apply 同步："
              "\n    python tools/sync_panel_baseline.py --apply")
        return 1

    apply(expected)
    warn_if_crlf()
    if args.no_test:
        return 0
    return 1 if run_tests() != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
