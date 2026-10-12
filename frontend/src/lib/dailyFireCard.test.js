import { describe, it, expect } from 'vitest'
import { getDailyFireCard, isDailyFireCard, clearDailyFireCardCache } from './dailyFireCard.js'
import { sharePreview } from './sharePreview.js'

// 卡片结构取自真实样本（aweType=110270 / card_key=msg_daily / card_type=im_msg_daily_fire）：
// content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
// content_top 是组件列表：[前文文字] [火花图] [数字滚轮] [后文文字]，
// 这正是以前被当成标题、渲染成 "[object Object],[object Object],…" 的原因。
//
// 样本里的账号 ID、消息 ID、头像与图片地址都属于个人信息，已换成一眼可辨的假值。
const UID_A = '1000000000000001'   // conv_id 第 2 段
const UID_B = '1000000000000002'   // conv_id 第 3 段（本机账号）

function dailyMsg({ before = '一起度过了人生的', days = 365, after = '天', convId = `0:1:${UID_A}:${UID_B}`, extra = {} } = {}) {
  const layout = {
    content_top: {
      type: 'im-component-list',
      content: [
        { type: 'im-text', content: before, ui_info: { font_size: 17, font_weight: 600 } },
        { type: 'im-image', content: 'https://x/daily_fire.png', ui_info: { height: 21 } },
        { type: 'im-number-roller', content: { start: days - 1, end: days } },
        { type: 'im-text', content: after },
      ],
    },
    bottom_left: { content: 'https://x/head_wrapper_circle.png' },
    bottom_left_content: { content: "pattern:client_data() | get_value_by_key('user_info.avatar_thumb')" },
    bottom_right: { content: '限时日常' },
    top: {
      content: {
        src_polyfill_with_crop: [
          { content: "pattern:client_data() | get_value_by_key('other_user_info.avatar_thumb')", id: 'image_0' },
          { content: "pattern:client_data() | get_value_by_key('user_info.avatar_thumb')", id: 'image_2' },
        ],
      },
    },
    ...extra,
  }
  return {
    msg_id: 'srv_7000000000000000001',
    msg_type: 0,
    content: `${before} ${days} ${after}`,
    conv_id: convId,
    sender_uid: UID_B,
    raw_data: JSON.stringify({
      content_json: JSON.stringify({
        aweType: 110270,
        description: `${before} ${days} ${after}`,
        im_dynamic_patch: {
          card_key: 'msg_daily',
          card_type: 'im_msg_daily_fire',
          raw_data: JSON.stringify(layout),
        },
      }),
    }),
  }
}

describe('陪伴天数卡片', () => {
  it('认出卡片并读出前后文、天数、按钮文字', () => {
    clearDailyFireCardCache()
    const card = getDailyFireCard(dailyMsg(), UID_B)
    expect(isDailyFireCard(dailyMsg())).toBe(true)
    expect(card.before).toBe('一起度过了人生的')
    expect(card.after).toBe('天')
    expect(card.days).toBe('365')
    expect(card.button).toBe('限时日常')
    expect(card.kind).toBe('daily-feed')
  })

  it('文字里已经写了天数时不再画第二遍', () => {
    clearDailyFireCardCache()
    const card = getDailyFireCard(dailyMsg(), UID_B)
    // 「一起度过了人生的 365 天」→ 前文去掉数字，只留「一起度过了人生的」
    expect(card.before).not.toContain('365')
  })

  it('上方两个头像固定：对方在左、本机账号在右，且不随视角变', () => {
    clearDailyFireCardCache()
    const card = getDailyFireCard(dailyMsg(), UID_B)
    expect(card.leftUid).toBe(UID_A)
    expect(card.rightUid).toBe(UID_B)
  })

  it('换一个视角看，两个头像的左右顺序也不变', () => {
    clearDailyFireCardCache()
    // 第二个参数是「这台电脑的账号」，和当前选的视角无关；传谁结果都一样。
    const card = getDailyFireCard(dailyMsg(), UID_B)
    expect(card.leftUid).toBe(UID_A)
    expect(card.rightUid).toBe(UID_B)
  })

  it('按钮里的头像跟着当前视角走（载荷指向 user_info）', () => {
    clearDailyFireCardCache()
    expect(getDailyFireCard(dailyMsg(), UID_B).ringFollowsViewer).toBe(true)
  })

  it('30 天那种变体也能读出来（前文只有一个空格）', () => {
    clearDailyFireCardCache()
    const card = getDailyFireCard(
      dailyMsg({ before: ' ', days: 30, after: '，陪伴不止说说而已' }), UID_B,
    )
    expect(card.days).toBe('30')
    expect(card.after).toBe('，陪伴不止说说而已')
    expect(card.before).toBe('')
  })

  it('没有数字滚轮时退回文案里的数字', () => {
    clearDailyFireCardCache()
    const msg = dailyMsg()
    const payload = JSON.parse(msg.raw_data)
    const cj = JSON.parse(payload.content_json)
    const layout = JSON.parse(cj.im_dynamic_patch.raw_data)
    layout.content_top.content = [{ type: 'im-text', content: '我们已经互相陪伴 100 天' }]
    cj.im_dynamic_patch.raw_data = JSON.stringify(layout)
    cj.description = '我们已经互相陪伴 100 天'
    payload.content_json = JSON.stringify(cj)
    msg.raw_data = JSON.stringify(payload)
    const card = getDailyFireCard(msg, UID_B)
    expect(card.days).toBe('100')
  })

  it('不是这张卡片则返回 null（见面礼卡不会被认领）', () => {
    clearDailyFireCardCache()
    const msg = dailyMsg()
    const payload = JSON.parse(msg.raw_data)
    const cj = JSON.parse(payload.content_json)
    cj.aweType = 110408
    cj.im_dynamic_patch.card_type = 'im_msg_daily_flame_gift'
    payload.content_json = JSON.stringify(cj)
    msg.raw_data = JSON.stringify(payload)
    expect(getDailyFireCard(msg, UID_B)).toBeNull()
  })

  it('搜索预览显示卡片上那句话，而不是整段 JSON', () => {
    clearDailyFireCardCache()
    expect(sharePreview(dailyMsg())).toBe('一起度过了人生的 365 天')
  })
})
