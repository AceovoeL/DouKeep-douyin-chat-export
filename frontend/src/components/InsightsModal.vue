<script setup>
// 数据洞察弹窗：热力图 / 消息排行 / 文字表情榜 / 友谊数据 / 关键词。
// 数据全部来自本地统计接口 /api/conversations/{id}/insights，打开时现算一次。
import { ref, watch, nextTick, onBeforeUnmount } from 'vue'

const props = defineProps({
  convId: { type: String, default: '' },
  convName: { type: String, default: '' },
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

const loading = ref(false)
const error = ref('')
const data = ref(null)

const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const WEEKDAY_SHORT = ['一', '二', '三', '四', '五', '六', '日']

// 热力图每格的深浅：0 → 透明，最大 → 最深
function cellStyle(count, max) {
  if (!count || !max) return ''
  const level = Math.ceil((count / max) * 5)
  const alpha = 0.12 + level * 0.16
  return `background: rgba(64, 132, 83, ${alpha.toFixed(2)})`
}

function load() {
  if (!props.convId) return
  loading.value = true
  error.value = ''
  data.value = null
  fetch(`/api/conversations/${encodeURIComponent(props.convId)}/insights`)
    .then((r) => { if (!r.ok) throw new Error(); return r.json() })
    .then((d) => { data.value = d })
    .catch(() => { error.value = '统计加载失败，请重试' })
    .finally(() => { loading.value = false })
}

watch(() => props.open, (open) => { if (open) load() })

// ── 弹窗高度过渡 ──────────────────────────────────────────────
// 弹窗会先从「正在统计…」的小框变成一整页数据，直接换高度会「啪」地跳一下。
// 做法是：先把高度钉在旧值，下一帧再过渡到新内容的高度，结束后把 height 交还给
// CSS 的 max-height（这样窗口变窄/变高时仍然能自己收缩，不会卡死）。
const modalEl = ref(null)
const contentEl = ref(null)
let wasOpen = false
let heightTimer = 0
let heightFrame = 0
let resizeFrame = 0
let resizeObserver = null

const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

// 量出内容自然展开时的高度；max-height 仍负责把它压在视口以内。
function measureNaturalHeight() {
  const el = modalEl.value
  if (!el) return 0
  const prevHeight = el.style.height
  const prevTransition = el.style.transition
  el.style.transition = 'none'
  el.style.height = 'auto'
  // 用 offsetHeight 而不是 getBoundingClientRect：刚打开时面板还带着缩放入场，
  // 后者会把缩放算进高度，量出来的目标值会偏小。
  const height = el.offsetHeight
  el.style.height = prevHeight
  el.style.transition = prevTransition
  return height
}

function animateHeight(from) {
  const el = modalEl.value
  if (!el) return
  if (reduceMotion()) {
    el.style.height = ''
    el.style.transition = ''
    return
  }
  const to = measureNaturalHeight()
  if (!from || Math.abs(to - from) < 1) {
    el.style.height = ''
    el.style.transition = ''
    return
  }
  clearTimeout(heightTimer)
  cancelAnimationFrame(heightFrame)
  el.style.transition = 'none'
  el.style.height = from + 'px'
  void el.offsetHeight // 先按旧高度排一次版，下一帧改高度才会真的走过渡
  el.style.transition = 'height 0.34s var(--ease-out)'
  heightFrame = requestAnimationFrame(() => {
    if (!modalEl.value) return
    el.style.height = to + 'px'
  })
  heightTimer = window.setTimeout(() => {
    if (!modalEl.value) return
    el.style.height = ''
    el.style.transition = ''
  }, 380)
}

// 图片/字体等晚一步撑开内容时也补一次过渡（正在做高度动画时不叠加）。
function scheduleHeightCheck() {
  cancelAnimationFrame(resizeFrame)
  resizeFrame = requestAnimationFrame(() => {
    const el = modalEl.value
    if (!wasOpen || !props.open || !el || el.style.height) return
    animateHeight(el.offsetHeight)
  })
}

watch(
  () => [props.open, loading.value, data.value, error.value],
  async ([open], [prevOpen]) => {
    if (!open) {
      wasOpen = false
      clearTimeout(heightTimer)
      cancelAnimationFrame(heightFrame)
      cancelAnimationFrame(resizeFrame)
      // 高度动画的 inline transition 会压住淡出用的 class transition，关闭时先摘掉；
      // 高度本身保留在当前像素值，所以淡出过程中不会突然跳高/跳矮。
      if (modalEl.value) modalEl.value.style.transition = ''
      return
    }
    const el = modalEl.value
    const from = el ? el.offsetHeight : 0
    await nextTick()
    if (!el || !props.open) return
    // 刚打开时不做「从 0 长高」，交给淡入/缩放；之后加载完成才平滑改高度。
    if (!wasOpen || !prevOpen) {
      wasOpen = true
      el.style.height = ''
      el.style.transition = ''
      return
    }
    animateHeight(from)
  },
  { flush: 'pre' }
)

// 数据区内容自己变了（例如图片加载、换会话重算）时也跟着缓一下。
watch(contentEl, (el) => {
  resizeObserver?.disconnect()
  if (!el || typeof ResizeObserver === 'undefined') return
  resizeObserver = new ResizeObserver(() => scheduleHeightCheck())
  resizeObserver.observe(el)
})

function onViewportResize() {
  if (props.open) scheduleHeightCheck()
}
window.addEventListener('resize', onViewportResize)

onBeforeUnmount(() => {
  window.removeEventListener('resize', onViewportResize)
  resizeObserver?.disconnect()
  clearTimeout(heightTimer)
  cancelAnimationFrame(heightFrame)
  cancelAnimationFrame(resizeFrame)
})

function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}

const fmtCount = (n) => (n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n ?? 0))
</script>

