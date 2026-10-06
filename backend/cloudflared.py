"""「公网访问」：用 Cloudflare 隧道把本机服务接到用户自己的域名。

面板「设置 → 公网访问」点「挂载到公网」之后，这里按顺序做完这件事：

1. **准备 cloudflared 程序** —— 首次开启时才下载（约 50MB，断点续传 + 实时进度）；
2. **授权** —— ``cloudflared tunnel login``：在浏览器里选中域名、点一下授权
   （本机已经有凭据就直接跳过）；
3. **建隧道** —— 同名隧道已存在就复用，不会越建越多；
4. **建解析** —— ``cloudflared tunnel route dns``：把域名指到这条隧道；
5. **起隧道** —— ``cloudflared tunnel run``，**脱离本服务独立跑**（重启面板不断线）；
6. **回头看** —— 从公网访问一次 ``/api/auth/check``，确认域名真的回到了这台电脑。

每一步的状态都放在模块级的 ``JOB`` 里，面板轮询 ``/panel/api/public/mount`` 画进度。

几条约定：

* 程序与配置放 ``config/cloudflared/``（这个目录不进仓库、更新代码也不会被冲掉），
  云端凭据仍用 cloudflared 自己的默认目录 ``~/.cloudflared/``（``login`` / ``create``
  本来就写那儿，用户手工折腾过的话也能直接复用）；
* 对外只说人话：「连不上 Cloudflare」「域名解析已被占用」这类，DNS／证书的技术细节
  不出现在接口返回里（面板按错误代码翻译文案）；
* 关掉公网访问只停进程 —— 域名、隧道、解析记录都留着，重新打开是一秒钟的事。
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from common import access as _access
from common import paths

#: 官方发布包的下载地址（按平台/架构拼文件名）
RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/{name}"

#: 下载分块（每读一块更新一次进度）
_CHUNK = 256 * 1024
#: 单次网络读取的超时：卡这么久没数据就算这次下载失败（已经下好的那截留着，重试续传）
_READ_TIMEOUT = 60
#: 服务器不给 Content-Length 时按这个估总大小（实际约 53MB），只为把进度条画出来
_FALLBACK_SIZE = 56 * 1024 * 1024
#: 下全了的程序至少这么大（官方约 50MB）—— 小了就是没下完或下到了错误页面，别拿去跑
_MIN_BINARY_SIZE = 10 * 1024 * 1024

#: cloudflared 自己的凭据目录（cert.pem 与隧道凭据都在这里）
CRED_DIR = os.path.join(os.path.expanduser("~"), ".cloudflared")
#: 我们生成的隧道配置（刻意不用 ~/.cloudflared/config.yml，免得覆盖用户手写的）
CONFIG_FILE = os.path.join(paths.CLOUDFLARED_DIR, "config.yml")
#: 隧道进程的进程号
PID_FILE = os.path.join(paths.CLOUDFLARED_DIR, "tunnel.pid")

#: 隧道连 Cloudflare 的方式：``http2`` 走 TCP 443，cloudflared 默认的 QUIC 走 UDP 7844。
#: 有些宽带/单位网络会拦 UDP，或 IPv6 只有地址却出不去，症状就是日志里反复
#: ``handshake did not complete in time`` / ``no recent network activity``，
#: 四条连接掉到只剩一条。固定成 http2 后这类握手超时基本不再出现（速度略慢一点）。
PROTOCOL = "http2"

#: Cloudflare 的接口地址。删解析记录走这里（见 delete_dns）：cloudflared 自己不会删。
_API_BASE = "https://api.cloudflare.com/client/v4"

#: 等用户在浏览器里点完授权的最长时间
AUTH_TIMEOUT = 600
#: 等隧道连上 Cloudflare 的最长时间
CONNECT_TIMEOUT = 60

#: 挂载过程的步骤（面板按这个顺序画清单）
STEPS = ("binary", "auth", "create", "dns", "start", "verify")

#: 挂载过程实时日志最多留多少行（弹窗里滚动显示）
_JOB_LOG_MAX = 200

#: 每个步骤在实时日志里的一句话（面板的步骤清单另有 i18n 文案）
_STEP_NOTES = {
    "binary": "准备 cloudflared 程序",
    "auth": "等你在浏览器里授权 Cloudflare 账号",
    "create": "创建隧道",
    "dns": "建立域名解析",
    "start": "启动隧道",
    "verify": "从公网确认一次",
}

_UA = "douyin-chat-export"

#: 合法域名：至少一段「名字.后缀」，允许子域名；协议头、端口、路径都不收
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


class TunnelError(Exception):
    """带一个短代码的错误：面板按代码翻成人话（见 panel.html 的文案表）。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def normalize_domain(value: str) -> str:
    """把用户填的域名收拾干净；不合法就抛 ``TunnelError("bad_domain")``。

    顺手容忍两种粘贴习惯：带了 ``https://`` 前缀、结尾多一个斜杠。IP 地址不收 ——
    Cloudflare 那边认的是域名，不是「一串数字」。
    """
    text = str(value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("/")
    if not _DOMAIN_RE.match(text) or _looks_like_ip(text):
        raise TunnelError("bad_domain")
    return text


def _looks_like_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


# ── 程序文件：首次开启时下载 ──

def binary_name() -> str:
    """官方发布包里的文件名（本地也用它当文件名）。"""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine or "amd64")
    system = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(sys.platform, sys.platform)
    suffix = ".exe" if system == "windows" else ""
    return f"cloudflared-{system}-{arch}{suffix}"


def binary_path() -> str:
    """cloudflared 在哪儿：先看我们下载的那份，再用系统里装的。

    下载没下完（或下到了错误页面）的文件按「没有」处理，免得拿一个坏程序去跑。
    """
    local = os.path.join(paths.CLOUDFLARED_DIR, binary_name())
    if os.path.isfile(local) and os.path.getsize(local) >= _MIN_BINARY_SIZE:
        return local
    return shutil.which("cloudflared") or ""


