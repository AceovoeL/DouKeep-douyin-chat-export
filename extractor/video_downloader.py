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

实况图（``aweType=2704``，一张静态封面 + 一段两三秒小视频）走的也是这条流程：它的
小视频字段形状和普通视频一样，只是密钥藏在 ``cj.live_photo_video`` 而不是 ``cj.video``
（见 ``im_media.live_photo_cenc``）。区别只有落地位置：普通视频写 ``media_local_path``，
实况图写 ``live_video_path`` —— 实况图的 ``media_local_path`` 要留给那张静态封面。
"""
import asyncio
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.im_media import (
    cenc_video, collect_cenc_jobs, collect_live_photo_jobs, live_photo_cenc,
    media_file_id,
)
from extractor.web_scraper import WebChatScraper
from extractor.models import get_db, init_db
from extractor.cenc import decrypt_cenc_mp4
from common import paths


VIDEOS_DIR = paths.VIDEOS_DIR

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


def _content_json(raw_data):
    """行的 ``raw_data`` → ``content_json``（双层编码，坏了就当空载荷）。"""
    try:
        ro = json.loads(raw_data)
        cj = json.loads(ro.get("content_json", "{}"))
    except Exception:
        return {}
    return cj if isinstance(cj, dict) else {}


def _cenc_payload(row) -> tuple[str, dict] | None:
    """这条消息里能下载的加密视频：``("video"|"live", {"tkey", "skey"})``。

    ``"live"`` 表示实况图里那段小视频（密钥在 ``live_photo_video``）；
    两者都挑不出来时返回 None（比如只是"引用了一段视频"的文本回复）。
    """
    cj = _content_json(row["raw_data"])
    video = cenc_video(cj)
    if video:
        return "video", video
    live = live_photo_cenc(cj)
    if live:
        return "live", live
    return None


#: 粗筛：普通视频（msg_type=5，或载荷里带 tkey + poster）与实况图（带
#: live_photo_video）。实况图的静态封面在 media_local_path 里，所以不能用
#: 「有没有 mp4」判断它下没下过 —— 那是 live_video_path 的事。
#:
#: 最后那条：实况图的小视频下好了、但静态封面还缺（比如采集时「自动下载图片」
#: 是关着的）也要进队列 —— 封面由 ensure_live_photo_covers 补，不然查看器里
#: 永远只有消息自带的那张模糊缩略图。
_PENDING_WHERE = (
    "(msg_type = 5 OR (raw_data LIKE '%tkey%' AND raw_data LIKE '%poster%') "
    "OR raw_data LIKE '%live_photo_video%') "
    "AND (media_local_path IS NULL OR media_local_path = '' "
    "OR media_local_path NOT LIKE '%.mp4') "
    "AND (live_video_path IS NULL OR live_video_path = '' "
    "OR (raw_data LIKE '%live_photo_video%' "
    "AND (media_local_path IS NULL OR media_local_path = '')))"
)


def _forwarded_cenc_jobs(conn, conv_id, only, seen):
    """合并转发正文里的视频 / 实况图任务（父消息反查出来的虚拟任务）。

    它们没有对应的数据库行（只有 file_id），落地后阅读端靠文件名约定认
    （见 ``extractor/im_media.find_live_photo_video``）。
    """
    sql = "SELECT msg_id, raw_data FROM messages WHERE raw_data LIKE '%tkey%'"
    args = []
    if conv_id:
        sql += " AND conv_id = ?"
        args.append(conv_id)
    jobs = []
    for row in conn.execute(sql, args):
        found = []
        if only != "live":
            found.extend(collect_cenc_jobs(row["raw_data"]))
        if only != "video":
            found.extend(collect_live_photo_jobs(row["raw_data"]))
        for job in found:
            msg_id = job["msg_id"]
            if msg_id in seen:
                continue
            seen.add(msg_id)
            jobs.append({
                "msg_id": msg_id, "file_id": job["file_id"],
                "kind": job.get("kind") or "video",
                "tkey": job["tkey"], "skey": job["skey"],
                "conv_id": "", "timestamp": 0,
            })
    return jobs


def pending_cenc_jobs(conn, conv_id=None, limit=None, only=None):
    """还没落地的加密视频任务（普通聊天视频 + 实况图的小视频 + 合并转发里的两类）。

    ``only`` 可以限定只要 ``"video"`` 或 ``"live"`` 一种。
    """
    sql = (f"SELECT msg_id, conv_id, timestamp, raw_data, media_local_path, "
           f"live_video_path FROM messages WHERE {_PENDING_WHERE}")
    args = []
    if conv_id:
        sql += " AND conv_id = ?"
        args.append(conv_id)
    sql += " ORDER BY timestamp DESC"

    jobs = []
    for r in conn.execute(sql, args):
        payload = _cenc_payload(r)
        if not payload:
            continue
        kind, video = payload
        if only and kind != only:
            continue
        jobs.append({
            "msg_id": r["msg_id"], "file_id": _file_id_for(r), "kind": kind,
            "tkey": video["tkey"], "skey": video["skey"],
            "conv_id": r["conv_id"], "timestamp": r["timestamp"],
            # 实况图的静态封面还没下：补小视频的同一轮里顺手补上
            "needs_cover": kind == "live" and not r["media_local_path"],
        })
        if limit and len(jobs) >= limit:
            return jobs
    jobs.extend(_forwarded_cenc_jobs(conn, conv_id, only, {j["msg_id"] for j in jobs}))
    if limit:
        return jobs[:limit]
    return jobs


def pending_videos(conn, conv_id=None, limit=None):
    """向后兼容的旧名字：面板的「待下载条数」与回填任务都用它。"""
    return pending_cenc_jobs(conn, conv_id=conv_id, limit=limit)


def live_photo_jobs(conn, conv_id=None, limit=None):
    """只要实况图那段小视频的任务（采集收尾时自动补的就是这些）。"""
    return pending_cenc_jobs(conn, conv_id=conv_id, limit=limit, only="live")


def ensure_live_photo_covers(conn, jobs, limit=None):
    """把实况图缺的静态封面补下来（纯 HTTPS，不用浏览器）。返回补好的张数。

    「实况图」是封面 + 小视频两段；只把小视频拉下来，查看器里封面还得退化成消息
    自带的那张模糊缩略图，所以补小视频的同一轮里顺手把封面也补上。任务清单是现成
    的（``jobs`` 里 ``needs_cover`` 的那些），不用再扫一遍库。

    ``conn`` 必须在调用它的线程里用（sqlite 连接不能跨线程），所以调用方要么直接
    调、要么整段丢进线程池。
    """
    from extractor.im_media import download_direct_media

    pending = [j for j in jobs if j.get("needs_cover")]
    if limit:
        pending = pending[:limit]
    done = 0
    for job in pending:
        row = conn.execute("SELECT raw_data FROM messages WHERE msg_id=?",
                           (job["msg_id"],)).fetchone()
        cj = _content_json(row["raw_data"]) if row else {}
        if not cj:
            continue
        path = download_direct_media(cj, job.get("file_id") or job["msg_id"])
        if not path:
            # 链接过期（403）之类：这条这次补不了，下次采集还会再试
            continue
        conn.execute(
            "UPDATE messages SET media_local_path=? WHERE msg_id=? "
            "AND (media_local_path IS NULL OR media_local_path = '')",
            (path, job["msg_id"]),
        )
        done += 1
    if done:
        conn.commit()
    return done


def reset_local_paths(conn):
    """Clear media_local_path for all video messages so they re-enter pending."""
    cur = conn.execute(
        """UPDATE messages SET media_local_path = NULL
           WHERE media_local_path LIKE 'videos/%.mp4'"""
    )
    # 实况图的小视频也躺在 videos/ 里，删文件时同步清掉记录，否则查看器会一直
    # 拿着一个已经不存在的地址，画出一个点不动的「实况」标。
    conn.execute(
        """UPDATE messages SET live_video_path = NULL
           WHERE live_video_path LIKE 'videos/%.mp4'"""
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


def _record_saved(conn, msg_id, kind, rel_path):
    """把下好的文件记到正确的列上。

    实况图的小视频**不能**写 ``media_local_path`` —— 那一列是它的静态封面的位置，
    覆盖掉查看器就只剩一段没有封面的视频了。
    """
    if conn is None or not msg_id or "/" in str(msg_id):
        return
    column = "live_video_path" if kind == "live" else "media_local_path"
    conn.execute(f"UPDATE messages SET {column}=? WHERE msg_id=?", (rel_path, msg_id))
    conn.commit()


async def save_cenc_jobs(page, jobs, *, conn=None, progress_cb=None,
                         batch_size=BATCH_SIZE, gate=None):
    """Resolve tkeys on a logged-in page, decrypt, and write videos/<file_id>.mp4.

    每个任务带 ``kind``：``"video"``（普通聊天视频，写 ``media_local_path``）或
    ``"live"``（实况图的小视频，写 ``live_video_path``）；缺省按普通视频处理。

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

    by_tkey: dict[str, list[tuple[str, str, str, str]]] = {}
    for job in jobs:
        file_id = job.get("file_id") or job.get("msg_id")
        by_tkey.setdefault(job["tkey"], []).append(
            (job.get("msg_id"), job["skey"], file_id, job.get("kind") or "video")
        )

    tkeys = list(by_tkey.keys())
    total = sum(len(v) for v in by_tkey.values())
    for batch_idx in range(0, len(tkeys), batch_size):
        batch = tkeys[batch_idx:batch_idx + batch_size]
        urls = await _resolve_batch(page, batch)
        log.append(f"  batch {batch_idx // batch_size + 1}: {len(batch)} req → {len(urls)} URL")
        for tkey in batch:
            url = urls.get(tkey)
            for msg_id, skey, file_id, kind in by_tkey[tkey]:
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
                    _record_saved(conn, msg_id, kind, out_rel)
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
                    _record_saved(conn, msg_id, kind, out_rel)
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
    init_db()          # 旧库要先补上 live_video_path 列，后面的查询才认得它
    conn = get_db()

    jobs = pending_videos(conn, conv_id=conv_id, limit=limit)
    live = sum(1 for j in jobs if j.get("kind") == "live")
    log = [f"[*] {len(jobs)} pending videos ({live} live photos)"]
    if not jobs:
        return {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "log": log}

    log.append(f"[*] {len({j['tkey'] for j in jobs})} unique tkeys")

    # 实况图缺的静态封面先补掉：这一步不用登录、不用浏览器，所以放在启动浏览器之前，
    # 万一没登录也至少把封面补上了。
    covers = await asyncio.to_thread(ensure_live_photo_covers, conn, jobs)
    if covers:
        log.append(f"[*] 顺带补了 {covers} 张实况图封面")

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
        init_db()      # 同上：reset 也要写 live_video_path
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
