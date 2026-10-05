"""文字式表情图片（[微笑]、[钱] 这些）的资源包：清单、下载与自检。

抖音私信里的表情存到数据库里是文字记号，界面上要画成图片。图片本身**不放进仓库**
（那是字节跳动的版权素材，见仓库根目录的 NOTICE），仓库只带一份
``assets/emoji_manifest.json``：名字 → 官方图片地址，顺序与
``frontend/src/lib/emojiAssets.js`` 的名单一一对应（共 214 个）。第一次运行时按需
下载到 ``assets/emoji/``，之后就一直用本机这份。

存盘用的扩展名按**实际拿到的格式**定：抖音给的表情绝大多数是 WebP，个别的直接给
PNG（[加功德] 那张就是，2026-10-04 核实过，加任何参数也变不出 WebP）。前端问图片时
统一按 ``/emoji/<名字>.webp`` 取，后端按名字把几种图片扩展名都认下来
（见 backend/emoji_files.py），所以本地存成什么格式都不影响前端。

三条原则：
* **不用第三方库**：首次运行时最先被用到的代码之一，urllib 够用；
* **能断点续下**：已经存在的文件跳过，所以中断了再点一次就行；
* **失败不炸**：单张下不来只记一笔继续下，最后把失败条数报给调用方。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from common import paths

#: 清单文件（进仓库；里面只有地址和哈希，没有图片本体）
MANIFEST_PATH = paths.EMOJI_MANIFEST

#: 图片下载到这里（不进仓库，见 .gitignore 里的 assets/emoji/）
EMOJI_DIR = paths.EMOJI_ASSET_DIR

#: 自检对不上的记录写在这里（config/ 本来就不进仓库）
LOG_PATH = paths.EMOJI_PACK_LOG

#: 抖音的图片 CDN 对没有 UA / Referer 的请求会比较挑剔，带上更稳。
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) douyin-chat-export-emoji-pack",
    "Referer": "https://www.douyin.com/",
}
TIMEOUT = 30
RETRIES = 2
_RETRY_WAIT = 0.6

#: 小于这个大小的文件一律当作没下好（比任何一张表情都小）。
MIN_BYTES = 64

#: 认得的图片扩展名。抖音给的表情绝大多数是 WebP，个别是 PNG —— 只认 WebP 的话，
#: 那张 PNG 会**每次都算失败**：点多少次「下载资源包」都补不上（2026-10-04 修）。
IMAGE_EXTS = (".webp", ".png", ".jpg", ".gif")


# ── 清单 ──────────────────────────────────────────────────────────────────

def load_manifest() -> list[dict]:
    """读清单；文件缺失或损坏时返回空表（调用方据此提示「清单读不到」）。"""
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items
            if isinstance(item, dict) and item.get("name") and item.get("url")]


def file_path(name: str) -> str:
    """这张图在本机的路径：下过就用磁盘上那份（webp / png 都认），没下过按 webp 起名。

    只在 ``_write`` 的默认路径和「本机有没有」这两处用得到，所以「返回一个不存在的
    路径」是正常情况。
    """
    for ext in IMAGE_EXTS:
        candidate = os.path.join(EMOJI_DIR, f"{name}{ext}")
        if os.path.exists(candidate):
            return candidate
    return os.path.join(EMOJI_DIR, f"{name}.webp")


def is_installed(name: str) -> bool:
    """本机有没有这张图（大小太小的一律算没有：可能是上次下了一半）。"""
    try:
        return os.path.getsize(file_path(name)) >= MIN_BYTES
    except OSError:
        return False


def status(items: list[dict] | None = None) -> dict:
    """本机装了多少、还缺多少。面板用它决定「要不要问用户下不下」。"""
    items = list(items if items is not None else load_manifest())
    total = len(items)
    installed = sum(1 for item in items if is_installed(item["name"]))
    return {
        "total": total,
        "installed": installed,
        "missing": total - installed,
        "installed_all": total > 0 and installed == total,
        # 清单本身读不到时（例如被人删了），面板显示「资源包不可用」而不是「0/0」
        "manifest_ok": total > 0,
    }


# ── 下载 ──────────────────────────────────────────────────────────────────

def download(items: list[dict] | None = None, *, on_progress=None,
             should_stop=None, force: bool = False) -> dict:
    """把清单里的图片下到 ``EMOJI_DIR``，返回这一轮的结果统计。

    :param items: 要下的清单，默认整个清单
    :param on_progress: 每处理完一项就回调一次，参数是当前进度字典
    :param should_stop: 返回 True 时停下（已经下好的那些保留）
    :param force: True = 已经存在的也重下（修坏文件用），默认跳过
    """
    items = list(items if items is not None else load_manifest())
    os.makedirs(EMOJI_DIR, exist_ok=True)
    result = {
        "total": len(items), "done": 0, "failed": 0, "skipped": 0,
        "mismatched": 0, "stopped": False, "current": "", "failed_names": [],
    }
    started = time.time()
    for item in items:
        if should_stop is not None and should_stop():
            result["stopped"] = True
            break

        name, url = item["name"], item["url"]
        result["current"] = name
        if not force and is_installed(name):
            result["done"] += 1
            result["skipped"] += 1
            _notify(on_progress, result)
            continue

        try:
            data = _fetch_with_retries(url)
            # 按真实格式落盘：抖音那几张 PNG 要是硬存成 .webp，本机就留了一张
            # 「名字说是 webp、内容其实是 png」的图，以后排查的人只会更糊涂。
            target = os.path.join(EMOJI_DIR, f"{name}{detect_image(data)}")
            _write(target, data)
            if not _hash_matches(item, data):
                result["mismatched"] += 1
                _log(f"[自检] {name}: 图片与清单里的 sha256 不一致（不影响使用）")
            result["done"] += 1
        except Exception as exc:                      # 单张失败不该中断整批
            result["failed"] += 1
            result["failed_names"].append(name)
            _log(f"[失败] {name}: {exc}")
        _notify(on_progress, result)

    result["elapsed"] = round(time.time() - started, 2)
    return result


def _notify(on_progress, result: dict) -> None:
    if on_progress is None:
        return
    try:
        on_progress(dict(result))
    except Exception:                                 # 回调出错不能影响下载
        pass


def _fetch_with_retries(url: str) -> bytes:
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            return _fetch(url)
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 403 多半是签名过期或不被接受：退一步用不带签名参数的地址试试
            if exc.code in (401, 403) and "?" in url:
                try:
                    return _fetch(url.split("?", 1)[0])
                except Exception as inner:
                    last_error = inner
        except Exception as exc:
            last_error = exc
        if attempt < RETRIES:
            time.sleep(_RETRY_WAIT * (attempt + 1))
    raise last_error if last_error else RuntimeError("下载失败")


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def detect_image(data: bytes) -> str:
    """看开头几个字节判断这是什么图，返回扩展名；不是认得的图片就报错。

    要挡住的是「403 错误页」这种照样 200 回来的非图片内容。判据只看魔数：以前这里
    写死了 WebP，导致抖音给 PNG 的那一张（[加功德]）永远下不下来。
    """
    if len(data) < MIN_BYTES:
        raise ValueError(f"内容过短（{len(data)} 字节）")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    raise ValueError("不是图片（可能拿到了错误页）")


def _write(path: str, data: bytes) -> None:
    """先写临时文件再改名：中途失败不会留下一个半截的图当「已下好」。"""
    tmp = path + ".part"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _hash_matches(item: dict, data: bytes) -> bool:
    expected = (item.get("sha256") or "").lower()
    if not expected:
        return True
    return hashlib.sha256(data).hexdigest() == expected


def _log(text: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass
