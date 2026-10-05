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
    <!-- 「实况」小标：点一下就从头播一次，播完自己淡回封面。
         上面那个小三角就是"这里能点"的提示 —— 手机上既没有鼠标悬停，也没有播放控件，
         不点它就没法让图动起来。
         @click.stop 是必须的：这次点击别再往上冒泡，冒上去会变成"打开/关掉放大查看"。 -->
    <button
      v-if="hasVideo"
      type="button"
      class="live-photo-badge"
      :title="badgeHint"
      :aria-label="badgeHint"
      @click.stop="playOnce"
    >
      <svg class="live-photo-badge-icon" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
        <path d="M3.3 1.7 10 6l-6.7 4.3z" />
      </svg>
      <span class="live-photo-badge-text">实况</span>
    </button>
  </div>
</template>

<script setup>
// 实况图（抖音的「会动的图」）：一张静态封面 + 一段两三秒的小视频叠在一起。
//
// 交互约定：
//   * 点左上角带小三角的「实况」小标 → 从头播一次，**播完自己淡回封面**
//     （手机上没有鼠标悬停，这是唯一能让图动起来的入口；电脑上点它也一样）；
//   * 鼠标移到图上 → 从小视频第一帧开始放，画面由封面淡化到视频，不显示任何控件；
//   * 鼠标移开 → 立刻暂停，并淡化回封面；下次移上来重新从第一帧开始（不是接着放）；
//   * 一直放着不挪开 → 播完停在最后一帧，不会自己弹回封面，直到鼠标移开才淡化回去
//     （点小标播的那次不这样：它放完就回封面，鼠标还压在上面也一样）。
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
const hovering = ref(false)   // 鼠标压在图上（桌面端"瞄一眼"的预览播放）
const pinned = ref(false)     // 点了「实况」小标：这一次要放完整，放完自己淡回封面
const frameReady = ref(false) // 已经解出画面，可以淡入了
let resetTimer = null

const hasVideo = computed(() => !!props.video && !broken.value)
const visible = computed(() => (hovering.value || pinned.value) && hasVideo.value && frameReady.value)
// 悬停时是"播放"，已经动起来时再点就是"重播"，把话说清楚一点。
const badgeHint = computed(() => (visible.value ? '重新播放实况图' : '播放实况图'))

// 有画面了才淡入，避免先看到一片空白再冒出来。
function onFrame() {
  if (hovering.value || pinned.value) frameReady.value = true
}

// 播放结束：点小标播的那次播完自己淡回封面（悬停预览仍然停在最后一帧，见 leave）。
function onEnded() {
  if (pinned.value) stop()
}

async function start() {
  const el = videoEl.value
  if (!el || !hasVideo.value) return
  clearTimeout(resetTimer)
  resetTimer = null
  // 每次播放都从头开始（读完能停的地方再播也是重放）。
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
  pinned.value = false
  const el = videoEl.value
  if (el && !el.paused) el.pause()
  // 等淡化结束再把时间轴收回开头：淡出过程中看到的仍是刚才那一帧，
  // 不会「啪」地跳回第一帧（下次播放本来就会重放）。
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
  // 点「实况」小标出来的那次意思是"让我看完整"：鼠标顺手挪开也让它放完
  // （放完了 onEnded 会淡回封面）。其余情况照旧——移开就停、淡回封面。
  const el = videoEl.value
  if (pinned.value && el && !el.paused && !el.ended) return
  stop()
}

// 点「实况」小标：从第一帧开始放一次。已经在放的时候再点就是重播，
// 播完（onEnded）自动淡回封面。
function playOnce() {
  if (!hasVideo.value) return
  pinned.value = true
  start()
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
  pinned.value = false
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

/* 「实况」小标 = 一个小三角 + 「实况」两个字，是个能点的按钮。
   视觉上还只是个小圆标，但 ::after 把能点的范围往外撑了 7px：
   小标本身太窄，手指点不准，而它是手机上唯一能让实况图动起来的入口。 */
.live-photo-badge {
  position: absolute;
  top: 6px;
  left: 6px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px 3px 6px;
  font-family: inherit;
  font-size: 11px;
  line-height: 1.35;
  letter-spacing: 0.5px;
  color: #fff;
  background: rgba(0, 0, 0, 0.5);
  border: none;
  border-radius: 999px;
  cursor: pointer;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
  transition: background 0.15s, transform 0.15s var(--ease-out, ease-out);
}
.live-photo-badge::after {
  content: '';
  position: absolute;
  inset: -7px;
}
.live-photo-badge:hover { background: rgba(0, 0, 0, 0.68); }
.live-photo-badge:active { transform: scale(0.94); }
.live-photo-badge-icon {
  width: 9px;
  height: 9px;
  flex-shrink: 0;
}
.live-photo-zoom .live-photo-badge {
  top: 10px;
  left: 10px;
  font-size: 13px;
  padding: 3px 11px 4px 9px;
}
.live-photo-zoom .live-photo-badge-icon {
  width: 11px;
  height: 11px;
}

@media (prefers-reduced-motion: reduce) {
  .live-photo-video { transition: none; }
  .live-photo-badge { transition: none; }
}
</style>
