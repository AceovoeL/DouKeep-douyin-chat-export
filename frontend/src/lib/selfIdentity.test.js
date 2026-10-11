import { describe, it, expect } from 'vitest'
import { senderDisplayName, selfPickerOptions, pickerOptionLabel } from './selfIdentity.js'

// 全部用占位数据：真实昵称和 uid 不进仓库。

const ME = 'uid-me-0001'
const PEER = 'uid-peer-0002'

describe('senderDisplayName · 单聊', () => {
  const conv = {
    conversationName: '给对方的备注名',
    isGroup: false,
    selfUid: ME,
    localUid: ME,
    users: { [ME]: { nickname: '本机账号昵称' }, [PEER]: { nickname: '对方的抖音昵称' } },
  }

  it('自己发的消息显示自己的昵称，不是对方的名字', () => {
    expect(senderDisplayName({ sender_uid: ME }, conv)).toBe('本机账号昵称')
  })

  it('users 表里还没有自己时退回「我」', () => {
    expect(senderDisplayName({ sender_uid: ME }, { ...conv, users: {} })).toBe('我')
  })

  it('对方发的消息用会话名（用户给对方的备注名，和侧边栏一致）', () => {
    expect(senderDisplayName({ sender_uid: PEER }, conv)).toBe('给对方的备注名')
  })

  it('认不出「我是谁」时，两条消息都不会被当成自己', () => {
    // 这是以前的毛病：两边都显示成对方的名字。至少自己那条不能再被认成对方。
    // 认不出「我是谁」时本机账号也无从得知，所以 localUid 同样是空的
    //（它和 selfUid 都来自后端的同一份推断，见 common/owner.py）。
    const blind = { ...conv, selfUid: '', localUid: '' }
    expect(senderDisplayName({ sender_uid: ME }, blind)).toBe('给对方的备注名')
    expect(senderDisplayName({ sender_uid: 'uid-someone-3' }, blind)).toBe('给对方的备注名')
  })
})

// 用户 2026-10-11 反馈：视角切到对方之后，双方昵称都变成了对方的名字。
describe('senderDisplayName · 视角切到对方时不能两边同名', () => {
  const conv = {
    conversationName: '给对方的备注名',
    isGroup: false,
    selfUid: ME,
    localUid: ME,          // 本机账号始终是 ME，视角是 PEER
    users: { [ME]: { nickname: '本机账号昵称' }, [PEER]: { nickname: '对方的抖音昵称' } },
  }

  it('视角切到对方时，本机账号那条不再套用会话名', () => {
    const asPeer = { ...conv, selfUid: PEER }
    expect(senderDisplayName({ sender_uid: ME }, asPeer)).toBe('本机账号昵称')
    expect(senderDisplayName({ sender_uid: PEER }, asPeer)).toBe('对方的抖音昵称')
  })

  it('两侧名字不会变成同一个', () => {
    const asPeer = { ...conv, selfUid: PEER }
    const left = senderDisplayName({ sender_uid: ME }, asPeer)
    const right = senderDisplayName({ sender_uid: PEER }, asPeer)
    expect(left).not.toBe(right)
  })

  it('本机账号查不到昵称时，也不该在对方视角下自称「我」', () => {
    const asPeer = { ...conv, selfUid: PEER, users: {} }
    const name = senderDisplayName({ sender_uid: ME }, asPeer)
    expect(name).not.toBe('我')
    expect(name).not.toBe('给对方的备注名')
    expect(name).toBe('用户' + ME.slice(-6))
  })
})

