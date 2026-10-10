#!/usr/bin/env python3
"""Export chat data from SQLite to ChatLab v0.0.2 format (JSON/JSONL) or plain text (TXT)."""
import base64
import json
import mimetypes
import os
import re
import time
import urllib.parse

from common import paths
from common.message_kinds import is_view_once, locale_notice_text, share_kind, share_text
from common.owner import FALLBACK_NAME, detect_owner
from extractor.im_media import live_photo_cenc
from extractor.models import get_db
from backend.forwarded import as_object, resolve_forward

# DB msg_type → ChatLab message type
CHATLAB_TYPE_MAP = {
    1: 0,   # text → TEXT
    2: 5,   # emoji → EMOJI
    3: 1,   # image → IMAGE
    4: 24,  # share → SHARE
    5: 0,   # video → readable duration label (analysis-oriented export)
    0: 99,  # other → OTHER
}


_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_FILENAME_BYTES = 255


def _safe_filename_component(
    value: str | None,
    fallback: str = "conversation",
    max_bytes: int = _MAX_FILENAME_BYTES,
) -> str:
    """Convert a conversation nickname into a portable filename component."""
    component = str(value or "").strip()
    component = _INVALID_FILENAME_CHARS_RE.sub("_", component)
    component = re.sub(r"\s+", "_", component)
    component = component.strip(" ._") or fallback
    if component.upper() in _WINDOWS_RESERVED_NAMES:
        component = f"_{component}"
    encoded = component.encode("utf-8")
    if len(encoded) > max_bytes:
        component = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return component.rstrip(" ._") or fallback


def build_export_filename(
    username: str | None,
    output_format: str = "jsonl",
    timestamp: int | float | None = None,
    collision_index: int | None = None,
) -> str:
    """Build a recognizable, filesystem-safe ChatLab export filename.

    The timestamp is the export time (local time) and includes seconds so
    repeated exports made in different seconds do not all become browser
    ``(1)``/``(2)`` downloads.
    """
    export_time = time.localtime(time.time() if timestamp is None else timestamp)
    stamp = time.strftime("%Y%m%d%H%M%S", export_time)
    extension = {"json": ".json", "txt": ".txt"}.get(output_format, ".jsonl")
    collision_suffix = f"_{collision_index}" if collision_index else ""
    suffix = f"{collision_suffix}_{stamp}_export{extension}"
    component = _safe_filename_component(
        username,
        max_bytes=_MAX_FILENAME_BYTES - len(suffix.encode("utf-8")),
    )
    return f"{component}{suffix}"


_STICKER_HEX_RE = re.compile(r"-ts-([0-9a-fA-F]{4,})(?:\.[a-zA-Z0-9]{1,5})?$")


def _decode_sticker_name(url: str) -> str | None:
    """Recover a sticker's human-readable name from a Douyin IM CDN URL.

    URLs look like .../im-resource/<digits>-ts-<utf8-hex>?...
    where <utf8-hex> is the UTF-8 bytes of e.g. "续火花.png" as hex.
    Returns the decoded name without extension, or None on no match.
    """
    if not url:
        return None
    try:
        path = urllib.parse.urlparse(url).path
        last = path.rsplit("/", 1)[-1]
        m = _STICKER_HEX_RE.search(last)
        if not m:
            return None
        raw = bytes.fromhex(m.group(1))
        name = raw.decode("utf-8")
        # Strip a trailing extension like .png/.webp/.gif
        base, sep, ext = name.rpartition(".")
        if sep and base and len(ext) <= 4 and ext.isalnum():
            return base
        return name
    except (UnicodeDecodeError, ValueError):
        return None


def _emoji_text_label(content: str | None, media_url: str | None) -> str:
    """Pick a text label '[name]' for an emoji message.
    Prefers the URL-decoded name (most reliable), then the existing content,
    finally a generic placeholder.
    """
    name = _decode_sticker_name(media_url or "")
    if name:
        return f"[{name}]"
    c = (content or "").strip()
    if c and c != "[表情]":
        if c.startswith("[") and c.endswith("]"):
            return c
        return f"[{c}]"
    return "[表情]"


