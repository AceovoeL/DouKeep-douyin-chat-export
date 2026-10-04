import { describe, it, expect } from 'vitest'
import {
  getContentJson, isJsonShare, getShareInfo, renderSystemMsg, shouldShow,
  isJsonSticker, isJsonSystemMsg, isVoiceMsg, getVoiceDuration, getVoiceUrl,
  isVideoMsg, isJsonVideo, getVideoDuration, getInlinePic, getImageSrc, getEmojiSrc,
  getRefMsg, getRefContent, getRefNickname, extractServerMsgIds, isRecalled,
  isViewOnce, getViewOnceText,
  getModifyKinds, getModifyReactions, modifyTimestamp, MODIFY_KIND_LABELS,
  getWatchTogether, getProfileCard, getForwardInfo, isSystemMsg, duplicateSystemMessageIds,
  isLooseEmoji, isLooseShare, isLooseImage, isShareCard, relationNoticeText,
  peerRelationNotice, systemNoticeSide, isDailyShare, shareCardTitle,
  getGroupNotice,
} from './douyinMessage.js'

// Build a message row. content_json is double-encoded inside raw_data, like the DB.
let _id = 0
function msg(fields = {}) {
  return { msg_id: `t${_id++}`, msg_type: 1, content: '', media_local_path: null,
           media_url: null, raw_data: null, ref_msg: null, timestamp: 0, sender_uid: 'u', ...fields }
}
function withCj(cj, fields = {}) {
  return msg({ raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }), ...fields })
}

describe('getContentJson', () => {
  it('parses double-encoded content_json', () => {
    expect(getContentJson(withCj({ a: 1 }))).toEqual({ a: 1 })
  })
  it('returns null without raw_data', () => {
    expect(getContentJson(msg())).toBeNull()
  })
})

describe('share detection + extraction', () => {
  it('isJsonShare true for content_title JSON, false for msg_type 4', () => {
    expect(isJsonShare(msg({ content: '{"content_title":"x"}' }))).toBe(true)
    expect(isJsonShare(msg({ content: '{"content_title":"x"}', msg_type: 4 }))).toBe(false)
    expect(isJsonShare(msg({ content: 'plain text' }))).toBe(false)
    expect(isJsonShare(msg({ content: '[分享视频] 标题' }))).toBe(true)
  })
  it('detects forwarded video vs comment shares from content_json, not the title text', () => {
    expect(isJsonShare(withCj({ aweType: 11054, content_title: '视频标题', text: '说明' },
      { content: '[分享视频] 视频标题' }))).toBe(true)
    expect(isJsonShare(withCj({ aweType: 10500, comment: '评论内容', content_title: '视频标题' },
      { content: '评论内容' }))).toBe(true)
    expect(isJsonShare(withCj({ aweType: 700, text: '评论', related_share_video: { itemId: '1' } },
      { content: '评论' }))).toBe(false)
  })
  it('getShareInfo pulls video-share fields', () => {
    const info = getShareInfo(withCj({ itemId: '42', content_title: 'T', content_name: 'A',
                                       cover_url: { url_list: ['http://c'] } }))
    expect(info.title).toBe('T')
    expect(info.author).toBe('A')
    expect(info.itemId).toBe('42')
    expect(info.cover).toBe('http://c')
  })
  it('getShareInfo resolves a product card via im_dynamic_patch', () => {
    const patch = { raw_data: JSON.stringify({
      content_top: { content: '商品名' },
      whole_card: { action_info: [{ params: { schema: 'x?commodity_id=999&y=1' } }] },
    }) }
    const info = getShareInfo(withCj({ im_dynamic_patch: patch }))
    expect(info.title).toBe('商品名')
    expect(info.productUrl).toBe('https://www.douyin.com/product/999')
  })
  it('does not treat a video share caption as a quoted comment', () => {
    const video = getShareInfo(withCj({ aweType: 11054, text: '说明文字', content_title: '视频标题', itemId: '42' }))
    expect(video.comment).toBe('')
    expect(video.title).toBe('视频标题')
    const quoted = getShareInfo(withCj({ aweType: 10500, comment: '评论内容', content_title: '视频标题' }))
    expect(quoted.comment).toBe('评论内容')
    expect(quoted.title).toBe('视频标题')
    const flattenedVideo = getShareInfo(msg({ content: '[分享视频] 视频标题' }))
    expect(flattenedVideo.comment).toBe('')
    expect(flattenedVideo.title).toBe('视频标题')
    const flattenedComment = getShareInfo(withCj({ aweType: 10500, text: '评论内容' }, { content: '[分享评论] 评论内容' }))
    expect(flattenedComment.comment).toBe('评论内容')
    expect(flattenedComment.title).toBe('评论内容')
  })
})

