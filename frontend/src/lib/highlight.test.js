import { describe, expect, it } from 'vitest'
import { escapeHtml, highlightText, renderRichText } from './highlight'
import { emojiUrl } from './emojiAssets'

describe('escapeHtml', () => {
  it('escapes the characters that could break out of HTML', () => {
    expect(escapeHtml('<b>"a"&\'b\'</b>')).toBe('&lt;b&gt;&quot;a&quot;&amp;&#39;b&#39;&lt;/b&gt;')
  })
  it('treats null and undefined as empty text', () => {
    expect(escapeHtml(null)).toBe('')
    expect(escapeHtml(undefined)).toBe('')
  })
})

describe('highlightText', () => {
  it('wraps matches without touching the escaped text', () => {
    expect(highlightText('a<b>c', 'b')).toBe('a&lt;<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">b</mark>&gt;c')
  })
  it('returns escaped text when there is no query', () => {
    expect(highlightText('<hi>', '')).toBe('&lt;hi&gt;')
  })

  // 命中位置必须落在用户真正看到的字符上，不能落在 HTML 实体里面。
  // 以前是先整段转义再搜索，`&` 转成 `&amp;` 之后拿用户输入的 `&` 去搜，就会命中
  // 实体的内部：一个真 `&` 冒出三个黄条；搜 `amp` 会把实体名也高亮；搜 `<` 一个都搜不到。
  it('highlights a real ampersand, not the escaped entity', () => {
    const mark = (inner) => `<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">${inner}</mark>`
    expect(highlightText('A & B', '&')).toBe(`A ${mark('&amp;')} B`)
    expect(highlightText('A & B', 'amp')).toBe('A &amp; B')     // amp 是实体名，不该命中
    expect(highlightText('A & B', '<')).toBe('A &amp; B')       // 原文里没有 <
    expect(highlightText('a<b', '<')).toBe(`a${mark('&lt;')}b`) // 原文里真有 <
  })

  it('does not turn HTML entity names into search hits', () => {
    // 原文里是 `&` 一个字符，转义后变成 `&amp;` 五个字符：搜 `amp` 不该命中那段实体名。
    expect(highlightText('a&b', 'amp')).toBe('a&amp;b')
    // 原文里真有 `amp` 三个字符时照旧命中（用户看到的就是这三个字）。
    expect(highlightText('amp', 'amp')).toBe(
      '<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">amp</mark>')
    // 原文里真有 `&amp;` 五个字符（用户看到的文本）时也照旧命中；开头的 `&` 照旧转义
    expect(highlightText('&amp;', 'amp')).toBe(
      '&amp;<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">amp</mark>;')
  })

  it('keeps the query escaped so regex metacharacters are literal', () => {
    expect(highlightText('a.b', '.')).toBe(
      'a<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">.</mark>b')
    expect(highlightText('a+b', '+')).toBe(
      'a<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">+</mark>b')
    expect(highlightText('100% 完成', '%')).toBe(
      '100<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">%</mark> 完成')
  })
})

describe('renderRichText', () => {
  it('turns a bracketed emoji that exists locally into an image', () => {
    // onerror 是资源包还没下载时的兜底：换回原来的文字记号，不留破图
    expect(renderRichText('给你[钱]')).toBe(
      '给你<img class="msg-emoji" src="/emoji/%E9%92%B1.webp" alt="[钱]" title="[钱]" draggable="false" onerror="this.replaceWith(this.alt)">',
    )
  })

  it('keeps bracketed text that has no local image', () => {
    expect(renderRichText('看[表情]和[小乔]')).toBe('看[表情]和[小乔]')
  })

  it('escapes html around and inside the emoji token', () => {
    expect(renderRichText('<b>[钱]</b>')).toBe(
      '&lt;b&gt;<img class="msg-emoji" src="/emoji/%E9%92%B1.webp" alt="[钱]" title="[钱]" draggable="false" onerror="this.replaceWith(this.alt)">&lt;/b&gt;',
    )
  })

  it('still highlights search hits in the surrounding text', () => {
    const html = renderRichText('钱到账啦', '到账')
    expect(html).toContain('<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">到账</mark>')
  })

  it('highlights the emoji image itself when the query matches its name', () => {
    const html = renderRichText('[钱]', '钱')
    expect(html.startsWith('<mark ')).toBe(true)
    expect(html).toContain('src="/emoji/%E9%92%B1.webp"')
  })

  it('does not eat the mark wrapper while replacing emoji', () => {
    expect(renderRichText('', '钱')).toBe('')
    expect(renderRichText(null, '钱')).toBe('')
  })

  it('renders the 「表情 | 回应者」 detail of a quick reaction, escaping the nickname', () => {
    // 消息下方的「表情快捷回复」明细走的就是这里：表情名换成图片，昵称当纯文本转义
    const html = renderRichText('[爱心] | <b>小明</b>')
    expect(html).toContain('src="/emoji/%E7%88%B1%E5%BF%83.webp"')
    expect(html).toContain('&lt;b&gt;小明&lt;/b&gt;')
    expect(html).not.toContain('<b>')
  })
})

describe('emojiUrl', () => {
  it('only knows the names from the douyin emoji panel', () => {
    expect(emojiUrl('微笑')).toBe('/emoji/%E5%BE%AE%E7%AC%91.webp')
    expect(emojiUrl('表情')).toBeNull()
    expect(emojiUrl('小乔')).toBeNull()
  })
})
