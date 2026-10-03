"""两个目录的分工：``data/`` 只放聊天记录，``config/`` 放其余一切。

2026-10-04 之前所有东西都堆在 ``data/`` 里（数据库、媒体、面板设置、凭据、登录态、
日志混在一起），备份时没法一眼分清「哪些删了会心疼」。现在按一条界线分开：

* ``data/``   —— 聊天记录：chat.db、下载的媒体、导出的聊天文件、数据库备份；
* ``config/`` —— 配置与运行状态：面板设置、私有仓库凭据、登录态、日志、更新状态、调试产物。

这个文件把这条界线钉住（免得以后又有人顺手往 data/ 里塞配置），并把老布局的搬迁
（``migrate_legacy_layout()``）一起测掉 —— 老用户升级上来全靠它。

注意：conftest 里把 ``paths.migrate_legacy_layout`` 换成了空操作（测试不该搬真实项目），
所以这里用的是那份真正的实现 ``REAL_MIGRATE_LEGACY_LAYOUT``。
"""
import os

from common import paths
from tests.conftest import REAL_MIGRATE_LEGACY_LAYOUT


def _under(child: str, parent: str) -> bool:
    """``child`` 是不是在 ``parent`` 里面（同盘符下的纯字符串判断，测试足够）。"""
    return os.path.commonpath([os.path.abspath(child), os.path.abspath(parent)]) == \
        os.path.abspath(parent)


# ── 两条目录各管一摊 ───────────────────────────────────────────────────────

def test_the_two_directories_are_siblings_under_the_project_root():
    assert paths.DATA_DIR == os.path.join(paths.REPO_ROOT, "data")
    assert paths.CONFIG_DIR == os.path.join(paths.REPO_ROOT, "config")
    assert not _under(paths.CONFIG_DIR, paths.DATA_DIR)
    assert not _under(paths.DATA_DIR, paths.CONFIG_DIR)


def test_chat_records_live_under_data():
    """数据库、媒体、导出与数据库备份都是「聊天记录」，留在 data/。"""
    for name in (
            "DB_PATH", "CONVERSATIONS_LIST",
            "BACKFILL_DAILY_SHARE_UNDO", "BACKFILL_MODIFY_UNDO",
            "MEDIA_DIR", "IMAGES_DIR", "EMOJI_DIR", "VOICE_DIR",
            "AVATARS_DIR", "VIDEOS_DIR",
            "TRANSCODE_DIR", "TRANSCODE_VIDEOS_DIR",
            "VIDEOS_FIXED_DIR", "VIDEOS_ORIG_DIR",
    ):
        value = getattr(paths, name)
        assert _under(value, paths.DATA_DIR), f"{name} 应该在 data/ 里：{value}"


def test_settings_logs_and_state_live_under_config():
    """设置、凭据、登录态、日志、跨进程状态、调试产物都进 config/。"""
    for name in (
            "CONFIG_PATH", "TOKEN_PATH", "BROWSER_PROFILE",
            "LOG_DIR", "SCRAPE_LOG", "VOICE_TRANSCRIPTION_LOG", "DISCOVER_LOG",
            "SERVER_LOG", "RESTART_LOG", "UPDATE_LOG", "LAUNCHER_LOG",
            "ENV_CHECK_LOG", "EMOJI_PACK_LOG",
            "ENV_REPORT", "LAUNCHER_STATE", "UPDATE_DONE_PATH",
            "DEBUG_DIR", "API_DUMP_DIR",
    ):
        value = getattr(paths, name)
        assert _under(value, paths.CONFIG_DIR), f"{name} 应该在 config/ 里：{value}"
        assert not _under(value, paths.DATA_DIR), f"{name} 不该在 data/ 里：{value}"

    # 日志集中在一个子目录里（面板「打开日志文件夹」打开的就是它）
    assert _under(paths.SERVER_LOG, paths.LOG_DIR)
    assert _under(paths.UPDATE_LOG, paths.LOG_DIR)


def test_repo_files_are_not_mistaken_for_runtime_state():
    """仓库里的东西（前端产物、表情清单）不属于上面两类，别混进来。"""
    assert not _under(paths.FRONTEND_DIST, paths.DATA_DIR)
    assert not _under(paths.FRONTEND_DIST, paths.CONFIG_DIR)
    assert not _under(paths.EMOJI_MANIFEST, paths.DATA_DIR)
    assert not _under(paths.EMOJI_MANIFEST, paths.CONFIG_DIR)


