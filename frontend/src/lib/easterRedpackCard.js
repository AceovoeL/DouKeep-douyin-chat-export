// 「彩蛋福袋」卡的纯解析函数（aweType=110360 · card_key=msg_easter_redpack ·
// card_type=pay_msg_easter_redpack_card）。
//
// 抖音把整张卡交给 lynx 模板 msg_easter_redpack/template.js 渲染，模板要的数据在
// im_dynamic_patch.raw_data 里：
//   top.cover_image    卡片上半部分那张插图（带签名的临时链接，会过期）
//   bottom.content     卡片底部的名字（「彩蛋福袋」）
//   content_content    中间那颗按钮（「拆福袋」/「已领取」…，随状态变）
//
// 这个项目里这张卡**一律按「已过期」画**（用户 2026-10-17 要求）：不管载荷里的文案
// 是「拆福袋」「已领取」还是「您有一个彩蛋福袋待领取」，中部偏下的状态文字固定写
// 「已过期」，底部名字固定写「彩蛋福袋」。
//
// 底图用项目自带的 assets/gift.png，上半部分那张插图叠在它上面；插图链接过期就只剩
// 底图和文字（采集时已尽量存进 data/media/card_icons/，见 common/card_icons.py）。
// 解析独立成模块（同 feedCard.js / dailyFireCard.js 的约定）：纯函数，便于单测。
import { getContentJson } from './douyinMessage'

const AWE_TYPE = 110360
const CARD_KEY = 'msg_easter_redpack'
const CARD_TYPES = new Set(['pay_msg_easter_redpack_card'])

// 这张卡在本项目里固定显示的两句话：
//   中部偏下 = 状态（抖音给「已领取」，这里统一改成「已过期」）
//   底部     = 卡片名字
const STATUS_TEXT = '已过期'
const TITLE_TEXT = '彩蛋福袋'

let cardCache = new WeakMap()

export function clearEasterRedpackCardCache() {
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

function str(value) {
  if (typeof value === 'string') return value.trim()
  return value === undefined || value === null ? '' : String(value).trim()
}

// 抖音的图片字段有两种形状：直接是字符串，或 {url_list:[...]}。
function imageUrl(value) {
  if (typeof value === 'string') return value.trim()
  const urls = asObject(value)?.url_list
  if (Array.isArray(urls) && typeof urls[0] === 'string') return urls[0].trim()
  return ''
}

export function isEasterRedpackCard(msg) {
  return !!getEasterRedpackCard(msg)
}

// 卡片数据：{ cover, status, title, preview }；不是这张卡则返回 null。
export function getEasterRedpackCard(msg) {
  if (!msg || typeof msg !== 'object') return null
  const cached = cardCache.get(msg)
  if (cached !== undefined) return cached
  const card = parseEasterRedpackCard(msg)
  cardCache.set(msg, card)
  return card
}

function parseEasterRedpackCard(msg) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch) || {}
  const recognized = str(patch.card_key) === CARD_KEY
    || CARD_TYPES.has(str(patch.card_type))
    || str(cj.aweType) === String(AWE_TYPE)
  if (!recognized) return null

  const layout = asObject(patch.raw_data) || {}
  const top = asObject(layout.top)
  // 插图优先取排版里的 top.cover_image；个别老载荷放在顶层 cover_url / icon。
  const cover = imageUrl(top?.cover_image)
    || imageUrl(top?.content)
    || imageUrl(layout.cover_image)
    || imageUrl(cj.cover_url)
    || imageUrl(cj.icon)

  return {
    cover,
    // 状态和名字是写死的（见文件头）：载荷里是「拆福袋」「已领取」都不影响。
    status: STATUS_TEXT,
    title: TITLE_TEXT,
    // 搜索/转发里的一句话预览用载荷原文，缺了就用卡片名兜底。
    preview: str(cj.description) || str(cj.push_detail) || TITLE_TEXT,
  }
}
