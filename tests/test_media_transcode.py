"""Tests for the on-demand H.264 transcoder and the ``/media`` mount.

Two layers are covered separately, because they fail in different ways:

* the **probe** (``common/mp4.py`` + ``media_transcode``) is driven with
  hand-built MP4s, so it runs everywhere and pins the exact byte layout of a
  ``stsd`` entry — the off-by-eight that made ``frma`` unwrapping silently
  return "unknown codec" is exactly the kind of bug this locks down;
* the **conversion** needs a real ffmpeg and is skipped without one.
"""
import os
import struct
import threading
import time

import pytest

from backend import media_files, media_transcode
from common import mp4, paths


# ── synthetic MP4 construction ────────────────────────────────────────────

def _box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


def _full_box(fourcc: bytes, body: bytes) -> bytes:
    return _box(fourcc, b"\x00\x00\x00\x00" + body)


def _visual_entry(fourcc: bytes, extra_children: bytes = b"") -> bytes:
    # reserved(6) + data_reference_index(2) + the 70-byte visual header
    payload = b"\x00" * 6 + b"\x00\x01" + b"\x00" * 70
    return _box(fourcc, payload + extra_children)


def _audio_entry(fourcc: bytes, extra_children: bytes = b"") -> bytes:
    # reserved(6) + data_reference_index(2) + the 20-byte audio header
    payload = b"\x00" * 6 + b"\x00\x01" + b"\x00" * 20
    return _box(fourcc, payload + extra_children)


def _sinf(original_fourcc: bytes) -> bytes:
    return _box(b"sinf", _box(b"frma", original_fourcc))


def _mvhd(timescale: int, duration: int) -> bytes:
    body = struct.pack(">II", 0, 0)                 # creation, modification
    body += struct.pack(">II", timescale, duration)  # timescale, duration
    body += struct.pack(">I", 0x00010000)            # rate
    body += struct.pack(">H", 0x0100)                # volume
    body += b"\x00" * 10 + b"\x00" * 36 + b"\x00" * 24
    body += struct.pack(">I", 0xFFFFFFFF)            # next_track_id
    return _full_box(b"mvhd", body)


def _synthetic_mp4(video_entry: bytes, audio_entry: bytes | None = None,
                   duration_s: float = 3.0) -> bytes:
    entries = video_entry + (audio_entry or b"")
    stsd = _full_box(b"stsd", struct.pack(">I", 1 + (1 if audio_entry else 0)) + entries)
    stbl = _box(b"stbl", stsd)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", minf)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", _mvhd(1000, int(duration_s * 1000)) + trak)
    return _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00") + moov


def _write(tmp_path, name, data) -> str:
    target = tmp_path / name
    target.write_bytes(data)
    return str(target)


# ── the probe ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fourcc, expected", [
    (b"avc1", "h264"),
    (b"hvc1", "hevc"),
    (b"hev1", "hevc"),
])
def test_plain_sample_entry_is_read(tmp_path, fourcc, expected):
    path = _write(tmp_path, "plain.mp4", _synthetic_mp4(_visual_entry(fourcc)))
    assert mp4.video_codec(path) == expected
    assert mp4.needs_container_fix(path) is False


def test_encv_entry_is_unwrapped_through_frma(tmp_path):
    """The whole point: ``encv`` must report the codec from ``frma``."""
    path = _write(tmp_path, "wrapped.mp4",
                  _synthetic_mp4(_visual_entry(b"encv", _sinf(b"hvc1"))))
    assert mp4.video_codec(path) == "hevc"
    assert mp4.needs_container_fix(path) is True
    assert "encv" in mp4.cenc_labels(path)["entries"]


def test_audio_track_is_read_and_unwrapped(tmp_path):
    path = _write(tmp_path, "both.mp4", _synthetic_mp4(
        _visual_entry(b"hvc1"), _audio_entry(b"enca", _sinf(b"mp4a"))))
    assert mp4.video_codec(path) == "hevc"
    assert mp4.audio_codec(path) == "aac"
    assert mp4.needs_container_fix(path) is True


def test_cenc_entry_without_frma_reports_unknown(tmp_path):
    """No ``frma`` means nothing can be concluded — never guess."""
    path = _write(tmp_path, "nofrma.mp4", _synthetic_mp4(_visual_entry(b"encv")))
    assert mp4.video_codec(path) is None
    assert mp4.needs_container_fix(path) is True


