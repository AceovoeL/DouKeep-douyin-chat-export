import { describe, it, expect } from 'vitest'
import { iconSrc, resolveAvatarUrl } from './media.js'

const INVITE_ICON = 'http://p3-aweme-im-img.byteimg.com/tos-cn-i-example/group-icon.webp'
// 用户报的那条豆包卡封面（真链接，带签名参数）
const COVER = 'http://p26-sign.douyinpic.com/large/tos-cn-i-example/demo-cover.jpeg?lk3s=138a59ce&x-expires=1781013600&x-signature=1frPMEUL%2BEuHBSTaVue2XUrPwM4%3D&from=327834062_large&s=PackSourceEnum_FEED&se=false&sc=cover&biz_tag=aweme_video&l=20260526224605C571133BC44DED8B0679'

describe('卡片图地址（iconSrc）', () => {
  it('换成 /media/card_icons/<哈希><扩展名>?url=… 并带上原链接', () => {
    // 这两个哈希要和 Python 端 common/card_icons.py 的 fnv1a_32() 一致，
    // 否则「采集时存下的图」阅读端找不到，会白拉一次网。
    expect(iconSrc(INVITE_ICON)).toBe(`/media/card_icons/e5e49455.webp?url=${encodeURIComponent(INVITE_ICON)}`)
    expect(iconSrc(COVER)).toBe(`/media/card_icons/15f52813.jpg?url=${encodeURIComponent(COVER)}`)
  })

  it('哈希只看链接本身：同一个链接两次算出来一样', () => {
    expect(iconSrc(COVER)).toBe(iconSrc(COVER))
    expect(iconSrc(COVER)).not.toBe(iconSrc(COVER + '&x=1'))
  })

  it('非抖音图床的链接一律拒绝（这个地址会让后端去请求它）', () => {
    expect(iconSrc('http://127.0.0.1:8000/api/stats')).toBe('')
    expect(iconSrc('http://localhost/x.webp')).toBe('')
    expect(iconSrc('http://evil.example.com/x.webp')).toBe('')
    expect(iconSrc('https://douyinpic.com.evil.com/x.webp')).toBe('')
    expect(iconSrc('')).toBe('')
    expect(iconSrc(null)).toBe('')
    expect(iconSrc('tos-cn-i-xxx/yyy.webp')).toBe('')
  })

  it('认抖音的几个图床域（含子域）', () => {
    expect(iconSrc('https://p3-sign.douyinpic.com/obj/x.webp')).toContain('/media/card_icons/')
    expect(iconSrc('https://p3-aweme-im-img.byteimg.com/x.webp~tplv-a.webp')).toContain('/media/card_icons/')
    expect(iconSrc('https://p3-webcast.douyinpic.com/img/x.image?biz_tag=a')).toContain('.webp?url=')
  })

  it('头像地址照旧（本地 avatars/ 走 /media，外链直连）', () => {
    expect(resolveAvatarUrl('avatars/1.webp')).toBe('/media/avatars/1.webp')
    expect(resolveAvatarUrl('https://x/y.jpg')).toBe('https://x/y.jpg')
    expect(resolveAvatarUrl('users/1.webp')).toBeNull()
    expect(resolveAvatarUrl(null)).toBeNull()
  })
})