// 群公告（type_code=1004）：结构取自线上样本（消息 id、群名已脱敏），群"示例群聊"。
describe('群公告', () => {
  const real = () => withCj({
    aweType: 0, notice_title: '群公告',
    notice_content: '示例群公告内容', upgraded_notice_content: '',
  })

  it('从 content_json 里认出群公告并取正文', () => {
    expect(getGroupNotice(real())).toEqual({ title: '群公告', body: '示例群公告内容' })
  })

  it('notice_content 为空时用 upgraded_notice_content', () => {
    const m = withCj({ aweType: 0, notice_title: '群公告', notice_content: '', upgraded_notice_content: '新版公告' })
    expect(getGroupNotice(m).body).toBe('新版公告')
  })

  it('没有 notice 字段的普通 JSON 不算群公告', () => {
    expect(getGroupNotice(withCj({ aweType: 0, text: '普通文本' }))).toBeNull()
    expect(getGroupNotice(msg({ content: 'plain text' }))).toBeNull()
  })

  it('正文为空也照样显示，不会被当成空消息丢掉', () => {
    const empty = withCj({ aweType: 0, notice_title: '群公告', notice_content: '', upgraded_notice_content: '' })
    expect(getGroupNotice(empty)).toEqual({ title: '群公告', body: '' })
    expect(shouldShow(empty)).toBe(true)
  })

  it('按系统提示排版（居中、不显示头像昵称那一套）', () => {
    expect(isSystemMsg(real())).toBe(true)
  })
})

describe('system messages', () => {
  it('renders a tips template', () => {
    const m = withCj({ tips: '{{1}}赞了你的 {{2}}',
                       template: [{ key: 1, name: '对方' }, { key: 2, name: '视频' }] }, { msg_type: 0 })
    expect(renderSystemMsg(m)).toBe('对方赞了你的 视频')
  })
  it('shouldShow hides empty system, keeps text', () => {
    expect(shouldShow(msg({ msg_type: 0, content: '{}' }))).toBe(false)
    expect(shouldShow(msg({ msg_type: 1, content: 'hi' }))).toBe(true)
  })
  it('isJsonSystemMsg needs both tips and aweType', () => {
    expect(isJsonSystemMsg(msg({ content: '{"tips":"x","aweType":1}' }))).toBe(true)
    expect(isJsonSystemMsg(msg({ content: '{"tips":"x"}' }))).toBe(false)
  })
})

describe('群通知（locale_resources 模板）', () => {
  const locale = (cj, fields = {}) => withCj(cj, { msg_type: 0, content: '[系统消息]', ...fields })

  it('把 {0}、{1} 换成 active_users / passive_users 里的昵称', () => {
    const m = locale({
      aweType: 100140,
      active_users: [{ uid: 1, nickname: '示例昵称' }],
      passive_users: [{ uid: 2, nickname: '示例用户' }],
      locale_resources: [{ lang: 'zh-Hans', text: '{0}邀请{1}加入了群聊，新成员可查看历史消息' }],
    })
    expect(renderSystemMsg(m)).toBe('示例昵称邀请示例用户加入了群聊，新成员可查看历史消息')
    expect(shouldShow(m)).toBe(true)
  })

  it('只有一个占位（改群头像 / 改群名）也能渲染', () => {
    const m = locale({
      aweType: 100115,
      active_users: [{ uid: 1000000000000030, nickname: '示例昵称' }],
      passive_users: null,
      locale_resources: [{ lang: 'zh-Hans', text: '{0}修改了群头像' }],
    })
    expect(renderSystemMsg(m)).toBe('示例昵称修改了群头像')
  })

  it('优先用简体中文那条，缺了就退回第一条', () => {
    const m = locale({
      aweType: 100106,
      active_users: [{ uid: 9, nickname: '示例群主' }],
      locale_resources: [
        { lang: 'en', text: '{0} changed the group name' },
        { lang: 'zh-Hans', text: '{0}修改群名为“example_user”' },
      ],
    })
    expect(renderSystemMsg(m)).toBe('示例群主修改群名为“example_user”')
  })

  it('当事人不是我时，用别人看到的那句（「我……」→ passive_notice）', () => {
    const cj = {
      aweType: 100149,
      active_users: null,
      passive_users: null,
      locale_resources: [{ lang: 'zh-Hans', text: '我发布了新作品，快来看看！' }],
      content_ext: { active_notice: '我发布了新作品', passive_notice: '群主发布了新作品，快来看看！' },
    }
    expect(renderSystemMsg(locale(cj, { sender_uid: 'u1' }), 'u2'))
      .toBe('群主发布了新作品，快来看看！')
    // 自己发的就保留「我……」
    expect(renderSystemMsg(locale(cj, { sender_uid: 'u1' }), 'u1'))
      .toBe('我发布了新作品，快来看看！')
  })

  it('没有模板时仍然显示 content 兜底，不吐 JSON', () => {
    const m = locale({ aweType: 100149, active_users: null, passive_users: null })
    expect(renderSystemMsg(m)).toBe('[系统消息]')
  })
})

