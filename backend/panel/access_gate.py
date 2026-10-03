"""局域网访问的「钥匙」：解锁页 + 拦截中间件 + 三个接口。

服务一旦监听 ``0.0.0.0``（控制面板 →「设置」→「向局域网开放」），同一局域网里的
任何设备都能连上来。这个模块决定「让不让进」：

* 请求来自本机（127.0.0.1 / ::1）—— 永远放行，本机不需要钥匙；
* 局域网访问没开 —— 局域网来的请求直接拒绝（那时服务本来也只监听本机）；
* 开了、没设访问密码 —— 放行，谁都能看；
* 开了、设了访问密码 —— 只放行「被信任的设备」（Cookie 里的凭据能在配置里对上号）。
  其它设备会看到 ``/access`` 这一页，输入访问密码后设备被记住，以后不用再输。

注意这跟控制面板自己的密码是**两道独立的锁**：过了访问密码只说明「这台设备允许连
上来」，面板密码仍然照要（面板页面里的登录框、查看器里的登录框都由各自原有的逻辑
负责）。两个都设了，别的设备就要先过访问密码、再输面板密码。
"""
from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from common import access
from common import appearance as _appearance

log = logging.getLogger("app.access")

access_gate_router = APIRouter()

_ACCESS_HTML_PATH = os.path.join(
    os.path.dirname(__file__), "static", "access.html"
)
with open(_ACCESS_HTML_PATH, encoding="utf-8") as _f:
    ACCESS_HTML = _f.read()

#: 面板已有的主题配色（面板页面里那一份的拷贝）：给解锁页套上同一套颜色。
#: 顺序和 _THEME_VAR_NAMES 一一对应；它必须和 panel.html 里同名主题块完全一致，
#: tests/test_lan_access.py 会拿面板页面逐项核对，改面板配色时别忘了这里。
_THEME_TOKENS: dict[str, str] = {
    "dark": "#0f1014  #16181f  #21242e  #16181f  #f2586e  #ff7488  #e9eaf0  #9a9daa  #656875  #23262f  #4dd08a  #ff5c6c  #e2b23c",
    "light": "#f7f7f9 #ffffff #eef0f3 #ffffff #e5405c #f2586e #1a1c22 #5b606b #9096a1 #e2e4e9 #1f9d57 #d93440 #b7791f",
    "ocean": "#0b1622 #10202f #18324a #10202f #35c0d6 #66d6e8 #dceaf3 #8bacc0 #5b7d93 #1c344b #45c98a #f2586e #e6b34a",
    "purple": "#120f1c #1a1626 #271f38 #1a1626 #b57cff #cda3ff #e7dcf7 #a394c0 #6f6190 #2c2440 #57d09a #ff6b81 #e6b34a",
}
#: 变量顺序必须和上面每一组颜色一一对应
_THEME_VAR_NAMES = ("bg", "bg2", "bg3", "surface", "accent", "accent2",
                    "text", "text2", "text3", "border", "green", "red", "yellow")

#: 认不出的主题名、「自定义」主题都从这一套打底
_BASE_THEME = "dark"

#: 面板里 --accent-bg / --red-bg 两个派生变量的混色比例（各主题略有不同）
_THEME_MIX: dict[str, tuple[int, int]] = {
    "dark": (13, 14),
    "light": (9, 10),
    "ocean": (13, 14),
    "purple": (14, 14),
}

#: 配置里的颜色字段 → 页面变量名。只有面板里开放修改的那几项在这里；
#: ``surface`` / ``accent2`` 改不了，自定义主题下由其它颜色派生（见 _theme_vars）。
_CUSTOM_COLOR_VARS = {
    "bg": "--bg", "bg2": "--bg2", "bg3": "--bg3", "accent": "--accent",
    "text": "--text", "text2": "--text2", "text3": "--text3",
    "border": "--border", "green": "--green", "red": "--red",
    "yellow": "--yellow",
}

#: 让日志里的 401 / 403 有一句人话（见 backend/access_log.py 的 status_note）
_ACCESS_LOG_HINT = {
    "/access": "局域网访问解锁页",
    "/api/access/login": "局域网访问密码校验",
    "/api/access/logout": "退出局域网访问",
    "/api/access/status": "局域网访问状态",
}

#: 访问密码的错误次数限制：同一个 IP 在窗口时间内错这么多次就暂时不理它。
#: 局域网里谁都能连过来猜密码，纯靠密码强度不够，得有个刹车。计数只在内存里
#: （重启服务就清空），所以它挡住的是「一直猜」这种行为，不是永久封禁。
_FAIL_WINDOW_SECONDS = 300
_MAX_FAILURES = 10
_FAILURES_LOCK = threading.Lock()
_FAILURES: dict[str, list[float]] = {}


