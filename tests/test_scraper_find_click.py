"""Nickname poll in _find_and_click_conversation (UID placeholder titles)."""
import asyncio

from extractor.web_scraper import WebChatScraper


def _run(coro):
    return asyncio.run(coro)


def _scraper_with_list_stubs(match_fn, *, at_bottom=False):
    s = WebChatScraper()
    s._scroll_top_n = 0
    s._scroll_down_n = 0
    clicks = []

    async def _top():
        s._scroll_top_n += 1

    async def _down():
        s._scroll_down_n += 1
        return at_bottom

    async def _click(idx, text):
        clicks.append((idx, text))
        return {"found": True, "text": text}

    s._match_conversation_in_dom = match_fn
    s._scroll_conv_list_to_top = _top
    s._scroll_conv_list_down = _down
    s._click_conversation_index = _click
    s._clicks = clicks
    return s


def test_immediate_nickname_match_does_not_wait():
    calls = {"n": 0}

    async def match(name):
        calls["n"] += 1
        return {"index": 0, "text": name, "names": [name]}

    s = _scraper_with_list_stubs(match)
    result = _run(s._find_and_click_conversation("示例昵称🍟", timeout_s=15, poll_s=0.5))

    assert result == {"found": True, "text": "示例昵称🍟"}
    assert calls["n"] == 1
    assert s._scroll_top_n == 0
    assert s._clicks == [(0, "示例昵称🍟")]


def test_uid_titles_then_nickname_match():
    calls = {"n": 0}

    async def match(name):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"index": -1, "text": "", "names": ["100000000000034", "10000000035"]}
        return {"index": 1, "text": name, "names": ["100000000000034", name]}

    s = _scraper_with_list_stubs(match)
    result = _run(s._find_and_click_conversation("示例会话", timeout_s=2, poll_s=0.01))

    assert result["found"] is True
    assert result["text"] == "示例会话"
    assert calls["n"] >= 3
    assert s._scroll_top_n >= 1
    assert s._clicks == [(1, "示例会话")]


def test_timeout_without_target_returns_not_found():
    async def match(name):
        return {"index": -1, "text": "", "names": ["100000000000034", "example_user"]}

    s = _scraper_with_list_stubs(match, at_bottom=True)
    result = _run(s._find_and_click_conversation("示例会话", timeout_s=0.05, poll_s=0.01))

    assert result["found"] is False
    assert result["count"] >= 1
    assert "100000000000034" in result["names"]
    assert s._scroll_top_n >= 2  # initial reset + wrap after hitting bottom
    assert s._clicks == []


def test_empty_dom_then_list_appears():
    calls = {"n": 0}

    async def match(name):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"index": -1, "text": "", "names": []}
        return {"index": 0, "text": name, "names": [name]}

    s = _scraper_with_list_stubs(match, at_bottom=True)
    result = _run(s._find_and_click_conversation("示例会话", timeout_s=2, poll_s=0.01))

    assert result == {"found": True, "text": "示例会话"}
    assert calls["n"] >= 3
    assert s._clicks == [(0, "示例会话")]
