"""Backfill self-recorded IM videos.

Flow:
  1. Playwright opens /chat once (loads session cookies + warms SDK).
  2. POST /aweme/v1/web/maya/story/batch_play_info/v1/ in page context
     to batch-resolve N tos_keys → signed CDN URLs (~10 vids per call).
  3. urllib downloads the encrypted bytes from each URL.
  4. extractor.cenc decrypts the MPEG-CENC AES-128-CTR samples in-place
     using cj.video.skey (16 bytes), giving a playable mp4.
  5. Save to data/media/videos/<msg_id>.mp4 and update DB.

No UI scrolling, no clicking, no Web Worker, no wasm. Just HTTPS + Python.
"""
import asyncio
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.im_media import collect_cenc_jobs
from extractor.web_scraper import WebChatScraper
from extractor.models import get_db
from extractor.cenc import decrypt_cenc_mp4


VIDEOS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "media", "videos",
)

BATCH_SIZE = 10
BATCH_API_PATH = (
    "/aweme/v1/web/maya/story/batch_play_info/v1/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web"
    "&app_name=douyin_web&pc_client_type=1"
)


class JobStopped(Exception):
    """Raised by the ``gate`` callback when the panel asks the job to stop.

    ``save_cenc_jobs`` turns it into a normal partial result so the caller can
    close the browser and report "stopped" instead of crashing.
    """


def _msg_video(msg) -> dict | None:
    """Returns cj.video dict if present (has tkey + skey)."""
    try:
        ro = json.loads(msg["raw_data"])
        cj = json.loads(ro.get("content_json", "{}"))
        v = cj.get("video") or {}
        if v.get("tkey") and v.get("skey"):
            return v
    except Exception:
        pass
    return None


def pending_videos(conn, conv_id=None, limit=None):
    """Video messages missing a working local mp4 (with cj.video.tkey present)."""
    sql = """SELECT msg_id, conv_id, timestamp, raw_data, media_local_path
             FROM messages
             WHERE (msg_type = 5 OR (raw_data LIKE '%tkey%' AND raw_data LIKE '%poster%'))
               AND (media_local_path IS NULL OR media_local_path = ''
                    OR media_local_path NOT LIKE '%.mp4')"""
    args = []
    if conv_id:
        sql += " AND conv_id = ?"
        args.append(conv_id)
    sql += " ORDER BY timestamp DESC"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        if _msg_video(r):
            out.append(r)
            if limit and len(out) >= limit:
                return out
    extra_sql = "SELECT msg_id, raw_data FROM messages WHERE raw_data LIKE '%tkey%'"
    extra_args = []
    if conv_id:
        extra_sql += " AND conv_id = ?"
        extra_args.append(conv_id)
    seen = {r["msg_id"] for r in out}
    for row in conn.execute(extra_sql, extra_args):
        for job in collect_cenc_jobs(row["raw_data"]):
            msg_id = job["msg_id"]
            if msg_id in seen:
                continue
            seen.add(msg_id)
            out.append({
                "msg_id": msg_id, "conv_id": "", "timestamp": 0,
                "raw_data": json.dumps({
                    "content_json": json.dumps({
                        "video": {"tkey": job["tkey"], "skey": job["skey"]},
                    })
                }),
                "media_local_path": None, "file_id": job["file_id"],
            })
            if limit and len(out) >= limit:
                return out
    return out


def reset_local_paths(conn):
    """Clear media_local_path for all video messages so they re-enter pending."""
    cur = conn.execute(
        """UPDATE messages SET media_local_path = NULL
           WHERE media_local_path LIKE 'videos/%.mp4'"""
    )
    conn.commit()
    return cur.rowcount


def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


