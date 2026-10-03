#!/usr/bin/env python3
"""Normalize the CENC-labelled MP4s Douyin's CDN hands back.

**The problem.** Some archived clips are stored as MPEG-CENC containers: the
sample entry is named ``encv``/``enca`` and ``moov`` carries ``sinf`` / ``frma``
/ ``schm`` / ``tenc`` / ``senc`` / ``saiz`` / ``saio`` / ``sgpd`` / ``sbgp``.
But ``tenc`` reports ``default_isProtected = 0`` — the sample bytes are plain.
Only the labels lie, and standards-compliant players believe them: Chromium and
Windows Media Foundation refuse the file, while PotPlayer / VLC / 网盘 read past
the labels and play it happily. (2026-09-27: 19 of 112 clips were like this; 2
of them were really H.264, so stripping the labels alone made them playable.)

**How it is fixed.** An ffmpeg stream copy (``-c copy``) rewrites the container
from scratch: same media samples, byte for byte, but a clean sample entry and no
CENC bookkeeping. We chose ffmpeg over a hand-rolled box rewriter because the
sample data is untouched either way and ffmpeg has been doing this for two
decades — the risky part is our own code, so we keep ours to the *validation*
half.

**How a rewrite is trusted.** Three independent checks must pass before an
original is allowed to be replaced:

1. the real codec (``frma``-unwrapped) survives;
2. the duration is unchanged (within frame-rounding tolerance);
3. ffmpeg decodes every frame of the result without a single complaint.

Usage::

    python tools/fix_media_containers.py                  # dry run: what would change
    python tools/fix_media_containers.py --apply          # write to data/videos_fixed/
    python tools/fix_media_containers.py --apply --replace # swap in place, originals kept

``--replace`` copies each original into ``data/videos_orig/`` first, so a bad
batch can always be undone by copying that directory back. Those staging
directories sit next to ``data/media/`` rather than inside it: ``/media`` is
served verbatim over HTTP, and rollback copies must not be published with the
videos.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import media_transcode  # noqa: E402
from common import mp4, paths  # noqa: E402


def _plan(source_dir: str, only: list[str] | None = None) -> list[str]:
    """Files in ``source_dir`` that still carry CENC labels."""
    names = sorted(
        name for name in os.listdir(source_dir)
        if name.lower().endswith(".mp4") and (not only or name in only)
    )
    return [os.path.join(source_dir, name) for name in names
            if mp4.needs_container_fix(os.path.join(source_dir, name))]


def _remux(ffmpeg: str, src: str, dest: str) -> str | None:
    """``ffmpeg -c copy`` ``src`` into ``dest``; return an error string or None."""
    cmd = [ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
           "-i", src, "-map", "0", "-c", "copy",
           "-movflags", "+faststart", dest]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"ffmpeg failed: {exc}"
    if proc.returncode != 0:
        return "ffmpeg: " + proc.stderr.decode("utf-8", "replace").strip()[-300:]
    return None


def _verify(ffmpeg: str, src: str, dest: str) -> str | None:
    """Return an error string when ``dest`` is not a faithful rewrite of ``src``."""
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        return "empty output"

    expected = mp4.video_codec(src)
    actual = mp4.video_codec(dest)
    if actual != expected:
        return f"codec changed: {expected!r} -> {actual!r}"

    if mp4.needs_container_fix(dest):
        return "CENC labels survived the rewrite"

    before, after = mp4.duration_seconds(src), mp4.duration_seconds(dest)
    if before and after and abs(after - before) > max(0.5, before * 0.02):
        return f"duration changed: {before:.2f}s -> {after:.2f}s"

    # A stream copy cannot repair timestamps that were already broken upstream
    # (Douyin ships a few clips with repeating DTS values), so a complaint the
    # source itself raises is not held against the rewrite. Anything *new* is.
    _, source_messages = media_transcode.decode_report(ffmpeg, src)
    allowed = media_transcode.message_kinds(source_messages)
    problem = media_transcode.decode_problem(ffmpeg, dest, allow=allowed)
    if problem:
        return "decode errors: " + problem
    return None


def _fix_one(ffmpeg: str, src: str, *, replace: bool) -> str | None:
    """Rewrite one file. Returns None on success, else an error string."""
    name = os.path.basename(src)
    if replace:
        shutil.os.makedirs(paths.VIDEOS_ORIG_DIR, exist_ok=True)
        backup = os.path.join(paths.VIDEOS_ORIG_DIR, name)
        target_dir = os.path.dirname(src)
    else:
        backup = None
        target_dir = paths.VIDEOS_FIXED_DIR
        os.makedirs(target_dir, exist_ok=True)

    final = os.path.join(target_dir, name)
    fd, tmp = tempfile.mkstemp(suffix=".tmp.mp4", dir=target_dir)
    os.close(fd)
    try:
        error = _remux(ffmpeg, src, tmp)
        if error:
            return error
        error = _verify(ffmpeg, src, tmp)
        if error:
            return error

        if replace:
            if not os.path.exists(backup):
                shutil.copy2(src, backup)
            os.replace(tmp, final)
            tmp = None
        else:
            os.replace(tmp, final)
            tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=paths.VIDEOS_DIR,
                        help="directory to scan (default: data/media/videos)")
    parser.add_argument("--only", action="append", default=None,
                        help="restrict to this file name (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="write fixed copies to data/videos_fixed/")
    parser.add_argument("--replace", action="store_true",
                        help="swap in place (implies --apply; originals kept)")
    args = parser.parse_args(argv)

    targets = _plan(args.dir, args.only)
    if not targets:
        print("没有需要规范化的文件。")
        return 0

    print(f"待处理 {len(targets)} 个文件：")
    for path in targets:
        print(f"  {os.path.basename(path):<34} {os.path.getsize(path):>10}  "
              f"{','.join(mp4.cenc_labels(path)['entries'])}")

    if not (args.apply or args.replace):
        print("\n（这是预演。加 --apply 生成修复副本，加 --replace 就地替换并保留原件。）")
        return 0

    ffmpeg = media_transcode.find_ffmpeg()
    if not ffmpeg:
        print("找不到 ffmpeg：设置 DOUYIN_FFMPEG 或把它装到 PATH。", file=sys.stderr)
        return 2
    print(f"\n使用 ffmpeg: {ffmpeg}\n")

    ok = failed = 0
    for path in targets:
        name = os.path.basename(path)
        error = _fix_one(ffmpeg, path, replace=args.replace)
        if error:
            failed += 1
            print(f"  失败 {name}: {error}")
        else:
            ok += 1
            where = paths.VIDEOS_ORIG_DIR if args.replace else paths.VIDEOS_FIXED_DIR
            print(f"  完成 {name}" + (f"（原件已存 {where}）" if args.replace else ""))

    print(f"\n成功 {ok} / 失败 {failed}")
    if failed:
        print("失败的文件保持原样，可以单独用 --only <文件名> 重试。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
