// Douyin IM message parsing/detection — pure functions of a message row.
// Extracted from MessageList.vue so the intricate payload heuristics can be
// unit-tested and reused. Nothing here depends on Vue reactivity or the DOM.
//
// The message shape: { msg_id, msg_type, content, raw_data (JSON string with a
// (often double-encoded) content_json), sender_uid, sender_name, timestamp,
// media_local_path, media_url, ref_msg, voice_transcription }.

// Cache by row identity: the same server message may also appear in a forward.
let cjCache = new WeakMap()

export function clearCjCache() {
  cjCache = new WeakMap()
  modifyCache = new WeakMap()
}

// raw_data / 「这条消息发生过哪些操作」的解析缓存（同一条消息行只算一次）。
let modifyCache = new WeakMap()

export function getContentJson(msg) {
  if (cjCache.has(msg)) return cjCache.get(msg)
  let cj = null
  try {
    if (msg.raw_data) {
      const raw = typeof msg.raw_data === 'string' ? JSON.parse(msg.raw_data) : msg.raw_data
      if (raw.content_json) {
        cj = typeof raw.content_json === 'string' ? JSON.parse(raw.content_json) : raw.content_json
      }
    }
  } catch {}
  cjCache.set(msg, cj)
  return cj
}

export function tryParseJson(str) {
  if (!str || !str.startsWith('{')) return null
  try { return JSON.parse(str) } catch { return null }
}

// 抖音的媒体字段形态很多：字符串、数组、{url_list:[...]} 或 {resource_url:{...}}
// 统一取出第一个可用 URL。
export function firstMediaUrl(value) {
  if (!value) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    const first = value[0]
    return typeof first === 'string' ? first : firstMediaUrl(first)
  }
  if (typeof value === 'object') {
    for (const key of ['url_list', 'origin_url_list', 'medium_url_list', 'large_url_list', 'thumb_url_list']) {
      const found = firstMediaUrl(value[key])
      if (found) return found
    }
    if (typeof value.url === 'string') return value.url
    if (typeof value.uri === 'string' && value.uri.startsWith('http')) return value.uri
  }
  return ''
}

// content_json 可能缺失（旧数据只把 JSON 留在 content 里），这里统一兜底。
export function payloadJson(msg) {
  return getContentJson(msg) || tryParseJson(msg.content)
}

export function tryParseShareContent(content) {
  if (!content || !content.startsWith('{')) return null
  try {
    const obj = JSON.parse(content)
    if (obj.content_title || obj.cover_url || obj.im_dynamic_patch || obj.item_id || obj.itemId || obj.poi_name || obj.aweme_poi_id || obj.aweType === 805 || obj.aweType === 2104) return obj
  } catch {}
  return null
}

export function extractShareTitle(content) {
  if (!content) return ''
  // "分享[商品]: 商品名称" / "分享[视频]: 标题" / "[分享视频]标题"
  const m = content.match(/^(?:分享\[.+?\][:：]\s*|\[分享.+?\])(.+)/s)
  return m ? m[1].trim() : ''
}

// msg_type=1 but actually a system-tip JSON (has tips, but not a sticker)
export function isJsonSystemMsg(msg) {
  if (msg.msg_type !== 1) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  if (isJsonSticker(msg)) return false  // 贴纸优先
  return msg.content.includes('"tips"') && msg.content.includes('"aweType"')
}

// msg_type=1 but actually a sticker JSON (content may be truncated → also check content_json)
export function isJsonSticker(msg) {
  if (msg.msg_type !== 1) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  if (msg.content.includes('"stickers"') || msg.content.includes('"joker_stickers"')) return true
  const cj = getContentJson(msg)
  if (cj && (cj.stickers || cj.joker_stickers)) return true
  return false
}

export function getStickerUrl(msg) {
  const cj = getContentJson(msg)
  const source = cj || tryParseShareContent(msg.content)
  if (!source) return null
  if (source.stickers?.length > 0) {
    return source.stickers[0].static_url?.url_list?.[0] || null
  }
  if (source.joker_stickers?.length > 0) {
    return source.joker_stickers[0].static_url?.url_list?.[0] || null
  }
  return null
}

// "一起看视频" 邀请卡片 (aweType=9000)：msg_type=0，但不是普通系统提示，
// 单独渲染成卡片。返回 {title, subtitle, cover} 或 null。
export function getWatchTogether(msg) {
  const cj = getContentJson(msg) || tryParseJson(msg.content)
  if (!cj || cj.aweType !== 9000) return null
  return {
    title: cj.title || '一起看视频',
    subtitle: cj.sub_title || cj.hint || '',
    cover: cj.cover_url?.url_list?.[0] || '',
  }
}

