"""Windows「下载标记」（Mark-of-the-Web / ``Zone.Identifier``）清理。

从浏览器下载的 ZIP 解出来的文件会带一条附加数据流，双击 ``.bat`` 时 Windows 会先弹
「无法验证发布者。你确定要运行此软件吗？」。这个框在**我们的代码跑起来之前**就弹出来，
所以脚本拦不住第一次；能做的是启动之后把标记清掉，让以后每次双击都不再问。

要守住的东西：

* ``common/download_mark.py`` 负责清理（这个文件测的就是它）；
* 后端启动时清一遍整个项目 —— 双击 bat / ``start.ps1`` / ``start.sh`` / 面板里起的服务，
  最后都会进后端，放这里一次覆盖所有入口；
* ``start.ps1`` 里顺手清项目根目录那一层：会被双击的启动脚本都在那儿，清得越早越少弹一次；
* ``启动服务（双击）.bat`` 里那次**隐藏**启动改走 ``cmd.exe`` —— 直接 ``Start-Process``
  会经过 Windows 的「关联打开」，带标记时又会弹一次，而那一次弹在隐藏窗口里：不点它，
  服务根本起不来（用户反馈的「明明点了运行还是卡住」多半就是它）。

非 Windows 系统没有附加数据流这回事，相关函数一律空操作。
"""
import os
import pathlib

import pytest

from common import download_mark

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BAT = REPO_ROOT / "启动服务（双击）.bat"
START_PS1 = REPO_ROOT / "start.ps1"
MAIN_PY = REPO_ROOT / "backend" / "main.py"

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="Zone.Identifier 是 NTFS 附加数据流，只有 Windows 才有"
)


def _mark(path: pathlib.Path) -> None:
    """按 Windows 的方式给文件盖上「来自 Internet」的标记。"""
    with open(f"{path}:Zone.Identifier", "wb") as handle:
        handle.write(b"[ZoneTransfer]\r\nZoneId=3\r\n")


@windows_only
def test_strip_download_mark_removes_the_stream(tmp_path):
    target = tmp_path / "启动服务.bat"
    target.write_text("@echo off\n", encoding="utf-8")

    assert not download_mark.has_download_mark(target)

    _mark(target)
    assert download_mark.has_download_mark(target)

    assert download_mark.strip_download_mark(target) is True
    assert not download_mark.has_download_mark(target)
    # 已经清干净的文件再清一次：不是错误，只是「本来就没有」
    assert download_mark.strip_download_mark(target) is False


@windows_only
def test_project_walk_clears_risky_files_and_skips_vendor_dirs(tmp_path):
    (tmp_path / "启动服务.bat").write_text("@echo off\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "helper.ps1").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "venv" / "Scripts" / "python.exe").write_bytes(b"MZ")

    for rel in ("启动服务.bat", "README.md", "tools/helper.ps1", "venv/Scripts/python.exe"):
        _mark(tmp_path / rel)

    stripped = download_mark.strip_project_download_marks(tmp_path)

    assert sorted(path.name for path in stripped) == ["helper.ps1", "启动服务.bat"]
    # 不会弹窗的扩展名不用动（留着也不碍事）
    assert download_mark.has_download_mark(tmp_path / "README.md")
    # venv 里上万个文件，每次启动都去扫不值得
    assert download_mark.has_download_mark(tmp_path / "venv" / "Scripts" / "python.exe")
    # 清完再跑一遍就没有可清的了（幂等）
    assert download_mark.strip_project_download_marks(tmp_path) == []


def test_every_launch_path_clears_the_mark():
    """清理得挂在「每次启动都会走到」的地方，否则标记会一直留着、框会一直弹。"""
    assert "strip_project_download_marks" in MAIN_PY.read_text(encoding="utf-8")

    start_src = START_PS1.read_text(encoding="utf-8")
    assert "Zone.Identifier" in start_src
    assert "Remove-Item -LiteralPath $_.FullName -Stream Zone.Identifier" in start_src


def test_the_hidden_relaunch_avoids_the_windows_shell():
    """隐藏那次启动要走 cmd.exe，别让 Windows 的「关联打开」再弹一次框。"""
    bat = BAT.read_bytes().decode("gbk")

    assert "-FilePath $env:ComSpec" in bat
    assert "-ArgumentList '/c','%~nx0 --serve'" in bat
    assert "-FilePath '%~f0'" not in bat, "直接 Start-Process 这个 bat 会再弹一次安全警告"
