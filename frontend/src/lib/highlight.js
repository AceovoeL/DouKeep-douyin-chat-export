// Shared search-highlight / rich-text helpers (used by MessageList + SearchBar
// + ForwardRecords).
import { emojiUrl } from './emojiAssets'

const MARK_STYLE = 'background:var(--highlight);color:#000;padding:0 2px;border-radius:2px'

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

// Escape text to safe HTML. Plain string replacement (instead of the previous
// `textContent` → `innerHTML` round-trip) so these helpers stay DOM-free and
// can be unit-tested outside a browser.
export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch])
}

const MARK = (inner) => `<mark style="${MARK_STYLE}">${inner}</mark>`

// Return `text` as HTML with case-insensitive matches of `query` wrapped in a
// <mark>. When there is no query (or no text) the escaped text is returned
// unchanged.
//
// ⚠️ 必须**先在原文上定位、再逐段转义**，不能先转义再搜索：转义会把 `&` 变成
// `&amp;`、`<` 变成 `&lt;`，之后拿用户原样输入的 query 去搜转义后的字符串，就会
// 命中这些实体的内部 —— 实测搜 `&` 会在 `&amp;` 里也包一个 <mark>（一个真 `&` 出
// 三个黄条），搜 `amp` 会把 `&amp;` 的实体名高亮出来，搜 `<` 则一个都命中不了。
// 这里是搜索高亮，命中位置必须落在用户真正看到的字符上。
export function highlightText(text, query) {
  const source = text === null || text === undefined ? '' : String(text)
  if (!source || !query) return escapeHtml(source)

  // query 也要转义成正则可用的形式；它本身不含要展示的 HTML，所以单独 re-escape。
  const pattern = new RegExp(String(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi')
  let out = ''
  let last = 0
  for (const match of source.matchAll(pattern)) {
    out += escapeHtml(source.slice(last, match.index))
    out += MARK(escapeHtml(match[0]))
    last = match.index + match[0].length
  }
  if (last < source.length) out += escapeHtml(source.slice(last))
  return out
}

// 文字式表情：[钱]、[憨笑] 这种记号（抖音把表情存成文字），长度有限，不会跨行。
const EMOJI_TOKEN_RE = /\[[^[\]\n]{1,20}\]/g

// 有本地图片就出 <img>，没有就返回 null（调用方保留原文字）。
// 图片大小由 style.css 里的 .msg-emoji 控制，跟随所在气泡的字号。
// onerror 是给「资源包还没下载」准备的：本机没有这张图时换回原来的文字记号，
// 而不是留一个破图（图片不进仓库，第一次运行要下，见 tools/download_emoji.py）。
function emojiImgHtml(token, query) {
  const url = emojiUrl(token.slice(1, -1))
  if (!url) return null
  const label = token.replace(/"/g, '&quot;')
  const img = `<img class="msg-emoji" src="${url}" alt="${label}" title="${label}" draggable="false" onerror="this.replaceWith(this.alt)">`
  // 搜索命中表情名时图片一起高亮，跟文字结果的观感保持一致。
  const hit = !!query && token.toLowerCase().includes(query.toLowerCase())
  return hit ? `<mark style="${MARK_STYLE}">${img}</mark>` : img
}

// 渲染消息正文的统一入口：既做搜索高亮，又把本地有图的文字式表情换成图片。
// 先按 [xx] 记号把原文切开，每段各自转义（不会把记号里的字符当 HTML）。
export function renderRichText(text, query) {
  const source = text === null || text === undefined ? '' : String(text)
  if (!source) return ''
  const parts = []
  let last = 0
  for (const match of source.matchAll(EMOJI_TOKEN_RE)) {
    if (match.index > last) parts.push(highlightText(source.slice(last, match.index), query))
    parts.push(emojiImgHtml(match[0], query) || highlightText(match[0], query))
    last = match.index + match[0].length
  }
  if (last < source.length) parts.push(highlightText(source.slice(last), query))
  return parts.join('')
}
