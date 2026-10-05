<template>
  <Transition name="lightbox">
    <div v-if="shown" class="lightbox-overlay" @click.self="close">
      <!-- 实况图：放大后鼠标停在图上，直接用同一套悬停播放规则 -->
      <div v-if="live" class="lightbox-live" @click="close">
        <LivePhoto variant="zoom" :cover="displaySrc" :video="live" loading="eager" />
      </div>
      <img v-else class="lightbox-img" :src="displaySrc" @click.self="close" />
      <button class="lightbox-close" @click="close">×</button>
    </div>
  </Transition>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import LivePhoto from './LivePhoto.vue'

// Fullscreen image overlay. Controlled via v-model: the src to show, or null.
// ``live`` 是实况图那段小视频的地址（普通图片不传），放大后同样跟着鼠标播放。
const props = defineProps({
  modelValue: { type: String, default: null },
  live: { type: String, default: null },
})
const emit = defineEmits(['update:modelValue'])

// shown 比 modelValue 晚一步收起：留出淡出时间，关闭时不至于瞬间消失。
// displaySrc 记住最后一张图，淡出过程中画面不会先变空。
const shown = ref(!!props.modelValue)
const displaySrc = ref(props.modelValue || '')
let closing = false

watch(() => props.modelValue, (src) => {
  if (src) {
    displaySrc.value = src
    closing = false
    shown.value = true
  } else if (shown.value) {
    shown.value = false
  }
})

function close() {
  if (closing) return
  closing = true
  shown.value = false
  emit('update:modelValue', null)
}
function onKey(e) {
  if (e.key === 'Escape' && props.modelValue) close()
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<style scoped>
.lightbox-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(0, 0, 0, 0.85);
  display: flex; align-items: center; justify-content: center;
  cursor: zoom-out;
}
.lightbox-enter-active, .lightbox-leave-active {
  transition: opacity 0.18s var(--ease-out);
}
.lightbox-enter-active .lightbox-img, .lightbox-leave-active .lightbox-img,
.lightbox-enter-active .lightbox-live, .lightbox-leave-active .lightbox-live {
  transition: transform 0.18s var(--ease-out), opacity 0.18s var(--ease-out);
}
.lightbox-enter-from, .lightbox-leave-to { opacity: 0; }
.lightbox-enter-from .lightbox-img, .lightbox-leave-to .lightbox-img,
.lightbox-enter-from .lightbox-live, .lightbox-leave-to .lightbox-live {
  opacity: 0;
  transform: scale(0.94);
}
.lightbox-live { line-height: 0; cursor: zoom-out; }
.lightbox-img {
  max-width: 92vw; max-height: 92vh;
  object-fit: contain;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  border-radius: 4px;
  cursor: zoom-out;
}
.lightbox-close {
  position: absolute; top: 20px; right: 28px;
  width: 40px; height: 40px;
  background: rgba(255, 255, 255, 0.1);
  border: none; border-radius: 50%;
  color: #fff; font-size: 28px; line-height: 1;
  cursor: pointer;
  transition: background 0.15s, transform 0.15s var(--ease-out);
}
.lightbox-close:hover { background: rgba(255, 255, 255, 0.2); transform: scale(1.06); }
</style>
