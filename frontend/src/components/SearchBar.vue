<template>
  <aside class="search-panel" aria-label="查找聊天记录" @keydown.escape="$emit('close')">
    <div v-if="mode === 'text'" class="search-header">
      <div class="search-input-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
        <input ref="inputRef" v-model="query" placeholder="搜索当前会话..." aria-label="搜索当前会话" @input="onInput" @keydown.enter="search()" />
        <button v-if="query" class="icon-button" aria-label="清空搜索" @click="clearQuery">×</button>
      </div>
      <button class="icon-button" aria-label="关闭查找" @click="$emit('close')">×</button>
    </div>
    <div v-else class="search-header">
      <button class="icon-button" aria-label="返回搜索" @click="setMode('text')">←</button>
      <strong>{{ headerTitle }}</strong>
      <button class="icon-button close-button" aria-label="关闭查找" @click="$emit('close')">×</button>
    </div>

    <template v-if="mode === 'text' && !query.trim()">
      <div class="search-shortcuts">
        <button @click="setMode('date')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18M8 15h2M14 15h2M8 18h2"/>
          </svg>
          日期
        </button>
        <button @click="setMode('media')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 3-3 6 6"/>
          </svg>
          图片与视频
        </button>
        <button @click="setMode('forward')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M8 7h11v10H8z"/><path d="M8 9H5v10h11"/><path d="m14 11 3 1.5L14 14"/>
          </svg>
          转发聊天记录
        </button>
      </div>
      <p class="search-status">输入关键词开始搜索</p>
    </template>

    <div v-else-if="mode === 'date'" ref="calendarRef" class="search-scroll calendar-scroll">
      <section v-for="month in months" :key="month.key" class="calendar-month">
        <h4>{{ month.title }}</h4>
        <div class="calendar-week"><span v-for="day in ['日', '一', '二', '三', '四', '五', '六']" :key="day">{{ day }}</span></div>
        <div class="calendar-days">
          <span v-for="blank in month.offset" :key="`blank-${blank}`"></span>
          <button v-for="day in month.days" :key="day.date" :data-date="day.date" :disabled="!day.count || loading"
            :class="{ selected: selectedDate === day.date }" :aria-label="`${day.date}，${day.count} 条消息`" :aria-pressed="selectedDate === day.date"
            :title="`${day.count} 条消息`" @click="jumpToDate(day.date)">{{ day.day }}</button>
        </div>
      </section>
      <p v-if="!months.length && !loading && !error" class="search-status">当前会话暂无聊天记录</p>
      <p v-if="loading" class="search-status">加载中...</p>
      <div v-if="error" class="search-status" role="alert">{{ error }} <button @click="loadCalendar">重试</button></div>
    </div>

    <template v-else>
      <div v-if="mode === 'media'" class="media-filters">
        <label><input v-model="includeVideos" type="checkbox" @change="search()" />视频</label>
        <label><input v-model="includeImages" type="checkbox" @change="search()" />图片</label>
      </div>
      <div ref="resultsRef" class="search-scroll" @scroll="onResultsScroll">
        <!-- 图片与视频按时间正序展示（旧上、新下）：更早的一页插在顶部，入口也放顶部 -->
        <!-- 加载中保持按钮占位，避免高度变化把阅读位置顶偏 -->
        <button
          v-if="mode === 'media' && results.length < total"
          class="search-more search-more-top"
          :disabled="loading"
          @click="search(true)"
        >{{ loading ? '加载中…' : `↑ 加载更早的图片与视频（已显示 ${results.length}/${total}）` }}</button>
        <template v-if="mode === 'text' || mode === 'forward'">
          <div v-if="total" class="search-results-header">找到 {{ total }} 条结果</div>
          <button v-for="(item, index) in results" :key="item.msg_id" class="search-result-item" :style="stagger(index)" @click="navigate(item)">
            <div class="result-sender">{{ item.sender_display_name || item.sender_name || '' }}</div>
            <div class="result-content" v-html="highlight(preview(item))"></div>
            <p v-if="mode === 'forward' && forwardSummary(item)" class="result-summary">{{ forwardSummary(item) }}</p>
            <time class="result-time">{{ formatTime(item.timestamp) }}</time>
          </button>
        </template>
        <template v-else-if="mode === 'media'">
          <section v-for="group in mediaGroups" :key="group.date" class="media-group">
            <h4>{{ group.date }}</h4>
            <div class="media-grid">
              <button v-for="(item, index) in group.items" :key="item.msg_id" class="media-tile" :style="stagger(index)" :aria-label="`${preview(item)} ${formatTime(item.timestamp)}，定位到聊天记录`" @click="navigate(item)">
                <img v-if="thumbnail(item)" :src="thumbnail(item)" alt="" loading="lazy" @error="e => e.target.style.display = 'none'" />
                <span v-else class="media-placeholder">{{ isVideo(item) ? '视频' : '图片' }}</span>
                <span v-if="isVideo(item)" class="media-video-badge">▶ <span>{{ getVideoDuration(item) }}</span></span>
              </button>
            </div>
          </section>
        </template>
        <p v-if="!loading && !error && !results.length" class="search-status">{{ emptyHint }}</p>
        <p v-if="loading" class="search-status">加载中...</p>
        <div v-if="error" class="search-status" role="alert">{{ error }} <button @click="search(page > 0)">重试</button></div>
        <button v-if="mode !== 'media' && !loading && results.length < total" class="search-more" @click="search(true)">加载更多（{{ results.length }}/{{ total }}）</button>
      </div>
    </template>
  </aside>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { renderRichText } from '@/lib/highlight'
