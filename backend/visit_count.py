from __future__ import annotations

import os
import re
import threading
import urllib.parse
import urllib.request
import uuid

from common import paths

#: 请求地址前缀（后面拼本机编号）。
PING_BASE = "https://count.bxtjsw.shop/device/"

#: 编号文件：单独一个文件，不和 panel_config.json 混在一起（坏了大不了换一个号）。
ID_PATH = os.path.join(paths.CONFIG_DIR, "visit_id")

_TIMEOUT = 3.0

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_id_lock = threading.Lock()
_inflight_lock = threading.Lock()
_cached: str | None = None
_inflight = False


def _is_id(text: str) -> bool:
    return bool(_ID_RE.match(text or ""))


def load() -> str:
    """读已保存的编号；没有、读不到、内容不像话时返回空串。"""
    try:
        with open(ID_PATH, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return ""
    return text if _is_id(text) else ""


def _create() -> str:
    text = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(ID_PATH), exist_ok=True)
        with open(ID_PATH, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    except OSError:
        pass            # 写不进去就先用内存里这个号，下次启动再试
    return text


def get_id() -> str:
    """本机编号：磁盘上有就用，没有才生成并落盘；进程内只算一次。"""
    global _cached
    with _id_lock:
        if _cached is None:
            _cached = load() or _create()
        return _cached


def url_for(visit_id: str) -> str:
    return PING_BASE + urllib.parse.quote(visit_id)


def _disabled() -> bool:
    # 跑测试时别真往外发请求：pytest 每个用例都会设这个环境变量。
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _send(url: str) -> None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "douyin-chat-export"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT):
            pass
    except Exception:
        pass            # 发不出去就算了，不能影响调用它的页面


def _run_once(url: str) -> None:
    global _inflight
    try:
        _send(url)
    finally:
        with _inflight_lock:
            _inflight = False


def ping() -> None:
    """发一次请求：立即返回，同一时刻只发一个。"""
    global _inflight
    if _disabled():
        return
    url = url_for(get_id())          # 先拿号，避免两个锁互相等
    with _inflight_lock:
        if _inflight:
            return
        _inflight = True
    try:
        threading.Thread(target=_run_once, args=(url,), daemon=True).start()
    except Exception:
        with _inflight_lock:
            _inflight = False
