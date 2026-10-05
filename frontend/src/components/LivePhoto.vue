<template>
  <div
    ref="root"
    class="live-photo"
    :class="[`live-photo-${variant}`, { 'live-photo-playing': visible }]"
    @mouseenter="enter"
    @mouseleave="leave"
  >
    <img
      class="live-photo-cover"
      :src="cover"
      alt="实况图"
      :loading="loading"
      draggable="false"
    />
    <video
      v-if="hasVideo"
      ref="videoEl"
      class="live-photo-video"
      :src="video"
      preload="none"
      muted
      playsinline
      @loadeddata="onFrame"
      @playing="onFrame"
      @ended="onEnded"
      @error="broken = true"
    ></video>
    <span v-if="hasVideo" class="live-photo-badge">实况</span>
  </div>
</template>

<script setup>
// 实况图（抖音的「会动的图」）：一张静态封面 + 一段两三秒的小视频叠在一起。
//
// 交互约定（跟抖音客户端一样，不用点播放键）：
//   * 鼠标移上去 → 从小视频第一帧开始放，画面由封面淡化到视频，不显示任何控件；
//   * 鼠标移开 → 立刻暂停，并淡化回封面；下次移上来重新从第一帧开始（不是接着放）；
//   * 一直放着不挪开 → 播完停在最后一帧，不会自己弹回封面，直到鼠标移开才淡化回去。
//
// 放大查看用的是同一个组件（variant="zoom"），所以两种尺寸下行为完全一致。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const FADE_MS = 280            // 与 CSS 里的过渡时长保持一致
const props = defineProps({
  cover: { type: String, default: '' },
  video: { type: String, default: '' },
  // inline = 聊天列表里的小图（受外观设置里的图片最大边长限制）
  // zoom   = 点击放大后的全屏大图
  variant: { type: String, default: 'inline' },
  loading: { type: String, default: 'lazy' },
})

const root = ref(null)
const videoEl = ref(null)
const broken = ref(false)     // 视频文件坏了 / 不存在：只当普通图片看
const hovering = ref(false)
const frameReady = ref(false) // 已经解出画面，可以淡入了
let resetTimer = null

const hasVideo = computed(() => !!props.video && !broken.value)
const visible = computed(() => hovering.value && hasVideo.value && frameReady.value)

// 有画面了才淡入，避免先看到一片空白再冒出来。
function onFrame() {
  if (hovering.value) frameReady.value = true
}

// 播放结束：什么都不做 —— 画面停在最后一帧，等鼠标移开再淡化回封面。
function onEnded() {}

async function start() {
  const el = videoEl.value
  if (!el || !hasVideo.value) return
  clearTimeout(resetTimer)
  resetTimer = null
  // 每次悬停都从头开始（读完能停的地方再悬停也是重放）。
  try { el.currentTime = 0 } catch {}
  frameReady.value = el.readyState >= 2
  try {
    await el.play()
  } catch {
    // 浏览器临时不给放（比如还没拿到数据）：封面留着，等 loadeddata 再淡入。
  }
}

function stop() {
  hovering.value = false
  const el = videoEl.value
  if (el && !el.paused) el.pause()
  // 等淡化结束再把时间轴收回开头：淡出过程中看到的仍是刚才那一帧，
  // 不会「啪」地跳回第一帧（下次悬停本来就会重放）。
  clearTimeout(resetTimer)
  resetTimer = setTimeout(() => {
    try { if (el) el.currentTime = 0 } catch {}
  }, FADE_MS + 80)
}

function enter() {
  hovering.value = true
  start()
}

function leave() {
  stop()
}

// 放大查看时鼠标本来就压在图上（不会触发 mouseenter），所以挂载后补一次判断。
onMounted(() => {
  nextTick(() => {
    if (root.value?.matches?.(':hover')) enter()
  })
})

onUnmounted(() => {
  clearTimeout(resetTimer)
  const el = videoEl.value
  if (el && !el.paused) el.pause()
})

// 同一个组件被复用去看另一张实况图时，把状态全部重置。
watch(() => props.video, () => {
  broken.value = false
  frameReady.value = false
  if (hovering.value) start()
})
</script>

<style scoped>
.live-photo {
  position: relative;
  display: inline-block;
  line-height: 0;
}
.live-photo-cover {
  display: block;
  object-fit: contain;
}
.live-photo-inline .live-photo-cover {
  max-width: var(--media-max-size, 240px);
  max-height: var(--media-max-size, 240px);
  border-radius: 8px;
  cursor: pointer;
}
.live-photo-zoom .live-photo-cover {
  max-width: 92vw;
  max-height: 92vh;
  border-radius: 4px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  cursor: zoom-out;
}
/* 视频正好盖住封面：宽高都取封面的尺寸，所以淡化时不会看到尺寸跳变。
   pointer-events:none 让点击、右键、鼠标指针的行为跟一张普通图片完全一样。 */
.live-photo-video {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  background: transparent;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.28s var(--ease-out, ease-out);
}
.live-photo-inline .live-photo-video { border-radius: 8px; }
.live-photo-zoom .live-photo-video { border-radius: 4px; }
.live-photo-playing .live-photo-video { opacity: 1; }

.live-photo-badge {
  position: absolute;
  top: 6px;
  left: 6px;
  padding: 1px 6px 2px;
  font-size: 11px;
  line-height: 1.35;
  letter-spacing: 0.5px;
  color: #fff;
  background: rgba(0, 0, 0, 0.5);
  border-radius: 999px;
  pointer-events: none;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
}
.live-photo-zoom .live-photo-badge {
  top: 10px;
  left: 10px;
  font-size: 13px;
  padding: 2px 9px 3px;
}

@media (prefers-reduced-motion: reduce) {
  .live-photo-video { transition: none; }
}
</style>
