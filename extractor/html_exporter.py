"""单文件 HTML 导出：把一个会话（可限日期区间）导成一个独立 .html 文件。

文件里除了聊天内容什么都不带：图片 / 表情 / 视频封面等本机已有的媒体转成
base64 内嵌，语音与视频只写文字占位（几 MB 的媒体塞进 HTML 会把浏览器拖死）；
没有本机媒体的旧链接直接丢弃（链接早已过期，留着也显示不出来）。
双击用浏览器打开就能看，不需要服务、不需要 Python。
"""
from __future__ import annotations

import base64
import html
import os
import re
import time
from datetime import datetime
from pathlib import Path

from common import paths
from common.owner import detect_owner
from extractor.exporter import (
    _get_content_json,
    _resolve_message,
    build_export_filename,
    sender_display_name,
)

#: 单个媒体文件内嵌上限：超大文件塞 base64 会把 HTML 和浏览器都拖垮
_MAX_EMBED_BYTES = 8 * 1024 * 1024

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


def _media_data_url(rel_path: str | None) -> str | None:
    """把 data/ 下的本机媒体读成 data URL；没有文件或太大就返回 None。

    库里的 media_local_path 是相对 data/media/ 的路径（images/... 、voice/...），
    兼容直接给完整相对路径的旧数据。
    """
    if not rel_path:
        return None
    candidates = [
        Path(paths.DATA_DIR) / "media" / rel_path,
        Path(paths.DATA_DIR) / rel_path,
        Path(rel_path) if os.path.isabs(rel_path) else None,
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if candidate.is_file() and candidate.stat().st_size <= _MAX_EMBED_BYTES:
                data = candidate.read_bytes()
                mime = _MIME_BY_SUFFIX.get(candidate.suffix.lower())
                if mime:
                    return "data:" + mime + ";base64," + base64.b64encode(data).decode()
                return None
        except OSError:
            continue
    return None


def _fmt_time(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _escape(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


class SingleFileHtmlExporter:
    """参数与 ChatLabExporter 对齐；额外支持 start_ts / end_ts 日期区间。"""

    def __init__(self, conv_name: str | None = None,
                 start_ts: int | None = None, end_ts: int | None = None,
                 output_dir: str | os.PathLike[str] | None = None):
        self.conv_name = conv_name
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.output_dir = paths.DATA_DIR if output_dir is None else os.fspath(output_dir)

    def export(self, output_path: str | None = None) -> str | None:
        from common.db import init_db
        from extractor.models import get_db

        init_db()
        conn = get_db()
        try:
            return self._export(conn, output_path)
        finally:
            conn.close()

    def _export(self, conn, output_path: str | None) -> str | None:
        if self.conv_name:
            row = conn.execute(
                "SELECT conv_id, name, conv_type FROM conversations WHERE name = ?",
                (self.conv_name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT conv_id, name, conv_type FROM conversations "
                "ORDER BY last_message_time DESC LIMIT 1"
            ).fetchone()
        if not row:
            print(f"[-] 未找到会话: {self.conv_name or '(any)'}")
            return None

        conv_id, conv_name, conv_type = row["conv_id"], row["name"], row["conv_type"]
        owner_uid, owner_name = detect_owner(conn)
        owner_name = owner_name or "我"

        users_map = {}
        users_avatars = {}
        for u in conn.execute("SELECT uid, nickname, avatar_url FROM users").fetchall():
            if u["uid"] and u["nickname"]:
                users_map[u["uid"]] = u["nickname"]
            if u["uid"] and u["avatar_url"]:
                users_avatars[u["uid"]] = u["avatar_url"]

        sql = (
            "SELECT m.*, vt.text_result AS voice_transcription "
            "FROM messages m "
            "LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id "
            "WHERE m.conv_id = ?"
        )
        params: list = [conv_id]
        if self.start_ts:
            sql += " AND m.timestamp >= ?"
            params.append(self.start_ts)
        if self.end_ts:
            sql += " AND m.timestamp < ?"
            params.append(self.end_ts)
        sql += " ORDER BY m.seq ASC"
        messages = conn.execute(sql, params).fetchall()

        media_dir = Path(paths.DATA_DIR) / "media"
        rows = []
        previous_shares: dict[str, str] = {}
        for msg in messages:
            rows.append(self._build_row(
                msg, conn, media_dir, users_map=users_map,
                users_avatars=users_avatars,
                owner_uid=owner_uid, owner_name=owner_name,
                conv_type=conv_type, conv_name=conv_name,
                previous_shares=previous_shares,
            ))

        if output_path is None:
            exported_at = int(time.time())
            output_path = os.path.join(
                self.output_dir,
                build_export_filename(conv_name or conv_id, "html", exported_at),
            )

        doc = render_html(
            conv_name=conv_name or conv_id,
            rows=rows,
            owner_name=owner_name,
            start_ts=self.start_ts,
            end_ts=self.end_ts,
        )
        Path(output_path).write_text(doc, encoding="utf-8")
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[+] HTML 导出完成: {output_path} ({size_mb:.1f} MB, {len(rows)} 条消息)")
        return output_path

    def _build_row(self, msg, conn, media_dir, *, users_map, users_avatars,
                   owner_uid, owner_name, conv_type, conv_name, previous_shares):
        """解析成 {time, name, self, text, image, transcript} 的展示行。"""
        cj = _get_content_json(msg)
        uid = msg["sender_uid"] or ""
        display_name = sender_display_name(
            uid, msg["sender_name"], users_map=users_map, owner_uid=owner_uid,
            owner_name=owner_name, conv_type=conv_type, conv_name=conv_name,
        )
        # plain=True：文本干净（语音写 [语音 N秒]、分享写 [分享X] 标题）
        content, chatlab_type, _stats = _resolve_message(
            msg, cj, media_dir, embed_images=False, plain=True,
        )
        is_self = bool(uid) and uid == owner_uid
        row = {
            "time": msg["timestamp"] or 0,
            "uid": uid,
            "name": display_name,
            "self": is_self,
            "type": chatlab_type,
            "text": content,
            "image": None,
            "transcript": msg["voice_transcription"] or "",
            "avatar": users_avatars.get(uid) or "",
        }
        # 图片/表情/实况图：有本机文件就内嵌
        local = msg["media_local_path"] or ""
        if chatlab_type in (1, 5) or local:
            row["image"] = _media_data_url(local)
        # 视频封面也内嵌一张（本机有的话）
        return row


# 查看器的 5 套主题变量（与 frontend/src/style.css 同源；导出文件里内嵌一份，
# 双击离线打开也能还原配色）。--bg-message-self 是渐变，其余是纯色。
_THEME_VARS = {
    "dark": {
        "--bg-primary": "#0f1014", "--bg-secondary": "#15171e", "--bg-tertiary": "#21242e",
        "--bg-message-self": "linear-gradient(180deg, #f2596f 0%, #e34a60 100%)",
        "--bg-message-other": "#1e212a",
        "--text-primary": "#e9eaf0", "--text-secondary": "#9a9daa", "--text-muted": "#656875",
        "--border-color": "#23262f", "--accent": "#f2586e", "--text-on-self": "#ffffff",
    },
    "wechat": {
        "--bg-primary": "#ededed", "--bg-secondary": "#f7f7f7", "--bg-tertiary": "#d9d9d9",
        "--bg-message-self": "#95ec69", "--bg-message-other": "#ffffff",
        "--text-primary": "#1a1a1a", "--text-secondary": "#555555", "--text-muted": "#999999",
        "--border-color": "#d0d0d0", "--accent": "#07c160", "--text-on-self": "#111111",
    },
    "light": {
        "--bg-primary": "#ffffff", "--bg-secondary": "#f5f5f5", "--bg-tertiary": "#ebebeb",
        "--bg-message-self": "#d1e8ff", "--bg-message-other": "#f0f0f0",
        "--text-primary": "#1a1a1a", "--text-secondary": "#555555", "--text-muted": "#999999",
        "--border-color": "#e0e0e0", "--accent": "#1677ff", "--text-on-self": "#1a1a1a",
    },
    "warm": {
        "--bg-primary": "#2b1f14", "--bg-secondary": "#3a2918", "--bg-tertiary": "#4a3520",
        "--bg-message-self": "#8b5e3c", "--bg-message-other": "#3d2e1e",
        "--text-primary": "#f0e0c8", "--text-secondary": "#c8a882", "--text-muted": "#906040",
        "--border-color": "#4a3520", "--accent": "#e8a050", "--text-on-self": "#ffffff",
    },
    "purple": {
        "--bg-primary": "#1a0a2e", "--bg-secondary": "#24104a", "--bg-tertiary": "#3a1a6e",
        "--bg-message-self": "#6a2fad", "--bg-message-other": "#2d1550",
        "--text-primary": "#e8d8ff", "--text-secondary": "#b090d0", "--text-muted": "#7050a0",
        "--border-color": "#3a1a6e", "--accent": "#c060ff", "--text-on-self": "#ffffff",
    },
}

# 与查看器 highlight.js 相同的文字表情记号规则
_EMOJI_TOKEN_RE = re.compile(r"\[([^\[\]\n]{1,20})\]")

# 抖音官方文字表情名单（与 frontend/src/lib/emojiAssets.js 一致，有本地图片才替换）
OFFICIAL_EMOJI_NAMES = (
    '微笑、色、发呆、酷拽、抠鼻、流泪、捂脸、发怒、呲牙、尬笑、害羞、调皮、'
    '舔屏、看、爱心、比心、赞、鼓掌、感谢、抱抱你、玫瑰、尴尬流汗、戳手手、'
    '星星眼、杀马特、黄脸干杯、抱紧自己、拜拜、热化了、黄脸祈祷、懵、举手、'
    '加功德、摊手、无语流汗、续火花吧、点火、哭哭、吐舌小狗、送花、爱心手、'
    '贴贴、灵机一动、耶、打脸、大笑、机智、送心、666、闭嘴、来看我、'
    '一起加油、哈欠、震惊、晕、衰、困、疑问、泣不成声、小鼓掌、大金牙、偷笑、'
    '石化、思考、吐血、可怜、嘘、撇嘴、笑哭、奸笑、得意、憨笑、坏笑、抓狂、'
    '泪奔、钱、恐惧、愉快、快哭了、翻白眼、互粉、我想静静、委屈、鄙视、飞吻、'
    '再见、紫薇别走、听歌、求抱抱、绝望的凝视、不失礼貌的微笑、不看、裂开、'
    '干饭人、庆祝、吐舌、呆无辜、白眼、猪头、冷漠、暗中观察、二哈、菜狗、黑脸、'
    '展开说说、蜜蜂狗、柴犬、摸头、皱眉、擦汗、红脸、做鬼脸、强、如花、吐、'
    '惊喜、敲打、奋斗、吐彩虹、大哭、嘿哈、惊恐、囧、难过、斜眼、阴险、悠闲、'
    '咒骂、吃瓜群众、绿帽子、敢怒不敢言、求求了、眼含热泪、叹气、好开心、'
    '不是吧、鞠躬、躺平、九转大肠、不你不想、一头乱麻、kisskiss、'
    '你不大行、噢买尬、宕机、苦涩、逞强落泪、求机位-黄脸、求机位3、点赞、'
    '精选、强壮、碰拳、OK、击掌、左上、握手、抱拳、勾引、拳头、弱、胜利、'
    '右边、左边、嘴唇、心碎、凋谢、愤怒、垃圾、啤酒、咖啡、蛋糕、礼物、撒花、'
    '加一、减一、okk、V5、绝、给力、红包、屎、发、18禁、炸弹、西瓜、'
    '加鸡腿、握爪、太阳、月亮、给跪了、蕉绿、扎心、胡瓜、打call、栓Q、'
    '雪花、圣诞树、平安果、圣诞帽、气球、烟花、福、candy、糖葫芦、鞭炮、'
    '元宝、灯笼、锦鲤、巧克力、戒指、棒棒糖、纸飞机、粽子'
).split('、')

def _emoji_img_html(token: str) -> str | None:
    """文字表情 → 内嵌 <img>；本机没有这张图就返回 None（保留文字记号）。"""
    name = token[1:-1]
    if name not in OFFICIAL_EMOJI_NAMES:
        return None
    path = Path(paths.REPO_ROOT) / "assets" / "emoji" / (name + ".webp")
    try:
        if not path.is_file() or path.stat().st_size > _MAX_EMBED_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return (
        '<img class="msg-emoji" src="data:image/webp;base64,'
        + base64.b64encode(data).decode() + '" alt="' + _escape(token) + '">'
    )

def _render_text_html(text: str) -> str:
    """正文 → HTML：先转义，再把官方文字表情记号换成内嵌图片。"""
    out = []
    last = 0
    for match in _EMOJI_TOKEN_RE.finditer(text):
        out.append(_escape(text[last:match.start()]))
        img = _emoji_img_html(match.group(0))
        out.append(img if img else _escape(match.group(0)))
        last = match.end()
    out.append(_escape(text[last:]))
    return "".join(out).replace("\n", "<br>")

def _avatar_html(name: str, avatar_rel: str | None, is_self: bool) -> str:
    """头像：本机有头像文件就内嵌 <img>，否则用昵称首字圆形底。"""
    if avatar_rel:
        data = _media_data_url(avatar_rel)
        if data:
            return '<img class="msg-avatar-img" src="' + data + '" alt="">'
    return _escape((name or "?")[:1])

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="__THEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --radius:12px; --bubble-radius:14px; --msg-font-size:14px; --msg-sender-size:12px;
  --msg-time-size:11px; --msg-line-height:1.55; --bubble-pad-x:13px; --bubble-pad-y:9px;
  --msg-max-width:74%; --msg-avatar-size:36px; --msg-gap:3px; --msg-group-gap:16px;
  --msg-list-pad-x:22px; --msg-list-pad-y:18px; --media-max-size:240px; --emoji-size:1em;
  --font-viewer:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;
  __THEMEVARS__
}
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg-primary); color:var(--text-primary); font:var(--msg-font-size)/var(--msg-line-height) var(--font-viewer); }
.wrap { max-width:820px; margin:0 auto; padding:var(--msg-list-pad-y) var(--msg-list-pad-x) 60px; }
h1 { font-size:17px; font-weight:600; text-align:center; padding:10px 0 2px; }
.range { text-align:center; color:var(--text-muted); font-size:12px; margin-bottom:10px; }
.msg-time-divider { text-align:center; margin:var(--msg-group-gap) 0 10px; }
.msg-time-divider span { font-size:var(--msg-time-size); color:var(--text-secondary);
  background:color-mix(in srgb, var(--text-primary) 6%, transparent); padding:3px 10px; border-radius:6px; }
.msg-item { display:flex; gap:10px; margin-bottom:var(--msg-gap); max-width:var(--msg-max-width); }
.msg-item.group-start { margin-top:var(--msg-group-gap); }
.msg-item.msg-self { flex-direction:row-reverse; margin-left:auto; }
.msg-item.msg-self > .msg-body { align-items:flex-end; }
.msg-avatar { width:var(--msg-avatar-size); height:var(--msg-avatar-size); border-radius:50%;
  display:flex; align-items:center; justify-content:center; font-size:calc(var(--msg-avatar-size)*0.39);
  font-weight:600; flex-shrink:0; margin-top:2px; overflow:hidden;
  background:var(--bg-tertiary); color:var(--text-secondary); }
.msg-avatar-img { width:100%; height:100%; object-fit:cover; }
.msg-body { min-width:0; display:flex; flex-direction:column; align-items:flex-start; }
.msg-sender { font-size:var(--msg-sender-size); color:var(--text-muted); margin-bottom:3px; }
.msg-bubble { background:var(--bg-message-other); padding:var(--bubble-pad-y) var(--bubble-pad-x);
  border-radius:var(--bubble-radius); border-top-left-radius:4px; font-size:var(--msg-font-size);
  line-height:var(--msg-line-height); width:fit-content; max-width:100%; word-break:break-word;
  white-space:pre-wrap; box-shadow:0 1px 2px rgba(0,0,0,0.15); }
.msg-item.msg-self > .msg-body > .msg-bubble { background:var(--bg-message-self);
  border-top-left-radius:var(--bubble-radius); border-top-right-radius:4px;
  color:var(--text-on-self); }
.msg-bubble img.media { max-width:var(--media-max-size); max-height:var(--media-max-size);
  border-radius:8px; display:block; }
.msg-emoji { width:var(--emoji-size); height:var(--emoji-size); vertical-align:-0.15em; }
.msg-time { font-size:var(--msg-time-size); color:var(--text-muted); margin-top:3px; }
.msg-item.msg-self > .msg-body > .msg-time { text-align:right; }
.sys { text-align:center; font-size:12px; color:var(--text-secondary); margin:8px 0; }
.foot { text-align:center; color:var(--text-muted); font-size:11px; margin-top:40px; opacity:.7; }
@media (prefers-color-scheme: dark) { body { background:var(--bg-primary); } }
</style>
</head>
<body>
<div class="wrap">
<h1>__TITLE__</h1>
<div class="range">__RANGE__</div>
__BODY__
<div class="foot">由 douyin-chat-export 导出于 __EXPORTED__</div>
</div>
</body>
</html>"""


def _current_theme() -> str:
    """导出时用服务器配置的查看器主题；读不到配置时用暗色。"""
    try:
        from common.appearance import load_appearance
        from common import config
        cfg = load_appearance(config.load_config())
        return cfg.get("viewer", {}).get("theme") or "dark"
    except Exception:
        return "dark"


def render_html(*, conv_name, rows, owner_name, start_ts=None, end_ts=None,
                theme: str | None = None) -> str:
    """把展示行渲染成与聊天查看器同款样式的独立 HTML 文档。"""
    theme = theme if theme in _THEME_VARS else (_current_theme() if theme is None else "dark")
    vars_css = "\n".join(f"  {k}: {v};" for k, v in _THEME_VARS[theme].items())
    day_labels = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    body_parts: list[str] = []
    last_day = None
    last_uid = None
    for row in rows:
        ts = row["time"]
        day = (datetime.fromtimestamp(ts).strftime("%Y年%m月%d日 ")
              + day_labels[datetime.fromtimestamp(ts).weekday()]) if ts else "时间未知"
        if day != last_day:
            body_parts.append('<div class="msg-time-divider" role="separator"><span>'
                             + _escape(day) + "</span></div>")
            last_day = day
            last_uid = None
        text = row["text"]
        if row["type"] == 99 or text.startswith("[系统消息]"):
            body_parts.append('<div class="sys">' + _escape(text) + "</div>")
            continue
        is_start = row["uid"] != last_uid
        last_uid = row["uid"]
        classes = "msg-item" + (" msg-self" if row["self"] else "")
        classes += " group-start" if is_start else ""
        avatar = _avatar_html(row["name"], row.get("avatar"), row["self"])
        head = (
            '<div class="msg-sender">' + _escape(row["name"]) + "</div>"
        ) if is_start else ""
        bubble: list[str] = []
        if row["image"]:
            bubble.append('<img class="media" src="' + row["image"] + '" alt="图片">')
        elif text:
            bubble.append(_render_text_html(text))
        if row.get("transcript"):
            bubble.append('<div style="font-size:12px;opacity:.75;margin-top:4px">'
                          + "转写：" + _escape(row["transcript"]) + "</div>")
        body_parts.append(
            '<div class="' + classes + '">'
            + '<div class="msg-avatar">' + avatar + "</div>"
            + '<div class="msg-body">' + head
            + '<div class="msg-bubble">' + "".join(bubble) + "</div>"
            + '<div class="msg-time">' + _escape(_fmt_time(ts)) + "</div></div></div>"
        )
    range_text = "全部消息"
    if start_ts or end_ts:
        start_label = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d") if start_ts else "最早"
        end_label = datetime.fromtimestamp((end_ts or 0) - 1).strftime("%Y-%m-%d") if end_ts else "最新"
        range_text = start_label + " 至 " + end_label
    doc = _HTML_TEMPLATE
    doc = doc.replace("__THEME__", _escape(theme))
    doc = doc.replace("__THEMEVARS__", vars_css)
    doc = doc.replace("__TITLE__", _escape(conv_name))
    doc = doc.replace("__RANGE__", _escape(range_text))
    doc = doc.replace("__BODY__", "\n".join(body_parts))
    doc = doc.replace("__EXPORTED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    return doc