// 群公告（type_code=1004）：content_json 形如
// { aweType:0, notice_title:"群公告", notice_content:"...", upgraded_notice_content:"" }。
// 抖音把这条消息推给全群成员，但消息本身带着"改公告那个人"的 uid，
// 所以能按「某某 发布了群公告」的卡片显示。命中时返回 {title, body}，否则 null。
export function getGroupNotice(msg) {
  const cj = getContentJson(msg) || tryParseJson(msg.content)
  if (!cj) return null
  if (!('notice_content' in cj) && !('upgraded_notice_content' in cj)) return null
  // 新老两条文案：notice_content 是旧字段，upgraded_notice_content 是新版，
  // 抖音有时只填其中一个，取第一个有内容的。
  const body = [cj.notice_content, cj.upgraded_notice_content]
    .map((v) => (typeof v === 'string' ? v.trim() : ''))
    .find(Boolean) || ''
  const title = (typeof cj.notice_title === 'string' && cj.notice_title.trim()) || '群公告'
  return { title, body }
}

// 关系类提示（互相关注 / 成为好友）：同一次事件抖音会向双方各下发一条，文本完全相同；
// 互相关注还会额外带一条纯文本提示（aweType=701）和一张打招呼卡片（hint_text 同文）。
// 这些行都属于系统提示，且只需要显示一次。命中时返回归一化文本，否则返回 ''。
const RELATION_NOTICE_RE =
  /^(?:我们已互相关注[，,]可以开始聊天了|你们已互相关注对方|我们已成为朋友|你们已成为朋友)$/
export function relationNoticeText(msg) {
  const cj = getContentJson(msg) || tryParseJson(msg.content)
  const candidates = [cj?.tips, cj?.hint_text, cj?.text]
  if (typeof msg.content === 'string' && !msg.content.startsWith('{')) {
    candidates.push(msg.content)
  }
  for (const candidate of candidates) {
    if (typeof candidate !== 'string') continue
    const text = candidate.trim()
    if (RELATION_NOTICE_RE.test(text)) return text
  }
  return ''
}

// 同一次互相关注/成为好友事件里，「我们…」那几句是**对方**发出来的：
// 抖音客户端把它显示成对方发来的一条消息（在左边，带头像和昵称）。
// 而「你们…」那几句是对双方说的提示，客户端仍然居中显示。
// 命中时返回这句话，否则返回 ''。
const PEER_NOTICE_RE = /^我们/
export function peerRelationNotice(msg) {
  const text = relationNoticeText(msg)
  return PEER_NOTICE_RE.test(text) ? text : ''
}

// Whether a message should render at all (empty system messages are hidden).
export function shouldShow(msg) {
  if (getProfileCard(msg) || getForwardInfo(msg) || isVoiceMsg(msg)) return true
  if (getWatchTogether(msg)) return true       // 一起看视频卡片始终显示
  if (getGroupNotice(msg)) return true         // 群公告始终显示（哪怕正文是空的）
  if (isLooseEmoji(msg) || isLooseShare(msg) || isLooseImage(msg)) return true
  if (msg.msg_type === 0) return !!renderSystemMsg(msg)
  if (isJsonSystemMsg(msg)) return !!renderSystemMsg(msg)
  return true
}

const LOCALE_PLACEHOLDER_RE = /\{(\d+)\}/g

// 群通知（谁改了群名 / 群头像、谁被拉进群、谁开播了……）在抖音接口里只给一句
// 多语言模板：locale_resources[].text 里用 {0}、{1} 占位，编号按 active_users、
// passive_users 的先后顺序一一对应。落库时 content 只剩 "[系统消息]" 这个占位，
// 所以要把模板里的编号换成当时那个人的昵称，界面上才有内容可看。
function localeNoticeText(source, msg = {}, selfUid = '') {
  if (!source) return ''
  const resources = Array.isArray(source.locale_resources) ? source.locale_resources : []
  const usable = resources.filter((r) => r && typeof r.text === 'string' && r.text.trim())
  const chosen = usable.find((r) => r.lang === 'zh-Hans') || usable[0]
  let text = chosen ? chosen.text.trim() : ''
  if (!text) return ''
  const users = [
    ...(Array.isArray(source.active_users) ? source.active_users : []),
    ...(Array.isArray(source.passive_users) ? source.passive_users : []),
  ]
  text = text.replace(LOCALE_PLACEHOLDER_RE, (whole, index) => {
    const user = users[Number(index)]
    const nickname = user && typeof user.nickname === 'string' ? user.nickname.trim() : ''
    return nickname || whole   // 对应的人缺失时保留原样，别把编号吞掉
  })
  // 有的通知准备了两套说法：当事人自己看到的是「我……」，群里其他人看到的是另一句。
  // 例：aweType=100149 →「我正在直播中」/「群主正在直播中，速来围观吧！」
  // 只有句子的主语确实是"我"、而这件事又不是我干的，才换成别人看的那一版。
  const ext = source.content_ext
  const actor = users[0] ? users[0].uid : msg.sender_uid
  const isActor = !!selfUid && actor !== undefined && actor !== null
    && String(actor) === String(selfUid)
  if (!isActor && /^我/.test(text) && ext && typeof ext === 'object'
      && typeof ext.passive_notice === 'string' && ext.passive_notice.trim()) {
    text = ext.passive_notice.trim()
  }
  return text
}

