// 「合养小火人」卡片（card_key=msg_fire_invitation 里的 joint_* 三种）的纯解析函数。
//
// 抖音把整张卡片交给 lynx 模板 msg_fire_invitation/template.js 渲染，模板要的数据全在
// im_dynamic_patch.raw_data 里；卡片外观是固定的两张素材（assets/request-firebaby.png、
// assets/accept-firebaby.png —— 顶上那颗小火人和底部「合养精灵」那一条都印在图里）。
//
// 这一族和「聊天火花」（lib/fireInviteCard.js）共用 card_key，但 card_type、外观和文案
// 完全是两套：那边是姐妹 / 兄弟 / 伴侣 / 死党火花，这边是合养小火人。两边各认各的
// card_type，谁也不抢谁的卡。
//
//   A. 邀请（im_msg_fire_invitation_joint_send，aweType=110182）
//      content_top      content@guest「对方邀请你一起养小火人」
//                       content@sender「邀请对方一起养小火人」
//      content_content  「查看合养小火人」后面跟一个 › 图标
//      bottom           content@1&guest「接受邀请」 / content@1&sender「待对方接受」
//                       content@2「已接受」 / content@3「邀请已失效」 / content@4「暂不支持合养」
//
//   B. 开始合养（im_msg_fire_invitation_joint_accept，aweType=110183）
//      content_top「我们开始养小火人啦」、content_content「连续聊天让精灵快快长大」、
//      bottom「去看看」。这两句话和素材图 accept-firebaby.png 里印的一模一样，
//      所以界面不必重画文字，只补那颗按钮（见下面 textBakedIn）。
//
//   C. 纪念（im_msg_fire_invitation_joint_accept_memory，aweType=110279）
//      content_top「我们开始养小火人啦」、content_content「今天是我们拥有小火人的第一天」、
//      bottom「纪念一下」。文案和 B 对不上，素材图没法直接用，所以挑那张空白的底纹自己画。
//
// A、C 用 assets/request-firebaby.png（图上只有小火人，文字一片空白），标题放不下时换行、
// 不省略 —— 抖音自己会把「对方邀请你一起养小火人」截成「对方邀请你一起养小…」，
// 这里要求把话说完。解析独立成模块（同 fireInviteCard.js 的约定）：纯函数，便于单测。
import { getContentJson } from './douyinMessage'

// 卡片自己声明的记号：card_key 固定 msg_fire_invitation，card_type 是它的各种变体。
const FIRE_CARD_KEY = 'msg_fire_invitation'

// 合养小火人这一族的三个类型号。
const REQUEST_TYPES = new Set(['im_msg_fire_invitation_joint_send'])
const ACCEPT_TYPES = new Set(['im_msg_fire_invitation_joint_accept'])
const MEMORY_TYPES = new Set(['im_msg_fire_invitation_joint_accept_memory'])

// accept-firebaby.png 里印好的那两句话。卡片载荷给出的文案和它俩一致时，界面才敢
// 只画按钮（否则会把图上那句不相干的话露出来）；对不上就改用空白底纹自己画。
const BAKED_TITLE = '我们开始养小火人啦'
const BAKED_SUBTITLE = '连续聊天让精灵快快长大'

// 卡片排版字符串约 5~10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果。
// 缓存键带 selfUid：标题和按钮文字都取决于「以谁的口吻看这张卡」。
let cardCache = new WeakMap()

