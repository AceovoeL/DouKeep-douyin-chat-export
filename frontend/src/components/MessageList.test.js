// @vitest-environment jsdom
/**
 * 聊天窗口「时间分隔条」的回归测试（MessageList.vue）。
 *
 * 用户 2026-10-14 的两点要求：
 *   1. 上下两条消息时间差 5 分钟以上，就在两条消息中间、界面正中间补一个
 *      **下面那条**消息的完整时间（年月日时分）；
 *   2. 每条消息下方原来的时间**照旧保留**，并且带上年份。
 *
 * 所以这里盯住四件事：
 *   - 隔够了才有分隔条，没隔够就没有；
 *   - 分隔条在**两条消息中间**，写的是下面那条的时间；
 *   - 系统消息不插（它本身就是居中的一行提示）；
 *   - 每条消息下方的时间还在，且是「2026年10月14日 21:00」这种带年份的完整写法。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'

import MessageList from './MessageList.vue'

// 用 embeddedMessages 喂消息：这条路不发请求，几件要做的事全靠它 —
//   1. 不发 /api/messages，测试就不必把后端整套都 mock 出来；
//   2. 直接用同一份 messages 数组渲染，分隔条算的是哪两条一眼看得见。
let items = []

function mount() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(MessageList, {
      conversation: { conv_id: '0:1:aaa:bbb', name: '测试会话' },
      embeddedMessages: items,
      // 指定「我是谁」，消息才分左右；不分也不影响分隔条，但能顺带验证布局没塌
      selfUidOverride: 'aaa',
    }),
  })
  app.mount(host)
  return { app, host }
}

// 本地时区的一个时刻：本地时间 2026-10-14 21:00
const T0 = Math.floor(new Date(2026, 9, 14, 21, 0).getTime() / 1000)

function textMsg(id, ts, senderUid = 'aaa') {
  return { msg_id: id, msg_type: 1, seq: Number(id.slice(1)), timestamp: ts, sender_uid: senderUid, content: '嗯' }
}

// 系统提示（msg_type=0 + 一句能渲染出来的正文）
function systemMsg(id, ts, senderUid = 'bbb') {
  return {
    msg_id: id, msg_type: 0, seq: Number(id.slice(1)), timestamp: ts, sender_uid: senderUid,
    content: '[系统消息]',
    raw_data: JSON.stringify({ content_ext: { default: { text: '这是一条系统提示' } } }),
  }
}

const dividers = (host) => [...host.querySelectorAll('.msg-time-divider > span')].map(el => el.textContent.trim())
const messageTimes = (host) => [...host.querySelectorAll('.msg-item .msg-time')].map(el => el.textContent.trim())

beforeEach(() => {
  // fetch 只可能是 fetchUserInfo 之类；这里一律给空，别让测试去碰网络。
  global.fetch = vi.fn(async () => ({ ok: false, json: async () => ({}) }))
})

afterEach(() => {
  delete global.fetch
  items = []
})

describe('聊天窗口的时间分隔条', () => {
  it('隔了 5 分钟以上才插，插的是下面那条消息的时间', async () => {
    items = [
      textMsg('m1', T0, 'bbb'),                 // 对方：你好
      textMsg('m2', T0 + 360, 'aaa'),           // 我：你好呀（6 分钟后）
    ]
    const { app, host } = mount()
    await flush()

    expect(dividers(host)).toEqual(['2026年10月14日 21:06'])
    // 分隔条夹在两条消息中间：第一条、分隔条、第二条
    const order = [...host.querySelectorAll('.msg-time-divider, .msg-item')].map(el => el.className.split(' ')[0])
    expect(order).toEqual(['msg-item', 'msg-time-divider', 'msg-item'])
    app.unmount()
    host.remove()
  })

  it('不到 5 分钟不插', async () => {
    items = [textMsg('m1', T0, 'bbb'), textMsg('m2', T0 + 180, 'aaa')]
    const { app, host } = mount()
    await flush()
    expect(dividers(host)).toEqual([])
    app.unmount()
    host.remove()
  })

  it('正好 5 分钟整也算断开', async () => {
    items = [textMsg('m1', T0, 'bbb'), textMsg('m2', T0 + 300, 'aaa')]
    const { app, host } = mount()
    await flush()
    expect(dividers(host)).toEqual(['2026年10月14日 21:05'])
    app.unmount()
    host.remove()
  })

  it('连着断开就每条都插，写各自那条的时间', async () => {
    items = [
      textMsg('m1', T0, 'bbb'),
      textMsg('m2', T0 + 600, 'aaa'),          // +10 分钟
      textMsg('m3', T0 + 1200, 'bbb'),         // 又 +10 分钟
    ]
    const { app, host } = mount()
    await flush()
    expect(dividers(host)).toEqual(['2026年10月14日 21:10', '2026年10月14日 21:20'])
    app.unmount()
    host.remove()
  })

  it('中间夹一条系统提示时，提示前后各自算间隔', async () => {
    // 系统提示本身也占一个位置：提示前断开 → 插提示的时间；提示后断开 → 再插一条。
    items = [
      textMsg('m1', T0, 'bbb'),
      systemMsg('m2', T0 + 600),
      textMsg('m3', T0 + 1200, 'aaa'),
    ]
    const { app, host } = mount()
    await flush()
    expect(dividers(host)).toEqual(['2026年10月14日 21:10', '2026年10月14日 21:20'])
    app.unmount()
    host.remove()
  })

  it('不画的消息（空载荷系统通知）前面不插分隔条', async () => {
    // shouldShow 判它不画：它不该留下一颗孤立的时间。
    const blank = { msg_id: 'm2', msg_type: 0, seq: 2, timestamp: T0 + 600, sender_uid: 'bbb', content: '{}' }
    items = [textMsg('m1', T0, 'bbb'), blank]
    const { app, host } = mount()
    await flush()
    // 只有 m1 会画出来，那颗空载荷不该拖出一颗分隔条
    expect(dividers(host)).toEqual([])
    expect(host.querySelectorAll('.msg-item').length).toBe(1)
    app.unmount()
    host.remove()
  })

  it('每条消息下方的时间还在，而且是带年份的完整写法', async () => {
    items = [textMsg('m1', T0, 'bbb'), textMsg('m2', T0 + 360, 'aaa')]
    const { app, host } = mount()
    await flush()
    expect(messageTimes(host)).toEqual(['2026年10月14日 21:00', '2026年10月14日 21:06'])
    app.unmount()
    host.remove()
  })
})

// 等 fetch 的 promise 链 + Vue 渲染都跑完。
async function flush() {
  await nextTick()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}


// ── 「彩蛋福袋」卡（aweType=110360 · card_key=msg_easter_redpack）────────────────
// 结构照真实样本：content_json 里嵌 im_dynamic_patch，raw_data 是再次编码的排版 JSON。
// 图片和账号只用假值；卡片上的两行字是固定的，不随载荷文案变。
function easterMsg(id, ts, senderUid = 'bbb') {
  const contentJson = {
    aweType: 110360,
    description: '[彩蛋福袋]',
    im_dynamic_patch: {
      card_key: 'msg_easter_redpack',
      card_type: 'pay_msg_easter_redpack_card',
      raw_data: JSON.stringify({
        top: { cover_image: 'https://p26-infras.byteimg.com/static-assets/demo.png' },
        bottom: { content: '彩蛋福袋' },
        content_content: { content: '已领取' },
      }),
    },
  }
  return {
    msg_id: id,
    msg_type: 0,
    seq: Number(id.slice(1)),
    timestamp: ts,
    sender_uid: senderUid,
    content: '[彩蛋福袋]',
    raw_data: JSON.stringify({ content_json: JSON.stringify(contentJson) }),
    media_local_path: 'card_icons/demo.png',
  }
}

describe('聊天窗口的彩蛋福袋卡', () => {
  it('按发送者分左右，两行文字固定写「已过期」「彩蛋福袋」', async () => {
    items = [easterMsg('m1', T0, 'bbb')]
    const { app, host } = mount()
    await flush()

    const item = host.querySelector('.msg-item')
    const card = host.querySelector('.msg-easter-card')
    expect(card).not.toBeNull()
    // 不是居中的系统提示（否则它会落在 .msg-system 里）
    expect(item.classList.contains('msg-system')).toBe(false)
    expect(item.classList.contains('msg-self')).toBe(false)
    // 载荷里写的是「已领取」，卡片上一律改成「已过期」
    expect(card.querySelector('.msg-easter-status').textContent.trim()).toBe('已过期')
    expect(card.querySelector('.msg-easter-name').textContent.trim()).toBe('彩蛋福袋')
    // 采集时存下的本地图优先（不发网络请求）
    expect(card.querySelector('.msg-easter-cover').getAttribute('src'))
      .toBe('/media/card_icons/demo.png')
    // 商品卡的兜底判据很宽，别把这族卡片截成一个空商品壳
    expect(host.querySelector('.msg-goods-card')).toBeNull()
    app.unmount()
    host.remove()
  })

  it('自己发的卡片贴右边', async () => {
    items = [easterMsg('m1', T0, 'aaa')]
    const { app, host } = mount()
    await flush()
    expect(host.querySelector('.msg-item').classList.contains('msg-self')).toBe(true)
    expect(host.querySelector('.msg-easter-card')).not.toBeNull()
    app.unmount()
    host.remove()
  })
})
