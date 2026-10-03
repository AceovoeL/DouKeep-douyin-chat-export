import { describe, expect, it } from 'vitest'
import { calendarMonths, dateBounds, groupMediaByDate, localDate, sortMediaChronologically } from './search'

describe('search calendar', () => {
  it('includes intervening months, leap days and only enables days with messages', () => {
    const months = calendarMonths([{ date: '2024-03-02', count: 3 }, { date: '2024-01-31', count: 1 }, { date: '2024-02-29', count: 2 }])
    expect(months.map(m => m.key)).toEqual(['2024-01', '2024-02', '2024-03'])
    expect(months[1].offset).toBe(4)
    expect(months[1].days).toHaveLength(29)
    expect(months[1].days[28]).toEqual({ day: 29, date: '2024-02-29', count: 2 })
    expect(months[1].days[0].count).toBe(0)
  })
  it('handles no history and year boundaries', () => {
    expect(calendarMonths([])).toEqual([])
    expect(calendarMonths([{ date: '2023-12-31', count: 1 }, { date: '2024-01-01', count: 1 }]).map(m => m.title)).toEqual(['2023年12月', '2024年1月'])
  })
  it('uses local midnight and an exclusive next-day boundary', () => {
    const bounds = dateBounds('2024-02-29')
    expect(new Date(bounds.start * 1000).getHours()).toBe(0)
    expect(localDate(bounds.start)).toBe('2024-02-29')
    expect(localDate(bounds.end)).toBe('2024-03-01')
    expect(dateBounds('invalid')).toBeNull()
  })
  it('merges media across pagination while preserving group and item order', () => {
    const a = dateBounds('2024-03-02').start
    const b = dateBounds('2024-03-01').start
    const items = [{ msg_id: 'a', timestamp: a + 20 }, { msg_id: 'b', timestamp: a }, { msg_id: 'c', timestamp: b }]
    expect(groupMediaByDate(items)).toEqual([{ date: '2024-03-02', items: items.slice(0, 2) }, { date: '2024-03-01', items: items.slice(2) }])
  })
})

describe('media chronological order (old on top, new at the bottom)', () => {
  it('sorts across loaded pages and lists date groups oldest first', () => {
    const day1 = dateBounds('2024-03-01').start
    const day2 = dateBounds('2024-03-02').start
    // The API pages newest-first: page 1 is the newest, the next page is older.
    const page1 = [
      { msg_id: 'c', timestamp: day2 + 30, seq: 30 },
      { msg_id: 'b', timestamp: day2 + 10, seq: 10 },
    ]
    const page2 = [{ msg_id: 'a', timestamp: day1 + 5, seq: 5 }]

    const merged = sortMediaChronologically([...page1, ...page2])
    expect(merged.map(item => item.msg_id)).toEqual(['a', 'b', 'c'])
    expect(groupMediaByDate(merged).map(group => group.date)).toEqual(['2024-03-01', '2024-03-02'])
  })

  it('breaks same-second ties by seq so media keeps the chat order', () => {
    const day = dateBounds('2024-03-02').start
    const items = [{ msg_id: 'z', timestamp: day, seq: 9 }, { msg_id: 'x', timestamp: day, seq: 2 }]
    expect(sortMediaChronologically(items).map(item => item.msg_id)).toEqual(['x', 'z'])
  })

  it('keeps the input array untouched', () => {
    const items = [{ msg_id: 'b', timestamp: 2 }, { msg_id: 'a', timestamp: 1 }]
    sortMediaChronologically(items)
    expect(items.map(item => item.msg_id)).toEqual(['b', 'a'])
  })
})
