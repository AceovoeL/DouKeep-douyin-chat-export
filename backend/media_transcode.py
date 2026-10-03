"""On-demand H.264 renditions for clips the chat viewer cannot decode.

**Why this exists.** Douyin's IM videos are overwhelmingly HEVC/H.265
(``hvc1``): 104 of the 112 files in ``data/media/videos`` at the time of
writing. Chromium — which is what renders 127.0.0.1:8000 — ships no HEVC
decoder of its own and only borrows the OS one, so without the Microsoft
"HEVC 视频扩展" package every clip fails with ``MEDIA_ERR_SRC_NOT_SUPPORTED``.

**Why on demand.** Re-encoding the archive up front costs disk, quality and
hours of CPU for clips nobody opens. Instead the ``<video>`` element retries
once with ``?tc=1`` when it errors, and only that single file is converted
(then cached under ``data/transcoded/videos/``). The second play is instant.

**Correctness rules.** Each of these exists because the alternative lost data
or hammered the machine:

* the original is never modified — the cache is a separate tree;
* one conversion per source file, enforced across both threads *and* processes;
* output is written to a temp file and validated (real H.264, same duration,
  error-free full decode) before ``os.replace`` puts it in the cache, so a
  killed process can never leave a half-written file behind a valid name;
* every encoder in the chain is tried in turn; the first that produces a valid
  file wins, and a structured error is returned when all of them fail so the
  caller can fall back to serving the original.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from common import mp4

# Where to look for ffmpeg, in order. ``DOUYIN_FFMPEG`` lets a user pin an
# exact build; the last two are the builds we know ship on this project's
# machines (JianyingPro bundles a full FFmpeg 7.x).
FFMPEG_ENV_VAR = "DOUYIN_FFMPEG"

_BUNDLED_FFMPEG_GLOBS = (
    os.path.join("tools", "ffmpeg"),
    os.path.join("tools", "ffmpeg", "bin"),
)

_JIANYING_FFMPEG = (
    r"D:\Program Files (x86)\JianyingPro\11.2.0.14339\ffmpeg.exe",
    r"C:\Program Files (x86)\JianyingPro\11.2.0.14339\ffmpeg.exe",
)

# Tried in order; ``libx264`` is software (always available, best quality/
# size), the rest are hardware encoders that exist only on matching GPUs.
ENCODER_CHAIN = (
    ("libx264", ["-preset", "veryfast", "-crf", "23"]),
    ("h264_qsv", ["-global_quality", "24"]),
    ("h264_mf", ["-b:v", "4000k"]),
    ("h264_nvenc", ["-preset", "p4", "-cq", "26"]),
    ("h264_amf", ["-b:v", "4000k"]),
)

# Refuse to burn CPU on very long sources: the viewer is for chat clips, and a
# 40-minute screen recording is a different problem.
MAX_SOURCE_SECONDS = 600

# A full decode of the result is the cheapest way to prove the transcode is not
# subtly corrupt, but it doubles the work — skip it for long inputs.
FULL_DECODE_LIMIT_SECONDS = 180

FFMPEG_TIMEOUT = 900
PROBE_TIMEOUT = 60

_LOCK_STALE_SECONDS = 600
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# ── ffmpeg discovery ──────────────────────────────────────────────────────

def find_ffmpeg() -> str | None:
    """Return a usable ffmpeg path, or ``None`` when none can be found."""
    pinned = os.environ.get(FFMPEG_ENV_VAR)
    if pinned and os.path.isfile(pinned):
        return pinned

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    return _find_bundled_ffmpeg()


def _find_bundled_ffmpeg() -> str | None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    for rel in _BUNDLED_FFMPEG_GLOBS:
        base = os.path.join(repo_root, rel)
        direct = os.path.join(base, exe)
        if os.path.isfile(direct):
            return direct
        # Defensive: a builds downloaded as ffmpeg-<version>/bin/ffmpeg.exe
        if os.path.isdir(base):
            for entry in sorted(os.listdir(base)):
                candidate = os.path.join(base, entry, "bin", exe)
                if os.path.isfile(candidate):
                    return candidate

    for candidate in _JIANYING_FFMPEG:
        if os.path.isfile(candidate):
            return candidate
    return None


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None


# ── what needs converting ─────────────────────────────────────────────────

def video_codec(path: str) -> str | None:
    """Real codec name of ``path``'s video track, CENC labels unwrapped."""
    return mp4.video_codec(path)


