"""Single source of truth for every filesystem path in the project.

两个目录，一条界线（2026-10-04 起）：

* ``data/``   —— **只放聊天记录**：SQLite 数据库（chat.db）、下载的媒体、导出与
  数据库备份。这些是「用户真正在乎的东西」，删掉就没有了，备份时只管这一个目录。
* ``config/`` —— **其余一切**：面板配置（panel_config.json）、私有仓库凭据
  （github_token）、登录态（browser_profile）、日志（config/logs/）、跨进程的运行
  状态（update-done.json 等）与调试产物（config/debug/）。这些删掉只是回到默认
  设置 / 需要重新登录，不会丢聊天记录。

以前所有东西都堆在 ``data/`` 里，备份时没法一眼分清「哪些是必须留的」。两个目录都
在 .gitignore 里，都不会进仓库。

``migrate_legacy_layout()`` 负责把老版本的 ``data/`` 布局搬成新布局（幂等，跑第二遍
什么都不做），由后端启动时调用一次；``_LEGACY_LAYOUT`` 是那张搬迁表。
"""
import os
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 聊天记录（数据库、媒体、导出/备份）
DATA_DIR = os.path.join(REPO_ROOT, "data")
#: 配置、凭据、日志、运行状态、调试产物
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

# ── 聊天记录 ──────────────────────────────────────────────────────────────
DB_PATH = os.path.join(DATA_DIR, "chat.db")
#: 「刷新会话列表」抓到的清单（会话名与 ID，属于聊天数据本身）
CONVERSATIONS_LIST = os.path.join(DATA_DIR, "conversations_list.json")
#: 两个回填工具的回滚记录：记的是数据库里被改写的那几行的原值，跟着数据库走
BACKFILL_DAILY_SHARE_UNDO = os.path.join(DATA_DIR, "backfill_daily_share_undo.json")
BACKFILL_MODIFY_UNDO = os.path.join(DATA_DIR, "backfill_modify_undo.json")

# Media tree（下载到本机的图片/表情/语音/头像/视频）
MEDIA_DIR = os.path.join(DATA_DIR, "media")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")
EMOJI_DIR = os.path.join(MEDIA_DIR, "emoji")
VOICE_DIR = os.path.join(MEDIA_DIR, "voice")
AVATARS_DIR = os.path.join(MEDIA_DIR, "avatars")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")

# On-demand H.264 renditions (see backend/media_transcode.py). Deliberately a
# sibling of MEDIA_DIR rather than a child: /media is served verbatim over
# HTTP, and the cache must never be reachable as if it were an original.
TRANSCODE_DIR = os.path.join(DATA_DIR, "transcoded")
TRANSCODE_VIDEOS_DIR = os.path.join(TRANSCODE_DIR, "videos")

# Staging areas used by tools/fix_media_containers.py when it rewrites the
# CENC-labelled downloads in place. Deliberately siblings of MEDIA_DIR, not
# children: everything under /media is served verbatim (and /media is public),
# and originals kept for rollback must not be published alongside the videos.
VIDEOS_FIXED_DIR = os.path.join(DATA_DIR, "videos_fixed")
VIDEOS_ORIG_DIR = os.path.join(DATA_DIR, "videos_orig")

# ── 配置与凭据 ────────────────────────────────────────────────────────────
#: 控制面板的所有设置项（密码哈希、API token、定时任务、外观、通知…）
CONFIG_PATH = os.path.join(CONFIG_DIR, "panel_config.json")
#: 私有仓库的只读 GitHub Token（单独一个文件，不混进 panel_config.json，
#: 免得哪天真有人在接口里整包返回配置时把 Token 带出去；见 common/github_auth.py）
TOKEN_PATH = os.path.join(CONFIG_DIR, "github_token")
#: 持久浏览器 profile —— 它**就是**登录态（扫码登录一次，之后一直用它采集）
BROWSER_PROFILE = os.path.join(CONFIG_DIR, "browser_profile")

# ── 日志 ──────────────────────────────────────────────────────────────────
#: 所有日志集中在这里，面板「日志」页与「打开日志文件夹」都指着它
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
#: 采集过程
SCRAPE_LOG = os.path.join(LOG_DIR, "scrape.log")
#: 历史语音转写补充
VOICE_TRANSCRIPTION_LOG = os.path.join(LOG_DIR, "voice_transcription.log")
#: 「刷新会话列表」
DISCOVER_LOG = os.path.join(LOG_DIR, "discover.log")
#: 后端服务自己的输出（启动脚本把它重定向到这里，面板「日志」页读的也是它）
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
#: 重启服务的过程记录（tools/restart_server.py 写的）
RESTART_LOG = os.path.join(LOG_DIR, "restart.log")
#: 「关于 → 更新」的输出（拉代码 / 装依赖 / 构建前端）
UPDATE_LOG = os.path.join(LOG_DIR, "update.log")
#: 双击启动时的建环境 / 装依赖 / 构建前端输出（start.ps1 的输出）
LAUNCHER_LOG = os.path.join(LOG_DIR, "launcher.log")
#: 首次运行的环境检测日志（tools/bridge.ps1 与 env_check.ps1 写）
ENV_CHECK_LOG = os.path.join(LOG_DIR, "env-check.log")
#: 表情资源包的自检记录（下载对不上的那几张）
EMOJI_PACK_LOG = os.path.join(LOG_DIR, "emoji_pack.log")

