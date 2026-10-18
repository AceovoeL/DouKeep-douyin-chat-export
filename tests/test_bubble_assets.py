import base64
import io
import json

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from common import bubble_assets, paths


def _png_with_flex(flex, text, size=(198, 162), color=(60, 60, 60, 255)):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, size[0] - 6, size[1] - 6), radius=18, fill=color)
    payload = {
        "flex_setting": flex,
        "text_setting": text,
        "width": size[0],
        "height": size[1],
    }
    xmp = (
        "<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
        "<rdf:Description rdf:about=''>"
        "<xmp:FlexData>" + base64.b64encode(json.dumps(payload).encode()).decode()
        + "</xmp:FlexData>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    info = PngInfo()
    info.add_itxt("XML:com.adobe.xmp", xmp)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def _png_without_flex(size=(120, 80)):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 109, 69), fill=(80, 90, 100, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_flex_data_converts_to_exact_slices_and_padding():
    data = _png_with_flex(
        [79.6, 18.5, 59.1, 39.4],
        [35.2, 20.4, 48.5, 15.2],
    )
    meta = bubble_assets.analyze_bubble_image(data)
    assert meta["estimated"] is False
    assert meta["display"] == {"width": 66.0, "height": 54.0}
    assert meta["slice"] == {"top": 79.6, "right": 39.4, "bottom": 18.5, "left": 59.1}
    assert meta["border"] == {"top": 42.98, "right": 26.0, "bottom": 9.99, "left": 39.01}
    assert meta["padding"] == {"top": 19.01, "right": 10.03, "bottom": 11.02, "left": 32.01}


def test_alpha_fallback_marks_estimated():
    meta = bubble_assets.analyze_bubble_image(
        _png_without_flex(),
        config={"left_gap": 4, "right_gap": 3, "top_gap": 5, "bottom_gap": 5},
    )
    assert meta["estimated"] is True
    assert meta["display"]["width"] == 40.0
    assert meta["padding"]["left"] >= 4
    assert meta["padding"]["right"] >= 3
    assert meta["slice"]["left"] > 0
    assert meta["slice"]["right"] >= 0


def test_mirror_variant_only_swaps_text_padding():
    variant = {
        "slice": {"top": 10, "right": 20, "bottom": 30, "left": 40},
        "border": {"top": 1, "right": 2, "bottom": 3, "left": 4},
        "padding": {"top": 5, "right": 6, "bottom": 7, "left": 8},
    }
    mirrored = bubble_assets.mirror_variant(variant)
    assert mirrored["padding"] == {"top": 5, "right": 8, "bottom": 7, "left": 6}
    assert mirrored["slice"] == variant["slice"]


def test_full_mirror_swaps_everything():
    variant = {
        "flex": [1, 2, 3, 4],
        "text": [5, 6, 7, 8],
        "slice": {"top": 1, "right": 2, "bottom": 3, "left": 4},
        "border": {"top": 1, "right": 2, "bottom": 3, "left": 4},
        "padding": {"top": 5, "right": 6, "bottom": 7, "left": 8},
    }
    mirrored = bubble_assets.mirror_variant_full(variant)
    assert mirrored["slice"]["left"] == 2
    assert mirrored["slice"]["right"] == 4
    assert mirrored["padding"]["left"] == 6
    assert mirrored["flex"] == [1, 2, 4, 3]


def test_bubble_id_from_fields():
    fields = {"f9": [
        "\n\rsource_aid\x12\x041128",
        '\n\ra:chat_bubble\x127{"bubble_id":"1234567890123456789","bubble_source":"1"}',
    ]}
    assert bubble_assets.bubble_id_from_fields(fields) == "1234567890123456789"
    assert bubble_assets.bubble_id_from_fields({"f9": ['{"bubble_id":"-1"}']}) == ""
    assert bubble_assets.bubble_id_from_fields({"f9": "not json"}) == ""
    assert bubble_assets.bubble_id_from_fields({}) == ""


def test_bubble_id_from_raw_data_prefers_top_level():
    raw = json.dumps({"bubble_id": "1234567890123456789", "fields": {}})
    assert bubble_assets.bubble_id_from_raw_data(raw) == "1234567890123456789"
    legacy = json.dumps({"fields": {"f9": ['a:chat_bubble {"bubble_id":"9876543210987654321"}']}})
    assert bubble_assets.bubble_id_from_raw_data(legacy) == "9876543210987654321"
    assert bubble_assets.bubble_id_from_raw_data("") == ""


def test_catalog_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_DIR", str(tmp_path))
    variant = {
        "image": "/media/bubbles/1234567890123456789/self.png",
        "display": {"width": 66, "height": 54},
        "slice": {"top": 79.6, "right": 39.4, "bottom": 18.5, "left": 59.1},
        "border": {"top": 42.98, "right": 26, "bottom": 9.99, "left": 39.01},
        "padding": {"top": 19.01, "right": 10.03, "bottom": 11.02, "left": 32.01},
    }
    bubble_assets.merge_bubble(
        "1234567890123456789", "self", variant,
        "/media/bubbles/1234567890123456789/self.png",
        name="测试气泡",
        config={"text_color": "#ffffff", "left_gap": 1},
        updated_at=123,
    )
    catalog = bubble_assets.load_catalog()
    entry = catalog["bubbles"]["1234567890123456789"]
    assert entry["name"] == "测试气泡"
    assert entry["text_color"] == "#ffffff"
    assert entry["self"]["slice"]["left"] == 59.1


def test_catalog_ignores_broken_json(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_DIR", str(tmp_path))
    (tmp_path / "bubbles").mkdir()
    (tmp_path / "bubbles" / "index.json").write_text("{not json", encoding="utf-8")
    assert bubble_assets.load_catalog() == {"version": 1, "bubbles": {}}


def test_save_bubble_image_rejects_path_tricks(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_DIR", str(tmp_path))
    data = _png_without_flex()
    assert bubble_assets.save_bubble_image("../x", "self", data) is None
    assert bubble_assets.save_bubble_image("1234567890123456789", "other", data) is None
    saved = bubble_assets.save_bubble_image("1234567890123456789", "self", data)
    assert saved and saved[1] == "png"
