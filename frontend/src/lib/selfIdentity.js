// 「我是谁」相关的纯逻辑（组件只负责取值和渲染）。
//
// 两件事：
//
// 1. senderDisplayName —— 一条消息该显示谁的名字。单聊里不认识的发送者用会话名
//    （= 用户给对方的备注名，和侧边栏显示的一致），但**本机账号自己发的消息绝不能
//    用会话名**：那样没选过「我」的时候，两边都会显示成对方的名字。
// 2. selfPickerOptions —— 「设置我」弹窗列哪些候选。群聊里自己可能一条消息都没发过，
//    于是压根不在发送者名单里；后端认出来的本机账号要补进去，否则选不了自己。
//
// 这里只处理传进来的数据，不碰真实昵称和 uid —— 测试里用的都是占位示例。

//: 认不出本机账号昵称时的占位名
export const SELF_FALLBACK_NAME = '我'

export function senderDisplayName(msg, {
  conversationName = '',
  isGroup = false,
  embedded = false,
  selfUid = '',
  users = {},
} = {}) {
  const uid = String(msg?.sender_uid ?? '')
  if (selfUid && uid === String(selfUid)) {
    return users[uid]?.nickname || SELF_FALLBACK_NAME
  }
  // 单聊的会话名就是「我给对方起的名字」：粉丝团表情、群邀请卡这类消息落库时
  // sender_name 是空的，用会话名才对得上侧边栏 —— 抖音昵称随时会改，users 表里的
  // 往往不是用户熟悉的那个。
  if (!isGroup && !embedded && conversationName) {
    return conversationName
  }
  const known = users[uid]?.nickname
  if (known) return known
  if (msg?.sender_name && msg.sender_name !== '__self__') {
    return msg.sender_name
  }
  // 群聊 / 合并转发里回退成会话名会把每个不认识的人都显示成群名（糊成同一个），
  // 所以给一个带 uid 后缀的临时名字；单聊没这个问题。
  if (embedded || isGroup) {
    return uid ? `用户${uid.slice(-6)}` : '群成员'
  }
  return conversationName || '对方'
}

export function selfPickerOptions(senders, selfUid = '', { name = '' } = {}) {
  const uid = String(selfUid || '')
  const options = (Array.isArray(senders) ? senders : [])
    .filter(s => s && s.sender_uid)
    .map(s => ({ ...s }))
  if (uid && !options.some(s => String(s.sender_uid) === uid)) {
    // 这个会话里没有它的消息（群聊里很常见）：补一条，标出来是自动认出来的
    options.push({ sender_uid: uid, msg_count: 0, nickname: name, discovered: true })
  }
  return options
}

export function pickerOptionLabel(option, { users = {} } = {}) {
  const uid = String(option?.sender_uid || '')
  const nickname = option?.nickname || users[uid]?.nickname
  if (nickname) return nickname
  return uid ? `UID ...${uid.slice(-6)}` : '未知'
}
