"""抖音 IM 载荷分类：抓取端、导出端与阅读端共用的判定。

`aweType=10401` 被抖音复用于两种完全不同的消息：

* **仅看一次**（view once）的文本消息 —— 正文放在 ``text`` 里，protobuf 字段
  ``is_recalled`` 存的是「仅看一次」的时间戳而不是撤回时间。这类消息不是分享卡片，
  预览、搜索与导出都必须按纯文本处理。
* **商品等分享卡片** —— 一定带卡片字段（``content_title`` / ``im_dynamic_patch`` /
  ``cover_url`` 等）。

因此判定「仅看一次」时必须同时要求：aweType=10401、有正文文本、且没有任何卡片字段。

`aweType=805` 是「限时日常」作品分享：卡片带 ``itemId`` / ``cover_url``，但
``content_title`` 通常为空。抓取端落库、导出端标注与历史数据回填共用
``daily_share_text()``，保证三处文案一致、且「限时日常」这个关键词可被检索。
"""
import re

VIEW_ONCE_AWE_TYPE = "10401"

DAILY_SHARE_AWE_TYPE = "805"

# 其中任意一个存在，就说明载荷确实是分享卡片，而不是仅看一次文本。
_SHARE_CARD_FIELDS = (
    "content_title", "itemId", "item_id", "im_dynamic_patch",
    "aweme_info", "awemeType", "cover_url", "comment",
)


def is_view_once(cj) -> bool:
    """判断 content_json 是否为「仅看一次」文本消息（aweType=10401 + 正文）。"""
    if not isinstance(cj, dict):
        return False
    if str(cj.get("aweType") or "") != VIEW_ONCE_AWE_TYPE:
        return False
    if not str(cj.get("text") or "").strip():
        return False
    return not any(cj.get(field) for field in _SHARE_CARD_FIELDS)


def is_daily_share(cj) -> bool:
    """判断 content_json 是否为「限时日常」作品分享（aweType=805）。"""
    return isinstance(cj, dict) and str(cj.get("aweType") or "") == DAILY_SHARE_AWE_TYPE


def daily_share_text(cj) -> str:
    """限时日常分享的正文文案：``[分享限时日常]``（有标题时接在后面）。

    这类卡片通常没有标题，文案就是标签本身；带标签是为了让「限时日常」
    这个关键词能被搜索到（老数据只能靠回填，见 tools/backfill_daily_share.py）。
    """
    if not isinstance(cj, dict):
        return "[分享限时日常]"
    title = str(cj.get("content_title") or "").strip()
    return f"[分享限时日常] {title}" if title else "[分享限时日常]"


# 群通知模板里的编号占位，例如 "{0}邀请{1}加入了群聊"。
_LOCALE_PLACEHOLDER_RE = re.compile(r"\{(\d+)\}")


def locale_notice_text(cj, sender_uid="", self_uid="") -> str:
    """把群通知的多语言模板渲染成一句能看的话，读不出来时返回 ``""``。

    抖音的群通知（谁改了群名 / 群头像、谁被拉进群、谁开播了……）只在
    ``locale_resources[].text`` 里给一句模板，里面用 ``{0}``、``{1}`` 占位，
    编号按 ``active_users``、``passive_users`` 的先后顺序一一对应，
    例如 ``{"active_users":[{"nickname":"小明"}]}`` + ``"{0}修改了群头像"``
    → ``"小明修改了群头像"``。

    这类消息落库时 ``content`` 只是 ``"[系统消息]"`` 这个占位，所以阅读端、
    导出端都要靠这里渲染（与前端 ``renderSystemMsg`` 的规则保持一致）。
    """
    if not isinstance(cj, dict):
        return ""
    resources = cj.get("locale_resources")
    if not isinstance(resources, list):
        return ""
    text = ""
    for item in resources:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        if not item["text"].strip():
            continue
        if item.get("lang") == "zh-Hans":   # 简体中文最优先
            text = item["text"].strip()
            break
        if not text:                        # 没有中文就用接口给的第一个非空模板
            text = item["text"].strip()
    if not text:
        return ""

    users = []
    for key in ("active_users", "passive_users"):
        value = cj.get(key)
        if isinstance(value, list):
            users.extend(value)

    def _nickname(match):
        index = int(match.group(1))
        user = users[index] if index < len(users) else None
        nickname = user.get("nickname") if isinstance(user, dict) else None
        nickname = nickname.strip() if isinstance(nickname, str) else ""
        return nickname or match.group(0)   # 对应的人缺失时保留编号，不要吞掉

    text = _LOCALE_PLACEHOLDER_RE.sub(_nickname, text)

    # 有的通知备了两套说法：当事人自己看到的是「我……」，群里其他人看到的是另一句
    # （例：aweType=100149 →「我正在直播中」/「群主正在直播中，速来围观吧！」）。
    # 只有句子的主语确实是"我"、而这件事又不是我干的，才换成别人看的那一版。
    ext = cj.get("content_ext")
    actor = users[0].get("uid") if users and isinstance(users[0], dict) else sender_uid
    is_actor = bool(self_uid) and actor is not None and str(actor) == str(self_uid)
    if (not is_actor and text.startswith("我") and isinstance(ext, dict)
            and isinstance(ext.get("passive_notice"), str)
            and ext["passive_notice"].strip()):
        text = ext["passive_notice"].strip()
    return text
