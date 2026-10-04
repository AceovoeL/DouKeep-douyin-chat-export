"""把「以前没存下来」的表情与卡片图补下来（媒体本地化的一部分）。

有两类消息在旧版本里漏了：

* **「小火人」表情（aweType=519）** —— 早先不在表情名单里，落库成了普通消息
  （``msg_type=0``），图没下载，只剩 ``content`` 里那几个字（「笑死」「续火花」）。
* **卡片自带的图** —— 群邀请卡的群头像、豆包卡（aweType=6001）的封面，以前压根
  不下载。

它们的下载地址都还在 ``raw_data.content_json`` 里（``url.url_list`` / ``icon`` /
``cover_url``），所以不用重新抓包，拿旧消息补一次下载就能把历史补回来。
注意**抖音的链接是带签名的**：过期了（403）谁也救不回来，只能把还没过期的先存下。

三个入口共用这一份实现：

* 采集时（``extractor/web_scraper.py`` 每轮结束）自动补一次；
* 面板「媒体下载 → 下载历史图片」（``backend/control_panel.py``）里一起补；
* 直接跑 ::func:`backfill_media` 也能当命令行小工具用。
"""
from __future__ import annotations

import json
import os

from common import card_icons
from common.paths import EMOJI_DIR

#: 一条 SQL 挑出「该补图」的行：表情（含 519）、卡片。SQLite 的 json_extract
#: 不一定可用（编译选项各异），所以用 LIKE 粗筛，再在 Python 里精确判定
#: （见 :func:`backfill_row_media`）—— 粗筛宁可多放几条进去，也不能漏。
#: 真实库里的 content_json 是**双层编码**（``\"aweType\":519``），但测试和别的写入
#: 路径可能存成普通 JSON（``"aweType": 519``），所以两种键名、两种空格都列上。
#: 注意别只写 `%519%`：那会命中正文/URL 里带这三个数字的普通消息（实测两万条）。
#: msg_type=2 要求 media_url 非空：没地址的表情下载不来，别每次都白跑一趟。
def _json_like_patterns(key: str, value: str) -> tuple[str, ...]:
    """``("aweType", "519")`` → 四种 ``LIKE`` 片段。

    键和值都可能被转义一层（``\\"aweType\\":\\"群聊邀请\\"``），所以各来一遍。
    """
    quoted = f'"{key}"'
    escaped = f'\\"{key}\\"'
    return (
        f'{quoted}:{value}',
        f'{escaped}:{value}',
        f'{quoted}: {value}',
        f'{escaped}: {value}',
    )


_ROW_HINTS = (
    _json_like_patterns("aweType", "519")
    + _json_like_patterns("aweType", "6001")
    + _json_like_patterns("type_desc", '"群聊邀请"')
    + _json_like_patterns("type_desc", '\\"群聊邀请\\"')
)
_MEDIA_ROW_WHERE = (
    "(("
    # 表情：有 media_url 或载荷里带 url_list 才有地址可下
    "msg_type = 2 AND (media_url LIKE 'http%' OR raw_data LIKE '%url_list%')"
    ") OR ("
    # 卡片（有载荷才谈得上"从旧载荷里补图"）
    "raw_data IS NOT NULL AND raw_data <> '' AND ("
    + " OR ".join(f"raw_data LIKE '%{hint}%'" for hint in _ROW_HINTS)
    + ")))"
)
_MEDIA_ROW_COLUMNS = "msg_id, msg_type, media_url, media_local_path, raw_data, content"


def is_media_payload(cj: dict, msg_type=None) -> bool:
    """这条载荷是不是「该补图」的那几种：卡片，或表情（含小火人 519）。"""
    if card_icons.card_icon_url(cj):
        return True
    if str(cj.get("aweType")) in card_icons.EMOJI_AWE_TYPES:
        return True
    # 老数据里 msg_type=2 就是表情，哪怕 aweType 已经看不出来
    return msg_type == 2


