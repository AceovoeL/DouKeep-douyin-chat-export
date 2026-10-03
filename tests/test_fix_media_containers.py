"""Tests for ``tools/fix_media_containers.py``.

The fixture matters here. Rather than hand-build an MP4 (which cannot contain a
real H.265 bitstream), we let ffmpeg generate a genuine clip and then perform
the same surgery Douyin's CDN does: rename the video sample entry to ``encv``
and bolt a ``sinf``/``frma`` onto it. The clip is encoded *without* faststart,
so ``moov`` follows ``mdat`` and growing it cannot shift any chunk offset —
that keeps the surgery honest (labels change, samples do not) without the test
having to reimplement offset fixing.
"""
import os
import struct

import pytest

from backend import media_transcode
from common import mp4, paths
from tools import fix_media_containers as fixer


def _box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


def _write_stub(tmp_path, name, marker: bytes) -> str:
    """A file whose content is irrelevant — every reader it hits is patched."""
    path = tmp_path / name
    path.write_bytes(marker)
    return str(path)


# The ``tenc`` payload copied byte-for-byte out of an archived download
# (data/media/videos/1000000000000000044.mp4): version/flags, then the fields
# that carry ``default_isProtected = 0`` — the flag that makes ffmpeg treat the
# samples as plain. Omitting it (or inventing the layout) makes ffmpeg assume
# real encryption and refuse the remux with "Could not find tag for codec none".
_DOUYIN_SCHM_PAYLOAD = b"\x00\x00\x00\x00cenc" + struct.pack(">I", 0x00010000)
_DOUYIN_TENC_PAYLOAD = bytes.fromhex(
    "00000000" "00000108" "ca57e7e3ae76daea490879bb61320aba")


def _sinf(original_fourcc: bytes) -> bytes:
    schm = _box(b"schm", _DOUYIN_SCHM_PAYLOAD)
    schi = _box(b"schi", _box(b"tenc", _DOUYIN_TENC_PAYLOAD))
    return _box(b"sinf", _box(b"frma", original_fourcc) + schm + schi)


def _wrap_as_cenc(src: str, dst: str) -> str:
    """Re-label ``src`` the way the CDN did: ``hvc1`` → ``encv`` + ``sinf``."""
    buf = bytearray(open(src, "rb").read())
    entry = next(e for e in mp4.sample_entries(bytes(buf))
                 if e.fourcc in mp4.VIDEO_ENTRIES)
    insert_at = entry.start + entry.size
    sinf = _sinf(entry.fourcc)
    buf[insert_at:insert_at] = sinf
    buf[entry.start + 4:entry.start + 8] = b"encv"

    # The sample entry swallows the new box, and every ancestor that ends
    # exactly at the insertion point grows with it.
    grown = [entry.start]

    def walk(start, end):
        for fourcc, pos, header, size in mp4.iter_boxes(bytes(buf), start, end):
            if pos < insert_at <= pos + size and fourcc in (
                    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"):
                grown.append(pos)
            if fourcc in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                walk(pos + header, pos + size)

    walk(0, len(buf))
    assert len(grown) == 7, (
        "expected the entry plus moov/trak/mdia/minf/stbl/stsd to grow")
    for pos in grown:
        struct.pack_into(">I", buf, pos,
                         struct.unpack_from(">I", buf, pos)[0] + len(sinf))

    open(dst, "wb").write(bytes(buf))
    return dst


requires_ffmpeg = pytest.mark.skipif(
    media_transcode.find_ffmpeg() is None, reason="ffmpeg not installed")


@pytest.fixture
def cenc_clip(tmp_path):
    """A real H.265 clip wearing the CENC labels, or a skip."""
    ffmpeg = media_transcode.find_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    src = str(tmp_path / "raw.mp4")
    code, err = media_transcode._run([
        ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=1",
        "-c:v", "libx265", "-pix_fmt", "yuv420p", src], 120)
    if code != 0:
        pytest.skip(f"libx265 unavailable: {err[:120]}")
    return _wrap_as_cenc(src, str(tmp_path / "cenc.mp4"))


@pytest.fixture
def videos_dir(tmp_path, cenc_clip):
    """A ``videos/`` directory holding one CENC-labelled clip."""
    directory = tmp_path / "videos"
    directory.mkdir(exist_ok=True)
    target = directory / os.path.basename(cenc_clip)
    target.write_bytes(open(cenc_clip, "rb").read())
    return str(directory)


def _use_temp_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "VIDEOS_FIXED_DIR", str(tmp_path / "fixed"))
    monkeypatch.setattr(paths, "VIDEOS_ORIG_DIR", str(tmp_path / "orig"))


