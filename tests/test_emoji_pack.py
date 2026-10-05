"""文字式表情的资源包：清单、下载、进度与面板接口。

这里钉住几件事：
* 清单里的名字与顺序必须和前端 `frontend/src/lib/emojiAssets.js` 的名单**完全一致**
  —— 图片是按顺序配对的，错位就等于给每个表情配错图；
* 图片不进仓库，所以「本机有没有」只能看磁盘；下载要能跳过已有的（中断后再点接着下）；
* 单张失败不中断整批，失败数要如实报给面板；
* 「第一次进面板问不问」只看两件事：本机没装全 + 没问过（问过就写进 panel_config.json）。
"""
import asyncio
import json
import os
import re
import urllib.error

import pytest

from backend import control_panel as cp
from backend.panel import emoji_pack as ep
from common import emoji_pack, paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_LIST = os.path.join(REPO_ROOT, "frontend", "src", "lib", "emojiAssets.js")

# 一个最小的「合法 WebP」：够 detect_image 认出格式，内容无所谓
FAKE_WEBP = b"RIFF" + (1000).to_bytes(4, "little") + b"WEBP" + b"x" * 80
# 抖音有一张表情（[加功德]）官方直接给 PNG：也要认，见下面的格式测试
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 80


@pytest.fixture
def emoji_dir(tmp_path, monkeypatch):
    """下载目录与日志指到临时目录（清单用仓库里那份真的）。"""
    target = tmp_path / "emoji"
    monkeypatch.setattr(emoji_pack, "EMOJI_DIR", str(target))
    monkeypatch.setattr(emoji_pack, "LOG_PATH", str(tmp_path / "emoji_pack.log"))
    monkeypatch.setattr(emoji_pack, "_RETRY_WAIT", 0)     # 测试里不等重试间隔
    return target


@pytest.fixture
def panel_config(tmp_path, monkeypatch):
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


def frontend_names() -> list[str]:
    """从面板前端那份名单里取出名字（顺序＝配对顺序）。"""
    source = open(FRONTEND_LIST, encoding="utf-8").read()
    block = re.search(r"EMOJI_NAMES = new Set\(\((.*?)\)\.split\('、'\)\)", source, re.S).group(1)
    return "".join(re.findall(r"'([^']*)'", block)).split("、")


def small_manifest() -> list[dict]:
    """三张图的小清单：跑得快，又能覆盖「跳过 / 失败 / 停止」这些分支。"""
    return [
        {"name": "微笑", "url": "https://example.invalid/a.webp", "sha256": ""},
        {"name": "色", "url": "https://example.invalid/b.webp", "sha256": ""},
        {"name": "发呆", "url": "https://example.invalid/c.webp", "sha256": ""},
    ]


# ── 清单 ──────────────────────────────────────────────────────────────────

def test_manifest_matches_the_frontend_name_list():
    items = emoji_pack.load_manifest()
    assert len(items) == 214
    assert [item["name"] for item in items] == frontend_names()
    assert len({item["url"] for item in items}) == 214, "地址重复 = 抄写错位"


def test_manifest_urls_point_at_the_emoji_cdn():
    for item in emoji_pack.load_manifest():
        assert item["url"].startswith("https://p3-pc-sign.douyinpic.com/obj/tos-cn-")


