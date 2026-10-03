#!/usr/bin/env python3
"""更新完成后（或面板点了「重启服务」后），把后端服务重新拉起来。

    python tools/restart_server.py --wait-pid 1234 --host 127.0.0.1 --port 8000
    python tools/restart_server.py --host 127.0.0.1 --port 8000 --build-frontend

为什么必须由**另一个进程**来做这件事：换完代码就得让正在跑的服务退出，而服务一退出
就没人能再发指令了，所以得有一个**脱离父进程**的小助手守在旁边，按顺序做三件事：

1. 等旧的服务进程真的退出；
2. 等端口空出来（旧进程刚退出时端口可能还要几百毫秒才释放）；
3. 用 venv 里的 python 重新启动 uvicorn，并确认端口真的能连上。

第 1、2 步是并列等的：哪个条件先满足就走下一步 —— Windows 上「进程还活着吗」可能被
别的句柄影响，而端口空没空才是真正决定新服务能不能起来的那件事。

带了 ``--build-frontend`` 时中间还夹一步：**先把前端重新构建一遍**（面板「日志 →
重启服务」用的就是这个）。这一步刻意放在「旧服务已经让位」之后、「新服务起来」之前 ——
``frontend/dist`` 在被清空重写的时候没有别的进程在读它，起回来的服务读到的就是新的
那一份。构建用的是 ``tools/update.py`` 里的 ``build_frontend()``（依赖缺了会先装），
和「一键更新」共用同一份实现。

构建失败**不拦着启动服务**：服务先回来，界面还是上一次构建好的那一份，用户能在面板
「日志」页上看到 npm 的报错，改完再点一次就行 —— 比起「服务起不来、什么也看不到」，
这样处理对用户更有用。

日志分两份（故意的：两个进程往同一个文件里追加会互相覆盖——Windows 上「追加」是
「先跳到末尾再写」，两边的定位会撞在一起）：

* ``config/logs/restart.log`` —— 本助手自己的过程日志（面板重定向它的输出到这里，npm 的
  输出也走这里，因为它是本助手拉起来的子进程）；
* ``config/logs/server.log``  —— 被重新启动起来的新服务自己的输出（``--log`` 可以改）。

一般不需要手敲这个脚本：面板「关于 → 更新」在跑完 ``tools/update.py`` 之后会自动
调用它，面板「日志 → 重启服务」也会（多一个 ``--build-frontend``）。手动排查重启用：

    venv\\Scripts\\python.exe tools\\restart_server.py --port 8000
"""
import argparse
import os
import socket
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 直接跑 `python tools/restart_server.py` 时 sys.path[0] 是 tools/，要能 `from tools import ...`
#: 就得先把项目根挂上（下面那份构建逻辑住在 tools/update.py 里，两边共用一份实现）。
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools import update as update_tool  # noqa: E402  (必须在 sys.path 调整之后)
from common import paths  # noqa: E402

#: 新服务自己的输出写这里（只有新服务一个进程写，不会和谁抢文件）。
#: 本助手自己的日志走 stdout/stderr：由面板重定向到 config/logs/restart.log，手动运行时
#: 直接打在终端上。两份分开是故意的 —— 两个进程往同一个文件里追加会互相覆盖
#: （Windows 上「追加」是「先跳到末尾再写」，两边的定位会撞在一起）。
DEFAULT_LOG = paths.SERVER_LOG
PID_FILE = os.path.join(REPO_ROOT, ".server.pid")

#: 起新服务用的 Windows 标志：不弹控制台窗口（CREATE_NO_WINDOW），并且单独一个进程组
#: （CREATE_NEW_PROCESS_GROUP），不会被别处的 Ctrl+C 之类的东西一起带走。
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
#: 启动本助手时用的标志：完全摆脱父进程的控制台，父进程退出后照样活着。
DETACHED_PROCESS = 0x00000008


def log(message: str) -> None:
    """打一行日志。

    这里刻意吞掉输出异常：日志写不出来不该把重启流程搞挂（往 GBK 控制台打 ``✓``
    这类字符会直接抛 UnicodeEncodeError，新服务起来了、助手却崩了）。
    """
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
    except (OSError, UnicodeEncodeError):
        pass


def pid_alive(pid: int) -> bool:
    """这个进程号还在不在（只用来等它退出，不保证还是原来那个程序）。

    Windows 上**不能**用 ``os.kill(pid, 0)`` 探活 —— 那是 TerminateProcess，
    会把进程直接杀掉。所以那边改用 OpenProcess 问一句。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            # 权限不足也算「还在跑」，只有「找不到这个进程」才算退出
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def probe_host(host: str) -> str:
    """用来连的地址：监听 0.0.0.0 / :: 时连本机回环地址才连得上。"""
    text = (host or "").strip()
    if text in ("", "0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return text.strip("[]")


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """这个地址上有没有人在接受连接（有 = 服务还在跑 / 端口还没释放）。"""
    try:
        with socket.create_connection((probe_host(host), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_old_service(pid: int, host: str, port: int, timeout: float) -> bool:
    """等旧服务让位：**进程退出** 或 **端口空出来**，哪个先到算哪个。

    只看进程不够：Windows 上进程对象在别的句柄关闭之前一直存在，探活可能误报
    「还在跑」。只看端口也不够：进程刚退出时端口可能还要几百毫秒才释放。两个条件
    并列着等，既不会干等也不会抢跑。超时返回 False（新服务仍然会尝试启动）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid and not pid_alive(pid):
            log(f"旧服务进程 {pid} 已退出")
            return True
        if not port_in_use(host, port):
            log(f"{probe_host(host)}:{port} 已经空出来")
            return True
        time.sleep(0.5)
    log(f"等了 {timeout:.0f} 秒，旧服务好像还占着 {probe_host(host)}:{port}")
    return False


