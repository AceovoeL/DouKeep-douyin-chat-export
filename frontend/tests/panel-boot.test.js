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
  const match = html.match(/<script>([\s\S]*)<\/script>/)
  if (!match) throw new Error('panel.html 里找不到内联脚本')

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
  dom.window.eval(`${match[1]}\nwindow.__panelBooted = true;`)
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