# ── 老布局搬迁 ─────────────────────────────────────────────────────────────

def _fake_layout(tmp_path, monkeypatch):
    """把两个目录指到临时目录（迁移表按调用时的目录算，所以这里改了就够）。"""
    data = tmp_path / "data"
    config = tmp_path / "config"
    data.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", str(data))
    monkeypatch.setattr(paths, "CONFIG_DIR", str(config))
    return data, config


def test_every_legacy_entry_lands_under_config():
    """搬迁表里不能有指到 config/ 外面的目标（写错了就等于把文件搬丢）。"""
    for old_name, target in paths.legacy_layout().items():
        assert _under(target, paths.CONFIG_DIR), f"{old_name} 的目标指到了 config/ 外面：{target}"


def test_legacy_layout_is_migrated_and_chat_records_stay(tmp_path, monkeypatch):
    data, config = _fake_layout(tmp_path, monkeypatch)
    (data / "panel_config.json").write_text("{}", encoding="utf-8")
    (data / "server.log").write_text("服务日志\n", encoding="utf-8")
    (data / "browser_profile").mkdir()
    (data / "browser_profile" / "Cookies").write_text("c", encoding="utf-8")
    # 聊天记录：一行都不许动
    (data / "chat.db").write_text("db", encoding="utf-8")
    (data / "media").mkdir()

    moved = REAL_MIGRATE_LEGACY_LAYOUT()

    assert set(moved) == {"panel_config.json", "server.log", "browser_profile"}
    assert (config / "panel_config.json").read_text(encoding="utf-8") == "{}"
    assert (config / "logs" / "server.log").read_text(encoding="utf-8") == "服务日志\n"
    assert (config / "browser_profile" / "Cookies").read_text(encoding="utf-8") == "c"
    assert not (data / "panel_config.json").exists(), "搬完旧位置不该还留着"
    assert (data / "chat.db").is_file(), "数据库是聊天记录，必须原地不动"
    assert (data / "media").is_dir(), "媒体目录也是聊天记录，不动"
    assert not (config / "chat.db").exists()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """再跑一次什么都不做（服务每次启动都会调它）。"""
    data, config = _fake_layout(tmp_path, monkeypatch)
    (data / "restart.log").write_text("r", encoding="utf-8")

    assert REAL_MIGRATE_LEGACY_LAYOUT() == ["restart.log"]
    assert (config / "logs" / "restart.log").is_file()
    assert REAL_MIGRATE_LEGACY_LAYOUT() == []


def test_migration_never_overwrites_the_new_file(tmp_path, monkeypatch):
    """新旧两处都有同一个文件时：新的那份不动，旧的那份留在原地（不删、不覆盖）。"""
    data, config = _fake_layout(tmp_path, monkeypatch)
    (data / "panel_config.json").write_text("旧", encoding="utf-8")
    (config).mkdir()
    (config / "panel_config.json").write_text("新", encoding="utf-8")

    assert REAL_MIGRATE_LEGACY_LAYOUT() == []
    assert (config / "panel_config.json").read_text(encoding="utf-8") == "新"
    assert (data / "panel_config.json").read_text(encoding="utf-8") == "旧"


def test_migration_survives_a_file_it_cannot_move(tmp_path, monkeypatch, capsys):
    """单个文件搬不动只是少搬一个：剩下的照搬，也不抛异常。"""
    data, config = _fake_layout(tmp_path, monkeypatch)
    (data / "github_token").write_text("t", encoding="utf-8")
    (data / "env-report.js").write_text("r", encoding="utf-8")

    real_move = paths.shutil.move

    def flaky_move(source, target):
        if os.path.basename(source) == "github_token":
            raise OSError("文件被占用")
        return real_move(source, target)

    monkeypatch.setattr(paths.shutil, "move", flaky_move)

    assert REAL_MIGRATE_LEGACY_LAYOUT() == ["env-report.js"]
    assert (data / "github_token").is_file(), "搬不动的那个留在原位，下次启动再试"
    assert (config / "env-report.js").is_file()
    assert "github_token" in capsys.readouterr().out