def test_duration_is_read_from_mvhd(tmp_path):
    path = _write(tmp_path, "dur.mp4",
                  _synthetic_mp4(_visual_entry(b"avc1"), duration_s=7.5))
    assert mp4.duration_seconds(path) == pytest.approx(7.5)


def test_truncated_file_stops_instead_of_raising(tmp_path):
    data = _synthetic_mp4(_visual_entry(b"hvc1"))
    path = _write(tmp_path, "cut.mp4", data[:40])
    assert mp4.sample_entries(mp4.read_probe_bytes(path)) == []


# ── needs / cache ─────────────────────────────────────────────────────────

def test_hevc_needs_transcode_and_h264_does_not(tmp_path):
    hevc = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    h264 = _write(tmp_path, "a.mp4", _synthetic_mp4(_visual_entry(b"avc1")))
    assert media_transcode.needs_transcode(hevc) is True
    assert media_transcode.needs_transcode(h264) is False


def test_cache_path_invalidates_when_the_source_changes(tmp_path):
    path = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    first = media_transcode.cache_path(path)
    assert media_transcode.cache_path(path) == first

    time.sleep(0.01)
    with open(path, "ab") as handle:
        handle.write(b"\x00" * 8)
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert media_transcode.cache_path(path) != first


def test_cached_path_lives_in_the_transcode_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    path = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    assert os.path.dirname(media_transcode.cached_path(path)) == str(tmp_path / "tc")
    assert media_transcode.cached_path(path).endswith(".mp4")


# ── ensure_transcoded error handling ──────────────────────────────────────

def test_missing_source_returns_error_not_exception(tmp_path):
    result = media_transcode.ensure_transcoded(str(tmp_path / "nope.mp4"))
    assert result["ok"] is False
    assert "no such file" in result["error"]


def test_playable_source_is_returned_untouched(tmp_path):
    path = _write(tmp_path, "a.mp4", _synthetic_mp4(_visual_entry(b"avc1")))
    result = media_transcode.ensure_transcoded(path)
    assert result["ok"] is True
    assert result["path"] == path
    assert result["converted"] is False
    assert result["skipped"] == "already playable"


def test_without_ffmpeg_the_caller_gets_a_structured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(media_transcode, "find_ffmpeg", lambda: None)
    path = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    result = media_transcode.ensure_transcoded(path)
    assert result["ok"] is False
    assert "ffmpeg not found" in result["error"]
    assert result["path"] is None


