"""单文件 HTML 导出的单元测试。"""

from datetime import datetime

from extractor.html_exporter import SingleFileHtmlExporter, render_html


def _row(text, name="小明", is_self=False, ts=None, type=0, image=None, uid="a"):
    return {
        "time": ts or 1730000000,
        "uid": uid,
        "name": name,
        "self": is_self,
        "type": type,
        "text": text,
        "image": image,
        "transcript": "",
        "avatar": None,
    }


def test_render_html_basic_structure():
    rows = [
        _row("你好"),
        _row("在吗", name="小红"),
        _row("[系统消息] 已成为好友", type=99),
    ]
    doc = render_html(conv_name="测试会话", rows=rows, owner_name="我")
    assert "测试会话" in doc
    assert "你好" in doc
    assert "在吗" in doc
    assert 'class="msg-item msg-self' not in doc  # 没有本机消息
    assert doc.startswith("<!DOCTYPE html>")


def test_render_html_self_side_and_images():
    rows = [_row("我自己发的", is_self=True), _row("看图", image="data:image/png;base64,xxx")]
    doc = render_html(conv_name="t", rows=rows, owner_name="我")
    assert 'class="msg-item msg-self' in doc
    assert 'src="data:image/png;base64,xxx"' in doc


def test_render_html_escapes_html():
    rows = [_row("<script>alert(1)</script>")]
    doc = render_html(conv_name="t", rows=rows, owner_name="我")
    assert "<script>alert" not in doc
    assert "&lt;script&gt;" in doc


def test_render_html_range_label():
    rows = [_row("hi", ts=1730000000)]
    start = int(datetime(2025, 1, 1).timestamp())
    end = int(datetime(2025, 2, 1).timestamp())
    doc = render_html(conv_name="t", rows=rows, owner_name="我", start_ts=start, end_ts=end)
    assert "2025-01-01" in doc and "至" in doc


def test_exporter_class_defaults():
    exp = SingleFileHtmlExporter(conv_name="某会话", start_ts=100, end_ts=200)
    assert exp.conv_name == "某会话"
    assert exp.start_ts == 100 and exp.end_ts == 200