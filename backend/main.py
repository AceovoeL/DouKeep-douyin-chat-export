"""FastAPI backend for browsing exported Douyin chat data."""
import hashlib
import hmac
import os
import secrets
import time
from typing import Literal

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from . import database
from .media_files import MediaStaticFiles
from .access_log import AccessLogMiddleware, configure_logging, log as access_log
from .panel.access_gate import access_gate_router, gate_middleware as lan_access_middleware
from common import appearance as appearance_cfg, config, paths
from common.version import VERSION as APP_VERSION

configure_logging()

app = FastAPI(title="抖音聊天记录浏览器", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AccessLogMiddleware)

# ── Auth system ──
_active_tokens: dict[str, float] = {}  # token -> expire_timestamp
_TOKEN_TTL = 7 * 24 * 3600  # 7 days


def _get_password_hash() -> str | None:
    """Read password hash from config."""
    return config.get_password_hash()


def _hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _verify_token(token: str) -> bool:
    if not token:
        return False
    exp = _active_tokens.get(token)
    if exp and time.time() < exp:
        return True
    _active_tokens.pop(token, None)
    return False


def _verify_api_token(token: str, path: str, method: str) -> bool:
    """持久 API token 只授权只读端点：GET /api/*（面板与删除操作除外）。"""
    if not token or method != "GET" or not path.startswith("/api/"):
        return False
    api_token = config.get_api_token()
    return bool(api_token) and hmac.compare_digest(token, api_token)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Public paths: auth endpoints, static assets, favicon
    if (path.startswith("/api/auth/") or
        path.startswith("/api/access/") or
        path.startswith("/assets") or
        path.startswith("/emoji") or
        path.startswith("/media") or
        path == "/favicon.svg"):
        return await call_next(request)
    # 外观设置只有一个颜色/字体/尺寸，没有隐私内容；公开它查看器才能在登录前
    # 就把主题套好，不会先闪一下默认样式。写入接口仍然需要登录。
    if path == "/api/appearance" and request.method == "GET":
        return await call_next(request)
    # Protected paths: /api/* and /panel*
    needs_auth = path.startswith("/api/") or path.startswith("/panel")
    if not needs_auth:
        return await call_next(request)
    # If no password set, allow all
    if not _get_password_hash():
        return await call_next(request)
    # Panel HTML page itself is allowed (login screen is embedded)
    if path in ("/panel", "/panel/"):
        return await call_next(request)
    # Check token from header, query param, or cookie
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        token = request.cookies.get("auth_token", "")
    if _verify_token(token):
        return await call_next(request)
    if _verify_api_token(token, path, request.method):
        return await call_next(request)
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.middleware("http")
async def utf8_response_middleware(request: Request, call_next):
    """让浏览器显式按 UTF-8 解码（Windows 部署下尤其重要）。"""
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if (
        content_type.startswith(("application/json", "text/html"))
        and "charset=" not in content_type.lower()
    ):
        response.headers["content-type"] = f"{content_type}; charset=utf-8"
    return response


# 局域网访问：最外层的一道门（写在这里 = 注册得最晚 = 最先执行）。本机请求直接放行，
# 局域网来的请求按「开关 + 访问密码 + 信任设备」判断，见 backend/panel/access_gate.py。
app.middleware("http")(lan_access_middleware)


from pydantic import BaseModel


class AuthLoginRequest(BaseModel):
    password: str


@app.get("/api/auth/check")
def auth_check(request: Request):
    """Check if password is set and if current token is valid."""
    pw_hash = _get_password_hash()
    if not pw_hash:
        return {"need_password": False, "authenticated": True}
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.query_params.get("token", "")
    return {"need_password": True, "authenticated": _verify_token(token)}


@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest):
    pw_hash = _get_password_hash()
    if not pw_hash:
        # Previously `return {...}, 400`, which FastAPI serialized as HTTP 200
        # with a JSON array body. Return a real 400.
        return JSONResponse({"error": "no password set"}, status_code=400)
    if not hmac.compare_digest(_hash_password(req.password), pw_hash):
        raise HTTPException(403, "密码错误")
    token = secrets.token_urlsafe(32)
    _active_tokens[token] = time.time() + _TOKEN_TTL
    return {"token": token}

