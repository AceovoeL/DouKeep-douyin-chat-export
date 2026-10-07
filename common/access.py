"""局域网访问：监听地址、访问密码与信任设备。

控制面板在「设置」里有一个「向局域网开放」开关。打开之后，服务会监听 ``0.0.0.0``，
同一个局域网里的其它设备就能用自己的 IP 访问：

* ``http://<这台电脑的 IP>:8000/panel`` —— 控制面板
* ``http://<这台电脑的 IP>:8000/``      —— 聊天查看器

默认谁都能看；不想这样的话，可以再给局域网访问单独设一个「访问密码」。这个密码跟
控制面板自己那个密码是**两回事**：面板密码保护的是「打开面板 / 查看器要登录」，
访问密码保护的是「只有知道密码的设备才连得上」。两个都设了，别的设备就要先过访问
密码、再输面板密码。

本模块是这些设置的唯一实现处，配置都存在 ``config/panel_config.json`` 里：

    lan_access            : true  = 允许局域网访问（默认 false，只监听 127.0.0.1）
    lan_password_hash     : 访问密码的 sha256；空/没有 = 不要访问密码
    access_trusted_devices: 被信任的设备列表，见 ``remember_device()``

「信任设备」是怎么认的：第一次在 ``/access`` 输入访问密码成功后，服务随机会发一个
设备 id + token，用 Cookie 放在那台设备上（``access_token``），同时把 token 的
sha256 记进配置。之后这台设备再访问就凭 Cookie 通过，不用再输密码。用户可以在面板
里对某台设备点「不信任」—— 那一行从配置里删掉，那台设备手上的 Cookie 立刻失效。
**主人换了访问密码，这些记录会全部清掉**（见 ``set_password()``）：局域网和公网的设备
都要拿新密码重新来一次。

**这块 Cookie 只是「省得重输密码」，不是安全凭据。** 它放在对方手里，人家清掉浏览器
数据就没了、也能整个复制走；IP 同样不算凭据（换个网络、开个热点就变了）。所以真正
管住「有人在猜密码」的是下面这本账：

    全服务共用一本「猜错了几次」的账（``access_guard``，存在配置里、重启也在）。
    连续错 MAX_PASSWORD_FAILURES 次 → 整个登录入口先锁 LOCK_STEP_SECONDS；解禁之后
    再错一次，锁的时间翻倍，直到 LOCK_MAX_SECONDS 封顶。谁输对了、或者主人点了面板上
    的「立即解锁」，这本账才清零。

账记在**服务端**，跟「谁来的、有没有 Cookie」全都无关：换网络、清 Cookie、换浏览器、
直接上脚本，都改不了这本账里的数字，能多猜的次数也就被卡死了。
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import socket
import threading
import time
from collections.abc import Mapping
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

#: 全服务共用一本账：连续输错这么多次访问密码，就把整个登录入口锁住（见 ``guard``）
MAX_PASSWORD_FAILURES = 5

#: 第 MAX_PASSWORD_FAILURES 次错误先锁这么久；之后再错一次翻一倍
LOCK_STEP_SECONDS = 15 * 60

#: 翻倍的上限：最多锁这么久（免得一本账把人永久关在门外）
LOCK_MAX_SECONDS = 24 * 60 * 60

#: 同一台设备单独错这么多次，就把它记进「需手动确认」（要主人在面板上点「解除」）。
#: 比上面那本全服务的账高：这本账认得出「同一台设备」，主人自己在手机上连打错几次也会
#: 被它记上，阈值一样高的话他会跟正在猜密码的人一起进名单，还得专门跑一趟主机去解除。
MAX_DEVICE_FAILURES = 10

#: 「登录保护」里最多列出这么多台「正在猜密码的设备」（只用来显示，不参与判定）
MAX_GUARD_DEVICES = 10

#: 「需手动确认」的原因代码：面板按代码翻成人话
PENDING_TOO_MANY_FAILURES = "too_many_failures"

#: 「需手动确认」列表最多留这么多台
MAX_PENDING_DEVICES = 50

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
    """服务该监听在哪儿：局域网开放时 0.0.0.0，否则只监听本机。

    公网访问不用单独考虑这个 —— 隧道程序连的是本机回环地址，而且开公网访问之前
    必须先把局域网访问打开（面板上就是这么要求的）。
    """
    return LAN_HOST if lan_enabled() else LOCAL_HOST


# ── 公网访问（把自己的域名接到本机服务）──
#: Cloudflare 上的隧道名，固定一个：重新挂载时复用同一条隧道，不会越建越多
PUBLIC_TUNNEL_NAME = "doukeep"


def public_access() -> dict:
    """公网访问的设置（``domain`` / ``enabled`` / ``tunnel``）；没配过时是空字典。"""
    value = load_config_cached().get("public_access")
    return dict(value) if isinstance(value, dict) else {}


def public_enabled() -> bool:
    """现在是不是开着公网访问。"""
    return bool(public_access().get("enabled"))


def public_domain() -> str:
    """用户填的域名（关掉之后也留着，重新打开不用再填）。"""
    return str(public_access().get("domain") or "")


def public_tunnel_name() -> str:
    return str(public_access().get("tunnel") or PUBLIC_TUNNEL_NAME)


def set_public_access(*, enabled: bool | None = None, domain: str | None = None,
                      tunnel: str | None = None) -> dict:
    """改公网访问的设置（只传要改的那几项，其余不动）。"""
    def mutate(cfg: dict) -> None:
        section = cfg.get("public_access")
        section = dict(section) if isinstance(section, dict) else {}
        if domain is not None:
            section["domain"] = str(domain)
        if tunnel is not None:
            section["tunnel"] = str(tunnel)
        if enabled is not None:
            section["enabled"] = bool(enabled)
        cfg["public_access"] = section

    update_config(mutate)
    return public_access()


def public_ready() -> tuple[bool, str]:
    """能不能开公网访问：要先打开局域网访问。

    访问密码**不在这里硬拦**：没有密码时面板会先把风险红字说清楚、要用户二次确认
    （见 panel.html 的 publicRiskConfirm），所以后端不再堵死这条路 —— 否则用户想
    「先挂上再补密码」就只能去改配置文件了。

    返回 ``(能不能, 原因代码)``；原因代码由面板翻译成提示语。
    """
    if not lan_enabled():
        return False, "lan_off"
    return True, ""


def public_urls() -> dict:
    """公网访问的两个地址；没填域名时返回空字典。"""
    domain = public_domain()
    if not domain:
        return {}
    return {"panel": f"https://{domain}/panel", "viewer": f"https://{domain}"}


def password_hash() -> str | None:
    """访问密码的 sha256；没设（或设成空）时返回 None。"""
    return load_config_cached().get("lan_password_hash") or None


def password_required() -> bool:
    """要不要过访问密码：局域网和公网只要开了一个、而且设了密码，就要。

    没设密码时是 False —— 那会儿局域网里谁都能直接进（面板上会把后果写在明处）；
    「没设密码就不许挂到公网」由 ``public_ready()`` 拦着，见它的说明。
    """
    return open_to_outsiders() and password_hash() is not None


def open_to_outsiders() -> bool:
    """有没有对「别的设备」开放：局域网访问或公网访问开着都算。

    公网访问是借 Cloudflare 隧道进来的，请求在服务看来也是「外面来的」，所以两道门
    共用同一把锁（访问密码）—— 面板上对用户的说法就是「局域网访问密码就是公网访问
    密码」。
    """
    return lan_enabled() or public_enabled()


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
    """设置 / 更换访问密码；传空字符串 = 取消密码。

    **两条路都把「设备登录态」清干净**：信任设备（它们手上的 Cookie 立刻失效）、「需手动
    确认」列表、还有猜密码那本账，一律清掉。理由不一样，结果一样：

    * 换密码：密码是唯一的凭据，换了就等于换锁 —— 旧密码知道的人不该还能靠手里的 Cookie
      继续看（局域网和公网设备都是）。主人改密码往往正是因为「有别人知道旧密码」。
    * 取消密码：那时候已经不需要凭据了，留着这些记录只会让下一轮重新设密码时凭空多出几台
      信任设备、或者让一台早该被忘掉的设备继续被暂停。

    清掉之后，之前被信任的设备再访问就会重新看到解锁页；面板上「信任设备」列表也一起空掉。
    """
    def mutate(cfg: dict) -> None:
        cfg.pop("access_guard", None)
        cfg.pop("access_trusted_devices", None)
        cfg.pop("access_pending_devices", None)
        if password:
            cfg["lan_password_hash"] = hash_password(password)
        else:
            cfg.pop("lan_password_hash", None)

    update_config(mutate)
    return bool(password)


# ── 猜密码账本（全服务共用，换网络 / 清 Cookie 都躲不开）──
#
# 以前是「按来源 IP + 浏览器型号」数错误次数，换个网络就成了一台「新设备」，错误次数
# 重新从 0 开始；Cookie 也一样能清。任何放在来访者手里、或者跟着网络变的东西都算不上
# 凭据。所以决定性的那本账改成**只记在服务端**：不看到来人是谁，只看「一共猜错了几次」。

def guard() -> dict:
    """猜密码账本：``{"failures": 连续错了几次, "locked_until": 解禁时间戳, "devices": [...]}``。

    ``devices`` 是「是哪几台设备在猜」（名字 / 来源地址 / 各自错了几次 / 最近一次什么时候），
    只用来显示给主人看 —— 决定锁不锁的只有 ``failures`` 那一个数字，它跟「谁来的」无关。

    读不到、或者配置被手改坏了，都当成「干干净净的一本账」，绝不让脏数据把主人挡在外面。
    """
    empty = {"failures": 0, "locked_until": 0, "devices": []}
    raw = load_config_cached().get("access_guard")
    if not isinstance(raw, dict):
        return empty
    try:
        failures = max(0, int(raw.get("failures") or 0))
        locked_until = max(0, int(float(raw.get("locked_until") or 0)))
    except (TypeError, ValueError):
        return empty
    devices: list[dict] = []
    for row in raw.get("devices") or []:
        if not isinstance(row, dict):
            continue
        try:
            devices.append({
                "name": str(row.get("name") or "未知设备"),
                "ip": str(row.get("ip") or ""),
                "ua_hash": str(row.get("ua_hash") or ""),
                "failures": max(0, int(row.get("failures") or 0)),
                "first_at": max(0, int(row.get("first_at") or 0)),
                "last_at": max(0, int(row.get("last_at") or 0)),
            })
        except (TypeError, ValueError):
            continue                              # 单条坏了就丢这一条，别拖垮整本账
    return {"failures": failures, "locked_until": locked_until, "devices": devices}


def guard_devices() -> list[dict]:
    """「正在猜密码的设备」清单（最近一次在猜的排最前；不含内部指纹，界面用不上）。

    先倒过来再按时间排：「同一秒里猜的」很常见（连着试几次），配置里最后写进去的那台
    才是最近的，排序是稳定的，倒过来就能让它在同一秒的几台里排前面。
    """
    rows = [{
        "name": device["name"],
        "ip": device["ip"],
        "failures": device["failures"],
        "first_at": device["first_at"],
        "last_at": device["last_at"],
    } for device in guard()["devices"]]
    rows.reverse()
    rows.sort(key=lambda row: row["last_at"], reverse=True)
    return rows


def guard_locked_seconds(now: float | None = None) -> int:
    """还要等多少秒才受理密码（没锁时是 0）；不足一秒也算 1 秒。"""
    moment = time.time() if now is None else float(now)
    remaining = guard()["locked_until"] - moment
    if remaining <= 0:
        return 0
    seconds = int(remaining)
    return seconds + 1 if remaining > seconds else seconds


def record_guard_failure(now: float | None = None, ip: str = "",
                         user_agent: str | None = None) -> dict:
    """又错了一次：记进账本，错够了就锁住（解禁之后每再错一次，时间翻倍）。

    每次点着都是「**从现在起**再锁这么久」（不是往上一段锁的尾巴上叠）—— 这样不管被
    点着几次，最多也就等到 ``LOCK_MAX_SECONDS`` 之后，不会越叠越远。

    ``ip`` / ``user_agent`` 只影响「是哪台设备在猜」那份清单（给主人看的），跟锁不锁
    完全无关：锁的是 ``failures`` 那一个数字。同一台设备（来源地址 + 浏览器指纹，跟信任
    设备用同一把尺子）重复猜只累积次数，不会把清单刷满。
    """
    moment = int(time.time() if now is None else now)
    state = {"failures": 0, "locked_until": 0}
    record = {
        "name": device_name(user_agent, ip),
        "ip": str(ip or ""),
        "ua_hash": _ua_fingerprint(user_agent),
        "last_at": moment,
    }
    key = device_group_key(record)

    def mutate(cfg: dict) -> None:
        raw = cfg.get("access_guard")
        raw = dict(raw) if isinstance(raw, dict) else {}
        try:
            failures = max(0, int(raw.get("failures") or 0)) + 1
        except (TypeError, ValueError):
            failures = 1
        locked_until = 0
        if failures >= MAX_PASSWORD_FAILURES:
            step = min(failures - MAX_PASSWORD_FAILURES, 20)     # 巨大数字也不要溢出
            penalty = min(LOCK_STEP_SECONDS * (2 ** step), LOCK_MAX_SECONDS)
            locked_until = moment + penalty
        rows = [row for row in raw.get("devices") or [] if isinstance(row, dict)]
        same = next((row for row in rows if device_group_key(row) == key), None)
        try:
            previous = int(same.get("failures") or 0) if same else 0
            first_at = int(same.get("first_at") or moment) if same else moment
        except (TypeError, ValueError):
            previous, first_at = 0, moment
        entry = dict(record, failures=previous + 1, first_at=first_at)
        kept = [row for row in rows if device_group_key(row) != key]
        kept.append(entry)
        cfg["access_guard"] = {"failures": failures, "locked_until": locked_until,
                               "devices": kept[-MAX_GUARD_DEVICES:]}
        state.update({"failures": failures, "locked_until": locked_until})

    update_config(mutate)
    return state


def clear_guard() -> None:
    """把账本清零、立刻解锁（面板上的「立即解锁」按钮、主人改密码时用）。"""
    update_config(lambda cfg: cfg.pop("access_guard", None))


def guard_snapshot() -> dict:
    """给面板看的状态（剩余秒数由界面自己换算成「到几点几分」，设备清单从最近一次排起）。"""
    state = guard()
    return {
        "failures": state["failures"],
        "locked_seconds": guard_locked_seconds(),
        "locked_until": state["locked_until"],
        "lock_after": MAX_PASSWORD_FAILURES,
        "lock_step_seconds": LOCK_STEP_SECONDS,
        "lock_max_seconds": LOCK_MAX_SECONDS,
        "devices": guard_devices(),
    }


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


#: 隧道 / 反代写在请求头上的「真实访客地址」（按可信程度排序；键都是小写）
CLIENT_HOST_HEADERS = ("cf-connecting-ip", "x-forwarded-for")


def _first_header_value(headers: Mapping | None, name: str) -> str:
    """从头里取一个值（``x-forwarded-for`` 这种可能有好几段时只取第一段）。"""
    if not headers:
        return ""
    try:
        value = headers.get(name)
    except AttributeError:                      # 不是 Mapping（拿错东西了）：当没有
        return ""
    text = str(value or "").strip()
    if name == "x-forwarded-for" and "," in text:
        text = text.split(",", 1)[0].strip()
    return text


def client_host(peer: str, headers: Mapping | None = None) -> str:
    """这次请求在服务看来是「谁来的」（``headers`` 的键必须是小写）。

    对端**不是**本机 —— 局域网里的设备直连 —— 就只用对端地址，一个头都不看：那台设备
    能自己编 ``X-Forwarded-For``，认它等于让人随便冒充。uvicorn 自带的反代处理也是这个
    原则（只信 ``127.0.0.1`` 这种可信对端转发的头），这里再兜一层。

    对端是本机（``127.0.0.1``）时有两种可能：真的是本机浏览器，或者 cloudflared 隧道把
    外面的请求转进来了。后者带着隧道写的真实访客地址，**必须**认出来 —— 隧道连的就是
    本机回环地址，不认这些头的话，公网访客在服务看来全是「本机」，访问密码那道门等于
    没关。区分这两种也没有风险：能连上回环的本来就只有这台电脑上的程序。

    （正常情况下 uvicorn 的 ``--proxy-headers`` 已经把头换算成 ``request.client.host``
    了，这里只是不依赖那个开关 —— 少一个「换个启动方式就漏」的坑。）
    """
    address = normalize_host(peer)
    if not is_local_address(address):
        return address
    for name in CLIENT_HOST_HEADERS:
        found = _first_header_value(headers, name)
        if found and not is_local_address(found):
            return found
    return address


def trusted_devices() -> list[dict]:
    """当前被信任的设备（返回副本，调用方改它不会影响缓存里的配置）。"""
    devices = load_config_cached().get("access_trusted_devices")
    if not isinstance(devices, list):
        return []
    return [dict(row) for row in devices if isinstance(row, dict)]


def _device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ua_fingerprint(user_agent: str | None) -> str:
    """浏览器指纹：完整 UA 的 sha256 前 16 位。

    只用来判断「两条记录是不是同一台设备」——同一个来源地址 + 同一个指纹就并成一行。
    完整 UA 存下来没什么用，存摘要还省地方。
    """
    text = str(user_agent or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def device_group_key(device: dict) -> tuple[str, str]:
    """一条设备记录属于哪一台设备：来源 IP + 浏览器指纹。

    同一个浏览器重试、旧标签页又提交一次密码，都会各自留下一条记录（见
    ``remember_device()``）。它们其实是同一台设备，这里给它们同一个键，界面据此
    合并成一行。升级前写下的老记录里没有 ``ua_hash``，退回用显示名当指纹。
    """
    ip = str(device.get("ip") or "")
    fingerprint = str(device.get("ua_hash") or device.get("name") or "")
    return ip, fingerprint


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
    """把这次的设备记进信任列表，返回 (设备 id, token)；配置不可写时返回 None。

    同一台设备重复登录（浏览器重试、旧标签页又提交一次密码）会留下多条记录，这是
    允许的：每条记录各有自己的 token，谁都不会把谁挤下线。界面按
    ``device_group_key()`` 把同一台设备的记录合并成一行显示，删除时也一起删。
    """
    device_id = secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    record = {
        "id": device_id,
        #: 只存 token 的摘要：配置泄漏也换不来一台被信任的设备
        "token_hash": _device_token_hash(token),
        "name": device_name(user_agent, ip),
        #: 完整 UA 的摘要，只用于「这是不是同一台设备」的判断
        "ua_hash": _ua_fingerprint(user_agent),
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
    """不再信任某台设备；返回是否真的删掉了一台。

    界面上同一台设备是合并成一行显示的，所以这里把属于同一台设备的记录一起删
    （见 ``device_group_key()``）——否则用户点一次「不再信任」，列表里还会剩一条
    长得一模一样的，看着像没删掉。去掉这一行 = 那台设备下次要重新输访问密码。
    """
    removed = {"ok": False}
    target = str(device_id)

    def mutate(cfg: dict) -> None:
        devices = [row for row in cfg.get("access_trusted_devices") or [] if isinstance(row, dict)]
        key = next((device_group_key(row) for row in devices
                    if str(row.get("id") or "") == target), None)
        kept = devices if key is None else [row for row in devices if device_group_key(row) != key]
        removed["ok"] = len(kept) != len(devices)
        cfg["access_trusted_devices"] = kept

    update_config(mutate)
    return removed["ok"]


def forget_all_devices() -> int:
    """把信任设备全部清掉；返回清掉了几台（按「同一台设备」合并后计数）。

    计数按界面上看到的一行一台来算：同一台设备遗留的多条记录只算一台，不然提示里
    的数字会比列表里的行数还大，看着像有设备没删掉。
    """
    count = {"n": 0}

    def mutate(cfg: dict) -> None:
        devices = [row for row in cfg.get("access_trusted_devices") or [] if isinstance(row, dict)]
        count["n"] = len({device_group_key(row) for row in devices})
        cfg["access_trusted_devices"] = []

    update_config(mutate)
    return count["n"]


def list_devices(current_id: str | None = None) -> list[dict]:
    """给面板用的设备清单（token 摘要绝不返回给界面）。

    同一台设备的记录会合并成一行：代表 id 取最新那条（界面上「不再信任」用的就是
    它，``forget_device()`` 会按同一台把所有记录一起删掉），时间显示最近一次登录的。
    ``current`` 表示正在看面板的这台设备是不是这一组里的。
    """
    groups: dict[tuple[str, str], dict] = {}
    for device in trusted_devices():
        device_id = str(device.get("id") or "")
        trusted_at = int(device.get("trusted_at") or 0)
        entry = groups.get(device_group_key(device))
        if entry is None:
            entry = {
                "id": device_id,
                "name": str(device.get("name") or "未知设备"),
                "ip": str(device.get("ip") or ""),
                "trusted_at": trusted_at,
                "current": False,
            }
            groups[device_group_key(device)] = entry
        if device_id and device_id == str(current_id or ""):
            entry["current"] = True
        if trusted_at >= entry["trusted_at"]:          # 留最近那一次的信息
            entry["id"] = device_id or entry["id"]
            entry["name"] = str(device.get("name") or entry["name"])
            entry["ip"] = str(device.get("ip") or entry["ip"])
            entry["trusted_at"] = trusted_at

    rows: list[dict] = []
    for index, entry in enumerate(groups.values(), start=1):
        rows.append({
            "id": entry["id"],
            "name": entry["name"],
            "ip": entry["ip"],
            "trusted_at": entry["trusted_at"],
            "index": index,
            "current": entry["current"],
        })
    return rows


# ── 需手动确认（连续输错密码的设备）──
# 连续输错 MAX_DEVICE_FAILURES 次访问密码之后，这台设备被**暂停**：不能继续输密码，
# 要在面板的「需手动确认」里点「解除」才重新放行。判断「是不是同一台设备」跟信任设备
# 用同一把尺子（来源 IP + 浏览器指纹，见 device_group_key）。
#
# 这只是**额外**一层：它认的是 IP + 浏览器指纹，被换网络 / 关掉无痕绕过去都正常 ——
# 真正卡住「换个网络接着猜」的是上面那本全服务共用的账（``guard``），这本账不看到来
# 的人是谁。设备这一条的价值在于「把正在猜的那台单独按下暂停」，并让主人在面板上看到
# 是哪台设备在猜。
#
# 记录写进配置（重启也在），而"错了几次"只记在内存里（见 access_gate.py）——
# 重启服务会把计数清零，但已经暂停的设备不会因此被放出来。

def pending_devices() -> list[dict]:
    """当前「需手动确认」的设备（返回副本）。"""
    rows = load_config_cached().get("access_pending_devices")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def is_pending(ip: str = "", user_agent: str | None = None) -> bool:
    """这台设备是不是已经被暂停输入密码。"""
    key = (str(ip or ""), _ua_fingerprint(user_agent))
    return any(device_group_key(row) == key for row in pending_devices())


def remember_pending_device(ip: str = "", user_agent: str | None = None, *,
                            reason: str = PENDING_TOO_MANY_FAILURES,
                            failures: int = 0) -> dict:
    """把一台设备记进「需手动确认」；同一台设备只留一条，原因刷新成最新的。"""
    record = {
        "id": secrets.token_hex(8),
        "name": device_name(user_agent, ip),
        "ip": str(ip or ""),
        "ua_hash": _ua_fingerprint(user_agent),
        "reason": str(reason or PENDING_TOO_MANY_FAILURES),
        "failures": int(failures or 0),
        "added_at": int(time.time()),
    }
    key = device_group_key(record)
    try:
        def mutate(cfg: dict) -> None:
            rows = [row for row in cfg.get("access_pending_devices") or [] if isinstance(row, dict)]
            existing = next((row for row in rows if device_group_key(row) == key), None)
            if existing is not None:
                # 同一台设备又犯：沿用原来的 id 和「什么时候进来的」，界面上那一行不会跳
                record["id"] = str(existing.get("id") or record["id"])
                record["added_at"] = int(existing.get("added_at") or record["added_at"])
            kept = [row for row in rows if device_group_key(row) != key]
            kept.append(record)
            cfg["access_pending_devices"] = kept[-MAX_PENDING_DEVICES:]

        update_config(mutate)
    except OSError as exc:
        print(f"[!] 保存「需手动确认」失败（{exc}）")
    return record


def forget_pending_device(device_id: str) -> bool:
    """解除某台设备的暂停；返回是否真的解除了一台。

    界面上同一台设备是一行，所以属于同一台设备的记录一起删（跟 ``forget_device()``
    一个道理）；删掉之后那台设备可以重新输访问密码。
    """
    removed = {"ok": False}
    target = str(device_id)

    def mutate(cfg: dict) -> None:
        rows = [row for row in cfg.get("access_pending_devices") or [] if isinstance(row, dict)]
        key = next((device_group_key(row) for row in rows
                    if str(row.get("id") or "") == target), None)
        kept = rows if key is None else [row for row in rows if device_group_key(row) != key]
        removed["ok"] = len(kept) != len(rows)
        cfg["access_pending_devices"] = kept

    update_config(mutate)
    return removed["ok"]


def forget_all_pending_devices() -> int:
    """把「需手动确认」里的设备全部解除；返回解除了几台（同一台设备的记录算一台）。"""
    count = {"n": 0}

    def mutate(cfg: dict) -> None:
        rows = [row for row in cfg.get("access_pending_devices") or [] if isinstance(row, dict)]
        count["n"] = len({device_group_key(row) for row in rows})
        cfg["access_pending_devices"] = []

    update_config(mutate)
    return count["n"]


def list_pending_devices(current_ip: str = "", current_agent: str | None = None) -> list[dict]:
    """给面板「需手动确认」用的清单：一台设备一行，带上原因和进来时间。"""
    current = (str(current_ip or ""), _ua_fingerprint(current_agent))
    rows: list[dict] = []
    for index, device in enumerate(pending_devices(), start=1):
        rows.append({
            "id": str(device.get("id") or ""),
            "name": str(device.get("name") or "未知设备"),
            "ip": str(device.get("ip") or ""),
            "reason": str(device.get("reason") or PENDING_TOO_MANY_FAILURES),
            "failures": int(device.get("failures") or 0),
            "added_at": int(device.get("added_at") or 0),
            "index": index,
            "current": device_group_key(device) == current,
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