# ── detection ─────────────────────────────────────────────────────────────

def test_surgery_reproduces_the_cdn_damage(cenc_clip):
    """Guards the fixture itself: the clip really is CENC-labelled H.265."""
    assert mp4.needs_container_fix(cenc_clip) is True
    assert mp4.video_codec(cenc_clip) == "hevc"
    assert "sinf" in mp4.cenc_labels(cenc_clip)["boxes"]


def test_plan_lists_only_cenc_labelled_files(videos_dir, tmp_path):
    (tmp_path / "videos" / "clean.mp4").write_bytes(b"\x00" * 32)
    planned = fixer._plan(videos_dir)
    assert [os.path.basename(p) for p in planned] == ["cenc.mp4"]
    assert all(mp4.needs_container_fix(p) for p in planned)


def test_dry_run_changes_nothing(videos_dir, tmp_path, monkeypatch, capsys):
    _use_temp_outputs(monkeypatch, tmp_path)
    before = {
        name: os.path.getmtime(os.path.join(videos_dir, name))
        for name in os.listdir(videos_dir)
    }
    assert fixer.main(["--dir", videos_dir]) == 0
    assert not os.path.exists(paths.VIDEOS_FIXED_DIR)
    after = {
        name: os.path.getmtime(os.path.join(videos_dir, name))
        for name in os.listdir(videos_dir)
    }
    assert before == after
    assert "预演" in capsys.readouterr().out


