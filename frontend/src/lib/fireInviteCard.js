// 「聊天火花」关系卡的纯解析函数（card_key=msg_fire_invitation）。
//
// 抖音把整张卡片交给 lynx 模板 msg_fire_invitation/template.js 渲染，模板要的数据全在
// im_dynamic_patch.raw_data 里。这一族卡片有十几种，仓库里只带了「聊天火花」这两种的
// 卡片外观（assets/request.png、assets/accept.png）；别的小火人 / 合养精灵 / 喂精灵卡
// 是另一套外观和另一个 card_type，照旧按系统提示显示。
//
//   A. 邀请（top 是一张 im-image，卡片上只有一颗火花）
//      top.content            火花图标（姐妹/兄弟/伴侣/死党各不相同，就是这张决定的）
//      content_top            content@guest「邀请你更改为姐妹火花」
//                             content@sender「邀请更改为姐妹火花」
//      content_content        副标题「更改后标识将会替换」，后面常跟一个 › 图标
//      bottom                 content@1&guest「接受邀请」/ content@1&sender「待接受」
//                             content@2「已接受」 / content@3「已失效」
//
//   B. 接受纪念（top@1 是 im-avatar-animax：两个头像中间夹一颗火花）
//      top@1.content.flame_icon / .left / .right    火花 + 左右两个人
//      content_content        「我们开始拥有姐妹火花啦」
//      bottom.content         「纪念一下」（已经画进背景图里了）
//
// 当前状态号在 whole_card.extra_info.status（1 待处理 / 2 已接受 / 3 已失效）。
//
// 卡片外观直接用项目 assets/request.png 与 assets/accept.png：两张都是抖音客户端里那张
// 卡的原样截图（含底部「聊天火花」条，accept.png 连「纪念一下」那颗按钮都一起带了），
// 所以这里只负责「认出卡片 + 读出文案、按钮、头像、火花图标」，坐标与排版交给
// MessageList.vue。
// 解析独立成模块（同 flameCard.js / callShopCard.js 的约定）：纯函数，便于单测。
import { getContentJson } from './douyinMessage'

// 卡片自己声明的记号：card_key 固定 msg_fire_invitation，card_type 是它的各种变体。
const FIRE_CARD_KEY = 'msg_fire_invitation'

// 「聊天火花」关系卡的两种形态（邀请 / 接受纪念）。
//
// 抖音的这一族卡按关系分姐妹 / 兄弟 / 伴侣 / 死党火花等好几种，但它们**共用同一套
// card_type**（下面这两个），卡片底纹和底部那条「聊天火花」也是客户端统一画的 ——
// 区分彼此的唯一东西就是**上面那颗火花图标**（载荷 ``top`` / ``top@1`` 里的图片）。
// 所以这里不能按「图标是不是姐妹火花那张」来限定，否则兄弟 / 伴侣 / 死党火花会被漏掉
// 或者画错图；认卡片只看 card_type，图标一律用载荷给的那张（见 common/card_icons.py
// 的 is_fire_card，采集时会把它存进 data/media/card_icons/）。
//
// 别的小火人 / 合养精灵卡是另一个页脚（「合养精灵」）、另一套外观，card_type 也不同，
// 不会被这里认领。
const FIRE_CARD_TYPES = new Set([
  'im_msg_fire_invitation_send',              // 邀请更改为姐妹/兄弟/伴侣/死党火花
  'im_msg_fire_invitation_accept_memory',     // 你们已更改为 XX 火花（纪念卡）
])

// accept.png 里已经画好的那颗按钮；卡片自己要画的文字和它一样时，界面不要再画一遍
// （否则会叠出两层）。
const BAKED_ACCEPT_BUTTON = '纪念一下'

// 卡片排版字符串约 5~10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果。
// 缓存键带 selfUid：标题和按钮文字都取决于「以谁的口吻看这张卡」。
let cardCache = new WeakMap()

