<template>
  <div
    class="msg-share-card"
    :class="{ 'msg-share-card-comment': info.isCommentShare }"
    :role="clickable ? 'link' : 'group'"
    :tabindex="clickable ? 0 : undefined"
    :aria-label="ariaLabel"
    :title="title || undefined"
    @click="emit('open')"
    @keydown.enter.prevent="emit('open')"
    @keydown.space.prevent="emit('open')"
  >
    <!-- 分享评论（aweType=10500），照抖音客户端的样子分三段：
         ①「分享 @某人 的评论」+ 评论正文（评论带图时补一张小图）
         ② 一条浅灰分割线
         ③ 视频缩略图 + 「来自视频」小字 + 作品标题（两行截断） -->
    <template v-if="info.isCommentShare">
      <div v-if="info.comment || info.commentUser || info.commentImg" class="msg-share-comment">
        <div class="msg-share-comment-label">
          <template v-if="info.commentUser">分享 <b class="msg-share-comment-user">@{{ info.commentUser }}</b> 的评论</template>
          <template v-else>分享的评论</template>
        </div>
        <div v-if="info.comment" class="msg-share-comment-text">{{ info.comment }}</div>
        <img
          v-if="info.commentImg"
          class="msg-share-comment-img"
          :src="info.commentImg"
          alt="评论图片"
          :loading="loading"
          @error="hideBroken"
        />
      </div>
      <template v-if="info.cover || title">
        <div class="msg-share-divider" aria-hidden="true"></div>
        <div class="msg-share-quote">
          <div v-if="info.cover" class="msg-share-quote-thumb">
            <img
              class="msg-share-quote-cover"
              :src="info.cover"
              alt=""
              :loading="loading"
              @error="onCoverError"
            />
            <!-- 视频作品在缩略图正中放一颗小白三角；图文作品没有 -->
            <span v-if="quoteIsVideo" class="msg-share-quote-play" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M8 5.2v13.6a1 1 0 0 0 1.55.83l9.25-6.8a1 1 0 0 0 0-1.66l-9.25-6.8A1 1 0 0 0 8 5.2Z" />
              </svg>
            </span>
          </div>
          <div class="msg-share-quote-body">
            <div class="msg-share-quote-from">{{ quoteLabel }}</div>
            <div v-if="title" class="msg-share-quote-title">{{ title }}</div>
          </div>
        </div>
      </template>
    </template>

    <!-- 没有封面的分享（地点 / 直播推送等）：退回「标题 + 作者」的紧凑样式，
         不要把一整块空封面留在聊天记录里。 -->
    <div v-else-if="!info.cover" class="msg-share-plain">
      <div class="msg-share-plain-title">{{ title || '分享内容' }}</div>
      <div v-if="info.author" class="msg-share-plain-author">@ {{ info.author }}</div>
    </div>

    <!-- 视频 / 图文 / 动图 / 文章：上面是作品封面（比例按封面本身的宽高，
         太窄太宽都夹一下），正中一颗白色播放三角，左下角作者头像 + 昵称；
         封面下方再向下延伸一块同底色的标题条（最多两行，超出省略）。 -->
    <template v-else>
      <div class="msg-share-cover" :style="{ aspectRatio: coverAspect }">
        <!-- 封面过期 / 加载失败时露出这块占位，图片自己隐藏 -->
        <div class="msg-share-cover-empty" aria-hidden="true">
          <span v-if="coverBroken">封面已失效</span>
        </div>
        <img
          class="msg-share-cover-img"
          :src="info.cover"
          alt=""
          :loading="loading"
          @load="onCoverLoad"
          @error="onCoverError"
        />
        <span class="msg-share-play" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M8 5.2v13.6a1 1 0 0 0 1.55.83l9.25-6.8a1 1 0 0 0 0-1.66l-9.25-6.8A1 1 0 0 0 8 5.2Z" />
          </svg>
        </span>
        <div v-if="info.author || info.authorAvatar" class="msg-share-author">
          <img
            v-if="info.authorAvatar && !avatarBroken"
            class="msg-share-author-avatar"
            :src="info.authorAvatar"
            alt=""
            :loading="loading"
            @error="avatarBroken = true"
          />
          <span v-else class="msg-share-author-avatar msg-share-author-fallback">{{ authorInitial }}</span>
          <span v-if="info.author" class="msg-share-author-name">{{ info.author }}</span>
        </div>
      </div>
      <!-- 向下延伸的标题条：底色用卡片主题色（var(--card-bg)），最多两行，超出省略。
           文字放在内层截断，外层只负责留白 —— 直接在带 padding 的盒子上做
           line-clamp 时，浏览器会把第三行的上半截漏在下内边距里。 -->
      <div v-if="info.title" class="msg-share-title">
        <span class="msg-share-title-text">{{ info.title }}</span>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { getShareInfo, shareCardTitle } from '@/lib/douyinMessage'