import { getImageSrc, getVideoPoster, getVideoDuration, isVideoMsg, isJsonVideo, getProfileCard, getForwardInfo, renderSystemMsg } from '@/lib/douyinMessage'
import { sharePreview } from '@/lib/sharePreview'
import { calendarMonths, dateBounds, groupMediaByDate, sortMediaChronologically } from '@/lib/search'

const props = defineProps({ convId: String, convName: String })
const emit = defineEmits(['navigate', 'close'])
const mode = ref('text')
const query = ref('')
const includeImages = ref(true)
const includeVideos = ref(true)
const selectedDate = ref('')
const months = ref([])
const results = ref([])
const total = ref(0)
const page = ref(0)
const loading = ref(false)
const error = ref('')
const inputRef = ref(null)
const calendarRef = ref(null)
const resultsRef = ref(null)
// 旧消息在上、新消息在下：先按时间正序排，再按日期分组。
const mediaGroups = computed(() => groupMediaByDate(sortMediaChronologically(results.value)))
const headerTitle = computed(() => ({
  date: '按日期查找', media: '图片与视频', forward: '转发聊天记录',
}[mode.value] || '查找'))
const emptyHint = computed(() => {
  if (mode.value === 'media' && !includeImages.value && !includeVideos.value) return '请选择图片或视频'
  if (mode.value === 'forward') return '当前会话暂无合并转发记录'
  return '无匹配结果'
})
let timer = null
let controller = null
let generation = 0
// 媒体列表滚到顶部就自动续拉更早的一页；lastDataAt 用来忽略「刚加载完/刚定位」之后的
// 那次滚动事件，避免刚打开（会被定位到底部）时误触发。
const AUTO_LOAD_TOP_PX = 120
let lastDataAt = 0

function cancel() {
  clearTimeout(timer)
  controller?.abort()
  generation++
  loading.value = false
}
function beginRequest() {
  cancel()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  return { id: generation, signal: controller.signal }
}
function resetResults() {
  results.value = []; total.value = 0; page.value = 0; error.value = ''
  if (resultsRef.value) resultsRef.value.scrollTop = 0
}
function clearQuery() {
  cancel(); query.value = ''; resetResults(); inputRef.value?.focus()
}
function reset() {
  cancel(); mode.value = 'text'; query.value = ''; months.value = []; selectedDate.value = ''
  includeImages.value = true; includeVideos.value = true; resetResults()
  nextTick(() => inputRef.value?.focus())
}
watch(() => props.convId, reset)
onMounted(() => inputRef.value?.focus())
onUnmounted(cancel)

