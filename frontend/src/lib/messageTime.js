// 消息时间的两种写法，以及「隔了多久算隔得开」的判据。
//
// 单独成模块是为了让 MessageList（聊天窗口）和 SearchBar（右侧搜索列表）
// 用同一份格式，也能脱离组件单测。
//
// 时区：一律按**浏览器本地时区**显示，和抖音客户端看下来的时间一致
// （同一台机器上截图、导出、界面都讲同一个时区）。

const pad = (n) => String(n).padStart(2, '0')

// 2026年10月14日 21:00 —— 年月日时分都在。
// 用在每条消息下方的时间、以及两条消息中间那颗时间分隔条上
// （用户 2026-10-14 要求这两处都带年份，免得翻到跨年的记录时分不清）。
export function formatFullTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getFullYear()}年${pad(d.getMonth() + 1)}月${pad(d.getDate())}日 `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// 10月14日 21:00 —— 今天只写 21:00。给搜索结果这类地方用：那里本来就只有时间一列。
export function formatBriefTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return time
  return `${pad(d.getMonth() + 1)}月${pad(d.getDate())}日 ${time}`
}

// 两条消息隔了这么久（秒）就当「中间断开了」。
// 既用来判断要不要另起一组（隐藏重复的头像昵称），也用来判断要不要插时间分隔条，
// 两处必须是同一个数，否则会出现「视觉上是一组、中间却插着时间」的割裂感。
//
// 5 分钟**整**算断开（>=，不是 >）：改之前「另起一组」用的是 > 300，5 分钟整那一秒
// 不算断开。有了时间分隔条之后这条线就是「要不要补一颗时间」的唯一标准；取语法上
// 也说得通的一侧（5 分钟整已经满足"5 分钟以上"），边界消息才有个明确去处。
export const MSG_GAP_SECONDS = 300  // 5 分钟

// prev、cur 两条消息是不是隔得开（时间戳缺省当 0）。
// 用绝对值：消息通常是升序的，但截图/嵌入列表的边界上不一定。
export function exceedsGap(prev, cur) {
  const a = Number(prev?.timestamp || 0)
  const b = Number(cur?.timestamp || 0)
  return Math.abs(b - a) >= MSG_GAP_SECONDS
}
