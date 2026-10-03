import { getContentJson, getShareInfo, isViewOnce, getViewOnceText, relationNoticeText } from './douyinMessage'

export function sharePreview(item) {
  let cj = getContentJson(item)
  if (!cj) {
    try { cj = JSON.parse(item.content) } catch { /* Legacy content can be truncated. */ }
  }
  if (!cj || typeof cj !== 'object' || Array.isArray(cj)) cj = {}
  const awe = Number(cj.aweType)
  // "View once" text messages (aweType=10401 + body, no card fields) are stored
  // as msg_type=4 by older scrapers. Searching them must show the message text
  // with no [分享] prefix — the search result is not a share.
  if (isViewOnce(item)) return getViewOnceText(item)
  // 互相关注/成为好友的提示（「我们已互相关注，可以开始聊天了」）不是分享卡，
  // 搜索结果里直接显示这句话，别把整段 JSON 当预览吐出来。
  const notice = relationNoticeText(item)
  if (notice) return notice
  const info = getShareInfo(item)
  const title = info.title
  const comment = typeof cj.comment === 'string' ? cj.comment : ''
  const prefix = String(item.content || '').match(/^\[分享([^\]]+)\]/)?.[1]
  if (item.msg_type !== 4 && !info.itemId && !cj.im_dynamic_patch && !cj.awemeType && !prefix && ![10500, 11029, 805].includes(awe)) return null
  let type = comment || awe === 10500 ? '评论' : String(cj.push_detail || '').match(/\[(?:分享)?(动图|图文|视频|评论|文章|商品)\]/)?.[1]
  if (!type && Number(cj.awemeType) === 68) type = Number(cj.is_live_photo) === 1 ? '动图' : '图文'
  if (!type && Number(cj.awemeType) === 163) type = '文章'
  // 10401 的商品卡片与「仅看一次」共用 aweType；后者已在上方按纯文本返回。
  if (!type && (awe === 11029 || awe === 10401)) type = '商品'
  // 805 是「限时日常」作品分享，卡片没有标题，单独标注而不是笼统的 [分享]。
  if (!type && awe === 805) type = '限时日常'
  // 作品分享（800/801/803/11054…）没有标题时以前标成 [分享链接]，看不出分享的是什么；
  // 这些卡片本身就是分享视频，统一标成 [分享视频]（有标题时标题接在后面）。
  if (!type && [800, 801, 803, 11054, 11055, 11063, 11066, 11067, 11069, 11070].includes(awe)) type = '视频'
  type ||= prefix || (info.itemId ? '视频' : '')
  const content = String(item.content || '').trim()
  const fallback = content.startsWith('{') || content.startsWith('[') && !prefix ? '' : content.replace(/^\[分享[^\]]*\]\s*/, '')
  return `[分享${type}] ${comment || title || fallback}`.trim()
}