# Serve media files. A request with `?tc=1` asks for an H.264 rendition of a
# clip the browser could not decode (see backend/media_files.py); every other
# request behaves exactly as StaticFiles did before.
media_dir = paths.MEDIA_DIR
os.makedirs(media_dir, exist_ok=True)
app.mount("/media", MediaStaticFiles(directory=media_dir), name="media")

# 文字式表情的本地图片（assets/emoji/*.webp）：前端把 [钱] 这类文字记号换成
# <img src="/emoji/钱.webp">。跟 /media 一样是公开的静态目录，里面没有隐私内容。
# 图片不进仓库（版权原因，见 NOTICE），第一次运行时由面板的「资源包」下到这里 ——
# 所以目录要先建出来再挂载：不然第一次下载完得重启服务才能取到图。
os.makedirs(paths.EMOJI_ASSET_DIR, exist_ok=True)
app.mount("/emoji", StaticFiles(directory=paths.EMOJI_ASSET_DIR), name="emoji")


@app.get("/api/stats")
def stats():
    return database.get_stats()


@app.get("/api/appearance")
def get_appearance():
    """聊天查看器启动时读取的外观设置。

    连同 ``defaults`` 一起返回，这样"单项恢复默认"不需要在前端再抄一份默认值；
    ``fonts`` 是可选字体清单，与面板设置页用的是同一份。
    """
    return {
        "appearance": appearance_cfg.load_appearance(),
        "defaults": appearance_cfg.default_appearance(),
        "fonts": appearance_cfg.font_options(),
    }