// System message: render the template (prefer content_json — content may be truncated).
export function renderSystemMsg(msg, selfUid = '') {
  const cj = getContentJson(msg)
  const source = cj || tryParseJson(msg.content)
  if (!source) {
    return (msg.content && msg.content !== '{}') ? msg.content : ''
  }
  if (source.tips) {
    let text = source.tips
    // These events carry the actor's UID, even when Douyin supplies a
    // recipient-oriented template. Change only template pronouns, never names
    // or the referenced video's title.
    if (selfUid && msg.sender_uid && String(source.aweType) === '126' && /赞了/.test(text)) {
      text = String(msg.sender_uid) === String(selfUid)
        ? '你赞了对方分享的 {{2}}' : '对方赞了你分享的 {{2}}'
    } else if (selfUid && msg.sender_uid && /^(你|对方)领取了火星/.test(text)) {
      text = text.replace(/^(你|对方)/, String(msg.sender_uid) === String(selfUid) ? '你' : '对方')
    }
    if (Array.isArray(source.template)) {
      for (const t of source.template) {
        if (!t || t.key === undefined) continue
        text = text.replaceAll(`{{${t.key}}}`, () => t.name || '')
      }
    }
    return text
  }
  if (source.hint_text) return source.hint_text
  const notice = relationNoticeText(msg)
  if (notice) return notice
  // 群通知只存在多语言模板里，content 是 "[系统消息]" 占位时优先把模板渲染出来。
  // （content 本身是能看的纯文本时保持原样，只有兜底提示才会落到这里。）
  if (!msg.content || msg.content === '[系统消息]' || msg.content.startsWith('{')) {
    const locale = localeNoticeText(source, msg, selfUid)
    if (locale) return locale
  }
  if (Object.keys(source).length <= 1) return ''
  // 兜底：不认识的 JSON 卡片消息不要把原始 JSON 吐到界面上；只回显纯文本 content。
  return (msg.content && msg.content !== '{}' && !msg.content.startsWith('{')) ? msg.content : ''
}

// 「查看 JSON」按钮放在系统提示的哪一侧。用**渲染后**的文本判断主语，所以切换"我是谁"时
// 人称一变，按钮位置跟着换：主语是"你"→ 与我有关，放左边；主语是"对方"→ 放右边；
// 主语分不清（我们 / 你们 / 昵称 / 其它）→ 统一放到提示下方。
export function systemNoticeSide(msg, selfUid = '') {
  const text = renderSystemMsg(msg, selfUid).trim()
  if (/^你(?!们)/.test(text)) return 'left'
  if (text.startsWith('对方')) return 'right'
  return 'below'
}

// Extract server_message_id from the raw JSON string (avoids JSON.parse losing
// >2^53 integer precision); tolerates escaped quotes in doubly-encoded raw_data.
export function extractServerMsgIds(msg) {
  const ids = []
  try {
    const raw = typeof msg.raw_data === 'string' ? msg.raw_data : JSON.stringify(msg.raw_data)
    const re = /server_message_id\\?"?\s*:\s*\\?"?(\d{15,})/g
    let match
    while ((match = re.exec(raw)) !== null) {
      ids.push(match[1])
    }
  } catch {}
  return ids
}

// Comment-quoting-a-video message (aweType=700 + related_share_video)
export function isVideoComment(msg) {
  const cj = getContentJson(msg)
  if (!cj) return false
  return cj.aweType === 700 && !!cj.related_share_video?.itemId
}

// Video/photo/live shares (800, 11054+), quoted-comment shares (10500), goods.
// Keep 10500 out of the video list so a video share is never treated as a comment.
// Note: aweType=10401 is dual-purpose (goods card *and* "view once" text) and is
// therefore decided by isViewOnce() instead of sitting in this list.
const SHARE_AWE_TYPES = new Set([
  800, 801, 803, 805, 10500, 11029,
  11054, 11055, 11063, 11066, 11067, 11069, 11070,
])
const VIDEO_SHARE_AWE_TYPES = new Set([
  800, 801, 803, 805,
  11054, 11055, 11063, 11066, 11067, 11069, 11070,
])

function sharePrefix(content) {
  return String(content || '').match(/^\[分享([^\]]+)\]/)?.[1] || ''
}

// "View once" (仅看一次) text messages reuse aweType=10401; the protobuf
// `is_recalled` field carries the view-once timestamp rather than a recall time.
// Goods cards use the same aweType but always carry card fields, so a card-less
// payload with real text is text, never a share.
const VIEW_ONCE_AWE_TYPE = 10401
const SHARE_CARD_FIELDS = [
  'content_title', 'itemId', 'item_id', 'im_dynamic_patch', 'aweme_info',
  'awemeType', 'cover_url', 'comment',
]

