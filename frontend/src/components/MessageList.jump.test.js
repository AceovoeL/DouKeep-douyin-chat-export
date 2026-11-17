// @vitest-environment jsdom
/**
 * 聊天窗口「跳到某条消息」的回归测试（MessageList.vue）。
 *
 * 用户 2026-11-17 反馈：在搜索结果里点一条消息，界面一直转圈、跳不过去；
 * 而点「↑ 最早消息」这种按钮很快。原因有两处：
 *   1. 点搜索结果时，App 会同时换会话对象并设 jumpToSeq；「补齐会话详情」回来
 *      后又把会话对象换成新的，MessageList 的会话 watcher 因此再跑一次，把正在
 *      进行的跳转请求顶成了过期请求；
 *   2. 过期请求早退时没人把 loading 关掉，也没人再发起新请求 —— 于是一直转。
 *
 * 这里盯住三件事：
 *   - 跳转从「目标前 50 条」开始取（after_seq），目标会被高亮；
 *   - 跳转途中会话对象又被替换一次，也一定会补发新请求，转圈收得起来；
 *   - 旧请求晚到不会把新请求的转圈关掉、也不会把界面固定在转圈状态。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'

import MessageList from './MessageList.vue'

async function flush() {
  await nextTick()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

// 一页消息：seq 连续，方便断言「目标有没有被加载进来」。
function msgPage(from, to) {
  const items = []
  for (let seq = from; seq <= to; seq++) {
    items.push({
      msg_id: `m${seq}`, msg_type: 1, seq, timestamp: 1700000000 + seq,
      sender_uid: 'bbb', content: '嗯',
    })
  }
  return { items, total: 2000, has_older: true, has_newer: true }
}

// 所有 /messages 请求都先挂起，由测试决定什么时候、以什么顺序放行。
let calls = []

function installFetch() {
  calls = []
  global.fetch = vi.fn((url) => {
    if (url.includes('/senders')) return Promise.resolve({ ok: true, json: async () => [] })
    if (url.includes('/messages?')) {
      let settle
      const promise = new Promise((resolve) => { settle = resolve })
      calls.push({ url, settle })
      return promise
    }
    return Promise.resolve({ ok: false, json: async () => ({}) })
  })
}

async function resolveCall(index, payload) {
  calls[index].settle({ ok: true, json: async () => payload })
  await flush()
}

function mount(state) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const props = reactive(state)
  const app = createApp({ render: () => h(MessageList, props) })
  app.mount(host)
  return { app, host, props }
}

beforeEach(() => {
  installFetch()
  // jsdom 没实现 scrollIntoView，定位那一步会调它
  Element.prototype.scrollIntoView = vi.fn()
  // 列表进场动画会问「用户有没有关掉动效」，jsdom 里没有 matchMedia
  window.matchMedia = vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
})

afterEach(() => {
  delete global.fetch
  delete Element.prototype.scrollIntoView
  delete window.matchMedia
  calls = []
})

describe('从搜索结果跳到某条消息', () => {
  it('从目标前 50 条开始加载，并高亮目标消息', async () => {
    const jumped = vi.fn()
    const { app, host, props } = mount({
      conversation: { conv_id: 'a', name: '测试会话' },
      jumpToSeq: null,
      onJumped: jumped,
    })
    await flush()
    expect(calls.length).toBe(1)                 // 初次加载最新一页
    await resolveCall(0, msgPage(900, 999))

    // 跟 App.vue 里点搜索结果一样：换会话对象 + 给跳转目标
    props.conversation = { conv_id: 'a', name: '测试会话' }
    props.jumpToSeq = 150
    await flush()
    expect(calls.length).toBe(2)
    expect(calls[1].url).toContain('after_seq=100')   // 150 - 50

    await resolveCall(1, msgPage(101, 200))
    expect(jumped).toHaveBeenCalledTimes(1)
    expect(host.querySelector('.msg-highlight')).not.toBeNull()

    app.unmount()
    host.remove()
  })

  it('跳转途中会话对象又被替换，会补发请求、转圈能收起来', async () => {
    const jumped = vi.fn()
    const { app, host, props } = mount({
      conversation: { conv_id: 'a', name: '测试会话' },
      jumpToSeq: null,
      onJumped: jumped,
    })
    await flush()
    await resolveCall(0, msgPage(900, 999))

    props.conversation = { conv_id: 'a', name: '测试会话' }
    props.jumpToSeq = 150
    await flush()
    expect(calls.length).toBe(2)                 // 跳转请求挂起中

    // 「补齐会话详情」回来，又换了一次对象。旧实现会在这里把跳转请求作废，
    // 自己却不发新请求，界面就一直转圈。
    props.conversation = { conv_id: 'a', name: '测试会话' }
    await flush()
    expect(calls.length).toBe(3)
    expect(calls[2].url).toContain('after_seq=100')

    await resolveCall(1, msgPage(101, 200))      // 旧请求晚到
    await resolveCall(2, msgPage(101, 200))      // 新请求收尾
    expect(host.querySelector('.msg-loading')).toBeNull()
    expect(jumped).toHaveBeenCalled()

    app.unmount()
    host.remove()
  })
})
