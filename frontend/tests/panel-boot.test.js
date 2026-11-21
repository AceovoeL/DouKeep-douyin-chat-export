/**
 * 控制面板页面（backend/panel/static/panel.html）的启动自检。
 *
 * 为什么要有这个用例：面板页那一大段内联脚本里，只要有**任何一处**在启动时抛错，
 * 整页 JS 就到此为止、不再往下执行，而启动的最后一步正是「判断要不要输密码」——
 * 于是用户看到一个「请输入密码」的框，可管理员根本没设过密码，输什么都进不去。
 * v1.3.6 真出过这个事故（时间表摘要读了还不存在的字段），当时所有测试都是文本比对，
 * 谁都没发现，直到用户被挡在门外。
 *
 * 所以这里不再只看文本：把那段脚本真的放进一个 DOM 里跑一遍，跑不完、或者中途被
 * 兜底 catch 吞掉一个错误，都算红。用 vitest 现成的 node 环境 + jsdom，不要浏览器，
 * CI 上也能跑。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import { describe, expect, it } from 'vitest'

const PANEL_HTML = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), '../../backend/panel/static/panel.html',
)

/** 把面板页装进一个 DOM，手动执行它的内联脚本。 */
function bootPanel() {
  const html = readFileSync(PANEL_HTML, 'utf8')
  // 页面开头还有一小段「手机端禁止缩放」的脚本；启动脚本是后面那一大段，
  // 所以取最后一个 <script> 块（原来的贪婪正则会把两段和中间的 HTML 全吞进来）。
  const scriptStart = html.lastIndexOf('<script>')
  const scriptEnd = html.lastIndexOf('</script>')
  if (scriptStart < 0 || scriptEnd < scriptStart) throw new Error('panel.html 里找不到内联脚本')
  const script = html.slice(scriptStart + '<script>'.length, scriptEnd)

  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8000/panel',
    runScripts: 'outside-only',      // 页面自己的脚本由下面手动跑，跑完才好断言
    pretendToBeVisual: true,
  })
  // 面板启动后的异步加载全部停在 fetch 上：这个用例只关心「启动那段代码跑不跑得完」，
  // 真去模拟每个接口的返回只会把用例搞得又长又脆。
  dom.window.fetch = () => new Promise(() => {})
  // 启动时的部件错误会被页面里的兜底 catch 接住（那是为了别把用户挡在门外），
  // 但它照样会写一条 console.error —— 用例要跟没兜底时一样红。
  const logged = []
  dom.window.console.error = (...args) => logged.push(args.map(String).join(' '))
  // 脚本正常跑完就会把标记打上；中途抛错的话 eval 直接把异常抛给用例
  dom.window.eval(`${script}\nwindow.__panelBooted = true;`)
  return { dom, window: dom.window, logged }
}

describe('控制面板页面启动', () => {
  it('内联脚本能一路跑到底，启动过程中没有任何脚本错误', () => {
    const { dom, window: win, logged } = bootPanel()
    expect(logged).toEqual([])
    expect(win.__panelBooted).toBe(true)
    // 启动的最后一步就是它：它没被调用 = 用户会卡在那个密码框上
    expect(typeof win.panelAuthCheck).toBe('function')
    dom.window.close()
  })

  it('登录框默认不显示，只有确实要密码时才由脚本打开', () => {
    const { dom, window: win } = bootPanel()
    expect(win.document.getElementById('loginOverlay').style.display).toBe('none')
    dom.window.close()
  })
})

/**
 * 「外观」设置的分组：字体、面板布局、查看器布局与字号。
 * 面板配色 / 查看器配色整块删除，气泡内边距和气泡最大宽度也不再出现；
 * 分享卡拆成视频卡、评论卡两个独立宽度。这里直接把设置渲染出来核对字段。
 */