const props = defineProps({
  message: { type: Object, required: true },
  // 截图模式要同步加载，平时懒加载（和 MessageList 的 imgLoading 一致）
  loading: { type: String, default: 'lazy' },
})
const emit = defineEmits(['open'])

const info = computed(() => getShareInfo(props.message))
const title = computed(() => shareCardTitle(props.message))
const clickable = computed(() => !!(info.value.itemId || info.value.productUrl))
const ariaLabel = computed(() => (title.value ? `打开分享内容：${title.value.slice(0, 80)}` : '打开分享内容'))
const authorInitial = computed(() => (info.value.author || '?').trim().slice(0, 1))

// 分享评论下方那句小字：抖音客户端写「来自视频」/「来自图文」，载荷里的
// media_type 是 2 时按图文算，其余（1、缺省）都当视频。
const quoteIsVideo = computed(() => Number(info.value.mediaType) !== 2)
const quoteLabel = computed(() => (quoteIsVideo.value ? '来自视频' : '来自图文'))

// 封面比例：优先用载荷里带的宽高（800 有 cover_width/cover_height，11054 有
// 排版卡的 default_width/default_height），没有就等图片加载完按图片本身算。
// 夹在 3:4 ～ 16:10 之间：竖屏视频不会被压成一条，横屏也不会把卡片撑太高。
const MIN_ASPECT = 0.75
const MAX_ASPECT = 1.6
const DEFAULT_ASPECT = 0.8
const loadedRatio = ref(0)
const payloadRatio = computed(() => {
  const w = Number(info.value.coverWidth)
  const h = Number(info.value.coverHeight)
  return w > 0 && h > 0 ? w / h : 0
})
const coverAspect = computed(() => {
  const raw = payloadRatio.value || loadedRatio.value || DEFAULT_ASPECT
  return String(Math.min(MAX_ASPECT, Math.max(MIN_ASPECT, raw)).toFixed(4))
})
function onCoverLoad(event) {
  const img = event?.target
  if (payloadRatio.value || loadedRatio.value || !img?.naturalWidth || !img?.naturalHeight) return
  loadedRatio.value = img.naturalWidth / img.naturalHeight
}

// 封面 / 头像加载失败的状态。列表按 msg_id 复用组件，换一条消息要重置。
const coverBroken = ref(false)
const avatarBroken = ref(false)
watch(() => props.message?.msg_id, () => {
  coverBroken.value = false
  avatarBroken.value = false
  loadedRatio.value = 0
})

// 图片过期 / 加载失败时只藏掉图片，让底下的占位或昵称兜底继续显示。
function hideBroken(event) {
  if (event?.target) event.target.style.display = 'none'
}
function onCoverError(event) {
  coverBroken.value = true
  hideBroken(event)
}
</script>

<style scoped>
.msg-share-card {
  /* 卡里的字号、缩略图、留白全部按卡宽的百分比换算（--share-unit = 卡宽的 1%），
     所以面板里改「分享视频卡宽度」时整张卡会一起缩放，而不是只有框变窄。 */
  --share-unit: calc(var(--share-video-card-width) / 100);
  width: var(--share-video-card-width);
  max-width: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--card-bg);
  border: 1px solid var(--card-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  color: var(--text-primary);
  cursor: pointer;
  transition: filter 0.15s var(--ease-out), box-shadow 0.15s var(--ease-out);
}
.msg-share-card:hover {
  filter: brightness(1.05);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.22);
}
.msg-share-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 视频 / 图文卡：整张卡就是封面（高度由行内的 aspect-ratio 决定） */
.msg-share-cover {
  position: relative;
  width: 100%;
  overflow: hidden;
  background: var(--bg-tertiary);
}
.msg-share-cover-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.3s var(--ease-out);
}
.msg-share-card:hover .msg-share-cover-img {
  transform: scale(1.02);
}
.msg-share-cover-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: max(10px, calc(var(--share-unit) * 4));
  color: var(--text-muted);
  background: linear-gradient(135deg, var(--bg-tertiary), color-mix(in srgb, var(--accent) 10%, var(--bg-tertiary)));
}
/* 居中的白色播放三角：参考图里就是一颗白三角，没有圆底 */
.msg-share-play {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: clamp(24px, 12%, 44px);
  display: flex;
  color: #fff;
  filter: drop-shadow(0 1px 4px rgba(0, 0, 0, 0.45));
  pointer-events: none;
  transition: transform 0.15s var(--ease-out);
}
.msg-share-play svg {
  width: 100%;
  height: auto;
  fill: currentColor;
}
.msg-share-card:hover .msg-share-play {
  transform: translate(-50%, -50%) scale(1.06);
}
/* 左下角：作者头像 + 昵称，只有文字阴影，不垫深色胶囊 */
.msg-share-author {
  position: absolute;
  left: calc(var(--share-unit) * 5);
  bottom: calc(var(--share-unit) * 5);
  max-width: calc(100% - var(--share-unit) * 10);
  display: flex;
  align-items: center;
  gap: calc(var(--share-unit) * 2.2);
  pointer-events: none;
}
.msg-share-author-avatar {
  width: max(20px, calc(var(--share-unit) * 9.4));
  height: max(20px, calc(var(--share-unit) * 9.4));
  flex: 0 0 max(20px, calc(var(--share-unit) * 9.4));
  border-radius: 50%;
  object-fit: cover;
}
.msg-share-author-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent);
  color: #fff;
  font-size: max(10px, calc(var(--share-unit) * 4.7));
  font-weight: 600;
}
.msg-share-author-name {
  min-width: 0;
  color: #fff;
  font-size: max(10px, calc(var(--share-unit) * 4.8));
  line-height: 1.2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.6);
}
/* 封面下方延伸出来的标题条：底色跟卡片一致（随查看器主题变），最多两行，
   超出的部分收成省略号。字号/留白同样按卡宽换算。 */