describe('system notice JSON toggle side', () => {
  const like = (sender) => withCj({ aweType: 126, tips: '{{1}}赞了你分享的 {{2}}',
    template: [{ key: 1, name: '对方' }, { key: 2, name: '视频' }] },
    { sender_uid: sender, msg_type: 0 })

  it('follows the rendered pronoun, so switching who "I" am moves the button', () => {
    const m = like('alice')
    expect(renderSystemMsg(m, 'alice')).toBe('你赞了对方分享的 视频')
    expect(systemNoticeSide(m, 'alice')).toBe('left')
    expect(renderSystemMsg(m, 'bob')).toBe('对方赞了你分享的 视频')
    expect(systemNoticeSide(m, 'bob')).toBe('right')
  })

  it('moves with the spark claim too', () => {
    const m = withCj({ aweType: 287, tips: '对方领取了火星 {{0}}', template: [{ key: 0, name: '去看看' }] },
      { sender_uid: 'alice', msg_type: 0 })
    expect(systemNoticeSide(m, 'alice')).toBe('left')
    expect(systemNoticeSide(m, 'bob')).toBe('right')
  })

  it('drops the button below when the subject is unknowable', () => {
    const cases = [
      withCj({ tips: '你们已互相关注对方', aweType: 0 }, { msg_type: 0 }),
      withCj({ tips: '我们已互相关注，可以开始聊天了' }, { msg_type: 0 }),
      withCj({ tips: '你们当前使用的合养精灵形象还有 3 天过期' }, { msg_type: 0 }),
      msg({ msg_type: 0, content: '某某 在聊天中截屏了' }),
    ]
    for (const m of cases) expect(systemNoticeSide(m, 'alice')).toBe('below')
  })
})

describe('mutual-follow notices (互相关注 / 成为好友)', () => {
  // 同一次互相关注事件抖音会下发 6 条：双方各一条 tips、各一条 701 纯文本、各一张打招呼卡片。
  const tipsRow = (ts, fields = {}) => withCj({ tips: '你们已互相关注对方', aweType: 0 },
    { conv_id: 'c1', timestamp: ts, sender_uid: 'alice', ...fields })
  const textRow = (ts, fields = {}) => withCj({ text: '我们已互相关注，可以开始聊天了', aweType: 701 },
    { conv_id: 'c1', timestamp: ts, sender_uid: 'alice',
      content: '我们已互相关注，可以开始聊天了', ...fields })
  const cardRow = (ts, fields = {}) => withCj(
    { hello_text: '打个招呼吧', hint_text: '我们已互相关注，可以开始聊天了', aweType: -1, joker_stickers: [{}] },
    { conv_id: 'c1', timestamp: ts, sender_uid: 'alice', msg_type: 0, ...fields })

  // 「我们…」那句在抖音客户端里是对方发来的消息，界面按对方消息渲染；
  // 「你们…」那句是对双方说的系统提示，仍然居中。
  it('marks the 「我们…」notice as a peer message and the 「你们…」one as a system tip', () => {
    const text = textRow(100)
    expect(relationNoticeText(text)).toBe('我们已互相关注，可以开始聊天了')
    expect(peerRelationNotice(text)).toBe('我们已互相关注，可以开始聊天了')
    expect(renderSystemMsg(text)).toBe('我们已互相关注，可以开始聊天了')
    expect(shouldShow(text)).toBe(true)

    const tip = tipsRow(100)
    expect(relationNoticeText(tip)).toBe('你们已互相关注对方')
    expect(peerRelationNotice(tip)).toBe('')
    expect(isSystemMsg(tip)).toBe(true)
  })

  it('treats 「我们已成为朋友」as a peer message too', () => {
    expect(peerRelationNotice(withCj({ tips: '我们已成为朋友', aweType: 0 })))
      .toBe('我们已成为朋友')
    expect(peerRelationNotice(withCj({ tips: '你们已成为朋友', aweType: 0 }))).toBe('')
  })

  it('reads the greeting card hint text but leaves ordinary messages alone', () => {
    expect(relationNoticeText(cardRow(100))).toBe('我们已互相关注，可以开始聊天了')
    expect(relationNoticeText(msg({ content: '你好' }))).toBe('')
    expect(isSystemMsg(msg({ content: '你好' }))).toBe(false)
  })

  it('keeps one line per distinct notice and hides the mirrored copies', () => {
    const rows = [textRow(100), cardRow(100), tipsRow(102),
      cardRow(102, { sender_uid: 'me' }), tipsRow(104, { sender_uid: 'me' }),
      textRow(105, { sender_uid: 'me' })]
    const hidden = duplicateSystemMessageIds(rows)
    expect([...hidden].sort()).toEqual(
      [rows[1].msg_id, rows[3].msg_id, rows[4].msg_id, rows[5].msg_id].sort())
    expect(hidden.has(rows[0].msg_id)).toBe(false)
    expect(hidden.has(rows[2].msg_id)).toBe(false)
  })

  it('shows a later re-follow again and never pairs across conversations', () => {
    expect(duplicateSystemMessageIds([tipsRow(100), tipsRow(130)]).size).toBe(1)
    expect(duplicateSystemMessageIds([tipsRow(100), tipsRow(131)]).size).toBe(0)
    expect(duplicateSystemMessageIds([tipsRow(100), tipsRow(120, { conv_id: 'c2' })]).size).toBe(0)
  })
})