def _failed_attempts(host: str) -> int:
    """这个地址在窗口时间内错了几次（顺便清掉过期的记录）。"""
    now = time.time()
    with _FAILURES_LOCK:
        stamps = [t for t in _FAILURES.get(host, []) if now - t < _FAIL_WINDOW_SECONDS]
        if stamps:
            _FAILURES[host] = stamps
        else:
            _FAILURES.pop(host, None)
        return len(stamps)


def _record_failure(host: str) -> None:
    now = time.time()
    with _FAILURES_LOCK:
        stamps = [t for t in _FAILURES.get(host, []) if now - t < _FAIL_WINDOW_SECONDS]
        stamps.append(now)
        _FAILURES[host] = stamps
        # 地址太多时顺手清一遍空桶，别让这个字典无限长
        if len(_FAILURES) > 200:
            for key in [k for k, v in _FAILURES.items() if not v]:
                _FAILURES.pop(key, None)


def _clear_failures(host: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop(host, None)


def route_label(path: str) -> str | None:
    """``/access*`` 这几个地址在访问日志里叫什么（其它请求返回 None）。"""
    if path in _ACCESS_LOG_HINT:
        return _ACCESS_LOG_HINT[path]
    return None


# ── 文案（跟面板一样中英双语；按浏览器的 Accept-Language 选） ──

_TEXTS = {
    "zh": {
        "lang": "zh-CN",
        "title": "需要访问密码",
        "pageTitle": "需要访问密码 · 抖音聊天记录",
        "brand": "抖音聊天记录",
        "desc": "这台设备第一次访问需要输入访问密码，之后会被记住，不用再输。",
        "placeholder": "访问密码",
        "button": "进入",
        "error": "访问密码不对，请再试一次。",
        "tip": "访问密码在运行这台电脑的面板里设置：先在「关于」页打开开发者模式，再到「设置 → 局域网访问」。",
    },
    "en": {
        "lang": "en",
        "title": "Access password required",
        "pageTitle": "Access password required - Douyin chat export",
        "brand": "Douyin chat export",
        "desc": "This device is asked once; after that it is remembered and won't ask again.",
        "placeholder": "Access password",
        "button": "Enter",
        "error": "Wrong access password. Try again.",
        "tip": "The access password is set on the host machine: turn on developer mode "
               "(About page) first, then Settings → LAN access.",
    },
}


def pick_language(accept_language: str | None) -> str:
    """按 Accept-Language 挑中文还是英文（默认中文）。"""
    header = str(accept_language or "").lower()
    if not header:
        return "zh"
    english = header.find("en")
    chinese = header.find("zh")
    if english != -1 and (chinese == -1 or english < chinese):
        return "en"
    return "zh"


def _safe_next(raw: str | None) -> str:
    """校验「进站后去哪儿」：只接受站内相对路径，挡掉跳去别处的写法。"""
    text = str(raw or "").strip()
    if text.startswith("/") and not text.startswith("//") and "\\" not in text:
        return text
    return "/"


def _panel_style() -> tuple[str, dict | None, str]:
    """面板当前的外观：``(主题名, 自定义颜色, 字体栈)``。

    解锁页和「未开放局域网访问」页都按它渲染，所以别的设备看到的页面跟主机上的
    面板是同一套配色 —— 读不出来时退回默认外观，绝不让外观问题挡住这两个页面。
    """
    try:
        style = _appearance.load_appearance(access.load_config_cached())
    except Exception:
        style = _appearance.default_appearance()
    panel = style.get("panel") if isinstance(style.get("panel"), dict) else {}
    colors = panel.get("colors") if isinstance(panel.get("colors"), dict) else None
    theme = str(panel.get("theme") or _BASE_THEME)
    fonts = style.get("fonts") if isinstance(style.get("fonts"), dict) else {}
    return theme, colors, _appearance.font_stack(fonts.get("ui", "system"))


def _theme_vars(theme: str, colors: dict | None) -> str:
    """解锁页的配色变量（一整组，13 个变量加两个派生色）。

    取色规则跟面板**完全一致**：铺主题自带的那一组，只有「自定义」主题才用配置里
    的 ``colors`` 逐项覆盖。配置里的 ``colors`` 永远是一份完整快照（默认就是暗色那
    套），拿它去盖 Light / Ocean / Purple 只会把页面重新拉回暗色 —— 之前解锁页
    「背景和输入框是黑的、卡片却是白的」就是这个原因。
    """
    values = dict(zip(_THEME_VAR_NAMES, _THEME_TOKENS[_BASE_THEME].split()))
    preset = _THEME_TOKENS.get(theme)
    if preset:
        values.update(zip(_THEME_VAR_NAMES, preset.split()))
    elif isinstance(colors, dict):
        for key in _CUSTOM_COLOR_VARS:           # 字段名和变量名一一对应
            value = colors.get(key)
            if isinstance(value, str) and value.startswith("#"):
                values[key] = value
        # 这两项用户改不了：卡片跟着卡片底色，悬停色＝主色（面板里它们本来也空着）
        values["surface"] = values["bg2"]
        values["accent2"] = values["accent"]
    lines = [f"  --{key}: {values[key]};" for key in _THEME_VAR_NAMES]
    accent_pct, red_pct = _THEME_MIX.get(theme, _THEME_MIX[_BASE_THEME])
    lines.append(f"  --accent-bg: color-mix(in srgb, var(--accent) {accent_pct}%, transparent);")
    lines.append(f"  --red-bg: color-mix(in srgb, var(--red) {red_pct}%, transparent);")
    return "\n".join(lines)


def render_access_page(*, next_path: str = "/", error: bool = False,
                       accept_language: str | None = None) -> str:
    """解锁页的 HTML（主题、字体跟随面板的外观设置）。"""
    lang = pick_language(accept_language)
    text = _TEXTS[lang]
    theme, colors, font = _panel_style()
    error_html = ""
    if error:
        error_html = f'<div class="err">{html.escape(text["error"])}</div>'
    replacements = {
        "__LANG_TAG__": text["lang"],
        "__PAGE_TITLE__": html.escape(text["pageTitle"]),
        "__TITLE__": html.escape(text["title"]),
        "__BRAND__": html.escape(text["brand"]),
        "__DESC__": html.escape(text["desc"]),
        "__PLACEHOLDER__": html.escape(text["placeholder"]),
        "__BTN__": html.escape(text["button"]),
        "__TIP__": html.escape(text["tip"]),
        "__ERROR__": error_html,
        "__NEXT__": html.escape(_safe_next(next_path), quote=True),
        "__THEME_VARS__": _theme_vars(theme, colors),
        "__FONT_STACK__": font,
    }
    page = ACCESS_HTML
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


# ── 拦截：这个请求要不要先过访问密码 ──

def is_api_path(path: str) -> bool:
    """是不是接口请求（查看器的 /api/*、面板的 /panel/api/*）。

    接口被拦下时要回 401，不能跳转：前端 fetch 跟着跳转会拿到一页 HTML，
    更麻烦的是 POST 会被 303 改写成 GET（打到别的路由上）。
    """
    return path.startswith("/api/") or path.startswith("/panel/api/")


def should_gate(request: Request) -> bool:
    """这个请求是不是需要「局域网访问密码」才放行。"""
    return access.password_required() and not access.is_trusted(
        request.cookies.get(access.COOKIE_NAME)
    )


def gate_response(request: Request) -> Response:
    """拦下来时的回应：页面请求转去解锁页，接口请求回 401。"""
    path = request.url.path
    if is_api_path(path):
        return JSONResponse(
            {
                "error": "lan_access_password_required",
                "message": "这台设备还没通过局域网访问密码",
            },
            status_code=401,
        )
    query = request.url.query
    target = path + (f"?{query}" if query else "")
    return RedirectResponse(f"/access?next={quote(target, safe='')}", status_code=303)


def denial_response(request: Request) -> Response:
    """局域网访问没打开时，从局域网来的请求看到的说明页（配色同样跟随面板主题）。"""
    lang = pick_language(request.headers.get("accept-language"))
    if lang == "en":
        heading = "LAN access is off"
        body = ("The service is only listening on 127.0.0.1. To allow other devices on "
                "this network, turn on developer mode on the About page of the control "
                "panel on the host machine, then turn on \"LAN access\" "
                "(Settings → LAN access).")
    else:
        heading = "未开放局域网访问"
        body = ("服务目前只监听本机（127.0.0.1）。要允许局域网里的其它设备访问，"
                "请在运行这台电脑上的控制面板里：先在「关于」页打开开发者模式，"
                "再到「设置 → 局域网访问」打开那一项。")
    theme, colors, font = _panel_style()
    page = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(heading)}</title><style>"
        f":root {{\n{_theme_vars(theme, colors)}\n  --panel-font: {font};\n}}"
        "body { margin:0; min-height:100vh; display:flex; align-items:center;"
        " justify-content:center; background:var(--bg); color:var(--text);"
        " font-family:var(--panel-font); text-align:center; padding:24px; }"
        "h1 { font-size:18px; margin:0 0 10px; }"
        "p { margin:0; color:var(--text2); font-size:13px; max-width:420px; line-height:1.7; }"
        "</style></head><body><div>"
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(body)}</p>"
        "</div></body></html>"
    )
    return HTMLResponse(content=page, status_code=403)


