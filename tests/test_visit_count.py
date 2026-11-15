import os

import pytest

from backend import visit_count


@pytest.fixture
def id_path(tmp_path, monkeypatch):
    path = tmp_path / "visit_id"
    monkeypatch.setattr(visit_count, "ID_PATH", str(path))
    monkeypatch.setattr(visit_count, "_cached", None)
    return path


def test_first_call_creates_id_and_writes_it(id_path):
    visit_id = visit_count.get_id()
    assert len(visit_id) == 32
    assert all(ch in "0123456789abcdef" for ch in visit_id)
    assert id_path.read_text(encoding="utf-8").strip() == visit_id


def test_existing_id_is_reused_after_restart(id_path):
    id_path.write_text("a" * 32 + "\n", encoding="utf-8")
    assert visit_count.get_id() == "a" * 32
    # 模拟重启：内存缓存清掉，还得读到同一个号
    visit_count._cached = None
    assert visit_count.get_id() == "a" * 32


def test_garbage_file_is_replaced(id_path):
    id_path.write_text("不是编号\n", encoding="utf-8")
    visit_id = visit_count.get_id()
    assert len(visit_id) == 32
    assert visit_count.load() == visit_id


def test_url_joins_prefix_and_id():
    assert visit_count.PING_BASE == "https://count.bxtjsw.shop/device/"
    assert visit_count.url_for("abc123") == "https://count.bxtjsw.shop/device/abc123"


def test_ping_is_silent_under_pytest(id_path, monkeypatch):
    called = []
    monkeypatch.setattr(visit_count, "_send", lambda url: called.append(url))
    visit_count.ping()
    assert called == []


def test_ping_sends_the_request_once(id_path, monkeypatch):
    import threading

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(visit_count, "_inflight", False)
    called = []
    done = threading.Event()

    def fake_send(url):
        called.append(url)
        done.set()

    monkeypatch.setattr(visit_count, "_send", fake_send)
    visit_count.ping()
    assert done.wait(3), "上报线程没有跑起来"
    assert called == [visit_count.url_for(visit_count.get_id())]
    assert visit_count._inflight is False
