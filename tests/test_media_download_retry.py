"""媒体下载被 CDN 掐断时重试，且不再冒充「采集报错」。

用户 2026-10-17 反馈：采集时冒出 ``[media] emoji 失败: [WinError 10054] 远程主机
强迫关闭了一个现有的连接``，面板弹「采集报错」并把整轮采集暂停，其实只是 51 张表情里
有 1 张被图片 CDN 顺手断开了。这里的用例把三件事钉住：

* 瞬时断线（10054 / 连接重置）会自动重试，第二次成功就不该报错；
* 403 这类重试没用的错误只试一次，不浪费时间；
* 重试仍失败的只在日志里留一行「没下到」，不被面板当成采集错误。
"""
import urllib.error

import pytest

from backend import control_panel as cp
from extractor import im_media, web_scraper

URL = "https://p3-sign.douyinpic.com/obj/tos-cn-i-example/emoji~tplv-x:large.webp"
JPEG = b"\xff\xd8\xff" + b"JPEGDATA" * 30


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    """重试间隔清零，用例不真等。"""
    monkeypatch.setattr(im_media, "_RETRY_WAIT", 0)


def _reset_error():
    # Windows 上真实的报错就是这句；这里用固定文案，避免依赖本机语言。
    return ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")


# ── 哪些错误值得重试 ───────────────────────────────────────────────────────

def test_connection_reset_is_transient():
    assert im_media.is_transient_download_error(_reset_error()) is True
    assert im_media.is_transient_download_error(
        urllib.error.URLError(_reset_error())) is True


def test_permanent_errors_are_not_transient():
    # 链接过期 / 没权限：重试多少次都一样
    assert im_media.is_transient_download_error(
        urllib.error.HTTPError(URL, 403, "Forbidden", {}, None)) is False
    # DNS 挂了、本地网络断了这类，也不该每张图都白等
    assert im_media.is_transient_download_error(OSError("offline")) is False
    # 读超时：单次已经等了 20 秒，再试两次只是白卡
    assert im_media.is_transient_download_error(TimeoutError("timed out")) is False


# ── 重试行为 ──────────────────────────────────────────────────────────────

def test_emoji_is_retried_after_a_reset(tmp_path, monkeypatch):
    calls = []

    def flaky(url, timeout=20):
        calls.append(url)
        if len(calls) == 1:
            raise _reset_error()
        return JPEG

    monkeypatch.setattr(im_media, "_fetch", flaky)
    rel = im_media._save_emoji(URL, str(tmp_path))

    assert calls == [URL, URL]                 # 第二次就成功了
    assert rel and (tmp_path / rel.split("/", 1)[1]).read_bytes() == JPEG


def test_giving_up_after_all_attempts(tmp_path, monkeypatch):
    calls = []

    def always_reset(url, timeout=20):
        calls.append(url)
        raise _reset_error()

    monkeypatch.setattr(im_media, "_fetch", always_reset)
    with pytest.raises(ConnectionResetError):
        im_media._save_emoji(URL, str(tmp_path))
    assert len(calls) == im_media.DOWNLOAD_ATTEMPTS == 3


def test_permanent_failure_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def forbidden(url, timeout=20):
        calls.append(url)
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(im_media, "_fetch", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        im_media._save_emoji(URL, str(tmp_path))
    assert calls == [URL]


# ── 失败日志：瞬时的不再冒充采集报错 ──────────────────────────────────────

def test_transient_problems_are_reported_calmly(capsys):
    web_scraper._report_download_problems(
        "media", "附件", [("emoji", _reset_error())],
        fail_line="  [media] {label} 失败: {exc}")

    out = capsys.readouterr().out.strip()
    assert "因网络中断没下到" in out and "失败" not in out
    # 面板不该为它弹框、暂停整轮采集
    assert cp.classify_scrape_log_line(out) is None


def test_permanent_problems_still_report_the_file(capsys):
    exc = urllib.error.HTTPError(URL, 403, "Forbidden", {}, None)
    web_scraper._report_download_problems(
        "media", "附件", [("emoji", exc)],
        fail_line="  [media] {label} 失败: {exc}")

    out = capsys.readouterr().out.strip()
    assert "emoji 失败" in out and "403" in out
    assert cp.classify_scrape_log_line(out) == "media"


def test_many_permanent_problems_are_capped(capsys):
    exc = urllib.error.HTTPError(URL, 403, "Forbidden", {}, None)
    problems = [(f"image{i}", exc) for i in range(6)]
    web_scraper._report_download_problems(
        "media", "附件", problems, fail_line="  [media] {label} 失败: {exc}")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 4                     # 前三条 + 一行「另有 3 个」
    assert "另有 3 个" in lines[-1]