.msg-share-title {
  padding: calc(var(--share-unit) * 4.2) calc(var(--share-unit) * 5.2) calc(var(--share-unit) * 4.8);
  background: var(--card-bg);
}
.msg-share-title-text {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  color: var(--text-primary);
  font-size: max(11px, calc(var(--share-unit) * 5.2));
  line-height: 1.45;
  word-break: break-word;
}

/* 分享评论卡：白底（跟随主题）、四周留白、中间一条浅灰分割线。
   宽度用单独一项「分享评论卡宽度」，--share-unit 也跟着换成它的 1%，
   这样字号、缩略图和留白同样按比例缩放。 */
.msg-share-card-comment {
  --share-unit: calc(var(--share-comment-card-width) / 100);
  width: var(--share-comment-card-width);
  padding: calc(var(--share-unit) * 5.6);
}
.msg-share-comment {
  display: flex;
  flex-direction: column;
  gap: calc(var(--share-unit) * 2.8);
  min-width: 0;
}
.msg-share-comment-label {
  min-width: 0;
  font-size: max(10px, calc(var(--share-unit) * 5.3));
  line-height: 1.4;
  color: var(--text-muted);
  word-break: break-word;
}
.msg-share-comment-user {
  color: var(--text-secondary);
  font-weight: 600;
}
.msg-share-comment-text {
  font-size: max(12px, calc(var(--share-unit) * 5.9));
  line-height: 1.5;
  color: var(--text-primary);
  word-break: break-word;
}
.msg-share-comment-img {
  display: block;
  /* 评论图靠左对齐。父级是 column flex，默认 align-items: stretch 会把
     <img> 的盒子拉满整张卡的宽度，图本身再由 object-fit: contain 居中——
     看着就像图片居中。align-self: flex-start 让盒子只占图片自己的宽度，
     从左边排起。 */
  align-self: flex-start;
  max-width: 100%;
  max-height: calc(var(--share-unit) * 40);
  border-radius: 8px;
  object-fit: contain;
}
.msg-share-divider {
  height: 1px;
  margin: calc(var(--share-unit) * 4.7) 0 calc(var(--share-unit) * 5);
  background: color-mix(in srgb, var(--text-primary) 12%, transparent);
}
/* 分割线下面：左边方形缩略图，右边「来自视频」+ 标题两行 */
.msg-share-quote {
  display: flex;
  gap: calc(var(--share-unit) * 4.7);
  align-items: flex-start;
  min-width: 0;
}
.msg-share-quote-thumb {
  position: relative;
  width: calc(var(--share-unit) * 25.5);
  height: calc(var(--share-unit) * 25.5);
  flex: 0 0 calc(var(--share-unit) * 25.5);
  border-radius: 8px;
  overflow: hidden;
  background: var(--bg-tertiary);
}
.msg-share-quote-cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.msg-share-quote-play {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: max(16px, calc(var(--share-unit) * 8.2));
  display: flex;
  color: #fff;
  filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.5));
  pointer-events: none;
}
.msg-share-quote-play svg {
  width: 100%;
  height: auto;
  fill: currentColor;
}
.msg-share-quote-body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: max(2px, calc(var(--share-unit) * 1.3));
}
.msg-share-quote-from {
  font-size: max(9px, calc(var(--share-unit) * 3.8));
  line-height: 1.4;
  color: var(--text-muted);
}
.msg-share-quote-title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: max(11px, calc(var(--share-unit) * 5.3));
  line-height: 1.45;
  color: var(--text-primary);
  word-break: break-word;
}

/* 没有封面的分享：紧凑的「标题 + 作者」 */
.msg-share-plain {
  display: flex;
  flex-direction: column;
  gap: calc(var(--share-unit) * 1.6);
  padding: calc(var(--share-unit) * 4.5);
}
.msg-share-plain-title {
  font-size: max(11px, calc(var(--share-unit) * 5.3));
  line-height: 1.45;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.msg-share-plain-author {
  font-size: max(9px, calc(var(--share-unit) * 3.8));
  color: var(--text-muted);
}
</style>
