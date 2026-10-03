"""外观设置（字体 / 配色 / 尺寸）：规范化规则与接口行为。

这些值最终会变成浏览器里的 CSS，所以重点验两件事：
1. 乱七八糟的输入（非法颜色、越界数字、奇怪字体名）都被收拾干净；
2. 写入外观设置不会碰到密码、通知等其它配置。
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from common import appearance
from common import config as panel_config
from common import paths


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(path))
    return path


def test_defaults_are_complete_and_roundtrip():
    defaults = appearance.default_appearance()
    assert defaults == appearance.normalize_appearance(defaults)
    # 缺字段时补默认值
    assert appearance.normalize_appearance({}) == defaults
    assert appearance.normalize_appearance(None) == defaults


def test_unknown_keys_and_wrong_types_are_dropped():
    normalized = appearance.normalize_appearance({
        "nope": 1,
        "panel": {"theme": "dark", "colors": {"accent": "#123456", "bogus": "#fff"}},
        "viewer": "not a dict",
    })
    assert "nope" not in normalized
    assert normalized["panel"]["colors"]["accent"] == "#123456"
    assert "bogus" not in normalized["panel"]["colors"]
    # 整个 viewer 写坏了也不会炸，直接回默认
    assert normalized["viewer"] == appearance.default_appearance()["viewer"]


def test_invalid_colors_fall_back_and_are_reported():
    warnings: list[str] = []
    normalized = appearance.normalize_appearance(
        {"panel": {"colors": {"bg": "red", "text": "#ABCDEF"}}}, warnings
    )
    assert normalized["panel"]["colors"]["bg"] == appearance.PANEL_COLOR_DEFAULTS["bg"]
    assert normalized["panel"]["colors"]["text"] == "#abcdef"  # 统一成小写
    assert "panel.colors.bg" in warnings
    assert "panel.colors.text" not in warnings


def test_numbers_are_clamped_to_safe_range():
    normalized = appearance.normalize_appearance({
        "panel": {"layout": {"sidebarWidth": 9999, "fontScale": 0.1, "radius": -5}},
        "viewer": {"layout": {"messageFontSize": 99, "lineHeight": 0}},
    })
    assert normalized["panel"]["layout"]["sidebarWidth"] == 360
    assert normalized["panel"]["layout"]["fontScale"] == 0.8
    assert normalized["panel"]["layout"]["radius"] == 0
    assert normalized["viewer"]["layout"]["messageFontSize"] == 24
    assert normalized["viewer"]["layout"]["lineHeight"] == 1.2


def test_garbage_numbers_fall_back_to_default():
    normalized = appearance.normalize_appearance({
        "viewer": {"layout": {"avatarSize": "big", "messageGap": None}},
    })
    assert normalized["viewer"]["layout"]["avatarSize"] == 36
    assert normalized["viewer"]["layout"]["messageGap"] == 3


def test_text_emoji_scale_is_clamped_and_keeps_two_decimals():
    """气泡里文字表情的倍数：0.5–2.5，默认 1（跟文字一样大）。"""
    layout = appearance.VIEWER_LAYOUT_DEFAULTS
    assert appearance.default_appearance()["viewer"]["layout"]["emojiScale"] == 1.0
    clamp = lambda value: appearance.normalize_appearance(
        {"viewer": {"layout": {"emojiScale": value}}}
    )["viewer"]["layout"]["emojiScale"]
    assert clamp(9) == layout["emojiScale"][2]
    assert clamp(0.1) == layout["emojiScale"][1]
    assert clamp(1.15) == 1.15
    assert clamp("1.5") == 1.0  # 字符串不接受，回默认


def test_message_letter_spacing_and_emoji_offsets_are_validated():
    """气泡字间距（px）和文字式表情偏移（em）：默认 0（不改观感），能填负数。"""
    defaults = appearance.default_appearance()["viewer"]["layout"]
    assert defaults["letterSpacing"] == 0.0
    assert defaults["emojiOffsetX"] == 0.0
    assert defaults["emojiOffsetY"] == 0.0

    spacing = lambda value: appearance.normalize_appearance(
        {"viewer": {"layout": {"letterSpacing": value}}}
    )["viewer"]["layout"]["letterSpacing"]
    assert spacing(99) == appearance.VIEWER_LAYOUT_DEFAULTS["letterSpacing"][2]
    assert spacing(-99) == appearance.VIEWER_LAYOUT_DEFAULTS["letterSpacing"][1]
    assert spacing(-0.5) == -0.5
    assert spacing(1.25) == 1.25  # 可以填小数（0.5 步进的滑块）

    offset = lambda key, value: appearance.normalize_appearance(
        {"viewer": {"layout": {key: value}}}
    )["viewer"]["layout"][key]
    assert offset("emojiOffsetX", 99) == appearance.VIEWER_LAYOUT_DEFAULTS["emojiOffsetX"][2]
    assert offset("emojiOffsetY", -99) == appearance.VIEWER_LAYOUT_DEFAULTS["emojiOffsetY"][1]
    assert offset("emojiOffsetX", -0.25) == -0.25
    assert offset("emojiOffsetY", 0.4) == 0.4  # 偏移量是相对字号的小数倍数
    assert offset("emojiOffsetX", "0.5") == 0.0  # 字符串不接受，回默认


def test_theme_choice_is_validated():
    assert appearance.normalize_appearance({"panel": {"theme": "ocean"}})["panel"]["theme"] == "ocean"
    assert appearance.normalize_appearance({"panel": {"theme": "neon"}})["panel"]["theme"] == "dark"
    assert appearance.normalize_appearance({"viewer": {"theme": "wechat"}})["viewer"]["theme"] == "wechat"
    # 面板没有 warm，查看器有
    assert appearance.normalize_appearance({"panel": {"theme": "warm"}})["panel"]["theme"] == "dark"


def test_font_values_are_sanitized():
    # 预设 id 原样保留
    assert appearance.normalize_appearance({"fonts": {"ui": "yahei"}})["fonts"]["ui"] == "yahei"
    # 本机字体名只留允许的字符，杜绝拼出意外的 CSS
    dirty = appearance.normalize_appearance({"fonts": {"ui": "family:'Bad}; font {x"}})
    assert dirty["fonts"]["ui"] == "family:Bad font x"
    # 清干净后是空的 -> 回退默认
    assert appearance.normalize_appearance({"fonts": {"viewer": "family:;;;"}})["fonts"]["viewer"] == "system"


def test_only_the_important_options_are_exposed():
    """选项经过精简：等宽字体、浮层背景、省略的配色/尺寸都不再是设置项。"""
    defaults = appearance.default_appearance()
    assert set(defaults["fonts"]) == {"ui", "viewer"}
    assert "surface" not in defaults["panel"]["colors"]
    assert "accent2" not in defaults["panel"]["colors"]
    assert "sectionGap" not in defaults["panel"]["layout"]
    assert {"accentHover", "scrollbarThumb", "priceColor"}.isdisjoint(defaults["viewer"]["colors"])
    assert {"senderFontSize", "timeFontSize", "groupGap", "listPadX", "listPadY"}.isdisjoint(
        defaults["viewer"]["layout"])
    # 卡片尺寸只留"图片最大边长"，并且并进了查看器尺寸组
    assert defaults["viewer"]["layout"]["imageMaxSize"] == 240
    assert "cards" not in defaults["viewer"]
    # 气泡字间距与文字式表情偏移是查看器布局里的独立项
    assert {"letterSpacing", "emojiOffsetX", "emojiOffsetY"} <= set(defaults["viewer"]["layout"])


def test_removed_options_are_dropped_from_old_configs():
    """老配置文件里被删掉的项不会报错，会被丢掉、界面上仍用内置默认值。"""
    normalized = appearance.normalize_appearance({
        "fonts": {"ui": "yahei", "mono": "mono"},
        "panel": {"colors": {"surface": "#123456", "accent": "#abcdef"}, "layout": {"sectionGap": 30}},
        "viewer": {"colors": {"accentHover": "#111111"}, "layout": {"groupGap": 20}, "cards": {"shareCardWidth": 300}},
    })
    assert normalized["fonts"] == {"ui": "yahei", "viewer": "system"}
    assert normalized["panel"]["colors"]["accent"] == "#abcdef"
    assert "surface" not in normalized["panel"]["colors"]
    assert "sectionGap" not in normalized["panel"]["layout"]
    assert "accentHover" not in normalized["viewer"]["colors"]
    assert "groupGap" not in normalized["viewer"]["layout"]
    assert "cards" not in normalized["viewer"]


def test_font_stack_resolution():
    assert "Microsoft YaHei" in appearance.font_stack("yahei")
    assert appearance.font_stack("family:My Font").startswith("'My Font', ")
    # 识别不了的值回退到系统默认栈
    assert appearance.font_stack("nope") == appearance.font_stack("system")


def test_font_options_expose_both_languages():
    options = appearance.font_options()
    ids = {o["id"] for o in options}
    assert {"system", "yahei", "mono"} <= ids
    assert all(o["labelZh"] and o["labelEn"] and o["stack"] for o in options)


def test_merge_appearance_is_recursive():
    base = appearance.default_appearance()
    merged = appearance.merge_appearance(base, {"viewer": {"colors": {"accent": "#111111"}}})
    assert merged["viewer"]["colors"]["accent"] == "#111111"
    assert merged["viewer"]["colors"]["bgPrimary"] == base["viewer"]["colors"]["bgPrimary"]
    assert merged["viewer"]["layout"] == base["viewer"]["layout"]


def test_api_reads_and_writes_appearance(config_path):
    client = TestClient(main.app)
    assert client.get("/panel/api/appearance").json()["appearance"] == appearance.default_appearance()

    saved = client.post(
        "/panel/api/appearance",
        json={"appearance": {"viewer": {"theme": "custom", "colors": {"accent": "#123456"}}}},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["status"] == "ok"
    assert body["appearance"]["viewer"]["theme"] == "custom"
    assert body["appearance"]["viewer"]["colors"]["accent"] == "#123456"
    # 没提到的字段保留默认值，不是整份被替换掉
    assert body["appearance"]["panel"]["theme"] == "dark"

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["appearance"]["viewer"]["colors"]["accent"] == "#123456"
    assert client.get("/panel/api/appearance").json()["appearance"] == body["appearance"]


def test_api_reports_rejected_values(config_path):
    client = TestClient(main.app)
    body = client.post(
        "/panel/api/appearance",
        json={"appearance": {"panel": {"colors": {"accent": "not-a-color"}}}},
    ).json()
    assert body["warnings"] == ["panel.colors.accent"]


def test_saving_appearance_keeps_other_config(config_path):
    # 直接调处理函数（跳过鉴权），专测"只改外观、不碰别的配置"。
    from backend import control_panel as cp

    panel_config.save_config({
        "password_hash": "abc",
        "notify_serverchan_key": "SCT123",
        "schedule": "0 6 * * *",
        "appearance": {"panel": {"theme": "ocean"}},
    })
    asyncio.run(cp.set_panel_appearance(cp.AppearanceUpdate(appearance={"panel": {"theme": "light"}})))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["password_hash"] == "abc"
    assert saved["notify_serverchan_key"] == "SCT123"
    assert saved["schedule"] == "0 6 * * *"
    assert saved["appearance"]["panel"]["theme"] == "light"


def test_appearance_endpoint_is_public_but_writes_are_not(config_path):
    """查看器要在登录前就把主题套好，所以读接口公开；写接口仍然要登录。"""
    panel_config.save_config({"password_hash": "abc"})
    client = TestClient(main.app)

    read = client.get("/api/appearance")
    assert read.status_code == 200
    assert read.json()["appearance"] == appearance.default_appearance()
    assert read.json()["fonts"]

    # 其它 /api/ 仍然要鉴权
    assert client.get("/api/stats").status_code == 401
    # 写接口同样要鉴权（走 /panel 前缀，跟其它面板接口一致）
    assert client.post("/panel/api/appearance", json={"appearance": {}}).status_code == 401