describe('外观设置分组', () => {
  it('只保留字体、面板布局、查看器布局，字段与要求一致', async () => {
    const { dom, window: win } = bootPanel()
    const appearance = {
      fonts: { ui: 'system', viewer: 'system' },
      panel: { theme: 'dark', colors: {}, layout: { radius: 14, sidebarWidth: 224, fontScale: 1, shadow: 1 } },
      viewer: { theme: 'dark', colors: {}, layout: {
        messageFontSize: 14, lineHeight: 1.55, bubbleRadius: 14, avatarSize: 36,
        shareVideoCardWidth: 180, shareCommentCardWidth: 240,
      } },
    }
    // 走真实加载路径：外观接口返回上面这份配置，页面自己填进分组
    win.fetch = async () => ({
      ok: true,
      json: async () => ({ appearance, defaults: appearance, fonts: [] }),
    })
    await win.loadAppearance()
    const doc = win.document
    const groups = [...doc.querySelectorAll('#apFields details[data-group]')]
      .map(el => el.dataset.group)
    expect(groups).toEqual(['fonts', 'panelLayout', 'viewerLayout'])

    const paths = [...doc.querySelectorAll('#apFields [data-ap-path]')]
      .map(el => el.dataset.apPath)
    // 字体与面板布局只留这 6 项
    for (const keep of [
      'fonts.ui', 'fonts.viewer', 'panel.layout.radius',
      'panel.layout.sidebarWidth', 'panel.layout.fontScale', 'panel.layout.shadow',
    ]) expect(paths).toContain(keep)
    expect(paths).not.toContain('panel.layout.cardPadding')

    // 查看器布局：气泡内边距 / 最大宽度删除，分享卡拆成两项
    for (const gone of [
      'viewer.layout.bubblePadX', 'viewer.layout.bubblePadY',
      'viewer.layout.bubbleMaxWidthPct',
    ]) expect(paths).not.toContain(gone)
    expect(paths).toContain('viewer.layout.shareVideoCardWidth')
    expect(paths).toContain('viewer.layout.shareCommentCardWidth')

    // 两个分享卡宽度按中文名显示，改一项就是整张卡等比缩放
    const labelOf = path => doc.querySelector(
      '#apFields [data-ap-path="' + path + '"]',
    ).closest('.ap-field').querySelector('label').textContent
    expect(labelOf('viewer.layout.shareVideoCardWidth')).toBe('分享视频卡宽度')
    expect(labelOf('viewer.layout.shareCommentCardWidth')).toBe('分享评论卡宽度')

    // 配色分组已经整块删除
    expect(doc.querySelector('#apFields [data-ap-path^="panel.colors."]')).toBeNull()
    expect(doc.querySelector('#apFields [data-ap-path^="viewer.colors."]')).toBeNull()
    expect(doc.querySelector('#apFields [data-ap-path="panel.theme"]')).toBeNull()
    expect(doc.querySelector('#apFields [data-ap-path="viewer.theme"]')).toBeNull()
    dom.window.close()
  })
})

/**
 * 资源包弹窗那两句分工：
 * * `#packWhat` 是固定的说明（这是什么、不下会怎样），**任何状态下都在**；
 * * `#packModalMessage` 只说「现在什么状态」（要不要下 / 下载中 / 装好了 / 失败）。
 * 之前把说明写进了「问要不要下」那一条状态文案里，于是装好的人再从「关于」页点开
 * 弹窗就只剩一句「已装好 214 项」。这里按状态各渲染一遍，钉住说明不会又丢掉。
 */
describe('资源包弹窗', () => {
  const BASE = {
    status: 'idle', done: 0, total: 214, failed: 0, skipped: 0, mismatched: 0,
    current: '', failed_names: [], error: '', installed: 0, missing: 214,
    installed_all: false, manifest_ok: true, percent: 0, prompt: false,
  }
  const STATES = [
    { 名字: '还没下载（问要不要下）', state: {} },
    { 名字: '下载中', state: { status: 'running', done: 5, percent: 2 } },
    { 名字: '已装好', state: { status: 'done', installed: 214, missing: 0, installed_all: true, percent: 100 } },
    { 名字: '下载失败', state: { status: 'failed', failed: 2, failed_names: ['加功德', '微笑'], error: '' } },
    { 名字: '读不到清单', state: { status: 'failed', manifest_ok: false, error: '读不到资源包清单' } },
  ]

  function render(state) {
    const boot = bootPanel()
    boot.window.applyEmojiPack({ ...BASE, ...state })
    return boot
  }

  it.each(STATES.map((entry) => [entry.名字, entry.state]))(
    '说明在任何状态下都在：%s',
    (_名字, state) => {
      const { dom, window: win } = render(state)
      const what = win.document.getElementById('packWhat').textContent.trim()
      expect(what).toContain('憨笑')          // 举例的那个表情
      expect(what).toContain('文字显示')      // 不下会怎样
      expect(win.document.getElementById('packModalMessage').textContent.trim()).not.toBe('')
      dom.window.close()
    },
  )

  it('下载失败时写清是哪几张没下来', () => {
    const { dom, window: win } = render({
      status: 'failed', failed: 2, failed_names: ['加功德', '微笑'], error: '',
    })
    expect(win.document.getElementById('packNote').textContent).toContain('加功德')
    dom.window.close()
  })
})

