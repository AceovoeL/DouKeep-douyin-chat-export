import { describe, it, expect } from 'vitest'
import { getFireBabyCard, isFireBabyCard, clearFireBabyCardCache } from './fireBabyCard.js'
import { isFireInviteCard } from './fireInviteCard.js'
import { isGoodsCard, getGoodsCard } from './goodsCard.js'
import { isShareCard } from './douyinMessage.js'

// 三段卡片结构取自真实样本（邀请 / 开始合养 / 纪念），文字就照抖音给的原文。
// content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
const SELF = '1000000000000001'
const PEER = '1000000000000002'

function cardMsg({ kind = 'request', status = '1', layout = true, senderIsSelf = false,
                   cardType, cardKey = 'msg_fire_invitation',
                   title, subtitle } = {}) {
  const raw = kind === 'request'
    ? {
      top: { content: 'https://p3.douyinpic.com/obj/im-resource/petelf.png', type: 'im-image' },
      content_top: {
        'content@guest': title || '对方邀请你一起养小火人',
        'content@sender': '邀请对方一起养小火人',
        type: 'im-text',
      },
      content_content: {
        content: [
          { content: '查看合养小火人', type: 'im-text' },
          { 'content@dark': 'selectArrowDark', type: 'im-icon' },
        ],
        type: 'im-graphic-text',
      },
      bottom: {
        'content@1&guest': '接受邀请',
        'content@1&sender': '待对方接受',
        'content@2': '已接受',
        'content@3': '邀请已失效',
        'content@4': '暂不支持合养',
        type: 'im-button',
      },
      whole_card: { extra_info: { status, business_type: 'elf_invitation_card' } },
    }
    : {
      top: { content: 'https://p3.douyinpic.com/obj/im-resource/petelf.png', type: 'im-image' },
      content_top: { content: title || '我们开始养小火人啦', type: 'im-text' },
      content_content: {
        content: subtitle || (kind === 'accept'
          ? '连续聊天让精灵快快长大'
          : '今天是我们拥有小火人的第一天'),
        type: 'im-text',
      },
      bottom: {
        content: kind === 'accept' ? '去看看' : '纪念一下',
        type: 'im-button',
        ui_info: { background_color: 'Primary', color: 'ConstTextInverse' },
      },
      whole_card: { extra_info: { status } },
    }
  const contentJson = {
    aweType: kind === 'request' ? 110182 : (kind === 'accept' ? 110183 : 110279),
    description: kind === 'request' ? '邀请你一起养小火人' : '我们开始养小火人啦',
    push_detail: '邀请你一起养小火人',
    im_dynamic_patch: {
      card_key: cardKey,
      card_type: cardType || (kind === 'request'
        ? 'im_msg_fire_invitation_joint_send'
        : (kind === 'accept'
          ? 'im_msg_fire_invitation_joint_accept'
          : 'im_msg_fire_invitation_joint_accept_memory')),
      raw_data: layout ? JSON.stringify(raw) : '',
    },
  }
  return {
    msg_id: 'srv_7000000000000000002',
    msg_type: 0,
    content: kind === 'request' ? '邀请你一起养小火人' : '我们开始养小火人啦',
    conv_id: `0:1:${SELF}:${PEER}`,
    sender_uid: senderIsSelf ? SELF : PEER,
    raw_data: JSON.stringify({ content_json: JSON.stringify(contentJson) }),
  }
}