def server_command(host: str, port: int) -> list[str]:
    """重新启动服务用的命令行（和 start.ps1 / start.sh 里那一条一致）。"""
    return [
        sys.executable, "-m", "uvicorn", "backend.main:app",
        "--host", str(host), "--port", str(port),
    ]


def build_frontend() -> int:
    """把前端重新构建一遍（0 = 成功）。

    真正的活儿在 ``tools/update.py`` 的 ``build_frontend()`` 里：它会先确认前端依赖
    装好了（缺了先 ``npm install``，走国内镜像、失败回退官方源），再 ``npm run build``。
    这里只是转一手，好处是「一键更新」和「重启服务」永远用同一份实现，不会各改一半。
    """
    return update_tool.build_frontend()


def detached_flags() -> dict:
    """起新服务时的「脱离父进程」参数。"""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def start_server(host: str, port: int, log_path: str) -> subprocess.Popen:
    """启动新服务，输出追加到 ``log_path``（和本助手共用一份日志）。"""
    with open(log_path, "ab", buffering=0) as handle:
        return subprocess.Popen(
            server_command(host, port),
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **detached_flags(),
        )


def wait_for_server(host: str, port: int, timeout: float) -> bool:
    """确认新服务真的起来了（能连上端口就算起来了）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use(host, port):
            return True
        time.sleep(0.5)
    return False


def write_pid_file(pid: int) -> None:
    """POSIX 上把 ``.server.pid`` 换成新进程号，``stop.sh`` 才找得到服务。"""
    if os.name == "nt":
        return
    try:
        with open(PID_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(pid))
    except OSError as exc:
        log(f"写 .server.pid 失败：{exc}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新完成后重新启动后端服务")
    parser.add_argument("--wait-pid", type=int, default=0, help="等这个进程退出（旧的服务）")
    parser.add_argument("--host", default="127.0.0.1", help="服务监听地址")
    parser.add_argument("--port", type=int, default=8000, help="服务端口")
    parser.add_argument("--log", default=DEFAULT_LOG,
                        help="新服务的输出写到这里（本助手自己的日志走 stdout）")
    parser.add_argument("--build-frontend", action="store_true",
                        help="起服务之前先重建前端（面板「日志 → 重启服务」用）")
    parser.add_argument("--wait-timeout", type=float, default=90, help="等旧进程/端口的最长秒数")
    parser.add_argument("--start-timeout", type=float, default=40, help="等新服务起来的最长秒数")
    parser.add_argument("--attempts", type=int, default=3, help="启动失败时的重试次数")
    return parser.parse_args(argv)


def run_frontend_build() -> None:
    """旧服务已经让位之后、新服务起来之前，重建一次前端产物。

    构建失败**不抛异常、也不拦着启动服务**（调用处照常往下走）：先把服务拉回来，
    用户才能打开面板看日志页上 npm 的报错。界面这时还是上一次构建好的那一份。
    """
    log("[i] 先重建前端（npm run build）：服务已经停下来了，这段期间 frontend/dist 不会被谁读着")
    started = time.time()
    try:
        code = build_frontend()
    except Exception as exc:                      # 构建脚本本身出问题也不能卡住重启
        log(f"[-] 前端构建没能跑起来：{exc}")
        return
    spent = max(0, int(time.time() - started))
    if code == 0:
        log(f"[+] 前端构建完成（用时 {spent} 秒）")
    else:
        log(f"[-] 前端构建失败（退出码 {code}，用时 {spent} 秒）：仍然继续启动服务，"
            "界面保持上一次构建好的产物；上面 npm 的输出就是原因")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log(f"开始重启后端服务：{probe_host(args.host)}:{args.port}"
        f"（等旧进程 {args.wait_pid or '无'}，日志 {args.log}）")
    wait_for_old_service(args.wait_pid, args.host, args.port, args.wait_timeout)

    if args.build_frontend:
        run_frontend_build()

    for attempt in range(1, max(1, args.attempts) + 1):
        log(f"第 {attempt}/{args.attempts} 次启动：{' '.join(server_command(args.host, args.port))}")
        try:
            proc = start_server(args.host, args.port, args.log)
        except OSError as exc:
            log(f"启动失败：{exc}")
        else:
            if wait_for_server(args.host, args.port, args.start_timeout):
                log(f"[+] 服务已启动（新进程 {proc.pid}）"
                    f"，浏览器访问 http://{probe_host(args.host)}:{args.port}")
                write_pid_file(proc.pid)
                return 0
            log(f"[-] 等了 {args.start_timeout:.0f} 秒还是连不上端口"
                f"（新进程 {proc.pid} 退出码 {proc.poll()}）")
            if proc.poll() is None:
                proc.terminate()
        if attempt < args.attempts:
            time.sleep(5)

    log("[-] 服务没能自动起来：请手动运行 start.ps1（Windows）或 start.sh，"
        f"并查看上面的报错与新服务的输出（{args.log}）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
