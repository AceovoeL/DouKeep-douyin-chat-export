"""聊天气泡装饰的素材与配置：读图、算九宫格、存目录。

抖音的气泡图（PNG）自带一段 XMP FlexData，里面写着：

* flex_setting：九宫格切片，顺序是 上、下、左、右（百分比）；
* text_setting：文字安全区，顺序是 上、下、左、右（百分比）；
* width / height：原图尺寸。

这段数据是抖音画气泡时一起写进去的，比我们自己量像素准。只有图片没有
FlexData（第三方图、截图等）时，才退回透明通道估算，并在配置里标
estimated=True，方便以后人工复查。

配置和图片都放在 data/media/bubbles/，不进仓库（版权原因，见 NOTICE）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
from collections import Counter

from common import paths

#: 抖音气泡是 3 倍图：原图 198×162 在界面里按 66×54 显示。
#: 这个比例和网页自己缓存的 renderMeta 对得上。
BUBBLE_RENDER_SCALE = 3.0

#: FlexData 里的百分比允许 0~100。
_MAX_PERCENT = 100.0
#: 文字区至少留这么多显示像素，防止极端配置把内容挤成 0。
_MIN_CONTENT_PX = 4.0
#: 纯色判定容差（RGB 每个通道）。
_COLOR_TOLERANCE = 12
#: raw_data 超过这个长度时只查末尾，不整包 JSON 解析。
_MAX_RAW_PARSE_CHARS = 2_000_000

_BUBBLE_ID_RE = re.compile(r"^\d{6,25}$")
_FLEX_RE = re.compile(rb"<xmp:FlexData>(.*?)</xmp:FlexData>", re.S)
_TAIL_BUBBLE_RE = re.compile(r'"bubble_id"\s*:\s*"(\d{6,25})"')
_BUBBLE_TEXT_RE = re.compile(
    r'a:chat_bubble.{0,300}?"bubble_id"\s*:\s*"(\d{6,25})"', re.S
)

_CATALOG_LOCK = threading.Lock()


def bubble_dir() -> str:
    """气泡素材目录（data/media/bubbles）。用函数而不是常量，测试好改。"""
    return os.path.join(paths.MEDIA_DIR, "bubbles")


def catalog_path() -> str:
    return os.path.join(bubble_dir(), "index.json")


def is_valid_bubble_id(value: object) -> bool:
    """只接受纯数字 ID，顺带挡住 ..、斜杠这类路径穿越。"""
    return bool(_BUBBLE_ID_RE.match(str(value or "").strip()))


def _empty_catalog() -> dict:
    return {"version": 1, "bubbles": {}}


# ── 气泡 ID ────────────────────────────────────────────────────────────────

def bubble_id_from_fields(fields: object) -> str:
    """从消息原始包的 fields.f9 里取 a:chat_bubble 的 bubble_id。

    f9 可能是字符串、字符串数组，也可能是嵌套对象；每条系统元数据形如
    a:chat_bubble\\x12...{"bubble_id":"...","bubble_source":"1"}。
    """
    if not isinstance(fields, dict):
        return ""
    entries = fields.get("f9")
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not isinstance(entries, list):
        return ""

    for entry in entries:
        if isinstance(entry, dict):
            direct = str(entry.get("bubble_id") or "").strip()
            if is_valid_bubble_id(direct):
                return direct
            found = bubble_id_from_fields(entry)
            if found:
                return found
            continue
        if not isinstance(entry, str) or "a:chat_bubble" not in entry:
            continue
        start = entry.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(entry[start:])
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            bubble_id = str(payload.get("bubble_id") or "").strip()
            if is_valid_bubble_id(bubble_id):
                return bubble_id
    return ""


def bubble_id_from_raw_data(raw: object) -> str:
    """从 messages.raw_data 里补 bubble_id（给没有新字段的旧记录用）。

    新采集的消息会在 raw_data 顶层直接写 bubble_id；旧消息要翻
    fields.f9。超大 raw_data 只查末尾，避免把阅读端拖慢。
    """
    if isinstance(raw, dict):
        direct = str(raw.get("bubble_id") or "").strip()
        if is_valid_bubble_id(direct):
            return direct
        return bubble_id_from_fields(raw.get("fields") or {})

    if not isinstance(raw, str):
        return ""
    if len(raw) > _MAX_RAW_PARSE_CHARS:
        match = _TAIL_BUBBLE_RE.search(raw[-8192:])
        return match.group(1) if match else ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        direct = str(payload.get("bubble_id") or "").strip()
        if is_valid_bubble_id(direct):
            return direct
        found = bubble_id_from_fields(payload.get("fields") or {})
        if found:
            return found
    if "a:chat_bubble" not in raw:
        return ""
    match = _BUBBLE_TEXT_RE.search(raw)
    return match.group(1) if match else ""


# ── 图片格式与尺寸 ─────────────────────────────────────────────────────────

def detect_image_ext(data: bytes) -> str:
    """按文件头判断扩展名；认不出返回空串。"""
    if not data:
        return ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    return ""


def _load_rgba(data_or_path):
    from PIL import Image

    if isinstance(data_or_path, (bytes, bytearray)):
        import io

        image = Image.open(io.BytesIO(bytes(data_or_path)))
    else:
        image = Image.open(data_or_path)
    image.load()
    return image.convert("RGBA")


def _read_flex_data(image) -> dict | None:
    """读 PNG XMP 里的 xmp:FlexData；没有或不合法返回 None。"""
    info = getattr(image, "info", {}) or {}
    raw = info.get("xmp")
    if raw is None:
        raw = info.get("XML:com.adobe.xmp")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "ignore")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    match = _FLEX_RE.search(bytes(raw))
    if not match:
        return None
    try:
        decoded = base64.b64decode(match.group(1).strip())
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    flex = payload.get("flex_setting")
    text = payload.get("text_setting")
    if not (_is_percent_list(flex) and _is_percent_list(text)):
        return None
    return {"flex": flex, "text": text, "payload": payload}


def _is_percent_list(value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return False
    return all(0.0 <= item <= _MAX_PERCENT for item in numbers)


def _normalize_config(config: object) -> dict:
    """把详情接口返回的 resource_config 弄成好用的字典。"""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            config = None
    if not isinstance(config, dict):
        config = {}

    text_color = str(config.get("text_color") or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", text_color):
        text_color = ""

    def number(key: str) -> float:
        try:
            value = float(config.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return value if -100.0 <= value <= 100.0 else 0.0

    return {
        "text_color": text_color,
        "offset_horizontal": number("offset_horizontal"),
        "offset_vertical": number("offset_vertical"),
        "top_gap": number("top_gap"),
        "right_gap": number("right_gap"),
        "bottom_gap": number("bottom_gap"),
        "left_gap": number("left_gap"),
    }


def _clamp_padding(padding: dict, display_w: float, display_h: float) -> dict:
    """保证左右/上下内边距加起来不会把文字区挤没。"""
    left = max(0.0, float(padding.get("left") or 0))
    right = max(0.0, float(padding.get("right") or 0))
    if left + right > display_w - _MIN_CONTENT_PX:
        scale = max(0.0, (display_w - _MIN_CONTENT_PX)) / (left + right)
        left *= scale
        right *= scale
    top = max(0.0, float(padding.get("top") or 0))
    bottom = max(0.0, float(padding.get("bottom") or 0))
    if top + bottom > display_h - _MIN_CONTENT_PX:
        scale = max(0.0, (display_h - _MIN_CONTENT_PX)) / (top + bottom)
        top *= scale
        bottom *= scale
    return {
        "top": round(top, 2),
        "right": round(right, 2),
        "bottom": round(bottom, 2),
        "left": round(left, 2),
    }


def _variant_from_percents(
    source_w: int,
    source_h: int,
    flex: list,
    text: list,
    *,
    scale: float = BUBBLE_RENDER_SCALE,
    estimated: bool = False,
) -> dict:
    """FlexData 的百分比 → 查看器直接能用的像素参数。

    flex / text 的顺序都是 上、下、左、右；配置里存成
    上、右、下、左，方便直接拼 CSS。
    """
    scale = float(scale or BUBBLE_RENDER_SCALE)
    if scale <= 0:
        scale = BUBBLE_RENDER_SCALE
    display_w = source_w / scale
    display_h = source_h / scale

    flex_values = [float(item) for item in flex]
    text_values = [float(item) for item in text]
    border_top = flex_values[0] / 100.0 * display_h
    border_bottom = flex_values[1] / 100.0 * display_h
    border_left = flex_values[2] / 100.0 * display_w
    border_right = flex_values[3] / 100.0 * display_w

    pad_top = text_values[0] / 100.0 * display_h
    pad_bottom = text_values[1] / 100.0 * display_h
    pad_left = text_values[2] / 100.0 * display_w
    pad_right = text_values[3] / 100.0 * display_w
    padding = _clamp_padding(
        {"top": pad_top, "right": pad_right, "bottom": pad_bottom, "left": pad_left},
        display_w,
        display_h,
    )

    return {
        "source": {"width": int(source_w), "height": int(source_h)},
        "display": {"width": round(display_w, 2), "height": round(display_h, 2)},
        "scale": scale,
        "flex": [round(item, 2) for item in flex_values],
        "text": [round(item, 2) for item in text_values],
        "slice": {
            "top": round(flex_values[0], 2),
            "right": round(flex_values[3], 2),
            "bottom": round(flex_values[1], 2),
            "left": round(flex_values[2], 2),
        },
        "border": {
            "top": round(border_top, 2),
            "right": round(border_right, 2),
            "bottom": round(border_bottom, 2),
            "left": round(border_left, 2),
        },
        "padding": padding,
        "estimated": bool(estimated),
    }


def _largest_uniform_rect(image) -> tuple[int, int, int, int]:
    """找最大的一块“不透明且颜色接近”的矩形，返回 (x, y, w, h)。"""
    width, height = image.size
    pixels = image.load()
    colors = Counter()
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            if alpha >= 250:
                colors[(red, green, blue)] += 1
    if not colors:
        return (0, 0, 0, 0)
    dominant = colors.most_common(1)[0][0]

    def matches(x: int, y: int) -> bool:
        red, green, blue, alpha = pixels[x, y]
        return (
            alpha >= 250
            and abs(red - dominant[0]) <= _COLOR_TOLERANCE
            and abs(green - dominant[1]) <= _COLOR_TOLERANCE
            and abs(blue - dominant[2]) <= _COLOR_TOLERANCE
        )

    def max_rect(mask):
        heights = [0] * width
        best_area, best_x, best_y, best_w, best_h = 0, 0, 0, 0, 0
        for y in range(height):
            for x in range(width):
                heights[x] = heights[x] + 1 if mask(x, y) else 0
            stack: list[int] = []
            for x in range(width + 1):
                current = heights[x] if x < width else 0
                while stack and heights[stack[-1]] >= current:
                    h = heights[stack.pop()]
                    left = stack[-1] + 1 if stack else 0
                    area = h * (x - left)
                    if area > best_area:
                        best_area, best_x, best_y, best_w, best_h = (
                            area, left, y - h + 1, x - left, h
                        )
                stack.append(x)
        return best_area, best_x, best_y, best_w, best_h

    area, x, y, w, h = max_rect(matches)
    if area < 16:
        alpha = image.getchannel("A")
        area, x, y, w, h = max_rect(lambda px, py: alpha.getpixel((px, py)) >= 250)
    if area < 16:
        bbox = image.getchannel("A").getbbox()
        if bbox:
            x, y, right, bottom = bbox
            w, h = right - x, bottom - y
        else:
            x = y = 0
            w, h = width, height
    return (int(x), int(y), int(w), int(h))


def analyze_bubble_image(image_or_bytes, *, scale: float = BUBBLE_RENDER_SCALE,
                         config: object = None) -> dict | None:
    """分析一张气泡图，返回查看器能用的参数；完全分析不了时返回 None。

    优先读 PNG 自带的 FlexData；没有才按透明通道估算，并标 estimated。
    """
    try:
        image = _load_rgba(image_or_bytes)
    except Exception:
        return None
    width, height = image.size
    if width < 8 or height < 8 or width > 4096 or height > 4096:
        return None

    flex = _read_flex_data(image)
    if flex:
        return _variant_from_percents(
            width, height, flex["flex"], flex["text"], scale=scale,
        )

    config = _normalize_config(config)
    rect = _largest_uniform_rect(image)
    x, y, rect_w, rect_h = rect
    if rect_w <= 0 or rect_h <= 0:
        return None
    display_w = width / float(scale or BUBBLE_RENDER_SCALE)
    display_h = height / float(scale or BUBBLE_RENDER_SCALE)

    # 用“纯色区域”的边缘当切片，再把配置里的 gap 加在文字安全区上。
    left = x / width * 100.0
    right = (width - (x + rect_w)) / width * 100.0
    top = y / height * 100.0
    bottom = (height - (y + rect_h)) / height * 100.0
    flex_values = [top, bottom, left, right]

    padding = {
        "top": top / 100.0 * display_h + config["top_gap"],
        "right": right / 100.0 * display_w + config["right_gap"],
        "bottom": bottom / 100.0 * display_h + config["bottom_gap"],
        "left": left / 100.0 * display_w + config["left_gap"],
    }
    padding = _clamp_padding(padding, display_w, display_h)
    text_values = [
        padding["top"] / display_h * 100.0,
        padding["bottom"] / display_h * 100.0,
        padding["left"] / display_w * 100.0,
        padding["right"] / display_w * 100.0,
    ]
    return _variant_from_percents(
        width, height, flex_values, text_values, scale=scale, estimated=True,
    )


def mirror_variant(variant: dict) -> dict:
    """把一侧的气泡参数左右镜像，供缺图时兜底。

    注意：CSS 背景层会用 scaleX(-1) 镜像图片，所以切片和边框不换，
    只换文字 padding 的左右，以及水平偏移的符号。这个函数只负责文字侧。
    """
    mirrored = json.loads(json.dumps(variant))
    padding = mirrored.get("padding") or {}
    padding["left"], padding["right"] = padding.get("right", 0), padding.get("left", 0)
    mirrored["padding"] = padding
    return mirrored


def mirror_variant_full(variant: dict) -> dict:
    """完整镜像：切片、边框、padding、百分比全部左右互换。"""
    mirrored = json.loads(json.dumps(variant))
    for key in ("slice", "border", "padding"):
        block = mirrored.get(key) or {}
        block["left"], block["right"] = block.get("right", 0), block.get("left", 0)
        mirrored[key] = block
    for key in ("flex", "text"):
        values = mirrored.get(key)
        if isinstance(values, list) and len(values) == 4:
            values[2], values[3] = values[3], values[2]
    return mirrored


# ── 配置表读写 ─────────────────────────────────────────────────────────────

def load_catalog() -> dict:
    """读配置表；不存在或坏了都返回空表，不让后端 500。"""
    path = catalog_path()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return _empty_catalog()
    if not isinstance(payload, dict):
        return _empty_catalog()
    bubbles = payload.get("bubbles")
    if not isinstance(bubbles, dict):
        bubbles = {}
    try:
        version = int(payload.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    return {
        "version": version,
        "bubbles": {
            str(key): value for key, value in bubbles.items()
            if is_valid_bubble_id(key) and isinstance(value, dict)
        },
    }


def write_catalog(catalog: dict) -> None:
    """原子写配置表，避免读到写了一半的 JSON。"""
    directory = bubble_dir()
    os.makedirs(directory, exist_ok=True)
    payload = {
        "version": int(catalog.get("version") or 1),
        "bubbles": catalog.get("bubbles") or {},
    }
    with _CATALOG_LOCK:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".index-", suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, catalog_path())
        except Exception:
            try:
                handle.close()
            finally:
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
            raise


def merge_bubble(bubble_id: str, side: str, variant: dict, image: str, *,
                 name: str = "", config: object = None,
                 updated_at: int | None = None) -> dict:
    """把一个变体合并进配置表并写回，返回最新配置。"""
    if not is_valid_bubble_id(bubble_id):
        raise ValueError("bubble_id 不合法")
    if side not in ("self", "guest"):
        raise ValueError("side 只能是 self 或 guest")
    normalized = _normalize_config(config)
    catalog = load_catalog()
    entry = catalog["bubbles"].setdefault(bubble_id, {
        "id": bubble_id,
        "name": "",
        "text_color": "",
        "offset": {"x": 0, "y": 0},
        "gap": {},
    })
    if name:
        entry["name"] = str(name)[:120]
    entry["text_color"] = normalized["text_color"]
    entry["offset"] = {
        "x": normalized["offset_horizontal"],
        "y": normalized["offset_vertical"],
    }
    entry["gap"] = {
        "top": normalized["top_gap"],
        "right": normalized["right_gap"],
        "bottom": normalized["bottom_gap"],
        "left": normalized["left_gap"],
    }
    entry[side] = {"image": image, **variant}
    entry["updated_at"] = int(updated_at or 0)
    catalog["bubbles"][bubble_id] = entry
    write_catalog(catalog)
    return entry


def save_bubble_image(bubble_id: str, side: str, data: bytes) -> tuple[str, str] | None:
    """把气泡图写到 data/media/bubbles/<id>/<side>.<ext>。

    返回 (相对 media 的路径, 扩展名)；格式认不出时返回 None。
    """
    if not is_valid_bubble_id(bubble_id):
        return None
    if side not in ("self", "guest"):
        return None
    ext = detect_image_ext(data)
    if not ext:
        return None
    directory = os.path.join(bubble_dir(), bubble_id)
    os.makedirs(directory, exist_ok=True)
    filename = f"{side}.{ext}"
    target = os.path.join(directory, filename)
    temp = target + ".tmp"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return f"bubbles/{bubble_id}/{filename}", ext


def analyze_local_image(path: str, *, scale: float = BUBBLE_RENDER_SCALE,
                        config: object = None) -> dict | None:
    """给工具和测试用：直接分析本地图片。"""
    return analyze_bubble_image(path, scale=scale, config=config)
