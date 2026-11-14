import { describe, expect, it } from 'vitest'
import { sparkColor, sparkIconUrl } from './spark.js'

describe('火花图标地址（sparkIconUrl）', () => {
  it('spark_icons/xxx.png 走 /media 取本地图', () => {
    expect(sparkIconUrl('spark_icons/blue_normal.png'))
      .toBe('/media/spark_icons/blue_normal.png')
  })

  it('空值、可疑路径一律不画图', () => {
    expect(sparkIconUrl('')).toBe('')
    expect(sparkIconUrl(null)).toBe('')
    expect(sparkIconUrl('avatars/x.png')).toBe('')
    expect(sparkIconUrl('spark_icons/../secret.png')).toBe('')
    expect(sparkIconUrl('spark_icons/a/b.png')).toBe('')
  })
})

describe('火花天数颜色（sparkColor）', () => {
  it('按官方文件名前缀取颜色', () => {
    // 抖音 Web 端普通火花：图标是橙色，天数数字 rgb(255, 94, 0)
    expect(sparkColor('spark_icons/normal_normal.png')).toBe('#FF5E00')
    expect(sparkColor('spark_icons/gray_normal.png')).toBe('#B2B2B2')
    expect(sparkColor('spark_icons/blue_normal.png')).toBe('#1CB0C3')
    expect(sparkColor('spark_icons/red_normal.png')).toBe('#FF4D4F')
  })

  it('认不出来的返回空串（组件用主题色兜底）', () => {
    expect(sparkColor('spark_icons/unknown.png')).toBe('')
    expect(sparkColor('')).toBe('')
    expect(sparkColor(null)).toBe('')
  })
})
