// 两种「抖音系统卡片」的纯解析函数：群邀请卡（type_code=58）和豆包分享卡（aweType=6001）。
//
// 这两类消息落库时 msg_type=0（other），以前跟系统提示混在一起居中显示，
// 只看得见一行标题，卡片里的群名、封面、简介全丢了：
//
//   群邀请卡 content_json:
//     { title:"群名", desc:"某某 添加你进群", type_desc:"群聊邀请", icon:{url_list:[...]},
//       event.param.conversation_id:"1234567890123456789",
//       aweme_invite_card:{ group_name, conversation_id, group_icon, from_uid, is_in } }
//
//   豆包卡 content_json（aweType=6001，抖音里点开是那个视频）:
//     { aweType:6001, title:"《音乐公开课》……", source_title:"豆包",
//       icon:{url_list:[...]}, open_url:"https://v.douyin.com/…" }
//
// 解析独立成模块（同 goodsCard.js / flameCard.js 的约定）：都是纯函数，便于单测。
import { getContentJson, tryParseJson } from './douyinMessage'

// 豆包分享卡的笑脸类型号（抖音官方叫「音乐/内容卡片」，这里照 aweType 认）。
const MUSIC_CARD_AWE_TYPE = 6001

// 群邀请卡的记号就在载荷里：type_desc 写着「群聊邀请」，并带着目标群的会话 id。
// （protobuf 类型号 58 只存在 message_packets 里，raw_data 不带它，认不得也没关系。）
const INVITE_TYPE_DESCS = new Set(['群聊邀请', '群邀请'])

// 群方括号里的文字太长时（抖音把整句提示塞进 title）不当作群名。
const MAX_GROUP_NAME = 40

let payloadCache = new WeakMap()
let inviteCache = new WeakMap()
let musicCache = new WeakMap()

export function clearSystemCardCache() {
  payloadCache = new WeakMap()
  inviteCache = new WeakMap()
  musicCache = new WeakMap()
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

function imageUrl(value) {
  if (typeof value === 'string') return value
  return value?.url_list?.[0] || ''
}

function str(value) {
  return typeof value === 'string' ? value.trim() : value === undefined || value === null ? '' : String(value)
}

// 一行消息在模板里会被问好几次（判断类型 + 取数据），按行对象缓存。
function parsed(msg) {
  if (!msg || typeof msg !== 'object') return null
  if (payloadCache.has(msg)) return payloadCache.get(msg)
  const cj = getContentJson(msg)
  const source = cj && typeof cj === 'object' ? cj : tryParseJson(msg.content)
  const value = source && typeof source === 'object' ? source : null
  payloadCache.set(msg, value)
  return value
}

// 卡片数据也按行缓存：模板里判断类型和取字段是两次调用，返回同一个对象最省事。
function cached(cache, msg, build) {
  if (cache.has(msg)) return cache.get(msg)
  const value = build()
  cache.set(msg, value)
  return value
}

function convIdOf(cj) {
  const card = asObject(cj.aweme_invite_card) || {}
  return str(card.conversation_id)
    || str(asObject(cj.event?.param)?.conversation_id)
    || str(asObject(cj.event)?.conversation_id)
    || str(card.conversation_short_id)
}

// 群邀请卡：{ groupName, description, icon, convId, fromUid }；不是邀请卡则 null。
export function getInviteCard(msg) {
  if (!msg || typeof msg !== 'object') return null
  return cached(inviteCache, msg, () => parseInviteCard(msg))
}

function parseInviteCard(msg) {
  const cj = parsed(msg)
  if (!cj) return null
  const card = asObject(cj.aweme_invite_card) || {}
  const desc = str(cj.type_desc)
  const convId = convIdOf(cj)
  const isInvite = INVITE_TYPE_DESCS.has(desc)
    || (desc.includes('群') && !!convId)
  if (!isInvite || !convId) return null

  const wholeTitle = str(cj.title)
  const groupName = str(card.group_name)
    || (wholeTitle && wholeTitle.length <= MAX_GROUP_NAME ? wholeTitle : '')
    || '群聊邀请'
  return {
    groupName,
    // 抖音给的是「某某 添加你进群」这类现成句子，抓包里没有时兜底一句通用提示。
    description: str(cj.desc) || str(cj.sub_title) || '邀请你加入群聊',
    icon: imageUrl(cj.icon) || imageUrl(card.group_icon),
    convId,
    fromUid: str(card.from_uid),
  }
}

export function isInviteCard(msg) {
  return !!getInviteCard(msg)
}

// 豆包（内容/音乐）分享卡：{ title, cover, source, url }；不是这张卡则 null。
export function getMusicCard(msg) {
  if (!msg || typeof msg !== 'object') return null
  return cached(musicCache, msg, () => parseMusicCard(msg))
}

function parseMusicCard(msg) {
  const cj = parsed(msg)
  if (!cj || Number(cj.aweType) !== MUSIC_CARD_AWE_TYPE) return null
  const title = str(cj.title) || str(cj.push_detail)
  const cover = imageUrl(cj.icon) || imageUrl(cj.cover_url)
  if (!title && !cover) return null
  return {
    title,
    cover,
    // 卡片右下角那行小字：抖音写「豆包」（来源 App 的名字）。
    source: str(cj.source_title) || str(cj.source_name) || str(cj.desc),
    url: str(cj.open_url),
  }
}

export function isMusicCard(msg) {
  return !!getMusicCard(msg)
}