def needs_transcode(path: str) -> bool:
    """True when the browser cannot be expected to decode this file."""
    return mp4.video_codec(path) in mp4.HARD_TO_PLAY


def cache_path(src: str) -> str:
    """Cache file for ``src``.

    The name folds in size and mtime so that re-downloading a clip (Douyin
    serves a different bitrate on the next fetch) naturally produces a new
    cache entry instead of serving a stale rendition.
    """
    stat = os.stat(src)
    digest = hashlib.sha256(
        f"{os.path.abspath(src)}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()[:16]
    stem = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(f"{stem}.{digest}.mp4")


def cached_path(src: str) -> str:
    """Absolute path of the cached rendition for ``src``."""
    from common import paths

    return os.path.join(paths.TRANSCODE_VIDEOS_DIR, cache_path(src))


# ── running ffmpeg ────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Run ``cmd``, returning ``(returncode, combined stderr tail)``."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as exc:  # binary vanished, no permission, …
        return 127, str(exc)
    tail = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode, tail[-2000:]


def _thread_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


class _ProcessLock:
    """Cross-process mutex via ``O_CREAT|O_EXCL`` on a lock file.

    uvicorn runs a single worker today, so the in-process lock would do; the
    lock file is what keeps the CLI, a ``--reload`` reload, and the test suite
    from converting the same clip at the same time.
    """

    def __init__(self, path: str, timeout: float = 300.0):
        self.path = path
        self.timeout = timeout

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                # A crashed worker must not block the cache forever.
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > _LOCK_STALE_SECONDS:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {self.path}")
                time.sleep(0.2)

    def __exit__(self, *exc_info):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


# ── the conversion ────────────────────────────────────────────────────────

def ensure_transcoded(src: str, *, force: bool = False) -> dict:
    """Return an H.264 rendition of ``src``, converting it if necessary.

    Never raises for the expected failure modes — the caller is a request
    handler that must degrade to "serve the original" instead of a 500.
    """
    started = time.monotonic()
    if not os.path.isfile(src):
        return _fail(f"no such file: {src}")

    if not force and not needs_transcode(src):
        return {"ok": True, "path": src, "cached": True, "converted": False,
                "codec": mp4.video_codec(src), "skipped": "already playable"}

    destination = cached_path(src)
    if os.path.isfile(destination) and os.path.getsize(destination) > 0:
        return {"ok": True, "path": destination, "cached": True,
                "converted": False, "codec": mp4.video_codec(destination)}

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return _fail("ffmpeg not found (set DOUYIN_FFMPEG or install it)")

    duration = mp4.duration_seconds(src)
    if duration is not None and duration > MAX_SOURCE_SECONDS:
        return _fail(f"source too long to convert on demand ({duration:.0f}s)")

    lock_dir = os.path.dirname(destination) or "."
    os.makedirs(lock_dir, exist_ok=True)
    with _thread_lock(destination):
        with _ProcessLock(destination + ".lock"):
            # Another worker may have finished while we waited.
            if os.path.isfile(destination) and os.path.getsize(destination) > 0:
                return {"ok": True, "path": destination, "cached": True,
                        "converted": False, "codec": mp4.video_codec(destination)}

            tried, errors = 0, []
            for encoder, extra in ENCODER_CHAIN:
                # Listed ≠ usable (h264_qsv is advertised on machines with no
                # Intel GPU), so a candidate that is present is still allowed
                # to fail and fall through to the next one.
                if not _encoder_available(ffmpeg, encoder):
                    continue
                tried += 1
                try:
                    _convert(ffmpeg, src, destination, encoder, extra, duration)
                except _ConvertError as exc:
                    errors.append(f"{encoder}: {exc}")
                    continue
                return {"ok": True, "path": destination, "cached": False,
                        "converted": True, "codec": "h264", "encoder": encoder,
                        "seconds": round(time.monotonic() - started, 2)}

            if not tried:
                return _fail(f"这台 ffmpeg 没有任何可用的 H.264 编码器: {ffmpeg}")
            return _fail("all encoders failed; " + " | ".join(errors))


class _ConvertError(RuntimeError):
    pass


def _encoder_available(ffmpeg: str, encoder: str) -> bool:
    """True when this ffmpeg build was compiled with ``encoder``.

    Deliberately does not go through ``_run``: ffmpeg prints the encoder list
    to **stdout**, while ``_run`` captures stderr (that is where diagnostics
    go). Reading the wrong stream makes this always return False, which
    silently skips every hardware encoder — and on a build without libx264
    (JianyingPro's, the documented fallback) that breaks the whole chain.
    See fix.md 2026-09-27 第二轮.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    pattern = rb"^\s*\S+\s+" + re.escape(encoder.encode()) + rb"\s"
    return re.search(pattern, proc.stdout, re.MULTILINE) is not None


def _convert(ffmpeg: str, src: str, destination: str, encoder: str,
             extra: list[str], duration: float | None) -> None:
    """Encode to a temp file, validate it, then publish it atomically."""
    tmp_dir = os.path.dirname(destination) or "."
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp.mp4", dir=tmp_dir)
    os.close(fd)
    try:
        cmd = [ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
               "-i", src,
               "-map", "0:v:0", "-map", "0:a:0?",
               "-sn", "-dn",
               "-c:v", encoder, *extra,
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart",
               tmp_path]
        code, err = _run(cmd, FFMPEG_TIMEOUT)
        if code != 0:
            raise _ConvertError(err or f"ffmpeg exited {code}")

        problem = _validate(ffmpeg, tmp_path, duration)
        if problem:
            raise _ConvertError(problem)

        os.replace(tmp_path, destination)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _validate(ffmpeg: str, path: str, source_duration: float | None) -> str | None:
    """Return a description of the first problem, or ``None`` when valid."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return "empty output"

    codec = mp4.video_codec(path)
    if codec != "h264":
        return f"unexpected codec {codec!r}"

    out_duration = mp4.duration_seconds(path)
    if source_duration and out_duration:
        # Remux/encode rounding moves the last frame or two; anything larger
        # means samples were dropped.
        if abs(out_duration - source_duration) > max(0.5, source_duration * 0.02):
            return (f"duration mismatch ({out_duration:.2f}s vs "
                    f"{source_duration:.2f}s)")

    check_until = out_duration is None or out_duration <= FULL_DECODE_LIMIT_SECONDS
    if check_until:
        problem = decode_problem(ffmpeg, path)
        if problem:
            return "decode errors: " + problem
    return None


_NUMBER = re.compile(r"\d+")
# ffmpeg prefixes every line with a context tag carrying a heap address, e.g.
# "[null @ 00000289c1e1f980] ". That address changes on every run, so a line
# can only be compared to another once the tag is gone.
_CONTEXT_TAG = re.compile(r"^(?:\[[^\[\]]*\]\s*)+")


def _message_kind(line: str) -> str:
    """Normalise an ffmpeg log line so two runs can be compared."""
    return _NUMBER.sub("#", _CONTEXT_TAG.sub("", line.strip()))


def message_kinds(lines) -> set[str]:
    """Collapse messages to a comparable shape.

    Two things vary between runs of the same defect: the context tag's address
    and the sample indices quoted in the message, so both are erased.
    """
    return {_message_kind(line) for line in lines}


def decode_report(ffmpeg: str, path: str) -> tuple[int, list[str]]:
    """Fully decode ``path``; return ``(exit_code, stderr lines)``.

    Every frame is decoded, which is the cheapest proof that a rewritten
    container still points at intact samples. The lines rather than a boolean
    come back because callers need to compare one file's complaints against
    another's (see ``message_kinds``).
    """
    code, err = _run([ffmpeg, "-hide_banner", "-v", "error",
                      "-i", path, "-f", "null", "-"], FFMPEG_TIMEOUT)
    return code, [line.strip() for line in err.splitlines() if line.strip()]


def decode_problem(ffmpeg: str, path: str, allow=frozenset()) -> str | None:
    """Return the first complaint about ``path`` that ``allow`` does not excuse.

    ``allow`` exists for timestamps that were already broken upstream: a stream
    copy cannot repair them, and Douyin ships a few clips whose DTS values
    repeat. Complaining about those would reject a perfect rewrite.
    """
    code, lines = decode_report(ffmpeg, path)
    if code != 0:
        return f"ffmpeg exited {code}: {' '.join(lines)[:300]}"
    for line in lines:
        if _message_kind(line) not in allow:
            return line
    return None


def _fail(message: str) -> dict:
    return {"ok": False, "path": None, "error": message}


def cache_size_bytes() -> int:
    """Total size of the rendition cache (used by the control panel/tests)."""
    from common import paths

    total = 0
    for root, _dirs, files in os.walk(paths.TRANSCODE_VIDEOS_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
