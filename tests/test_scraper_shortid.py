"""short_id 获取逻辑（issue #24/#25 的取数根因）。

那个被误称 "cursor" 的值其实是 conversation_short_id（imapi 请求 field 3，每会话固定）。
- 群聊：纯数字 conv_id 本身就是 short_id，直接返回，不依赖 SDK 时序。
- 单聊：conv_id 形如 '0:1:a:b'，short_id 是另一个数字，只能偷 SDK 请求，带重试。

旧代码对群聊也走"偷请求"，而群聊 SDK 有缓存时不发请求 → 偷不到 → 回退滚动 → 抓不到。
"""
import asyncio

import pytest

from extractor.web_scraper import WebChatScraper

GROUP = "1000000000000000037"
SINGLE = "0:1:100000000000032:1000000000000033"


def _run(coro):
    return asyncio.run(coro)


def test_group_short_id_is_conv_id_without_touching_sdk():
    """群聊：short_id = conv_id，且完全不碰 SDK（不清缓存、不重载、不偷请求）。"""
    s = WebChatScraper()
    called = {"steal": 0}

    async def _steal(*a):
        called["steal"] += 1
        return None

    s._steal_short_id_from_sdk = _steal

    sid = _run(s._acquire_short_id(GROUP, "某某粉丝群"))
    assert sid == GROUP
    assert called["steal"] == 0  # 群聊绝不走偷请求那条脆弱路径


def test_single_chat_steals_short_id():
    s = WebChatScraper()

    async def _steal(conv_id, name):
        return "1000000000000000038"

    s._steal_short_id_from_sdk = _steal

    sid = _run(s._acquire_short_id(SINGLE, "示例会话"))
    assert sid == "1000000000000000038"


def _scrollable_stub(scrollable=True):
    async def _si():
        return {"scrollable": scrollable, "scrollHeight": 5000, "clientHeight": 600}
    return _si


def test_single_chat_retries_then_succeeds():
    """单聊偷 short_id 原本零重试，一次失败就回退滚动（issue #25）。现在重试。"""
    s = WebChatScraper()
    s._get_scroll_info = _scrollable_stub(True)  # 有历史 → 允许重试
    attempts = {"n": 0}

    async def _steal(conv_id, name):
        attempts["n"] += 1
        return "77777" if attempts["n"] == 2 else None  # 第 2 次才成功

    s._steal_short_id_from_sdk = _steal

    sid = _run(s._acquire_short_id(SINGLE, "示例会话"))
    assert sid == "77777"
    assert attempts["n"] == 2


def test_single_chat_gives_up_after_retries():
    """始终偷不到 → 返回 None（上层据此回退滚动，不会无限试）。"""
    s = WebChatScraper()
    s._get_scroll_info = _scrollable_stub(True)  # 有历史 → 试满 3 次
    attempts = {"n": 0}

    async def _steal(conv_id, name):
        attempts["n"] += 1
        return None

    s._steal_short_id_from_sdk = _steal

    sid = _run(s._acquire_short_id(SINGLE, "示例会话"))
    assert sid is None
    assert attempts["n"] == 3  # 试满 3 次就放弃，不卡死


def test_stranger_chat_stops_retrying_early():
    """陌生人会话消息列表不可滚动 → 第一次失败后就停，不做多余的清缓存+重载。"""
    s = WebChatScraper()
    s._get_scroll_info = _scrollable_stub(False)  # 无可翻页历史
    attempts = {"n": 0}

    async def _steal(conv_id, name):
        attempts["n"] += 1
        return None

    s._steal_short_id_from_sdk = _steal

    sid = _run(s._acquire_short_id(SINGLE, "飞天闪客"))
    assert sid is None
    assert attempts["n"] == 1  # 只试一次就放弃
