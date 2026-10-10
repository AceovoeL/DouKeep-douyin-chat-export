"""纯文本（txt）导出：一条消息一行「[昵称]正文」。

用户要的形态（2026-10-11 提）：

* 文字消息 —— ``[昵称]文字消息``（表情本来就在正文里，例如 ``[昵称]文字消息[憨笑]``）；
* 分享消息 —— ``[昵称][分享视频] 标题``：只留类型前缀和具体内容（作品标题、评论内容），
  不带作者与跳转链接；
* 系统消息 —— ``[系统消息] 内容``：这类消息没有发送者，不写昵称。

上万条消息的会话导成 txt 是为了直接喂给 AI 分析，所以这里除了一行行钉住写法，
还钉住「文件里不许出现跳转链接、base64 图片和原始 JSON」。
"""
import json

from common.message_kinds import share_kind, share_text, strip_share_label
from extractor.exporter import (
    ChatLabExporter,
    is_empty_system_line,
    plain_text_line,
    _resolve_message,
)
from tests.conftest import insert_conversation, insert_message


def _raw(cj):
    return json.dumps({"content_json": json.dumps(cj, ensure_ascii=False)}, ensure_ascii=False)


def _msg(**kw):
    base = {"msg_type": 1, "content": "", "media_url": None, "media_local_path": None}
    base.update(kw)
    return base


# ── 分享卡片的类型与标题（common/message_kinds.py）──

def test_share_kind_matches_the_panel_classifier():
    # 样例与前端 sharePreview 的用例同源（frontend/src/lib/sharePreview.test.js）
    assert share_kind({"aweType": 800, "content_title": "标题"}) == "视频"
    assert share_kind({"aweType": 11054, "itemId": "42"}) == "视频"
    assert share_kind({"aweType": 10500, "comment": "评论内容"}) == "评论"
    assert share_kind({"awemeType": 68, "content_title": "标题"}) == "图文"
    assert share_kind({"awemeType": "68", "is_live_photo": "1"}) == "动图"
    assert share_kind({"awemeType": 163, "content_title": "标题"}) == "文章"
    assert share_kind({"aweType": 10401, "content_title": "商品名称"}) == "商品"
    assert share_kind({"aweType": 805, "itemId": "9"}) == "限时日常"
    assert share_kind({}) == ""


def test_share_kind_reads_the_card_own_label():
    # 11054/11063/11070 这些卡片把类型写在 push_detail 或正文的 [分享X] 标签里
    assert share_kind({"aweType": 11054, "push_detail": "分享[图文]"}) == "图文"
    assert share_kind({"aweType": 11070, "push_detail": "分享[直播]"}) == "直播"
    assert share_kind({"aweType": 11054}, "[分享动图]示例标题") == "动图"
    # 评论优先于卡片标注：10500 的正文就是评论内容
    assert share_kind({"aweType": 10500, "push_detail": "分享[视频]", "comment": "评论"}) == "评论"


def test_type_word_inside_a_text_message_is_not_a_share():
    """正文中间的 "[视频]" 可能只是谁打的字，这种普通文本消息别被当成分享卡片。"""
    assert share_kind({}, "这个是[分享视频]吗") == ""
    assert share_kind({}, "我刚看了那个[视频]") == ""
    # 正文以标签开头才算卡片标注
    assert share_kind({}, "[分享视频]标题") == "视频"


def test_share_text_keeps_the_title_and_drops_links():
    assert share_text(
        {"aweType": 800, "itemId": "42", "content_title": "视频标题", "content_name": "作者"}
    ) == "[分享视频] 视频标题"
    assert share_text({"aweType": 10500, "comment": "评论内容"}) == "[分享评论] 评论内容"
    # 商品正文是 "分享[商品]: 标题"，标签换成正统写法
    assert share_text({"aweType": 11029}, "分享[商品]: 中性笔套装") == "[分享商品] 中性笔套装"
    # 卡片没有标题：只留标签，不写 "[分享 链接]"
    assert share_text({"aweType": 11063, "push_detail": "分享[视频]"}, "分享[视频]") == "[分享视频]"
    assert share_text({"aweType": 805}, "[分享限时日常]") == "[分享限时日常]"
    # 老数据把整包 JSON 塞在 content 里：不许原样吐出来
    assert share_text({"aweType": 800}, '{"aweType":800,"content_title":"标题') == "[分享视频]"


def test_strip_share_label():
    assert strip_share_label("[分享图文]标题") == "标题"
    assert strip_share_label("分享[商品]: 标题") == "标题"
    assert strip_share_label('{"aweType":800}') == ""
    assert strip_share_label(None) == ""