/**
 * 「概览 → 数据总览」：接口数据要画成第二排卡片 + 消息构成 + 数据范围 + 存储占用。
 * 只 mock 一份 JSON 直接喂给渲染函数，不跑后端；数字格式和中文标签在这里钉住。
 */
describe('数据总览', () => {
  // 用本机时区造时间戳，期望值就是同一个时区展开的结果，跑在哪个时区都不会错
  const TS = Math.floor(new Date(2026, 10, 14, 16, 51, 38).getTime() / 1000)
  const FIXTURE = {
    available: true, stale: false, generated_at: 1763100000, collected_at: TS,
    totals: { conversations: 2, messages: 10, users: 4, singles: 1, groups: 1, avg_messages: 5.0 },
    activity: {
      today: 4, last7: 6, last30: 7, active_days: 4, senders: 3,
      first_message_time: 1700000000, last_message_time: 1763000000,
    },
    types: [
      { key: 'text', count: 3, percent: 30.0, saved: 0 },
      { key: 'image', count: 2, percent: 20.0, saved: 1 },
      { key: 'emoji', count: 1, percent: 10.0, saved: 1 },
      { key: 'video', count: 1, percent: 10.0, saved: 1 },
      { key: 'share', count: 1, percent: 10.0, saved: 0 },
      { key: 'other', count: 2, percent: 20.0, saved: 0 },
    ],
    media: {
      images: { total: 2, saved: 1 },
      emoji: { total: 1, saved: 1 },
      videos: { total: 1, saved: 1 },
      live_photo: { total: 1, saved: 1 },
    },
    voice: { success: 1, failed: 1, pending: 1, total: 3 },
    storage: {
      db_bytes: 2048, media_bytes: 692, media_files: 8, exports_bytes: 0,
      total_bytes: 2740,
      detail: [
        { key: 'images', bytes: 100, files: 1 },
        { key: 'emoji', bytes: 200, files: 1 },
        { key: 'voice', bytes: 10, files: 1 },
        { key: 'avatars', bytes: 50, files: 1 },
        { key: 'videos', bytes: 300, files: 1 },
        { key: 'other', bytes: 32, files: 3 },
      ],
    },
    schedule: { enabled: true, next_run: 1763110000 },
  }

  it('把接口数据画成卡片、消息构成、数据范围与存储占用', async () => {
    const { dom, window: win } = bootPanel()
    win.fetch = async () => ({ ok: true, status: 200, json: async () => FIXTURE })
    await win.loadOverview(true)
    const doc = win.document

    // 最上方「数据统计截止至」：跟聊天查看器同一个写法
    const cutoff = doc.getElementById('ovCutoff')
    expect(cutoff.hidden).toBe(false)
    expect(cutoff.textContent).toBe('数据统计截止至 2026年11月14日 16:51:38')

    expect(doc.getElementById('ovToday').textContent).toBe('4')
    expect(doc.getElementById('ovWeek').textContent).toBe('6')
    expect(doc.getElementById('ovActiveDays').textContent).toBe('4')
    expect(doc.getElementById('ovSenders').textContent).toBe('3')

    const bars = [...doc.querySelectorAll('#ovTypeBars .ov-bar-row')]
    expect(bars).toHaveLength(6)
    expect(bars[0].textContent).toContain('文字')
    expect(bars[0].textContent).toContain('30.0%')
    expect(bars[1].querySelector('.ov-bar-fill').style.width).toBe('20%')

    const range = doc.getElementById('ovRangeList').textContent
    expect(range).toContain('单聊')
    expect(range).toContain('群聊')
    expect(range).toContain('近 30 天消息')
    expect(range).toContain('定时采集')
    expect(range).toContain('已开启')

    const local = doc.getElementById('ovLocalList').textContent
    expect(local).toContain('视频消息')
    expect(local).toContain('实况图视频')
    expect(local).toContain('已保存 1 / 共 1')
    expect(local).toContain('语音转写')
    expect(local).toContain('待处理 1')

    const storage = doc.getElementById('ovStorageList').textContent
    expect(storage).toContain('数据库')
    expect(storage).toContain('2.0 KB')
    expect(storage).toContain('合计')
    expect(storage).not.toContain('导出/备份文件')
    expect(storage).not.toContain('视频消息')

    expect(typeof win.refreshOverview).toBe('function')
    expect(doc.getElementById('ovRefreshBtn')).not.toBeNull()
    expect(doc.getElementById('ovError').hidden).toBe(true)
    dom.window.close()
  })

  it('没有采集记录时，截止时间整行不显示', async () => {
    const { dom, window: win } = bootPanel()
    win.fetch = async () => ({
      ok: true, status: 200, json: async () => ({ ...FIXTURE, collected_at: null }),
    })
    await win.loadOverview(true)
    const cutoff = win.document.getElementById('ovCutoff')
    expect(cutoff.hidden).toBe(true)
    expect(cutoff.textContent).toBe('')
    dom.window.close()
  })
})