def test_over_long_sources_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(media_transcode, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(media_transcode.mp4, "duration_seconds",
                        lambda _p: media_transcode.MAX_SOURCE_SECONDS + 1)
    path = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    result = media_transcode.ensure_transcoded(path)
    assert result["ok"] is False
    assert "too long" in result["error"]


def test_lock_file_excludes_a_second_holder(tmp_path):
    lock = str(tmp_path / "x.lock")
    with media_transcode._ProcessLock(lock, timeout=0.5):
        with pytest.raises(TimeoutError):
            with media_transcode._ProcessLock(lock, timeout=0.3):
                pass
    # Released on the way out, so the next holder gets in.
    with media_transcode._ProcessLock(lock, timeout=0.5):
        pass
    assert not os.path.exists(lock)


# ── encoder discovery ─────────────────────────────────────────────────────
#
# Regression: the first version looked for the encoder list on stderr, but
# ffmpeg prints `-encoders` to stdout. It therefore always answered "no", which
# silently skipped the whole fallback chain — fatal on a build without libx264
# (JianyingPro's, the documented fallback). See fix.md 2026-09-27 第二轮.

class _Completed:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


_ENCODER_LISTING = (
    b"Encoders:\n"
    b" V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC\n"
    b" V....D h264_mf              H264 via MediaFoundation\n"
    b" V..... h264_qsv             H.264 / AVC / MPEG-4 AVC\n"
)


def test_encoder_detection_reads_the_stdout_listing(monkeypatch):
    monkeypatch.setattr(media_transcode.subprocess, "run",
                        lambda *a, **k: _Completed(stdout=_ENCODER_LISTING))
    assert media_transcode._encoder_available("ffmpeg", "libx264") is True
    assert media_transcode._encoder_available("ffmpeg", "h264_mf") is True
    assert media_transcode._encoder_available("ffmpeg", "h264_nvenc") is False


def test_encoder_detection_does_not_mistake_stderr_for_the_listing(monkeypatch):
    """Pins the stdout-not-stderr mistake so it cannot come back."""
    monkeypatch.setattr(media_transcode.subprocess, "run",
                        lambda *a, **k: _Completed(stderr=_ENCODER_LISTING))
    assert media_transcode._encoder_available("ffmpeg", "libx264") is False


def test_encoder_detection_survives_a_broken_ffmpeg(monkeypatch):
    monkeypatch.setattr(media_transcode.subprocess, "run",
                        lambda *a, **k: _Completed(returncode=1))
    assert media_transcode._encoder_available("ffmpeg", "libx264") is False

    def boom(*_a, **_k):
        raise OSError("no such file")
    monkeypatch.setattr(media_transcode.subprocess, "run", boom)
    assert media_transcode._encoder_available("ffmpeg", "libx264") is False


def test_chain_skips_encoders_the_build_lacks(tmp_path, monkeypatch):
    """Only h264_mf exists → it must be the one actually invoked."""
    path = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    monkeypatch.setattr(media_transcode, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(media_transcode.mp4, "duration_seconds", lambda _p: 1.0)
    monkeypatch.setattr(media_transcode, "_encoder_available",
                        lambda _ff, name: name == "h264_mf")
    calls = []

    def fake_convert(_ff, _src, dest, encoder, _extra, _duration):
        calls.append(encoder)
        with open(dest, "wb") as handle:
            handle.write(b"stub")

    monkeypatch.setattr(media_transcode, "_convert", fake_convert)
    result = media_transcode.ensure_transcoded(path)
    assert calls == ["h264_mf"]
    assert result["ok"] is True
    assert result["encoder"] == "h264_mf"


def test_a_build_with_no_h264_encoder_says_so(tmp_path, monkeypatch):
    path = _write(tmp_path, "v.mp4", _synthetic_mp4(_visual_entry(b"hvc1")))
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    monkeypatch.setattr(media_transcode, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(media_transcode.mp4, "duration_seconds", lambda _p: 1.0)
    monkeypatch.setattr(media_transcode, "_encoder_available", lambda _ff, _n: False)
    result = media_transcode.ensure_transcoded(path)
    assert result["ok"] is False
    assert "没有任何可用的 H.264 编码器" in result["error"]


# ── decode validation ─────────────────────────────────────────────────────

def test_message_kinds_erases_the_sample_indices():
    """The same defect must compare equal across files; only the numbers differ."""
    one = "Application provided invalid, non monotonically increasing dts to muxer in stream 0: 3 >= 3"
    two = one.replace("3 >= 3", "191 >= 191")
    assert media_transcode.message_kinds([one]) == media_transcode.message_kinds([two])


def test_message_kinds_erases_the_heap_address_in_the_context_tag():
    """ffmpeg's "[null @ 0x...] " prefix differs on every single run."""
    one = "[null @ 00000289c1e1f980] non monotonically increasing dts"
    two = "[null @ 0000023c01ba3ac0] non monotonically increasing dts"
    assert media_transcode.message_kinds([one]) == media_transcode.message_kinds([two])


def test_decode_problem_excuses_allowed_complaints(monkeypatch):
    line = "Application provided invalid, non monotonically increasing dts to muxer in stream 0: 3 >= 3"
    monkeypatch.setattr(media_transcode, "decode_report", lambda _f, _p: (0, [line]))
    assert media_transcode.decode_problem("ffmpeg", "x.mp4") == line

    allowed = media_transcode.message_kinds([line])
    assert media_transcode.decode_problem("ffmpeg", "x.mp4", allow=allowed) is None


def test_decode_problem_always_reports_a_nonzero_exit(monkeypatch):
    """An allow-list must never turn a crash into a pass."""
    monkeypatch.setattr(media_transcode, "decode_report",
                        lambda _f, _p: (1, ["whatever"]))
    assert "ffmpeg exited 1" in media_transcode.decode_problem(
        "ffmpeg", "x.mp4", allow=media_transcode.message_kinds(["whatever"]))


# ── the /media mount ──────────────────────────────────────────────────────

@pytest.mark.parametrize("query, expected", [
    (b"", False),
    (b"v=2", False),
    (b"tc=1", True),
    (b"a=1&tc=1", True),
    (b"tc=1&a=1", True),
    (b"tc=0", True),          # presence is the signal; the value is ignored
    (b"tcp=1", False),
])
def test_wants_transcode_reads_the_query_string(query, expected):
    assert media_files._wants_transcode({"query_string": query}) is expected


def test_wants_transcode_tolerates_missing_scope_key():
    assert media_files._wants_transcode({}) is False


def test_media_mount_serves_plain_files_without_a_flag(tmp_path):
    """Without ?tc=1 nothing is converted and lookup behaves as before."""
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "a.mp4").write_bytes(b"\x00" * 16)
    mount = media_files.MediaStaticFiles(directory=str(tmp_path))
    full_path, stat_result = mount.lookup_path("videos/a.mp4")
    assert os.path.basename(full_path) == "a.mp4"
    assert stat_result is not None


# ── the conversion itself (needs a real ffmpeg) ───────────────────────────

requires_ffmpeg = pytest.mark.skipif(
    media_transcode.find_ffmpeg() is None, reason="ffmpeg not installed")


@pytest.fixture
def hevc_clip(tmp_path):
    """A tiny real H.265 clip, or skip when ffmpeg cannot produce one."""
    ffmpeg = media_transcode.find_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    path = str(tmp_path / "src.mp4")
    code, err = media_transcode._run([
        ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=1",
        "-c:v", "libx265", "-pix_fmt", "yuv420p", path], 120)
    if code != 0:
        pytest.skip(f"libx265 unavailable: {err[:120]}")
    return path


@requires_ffmpeg
def test_conversion_produces_a_playable_h264_rendition(hevc_clip, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    assert media_transcode.needs_transcode(hevc_clip) is True

    result = media_transcode.ensure_transcoded(hevc_clip)
    assert result["ok"] is True, result.get("error")
    assert result["converted"] is True
    assert media_transcode.video_codec(result["path"]) == "h264"
    assert not mp4.needs_container_fix(result["path"])
    # The original is never touched.
    assert media_transcode.video_codec(hevc_clip) == "hevc"


@requires_ffmpeg
def test_second_request_is_served_from_the_cache(hevc_clip, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    first = media_transcode.ensure_transcoded(hevc_clip)
    second = media_transcode.ensure_transcoded(hevc_clip)
    assert second["ok"] is True
    assert second["path"] == first["path"]
    assert second["converted"] is False
    assert second["cached"] is True


@requires_ffmpeg
def test_concurrent_requests_convert_once(hevc_clip, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    results, errors = [], []

    def worker():
        try:
            results.append(media_transcode.ensure_transcoded(hevc_clip))
        except Exception as exc:  # pragma: no cover - surfaced via the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert all(r["ok"] for r in results)
    assert len({r["path"] for r in results}) == 1
    assert sum(1 for r in results if r["converted"]) == 1


@requires_ffmpeg
def test_a_broken_rendition_never_reaches_the_cache(hevc_clip, tmp_path, monkeypatch):
    """Validation runs before publish, so a corrupt encode is rejected."""
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    monkeypatch.setattr(media_transcode, "_validate",
                        lambda *a, **k: "simulated corruption")
    result = media_transcode.ensure_transcoded(hevc_clip)
    assert result["ok"] is False
    assert "simulated corruption" in result["error"]
    assert not os.path.exists(media_transcode.cached_path(hevc_clip))


# JianyingPro ships a full FFmpeg 7.x with no libx264 — exactly the build the
# discovery order falls back to on a machine without ffmpeg on PATH, and the
# one that exposed the stderr/stdout bug above.
JIANYING_FFMPEG = r"D:\Program Files (x86)\JianyingPro\11.2.0.14339\ffmpeg.exe"


@pytest.mark.skipif(not os.path.isfile(JIANYING_FFMPEG),
                    reason="剪映自带 ffmpeg 不在本机")
def test_a_build_without_libx264_still_produces_h264(hevc_clip, tmp_path,
                                                     monkeypatch):
    """End-to-end regression for the fallback chain (fix.md 第二轮)."""
    monkeypatch.setattr(paths, "TRANSCODE_VIDEOS_DIR", str(tmp_path / "tc"))
    monkeypatch.setattr(media_transcode, "find_ffmpeg", lambda: JIANYING_FFMPEG)

    assert media_transcode._encoder_available(JIANYING_FFMPEG, "libx264") is False
    assert media_transcode._encoder_available(JIANYING_FFMPEG, "h264_mf") is True

    result = media_transcode.ensure_transcoded(hevc_clip)
    assert result["ok"] is True, result.get("error")
    assert result["encoder"] != "libx264"
    assert media_transcode.video_codec(result["path"]) == "h264"