// 用户 2026-10-11 反馈：小火人 AI 的昵称显示成了对方的昵称。
describe('senderDisplayName · 小火人（精灵 AI）', () => {
  const spiritMsg = {
    sender_uid: 'uid-spirit-1',
    content: '示例正文',
    raw_data: JSON.stringify({ content_json: JSON.stringify({
      aweType: 716, text: '示例正文',
      ai_business_ext: { petai_style: '示例性格' },
    }) }),
  }
  const conv = {
    conversationName: '给对方的备注名',
    isGroup: false,
    selfUid: ME,
    localUid: ME,
    users: { [ME]: { nickname: '本机账号昵称' }, [PEER]: { nickname: '对方的抖音昵称' } },
  }

  it('精灵消息显示「精灵AI」，不是会话名', () => {
    expect(senderDisplayName(spiritMsg, conv)).toBe('精灵AI')
  })

  it('视角切到对方之后，精灵的名字仍然不变', () => {
    expect(senderDisplayName(spiritMsg, { ...conv, selfUid: PEER })).toBe('精灵AI')
  })

  it('认不出「我是谁」时精灵也不叫对方的备注名', () => {
    expect(senderDisplayName(spiritMsg, { ...conv, selfUid: '' })).toBe('精灵AI')
  })
})

describe('senderDisplayName · 群聊与合并转发', () => {
  const group = {
    conversationName: '示例群名',
    isGroup: true,
    embedded: false,
    selfUid: ME,
    users: { [ME]: { nickname: '本机账号昵称' } },
  }

  it('群聊里认不出的人用 uid 后缀，不能糊成群名', () => {
    // uid 末 6 位 = 123456
    const name = senderDisplayName({ sender_uid: 'uid-abcdef123456' }, group)
    expect(name).toBe('用户123456')
    expect(name).not.toBe('示例群名')
  })

  it('群聊里自己发的消息显示自己的昵称', () => {
    expect(senderDisplayName({ sender_uid: ME }, group)).toBe('本机账号昵称')
  })

  it('消息自带 sender_name 时优先用它', () => {
    const msg = { sender_uid: 'uid-x-9', sender_name: '落库里的名字' }
    expect(senderDisplayName(msg, group)).toBe('落库里的名字')
  })

  it('合并转发里 __self__ 不算名字', () => {
    const msg = { sender_uid: 'uid-y-8', sender_name: '__self__' }
    expect(senderDisplayName(msg, { ...group, embedded: true })).not.toBe('__self__')
  })
})

describe('selfPickerOptions · 「设置我」候选', () => {
  it('群聊里自己没有消息时，也要能选到自己', () => {
    const senders = [
      { sender_uid: 'uid-a', msg_count: 8 },
      { sender_uid: 'uid-b', msg_count: 3 },
    ]
    const options = selfPickerOptions(senders, ME, { name: '本机账号昵称' })
    expect(options.map(o => o.sender_uid)).toEqual(['uid-a', 'uid-b', ME])
    const mine = options[2]
    expect(mine.discovered).toBe(true)
    expect(mine.nickname).toBe('本机账号昵称')
    expect(mine.msg_count).toBe(0)
  })

  it('自己已经在发送者名单里时不重复添加', () => {
    const senders = [{ sender_uid: ME, msg_count: 5 }]
    const options = selfPickerOptions(senders, ME, { name: '本机账号昵称' })
    expect(options).toHaveLength(1)
    expect(options[0].discovered).toBeUndefined()
  })

  it('不知道「我是谁」时原样返回', () => {
    const senders = [{ sender_uid: 'uid-a', msg_count: 1 }]
    expect(selfPickerOptions(senders, '')).toHaveLength(1)
  })

  it('不修改传进来的数组', () => {
    const senders = [{ sender_uid: 'uid-a', msg_count: 1 }]
    selfPickerOptions(senders, ME, { name: '本机账号昵称' })
    expect(senders).toHaveLength(1)
  })
})

describe('pickerOptionLabel', () => {
  it('候选自带昵称优先（自动认出来的那条）', () => {
    expect(pickerOptionLabel({ sender_uid: ME, nickname: '本机账号昵称' }, { users: {} }))
      .toBe('本机账号昵称')
  })

  it('其次用 users 缓存里的昵称', () => {
    expect(pickerOptionLabel({ sender_uid: ME }, { users: { [ME]: { nickname: '缓存里的昵称' } } }))
      .toBe('缓存里的昵称')
  })

  it('都没有时退回 uid 后缀', () => {
    expect(pickerOptionLabel({ sender_uid: 'uid-abcdef123456' }, { users: {} }))
      .toBe('UID ...123456')
  })
})
