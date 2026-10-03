"""「这条消息在阅读端会不会真的显示出来」的 Python 判定。

前端（``frontend/src/lib/douyinMessage.js``）在画聊天记录时，有三件事会让一条
数据库里的记录**不显示**：

1. 抖音对同一次事件会向双方各下发一份，两份内容一样（互相关注、成为朋友、
   点赞、火花等）。``duplicate_system_message_ids`` 会把多余的那份藏掉。
2. 载荷里没有任何可展示内容的空系统消息（库里很常见的 ``{}``）。
3. 阅读端还不认识的 JSON 载荷（例如通话记录、音乐分享、群系统提示）。

会话列表和聊天窗口左上角的「N 条消息」以前用的是数据库里的**原始行数**，
于是会出现"显示 6 条、实际只画得出 2 条"的矛盾。这里把上面三条规则原样搬成
Python，供后端算出「真正显示出来的条数」（``conversations.display_count``）。

⚠️ 这里是前端规则的镜像。改前端显示规则时，必须同步改本文件并跑
``tests/test_display_rules.py``（它和前端 ``displayCountCases.json`` 用同一份
样例，两边不一致就会失败）。
"""
import json
import math
import re

from common.message_kinds import locale_notice_text

RELATION_NOTICE_RE = re.compile(
    r"^(?:我们已互相关注[，,]可以开始聊天了|你们已互相关注对方|我们已成为朋友|你们已成为朋友)$"
)

VIEW_ONCE_AWE_TYPE = 10401

SHARE_CARD_FIELDS = (
    "content_title", "itemId", "item_id", "im_dynamic_patch", "aweme_info",
    "awemeType", "cover_url", "comment",
)

SHARE_AWE_TYPES = frozenset([
    800, 801, 803, 805, 10500, 11029,
    11054, 11055, 11063, 11066, 11067, 11069, 11070,
])

LOOSE_EMOJI_AWES = frozenset([515, 517, 520])

LOOSE_SHARE_AWES = frozenset([805, 2104])

_SERVER_MSG_ID_RE = re.compile(r'server_message_id\\?"?\s*:\s*\\?"?(\d{15,})')

_SHARE_PREFIX_RE = re.compile(r"^\[分享([^\]]+)\]")

_SHARE_TITLE_RE = re.compile(r"^(?:分享\[.+?\][:：]\s*|\[分享.+?\])(.+)", re.S)


def _truthy(value):
    """JavaScript 的真假判断（和 Python 的 bool 在空字符串/0 之外一致）。"""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return bool(value)


def _num(value):
    """JavaScript 的 Number()：转不出来就是 NaN。"""
    if value is None or isinstance(value, bool):
        return float("nan") if value is None else float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0.0
        try:
            return float(text)
        except ValueError:
            return float("nan")
    return float("nan")


def _num_eq(value, expected):
    got = _num(value)
    return not math.isnan(got) and got == expected


