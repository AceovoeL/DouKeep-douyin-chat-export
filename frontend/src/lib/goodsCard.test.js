import { describe, it, expect } from 'vitest'
import { isGoodsCard, getGoodsCard, getGoodsLayout, clearGoodsCardCache } from './goodsCard.js'

let _id = 0
function msg(fields = {}) {
  return { msg_id: `g${_id++}`, msg_type: 4, content: '', media_local_path: null,
           media_url: null, raw_data: null, ref_msg: null, timestamp: 0, sender_uid: 'u', ...fields }
}
// 商品卡真实结构：content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
function goodsMsg({ layout, cardKey = 'msg_big_product', cardType = 'commodity_msg_big_product', extra = {}, fields = {} }) {
  return msg({
    raw_data: JSON.stringify({ content_json: JSON.stringify({
      aweType: 11029,
      im_dynamic_patch: { card_key: cardKey, card_type: cardType, raw_data: JSON.stringify(layout) },
      ...extra,
    }) }),
    ...fields,
  })
}
const text = content => ({ type: 'im-text', content })
const textList = (...items) => ({ type: 'im-text-list', content: items.map((t, i) => ({ id: String(i + 1), text: t })) })
const image = url => ({ type: 'im-image', content: url })

// 带 meta_params.real_price（单位：分）的跳转 schema
const schema = (realPrice, commodityId = '123') => 'sslocal://ec_goods_detail/?commodity_id=' + commodityId +
  '&meta_params=' + encodeURIComponent(JSON.stringify({ entrance_info: { real_price: realPrice } }))
const wholeCard = (url) => ({ whole_card: { action_info: [{ action: 'tap', params: { schema: url } }] } })

describe('isGoodsCard', () => {
  it('matches the real msg_big_product card', () => {
    const m = goodsMsg({ layout: { content_top: text('尺子'), content_content: textList('¥9', '券后价'), top: image('http://img') } })
    expect(isGoodsCard(m)).toBe(true)
  })
  it('matches by card_key even without the full layout', () => {
    const m = msg({ raw_data: JSON.stringify({ content_json: JSON.stringify({
      aweType: 11029, im_dynamic_patch: { card_key: 'msg_big_product', raw_data: '{}' } }) }) })
    expect(isGoodsCard(m)).toBe(true)
  })
  it('ignores the video-product card (11063 / msg_video, different layout)', () => {
    const m = goodsMsg({
      cardKey: 'msg_video', cardType: 'commodity_msg_video_product',
      layout: { top_bottom_top: text('视频商品'), content: text('¥12.9 券后价') },
    })
    expect(isGoodsCard(m)).toBe(false)
    expect(getGoodsCard(m)).toBeNull()
  })
  it('ignores video and comment shares', () => {
    expect(isGoodsCard(msg({ content: '[分享视频] 标题' }))).toBe(false)
    expect(isGoodsCard(msg({ raw_data: JSON.stringify({ content_json: JSON.stringify({ aweType: 11054, content_title: 'T' }) }) }))).toBe(false)
  })
  it('accepts a goods card whose JSON only lives in content', () => {
    const layout = { content_top: text('商品名'), content_content: textList('¥1.2'), top: image('http://i') }
    const m = msg({ content: JSON.stringify({ aweType: 11029, im_dynamic_patch: { card_key: 'msg_big_product', raw_data: JSON.stringify(layout) } }) })
    expect(isGoodsCard(m)).toBe(true)
  })
  it('tolerates a malformed layout', () => {
    const m = msg({ raw_data: JSON.stringify({ content_json: JSON.stringify({
      im_dynamic_patch: { card_key: 'msg_big_product', raw_data: '{truncated' } }) }) })
    expect(getGoodsLayout(m)).toBeNull()
    expect(getGoodsCard(m)).toBeNull()
  })
})

