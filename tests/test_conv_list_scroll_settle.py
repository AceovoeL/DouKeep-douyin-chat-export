"""刷新会话列表：滑动之后先停 1 秒等昵称渲染，再读 DOM。

抖音的私信列表滑动后是「先 uid、后昵称」两步渲染：滑完立刻读 DOM，会把同一个
会话的 uid 版本也当成一条新会话（数量虚高、面板里显示 uid）；反过来，滚到底就
马上收工，又会漏掉抖音刚异步补上的那一页（刷新一遍 45 个、再刷一遍 94 个）。
"""
import asyncio
import time

from extractor import web_scraper
from extractor.web_scraper import WebChatScraper


def test_the_settle_is_the_one_second_the_user_asked_for():
    assert web_scraper.CONV_SCROLL_SETTLE_SECONDS == 1.0


def test_uid_placeholder_detection():
    assert web_scraper._looks_like_uid("100000000123")
    assert web_scraper._looks_like_uid(" 100000000123\xa0")
    assert not web_scraper._looks_like_uid("小明")
    assert not web_scraper._looks_like_uid("12345")


class _FakeConvPage:
    """模拟抖音会话列表：滑动后标题先变 uid，过了 hydrate_delay 才变昵称。

    ``available`` 模拟「抖音是滚到底才异步拉下一页」：贴底后要连续
    ``load_delay_rounds`` 次滚不动，才又渲染出一页。
    ``numeric_names`` 里的会话，昵称本身就是一串数字（不是 uid 占位，永远不会变）。
    """

    def __init__(self, total=30, view=8, step=6, hydrate_delay=0.15,
                 load_delay_rounds=0, numeric_names=()):
        self.total = total
        self.view = view
        self.step = step
        self.hydrate_delay = hydrate_delay
        self.load_delay_rounds = load_delay_rounds
        self.numeric_names = set(numeric_names)
        self.available = min(total, view)
        self.offset = 0
        self.bottom_rounds = 0
        self.last_scroll_at = -99.0
        self.uid_reads = 0
        self.uid_checks = 0

    async def evaluate(self, js, *args):
        now = time.monotonic()
        if "scrollTop += 400" in js:
            self.last_scroll_at = now
            return self._scroll_down()
        if "scrollTop = 0" in js:
            self.last_scroll_at = now
            self.offset = 0
            return None
        if "preview: previewEl" in js:
            return self._read(now)
        if "titles.push" in js:
            self.uid_checks += 1
            return self._uid_like_titles(now)
        raise AssertionError(f"意外的 evaluate: {js[:80]}")

    def _title(self, index, now):
        if index in self.numeric_names:
            return f"1380000{index:04d}"       # 昵称本来就是数字，永远不变
        if now - self.last_scroll_at < self.hydrate_delay:
            return f"100000000{index:03d}"
        return f"用户{index:03d}"

    def _visible(self, now):
        return list(range(self.offset, min(self.offset + self.view, self.available)))

    def _scroll_down(self):
        if self.offset + self.step < self.available:
            self.offset += self.step
            return False
        self.bottom_rounds += 1
        if (self.bottom_rounds > self.load_delay_rounds
                and self.available < self.total):
            self.available = min(self.total, self.available + self.step)
            self.bottom_rounds = 0
        return True

    def _read(self, now):
        rows = []
        for index in self._visible(now):
            title = self._title(index, now)
            if web_scraper._looks_like_uid(title):
                self.uid_reads += 1
            rows.append({"name": title, "nickname": title,
                         "time": "刚刚", "preview": ""})
        return rows

    def _uid_like_titles(self, now):
        return [self._title(index, now) for index in self._visible(now)
                if web_scraper._looks_like_uid(self._title(index, now))]


def _scraper(page):
    s = WebChatScraper()
    s.page = page
    return s