def test_missing_manifest_is_reported_not_raised(emoji_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(emoji_pack, "MANIFEST_PATH", str(tmp_path / "nope.json"))
    assert emoji_pack.load_manifest() == []
    state = emoji_pack.status()
    assert state == {"total": 0, "installed": 0, "missing": 0,
                     "installed_all": False, "manifest_ok": False}


# ── 本机状态 ───────────────────────────────────────────────────────────────

def test_status_with_nothing_downloaded(emoji_dir):
    state = emoji_pack.status()
    assert state["manifest_ok"] is True
    assert state["total"] == 214
    assert state["installed"] == 0
    assert state["missing"] == 214
    assert state["installed_all"] is False


def test_tiny_file_does_not_count_as_installed(emoji_dir):
    """下了一半的残文件（或错误页）不能当成「已装好」，否则下次不会补下。"""
    emoji_dir.mkdir(parents=True, exist_ok=True)
    (emoji_dir / "微笑.webp").write_bytes(b"RIFF")
    assert emoji_pack.is_installed("微笑") is False


# ── 下载 ──────────────────────────────────────────────────────────────────

def test_download_writes_files_and_reports_progress(emoji_dir, monkeypatch):
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: FAKE_WEBP)
    seen = []

    result = emoji_pack.download(small_manifest(), on_progress=lambda r: seen.append(dict(r)))

    assert result["done"] == 3 and result["failed"] == 0
    assert (emoji_dir / "微笑.webp").read_bytes() == FAKE_WEBP
    # 每处理完一项回调一次：面板的「0/214、进度条、百分比」就是靠这个数数
    assert [item["done"] for item in seen] == [1, 2, 3]
    assert seen[-1]["current"] == "发呆"


def test_download_skips_existing_but_force_redownloads(emoji_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: calls.append(url) or FAKE_WEBP)

    first = emoji_pack.download(small_manifest())
    again = emoji_pack.download(small_manifest())
    forced = emoji_pack.download(small_manifest(), force=True)

    assert first["done"] == 3 and again["skipped"] == 3
    assert forced["skipped"] == 0
    assert len(calls) == 6, "第二次只该跳过，不该再请求"


def test_one_failure_does_not_stop_the_batch(emoji_dir, monkeypatch, tmp_path):
    def flaky(url):
        if url.endswith("b.webp"):
            raise OSError("连接被重置")
        return FAKE_WEBP

    monkeypatch.setattr(emoji_pack, "_fetch", flaky)
    result = emoji_pack.download(small_manifest())

    assert result["done"] == 2 and result["failed"] == 1
    assert result["failed_names"] == ["色"]
    assert "色" in (tmp_path / "emoji_pack.log").read_text(encoding="utf-8")


def test_non_image_payload_is_a_failure(emoji_dir, monkeypatch):
    """403 的错误页也是 200 回来的：不是图片就当失败，别把 HTML 存成图片。"""
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: b"<html>403</html>" + b"x" * 80)
    result = emoji_pack.download(small_manifest())
    assert result["failed"] == 3 and result["done"] == 0
    assert not (emoji_dir / "微笑.webp").exists()


# ── 图片格式（那次「总有一张下不下来」的修复） ──────────────────────────────

def test_png_sticker_is_stored_with_its_real_extension(emoji_dir, monkeypatch):
    """[加功德] 那张官方给的是 PNG：要按 .png 存下来，不能再每次点都算失败。"""
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: FAKE_PNG)
    items = [{"name": "加功德", "url": "https://example.invalid/a", "sha256": ""}]

    result = emoji_pack.download(items)

    assert result["failed"] == 0 and result["done"] == 1
    assert (emoji_dir / "加功德.png").read_bytes() == FAKE_PNG
    assert not (emoji_dir / "加功德.webp").exists(), "别留一张名字说是 webp 的 png"

    # 下过的 PNG 算「本机已有」：再点下载不该重下（以前这里会一遍遍失败）
    assert emoji_pack.is_installed("加功德") is True
    assert emoji_pack.status(items)["installed"] == 1
    again = emoji_pack.download(items)
    assert again["skipped"] == 1 and again["done"] == 1


@pytest.mark.parametrize("payload,ext", [
    (FAKE_WEBP, ".webp"),
    (FAKE_PNG, ".png"),
    (b"\xff\xd8\xff" + b"x" * 80, ".jpg"),
    (b"GIF89a" + b"x" * 80, ".gif"),
])
def test_detect_image_recognizes_the_common_formats(payload, ext):
    assert emoji_pack.detect_image(payload) == ext


def test_detect_image_rejects_error_pages_and_short_files():
    with pytest.raises(ValueError):
        emoji_pack.detect_image(b"<html>403</html>" + b"x" * 80)
    with pytest.raises(ValueError):
        emoji_pack.detect_image(FAKE_PNG[:20])