# ── 一行一条 ──

def test_plain_text_line():
    assert plain_text_line("小明", "你好[憨笑]") == "[小明]你好[憨笑]"
    assert plain_text_line("小明", "[分享视频] 标题") == "[小明][分享视频] 标题"
    # 系统消息让位给 [系统消息] 标记，不写昵称
    assert plain_text_line("会话名", "[系统消息] 小明关注了你") == "[系统消息] 小明关注了你"


def test_empty_system_notice_is_not_a_line():
    """只有 [系统消息] 标签、没有正文的空壳通知不写（面板里本来也不显示）。"""
    assert is_empty_system_line("[系统消息]")
    assert is_empty_system_line("  [系统消息]  ")
    assert not is_empty_system_line(None)
    assert not is_empty_system_line("[系统消息] 小明关注了你")
    assert not is_empty_system_line("[小明]你好")


def test_plain_resolve_message_forms():
    # 文字与表情照原样保留
    assert _resolve_message(_msg(content="你好[憨笑]"), None, "/tmp", plain=True)[0] == "你好[憨笑]"
    # 图片只留标签，不嵌 base64
    assert _resolve_message(_msg(msg_type=3, media_url="https://cdn/x.jpg"), None, "/tmp", plain=True)[:2] == ("[图片]", 1)
    # 分享：标题 + 类型，不带 @作者 和链接
    share = {"aweType": 800, "itemId": "42", "content_title": "T", "content_name": "A"}
    assert _resolve_message(_msg(msg_type=4, content="{...}"), share, "/tmp", plain=True)[0] == "[分享视频] T"
    # 商品：不带商品链接
    product = {"aweType": 11029, "im_dynamic_patch": {"raw_data": json.dumps(
        {"content_top": {"content": "商品名称"},
         "whole_card": {"action_info": [{"params": {"schema": "sslocal://goods?commodity_id=123"}}]}})}}
    text, typ, _ = _resolve_message(_msg(content="{truncated"), product, "/tmp", plain=True)
    assert (text, typ) == ("[分享商品] 商品名称", 24)
    # 系统消息：统一 [系统消息] 前缀，不带昵称
    tips = {"aweType": 126, "tips": "{{1}}关注了你", "template": [{"key": 1, "name": "小明"}]}
    assert _resolve_message(_msg(msg_type=0, content="{}"), tips, "/tmp", plain=True)[0] == "[系统消息] 小明关注了你"
    # 用户名片：只留名字
    card = {"name": "名片昵称", "secUID": "sec-test", "desc": "简介"}
    assert _resolve_message(_msg(content="{...}"), card, "/tmp", plain=True)[0] == "[用户名片] 名片昵称"


def test_plain_keeps_voice_transcription_and_labels():
    voice = {"resource_url": "https://cdn/v.mpeg", "duration": 3000}
    assert _resolve_message(_msg(msg_type=0), voice, "/tmp", plain=True)[0] == "[语音 3秒]"
    video = {"video": {"vid": "v1"}, "duration": 12}
    assert _resolve_message(_msg(msg_type=5), video, "/tmp", plain=True)[0] == "[视频 12秒]"


# ── 整份文件 ──

def _export_txt(temp_db, tmp_path, conv_name="测试会话"):
    out = str(tmp_path / "export.txt")
    ChatLabExporter(conv_name=conv_name, output_format="txt").export(out)
    with open(out, encoding="utf-8") as handle:
        return out, handle.read().splitlines()


def test_txt_export_is_one_line_per_message(temp_db, tmp_path):
    import extractor.models as models

    conn = models.get_db()
    insert_conversation(conn, "c1", "测试会话", participant_uids='["owner","other"]', last_message_time=100)
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我方')")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('other','对方')")

    insert_message(conn, "m1", "c1", 1, sender_uid="owner", content="你好[憨笑]")
    insert_message(conn, "m2", "c1", 2, sender_uid="other", msg_type=2, content="[表情]",
                   media_url="https://cdn/9-ts-68616861.png")
    insert_message(conn, "m3", "c1", 3, sender_uid="owner", msg_type=3, media_url="https://cdn/pic.jpg")
    insert_message(conn, "m4", "c1", 4, sender_uid="other", msg_type=4, content="漏出来的json",
                   raw_data=_raw({"aweType": 800, "itemId": "7777", "content_title": "视频标题",
                                  "content_name": "作者"}))
    insert_message(conn, "m5", "c1", 5, sender_uid="owner", msg_type=4,
                   raw_data=_raw({"aweType": 10500, "itemId": "8888", "aweme_title": "作品标题",
                                  "comment": "评论内容", "comment_user_name": "路人"}))
    insert_message(conn, "m6", "c1", 6, sender_uid="other", msg_type=4, content="[分享图文]示例标题",
                   raw_data=_raw({"aweType": 11054, "item_id": "9999", "push_detail": "分享[图文]"}))
    insert_message(conn, "m7", "c1", 7, sender_uid="other", msg_type=0, content="{}",
                   raw_data=_raw({"tips": "{{1}}赞了你", "template": [{"key": 1, "name": "对方"}]}))
    insert_message(conn, "m8", "c1", 8, sender_uid="owner", msg_type=0,
                   raw_data=_raw({"resource_url": "https://cdn/v.mpeg", "duration": 3000}))
    insert_message(conn, "m9", "c1", 9, sender_uid="other", msg_type=5,
                   raw_data=_raw({"video": {"vid": "v1"}, "duration": 12}))
    # 正文中间出现 "[分享视频]" 字样只是谁打的字，不能被当成分享卡改写
    insert_message(conn, "m10", "c1", 10, sender_uid="other", content="这个是[分享视频]吗")
    # 抖音只发了个通知壳：库里只是 "[系统消息]" 占位，txt 里不该白占一行
    insert_message(conn, "m11", "c1", 11, sender_uid="other", msg_type=0, content="{}",
                   raw_data=_raw({}))
    conn.commit()
    conn.close()

    out, lines = _export_txt(temp_db, tmp_path)

    assert out.endswith(".txt")
    assert "[系统消息]" not in lines
    assert lines == [
        "[我方]你好[憨笑]",
        "[对方][haha]",
        "[我方][图片]",
        "[对方][分享视频] 视频标题",
        "[我方][分享评论] 评论内容",
        "[对方][分享图文] 示例标题",
        "[系统消息] 对方赞了你",
        "[我方][语音 3秒]",
        "[对方][视频 12秒]",
        "[对方]这个是[分享视频]吗",
    ]


def test_txt_export_has_no_links_media_or_raw_json(temp_db, tmp_path):
    """给 AI 看的文件里不该出现跳转链接、base64 和原始 JSON。"""
    import extractor.models as models

    conn = models.get_db()
    insert_conversation(conn, "c1", "干净会话", participant_uids='["owner"]')
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我')")
    # 本地真有这张图：非纯文本导出会内嵌成 base64，纯文本导出只写 [图片]
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nexample")
    insert_message(conn, "m1", "c1", 1, sender_uid="owner", msg_type=3,
                   media_local_path="image.png", media_url="https://cdn/pic.jpg")
    insert_message(conn, "m2", "c1", 2, sender_uid="owner", msg_type=4,
                   raw_data=_raw({"aweType": 800, "itemId": "42", "content_title": "标题"}))
    insert_message(conn, "m3", "c1", 3, sender_uid="owner", msg_type=4,
                   raw_data=_raw({"aweType": 700, "text": "这条也在说事",
                                  "related_share_video": {"itemId": "42"}}))
    conn.commit()
    conn.close()

    _out, lines = _export_txt(temp_db, tmp_path, "干净会话")
    text = "\n".join(lines)

    assert "http" not in text and "data:" not in text and "douyin.com" not in text
    assert "[图片]" in text and "base64" not in text
    assert not any(line.lstrip().startswith("{") for line in lines)
    assert "[我]这条也在说事" in lines
    # 每行都以 [ 开头（合并转发除外，那种正文本身就分几行）
    assert all(line.startswith("[") for line in lines)


def test_txt_filename_and_panel_bundle(temp_db, tmp_path, monkeypatch):
    import zipfile

    from backend import control_panel
    from common import paths
    from extractor.exporter import build_export_filename
    import extractor.models as models

    assert build_export_filename("会话", "txt", timestamp=0).endswith("_export.txt")

    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    conn = models.get_db()
    insert_conversation(conn, "c1", "会话一", participant_uids='["owner"]')
    insert_conversation(conn, "c2", "会话二", participant_uids='["owner"]')
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我')")
    insert_message(conn, "m1", "c1", 1, sender_uid="owner", content="一")
    insert_message(conn, "m2", "c2", 1, sender_uid="owner", content="二")
    conn.commit()
    conn.close()

    control_panel._export_state.update({"status": "idle", "file_path": None, "message": ""})
    control_panel._do_export("txt", "", ["会话一", "会话二"])

    assert control_panel._export_state["status"] == "completed"
    with zipfile.ZipFile(tmp_path / "export.zip") as archive:
        names = archive.namelist()
        assert len(names) == 2 and all(name.endswith(".txt") for name in names)
        assert archive.read(names[0]).decode("utf-8").strip() == "[我]一"