def _fast_settle(monkeypatch, settle=0.05, wait=1.0, poll=0.02):
    monkeypatch.setattr(web_scraper, "CONV_SCROLL_SETTLE_SECONDS", settle)
    monkeypatch.setattr(web_scraper, "CONV_NICKNAME_WAIT_SECONDS", wait)
    monkeypatch.setattr(web_scraper, "CONV_NICKNAME_POLL_SECONDS", poll)


def test_every_scroll_waits_before_the_caller_reads_the_dom(monkeypatch):
    """滑动之后必须先停一下；这是「滑完立刻读会读到 uid」的兜底。"""
    _fast_settle(monkeypatch, settle=0.1, wait=0.0)
    page = _FakeConvPage(hydrate_delay=0.0)
    s = _scraper(page)

    started = time.monotonic()
    asyncio.run(s._scroll_conv_list_down())
    assert time.monotonic() - started >= 0.1

    started = time.monotonic()
    asyncio.run(s._scroll_conv_list_to_top())
    assert time.monotonic() - started >= 0.1


def test_settle_keeps_waiting_while_titles_are_still_uid(monkeypatch):
    """昵称没渲染完就一直等，而不是固定停 1 秒就走。"""
    _fast_settle(monkeypatch, settle=0.02, wait=1.0, poll=0.02)
    page = _FakeConvPage(hydrate_delay=0.25)
    s = _scraper(page)

    started = time.monotonic()
    asyncio.run(s._scroll_conv_list_down())
    waited = time.monotonic() - started

    assert waited >= 0.25, "必须等昵称渲染出来"
    assert waited < 1.0, "昵称提前渲染出来就不该干等到上限"
    assert page.uid_reads == 0


def test_refresh_waits_for_nicknames_and_reads_the_whole_list(monkeypatch):
    """一次刷新就该拿到整份列表：不漏页、也不把 uid 占位收成重复会话。"""
    _fast_settle(monkeypatch)
    page = _FakeConvPage(total=30, view=8, step=6, hydrate_delay=0.15,
                         load_delay_rounds=2)
    s = _scraper(page)

    convs = asyncio.run(s._load_all_conversations())

    names = [c["nickname"] for c in convs]
    assert len(names) == page.total, "滚到底后异步补上的那一页不能漏"
    assert len(set(names)) == len(names), "同一个会话不能既收 uid 又收昵称"
    assert page.uid_reads == 0, "读 DOM 时标题必须已经渲染成昵称"
    assert not [n for n in names if web_scraper._looks_like_uid(n)]


def test_short_list_still_finishes(monkeypatch):
    """列表很短时不会因为「要连续几轮没新增」而空转到 120 轮。"""
    _fast_settle(monkeypatch, settle=0.01, wait=0.0)
    page = _FakeConvPage(total=3, view=8, step=6, hydrate_delay=0.0)
    s = _scraper(page)

    convs = asyncio.run(s._load_all_conversations())

    assert [c["nickname"] for c in convs] == ["用户000", "用户001", "用户002"]


def test_numeric_nickname_is_kept_and_only_waited_for_once(monkeypatch):
    """对方昵称本身就是一串数字：会话照样收进来，但只空等一次，之后不再等。

    这种会话等多久都不会变成昵称，不能每轮都干等到上限（那会把刷新拖得很慢），
    也不能因为「看起来像 uid」就把它丢掉。
    """
    _fast_settle(monkeypatch, settle=0.02, wait=0.2, poll=0.02)
    page = _FakeConvPage(total=6, view=8, step=6, hydrate_delay=0.0,
                         numeric_names={2})
    s = _scraper(page)

    convs = asyncio.run(s._load_all_conversations())

    names = [c["nickname"] for c in convs]
    assert len(names) == 6
    assert "13800000002" in names, "昵称是数字的会话不能被丢掉"
    assert page.uid_checks <= 20, (
        f"等满一轮上限后就该记住「这是真昵称」，不该每轮都空等（检查了 {page.uid_checks} 次）"
    )
