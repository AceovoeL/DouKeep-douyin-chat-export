import { describe, it, expect } from 'vitest'
import {
  clearEasterRedpackCardCache,
  getEasterRedpackCard,
  isEasterRedpackCard,
} from './easterRedpackCard.js'

// 结构取自真实样本（aweType=110360 / card_key=msg_easter_redpack /
// card_type=pay_msg_easter_redpack_card）：content_json 里嵌 im_dynamic_patch，
// raw_data 是再次编码的排版 JSON。图片地址已换成一眼可辨的假值。
const COVER = 'https://p26-infras.byteimg.com/static-assets/demo-cover.png'

function cardMsg({ description = '[彩蛋福袋]', button = '拆福袋', cover = COVER, cardKey = 'msg_easter_redpack', cardType = 'pay_msg_easter_redpack_card', aweType = 110360 } = {}) {
  const layout = {
    top: { cover_image: cover, type: 'im-image' },
    bottom: { content: '彩蛋福袋', type: 'im-text' },
    content_content: { content: button, type: 'im-button' },
  }
  return {
    msg_id: 'srv_7000000000000000002',
    msg_type: 0,
    content: '[彩蛋福袋]',
    raw_data: JSON.stringify({
      content_json: JSON.stringify({
        aweType,
        description,
        im_dynamic_patch: {
          card_key: cardKey,
          card_type: cardType,
          raw_data: JSON.stringify(layout),
        },
      }),
    }),
  }
}

describe('彩蛋福袋卡片', () => {
  it('认出卡片并取出上半部分那张插图', () => {
    clearEasterRedpackCardCache()
    const card = getEasterRedpackCard(cardMsg())
    expect(card.cover).toBe(COVER)
    expect(isEasterRedpackCard(cardMsg())).toBe(true)
  })

  it('状态固定写「已过期」，名字固定写「彩蛋福袋」', () => {
    clearEasterRedpackCardCache()
    // 抖音官方这张卡中部写的是「已领取」，底部按钮写「拆福袋」——这里都不照抄。
    const card = getEasterRedpackCard(cardMsg({ button: '已领取' }))
    expect(card.status).toBe('已过期')
    expect(card.title).toBe('彩蛋福袋')
    expect(card.preview).toBe('[彩蛋福袋]')

    // 另一条真实样本：「您有一个彩蛋福袋待领取」，状态同样改成「已过期」。
    const pending = getEasterRedpackCard(cardMsg({ description: '您有一个彩蛋福袋待领取', button: '拆福袋' }))
    expect(pending.status).toBe('已过期')
    expect(pending.title).toBe('彩蛋福袋')
  })

  it('缺 card_key / card_type 时仍按 aweType 认出来', () => {
    clearEasterRedpackCardCache()
    const card = getEasterRedpackCard(cardMsg({ cardKey: '', cardType: '' }))
    expect(card).not.toBeNull()
    expect(card.cover).toBe(COVER)
  })

  it('缺插图也照常画卡（只有底图和文字）', () => {
    clearEasterRedpackCardCache()
    const card = getEasterRedpackCard(cardMsg({ cover: '' }))
    expect(card).not.toBeNull()
    expect(card.cover).toBe('')
    expect(card.status).toBe('已过期')
  })

  it('别的卡片不会被误认', () => {
    clearEasterRedpackCardCache()
    // 商品卡的排版也是 top + content_top + content_content，但 aweType/card_key 不同。
    const goods = cardMsg({ cardKey: 'msg_big_product', cardType: '', aweType: 11029 })
    expect(getEasterRedpackCard(goods)).toBeNull()
    expect(isEasterRedpackCard(goods)).toBe(false)
    // 坏掉的排版不炸，卡片仍能画（没有插图）
    const broken = cardMsg()
    const payload = JSON.parse(broken.raw_data)
    const cj = JSON.parse(payload.content_json)
    cj.im_dynamic_patch.raw_data = '{不是 JSON'
    payload.content_json = JSON.stringify(cj)
    broken.raw_data = JSON.stringify(payload)
    expect(getEasterRedpackCard(broken).cover).toBe('')
  })
})
