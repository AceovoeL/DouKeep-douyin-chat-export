import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  loadBubbleCatalog, resetBubbleCatalogCache, resolveBubble, bubbleCssVars,
} from './bubbleConfig.js'

const ID = '1234567890123456789'

function variant(overrides = {}) {
  return {
    image: '/media/bubbles/' + ID + '/self.png',
    display: { width: 66, height: 54 },
    slice: { top: 79.6, right: 39.4, bottom: 18.5, left: 59.1 },
    border: { top: 42.98, right: 26, bottom: 9.99, left: 39.01 },
    padding: { top: 19.01, right: 10.03, bottom: 11.02, left: 32.01 },
    ...overrides,
  }
}

function catalog(overrides = {}) {
  return {
    [ID]: {
      id: ID,
      text_color: '#ffffff',
      offset: { x: 0, y: 0 },
      self: variant(),
      guest: variant({
        image: '/media/bubbles/' + ID + '/guest.png',
        slice: { top: 79.6, right: 59.1, bottom: 18.5, left: 39.4 },
        border: { top: 42.98, right: 39.01, bottom: 9.99, left: 26 },
        padding: { top: 19.01, right: 32.01, bottom: 11.02, left: 10.03 },
      }),
      ...overrides,
    },
  }
}

describe('气泡配置解析', () => {
  it('己方用 self，对方用 guest', () => {
    const data = catalog()
    expect(resolveBubble(data, ID, true).image).toContain('/self.png')
    expect(resolveBubble(data, ID, true).padding.left).toBe(32.01)
    expect(resolveBubble(data, ID, false).image).toContain('/guest.png')
    expect(resolveBubble(data, ID, false).padding.left).toBe(10.03)
  })

  it('缺一侧图时用另一侧镜像，padding 左右互换', () => {
    const data = catalog({ guest: null })
    const mirrored = resolveBubble(data, ID, false)
    expect(mirrored.mirrored).toBe(true)
    expect(mirrored.image).toContain('/self.png')
    expect(mirrored.padding.left).toBe(10.03)
    expect(mirrored.padding.right).toBe(32.01)
    // 切片和边框不换，背景层由 CSS scaleX(-1) 翻转
    expect(mirrored.slice.left).toBe(59.1)
  })

  it('缺 ID、缺图、数字不合法时返回 null', () => {
    expect(resolveBubble(catalog(), '', true)).toBeNull()
    expect(resolveBubble(catalog(), '999', true)).toBeNull()
    expect(resolveBubble(catalog({ self: null, guest: null }), ID, true)).toBeNull()
    expect(resolveBubble(catalog({ self: variant({ display: { width: 0, height: 54 } }), guest: null }), ID, true)).toBeNull()
  })

  it('采集端存的相对路径会自动补上 /media 前缀', () => {
    const data = catalog({ self: variant({ image: 'bubbles/' + ID + '/self.png' }) })
    expect(resolveBubble(data, ID, true).image).toBe('/media/bubbles/' + ID + '/self.png')
  })

  it('生成 border-image 需要的 CSS 变量', () => {
    const vars = bubbleCssVars(resolveBubble(catalog(), ID, true))
    expect(vars['--bubble-image']).toBe('url("/media/bubbles/' + ID + '/self.png")')
    expect(vars['--bubble-slice']).toBe('79.6% 39.4% 18.5% 59.1% fill')
    expect(vars['--bubble-border-width']).toBe('42.98px 26px 9.99px 39.01px')
    expect(vars['--bubble-pad']).toBe('19.01px 10.03px 11.02px 32.01px')
    expect(vars['--bubble-min-width']).toBe('66px')
    expect(vars['--bubble-min-height']).toBe('54px')
    expect(vars['--bubble-text-color']).toBe('#ffffff')
  })

  it('配置请求失败时返回空表，下一次还能重试', async () => {
    resetBubbleCatalogCache()
    const failing = vi.fn().mockRejectedValue(new Error('offline'))
    expect(await loadBubbleCatalog(failing)).toEqual({})
    const ok = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ version: 1, bubbles: catalog() }),
    })
    const loaded = await loadBubbleCatalog(ok)
    expect(loaded[ID].text_color).toBe('#ffffff')
    resetBubbleCatalogCache()
  })
})
