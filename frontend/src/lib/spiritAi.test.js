import { describe, it, expect } from 'vitest'
import { getSpiritAi, SPIRIT_NAME } from './spiritAi.js'

// 全部用占位数据：真实昵称、uid 与性格以外的信息不进仓库。

const spiritRow = (cj, extra = {}) => ({
  msg_type: 1,
  content: '示例正文',
  sender_uid: 'uid-spirit-1',
  timestamp: 1750000000,
  raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }),
  ...extra,
})

describe('getSpiritAi · 认出小火人（精灵 AI）', () => {
  it('带 ai_business_ext 的消息算精灵，并取到性格', () => {
    const found = getSpiritAi(spiritRow({
      aweType: 716, text: '示例正文',
      ai_business_ext: { petai_style: '示例性格' },
    }))
    expect(found).toEqual({ name: SPIRIT_NAME, style: '示例性格' })
  })

  it('性格前后有空格时去掉空格', () => {
    const found = getSpiritAi(spiritRow({
      aweType: 716, ai_business_ext: { petai_style: '  示例性格  ' },
    }))
    expect(found.style).toBe('示例性格')
  })

  // 开场的两句问候（「我学会说话啦」）没有 ai_business_ext，只有 aweType=716。
  it('只有 aweType=716、没有扩展时也认得出，性格为空', () => {
    const found = getSpiritAi(spiritRow({ aweType: 716, text: '示例问候' }))
    expect(found).toEqual({ name: SPIRIT_NAME, style: '' })
  })

  it('扩展在但性格缺失时，仍认得出精灵、性格为空', () => {
    const found = getSpiritAi(spiritRow({ aweType: 716, ai_business_ext: {} }))
    expect(found).toEqual({ name: SPIRIT_NAME, style: '' })
  })

  it('普通文本消息不算精灵', () => {
    expect(getSpiritAi(spiritRow({ aweType: 700, text: '示例正文' }))).toBe(null)
  })

  it('别的 aweType 也不认领', () => {
    expect(getSpiritAi(spiritRow({ aweType: 0, text: '示例正文' }))).toBe(null)
  })

  it('没有载荷时不报错', () => {
    expect(getSpiritAi({ msg_type: 1, content: '纯文本' })).toBe(null)
    expect(getSpiritAi(null)).toBe(null)
    expect(getSpiritAi(undefined)).toBe(null)
  })

  it('aweType 是字符串 "716" 时也认（不同版本落库类型不一致）', () => {
    expect(getSpiritAi(spiritRow({ aweType: '716', text: '示例正文' }))).toEqual({
      name: SPIRIT_NAME, style: '',
    })
  })
})