function setMode(value) {
  cancel(); mode.value = value; resetResults()
  if (value === 'date') loadCalendar()
  else if (value === 'media' || value === 'forward') search()
  else { query.value = ''; nextTick(() => inputRef.value?.focus()) }
}
function onInput() {
  cancel(); resetResults()
  if (query.value.trim()) timer = setTimeout(() => search(), 400)
}
// 图片与视频：滚到接近顶部时自动加载更早的一页（顶部按钮仍在，可看到进度）。
function onResultsScroll() {
  if (mode.value !== 'media' || loading.value) return
  const el = resultsRef.value
  if (!el || el.scrollTop > AUTO_LOAD_TOP_PX) return
  if (results.value.length >= total.value) return
  // 内容不足一屏时滚不动，交回顶部按钮，免得反复触发。
  if (el.scrollHeight <= el.clientHeight + 4) return
  if (performance.now() - lastDataAt < 400) return
  search(true)
}
async function search(append = false) {
  if (!props.convId) return
  if (mode.value === 'text' && !query.value.trim()) { clearQuery(); return }
  if (mode.value === 'media' && !includeImages.value && !includeVideos.value) { cancel(); resetResults(); return }
  const { id, signal } = beginRequest()
  const nextPage = append ? page.value + 1 : 1
  if (!append) resetResults()
  // 媒体列表正序展示，加载更早的一页会插到顶部：记住当前最旧那一张的位置，
  // 数据更新后按它的实际位移补偿滚动，阅读位置就不跳（不依赖高度差推算，
  // 也不会和浏览器自带的滚动锚定打架）。
  const anchorTile = append && mode.value === 'media'
    ? resultsRef.value?.querySelector('.media-tile') || null : null
  const anchorTop = anchorTile ? anchorTile.getBoundingClientRect().top : null
  const params = new URLSearchParams({ conv_id: props.convId, page_size: '50', page: String(nextPage) })
  if (mode.value === 'text') params.set('q', query.value.trim())
  else if (mode.value === 'forward') params.set('media_type', 'forward')
  else params.set('media_type', includeImages.value && includeVideos.value ? 'media' : includeImages.value ? 'image' : 'video')
  try {
    const res = await fetch(`/api/search?${params}`, { signal })
    if (!res.ok) throw new Error()
    const data = await res.json()
    if (id !== generation) return
    results.value = append ? [...results.value, ...data.items] : data.items
    total.value = data.total; page.value = nextPage
  } catch (e) {
    if (id === generation && e.name !== 'AbortError') error.value = '搜索失败，请重试'
  } finally { if (id === generation) loading.value = false }
  if (id !== generation) return
  // 等「加载中」提示收起、列表布局稳定后再定位，避免被状态行等高度变化顶偏。
  if (anchorTile && anchorTop !== null) {
    // 更早的一页插在顶部：按锚点元素的实际位移把滚动位置挪回去。
    await nextTick()
    const el = resultsRef.value
    const delta = anchorTile.getBoundingClientRect().top - anchorTop
    if (el) el.scrollTop += delta
  } else if (mode.value === 'media') {
    // 一打开（含切换筛选）就停在最新处：正序列表里最新在底部，往上滚看更早。
    await nextTick()
    const el = resultsRef.value
    if (el) el.scrollTop = el.scrollHeight
  }
  lastDataAt = performance.now()
}
async function loadCalendar() {
  const { id, signal } = beginRequest()
  try {
    const tz = -new Date().getTimezoneOffset() / 60
    const res = await fetch(`/api/conversations/${encodeURIComponent(props.convId)}/stats/daily?tz=${tz}`, { signal })
    if (!res.ok) throw new Error()
    const data = await res.json()
    if (id !== generation) return
    months.value = calendarMonths(data.items)
    await nextTick()
    const target = selectedDate.value || data.items.at(-1)?.date
    if (target) calendarRef.value?.querySelector(`[data-date="${target}"]`)?.scrollIntoView({ block: 'center' })
  } catch (e) {
    if (id === generation && e.name !== 'AbortError') error.value = '日历加载失败，请重试'
  } finally { if (id === generation) loading.value = false }
}
async function jumpToDate(date) {
  const bounds = dateBounds(date)
  if (!bounds) return
  const { id, signal } = beginRequest()
  try {
    const tz = -new Date(bounds.start * 1000).getTimezoneOffset() / 60
    const res = await fetch(`/api/conversations/${encodeURIComponent(props.convId)}/messages/by-date?date=${date}&tz=${tz}&limit=1`, { signal })
    if (!res.ok) throw new Error()
    const data = await res.json()
    if (id !== generation) return
    if (data.items.length) { selectedDate.value = date; navigate(data.items[0]) }
    else error.value = '这一天没有聊天记录'
  } catch (e) {
    if (id === generation && e.name !== 'AbortError') error.value = '日期定位失败，请重试'
  } finally { if (id === generation) loading.value = false }
}
function navigate(item) {
  emit('navigate', { ...item, conv_id: props.convId, conv_name: props.convName, search_query: mode.value === 'text' ? query.value.trim() : '' })
}
function isVideo(item) { return isVideoMsg(item) || isJsonVideo(item) }
function preview(item) {
  if (getProfileCard(item)) return `[用户名片] ${getProfileCard(item).name}`
  if (getForwardInfo(item)) return `[聊天记录] ${getForwardInfo(item).title}`
  const share = sharePreview(item)
  if (share) return share
  if (isVideo(item)) return '[视频]'
  if (item.msg_type === 3) return '[图片]'
  if (item.voice_transcription) return item.voice_transcription
  // 系统消息（群通知等）的正文是占位符，显示渲染后的句子：既看得懂，
  // 搜索关键词的高亮也能落在对的地方（跟聊天窗口里显示的一致）。
  if (item.msg_type === 0) {
    return renderSystemMsg(item, localStorage.getItem('selfUid') || '') || item.content || '[消息]'
  }
  return item.content || '[消息]'
}
function thumbnail(item) { return isVideo(item) ? getVideoPoster(item) : getImageSrc(item) }
function forwardSummary(item) {
  const info = getForwardInfo(item)
  if (!info) return ''
  const lines = (info.preview || []).slice(0, 3).map(row => {
    const name = row.nick_name ? `${row.nick_name}：` : ''
    return `${name}${row.text || ''}`.trim()
  }).filter(Boolean)
  const extra = info.count != null && info.count > lines.length ? `等 ${info.count} 条` : ''
  return [...lines, extra].filter(Boolean).join('\n')
}
function highlight(text) { return renderRichText(text, query.value.trim()) }
// 列表项错位浮现：只给前 10 项递增延迟，长列表不会整体被推迟
function stagger(index) { return { '--stagger': `${Math.min(index, 10) * 18}ms` } }
function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}年${pad(d.getMonth() + 1)}月${pad(d.getDate())}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
</script>

