// @vitest-environment jsdom
/**
 * 数据洞察弹窗最上方的「数据统计截止至 …」与火花天数（用户 2026-11-14 的要求）。
 * 这两项来自会话对象，不依赖统计接口，所以即使统计还在加载也应该看得见。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'

import InsightsModal from './InsightsModal.vue'

const TS = Math.floor(new Date(2026, 10, 14, 16, 51, 38).getTime() / 1000)

function mount(extra = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(InsightsModal, {
      open: true,
      convId: 'c1',
      convName: '测试会话',
      collectedAt: TS,
      sparkDays: 101,
      sparkIcon: 'spark_icons/blue_normal.png',
      ...extra,
    }),
  })
  app.mount(host)
  return { app, host }
}

async function flush() {
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true,
    json: async () => ({
      heatmap: null, talkers: [], emojis: [], friendship: null, keywords: [], meta: {},
    }),
  }))
  vi.stubGlobal('matchMedia', () => ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  }))
})

afterEach(() => {
  delete global.fetch
  vi.unstubAllGlobals()
})

describe('数据洞察顶部的截止时间与火花', () => {
  it('最上方显示「数据统计截止至 …」和火花天数', async () => {
    const { app, host } = mount()
    await flush()
    const text = host.textContent.replace(/\s+/g, ' ')
    expect(text).toContain('数据统计截止至 2026年11月14日 16:51:38')
    expect(text).toContain('火花天数')
    expect(text).toContain('101')
    app.unmount()
    host.remove()
  })

  it('没有火花、没有时间时整条不显示', async () => {
    const { app, host } = mount({ collectedAt: 0, sparkDays: 0 })
    await flush()
    expect(host.querySelector('.insights-meta')).toBeNull()
    app.unmount()
    host.remove()
  })

  it('只有火花、没有采集时间时不显示截止时间', async () => {
    const { app, host } = mount({ collectedAt: 0, sparkDays: 7 })
    await flush()
    const text = host.textContent.replace(/\s+/g, ' ')
    expect(text).not.toContain('数据统计截止至')
    expect(text).toContain('火花天数')
    app.unmount()
    host.remove()
  })
})