export function clearFireInviteCardCache() {
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

// 收件人看 guest 版（「邀请你更改为姐妹火花」），发件人看 sender 版。
function roleSuffixes(role) {
  return role === 'sender' ? ['@1&sender', '@sender'] : ['@1&guest', '@guest']
}

// 按钮文字：状态 2 / 3（已接受 / 已失效）是双方共用的说法，状态 1 才分人称。
function buttonText(bottom, status, role) {
  const obj = asObject(bottom)
  if (!obj) return ''
  const state = ['2', '3'].includes(str(status)) ? [`@${status}`] : roleSuffixes(role)
  return variantText(obj, state)
}

// 说话人的排列：[1] 是发卡的人，[2] 是对方。实测样本里 top@1.content 的 left 就是
// 发卡那个人（自己接受时 left 就是「我」），所以这里按「左=发卡方、右=对方」约定。
function topAvatars(layout) {
  const animax = asObject(layout?.['top@1'])?.content
  if (animax && typeof animax === 'object') {
    return { left: str(animax.left), right: str(animax.right) }
  }
  const content = asObject(layout?.top)?.content
  if (Array.isArray(content)) {
    return { left: str(content[0]), right: str(content[1]) }
  }
  return { left: '', right: '' }
}

// 顶部那颗火花：邀请卡在 top.content，接受纪念卡在 top@1.content.flame_icon。
function topIcon(layout) {
  const animax = asObject(layout?.['top@1'])?.content
  if (animax && typeof animax === 'object') {
    const url = str(animax.flame_icon) || str(animax.icon)
    if (url) return url
  }
  const top = asObject(layout?.top)
  const direct = str(top?.content)
  if (direct) return direct
  return str(asObject(top?.top_content)?.content) || str(top?.top_content)
}

// 是不是「两个头像夹一颗火花」那版（接受纪念卡）。
function isAvatarTop(layout) {
  if (asObject(layout?.['top@1'])) return true
  return str(asObject(layout?.top)?.type) === 'im-avatar-animax'
}

function isFireCardType(cardType) {
  return FIRE_CARD_TYPES.has(str(cardType))
}

// 单聊的 conv_id 形如 0:1:uidA:uidB；返回除发卡人之外的那一个（群聊认不出，返回空）。
function otherParticipant(convId, senderUid) {
  const parts = str(convId).split(':')
  if (parts.length < 4) return ''
  const [, , uidA, uidB] = parts
  if (!senderUid) return uidB
  if (uidA === str(senderUid)) return uidB
  if (uidB === str(senderUid)) return uidA
  return ''
}

function parseFireInviteCard(msg, selfUid) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch)
  if (str(patch?.card_key) !== FIRE_CARD_KEY) return null
  // 先认卡片再解析排版：别的分享卡（视频卡等）也带 im_dynamic_patch，
  // 不该为了看一眼就白白解析那几 KB 的排版 JSON。
  const layout = patch ? asObject(patch.raw_data) : null
  if (!layout) return null
  // 认卡片只看类型号，不看图标是什么花 —— 姐妹 / 兄弟 / 伴侣 / 死党火花共用这两个
  // card_type，图标才是区分它们的东西，硬认某一种的图标会把别的漏掉。
  if (!isFireCardType(patch?.card_type)) return null

  // 「以谁的口吻看这张卡」：这条是我发的 → sender，否则 → guest（收件人）。
  // 认不出「我」时按 guest，和抖音给收件人的说法一致。
  const mine = !!(selfUid && msg.sender_uid && String(msg.sender_uid) === String(selfUid))
  const role = mine ? 'sender' : 'guest'
  const kind = isAvatarTop(layout) ? 'accept' : 'invite'

  const status = str(asObject(layout.whole_card?.extra_info)?.status) || '1'
  const button = kind === 'invite'
    ? buttonText(layout.bottom, status, role)
    : str(asObject(layout.bottom)?.content)
  const avatars = topAvatars(layout)
  const senderUid = str(msg.sender_uid)

  return {
    kind,
    title: variantText(layout.content_top, roleSuffixes(role)),
    subtitle: nodeText(layout.content_content),
    // 副标题后面那个 ›（载荷里是个 im-icon 组件）。
    arrow: hasArrow(layout.content_content),
    button,
    // 背景图里已经画好了同一颗按钮，界面就别再叠一个。
    buttonBakedIn: kind === 'accept' && button === BAKED_ACCEPT_BUTTON,
    // 状态 1 又是给我看的（收件人视角）= 这颗按钮在等我处理，抖音给它 Primary 配色；
    // 待接受 / 已接受 / 已失效都画成灰的。按钮一律点不动（这里只是展示聊天记录）。
    buttonPrimary: status === '1' && role === 'guest',
    // 卡片上那颗火花：抖音给的是带签名的临时链接，所以一律走 iconSrc() 交给后端缓存
    // （采集时已经存进 data/media/card_icons/，见 common/card_icons.py 的 is_fire_card）。
    icon: topIcon(layout),
    // 卡片自带的头像是发消息那天的快照，界面会优先用 users 表里的最新那份。
    leftUid: senderUid,
    rightUid: otherParticipant(msg.conv_id, senderUid),
    leftAvatar: avatars.left,
    rightAvatar: avatars.right,
    status,
    // 搜索结果里的一句话预览（固定收件人说法）。
    preview: variantText(layout.content_top, ['@guest', '@sender'])
      || str(cj.description) || str(cj.push_detail) || str(cj.receiver_description),
  }
}

// 卡片数据；不是「聊天火花」关系卡则返回 null。
export function getFireInviteCard(msg, selfUid = '') {
  if (!msg || typeof msg !== 'object') return null
  const key = String(selfUid || '')
  const perMessage = cardCache.get(msg)
  if (perMessage?.has(key)) return perMessage.get(key)
  const card = parseFireInviteCard(msg, key)
  if (perMessage) perMessage.set(key, card)
  else cardCache.set(msg, new Map([[key, card]]))
  return card
}

export function isFireInviteCard(msg) {
  return !!getFireInviteCard(msg)
}
