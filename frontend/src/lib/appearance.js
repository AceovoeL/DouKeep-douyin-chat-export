/**
 * 外观设置（主题 / 字体 / 尺寸）→ CSS 变量的映射。
 *
 * 默认值不在这里再写一份：面板和查看器都用后端 `GET /api/appearance` 返回的
 * `defaults`，所以「单项恢复默认」永远和后端同一套规则。
 *
 * 关键规则：
 * - 颜色只在主题是 `custom` 时才覆盖，选内置主题时让 style.css 里的主题块生效；
 * - 字体、尺寸、卡片宽度任何时候都生效（它们和配色是两回事）。
 */

export const THEME_IDS = ['dark', 'wechat', 'light', 'warm', 'purple', 'custom']

// 自定义配色 → CSS 变量
const COLOR_VARS = {
  bgPrimary: '--bg-primary',
  bgSecondary: '--bg-secondary',
  bgTertiary: '--bg-tertiary',
  bgMessageOther: '--bg-message-other',
  textOnSelf: '--text-on-self',
  textPrimary: '--text-primary',
  textSecondary: '--text-secondary',
  textMuted: '--text-muted',
  borderColor: '--border-color',
  accent: '--accent',
  highlight: '--highlight',
  systemText: '--system-text-color',
  systemBg: '--system-bg',
  cardBg: '--card-bg',
  cardBorder: '--card-border',
}

// 尺寸/间距 → CSS 变量（px 后缀在这里统一补上）
const SIZE_VARS = {
  messageFontSize: '--msg-font-size',
  listFontSize: '--conv-name-size',
  bubbleRadius: '--bubble-radius',
  bubblePadX: '--bubble-pad-x',
  bubblePadY: '--bubble-pad-y',
  avatarSize: '--msg-avatar-size',
  messageGap: '--msg-gap',
  sidebarWidth: '--sidebar-width',
  emojiMaxSize: '--emoji-max-size',
  imageMaxSize: '--media-max-size',
  videoMaxSize: '--video-max-size',
  letterSpacing: '--msg-letter-spacing',
}

// 无单位数值
const UNITLESS_VARS = {
  lineHeight: '--msg-line-height',
}

// 相对字号的无单位倍数（写进 CSS 时带 em，1 就是"跟所在位置的文字一样大"）
const EM_VARS = {
  emojiScale: '--emoji-size',
  emojiOffsetX: '--emoji-offset-x',
  emojiOffsetY: '--emoji-offset-y',
}

const PERCENT_VARS = {
  bubbleMaxWidthPct: '--msg-max-width',
}

// 所有可能被写进去的变量名，换主题时要先清干净，否则会残留上一套配色。
const ALL_VARS = [
  ...Object.values(COLOR_VARS),
  '--bg-message-self',
  ...Object.values(SIZE_VARS),
  ...Object.values(UNITLESS_VARS),
  ...Object.values(EM_VARS),
  ...Object.values(PERCENT_VARS),
  '--font-viewer',
]

/**
 * 把外观设置换算成一组 CSS 变量。
 * `payload` 是接口返回的 `{appearance, defaults, fonts}`（或它的缓存）。
 * @returns {{theme: string, vars: Record<string, string>}}
 */
export function toCssVars(payload, options = {}) {
  const appearance = payload?.appearance || {}
  const theme = options.theme || appearance?.viewer?.theme || 'dark'
  const vars = {}
  const layout = appearance?.viewer?.layout || {}

  for (const [key, name] of Object.entries(SIZE_VARS)) {
    const value = layout[key]
    if (typeof value === 'number') vars[name] = `${value}px`
  }
  for (const [key, name] of Object.entries(UNITLESS_VARS)) {
    const value = layout[key]
    if (typeof value === 'number') vars[name] = String(value)
  }
  for (const [key, name] of Object.entries(EM_VARS)) {
    const value = layout[key]
    if (typeof value === 'number') vars[name] = `${value}em`
  }
  for (const [key, name] of Object.entries(PERCENT_VARS)) {
    const value = layout[key]
    if (typeof value === 'number') vars[name] = `${value}%`
  }

  if (appearance?.fonts?.viewer) {
    vars['--font-viewer'] = fontStack(appearance.fonts.viewer, payload)
  }

  // 颜色只在「自定义主题」下覆盖
  if (theme === 'custom') {
    const colors = appearance?.viewer?.colors || {}
    for (const [key, name] of Object.entries(COLOR_VARS)) {
      if (typeof colors[key] === 'string') vars[name] = colors[key]
    }
    const start = colors.bgMessageSelf
    const end = colors.bgMessageSelfEnd
    if (typeof start === 'string') {
      const flat = typeof end !== 'string' || end.toLowerCase() === start.toLowerCase()
      vars['--bg-message-self'] = flat
        ? start
        : `linear-gradient(180deg, ${start} 0%, ${end} 100%)`
    }
  }

  return { theme, vars }
}

/** 把 `system` / `family:微软雅黑` 解析成 CSS font-family。 */
export function fontStack(value, payload) {
  const catalog = payload?.fonts
  const system =
    catalog?.find((f) => f.id === 'system')?.stack ||
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif"
  if (typeof value !== 'string') return system
  const preset = catalog?.find((f) => f.id === value)
  if (preset) return preset.stack
  if (value.startsWith('family:')) {
    const name = value.slice('family:'.length).replace(/['\\]/g, '').trim()
    if (name) return `'${name}', ${system}`
  }
  return system
}

/** 把一组 CSS 变量写到 <html> 上，并同步 data-theme。 */
export function applyAppearance(payload, options = {}) {
  const root = document.documentElement
  const { theme, vars } = toCssVars(payload, options)
  for (const name of ALL_VARS) root.style.removeProperty(name)
  for (const [name, value] of Object.entries(vars)) {
    root.style.setProperty(name, value)
  }
  // 暗色是 style.css 里的基础值，用空属性表示
  root.setAttribute('data-theme', theme === 'dark' ? '' : theme)
  return theme
}

const CACHE_KEY = 'viewer-appearance'
const THEME_OVERRIDE_KEY = 'viewer-theme-override'

/** 本机上次拿到的外观（先用它上色，避免闪一下默认样式）。 */
export function cachedAppearance() {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/** 访客在本机临时选的主题；null 表示跟随服务器样式。 */
export function themeOverride() {
  const id = localStorage.getItem(THEME_OVERRIDE_KEY)
  return THEME_IDS.includes(id) ? id : null
}

export function setThemeOverride(id) {
  if (!id) localStorage.removeItem(THEME_OVERRIDE_KEY)
  else localStorage.setItem(THEME_OVERRIDE_KEY, id)
}

/** 拉服务器的外观配置；失败（断网 / 旧后端）时返回 null。 */
export async function fetchAppearance() {
  try {
    const res = await fetch('/api/appearance')
    if (!res.ok) return null
    const data = await res.json()
    if (!data || !data.appearance) return null
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(data))
    } catch {}
    return data
  } catch {
    return null
  }
}
