"""Richer console access logs than uvicorn's default one-liners."""
import logging
import re
import sys
import time
from urllib.parse import unquote

from starlette.middleware.base import BaseHTTPMiddleware

from backend.panel.access_gate import route_label as lan_access_route_label

log = logging.getLogger("app.access")

_SECRET_QS = re.compile(r"(?i)(token|password|authorization|cookie)=[^&]*")

_ROUTES = (
    (r"^/api/auth/check$", "登录状态检查"),
    (r"^/api/auth/", "登录认证"),
    (r"^/api/conversations/[^/]+/messages$", "拉取会话消息"),
    (r"^/api/conversations/[^/]+/senders$", "拉取会话发送者"),
    (r"^/api/conversations$", "会话列表"),
    (r"^/api/messages/[^/]+/forward$", "展开合并转发"),
    (r"^/api/messages/[^/]+/referenced-video$", "定位引用视频"),
    (r"^/api/messages/", "查询单条消息（系统提示/引用定位）"),
    (r"^/api/users/", "查询用户资料"),
    (r"^/api/search$", "搜索消息"),
    (r"^/api/", "浏览 API"),
    (r"^/panel/api/conversations/refresh/log", "控制面板：刷新日志"),
    (r"^/panel/api/conversations/refresh/status$", "控制面板：刷新进度轮询"),
    (r"^/panel/api/conversations/refresh$", "控制面板：开始刷新会话"),
    (r"^/panel/api/conversations/selected$", "控制面板：已选会话"),
    (r"^/panel/api/media/videos/", "控制面板：视频回填"),
    (r"^/panel/api/media/backfill/", "控制面板：图片回填"),
    (r"^/panel/api/config/download-images$", "控制面板：图片下载开关"),
    (r"^/panel/api/notify/", "控制面板：通知设置"),
    (r"^/panel/api/password/", "控制面板：密码设置"),
    (r"^/panel/api/login/check$", "控制面板：抖音登录探测"),
    (r"^/panel/api/status$", "控制面板：运行状态轮询"),
    (r"^/panel/api/", "控制面板 API"),
    (r"^/panel", "控制面板页面"),
    (r"^/media/images/", "本地图片"),
    (r"^/media/videos/", "本地视频"),
    (r"^/media/voice/", "本地语音"),
    (r"^/media/emoji/", "本地表情"),
    (r"^/media/avatars/", "会话头像"),
    (r"^/media/", "本地媒体"),
    (r"^/emoji/", "本地表情图片"),
    (r"^/assets/", "前端静态资源"),
    (r"^/$", "前端页面"),
)


def route_label(path: str) -> str:
    # 局域网访问那几个地址（/access、/api/access/*）由 access_gate 自己认领，
    # 免得同一句说明在两个文件里各写一遍。
    lan = lan_access_route_label(path)
    if lan:
        return lan
    for pattern, label in _ROUTES:
        if re.search(pattern, path):
            return label
    return "其他请求"


def status_note(path: str, status: int) -> str:
    if status == 304:
        return "浏览器缓存未变"
    if status == 401:
        if path.startswith("/api/access/"):
            return "这台设备还没通过局域网访问密码"
        return "未登录或令牌无效"
    if status == 403:
        if path != "/access":
            return "未开放局域网访问，只允许本机访问"
        return ""
    if status == 422:
        if path.endswith("/forward"):
            return "该消息不是合并转发"
        return "请求参数无效"
    if status == 404:
        if path.startswith("/api/messages/") and "/forward" not in path:
            return "本地未归档：系统提示在定位原消息，或引用目标不在当前库"
        if path.startswith("/api/users/"):
            return "本地用户表没有该 UID（合并转发里的外人常见）"
        if "/referenced-video" in path:
            return "引用的视频消息未归档"
        if path.startswith("/media/"):
            return "本地没有该媒体文件"
        return "资源不存在"
    if 200 <= status < 300:
        if "refresh/status" in path or path.endswith("/panel/api/status"):
            return "轮询"
        return "成功"
    if status >= 500:
        return "服务器内部错误"
    return ""


def safe_query(query: str) -> str:
    if not query:
        return ""
    return _SECRET_QS.sub(r"\1=***", query)


def describe_request(method: str, path: str, query: str, status: int, elapsed_ms: float) -> str:
    shown = unquote(path)
    qs = safe_query(query)
    if qs:
        shown = f"{shown}?{unquote(qs)}"
    note = status_note(path, status)
    extra = f" — {note}" if note else ""
    return (
        f"{method} {shown} → {status}  "
        f"{elapsed_ms:.0f}ms  [{route_label(path)}]{extra}"
    )


def force_utf8_output(streams=None) -> None:
    """把**重定向走的**标准输出固定成 UTF-8，别让日志文件里 UTF-8 和 GBK 混着。

    面板「日志」页是按 UTF-8 读 ``data/server.log`` 的，这份文件越干净越好。用
    start.ps1 /「启动服务（双击）.bat」/ 面板的自动重启助手起服务时，环境里已经带了
    ``PYTHONUTF8=1``（写出来就是 UTF-8）；但服务也可能是别的方式起的 —— 照着 README
    手敲 ``python -m uvicorn``、nssm、计划任务，或者旧版本的启动脚本 —— 那时 Windows
    默认按 GBK 写：同一份日志里就一段 UTF-8、一段 GBK，面板整份按一种编码读，
    另一种编码的那几段必是乱码（真机上反馈过：乱码的正是 UTF-8 那几段）。

    只动**不是终端**的流：输出重定向到文件 / 管道时统一按 UTF-8 写；人正对着终端看时
    保留控制台自己的编码（在 GBK 控制台上硬写 UTF-8 反而糊成一片）。

    ``streams`` 只给测试用；不传就管 ``sys.stdout`` / ``sys.stderr``。改不动就算了，
    这条流照样能用，不该因为改编码把服务拖住。
    """
    for stream in (sys.stdout, sys.stderr) if streams is None else streams:
        try:
            if stream is None or stream.isatty():
                continue
            encoding = str(getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding == "utf8":              # 已经是了：别白折腾（也免得动到 pytest 的捕获流）
                continue
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def configure_logging():
    """Use a compact clock prefix; silence uvicorn's duplicate access lines."""
    force_utf8_output()
    formatter = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger("app")
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False
    logging.getLogger("app.media").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.info(describe_request(
            request.method,
            request.url.path,
            request.url.query,
            response.status_code,
            elapsed_ms,
        ))
        return response