_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{\d+\}\}")


def _render_template_tips(obj: dict) -> str | None:
    """渲染抖音系统消息模板。
    例：{"tips":"{{1}}赞了你分享的 {{2}}","template":[{"key":1,"name":"对方"},{"key":2,"name":"视频X"}]}
    → "对方赞了你分享的 视频X"
    """
    tips = obj.get("tips") or obj.get("hint") or obj.get("title")
    if not tips:
        return None
    names = {}
    for it in obj.get("template") or []:
        if isinstance(it, dict) and it.get("key") is not None:
            names[it["key"]] = (it.get("name") or "").strip()
    out = tips
    for k, name in names.items():
        out = out.replace(f"{{{{{k}}}}}", name)
    out = _TEMPLATE_PLACEHOLDER_RE.sub("", out).strip()
    return out or None


def _system_message_text(content: str | None, cj: dict | None = None) -> str:
    """msg_type=0 系统消息的可读文本。
    - 文本（已是 [语音 X秒]）原样返回
    - 群通知（改群名/群头像、拉人进群、开播…）按 locale_resources 模板渲染
    - JSON 模板渲染成 [系统] 前缀的可读文字（优先用完整 cj，content 在 DB 里被截 200 字符）
    - 无法识别的兜底为 [系统消息]
    """
    c = (content or "").strip()
    obj = cj if isinstance(cj, dict) else None
    if not obj and c.startswith("{"):
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            obj = None
    # 群通知只在 cj 的多语言模板里，content 落库时是 "[系统消息]" 占位，
    # 所以先把模板渲染出来，否则导出文件里也只剩一句 "[系统消息]"。
    if obj and (not c or c == "[系统消息]"):
        locale_text = locale_notice_text(obj)
        if locale_text:
            return f"[系统] {locale_text}"
    if c and not c.startswith("{"):
        return c
    if not obj:
        return "[系统消息]"
    # 一起看视频邀请 (aweType=9000) 优先处理：它带 title="邀你一起看视频"，
    # 会被 _render_template_tips 的 title 兜底 + 下面的 "看视频" 启发式双重误判。
    if obj.get("aweType") == 9000:
        title = (obj.get("title") or "").strip()
        return f"[一起看视频] {title}".strip() if title else "[一起看视频]"
    rendered = _render_template_tips(obj)
    if rendered:
        return f"[系统] {rendered}"
    if obj.get("aweType") == 193 or obj.get("tips") == "通话成功":
        return "[通话成功]"
    title = obj.get("title") or ""
    hint = obj.get("hint") or ""
    if "看视频" in title or "通话邀请" in hint:
        return "[视频通话邀请]"
    return "[系统消息]"


