"""``tools/restart_server.py``：更新完成后把服务重新拉起来的那个小助手。

这些用例都不真的启动服务：只验证「等谁退出」「端口空没空」「用什么命令起服务」，
以及重试失败时的行为。真的端到端重启由人工在真机上跑一遍（见发版说明）。
"""
import os
import socket
import subprocess
import sys
import time

from tools import restart_server


def listen_on_free_port() -> tuple[socket.socket, int]:
    """占住一个随机端口，返回 (socket, 端口号)，用完记得 close。

    backlog 给大一点：探活是「连一次看看」，backlog 只有 1 的话第二次探测会被拒，
    看起来就像端口已经空出来了。
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    return server, server.getsockname()[1]


def test_probe_host_maps_wildcard_addresses():
    """监听 0.0.0.0 / :: 时，探测要连回环地址才连得上。"""
    assert restart_server.probe_host("0.0.0.0") == "127.0.0.1"
    assert restart_server.probe_host("") == "127.0.0.1"
    assert restart_server.probe_host("::") == "127.0.0.1"
    assert restart_server.probe_host("[::1]") == "::1"
    assert restart_server.probe_host(" 192.168.1.5 ") == "192.168.1.5"


def test_pid_alive_for_self_and_finished_process():
    assert restart_server.pid_alive(os.getpid()) is True
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    if os.name == "nt":
        # Windows 上 Popen 自己还握着进程句柄，「进程对象」因此一直存在；显式关掉它
        # 才等于「服务进程真的没了」。产品代码不依赖这一点，它还会看端口。
        handle = getattr(proc, "_handle", None)
        if handle is not None:
            handle.Close()
    time.sleep(0.2)
    assert restart_server.pid_alive(proc.pid) is False
    assert restart_server.pid_alive(0) is False


def test_port_in_use_tracks_a_real_listener():
    server, port = listen_on_free_port()
    try:
        assert restart_server.port_in_use("127.0.0.1", port) is True
        assert restart_server.port_in_use("127.0.0.1", port) is True   # 反复探测都要准
    finally:
        server.close()
    time.sleep(0.2)
    assert restart_server.port_in_use("127.0.0.1", port) is False


def test_wait_for_old_service_returns_once_the_port_closes():
    server, port = listen_on_free_port()
    server.close()
    time.sleep(0.2)
    # 进程号用一个不可能存在的值：端口空出来这件事自己就能让它返回
    assert restart_server.wait_for_old_service(0, "127.0.0.1", port, timeout=2) is True


def test_wait_for_old_service_gives_up_after_timeout():
    server, port = listen_on_free_port()
    try:
        assert restart_server.wait_for_old_service(os.getpid(), "127.0.0.1", port, timeout=0.6) is False
    finally:
        server.close()


def test_wait_for_old_service_returns_when_the_process_is_gone():
    """旧进程已经没了（但端口还占着）时也要立刻返回，不必等满超时。"""
    server, port = listen_on_free_port()
    try:
        started = time.time()
        assert restart_server.wait_for_old_service(0, "127.0.0.1", port, timeout=0) is False
        assert time.time() - started < 1
    finally:
        server.close()


def test_server_command_uses_this_python_and_the_given_address():
    command = restart_server.server_command("127.0.0.1", 8123)
    assert command[0] == sys.executable
    assert command[1:] == ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8123"]


def test_start_server_detaches_and_writes_to_the_log(monkeypatch, tmp_path):
    """起新服务的进程必须脱离本助手：本助手马上会退出，服务得自己活着。"""
    captured: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            self.pid = 4321

    monkeypatch.setattr(restart_server.subprocess, "Popen", FakePopen)
    log_path = str(tmp_path / "restart.log")

    proc = restart_server.start_server("127.0.0.1", 8123, log_path)

    assert proc.pid == 4321
    assert captured["command"] == restart_server.server_command("127.0.0.1", 8123)
    assert captured["cwd"] == restart_server.REPO_ROOT
    assert captured["stdin"] == subprocess.DEVNULL
    if os.name == "nt":
        assert captured["creationflags"] & restart_server.CREATE_NO_WINDOW
    else:
        assert captured["start_new_session"] is True
    # 日志文件交给子进程之后本助手就关掉自己的句柄（子进程握着它一直写）
    assert captured["stdout"].name == log_path
    assert captured["stdout"].closed is True


def test_main_waits_then_starts_and_reports_success(monkeypatch, tmp_path):
    calls: list = []

    monkeypatch.setattr(restart_server, "wait_for_old_service",
                        lambda pid, host, port, timeout: calls.append(("wait", pid)) or True)
    monkeypatch.setattr(restart_server, "write_pid_file", lambda pid: calls.append(("pid", pid)))

    class FakeProc:
        pid = 9999

        def poll(self):
            return None

    monkeypatch.setattr(restart_server, "start_server",
                        lambda host, port, log: calls.append(("start", host, port)) or FakeProc())
    monkeypatch.setattr(restart_server, "wait_for_server",
                        lambda host, port, timeout: calls.append(("up", host, port)) or True)

    code = restart_server.main([
        "--wait-pid", "1234", "--host", "127.0.0.1", "--port", "8123",
        "--log", str(tmp_path / "restart.log"),
    ])

    assert code == 0
    assert calls == [
        ("wait", 1234),
        ("start", "127.0.0.1", 8123),
        ("up", "127.0.0.1", 8123),
        ("pid", 9999),
    ]


def test_main_retries_then_reports_failure(monkeypatch, tmp_path):
    """连续起不来时返回非零，并且不会无限重试。"""
    attempts = {"count": 0}
    sleeps: list = []

    class FakeTime:
        @staticmethod
        def sleep(seconds):
            sleeps.append(seconds)

        @staticmethod
        def strftime(fmt):
            return "2026-10-02 12:00:00"

        @staticmethod
        def time():
            return 0.0

    class FakeProc:
        pid = 9998

        def poll(self):
            return 1

        def terminate(self):                     # pragma: no cover - 不该走到
            raise AssertionError("已经退出的进程不该再 terminate")

    def fake_start(host, port, log):
        attempts["count"] += 1
        return FakeProc()

    monkeypatch.setattr(restart_server, "time", FakeTime)
    monkeypatch.setattr(restart_server, "wait_for_old_service", lambda pid, host, port, timeout: True)
    monkeypatch.setattr(restart_server, "wait_for_server", lambda host, port, timeout: False)
    monkeypatch.setattr(restart_server, "start_server", fake_start)

    code = restart_server.main([
        "--wait-pid", "1234", "--port", "8123", "--attempts", "2",
        "--log", str(tmp_path / "restart.log"),
    ])

    assert code == 1
    assert attempts["count"] == 2
    assert sleeps == [5], "两次尝试之间等一下再重试"


def test_restart_script_is_plain_utf8_without_bom():
    """Python 脚本不要 BOM（只有 .ps1 才需要 BOM，那由另一条用例管）。"""
    path = os.path.join(restart_server.REPO_ROOT, "tools", "restart_server.py")
    with open(path, "rb") as handle:
        head = handle.read(3)
    assert head != b"\xef\xbb\xbf"
    with open(path, encoding="utf-8") as handle:
        assert handle.readline().startswith("#!")
