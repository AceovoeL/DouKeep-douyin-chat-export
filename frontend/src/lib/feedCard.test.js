// 「喂精灵」卡（card_key=msg_fire_invitation 的 feeding 一种）的解析。
//
// 结构取自真实样本：content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版
// JSON；封面由两张图叠出来（background_image 打底 + top.content 那根辣条居中盖上）。
import { describe, expect, it } from 'vitest'

import { getFeedCard, isFeedCard, clearFeedCardCache } from './feedCard.js'

const BACKGROUND = 'https://lf3-creative.dailygn.com/obj/dino/pet_elf/images/feed_card/like2.png'
const FOOD = 'https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_icon.png'

// 真实载荷的骨架：卡片的文字在 raw_data 里，两张图分别在 whole_card 和 top。
function layout(overrides = {}) {
  return {
    content_top: { content: '邀请你一起喂精灵', type: 'im-text' },
    content_content: { content: '精灵还在慢慢品味中~', type: 'im-text' },
    bottom: { content: '去喂食', type: 'im-button' },
    top: { content: FOOD, type: 'im-image' },
    whole_card: {
      ui_info: {
        'background_image@light': BACKGROUND,
        'background_image@dark': 'https://lf3-creative.dailygn.com/obj/dino/pet_elf/images/feed_card/dark.png',
      },
    },
    ...overrides,
  }
}

function cardMsg(rawLayout, { cardType = 'im_msg_fire_invitation_feeding', cardKey = 'msg_fire_invitation' } = {}) {
  const contentJson = {
    description: '邀请你一起喂精灵',
    aweType: 110274,
    im_dynamic_patch: {
      card_key: cardKey,
      card_type: cardType,
      raw_data: rawLayout ? JSON.stringify(rawLayout) : '',
    },
  }
  return {
    msg_id: 'm1', msg_type: 0, content: '', sender_uid: 'u1',
    raw_data: JSON.stringify({ content_json: JSON.stringify(contentJson) }),
  }
}

// 老版版式（2026 年 7 月那批真实样本）：载荷里**没有**背景图 —— whole_card 里只有一段
// 日志信息，ui_info 是空的。抖音客户端那张卡是浅色渐变底 + 精灵图的另一种外观
// （截图见 assets/invite-feed.png），所以卡片要改用那套版式，并且照抄载荷里的文字。
const OLD_SPIRIT = 'https://lf6-creative.dailygn.com/obj/dino/pet_elf/images/food/hotstick_card.png'

function oldLayout(overrides = {}) {
  return {
    content_top: { content: '你还没有喂精灵~', type: 'im-text' },
    content_content: { content: '一起喂解锁彩蛋效果', type: 'im-text' },
    bottom: { content: '去喂食', type: 'im-button' },
    top: { content: OLD_SPIRIT, type: 'im-image' },
    whole_card: { extra_info: { log_info: { card_type: 'im_msg_fire_invitation_feeding' } } },
    ...overrides,
  }
}

describe('getFeedCard 老版版式（载荷没有背景图）', () => {
  it('认出老版卡：没有背景图，精灵图在 top.content', () => {
    const card = getFeedCard(cardMsg(oldLayout()))
    expect(card).toMatchObject({
      kind: 'feed',
      background: '',
      food: '',
      spirit: OLD_SPIRIT,
    })
  })

  it('标题 / 副标题照抄载荷里的原文', () => {
    const card = getFeedCard(cardMsg(oldLayout()))
    expect(card.title).toBe('你还没有喂精灵~')
    expect(card.subtitle).toBe('一起喂解锁彩蛋效果')
    expect(card.button).toBe('去喂食')
  })

  it('载荷缺标题 / 副标题时用那两句兜底，不是空字符串', () => {
    const card = getFeedCard(cardMsg(oldLayout({
      content_top: undefined,
      content_content: undefined,
    })))
    expect(card.title).toBe('你还没有喂精灵~')
    expect(card.subtitle).toBe('一起喂解锁彩蛋效果')
  })

  it('老版也给预览（搜索用），用卡片自己那句话', () => {
    expect(getFeedCard(cardMsg(oldLayout())).preview).toBe('一起喂解锁彩蛋效果')
  })

  it('老版没有背景图，但仍旧是喂食卡（不是商品卡 / 分享卡）', () => {
    expect(isFeedCard(cardMsg(oldLayout()))).toBe(true)
    // 别的卡片不会被认成喂食卡
    expect(isFeedCard(cardMsg(oldLayout(), { cardType: 'im_msg_fire_invitation_joint_send' }))).toBe(false)
  })
})

