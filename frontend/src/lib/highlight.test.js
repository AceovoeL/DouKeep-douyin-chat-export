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
})

describe('emojiUrl', () => {
  it('only knows the names from the douyin emoji panel', () => {
    expect(emojiUrl('微笑')).toBe('/emoji/%E5%BE%AE%E7%AC%91.webp')
    expect(emojiUrl('表情')).toBeNull()
    expect(emojiUrl('小乔')).toBeNull()
  })
})
