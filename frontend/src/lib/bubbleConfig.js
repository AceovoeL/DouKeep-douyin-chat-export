// 聊天气泡装饰的配置层。
//
// 自动生成的配置在 data/media/bubbles/index.json，由后端 /api/bubbles 返回。
// 这里再留一份 BUILTIN_BUBBLES，方便以后手工覆盖某个气泡的外观；默认为空，
// 不会把抖音素材或几百个 ID 写进仓库。
//
// 气泡图自带的 FlexData 已经算好了：
// - slice：九宫格切片（百分比，顺序 上/右/下/左）
// - border：九宫格边框显示宽度（px，顺序 上/右/下/左）
// - padding：文字安全区（px，顺序 上/右/下/左）
// 查看器只负责把这些数字绑成 CSS 变量。

export const BUILTIN_BUBBLES = {}

let cachedCatalog = null
let pendingCatalog = null

function isObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function finite(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function round(value) {
  return Math.round(finite(value) * 100) / 100
}

function validBlock(value) {
  return isObject(value)
    && ['top', 'right', 'bottom', 'left'].every(key => Number.isFinite(Number(value[key])))
}

function imageUrl(value) {
  if (typeof value !== 'string') return ''
  if (value.startsWith('/media/')) return value
  // 采集端存的是相对 media 的路径（bubbles/<id>/self.png），阅读端补上前缀。
  if (value.startsWith('bubbles/')) return '/media/' + value
  return ''
}

export function isValidBubbleVariant(variant) {
  if (!isObject(variant)) return false
  if (!imageUrl(variant.image)) return false
  if (!isObject(variant.display)
      || finite(variant.display.width) <= 0
      || finite(variant.display.height) <= 0) return false
  return validBlock(variant.slice)
    && validBlock(variant.border)
    && validBlock(variant.padding)
}

function mergeCatalog(builtin, runtime) {
  const merged = {}
  for (const [id, entry] of Object.entries(isObject(runtime) ? runtime : {})) {
    if (isObject(entry)) merged[id] = entry
  }
  for (const [id, entry] of Object.entries(isObject(builtin) ? builtin : {})) {
    if (isObject(entry)) merged[id] = { ...(merged[id] || {}), ...entry }
  }
  return merged
}

/** 取配置表；同一个页面只请求一次，失败返回空表并允许下次重试。 */
export async function loadBubbleCatalog(fetcher) {
  if (cachedCatalog) return cachedCatalog
  if (pendingCatalog) return pendingCatalog
  const doFetch = fetcher || (typeof fetch === 'function' ? fetch : null)
  if (!doFetch) return {}
  let succeeded = false
  pendingCatalog = Promise.resolve(doFetch('/api/bubbles', { cache: 'no-store' }))
    .then(response => {
      succeeded = !!response && response.ok
      return succeeded ? response.json() : {}
    })
    .then(payload => {
      if (!succeeded) return {}
      const runtime = isObject(payload) && isObject(payload.bubbles) ? payload.bubbles : {}
      cachedCatalog = mergeCatalog(BUILTIN_BUBBLES, runtime)
      return cachedCatalog
    })
    .catch(() => ({}))
    .finally(() => {
      pendingCatalog = null
    })
  return pendingCatalog
}

/** 测试用：清掉模块级缓存。 */
export function resetBubbleCatalogCache() {
  cachedCatalog = null
  pendingCatalog = null
}

/**
 * 选当前视角该用的气泡。
 *
 * - 有对应一侧的图：直接用（对方图本身已经是镜像图，文字边距也是反的）；
 * - 缺这一侧：用另一侧，标记 mirrored，文字 padding 左右互换；
 *   背景层由 CSS 做 scaleX(-1)；
 * - 缺 ID、缺图、数字不合法：返回 null，前端退回普通气泡。
 */
export function resolveBubble(catalog, bubbleId, isSelf) {
  const id = String(bubbleId || '')
  if (!id || !isObject(catalog)) return null
  const entry = catalog[id]
  if (!isObject(entry)) return null

  const preferred = isSelf ? entry.self : entry.guest
  const fallback = isSelf ? entry.guest : entry.self
  let variant = preferred
  let mirrored = false
  if (!isValidBubbleVariant(variant)) {
    if (!isValidBubbleVariant(fallback)) return null
    variant = fallback
    mirrored = true
  }

  const padding = {
    top: round(variant.padding.top),
    right: round(variant.padding.right),
    bottom: round(variant.padding.bottom),
    left: round(variant.padding.left),
  }
  if (mirrored) {
    const left = padding.left
    padding.left = padding.right
    padding.right = left
  }

  const offset = isObject(entry.offset) ? entry.offset : {}
  const direction = isSelf ? 1 : -1
  const textColor = typeof entry.text_color === 'string'
    && /^#[0-9a-fA-F]{3,8}$/.test(entry.text_color)
    ? entry.text_color
    : ''

  return {
    id,
    image: imageUrl(variant.image),
    display: {
      width: round(variant.display.width),
      height: round(variant.display.height),
    },
    slice: {
      top: round(variant.slice.top),
      right: round(variant.slice.right),
      bottom: round(variant.slice.bottom),
      left: round(variant.slice.left),
    },
    border: {
      top: round(variant.border.top),
      right: round(variant.border.right),
      bottom: round(variant.border.bottom),
      left: round(variant.border.left),
    },
    padding,
    textColor,
    offsetX: round(finite(offset.x) * direction),
    offsetY: round(finite(offset.y)),
    mirrored,
    estimated: !!variant.estimated,
  }
}

/** 把解析好的气泡换成 CSS 变量；pseudo 元素和文字层都靠这些变量。 */
export function bubbleCssVars(bubble) {
  if (!bubble) return {}
  const px = value => round(value) + 'px'
  const pct = value => round(value) + '%'
  const image = String(bubble.image).replace(/["\\]/g, '')
  const vars = {
    '--bubble-image': 'url("' + image + '")',
    '--bubble-slice': [bubble.slice.top, bubble.slice.right, bubble.slice.bottom, bubble.slice.left]
      .map(pct).join(' ') + ' fill',
    '--bubble-border-width': [bubble.border.top, bubble.border.right, bubble.border.bottom, bubble.border.left]
      .map(px).join(' '),
    '--bubble-pad': [bubble.padding.top, bubble.padding.right, bubble.padding.bottom, bubble.padding.left]
      .map(px).join(' '),
    '--bubble-min-width': px(bubble.display.width),
    '--bubble-min-height': px(bubble.display.height),
    '--bubble-offset-x': px(bubble.offsetX || 0),
    '--bubble-offset-y': px(bubble.offsetY || 0),
  }
  if (bubble.textColor) vars['--bubble-text-color'] = bubble.textColor
  return vars
}