async def _resolve_batch(page, tkeys: list[str]) -> dict[str, str]:
    """Resolve a batch of tkeys → main_url. Returns {tkey: url}."""
    result = await page.evaluate(
        r"""async ({path, tkeys}) => {
            try {
                const body = JSON.stringify({
                    req_infos: tkeys.map(k => ({ item_id: 0, tos_key: k, type: 2 })),
                    with_caption: true,
                });
                const r = await fetch(path, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body,
                });
                return { status: r.status, body: await r.text() };
            } catch (e) { return { err: String(e) }; }
        }""", {"path": BATCH_API_PATH, "tkeys": tkeys},
    )
    if result.get("err") or result.get("status") != 200:
        return {}
    try:
        payload = json.loads(result["body"])
    except Exception:
        return {}
    if payload.get("err_no") != 0:
        return {}
    out = {}
    infos = (payload.get("data") or {}).get("play_infos") or []
    for tkey, info in zip(tkeys, infos):
        eu = (info or {}).get("encrypted_url") or {}
        url = eu.get("main_url") or eu.get("backup_url")
        if url:
            out[tkey] = url
    return out


def _faststart_mp4(path: str) -> None:
    """Move moov to the front so HTML5 <video> can start playback without
    reading the whole file. ffmpeg remux only — no re-encode."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return
    tmp = path + ".faststart.mp4"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp],
            capture_output=True, timeout=60,
        )
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
        else:
            try: os.remove(tmp)
            except OSError: pass
    except Exception:
        try: os.remove(tmp)
        except OSError: pass


def _file_id_for(msg):
    if isinstance(msg, dict) and msg.get("file_id"):
        return str(msg["file_id"])
    from extractor.im_media import media_file_id
    return media_file_id(msg["msg_id"]) or str(msg["msg_id"]).replace("/", "_")


async def save_cenc_jobs(page, jobs, *, conn=None, progress_cb=None,
                         batch_size=BATCH_SIZE, gate=None):
    """Resolve tkeys on a logged-in page, decrypt, and write videos/<file_id>.mp4.

    ``gate`` is an optional async callable awaited before each video. The panel
    uses it to pause the job while an error dialog is open, and to raise
    ``JobStopped`` when the user stops the download; both end up as a normal
    (partial) result instead of an exception.
    """
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    log = []
    ok = fail = skipped = 0
    last_error = ""
    if not jobs:
        return {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "log": log}

    by_tkey: dict[str, list[tuple[str, str, str]]] = {}
    for job in jobs:
        file_id = job.get("file_id") or job.get("msg_id")
        by_tkey.setdefault(job["tkey"], []).append((job.get("msg_id"), job["skey"], file_id))

    tkeys = list(by_tkey.keys())
    total = sum(len(v) for v in by_tkey.values())
    for batch_idx in range(0, len(tkeys), batch_size):
        batch = tkeys[batch_idx:batch_idx + batch_size]
        urls = await _resolve_batch(page, batch)
        log.append(f"  batch {batch_idx // batch_size + 1}: {len(batch)} req → {len(urls)} URL")
        for tkey in batch:
            url = urls.get(tkey)
            for msg_id, skey, file_id in by_tkey[tkey]:
                if gate is not None:
                    try:
                        await gate()
                    except JobStopped:
                        log.append("  [!] stopped by user")
                        return {"total": total, "ok": ok, "fail": fail, "skipped": skipped,
                                "log": log, "stopped": True}
                out_rel = f"videos/{file_id}.mp4"
                out_abs = os.path.join(VIDEOS_DIR, f"{file_id}.mp4")
                if os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
                    if conn is not None and msg_id and "/" not in str(msg_id):
                        conn.execute("UPDATE messages SET media_local_path=? WHERE msg_id=?",
                                     (out_rel, msg_id))
                        conn.commit()
                    skipped += 1
                    continue
                if not url:
                    fail += 1
                    last_error = f"{file_id}: no URL from resolver"
                    log.append(f"    [-] {last_error}")
                    if progress_cb:
                        progress_cb({
                            "ok": ok, "fail": fail, "skipped": skipped,
                            "total": total, "current": str(file_id),
                            "last_error": last_error,
                        })
                    continue
                try:
                    enc = await asyncio.to_thread(_download, url, 60)
                    size = await asyncio.to_thread(_process_one, enc, skey, out_abs)
                    if conn is not None and msg_id and "/" not in str(msg_id):
                        conn.execute(
                            "UPDATE messages SET media_local_path=? WHERE msg_id=?",
                            (out_rel, msg_id),
                        )
                        conn.commit()
                    ok += 1
                    log.append(f"    [+] {file_id} ({size // 1024} KB)")
                except Exception as e:
                    fail += 1
                    last_error = f"{file_id}: {e}"
                    log.append(f"    [-] {last_error}")
                if progress_cb:
                    progress_cb({
                        "ok": ok, "fail": fail, "skipped": skipped,
                        "total": total, "current": str(file_id),
                        "last_error": last_error,
                    })
    return {"total": total, "ok": ok, "fail": fail, "skipped": skipped, "log": log}


def _process_one(enc_bytes: bytes, skey_hex: str, out_path: str) -> int:
    """Decrypt + save (with moov-at-front faststart). Returns size on disk."""
    if len(enc_bytes) < 1024 or enc_bytes[4:8] != b'ftyp':
        raise ValueError(f"bad mp4 ({len(enc_bytes)}B magic={enc_bytes[:8].hex()})")
    plain = decrypt_cenc_mp4(enc_bytes, skey_hex)
    with open(out_path, "wb") as f:
        f.write(plain)
    # In-place CENC decrypt preserves the unfaststart layout; remux to put moov first.
    _faststart_mp4(out_path)
    return os.path.getsize(out_path)


async def backfill(conv_id: str | None = None, limit: int | None = None,
                   batch_size: int = BATCH_SIZE, progress_cb=None,
                   gate=None) -> dict:
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    conn = get_db()

    pending = pending_videos(conn, conv_id=conv_id, limit=limit)
    log = [f"[*] {len(pending)} pending videos"]
    if not pending:
        return {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "log": log}

    jobs = []
    for m in pending:
        v = _msg_video(m)
        if not v:
            continue
        jobs.append({
            "msg_id": m["msg_id"],
            "file_id": m["file_id"] if isinstance(m, dict) and m.get("file_id") else _file_id_for(m),
            "tkey": v["tkey"],
            "skey": v["skey"],
        })
    log.append(f"[*] {len({j['tkey'] for j in jobs})} unique tkeys")

    s = WebChatScraper()
    await s.launch()
    if not await s.wait_for_login():
        log.append("[-] login required")
        return {"total": len(jobs), "ok": 0, "fail": 0, "skipped": 0, "log": log}
    page = s.page

    log.append("[*] warming SDK at /chat")
    await page.goto("https://www.douyin.com/chat", wait_until="commit", timeout=30000)
    await asyncio.sleep(6)

    log.append(f"[*] resolving + decrypting in batches of {batch_size}")
    result = await save_cenc_jobs(
        page, jobs, conn=conn, progress_cb=progress_cb, batch_size=batch_size,
        gate=gate,
    )

    try:
        await asyncio.wait_for(s.close(), timeout=10)
    except Exception:
        pass

    log.extend(result.get("log") or [])
    if result.get("stopped"):
        log.append(f"[!] stopped: ok={result['ok']} fail={result['fail']} skipped={result['skipped']}")
    else:
        log.append(f"[+] done: ok={result['ok']} fail={result['fail']} skipped={result['skipped']}")
    return {**result, "log": log}


async def main():
    """CLI:
      backfill all pending:  `python -m extractor.video_downloader`
      backfill N:            `python -m extractor.video_downloader N`
      reset DB+files:        `python -m extractor.video_downloader --reset`
    """
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        conn = get_db()
        n = reset_local_paths(conn)
        print(f"[*] cleared media_local_path on {n} rows")
        if os.path.isdir(VIDEOS_DIR):
            files = [f for f in os.listdir(VIDEOS_DIR) if f.endswith(".mp4")]
            for f in files:
                try: os.unlink(os.path.join(VIDEOS_DIR, f))
                except OSError: pass
            print(f"[*] deleted {len(files)} mp4 files")
        return

    limit = None
    if len(sys.argv) > 1:
        try: limit = int(sys.argv[1])
        except ValueError: pass

    def cb(p):
        print(f"  → ok={p['ok']} fail={p['fail']} skipped={p['skipped']} / {p['total']}", flush=True)

    result = await backfill(limit=limit, progress_cb=cb)
    print("\n=== log (tail) ===")
    for line in result["log"][-15:]:
        print(line)
    print(f"\n=== summary ===")
    print(f"  total={result['total']} ok={result['ok']} fail={result['fail']} skipped={result['skipped']}")


if __name__ == "__main__":
    asyncio.run(main())