describe('sticker / voice / video detection', () => {
  it('isJsonSticker via content or content_json', () => {
    expect(isJsonSticker(msg({ content: '{"stickers":[]}' }))).toBe(true)
    expect(isJsonSticker(withCj({ joker_stickers: [{}] }, { content: '{"x":1}' }))).toBe(true)
  })
  it('voice: detected by resource_url, duration in ms', () => {
    const v = withCj({ resource_url: { url_list: ['http://v'] }, duration: 4200 }, { msg_type: 0 })
    expect(isVoiceMsg(v)).toBe(true)
    expect(getVoiceDuration(v)).toBe(4)
    expect(getVoiceUrl(v)).toBe('http://v')
  })
  it('voice: recognizes legacy text-typed rows and resource duration', () => {
    const v = withCj({ resource_url: { uri: 'voice-uri', duration: '1200' } }, { msg_type: 1 })
    expect(isVoiceMsg(v)).toBe(true)
    expect(getVoiceDuration(v)).toBe(1)
    expect(getVoiceUrl(v)).toBe('voice-uri')
  })
  it('voice: does not classify an image resource with a duration as audio', () => {
    const image = withCj({ aweType: 2702, resource_url: { url_list: ['http://image'] }, duration: 1200 }, { msg_type: 3 })
    expect(isVoiceMsg(image)).toBe(false)
  })
  it('video: msg_type 5 or cj.video.vid; duration in seconds with ″', () => {
    expect(isJsonVideo(msg({ msg_type: 5 }))).toBe(true)
    expect(isJsonVideo(withCj({ video: { vid: 'v1' } }))).toBe(true)
    expect(getVideoDuration(withCj({ duration: 12 }))).toBe('12″')
    expect(isVideoMsg(msg({ media_local_path: 'videos/x.mp4' }))).toBeTruthy()
    expect(isVideoMsg(msg({ media_local_path: 'images/x.jpg' }))).toBeFalsy()
  })
})

describe('media src helpers', () => {
  it('getInlinePic strips embedded newlines', () => {
    expect(getInlinePic(withCj({ inline_pic: 'AA\nBB\r\nCC' }))).toBe('data:image/webp;base64,AABBCC')
  })
  it('getImageSrc prefers local non-mp4, getEmojiSrc prefers local', () => {
    expect(getImageSrc(msg({ media_local_path: 'images/a.jpg' }))).toBe('/media/images/a.jpg')
    expect(getEmojiSrc(msg({ media_local_path: 'emoji/e.webp' }))).toBe('/media/emoji/e.webp')
    expect(getEmojiSrc(msg({ media_url: 'http://cdn/e' }))).toBe('http://cdn/e')
  })
})

describe('reply/quote', () => {
  it('new format uses content/nickname directly', () => {
    const m = msg({ ref_msg: JSON.stringify({ content: '原文', nickname: '小明' }) })
    const ref = getRefMsg(m)
    expect(getRefContent(ref)).toBe('原文')
    expect(getRefNickname(ref)).toBe('小明')
  })
  it('old format maps emoji/share content_json', () => {
    expect(getRefContent({ content_json: JSON.stringify({ aweType: 501 }) })).toBe('[表情]')
    expect(getRefContent({ content_json: JSON.stringify({ content_title: 'T' }) })).toBe('[分享] T')
  })
  it('quoted 限时日常 share shows its label instead of [消息]', () => {
    expect(getRefContent({ content_json: JSON.stringify({ aweType: 805, content_title: '' }) }))
      .toBe('[分享限时日常]')
  })
})

describe('watch-together invite (aweType 9000)', () => {
  const cj = { aweType: 9000, title: '邀你一起看视频', sub_title: '加入和我一起看',
               cover_url: { url_list: ['http://c/card.png'] }, room_id: 123 }
  it('getWatchTogether pulls title/subtitle/cover, null otherwise', () => {
    const wt = getWatchTogether(withCj(cj, { msg_type: 0 }))
    expect(wt).toEqual({ title: '邀你一起看视频', subtitle: '加入和我一起看', cover: 'http://c/card.png' })
    expect(getWatchTogether(msg({ msg_type: 0, content: 'hi' }))).toBeNull()
  })
  it('is shown (not hidden as an empty system message)', () => {
    expect(shouldShow(withCj(cj, { msg_type: 0 }))).toBe(true)
  })
  it('also detected when the JSON is only in content', () => {
    expect(getWatchTogether(msg({ msg_type: 0, content: JSON.stringify(cj) }))).not.toBeNull()
  })
})

describe('renderSystemMsg never leaks raw JSON', () => {
  it('unrecognized JSON card -> empty string, not the raw JSON', () => {
    expect(renderSystemMsg(msg({ msg_type: 0, content: '{"aweType":9000,"title":"x"}' }))).toBe('')
  })
  it('plain text system content still shows', () => {
    expect(renderSystemMsg(msg({ msg_type: 0, content: '你已添加对方为好友' }))).toBe('你已添加对方为好友')
  })
})