<template>
  <Transition name="modal-fade">
    <div v-if="open" class="insights-mask" @click.self="emit('close')">
      <div ref="modalEl" class="insights-modal modal-fade-panel" role="dialog" aria-modal="true" :aria-label="convName + ' 数据洞察'">
        <header class="insights-head">
          <h2>数据洞察 · {{ convName }}</h2>
          <button class="insights-close" aria-label="关闭" @click="emit('close')">✕</button>
        </header>

        <div v-if="loading" class="insights-status">正在统计…（可能需要数秒）</div>
        <div v-else-if="error" class="insights-status">{{ error }} <button class="insights-retry" @click="load">重试</button></div>

        <div v-else-if="data" class="insights-body">
          <div ref="contentEl" class="insights-body-inner">
            <!-- 友谊数据 -->
            <section v-if="data.friendship && data.friendship.first_chat" class="insights-section">
              <h3>友谊数据</h3>
              <div class="friend-grid">
                <div class="friend-item"><span class="friend-num">{{ fmtDate(data.friendship.first_chat) }}</span><span class="friend-label">第一次聊天</span></div>
                <div class="friend-item"><span class="friend-num">{{ data.friendship.days_known }}</span><span class="friend-label">认识天数</span></div>
                <div class="friend-item"><span class="friend-num">{{ data.friendship.total_chat_days }}</span><span class="friend-label">聊过天的天数</span></div>
                <div class="friend-item"><span class="friend-num">{{ data.friendship.longest_mutual_streak }}</span><span class="friend-label">最长连续互聊天数</span></div>
              </div>
              <p class="insights-note">「互聊」按天算：这一天两个人都发过消息才算 1 天，只有一方说话不算。</p>
            </section>

            <!-- 热力图 -->
            <section v-if="data.heatmap && data.heatmap.total" class="insights-section">
              <h3>热力图</h3>
              <div class="heat-summary">
                <template v-if="data.heatmap.peak">
                  最常聊：<b>{{ WEEKDAYS[data.heatmap.peak.weekday] }} {{ data.heatmap.peak.hour }} 点</b>
                  （{{ data.heatmap.peak.count }} 条）
                </template>
                <span class="heat-tag" v-if="data.heatmap.night_ratio > 0.3">深夜型 🌙（深夜消息占 {{ Math.round(data.heatmap.night_ratio * 100) }}%）</span>
                <span class="heat-tag" v-else-if="data.heatmap.morning_ratio > 0.25">早鸟型 ☀️（上午消息占 {{ Math.round(data.heatmap.morning_ratio * 100) }}%）</span>
              </div>
              <div class="heat-table-wrap">
                <table class="heat-table">
                  <thead>
                    <tr><th></th><th v-for="h in 24" :key="h">{{ h - 1 }}</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, wd) in data.heatmap.grid" :key="wd">
                      <th>{{ WEEKDAY_SHORT[wd] }}</th>
                      <td v-for="(count, h) in row" :key="h" :style="cellStyle(count, data.heatmap.peak ? data.heatmap.peak.count : 0)" :title="`${WEEKDAYS[wd]} ${h} 点：${count} 条`"></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>

            <!-- 消息排行 -->
            <section v-if="data.talkers && data.talkers.length" class="insights-section">
              <h3>消息排行</h3>
              <div class="rank-list">
                <div v-for="(t, i) in data.talkers" :key="t.uid" class="rank-item">
                  <span class="rank-no" :class="{ top: i < 3 }">{{ i + 1 }}</span>
                  <span class="rank-name">{{ t.name }}</span>
                  <span class="rank-bar-wrap"><span class="rank-bar" :style="{ width: (t.count / data.talkers[0].count * 100) + '%' }"></span></span>
                  <span class="rank-val">{{ fmtCount(t.count) }} 条 · {{ fmtCount(t.chars) }} 字</span>
                </div>
              </div>
            </section>

            <!-- 表情榜 -->
            <section v-if="data.emojis && data.emojis.length" class="insights-section">
              <h3>最爱的文字表情</h3>
              <div class="emoji-list">
                <div v-for="e in data.emojis" :key="e.name" class="emoji-item">
                  <img class="emoji-img" :src="`/emoji/${encodeURIComponent(e.name)}.webp`" :alt="`[${e.name}]`" loading="lazy" @error="$event.target.replaceWith($event.target.alt)" />
                  <span class="emoji-count">×{{ fmtCount(e.count) }}</span>
                </div>
              </div>
              <p class="insights-note">只统计抖音自带的文字表情（如「[钱]」），图片表情包不在此列。</p>
            </section>

            <!-- 关键词 -->
            <section v-if="data.keywords && data.keywords.length" class="insights-section">
              <h3>常聊的关键词</h3>
              <div class="kw-list">
                <span v-for="(k, i) in data.keywords" :key="k.word" class="kw-tag" :style="{ fontSize: (18 - i * 1.4) + 'px', opacity: 1 - i * 0.08 }">{{ k.word }}<small>{{ k.count }}</small></span>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.insights-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; z-index: 300; padding: 16px; }
