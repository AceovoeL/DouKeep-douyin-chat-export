import { describe, it, expect } from 'vitest'
import { getCallShopCard, isCallShopCard, clearCallShopCardCache } from './callShopCard.js'

const ICON = 'http://p3-life-governance-serving.byteimg.com/tos-cn-i-ri7wgflg39/guide_bullet_call.png'

// 卡片结构取自真实样本：content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
// 排版里 content_left 是电话图标、content_middle_top/bottom 是两行文案、
// content_right 是「联系」按钮（带拨号 schema）。
// 样本里的门店电话、消息 ID、会话 ID 都属于个人信息，已换成一眼可辨的假值。
function callShopMsg({ aweType = 110284, cardType = 'life_bar_link_private_msg_guide', layout = true } = {}) {
  const raw = {
    content_left: { content: ICON, type: 'im-image', ui_info: { border_radius: 21 } },
    content_middle_top: { content: '联系门店', type: 'im-text' },
    content_middle_bottom: { content: '回复较慢，可拨打电话', type: 'im-text' },
    content_right: {
      action_info: { action: 'tap', event: 'openSchema', params: { schema: 'aweme://lynxview_popup/?merchant_phone_number=10000000000' } },
      content: '联系',
      type: 'im-button',
    },
    whole_card: { action_info: [{ action: 'tap', event: 'openSchema', params: { schema: 'aweme://lynxview_popup/' } }] },
  }
  const contentJson = {
    aweType,
    description: '回复较慢，建议拨打门店电话',
    is_system_type: true,
    push_detail: '回复较慢，建议拨打门店电话',
    ui_info: { location_type: 1 },
    im_dynamic_patch: {
      card_key: 'bar_link',
      card_type: cardType,
      server_card_type: cardType,
      scene_type: 'life',
      raw_data: layout ? JSON.stringify(raw) : '',
    },
  }
  return {
    msg_id: 'srv_7000000000000000001',
    msg_type: 0,
    content: '回复较慢，建议拨打门店电话',
    conv_id: '0:1:1000000000000001:1000000000000002',
    sender_uid: '1000000000000002',
    raw_data: JSON.stringify({ content_json: JSON.stringify(contentJson) }),
  }
}

describe('联系门店引导卡片', () => {
  it('认出卡片并读出按钮、标题与副标题', () => {
    clearCallShopCardCache()
    expect(isCallShopCard(callShopMsg())).toBe(true)
    const card = getCallShopCard(callShopMsg())
    expect(card.button).toBe('联系')
    expect(card.title).toBe('联系门店')
    expect(card.subtitle).toBe('回复较慢，可拨打电话')
    expect(card.icon).toBe(ICON)
  })

  it('card_type 命中就认，不看 aweType', () => {
    clearCallShopCardCache()
    const card = getCallShopCard(callShopMsg({ aweType: 0 }))
    expect(card.button).toBe('联系')
  })

  it('aweType 命中就认，不看 card_type', () => {
    clearCallShopCardCache()
    const card = getCallShopCard(callShopMsg({ cardType: 'bar_link' }))
    expect(card.title).toBe('联系门店')
  })

  it('排版缺失时仍给默认按钮文字，卡片不会空白', () => {
    clearCallShopCardCache()
    const card = getCallShopCard(callShopMsg({ layout: false }))
    expect(card.button).toBe('联系')
    expect(card.title).toBe('联系门店')
    expect(card.subtitle).toBe('')
  })

  it('别的分享卡片返回 null（商品卡 / 视频卡 / 火花卡）', () => {
    clearCallShopCardCache()
    expect(getCallShopCard(null)).toBeNull()
    const goods = callShopMsg({ aweType: 11029, cardType: 'msg_big_product' })
    expect(isCallShopCard(goods)).toBe(false)
    const video = callShopMsg({ aweType: 800, cardType: 'video_share' })
    expect(isCallShopCard(video)).toBe(false)
    const flame = callShopMsg({ aweType: 110408, cardType: 'im_msg_daily_flame_gift' })
    expect(isCallShopCard(flame)).toBe(false)
  })

  it('content 不是字符串的排版节点不会被当成文字', () => {
    clearCallShopCardCache()
    const msg = callShopMsg()
    const parsed = JSON.parse(msg.raw_data)
    const cj = JSON.parse(parsed.content_json)
    const layout = JSON.parse(cj.im_dynamic_patch.raw_data)
    layout.content_right = { type: 'im-component-list', content: [{ type: 'im-text', content: '联系' }] }
    cj.im_dynamic_patch.raw_data = JSON.stringify(layout)
    msg.raw_data = JSON.stringify({ content_json: JSON.stringify(cj) })
    expect(getCallShopCard(msg).button).toBe('联系')
  })
})
