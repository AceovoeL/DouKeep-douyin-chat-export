import { describe, expect, it } from 'vitest'
import { CUTOFF_LABEL, cutoffLabel, formatCutoff } from './insightMeta.js'

describe('数据统计截止时间', () => {
  it('按本地时区输出年月日时分秒', () => {
    const ts = Math.floor(new Date(2026, 10, 14, 16, 51, 38).getTime() / 1000)
    expect(formatCutoff(ts)).toBe('2026年11月14日 16:51:38')
  })

  it('空值/坏值不显示', () => {
    expect(formatCutoff(0)).toBe('')
    expect(formatCutoff(null)).toBe('')
    expect(formatCutoff(undefined)).toBe('')
    expect(formatCutoff('x')).toBe('')
  })

  it('只认采集时间，不拿最后一条消息的发送时间顶替', () => {
    // 没有采集记录时：空串（界面整行不显示），而不是最后一条消息的时间
    expect(formatCutoff(0)).not.toContain('年')
    const collected = Math.floor(new Date(2026, 10, 14, 17, 3, 17).getTime() / 1000)
    expect(formatCutoff(collected)).toBe('2026年11月14日 17:03:17')
  })
})

describe('数据统计截止至的整行文案', () => {
  const ts = Math.floor(new Date(2026, 10, 14, 16, 51, 38).getTime() / 1000)

  it('前缀用「截止至」，后面跟本地时间', () => {
    expect(CUTOFF_LABEL).toBe('数据统计截止至')
    expect(cutoffLabel(ts)).toBe('数据统计截止至 2026年11月14日 16:51:38')
  })

  it('没有采集记录时整行为空', () => {
    expect(cutoffLabel(0)).toBe('')
    expect(cutoffLabel(undefined)).toBe('')
  })
})
