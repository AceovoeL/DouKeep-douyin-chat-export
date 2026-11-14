// 聊天窗口工具栏、数据洞察弹窗顶部共用的「数据统计截止至 …」。
//
// 采集端在「这个会话本次采集任务里第一次真正调消息接口拉数据」的那一刻写
// collected_at（Unix 秒），这里只负责把它按浏览器本地时区显示成
// 年月日 + 时分秒。没有这个记录就返回空串、整行都不显示 ——
// 绝不拿「最后一条消息的发送时间」顶替：那是消息发出去的时间，
// 跟这次采集是什么时候做的完全不是一回事。

export const CUTOFF_LABEL = '数据统计截止至'

export function formatCutoff(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return ''
  const date = new Date(value * 1000)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 完整一行文案：「数据统计截止至 2026年11月14日 16:51:38」；没有记录时返回空串。 */
export function cutoffLabel(seconds) {
  const text = formatCutoff(seconds)
  return text ? `${CUTOFF_LABEL} ${text}` : ''
}
