"""外观（主题/字体/尺寸）配置：默认值 + 校验，浏览器端与后端共用同一份规则。

配置存在 ``data/panel_config.json`` 的 ``appearance`` 字段里。所有进入浏览器
的值都必须先过 ``normalize_appearance()``：多余的字段丢掉、数字夹到安全范围、
颜色必须是十六进制，手改或导入一份乱七八糟的 JSON 也绝不会把界面弄坏。

浏览器端不重复保存默认值 —— 面板和查看器都用 ``GET .../api/appearance``
返回的 ``defaults`` 做"单项恢复默认"，所以这里就是唯一的真相来源。
"""
from __future__ import annotations

import re

# ── 字体 ──

# 内置字体清单：id -> (中文名, 英文名, CSS font-family 栈)
FONT_PRESETS: tuple[tuple[str, str, str, str], ...] = (
    ("system", "系统默认", "System default",
     "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', "
     "'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"),
    ("yahei", "微软雅黑", "Microsoft YaHei",
     "'Microsoft YaHei', 'PingFang SC', 'Noto Sans SC', sans-serif"),
    ("pingfang", "苹方", "PingFang",
     "'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"),
    ("sourcehan", "思源黑体", "Source Han Sans",
     "'Source Han Sans SC', 'Noto Sans SC', 'Microsoft YaHei', sans-serif"),
    ("simhei", "黑体", "SimHei",
     "'SimHei', 'Heiti SC', 'Microsoft YaHei', sans-serif"),
    ("simsun", "宋体", "SimSun",
     "'SimSun', 'Songti SC', 'Noto Serif SC', serif"),
    ("kaiti", "楷体", "KaiTi",
     "'KaiTi', 'Kaiti SC', 'STKaiti', serif"),
    ("fangsong", "仿宋", "FangSong",
     "'FangSong', 'STFangsong', 'SimSun', serif"),
    ("dengxian", "等线", "DengXian",
     "'DengXian', 'Microsoft YaHei', sans-serif"),
    ("mono", "等宽（代码）", "Monospace",
     "ui-monospace, SFMono-Regular, 'Cascadia Code', Consolas, monospace"),
    ("consolas", "Consolas", "Consolas",
     "'Consolas', 'Cascadia Code', ui-monospace, monospace"),
    ("serif", "衬线", "Serif",
     "Georgia, 'Times New Roman', 'Songti SC', serif"),
)

_FONT_IDS = {row[0] for row in FONT_PRESETS}
_FONT_STACKS = {row[0]: row[3] for row in FONT_PRESETS}

# 选了"自己电脑上的字体"时，后面接的系统兜底栈
_FONT_FALLBACK = _FONT_STACKS["system"]

# 字体名里允许出现的字符：中英文、数字、空格、连字符、点、下划线。
# 其它字符（引号、分号、括号……）全部丢掉，避免拼出意外的 CSS。
_FONT_NAME_ALLOWED = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf ._-]")
_FONT_NAME_MAX = 64

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# 只留两个真正影响观感的字体：面板界面、聊天内容。等宽字体（只影响日志和原始
# JSON）太偏门，不再开放。
_FONT_KEYS = ("ui", "viewer")
_DEFAULT_FONT = {"ui": "system", "viewer": "system"}

PANEL_THEMES = ("dark", "light", "ocean", "purple", "custom")
VIEWER_THEMES = ("dark", "wechat", "light", "warm", "purple", "custom")

# ── 面板：颜色 / 布局 ──
# 颜色默认值取自 panel.html 的 dark 主题，选"自定义"时以它当起点。
PANEL_COLOR_DEFAULTS: dict[str, str] = {
    "bg": "#0f1014",
    "bg2": "#16181f",
    "bg3": "#21242e",
    "accent": "#f2586e",
    "text": "#e9eaf0",
    "text2": "#9a9daa",
    "text3": "#656875",
    "border": "#23262f",
    "green": "#4dd08a",
    "red": "#ff5c6c",
    "yellow": "#e2b23c",
}

# name -> (默认值, 最小, 最大)
PANEL_LAYOUT_DEFAULTS: dict[str, tuple[float, float, float]] = {
    "radius": (14.0, 0.0, 32.0),
    "cardPadding": (22.0, 8.0, 48.0),
    "sidebarWidth": (224.0, 160.0, 360.0),
    "fontScale": (1.0, 0.8, 1.5),
    "shadow": (1.0, 0.0, 3.0),  # 0 无 / 1 轻 / 2 中 / 3 重
}

