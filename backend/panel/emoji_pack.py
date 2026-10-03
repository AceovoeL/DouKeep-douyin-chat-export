"""资源包下载任务：状态、进度与启动/停止（面板的弹窗和「关于」页都用它）。

「资源包」= 文字式表情的图片（[微笑]、[钱] 这些），图片本身不进仓库，第一次运行时
按需下载到 ``assets/emoji/``（见 common/emoji_pack.py）。面板第一次打开时会问一句
要不要下 —— 问过一次就把 ``emoji_pack_prompted`` 记进 panel_config.json，不再打扰；
「关于」页还留着手动入口，随时能补下。

状态只放在内存里：服务重启就忘了这次下到哪 —— 没关系，已经下好的文件还在，
下一次读磁盘就知道装没装。下载跑在单独线程里，面板按固定间隔轮询进度。
"""
import asyncio
import time

from common import config as _cfg
from common import emoji_pack as _pack

#: panel_config.json 里的字段：问过用户没有（点了下载或点了「以后再说」都算问过）
PROMPTED_KEY = "emoji_pack_prompted"

_state = {
    "status": "idle",             # idle / running / done / failed / stopped
    "done": 0,
    "total": 0,
    "failed": 0,
    "skipped": 0,
    "mismatched": 0,
    "current": "",
    "failed_names": [],
    "error": "",
    "started_at": 0.0,
    "finished_at": 0.0,
}
_stop = False
_task: asyncio.Task | None = None


def prompted() -> bool:
    return bool(_cfg.load_config().get(PROMPTED_KEY))


def mark_prompted() -> dict:
    """记下「已经问过用户」：之后即便图片没下全，也不再自动弹窗。"""
    cfg = _cfg.load_config()
    if not cfg.get(PROMPTED_KEY):
        cfg[PROMPTED_KEY] = True
        _cfg.save_config(cfg)
    return {"status": "ok", "prompted": True}


def snapshot() -> dict:
    """磁盘状态 + 这次下载的进度，给面板一次性取走。"""
    disk = _pack.status()
    total = _state["total"] or disk["total"]
    done = _state["done"]
    if _state["status"] == "running":
        percent = int(done * 100 / total) if total else 0
    else:
        # 不在下载时，进度按磁盘上实际有多少张算（服务重启后也能显示对）
        percent = int(disk["installed"] * 100 / disk["total"]) if disk["total"] else 0
    return {
        "status": _state["status"],
        "current": _state["current"],
        "done": done,
        "total": total,
        "failed": _state["failed"],
        "skipped": _state["skipped"],
        "mismatched": _state["mismatched"],
        "error": _state["error"],
        "failed_names": list(_state["failed_names"][:20]),
        "started_at": _state["started_at"],
        "finished_at": _state["finished_at"],
        "installed": disk["installed"],
        "missing": disk["missing"],
        "installed_all": disk["installed_all"],
        "manifest_ok": disk["manifest_ok"],
        "percent": percent,
        # 第一次进面板时问不问：还没装全、清单读得到、而且没问过
        "prompt": bool(disk["manifest_ok"] and not disk["installed_all"]
                       and not prompted() and _state["status"] != "running"),
    }


async def start() -> dict:
    """开始下载（已经在跑就直接返回现状，不重复启动）。"""
    global _task, _stop
    if _state["status"] == "running":
        return snapshot()

    items = _pack.load_manifest()
    if not items:
        _state.update({"status": "failed", "error": "读不到资源包清单",
                       "finished_at": time.time()})
        return snapshot()

    _stop = False
    _state.update({
        "status": "running", "done": 0, "total": len(items), "failed": 0,
        "skipped": 0, "mismatched": 0, "current": "", "failed_names": [],
        "error": "", "started_at": time.time(), "finished_at": 0.0,
    })
    # 用户点了下载 = 这件事问过了，别再自动弹窗
    mark_prompted()
    _task = asyncio.create_task(_run(items))
    return snapshot()


async def _run(items: list[dict]) -> None:
    def on_progress(result: dict) -> None:
        _state.update({
            "done": result["done"], "failed": result["failed"],
            "skipped": result["skipped"], "mismatched": result["mismatched"],
            "current": result["current"], "failed_names": result["failed_names"],
        })

    try:
        result = await asyncio.to_thread(
            _pack.download, items, on_progress=on_progress, should_stop=lambda: _stop,
        )
    except Exception as exc:                      # 兜底：线程里出意外也要让状态落地
        _state.update({"status": "failed", "error": str(exc), "finished_at": time.time()})
        return

    _state.update({
        "done": result["done"], "failed": result["failed"],
        "skipped": result["skipped"], "mismatched": result["mismatched"],
        "current": "", "failed_names": result["failed_names"],
        "finished_at": time.time(),
        "status": "stopped" if result["stopped"] else
                  ("failed" if result["failed"] else "done"),
        "error": "" if not result["failed"] else f"{result['failed']} 项没下成功",
    })


def stop() -> dict:
    """停下这一次下载；已经下好的文件留着（再点下载会跳过它们接着下）。"""
    global _stop
    _stop = True
    if _state["status"] == "running":
        _state["current"] = ""
    return snapshot()
