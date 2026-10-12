"""纯文本（txt）导出：一条消息一行「[昵称]正文」。

用户要的形态（2026-10-11 提）：

* 文字消息 —— ``[昵称]文字消息``（表情本来就在正文里，例如 ``[昵称]文字消息[憨笑]``）；
* 分享消息 —— ``[昵称][分享视频] 标题``：只留类型前缀和具体内容（作品标题、评论内容），
  不带作者与跳转链接；
* 系统消息 —— ``[系统消息] 内容``：这类消息没有发送者，不写昵称。

2026-10-12 又加了两条（同一个用户提的，为了把文件喂给 AI 时更小）：

* **同一个人连着说话只在第一条写昵称**，后面的消息直接写正文、不缩进（中间夹了别人的
  话或一条系统消息就算断开）；
* **昵称可以用代号代替** —— 开头先写一段「定义昵称：」对照表，正文里的昵称换成
  ``[0]``/``[A]`` 这种代号。

上万条消息的会话导成 txt 是为了直接喂给 AI 分析，所以这里除了一行行钉住写法，
还钉住「文件里不许出现跳转链接、base64 图片和原始 JSON」。
"""
import json

from common.message_kinds import share_kind, share_text, strip_share_label
from extractor.exporter import (
    ChatLabExporter,
    format_name_alias,
    is_empty_system_line,
    normalize_name_style,
    plain_text_line,
    plain_text_lines,
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


# ── 连着说话只写一次昵称、昵称换代号 ──

def _chatlab(name, content, sender=None):
    return {"sender": sender or name, "accountName": name, "content": content}


def test_only_the_first_message_of_a_run_keeps_the_name():
    """用户给的例子：同一个人的第二句起只写正文，前面不加缩进。"""
    messages = [
        _chatlab("小红", "今天天气怎么样"),
        _chatlab("小红", "下雨了吗"),
        _chatlab("小明", "下雨了"),
        _chatlab("小明", "记得带伞"),
        _chatlab("小明", "不然会被淋湿的"),
        _chatlab("小红", "收到"),
        _chatlab("小红", "我会带伞的"),
    ]
    assert plain_text_lines(messages) == [
        "[小红]今天天气怎么样",
        "下雨了吗",
        "[小明]下雨了",
        "记得带伞",
        "不然会被淋湿的",
        "[小红]收到",
        "我会带伞的",
    ]


def test_a_system_message_breaks_the_run():
    """系统消息会写出来插在中间，所以它后面那条要把昵称补回来。"""
    messages = [
        _chatlab("小红", "你好"),
        _chatlab("会话名", "[系统消息] 小明关注了你"),
        _chatlab("小红", "在吗"),
    ]
    assert plain_text_lines(messages) == [
        "[小红]你好",
        "[系统消息] 小明关注了你",
        "[小红]在吗",
    ]


def test_an_unwritten_notice_does_not_break_the_run():
    """只有 [系统消息] 标签的空壳整行不写，也就没有打断连着说话的那一串。"""
    messages = [_chatlab("小红", "你好"), _chatlab("小红", "[系统消息]"), _chatlab("小红", "在吗")]
    assert plain_text_lines(messages) == ["[小红]你好", "在吗"]


def test_names_replaced_by_numbers():
    messages = [_chatlab("小红", "你好"), _chatlab("小明", "你好呀")]
    assert plain_text_lines(messages, "number") == [
        "定义昵称：",
        "[0]小红",
        "[1]小明",
        "导出消息：",
        "[0]你好",
        "[1]你好呀",
    ]
    # 连着说话照样省略代号，文件更小
    run = [_chatlab("小红", "你好"), _chatlab("小红", "在吗"), _chatlab("小明", "在")]
    assert plain_text_lines(run, "number") == [
        "定义昵称：", "[0]小红", "[1]小明", "导出消息：", "[0]你好", "在吗", "[1]在",
    ]


def test_names_replaced_by_letters():
    messages = [_chatlab("小红", "你好"), _chatlab("小明", "你好呀")]
    assert plain_text_lines(messages, "letter") == [
        "定义昵称：",
        "[A]小红",
        "[B]小明",
        "导出消息：",
        "[A]你好",
        "[B]你好呀",
    ]


def test_name_style_accepts_panel_values_and_falls_back():
    assert normalize_name_style("") == "full"
    assert normalize_name_style("FULL") == "full"
    assert normalize_name_style("number") == "number"
    assert normalize_name_style("letter") == "letter"
    assert normalize_name_style("看不懂的值") == "full"       # 认不出来＝不代替，不报错
    # 超过 26 个人也不会撞车
    assert format_name_alias("letter", 0) == "A"
    assert format_name_alias("letter", 25) == "Z"
    assert format_name_alias("letter", 26) == "AA"
    assert format_name_alias("number", 12) == "12"


def test_number_style_defines_every_name_that_shows_up():
    """对照表要按第一次出现的先后列全，正文里不许冒出没定义的昵称。"""
    messages = [
        _chatlab("小红", "你好"),
        _chatlab("会话名", "[系统消息] 有人关注了你"),   # 系统消息不进对照表
        _chatlab("小明", "在"),
    ]
    lines = plain_text_lines(messages, "number")
    assert lines[:4] == ["定义昵称：", "[0]小红", "[1]小明", "导出消息："]
    assert lines[4:] == ["[0]你好", "[系统消息] 有人关注了你", "[1]在"]
    body = "\n".join(lines[4:])
    assert "小红" not in body and "小明" not in body


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

def _export_txt(temp_db, tmp_path, conv_name="测试会话", name_style="full"):
    out = str(tmp_path / "export.txt")
    ChatLabExporter(conv_name=conv_name, output_format="txt", name_style=name_style).export(out)
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
    # 最后两条都是「对方」连着发的，第二条起不写昵称
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
        "这个是[分享视频]吗",
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
    # 三条都是「我」一个人发的：只有第一条带昵称，其余直接写正文
    assert lines[0] == "[我][图片]"
    assert "这条也在说事" in lines
    assert sum(1 for line in lines if line.startswith("[我]")) == 1


def test_txt_export_can_hide_names_behind_aliases(temp_db, tmp_path):
    """面板上选「以数字代替昵称」：文件开头有对照表，正文里只剩代号。"""
    import extractor.models as models

    conn = models.get_db()
    insert_conversation(conn, "c1", "别名会话", participant_uids='["owner","other"]', last_message_time=100)
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我方')")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('other','对方')")
    insert_message(conn, "m1", "c1", 1, sender_uid="owner", content="你好")
    insert_message(conn, "m2", "c1", 2, sender_uid="other", content="你好呀")
    conn.commit()
    conn.close()

    _out, lines = _export_txt(temp_db, tmp_path, "别名会话", name_style="number")

    text = "\n".join(lines)
    assert lines == ["定义昵称：", "[0]我方", "[1]对方", "导出消息：", "[0]你好", "[1]你好呀"]
    assert "我方" not in "\n".join(lines[4:]) and "对方" not in "\n".join(lines[4:])
    assert "[我方]" not in text


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
