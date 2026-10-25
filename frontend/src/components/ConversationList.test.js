// @vitest-environment jsdom
/**
 * 会话列表「正在加载」和「暂无会话数据」必须是两种样子（ConversationList.vue）。
 *
 * 用户 2026-10-25 反馈：抓取跑完之后第一次打开聊天查看器，列表显示
 * 「暂无会话数据」，刷新好几遍或者等很久才出数据，
 * 之后再打开就正常。
 *
 * 原因是那个请求要等后端把每个会话的显示条数算一遍（几十秒），而组件里没有
 * 加载态 —— `conversations` 初值是空数组，「还没回来」就画成了「一条都没有」。
 * 后端现在已经不等它了（见 tests/test_display_count_lazy.py），但前端这一层
 * 也必须自己说清楚「正在加载」，否则同样的误会还会在别的慢请求上重演。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h } from 'vue'

import ConversationList from './ConversationList.vue'

let app = null
let host = null

function mount() {
  host = document.createElement('div')
  document.body.appendChild(host)
  app = createApp({
    render: () => h(ConversationList, { activeId: null }),
  })
  app.mount(host)
}

// 让 fetch 的 promise 链 + Vue 渲染都跑完。
async function flush(times = 4) {
  for (let i = 0; i < times; i += 1) {
    await Promise.resolve()
    await new Promise(resolve => setTimeout(resolve, 0))
  }
}

function text() {
  return host.textContent.replace(/\s+/g, ' ')
}

beforeEach(() => {
  vi.useRealTimers()
})

afterEach(() => {
  app?.unmount()
  host?.remove()
  app = null
  host = null
  vi.restoreAllMocks()
})

describe('会话列表的加载态', () => {
  it('请求还没回来时显示「正在加载」，不显示「暂无会话数据」', async () => {
    let release
    const pending = new Promise(resolve => { release = resolve })
    vi.stubGlobal('fetch', vi.fn(() => pending))

    mount()
    await flush(1)

    expect(text()).toContain('正在加载会话')
    expect(text()).not.toContain('暂无会话数据')

    release({ ok: true, json: async () => ({ items: [], total: 0 }) })
    await flush()
  })

  it('确实一个会话都没有时，才显示「暂无会话数据」', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ items: [], total: 0, display_count_pending: 0 }),
    })))

    mount()
    await flush()

    expect(text()).toContain('暂无会话数据')
    expect(text()).not.toContain('正在加载会话')
  })

  it('有数据时正常画列表，不出现空状态', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({
        items: [
          { conv_id: 'c1', name: '小明', message_count: 10, display_count: 8 },
          { conv_id: 'c2', name: '小红', message_count: 3, display_count: 3 },
        ],
        total: 2,
      }),
    })))

    mount()
    await flush()

    expect(text()).toContain('小明')
    expect(text()).toContain('8 条消息')
    expect(text()).not.toContain('暂无会话数据')
  })

  it('请求失败时给出错误和重试，而不是谎称没有会话', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error('网络断了'))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ items: [{ conv_id: 'c1', name: '小明', message_count: 1 }], total: 1 }),
      })
    vi.stubGlobal('fetch', fetchMock)

    mount()
    await flush()

    expect(text()).toContain('会话列表加载失败')
    expect(text()).not.toContain('暂无会话数据')

    // 点「重试」应该重新请求，并把列表画出来。
    const retry = [...host.querySelectorAll('button')]
      .find(button => button.textContent.trim() === '重试')
    expect(retry).toBeTruthy()
    retry.click()
    await flush()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(text()).toContain('小明')
  })
})