.insights-modal { background: var(--panel-bg, #fff); color: var(--ink, #111); border-radius: 12px; max-width: 720px; width: 100%; max-height: 88vh; display: flex; flex-direction: column; box-shadow: 0 12px 40px rgba(0,0,0,.25); }
.insights-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--line, #eee); }
.insights-head h2 { font-size: 16px; font-weight: 600; }
.insights-close { border: none; background: none; font-size: 16px; cursor: pointer; color: inherit; opacity: .6; padding: 4px 8px; }
.insights-close:hover { opacity: 1; }
.insights-status { padding: 40px 20px; text-align: center; color: var(--sub, #888); font-size: 14px; }
.insights-retry { margin-left: 8px; border: 1px solid var(--line, #ddd); background: none; color: inherit; border-radius: 6px; padding: 2px 10px; cursor: pointer; }
.insights-body { overflow-y: auto; padding: 6px 18px 20px; }
/* 测量内容自然高度时用：把各分区的上外边距算进来，避免量少一截 */
.insights-body-inner { display: flow-root; }
.insights-section { margin-top: 18px; }
.insights-section h3 { font-size: 14px; margin-bottom: 10px; opacity: .85; }
.insights-note { font-size: 12px; color: var(--sub, #999); margin-top: 8px; }
.friend-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.friend-item { background: var(--line-soft, #f5f5f5); border-radius: 10px; padding: 12px 10px; text-align: center; display: flex; flex-direction: column; gap: 4px; }
.friend-num { font-size: 16px; font-weight: 600; }
.friend-label { font-size: 12px; color: var(--sub, #888); }
.heat-summary { font-size: 13px; color: var(--sub, #666); margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.heat-tag { background: var(--line-soft, #f0f0f0); border-radius: 6px; padding: 2px 8px; font-size: 12px; }
.heat-table-wrap { overflow-x: auto; }
.heat-table { border-collapse: collapse; font-size: 10px; }
.heat-table th { color: var(--sub, #999); font-weight: 400; padding: 1px 3px; white-space: nowrap; }
.heat-table td { width: 13px; height: 13px; border-radius: 2px; background: var(--line-soft, #f2f2f2); }
.rank-list { display: flex; flex-direction: column; gap: 8px; }
.rank-item { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.rank-no { width: 20px; height: 20px; border-radius: 50%; background: var(--line-soft, #eee); font-size: 11px; display: inline-flex; align-items: center; justify-content: center; flex: none; }
.rank-no.top { background: #f2c94c; color: #6b4e00; font-weight: 600; }
.rank-name { width: 90px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: none; }
.rank-bar-wrap { flex: 1; background: var(--line-soft, #f0f0f0); border-radius: 4px; height: 10px; overflow: hidden; }
.rank-bar { display: block; height: 100%; background: linear-gradient(90deg, #6aab72, #4d8f5c); border-radius: 4px; }
.rank-val { font-size: 12px; color: var(--sub, #888); flex: none; width: 110px; text-align: right; }
.emoji-list { display: flex; flex-wrap: wrap; gap: 12px; }
.emoji-item { display: flex; align-items: center; gap: 5px; background: var(--line-soft, #f5f5f5); border-radius: 10px; padding: 6px 10px; }
.emoji-img { width: 28px; height: 28px; }
.emoji-count { font-size: 12px; color: var(--sub, #666); }
.kw-list { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.kw-tag { background: var(--line-soft, #f0f0f0); color: var(--ink, #333); border-radius: 8px; padding: 4px 10px; line-height: 1.3; }
.kw-tag small { color: var(--sub, #999); margin-left: 4px; font-size: .7em; }
@media (max-width: 560px) { .rank-name { width: 64px; } .rank-val { width: 92px; } }
</style>