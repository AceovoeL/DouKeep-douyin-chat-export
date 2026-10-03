import { describe, expect, it } from 'vitest'
import { sharePreview } from './sharePreview'
const row = cj => ({ msg_type: 4, content: '{truncated', raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }) })
describe('readable shared-message search results', () => {
  it.each([
    [{ aweType: 800, content_title: '标题' }, '[分享视频] 标题'],
    [{ aweType: 10500, comment: '评论内容', content_title: '标题' }, '[分享评论] 评论内容'],
    [{ awemeType: 68, content_title: '标题' }, '[分享图文] 标题'],
    [{ awemeType: '68', is_live_photo: '1', content_title: '标题' }, '[分享动图] 标题'],
    [{ awemeType: 163, content_title: '标题' }, '[分享文章] 标题'],
    [{ push_detail: '分享[动图]', aweType: 800, content_title: '标题' }, '[分享动图] 标题'],
    [{ aweType: 10401, content_title: '商品名称' }, '[分享商品] 商品名称'],
    // 限时日常（aweType=805）：卡片不带标题，标注为 [分享限时日常] 而不是 [分享]。
    [{ aweType: 805, itemId: '1000000000000000040' }, '[分享限时日常]'],
    [{ aweType: 805, content_title: '和 @示例用户 一起 #合拍' }, '[分享限时日常] 和 @示例用户 一起 #合拍'],
  ])('labels %j', (cj, text) => expect(sharePreview(row(cj))).toBe(text))
  it('does not leak malformed JSON or classify ordinary text as a share', () => {
    expect(sharePreview({ msg_type: 4, content: '{broken', raw_data: '{broken' })).toBe('[分享]')
    expect(sharePreview({ msg_type: 1, content: '普通消息' })).toBeNull()
  })
})

// 没有标题的分享视频：卡片标题行留白，但搜索结果那一行要写清类型 ——
// 以前是笼统的 [分享链接]，看不出分享的是什么。
describe('没有标题的分享视频搜索结果', () => {
  const row = cj => ({ msg_type: 4, content: '[分享]',
    raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }) })
  it.each([
    ['800', { aweType: 800, itemId: '42' }],
    ['801', { aweType: 801, itemId: '42' }],
    ['803', { aweType: 803, itemId: '42' }],
    ['11054', { aweType: 11054, itemId: '42' }],
  ])('aweType=%s 标注为 [分享视频]', (_awe, cj) => {
    expect(sharePreview(row(cj))).toBe('[分享视频]')
  })
  it('用户报告的那类消息（有封面和作者、没有标题；已脱敏）', () => {
    expect(sharePreview(row({
      aweType: 800, awemeType: 0, content_title: '', content_name: '示例作者',
      cover_url: { url_list: ['https://example.com/cover.jpeg'] },
      itemId: '1000000000000000041',
    }))).toBe('[分享视频]')
  })
  it('有标题时标题照旧接在类型后面', () => {
    expect(sharePreview(row({ aweType: 800, content_title: '视频标题', itemId: '42' })))
      .toBe('[分享视频] 视频标题')
  })
})

it('reads dynamic titles and bracketed share hints without mislabeling photos', () => {
  for (const type of ['图文', '动图', '视频']) {
    const msg = row({ aweType: 11054, push_detail: `[分享${type}]`, item_id: '123',
      im_dynamic_patch: { raw_data: JSON.stringify({ top_bottom_top: { content: '完整标题' } }) } })
    expect(sharePreview(msg)).toBe(`[分享${type}] 完整标题`)
  }
})

// 回填之后的库里，805 行的 content 是 [分享限时日常]（不再是截断的 JSON）。
it('keeps the 限时日常 label for backfilled rows', () => {
  const backfilled = { msg_type: 0, content: '[分享限时日常]',
    raw_data: JSON.stringify({ content_json: JSON.stringify({ aweType: 805, itemId: '99' }) }) }
  expect(sharePreview(backfilled)).toBe('[分享限时日常]')
  const titled = { msg_type: 0, content: '[分享限时日常] 和 @示例用户 一起 #合拍',
    raw_data: JSON.stringify({ content_json: JSON.stringify({
      aweType: 805, itemId: '99', content_title: '和 @示例用户 一起 #合拍' }) }) }
  expect(sharePreview(titled)).toBe('[分享限时日常] 和 @示例用户 一起 #合拍')
})

// Search results for a "view once" message must show its text with no [分享]
// prefix. AweType 10401 was previously mislabelled: the goods branch and the
// generic share fallback both matched a card-less text payload.
describe('view once (仅看一次) search results', () => {
  const viewOnce = text => ({ msg_type: 4, content: '{truncated',
    raw_data: JSON.stringify({ is_recalled: 1778510816059, content_json: JSON.stringify({
      text, richTextInfos: [], ai_ext: '{}', scene: '', aweType: 10401,
      related_share_video: {}, mention_users: [],
    }) }) })
  it.each(['示例文本一', '示例文本二'])('shows %s verbatim', text => {
    expect(sharePreview(viewOnce(text))).toBe(text)
  })
  it('still labels a 10401 goods card as a share', () => {
    expect(sharePreview(row({ aweType: 10401, content_title: '商品名称' }))).toBe('[分享商品] 商品名称')
  })
})

// 互相关注/成为好友那几句提示以前会在搜索结果里露出整段 JSON（不是分享卡），
// 现在直接显示那句话本身。
describe('mutual-follow notices in search results', () => {
  const notice = cj => ({ msg_type: 1, content: JSON.stringify(cj),
    raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }) })
  it('shows the notice text instead of raw JSON', () => {
    expect(sharePreview(notice({ aweType: 0, tips: '你们已互相关注对方' }))).toBe('你们已互相关注对方')
    expect(sharePreview(notice({ aweType: 701, text: '我们已互相关注，可以开始聊天了' })))
      .toBe('我们已互相关注，可以开始聊天了')
  })
})
