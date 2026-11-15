"""滑到会话列表最底部时，点一下最底部那个会话，让抖音重新刷新一次列表。

抖音网页版有时只渲染出一部分会话：只进聊天页、什么也不干，剩下的一直翻不出来。
点一下列表最底部那个会话会触发一次列表重渲染，没加载的会话往往就补上了；点完再
接着往下滑，能翻就继续收，翻不动才收工。
"""
import asyncio

from extractor import web_scraper
from extractor.web_scraper import WebChatScraper


class _FakeConvItem:
    """会话列表里的一项（只关心「点得到」这件事）。"""

    def __init__(self, page, index):
        self.page = page
        self.index = index

    async def evaluate(self, js):
        return False                      # 没有会话处于「正打开着」的状态

    async def click(self, timeout=3000, force=False):
        self.page.click_item(self.index)


class _StuckConvPage:
    """模拟「抖音只渲染出一部分会话、怎么滑都翻不动」的列表。

    只有点一下列表最底部那个会话（= 切到那个聊天），抖音才会再补出一页；
    ``pages_per_click=0`` 表示点了也没用（列表真的到头了）。
    """

    def __init__(self, total=18, view=6, step=6, pages_per_click=6):
        self.total = total
        self.view = view
        self.step = step
        self.pages_per_click = pages_per_click
        self.available = min(total, view)
        self.offset = 0
        self.clicks = 0
        self.clicked_indexes = []

    # --- 页面接口 -------------------------------------------------------
    async def evaluate(self, js, *args):
        if "scrollTop += 400" in js:
            return self._scroll_down()
        if "scrollTop = 0" in js:
            self.offset = 0
            return None
        if "preview: previewEl" in js:
            return self._read()
        if "titles.push" in js:
            return []                      # 昵称都渲染好了，没有 uid 占位
        raise AssertionError(f"意外的 evaluate: {js[:80]}")

    async def query_selector_all(self, selector):
        assert selector == web_scraper.SEL_CONV_ITEM, selector
        return [_FakeConvItem(self, i) for i in self._visible()]

    # --- 列表行为 -------------------------------------------------------
    def _visible(self):
        return list(range(self.offset, min(self.offset + self.view, self.available)))

    def _scroll_down(self):
        """滑动本身永远翻不出新会话 —— 这正是网页版那个毛病。"""
        if self.offset + self.step < self.available:
            self.offset += self.step
            return False
        self.offset = max(0, self.available - self.view)
        return True

    def _read(self):
        return [{"name": f"用户{i:03d}", "nickname": f"用户{i:03d}",
                 "time": "刚刚", "preview": ""} for i in self._visible()]

    def click_item(self, index):
        # 必须点的是 DOM 里最底下那一项（列表滚到哪，最底下一项就是哪）
        assert index == max(self._visible()), (
            f"应该点列表最底部那一项，实际点了第 {index} 项"
        )
        self.clicks += 1
        self.clicked_indexes.append(index)
        self.available = min(self.total, self.available + self.pages_per_click)
        self.offset = 0                   # 切会话后列表从头重排


def _scraper(page):
    s = WebChatScraper()
    s.page = page
    return s


def _fast(monkeypatch):
    monkeypatch.setattr(web_scraper, "CONV_SCROLL_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(web_scraper, "CONV_NICKNAME_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(web_scraper, "CONV_NICKNAME_POLL_SECONDS", 0.0)
    monkeypatch.setattr(web_scraper, "CONV_BOTTOM_CLICK_SETTLE_SECONDS", 0.0)


def test_clicking_the_bottom_conversation_loads_the_rest(monkeypatch):
    """滑到底翻不动时，点一下最底部会话让抖音重刷列表，剩下的一页就能收上来。"""
    _fast(monkeypatch)
    page = _StuckConvPage(total=18, view=6, step=6, pages_per_click=6)
    s = _scraper(page)

    convs = asyncio.run(s._load_all_conversations())

    names = [c["nickname"] for c in convs]
    assert len(names) == 18, (
        f"点完要继续往下翻、把没加载的会话补齐（只收到 {len(names)} 个）"
    )
    assert len(set(names)) == 18, "同一个会话不能被重复收进来"
    assert page.clicks >= 1, "卡在最底部时必须点一下列表最底部那个会话"
    assert page.clicked_indexes, "点的必须是列表里最底部那一项"


def test_bottom_click_gives_up_when_the_list_really_ends(monkeypatch):
    """点了也没多出会话时不能一直点（列表真到头了就该收工）。"""
    _fast(monkeypatch)
    page = _StuckConvPage(total=30, view=6, step=6, pages_per_click=0)
    s = _scraper(page)

    convs = asyncio.run(s._load_all_conversations())

    assert len(convs) == 6, "列表真到头了就保持现状，别凭空多收"
    assert page.clicks == web_scraper.CONV_BOTTOM_CLICK_GIVE_UP_ROUNDS, (
        f"连着几次点了没效果就该停（实际点了 {page.clicks} 次）"
    )