# ── 查看器：颜色 / 布局 / 卡片 ──
# 颜色默认值取自 frontend/src/style.css 的 :root（暗色主题）。
VIEWER_COLOR_DEFAULTS: dict[str, str] = {
    "bgPrimary": "#0f1014",
    "bgSecondary": "#15171e",
    "bgTertiary": "#21242e",
    "bgMessageSelf": "#f2596f",
    "bgMessageSelfEnd": "#e34a60",
    "bgMessageOther": "#1e212a",
    "textOnSelf": "#ffffff",
    "textPrimary": "#e9eaf0",
    "textSecondary": "#9a9daa",
    "textMuted": "#656875",
    "borderColor": "#23262f",
    "accent": "#f2586e",
    "highlight": "#ffd36b",
    "systemText": "#9a9daa",
    "systemBg": "#1b1e26",
    "cardBg": "#1e212a",
    "cardBorder": "#2a2e3a",
}

VIEWER_LAYOUT_DEFAULTS: dict[str, tuple[float, float, float]] = {
    "messageFontSize": (14.0, 10.0, 24.0),
    # 气泡里的文字式表情（[钱]、[憨笑] 这种）大小：相对消息字号的倍数，
    # 1 就是"跟文字一样大"，默认值保持原样。
    "emojiScale": (1.0, 0.5, 2.5),
    "listFontSize": (13.0, 10.0, 18.0),
    "lineHeight": (1.55, 1.2, 2.0),
    "bubbleRadius": (14.0, 0.0, 32.0),
    "bubblePadX": (13.0, 4.0, 24.0),
    "bubblePadY": (9.0, 4.0, 24.0),
    "bubbleMaxWidthPct": (74.0, 40.0, 95.0),
    "avatarSize": (36.0, 20.0, 64.0),
    "messageGap": (3.0, 0.0, 24.0),
    "sidebarWidth": (300.0, 200.0, 420.0),
    # 表情包、图片、视频分开控制最大边长（默认值＝原来的观感，不改也不会变样）
    "emojiMaxSize": (240.0, 40.0, 320.0),
    "imageMaxSize": (240.0, 120.0, 600.0),
    "videoMaxSize": (280.0, 120.0, 600.0),
    # 气泡文字的字间距（px）：0＝原样，压缩字距填负数
    "letterSpacing": (0.0, -1.0, 8.0),
    # 文字式表情（[钱]、[憨笑] 这种被换成小图片的记号）的偏移：相对消息字号的
    # 倍数，0＝原来的基线位置，正 X 向右、正 Y 向下。
    "emojiOffsetX": (0.0, -1.0, 1.0),
    "emojiOffsetY": (0.0, -1.0, 1.0),
}

# 小数位：这些字段是"几倍""多少行高"以及"可以填小数"的间距，保留 2 位；
# 其余取整（阴影是 0-3 的档位）。
_FLOAT_KEYS = {"fontScale", "lineHeight", "emojiScale", "letterSpacing",
               "emojiOffsetX", "emojiOffsetY"}


def default_appearance() -> dict:
    """返回一份全新的默认外观配置（每次调用都是新对象）。"""
    def layout(specs: dict[str, tuple[float, float, float]]) -> dict:
        return {
            key: (round(spec[0], 2) if key in _FLOAT_KEYS else int(spec[0]))
            for key, spec in specs.items()
        }

    return {
        "version": 1,
        "fonts": {key: _DEFAULT_FONT[key] for key in _FONT_KEYS},
        "panel": {
            "theme": "dark",
            "colors": dict(PANEL_COLOR_DEFAULTS),
            "layout": layout(PANEL_LAYOUT_DEFAULTS),
        },
        "viewer": {
            "theme": "dark",
            "colors": dict(VIEWER_COLOR_DEFAULTS),
            "layout": layout(VIEWER_LAYOUT_DEFAULTS),
        },
    }


def _warn(warnings: list[str] | None, path: str) -> None:
    if warnings is not None and path not in warnings:
        warnings.append(path)


def _norm_color(value, default: str, path: str, warnings: list[str] | None) -> str:
    if isinstance(value, str):
        text = value.strip()
        if _HEX_COLOR.match(text):
            return text.lower()
    if value is not None:
        _warn(warnings, path)
    return default


def _norm_number(value, spec: tuple[float, float, float], path: str,
                 warnings: list[str] | None, *, is_float: bool = False):
    default, low, high = spec
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if value is not None:
            _warn(warnings, path)
        return default
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        _warn(warnings, path)
        return default
    clamped = min(max(number, low), high)
    if clamped != number:
        _warn(warnings, path)
    if is_float:
        return round(clamped, 2)
    return int(round(clamped))