describe('misc', () => {
  it('extractServerMsgIds finds 15+ digit ids in raw string', () => {
    const m = msg({ raw_data: 'x server_message_id":123456789012345 y' })
    expect(extractServerMsgIds(m)).toEqual(['123456789012345'])
  })
  it('isRecalled 只认撤回结论，不把「改动时间」当撤回', () => {
    const recalled = msg({
      raw_data: JSON.stringify({ content_json: JSON.stringify({ aweType: 700 }), modify: { kinds: ['recall'] } }),
    })
    expect(isRecalled(recalled)).toBe(true)
    // 旧字段 is_recalled 只是「这条消息被改动过」，撤回 / 编辑 / 表情快捷回复共用它。
    expect(isRecalled(withCj({ is_recalled: true }))).toBe(false)
    expect(isRecalled(msg({ content: 'hi' }))).toBe(false)
  })
})

// 仅看一次 / 撤回 / 编辑 / 表情快捷回复：抖音把它们的时间都写在同一个
// 第 11 号字段上，分得清的是包里的其它字段。抓取端解析完把结论写进
// raw_data.modify（见 common/message_modify.py），前端只读结论。
const SENT_AT = 1750000000
const modifyRow = (modify, fields = {}) => msg({
  timestamp: SENT_AT, msg_type: 1, content: '在吗', sender_uid: 'me',
  raw_data: JSON.stringify({
    is_recalled: (SENT_AT + 17) * 1000,
    content_json: JSON.stringify({ aweType: 700, text: '在吗' }),
    modify,
  }),
  ...fields,
})

describe('仅看一次 / 撤回 / 编辑 / 表情快捷回复 标注', () => {
  it('正文是撤回占位 → 已撤回', () => {
    expect(getModifyKinds(msg({ content: 'Recall Content Hided' }))).toEqual(['recall'])
    expect(MODIFY_KIND_LABELS.recall).toBe('已撤回')
  })
  it('结论说撤回 → 已撤回，并且 isRecalled 为真', () => {
    const m = modifyRow({ kinds: ['recall'], recalled_type: 7, time: 1750000017000 })
    expect(getModifyKinds(m)).toEqual(['recall'])
    expect(isRecalled(m)).toBe(true)
  })
  it('结论说编辑 → 已编辑', () => {
    const m = modifyRow({ kinds: ['edit'], edited: true, editor: 'me', time: 1750000017000 })
    expect(getModifyKinds(m)).toEqual(['edit'])
    expect(isRecalled(m)).toBe(false)
  })
  it('表情快捷回复 → 带出表情文字与回应者', () => {
    const m = modifyRow({
      kinds: ['reaction'],
      reactions: [{ emoji: '[爱心]', uid: '100000000036', time: 1771820351 }],
    })
    expect(getModifyKinds(m)).toEqual(['reaction'])
    expect(getModifyReactions(m)).toEqual([{ emoji: '[爱心]', uid: '100000000036', time: 1771820351 }])
  })
  it('仅看一次 → 仅看一次，不算撤回', () => {
    const m = modifyRow({ kinds: ['view_once'], time: 1750000017000 })
    expect(getModifyKinds(m)).toEqual(['view_once'])
    expect(isRecalled(m)).toBe(false)
  })
  it('type_code=104 时即使没有结论也认得出仅看一次', () => {
    const m = msg({
      msg_type: 1, content: '都不用我撤回了',
      raw_data: JSON.stringify({ type_code: 104, is_recalled: 1778510951356 }),
    })
    expect(isViewOnce(m)).toBe(true)
    expect(getModifyKinds(m)).toEqual(['view_once'])
  })
  it('认不出种类（unknown）时不标注', () => {
    expect(getModifyKinds(modifyRow({ kinds: ['unknown'] }))).toEqual([])
  })
  it('只有改动时间、没有结论的旧数据不标注（不再靠时间猜）', () => {
    const m = msg({
      timestamp: SENT_AT, msg_type: 1, content: '在吗', sender_uid: 'me',
      raw_data: JSON.stringify({
        is_recalled: (SENT_AT + 17) * 1000,
        content_json: JSON.stringify({ aweType: 700, text: '在吗' }),
      }),
    })
    expect(modifyTimestamp(m)).toBe((SENT_AT + 17) * 1000)
    expect(getModifyKinds(m)).toEqual([])
  })
  it('没有改动时间的普通消息不标注', () => {
    expect(getModifyKinds(msg({ content: '在吗' }))).toEqual([])
    expect(modifyTimestamp(msg({ content: '在吗' }))).toBe(0)
  })
  it('老数据里靠正文特征认出的仅看一次也算', () => {
    expect(getModifyKinds(viewOnceRow(viewOncePayloads[0]))).toEqual(['view_once'])
  })
})

// aweType=10401 is dual-purpose: goods cards and "view once" text messages.
// Real payloads from a conversation, stored by an older scraper as msg_type=4.
const viewOncePayloads = ['示例文本一', '示例文本二']
const viewOnceRow = (text, fields = {}) => msg({
  msg_type: 4, content: '{truncated',
  raw_data: JSON.stringify({ is_recalled: 1778510816059, content_json: JSON.stringify({
    text, richTextInfos: [], ai_ext: '{}', scene: '', aweType: 10401,
    related_share_video: {}, mention_users: [],
  }) }),
  ...fields,
})

