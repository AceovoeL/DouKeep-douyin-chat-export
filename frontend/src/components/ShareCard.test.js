// @vitest-environment jsdom
/**
 * 分享卡（ShareCard.vue）的外观回归测试。
 *
 * 用户给出的抖音官方参考图里：
 *   · 视频 / 图文 / 动图 / 文章 → 封面 + 正中一颗白色播放三角、左下角作者
 *     头像和昵称，封面下方再向下延伸一条同底色的标题（最多两行）；
 *   · 分享评论（aweType=10500）→ 一行灰字「分享 @某人 的评论」+ 评论内容，
 *     一条浅灰分割线，下面是方形缩略图 + 「来自视频」+ 两行标题。
 * 这几条都是"看图才看得出来"的要求，这里用 DOM 断言把它们钉住。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { createApp, h, nextTick } from 'vue'

import ShareCard from './ShareCard.vue'

let app = null
let host = null
let opened = 0

function mount(cj, extra = {}, props = {}) {
  host = document.createElement('div')
  document.body.appendChild(host)
  opened = 0
  const message = {
    msg_id: 'm1',
    msg_type: 4,
    content: '',
    raw_data: JSON.stringify({ content_json: JSON.stringify({ ...cj, ...extra }) }),
  }
  app = createApp({
    render: () => h(ShareCard, { message, ...props, onOpen: () => { opened += 1 } }),
  })
  app.mount(host)
}

afterEach(() => {
  app?.unmount()
  host?.remove()
})

const q = selector => host.querySelector(selector)

describe('普通分享视频 / 图文卡', () => {
  const video = {
    aweType: 800,
    content_title: '示例视频标题',
    content_name: '示例作者',
    itemId: '1000000000000000001',
    cover_width: 576,
    cover_height: 1128,
    cover_url: { url_list: ['https://example.com/cover.jpeg'] },
    content_thumb: { url_list: ['https://example.com/avatar.jpeg'] },
  }

  it('封面下方延伸出标题条，标题显示在封面之下', () => {
    mount(video)
    expect(q('.msg-share-card')).toBeTruthy()
    expect(q('.msg-share-cover-img').getAttribute('src')).toBe('https://example.com/cover.jpeg')
    expect(q('.msg-share-play svg')).toBeTruthy()
    expect(q('.msg-share-title').textContent).toBe('示例视频标题')
    // 两行截断放在内层文字上，外层的内边距不会漏出第三行
    expect(q('.msg-share-title-text').textContent).toBe('示例视频标题')
    // 标题条是封面的下一个兄弟节点，不能盖在封面上
    const order = [...q('.msg-share-card').children].map(el => el.className)
    expect(order.findIndex(c => c.includes('msg-share-cover')))
      .toBeLessThan(order.findIndex(c => c.includes('msg-share-title')))
    // 悬停提示仍然保留完整标题
    expect(q('.msg-share-card').getAttribute('title')).toBe('示例视频标题')
  })

  it('标题超过两行时由内层文字截断（CSS line-clamp）', () => {
    mount({ ...video, content_title: '很长的标题'.repeat(20) })
    const text = q('.msg-share-title-text')
    expect(text).toBeTruthy()
    // 不在 JS 里砍字，交给 CSS 显示省略号
    expect(text.textContent).toBe('很长的标题'.repeat(20))
  })

  it('没有标题时不留下空的标题条', () => {
    mount({ ...video, content_title: '' })
    expect(q('.msg-share-title')).toBeNull()
  })

  it('封面比例按载荷里的宽高来，竖屏夹到 3:4、横屏夹到 16:10', () => {
    mount(video)
    // jsdom 会把行内样式写成 "0.75 / 1"，取数字比较
    expect(parseFloat(q('.msg-share-cover').style.aspectRatio)).toBe(0.75)
    app.unmount()
    host.remove()
    mount({ ...video, cover_width: 1920, cover_height: 1080 })
    expect(parseFloat(q('.msg-share-cover').style.aspectRatio)).toBe(1.6)
  })

  it('左下角是作者头像 + 昵称', () => {
    mount(video)
    expect(q('.msg-share-author-avatar').getAttribute('src')).toBe('https://example.com/avatar.jpeg')
    expect(q('.msg-share-author-name').textContent).toBe('示例作者')
  })

  it('点整张卡会通知外层打开抖音官方链接', () => {
    mount(video)
    q('.msg-share-card').click()
    expect(opened).toBe(1)
    expect(q('.msg-share-card').getAttribute('role')).toBe('link')
  })

  it('没有头像时用昵称首字兜底', () => {
    mount({ ...video, content_thumb: undefined })
    expect(q('img.msg-share-author-avatar')).toBeNull()
    expect(q('.msg-share-author-fallback').textContent).toBe('示')
  })

  it('封面加载失败时藏起图片、露出「封面已失效」', async () => {
    mount(video)
    const img = q('.msg-share-cover-img')
    img.dispatchEvent(new Event('error'))
    await nextTick()
    expect(img.style.display).toBe('none')
    expect(q('.msg-share-cover-empty').textContent.trim()).toBe('封面已失效')
  })
})

describe('分享评论卡（aweType=10500）', () => {
  const comment = {
    aweType: 10500,
    comment_user_name: '示例评论者',
    comment: '示例评论内容',
    aweme_title: '示例视频标题',
    media_type: 1,
    itemId: '1000000000000000002',
    cover_url: { url_list: ['https://example.com/cover.jpeg'] },
    content_thumb: { url_list: ['https://example.com/commenter-avatar.jpeg'] },
  }

  it('顶部是灰字「分享 @某人 的评论」+ 评论正文，不画评论者头像', () => {
    mount(comment)
    expect(q('.msg-share-comment-user').textContent).toBe('@示例评论者')
    expect(q('.msg-share-comment-label').textContent).toContain('的评论')
    expect(q('.msg-share-comment-text').textContent).toBe('示例评论内容')
    expect(q('.msg-share-comment-avatar')).toBeNull()
  })

  it('分割线下面是方形缩略图 + 「来自视频」+ 两行标题', () => {
    mount(comment)
    expect(q('.msg-share-divider')).toBeTruthy()
    expect(q('.msg-share-quote-cover').getAttribute('src')).toBe('https://example.com/cover.jpeg')
    expect(q('.msg-share-quote-from').textContent).toBe('来自视频')
    expect(q('.msg-share-quote-play')).toBeTruthy()
    expect(q('.msg-share-quote-title').textContent).toBe('示例视频标题')
    // 评论卡下半部分不重复作者条
    expect(q('.msg-share-author')).toBeNull()
    // 分割线、缩略图、标题的先后顺序
    const card = q('.msg-share-card')
    const order = [...card.children].map(el => el.className)
    expect(order.findIndex(c => c.includes('msg-share-divider')))
      .toBeLessThan(order.findIndex(c => c.includes('msg-share-quote')))
  })

  it('分享评论带气泡配置时，整张卡使用气泡装饰', () => {
    mount(comment, {}, {
      bubble: {
        image: '/media/bubbles/1234567890123456789/self.png',
        display: { width: 66, height: 54 },
        slice: { top: 79.6, right: 39.4, bottom: 18.5, left: 59.1 },
        border: { top: 42.98, right: 26, bottom: 9.99, left: 39.01 },
        padding: { top: 19.01, right: 10.03, bottom: 11.02, left: 32.01 },
        textColor: '#533000',
        offsetX: 0,
        offsetY: 0,
        mirrored: false,
      },
    })
    const card = q('.msg-share-card')
    expect(card.classList.contains('msg-share-card--decorated')).toBe(true)
    expect(card.style.getPropertyValue('--bubble-image')).toBe(
      'url("/media/bubbles/1234567890123456789/self.png")',
    )
    expect(card.style.getPropertyValue('--bubble-slice')).toBe(
      '79.6% 39.4% 18.5% 59.1% fill',
    )
  })

  it('普通分享卡不会因为传了 bubble 就套装饰', () => {
    mount({ aweType: 800, content_title: '示例视频' }, {}, { bubble: { image: '/media/x.png' } })
    expect(q('.msg-share-card').classList.contains('msg-share-card--decorated')).toBe(false)
  })

  it('media_type=2 写「来自图文」，且不画播放三角', () => {
    mount({ ...comment, media_type: 2 })
    expect(q('.msg-share-quote-from').textContent).toBe('来自图文')
    expect(q('.msg-share-quote-play')).toBeNull()
  })

  it('评论图靠左对齐，不被 column flex 拉满整张卡', () => {
    // 评论图在 .msg-share-comment（column flex）里：默认 align-items: stretch
    // 会把 <img> 的盒子撑满卡宽，图本身再由 object-fit: contain 居中，看起来
    // 就是“图片居中”。jsdom 不做布局，测不到这一点，只能盯住源码里那条约束。
    // jsdom 环境里 import.meta.url 是 http:// 而不是 file://，所以用 vitest 的
    // 工作目录（frontend/）拼路径。
    const source = readFileSync('src/components/ShareCard.vue', 'utf8')
    const block = source.match(/\.msg-share-comment-img\s*\{([\s\S]*?)\}/)?.[1] || ''
    expect(block).toContain('align-self: flex-start')
  })
})

describe('没有封面的分享（地点 / 直播推送等）', () => {
  it('退回紧凑的标题 + 作者，不留下空白封面块', () => {
    mount({ aweType: 800, content_title: '示例地点', content_name: '示例作者' })
    expect(q('.msg-share-cover')).toBeNull()
    expect(q('.msg-share-plain-title').textContent).toBe('示例地点')
    expect(q('.msg-share-plain-author').textContent).toBe('@ 示例作者')
  })
})
