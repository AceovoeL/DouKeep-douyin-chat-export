// 会话列表 / 聊天标题 / 数据洞察共用的「火花」显示规则。
//
// 图标由采集端按抖音官方文件名存到 data/media/spark_icons/（例如
// blue_normal.png），这里只负责拼出 /media 地址，并根据文件名前缀取天数颜色。

const SPARK_COLORS = {
  // 抖音 Web 端普通（橙色）火花的天数色就是 rgb(255, 94, 0)，
  // 其余几种也按官方图里图标的颜色取，图标和数字才是一套。
  normal: '#FF5E00',
  gray: '#B2B2B2',
  blue: '#1CB0C3',
  red: '#FF4D4F',
  orange: '#FF7A45',
  yellow: '#F5A623',
  green: '#52C41A',
  purple: '#9254DE',
  pink: '#FF85C0',
}

export function sparkIconUrl(icon) {
  if (typeof icon !== 'string' || !icon.startsWith('spark_icons/')) return ''
  const name = icon.slice('spark_icons/'.length)
  if (!name || name.includes('/') || name.includes('..')) return ''
  return `/media/spark_icons/${encodeURIComponent(name)}`
}

export function sparkColor(icon) {
  if (typeof icon !== 'string' || !icon) return ''
  const name = icon.split('/').pop().toLowerCase()
  const key = Object.keys(SPARK_COLORS).find((color) => name.startsWith(color))
  return key ? SPARK_COLORS[key] : ''
}