describe('view once (仅看一次) messages', () => {
  it.each(viewOncePayloads)('detects and reads the body of %s', text => {
    const m = viewOnceRow(text)
    expect(isViewOnce(m)).toBe(true)
    expect(getViewOnceText(m)).toBe(text)
    expect(isRecalled(m)).toBe(false)
  })
  it('keeps goods cards (same aweType, card fields present) as shares', () => {
    const product = withCj({ aweType: 10401, content_title: '商品名称' })
    expect(isViewOnce(product)).toBe(false)
    expect(isJsonShare(withCj({ aweType: 10401, content_title: '商品名称' }))).toBe(true)
  })
  it('never renders a view-once body as a share card', () => {
    for (const text of viewOncePayloads) {
      const m = viewOnceRow(text)
      expect(isJsonShare(m)).toBe(false)
      expect(getShareInfo(m).title).toBe('')
      expect(getShareInfo(m).comment).toBe('')
    }
  })
  it('detects rows that only carry the JSON blob in content', () => {
    const m = msg({ msg_type: 1, content: JSON.stringify({ aweType: 10401, text: viewOncePayloads[0] }) })
    expect(isViewOnce(m)).toBe(true)
    expect(getViewOnceText(m)).toBe(viewOncePayloads[0])
  })
  it('is not view-once without a body of its own', () => {
    expect(isViewOnce(withCj({ aweType: 10401 }, { msg_type: 4 }))).toBe(false)
    expect(isViewOnce(msg({ msg_type: 1, content: '普通消息' }))).toBe(false)
    // A recalled message stays recalled.
    expect(isRecalled(msg({ content: 'Recall Content Hided' }))).toBe(true)
  })
})

// issue #36: names/cards, actor perspective and merged-record detection.
describe('issue 36 message cards and system perspective', () => {
  it('changes like pronouns without altering the referenced video name', () => {
    const m = withCj({ aweType: 126, tips: '{{1}}赞了你分享的 {{2}}', template: [
      { key: 1, name: '对方' }, { key: 2, name: '你和对方的旅行 $&' },
    ] }, { sender_uid: 'alice' })
    expect(renderSystemMsg(m, 'alice')).toBe('你赞了对方分享的 你和对方的旅行 $&')
    expect(renderSystemMsg(m, 'bob')).toBe('对方赞了你分享的 你和对方的旅行 $&')
    expect(renderSystemMsg(m)).toBe('对方赞了你分享的 你和对方的旅行 $&')
  })
  it('changes spark claims in both directions', () => {
    const m = withCj({ aweType: 287, tips: '对方领取了火星 {{0}}', template: [{ key: 0, name: '查看' }] }, { sender_uid: 'alice' })
    expect(renderSystemMsg(m, 'alice')).toBe('你领取了火星 查看')
    expect(renderSystemMsg(m, 'bob')).toBe('对方领取了火星 查看')
  })
  it('does not share parsed content between distinct rows with the same ID', () => {
    const a = withCj({ text: 'old' }, { msg_id: 'same' })
    const b = withCj({ text: 'new' }, { msg_id: 'same' })
    expect(getContentJson(a).text).toBe('old')
    expect(getContentJson(b).text).toBe('new')
  })
})


describe('profile and forwarded cards', () => {
  it('renders old profile rows of every stored type using the complete JSON', () => {
    for (const type of [0, 1, 4]) {
      const m = withCj({ name: '名片', secUID: 'MS4w-test', desc: 'douyin123', follower_count: 0,
        avatar: { url_list: ['https://example.com/avatar.jpg'] } }, { msg_type: type, content: '{"name":' })
      expect(getProfileCard(m)).toEqual({ name: '名片', avatar: 'https://example.com/avatar.jpg', description: 'douyin123', followers: 0, url: 'https://www.douyin.com/user/MS4w-test' })
      expect(shouldShow(m)).toBe(true)
      expect(isSystemMsg(m)).toBe(false)
    }
  })
  it('does not confuse video shares with profiles or malformed summaries with arrays', () => {
    expect(getProfileCard(withCj({ content_name: '作者', secUID: 'abc', itemId: '42' }))).toBeNull()
    const m = withCj({ aweType: 13600, title: '聊天记录', list_content: {}, msg_ids: null }, { msg_type: 0 })
    expect(getForwardInfo(m).preview).toEqual([])
    expect(isSystemMsg(m)).toBe(false)
    expect(shouldShow(m)).toBe(true)
  })
  it('pairs opposite notifications but retains repeated likes and different actors', () => {
    const make = (tips, fields = {}) => withCj({ aweType: 126, tips,
      template: [{ key: 2, name: '视频', extra: { server_message_id: '7600000000000000012' } }],
    }, { sender_uid: 'alice', conv_id: 'c1', timestamp: 100, ...fields })
    const a = make('你赞了{{1}}分享的 {{2}}')
    const b = make('{{1}}赞了你分享的 {{2}}', { timestamp: 115 })
    const c = make('你赞了{{1}}分享的 {{2}}', { timestamp: 116 })
    expect([...duplicateSystemMessageIds([a, b, c])]).toEqual([b.msg_id])
    expect(duplicateSystemMessageIds([a, make('{{1}}赞了你分享的 {{2}}', { sender_uid: 'bob' })]).size).toBe(0)
    expect(duplicateSystemMessageIds([a, make('{{1}}赞了你分享的 {{2}}', { timestamp: 131 })]).size).toBe(0)
  })
})


