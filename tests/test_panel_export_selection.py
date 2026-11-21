"""导出时「一个会话都没勾」该怎么处理。

2026-10-10 的实测：面板上一个都没勾就点「导出」，后端把空列表当成「没指定」，
顺手导出了最近活跃的那一个会话（2.8 万条消息、509 MB），看着像"什么都没勾却导出了
一大堆"。这里的用例锁住新行为：拒绝并说明白，绝不替用户挑会话；整库导出不受勾选影响。
"""
import asyncio
import json
import os

from backend import control_panel as cp
from common import paths

PANEL_HTML = os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html")


def _isolate(tmp_path, monkeypatch):
    """配置指到临时文件：这些用例不能碰真的 config/panel_config.json。"""
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "panel_config.json"))
    monkeypatch.setitem(cp._export_state, "status", "idle")
    monkeypatch.setitem(cp._export_state, "file_path", None)
    monkeypatch.setitem(cp._export_state, "message", "")


def _record_export(monkeypatch):
    """把真正干活的那一步换成记账的：只关心「被要求导出什么」。"""
    calls = []

    def fake_do_export(fmt, filter_name, conversations, name_style="full", start_ts=None, end_ts=None):
        calls.append((fmt, filter_name, conversations, name_style, start_ts, end_ts))

    monkeypatch.setattr(cp, "_do_export", fake_do_export)
    return calls


def test_nothing_selected_is_refused(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(format="jsonl", conversations=[])))

    assert response.status_code == 400
    assert "勾选" in response.body.decode("utf-8")
    assert calls == []                              # 没有替用户挑会话
    assert cp._export_state["status"] == "idle"     # 也没把面板状态改成「正在导出」


def test_missing_selection_is_refused_too(tmp_path, monkeypatch):
    """老调用方只发 {"format": "jsonl"}：同样不许偷偷导出。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(format="jsonl")))

    assert response.status_code == 400
    assert calls == []


def test_blank_names_do_not_count_as_a_selection(tmp_path, monkeypatch):
    """空串 / 空格不算「选了一个会话」（否则会去库里找一个名字为空的会话）。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(format="txt", conversations=[" ", ""])))

    assert response.status_code == 400
    assert calls == []


def test_selected_conversations_are_exported(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(format="txt", conversations=[" 会话A ", "会话B"])))

    assert calls == [("txt", "", ["会话A", "会话B"], "full", None, None)]   # 首尾空格顺手清掉


def test_database_export_ignores_the_selection(tmp_path, monkeypatch):
    """整库导出就是「全部聊天记录」，不看勾选 —— 这是唯一会导出所有会话的方式。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(format="database", conversations=[])))

    assert calls == [("database", "", None, "full", None, None)]


def test_name_style_is_passed_through_and_bad_values_fall_back(tmp_path, monkeypatch):
    """txt 的「昵称怎么代替」要传到导出那一层；乱传的值当「不代替」，不能带进文件。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(
        format="txt", conversations=["会话A"], name_style="letter")))
    _isolate(tmp_path, monkeypatch)          # 上一次还停在「进行中」，先让它闲下来
    asyncio.run(cp.start_export(cp.ExportRequest(
        format="txt", conversations=["会话A"], name_style="随便写的")))

    assert [call[3] for call in calls] == ["letter", "full"]


def test_selection_is_persisted(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(format="jsonl", conversations=["会话A"])))

    with open(paths.CONFIG_PATH, encoding="utf-8") as handle:
        assert json.load(handle)["export_selected"] == ["会话A"]


def test_panel_html_blocks_export_until_something_is_checked():
    with open(PANEL_HTML, encoding="utf-8") as handle:
        html = handle.read()

    assert html.count("exportNeedConv:") == 2      # 中文、英文各一份
    assert "exportBlocked" in html
    assert "updateExportGate" in html


def test_panel_html_offers_the_three_name_styles():
    """txt 的「不代替 / 数字代替 / 字母代替」三个单选项，中英文文案都要在。"""
    with open(PANEL_HTML, encoding="utf-8") as handle:
        html = handle.read()

    for key in ("nameStyleFull:", "nameStyleNumber:", "nameStyleLetter:"):
        assert html.count(key) == 2                # 中文、英文各一份
    assert "exportNameStyleRow" in html
    assert "name_style: exportNameStyle()" in html

def test_txt_date_range_is_passed_through(tmp_path, monkeypatch):
    """面板选「部分导出」时，起止时间要原样传到导出那一层。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(
        format="txt", conversations=["会话A"], start_ts=100, end_ts=200)))

    assert calls == [("txt", "", ["会话A"], "full", 100, 200)]


def test_invalid_txt_date_range_is_refused(tmp_path, monkeypatch):
    """开始晚于结束要当场拒绝，绝不去动手导出。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(
        format="txt", conversations=["会话A"], start_ts=200, end_ts=100)))

    assert response.status_code == 400
    assert calls == []


def test_panel_html_has_txt_export_dialog():
    """纯文本 TXT 也先弹窗选「全部 / 区间」，全部导出同样给出预估。"""
    with open(PANEL_HTML, encoding="utf-8") as handle:
        html = handle.read()

    for key in ("txtExportModal", "openTxtExport", "onTxtScopeChange", "runTxtExport"):
        assert key in html
    assert "/panel/api/export/txt/daily" in html
    assert "/panel/api/export/txt/estimate" in html


def test_export_estimate_formats_size_with_matching_unit():
    """导出预估的文件大小要按 B / KB / MB / GB 自动换单位，不能一律写成 KB。"""
    with open(PANEL_HTML, encoding="utf-8") as handle:
        html = handle.read()

    assert "estimated_kb" not in html
    assert "estimated_mb" not in html
    assert html.count("fmtBytes(d.estimated_bytes)") == 2

