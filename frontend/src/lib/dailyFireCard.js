// 「陪伴天数」卡的纯解析函数（aweType=110270 · card_key=msg_daily ·
// card_type=im_msg_daily_fire，按钮上写着「限时日常」）。
//
// 这张卡和「获得火花见面礼」（aweType=110408，见 lib/flameCard.js）是同一个 card_key
// 下的两种，长得也像，但数据来源完全不同：
//
//   见面礼卡：背景图、两个头像、按钮都从载荷或素材里来，天数藏在
//             whole_card.action_info.params.extra_params.gift_day。
//   陪伴卡：  **整张卡都要自己画** —— 载荷只给文字、天数和一个头像摆放约定，
//             没有背景图、没有头像链接、没有按钮样式。读的是：
//               content_top.content  组件列表，固定顺序为
//                                    [前文文字] [火花图] [数字滚轮 天数] [后文文字]
//               bottom_right         按钮文字（「限时日常」）
//               bottom_left_content  按钮左侧那颗头像是谁（pattern 指向当前视角的人）
//               top.content.src_polyfill_with_crop  上方两个头像的摆放（image_0 / image_2）
//
// 关于图片：卡片给的火花图和光环图都是抖音图床上的临时链接
// （p3-aweme-im-img.byteimg.com / lf3-static.bytednsdoc.com），链接会过期，
// 而且光环那个域名不在项目的图床白名单里。这两张图项目已经自带
// （assets/fire.png、assets/head_wrapper_circle.png），所以界面直接用自带的那份，
// 不依赖网络，也就没必要在这里返回链接。
//
// 三个约定（用户 2026-10-11 明确）：
//   * 卡片**居中**显示，不跟着发送者分左右；
//   * 按钮里那颗头像放的是**当前视角的人**（切换「我是谁」时跟着换）；
//   * 上方那两个叠在一起的头像**不随视角变**，左边永远是 conv_id 第 2 段、右边第 3 段。
//
// 解析独立成模块（同 flameCard.js / fireInviteCard.js 的约定）：纯函数，便于单测。
import { getContentJson } from './douyinMessage'

// 卡片自己声明的记号。
const DAILY_CARD_KEY = 'msg_daily'
const DAILY_FIRE_AWE_TYPE = 110270
const DAILY_FIRE_CARD_TYPES = new Set(['im_msg_daily_fire'])

// 卡片上那句兜底文案（载荷里读不出文字时用）。
const FALLBACK_TITLE = '限时日常'

// 卡片排版字符串约 5~10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果。
let cardCache = new WeakMap()

