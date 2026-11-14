// @vitest-environment jsdom
/**
 * 聊天窗口工具栏「数据统计截止至 …」的回归测试（App.vue）。
 *
 * 用户 2026-11-14 的要求：点进某个人的会话后，「数据洞察」按钮右边要显示一行
 * 「数据统计截止至 xxx」，写法和规则跟弹窗顶部那行完全一致 ——
 * 只认采集时记下的 collected_at，没有记录就整行不显示。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'

const TS = Math.floor(new Date(2026, 10, 14, 16, 51, 38).getTime() / 1000)

let conversations = []

function ok(body) {
  return { ok: true, status: 200, json: async () => body, clone() { return this } }
}

// App.vue 在模块加载时会把 window.fetch 包一层（为了自动带登录 token），
// 所以必须先放好 fetch / matchMedia 的替身，再动态 import 组件。
vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
vi.stubGlobal('fetch', vi.fn(async (url) => {
  const target = String(url)
  if (target.startsWith('/api/auth/check')) return ok({ need_password: false, authenticated: true })
  if (target.startsWith('/api/appearance')) return ok({})
  if (target.startsWith('/api/conversations?')) return ok({ items: conversations })
  // 消息等接口在测试里不需要真数据，直接给个失败响应，页面自己会显示占位
  return { ok: false, status: 404, json: async () => ({}), clone() { return this } }
}))

const { default: App } = await import('./App.vue')

const mounted = []

function mount() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(App)
  app.mount(host)
  mounted.push(app)
  return { app, host }
}

async function flush(times = 4) {
  for (let i = 0; i < times; i++) {
    await nextTick()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

async function mountWithConversations(convList) {
  conversations = convList
  const { app, host } = mount()
  await flush()
  for (let i = 0; i < 10 && !host.querySelector('.conv-item'); i++) await flush(1)
  return { app, host }
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
})

afterEach(() => {
  while (mounted.length) mounted.pop().unmount()
  document.body.innerHTML = ''
})

describe('工具栏的「数据统计截止至」', () => {
  it('点进带采集时间的会话后，在「数据洞察」按钮右边显示一行', async () => {
    const { host } = await mountWithConversations([
      { conv_id: 'c1', name: '测试会话', collected_at: TS },
    ])
    expect(host.querySelector('.toolbar-cutoff')).toBeNull()
    host.querySelector('.conv-item').click()
    await flush()
    const cutoff = host.querySelector('.toolbar-cutoff')
    expect(cutoff).not.toBeNull()
    expect(cutoff.textContent.trim()).toBe('数据统计截止至 2026年11月14日 16:51:38')
    // 位置在「数据洞察」按钮的右边（同一个工具栏里、紧跟在按钮后面）
    const button = host.querySelector('.insights-toggle')
    expect(button.nextElementSibling).toBe(cutoff)
  })

  it('会话没有采集记录时整行不显示', async () => {
    const { host } = await mountWithConversations([
      { conv_id: 'c2', name: '老会话', collected_at: 0 },
    ])
    host.querySelector('.conv-item').click()
    await flush()
    expect(host.querySelector('.toolbar-cutoff')).toBeNull()
  })
})