/**
 * 纯文本 TXT 的导出弹窗：和 HTML 那个共用一套日历。
 * 这里直接驱动窗口里的全局函数：打开 → 全部导出有预估 → 切到部分导出 → 日历点两下选区间 → 区间预估。
 */
describe('纯文本 TXT 导出弹窗', () => {
  it('全部/部分切换，部分导出能选日期区间并给出预估', async () => {
    const { dom, window: win } = bootPanel()
    win.fetch = async (url) => {
      const u = String(url)
      if (u.startsWith('/panel/api/export/txt/daily')) {
        return { ok: true, json: async () => ({ items: [
          { date: '2026-11-01', count: 3 },
          { date: '2026-11-02', count: 1 },
        ] }) }
      }
      if (u.startsWith('/panel/api/export/txt/estimate')) {
        return { ok: true, json: async () => ({ messages: 4, estimated_bytes: 2048 }) }
      }
      return { ok: true, json: async () => ({}) }
    }

    win.openTxtExport(['会话A'])
    const doc = win.document
    expect(doc.getElementById('txtExportModal').hidden).toBe(false)
    expect(doc.getElementById('txtScopeAll').checked).toBe(true)
    expect(doc.getElementById('txtExportTitle').textContent).toContain('会话A')

    // 全部导出也要有预估（消息条数 + 文件大小）
    await new Promise((r) => setTimeout(r, 400))
    expect(doc.getElementById('txtEstimate').textContent).toContain('预计')
    expect(doc.getElementById('txtEstimate').textContent).toContain('2.0 KB')

    // 切到部分导出：日期区间出现，日历按接口返回的有消息日期画
    doc.getElementById('txtScopePart').checked = true
    win.onTxtScopeChange()
    expect(doc.getElementById('txpDateSection').style.display).toBe('')
    await new Promise((r) => setTimeout(r, 30))
    const days = [...doc.querySelectorAll('#txpGrid .hdp-day.has-msg')]
    expect(days.length).toBeGreaterThan(0)

    // 两次点击选区间（和 HTML 弹窗的操作一致）
    days[0].dispatchEvent(new win.Event('click'))
    days[days.length - 1].dispatchEvent(new win.Event('click'))
    expect(doc.getElementById('txpRangeText').textContent).toContain('导出区间')

    await new Promise((r) => setTimeout(r, 400))
    expect(doc.getElementById('txtEstimate').textContent).toContain('区间内')
    dom.window.close()
  })

  it('预估大小按 B / KB / MB / GB 自动换单位', async () => {
    const { dom, window: win } = bootPanel()
    const cases = [
      [900, '900 B'],
      [2048, '2.0 KB'],
      [5 * 1024 * 1024, '5.0 MB'],
      [3.6 * 1024 * 1024 * 1024, '3.6 GB'],
    ]
    for (const [bytes, shown] of cases) {
      win.fetch = async () => ({ ok: true, json: async () => ({ messages: 1, estimated_bytes: bytes }) })
      win.openTxtExport(['会话A'])
      await new Promise((r) => setTimeout(r, 400))
      expect(win.document.getElementById('txtEstimate').textContent).toContain(shown)
      win.closeTxtExport()
    }
    dom.window.close()
  })
})

