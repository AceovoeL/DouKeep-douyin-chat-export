// 「喂精灵」卡的纯解析函数（card_key=msg_fire_invitation 的 feeding 一种）。
//
// 抖音把整张卡片交给 lynx 模板 msg_fire_invitation/template.js 渲染，模板要的数据全在
// im_dynamic_patch.raw_data 里。这族卡片有**两版版式**，靠有没有背景图区分（实测 2026
// 年 7 月那批是第一版、8 月起换成第二版）：
//
//   ① 老版（载荷里没有背景图，whole_card.ui_info 是空的）
//      整张卡用项目自带的 assets/invite-feed.png（浅色渐变底 + 底部「合养精灵」条，
//      尺寸和抖音那张一模一样），上面叠：精灵图（top.content，就是那根辣条）、
//      标题（content_top）、副标题（content_content）、「去喂食」按钮（bottom）。
//      老版载荷里的文字就是卡片上真正显示的字（实测「你还没有喂精灵~」+「一起喂解锁
//      彩蛋效果」），所以这一版**照抄载荷里的原文**，不再写死那两句。
//   ② 新版（载荷里带背景图 whole_card.ui_info.background_image@light）
//      封面由两张图叠出来：背景图垫底、食物图居中偏左盖上去；下半部分是项目自带的
//      assets/feed-card.png。老版用的是同一条消息族的新外观（见 v1.3.x 的说明），
//      标题/副标题固定显示参考图那两句。
//
// 和「合养小火人」（lib/fireBabyCard.js）的关键区别：**喂食卡不等人接受**。用户接受
// 邀请后会另外发一条消息，所以那颗按钮含且仅含「去喂食」一种文案，没有状态之分，
// 也不分「是不是我发的」。
//
// 解析独立成模块（同 fireInviteCard.js / fireBabyCard.js 的约定）：纯函数，便于单测。
import { getContentJson } from './douyinMessage'

// 卡片自己声明的记号：card_key 固定 msg_fire_invitation，card_type 是它的各种变体。
const FIRE_CARD_KEY = 'msg_fire_invitation'

// 喂食卡的 card_type（aweType=110274）。整族里只有这一种是喂食卡，
// 别的小火人 / 合养精灵卡各有各的 card_type，不会被这里认领。
const FEED_CARD_TYPES = new Set(['im_msg_fire_invitation_feeding'])

// 新版卡片上固定显示的两行字（参考图里就是这两句，不随消息里的文案变）。
const FEED_TITLE = '邀请你一起喂精灵'
const FEED_SUBTITLE = '精灵爱吃这个，你也来喂'
// 老版载荷偶尔会缺标题 / 副标题，这两句只当兜底。
const FEED_OLD_TITLE = '你还没有喂精灵~'
const FEED_OLD_SUBTITLE = '一起喂解锁彩蛋效果'
// 那颗按钮只有一种文案：接受邀请后会另外发一条消息，所以没有「待接受」这些状态。
const FEED_BUTTON = '去喂食'

// 卡片排版字符串约 5~10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果。
let cardCache = new WeakMap()

export function clearFeedCardCache() {
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

// 排版里的文字节点：{content:"..."} 或直接一句话。
function textOf(node) {
  if (typeof node === 'string') return node.trim()
  return str(asObject(node)?.content)
}

// 新版封面那两张图：背景在 whole_card.ui_info，食物在 top.content。
function coverImages(layout) {
  const ui = asObject(asObject(layout?.whole_card)?.ui_info) || {}
  const background = imageUrl(ui['background_image@light'])
    || imageUrl(ui['background_image@dark'])
    || imageUrl(ui.background_image)
  const top = asObject(layout?.top)
  return { background, food: top ? imageUrl(top.content) : '' }
}

function isFeedCardType(cardType) {
  return FEED_CARD_TYPES.has(str(cardType))
}

function parseFeedCard(msg) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch)
  if (str(patch?.card_key) !== FIRE_CARD_KEY) return null
  // 先认卡片再解析排版：别的分享卡（视频卡等）也带 im_dynamic_patch，
  // 不该为了看一眼就白白解析那几 KB 的排版 JSON。
  if (!isFeedCardType(patch?.card_type)) return null
  const layout = patch ? asObject(patch.raw_data) : null
  if (!layout) return null

  const { background, food } = coverImages(layout)
  // 老版：载荷没给背景图，卡片外观改用项目自带的 invite-feed.png（见文件头）。
  const old = !background

  const payloadTitle = textOf(layout.content_top)
  const payloadSubtitle = textOf(layout.content_content)
  const payloadBody = textOf(layout.content_content)

  return {
    kind: 'feed',
    // 老版照抄载荷里的字（抖音客户端就是这么显示的），缺了才用兜底那两句；
    // 新版固定显示参考图那两句，不跟着载荷变。
    title: old ? (payloadTitle || FEED_OLD_TITLE) : FEED_TITLE,
    subtitle: old ? (payloadSubtitle || FEED_OLD_SUBTITLE) : FEED_SUBTITLE,
    button: FEED_BUTTON,
    // 老版：带食物 / 精灵图的原始链接，界面走 iconSrc() 交给后端缓存（采集时已经存在
    // data/media/card_icons/ 下，文件名沿用官方原名，见 common/card_icons.py）。
    // 新版：背景图垫底 + 食物图叠在上面。
    background,
    food: old ? '' : food,
    spirit: old ? food : '',
    // 载荷里卡片自己带的那句话（新版实测像「精灵还在慢慢品味中~」），只当搜索预览用。
    body: payloadBody,
    // 搜索结果里的一句话预览：优先卡片自己的话，没有就用固定标题。
    preview: payloadBody
      || payloadTitle
      || str(cj.description) || str(cj.push_detail) || FEED_TITLE,
  }
}

// 卡片数据；不是「喂精灵」卡则返回 null。
export function getFeedCard(msg) {
  if (!msg || typeof msg !== 'object') return null
  const cached = cardCache.get(msg)
  if (cached !== undefined) return cached
  const card = parseFeedCard(msg)
  cardCache.set(msg, card)
  return card
}

export function isFeedCard(msg) {
  return !!getFeedCard(msg)
}
