"""打开私信页后要先等几秒，再去读会话列表。

抖音的私信页是前端异步渲染的：页面刚出来时只画出一部分会话，过几秒才补齐。不等就
往下走（刷新会话列表 / 采集都是从 navigate_to_chat 出发），会只拿到一半左右的会话。
"""
import asyncio
import time

from extractor import web_scraper
from extractor.web_scraper import WebChatScraper


class _FakePage:
    """只记时间点：goto 什么时候发生、等会话列表的 selector 什么时候开始等。"""

    def __init__(self):
        self.url = "about:blank"
        self.calls = []

    async def goto(self, url, wait_until=None):
        self.calls.append(("goto", url, time.monotonic()))

    async def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("selector", selector, time.monotonic()))


def test_the_wait_is_the_five_seconds_the_user_asked_for():
    assert web_scraper.CHAT_PAGE_SETTLE_SECONDS == 5


def test_navigate_to_chat_waits_after_opening_the_page(monkeypatch):
    """等的那几秒必须发生在 goto 之后、找会话列表之前。"""
    monkeypatch.setattr(web_scraper, "CHAT_PAGE_SETTLE_SECONDS", 0.2)
    page = _FakePage()
    scraper = WebChatScraper()
    scraper.page = page

    asyncio.run(scraper.navigate_to_chat())

    kinds = [c[0] for c in page.calls]
    assert kinds == ["goto", "selector"]
    goto_at = page.calls[0][2]
    selector_at = page.calls[1][2]
    assert selector_at - goto_at >= 0.2, "goto 之后要先静等，再去找会话列表"
    assert page.calls[0][1] == web_scraper.CHAT_URL


def test_the_wait_helper_really_sleeps(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(web_scraper.asyncio, "sleep", fake_sleep)

    asyncio.run(WebChatScraper()._wait_chat_page_settled())

    assert slept == [web_scraper.CHAT_PAGE_SETTLE_SECONDS]


class _RecordingPage:
    """只要 goto / on / remove_listener 三个方法，事件按发生顺序记下来。"""

    def __init__(self, events):
        self.events = events
        self.listeners = []

    async def goto(self, url, wait_until=None):
        self.events.append(("goto", time.monotonic()))

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def remove_listener(self, event, handler):
        self.listeners = [(e, h) for e, h in self.listeners if h is not handler]


def test_the_reload_before_stealing_short_id_also_waits(monkeypatch):
    """采集时为了偷 short_id 会重载聊天页：重载后同样要先等几秒。

    不等的话，重载前还能点到的会话，重载后列表只画了一半就去找，会报
    「重新加载后未找到会话」——这个会话这次就抓不到了。
    """
    monkeypatch.setattr(web_scraper, "CHAT_PAGE_SETTLE_SECONDS", 0.2)
    events = []
    page = _RecordingPage(events)
    scraper = WebChatScraper()
    scraper.page = page

    async def _clear_cache():
        events.append(("clear_cache", time.monotonic()))

    async def _ensure_conv_list():
        events.append(("conv_list", time.monotonic()))
        return 1

    async def _find_and_click(name):
        events.append(("click", time.monotonic()))
        return {"found": False, "names": [], "count": 0}

    scraper._clear_sdk_cache = _clear_cache
    scraper._ensure_conv_list_loaded = _ensure_conv_list
    scraper._find_and_click_conversation = _find_and_click

    asyncio.run(scraper._steal_short_id_from_sdk("0:1:uidA:uidB", "某人"))

    assert [kind for kind, _ in events] == ["clear_cache", "goto", "conv_list", "click"]
    goto_at = events[1][1]
    click_at = events[3][1]
    assert click_at - goto_at >= 0.2, "重载之后要先静等，再去找那个会话"