describe('dynamic and ordinary share layouts', () => {
  it.each(['[分享图文]', '[分享动图]', '[分享视频]'])('reads dynamic title, author and precise ID for %s', prefix => {
    const msg = withCj({ aweType: 11054, item_id: '1000000000000000039',
      im_dynamic_patch: { raw_data: JSON.stringify({
        top_bottom_top: { content: '完整作品标题' },
        top_bottom_content_right: { content: '作者' }, top: { content: 'https://example.com/cover.jpg' },
      }) } }, { content: prefix + '备用标题', msg_type: 4 })
    expect(getShareInfo(msg)).toMatchObject({ title: '完整作品标题', author: '作者', itemId: '1000000000000000039', productUrl: '' })
  })
  it('falls back to a prefixed title and nested ID for incomplete layouts', () => {
    expect(getShareInfo(withCj({ aweme_info: { item_id: '123' }, im_dynamic_patch: { raw_data: '{}' } },
      { content: '[分享动图] 备用标题' }))).toMatchObject({ title: '备用标题', itemId: '123' })
  })
  it('preserves top-level fields when the dynamic layout is malformed', () => {
    expect(getShareInfo(withCj({ item_id: '456', content_name: '作者', content_title: '标题',
      im_dynamic_patch: { raw_data: '{bad' } }))).toMatchObject({ title: '标题', author: '作者', itemId: '456' })
  })
})

describe('leftover media payload display', () => {
  it('treats awe 515/517/519/520 as loose emoji instead of system lines', () => {
    const emoji = withCj({
      aweType: 517,
      url: { url_list: ['http://cdn/e.webp'] },
    }, { msg_type: 0, content: '{"aweType":517}' })
    expect(isLooseEmoji(emoji)).toBe(true)
    expect(getEmojiSrc(emoji)).toBe('http://cdn/e.webp')
    expect(shouldShow(emoji)).toBe(true)
    expect(isSystemMsg(emoji)).toBe(false)
  })

  // 「小火人」（aweType=519）落库时 msg_type=0、content 是「笑死」这类文字，
  // 真正的图在 payload 的 url.url_list 里；以前被当系统提示，界面上只剩一行字。
  it('renders the awe 519 monster emoji as the sticker image', () => {
    const flame = withCj({
      aweType: 519,
      display_name: '笑死',
      image_id: 1010,
      image_type: 'webp',
      sticker_type: 23,
      url: { url_list: ['https://cdn.example.com/emoji/monster.webp'] },
    }, { msg_type: 0, content: '笑死', sender_uid: 'peer' })
    expect(isLooseEmoji(flame)).toBe(true)
    expect(getEmojiSrc(flame)).toBe('https://cdn.example.com/emoji/monster.webp')
    expect(shouldShow(flame)).toBe(true)
    expect(isSystemMsg(flame)).toBe(false)
  })

  it('prefers the downloaded file over the CDN link for a 519 sticker', () => {
    const saved = withCj({
      aweType: 519,
      display_name: '续火花',
      url: { url_list: ['https://cdn/x.webp'] },
    }, { msg_type: 2, content: '续火花', media_local_path: 'emoji/abc.webp' })
    expect(getEmojiSrc(saved)).toBe('/media/emoji/abc.webp')
  })

  it('does not hand out a non-http url as an emoji image', () => {
    const broken = withCj({ aweType: 519, display_name: '打招呼', url: { uri: 'tos-cn/xx' } },
      { msg_type: 0, content: '打招呼' })
    expect(isLooseEmoji(broken)).toBe(true)
    expect(getEmojiSrc(broken)).toBeNull()
  })

  it('renders poi / awe 805 / 2104 as share cards', () => {
    const poi = withCj({
      aweType: 0,
      poi_name: '某咖啡店',
      aweme_poi_id: '123',
      cover_info: { resource_url: { url_list: ['http://cdn/poi.jpg'] } },
    }, { msg_type: 1, content: '{"poi_name":"某咖啡店"}' })
    expect(isLooseShare(poi)).toBe(true)
    expect(getShareInfo(poi).title).toBe('某咖啡店')
    expect(getShareInfo(poi).cover).toBe('http://cdn/poi.jpg')

    const live = withCj({
      aweType: 2104,
      push_detail: '杏仁体',
      cover_url: { url_list: ['http://cdn/live.jpg'] },
    }, { msg_type: 0, content: '杏仁体' })
    expect(isShareCard(live)).toBe(true)
    expect(getShareInfo(live).title).toBe('杏仁体')
    expect(shouldShow(live)).toBe(true)

    const work = withCj({
      aweType: 805,
      content_title: '作品标题',
      cover_url: { url_list: ['http://cdn/w.jpg'] },
      itemId: '99',
    }, { msg_type: 0, content: '{"aweType":805,"content_title":"作品标题"}' })
    expect(isLooseShare(work)).toBe(true)
    expect(getShareInfo(work).itemId).toBe('99')
  })

  it('renders inline_pic leftovers as images instead of raw JSON', () => {
    const pic = withCj({
      aweType: 0,
      inline_pic: 'QQ',
      check_pics: ['tos-x'],
      create_type: 0,
    }, { msg_type: 1, content: '{"inline_pic":"QQ"}' })
    expect(isLooseImage(pic)).toBe(true)
    expect(getImageSrc(pic)).toBe('data:image/webp;base64,QQ')
    expect(shouldShow(pic)).toBe(true)
    expect(isSystemMsg(pic)).toBe(false)
  })
})