async def gate_middleware(request: Request, call_next):
    """本模块的主入口：在鉴权之前先决定局域网这次请求放不放行。"""
    path = request.url.path
    if path in ("/access", "/api/access/login") or path.startswith("/api/access/"):
        return await call_next(request)          # 解锁页和配套接口本身不设防
    client_host = request.client.host if request.client else ""
    if not access.is_remote(client_host):
        return await call_next(request)          # 本机：永远不用钥匙
    if not access.lan_enabled():
        log.info("拒绝来自 %s 的请求（未开放局域网访问）：%s", client_host, path)
        return denial_response(request)
    if should_gate(request):
        log.info("局域网设备 %s 还没通过访问密码：%s", client_host, path)
        return gate_response(request)
    return await call_next(request)


# ── 路由 ──

def _set_cookie(response: Response, token: str, *, max_age: int | None = None) -> None:
    response.set_cookie(
        access.COOKIE_NAME, token, path="/", httponly=True, samesite="lax",
        max_age=max_age,
    )


async def _read_login_payload(request: Request) -> dict:
    """登录请求的字段：表单（解锁页提交）和 JSON（接口调用）都收。"""
    content_type = str(request.headers.get("content-type") or "").lower()
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
    except Exception:
        return {}
    if "json" in content_type:
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return dict(parse_qsl(raw, keep_blank_values=True))


