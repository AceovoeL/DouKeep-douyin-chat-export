// @vitest-environment jsdom
/**
 * 「实况」小标（LivePhoto.vue）的行为回归测试。
 *
 * 盯住四件事，任意一件被改掉，手机上的实况图就又"动不起来"或者行为变怪：
 *   1. 小标本身是 <button>，旁边带一颗小三角提示"这里能点"；
 *   2. 点小标 = 播放，而且这次点击**不许再往上冒泡**（冒上去会变成打开/关掉放大查看）；
 *   3. 播完要**自己淡回封面**，不能停在最后一帧；
 *   4. 电脑上原有的"悬停播、移开停"照旧，但"点小标"的那一次鼠标移开也不许被打断。
 *
 * jsdom 没有真正的媒体播放，所以把 play/pause/readyState/ended 换成假的：
 * 这里要验证的是我们自己的状态机，不是浏览器解码。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, h, nextTick } from 'vue'

import LivePhoto from './LivePhoto.vue'

let app = null
let host = null
let parentClicks = 0

function stubMediaElement() {
  const proto = window.HTMLMediaElement.prototype
  const define = (name, get, set) => {
    Object.defineProperty(proto, name, { configurable: true, get, set })
  }
  define('paused', function () { return this.__paused !== false })
  define('ended', function () { return this.__ended === true })
  define('readyState', function () { return 4 })      // 4 = 已经有画面，可以淡入
  define('currentTime',
    function () { return this.__time || 0 },
    function (value) { this.__time = value })
  proto.play = function () { this.__paused = false; this.__ended = false; return Promise.resolve() }
  proto.pause = function () { this.__paused = true }
}

// 像 MessageList 那样把 @click 挂在组件上（Vue 会把它落到根元素），
// 于是"小标那一下有没有冒泡上来"就变成可断言的了。
function mount() {
  host = document.createElement('div')
  document.body.appendChild(host)
  parentClicks = 0
  app = createApp({
    render: () => h(LivePhoto, {
      cover: '/media/images/cover.jpg',
      video: '/media/videos/clip.mp4',
      onClick: () => { parentClicks += 1 },
    }),
  })
  app.mount(host)
}

const root = () => host.querySelector('.live-photo')
const badge = () => host.querySelector('.live-photo-badge')
const video = () => host.querySelector('.live-photo-video')
const playing = () => root().classList.contains('live-photo-playing')

beforeEach(() => {
  stubMediaElement()
  mount()
})

afterEach(() => {
  app.unmount()
  host.remove()
})

describe('实况图的「实况」小标', () => {
  it('是个能点的按钮，字是「实况」，旁边带一颗小三角', () => {
    expect(badge()).toBeTruthy()
    expect(badge().tagName).toBe('BUTTON')
    expect(badge().textContent).toContain('实况')
    expect(badge().querySelector('svg')).toBeTruthy()
    // 没有视频时不该出现这个标（坏掉的实况图只当普通图片看）
    expect(badge().getAttribute('aria-label')).toBe('播放实况图')
  })

  it('点一下就播，而且这一下不会冒泡出去（聊天列表靠它区分"播放"和"放大"）', async () => {
    expect(playing()).toBe(false)
    badge().click()
    await nextTick()
    expect(playing()).toBe(true)
    expect(parentClicks).toBe(0)
    expect(video().paused).toBe(false)
  })

  it('播完自己淡回封面', async () => {
    badge().click()
    await nextTick()
    expect(playing()).toBe(true)
    // 模拟播到结尾
    video().__ended = true
    video().__paused = true
    video().dispatchEvent(new Event('ended'))
    await nextTick()
    expect(playing()).toBe(false)
    expect(video().paused).toBe(true)
  })

  it('电脑上悬停照旧会播、移开就淡回封面', async () => {
    root().dispatchEvent(new Event('mouseenter'))
    await nextTick()
    expect(playing()).toBe(true)
    root().dispatchEvent(new Event('mouseleave'))
    await nextTick()
    expect(playing()).toBe(false)
  })

  it('点小标播的那一次要放完整：鼠标顺手移开也不打断', async () => {
    badge().click()
    await nextTick()
    root().dispatchEvent(new Event('mouseleave'))
    await nextTick()
    expect(playing()).toBe(true)
  })
})
