import { afterEach, describe, expect, it } from 'vitest'
import { applyAppearance, fontStack, toCssVars } from './appearance'

const FONTS = [
  { id: 'system', labelZh: '系统默认', labelEn: 'System default', stack: 'system-stack' },
  { id: 'yahei', labelZh: '微软雅黑', labelEn: 'Microsoft YaHei', stack: 'yahei-stack' },
  { id: 'mono', labelZh: '等宽', labelEn: 'Monospace', stack: 'mono-stack' },
]

function payload(overrides = {}) {
  return {
    fonts: FONTS,
    appearance: {
      fonts: { ui: 'system', viewer: 'yahei' },
      viewer: {
        theme: 'dark',
        colors: {
          bgPrimary: '#101010',
          bgMessageSelf: '#ff0000',
          bgMessageSelfEnd: '#ff0000',
          accent: '#00ff00',
        },
        layout: {
          messageFontSize: 16, lineHeight: 1.8, bubbleMaxWidthPct: 60, avatarSize: 40,
          emojiMaxSize: 120, imageMaxSize: 300, videoMaxSize: 420,
          emojiScale: 1.2,
          letterSpacing: 0.5, emojiOffsetX: -0.25, emojiOffsetY: 0.4,
        },
        ...overrides,
      },
    },
  }
}

describe('toCssVars', () => {
  it('maps sizes/fonts even when a built-in theme is active', () => {
    const { theme, vars } = toCssVars(payload())
    expect(theme).toBe('dark')
    expect(vars['--msg-font-size']).toBe('16px')
    expect(vars['--msg-line-height']).toBe('1.8')
    expect(vars['--msg-max-width']).toBe('60%')
    expect(vars['--msg-avatar-size']).toBe('40px')
    expect(vars['--media-max-size']).toBe('300px')
    // 文字式表情大小是相对字号的倍数（em）
    expect(vars['--emoji-size']).toBe('1.2em')
    // 表情包 / 图片 / 视频 三档各自独立
    expect(vars['--emoji-max-size']).toBe('120px')
    expect(vars['--video-max-size']).toBe('420px')
    expect(vars['--font-viewer']).toBe('yahei-stack')
    // 气泡文字的字间距（px，0 就是原样）
    expect(vars['--msg-letter-spacing']).toBe('0.5px')
    // 文字式表情的偏移：相对字号的倍数（em），0 就是原来的基线位置
    expect(vars['--emoji-offset-x']).toBe('-0.25em')
    expect(vars['--emoji-offset-y']).toBe('0.4em')
    // 已经砍掉的选项不再产生变量，交给 style.css 里的默认值
    expect(vars['--msg-group-gap']).toBeUndefined()
    expect(vars['--scrollbar-thumb']).toBeUndefined()
    expect(vars['--accent-hover']).toBeUndefined()
  })

  it('does not override colors unless the theme is custom', () => {
    expect(toCssVars(payload()).vars['--bg-primary']).toBeUndefined()
    const custom = toCssVars(payload({ theme: 'custom' })).vars
    expect(custom['--bg-primary']).toBe('#101010')
    expect(custom['--accent']).toBe('#00ff00')
    // 起止色相同 -> 纯色；不同 -> 渐变
    expect(custom['--bg-message-self']).toBe('#ff0000')
  })

  it('builds a gradient when the two bubble colors differ', () => {
    const vars = toCssVars(payload({
      theme: 'custom',
      colors: { bgMessageSelf: '#111111', bgMessageSelfEnd: '#222222' },
    })).vars
    expect(vars['--bg-message-self']).toBe('linear-gradient(180deg, #111111 0%, #222222 100%)')
  })

  it('lets an explicit theme override beat the server theme', () => {
    const { theme, vars } = toCssVars(payload({ theme: 'custom' }), { theme: 'light' })
    expect(theme).toBe('light')
    expect(vars['--bg-primary']).toBeUndefined()
    // 尺寸仍然生效
    expect(vars['--msg-font-size']).toBe('16px')
  })

  it('survives a missing payload', () => {
    const { theme, vars } = toCssVars(null)
    expect(theme).toBe('dark')
    expect(vars).toEqual({})
  })
})

describe('fontStack', () => {
  it('resolves presets, local families and unknown values', () => {
    expect(fontStack('yahei', { fonts: FONTS })).toBe('yahei-stack')
    expect(fontStack('family:My Font', { fonts: FONTS })).toBe("'My Font', system-stack")
    expect(fontStack('family:Bad\\Name', { fonts: FONTS })).toBe("'BadName', system-stack")
    expect(fontStack('nope', { fonts: FONTS })).toBe('system-stack')
  })
})

describe('applyAppearance', () => {
  const original = globalThis.document
  afterEach(() => { globalThis.document = original })

  function stubRoot() {
    const props = new Map()
    const attrs = {}
    globalThis.document = {
      documentElement: {
        style: {
          setProperty: (key, value) => props.set(key, value),
          removeProperty: (key) => props.delete(key),
        },
        setAttribute: (key, value) => { attrs[key] = value },
      },
    }
    return { props, attrs }
  }

  it('writes the variables and syncs data-theme', () => {
    const root = stubRoot()
    const theme = applyAppearance(payload({ theme: 'custom' }))
    expect(theme).toBe('custom')
    expect(root.attrs['data-theme']).toBe('custom')
    expect(root.props.get('--msg-font-size')).toBe('16px')
    expect(root.props.get('--bg-primary')).toBe('#101010')
  })

  it('uses an empty data-theme for the default dark theme and clears stale colors', () => {
    const root = stubRoot()
    applyAppearance(payload({ theme: 'custom' }))
    applyAppearance(payload({ theme: 'dark' }))
    expect(root.attrs['data-theme']).toBe('')
    expect(root.props.has('--bg-primary')).toBe(false)
    expect(root.props.has('--accent')).toBe(false)
  })
})