def _file_to_data_url(filepath: str) -> str | None:
    """Read a local file and return a data URL (base64 encoded)."""
    if not filepath or not os.path.isfile(filepath):
        return None

    ext = os.path.splitext(filepath)[1].lower()
    # 优先使用自定义映射（mimetypes 会把 .mpeg 识别为 video/mpeg）
    mime = {
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".mpeg": "audio/mpeg",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
    }.get(ext)
    if not mime:
        mime, _ = mimetypes.guess_type(filepath)
    if not mime:
        mime = "application/octet-stream"

    try:
        with open(filepath, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _get_content_json(msg) -> dict | None:
    """从 raw_data 中提取完整的 content_json。"""
    raw = msg["raw_data"]
    if not raw:
        return None
    try:
        raw_obj = as_object(raw)
        cj_str = raw_obj.get("content_json", "")
        if cj_str:
            return as_object(cj_str) or None
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _message_field(msg, key: str, default=None):
    """Read a field from either sqlite3.Row or a plain test dictionary."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    try:
        return msg[key]
    except (KeyError, IndexError, TypeError):
        return default


#: 纯文本导出里系统消息的固定开头（见 plain_text_line / _plain_system_text）。
SYSTEM_TEXT_LABEL = "[系统消息]"

#: 系统消息原本的类型标签（"[系统] "、"[通话成功]"…），纯文本导出统一换掉。
_SYSTEM_LABEL_RE = re.compile(r"^\[[^\]]*\]\s*")


def plain_text_line(display_name: str, content: str) -> str:
    """txt 导出里的一行：``[昵称]正文``。

    系统消息没有发送者（``sender_uid`` 常为空，昵称会退化成会话名，写出来是
    「[会话名][系统消息]…」这种假昵称），所以统一写成 ``[系统消息] 正文`` ——
    正文的 ``[系统消息]`` 开头就是这里的标记。
    """
    text = str(content or "").strip()
    if text.startswith(SYSTEM_TEXT_LABEL):
        return text
    return f"[{display_name}]{text}"


def _plain_system_text(text: str) -> str:
    """系统消息在纯文本导出里的写法：统一 ``[系统消息] 正文``。

    系统消息本来带的 ``[系统]``、``[通话成功]``、``[一起看视频]`` 都是类型标签，
    这里统一换成 ``[系统消息]`` 一个前缀、剩下的当正文 —— 否则会写成
    「[系统消息][系统] 小明关注了你」这种两层括号。
    """
    body = _SYSTEM_LABEL_RE.sub("", str(text or "").strip()).strip()
    return f"{SYSTEM_TEXT_LABEL} {body}".strip()


def is_empty_system_line(line: str) -> bool:
    """这一行是不是「只有 ``[系统消息]`` 标签、没有正文」的空壳通知。

    抖音有时只发一个通知壳：库里 ``content`` 就是 ``"[系统消息]"`` 这个占位，
    模板也渲染不出东西。面板里本来就不显示这种消息（见 common/display_rules.py），
    纯文本导出同样不写这一行 —— 免得文件里混着一堆没有内容的行。
    """
    return str(line or "").strip() == SYSTEM_TEXT_LABEL


def _system_text(content, cj, plain: bool) -> str:
    """系统消息的正文；纯文本导出统一写成 ``[系统消息] 正文``。"""
    text = _system_message_text(content, cj)
    return _plain_system_text(text) if plain else text


def _resolve_message(
    msg, cj: dict | None, media_dir: str, embed_images: bool = True, plain: bool = False
) -> tuple:
    """Decide the ChatLab content + type for one DB message.

    Returns (content, chatlab_type, stats) where stats is a dict of counter
    increments ({'voice':1}, {'image':1,'image_embedded':1}, ...). The ordering
    of the voice/video/emoji/image and share/system branches is load-bearing and
    matches the original inline loop exactly (see test_exporter).

    embed_images=False keeps images as a plain ``[图片]`` label (type IMAGE)
    instead of inlining base64 data URLs — used by the ChatLab pull API where
    payload size matters and the picture itself adds nothing to analysis.

    plain=True 是「纯文本导出」（txt）用的形态：不内嵌图片、不写链接与作者，
    分享消息写成 ``[分享X] 标题``、系统消息写成 ``[系统消息] 正文``；语音时长与转写、
    视频时长、表情文字标签、图片/实况图标签这些和非纯文本一致。

    实况图（aweType=2704，一张静态封面 + 一段小视频）的图片本身照旧导出，只是标签
    写成 ``[实况图]``：导出格式里没有"角标"这回事，标签是唯一能表达它的地方。
    """
    cj = cj if isinstance(cj, dict) else {}
    embed_images = embed_images and not plain   # 纯文本导出不内嵌图片（base64 会把文件撑大）
    msg_type = msg["msg_type"]
    content = msg["content"] or ""
    awe = str(cj.get("aweType", ""))
    if cj.get("name") and (cj.get("secUID") or cj.get("sec_uid") or cj.get("uid")):
        if plain:
            # 纯文本只留名片的名字：简介和主页链接对分析没有用。
            return "[用户名片] " + str(cj["name"]), 27, {}
        uid = cj.get("secUID") or cj.get("sec_uid") or cj.get("uid")
        link = "https://www.douyin.com/user/" + urllib.parse.quote(str(uid), safe="")
        return " | ".join(str(v) for v in ["[用户名片] " + str(cj["name"]), cj.get("desc"), link] if v), 27, {}
    patch = as_object(as_object(cj.get("im_dynamic_patch")).get("raw_data"))
    if awe == "11029" and patch:
        title = as_object(patch.get("content_top")).get("content") or ""
        if plain:
            # 商品名的正主在动态卡片的布局里，正文里可能只剩一个 "[分享商品]" 标签。
            return (f"[分享商品] {title}" if title else share_text(cj, content)), 24, {"share": 1}
        title = title or "商品"
        actions = as_object(patch.get("whole_card")).get("action_info") or []
        schema = as_object(as_object(actions[0]).get("params")).get("schema", "") if isinstance(actions, list) and actions else ""
        match = re.search(r"commodity_id=(\d+)", str(schema))
        link = " | https://www.douyin.com/product/" + match[1] if match else ""
        return "[分享商品] " + str(title) + link, 24, {"share": 1}
    if awe == "9000":
        return _system_text(None, cj, plain), 0, {"system": 1}
    if cj.get("tips") and not cj.get("resource_url"):
        return _system_text(None, cj, plain), 0, {"system": 1}
    if awe in {"500", "501", "507", "508", "510", "514", "516"}:
        msg_type = 2
        content = cj.get("display_name") or "[表情]"
    elif awe in {"2702", "2703", "2704"}:
        msg_type = 3
    if cj.get("text") and (not content or content.startswith("{")):
        content = str(cj["text"])

    chatlab_type = CHATLAB_TYPE_MAP.get(msg_type, 99)
    stats: dict = {}

    # 语音消息：msg_type=0 但有 resource_url + duration
    # 不再嵌 base64 / CDN URL —— 对 LLM 无意义且每条几百 KB；用纯文字标签即可。
    is_voice = False
    voice_resource = cj.get("resource_url") if cj else None
    voice_duration = cj.get("duration") if cj else None
    if voice_duration in (None, "") and isinstance(voice_resource, dict):
        voice_duration = voice_resource.get("duration")
    video_payload = cj.get("video") if isinstance(cj, dict) else None
    has_video_payload = (
        isinstance(video_payload, dict) and bool(video_payload.get("vid"))
    )
    is_image_payload = bool(
        cj and str(cj.get("aweType")) in {"2702", "2703", "2704"}
    )
    if (
        voice_resource is not None
        and voice_duration not in (None, "")
        and not has_video_payload
        and not is_image_payload
    ):
        is_voice = True
        chatlab_type = 0  # TEXT
        try:
            dur_sec = round(float(voice_duration) / 1000)
        except (TypeError, ValueError):
            dur_sec = 0
        voice_label = f"[语音 {dur_sec}秒]" if dur_sec else "[语音]"
        transcription = _message_field(msg, "voice_transcription", "")
        transcription_status = _message_field(
            msg, "voice_transcription_status", None
        )
        if transcription_status not in (None, "", "success"):
            transcription = ""
        transcription = transcription.strip() if isinstance(transcription, str) else ""
        content = f"{voice_label} {transcription}" if transcription else voice_label
        stats["voice"] = 1

    # 视频消息：msg_type=5 (新分类) 或 cj.video.vid 兜底（老数据）
    is_video = False
    if not is_voice and ((msg_type == 5) or has_video_payload or str(_message_field(msg, "media_local_path") or "").lower().endswith(".mp4")):
        is_video = True
        try:
            dur_sec = round(float((cj or {}).get("duration") or 0))
        except (TypeError, ValueError):
            dur_sec = 0
        content = f"[视频 {dur_sec}秒]" if dur_sec else "[视频]"
        chatlab_type = 0  # Keep the existing analysis-oriented duration label policy.
        stats["video"] = 1

    # 表情：用文字标签代替 URL — CDN 早晚过期，URL 对 LLM 也没意义。
    if not is_voice and not is_video and chatlab_type == 5:
        content = _emoji_text_label(content, msg["media_url"])
        stats["emoji"] = 1
    # Prefer the archived plaintext image; signed origin URLs may be encrypted/expired.
    elif not is_voice and not is_video and chatlab_type == 1 and not embed_images:
        # 实况图（会动的图）：标签写清它不只是张静态图（图片本身照旧导出）
        is_live = live_photo_cenc(cj) is not None
        content = "[实况图]" if is_live else "[图片]"
        stats["image"] = 1
    elif not is_voice and not is_video and chatlab_type == 1:
        local = _message_field(msg, "media_local_path")
        full = os.path.realpath(os.path.join(media_dir, local)) if local else None
        root = os.path.realpath(media_dir)
        data_url = None
        if full and os.path.commonpath([root, full]) == root:
            data_url = _file_to_data_url(full)
        if data_url:
            content = data_url
            stats["image_embedded"] = 1
        elif cj.get("inline_pic"):
            content = "data:image/webp;base64," + re.sub(r"\s+", "", str(cj["inline_pic"]))
            stats["image_embedded"] = 1
        elif msg["media_url"] and not as_object(cj.get("resource_url")).get("skey"):
            content = msg["media_url"]
        else:
            live = live_photo_cenc(cj) is not None
            content, chatlab_type = ("[实况图未下载]" if live else "[图片未下载]"), 0
        stats["image"] = 1

    # 仅看一次文本消息：aweType=10401 + 正文、无卡片字段。旧数据可能把它存成
    # msg_type=4，这里统一按纯文本（TEXT）导出，正文不会被分享/系统分支改写。
    view_once = not is_voice and not is_video and is_view_once(cj)

    # 分享消息：以 cj 的形态判断（含 itemId），不依赖 msg_type。
    # 不要放宽到 aweType / content_title 等字段 —— 表情消息的 cj 也带这些。
    if plain:
        # 纯文本导出多认一批卡片：它们只有下划线的 item_id（11054/11063/11070…）
        # 或只有 push_detail 的类型标注，没有 itemId，走不进下面那条判断，
        # 但正文本身就是 "[分享图文]标题"，正好能写成 [分享图文] 标题。
        # 已经在上面认出是表情（5）或图片（1）的，不因为顺带带着卡片字段就被改写。
        extra_card = bool(cj.get("item_id") or cj.get("im_dynamic_patch")
                          or share_kind(cj, content))
        share_like = bool(cj and cj.get("itemId")) or (extra_card and chatlab_type not in (1, 5))
    else:
        share_like = bool(cj and cj.get("itemId"))
    if view_once:
        content = str(cj.get("text") or content)
        chatlab_type = 0  # TEXT
    elif not is_voice and not is_video and share_like:
        if plain:
            # 分享只留「[分享视频] 标题」这种一句话，不带作者和跳转链接。
            content = share_text(cj, content)
        else:
            item_id = cj.get("itemId", "")
            title = (cj.get("content_title") or "").strip()
            author = (cj.get("content_name") or "").strip()
            parts = []
            if title:
                parts.append(title)
            if author:
                parts.append(f"@{author}")
            if item_id:
                parts.append(f"https://www.douyin.com/video/{item_id}")
            # aweType=805 是「限时日常」作品分享（卡片没有标题），单独标注。
            kind = "限时日常" if str(cj.get("aweType")) == "805" else "视频"
            content = (f"[分享{kind}] " + " | ".join(parts)) if parts else f"[分享{kind}]"
            if cj.get("comment"):
                content += "\n" + str(cj.get("comment_user_name") or "") + ": " + str(cj["comment"])
        chatlab_type = 24  # SHARE，统一类型
        stats["share"] = 1
    # 系统消息（msg_type=0 但不是语音 / 不是 share / 不是 video）
    elif not is_voice and not is_video and msg_type == 0:
        content = _system_text(content, cj, plain)
        if chatlab_type == 99:
            chatlab_type = 0  # TEXT
        stats["system"] = 1

    # 最终兜底：还是 JSON 的内容统一收敛
    if isinstance(content, str) and content.startswith("{"):
        fallback = str(cj.get("text") or cj.get("description") or cj.get("content_title") or "")
        content = fallback if plain else (fallback or "[分享内容]")

    return content, chatlab_type, stats


def _build_reply_to(ref_msg_raw) -> dict | None:
    """Build the ChatLab replyTo block from a message's stored ref_msg JSON."""
    if not ref_msg_raw:
        return None
    try:
        ref = as_object(ref_msg_raw)
        ref_info = {}
        if ref.get("server_id"):
            ref_info["replyTo"] = f"srv_{ref['server_id']}"
        if ref.get("nickname"):
            ref_info["replyToAuthor"] = ref["nickname"]
        if ref.get("content"):
            ref_info["replyToContent"] = ref["content"]
        return ref_info or None
    except (json.JSONDecodeError, TypeError):
        return None


def _forward_text(detail, media_dir, plain=False):
    if not detail:
        return "[合并转发] 正文未取得"
    lines = [f"[合并转发] {detail['title']}（已取得 {detail['available']}/{detail['total']} 条）"]
    for row in detail['items']:
        if row.get('forward_detail') is not None:
            text = _forward_text(row['forward_detail'], media_dir, plain)
        elif row.get('detail_missing'):
            text = '[正文未取得，仅摘要] ' + (row.get('content') or '')
        else:
            text, _, _ = _resolve_message(row, _get_content_json(row), media_dir, plain=plain)
        who = row.get('sender_name') or row.get('sender_uid') or '未知用户'
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


CHATLAB_FORMAT_VERSION = "0.0.2"
CHATLAB_GENERATOR = "douyin-chat-export"


def conv_display_name(conv_name: str | None, conv_type: int) -> str:
    """ChatLab meta.name: group keeps its name, private chats become 与X的对话."""
    return conv_name if conv_type == 2 else f"与{conv_name}的对话"


def build_chatlab_header(conv_id: str, conv_name: str | None, conv_type: int,
                         owner_uid: str, exported_at: int | None = None) -> dict:
    """The ``chatlab`` + ``meta`` blocks shared by file export and the pull API."""
    header = {
        "chatlab": {
            "version": CHATLAB_FORMAT_VERSION,
            "exportedAt": int(time.time()) if exported_at is None else exported_at,
            "generator": CHATLAB_GENERATOR,
        },
        "meta": {
            "name": conv_display_name(conv_name, conv_type),
            "platform": "douyin",
            "type": "group" if conv_type == 2 else "private",
            "ownerId": owner_uid,
        },
    }
    if conv_type == 2:
        header["meta"]["groupId"] = conv_id
    return header


def sender_display_name(uid: str, sender_name: str | None, *, users_map: dict,
                        owner_uid: str, owner_name: str, conv_type: int,
                        conv_name: str | None) -> str:
    """Nickname rule: users table → owner name → message sender_name → fallback."""
    name = users_map.get(uid, "")
    if name:
        return name
    if uid == owner_uid:
        return owner_name
    return sender_name or (f"用户{uid}" if conv_type == 2 else conv_name)


def build_chatlab_message(msg, conn, media_dir: str, *, users_map: dict,
                          owner_uid: str, owner_name: str, conv_type: int,
                          conv_name: str | None, previous_shares: dict,
                          embed_images: bool = True, plain: bool = False) -> tuple[dict, dict]:
    """Convert one DB message row into a ChatLab message dict.

    ``previous_shares`` (itemId → msg_id) is threaded through consecutive calls
    so a "引用视频" reply can point back at the share it refers to; callers that
    page through messages keep it per page. Returns (message, stats) where stats
    is the counter dict from ``_resolve_message``.

    ``plain=True`` 供纯文本导出使用：``content`` 变成贴一行就能读的纯文字
    （见 ``_resolve_message``），引用视频不再追写跳转链接。
    """
    cj = _get_content_json(msg)
    uid = msg["sender_uid"] or ""
    display_name = sender_display_name(
        uid, msg["sender_name"], users_map=users_map, owner_uid=owner_uid,
        owner_name=owner_name, conv_type=conv_type, conv_name=conv_name,
    )

    content, chatlab_type, stats = _resolve_message(msg, cj, media_dir, embed_images, plain)
    if str((cj or {}).get("aweType")) == "13600":
        detail = resolve_forward(dict(msg), conn)
        content = _forward_text(detail, media_dir, plain)
        chatlab_type = 26

    chatlab_msg = {
        "sender": uid,
        "accountName": display_name,
        "timestamp": msg["timestamp"] or 0,
        "type": chatlab_type,
        "content": content,
        "platformMessageId": msg["msg_id"],
    }

    # 引用/回复消息
    reply_to = _build_reply_to(msg["ref_msg"])
    if reply_to:
        chatlab_msg["replyTo"] = reply_to
        if reply_to.get("replyTo"):
            chatlab_msg["replyToMessageId"] = reply_to["replyTo"]
    elif as_object((cj or {}).get("related_share_video")).get("itemId"):
        item_id = str(cj["related_share_video"]["itemId"])
        target = previous_shares.get(item_id)
        if target and not plain:
            chatlab_msg["replyToMessageId"] = target
        content = str((cj or {}).get("text") or content)
        chatlab_msg["content"] = content if plain else content + "\n[引用视频] https://www.douyin.com/video/" + item_id
    if (cj or {}).get("itemId") and not (cj or {}).get("related_share_video"):
        previous_shares[str(cj["itemId"])] = msg["msg_id"]

    return chatlab_msg, stats


class ChatLabExporter:
    def __init__(
        self,
        conv_name: str = None,
        output_format: str = "jsonl",
        output_dir: str | os.PathLike[str] | None = None,
    ):
        self.conv_name = conv_name
        self.output_format = output_format  # "json" / "jsonl" / "txt"（纯文字，见 _resolve_message）
        self.output_dir = (
            paths.DATA_DIR if output_dir is None else os.fspath(output_dir)
        )

    def export(self, output_path: str | None = None) -> str | None:
        from common.db import init_db
        init_db()
        conn = get_db()

        # Detect owner
        owner_uid, owner_name = detect_owner(conn)
        # 认不出来时沿用旧行为：名字留「我」，uid 留空（sender_uid 也为空的系统行就显示「我」）
        owner_name = owner_name or FALLBACK_NAME
        print(f"[*] 检测到 owner: {owner_name} ({owner_uid})")

        # Find conversation
        if self.conv_name:
            row = conn.execute(
                "SELECT conv_id, name, conv_type FROM conversations WHERE name = ?",
                (self.conv_name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT conv_id, name, conv_type FROM conversations ORDER BY last_message_time DESC LIMIT 1"
            ).fetchone()

        if not row:
            print(f"[-] 未找到会话: {self.conv_name or '(any)'}")
            conn.close()
            return

        conv_id = row["conv_id"]
        conv_name = row["name"]
        print(f"[*] 导出会话: {conv_name} (ID: {conv_id})")

        exported_at = int(time.time())
        if output_path is None:
            output_path = os.path.join(
                self.output_dir,
                build_export_filename(conv_name or conv_id, self.output_format, exported_at),
            )
        else:
            output_path = os.fspath(output_path)

        # Load messages ordered by seq
        messages = conn.execute(
            """SELECT m.*,
                      vt.text_result AS voice_transcription,
                      vt.status AS voice_transcription_status,
                      vt.error AS voice_transcription_error
               FROM messages m
               LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
               WHERE m.conv_id = ? ORDER BY m.seq ASC""",
            (conv_id,),
        ).fetchall()

        print(f"[*] 共 {len(messages)} 条消息")

        # Build users map from DB
        users_map = {}
        users_rows = conn.execute("SELECT uid, nickname FROM users").fetchall()
        for u in users_rows:
            if u["uid"] and u["nickname"]:
                users_map[u["uid"]] = u["nickname"]

        # Collect members from messages
        members_map = {}
        for msg in messages:
            uid = msg["sender_uid"] or ""
            if uid and uid not in members_map:
                members_map[uid] = sender_display_name(
                    uid, msg["sender_name"], users_map=users_map,
                    owner_uid=owner_uid, owner_name=owner_name,
                    conv_type=row["conv_type"], conv_name=conv_name,
                )

        # Media base dir
        media_dir = paths.MEDIA_DIR

        # Build ChatLab structure
        header = build_chatlab_header(conv_id, conv_name, row["conv_type"], owner_uid, exported_at)

        members = []
        for uid, name in members_map.items():
            member = {"platformId": uid, "accountName": name}
            members.append(member)

        chatlab_messages = []
        image_count = 0
        image_embedded = 0
        emoji_count = 0
        voice_count = 0
        video_count = 0
        system_count = 0
        share_normalized = 0
        ref_count = 0
        skipped_empty = 0        # txt 里没写的空壳系统通知（见 is_empty_system_line）

        previous_shares = {}
        plain = self.output_format == "txt"
        for msg in messages:
            chatlab_msg, stats = build_chatlab_message(
                msg, conn, media_dir, users_map=users_map,
                owner_uid=owner_uid, owner_name=owner_name,
                conv_type=row["conv_type"], conv_name=conv_name,
                previous_shares=previous_shares, plain=plain,
            )
            voice_count += stats.get("voice", 0)
            video_count += stats.get("video", 0)
            emoji_count += stats.get("emoji", 0)
            image_count += stats.get("image", 0)
            image_embedded += stats.get("image_embedded", 0)
            share_normalized += stats.get("share", 0)
            system_count += stats.get("system", 0)
            if "replyTo" in chatlab_msg:
                ref_count += 1
            chatlab_messages.append(chatlab_msg)

        conn.close()

        # Write output
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        if self.output_format == "json":
            output = {**header, "members": members, "messages": chatlab_messages}
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False)
            print(f"[+] JSON 导出完成: {output_path}")
        elif self.output_format == "txt":
            # 一条消息一行「[昵称]正文」；合并转发的正文本身是几行，照原样写。
            # 只有 "[系统消息]" 标签、没有正文的空壳通知不写这一行（见 is_empty_system_line）。
            with open(output_path, "w", encoding="utf-8") as f:
                for msg in chatlab_messages:
                    line = plain_text_line(msg["accountName"], msg["content"])
                    if is_empty_system_line(line):
                        skipped_empty += 1
                        continue
                    f.write(line + "\n")
            print(f"[+] 纯文本导出完成: {output_path}")
        else:
            # JSONL format
            with open(output_path, "w", encoding="utf-8") as f:
                # Header line
                header_line = {"_type": "header", **header}
                f.write(json.dumps(header_line, ensure_ascii=False) + "\n")
                # Member lines
                for member in members:
                    member_line = {"_type": "member", **member}
                    f.write(json.dumps(member_line, ensure_ascii=False) + "\n")
                # Message lines
                for msg in chatlab_messages:
                    msg_line = {"_type": "message", **msg}
                    f.write(json.dumps(msg_line, ensure_ascii=False) + "\n")
            print(f"[+] JSONL 导出完成: {output_path}")

        print(f"  消息: {len(chatlab_messages)}")
        print(f"  成员: {len(members)}")
        if image_count:
            if plain:
                print(f"  图片: {image_count} (只写 [图片] 标签)")
            else:
                print(f"  图片: {image_count} (嵌入 data URL: {image_embedded})")
        if emoji_count:
            print(f"  表情: {emoji_count} (转为文字标签)")
        if voice_count:
            print(f"  语音: {voice_count} (转为文字标签)")
        if video_count:
            tail = "" if plain else " + 封面图"
            print(f"  视频: {video_count} (转为文字标签{tail})")
        if system_count:
            tail = f"，另有 {skipped_empty} 条空通知未写入" if skipped_empty else " (模板渲染为文字)"
            print(f"  系统消息: {system_count - skipped_empty}{tail}")
        if share_normalized:
            label = "分享" if plain else "分享视频"
            print(f"  {label}: {share_normalized} (含 type=1 错分类的)")
        if ref_count:
            print(f"  引用/回复: {ref_count}")
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  文件大小: {size_mb:.1f} MB")
        return output_path