function isViewOnceContentJson(cj) {
  if (!cj || typeof cj !== 'object') return false
  if (Number(cj.aweType) !== VIEW_ONCE_AWE_TYPE) return false
  if (!String(cj.text || '').trim()) return false
  return !SHARE_CARD_FIELDS.some(field => cj[field])
}

function isShareContentJson(cj) {
  if (!cj || typeof cj !== 'object') return false
  if (isViewOnceContentJson(cj)) return false
  const awe = Number(cj.aweType)
  if (SHARE_AWE_TYPES.has(awe)) return true
  if (awe === 9000 || awe === 13600) return false
  const text = String(cj.text || cj.push_detail || '')
  if (text.startsWith('[分享') || text.startsWith('分享[')) return true
  return !!(cj.im_dynamic_patch || cj.awemeType || cj.itemId || cj.item_id || cj.comment || cj.content_title)
}

// Share card stored as msg_type=1 (JSON content, or forwarded body with title text).
export function isJsonShare(msg) {
  if (Number(msg.msg_type) === 4) return false
  if (isViewOnce(msg)) return false
  const cj = getContentJson(msg)
  if (msg.content?.startsWith('{') && (msg.content.includes('content_title') || msg.content.includes('cover_url'))) {
    return true
  }
  if (isShareContentJson(cj)) return true
  return !!(sharePrefix(msg.content) || /^(?:分享\[.+?\][:：])/.test(msg.content || ''))
}

// 「小火人」表情（aweType=519，抖音的表情面板里叫 monster emoji）：载荷里
// display_name 是「笑死」「续火花」「打招呼」这类文字，url.url_list 才是那张动图。
// 它跟 515/517/520 一样是 msg_type=0 落库的表情，必须当表情图片画出来，
// 否则界面上只剩"笑死"这三个字，看着像一句系统提示。
const LOOSE_EMOJI_AWES = new Set([515, 517, 519, 520])
const LOOSE_SHARE_AWES = new Set([805, 2104])

// msg_type=0/1 里被漏判的表情包：贴纸 JSON，或 aweType=515/517/519/520。
export function isLooseEmoji(msg) {
  if (isJsonSticker(msg)) return true
  const cj = payloadJson(msg)
  return !!cj && LOOSE_EMOJI_AWES.has(Number(cj.aweType))
}

// msg_type=0/1 里被漏判的分享/地点卡：JSON 分享、aweType=805/2104、POI 卡片。
export function isLooseShare(msg) {
  if (msg.msg_type === 4) return false
  if (isJsonShare(msg)) return true
  const cj = payloadJson(msg)
  if (!cj) return false
  if (LOOSE_SHARE_AWES.has(Number(cj.aweType))) return true
  if (cj.poi_name || cj.cover_info) return true
  return !!(cj.aweme_poi_id && String(cj.aweme_poi_id))
}

export function isShareCard(msg) {
  return msg.msg_type === 4 || isLooseShare(msg)
}

// msg_type=0/1 里被漏判的图片（inline_pic / check_pics 的图文载荷）。
export function isLooseImage(msg) {
  if (msg.msg_type === 3) return false
  if (isLooseShare(msg) || isLooseEmoji(msg) || isJsonVideo(msg)) return false
  const cj = payloadJson(msg)
  if (!cj?.inline_pic) return false
  return !!(cj.check_pics || cj.is_long_pic != null || cj.create_type != null)
}

// Share-card info extraction (video share, product card, quoted-video comment).
// 排版节点 → 文字。抖音给分享卡片的 content_top / top_bottom_top 有时是**组件列表**
// （im-component-list，例如「获得火花见面礼」卡），直接当标题用会被渲染成
// "[object Object],[object Object],…"，这里只拼文字节点。
function layoutText(node) {
  const value = node?.content
  if (typeof value === 'string') return value
  if (!Array.isArray(value)) return ''
  return value
    .filter(item => item && typeof item === 'object' && item.type === 'im-text')
    .map(item => String(item.content ?? ''))
    .join('')
    .trim()
}