describe('getFeedCard', () => {
  it('认出喂食卡并读出封面两张图', () => {
    const card = getFeedCard(cardMsg(layout()))
    expect(card).toMatchObject({
      kind: 'feed',
      background: BACKGROUND,
      food: FOOD,
    })
    // 新版走「背景 + 食物」两图层，不用老版那张精灵图
    expect(card.spirit).toBe('')
  })

  it('卡片上的两行字固定不变，不跟着载荷里的文案走', () => {
    // 实测载荷里这句话是「精灵还在慢慢品味中~」，但卡片上要显示参考图那两句。
    const card = getFeedCard(cardMsg(layout()))
    expect(card.title).toBe('邀请你一起喂精灵')
    expect(card.subtitle).toBe('精灵爱吃这个，你也来喂')
    expect(card.button).toBe('去喂食')
  })

  it('按钮只有「去喂食」一种文案，没有状态之分', () => {
    // 喂食卡不等人接受（接受后会另外发一条消息），所以载荷里写别的也不改按钮。
    const card = getFeedCard(cardMsg(layout({ bottom: { content: '喂一下' } })))
    expect(card.button).toBe('去喂食')
  })

  it('缺背景图时用 @dark 那张顶上', () => {
    const light = layout()
    delete light.whole_card.ui_info['background_image@light']
    expect(getFeedCard(cardMsg(light)).background).toContain('dark.png')
  })

  it('预览用卡片自己那句话，没有就用固定标题', () => {
    expect(getFeedCard(cardMsg(layout())).preview).toBe('精灵还在慢慢品味中~')
    const blank = layout({ content_content: undefined })
    expect(getFeedCard(cardMsg(blank)).preview).toBe('邀请你一起喂精灵')
  })

  it('缺图不报错，能读到的照给', () => {
    const bare = layout({ top: undefined, whole_card: undefined })
    const card = getFeedCard(cardMsg(bare))
    expect(card).toMatchObject({ background: '', food: '', spirit: '' })
    // 图都没有也仍然是这张卡：没有背景图 = 老版版式，文字用载荷原文
    expect(card.title).toBe('邀请你一起喂精灵')
  })

  it('别的卡片不会被认成喂食卡', () => {
    // 同一个 card_key，但 card_type 是合养小火人那一族
    expect(isFeedCard(cardMsg(layout(), { cardType: 'im_msg_fire_invitation_joint_send' }))).toBe(false)
    // card_type 对但 card_key 不对（别的分享卡）
    expect(isFeedCard(cardMsg(layout(), { cardKey: 'video_share' }))).toBe(false)
  })

  it('排版坏掉时返回 null，不抛异常', () => {
    const broken = {
      msg_id: 'm2', msg_type: 0, content: '', sender_uid: 'u1',
      raw_data: JSON.stringify({ content_json: JSON.stringify({
        im_dynamic_patch: { card_key: 'msg_fire_invitation',
                            card_type: 'im_msg_fire_invitation_feeding',
                            raw_data: '{不是 JSON' } }) }),
    }
    expect(getFeedCard(broken)).toBeNull()
  })

  it('普通消息、空值都不算喂食卡', () => {
    expect(isFeedCard(null)).toBe(false)
    expect(isFeedCard({})).toBe(false)
    expect(isFeedCard({ raw_data: JSON.stringify({ content_json: JSON.stringify({ aweType: 0 }) }) })).toBe(false)
  })

  it('同一条消息重复问只解析一次', () => {
    const msg = cardMsg(layout())
    const first = getFeedCard(msg)
    expect(getFeedCard(msg)).toBe(first)
    // 清缓存后重新解析出的是新对象
    clearFeedCardCache()
    expect(getFeedCard(msg)).not.toBe(first)
  })
})