describe('「合养小火人」卡片', () => {
  it('邀请卡：对方发的看收件人说法，按钮「接受邀请」是主色、文字要自己画', () => {
    clearFireBabyCardCache()
    const card = getFireBabyCard(cardMsg(), SELF)
    expect(card.kind).toBe('request')
    expect(card.title).toBe('对方邀请你一起养小火人')
    expect(card.subtitle).toBe('查看合养小火人')
    expect(card.arrow).toBe(true)
    expect(card.button).toBe('接受邀请')
    expect(card.buttonPrimary).toBe(true)
    // 空白底纹 → 标题、副标题、按钮都由界面画
    expect(card.textBakedIn).toBe(false)
  })

  it('邀请卡：自己发的那条换人称，按钮变灰的「待对方接受」', () => {
    clearFireBabyCardCache()
    const card = getFireBabyCard(cardMsg({ senderIsSelf: true }), SELF)
    expect(card.title).toBe('邀请对方一起养小火人')
    expect(card.button).toBe('待对方接受')
    expect(card.buttonPrimary).toBe(false)
  })

  it('邀请卡：状态 2/3/4 只看状态号，不分人称', () => {
    clearFireBabyCardCache()
    for (const [status, text] of [['2', '已接受'], ['3', '邀请已失效'], ['4', '暂不支持合养']]) {
      const card = getFireBabyCard(cardMsg({ status }), SELF)
      expect(card.button).toBe(text)
      expect(card.buttonPrimary).toBe(false)
    }
  })

  it('开始合养卡：文案和素材图一致 → 只补按钮，文字不重画', () => {
    clearFireBabyCardCache()
    const card = getFireBabyCard(cardMsg({ kind: 'accept' }), SELF)
    expect(card.kind).toBe('accept')
    expect(card.title).toBe('我们开始养小火人啦')
    expect(card.subtitle).toBe('连续聊天让精灵快快长大')
    expect(card.arrow).toBe(false)
    expect(card.button).toBe('去看看')
    expect(card.buttonPrimary).toBe(true)
    expect(card.textBakedIn).toBe(true)
  })

  it('开始合养卡万一文案对不上素材图，就退回空白底纹自己画（免得露出错的话）', () => {
    clearFireBabyCardCache()
    const card = getFireBabyCard(cardMsg({ kind: 'accept', subtitle: '今天是我们拥有小火人的第一天' }), SELF)
    expect(card.button).toBe('去看看')
    expect(card.textBakedIn).toBe(false)
  })

  it('纪念卡：文案和素材图不同，自己画，按钮是「纪念一下」', () => {
    clearFireBabyCardCache()
    const card = getFireBabyCard(cardMsg({ kind: 'memory' }), SELF)
    expect(card.kind).toBe('memory')
    expect(card.title).toBe('我们开始养小火人啦')
    expect(card.subtitle).toBe('今天是我们拥有小火人的第一天')
    expect(card.button).toBe('纪念一下')
    expect(card.buttonPrimary).toBe(true)
    expect(card.textBakedIn).toBe(false)
  })

  it('别的家族不认领：聊天火花、喂精灵、商品卡各归各的', () => {
    clearFireBabyCardCache()
    const spark = cardMsg({ cardType: 'im_msg_fire_invitation_send' })
    expect(getFireBabyCard(spark, SELF)).toBeNull()
    expect(isFireInviteCard(spark)).toBe(true)

    const feeding = cardMsg({ cardType: 'im_msg_fire_invitation_feeding' })
    expect(getFireBabyCard(feeding, SELF)).toBeNull()

    expect(getFireBabyCard(null)).toBeNull()
    expect(isFireBabyCard(cardMsg({ cardKey: 'msg_big_product', cardType: 'msg_big_product' }))).toBe(false)
  })

  it('排版缺失时返回 null，不会画出一张空白卡', () => {
    clearFireBabyCardCache()
    expect(getFireBabyCard(cardMsg({ layout: false }), SELF)).toBeNull()
  })

  it('同一条消息换视角看会重新解析（缓存键带 selfUid）', () => {
    clearFireBabyCardCache()
    const msg = cardMsg()
    expect(getFireBabyCard(msg, SELF).button).toBe('接受邀请')
    expect(getFireBabyCard(msg, PEER).button).toBe('待对方接受')
    expect(getFireBabyCard(msg, SELF).button).toBe('接受邀请')
  })

  it('搜索结果预览固定用收件人说法', () => {
    clearFireBabyCardCache()
    expect(getFireBabyCard(cardMsg({ senderIsSelf: true }), SELF).preview)
      .toBe('对方邀请你一起养小火人')
  })

  // 模板里这几支是 v-else-if 串起来的，谁先命中谁画。这张卡的载荷同时带着
  // im_dynamic_patch，所以商品卡 / 分享卡的兜底判据也会「认领」它 —— 这条卡在消息流里
  // 必须排在它们前面，否则会被画成一个「[商品]」空壳（见 MessageList.vue 的注释）。
  it('这张卡同时会被商品卡、分享卡的宽判据认领（所以模板里必须排在它们前面）', () => {
    clearFireBabyCardCache()
    const msg = cardMsg()
    expect(isShareCard(msg)).toBe(true)
    expect(isGoodsCard(msg)).toBe(true)
    // 商品卡那支只会画出一个「[商品]」空壳（卡片载荷里没有商品标题，它就退到兜底文字），
    // 这正是「必须排它前面」的原因。
    expect(getGoodsCard(msg).title).toBe('[商品]')
    expect(isFireBabyCard(msg)).toBe(true)
  })
})