@access_gate_router.get("/access")
async def access_page(request: Request, next: str = "/", err: str = ""):
    """解锁页：输入访问密码，密码对了这台设备就被记住。"""
    if not access.password_required():
        # 不需要密码（或干脆没开局域网）时，这一页没有意义，直接送去该去的地方
        return RedirectResponse(_safe_next(next), status_code=303)
    return HTMLResponse(
        content=render_access_page(
            next_path=next, error=bool(err),
            accept_language=request.headers.get("accept-language"),
        ),
        headers={"Cache-Control": "no-store"},
    )


@access_gate_router.post("/api/access/login")
@access_gate_router.post("/api/access/login/")
async def access_login(request: Request):
    """校验访问密码：对了就下发凭据，这台设备进信任列表。"""
    payload = await _read_login_payload(request)
    next_path = _safe_next(payload.get("next"))
    if not access.password_required():
        return RedirectResponse(next_path, status_code=303)
    client_host = request.client.host if request.client else ""
    if _failed_attempts(client_host) >= _MAX_FAILURES:
        log.info("访问密码错误次数过多，暂时不受理来自 %s 的尝试", client_host)
        return RedirectResponse(
            f"/access?err=1&next={quote(next_path, safe='')}", status_code=303,
        )
    if not access.verify_password(str(payload.get("password") or "")):
        _record_failure(client_host)
        log.info("访问密码错误（来自 %s）", client_host or "?")
        return RedirectResponse(
            f"/access?err=1&next={quote(next_path, safe='')}", status_code=303,
        )
    _clear_failures(client_host)
    remembered = access.remember_device(client_host, request.headers.get("user-agent"))
    if not remembered:
        return RedirectResponse(
            f"/access?err=1&next={quote(next_path, safe='')}", status_code=303,
        )
    device_id, token = remembered
    log.info("设备通过访问密码（%s，%s），已加入信任设备", client_host, device_id)
    response = RedirectResponse(next_path, status_code=303)
    _set_cookie(response, f"{device_id}.{token}", max_age=365 * 24 * 3600)
    return response


@access_gate_router.post("/api/access/logout")
async def access_logout(request: Request):
    """不再信任当前设备：忘掉它（那一行从信任设备里删掉），并清掉它的 Cookie。"""
    device_id = access.trusted_device_id(request.cookies.get(access.COOKIE_NAME))
    if device_id:
        access.forget_device(device_id)
    response = JSONResponse({"ok": True, "removed": bool(device_id)})
    response.delete_cookie(access.COOKIE_NAME, path="/")
    return response


@access_gate_router.get("/api/access/status")
async def access_status(request: Request):
    """解锁页 / 面板要的当前状态（不含任何凭据）。"""
    cookie = request.cookies.get(access.COOKIE_NAME)
    return {
        "lan_enabled": access.lan_enabled(),
        "password_required": access.password_required(),
        "trusted": access.is_trusted(cookie),
        "device_id": access.trusted_device_id(cookie) or "",
    }