def _jstr(value):
    """JavaScript 的 String()：数字不带多余的小数点。"""
    if value is None:
        return "undefined"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _loads(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


class _Row:
    """把一行数据包起来，缓存 JSON 解析结果。

    一条消息在判定过程中会被反复问"你的 content_json 是什么"（十几次），
    而 raw_data 平均 1.8KB，重复解析会让整库统计慢十倍，所以这里缓存一次。
    """

    __slots__ = ("data", "_raw", "_raw_done", "_cj", "_cj_done",
                 "_parsed", "_parsed_done", "_payload", "_payload_done",
                 "_notice", "_notice_done")

    def __init__(self, data):
        self.data = data
        self._raw = None
        self._raw_done = False
        self._cj = None
        self._cj_done = False
        self._parsed = None
        self._parsed_done = False
        self._payload = None
        self._payload_done = False
        self._notice = None
        self._notice_done = False

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def raw_json(self):
        """raw_data 解析后的对象（对应 JS 端 JSON.parse(msg.raw_data)）。"""
        if not self._raw_done:
            self._raw_done = True
            raw = self.data.get("raw_data")
            if isinstance(raw, str):
                raw = _loads(raw)
            self._raw = raw if isinstance(raw, dict) else None
        return self._raw

    def content_json(self):
        """对应前端 ``getContentJson``：raw_data 里的 content_json（可能双重编码）。"""
        if not self._cj_done:
            self._cj_done = True
            cj = None
            raw = self.raw_json()
            if raw is not None:
                cj = raw.get("content_json")
                if isinstance(cj, str):
                    cj = _loads(cj)
            self._cj = cj if isinstance(cj, (dict, list)) else None
        return self._cj

    def parsed_content(self):
        """对应前端 ``tryParseJson(msg.content)``。"""
        if not self._parsed_done:
            self._parsed_done = True
            self._parsed = try_parse_json(self.data.get("content"))
        return self._parsed

    def payload(self):
        """对应前端 ``payloadJson``。"""
        if not self._payload_done:
            self._payload_done = True
            self._payload = self.content_json() or self.parsed_content()
        return self._payload

    def notice(self):
        if not self._notice_done:
            self._notice_done = True
            self._notice = _relation_notice_text(self)
        return self._notice


def _as_row(row):
    return row if isinstance(row, _Row) else _Row(row)


def content_json(row):
    return _as_row(row).content_json()


def try_parse_json(text):
    if not isinstance(text, str) or not text.startswith("{"):
        return None
    parsed = _loads(text)
    return parsed if isinstance(parsed, dict) else None


def payload_json(row):
    return _as_row(row).payload()


def _relation_notice_text(row):
    cj = row.content_json() or row.parsed_content()
    candidates = []
    if isinstance(cj, dict):
        candidates += [cj.get("tips"), cj.get("hint_text"), cj.get("text")]
    content = row.get("content")
    if isinstance(content, str) and not content.startswith("{"):
        candidates.append(content)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        text = candidate.strip()
        if RELATION_NOTICE_RE.match(text):
            return text
    return ""


def relation_notice_text(row):
    return _as_row(row).notice()


def render_system_msg_text(row, self_uid=""):
    """对应前端 ``renderSystemMsg``（这里只关心"渲染出来是不是空的"）。"""
    row = _as_row(row)
    cj = row.content_json()
    source = cj or row.parsed_content()
    if not isinstance(source, dict) or not source:
        content = row.get("content")
        return content if (_truthy(content) and content != "{}") else ""
    if _truthy(source.get("tips")):
        return str(source.get("tips"))
    if _truthy(source.get("hint_text")):
        return str(source.get("hint_text"))
    notice = row.notice()
    if notice:
        return notice
    # 群通知（改群名/群头像、拉人进群、开播…）只在多语言模板里，content 是占位。
    content = row.get("content")
    if not content or content == "[系统消息]" or str(content).startswith("{"):
        locale_text = locale_notice_text(source, row.get("sender_uid"), self_uid)
        if locale_text:
            return locale_text
    if len(source) <= 1:
        return ""
    if isinstance(content, str) and content and content != "{}" and not content.startswith("{"):
        return content
    return ""


def is_json_sticker(row):
    row = _as_row(row)
    if row.get("msg_type") != 1:
        return False
    content = row.get("content")
    if not isinstance(content, str) or not content.startswith("{"):
        return False
    if '"stickers"' in content or '"joker_stickers"' in content:
        return True
    cj = row.content_json()
    return bool(isinstance(cj, dict) and (cj.get("stickers") or cj.get("joker_stickers")))


def is_json_system_msg(row):
    row = _as_row(row)
    if row.get("msg_type") != 1:
        return False
    content = row.get("content")
    if not isinstance(content, str) or not content.startswith("{"):
        return False
    if is_json_sticker(row):
        return False
    return '"tips"' in content and '"aweType"' in content


def is_view_once_content_json(cj):
    if not isinstance(cj, dict):
        return False
    if not _num_eq(cj.get("aweType"), VIEW_ONCE_AWE_TYPE):
        return False
    if not str(cj.get("text") or "").strip():
        return False
    return not any(_truthy(cj.get(field)) for field in SHARE_CARD_FIELDS)


def _view_once_source(row):
    cj = row.content_json()
    if cj:
        return cj
    return row.parsed_content()


def is_view_once(row):
    row = _as_row(row)
    if row.get("content") == "Recall Content Hided":
        return False
    if not is_view_once_content_json(_view_once_source(row)):
        return False
    raw = row.raw_json()
    if raw is None:
        return True
    return _truthy(raw.get("is_recalled"))


def is_share_content_json(cj):
    if not isinstance(cj, dict):
        return False
    if is_view_once_content_json(cj):
        return False
    awe = _num(cj.get("aweType"))
    if not math.isnan(awe) and int(awe) in SHARE_AWE_TYPES:
        return True
    if not math.isnan(awe) and int(awe) in (9000, 13600):
        return False
    text = _jstr(cj.get("text") or cj.get("push_detail") or "")
    if text.startswith("[分享") or text.startswith("分享["):
        return True
    return any(_truthy(cj.get(field)) for field in
               ("im_dynamic_patch", "awemeType", "itemId", "item_id", "comment", "content_title"))


def is_json_share(row):
    row = _as_row(row)
    if _num(row.get("msg_type")) == 4:
        return False
    if is_view_once(row):
        return False
    content = row.get("content")
    if isinstance(content, str) and content.startswith("{") and (
            "content_title" in content or "cover_url" in content):
        return True
    if is_share_content_json(row.content_json()):
        return True
    if not isinstance(content, str):
        return False
    if _SHARE_PREFIX_RE.match(content):
        return True
    return bool(re.match(r"^(?:分享\[.+?\][:：])", content))


def is_loose_emoji(row):
    row = _as_row(row)
    if is_json_sticker(row):
        return True
    cj = row.payload()
    if not isinstance(cj, dict):
        return False
    awe = _num(cj.get("aweType"))
    return not math.isnan(awe) and int(awe) in LOOSE_EMOJI_AWES


def is_json_video(row):
    row = _as_row(row)
    msg_type = _num(row.get("msg_type"))
    if msg_type == 5:
        return True
    if msg_type != 1:
        return False
    cj = row.content_json()
    if not isinstance(cj, dict):
        return False
    video = cj.get("video")
    return bool(isinstance(video, dict) and _truthy(video.get("vid")))


def is_loose_share(row):
    row = _as_row(row)
    if row.get("msg_type") == 4:
        return False
    if is_json_share(row):
        return True
    cj = row.payload()
    if not isinstance(cj, dict):
        return False
    awe = _num(cj.get("aweType"))
    if not math.isnan(awe) and int(awe) in LOOSE_SHARE_AWES:
        return True
    if _truthy(cj.get("poi_name")) or _truthy(cj.get("cover_info")):
        return True
    poi = cj.get("aweme_poi_id")
    return bool(_truthy(poi) and _jstr(poi))


def is_loose_image(row):
    row = _as_row(row)
    if row.get("msg_type") == 3:
        return False
    if is_loose_share(row) or is_loose_emoji(row) or is_json_video(row):
        return False
    cj = row.payload()
    if not isinstance(cj, dict) or not _truthy(cj.get("inline_pic")):
        return False
    return bool(_truthy(cj.get("check_pics"))
                or cj.get("is_long_pic") is not None
                or cj.get("create_type") is not None)


def get_voice_content(row):
    row = _as_row(row)
    cj = row.content_json()
    if cj:
        return cj
    return row.parsed_content()


def is_voice_msg(row):
    row = _as_row(row)
    cj = get_voice_content(row)
    if not isinstance(cj, dict):
        return False
    resource = cj.get("resource_url")
    if not resource or not isinstance(resource, (dict, str)):
        return False
    if _jstr(cj.get("aweType")) in ("2702", "2703", "2704") and not (
            _truthy(cj.get("voice_wave")) or _truthy(cj.get("tkey"))
            or (isinstance(resource, dict) and resource.get("is_voice"))):
        return False
    video = cj.get("video")
    if isinstance(video, dict) and _truthy(video.get("vid")) and not (
            _truthy(cj.get("voice_wave")) or _truthy(cj.get("tkey"))
            or (isinstance(resource, dict) and resource.get("is_voice"))):
        return False
    url_list = resource.get("url_list") if isinstance(resource, dict) else None
    has_url = isinstance(url_list, list) and len(url_list) > 0
    has_duration = _present(cj.get("duration")) or (
        isinstance(resource, dict) and _present(resource.get("duration")))
    has_marker = bool(_truthy(cj.get("tkey")) or _truthy(cj.get("voice_wave"))
                      or (isinstance(resource, dict) and resource.get("is_voice")))
    msg_type = row.get("msg_type")
    stored_voice_type = msg_type in (0, "other", None)
    return (has_url or has_duration or has_marker) if stored_voice_type else (has_duration or has_marker)


def _present(value):
    """对应 JS 的 `value !== undefined && value !== null && value !== ''`。"""
    return value is not None and value != ""


def get_profile_card(row):
    row = _as_row(row)
    cj = row.content_json() or row.parsed_content()
    if not isinstance(cj, dict) or not _truthy(cj.get("name")):
        return None
    identifier = (cj.get("secUID") or cj.get("sec_uid")
                  or (cj.get("uid") if cj.get("source") == "others_homepage" else None))
    if not _truthy(identifier):
        return None
    return {"name": _jstr(cj.get("name")), "id": _jstr(identifier)}


def get_forward_info(row):
    row = _as_row(row)
    cj = row.content_json() or row.parsed_content()
    if not isinstance(cj, dict) or _jstr(cj.get("aweType")) != "13600":
        return None
    return {"title": cj.get("title") or "聊天记录"}


def get_watch_together(row):
    row = _as_row(row)
    cj = row.content_json() or row.parsed_content()
    if not isinstance(cj, dict) or cj.get("aweType") != 9000:
        return None
    return {"title": cj.get("title") or "一起看视频"}


def should_show(row):
    """对应前端 ``shouldShow``：这条记录在聊天窗口里会不会被画出来。"""
    row = _as_row(row)
    if get_profile_card(row) or get_forward_info(row) or is_voice_msg(row):
        return True
    if get_watch_together(row):
        return True
    if is_loose_emoji(row) or is_loose_share(row) or is_loose_image(row):
        return True
    if row.get("msg_type") == 0:
        return bool(render_system_msg_text(row))
    if is_json_system_msg(row):
        return bool(render_system_msg_text(row))
    return True


def extract_server_msg_ids(row):
    row = _as_row(row)
    raw = row.get("raw_data")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)
    return _SERVER_MSG_ID_RE.findall(raw)