<style scoped>
.search-panel { width: 100%; height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--bg-secondary); border-left: 1px solid var(--border-color); color: var(--text-primary); }
.search-header { display: flex; align-items: center; gap: 8px; padding: 12px; min-height: 58px; border-bottom: 1px solid var(--border-color); }
.search-header strong { font-size: 13px; font-weight: 500; }
.close-button { margin-left: auto; }
.icon-button { border: 0; background: transparent; color: var(--text-muted); cursor: pointer; padding: 3px 5px; font-size: 18px; line-height: 1; }
.icon-button:hover { color: var(--text-primary); }
.search-input-wrap { display: flex; align-items: center; gap: 7px; flex: 1; min-width: 0; padding: 7px 9px; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 7px; }
.search-input-wrap:focus-within { border-color: var(--accent); }
.search-input-wrap svg { width: 14px; height: 14px; color: var(--text-muted); flex-shrink: 0; }
.search-input-wrap input { width: 100%; min-width: 0; border: 0; outline: none; background: transparent; color: var(--text-primary); font-size: 13px; }
.search-shortcuts { display: flex; flex-direction: column; gap: 8px; padding: 12px; border-bottom: 1px solid var(--border-color); }
.search-shortcuts button { display: flex; align-items: center; gap: 8px; text-align: left; padding: 9px 12px; background: var(--bg-primary); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 6px; cursor: pointer; transition: border-color var(--dur-fast), transform var(--dur-fast) var(--ease-out); }
.search-shortcuts button:hover { border-color: var(--accent); }
.search-shortcuts button:active { transform: scale(0.985); }
.search-shortcuts svg { width: 20px; height: 20px; flex-shrink: 0; color: var(--text-secondary); }
/* 只淡入、不做位移：列表项本身有错位动画，且滚动补偿靠 getBoundingClientRect 计算，
   容器上挂 transform 会让那套补偿在过渡期间读到偏移值。 */
