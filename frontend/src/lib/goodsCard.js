// 商品分享卡片（aweType=11029 · im_dynamic_patch.card_key=msg_big_product）的
// 纯解析函数。抖音把整张卡片的排版放在 im_dynamic_patch.raw_data 里：
//   top                 商品主图（im-image）
//   content_top         商品描述（im-text）
//   content_content     价格文案列表（im-text-list，如 "¥9" / "券后价" / "已售3956件"）
//   content_bottom_right 右侧优惠标签（如 "券" / "立减6" / "8.5折" / "满5减2"）
//   whole_card.action_info[].params.schema  跳转 schema（含 commodity_id、meta_params）
// 解析纯函数独立成模块，便于单测（同 douyinMessage.js 的约定）。
import { getContentJson, extractShareTitle } from './douyinMessage'

const GOODS_CARD_KEYS = new Set(['msg_big_product', 'commodity_msg_big_product'])

// 卡片排版字符串约 10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果
// （与 douyinMessage.js 的 cjCache 同一套约定：同一行对象 → 同一结果）。
let layoutCache = new WeakMap()
let cardCache = new WeakMap()

export function clearGoodsCardCache() {
  layoutCache = new WeakMap()
  cardCache = new WeakMap()
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

// content_json（存在 raw_data 里，双层编码）；旧数据可能只把卡片 JSON 放在 content。
function contentSource(msg) {
  const cj = getContentJson(msg)
  if (cj && typeof cj === 'object') return cj
  const text = String(msg?.content || '')
  if (text.startsWith('{')) return asObject(text) || {}
  return {}
}

function dynamicPatch(msg) {
  return asObject(contentSource(msg).im_dynamic_patch)
}

// im_dynamic_patch.raw_data 里的排版对象（字符串或已解析对象都兼容）。
export function getGoodsLayout(msg) {
  if (layoutCache.has(msg)) return layoutCache.get(msg)
  const patch = dynamicPatch(msg)
  const layout = patch ? asObject(patch.raw_data) : null
  layoutCache.set(msg, layout)
  return layout
}

// 卡片上的图片地址：top.content 可能是字符串，也可能是 {url_list:[...]}。
function imageUrl(value) {
  if (typeof value === 'string') return value
  return value?.url_list?.[0] || ''
}

// im-text / im-text-list 节点 → 文本数组。
function textList(node) {
  const parsed = asObject(node)
  const content = parsed?.content
  if (Array.isArray(content)) {
    return content
      .map(item => (typeof item === 'string' ? item : item?.text))
      .filter(text => typeof text === 'string' && text.trim())
      .map(text => text.trim())
  }
  if (typeof content === 'string' && content.trim()) return [content.trim()]
  return []
}

// 纯价格文本（"¥9" / "￥8.27" / "9"）→ 数值；其它文案（"已售3956件"）返回 null。
function parsePrice(text) {
  const match = String(text).match(/^[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)$/)
  if (!match) return null
  const amount = Number(match[1])
  return Number.isFinite(amount) ? amount : null
}

function money(value) {
  return String(Math.round(value * 100) / 100)
}

// schema 的 meta_params.entrance_info.real_price 是「单位：分」的划线价（原价）。
function schemaOriginalPrice(layout, coupon) {
  for (const action of asObject(layout.whole_card)?.action_info || []) {
    const schema = String(asObject(action)?.params?.schema || '')
    const raw = schema.match(/[?&]meta_params=([^&]+)/)?.[1]
    if (!raw) continue
    let meta = null
    try { meta = JSON.parse(decodeURIComponent(raw)) } catch { continue }
    let entrance = meta?.entrance_info
    if (typeof entrance === 'string') entrance = asObject(entrance)
    const cents = Number(entrance?.real_price ?? meta?.real_price)
    if (!Number.isFinite(cents) || cents <= 0) continue
    const yuan = money(cents / 100)
    if (coupon === '' || Number(yuan) > Number(coupon)) return yuan
  }
  return ''
}

// 底部优惠标签推算原价：立减N → 券后价+N；N折 → 券后价÷(N/10)。「满X减Y」不推算。
function discountOriginalPrice(coupon, discountText) {
  if (coupon === '') return ''
  const off = discountText.match(/立减\s*([0-9]+(?:\.[0-9]+)?)/)
  if (off) return money(Number(coupon) + Number(off[1]))
  const zhe = discountText.match(/([0-9]+(?:\.[0-9]+)?)\s*折/)
  if (zhe && Number(zhe[1]) > 0) return money(Number(coupon) / (Number(zhe[1]) / 10))
  return ''
}

// 商品卡识别：card_key/card_type 命中，或排版同时给出描述 + 价格 + 主图。
export function isGoodsCard(msg) {
  const layout = getGoodsLayout(msg)
  if (!layout) return false
  const patch = dynamicPatch(msg)
  const cardKey = String(patch?.card_key || '')
  const cardType = String(patch?.card_type || '')
  if (GOODS_CARD_KEYS.has(cardKey) || GOODS_CARD_KEYS.has(cardType)) return true
  return !!layout.content_top && !!layout.content_content && !!layout.top
}

// 商品卡数据：{ title, cover, couponPrice, originalPrice }；非商品卡返回 null。
export function getGoodsCard(msg) {
  if (cardCache.has(msg)) return cardCache.get(msg)
  const card = parseGoodsCard(msg)
  cardCache.set(msg, card)
  return card
}

function parseGoodsCard(msg) {
  if (!isGoodsCard(msg)) return null
  const layout = getGoodsLayout(msg)
  const cj = contentSource(msg)
  const priceItems = textList(layout.content_content)
  const prices = priceItems
    .map((text, index) => ({ index, amount: parsePrice(text) }))
    .filter(item => item.amount !== null)

  // 「券后价」标签紧跟其后的那个价格就是券后价；没有标签时把首个价格当作券后价。
  let coupon = ''
  let original = ''
  if (prices.length) {
    let couponItem = prices.find(item => /券后价/.test(priceItems[item.index + 1] || ''))
    if (!couponItem) {
      const marker = priceItems.findIndex(text => /券后价/.test(text))
      couponItem = (marker >= 0 ? prices.filter(item => item.index < marker).pop() : null) || prices[0]
    }
    coupon = money(couponItem.amount)
    const other = prices.find(item => item !== couponItem)
    if (other) original = money(other.amount)
  }

  const discountText = textList(layout.content_bottom_right).join('')
  if (!original) original = schemaOriginalPrice(layout, coupon)
  if (!original) original = discountOriginalPrice(coupon, discountText)

  return {
    title: String(asObject(layout.content_top)?.content || extractShareTitle(msg.content) || '[商品]'),
    cover: imageUrl(layout.top?.content) || imageUrl(cj.cover_url) || imageUrl(cj.aweme_info?.cover_url),
    couponPrice: coupon,
    originalPrice: original,
  }
}
