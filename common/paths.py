"""Single source of truth for every filesystem path in the project.

Before this module, `os.path.join(os.path.dirname(os.path.dirname(__file__)), ...)`
was hand-rolled in 8+ places across backend/, extractor/, and the root scripts.
All state lives under DATA_DIR (./data), which is git-ignored; that one directory
is everything worth backing up or carrying to another machine.
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(REPO_ROOT, "data")

# Databases / config
DB_PATH = os.path.join(DATA_DIR, "chat.db")
CONFIG_PATH = os.path.join(DATA_DIR, "panel_config.json")

# Logs & discovery artifacts
SCRAPE_LOG = os.path.join(DATA_DIR, "scrape.log")
VOICE_TRANSCRIPTION_LOG = os.path.join(DATA_DIR, "voice_transcription.log")
DISCOVER_LOG = os.path.join(DATA_DIR, "discover.log")
CONVERSATIONS_LIST = os.path.join(DATA_DIR, "conversations_list.json")

# Browser profile (the persistent Chromium context that *is* the login state)
BROWSER_PROFILE = os.path.join(DATA_DIR, "browser_profile")

# Media tree
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
