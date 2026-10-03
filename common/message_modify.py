"""从抖音消息包里读出「这条消息发生过什么」。

抖音把四种不同的行为分别记在消息包的字段里，但它们的「时间」都写在同一个
protobuf 第 11 号字段上（沿用旧字段名 ``is_recalled``）。所以只看第 11 号
字段分不出具体是哪种操作，必须看其它字段：

* **仅看一次**：第 6 号字段（消息类型）``= 104``；扩展里还有
  ``s:once_view_count`` / ``s:once_view_done``。
* **撤回**：扩展 ``f9`` 里有 ``a:recalled_msg_type``。
* **编辑**：扩展 ``f9`` 里有 ``s:edit_info``（JSON，含编辑者 uid 和
  ``content_is_edited``）与 ``s:edit_count``。
* **表情快捷回复**：第 15 号字段，记录了表情名、回应者 uid 和回应时间。

这里产出的 ``modify`` 结构会被写进 ``messages.raw_data``，阅读端（前端
查看器）只认它，不再靠「有时间戳就猜是编辑」。
"""
import json

VIEW_ONCE_TYPE_CODE = 104

# 判定顺序：能同时命中的情况极少（全库 1 条），命中多个时按这个顺序展示。
KIND_ORDER = ("view_once", "recall", "edit", "reaction")


def _read_varint(buf, pos):
    result = 0
    shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, pos


def _iter_fields(buf):
    """逐字段产出 ``(字段号, 类型, 值)``；值按 wire type 给 int 或 bytes。"""
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except IndexError:
            return
        fn, wt = tag >> 3, tag & 7
        if fn == 0 or fn > 500:
            return
        if wt == 0:
            value, pos = _read_varint(buf, pos)
            yield fn, wt, value
        elif wt == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                return
            yield fn, wt, bytes(buf[pos:pos + length])
            pos += length
        elif wt == 1:
            yield fn, wt, bytes(buf[pos:pos + 8])
            pos += 8
        elif wt == 5:
            yield fn, wt, bytes(buf[pos:pos + 4])
            pos += 4
        else:
            return


def _text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value)


def _ext_map(fields):
    """``f9`` 是「键 → 值」的扩展列表，键形如 ``a:msg_scene`` / ``s:edit_info``。"""
    out = {}
    for fn, wt, value in fields:
        if fn != 9 or wt != 2:
            continue
        key = val = ""
        for sub_fn, sub_wt, sub_value in _iter_fields(value):
            if sub_fn == 1 and sub_wt == 2:
                key = _text(sub_value)
            elif sub_fn == 2 and sub_wt == 2:
                val = _text(sub_value)
        if key:
            out[key] = val
    return out


def _reactions(fields):
    """``f15`` 是表情快捷回复记录：表情名 + (回应者 uid, 时间)。

    同一次回应会出现两条（``se:[爱心]`` 是展示名、``e:love`` 是引擎名），
    按 uid 合并，优先保留 ``se:`` 那条。
    """
    by_uid = {}
    for fn, wt, value in fields:
        if fn != 15 or wt != 2:
            continue
        name = ""
        uid = None
        stamp = 0
        for sub_fn, sub_wt, sub_value in _iter_fields(value):
            if sub_fn == 1 and sub_wt == 2:
                name = _text(sub_value)
            elif sub_fn == 2 and sub_wt == 2:
                for inner_fn, inner_wt, inner in _iter_fields(sub_value):
                    if inner_fn != 1 or inner_wt != 2:
                        continue
                    for leaf_fn, leaf_wt, leaf in _iter_fields(inner):
                        if leaf_fn == 1 and leaf_wt == 0:
                            uid = str(leaf)
                        elif leaf_fn == 3 and leaf_wt == 0:
                            stamp = int(leaf)
        key = uid or name
        if not key:
            continue
        item = by_uid.setdefault(key, {"uid": uid or "", "name": "", "e_name": "", "time": stamp})
        if stamp:
            item["time"] = stamp
        if name.startswith("se:"):
            item["name"] = item["name"] or name[3:]
        elif name.startswith("e:"):
            item["e_name"] = item["e_name"] or name[2:]
        elif name:
            item["name"] = item["name"] or name
    out = []
    for item in by_uid.values():
        out.append({
            "emoji": item["name"] or item["e_name"],
            "uid": item["uid"],
            "time": item["time"],
        })
    return out


def _from_fields(type_code, ext, reactions, modify_time):
    """把「字段」翻译成 ``kinds``。整包缺失时也走这里（只靠 raw_data.fields）。"""
    kinds = []
    detail = {}
    if str(type_code) == str(VIEW_ONCE_TYPE_CODE) or "s:once_view_count" in ext or "s:once_view_done" in ext:
        kinds.append("view_once")
    if "a:recalled_msg_type" in ext:
        kinds.append("recall")
        try:
            detail["recalled_type"] = int(ext["a:recalled_msg_type"])
        except (TypeError, ValueError):
            pass
    if "s:edit_info" in ext:
        kinds.append("edit")
        try:
            info = json.loads(ext["s:edit_info"])
        except (TypeError, ValueError):
            info = {}
        if isinstance(info, dict):
            detail["edited"] = bool(info.get("content_is_edited"))
            if info.get("content_editor"):
                detail["editor"] = str(info["content_editor"])
            if info.get("content_edit_time"):
                detail["edit_time"] = int(info["content_edit_time"])
        try:
            detail["edit_count"] = int(ext.get("s:edit_count") or 1)
        except (TypeError, ValueError):
            detail["edit_count"] = 1
    if reactions:
        kinds.append("reaction")
        detail["reactions"] = reactions
    if not kinds:
        if not modify_time:
            return None
        kinds.append("unknown")
    ordered = [kind for kind in KIND_ORDER if kind in kinds]
    result = {"kinds": ordered or kinds}
    result.update(detail)
    if modify_time:
        result["time"] = int(modify_time)
    return result


def parse_modify(packet):
    """从整包 protobuf 里读出 ``modify``；没有发生过任何操作时返回 None。"""
    if not packet:
        return None
    if isinstance(packet, str):
        packet = packet.encode("utf-8")
    top = list(_iter_fields(packet))
    type_code = None
    modify_time = 0
    for fn, wt, value in top:
        if wt == 0 and fn == 6:
            type_code = int(value)
        elif wt == 0 and fn == 11:
            modify_time = int(value)
    ext = _ext_map(top)
    reactions = _reactions(top)
    return _from_fields(type_code, ext, reactions, modify_time)


def modify_from_raw(raw):
    """整包缺失时的兜底：只用 ``messages.raw_data`` 里已有的字段推断。

    回填模式之前抓到的老消息（超过一年的）没有整包，只有 ``fields`` 摘要，
    这条路径尽量还原，还原不了就不标。
    """
    if not isinstance(raw, dict):
        return None
    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        return None
    modify_time = raw.get("is_recalled") or 0
    type_code = fields.get("f6") if fields.get("f6") is not None else raw.get("type_code")
    ext = _ext_map([
        # fields 里的字符串来自 JS 的 TextDecoder('utf-8')，按 UTF-8 编回去即可
        # 还原原始字节（控制字符与多字节字符都能一一对应）。二进制条目（形如
        # {"bytes": N, "head": ...}）不是文本扩展，跳过。
        (9, 2, entry.encode("utf-8"))
        for entry in (fields.get("f9") or []) if isinstance(entry, str) and entry
    ])
    reactions = [{"emoji": "", "uid": "", "time": 0}] if fields.get("f15") else []
    return _from_fields(type_code, ext, reactions, modify_time)