describe('getGoodsCard', () => {
  const fullLayout = {
    content_top: text('示例商品标题'),
    content_content: textList('¥9', '券后价'),
    content_bottom_right: textList('券', '立减6'),
    top: image('http://img/cover.jpeg'),
    ...wholeCard('sslocal://ec_goods_detail/?commodity_id=100000000000000048'),
  }

  it('reads title, cover and the coupon price', () => {
    const card = getGoodsCard(goodsMsg({ layout: fullLayout }))
    expect(card.title).toBe('示例商品标题')
    expect(card.cover).toBe('http://img/cover.jpeg')
    expect(card.couponPrice).toBe('9')
    expect(card.originalPrice).toBe('15')  // 券后价 9 + 立减 6
  })

  it('uses meta_params.real_price (cents) as the original price when present', () => {
    const layout = { ...fullLayout, ...wholeCard(schema(1500)) }
    const card = getGoodsCard(goodsMsg({ layout }))
    expect(card.couponPrice).toBe('9')
    expect(card.originalPrice).toBe('15')
  })

  it('derives the original price from a percentage discount', () => {
    const layout = { ...fullLayout, content_bottom_right: textList('券', '8.5折') }
    const card = getGoodsCard(goodsMsg({ layout }))
    expect(card.originalPrice).toBe('10.59')  // 9 ÷ 0.85
  })

  it('keeps sold-count text out of the prices', () => {
    const layout = { ...fullLayout, content_content: textList('¥8.27', '券后价', '已售3956件') }
    const card = getGoodsCard(goodsMsg({ layout }))
    expect(card.couponPrice).toBe('8.27')
    expect(card.originalPrice).toBe('14.27')
  })

  it('falls back to the first price when there is no 券后价 label', () => {
    const layout = { ...fullLayout, content_content: textList('¥6.9'), content_bottom_right: undefined }
    const card = getGoodsCard(goodsMsg({ layout }))
    expect(card.couponPrice).toBe('6.9')
    expect(card.originalPrice).toBe('')
  })

  it('leaves the original price empty when it cannot be derived', () => {
    const layout = { ...fullLayout, content_bottom_right: textList('券', '满5减2') }
    expect(getGoodsCard(goodsMsg({ layout })).originalPrice).toBe('')
  })

  it('never shows an original price below the coupon price', () => {
    const layout = { ...fullLayout, ...wholeCard(schema(500)) }
    expect(getGoodsCard(goodsMsg({ layout })).originalPrice).toBe('15')
  })

  it('falls back to the content title/cover when the layout omits them', () => {
    const layout = { ...fullLayout, content_top: undefined, top: undefined }
    const m = goodsMsg({ layout, extra: { cover_url: { url_list: ['http://cj/cover'] } }, fields: { content: '分享[商品]: 备用标题' } })
    const card = getGoodsCard(m)
    expect(card.title).toBe('备用标题')
    expect(card.cover).toBe('http://cj/cover')
  })

  it('accepts an already-parsed raw_data object', () => {
    const m = msg({ raw_data: JSON.stringify({ content_json: JSON.stringify({
      im_dynamic_patch: { card_key: 'msg_big_product', raw_data: fullLayout } }) }) })
    expect(getGoodsCard(m).title).toContain('示例商品标题')
  })

  it('caches the parsed layout and card per message row, and can be cleared', () => {
    const m = goodsMsg({ layout: fullLayout })
    const layout = getGoodsLayout(m)
    expect(getGoodsLayout(m)).toBe(layout)
    const card = getGoodsCard(m)
    expect(getGoodsCard(m)).toBe(card)
    clearGoodsCardCache()
    expect(getGoodsLayout(m)).toEqual(layout)   // 内容一致，只是缓存被丢弃后重建
    expect(getGoodsCard(m)).toEqual(card)
    expect(getGoodsCard(m)).not.toBe(card)
  })

  it('caches the null result for non-goods messages', () => {
    const video = msg({ content: '[分享视频] 标题' })
    expect(getGoodsCard(video)).toBeNull()
    expect(getGoodsCard(video)).toBeNull()
  })
})