export function getShareInfo(msg) {
  const cj = getContentJson(msg)
  const source = cj || tryParseShareContent(msg.content) || {}
  // "View once" text (aweType=10401, card-less) is not a share at all: never let
  // its body leak into the card title/comment fields.
  if (isViewOnceContentJson(source)) {
    return { title: '', author: '', cover: '', itemId: '', productUrl: '', comment: '', commentUser: '', commentImg: '' }
  }

  // Dynamic layouts are also used by video/photo/live-photo shares.
  let layout = {}
  try {
    const patch = source.im_dynamic_patch?.raw_data
    const parsed = typeof patch === 'string' ? JSON.parse(patch) : patch
    if (parsed && typeof parsed === 'object') layout = parsed
  } catch { /* Keep usable top-level fields if the layout is malformed. */ }
  const imageUrl = value => typeof value === 'string' ? value : value?.url_list?.[0] || ''
  let productUrl = ''
  for (const action of Array.isArray(layout.whole_card?.action_info) ? layout.whole_card.action_info : []) {
    const match = String(action?.params?.schema || '').match(/commodity_id=(\d+)/)
    if (match) { productUrl = 'https://www.douyin.com/product/' + match[1]; break }
  }

  // 10500 stores the quoted comment in `comment`. Video shares (11054/800) may
  // also have `text` (caption / push copy) — that is not a comment card.
  // aweType=700 (text + related_share_video) is handled by isVideoComment.
  const awe = Number(source.aweType)
  const prefix = sharePrefix(msg.content)
  const isCommentShare = !VIDEO_SHARE_AWE_TYPES.has(awe) && (
    !!source.comment || awe === 10500 || prefix === '评论'
  )
  const comment = isCommentShare
    ? (source.comment || source.text || extractShareTitle(msg.content) || '')
    : (awe === 700 ? source.text : '') || ''
  const commentUser = source.comment_user_name || ''
  const commentImg = firstMediaUrl(source.comment_url)
  const relatedVideo = source.related_share_video || {}
  // 封面可能藏在 cover_url / cover_info / content_thumb 等不同字段里。
  const cover = firstMediaUrl(source.cover_url)
    || firstMediaUrl(source.cover_info?.resource_url)
    || firstMediaUrl(source.content_thumb)
    || firstMediaUrl(source.cover_info)
  // 卡片内容有时只是纯文本（地点、直播推送），此时标题直接用原文；
  // 但要剥掉 "[分享xxx]" 前缀 —— 否则正文只剩标签本身时（例如回填后的
  // 「[分享限时日常]」）标签会被当成标题，与卡片前缀重复一遍。
  const plainContent = (msg.content && !msg.content.startsWith('{'))
    ? msg.content.replace(/^(?:分享\[[^\]]+\][:：]\s*|\[分享[^\]]*\]\s*)/, '')
    : ''
  return {
    title: layoutText(layout.top_bottom_top) || layoutText(layout.content_top) || source.content_title || source.aweme_title || source.poi_name || source.push_detail || source.bottom_card_title || extractShareTitle(msg.content) || plainContent || '',
    author: layoutText(layout.top_bottom_content_right) || source.content_name || '',
    cover: imageUrl(layout.top?.content) || cover || imageUrl(source.aweme_info?.cover_url),
    itemId: String(source.itemId || source.item_id || source.aweme_info?.item_id || relatedVideo.itemId || ''),
    productUrl,
    comment,
    commentUser,
    commentImg,
  }
}

// 「限时日常」（aweType=805）：抖音的限时「日常」作品分享。这类卡片不带标题，
// 退化成笼统的 [分享] 会丢掉「限时日常」这个信息，因此单独标注。
export const DAILY_SHARE_AWE_TYPE = 805

export function isDailyShare(msg) {
  const cj = getContentJson(msg) || tryParseShareContent(msg.content)
  return Number(cj?.aweType) === DAILY_SHARE_AWE_TYPE
}

// Share-card heading: [分享限时日常]（+ 标题，若有）；其余分享只显示作品标题本身。
// 作品分享（视频/图片/动图/文章/评论）本来就不带标题时，以前会退化成笼统的
// "[分享]"：它既看不出分享的是什么，又和卡片上的封面、作者重复，所以现在留白。
export function shareCardTitle(msg) {
  const title = getShareInfo(msg).title
  const label = isDailyShare(msg) ? '[分享限时日常]' : ''
  return [label, title].filter(Boolean).join(' ')
}

// Image inline_pic base64 (WebP thumbnail); strip embedded newlines from the base64.
export function getInlinePic(msg) {
  const cj = payloadJson(msg)
  if (cj?.inline_pic) {
    return 'data:image/webp;base64,' + cj.inline_pic.replace(/\r?\n/g, '')
  }
  return null
}

// msg_type=3 whose local file is an .mp4 (a real downloaded video, not an image)
export function isVideoMsg(msg) {
  return msg.media_local_path && /\.mp4$/i.test(msg.media_local_path)
}

// Alias: a JSON-video message has a playable local .mp4 (same test).
export const hasLocalVideo = isVideoMsg

// JSON video message (msg_type=5, or legacy msg_type=1 with cj.video.vid) — poster only.
export function isJsonVideo(msg) {
  if (msg.msg_type === 5) return true
  if (msg.msg_type !== 1) return false
  const cj = getContentJson(msg)
  return !!(cj && cj.video && cj.video.vid)
}

// Video poster: the inline_pic base64 WebP thumbnail.
export function getVideoPoster(msg) {
  return getInlinePic(msg)
}

