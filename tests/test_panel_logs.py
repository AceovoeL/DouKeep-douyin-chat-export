"""控制面板「日志」页：接口 + 接线。

后端服务把自己的输出写进 ``data/server.log``，重启过程本身写进 ``data/restart.log``；
这一页要能看这两份文件，还要能一键打开日志目录。

测试不碰真实 data/：两份日志的路径都指到临时文件，「打开文件夹」也把要跑的
命令记下来（不会真的弹出文件管理器）。
"""
import os

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import control_panel as cp

_PANEL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "panel", "static", "panel.html",
)


def _panel_html() -> str:
    with open(_PANEL, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    """把「后端服务日志」的路径指到临时文件（默认这个文件不存在）。"""
    path = tmp_path / "server.log"
    monkeypatch.setattr(cp, "SERVER_LOG_PATH", str(path))
    return path


@pytest.fixture
def restart_log_path(tmp_path, monkeypatch):
    """把「重启过程日志」的路径也指到临时文件。"""
    path = tmp_path / "restart.log"
    monkeypatch.setattr(cp, "RESTART_LOG_PATH", str(path))
    return path


# ── 读日志 ──────────────────────────────────────────────────────────────────

def test_real_log_paths_are_shown_as_data_paths():
    """真机上这两份日志就在项目的 data/ 里，面板上要显示成 data/xxx.log 这种好认的样子。"""
    assert cp._display_path(cp.SERVER_LOG_PATH) == "data/server.log"
    assert cp._display_path(cp.RESTART_LOG_PATH) == "data/restart.log"


def test_missing_log_is_an_empty_state_not_an_error(client, log_path):
    """文件还没有时也要 200：这不是错误，面板靠 exists 显示「还没有这份日志」。"""
    response = client.get("/panel/api/logs/server")

    assert response.status_code == 200
    body = response.json()
    assert body["log"] == ""
    assert body["exists"] is False
    assert body["size"] == 0
    assert body["modified_at"] is None
    # 路径按临时文件的位置算（真机上是 data/server.log，见上一个用例）
    assert body["path"] == cp._display_path(str(log_path))
    assert body["name"] == "server"


def test_returns_the_tail_of_the_file(client, log_path):
    # 按字节写：Windows 上 write_text 会把 \n 变成 \r\n，那和真实日志的换行不一样，
    # 断言里就得多带一个 \r，读起来全是噪音。
    log_path.write_bytes("".join(f"line {i}\n" for i in range(1, 21)).encode("utf-8"))

    body = client.get("/panel/api/logs/server?lines=5").json()

    assert body["exists"] is True
    assert body["log"] == "line 16\nline 17\nline 18\nline 19\nline 20\n"
    assert body["size"] == log_path.stat().st_size
    assert body["modified_at"] and body["modified_at"] > 0


def test_short_file_is_returned_whole(client, log_path):
    log_path.write_bytes("只有两行\n第二行\n".encode("utf-8"))

    body = client.get("/panel/api/logs/server?lines=500").json()

    assert body["log"] == "只有两行\n第二行\n"


def test_line_count_is_capped(client, log_path):
    """别人手动传一个很大的 lines 也不能把服务拖住。"""
    total = cp.LOG_VIEW_MAX_LINES + 50
    log_path.write_bytes("".join(f"line {i}\n" for i in range(total)).encode("utf-8"))

    body = client.get(f"/panel/api/logs/server?lines={total * 10}").json()

    assert len(body["log"].splitlines()) == cp.LOG_VIEW_MAX_LINES


def test_gbk_log_is_still_readable(client, log_path):
    """日志统一按 UTF-8 写，但真有 GBK 残留在里面时也不能让接口炸掉。"""
    log_path.write_bytes("启动完成\n".encode("gbk"))

    response = client.get("/panel/api/logs/server")

    assert response.status_code == 200
    assert "启动完成" in response.json()["log"]


def test_mixed_encoding_log_shows_every_line_correctly(client, log_path):
    """真机反馈的乱码：同一份 server.log 里 UTF-8 与 GBK 各占一段。

    服务的输出按什么编码写，取决于它是怎么起来的（手敲 uvicorn = GBK，start.ps1 /
    面板自动重启 = UTF-8），所以「后面新写的那几段」和「前面旧的那几段」可能编码不同。
    面板那边整份按一种编码读时，另一种编码的段落就变成了「鈫� / 鎴愬姛」这种乱码；
    按行判断编码之后，两种编码的行都要原样显示出来。
    """
    line = "17:40:45 GET /panel → 200  [控制面板页面] — 成功"
    newer = "17:52:06 GET /assets/index.js → 200  [前端静态资源] — 成功"
    log_path.write_bytes(line.encode("gb18030") + b"\n" + newer.encode("utf-8") + b"\n")

    body = client.get("/panel/api/logs/server").json()

    assert body["log"].splitlines() == [line, newer]


def test_restart_log_is_a_second_file(client, restart_log_path):
    """面板上的「重启过程」看的是另一份文件（data/restart.log）。"""
    restart_log_path.write_bytes("开始重启后端服务\n旧服务进程 1234 已退出\n".encode("utf-8"))

    body = client.get("/panel/api/logs/restart").json()

    assert body["name"] == "restart"
    assert body["path"] == cp._display_path(str(restart_log_path))
    assert body["exists"] is True
    assert "旧服务进程 1234 已退出" in body["log"]


def test_the_two_logs_do_not_mix(client, log_path, restart_log_path):
    log_path.write_bytes("服务日志\n".encode("utf-8"))
    restart_log_path.write_bytes("重启日志\n".encode("utf-8"))

    assert client.get("/panel/api/logs/server").json()["log"] == "服务日志\n"
    assert client.get("/panel/api/logs/restart").json()["log"] == "重启日志\n"


def test_unknown_log_name_is_rejected(client):
    """名字走白名单：不认识的（包括相对路径）一律 404，绝不按它去读文件。"""
    response = client.get("/panel/api/logs/secret")

    assert response.status_code == 404
    assert "未知的日志" in response.json()["error"]


# ── 打开日志文件夹 ──────────────────────────────────────────────────────────

def _record_popen(monkeypatch):
    """把 Popen 换掉，只记下要跑的命令（测试不会真弹文件管理器）。"""
    calls: list[list[str]] = []

    class _FakePopen:
        def __init__(self, command, **kwargs):
            calls.append(list(command))

    monkeypatch.setattr(cp.subprocess, "Popen", _FakePopen)
    return calls


@pytest.fixture
def panel_data_dir(tmp_path, monkeypatch):
    """把日志目录也指到临时目录：不在仓库里建 data/，也不去开真文件夹。"""
    folder = tmp_path / "data"
    folder.mkdir()
    monkeypatch.setattr(cp.paths, "DATA_DIR", str(folder))
    return folder


@pytest.mark.parametrize("platform, expected_head", [("windows", "explorer"),
                                                    ("macos", "open"),
                                                    ("linux", "xdg-open")])
def test_reveal_command_per_platform(tmp_path, platform, expected_head):
    """三个系统各用各的文件管理器；认得出文件时尽量连文件一起选中。"""
    target = str(tmp_path / "server.log")
    folder = str(tmp_path)

    with_file = cp._reveal_command(target, folder, platform=platform)
    without_file = cp._reveal_command(None, folder, platform=platform)

    assert with_file[0] == expected_head
    assert without_file[0] == expected_head
    # 选中文件那条命令里一定要有文件名；没有文件时只开目录
    assert target in " ".join(with_file) or platform == "linux"
    assert target not in " ".join(without_file)
    assert folder in " ".join(without_file)


def test_open_folder_runs_the_reveal_command(client, log_path, panel_data_dir, monkeypatch):
    log_path.write_bytes("服务日志\n".encode("utf-8"))
    calls = _record_popen(monkeypatch)

    response = client.post("/panel/api/logs/open-folder?name=server")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["folder"] == cp._display_path(str(panel_data_dir))
    assert calls == [cp._reveal_command(str(log_path), str(panel_data_dir))]


def test_open_folder_with_a_missing_file_still_opens_the_folder(client, log_path,
                                                                panel_data_dir, monkeypatch):
    """日志还没生成也要能打开目录（这是最常见的用法：先去看看有没有日志）。"""
    calls = _record_popen(monkeypatch)          # log_path 故意不创建

    response = client.post("/panel/api/logs/open-folder?name=server")

    assert response.status_code == 200
    assert calls == [cp._reveal_command(None, str(panel_data_dir))]


def test_open_folder_never_takes_a_path_from_the_caller(client, log_path,
                                                       panel_data_dir, monkeypatch):
    """名字只当白名单用：传个路径进来也不能出现在命令里（面板可能被远程打开）。"""
    calls = _record_popen(monkeypatch)
    evil = "../../etc"

    response = client.post("/panel/api/logs/open-folder?name=" + evil)

    assert response.status_code == 200
    assert calls and all(evil not in " ".join(command) for command in calls)
    assert calls == [cp._reveal_command(None, str(panel_data_dir))]


def test_open_folder_reports_a_failure(client, panel_data_dir, monkeypatch):
    """文件管理器起不来（例如 Linux 上没有 xdg-open）时要说清楚，不能装作成功。"""
    def _boom(command, **kwargs):
        raise OSError("no such file manager")

    monkeypatch.setattr(cp.subprocess, "Popen", _boom)

    response = client.post("/panel/api/logs/open-folder")

    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert "no such file manager" in body["error"]


# ── 面板接线 ────────────────────────────────────────────────────────────────

def test_panel_has_a_logs_page_in_the_sidebar():
    html = _panel_html()

    # 侧栏多一项「日志」，点一下切到这一页
    assert 'data-page="logs" onclick="showPage(\'logs\')"' in html
    assert 'data-i18n="navLogs">日志<' in html
    # 页面本体（和别的页一样是 panel-page）
    assert '<div class="panel-page" id="page-logs">' in html
    # 进页面恢复上次看的那份日志，并立刻取一次
    assert "if (id === 'logs')" in html
    assert "restoreLogsKind();" in html
    assert "document.getElementById('page-logs')?.classList.contains('active')" in html


def test_panel_can_switch_between_the_two_logs():
    html = _panel_html()

    # 两个单选框（和「采集」页的增量/全量用同一套 toggle 样式）
    assert 'id="logsKindServer" value="server" checked' in html
    assert 'id="logsKindRestart" value="restart" onchange="setLogsKind(\'restart\')"' in html
    assert 'data-i18n="logsKindServer">后端服务<' in html
    assert 'data-i18n="logsKindRestart">重启过程<' in html
    # 取哪一份由 logsKind 决定；切换时把结果记在本机
    assert "'/panel/api/logs/' + encodeURIComponent(kind) + '?lines=500'" in html
    assert "localStorage.setItem('panel-log-kind', logsKind);" in html
    # 说明/空状态/路径都跟着当前这份走
    assert "function logsKindMeta()" in html
    assert "logsNoteServer" in html and "logsNoteRestart" in html
    assert "logsEmptyServer" in html and "logsEmptyRestart" in html


def test_panel_has_an_open_folder_button():
    html = _panel_html()

    assert 'id="logsOpenFolderBtn" onclick="openLogFolder()"' in html
    assert 'data-i18n="logsOpenFolder">打开日志文件夹<' in html
    assert "'/panel/api/logs/open-folder?name='" in html
    assert "method: 'POST'" in html
    # 结果（成功 / 失败原因）显示在日志框上面那一行
    assert 'id="logsFolderHint"' in html


def test_logs_page_keeps_its_older_habits():
    html = _panel_html()

    assert 'id="serverLogBox"' in html
    assert 'id="logsAutoRefresh"' in html
    assert 'id="logsRefreshBtn"' in html
    # 自动跟到底只在用户本来就在看末尾时发生
    assert "box.scrollHeight - box.scrollTop - box.clientHeight < 40" in html
    # 顶部那行「大小 · 更新于 …」还在，且切语言会重画
    assert 'id="logsMeta"' in html
    assert "renderServerLogMeta();" in html


def test_logs_page_texts_exist_in_both_languages():
    html = _panel_html()

    for key in ("navLogs", "logsDesc", "logsTitle", "logsKindServer", "logsKindRestart",
                "logsNoteServer", "logsNoteRestart", "logsRefresh", "logsAutoRefresh",
                "logsOpenFolder", "logsOpenFolderDone", "logsOpenFolderFailed",
                "logsEmptyServer", "logsEmptyRestart", "logsMeta", "logsLoadFailed"):
        assert html.count(key + ":") == 2, f"{key} 的中英文案要各有一份"