export function clearDailyFireCardCache() {
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

// 组件列表按顺序拆开：滚轮前面的字归 before、后面的字归 after，
// 中间那张火花图和滚轮数字单独取出来。
// 实测这行固定是 [前文] [火花] [数字滚轮] [后文]，例如
//   「一起度过了人生的」 + 火花 + 365 + 「天」
//   「 」              + 火花 + 30  + 「，陪伴不止说说而已」
function splitTopNodes(node) {
  const raw = asObject(node)?.content
  if (!Array.isArray(raw)) {
    // 个别载荷把整句话直接放在 content 字符串里，那就只有文字、没有数字。
    return { before: typeof raw === 'string' ? str(raw) : '', after: '', days: '' }
  }
  const before = []
  const after = []
  let days = ''
  let seenRoller = false
  for (const item of raw) {
    const obj = asObject(item)
    if (!obj) continue
    const type = str(obj.type)
    if (type === 'im-image') continue              // 火花图：界面用自带素材画
    if (type === 'im-number-roller') {
      // content 是 {start, end} 这样的下标区间，天数取 end。
      const end = asObject(obj.content)?.end
      if (end !== undefined && end !== null && str(end)) days = str(end)
      seenRoller = true
      continue
    }
    const text = str(obj.content)
    if (!text) continue
    if (seenRoller) after.push(text)
    else before.push(text)
  }
  // 两端文字按空格拼回去：滚轮那一位在客户端是数字滚动组件，文字节点里通常留一个
  // 空格占位（「我们已经互相陪伴」+「天」），所以要把多余空白收干净。
  const tidy = parts => parts.join('').replace(/\s+/g, ' ').trim()
  return { before: tidy(before), after: tidy(after), days }
}

// 天数：优先数字滚轮；老载荷没有滚轮时退回卡片自己那句话里的数字。
function pickDays(rollerDays, before, after, description) {
  if (rollerDays) return rollerDays
  const match = [before, after, description].filter(Boolean).join(' ').match(/\d+/)
  return match ? match[0] : ''
}

function parseDailyFireCard(msg, localUid) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch)
  if (str(patch?.card_key) !== DAILY_CARD_KEY) return null
  // 先认卡片再解析排版：别的分享卡（视频卡等）也带 im_dynamic_patch，
  // 不该为了看一眼就白白解析那几 KB 的排版 JSON。
  const cardType = str(patch?.card_type)
  if (!DAILY_FIRE_CARD_TYPES.has(cardType) && Number(cj.aweType) !== DAILY_FIRE_AWE_TYPE) {
    return null
  }
  const layout = patch ? asObject(patch.raw_data) : null
  if (!layout) return null

  const { before, after, days: rollerDays } = splitTopNodes(layout.content_top)
  const description = str(cj.description) || str(cj.push_detail)
  const days = pickDays(rollerDays, before, after, description)
  // 「一起度过了人生的 365 天」这种：文字里已经写了数字，滚轮又给同一个数，
  // 界面上别再画第二遍。
  const beforeText = days && before.includes(days) ? before.replace(days, '').trim() : before

  // 上方两个头像的归属按载荷语义认，**不随视角变**：
  //   top.content.src_polyfill_with_crop 里
  //     image_0 = other_user_info.avatar_thumb（对方）
  //     image_2 = user_info.avatar_thumb（本机账号）
  //   画在界面上就是「对方在左、本机账号在右」，两个圆右侧叠一点点。
  // 1:1 会话的 conv_id 是 0:1:<甲>:<乙>，「对方」就是本机账号之外的那一个。
  const parts = str(msg.conv_id).split(':')
  const hasPair = parts.length >= 4
  const uidA = hasPair ? parts[2] : ''
  const uidB = hasPair ? parts[3] : ''
  const me = str(localUid)
  // 认不出本机账号时按 conv_id 的先后定序（甲左乙右）—— 同样与视角无关，
  // 绝不会因为切换「我是谁」而左右对调。
  const leftUid = hasPair && me ? (me === uidA ? uidB : uidA) : uidA
  const rightUid = hasPair && me ? (me === uidA ? uidA : uidB) : uidB

  return {
    kind: 'daily-feed',
    // 卡片主文案（前文 + 数字 + 后文），数字和它的前后文分开返回，
    // 界面好在中间插上那颗橙色数字和火花图。
    before: beforeText,
    after,
    days,
    // 按钮：左边是当前视角的人的头像 + 一圈渐变光环，右边才是这行字。
    button: str(asObject(layout.bottom_right)?.content) || FALLBACK_TITLE,
    // 光环里放谁：载荷的 bottom_left_content 指向「当前视角的人」（user_info），
    // 所以这颗头像跟着 effectiveSelfUid 走（切换视角时它要换人）。
    ringFollowsViewer: /user_info\.avatar_thumb/.test(str(asObject(layout.bottom_left_content)?.content)),
    // 上方那两个头像（固定顺序，不随视角变）。
    leftUid,
    rightUid,
    // 搜索结果里的一句话预览：卡片自己那句最完整，没有就用文字节点拼出来的。
    preview: description
      || [beforeText, days, after].filter(Boolean).join(' ')
      || FALLBACK_TITLE,
  }
}

// 卡片数据；不是「陪伴天数」卡则返回 null。
//
// ``localUid`` 是「这台电脑的抖音账号」（界面传 detectedSelf.uid，见 common/owner.py），
// 只用来判断上方两个头像谁是对方、谁是本机账号 —— 它和当前选的视角无关，所以
// 切换「我是谁」不会让那张卡上的人左右对调。
export function getDailyFireCard(msg, localUid = '') {
  if (!msg || typeof msg !== 'object') return null
  const key = str(localUid)
  const perMessage = cardCache.get(msg)
  if (perMessage?.has(key)) return perMessage.get(key)
  const card = parseDailyFireCard(msg, key)
  if (perMessage) perMessage.set(key, card)
  else cardCache.set(msg, new Map([[key, card]]))
  return card
}

export function isDailyFireCard(msg) {
  return !!getDailyFireCard(msg)
}