# ── 跨进程运行状态（小文件，给面板或 start.html 读） ────────────────────────
#: 环境检测报告。写成 JavaScript（JSONP）是因为 start.html 是 file:// 页面，
#: 只有 <script src="..."> 这条路过得了浏览器的本地文件限制。
ENV_REPORT = os.path.join(CONFIG_DIR, "env-report.js")
#: 启动器状态（谁在启动 start.ps1），同样是给 start.html 用 <script src> 读的
LAUNCHER_STATE = os.path.join(CONFIG_DIR, "launcher-state.js")
#: 「更新完成」记录：旧服务退出前写、新服务启动时读一次就删（只会弹一次提示）
UPDATE_DONE_PATH = os.path.join(CONFIG_DIR, "update-done.json")

# ── 调试产物 ──────────────────────────────────────────────────────────────
#: 抓取失败时的现场快照（DOM 结构 JSON、页面截图）
DEBUG_DIR = os.path.join(CONFIG_DIR, "debug")
#: tools/discover_conv_api.py 抓下来的接口报文
API_DUMP_DIR = os.path.join(CONFIG_DIR, "api_dumps")

# ── 仓库里的东西（不是运行产物） ────────────────────────────────────────────
# Frontend build output served by the backend
FRONTEND_DIST = os.path.join(REPO_ROOT, "frontend", "dist")

# 仓库里的静态图片。frontend/src/assets 里那份是前端打包用的（构建后进 dist）。
# assets/emoji 不参与打包也不进仓库：文字式表情（[钱]、[憨笑] 这些）有 200 多张，
# 都是字节跳动的版权素材（见 NOTICE），第一次运行时按清单下载到这里，由后端挂在
# /emoji 下按需取用 —— 前端消息正文里的 <img> 直接引这个地址。
ASSETS_DIR = os.path.join(REPO_ROOT, "assets")
EMOJI_ASSET_DIR = os.path.join(ASSETS_DIR, "emoji")
#: 表情图片的下载清单（名字 → 官方地址，进仓库；见 common/emoji_pack.py）
EMOJI_MANIFEST = os.path.join(ASSETS_DIR, "emoji_manifest.json")


# ── 老布局（所有东西都在 data/）→ 新布局 ────────────────────────────────────
#
# 键是相对 data/ 的旧名字，值是相对 config/ 的新名字。只搬「不是聊天记录」的那些：
# 数据库、媒体、导出、数据库备份都原地不动。目标已存在时跳过（不覆盖新文件）。
_LEGACY_LAYOUT = {
    "panel_config.json": "panel_config.json",
    "github_token": "github_token",
    "browser_profile": "browser_profile",
    "scrape.log": "logs/scrape.log",
    "voice_transcription.log": "logs/voice_transcription.log",
    "discover.log": "logs/discover.log",
    "server.log": "logs/server.log",
    "restart.log": "logs/restart.log",
    "update.log": "logs/update.log",
    "launcher.log": "logs/launcher.log",
    "env-check.log": "logs/env-check.log",
    "emoji_pack.log": "logs/emoji_pack.log",
    "env-report.js": "env-report.js",
    "launcher-state.js": "launcher-state.js",
    "update-done.json": "update-done.json",
    "debug": "debug",
    # v1.0.0 时调试截图落在 data/ 根目录下，新版统一进 config/debug/
    "debug_no_conv.png": "debug/debug_no_conv.png",
    "api_dumps": "api_dumps",
}


def legacy_layout() -> dict[str, str]:
    """旧布局的搬迁表：相对 data/ 的旧名字 → 新的绝对路径（按当前目录算）。"""
    return {
        name: os.path.join(CONFIG_DIR, *relative.split("/"))
        for name, relative in _LEGACY_LAYOUT.items()
    }


def migrate_legacy_layout() -> list[str]:
    """把老版本堆在 ``data/`` 里的配置 / 日志 / 状态搬进 ``config/``。

    幂等：搬过一次之后源文件就没了，再跑什么都不做（目标已存在时也跳过，绝不覆盖
    新文件）。整体是「尽力而为」—— 单个文件搬不动（被占用、权限不足）只打一行提示
    继续搬下一个，绝不因为搬迁失败把服务启动拖住。

    返回这次搬动了的旧名字（给日志用；测试也靠它断言）。
    """
    moved: list[str] = []
    for name, target in legacy_layout().items():
        source = os.path.join(DATA_DIR, name)
        if not os.path.exists(source) or os.path.exists(target):
            continue
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(source, target)
        except OSError as exc:
            print(f"[!] 旧文件没能搬到新位置：{source} → {target}（{exc}）", flush=True)
            continue
        moved.append(name)
    if moved:
        print(f"[i] 已把 {len(moved)} 项从 data/ 搬到 config/：{'、'.join(moved)}", flush=True)
    return moved
