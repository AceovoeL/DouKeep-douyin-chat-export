"""Resolve Douyin merged records from preserved inline bodies and local messages.

13600 has two observed formats: msg_ids/list_content (summary only), and
is_new_mr_share + inline_content (full MessageBody objects). Parse in Python
so 64-bit message/user IDs never pass through JavaScript floating point.
"""
import json


def as_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def content_json(message):
    raw = as_object(message.get("raw_data"))
    return as_object(raw.get("content_json")) or as_object(message.get("content"))


def _array(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


from common.message_kinds import is_view_once
from common.card_icons import EMOJI_AWE_TYPES as _EMOJI_AWE_TYPES
from common.card_icons import MUSIC_CARD_AWE_TYPE

# Share-card aweTypes used by the main scraper / preview UI. 800 is the older
# video share; 11054+ is the current web video/photo share; 10500 is a quoted
# comment (must not be lumped in with 11054); 11029 is goods. 10401 is
# dual-purpose: goods cards *and* "view once" text messages (see is_view_once).
_SHARE_AWETYPES = {
    "800", "801", "803", "805",
    "10401", "10500", "11029",
    "11054", "11055", "11063", "11066", "11067", "11069", "11070",
}


_VIDEO_SHARE_AWETYPES = {
    "800", "801", "803", "805",
    "11054", "11055", "11063", "11066", "11067", "11069", "11070",
}


def _descriptor_awe(descriptor):
    value = descriptor.get("awe_type", descriptor.get("aweType"))
    return "" if value in (None, "") else str(value)


def _is_share_payload(cj, awe):
    # aweType=10401 with real text and no card fields is a "view once" text
    # message; the scraper stores it as plain text, so merged records must too.
    if is_view_once(cj):
        return False
    if awe in _SHARE_AWETYPES:
        return True
    # Dynamic photo/article/goods layouts (e.g. 分享图文) share these fields.
    # Do not key off cover_url alone: aweType=9000 watch-together also has one.
    if awe in {"9000", "13600"}:
        return False
    text = str(cj.get("text") or cj.get("push_detail") or "")
    if text.startswith("[分享") or text.startswith("分享["):
        return True
    return bool(
        cj.get("im_dynamic_patch") or cj.get("awemeType")
        or cj.get("itemId") or cj.get("item_id") or cj.get("comment")
        or cj.get("content_title")
    )


def _inline_row(body, descriptor, preview, parent_id):
    cj = dict(as_object(body.get("content")))
    if not cj:
        raise ValueError("合并记录正文缺失或损坏")
    # Merged-record bodies are often flattened to aweType=0 plus "[分享视频] 标题".
    # msg_ids.awe_type still carries the original share kind (11054 vs 10500).
    desc_awe = _descriptor_awe(descriptor)
    if str(cj.get("aweType") or "") in {"", "0"} and desc_awe:
        cj["aweType"] = int(desc_awe) if desc_awe.isdigit() else desc_awe
    awe = str(cj.get("aweType", ""))
    if awe == "10500" and not cj.get("comment"):
        # Quoted-comment shares; never copy a video share's caption into comment.
        cj["comment"] = cj.get("text") or ""
    msg_type = 1
    content = cj.get("text") or cj.get("content_title") or ""
    media_url = None
    if awe in _EMOJI_AWE_TYPES:
        msg_type, content = 2, cj.get("display_name") or "[表情]"
        urls = as_object(cj.get("url")).get("url_list")
        media_url = urls[0] if isinstance(urls, list) and urls else None
    elif awe == str(MUSIC_CARD_AWE_TYPE):
        # 豆包分享卡：转发记录里当卡片画（前端 getMusicCard），正文用标题。
        msg_type, content = 1, cj.get("title") or cj.get("push_detail") or "[分享视频]"
    elif awe in {"2702", "2703", "2704"}:
        msg_type, content = 3, "[图片]"
    elif as_object(cj.get("video")).get("vid"):
        msg_type, content = 5, "[视频]"
    elif _is_share_payload(cj, awe):
        # Keep comment text for 10500; video/photo shares use title / push_detail.
        if awe == "10500" or (cj.get("comment") and awe not in _VIDEO_SHARE_AWETYPES):
            content = cj.get("comment") or content or "[分享评论]"
        else:
            # aweType=805 是「限时日常」作品分享（卡片不带标题）。
            kind = "限时日常" if awe == "805" else "视频"
            content = (
                cj.get("content_title") or cj.get("push_detail")
                or content or f"[分享{kind}]"
            )
        msg_type = 4
    elif cj.get("tips") or cj.get("resource_url"):
        msg_type = 0
    # Preserving the entire content_json also handles cards/nested records that
    # a previous scraper classified as plain text or other.
    content = content or preview.get("text") or "[消息]"
    sid = str(body.get("server_message_id") or descriptor.get("msg_id") or "")
    timestamp = body.get("create_time") or descriptor.get("create_time") or 0
    timestamp = int(timestamp)
    if timestamp > 10**14:
        timestamp //= 1_000_000
    elif timestamp > 10**11:
        timestamp //= 1000
    return {
        "msg_id": f"{parent_id}/srv_{sid}", "conv_id": str(body.get("conversation_id") or ""),
        "sender_uid": str(body.get("sender") or descriptor.get("uid") or ""),
        "sender_name": preview.get("nick_name") or "", "timestamp": timestamp,
        "content": content, "msg_type": msg_type, "media_url": media_url,
        "media_local_path": None, "server_message_id": sid,
        "raw_data": json.dumps({"content_json": json.dumps(cj, ensure_ascii=False)}, ensure_ascii=False),
    }


def _overlay_local_media(inline_row, local):
    row = dict(inline_row)
    for key in ("media_local_path", "media_url", "voice_transcription", "voice_transcription_status"):
        if local.get(key):
            row[key] = local[key]
    return row


def resolve_forward(message, conn, *, ancestors=(), budget=None, fetch_media=True, media_budget=None):
    cj = content_json(message)
    if str(cj.get("aweType")) != "13600":
        return None
    from extractor.im_media import ensure_row_media

    budget = budget if budget is not None else [1000]
    media_budget = media_budget if media_budget is not None else [40]
    descriptors = _array(cj.get("msg_ids"))
    downloaded = _array(as_object(message.get("raw_data")).get("forwarded_bodies"))
    inline = {str(b.get("server_message_id")): b
              for b in [*downloaded, *_array(cj.get("inline_content"))]}
    previews = {str(b.get("msgid")): b for b in _array(cj.get("list_content"))}
    ids = [str(d.get("msg_id")) for d in descriptors] or list(inline) or list(previews)
    descriptor_map = {str(d.get("msg_id")): d for d in descriptors}
    # Preview nicknames identify senders, not just the first three rows.
    sender_names = {}
    for sid, preview in previews.items():
        uid = str(inline.get(sid, {}).get("sender") or descriptor_map.get(sid, {}).get("uid") or "")
        if uid and preview.get("nick_name"):
            sender_names[uid] = preview["nick_name"]
    result = {"title": cj.get("title") or "聊天记录", "items": [], "total": len(ids),
              "available": 0, "missing": len(ids), "complete": False}
    if len(ancestors) >= 5 or budget[0] <= 0:
        return result
    for sid in ids:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        preview = previews.get(sid, {})
        # Prefer locally archived originals (including downloaded media).
        local = conn.execute("SELECT * FROM messages WHERE msg_id = ?", (f"srv_{sid}",)).fetchone()
        local_row = dict(local) if local else None
        inline_row = None
        if sid in inline:
            try:
                inline_row = _inline_row(inline[sid], descriptor_map.get(sid, {}), preview, message["msg_id"])
            except (ValueError, TypeError, IndexError):
                inline_row = None
        # Prefer inline share/card payloads when the locally archived original
        # was stored as plain text; keep downloaded media from the local row.
        if inline_row is not None and (
            local_row is None
            or (local_row.get("msg_type") == 1 and inline_row.get("msg_type") not in (None, 1))
        ):
            row = _overlay_local_media(inline_row, local_row) if local_row else inline_row
        else:
            row = local_row
        if row is None:
            row = {"msg_id": f"{message['msg_id']}/missing_{sid}", "msg_type": 1,
                   "sender_name": preview.get("nick_name") or "", "sender_uid": str(descriptor_map.get(sid, {}).get("uid") or ""),
                   "content": preview.get("text") or "[消息详情未采集]", "timestamp": 0,
                   "detail_missing": True}
        else:
            result["available"] += 1
            ensure_row_media(row, conn, fetch=fetch_media, budget=media_budget)
            user = conn.execute("SELECT nickname FROM users WHERE uid = ?", (row.get("sender_uid", ""),)).fetchone()
            if user and user[0]:
                row["sender_name"] = user[0]
            if sid not in ancestors:
                nested = resolve_forward(
                    row, conn, ancestors=(*ancestors, sid), budget=budget,
                    fetch_media=fetch_media, media_budget=media_budget,
                )
            else:
                nested = {"title": "聊天记录", "items": [], "total": 0, "available": 0,
                          "missing": 0, "complete": False}
            if nested is not None:
                row["forward_detail"] = nested
        result["items"].append(row)
    for row in result["items"]:
        if not row.get("sender_name"):
            row["sender_name"] = sender_names.get(row.get("sender_uid"), "")
    result["missing"] = result["total"] - result["available"]
    result["complete"] = bool(ids) and result["missing"] == 0
    return result