def has_binary() -> bool:
    return bool(binary_path())


def _partial_path() -> str:
    return os.path.join(paths.CLOUDFLARED_DIR, binary_name() + ".part")


def download_binary(progress=None, cancelled=None) -> str:
    """下载 cloudflared（支持断点续传、中途可取消），返回可执行文件路径。

    ``progress(received, total)`` 每读一块调一次；``total`` 可能为 0（服务器没给长度）。
    ``cancelled()`` 返回真就停手 —— 已经下好的那截留在 ``.part`` 里，下次接着下。
    """
    os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
    target = os.path.join(paths.CLOUDFLARED_DIR, binary_name())
    part = _partial_path()
    received = os.path.getsize(part) if os.path.isfile(part) else 0
    request = urllib.request.Request(
        RELEASE_URL.format(name=binary_name()),
        headers={"User-Agent": _UA, "Range": f"bytes={received}-"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=_READ_TIMEOUT)
    except OSError as exc:
        raise TunnelError("download_failed", str(exc)) from exc
    with response:
        # 206 = 服务器同意续传；200 = 它不支持，只能从头再来
        if received and getattr(response, "status", 200) != 206:
            received = 0
        total = received + int(response.headers.get("Content-Length") or 0)
        if progress:
            progress(received, total)
        with open(part, "ab" if received else "wb") as handle:
            while True:
                if cancelled and cancelled():
                    raise TunnelError("cancelled")
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if progress:
                    progress(received, total)
    if received < _MIN_BINARY_SIZE:
        # 没下全，或者服务器回了一页 HTML：这一份不能用，也别留着污染下次续传
        try:
            os.remove(part)
        except OSError:
            pass
        raise TunnelError("download_failed", f"too small ({received} bytes)")
    os.replace(part, target)
    if os.name != "nt":
        os.chmod(target, 0o755)
    return target


def ensure_binary(progress=None, cancelled=None) -> str:
    """保证有 cloudflared 可用：本地有就用，没有才下载。"""
    found = binary_path()
    if found:
        return found
    return download_binary(progress, cancelled)


# ── 跑一次性命令 ──

def is_elevated() -> bool:
    """本服务是不是以管理员 / root 身份在跑。

    为什么要问这个：以管理员身份启动时，`cloudflared tunnel login` 让它去开浏览器会被
    Windows 拦下来（Edge 会弹「现有实例正在以提升的权限运行」），用户就卡在授权那一步。
    面板据此提前给一句提示，并把手动链接摆出来。
    """
    if os.name != "nt":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token = wintypes.HANDLE()
        # TOKEN_QUERY = 0x0008，TokenElevation = 20
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                         ctypes.byref(token)):
            return False
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD(ctypes.sizeof(elevation))
            ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(elevation),
                                              size, ctypes.byref(size))
            return bool(ok and elevation.value)
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError):
        return False


def _run(args: list[str], timeout: float = 120) -> tuple[int, str]:
    """跑一次 cloudflared 并等它结束（list / create / route / delete 这类）。

    stdout 与 stderr 合在一起返回 —— cloudflared 的日志全写在 stderr 上。
    """
    binary = binary_path()
    if not binary:
        return 1, "cloudflared not found"
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=_env(), **_hidden_flags(),
        )
    except subprocess.TimeoutExpired:
        _log_command(args, 124, "timeout")
        return 1, "timeout"
    except OSError as exc:
        _log_command(args, 1, str(exc))
        return 1, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    _log_command(args, proc.returncode, output)
    return proc.returncode, output


#: 日志文件开头写一句时间说明：cloudflared 用的是世界时，用户拿它对自己的钟会以为坏了。
#: 面板「日志」页读这份文件时会按本机时间显示，所以这句只针对直接打开文件的人。
_TUNNEL_LOG_HEADER = (
    "# cloudflared 与面板共同写这份日志：行首时间是世界时（UTC，结尾带 Z），"
    "不是本机时间；面板「日志 → 公网挂载」里已按本机时间显示\n"
)


def _append_tunnel_log(prefix: str, line: str) -> None:
    """往 ``config/logs/cloudflared.log`` 追加一行（面板「日志 → 公网挂载」看的就是它）。"""
    try:
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fresh = not os.path.exists(paths.CLOUDFLARED_LOG) or os.path.getsize(paths.CLOUDFLARED_LOG) == 0
        with open(paths.CLOUDFLARED_LOG, "a", encoding="utf-8") as handle:
            if fresh:
                handle.write(_TUNNEL_LOG_HEADER)
            handle.write(f"{stamp} {prefix} {line}\n")
    except OSError:
        pass                                   # 写日志失败绝不能影响正经流程


def _job_log(line: str) -> None:
    """往「这一次挂载」的实时日志里写一行（弹窗滚动显示的就是它）。"""
    JOB.add_log(line)


#: Cloudflare 自己公布的地址段（IPv4 + IPv6）。``api.cloudflare.com`` 只可能落在这些里面，
#: 落在外面基本就说明是「被解析坏了」（运营商/路由器的 DNS 劫持），得直接告诉用户。
_CF_RANGES = (
    "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "162.159.0.0/16",
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)


def _api_addresses(host: str = "api.cloudflare.com") -> list[str]:
    """本机把 ``host`` 解析成了哪些地址（解析不了就返回空表）。"""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        return []
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def bad_api_addresses(host: str = "api.cloudflare.com") -> list[str]:
    """解析结果里**不属于 Cloudflare** 的那些地址（干净的解析返回空表）。

    用于失败时给一句人话提示：域名被解析到莫名其妙的地方时，用户再怎么点授权也没用。
    """
    try:
        networks = [ipaddress.ip_network(item) for item in _CF_RANGES]
    except ValueError:
        return []
    bad = []
    for address in _api_addresses(host):
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not any(parsed in network for network in networks):
            bad.append(address)
    return bad


