import { describe, it, expect } from 'vitest'
import { getFireInviteCard, isFireInviteCard, clearFireInviteCardCache } from './fireInviteCard.js'
import { isGoodsCard } from './goodsCard.js'
import { isShareCard } from './douyinMessage.js'

const SPARK = 'https://p11.douyinpic.com/obj/im-resource/1734337175292-ts-626f736f6d313231362e706e67?biz_tag=aweme_im'
// 兄弟火花的图标（同一个 card_type，只是这一张图不同）—— 用来盯住「别认死姐妹那张」。
const BROTHER_SPARK = 'https://p11.douyinpic.com/obj/im-resource/1734337200000-ts-62726f74686572.png?biz_tag=aweme_im'
const OTHER_SPARK = 'https://p11.douyinpic.com/obj/im-resource/1734337175292-ts-joint1216.png?biz_tag=aweme_im'
const LEFT = 'https://p3.douyinpic.com/img/aweme-avatar/example-left~c5_168x168.webp'
const RIGHT = 'https://p3.douyinpic.com/img/aweme-avatar/example-right~c5_168x168.webp'

// 两段卡片结构取自真实样本（邀请 / 接受纪念），文字与头像地址换成一眼可辨的假值。
// content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
function cardMsg({ kind = 'invite', status = '1', layout = true, senderIsSelf = false,
                   spark = SPARK, cardType, cardKey = 'msg_fire_invitation',
                   relationType = 'bosom' } = {}) {
  const self = '1000000000000001'
  const peer = '1000000000000002'
  const raw = kind === 'invite'
    ? {
      top: { content: spark, type: 'im-image', ui_info: { height: 84 } },
      content_top: {
        'content@guest': '邀请你更改为姐妹火花',
        'content@sender': '邀请更改为姐妹火花',
        type: 'im-text',
      },
      content_content: {
        content: [
          { content: '更改后标识将会替换', type: 'im-text' },
          { 'content@dark': 'selectArrowDark', type: 'im-icon' },
        ],
        type: 'im-graphic-text',
      },
      bottom: {
        'content@1&guest': '接受邀请',
        'content@1&sender': '待接受',
        'content@2': '已接受',
        'content@3': '已失效',
        type: 'im-button',
      },
      whole_card: { extra_info: { status } },
    }
    : {
      top: { content: [LEFT, RIGHT], type: 'im-avatar' },
      'top@1': {
        content: { flame_icon: spark, left: LEFT, right: RIGHT, relation_type: relationType },
        type: 'im-avatar-animax',
      },
      content_top: { 'content@guest': '更改为姐妹火花', 'content@sender': '更改为姐妹火花', type: 'im-text' },
      content_content: { content: '我们开始拥有姐妹火花啦', type: 'im-text' },
      bottom: { content: '纪念一下', type: 'im-button' },
      whole_card: { extra_info: { status } },
    }
  const contentJson = {
    aweType: kind === 'invite' ? 110170 : 110277,
    description: '姐妹火花',
    push_detail: '姐妹火花',
    im_dynamic_patch: {
      card_key: cardKey,
      card_type: cardType || (kind === 'invite'
        ? 'im_msg_fire_invitation_send'
        : 'im_msg_fire_invitation_accept_memory'),
      raw_data: layout ? JSON.stringify(raw) : '',
    },
  }
  return {
    msg_id: 'srv_7000000000000000001',
    msg_type: 0,
    content: '',
    conv_id: `0:1:${self}:${peer}`,
    sender_uid: senderIsSelf ? self : peer,
    raw_data: JSON.stringify({ content_json: JSON.stringify(contentJson) }),
  }
}

const SELF = '1000000000000001'