// Video duration in seconds (rendered with a ″ suffix).
export function getVideoDuration(msg) {
  const cj = getContentJson(msg)
  const d = cj?.duration
  if (d === undefined || d === null) return ''
  const n = typeof d === 'string' ? parseFloat(d) : Number(d)
  if (!n || isNaN(n)) return ''
  return Math.round(n) + '″'
}

// Image src: local original > inline_pic thumbnail (video goes through its own branch).
export function getImageSrc(msg) {
  if (msg.media_local_path && !isVideoMsg(msg)) return '/media/' + msg.media_local_path
  return getInlinePic(msg)
}

// 实况图（抖音的「会动的图」）= 静态封面 + 一段两三秒的小视频。封面就是普通图片
// （media_local_path / inline_pic），小视频是采集端单独补下来的 live_video_path；
// 只有那份视频真的下到本地了，这一列才有值（见 extractor/video_downloader.py）。
export function getLivePhotoVideo(msg) {
  const path = msg?.live_video_path
  return typeof path === 'string' && path ? '/media/' + path : null
}

// Emoji src: local > 已存 CDN 链接 > 贴纸载荷 > cj.url（小火人 519 走这条）。
export function getEmojiSrc(msg) {
  if (msg.media_local_path) return '/media/' + msg.media_local_path
  if (msg.media_url) return msg.media_url
  const sticker = getStickerUrl(msg)
  if (sticker) return sticker
  const cj = payloadJson(msg)
  const url = firstMediaUrl(cj?.url)
  return url.startsWith('http') ? url : null
}

// Recalled-message detection: the placeholder body, or the recall marker that the
// extractor wrote into raw_data.modify（见 common/message_modify.py）。
export function isRecalled(msg) {
  if (msg.content === 'Recall Content Hided') return true
  return getModifyKinds(msg).includes('recall')
}

// Legacy rows may keep the JSON blob only in `content`, so fall back to it.
function viewOnceSource(msg) {
  const cj = getContentJson(msg)
  if (cj) return cj
  const parsed = tryParseJson(msg.content)
  return parsed && typeof parsed === 'object' ? parsed : null
}

// View-once messages (aweType=10401 + real text, no card fields). These are
// text messages sent with the "view once" flag, not true recalls and not shares.
export function isViewOnce(msg) {
  if (msg.content === 'Recall Content Hided') return false
  // 抖音用消息类型号 104 标记「仅看一次」，比正文特征准。
  const raw = rawData(msg)
  if (raw && Number(raw.type_code) === VIEW_ONCE_TYPE_CODE) return true
  // 兜底：老数据没有类型号，只能靠正文特征（aweType=10401 + 纯文本 + 无卡片字段）。
  if (!isViewOnceContentJson(viewOnceSource(msg))) return false
  if (!raw) return true
  return !!raw.is_recalled
}

// Readable body of a "view once" message.
export function getViewOnceText(msg) {
  const cj = viewOnceSource(msg)
  if (cj?.text) return String(cj.text)
  return msg.content && !msg.content.startsWith('{') ? msg.content : ''
}

// 特殊操作标注：仅看一次 / 撤回 / 编辑 / 表情快捷回复。
//
// 抖音把这四种行为的「时间」都写在同一个 protobuf 第 11 号字段里（沿用旧字段名
// is_recalled），所以光看时间戳分不出是哪一种；能分清的是包里的其它字段：
//
//   * 仅看一次：第 6 号字段（消息类型）= 104，另有 s:once_view_count / s:once_view_done
//   * 撤回：扩展里有 a:recalled_msg_type
//   * 编辑：扩展里有 s:edit_info（含编辑者 uid 与 content_is_edited）
//   * 表情快捷回复：第 15 号字段（表情名 + 回应者 uid + 时间）
//
// 抓取端抓包时已经解析好并落在 raw_data.modify 里（见 common/message_modify.py），
// 这里只读结论，不再靠时间窗口猜。老数据（超过一年、抖音接口已经拿不到的消息）
// 没有这份结论，就不标注。
export const MODIFY_KIND_LABELS = {
  view_once: '仅看一次',
  recall: '已撤回',
  edit: '已编辑',
  reaction: '表情快捷回复',
}

export const MODIFY_KIND_TITLES = {
  view_once: '仅看一次：抖音在这条消息上标记了阅后即焚',
  recall: '已撤回：服务端记录了撤回时间',
  edit: '已编辑：服务端记录了编辑者和编辑时间',
  reaction: '表情快捷回复：长按这条消息用表情包回应，回应显示在消息下方',
}

// 仅看一次的消息类型号（protobuf 第 6 号字段）。
const VIEW_ONCE_TYPE_CODE = 104

