// 「哪些消息算语音」的规则同时写在前端（douyinMessage.js 的 isVoiceMsg）和后端
// （backend/database.py 的 _voice_clause）。两边只要有一点不一样，就会出现
// 「聊天窗口里画成语音、搜索却搜不到」这种怪事，所以用例集中放在
// tests/fixtures/voice_cases.json，这里和 tests/test_voice_rule_shared.py 各跑一遍。
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { isVoiceMsg } from './douyinMessage'

const path = fileURLToPath(new URL('../../../tests/fixtures/voice_cases.json', import.meta.url))
const cases = JSON.parse(readFileSync(path, 'utf8')).cases

// 后端存库时的形状：raw_data = { content_json: "<JSON 字符串>" }。
function asStored(c) {
  return {
    msg_id: c.name,
    msg_type: c.msg_type,
    raw_data: JSON.stringify({ content_json: JSON.stringify(c.content_json) }),
  }
}

describe('voice rule matches the backend search filter', () => {
  it.each(cases)('$name', (c) => {
    expect(isVoiceMsg(asStored(c))).toBe(c.voice)
  })
})
