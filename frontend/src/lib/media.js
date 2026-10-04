// Resolve a stored avatar path to a servable URL (used by MessageList +
// ConversationList). Local 'avatars/…' paths are served under /media/; full
// http(s) URLs pass through; anything else has no avatar (initial-letter fallback).
export function resolveAvatarUrl(url) {
  if (!url) return null
  if (url.startsWith('avatars/')) return `/media/${url}`
  if (url.startsWith('http')) return url
  return null
}

// 卡片自带的图（群邀请卡的群头像、豆包卡的封面）：抖音给的是**带签名的临时链接**
// （180 天左右过期，群头像那个还是 http），直接塞进 <img> 迟早会碎。
//
// 统一走 /media/card_icons/<链接哈希><扩展名>?url=<原链接>：后端优先发本地存好的
// 那一份，本地没有且链接还没过期时顺手补拉一份存下来（见 common/card_icons.py）。
const CARD_ICON_HOSTS = /(^|\.)(douyinpic\.com|byteimg\.com|douyin\.com|douyinstatic\.com|bytedance\.com|bytecdn\.cn|ibytedtos\.com|volces\.com)$/i

// 文件名 = 链接的 FNV-1a 哈希（8 位十六进制）+ 扩展名，和 Python 端
// common/card_icons.py 的 icon_filename() 算出来一模一样 —— 采集时存下的那张图，
// 阅读端直接命中，不会因为两边名字对不上而白拉一次网。
function iconHash(text) {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

function iconExtension(url) {
  const path = url.split('?')[0].split('#')[0]
  const match = path.match(/\.([a-z0-9]+)$/i)
  const ext = match ? match[1].toLowerCase() : ''
  if (ext === 'jpeg') return '.jpg'
  return ['jpg', 'png', 'webp', 'gif', 'bmp'].includes(ext) ? `.${ext}` : '.webp'
}

// 卡片图该用的地址；不是抖音图床的链接（或压根没链接）返回空串。
export function iconSrc(url) {
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return ''
  let host = ''
  try {
    host = new URL(url).hostname
  } catch {
    return ''
  }
  if (!CARD_ICON_HOSTS.test(host)) return ''
  return `/media/card_icons/${iconHash(url)}${iconExtension(url)}?url=${encodeURIComponent(url)}`
}