def duplicate_system_message_ids(messages):
    """对应前端 ``duplicateSystemMessageIds``：返回该被藏起来的 msg_id 集合。"""
    messages = [_as_row(row) for row in messages]
    pending = {}
    hidden = set()
    relation_seen = {}
    for row in messages:
        msg_id = row.get("msg_id")
        notice = row.notice()
        if notice:
            timestamp = row.get("timestamp")
            if not timestamp:
                continue
            key = (row.get("conv_id"), notice)
            first = relation_seen.get(key)
            if first is None or timestamp - first > 30:
                relation_seen[key] = timestamp
            else:
                hidden.add(msg_id)
            continue
        cj = row.content_json() or row.parsed_content()
        tips = cj.get("tips") if isinstance(cj, dict) else None
        if not _truthy(tips) or not row.get("sender_uid") or not row.get("timestamp"):
            continue
        tips = str(tips)
        like = _jstr(cj.get("aweType")) == "126" and "赞了" in tips
        spark = bool(re.match(r"^(你|对方)领取了火星", tips))
        if not like and not spark:
            continue
        refs = ",".join(extract_server_msg_ids(row))
        if like and not refs:
            continue
        key = (row.get("conv_id"), row.get("sender_uid"),
               "like" if like else "spark", refs,
               cj.get("template") if spark else None)
        key = json.dumps(key, ensure_ascii=False, default=str)
        prev = pending.get(key)
        timestamp = row.get("timestamp")
        if prev and prev["tips"] != tips and abs(timestamp - prev["timestamp"]) <= 30:
            hidden.add(msg_id)
            pending.pop(key, None)
        else:
            pending[key] = {"timestamp": timestamp, "tips": tips}
    return hidden


def hidden_message_ids(messages):
    """该被藏起来的全部 msg_id：重复镜像 + 画不出内容的空记录。"""
    messages = [_as_row(row) for row in messages]
    hidden = set(duplicate_system_message_ids(messages))
    for row in messages:
        if row.get("msg_id") in hidden:
            continue
        if not should_show(row):
            hidden.add(row.get("msg_id"))
    return hidden


def display_count(messages):
    """会话里真正会显示出来的消息条数。"""
    if not messages:
        return 0
    return len(messages) - len(hidden_message_ids(messages))


def sender_display_counts(messages):
    """按发送者统计「真正显示出来的条数」（"设置我" 弹窗用）。"""
    hidden = hidden_message_ids(messages)
    counts = {}
    for row in messages:
        if row.get("msg_id") in hidden:
            continue
        uid = row.get("sender_uid")
        counts[uid] = counts.get(uid, 0) + 1
    return counts