def _norm_font(value, fallback: str, path: str, warnings: list[str] | None) -> str:
    if not isinstance(value, str):
        if value is not None:
            _warn(warnings, path)
        return fallback
    text = value.strip()
    if text in _FONT_IDS:
        return text
    if text.startswith("family:"):
        name = _FONT_NAME_ALLOWED.sub("", text[len("family:"):]).strip()
        name = re.sub(r"\s+", " ", name)[:_FONT_NAME_MAX].strip()
        if name:
            return "family:" + name
    _warn(warnings, path)
    return fallback


def font_stack(value: str) -> str:
    """把 ``system`` / ``family:微软雅黑`` 解析成可直接用的 CSS font-family。"""
    if value in _FONT_STACKS:
        return _FONT_STACKS[value]
    if isinstance(value, str) and value.startswith("family:"):
        name = value[len("family:"):].strip()
        if name:
            escaped = name.replace("\\", "").replace("'", "")
            if escaped:
                return f"'{escaped}', {_FONT_FALLBACK}"
    return _FONT_FALLBACK


def font_options() -> list[dict]:
    """给设置界面用的字体清单（中文名 + 英文名 + 解析后的字体栈）。"""
    return [
        {"id": fid, "labelZh": zh, "labelEn": en, "stack": stack}
        for fid, zh, en, stack in FONT_PRESETS
    ]


def _norm_color_group(raw, defaults: dict[str, str], prefix: str,
                      warnings: list[str] | None) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    return {
        key: _norm_color(source.get(key), default, f"{prefix}.{key}", warnings)
        for key, default in defaults.items()
    }


def _norm_number_group(raw, specs: dict[str, tuple[float, float, float]],
                       prefix: str, warnings: list[str] | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    return {
        key: _norm_number(
            source.get(key), spec, f"{prefix}.{key}", warnings,
            is_float=key in _FLOAT_KEYS,
        )
        for key, spec in specs.items()
    }


def _norm_choice(value, allowed: tuple[str, ...], default: str, path: str,
                 warnings: list[str] | None) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    if value is not None:
        _warn(warnings, path)
    return default


def normalize_appearance(raw, warnings: list[str] | None = None) -> dict:
    """把任意输入整理成一份合法、完整的外观配置。

    ``warnings`` 传入一个列表时，会被填入"哪些字段没被接受"的路径，
    供设置界面提示用户（例如 ``viewer.colors.accent``）。
    """
    defaults = default_appearance()
    source = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        _warn(warnings, "appearance")

    fonts_raw = source.get("fonts") if isinstance(source.get("fonts"), dict) else {}
    fonts = {
        key: _norm_font(fonts_raw.get(key), defaults["fonts"][key], f"fonts.{key}", warnings)
        for key in _FONT_KEYS
    }

    panel_raw = source.get("panel") if isinstance(source.get("panel"), dict) else {}
    panel = {
        "theme": _norm_choice(
            panel_raw.get("theme"), PANEL_THEMES, defaults["panel"]["theme"],
            "panel.theme", warnings,
        ),
        "colors": _norm_color_group(
            panel_raw.get("colors"), PANEL_COLOR_DEFAULTS, "panel.colors", warnings,
        ),
        "layout": _norm_number_group(
            panel_raw.get("layout"), PANEL_LAYOUT_DEFAULTS, "panel.layout", warnings,
        ),
    }

    viewer_raw = source.get("viewer") if isinstance(source.get("viewer"), dict) else {}
    viewer = {
        "theme": _norm_choice(
            viewer_raw.get("theme"), VIEWER_THEMES, defaults["viewer"]["theme"],
            "viewer.theme", warnings,
        ),
        "colors": _norm_color_group(
            viewer_raw.get("colors"), VIEWER_COLOR_DEFAULTS, "viewer.colors", warnings,
        ),
        "layout": _norm_number_group(
            viewer_raw.get("layout"), VIEWER_LAYOUT_DEFAULTS, "viewer.layout", warnings,
        ),
    }

    return {"version": 1, "fonts": fonts, "panel": panel, "viewer": viewer}


def load_appearance(config: dict | None = None) -> dict:
    """从面板配置里读出（并规范化）外观设置。"""
    if config is None:
        from common import config as _config

        config = _config.load_config()
    raw = config.get("appearance") if isinstance(config, dict) else None
    return normalize_appearance(raw)


def merge_appearance(base: dict, patch) -> dict:
    """把一份"只改了部分字段"的外观配置合并进已有配置（递归合并字典）。"""
    if not isinstance(patch, dict):
        return base
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_appearance(current, value)
        else:
            merged[key] = value
    return merged
