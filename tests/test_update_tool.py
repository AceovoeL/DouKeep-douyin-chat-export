"""Tests for ``tools/update.py`` 的「没有 git 也能更新」那条路。

下载那一步单独打桩（用例不联网），解压与覆盖都在 tmp 目录里做。这里钉住三件最容易
出错的事：代码包最外层那层目录要被剥掉、写文件不能越出目标目录、覆盖只碰仓库里的
代码文件（data/ 里的本地数据必须原样留着）。
"""
import io
import os
import sys
import zipfile

import pytest

from tools import update as updater

#: GitHub zipball 最外层目录的形状：仓库名 + commit 短 sha
ARCHIVE_ROOT = "AceovoeL-DouKeep-douyin-chat-export-abc1234"


def make_archive(entries: dict, root: str = ARCHIVE_ROOT) -> bytes:
    """造一个和 GitHub zipball 同形状的代码包。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(f"{root}/{name}" if root else name, payload)
    return buffer.getvalue()


def infos_of(data: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.infolist()


def fake_repo(tmp_path, *, with_git: bool = False):
    """一个最小项目目录：一份代码，外加一份不该被动到的本地数据。"""
    repo = tmp_path / "repo"
    (repo / "common").mkdir(parents=True)
    (repo / "common" / "version.py").write_text('VERSION = "1.0.0"\n', encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "chat.db").write_bytes(b"keep me")
    if with_git:
        (repo / ".git").mkdir()
    return repo


def test_archive_prefix_is_the_outer_folder():
    assert updater.archive_prefix(infos_of(make_archive({"a.txt": b"a"}))) == ARCHIVE_ROOT + "/"


def test_archive_without_outer_folder_has_empty_prefix():
    data = make_archive({"a.txt": b"a", "b/c.txt": b"c"}, root="")
    assert updater.archive_prefix(infos_of(data)) == ""


def test_extract_strips_outer_folder_and_keeps_tree(tmp_path):
    data = make_archive({"common/version.py": b'VERSION = "2.0.0"\n', "start.sh": b"#!/bin/sh\n"})
    staging = tmp_path / "staging"
    staging.mkdir()

    assert updater.extract_archive(data, str(staging)) == 2

    assert (staging / "common" / "version.py").read_bytes() == b'VERSION = "2.0.0"\n'
    assert (staging / "start.sh").read_bytes() == b"#!/bin/sh\n"
    assert not (staging / ARCHIVE_ROOT).exists(), "外层目录必须被剥掉"


def test_extract_rejects_paths_outside_destination(tmp_path):
    data = make_archive({"../evil.txt": b"x"})

    with pytest.raises(ValueError):
        updater.extract_archive(data, str(tmp_path))

    assert not (tmp_path.parent / "evil.txt").exists(), "越界文件一个都不许写出去"


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有 Unix 权限位")
def test_extract_restores_exec_bit(tmp_path):
    info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/start.sh")
    info.external_attr = 0o100755 << 16            # 高 16 位是 Unix mode
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, "#!/bin/sh\n")

    updater.extract_archive(buffer.getvalue(), str(tmp_path))

    assert os.access(tmp_path / "start.sh", os.X_OK)


def test_merge_covers_code_but_leaves_data_alone(tmp_path):
    data = make_archive({"common/version.py": b'VERSION = "2.0.0"\n', "README.md": b"new\n"})
    staging = tmp_path / "staging"
    staging.mkdir()
    updater.extract_archive(data, str(staging))
    repo = fake_repo(tmp_path)

    assert updater.merge_tree(str(staging), str(repo)) == 2
    assert (repo / "common" / "version.py").read_bytes() == b'VERSION = "2.0.0"\n'
    assert (repo / "README.md").read_bytes() == b"new\n"
    assert (repo / "data" / "chat.db").read_bytes() == b"keep me", "本地数据不能被覆盖"


def test_version_on_disk_reads_the_freshly_written_file(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))

    assert updater.version_on_disk() == "1.0.0"

    (repo / "common" / "version.py").write_text('VERSION = "1.4.2"\n', encoding="utf-8")
    assert updater.version_on_disk() == "1.4.2"


def test_version_on_disk_falls_back_when_file_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "REPO_ROOT", str(tmp_path))

    assert updater.version_on_disk() == updater.version.VERSION


def test_archive_without_token_downloads_anonymously(tmp_path, monkeypatch):
    """没填凭据也照常更新：本项目的公开仓库匿名就能下，不该先拦下来要 Token。"""
    repo = fake_repo(tmp_path)
    data = make_archive({"common/version.py": b'VERSION = "1.2.3"\n'})
    seen: list = []
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater.github_auth, "load_for_use", lambda: "")
    monkeypatch.setattr(updater.github_auth, "load", lambda: "")
    monkeypatch.setattr(updater, "download_archive",
                        lambda slug, branch, token: seen.append(token) or data)
    monkeypatch.setattr(updater, "install_and_build", lambda: 0)

    assert updater.main([]) == 0

    assert seen == [""], "没有凭据就用空 Token 匿名下载"
    assert (repo / "common" / "version.py").read_bytes() == b'VERSION = "1.2.3"\n'


def test_archive_hints_at_the_token_when_the_download_needs_one(tmp_path, monkeypatch, capsys):
    """没填过凭据、匿名又下不到（私有仓库）时，提示去哪儿填只读 Token。"""
    repo = fake_repo(tmp_path)
    tried: list = []
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater.github_auth, "load_for_use", lambda: "")
    monkeypatch.setattr(updater.github_auth, "load", lambda: "")

    def not_found(slug, branch, token):
        tried.append(token)
        raise updater.urllib.error.HTTPError(slug, 404, "Not Found", None, None)

    monkeypatch.setattr(updater, "download_archive", not_found)

    assert updater.main([]) == 1

    out = capsys.readouterr().out
    assert tried == [""], "先匿名试一次"
    assert "404" in out
    assert "只读 Token" in out
    assert "没有 .git" in out


def test_archive_points_at_developer_mode_when_a_saved_token_is_not_used(
        tmp_path, monkeypatch, capsys):
    """存过 Token、但开发者模式关着：这条路不用它，下不到时要指向「关于 → 开发者模式」。

    不能让人以为 Token 丢了、又去粘一遍。
    """
    repo = fake_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater.github_auth, "load_for_use", lambda: "")
    monkeypatch.setattr(updater.github_auth, "load", lambda: "tok")

    def not_found(slug, branch, token):
        raise updater.urllib.error.HTTPError(slug, 404, "Not Found", None, None)

    monkeypatch.setattr(updater, "download_archive", not_found)

    assert updater.main([]) == 1

    out = capsys.readouterr().out
    assert "开发者模式" in out
    assert "没有 .git" in out


def test_archive_retries_without_the_token_when_it_is_rejected(tmp_path, monkeypatch):
    """凭据过期/被撤销（401）时不该整个更新失败：退回匿名再下一次。"""
    repo = fake_repo(tmp_path)
    data = make_archive({"common/version.py": b'VERSION = "1.2.3"\n'})
    seen: list = []
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater.github_auth, "load_for_use", lambda: "ghp_" + "b" * 36)
    monkeypatch.setattr(updater, "install_and_build", lambda: 0)

    def download(slug, branch, token):
        seen.append(token)
        if token:
            raise updater.urllib.error.HTTPError(slug, 401, "Unauthorized", None, None)
        return data

    monkeypatch.setattr(updater, "download_archive", download)

    assert updater.main([]) == 0

    assert len(seen) == 2 and seen[0] != "" and seen[1] == ""
    assert (repo / "common" / "version.py").read_bytes() == b'VERSION = "1.2.3"\n'


def test_archive_mode_replaces_code_and_keeps_data(tmp_path, monkeypatch):
    """没有 .git 时点「立即更新」：下载代码包 → 覆盖代码 → 装依赖/构建。"""
    repo = fake_repo(tmp_path)
    data = make_archive({"common/version.py": b'VERSION = "1.2.3"\n', "README.md": b"new\n"})
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater.github_auth, "load_for_use", lambda: "tok")
    monkeypatch.setattr(updater, "download_archive", lambda slug, branch, token: data)
    monkeypatch.setattr(updater, "install_and_build", lambda: 0)

    assert updater.main([]) == 0

    assert (repo / "common" / "version.py").read_bytes() == b'VERSION = "1.2.3"\n'
    assert (repo / "README.md").read_bytes() == b"new\n"
    assert (repo / "data" / "chat.db").read_bytes() == b"keep me"


def test_archive_download_failure_is_reported(tmp_path, monkeypatch, capsys):
    repo = fake_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater.github_auth, "load_for_use", lambda: "tok")

    def boom(slug, branch, token):
        raise updater.urllib.error.HTTPError(slug, 404, "Not Found", None, None)

    monkeypatch.setattr(updater, "download_archive", boom)

    assert updater.main([]) == 1
    assert "读取权限" in capsys.readouterr().out


def test_git_worktree_uses_pull(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, with_git=True)
    seen: list[bool] = []
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater, "git_available", lambda: True)
    monkeypatch.setattr(updater, "update_code_via_git",
                        lambda allow_dirty=False: (seen.append(allow_dirty), 0)[1])
    monkeypatch.setattr(updater, "update_code_via_archive", lambda: pytest.fail("不该走下载"))
    monkeypatch.setattr(updater, "install_and_build", lambda: 0)

    assert updater.main([]) == 0
    assert seen == [False]


def test_git_dir_without_git_command_falls_back_to_archive(tmp_path, monkeypatch):
    """有 .git 但这台机器没装 git：不能卡在 FileNotFoundError 上，得走下载。"""
    repo = fake_repo(tmp_path, with_git=True)
    calls: list[str] = []
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater, "git_available", lambda: False)
    monkeypatch.setattr(updater, "update_code_via_git", lambda allow_dirty=False: pytest.fail("没有 git 命令"))
    monkeypatch.setattr(updater, "update_code_via_archive", lambda: (calls.append("archive"), 0)[1])
    monkeypatch.setattr(updater, "install_and_build", lambda: 0)

    assert updater.main([]) == 0
    assert calls == ["archive"]


def test_archive_flag_overrides_git(tmp_path, monkeypatch):
    """--archive 是分叉时的手动逃生口：有 .git 也照样用下载覆盖。"""
    repo = fake_repo(tmp_path, with_git=True)
    calls: list[str] = []
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater, "git_available", lambda: True)
    monkeypatch.setattr(updater, "update_code_via_git", lambda allow_dirty=False: pytest.fail("不该走 git"))
    monkeypatch.setattr(updater, "update_code_via_archive", lambda: (calls.append("archive"), 0)[1])
    monkeypatch.setattr(updater, "install_and_build", lambda: 0)

    assert updater.main(["--archive"]) == 0
    assert calls == ["archive"]


def test_update_code_failure_skips_install_and_build(tmp_path, monkeypatch):
    """代码没换成功就不该去装依赖、构建前端（免得留下半新半旧的状态）。"""
    repo = fake_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater, "update_code_via_archive", lambda: updater.EXIT_DIRTY)
    monkeypatch.setattr(updater, "install_and_build", lambda: pytest.fail("不该继续构建"))

    assert updater.main([]) == updater.EXIT_DIRTY


def test_archive_url_points_at_the_api_zipball():
    url = updater.archive_url("AceovoeL/DouKeep-douyin-chat-export", "main")
    assert url == "https://api.github.com/repos/AceovoeL/DouKeep-douyin-chat-export/zipball/main"


# ── 前端构建（「一键更新」和面板「重启服务」共用这一段） ───────────────────
#
# 这段以前长在 install_and_build 里，现在被拎出来共用：面板的「重启服务」是「先停服务、
# 再构建」，npm 要是卡住不返回，服务就永远回不来，所以每条命令都带超时。

def test_run_reports_a_timeout_as_a_failure():
    """超时就当失败返回（不能让调用方无限等下去）。"""
    code = updater.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)

    assert code == 1


def test_build_frontend_installs_missing_deps_then_builds(monkeypatch):
    commands: list[list[str]] = []
    state = {"vite": False}
    monkeypatch.setattr(updater, "vite_available", lambda: state["vite"])

    def fake_run(args, **kwargs):
        commands.append(list(args))
        if "install" in args:
            state["vite"] = True          # 装完就找得到 vite 了
        assert kwargs.get("timeout") == updater.FRONTEND_BUILD_TIMEOUT
        return 0

    monkeypatch.setattr(updater, "run", fake_run)

    assert updater.build_frontend() == 0
    assert "install" in commands[0]
    assert commands[-1][1:] == ["run", "build"], "装完依赖必须再构建一次"


def test_build_frontend_skips_the_install_when_vite_is_there(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(updater, "vite_available", lambda: True)
    monkeypatch.setattr(updater, "run", lambda args, **kwargs: commands.append(list(args)) or 0)

    assert updater.build_frontend() == 0
    assert commands == [[*updater.npm_command(), "run", "build"]]


def test_build_frontend_reports_a_failed_build(monkeypatch):
    monkeypatch.setattr(updater, "vite_available", lambda: True)
    monkeypatch.setattr(updater, "run", lambda args, **kwargs: 1)

    assert updater.build_frontend() != 0


def test_install_and_build_still_builds_the_frontend(monkeypatch):
    """拎出来之后 install_and_build 仍然会走到那一步（更新完前端必须是新的）。"""
    seen: list = []
    monkeypatch.setattr(updater, "run", lambda args, **kwargs: seen.append(list(args)) or 0)
    monkeypatch.setattr(updater, "build_frontend", lambda: seen.append("build") or 0)

    assert updater.install_and_build() == 0
    assert seen[-1] == "build"


def test_install_and_build_stops_when_the_frontend_build_fails(monkeypatch):
    monkeypatch.setattr(updater, "run", lambda args, **kwargs: 0)
    monkeypatch.setattr(updater, "build_frontend", lambda: 1)

    assert updater.install_and_build() == 1


# ── 下载代码包时的进度显示 ────────────────────────────────────────────────
#
# 这一步在国内网络上可能耗掉半分钟以上，日志里一直没动静就会被当成卡死，所以边下边报。
# 但也不能每读一块就刷一行（日志框只看最后几十行，报错会被刷没）。

class FakeResponse:
    """假的下载响应：按块吐数据，可以带/不带 Content-Length。"""

    def __init__(self, payload: bytes, chunk: int = 1024, headers: dict | None = None):
        self._payload = payload
        self._chunk = chunk
        self._pos = 0
        self.headers = {"Content-Length": str(len(payload))} if headers is None else headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        step = self._chunk if size is None or size < 0 else min(size, self._chunk)
        piece = self._payload[self._pos:self._pos + step]
        self._pos += len(piece)
        return piece


class FakeClock:
    """假时钟：每问一次就往前走固定的几秒（用来验证「总大小未知」时按时间报进度）。"""

    def __init__(self, step: float = 10.0):
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def sleep(self, seconds: float) -> None:      # pragma: no cover - update.py 不用它
        pass

    def strftime(self, fmt: str) -> str:          # pragma: no cover - 同上
        return "2026-10-02 12:00:00"


def test_format_size_reads_like_a_person_wrote_it():
    assert updater.format_size(0) == "0 KB"
    assert updater.format_size(1024) == "1 KB"
    assert updater.format_size(1536) == "2 KB"
    assert updater.format_size(int(3.5 * 1024 * 1024)) == "3.5 MB"


def test_response_length_tolerates_missing_or_broken_headers():
    assert updater.response_length(FakeResponse(b"x", headers={"Content-Length": "123"})) == 123
    assert updater.response_length(FakeResponse(b"x", headers={})) == 0
    assert updater.response_length(FakeResponse(b"x", headers={"Content-Length": "abc"})) == 0


def test_download_headers_only_carry_a_token_when_there_is_one(monkeypatch):
    """公开仓库匿名下载：没有凭据就不要带 Authorization 头（带着反而会被拒）。"""
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return FakeResponse(b"x")

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    updater.download_archive("owner/repo", "main", "")
    assert "authorization" not in seen["headers"]

    updater.download_archive("owner/repo", "main", "tok")
    assert seen["headers"]["authorization"] == "Bearer tok"


def test_download_reports_percent_progress_and_keeps_the_bytes(monkeypatch, capsys):
    """总大小已知时按百分比报：一共 10 块、每块 10%，不该打 10 行进度的上限之上。"""
    payload = bytes(range(256)) * 40          # 10240 字节
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(payload, chunk=1024))

    data = updater.download_archive("owner/repo", "main", "tok")

    assert data == payload, "报了进度也不能把下载内容弄坏"
    out = capsys.readouterr().out
    assert "正在下载代码包（共 10 KB）" in out
    progress = [line for line in out.splitlines() if "已下载" in line]
    assert 2 <= len(progress) <= 12, progress
    assert any("100%" in line for line in progress), progress
    assert "下载完成" in out and "KB/s" in out


def test_download_reports_by_time_when_the_size_is_unknown(monkeypatch, capsys):
    """服务端没给 Content-Length 时也要有动静：按时间（每几秒）报一次。"""
    payload = b"x" * 4096
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(payload, chunk=1024, headers={}))
    monkeypatch.setattr(updater, "time", FakeClock(step=10.0))

    data = updater.download_archive("owner/repo", "main", "tok")

    assert data == payload
    out = capsys.readouterr().out
    assert "总大小未知" in out
    assert out.count("已下载 ") >= 2, out
    assert "下载完成" in out


def test_download_progress_finishes_on_a_single_chunk(monkeypatch, capsys):
    """小文件一次读完也要有头有尾：开始一行、完成一行，不能报错。"""
    payload = b"tiny"
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(payload, chunk=1024))

    assert updater.download_archive("owner/repo", "main", "tok") == payload

    out = capsys.readouterr().out
    assert "正在下载代码包" in out
    assert "下载完成" in out