def test_nothing_to_do_is_not_an_error(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert fixer.main(["--dir", str(empty)]) == 0
    assert "没有需要规范化" in capsys.readouterr().out


# ── rewriting (needs a real ffmpeg) ───────────────────────────────────────

@requires_ffmpeg
def test_apply_writes_a_clean_copy_and_leaves_the_original(videos_dir, tmp_path,
                                                           monkeypatch):
    _use_temp_outputs(monkeypatch, tmp_path)
    name = os.listdir(videos_dir)[0]
    source = os.path.join(videos_dir, name)
    before = open(source, "rb").read()

    assert fixer.main(["--dir", videos_dir, "--apply"]) == 0

    fixed = os.path.join(paths.VIDEOS_FIXED_DIR, name)
    assert os.path.isfile(fixed)
    assert mp4.needs_container_fix(fixed) is False
    assert mp4.video_codec(fixed) == "hevc"
    assert mp4.duration_seconds(fixed) == pytest.approx(
        mp4.duration_seconds(source), abs=0.2)
    assert media_transcode.decode_problem(
        media_transcode.find_ffmpeg(), fixed) is None
    # --apply must not touch the source.
    assert open(source, "rb").read() == before
    assert not os.path.exists(paths.VIDEOS_ORIG_DIR)


@requires_ffmpeg
def test_replace_swaps_in_place_and_keeps_the_original(videos_dir, tmp_path,
                                                       monkeypatch):
    _use_temp_outputs(monkeypatch, tmp_path)
    name = os.listdir(videos_dir)[0]
    source = os.path.join(videos_dir, name)
    before = open(source, "rb").read()

    assert fixer.main(["--dir", videos_dir, "--replace"]) == 0

    assert mp4.needs_container_fix(source) is False
    assert mp4.video_codec(source) == "hevc"
    backup = os.path.join(paths.VIDEOS_ORIG_DIR, name)
    assert open(backup, "rb").read() == before
    assert mp4.needs_container_fix(backup) is True


@requires_ffmpeg
def test_only_limits_the_batch(videos_dir, tmp_path, monkeypatch, capsys):
    _use_temp_outputs(monkeypatch, tmp_path)
    other = os.path.join(videos_dir, "other.mp4")
    open(other, "wb").write(open(os.path.join(videos_dir, os.listdir(videos_dir)[0]), "rb").read())

    assert fixer.main(["--dir", videos_dir, "--apply", "--only", "other.mp4"]) == 0
    assert os.listdir(paths.VIDEOS_FIXED_DIR) == ["other.mp4"]


# ── verification rejects bad rewrites ─────────────────────────────────────

def test_verification_rejects_surviving_labels(tmp_path, monkeypatch):
    source = _write_stub(tmp_path, "src.mp4", b"hvc1")
    same = _write_stub(tmp_path, "same.mp4", b"hvc1")
    monkeypatch.setattr(mp4, "needs_container_fix", lambda _p: True)
    monkeypatch.setattr(media_transcode, "decode_problem", lambda *a: None)
    assert "survived" in fixer._verify("ffmpeg", source, same)


def test_verification_rejects_a_codec_change(tmp_path, monkeypatch):
    source = _write_stub(tmp_path, "src.mp4", b"x")
    dest = _write_stub(tmp_path, "dst.mp4", b"x")
    monkeypatch.setattr(mp4, "needs_container_fix", lambda _p: False)
    monkeypatch.setattr(mp4, "video_codec",
                        lambda p: "hevc" if p == source else "h264")
    assert "codec changed" in fixer._verify("ffmpeg", source, dest)


def test_verification_rejects_a_shortened_duration(tmp_path, monkeypatch):
    source = _write_stub(tmp_path, "src.mp4", b"x")
    dest = _write_stub(tmp_path, "dst.mp4", b"x")
    monkeypatch.setattr(mp4, "needs_container_fix", lambda _p: False)
    monkeypatch.setattr(mp4, "video_codec", lambda _p: "hevc")
    monkeypatch.setattr(mp4, "duration_seconds",
                        lambda p: 10.0 if p == source else 4.0)
    assert "duration changed" in fixer._verify("ffmpeg", source, dest)


def _stub_comparisons(monkeypatch, decode_report):
    monkeypatch.setattr(mp4, "needs_container_fix", lambda _p: False)
    monkeypatch.setattr(mp4, "video_codec", lambda _p: "hevc")
    monkeypatch.setattr(mp4, "duration_seconds", lambda _p: 1.0)
    monkeypatch.setattr(media_transcode, "decode_report", decode_report)


# The exact message Douyin's broken-timestamp clips produce; only the sample
# indices differ between files, which is why kinds erase the digits.
_DTS_COMPLAINT = ("Application provided invalid, non monotonically increasing "
                  "dts to muxer in stream 0: 191 >= 191")


def test_verification_surfaces_new_decode_errors(tmp_path, monkeypatch):
    source = _write_stub(tmp_path, "src.mp4", b"x")
    dest = _write_stub(tmp_path, "dst.mp4", b"x")
    _stub_comparisons(monkeypatch, lambda _ff, p: (
        (0, []) if p == source else (0, ["Invalid NAL unit size"])))
    assert "Invalid NAL unit" in fixer._verify("ffmpeg", source, dest)


def test_verification_forgives_a_complaint_the_source_also_raises(tmp_path,
                                                                  monkeypatch):
    """A stream copy cannot fix upstream timestamps — and must not be blamed."""
    source = _write_stub(tmp_path, "src.mp4", b"x")
    dest = _write_stub(tmp_path, "dst.mp4", b"x")
    _stub_comparisons(monkeypatch, lambda _ff, p: (0, [_DTS_COMPLAINT]))
    assert fixer._verify("ffmpeg", source, dest) is None


def test_verification_still_fails_when_the_same_kind_gains_new_lines(
        tmp_path, monkeypatch):
    source = _write_stub(tmp_path, "src.mp4", b"x")
    dest = _write_stub(tmp_path, "dst.mp4", b"x")
    _stub_comparisons(monkeypatch, lambda _ff, p: (
        (0, [_DTS_COMPLAINT]) if p == source
        else (0, [_DTS_COMPLAINT, "Invalid NAL unit size"])))
    assert "Invalid NAL unit" in fixer._verify("ffmpeg", source, dest)


def test_verification_rejects_an_empty_output(tmp_path):
    source = _write_stub(tmp_path, "src.mp4", b"x")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert fixer._verify("ffmpeg", source, str(empty)) == "empty output"


def test_a_failing_file_is_reported_and_left_alone(videos_dir, tmp_path,
                                                   monkeypatch, capsys):
    _use_temp_outputs(monkeypatch, tmp_path)
    monkeypatch.setattr(fixer, "_remux", lambda *a, **k: "boom")
    assert fixer.main(["--dir", videos_dir, "--apply"]) == 1
    out = capsys.readouterr().out
    assert "失败" in out and "成功 0 / 失败 1" in out
    assert not os.path.exists(paths.VIDEOS_FIXED_DIR) or not os.listdir(
        paths.VIDEOS_FIXED_DIR)
