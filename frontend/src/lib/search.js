export function dateBounds(date) {
  const start = new Date(`${date}T00:00:00`)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  if (!Number.isFinite(start.getTime())) return null
  return { start: Math.floor(start.getTime() / 1000), end: Math.floor(end.getTime() / 1000) }
}

export function calendarMonths(stats) {
  const counts = new Map(stats.map(item => [item.date, item.count]))
  const dates = [...counts.keys()].filter(date => /^\d{4}-\d{2}-\d{2}$/.test(date)).sort()
  if (!dates.length) return []
  const [year, month] = dates[0].split('-').map(Number)
  const [lastYear, lastMonth] = dates.at(-1).split('-').map(Number)
  const result = []
  const cursor = new Date(year, month - 1, 1)
  const last = new Date(lastYear, lastMonth - 1, 1)
  while (cursor <= last) {
    const y = cursor.getFullYear(), m = cursor.getMonth()
    const key = `${y}-${String(m + 1).padStart(2, '0')}`
    const days = []
    for (let day = 1; day <= new Date(y, m + 1, 0).getDate(); day++) {
      const date = `${key}-${String(day).padStart(2, '0')}`
      days.push({ day, date, count: counts.get(date) || 0 })
    }
    result.push({ key, title: `${y}年${m + 1}月`, offset: cursor.getDay(), days })
    cursor.setMonth(m + 1)
  }
  return result
}

export function localDate(timestamp) {
  const date = new Date(timestamp * 1000)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export function groupMediaByDate(items) {
  const groups = new Map()
  for (const item of items) {
    const date = item.timestamp ? localDate(item.timestamp) : '日期未知'
    if (!groups.has(date)) groups.set(date, [])
    groups.get(date).push(item)
  }
  return [...groups].map(([date, items]) => ({ date, items }))
}

// 图片与视频按时间正序排列（旧 → 新），与聊天记录方向一致。
// 接口是按 seq 倒序分页的（第一页最新），所以要在合并已加载的分页后再排序；
// timestamp 相同的消息（同一秒内）用 seq 还原聊天顺序。
export function sortMediaChronologically(items) {
  return [...items].sort((a, b) => {
    const at = a.timestamp || 0
    const bt = b.timestamp || 0
    if (at !== bt) return at - bt
    const as = Number(a.seq ?? 0)
    const bs = Number(b.seq ?? 0)
    if (as !== bs) return as - bs
    return String(a.msg_id ?? '').localeCompare(String(b.msg_id ?? ''))
  })
}