def test_download_can_be_stopped(emoji_dir, monkeypatch):
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: FAKE_WEBP)
    result = emoji_pack.download(small_manifest(), should_stop=lambda: True)
    assert result["stopped"] is True and result["done"] == 0
    assert result["total"] == 3


def test_hash_mismatch_is_counted_but_keeps_the_file(emoji_dir, monkeypatch):
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: FAKE_WEBP)
    items = [{"name": "微笑", "url": "https://example.invalid/a.webp", "sha256": "0" * 64}]

    result = emoji_pack.download(items)

    assert result["mismatched"] == 1 and result["failed"] == 0
    assert (emoji_dir / "微笑.webp").exists(), "自检对不上只是记一笔，不影响使用"


def test_signed_url_falls_back_to_the_plain_one(emoji_dir, monkeypatch):
    """签名过期（403）时退一步用不带参数的地址。"""
    tried = []

    def fake(url):
        tried.append(url)
        if "?" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return FAKE_WEBP

    monkeypatch.setattr(emoji_pack, "_fetch", fake)
    result = emoji_pack.download([{"name": "微笑", "url": "https://example.invalid/a.webp?sig=1"}])

    assert result["done"] == 1
    assert tried == ["https://example.invalid/a.webp?sig=1", "https://example.invalid/a.webp"]


# ── 面板接口 ───────────────────────────────────────────────────────────────

def test_status_prompts_when_nothing_is_installed(emoji_dir, panel_config):
    payload = asyncio.run(cp.emoji_pack_status())
    assert payload["prompt"] is True
    assert payload["installed_all"] is False
    assert payload["total"] == 214
    assert payload["percent"] == 0


def test_skip_records_the_prompt_in_the_config(emoji_dir, panel_config):
    assert asyncio.run(cp.emoji_pack_skip())["prompted"] is True
    assert json.loads(panel_config.read_text(encoding="utf-8"))["emoji_pack_prompted"] is True
    assert asyncio.run(cp.emoji_pack_status())["prompt"] is False, "问过一次就不该再自动弹"


def test_skip_keeps_other_config_values(panel_config, emoji_dir):
    panel_config.write_text(json.dumps({"password_hash": "H", "developer_mode": True}),
                            encoding="utf-8")
    asyncio.run(cp.emoji_pack_skip())
    saved = json.loads(panel_config.read_text(encoding="utf-8"))
    assert saved["password_hash"] == "H" and saved["developer_mode"] is True


def test_download_endpoint_runs_the_job_and_reaches_installed(emoji_dir, panel_config, monkeypatch):
    monkeypatch.setattr(emoji_pack, "load_manifest", small_manifest)
    monkeypatch.setattr(emoji_pack, "_fetch", lambda url: FAKE_WEBP)

    async def scenario():
        started = await cp.emoji_pack_download()
        assert started["status"] == "running"
        assert started["total"] == 3
        await ep._task                       # 等这一轮跑完
        return await cp.emoji_pack_status()

    payload = asyncio.run(scenario())

    assert payload["status"] == "done"
    assert payload["installed_all"] is True
    assert payload["installed"] == 3 and payload["missing"] == 0
    assert payload["percent"] == 100
    assert payload["prompt"] is False, "装全了就不该再问"


def test_stop_sets_the_flag_the_downloader_polls(emoji_dir, panel_config):
    """停下不是杀线程，只是把标志立起来：下载循环下一项之前会看到它。"""
    payload = asyncio.run(cp.emoji_pack_stop())
    assert ep._stop is True
    assert {"status", "done", "total", "installed", "percent"} <= set(payload)


def test_status_never_leaks_file_paths(emoji_dir, panel_config):
    """面板只需要数字与状态：别把本机路径顺手发出去。"""
    payload = json.dumps(asyncio.run(cp.emoji_pack_status()), ensure_ascii=False)
    assert str(emoji_dir) not in payload
