"""「撤回 / 编辑 / 表情快捷回复 / 仅看一次」的判定（common/message_modify.py）。

重点是编辑那条：小火人 AI 的**每条**消息都带一段 ``s:edit_info``，但里面
``content_is_edited`` 是 false（疑似生成消息时借用了编辑通道）。早先按「有这段
就是编辑」硬认，每组 AI 消息的最后一条都会被标成「已编辑」（用户 2026-10-11 反馈）。
"""
import json

from common.message_modify import modify_from_raw, parse_modify


def _field(number, payload: bytes) -> bytes:
    """拼一个 wire type=2 的 protobuf 字段。"""
    return _tag(number) + _varint(len(payload)) + payload


def _tag(number: int) -> bytes:
    return _varint((number << 3) | 2)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _ext(key: str, value: str) -> bytes:
    """f9 里的一条「键 → 值」扩展。"""
    return _field(9, _field(1, key.encode()) + _field(2, value.encode()))


def _packet(ext_entries=(), type_code=None, modify_time=None) -> bytes:
    out = b""
    for entry in ext_entries:
        out += entry
    if type_code is not None:
        out += _varint((6 << 3) | 0) + _varint(type_code)
    if modify_time is not None:
        out += _varint((11 << 3) | 0) + _varint(modify_time)
    return out


def _edit_info(edited: bool, editor="100000000001", time=1787568019662) -> str:
    return json.dumps({
        "content_is_edited": edited,
        "content_editor": editor,
        "content_edit_time": time,
    })


# ── 真编辑（人类改过消息）─────────────────────────────────────────────

def test_real_edit_is_reported_as_edit():
    packet = _packet(
        [_ext("s:edit_info", _edit_info(True)), _ext("s:edit_count", "1")],
        modify_time=1787568019662,
    )
    result = parse_modify(packet)
    assert result["kinds"] == ["edit"]
    assert result["edited"] is True
    assert result["editor"] == "100000000001"
    assert result["edit_time"] == 1787568019662
    assert result["edit_count"] == 1


# ── 假编辑（小火人 AI 的每条消息）────────────────────────────────────

def test_edit_info_with_content_is_edited_false_is_not_an_edit():
    """AI 的消息带 edit_info 但内容没改过 —— 不能标成「已编辑」。

    这条消息本身带改动时间，所以结论是「有种操作、但认不出是什么」（unknown），
    关键是**不含 edit**：界面不会挂上「已编辑」那个标签。
    """
    packet = _packet(
        [_ext("s:edit_info", _edit_info(False)), _ext("s:edit_count", "1")],
        modify_time=1789228700475,
    )
    result = parse_modify(packet)
    assert result is not None
    assert "edit" not in result["kinds"]
    assert "edited" not in result, "不是真编辑就不该留下 edited 明细"


def test_false_edit_detail_is_not_written_into_raw_data_path():
    """整包缺失时也一样：不是真编辑就不该留下 editor / edit_time 明细。"""
    raw = {
        "fields": {
            "f9": [_ext("s:edit_info", _edit_info(False)).decode("latin-1")],
            "f6": 7,
        },
        "is_recalled": 1789228700475,
    }
    result = modify_from_raw(raw)
    assert result is not None
    assert "edit" not in result["kinds"]
    assert "editor" not in result and "edit_time" not in result


def test_false_edit_does_not_hide_a_real_recall():
    """同一条消息既被撤回、又带着假编辑标记时，撤回要照旧显示。"""
    packet = _packet(
        [
            _ext("a:recalled_msg_type", "7"),
            _ext("s:edit_info", _edit_info(False)),
        ],
        modify_time=1789228700475,
    )
    result = parse_modify(packet)
    assert result["kinds"] == ["recall"]
    assert result["recalled_type"] == 7


def test_false_edit_does_not_hide_a_real_reaction():
    packet = _packet(
        [_ext("s:edit_info", _edit_info(False))],
        modify_time=1789228700475,
    ) + _field(15, _field(1, b"se:[\xe7\x88\xb1\xe5\xbf\x83]"))
    result = parse_modify(packet)
    assert result["kinds"] == ["reaction"]


# ── 字段缺失时保持老行为（不要因为这次改动丢掉别的判定）────────────────

def test_edit_info_without_the_flag_still_counts_as_edit():
    """老数据里没有 content_is_edited 这个键：按老样子算编辑。"""
    packet = _packet(
        [_ext("s:edit_info", json.dumps({"content_editor": "100000000001"}))],
        modify_time=1787568019662,
    )
    result = parse_modify(packet)
    assert result["kinds"] == ["edit"]
    assert result["edited"] is False


def test_broken_edit_info_json_falls_back_to_edit():
    packet = _packet([_ext("s:edit_info", "{not json")], modify_time=1787568019662)
    result = parse_modify(packet)
    assert result["kinds"] == ["edit"]


def test_recall_and_view_once_still_work():
    assert parse_modify(_packet([_ext("a:recalled_msg_type", "7")]))["kinds"] == ["recall"]
    assert parse_modify(_packet([], type_code=104))["kinds"] == ["view_once"]
    assert parse_modify(_packet([_ext("s:once_view_done", "1")]))["kinds"] == ["view_once"]


def test_no_operation_returns_none():
    assert parse_modify(_packet()) is None
    assert parse_modify(b"") is None
