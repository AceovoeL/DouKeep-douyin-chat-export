// 消息时间显示规则的回归测试（lib/messageTime.js）。
//
// 盯着用户 2026-10-14 的两点要求：
//   1. 聊天窗口里，上下两条消息隔了 5 分钟以上，就在两条消息中间插一个居中的
//      完整时间（年月日时分）；
//   2. 每条消息下方原来的时间**不去掉**，同时要带上年份。
// 所以这里把「完整格式带年份」和「5 分钟判据」都钉住，改坏了会红。
import { describe, it, expect } from 'vitest'

import { formatFullTime, formatBriefTime, MSG_GAP_SECONDS, exceedsGap } from './messageTime.js'

// 本地时区的某个时刻：2026-10-14 21:00。用 new Date(...) 构造才能落到本地时区。
function ts(y, mo, d, h, mi) {
  return Math.floor(new Date(y, mo - 1, d, h, mi).getTime() / 1000)
}

describe('formatFullTime', () => {
  it('年月日时分都写出来，不足两位补零', () => {
    expect(formatFullTime(ts(2026, 10, 14, 21, 0))).toBe('2026年10月14日 21:00')
    expect(formatFullTime(ts(2026, 1, 2, 3, 5))).toBe('2026年01月02日 03:05')
  })

  it('时间是 23:59 / 00:00 这种边界值也不塌', () => {
    expect(formatFullTime(ts(2026, 12, 31, 23, 59))).toBe('2026年12月31日 23:59')
    expect(formatFullTime(ts(2027, 1, 1, 0, 0))).toBe('2027年01月01日 00:00')
  })

  it('跨年要带对年份', () => {
    expect(formatFullTime(ts(2025, 12, 31, 23, 59))).toBe('2025年12月31日 23:59')
    expect(formatFullTime(ts(2026, 1, 1, 0, 1))).toBe('2026年01月01日 00:01')
  })

  it('空时间戳和解析不了的时间戳给空串', () => {
    expect(formatFullTime(0)).toBe('')
    expect(formatFullTime(undefined)).toBe('')
    expect(formatFullTime(NaN)).toBe('')
  })
})

describe('formatBriefTime', () => {
  it('今天只写时分，不写年月日', () => {
    const now = new Date()
    const today = ts(now.getFullYear(), now.getMonth() + 1, now.getDate(), 9, 5)
    expect(formatBriefTime(today)).toBe('09:05')
  })

  it('不是今天写几月几日 + 时分，补零', () => {
    expect(formatBriefTime(ts(2020, 3, 5, 7, 8))).toBe('03月05日 07:08')
  })
})

describe('隔多久算断开', () => {
  it('5 分钟整就算断开（用户在意的就是这条线）', () => {
    const a = ts(2026, 10, 14, 21, 0)
    expect(exceedsGap({ timestamp: a }, { timestamp: a + MSG_GAP_SECONDS })).toBe(true)
    expect(exceedsGap({ timestamp: a }, { timestamp: a + MSG_GAP_SECONDS - 1 })).toBe(false)
  })

  it('顺序反了也照断（截图/嵌入列表的边界上时间不一定升序）', () => {
    const a = ts(2026, 10, 14, 21, 0)
    expect(exceedsGap({ timestamp: a + MSG_GAP_SECONDS }, { timestamp: a })).toBe(true)
  })

  it('时间戳缺失时不误判成断开', () => {
    expect(exceedsGap({ timestamp: 0 }, { timestamp: 0 })).toBe(false)
    expect(exceedsGap({}, {})).toBe(false)
  })
})