describe('「聊天火花」关系卡', () => {
  it('邀请卡：收件人看到「接受邀请」，副标题带箭头，火花用载荷里那张', () => {
    clearFireInviteCardCache()
    const card = getFireInviteCard(cardMsg(), SELF)
    expect(card.kind).toBe('invite')
    expect(card.title).toBe('邀请你更改为姐妹火花')
    expect(card.subtitle).toBe('更改后标识将会替换')
    expect(card.arrow).toBe(true)
    expect(card.button).toBe('接受邀请')
    // 我的邀请还没被处理 → 这颗按钮是主色；背景图里没有画按钮，交给界面画
    expect(card.buttonPrimary).toBe(true)
    expect(card.buttonBakedIn).toBe(false)
    // 火花的图**一律**来自载荷（走缓存），不用仓库里任何一张本地图
    expect(card.icon).toBe(SPARK)
  })

  it('邀请卡：我自己看是「邀请更改为姐妹火花」，按钮文字变「待接受」且不再是主色', () => {
    clearFireInviteCardCache()
    const card = getFireInviteCard(cardMsg({ senderIsSelf: true }), SELF)
    expect(card.title).toBe('邀请更改为姐妹火花')
    expect(card.button).toBe('待接受')
    expect(card.buttonPrimary).toBe(false)
  })

  it('邀请卡：已接受 / 已失效只看状态号，不分人称', () => {
    clearFireInviteCardCache()
    for (const [status, text] of [['2', '已接受'], ['3', '已失效']]) {
      const card = getFireInviteCard(cardMsg({ status }), SELF)
      expect(card.button).toBe(text)
      expect(card.buttonPrimary).toBe(false)
    }
  })

  it('接受纪念卡：两个头像夹一颗火花，按钮已画进背景图', () => {
    clearFireInviteCardCache()
    const card = getFireInviteCard(cardMsg({ kind: 'accept', status: '2' }), SELF)
    expect(card.kind).toBe('accept')
    expect(card.title).toBe('更改为姐妹火花')
    expect(card.subtitle).toBe('我们开始拥有姐妹火花啦')
    expect(card.arrow).toBe(false)
    expect(card.button).toBe('纪念一下')
    expect(card.buttonBakedIn).toBe(true)
    expect(card.icon).toBe(SPARK)
    // 左 = 发卡的人、右 = 对方
    expect(card.leftUid).toBe('1000000000000002')
    expect(card.rightUid).toBe('1000000000000001')
    expect(card.leftAvatar).toBe(LEFT)
    expect(card.rightAvatar).toBe(RIGHT)
  })

  // 姐妹 / 兄弟 / 伴侣 / 死党火花**共用同一套 card_type**，区分它们的只有那颗图。
  // 所以认卡片不能看图标是什么花，也不能把图标换成仓库里的固定一张。
  it('兄弟火花：同一个 card_type、只是图标不同，照样认，且用载荷里那张图', () => {
    clearFireInviteCardCache()
    const card = getFireInviteCard(cardMsg({ spark: BROTHER_SPARK }), SELF)
    expect(card).not.toBeNull()
    expect(card.icon).toBe(BROTHER_SPARK)
  })

  it('接受纪念卡也照同样规矩走载荷图标', () => {
    clearFireInviteCardCache()
    const card = getFireInviteCard(cardMsg({ kind: 'accept', status: '2', spark: BROTHER_SPARK }), SELF)
    expect(card.icon).toBe(BROTHER_SPARK)
  })

  it('这是另一族卡（合养小火人），不认领', () => {
    clearFireInviteCardCache()
    const joint = cardMsg({ cardType: 'im_msg_fire_invitation_joint_send', spark: OTHER_SPARK })
    expect(getFireInviteCard(joint, SELF)).toBeNull()
  })

  it('别的分享卡片返回 null（商品卡 / 视频卡 / 豆包卡）', () => {
    clearFireInviteCardCache()
    expect(getFireInviteCard(null)).toBeNull()
    expect(isFireInviteCard(cardMsg({ cardKey: 'msg_big_product', cardType: 'msg_big_product' }))).toBe(false)
    expect(isFireInviteCard(cardMsg({ cardKey: 'video_share', cardType: 'video_share' }))).toBe(false)
  })

  it('排版缺失时返回 null，不会画出一张空白卡', () => {
    clearFireInviteCardCache()
    expect(getFireInviteCard(cardMsg({ layout: false }), SELF)).toBeNull()
  })

  it('副标题是组件列表时只取 im-text，不会渲染出 [object Object]', () => {
    clearFireInviteCardCache()
    const msg = cardMsg()
    const parsed = JSON.parse(msg.raw_data)
    const cj = JSON.parse(parsed.content_json)
    const layout = JSON.parse(cj.im_dynamic_patch.raw_data)
    layout.content_content = {
      type: 'im-component-list',
      content: [{ type: 'im-text', content: '更改后标识将会替换' }, { type: 'im-icon', content: 'arrow' }],
    }
    cj.im_dynamic_patch.raw_data = JSON.stringify(layout)
    msg.raw_data = JSON.stringify({ content_json: JSON.stringify(cj) })
    const card = getFireInviteCard(msg, SELF)
    expect(card.subtitle).toBe('更改后标识将会替换')
    expect(card.arrow).toBe(true)
  })

  it('搜索结果预览固定用收件人说法', () => {
    clearFireInviteCardCache()
    const card = getFireInviteCard(cardMsg({ senderIsSelf: true }), SELF)
    expect(card.title).toBe('邀请更改为姐妹火花')
    expect(card.preview).toBe('邀请你更改为姐妹火花')
  })

  it('同一条消息换视角看会重新解析（缓存键带 selfUid）', () => {
    clearFireInviteCardCache()
    const msg = cardMsg()
    expect(getFireInviteCard(msg, SELF).button).toBe('接受邀请')
    expect(getFireInviteCard(msg, '1000000000000002').button).toBe('待接受')
    expect(getFireInviteCard(msg, SELF).button).toBe('接受邀请')
  })

  // 模板里这几支是 v-else-if 串起来的，谁先命中谁画。这张卡的载荷同时带着
  // im_dynamic_patch，所以商品卡 / 分享卡的兜底判据也会"认领"它 —— 这条卡在消息流里
  // 必须排在它们前面，否则会被画成一个「[商品]」空壳（见 MessageList.vue 的注释）。
  it('这张卡同时会被商品卡、分享卡的宽判据认领（所以模板里必须排在它们前面）', () => {
    clearFireInviteCardCache()
    const msg = cardMsg()
    // 这两条就是"顺序不能乱"的依据：它们都为 true，靠模板里的先后决定谁生效。
    expect(isShareCard(msg)).toBe(true)
    expect(isGoodsCard(msg)).toBe(true)
    // 而这张卡自己认得更准（有卡片外观、文案、按钮），所以应当优先。
    expect(isFireInviteCard(msg)).toBe(true)
  })
})
