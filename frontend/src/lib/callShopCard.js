// 「联系门店」引导卡片（aweType=110284 · im_dynamic_patch.card_type=life_bar_link_private_msg_guide）
// 的纯解析函数。抖音把整张卡片交给一个 lynx 模板渲染，模板要的数据全在
// im_dynamic_patch.raw_data 里：
//   content_left           左侧橙色电话圆形图标（im-image）
//   content_middle_top     主标题（im-text，如「联系门店」）
//   content_middle_bottom  副标题（im-text，如「回复较慢，可拨打电话」）
//   content_right          右侧按钮（im-button，如「联系」，带拨号 schema）
//   whole_card             整卡可点区域（同样是拨号 schema）
// 卡片外观直接用项目 assets/call-to-shop.png（构建时打进 dist/assets），
// 这里只负责「认出这张卡 + 读出按钮文字」，排版交给 MessageList.vue。
// 解析独立成模块（同 flameCard.js / goodsCard.js 的约定）：纯函数，便于单测。
import { getContentJson } from './douyinMessage'

const CALL_SHOP_AWE_TYPE = 110284
const CALL_SHOP_CARD_TYPES = new Set(['life_bar_link_private_msg_guide'])

// 卡片排版字符串约 10KB，一条消息在模板里会被问好几次；按消息行缓存解析结果。
let cardCache = new WeakMap()

export function clearCallShopCardCache() {
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

// 排版节点 → 文字。content 可能是字符串，也可能是组件列表；不是字符串就不当文字用
// （否则整个数组会被渲染成 "[object Object]"）。
function nodeText(node) {
  const content = asObject(node)?.content
  return typeof content === 'string' ? content.trim() : ''
}

function parseCallShopCard(msg) {
  const cj = getContentJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const patch = asObject(cj.im_dynamic_patch)
  const cardTypes = [patch?.card_type, patch?.server_card_type, patch?.card_key].map(v => String(v || ''))
  const isCallShop = cardTypes.some(t => CALL_SHOP_CARD_TYPES.has(t))
    || Number(cj.aweType) === CALL_SHOP_AWE_TYPE
  if (!isCallShop) return null
  // 先认卡片再解析排版：别的分享卡（视频卡、商品卡等）也带 im_dynamic_patch，
  // 不该为了看一眼就白白解析那几 KB 的排版 JSON。
  const layout = patch ? asObject(patch.raw_data) : null
  return {
    title: nodeText(layout?.content_middle_top) || '联系门店',
    subtitle: nodeText(layout?.content_middle_bottom),
    button: nodeText(layout?.content_right) || '联系',
    // 卡片自带的小图标（远程地址）；本项目用本地 assets/call-to-shop.png 当卡片外观，
    // 这里保留字段只是为了排查数据时能看到原始地址。
    icon: nodeText(layout?.content_left),
    description: String(patch?.description || cj.description || '').trim(),
  }
}

// 卡片数据：{ title, subtitle, button, icon, description }；不是这张卡则返回 null。
export function getCallShopCard(msg) {
  if (!msg) return null
  if (cardCache.has(msg)) return cardCache.get(msg)
  const card = parseCallShopCard(msg)
  cardCache.set(msg, card)
  return card
}

export function isCallShopCard(msg) {
  return !!getCallShopCard(msg)
}