def content_json(row: dict) -> dict:
    """行里的 ``raw_data.content_json``（可能是字符串，也可能缺失）。"""
    raw = row.get("raw_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    if not isinstance(raw, dict):
        return {}
    cj = raw.get("content_json")
    if isinstance(cj, str):
        try:
            cj = json.loads(cj)
        except (ValueError, TypeError):
            return {}
    return cj if isinstance(cj, dict) else {}


def has_media_source(row: dict) -> bool:
    """这条现在真的下得动吗：载荷里认得出来，并且有地址。"""
    cj = content_json(row)
    if cj and is_media_payload(cj, row.get("msg_type")):
        if card_icons.card_icon_url(cj):
            return True
        if emoji_media_url(cj, row.get("media_url")):
            return True
    # 老数据可能连载荷都没有，只剩数据库里记着的地址；msg_type=2 就是表情，照下。
    return row.get("msg_type") == 2 and str(row.get("media_url") or "").startswith("http")


def iter_media_rows(conn, *, exclude: set[str] | None = None) -> list[dict]:
    """还没存下图、但**现在确实下得动**的行（表情 + 卡片）。

    SQL 那一步只是粗筛（可能多放几条进来）；这里再用 ``has_media_source`` 过一遍，
    免得把"连地址都没有"的记录也算成待补、白记一堆失败。

    ``exclude`` 是调用方已经处理过的 msg_id（例如面板那轮图片回填自己下过、
    只是失败的条目），别再算第二遍。
    """
    rows = conn.execute(
        f"SELECT {_MEDIA_ROW_COLUMNS} FROM messages "
        f"WHERE (media_local_path IS NULL OR media_local_path = '') AND {_MEDIA_ROW_WHERE}",
    ).fetchall()
    exclude = exclude or set()
    return [row for row in (dict(r) for r in rows)
            if row["msg_id"] not in exclude and has_media_source(row)]


def backfill_media(conn, *, limit: int | None = None, on_progress=None,
                   exclude: set[str] | None = None) -> dict:
    """把缺的表情/卡片图补下来，成功的那几条顺手写回 ``media_local_path``。

    ⚠️ 这个函数**必须在打开 ``conn`` 的同一个线程里跑**（sqlite3 的连接不能跨线程）。
    面板那边是另开一个连接、整段丢进 ``run_in_executor`` 执行的，进度靠
    ``on_progress`` 回调（每处理一条调一次）带回主线程。

    ``exclude``：调用方自己已经处理过的 msg_id，别重复统计。

    返回 ``{"total", "ok", "failed", "paths"}``：``paths`` 是
    ``msg_id → 相对路径``（测试和日志用）。
    """
    rows = iter_media_rows(conn, exclude=exclude)
    if limit is not None:
        rows = rows[:limit]
    result = {"total": len(rows), "ok": 0, "failed": 0, "paths": {}}
    for index, row in enumerate(rows, start=1):
        rel, source = backfill_row_media(row)
        if not rel:
            result["failed"] += 1
        else:
            conn.execute(
                "UPDATE messages SET media_local_path = ?, "
                "media_url = COALESCE(NULLIF(media_url, ''), ?) WHERE msg_id = ?",
                (rel, source, row["msg_id"]),
            )
            result["ok"] += 1
            result["paths"][row["msg_id"]] = rel
        if on_progress is not None:
            on_progress(index, result)
    if result["ok"]:
        conn.commit()
    return result


def backfill_row(row: dict) -> str | None:
    """单独补一条：返回相对 ``data/media`` 的路径，补不到返回 None。"""
    return backfill_row_media(row)[0]


def backfill_row_media(row: dict) -> tuple[str | None, str | None]:
    """补一条，返回 ``(相对路径, 这条用的原始下载地址)``。

    地址要一起带出来：老数据里 ``media_url`` 往往是空的（小火人当初就是当普通消息
    存的），补完之后把它记上，下次就不会再被当成"缺图"。
    """
    cj = content_json(row)
    if cj and is_media_payload(cj, row.get("msg_type")):
        icon = card_icons.card_icon_url(cj)
        if icon:
            return card_icons.save_card_icon(icon), icon
        url = emoji_media_url(cj, row.get("media_url"))
        return save_emoji_media(cj, row.get("media_url")), url
    # 连载荷都没有的老表情：数据库里记着的地址就是唯一线索
    url = row.get("media_url")
    if row.get("msg_type") == 2 and isinstance(url, str) and url.startswith("http"):
        return save_emoji_media({}, url), url
    # SQL 那一步只是粗筛，这里把命中的"其它东西"挡掉
    return None, None


def emoji_media_url(cj: dict, row_url: str | None = None) -> str:
    """表情的图：优先数据库里记的 media_url，没有就回载荷里找。"""
    if isinstance(row_url, str) and row_url.startswith("http"):
        return row_url
    return card_icons.image_url(cj.get("url"))


def save_emoji_media(cj: dict, row_url: str | None = None) -> str | None:
    """下载表情图（明文 CDN 链接），存进 ``data/media/emoji/``。"""
    url = emoji_media_url(cj, row_url)
    if not url:
        return None
    from extractor.web_scraper import _save_emoji

    os.makedirs(EMOJI_DIR, exist_ok=True)
    try:
        return _save_emoji(url, EMOJI_DIR)
    except Exception:  # 过期链接 / 网络问题：这条补不了，别把整轮打断
        return None