// raw_data 解析缓存（同一条消息行只解析一次）。
function rawData(msg) {
  if (!msg || !msg.raw_data) return null
  const cached = modifyCache.get(msg)
  if (cached && cached.raw !== undefined) return cached.raw
  let raw = null
  try {
    raw = typeof msg.raw_data === 'string' ? JSON.parse(msg.raw_data) : msg.raw_data
  } catch { raw = null }
  if (raw && typeof raw !== 'object') raw = null
  modifyCache.set(msg, { raw })
  return raw
}

// 第 11 号字段：消息最后一次被改动的时间（毫秒），没有则返回 0。
export function modifyTimestamp(msg) {
  return Number(rawData(msg)?.is_recalled) || 0
}

// raw_data.modify 里记着「这条消息发生过什么」（抓取端写入）。
function modifyInfo(msg) {
  const raw = rawData(msg)
  const info = raw && raw.modify
  if (!info || typeof info !== 'object') return null
  const kinds = Array.isArray(info.kinds) ? info.kinds.filter(kind => kind in MODIFY_KIND_LABELS) : []
  if (!kinds.length) return null
  return { kinds, info }
}

// 这条消息发生过的操作，按 仅看一次 / 撤回 / 编辑 / 表情快捷回复 的顺序返回。
// 空数组表示这是一条普通消息，不需要标注。
export function getModifyKinds(msg) {
  // 正文就是撤回占位，这条不用看字段也能确定。
  if (msg.content === 'Recall Content Hided') return ['recall']
  const found = modifyInfo(msg)
  if (found) return found.kinds
  // 兜底：老数据没有整包结论，仅看一次还能靠正文特征（aweType=10401 + 纯文本）认出来。
  return isViewOnce(msg) ? ['view_once'] : []
}

// 表情快捷回复的明细：[{ emoji: "[爱心]", uid: "100000000036", time: 1771820351 }]
export function getModifyReactions(msg) {
  const found = modifyInfo(msg)
  const list = found && found.info.reactions
  return Array.isArray(list) ? list.filter(item => item && typeof item === 'object') : []
}

function getVoiceContent(msg) {
  const cj = getContentJson(msg)
  if (cj) return cj
  if (msg.content?.startsWith('{') && msg.content.includes('resource_url')) {
    try { return JSON.parse(msg.content) } catch {}
  }
  return null
}

// Voice messages normally stay msg_type=0/other and carry resource_url plus
// duration.  Also recognize legacy rows that an older scraper stored as
// msg_type=1, including payloads where only tkey/voice_wave was retained.
export function isVoiceMsg(msg) {
  const cj = getVoiceContent(msg)
  const resource = cj?.resource_url
  if (!resource || (typeof resource !== 'object' && typeof resource !== 'string')) return false
  if (['2702', '2703', '2704'].includes(String(cj.aweType)) && !cj.voice_wave && !cj.tkey && !resource.is_voice) return false
  if (cj.video?.vid && !cj.voice_wave && !cj.tkey && !resource.is_voice) return false
  const hasUrl = Array.isArray(resource.url_list) && resource.url_list.length > 0
  const hasDuration = (cj.duration !== undefined && cj.duration !== null && cj.duration !== '') ||
    (resource.duration !== undefined && resource.duration !== null && resource.duration !== '')
  const hasVoiceMarker = !!(cj.tkey || cj.voice_wave || resource.is_voice)
  const storedVoiceType = msg.msg_type === 0 || msg.msg_type === 'other' || msg.msg_type === undefined || msg.msg_type === null
  return storedVoiceType ? (hasUrl || hasDuration || hasVoiceMarker) : (hasDuration || hasVoiceMarker)
}

export function getVoiceUrl(msg) {
  const source = getVoiceContent(msg)
  // Guard JSON.parse: malformed content that merely starts with '{' must not
  // throw during render (matches getVoiceDuration below).
  if (msg.media_local_path) return `/media/${msg.media_local_path}`
  const resource = source?.resource_url
  if (typeof resource === 'string') return resource
  if (!resource || typeof resource !== 'object') return ''
  return resource.url_list?.[0] || resource.url || resource.uri || ''
}

export function getVoiceDuration(msg) {
  const source = getVoiceContent(msg)
  const duration = source?.duration ?? source?.resource_url?.duration
  if (duration === undefined || duration === null || duration === '') return '?'
  const seconds = Number(duration) / 1000
  return Number.isFinite(seconds) ? Math.round(seconds) : '?'
}

// Reply/quote parsing (new field-18 format + legacy formats).
export function getRefMsg(msg) {
  if (!msg.ref_msg) return null
  try {
    const ref = typeof msg.ref_msg === 'string' ? JSON.parse(msg.ref_msg) : msg.ref_msg
    if (ref.content || ref.nickname) return ref
    if (ref.server_id && String(ref.server_id).length >= 15) return ref
    if (ref.content_json) return ref
  } catch {}
  return null
}

