import { describe, it, expect } from 'vitest'
import { getFlameGiftCard, isFlameGiftCard, pickFlameAvatar, clearFlameCardCache } from './flameCard.js'
import { getShareInfo } from './douyinMessage.js'
import { sharePreview } from './sharePreview.js'

const OWNER = '1000000000000001'
const PEER = '1000000000000002'
const PEER_AVATAR = 'https://example.com/avatar/peer.webp'
const SELF_AVATAR = 'https://example.com/avatar/self.webp'

// 卡片结构取自真实样本：content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
// content_top 是组件列表（空文字 + 火花图标 + 天数滚轮 + 文案），
// 这正是以前被当成标题、渲染成 "[object Object],[object Object],…" 的原因。
// 样本里的账号 ID、消息 ID 和头像地址都属于个人信息，已换成一眼可辨的假值。
function flameMsg({ convId = `0:1:${OWNER}:${PEER}`, content = '获得火花见面礼', extra = {} } = {}) {
  const layout = {
    top: {
      content: {
        src_polyfill_with_crop: [
          { content: PEER_AVATAR, id: 'image_0' },
          { content: SELF_AVATAR, id: 'image_2' },
        ],
      },
    },
    content_top: {
      type: 'im-component-list',
      content: [
        { type: 'im-text', content: '' },
        { type: 'im-image', content: 'https://x/flame_icon.png' },
        { type: 'im-number-roller', content: { start: 2, end: 3 } },
        { type: 'im-text', content: ' 获得火花见面礼' },
      ],
    },
    bottom_right: { type: 'im-text', content: '立即领取' },
    whole_card: {
      action_info: [{ action: 'onShow', params: { extra_params: { gift_day: '3' } } }],
    },
    ...extra,
  }
  return {
    msg_id: 'srv_7000000000000000002',
    msg_type: 0,
    content,
    conv_id: convId,
    sender_uid: PEER,
    raw_data: JSON.stringify({
      content_json: JSON.stringify({
        aweType: 110408,
        im_dynamic_patch: {
          card_key: 'msg_daily',
          card_type: 'im_msg_daily_flame_gift',
          raw_data: JSON.stringify(layout),
        },
      }),
    }),
  }
}

describe('火花见面礼卡片', () => {
  it('认出卡片并读出天数、文案、按钮', () => {
    clearFlameCardCache()
    const card = getFlameGiftCard(flameMsg())
    expect(isFlameGiftCard(flameMsg())).toBe(true)
    expect(card.days).toBe('3')
    expect(card.title).toBe('获得火花见面礼')
    expect(card.button).toBe('立即领取')
  })

  it('头像归属按 conv_id 认：第 2 段是本机账号（image_2），第 3 段是对方（image_0）', () => {
    clearFlameCardCache()
    const card = getFlameGiftCard(flameMsg())
    expect(card.selfAvatar).toBe(SELF_AVATAR)
    expect(card.peerAvatar).toBe(PEER_AVATAR)
  })

  it('选了「我是谁」且落在第 3 段时两人对调', () => {
    clearFlameCardCache()
    const card = getFlameGiftCard(flameMsg({ convId: `0:1:${PEER}:${OWNER}` }), OWNER)
    expect(card.selfAvatar).toBe(PEER_AVATAR)
    expect(card.peerAvatar).toBe(SELF_AVATAR)
  })

  it('天数缺失时退回数字滚轮的下标，按钮缺失时有默认文字', () => {
    clearFlameCardCache()
    const card = getFlameGiftCard(flameMsg({
      extra: { whole_card: { action_info: [{ params: { extra_params: {} } }] }, bottom_right: {} },
    }))
    expect(card.days).toBe('3')
    expect(card.button).toBe('立即领取')
  })

  it('不是火花卡的分享卡片返回 null', () => {
    clearFlameCardCache()
    const other = flameMsg()
    const parsed = JSON.parse(other.raw_data)
    const cj = JSON.parse(parsed.content_json)
    cj.aweType = 800
    cj.im_dynamic_patch.card_type = 'msg_big_product'
    cj.im_dynamic_patch.card_key = 'msg_big_product'
    other.raw_data = JSON.stringify({ content_json: JSON.stringify(cj) })
    expect(getFlameGiftCard(other)).toBeNull()
  })

  // 对方近期换过头像：查看器用的是 users 表里的最新头像，卡片里存的是发消息那天的快照。
  it('头像优先用查看器同款的最新头像，没有资料时才退回卡片里那张', () => {
    clearFlameCardCache()
    const card = getFlameGiftCard(flameMsg())
    const latest = 'https://example.com/avatar/new.webp'
    expect(pickFlameAvatar(card, 'peer', latest)).toBe(latest)
    expect(pickFlameAvatar(card, 'peer', '')).toBe(PEER_AVATAR)
    expect(pickFlameAvatar(card, 'self', '')).toBe(SELF_AVATAR)
    expect(pickFlameAvatar(null, 'peer', latest)).toBe('')
  })

  it('标题不会再变成 [object Object]，搜索预览也读得懂', () => {
    clearFlameCardCache()
    const msg = flameMsg()
    expect(getShareInfo(msg).title).toBe('获得火花见面礼')
    expect(sharePreview(msg)).toBe('[分享] 获得火花见面礼')
  })

})
