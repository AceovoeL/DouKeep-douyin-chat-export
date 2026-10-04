import { describe, it, expect } from 'vitest'
import { getInviteCard, isInviteCard, getMusicCard, isMusicCard, clearSystemCardCache } from './cardKinds.js'

// 消息行：content_json 双层编码在 raw_data 里，和数据库一致。
function msg(cj, fields = {}) {
  return {
    msg_id: 'srv_1', msg_type: 0, content: '', media_local_path: null, media_url: null,
    ref_msg: null, timestamp: 0, sender_uid: 'u1',
    raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }),
    ...fields,
  }
}

// 用户报的那条真实群邀请卡（已脱敏）。
const REAL_INVITE = {
  forbidden_actions: 29,
  title: '示例群名',
  sub_type: 0,
  desc: '某某 添加你进群',
  desc_spans: null,
  icon: {
    uri: 'tos-cn-i-7lppr0tkux/xxx',
    url_list: ['https://p3-aweme-im-img.byteimg.com/tos-cn-i-7lppr0tkux/xxx.webp'],
  },
  type_desc: '群聊邀请',
  open_url: '',
  event: {
    event_name: 'group_chat_click_invite',
    param: { 'a:invite_card_member_cnt': '6', conversation_id: '1234567890123456789', from_im_uid: '1234567890' },
  },
  aweme_invite_card: {
    group_icon: { url_list: ['https://p3-aweme-im-img.byteimg.com/tos-cn-i-7lppr0tkux/xxx.webp'], uri: 'xxx', height: 0, width: 0 },
    group_name: '示例群名',
    from_uid: 1234567890,
    conversation_id: '1234567890123456789',
    conversation_short_id: 1234567890123456789,
    group_owner_nickname: '某某',
    is_in: 1,
  },
  push_detail: '示例群名',
}

// 用户报的那条真实豆包卡（aweType=6001）。
const REAL_MUSIC = {
  source_title: '豆包',
  push_detail: '《音乐公开课》——《光荣啊，中国共青团》燃“五四”唱青春#五四中国青年的模样   ',
  aweType: 6001,
  sub_type: 1,
  client_key: 'aw730v27vx2bu2qw',
  title: '《音乐公开课》——《光荣啊，中国共青团》燃“五四”唱青春#五四中国青年的模样   ',
  icon: { url_list: ['http://p26-sign.douyinpic.com/large/tos-cn-i-dy/example-cover.jpeg'] },
  source_icon: { url_list: ['https://p3-sign.douyinpic.com/obj/douyin-open-platform/8b6963c8f51725560adb75f1494ab766'] },
  package_name: 'com.bot.doubao',
  open_url: 'https://v.douyin.com/-4Svyy3Lma8/?share_token=B5E2293F-CF8F-46CD-8890-19CDD2863F1E',
  desc: '在抖音，记录美好生活',
}

describe('群邀请卡（type_desc=群聊邀请）', () => {
  it('取群名、群头像、群会话 id 和「谁 添加你进群」', () => {
    const card = getInviteCard(msg(REAL_INVITE))
    expect(card).toEqual({
      groupName: '示例群名',
      description: '某某 添加你进群',
      icon: 'https://p3-aweme-im-img.byteimg.com/tos-cn-i-7lppr0tkux/xxx.webp',
      convId: '1234567890123456789',
      fromUid: '1234567890',
    })
    expect(isInviteCard(msg(REAL_INVITE))).toBe(true)
  })

  it('群名缺失时退回标题，标题也缺就写「群聊邀请」', () => {
    const noCard = { ...REAL_INVITE, aweme_invite_card: undefined }
    expect(getInviteCard(msg(noCard)).groupName).toBe('示例群名')
    expect(getInviteCard(msg(noCard)).convId).toBe('1234567890123456789')
    expect(getInviteCard(msg({ type_desc: '群聊邀请', title: '', desc: 'A 添加你进群',
      event: { param: { conversation_id: '7' } } })).groupName).toBe('群聊邀请')
  })

  it('描述缺失时兜底「邀请你加入群聊」', () => {
    const cj = { ...REAL_INVITE, desc: '' }
    expect(getInviteCard(msg(cj)).description).toBe('邀请你加入群聊')
  })

  it('没有目标群会话 id 就不认这张卡（认了也没法跳转）', () => {
    const cj = { ...REAL_INVITE, event: undefined, aweme_invite_card: undefined }
    expect(getInviteCard(msg(cj))).toBeNull()
  })

  it('别的卡片不会被当成邀请卡', () => {
    expect(isInviteCard(msg(REAL_MUSIC))).toBe(false)
    expect(isInviteCard(msg({ aweType: 800, itemId: '42' }))).toBe(false)
    expect(isInviteCard(msg({ type_desc: '群公告', notice_content: 'x' }))).toBe(false)
  })
})

describe('豆包分享卡（aweType=6001）', () => {
  it('取标题、封面、来源和跳转地址', () => {
    const card = getMusicCard(msg(REAL_MUSIC))
    // 卡片两端都 trim 过：抖音在标题尾上留的空格不必带进界面。
    expect(card.title).toBe('《音乐公开课》——《光荣啊，中国共青团》燃“五四”唱青春#五四中国青年的模样')
    expect(card.cover).toBe('http://p26-sign.douyinpic.com/large/tos-cn-i-dy/example-cover.jpeg')
    expect(card.source).toBe('豆包')
    expect(card.url).toBe('https://v.douyin.com/-4Svyy3Lma8/?share_token=B5E2293F-CF8F-46CD-8890-19CDD2863F1E')
    expect(isMusicCard(msg(REAL_MUSIC))).toBe(true)
  })

  it('来源缺失时退回 desc，标题/来源/封面全缺才不认', () => {
    const cj = { ...REAL_MUSIC, source_title: '', desc: '在抖音，记录美好生活' }
    expect(getMusicCard(msg(cj)).source).toBe('在抖音，记录美好生活')
    expect(getMusicCard(msg({ aweType: 6001 }))).toBeNull()
    expect(getMusicCard(msg({ aweType: 6001, icon: { url_list: ['https://x/y.jpeg'] } })).cover)
      .toBe('https://x/y.jpeg')
  })

  it('别的卡片不会被当成豆包卡', () => {
    expect(isMusicCard(msg(REAL_INVITE))).toBe(false)
    expect(isMusicCard(msg({ aweType: 805, itemId: '9' }))).toBe(false)
  })
})

describe('缓存', () => {
  it('同一行重复问只解析一次，清缓存后重新解析', () => {
    const row = msg(REAL_MUSIC)
    const first = getMusicCard(row)
    expect(getMusicCard(row)).toBe(first)   // 同一个对象 = 走缓存
    clearSystemCardCache()
    expect(getMusicCard(row)).not.toBe(first)
    expect(getMusicCard(row)).toEqual(first)
  })
})