@app.get("/api/conversations")
def list_conversations(
    search: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    items, total = database.get_conversations(search=search, page=page, page_size=page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return conv


def _do_delete_conversation(conv_id: str):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return database.delete_conversation(conv_id)


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    return _do_delete_conversation(conv_id)


# POST alias for proxies that block DELETE method
@app.post("/api/conversations/{conv_id}/delete")
def delete_conversation_post(conv_id: str):
    return _do_delete_conversation(conv_id)


@app.get("/api/conversations/{conv_id}/messages")
def list_messages(
    conv_id: str,
    page_size: int = Query(100, ge=1, le=500),
    before_seq: int = Query(None),
    after_seq: int = Query(None),
):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    items, total = database.get_messages(conv_id, page_size=page_size, before_seq=before_seq, after_seq=after_seq)
    return {"items": items, "total": total, **database.get_message_page_bounds(conv_id, items)}


@app.get("/api/conversations/{conv_id}/messages/by-date")
def list_messages_by_date(
    conv_id: str,
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    tz: float = Query(8, ge=-12, le=14),
    limit: int = Query(5000, ge=1, le=5000),
):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    items = database.get_messages_by_date(conv_id, date, tz_hours=tz, limit=limit)
    return {"items": items, "total": len(items), "date": date}


@app.get("/api/conversations/{conv_id}/messages/range")
def list_messages_range(
    conv_id: str,
    start_seq: int = Query(..., ge=0),
    end_seq: int = Query(..., ge=0),
):
    if end_seq < start_seq:
        raise HTTPException(400, "end_seq 不能小于 start_seq")
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    items = database.get_messages_range(conv_id, start_seq, end_seq)
    return {"items": items, "total": len(items)}


@app.get("/api/conversations/{conv_id}/stats/daily")
def daily_stats(conv_id: str, tz: float = Query(8, ge=-12, le=14)):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return {"items": database.get_daily_stats(conv_id, tz_hours=tz)}


@app.get("/api/token")
def get_api_token():
    """查看持久 API token（受与其余 /api/ 相同的鉴权保护）。"""
    return {"token": config.ensure_api_token()}


@app.get("/api/conversations/{conv_id}/senders")
def list_senders(conv_id: str):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return database.get_senders(conv_id)


@app.get("/api/search")
def search(
    q: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    conv_id: str | None = Query(None),
    start_time: int | None = Query(None, ge=0),
    end_time: int | None = Query(None, ge=0),
    media_type: Literal["image", "video", "media", "forward"] | None = Query(None),
):
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise HTTPException(422, "结束时间必须晚于开始时间")
    if not q.strip() and not conv_id and start_time is None and end_time is None and not media_type:
        raise HTTPException(422, "请指定搜索条件")
    items, total = database.search_messages(
        q.strip(), page=page, page_size=page_size, conv_id=conv_id,
        start_time=start_time, end_time=end_time, media_type=media_type,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/messages/{msg_id}")
def get_message(msg_id: str):
    msg = database.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "消息不存在")
    return msg


@app.get("/api/messages/{msg_id}/forward")
def get_forward_messages(msg_id: str):
    from .forwarded import resolve_forward

    msg = database.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "消息不存在")
    conn = database.get_db()
    try:
        result = resolve_forward(msg, conn)
    finally:
        conn.close()
    if result is None:
        raise HTTPException(422, "这不是合并转发消息")
    local_media = sum(1 for row in result.get("items") or [] if row.get("media_local_path"))
    access_log.info(
        "合并转发 %s「%s」正文 %s/%s，缺失 %s，已挂本地媒体 %s 条",
        msg_id, result.get("title") or "聊天记录",
        result.get("available"), result.get("total"),
        result.get("missing"), local_media,
    )
    return result


@app.get("/api/messages/{msg_id}/referenced-video")
def get_referenced_video(msg_id: str):
    message = database.find_referenced_video(msg_id)
    if not message:
        raise HTTPException(404, "引用的消息未归档")
    return message


@app.get("/api/users")
def list_users():
    return database.get_all_users()


@app.get("/api/users/{uid}")
def get_user(uid: str):
    user = database.get_user(uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    return user


# Control panel
from backend.control_panel import (
    control_router,
    restore_schedule_on_startup,
    restore_update_schedule_on_startup,
    restore_update_done_notice_on_startup,
)
app.include_router(control_router)

# 局域网访问：解锁页 /access 与配套接口（拦截逻辑见上面的 lan_access_middleware）
app.include_router(access_gate_router)

# Screenshot rendering (headless chromium against our own frontend)
from backend.screenshot import screenshot_router
app.include_router(screenshot_router)

# ChatLab 远程数据源协议（Pull 模式）：ChatLab 定时来拉增量消息
from backend.chatlab_pull import chatlab_router
app.include_router(chatlab_router)


@app.on_event("startup")
async def startup():
    from common.db import init_db
    init_db()
    config.ensure_api_token()
    await restore_schedule_on_startup()
    await restore_update_schedule_on_startup()
    # 若是刚更新完自动重启起来的，把「更新完成」弹给用户看一次
    await restore_update_done_notice_on_startup()

# Serve Vue frontend (must be last)
_frontend_dist = paths.FRONTEND_DIST
if os.path.isdir(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

    _index_html = os.path.join(_frontend_dist, "index.html")

    # The shell names the hashed bundle, so a cached copy pins the browser to an
    # old build — which is exactly how a fixed frontend keeps looking broken
    # (see fix.md 2026-09-27 第三轮). The file is ~700 bytes; always revalidate.
    # The hashed assets themselves stay cacheable: a new build means a new name.
    _SHELL_HEADERS = {"Cache-Control": "no-store, must-revalidate"}

    @app.get("/favicon.svg")
    def serve_favicon():
        return FileResponse(os.path.join(_frontend_dist, "favicon.svg"), media_type="image/svg+xml")

    @app.get("/")
    def serve_frontend_root():
        return FileResponse(_index_html, headers=_SHELL_HEADERS)

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        if full_path.startswith("panel"):
            raise HTTPException(404)
        return FileResponse(_index_html, headers=_SHELL_HEADERS)
