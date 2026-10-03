"""局域网访问：监听地址、访问密码与信任设备。

控制面板在「设置」里有一个「向局域网开放」开关。打开之后，服务会监听 ``0.0.0.0``，
同一个局域网里的其它设备就能用自己的 IP 访问：

* ``http://<这台电脑的 IP>:8000/panel`` —— 控制面板
* ``http://<这台电脑的 IP>:8000/``      —— 聊天查看器

默认谁都能看；不想这样的话，可以再给局域网访问单独设一个「访问密码」。这个密码跟
控制面板自己那个密码是**两回事**：面板密码保护的是「打开面板 / 查看器要登录」，
访问密码保护的是「只有知道密码的设备才连得上」。两个都设了，别的设备就要先过访问
密码、再输面板密码。

本模块是这些设置的唯一实现处，配置都存在 ``data/panel_config.json`` 里：

    lan_access            : true  = 允许局域网访问（默认 false，只监听 127.0.0.1）
    lan_password_hash     : 访问密码的 sha256；空/没有 = 不要访问密码
    access_trusted_devices: 被信任的设备列表，见 ``remember_device()``

「信任设备」是怎么认的：第一次在 ``/access`` 输入访问密码成功后，服务随机会发一个
设备 id + token，用 Cookie 放在那台设备上（``access_token``），同时把 token 的
sha256 记进配置。之后这台设备再访问就凭 Cookie 通过，不用再输密码。用户可以在面板
里对某台设备点「不信任」—— 那一行从配置里删掉，那台设备手上的 Cookie 立刻失效。
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import socket
import threading
import time
from urllib.parse import unquote

from common import config, paths

#: 记住「这台设备已经过了访问密码」用的 Cookie 名字
COOKIE_NAME = "access_token"

#: 服务端口（访问地址、重启时都用它；start.ps1 / start.sh 里也是 8000）
DEFAULT_PORT = 8000

#: 监听地址：局域网开放 = 0.0.0.0，否则只监听本机
LAN_HOST = "0.0.0.0"
LOCAL_HOST = "127.0.0.1"

#: 信任设备最多留这么多台，超了就丢掉最早信任的那台（配合界面上的手动移除）
MAX_TRUSTED_DEVICES = 50

#: 「这台设备」在界面上的显示名：UA 里能认出来就叫名字（顺序有意义，Edge / 微信
#: 的 UA 里都带 Chrome，必须先判断它们）
_UA_PATTERNS: tuple[tuple[str, str], ...] = (
    ("micromessenger", "微信内置浏览器"),
    ("qqbrowser", "QQ 浏览器"),
    ("edg/", "Edge"),
    ("edga/", "Edge"),
    ("opr/", "Opera"),
    ("firefox/", "Firefox"),
    ("chrome/", "Chrome"),
    ("safari/", "Safari"),
)
_OS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("windows nt", "Windows"),
    ("iphone", "iPhone"),
    ("ipad", "iPad"),
    ("android", "Android"),
    ("mac os x", "Mac"),
    ("macintosh", "Mac"),
    ("cros", "ChromeOS"),
    ("linux", "Linux"),
)
_AUTO_CHECK = ("curl", "wget", "python-requests", "python-urllib", "httpx")

# ── 配置读写 ──
# 读配置带了 mtime 缓存：鉴权路径上每个请求都要看一次配置（局域网开关 / 访问密码），
# 每次都开文件太浪费；缓存跟着文件的修改时间失效，所以手改文件或别的进程写了都会
# 立刻读到新的。写配置统一走 update_config()，用锁串行化，避免并发请求互相覆盖。
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"path": None, "mtime": None, "config": None}


def load_config_cached() -> dict:
    """读一份配置（按文件 mtime 缓存）；读不到时返回默认配置。"""
    path = paths.CONFIG_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _CACHE_LOCK:
        if _CACHE["path"] == path and _CACHE["mtime"] == mtime and mtime is not None:
            return _CACHE["config"]
    cfg = config.load_config()
    with _CACHE_LOCK:
        _CACHE["path"] = path
        _CACHE["mtime"] = mtime
        _CACHE["config"] = cfg
    return cfg


def invalidate_cache() -> None:
    """让下次读配置一定重新开文件（外部改了配置之后调用）。"""
    with _CACHE_LOCK:
        _CACHE["mtime"] = None
        _CACHE["config"] = None


def update_config(mutate) -> dict:
    """改配置：``mutate(cfg)`` 原地修改，改完原子写回，返回改完的配置。

    读写全程持锁，所以两个请求同时改配置不会丢掉对方那一次修改。
    """
    with _CACHE_LOCK:
        cfg = config.load_config()
        mutate(cfg)
        config.save_config(cfg)
        _CACHE["path"] = paths.CONFIG_PATH
        _CACHE["mtime"] = os.path.getmtime(paths.CONFIG_PATH)
        _CACHE["config"] = cfg
        return cfg


# ── 开关 / 密码 ──

def lan_enabled() -> bool:
    """是否允许局域网访问（默认关）。"""
    return bool(load_config_cached().get("lan_access"))


def server_host() -> str:
    """服务该监听在哪儿：局域网开放时 0.0.0.0，否则只监听本机。"""
    return LAN_HOST if lan_enabled() else LOCAL_HOST


def password_hash() -> str | None:
    """访问密码的 sha256；没设（或设成空）时返回 None。"""
    return load_config_cached().get("lan_password_hash") or None


def password_required() -> bool:
    """局域网访问是不是要密码（开关没开时一律 False）。"""
    return lan_enabled() and password_hash() is not None


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str) -> bool:
    """输入访问密码后校验（没有设置密码时一律 False）。"""
    import hmac

    stored = password_hash()
    if not stored:
        return False
    return hmac.compare_digest(hash_password(password), stored)


def set_password(password: str) -> bool:
    """设置访问密码；传空字符串 = 取消密码。

    取消密码时，之前信任过的设备会一起清掉 —— 那时候已经不需要凭据了，
    留着这些记录只会让下一轮重新设密码时凭空多出几台信任设备。
    """
    def mutate(cfg: dict) -> None:
        if password:
            cfg["lan_password_hash"] = hash_password(password)
        else:
            cfg.pop("lan_password_hash", None)
            cfg.pop("access_trusted_devices", None)

    update_config(mutate)
    return bool(password)


def set_lan_access(enabled: bool) -> None:
    """打开 / 关闭局域网访问（下次启动服务时生效，见 ``tools/restart_server.py``）。"""
    update_config(lambda cfg: cfg.__setitem__("lan_access", bool(enabled)))


# ── 地址判定 ──

def normalize_host(value) -> str:
    """把命令行 / 配置里的监听地址整理成能比较的形式。

    ``[::]`` → ``::``、``::ffff:127.0.0.1`` → ``127.0.0.1``，空字符串按 127.0.0.1。
    """
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return LOCAL_HOST
    if text.lower().startswith("::ffff:") and "." in text:
        return text[7:]
    return text


def is_local_address(host: str) -> bool:
    """这个来源地址是不是「本机」：回环地址，或者干脆不是一个 IP 地址。

    不是 IP 的来源按「本机」处理：真实连接一定有 IP，读不出 IP 的都是进程内调用
    （测试客户端、本地工具）。宁可放行这种取不到地址的情况，也不要因为判不出来就把
    人挡在门外 —— 局域网那道锁防的是「另一台设备」，而不是「读不出地址的调用方」。
    """
    text = normalize_host(host)
    if text in ("", "localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return True


def is_remote(host: str) -> bool:
    """这个来源地址要不要过局域网那道锁（本机一律不拦）。"""
    return not is_local_address(host)


def trusted_devices() -> list[dict]:
    """当前被信任的设备（返回副本，调用方改它不会影响缓存里的配置）。"""
    devices = load_config_cached().get("access_trusted_devices")
    if not isinstance(devices, list):
        return []
    return [dict(row) for row in devices if isinstance(row, dict)]


def _device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_cookie(raw: str | None) -> tuple[str, str] | None:
    """把 ``access_token`` 的值拆成 (设备 id, token)；格式不对返回 None。"""
    if not raw:
        return None
    value = unquote(str(raw)).strip()
    if "." not in value:
        return None
    device_id, _, token = value.partition(".")
    if not device_id or not token:
        return None
    return device_id, token


def trusted_device_id(raw: str | None) -> str | None:
    """这个 Cookie 对应哪台被信任的设备；不信任（或已被移除）时返回 None。

    只有两边都对得上才算数：设备 id 在列表里，并且 Cookie 里的 token 的 sha256
    跟存下来的那份一致。所以用户在面板上点「不信任」之后，那台设备手上的 Cookie
    立刻失效，不用等它过期。
    """
    import hmac

    parsed = parse_cookie(raw)
    if not parsed:
        return None
    device_id, token = parsed
    presented = _device_token_hash(token)
    for device in trusted_devices():
        if str(device.get("id") or "") != device_id:
            continue
        stored = str(device.get("token_hash") or "")
        if stored and hmac.compare_digest(stored, presented):
            return device_id
        return None
    return None


def is_trusted(raw: str | None) -> bool:
    return trusted_device_id(raw) is not None


def device_name(user_agent: str | None, ip: str = "") -> str:
    """给设备取一个能认出来的名字（认不出来就用 IP）。"""
    text = str(user_agent or "").lower()
    browser = next((label for key, label in _UA_PATTERNS if key in text), "")
    system = next((label for key, label in _OS_PATTERNS if key in text), "")
    if browser and system:
        name = f"{browser} · {system}"
    else:
        name = browser or system
    if not name and text and any(key in text for key in _AUTO_CHECK):
        name = "自动检查/脚本"
    if not name:
        name = "其它浏览器"
    return f"{name}（{ip}）" if ip else name


def remember_device(ip: str = "", user_agent: str | None = None) -> tuple[str, str] | None:
    """把这次的设备记进信任列表，返回 (设备 id, token)；配置不可写时返回 None。"""
    device_id = secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    record = {
        "id": device_id,
        #: 只存 token 的摘要：配置泄漏也换不来一台被信任的设备
        "token_hash": _device_token_hash(token),
        "name": device_name(user_agent, ip),
        "ip": str(ip or ""),
        "trusted_at": int(time.time()),
    }
    try:
        def mutate(cfg: dict) -> None:
            devices = [row for row in cfg.get("access_trusted_devices") or [] if isinstance(row, dict)]
            devices.append(record)
            cfg["access_trusted_devices"] = devices[-MAX_TRUSTED_DEVICES:]

        update_config(mutate)
    except OSError as exc:                       # 磁盘写不进去：这次先放行，别把用户挡在外面
        print(f"[!] 保存信任设备失败（{exc}），本次访问仍然放行")
    return device_id, token


def forget_device(device_id: str) -> bool:
    """不再信任某台设备；返回是否真的删掉了一台。"""
    removed = {"ok": False}

    def mutate(cfg: dict) -> None:
        devices = [row for row in cfg.get("access_trusted_devices") or [] if isinstance(row, dict)]
        kept = [row for row in devices if str(row.get("id") or "") != str(device_id)]
        removed["ok"] = len(kept) != len(devices)
        cfg["access_trusted_devices"] = kept

    update_config(mutate)
    return removed["ok"]


def forget_all_devices() -> int:
    """把信任设备全部清掉（比如换了访问密码）；返回清掉了几台。"""
    count = {"n": 0}

    def mutate(cfg: dict) -> None:
        count["n"] = len([row for row in cfg.get("access_trusted_devices") or [] if isinstance(row, dict)])
        cfg["access_trusted_devices"] = []

    update_config(mutate)
    return count["n"]


def list_devices(current_id: str | None = None) -> list[dict]:
    """给面板用的设备清单（token 摘要绝不返回给界面）。"""
    rows: list[dict] = []
    for index, device in enumerate(trusted_devices(), start=1):
        ip = str(device.get("ip") or "")
        rows.append({
            "id": str(device.get("id") or ""),
            "name": str(device.get("name") or "未知设备"),
            "ip": ip,
            "trusted_at": int(device.get("trusted_at") or 0),
            "index": index,
            "current": bool(current_id) and str(device.get("id") or "") == str(current_id),
        })
    return rows


# ── 访问地址（给面板显示「用这个地址访问」） ──

def lan_addresses() -> list[str]:
    """这台电脑在内网里的 IPv4 地址（可能有多个网卡，取不到的项会跳过）。"""
    found: list[str] = []

    def add(value: str) -> None:
        text = str(value or "").strip()
        if not text or is_local_address(text):
            return
        if text not in found:
            found.append(text)

    # 查默认路由会让操作系统挑一张「用得上的」网卡，这里不发任何数据
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.4)
            sock.connect(("8.8.8.8", 80))
            add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return found


def access_addresses(port: int = DEFAULT_PORT, client_host: str = "") -> dict:
    """面板上展示的访问地址：本机的，以及（可能好几个）局域网地址。"""
    port = int(port or DEFAULT_PORT)
    addresses = [f"http://127.0.0.1:{port}"]
    for ip in lan_addresses():
        addresses.append(f"http://{ip}:{port}")
    # 用户自己就是从局域网某个地址进来的：把它排在前面，省得去猜自己是哪个 IP
    if client_host and not is_local_address(client_host):
        candidates = [a for a in addresses if f"//{normalize_host(client_host)}:" in a]
        addresses = candidates + [a for a in addresses if a not in candidates]
    return {
        "panel": f"{addresses[0]}/panel",
        "viewer": addresses[0],
        "addresses": addresses,
        "port": port,
    }
