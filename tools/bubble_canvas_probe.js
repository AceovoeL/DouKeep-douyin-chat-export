/* 抖音网页版控制台脚本：导出气泡背景 Canvas，顺便算九宫格参数。
 *
 * 用法：
 *   1. 打开抖音网页版私信，让聊天气泡显示出来；
 *   2. F12 → Console，整段粘贴回车；
 *   3. 每张气泡背景会下载成 PNG，控制台同时打印可填进配置表的参数。
 *
 * 注意：
 * - 这个脚本只是备用/诊断工具。采集器优先读 PNG 里自带的 FlexData，
 *   那才是抖音官方写的切片和文字安全区；这里是从像素估算的。
 * - 如果 canvas 被跨域图片污染，浏览器会禁止读像素，脚本会跳过并提示。
 * - 气泡编号请从消息 raw_data 的 fields.f9 里找 a:chat_bubble 后面的
 *   bubble_id，脚本无法可靠地从 canvas 反推出编号。
 */
(function () {
  'use strict'

  var SELECTOR = 'canvas[class*="MessageItemBubblecanvasBubbleBackground"]'
  var TOLERANCE = 12

  function dominantColor(data, width, height) {
    var counts = new Map()
    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        var offset = (y * width + x) * 4
        if (data[offset + 3] < 250) continue
        var key = data[offset] + ',' + data[offset + 1] + ',' + data[offset + 2]
        counts.set(key, (counts.get(key) || 0) + 1)
      }
    }
    var best = ''
    var bestCount = 0
    counts.forEach(function (count, key) {
      if (count > bestCount) { best = key; bestCount = count }
    })
    if (!best) return null
    return best.split(',').map(Number)
  }

  function largestRect(data, width, height, dominant) {
    var mask = new Uint8Array(width * height)
    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        var offset = (y * width + x) * 4
        var alpha = data[offset + 3]
        var near = Math.abs(data[offset] - dominant[0]) <= TOLERANCE
          && Math.abs(data[offset + 1] - dominant[1]) <= TOLERANCE
          && Math.abs(data[offset + 2] - dominant[2]) <= TOLERANCE
        mask[y * width + x] = alpha >= 250 && near ? 1 : 0
      }
    }
    var heights = new Array(width).fill(0)
    var best = { area: 0, x: 0, y: 0, w: 0, h: 0 }
    for (var row = 0; row < height; row += 1) {
      for (var col = 0; col < width; col += 1) {
        heights[col] = mask[row * width + col] ? heights[col] + 1 : 0
      }
      var stack = []
      for (var c = 0; c <= width; c += 1) {
        var current = c < width ? heights[c] : 0
        while (stack.length && heights[stack[stack.length - 1]] >= current) {
          var h = heights[stack.pop()]
          var left = stack.length ? stack[stack.length - 1] + 1 : 0
          var area = h * (c - left)
          if (area > best.area) {
            best = { area: area, x: left, y: row - h + 1, w: c - left, h: h }
          }
        }
        stack.push(c)
      }
    }
    return best
  }

  function analyzeCanvas(canvas) {
    var width = canvas.width
    var height = canvas.height
    if (!width || !height) return null
    var copy = document.createElement('canvas')
    copy.width = width
    copy.height = height
    var context = copy.getContext('2d', { willReadFrequently: true })
    context.drawImage(canvas, 0, 0)
    var pixels
    try {
      pixels = context.getImageData(0, 0, width, height).data
    } catch (error) {
      return { error: '画布被跨域图片污染，浏览器不允许读像素：' + error.message }
    }
    var dominant = dominantColor(pixels, width, height)
    if (!dominant) return { error: '画布完全是透明的，找不到气泡主体。' }
    var rect = largestRect(pixels, width, height, dominant)
    if (!rect.area) return { error: '找不到足够大的不透明纯色区域。' }

    var box = canvas.getBoundingClientRect()
    var scale = box.width > 0 ? width / box.width : 3
    if (!Number.isFinite(scale) || scale <= 0) scale = 3
    var displayW = width / scale
    var displayH = height / scale

    var left = rect.x / width * 100
    var right = (width - (rect.x + rect.w)) / width * 100
    var top = rect.y / height * 100
    var bottom = (height - (rect.y + rect.h)) / height * 100
    var gap = 4
    var padding = {
      top: top / 100 * displayH + gap,
      right: right / 100 * displayW + gap,
      bottom: bottom / 100 * displayH + gap,
      left: left / 100 * displayW + gap,
    }
    return {
      bubble_id: '把消息 fields.f9 里的 bubble_id 填在这里',
      image: '（导出 PNG 后放到 data/media/bubbles/<bubble_id>/self.png）',
      display: { width: Math.round(displayW * 100) / 100, height: Math.round(displayH * 100) / 100 },
      slice: {
        top: Math.round(top * 100) / 100,
        right: Math.round(right * 100) / 100,
        bottom: Math.round(bottom * 100) / 100,
        left: Math.round(left * 100) / 100,
      },
      border: {
        top: Math.round(top / 100 * displayH * 100) / 100,
        right: Math.round(right / 100 * displayW * 100) / 100,
        bottom: Math.round(bottom / 100 * displayH * 100) / 100,
        left: Math.round(left / 100 * displayW * 100) / 100,
      },
      padding: {
        top: Math.round(padding.top * 100) / 100,
        right: Math.round(padding.right * 100) / 100,
        bottom: Math.round(padding.bottom * 100) / 100,
        left: Math.round(padding.left * 100) / 100,
      },
      min_width: Math.round(displayW * 100) / 100,
      min_height: Math.round(displayH * 100) / 100,
      text_color: '#ffffff（按实际文字颜色改）',
      estimated: true,
    }
  }

  function downloadCanvas(canvas, index) {
    try {
      var url = canvas.toDataURL('image/png')
      var link = document.createElement('a')
      link.href = url
      link.download = 'bubble-' + index + '.png'
      document.body.appendChild(link)
      link.click()
      link.remove()
      return true
    } catch (error) {
      console.warn('第 ' + index + ' 张导出失败：' + error.message)
      return false
    }
  }

  var canvases = document.querySelectorAll(SELECTOR)
  if (!canvases.length) {
    console.log('没有找到气泡背景 canvas。请先打开私信，让聊天消息显示出来。')
    return
  }
  console.log('找到 ' + canvases.length + ' 张气泡背景。')
  Array.prototype.forEach.call(canvases, function (canvas, index) {
    console.log('--- 第 ' + (index + 1) + ' 张 ---')
    downloadCanvas(canvas, index + 1)
    var result = analyzeCanvas(canvas)
    if (!result) {
      console.log('这张画布没有有效尺寸，跳过。')
    } else if (result.error) {
      console.log(result.error)
    } else {
      console.log(JSON.stringify(result, null, 2))
    }
  })
})()