.search-scroll { overflow-y: auto; flex: 1; min-height: 0; animation: panel-content-in var(--dur-base) var(--ease-out); }
@keyframes panel-content-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.search-status { padding: 28px 12px; text-align: center; font-size: 12px; color: var(--text-muted); }
.search-status button { border: 0; background: none; color: var(--accent); cursor: pointer; }
.search-results-header { padding: 8px 14px; color: var(--text-muted); font-size: 12px; }
.search-result-item { display: block; width: calc(100% - 24px); margin: 0 12px; text-align: left; padding: 13px 0; border: 0; border-bottom: 1px solid var(--border-color); background: none; color: var(--text-primary); cursor: pointer; animation: item-in var(--dur-base) var(--ease-out) both; animation-delay: var(--stagger, 0ms); }
@keyframes item-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.search-result-item:hover { background: var(--bg-tertiary); }
.result-sender { font-size: 12px; margin-bottom: 5px; color: var(--text-secondary); }
.result-content { font-size: 13px; line-height: 1.5; overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow-wrap: anywhere; }
.result-time { display: block; margin-top: 6px; font-size: 11px; color: var(--text-muted); text-align: left; }
.result-summary { margin: 6px 0 0; font-size: 12px; line-height: 1.45; color: var(--text-secondary); white-space: pre-line; overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; }
.search-more { display: block; width: 100%; padding: 18px 10px; background: none; border: 0; color: var(--accent); cursor: pointer; font-size: 12px; }
.search-more-top { border-bottom: 1px solid var(--border-color); }
.search-more:disabled { color: var(--text-muted); cursor: default; }
.media-filters { display: flex; gap: 12px; padding: 10px 12px; font-size: 12px; }
.media-filters label { display: flex; align-items: center; gap: 4px; cursor: pointer; }
.media-filters input { accent-color: var(--accent); }
.media-group { padding: 0 12px 12px; }
.media-group h4 { font-size: 12px; font-weight: 400; padding: 12px 0; }
.media-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 3px; }
.media-tile { position: relative; aspect-ratio: 1; border: 0; padding: 0; background: var(--bg-tertiary); cursor: pointer; overflow: hidden; color: var(--text-secondary); animation: item-in var(--dur-base) var(--ease-out) both; animation-delay: var(--stagger, 0ms); transition: transform var(--dur-fast) var(--ease-out); }
.media-tile img { width: 100%; height: 100%; object-fit: cover; transition: transform var(--dur-base) var(--ease-out); }
.media-tile:hover { transform: scale(1.03); z-index: 1; outline: 2px solid var(--accent); outline-offset: -2px; }
.media-tile:hover img { transform: scale(1.06); }
.media-placeholder { font-size: 12px; }
.media-video-badge { position: absolute; bottom: 0; left: 0; right: 0; display: flex; justify-content: space-between; padding: 4px; color: #fff; background: linear-gradient(transparent, #0009); font-size: 10px; }
.calendar-scroll { padding: 0 12px 20px; }
.calendar-month { margin: 18px 0; }
.calendar-month h4 { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 400; margin: 12px 0; }
.calendar-month h4::before, .calendar-month h4::after { content: ''; flex: 1; height: 1px; background: var(--border-color); }
.calendar-week, .calendar-days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; text-align: center; }
.calendar-week { font-size: 11px; color: var(--text-muted); margin-bottom: 8px; }
.calendar-days button { aspect-ratio: 1; border-radius: 50%; border: 0; background: var(--bg-tertiary); color: var(--text-primary); font-size: 11px; cursor: pointer; transition: background var(--dur-fast), color var(--dur-fast), transform var(--dur-fast) var(--ease-out); }
.calendar-days button:active:not(:disabled) { transform: scale(0.9); }
.calendar-days button:disabled { background: transparent; color: var(--text-muted); opacity: .4; cursor: default; }
.calendar-days button.selected, .calendar-days button:hover:not(:disabled) { background: var(--accent); color: #fff; }
</style>
