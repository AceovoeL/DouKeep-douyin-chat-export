// 「N 条消息」该显示几：和 Python 端（common/display_rules.py）用同一份样例。
// 任一端的显示规则改了、另一头没跟上，这里就会红。
import { describe, it, expect } from 'vitest'
import cases from './displayCountCases.json'
import { shouldShow, duplicateSystemMessageIds } from './douyinMessage.js'

function hiddenIds(messages) {
  const duplicates = duplicateSystemMessageIds(messages)
  return messages
    .filter(msg => duplicates.has(msg.msg_id) || !shouldShow(msg))
    .map(msg => msg.msg_id)
}

describe('显示条数（会话列表 / 聊天窗口左上角）', () => {
  for (const sample of cases) {
    it(sample.name, () => {
      const hidden = hiddenIds(sample.messages)
      expect(hidden.sort()).toEqual([...sample.hidden].sort())
      expect(sample.messages.length - hidden.length).toBe(sample.display)
    })
  }

  it('样例覆盖了「隐藏」和「显示」两类', () => {
    expect(cases.some(sample => sample.hidden.length > 0)).toBe(true)
    expect(cases.some(sample => sample.display > 0)).toBe(true)
    expect(cases.length).toBeGreaterThanOrEqual(5)
  })
})
