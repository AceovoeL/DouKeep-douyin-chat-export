// @vitest-environment jsdom
/**
 * 「图片与视频」里实况图筛选的回归测试（SearchBar.vue）。
 *
 * 用户 2026-10-13 的要求，盯住三件事：
 *   1. 上方多出一个「实况图」勾选框，跟「视频」「图片」并列；
 *   2. 勾选的组合要正确翻译成后端的 media_type（只勾实况图 → live）；
 *   3. 列表里的实况图要带左上角「实况」小标，但**列表里悬停不许自动播放**
 *      —— 那儿根本不该有 <video> 元素。只有点了它、左侧聊天窗口跳到那条原文之后，
 *      在聊天窗口里悬停才会播（那边用的是 LivePhoto.vue）。
 *
 * 所以这里的第 3 条是有意"反向"断言的：将来谁想在列表里也加悬停预览，
 * 这条测试会拦住他。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'

import SearchBar from './SearchBar.vue'

let app = null
let host = null
let requests = []

// 后端返回什么由这个变量决定；默认给一条实况图 + 一条普通图片 + 一条视频。
let searchItems = []

function mount() {
  host = document.createElement('div')
  document.body.appendChild(host)
  app = createApp({
    render: () => h(SearchBar, { convId: 'c1', convName: '测试会话' }),
  })
  app.mount(host)
}

// 点标题栏里的那个「图片与视频」入口。
async function openMediaMode() {
  const button = [...host.querySelectorAll('.search-shortcuts button')]
    .find(el => el.textContent.includes('图片与视频'))
  button.click()
  await flush()
}

// 等 fetch 的 promise 链 + Vue 渲染都跑完。
async function flush() {
  await nextTick()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

function liveItem(overrides = {}) {
  return {
    msg_id: 'live-1', msg_type: 3, seq: 30, timestamp: 1000,
    media_local_path: 'images/live.webp', live_video_path: 'videos/live.mp4',
    ...overrides,
  }
}

function plainImage(overrides = {}) {
  return { msg_id: 'img-1', msg_type: 3, seq: 20, timestamp: 900, media_local_path: 'images/a.jpg', ...overrides }
}

function videoItem(overrides = {}) {
  return { msg_id: 'vid-1', msg_type: 5, seq: 10, timestamp: 800, media_local_path: 'videos/a.mp4', ...overrides }
}

function isLive(item) { return !!(item.live_video_path && item.live_video_path.length) }
function isVideo(item) { return item.msg_type === 5 }

// 假后端：按 media_type 真的筛一遍，这样"前端传对了档位"才是可验证的
// （只回一堆固定数据的话，测试自己就把错误掩盖了）。
function backendItems(mediaType) {
  if (mediaType === 'live') return searchItems.filter(isLive)
  if (mediaType === 'image') return searchItems.filter(i => i.msg_type === 3 && !isLive(i) && !isVideo(i))
  if (mediaType === 'video') return searchItems.filter(isVideo)
  return searchItems
}

beforeEach(() => {
  requests = []
  searchItems = [liveItem(), plainImage(), videoItem()]
  global.fetch = vi.fn(async (url) => {
    requests.push(String(url))
    const mediaType = new URL(String(url), 'http://localhost').searchParams.get('media_type')
    const items = backendItems(mediaType)
    return {
      ok: true,
      json: async () => ({ items, total: items.length, total_capped: false }),
    }
  })
  mount()
})

afterEach(() => {
  app.unmount()
  host.remove()
  delete global.fetch
})

const checkboxes = () => [...host.querySelectorAll('.media-filters input')]
const checkboxByLabel = (label) => {
  const el = [...host.querySelectorAll('.media-filters label')]
    .find(n => n.textContent.includes(label))
  return el?.querySelector('input')
}
const lastRequest = () => requests.at(-1) || ''
const tiles = () => [...host.querySelectorAll('.media-tile')]

describe('实况图勾选框', () => {
  it('「图片与视频」上方有视频、图片、实况图三个勾选框，默认都勾上', async () => {
    await openMediaMode()
    const labels = [...host.querySelectorAll('.media-filters label')].map(n => n.textContent.trim())
    expect(labels).toEqual(['视频', '图片', '实况图'])
    expect(checkboxes().every(box => box.checked)).toBe(true)
    // 三个都勾 = 全部，走后端原有的 media 档。
    expect(lastRequest()).toContain('media_type=media')
  })

  it('只勾实况图时向后端要 live 这一档', async () => {
    await openMediaMode()
    for (const label of ['视频', '图片']) {
      const box = checkboxByLabel(label)
      box.checked = false
      box.dispatchEvent(new Event('change'))
    }
    await flush()
    expect(lastRequest()).toContain('media_type=live')
    // 后端已经筛准了，本地不该再过滤掉什么。
    expect(tiles().length).toBe(1)
    expect(host.querySelector('.media-live-badge')).toBeTruthy()
  })

  it('三个框都不勾时不发请求，并提示要选点什么', async () => {
    await openMediaMode()
    for (const label of ['视频', '图片', '实况图']) {
      const box = checkboxByLabel(label)
      box.checked = false
      box.dispatchEvent(new Event('change'))
      await flush()
    }
    // 前两次取消勾选还是会去问后端（那时还剩别的档）；第三次三个框全空了，
    // 就不该再问后端 —— 记录最后一次取消勾选前后请求数有没有变。
    const countAfterAllUnchecked = requests.length
    const liveBox = checkboxByLabel('实况图')
    liveBox.checked = true
    liveBox.dispatchEvent(new Event('change'))
    await flush()
    const afterRecheck = requests.length
    expect(afterRecheck).toBe(countAfterAllUnchecked + 1)
    liveBox.checked = false
    liveBox.dispatchEvent(new Event('change'))
    await flush()
    expect(requests.length).toBe(countAfterAllUnchecked + 1)
    expect(host.textContent).toContain('请选择图片、视频或实况图')
    expect(tiles().length).toBe(0)
  })

  it('「图片 + 实况图」这种混合勾选：后端取宽的，本地把视频滤掉', async () => {
    await openMediaMode()
    const box = checkboxByLabel('视频')
    box.checked = false
    box.dispatchEvent(new Event('change'))
    await flush()
    // 后端没有"图片 + 实况图"这一档，只能取 media 再本地滤。
    expect(lastRequest()).toContain('media_type=media')
    const ids = tiles().map(el => el.querySelector('img')?.src || '')
    expect(ids.length).toBe(2)
    // 视频那张不该出现。缩略图走 getVideoPoster，没有 poster 时是占位文字。
    expect(tiles().some(el => el.querySelector('.media-video-badge'))).toBe(false)
  })
})

describe('列表里的「实况」小标', () => {
  it('实况图带左上角「实况」小标，普通图片和视频没有', async () => {
    await openMediaMode()
    const badges = host.querySelectorAll('.media-live-badge')
    expect(badges.length).toBe(1)
    expect(badges[0].textContent).toContain('实况')
    // 小三角（提示"能播"）
    expect(badges[0].querySelector('svg')).toBeTruthy()
  })

  it('列表里悬停不会播放：那儿根本没有 video 元素', async () => {
    await openMediaMode()
    // 整个搜索面板里一个 video 都不该有 —— 有就说明有人把 LivePhoto.vue 塞进来了。
    expect(host.querySelectorAll('video').length).toBe(0)
    // 小标是个普通 span，不是按钮、也点不动（点了应该还是"跳转"那条路）。
    const badge = host.querySelector('.media-live-badge')
    expect(badge.tagName).toBe('SPAN')
    expect(badge.hasAttribute('aria-label')).toBe(false)
  })

  it('点实况图那张是跳转到原文，走的是 navigate 那条路', async () => {
    const emitted = []
    app.unmount()
    host.remove()
    host = document.createElement('div')
    document.body.appendChild(host)
    app = createApp({
      render: () => h(SearchBar, {
        convId: 'c1', convName: '测试会话',
        onNavigate: item => emitted.push(item),
      }),
    })
    app.mount(host)
    await openMediaMode()
    // 实况图在正序列表的最后（seq 最大），找带小标的那张。
    const tile = [...tiles()].find(el => el.querySelector('.media-live-badge'))
    tile.click()
    await flush()
    expect(emitted.length).toBe(1)
    expect(emitted[0].msg_id).toBe('live-1')
    // 跳过去要带着 seq，聊天窗口靠它滚到那条消息；小视频地址也在，跳过去就能播。
    expect(emitted[0].seq).toBe(30)
    expect(emitted[0].live_video_path).toBe('videos/live.mp4')
  })
})
