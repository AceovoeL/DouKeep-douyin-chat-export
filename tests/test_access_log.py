import io
import logging
import sys

from backend.access_log import (configure_logging, describe_request, force_utf8_output,
                                route_label, safe_query, status_note)


def test_forward_and_missing_message_notes():
    assert "合并转发" in route_label("/api/messages/srv_1/forward")
    assert "未归档" in status_note("/api/messages/srv_1", 404)
    assert "用户表" in status_note("/api/users/123", 404)
    line = describe_request("GET", "/api/messages/srv_1", "", 404, 12.4)
    assert "404" in line and "12ms" in line and "未归档" in line


def test_poll_and_cache_notes():
    assert "轮询" in status_note("/panel/api/status", 200)
    assert "缓存" in status_note("/media/avatars/a.webp", 304)


def test_query_redacts_token_and_keeps_search():
    assert "token=***" in safe_query("token=secret&page=1")
    line = describe_request(
        "GET", "/api/search", "q=%E9%80%9F%E9%80%9F&conv_id=c1", 200, 8,
    )
    assert "速速" in line
    assert "成功" in line


# ── 输出编码：面板「日志」页按 UTF-8 读 data/server.log ──────────────────────

def test_redirected_output_is_forced_to_utf8():
    """重定向到文件 / 管道时按 UTF-8 写：服务不管是怎么起来的，日志都是一种编码。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", newline="")

    force_utf8_output([stream])
    stream.write("GET /panel → 200  成功")
    stream.flush()

    assert raw.getvalue() == "GET /panel → 200  成功".encode("utf-8")


def test_terminal_output_keeps_the_console_encoding():
    """人正对着终端看时不动它：GBK 控制台上硬写 UTF-8 反而糊成一片。"""
    class _FakeTty:
        encoding = "gbk"

        def isatty(self):
            return True

        def reconfigure(self, **kwargs):
            raise AssertionError("终端上的编码不该被改")

    force_utf8_output([_FakeTty()])          # 不抛异常 = 原样留着


def test_output_without_a_stream_or_reconfigure_is_left_alone():
    """没有 stdout（pythonw / 脱离控制台启动）或不支持 reconfigure 的流也不能把服务搞挂。"""
    force_utf8_output([None, object()])


def test_configure_logging_switches_redirected_output_to_utf8(monkeypatch):
    """接线：configure_logging() 一进来就把重定向的输出改成 UTF-8。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    # configure_logging() 可能顺手往 app logger 上挂一个绑着这条临时流的 handler，
    # 用完摘掉，免得后面的用例往一个已经关掉的 BytesIO 里写日志。
    app_logger = logging.getLogger("app")
    before = list(app_logger.handlers)
    try:
        configure_logging()

        assert stream.encoding.lower().replace("-", "") == "utf8"
    finally:
        for handler in list(app_logger.handlers):
            if handler not in before:
                app_logger.removeHandler(handler)
