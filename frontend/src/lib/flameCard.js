// 「获得火花见面礼」卡片（aweType=110408 · im_dynamic_patch.card_type=im_msg_daily_flame_gift）
// 的纯解析函数。抖音把整张卡片交给一个 lynx 模板渲染，模板要的数据全在
// im_dynamic_patch.raw_data 里：
//   top.content.src_polyfill_with_crop   两个圆形头像（image_0 / image_2）
//   content_top.content                  组件列表 im-component-list：
//                                        [im-text 空] [im-image 火花图标] [im-number-roller 天数] [im-text 文案]
//   bottom_right.content                 按钮文字（「立即领取」）
//   whole_card.action_info[0].params.extra_params.gift_day   火花天数
// 解析独立成模块（同 goodsCard.js 的约定）：都是纯函数，便于单测。
import { getContentJson } from './douyinMessage'

const FLAME_AWE_TYPE = 110408
const FLAME_CARD_TYPES = new Set(['im_msg_daily_flame_gift'])

// 卡片排版字符串约 10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果。
// 缓存键带 selfUid：头像归属取决于「谁是我」。
let cardCache = new WeakMap()

export function clearFlameCardCache() {
  cardCache = new WeakMap()
}

function asObject(value) {
  if (!value) return null
  if (typeof value !== 'string') return typeof value === 'object' ? value : null
  try {
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

// 排版节点 → 文字。content_top 是组件列表，图片/数字滚轮要跳过，只拼文字节点
// （否则整个数组会被当成标题渲染成 "[object Object],[object Object],…"）。
function nodeText(node) {
  const obj = asObject(node)
  const content = obj?.content
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content
    .filter(item => asObject(item)?.type === 'im-text')
    .map(item => String(item.content ?? ''))
    .join('')
    .trim()
}

// 数字滚轮给的是下标区间 {start, end}，天数取 end。
function rollerNumber(node) {
  const obj = asObject(node)
  if (obj?.type !== 'im-number-roller') return ''
  const end = asObject(obj.content)?.end
  return end === undefined || end === null ? '' : String(end).trim()
}

function giftDays(layout) {
  const whole = asObject(layout?.whole_card)
  const action = Array.isArray(whole?.action_info) ? asObject(whole.action_info[0]) : null
  const extra = asObject(action?.params?.extra_params)
  const logInfo = asObject(asObject(whole?.extra_info)?.log_info)
  const direct = extra?.gift_day ?? logInfo?.gift_day_num
  if (direct !== undefined && direct !== null && String(direct).trim()) return String(direct).trim()
  const items = asObject(layout?.content_top)?.content
  if (Array.isArray(items)) {
    for (const item of items) {
      const number = rollerNumber(item)
      if (number) return number
    }
  }
  return ''
}

// 卡片上的两个头像：按 id 建表，同时保留原始顺序做兜底。
function cardAvatars(layout) {
  const raw = asObject(layout?.top)?.content?.src_polyfill_with_crop
  const byId = {}
  const ordered = []
  for (const item of Array.isArray(raw) ? raw : []) {
    const obj = asObject(item)
    const url = typeof obj?.content === 'string' ? obj.content : asObject(obj?.content)?.url_list?.[0] || ''
    if (!url) continue
    ordered.push(url)
    if (obj.id) byId[obj.id] = url
  }
  return { byId, ordered }
}

// conv_id 形如 0:1:<uidA>:<uidB>。实测 13 条样本，卡片固定把 uidB（第 3 段）画在
// image_0、uidA（第 2 段）画在 image_2，所以头像归属要按 uid 认，不能按图片顺序认。
function avatarFor(avatars, convId, uid) {
  const parts = String(convId || '').split(':')
  const isB = !!uid && uid === parts[3]
  return (isB ? avatars.byId.image_0 : avatars.byId.image_2)
    || avatars.ordered[isB ? 0 : 1]
    || ''
}

function plainTitle(msg, cj, patch) {
  const text = String(msg?.content || '')
  if (text && !text.startsWith('{')) return text.trim()
  return String(patch?.description || cj?.description || '').trim()
}

function parseFlameGiftCard(msg, selfUid) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch)
  const cardType = String(patch?.card_type || '')
  const cardKey = String(patch?.card_key || '')
  const isFlame = FLAME_CARD_TYPES.has(cardType)
    || FLAME_CARD_TYPES.has(cardKey)
    || Number(cj.aweType) === FLAME_AWE_TYPE
  if (!isFlame) return null
  // 先认卡片再解析排版：别的分享卡（视频卡等）也带 im_dynamic_patch，
  // 不该为了看一眼就白白解析那几 KB 的排版 JSON。
  const layout = patch ? asObject(patch.raw_data) : null
  if (!layout) return null

  const parts = String(msg.conv_id || '').split(':')
  const uidA = parts[2] || ''
  const uidB = parts[3] || ''
  // 「我」默认是本机账号（第 2 段）；用户手工选了 selfUid 且它落在第 3 段时两人对调。
  const mineIsB = !!selfUid && selfUid === uidB && selfUid !== uidA
  const selfPerson = mineIsB ? uidB : uidA
  const peerPerson = mineIsB ? uidA : uidB
  const avatars = cardAvatars(layout)

  return {
    days: giftDays(layout),
    title: nodeText(layout.content_top) || plainTitle(msg, cj, patch) || '获得火花见面礼',
    button: String(asObject(layout.bottom_right)?.content || '').trim() || '立即领取',
    selfUid: selfPerson,
    peerUid: peerPerson,
    selfAvatar: avatarFor(avatars, msg.conv_id, selfPerson),
    peerAvatar: avatarFor(avatars, msg.conv_id, peerPerson),
  }
}

// 卡片数据：{ days, title, button, selfAvatar, peerAvatar }；不是火花卡则返回 null。
export function getFlameGiftCard(msg, selfUid = '') {
  if (!msg) return null
  const key = String(selfUid || '')
  const perMessage = cardCache.get(msg)
  if (perMessage?.has(key)) return perMessage.get(key)
  const card = parseFlameGiftCard(msg, key)
  if (perMessage) perMessage.set(key, card)
  else cardCache.set(msg, new Map([[key, card]]))
  return card
}

export function isFlameGiftCard(msg) {
  return !!getFlameGiftCard(msg)
}

// 卡片自带的头像是**消息发出那一刻**的快照，而聊天查看器里的头像是 users 表里的
// 最新那份（每次抓取都会更新）。两边不一样时，这里优先用最新的那份，让卡片和
// 查看器保持一致；只有拿不到这个人的资料时才退回卡片里存的那张。
export function pickFlameAvatar(card, which, currentAvatar = '') {
  if (!card) return ''
  const self = which === 'self'
  return String(currentAvatar || '') || (self ? card.selfAvatar : card.peerAvatar)
}