export function clearFireBabyCardCache() {
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

// 排版节点 → 文字。content 可能是字符串，也可能是一串组件（im-component-list /
// im-graphic-text）；后者只拼里面的 im-text，否则整串会被渲染成 "[object Object]"。
function nodeText(node) {
  const content = asObject(node)?.content
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content
    .filter(item => asObject(item)?.type === 'im-text')
    .map(item => str(asObject(item)?.content))
    .join('')
    .trim()
}

// 组件列表里有没有图标组件（副标题后面那个 › 就是一个 im-icon）。
function hasArrow(node) {
  const content = asObject(node)?.content
  if (!Array.isArray(content)) return false
  return content.some(item => asObject(item)?.type === 'im-icon')
}

// 带条件后缀的取值：排版键会带后缀（@sender / @guest / @1&guest / @2 …），按候选
// 顺序取第一个有内容的；都没有就退回不带后缀的 content。
function variantText(node, suffixes) {
  const obj = asObject(node)
  if (!obj) return ''
  for (const suffix of ['', ...suffixes]) {
    const key = suffix ? `content${suffix}` : 'content'
    if (!(key in obj)) continue
    const text = nodeText({ content: obj[key] })
    if (text) return text
  }
  return ''
}

// 收件人看 guest 版（「对方邀请你一起养小火人」），发件人看 sender 版。
function roleSuffixes(role) {
  return role === 'sender' ? ['@1&sender', '@sender'] : ['@1&guest', '@guest']
}

// 三个类型号 → 三种处理方式；不是这一族则返回空串。
function cardKind(cardType) {
  const type = str(cardType)
  if (REQUEST_TYPES.has(type)) return 'request'
  if (ACCEPT_TYPES.has(type)) return 'accept'
  if (MEMORY_TYPES.has(type)) return 'memory'
  return ''
}

// 按钮文字：状态 2 / 3 / 4（已接受 / 已失效 / 暂不支持）是双方共用的说法，状态 1 才分人称。
function buttonText(bottom, status, role) {
  const obj = asObject(bottom)
  if (!obj) return ''
  const state = ['2', '3', '4'].includes(str(status)) ? [`@${status}`] : roleSuffixes(role)
  return variantText(obj, state)
}

function parseFireBabyCard(msg, selfUid) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch)
  if (str(patch?.card_key) !== FIRE_CARD_KEY) return null
  // 先认卡片再解析排版：别的分享卡（视频卡等）也带 im_dynamic_patch，
  // 不该为了看一眼就白白解析那几 KB 的排版 JSON。
  const kind = cardKind(patch?.card_type)
  if (!kind) return null
  const layout = patch ? asObject(patch.raw_data) : null
  if (!layout) return null

  // 「以谁的口吻看这张卡」：这条是我发的 → sender，否则 → guest（收件人）。
  // 认不出「我」时按 guest，和抖音给收件人的说法一致。
  const mine = !!(selfUid && msg.sender_uid && String(msg.sender_uid) === String(selfUid))
  const role = mine ? 'sender' : 'guest'

  const status = str(asObject(layout.whole_card?.extra_info)?.status) || '1'
  const title = variantText(layout.content_top, roleSuffixes(role))
  const subtitle = nodeText(layout.content_content)
  const button = kind === 'request'
    ? buttonText(layout.bottom, status, role)
    : str(asObject(layout.bottom)?.content)

  return {
    kind,
    status,
    title,
    subtitle,
    // 副标题后面那个 ›（载荷里是个 im-icon 组件）。
    arrow: hasArrow(layout.content_content),
    button,
    // 素材图 accept-firebaby.png 里已经印好了同一套文字，界面别再画一遍（否则会叠两层）。
    textBakedIn: kind === 'accept' && title === BAKED_TITLE && subtitle === BAKED_SUBTITLE,
    // 状态 1 又是给我看的（收件人视角）= 这颗按钮在等我处理，抖音给它 Primary 配色；
    // 待接受 / 已接受 / 已失效都画成灰的。合养成功那两张卡固定是 Primary。
    buttonPrimary: kind === 'request' ? (status === '1' && role === 'guest') : true,
    // 搜索结果里的一句话预览（固定收件人说法）。
    preview: variantText(layout.content_top, ['@guest', '@sender'])
      || str(cj.description) || str(cj.push_detail) || title,
  }
}

// 卡片数据；不是「合养小火人」卡则返回 null。
export function getFireBabyCard(msg, selfUid = '') {
  if (!msg || typeof msg !== 'object') return null
  const key = String(selfUid || '')
  const perMessage = cardCache.get(msg)
  if (perMessage?.has(key)) return perMessage.get(key)
  const card = parseFireBabyCard(msg, key)
  if (perMessage) perMessage.set(key, card)
  else cardCache.set(msg, new Map([[key, card]]))
  return card
}

export function isFireBabyCard(msg) {
  return !!getFireBabyCard(msg)
}