def _log_command(args: list[str], code: int, output: str) -> None:
    """把一次性命令的输出追加进隧道日志（``config/logs/cloudflared.log``）。

    面板「日志 → 公网挂载」看的就是这份文件：光有隧道进程自己的输出不够 —— 挂载失败
    往往就失败在 ``create`` / ``route dns`` 这一步，事后要能翻到它们当时说了什么。
    行首一律打 ``[panel]``，和隧道进程自己的日志区分开；同一批内容也进挂载弹窗的
    实时日志（少一个时间戳前缀）。
    """
    text = str(output or "")
    _append_tunnel_log("INF [panel]", f"cloudflared {' '.join(args)} → exit {code}")
    _job_log(f"$ cloudflared {' '.join(args)} → exit {code}")
    for line in text.splitlines():
        _append_tunnel_log("INF [panel]", line)
        _job_log(line)


def _hidden_flags(*, detached: bool = False) -> dict:
    """子进程一律**不弹可见窗口**；常驻的那个还要脱离本服务。

    Windows 上分两种：

    * 一次性命令（``tunnel list`` / ``create`` / ``route dns`` / ``login``）→
      ``CREATE_NO_WINDOW``：不给它控制台，也就不会闪一个黑框；
    * 常驻的 ``tunnel run`` → ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``：
      同样没有窗口，而且面板重启 / 退出不会把它一起带走。

    再叠一层 ``STARTUPINFO`` 的 ``SW_HIDE``：有些环境是从别的控制台继承过来的，
    只认这个（多一层保险，没有副作用）。POSIX 上不弹窗是天然行为，只在需要时加
    ``start_new_session`` 达到「脱离本服务」的同样效果。
    """
    if os.name != "nt":
        return {"start_new_session": True} if detached else {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": flags, "startupinfo": startupinfo}


def _parse_tunnel_table(text: str) -> list[dict]:
    """从 ``tunnel list`` 的表格里抠出 id + 名字（JSON 走不通时的退路）。"""
    rows = []
    for line in str(text or "").splitlines():
        match = re.match(r"\s*([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})\s+(\S+)", line)
        if match:
            rows.append({"id": match.group(1), "name": match.group(2)})
    return rows


def _normalize_rows(data) -> list[dict]:
    """把解析出来的对象整理成 ``{"id", "name"}``（不同版本字段大小写不一样）。"""
    rows = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        uid = row.get("id") or row.get("ID") or row.get("uuid") or row.get("tunnelId")
        name = row.get("name") or row.get("Name")
        if uid and name:
            rows.append({"id": str(uid), "name": str(name)})
    return rows


def list_tunnels() -> list[dict]:
    """云端已有的隧道；问不到就返回空列表（调用方按「还没建」继续）。

    输出格式在版本之间变过：可能是 JSON 数组、可能是一行一个 JSON、也可能还是老表格，
    三种都认（认不出来的行会被丢掉，不影响结果）。
    """
    code, out = _run(["tunnel", "list", "--output", "json"], timeout=60)
    if code == 0:
        start = out.find("[")
        if start >= 0:
            try:
                rows = _normalize_rows(json.loads(out[start:]))
            except ValueError:
                rows = []
            if rows:
                return rows
        lines = []
        for line in str(out).splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    pass
        rows = _normalize_rows(lines)
        if rows:
            return rows
    return _parse_tunnel_table(out)


def tunnel_id(name: str) -> str:
    """云端这条隧道的 UUID；没有就返回空串。"""
    for row in list_tunnels():
        if str(row.get("name") or "") == name:
            return str(row.get("id") or "")
    return ""


def ensure_tunnel(name: str) -> str:
    """保证云端有这条隧道，返回它的 UUID（已存在就复用）。"""
    found = tunnel_id(name)
    if found:
        return found
    code, out = _run(["tunnel", "create", name], timeout=120)
    match = re.search(r"with id ([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})", out)
    if match:
        return match.group(1)
    found = tunnel_id(name)                      # 输出格式变了也不要紧，再看一眼列表
    if found:
        return found
    if code != 0:
        raise TunnelError("create_failed", out.strip()[-300:])
    raise TunnelError("create_failed", out.strip()[-300:])


def write_config(tunnel_uuid: str, domain: str) -> str:
    """写隧道配置：这个域名 → 本机 8000，其它地址一律 404。"""
    os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
    # 凭据文件跟着 cert.pem 待在同一个目录（cloudflared 的 create 也是写在那儿）
    credentials = os.path.join(os.path.dirname(cert_path()), f"{tunnel_uuid}.json")
    text = "\n".join([
        f"tunnel: {tunnel_uuid}",
        f"credentials-file: {credentials}",
        "no-autoupdate: true",
        "loglevel: info",
        f"protocol: {PROTOCOL}",
        "ingress:",
        f"  - hostname: {domain}",
        "    service: http://127.0.0.1:8000",
        "  - service: http_status:404",
        "",
    ])
    with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
        handle.write(text)
    return CONFIG_FILE


def route_dns(name: str, domain: str, *, overwrite: bool = False) -> tuple[bool, str]:
    """把域名指到这条隧道；返回 ``(成功, 说明)``。

    ``overwrite=True`` 时带上 ``--overwrite-dns``（覆盖已存在的记录）—— 开关必须排在
    位置参数**前面**，cloudflared 只认这个顺序。

    失败时把 cloudflared 的原始输出当说明带回去（面板只在「详细信息」里显示，
    正常的提示语由错误代码翻译）。
    """
    args = ["tunnel", "route", "dns"]
    # 开关必须排在位置参数**前面**：放后面会被 cloudflared 当成多出来的参数，
    # 直接报「This command expects the format "cloudflared tunnel route dns
    # <tunnel name/id> <hostname>"」然后失败（2026-10-06「把解析改到这台电脑」
    # 的按钮就是这么挂的）。
    if overwrite:
        args.append("--overwrite-dns")
    args += [name, domain]
    code, out = _run(args, timeout=120)
    if code == 0:
        return True, out
    return False, out


def delete_tunnel(name: str) -> bool:
    code, _ = _run(["tunnel", "delete", "-f", name], timeout=120)
    return code == 0


#: cloudflared 遇到不认识的开关时打的这句话 —— 打完它**退出码还是 0**，光看退出码会被骗
_BAD_USAGE_MARKERS = ("incorrect usage", "flag provided but not defined")


def looks_like_bad_usage(output: str) -> bool:
    """这段输出是不是「开关不存在」（cloudflared 对不认识的开关照样退出 0）。"""
    text = str(output or "").lower()
    return any(marker in text for marker in _BAD_USAGE_MARKERS)


def argo_credentials() -> dict:
    """读出 ``cert.pem`` 里的 Cloudflare 接口凭据。

    这份文件是 ``cloudflared tunnel login`` 的产物（``ARGO TUNNEL TOKEN`` 里那段 base64），
    解开是一小段 JSON：``zoneID`` / ``accountID`` / ``apiToken``。cloudflared 自己只会
    「建解析」和「覆盖解析」，**没有删解析的功能** —— 所以删记录得拿这份凭据直接调接口。
    没登录过、文件坏了就返回空字典。
    """
    try:
        with open(cert_path(), encoding="utf-8") as handle:
            body = "".join(line.strip() for line in handle if "-----" not in line)
    except OSError:
        return {}
    if not body:
        return {}
    try:
        data = json.loads(base64.b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or not data.get("apiToken") or not data.get("zoneID"):
        return {}
    return data


def api_error(payload: dict) -> str:
    """把接口返回的报错压成一句话（给人看，不含凭据）。"""
    errors = payload.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    message = str(first.get("message") or "接口没有返回原因")
    code = first.get("code")
    return f"{message}（code {code}）" if code else message


def api_call(path: str, *, method: str = "GET", timeout: float = 30) -> tuple[bool, dict]:
    """调一次 Cloudflare 接口；返回 ``(成功, 响应 JSON)``，失败的响应里带原因。"""
    creds = argo_credentials()
    if not creds:
        return False, {"errors": [{"message": "没有 Cloudflare 凭据"}]}
    request = urllib.request.Request(
        _API_BASE + path, method=method,
        headers={"Authorization": "Bearer " + str(creds["apiToken"]),
                 "User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, OSError):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    return bool(payload.get("success")), payload


def tunnel_dns_record(domain: str) -> tuple[str, str, str]:
    """在 Cloudflare 上找 ``domain`` 那条**指向隧道**的解析记录。

    返回 ``(zoneID, 记录 ID, 说明)``：``zoneID`` 为空表示接口这条路根本不通；
    记录 ID 为空表示查过了、没有可删的隧道记录（说明里写原因）。

    只认 ``CNAME → *.cfargotunnel.com``：域名上万一挂着用户自己别的记录，
    绝不能替他删掉。
    """
    zone = str(argo_credentials().get("zoneID") or "")
    if not zone:
        return "", "", "没有 Cloudflare 凭据"
    query = urllib.parse.urlencode({"type": "CNAME", "name": domain})
    ok, payload = api_call(f"/zones/{zone}/dns_records?{query}")
    if not ok:
        return "", "", api_error(payload)
    for record in payload.get("result") or []:
        name = str(record.get("name", ""))
        content = str(record.get("content", ""))
        if name.lower() == domain.lower() and content.endswith(".cfargotunnel.com"):
            return zone, str(record.get("id") or ""), ""
    return zone, "", "这个域名上没有指向隧道的解析记录"


def delete_dns(name: str, domain: str) -> bool:
    """删掉域名那条解析记录（「彻底移除」用）。

    cloudflared 自己**没有**「删解析」这个功能：``route dns`` 只有建和覆盖，老代码发的
    ``--delete`` 根本不是它的开关 —— 而且它遇到不认识的开关时，打完 Incorrect Usage
    **照样退出 0**。于是「以为删掉了、其实没删」，还把面板该给的那句「请到 Cloudflare
    的 DNS 页面手动删一下」一起吞掉了（2026-10-06 修的就是这个）。

    现在优先走 Cloudflare 接口把记录真正删掉；接口这条路不通时才退回命令行，
    并且认得出上面那种假成功。
    """
    zone, record_id, reason = tunnel_dns_record(domain)
    if not zone:
        # 接口这条路不通（没凭据 / 请求失败）：退回命令行，但不当它是假成功
        _append_tunnel_log("INF [panel]", f"删解析记录 {domain} → 改走命令行：{reason}")
        code, out = _run(["tunnel", "route", "dns", "--delete", name, domain], timeout=120)
        return code == 0 and not looks_like_bad_usage(out)
    if not record_id:
        _append_tunnel_log("INF [panel]", f"删解析记录 {domain} → 没有需要删的隧道记录（{reason}）")
        return True
    ok, payload = api_call(f"/zones/{zone}/dns_records/{record_id}", method="DELETE")
    if ok:
        _append_tunnel_log("INF [panel]", f"删解析记录 {domain} → 成功")
        return True
    _append_tunnel_log("INF [panel]", f"删解析记录 {domain} → 失败：{api_error(payload)}")
    return False


def overwrite_dns() -> tuple[bool, str]:
    """把域名解析改成指向**本机这条**隧道（面板「把解析改到这台电脑」按钮用）。

    平时挂载绝不覆盖别人的记录；只有用户明确点了这个按钮，才带 ``--overwrite-dns``。
    改成功就把「记录已存在」那句提示收掉 —— 它已经不成立了。
    """
    name = _access.public_tunnel_name()
    domain = _access.public_domain()
    if not domain:
        return False, "no_domain"
    ok, out = route_dns(name, domain, overwrite=True)
    if ok:
        _append_tunnel_log("INF [panel]",
                           f"按用户要求把 {domain} 的解析改指向本机这条隧道（覆盖了原有记录）")
        JOB.clear_warning()
        return True, ""
    return False, out.strip()[-300:]


# ── 授权 ──

def cert_path() -> str:
    """云端的登录凭据（``cert.pem``）放在哪。

    **优先项目目录**里那份（``config/cloudflared/cert.pem``）：有些机器上的安全策略会
    **按程序路径**挡住 cloudflared 往 ``~/.cloudflared`` 写（实测同一份程序换个位置就能
    写进去），放项目目录既不撞那个规则，也能跟 config.yml、隧道凭据放一起、搬家时一起带走。

    旧机器上手工跑过 cloudflared、``~/.cloudflared/cert.pem`` 已经在的话继续兼容它 ——
    不然那些人得重新授权一次。
    """
    project = os.path.join(paths.CLOUDFLARED_DIR, "cert.pem")
    for candidate in (project, os.path.join(CRED_DIR, "cert.pem")):
        try:
            if os.path.getsize(candidate) > 0:
                return candidate
        except OSError:
            continue
    return project


def _env() -> dict:
    """跑 cloudflared 时给它的环境变量：把登录凭据固定到 ``cert_path()``。

    这样 ``login`` 写在哪儿、``create`` / ``route dns`` / ``run`` 就从哪儿读，不会各找各的。
    """
    return {**os.environ, "TUNNEL_ORIGIN_CERT": cert_path()}


def logged_in() -> bool:
    """本机有没有可用的 Cloudflare 登录凭据（``cloudflared tunnel login`` 的产物）。"""
    try:
        return os.path.getsize(cert_path()) > 0
    except OSError:
        return False


def _can_write_with(runner: str, folder: str) -> bool:
    """这个可执行文件能不能在 ``folder`` 里写文件（拿 cloudflared 自己的 ``--logfile`` 当探针）。

    为什么要探：有些机器上的安全策略是**按程序路径**生效的 —— 同一个 cloudflared，放在项目
    目录里往 ``~/.cloudflared`` 写就被拒（``Access is denied``），复制到别处就能写。登录这一步
    要往磁盘上落 ``cert.pem``，所以得先知道它写不写得下去，别等用户点完授权才发现证书落不下来。
    """
    probe = os.path.join(folder, f"cf-write-probe-{os.getpid()}.log")
    try:
        os.makedirs(folder, exist_ok=True)
        subprocess.run([runner, "--logfile", probe, "tunnel", "list"],
                       capture_output=True, timeout=30, **_hidden_flags())
        return os.path.getsize(probe) > 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _size(path: str) -> int:
    """文件大小；不存在或读不了都当 0（省得到处写 try/except）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _adopt_legacy_cert() -> None:
    """cloudflared 可能把证书写到了它自己的默认位置：搬到我们指定的地方。

    ``TUNNEL_ORIGIN_CERT`` 我们一直传着，但万一某个版本不认（或者用户手工登录过），
    证书就落在 ``~/.cloudflared/cert.pem`` —— 搬进项目目录，后续命令才好统一从一处读。
    """
    target = os.path.join(paths.CLOUDFLARED_DIR, "cert.pem")
    legacy = os.path.join(CRED_DIR, "cert.pem")
    if _size(target) > 0 or _size(legacy) <= 0:
        return
    try:
        os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
        shutil.move(legacy, target)
    except OSError:
        pass


class LoginSession:
    """一次 ``cloudflared tunnel login``：把授权网址交给面板，等用户在浏览器里点完。

    进程的输出**不能丢**：授权失败时 cloudflared 自己会把原因写在里面（比如它连不上
    ``api.cloudflare.com``），而用户看到的只是「失败」两个字。这里逐行收下来：
    进挂载弹窗的实时日志、进 ``config/logs/cloudflared.log``（面板「日志 → 公网挂载」），
    失败时最后几行还会带进错误详情。
    """

    _URL_RE = re.compile(r"https://dash\.cloudflare\.com/\S+")
    #: 留最近多少行（够看清原因，又不至于把日志撑爆）
    _KEEP_LINES = 40

    def __init__(self):
        self.url = ""
        self.output: list[str] = []
        self.finished = False                   # 进程自己退出了（不是我们等超时）
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._temp_dir = ""                     # 用临时副本跑时，用完要收拾掉

    def _runner(self) -> str:
        """拿哪个可执行文件跑登录。

        默认就是项目里那个；**只有在它写不进 ``~/.cloudflared`` 时**才改用一份临时副本 ——
        那类"按程序路径"生效的安全策略，换一份副本就能写进去（实测如此）。这样不管
        cloudflared 认不认 ``TUNNEL_ORIGIN_CERT``（写项目目录还是写它自己的默认位置），
        证书都能落下来；临时副本用完删掉。
        """
        binary = binary_path() or ""
        if not binary:
            return ""
        if _can_write_with(binary, CRED_DIR):
            return binary
        try:
            self._temp_dir = tempfile.mkdtemp(prefix="cloudflared-login-")
            runner = os.path.join(self._temp_dir, os.path.basename(binary))
            shutil.copy2(binary, runner)
            _job_log("登录时改用一份临时副本运行 cloudflared（这台电脑的策略不让它原地写凭据）")
            return runner
        except OSError:
            return binary

    def start(self) -> None:
        binary = self._runner()
        if not binary:
            raise TunnelError("binary_missing")
        os.makedirs(os.path.dirname(cert_path()), exist_ok=True)
        os.makedirs(CRED_DIR, exist_ok=True)     # 万一它还是往默认位置写，也别因为目录不在而失败
        self._proc = subprocess.Popen(
            [binary, "tunnel", "login"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=_env(), **_hidden_flags(),
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        """把 cloudflared 的输出读出来：认出授权网址，顺手记进日志。"""
        proc = self._proc
        if proc is None or getattr(proc, "stdout", None) is None:
            return
        try:
            for line in proc.stdout:
                text = line.rstrip()
                if not text:
                    continue
                self.output.append(text)
                if len(self.output) > self._KEEP_LINES:
                    del self.output[: len(self.output) - self._KEEP_LINES]
                _append_tunnel_log("[login]", text)
                _job_log(text)
                match = self._URL_RE.search(text)
                if match and not self.url:
                    self.url = match.group(0).rstrip('.,)>"\'')
        except (OSError, ValueError):
            pass

    def wait(self, timeout: float = AUTH_TIMEOUT, cancelled=None) -> bool:
        """等凭据文件出现（用户在浏览器里点完授权就有了）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            _adopt_legacy_cert()                # 它可能写到了自己的默认位置，随时搬过来
            if logged_in():
                return True
            if cancelled and cancelled():
                return False
            proc = self._proc
            if proc is not None and proc.poll() is not None and not logged_in():
                self.finished = True
                time.sleep(0.3)                 # 进程刚退出，文件可能还在落盘
                _adopt_legacy_cert()
                return logged_in()
            time.sleep(0.5)
        return False

    def tail(self, lines: int = 6) -> str:
        """最后几行输出（失败时给用户看的那段）。"""
        return "\n".join(self.output[-lines:])

    def stop(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass
        if self._temp_dir:                      # 临时副本用完就删，别在系统里留垃圾
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = ""


# ── 隧道进程（脱离本服务独立跑）──

def pid_alive(pid: int) -> bool:
    """这个进程号还在不在。

    Windows 上**不能**用 ``os.kill(pid, 0)`` 探活 —— 那是 TerminateProcess，会把进程
    直接杀掉，所以那边改用 OpenProcess 问一句（和 tools/restart_server.py 一个思路）。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, 0, int(pid))       # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5                  # 5 = 权限不足，也算活着
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid() -> int:
    try:
        with open(PID_FILE, encoding="utf-8") as handle:
            return int((handle.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def tunnel_running() -> bool:
    """隧道进程还在跑吗（进程号 + 进程真的存在，两步都看）。"""
    return pid_alive(read_pid())


def start_tunnel(name: str) -> int:
    """后台拉起隧道，返回进程号；同时把输出追加到 ``config/logs/cloudflared.log``。"""
    binary = binary_path()
    if not binary:
        raise TunnelError("binary_missing")
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    os.makedirs(paths.CLOUDFLARED_DIR, exist_ok=True)
    log = open(paths.CLOUDFLARED_LOG, "ab")
    try:
        proc = subprocess.Popen(
            [binary, "tunnel", "--config", CONFIG_FILE, "run", name],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            cwd=paths.CLOUDFLARED_DIR, env=_env(), **_hidden_flags(detached=True),
        )
    except OSError as exc:
        log.close()
        raise TunnelError("start_failed", str(exc)) from exc
    log.close()
    with open(PID_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    return proc.pid


def stop_tunnel() -> bool:
    """停掉隧道进程（连同它的子进程）；没在跑就返回 False。"""
    pid = read_pid()
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, **_hidden_flags())
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return bool(pid)


def _log_offset() -> int:
    try:
        return os.path.getsize(paths.CLOUDFLARED_LOG)
    except OSError:
        return 0


def _log_text(offset: int) -> str:
    try:
        with open(paths.CLOUDFLARED_LOG, "rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _read_delta(cursor: int) -> tuple[str, int]:
    """从 ``cursor`` 字节处读到文件末尾；返回 ``(文本, 新游标)``。

    用字节游标（不是行号）是因为日志还在被隧道进程追加写，按字节推进才不会重复读。
    """
    try:
        with open(paths.CLOUDFLARED_LOG, "rb") as handle:
            handle.seek(cursor)
            data = handle.read()
    except OSError:
        return "", cursor
    return data.decode("utf-8", "replace"), cursor + len(data)


def wait_connected(offset: int = 0, timeout: float = CONNECT_TIMEOUT, cancelled=None) -> bool:
    """等日志里出现「连接已注册」——那才算隧道真的连上了 Cloudflare。

    ``offset`` 是启动前日志的长度：只看这次启动之后新写的那一段，免得把上一次的
    「已注册」当成这一次的成功。等的过程里把新写出的行实时发给挂载日志（弹窗滚动
    显示），这样卡住时用户能看见 cloudflared 当时在说什么。
    """
    deadline = time.time() + timeout
    cursor = offset
    tail = ""
    seen = ""
    while time.time() < deadline:
        text, cursor = _read_delta(cursor)
        if text:
            seen += text
            tail += text
            *lines, tail = tail.split("\n")     # 最后一段可能还没写完，留着下次
            for line in lines:
                if line.strip():
                    _job_log(line)
        if "Registered tunnel connection" in seen:
            return True
        if not tunnel_running():
            return False
        if cancelled and cancelled():
            return False
        time.sleep(0.5)
    return False


def verify_public_url(domain: str, timeout: float = 20) -> bool | None:
    """从公网绕一圈回来看看：这个域名是不是真的回到了这台电脑。

    走 ``/api/auth/check``（不需要登录）：能解析出这个接口特有的 JSON 就算通。
    解析记录指向别处、或还没生效时返回 False；网络本身不通返回 None（不算失败，
    只是「没验成」）。
    """
    request = urllib.request.Request(
        f"https://{domain}/api/auth/check", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return False                                # 有东西在应答，但不是这个接口
    except ValueError:
        return False
    except OSError:
        return None                                 # 网络本身不通：验不了，不当失败
    return isinstance(data, dict) and "need_password" in data


# ── 一次「挂载到公网」的过程 ──

class MountJob:
    """挂载过程的状态（面板每秒钟轮询一次，用来画进度清单和下载进度条）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._login: LoginSession | None = None
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.state = "idle"                     # idle / running / done / error
            self.step = ""
            self.steps = {key: "pending" for key in STEPS}
            self.error = ""
            self.error_detail = ""
            self.auth_url = ""
            self.domain = ""
            self.download = None                    # {"received","total","percent"}
            self.warning = ""
            self.started_at = 0
            self.log = []                           # 这次挂载的实时日志（面板弹窗里滚动显示）
            self._download_logged = -1              # 下载进度已经记到第几个 10%

    # —— 给流程用的内部方法 ——

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def add_log(self, line: str) -> None:
        """往这次挂载的日志里追加一行（只留最近 ``_JOB_LOG_MAX`` 行）。

        内容有三路：我们自己的步骤提示、面板跑的一次性命令输出、隧道进程的原始输出。
        面板弹窗、「日志 → 公网挂载」看的都是同一批东西。
        """
        text = str(line or "").rstrip()
        if not text:
            return
        stamp = time.strftime("%H:%M:%S", time.localtime())
        with self._lock:
            self.log.append(f"[{stamp}] {text}")
            if len(self.log) > _JOB_LOG_MAX:
                del self.log[: len(self.log) - _JOB_LOG_MAX]

    def begin_step(self, key: str) -> None:
        with self._lock:
            self.step = key
            self.steps[key] = "running"
        self.add_log(f"— {_STEP_NOTES.get(key, key)} —")

    def finish_step(self, key: str) -> None:
        with self._lock:
            self.steps[key] = "done"

    def fail_step(self, key: str) -> None:
        with self._lock:
            self.steps[key] = "error"

    def clear_warning(self) -> None:
        """收掉挂载留下的提示（用户按过「把解析改到这台电脑」之后，提示就不成立了）。"""
        with self._lock:
            self.warning = ""

    def set_download(self, received: int, total: int) -> None:
        """下载进度；每跨过 10% 往实时日志里记一句（免得日志被进度刷屏）。"""
        total = total or _FALLBACK_SIZE
        percent = max(0, min(100, int(received * 100 / total))) if total else 0
        with self._lock:
            self.download = {"received": received, "total": total, "percent": percent}
            logged = self._download_logged
        bucket = percent // 10
        if bucket > logged:
            self._download_logged = bucket
            self.add_log("下载 cloudflared… %d%%（%.1f / %.1f MB）"
                         % (percent, received / 1048576, total / 1048576))

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # —— 面板用的公开接口 ——

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "step": self.step,
                "steps": dict(self.steps),
                "error": self.error,
                "error_detail": self.error_detail,
                "auth_url": self.auth_url,
                "domain": self.domain,
                "download": dict(self.download) if self.download else None,
                "warning": self.warning,
                "log": list(self.log),
                "running": self.state == "running",
            }

    def cancel(self) -> dict:
        """用户点了取消：停掉授权进程，流程会在下一个检查点收工。

        取消标志**不能**在这里清掉 —— 后台线程正是靠它知道该停的；下一次
        ``start()`` 才会把它清干净。
        """
        self._cancel.set()
        if self._login:
            self._login.stop()
        self._set(state="idle", step="", auth_url="", download=None)
        return self.snapshot()

    def fail(self, code: str) -> dict:
        """还没开始就失败了（比如没开局域网、没设访问密码）。"""
        self.reset()
        self._set(state="error", error=code)
        return self.snapshot()

    def start(self, domain: str) -> dict:
        """开始挂载（后台线程里跑；重复点就重来一次）。"""
        self._cancel.clear()
        self.reset()
        self._set(state="running", domain=domain, started_at=time.time())
        self._thread = threading.Thread(target=self._run_flow, args=(domain,), daemon=True)
        self._thread.start()
        return self.snapshot()

    # —— 真正的流程 ——

    def _run_flow(self, domain: str) -> None:
        tunnel_name = _access.public_tunnel_name()
        self.add_log(f"开始挂载：域名 {domain}，隧道 {tunnel_name}")
        try:
            self._step_binary()
            self._step_auth()
            tunnel_uuid = self._step_create(tunnel_name)
            self._step_dns(tunnel_name, domain)
            self._step_start(tunnel_name, tunnel_uuid, domain)
            self._step_verify(domain)
            _access.set_public_access(enabled=True, domain=domain, tunnel=tunnel_name)
            self._set(state="done", step="")
            self.add_log(f"挂载完成：https://{domain} 现在能打开了")
        except TunnelError as exc:
            if exc.code == "cancelled":
                self.add_log("已取消")
                self._set(state="idle", step="")
                return
            with self._lock:
                if self.step in self.steps:
                    self.steps[self.step] = "error"
            self.add_log(f"失败：{exc.code} {exc.detail}".strip())
            self._set(state="error", error=exc.code, error_detail=exc.detail)
        except Exception as exc:                    # 兜底：别让线程静默死掉
            with self._lock:
                if self.step in self.steps:
                    self.steps[self.step] = "error"
            self.add_log(f"失败：{exc}")
            self._set(state="error", error="unknown", error_detail=str(exc))

    def _step_binary(self) -> None:
        self.begin_step("binary")
        binary = binary_path()
        if binary:
            self.add_log(f"使用已有的 cloudflared：{binary}")
        ensure_binary(self.set_download, self.cancelled)
        self._set(download=None)
        self.finish_step("binary")

    def _step_auth(self) -> None:
        self.begin_step("auth")
        if logged_in():
            self.add_log("本机已有 Cloudflare 登录凭据，跳过授权")
            self.finish_step("auth")
            return
        session = LoginSession()
        self._login = session
        session.start()
        # 把授权网址给面板（cloudflared 要一两秒才打印出来）
        deadline = time.time() + 20
        while time.time() < deadline and not session.url and not self.cancelled():
            time.sleep(0.3)
        self._set(auth_url=session.url)
        self.add_log("授权链接已就绪，请在浏览器里选中域名并点「授权」" if session.url
                     else "正在等 cloudflared 打印授权链接…")
        ok = session.wait(cancelled=self.cancelled)
        session.stop()
        self._login = None
        _adopt_legacy_cert()
        if self.cancelled():
            raise TunnelError("cancelled")
        if not ok:
            # 证书其实落地了、只是进程收尾不漂亮：也算成功，别让用户白授权一次
            ok = logged_in()
        if not ok:
            # 失败原因要分清楚，别再一律报「超时」：
            # ① 域名被解析到 Cloudflare 之外 → 云端根本收不到，用户点多少次都没用；
            # ② cloudflared 自己退出了 → 它最后几行就是原因；
            # ③ 真的只是等太久。
            bad = bad_api_addresses()
            if bad:
                # 解析到哪去了写进实时日志（失败那屏就留着它），错误详情只放 cloudflared 原话
                self.add_log("api.cloudflare.com 被解析到：" + "、".join(bad) + "（不是 Cloudflare 的地址）")
                raise TunnelError("auth_dns", session.tail())
            if session.finished:
                raise TunnelError("auth_failed", session.tail())
            raise TunnelError("auth_timeout", session.tail())
        self.add_log("授权完成，凭据已写入本机")
        self.finish_step("auth")

    def _step_create(self, tunnel_name: str) -> str:
        self.begin_step("create")
        if self.cancelled():
            raise TunnelError("cancelled")
        tunnel_uuid = ensure_tunnel(tunnel_name)
        self.add_log(f"隧道就绪：{tunnel_name}（{tunnel_uuid}）")
        self.finish_step("create")
        return tunnel_uuid

    def _step_dns(self, tunnel_name: str, domain: str) -> None:
        self.begin_step("dns")
        if self.cancelled():
            raise TunnelError("cancelled")
        ok, out = route_dns(tunnel_name, domain)
        if not ok:
            # 记录已经存在是最常见的情况：可能本来就指着这条隧道，也可能是别人的。
            # 不硬覆盖，交给用户决定；下面第 6 步的「回头看」会告诉他到底通没通。
            if re.search(r"already exists|record with that name", out, re.I):
                self.add_log(f"{domain} 上已经有一条解析记录，没有替你改动")
                with self._lock:
                    self.warning = "dns_exists"
                self.finish_step("dns")
                return
            raise TunnelError("dns_failed", out.strip()[-300:])
        self.add_log(f"解析已建好：{domain} → 这条隧道")
        self.finish_step("dns")

    def _step_start(self, tunnel_name: str, tunnel_uuid: str, domain: str) -> None:
        self.begin_step("start")
        if self.cancelled():
            raise TunnelError("cancelled")
        offset = _log_offset()
        if tunnel_running():
            self.add_log("隧道进程已经在跑，先停掉再按新配置起")
            stop_tunnel()
        config_path = write_config(tunnel_uuid, domain)
        self.add_log(f"隧道配置已写入：{config_path}")
        pid = start_tunnel(tunnel_name)
        self.add_log(f"隧道进程已启动（pid={pid}），等它连上 Cloudflare…")
        if not wait_connected(offset, cancelled=self.cancelled):
            raise TunnelError("start_failed")
        self.add_log("隧道已连上 Cloudflare")
        self.finish_step("start")

    def _step_verify(self, domain: str) -> None:
        """从公网回来看一眼。刚起隧道时边缘可能还没生效，多试两次再下结论。"""
        self.begin_step("verify")
        self.add_log(f"从公网访问一次 https://{domain}/api/auth/check 确认域名指回了这台电脑")
        result = None
        for attempt in range(3):
            result = verify_public_url(domain)
            if result is not None:
                if result is True:
                    break
                if attempt < 2:
                    self.add_log("还没通，3 秒后再试一次…")
                    time.sleep(3)
        if result is False:
            self.add_log("域名暂时没回到这台电脑（解析可能还没生效，或者记录指的是别处）")
            self._set(warning="verify_failed")
            self.fail_step("verify")
        else:
            if result is True:
                self.add_log("确认通过：域名已经回到这台电脑")
            self.finish_step("verify")


#: 全局唯一的一次挂载过程（同一时间只允许挂一个域名）
JOB = MountJob()


def mount_status() -> dict:
    """面板要的全部状态：当前设置 + 隧道进程 + 正在跑的挂载过程。

    ``has_password`` 是给面板用的：没有访问密码时它要红字警告并要用户二次确认
    （公网开着却不设密码 = 谁都能看）。
    """
    ready, reason = _access.public_ready()
    domain = _access.public_domain()
    return {
        "enabled": _access.public_enabled(),
        "domain": domain,
        "urls": _access.public_urls() if _access.public_enabled() else {},
        "tunnel": _access.public_tunnel_name(),
        "running": tunnel_running(),
        "ready": ready,
        "ready_reason": reason,
        "has_password": _access.password_hash() is not None,
        "elevated": is_elevated(),
        "binary": has_binary(),
        "job": JOB.snapshot(),
    }


def start_mount(domain: str) -> tuple[bool, dict]:
    """校验 + 开始挂载；不满足条件时返回 ``(False, 状态)``（原因在 job 里）。"""
    ready, reason = _access.public_ready()
    if not ready:
        JOB.fail(reason)
        return False, mount_status()
    JOB.start(domain)
    return True, mount_status()


def disable_public() -> dict:
    """关掉公网访问：只停进程，域名和云端记录都留着。"""
    if JOB.snapshot()["running"]:
        JOB.cancel()
    stop_tunnel()
    _access.set_public_access(enabled=False)
    return mount_status()


def restart_tunnel() -> bool:
    """按保存下来的配置把隧道再拉起来（域名没换时不用重走整个挂载流程）。"""
    name = _access.public_tunnel_name()
    domain = _access.public_domain()
    if not domain or not has_binary():
        return False
    tunnel_uuid = tunnel_id(name)
    if not tunnel_uuid:
        return False
    write_config(tunnel_uuid, domain)
    if tunnel_running():
        stop_tunnel()
    start_tunnel(name)
    return tunnel_running()


def restore_on_startup() -> bool:
    """服务启动时：配置里开着公网访问就把隧道拉回来。

    机器重启、面板点过「重启服务」之后，公网入口不该默默消失 —— 这里补上。没下载过
    cloudflared（或配置不完整）时什么都不做，绝不因此拦住服务启动。
    """
    if not _access.public_enabled() or tunnel_running():
        return False
    try:
        return restart_tunnel()
    except (TunnelError, OSError):
        return False