// 「限时日常」（aweType=805）是限时日常作品的分享卡片，卡片本身不带标题。
// 之前没有标题就退化成笼统的 [分享]，这里锁定 [分享限时日常] 标注。
describe('限时日常分享 (aweType 805)', () => {
  const daily = () => withCj({
    aweType: 805,
    awemeType: 0,
    content_title: '',
    content_name: '示例作者',
    cover_url: { url_list: ['http://cdn/daily.jpg'] },
    itemId: '1000000000000000040',
    is_hot_spot_video: true,
    is_story: false,
  }, { msg_type: 0, content: '{"aweType": 805, "awemeType": 0, "content_name": ' })

  it('is a share card even from a truncated msg_type=0 row', () => {
    const m = daily()
    expect(isDailyShare(m)).toBe(true)
    expect(isShareCard(m)).toBe(true)
    expect(shouldShow(m)).toBe(true)
    expect(isSystemMsg(m)).toBe(false)
  })

  it('labels the card [分享限时日常] when the payload has no title', () => {
    expect(getShareInfo(daily()).title).toBe('')
    expect(getShareInfo(daily()).author).toBe('示例作者')
    expect(shareCardTitle(daily())).toBe('[分享限时日常]')
  })

  it('keeps a title when the payload has one', () => {
    const titled = withCj({ aweType: 805, content_title: '和 @示例用户 一起 #合拍' },
      { msg_type: 0, content: '{"aweType":805}' })
    expect(shareCardTitle(titled)).toBe('[分享限时日常] 和 @示例用户 一起 #合拍')
  })

  it('leaves a title-less share blank instead of the generic [分享] placeholder', () => {
    const other = withCj({ aweType: 800, itemId: '42' }, { msg_type: 4, content: '{truncated' })
    expect(isDailyShare(other)).toBe(false)
    expect(shareCardTitle(other)).toBe('')
  })
})

// 用户报告的那类消息（消息 id、会话昵称、作品 id 已脱敏）：aweType=800 的分享视频，
// content_title 为空，正文又是老抓取端落库的 "[分享]"。卡片以前标题行写 "[分享]"，
// 和下面的封面 / 作者昵称重复；现在这类没有标题的内容分享一律留白。
describe('没有标题的内容分享（视频/图片/动图/文章/评论）', () => {
  const realVideoShare = () => ({
    msg_id: 'srv_1000000000000000042', msg_type: 4, content: '[分享]',
    media_local_path: null, media_url: null, ref_msg: null, timestamp: 0, sender_uid: 'u',
    raw_data: JSON.stringify({ content_json: JSON.stringify({
      aweType: 800, awemeType: 0, content_title: '', content_name: '示例作者',
      cover_url: { url_list: ['https://example.com/cover.jpeg'] },
      itemId: '1000000000000000041',
    }) }),
  })

  it('hides the label but keeps cover and author', () => {
    const m = realVideoShare()
    expect(isShareCard(m)).toBe(true)
    expect(getShareInfo(m).title).toBe('')
    expect(getShareInfo(m).cover).toBe('https://example.com/cover.jpeg')
    expect(getShareInfo(m).author).toBe('示例作者')
    expect(shareCardTitle(m)).toBe('')
  })

  it.each([
    ['分享视频', { aweType: 11054, itemId: '42' }],
    ['分享图文（图片）', { awemeType: 68, itemId: '42' }],
    ['分享动图', { awemeType: '68', is_live_photo: 1, itemId: '42' }],
    ['分享文章', { awemeType: 163, itemId: '42' }],
    ['分享评论', { aweType: 10500, itemId: '42' }],
  ])('%s 没有标题时留白', (_kind, cj) => {
    expect(shareCardTitle(withCj(cj, { msg_type: 4, content: '[分享]' }))).toBe('')
  })

  it('still shows the title when the card has one', () => {
    expect(shareCardTitle(withCj({ aweType: 800, content_title: '标题' }, { msg_type: 4 }))).toBe('标题')
    expect(shareCardTitle(withCj({ awemeType: 68, content_title: '图文标题' }, { msg_type: 4 }))).toBe('图文标题')
  })
})