export function getRefContent(ref) {
  if (!ref) return ''
  if (ref.content) return ref.content
  if (ref.refmsg_content) {
    try {
      const cj = JSON.parse(ref.refmsg_content)
      if (cj.text) return cj.text
    } catch {}
  }
  if (ref.content_json) {
    try {
      const cj = JSON.parse(ref.content_json)
      if (cj.text) return cj.text
      if (Number(cj.aweType) === 805) return '[分享限时日常]'
      if (cj.content_title) return `[分享] ${cj.content_title}`
      if (cj.aweType === 501 || cj.aweType === 507) return '[表情]'
    } catch {}
    if (!ref.content_json.startsWith('{')) return ref.content_json
  }
  return '[消息]'
}

export function getRefNickname(ref) {
  if (!ref) return ''
  return ref.nickname || ''
}

// User-homepage cards can be stored as text, share, or other by older scrapers.
export function getProfileCard(msg) {
  const cj = getContentJson(msg) || tryParseJson(msg.content)
  if (!cj || !cj.name || !(cj.secUID || cj.sec_uid || (cj.uid && cj.source === 'others_homepage'))) return null
  const id = cj.secUID || cj.sec_uid || String(cj.uid)
  const avatar = cj.avatar_thumb || cj.avatar || cj.avatar_url || cj.cover_url
  return {
    name: String(cj.name),
    avatar: typeof avatar === 'string' ? (/^https?:\/\//.test(avatar) ? avatar : '') : avatar?.url_list?.[0] || '',
    description: cj.desc || '',
    followers: cj.follower_count ?? null,
    url: `https://www.douyin.com/user/${encodeURIComponent(id)}`,
  }
}

export function getForwardInfo(msg) {
  const cj = getContentJson(msg) || tryParseJson(msg.content)
  if (String(cj?.aweType) !== '13600') return null
  return {
    title: cj.title || '聊天记录',
    preview: Array.isArray(cj.list_content) ? cj.list_content : [],
    count: Array.isArray(cj.msg_ids) ? cj.msg_ids.length : null,
  }
}

export function isSystemMsg(msg) {
  if (getProfileCard(msg) || getForwardInfo(msg) || isVoiceMsg(msg)) return false
  if (isLooseEmoji(msg) || isLooseShare(msg) || isLooseImage(msg)) return false
  // 豆包分享卡（aweType=6001）落库是 msg_type=0，但它是一张带封面和标题的正经卡片，
  // 位置跟着发送者走（对方发的在左边、标题在左封面在右），不能当居中的系统提示。
  // src/lib/cardKinds.js 里对应 getMusicCard。
  if (Number(payloadJson(msg)?.aweType) === 6001) return false
  // 群公告在抖音里是居中的卡片（带头像和"谁发布了"），所以也走系统提示那一套排版。
  // 群邀请卡（type_desc=群聊邀请）同样是居中的卡片，由 MessageList 单独排版。
  if (getGroupNotice(msg)) return true
  // 互相关注提示里的纯文本那条是 msg_type=1，内容不是 JSON，按系统提示居中显示。
  if (relationNoticeText(msg)) return true
  return msg.msg_type === 0 || isJsonSystemMsg(msg)
}

// Douyin may return both sender/recipient notifications for one event. Pair
// only opposite templates for the same actor/reference within 30 seconds (observed mirror delays
// reach 15 seconds);
// repeated likes with the same template remain distinct events.
export function duplicateSystemMessageIds(messages) {
  const pending = new Map()
  const hidden = new Set()
  // 关系类提示（互相关注 / 成为好友）没有"双方视角"差异，两侧文本完全相同，
  // 按会话 + 文本在 30 秒窗口内只保留最早一条。
  const relationSeen = new Map()
  for (const msg of messages) {
    const notice = relationNoticeText(msg)
    if (notice) {
      if (!msg.timestamp) continue
      const key = JSON.stringify([msg.conv_id, notice])
      const first = relationSeen.get(key)
      if (first === undefined || msg.timestamp - first > 30) {
        relationSeen.set(key, msg.timestamp)
      } else {
        hidden.add(msg.msg_id)
      }
      continue
    }
    const cj = getContentJson(msg) || tryParseJson(msg.content)
    if (!cj?.tips || !msg.sender_uid || !msg.timestamp) continue
    const like = String(cj.aweType) === '126' && cj.tips.includes('赞了')
    const spark = /^(你|对方)领取了火星/.test(cj.tips)
    if (!like && !spark) continue
    const refs = extractServerMsgIds(msg).join(',')
    if (like && !refs) continue
    const key = JSON.stringify([msg.conv_id, msg.sender_uid, like ? 'like' : 'spark', refs,
      spark ? cj.template : null])
    const prev = pending.get(key)
    if (prev && prev.tips !== cj.tips && Math.abs(msg.timestamp - prev.timestamp) <= 30) {
      hidden.add(msg.msg_id)
      pending.delete(key)
    } else {
      pending.set(key, { timestamp: msg.timestamp, tips: cj.tips })
    }
  }
  return hidden
}
