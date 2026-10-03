<script setup>
// 截图模式页面（/screenshot）：无导航 chrome，静态渲染一个消息区间，
// 供后端 headless 浏览器整页截图。就绪后设置 window.__shotReady 供其等待。
import { ref, onMounted } from 'vue'
import MessageList from './MessageList.vue'
import { applyAppearance, cachedAppearance, fetchAppearance } from '../lib/appearance.js'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const title = params.get('title') || ''
const subtitle = params.get('subtitle') || ''
const selfUid = params.get('self_uid') || ''
const startSeq = Number(params.get('start_seq') || 0)
const endSeq = Number(params.get('end_seq') || 0)
const convId = params.get('conv_id') || ''

const conv = ref(null)
const staticRange = { startSeq, endSeq }

onMounted(async () => {
  // 先把外观套完再渲染消息，否则截图可能拍到还没上色的画面。
  // theme=auto 表示"用服务器上的外观"；其它主题名保持原行为（只换配色，
  // 字号/间距/字体仍然跟随服务器的外观设置）。
  const payload = (await fetchAppearance()) || cachedAppearance()
  applyAppearance(payload, { theme: theme === 'auto' ? undefined : theme })
  // watch(conversation) 无 immediate，须在挂载后赋值才会触发加载
  conv.value = { conv_id: convId, name: '' }
})

// 所有图片 settle（加载完或出错）后才宣布就绪，超时兜底 15s
async function onStaticLoaded() {
  const imgs = Array.from(document.images)
  const settle = (img) =>
    img.complete
      ? Promise.resolve()
      : new Promise((resolve) => {
          img.addEventListener('load', resolve, { once: true })
          img.addEventListener('error', resolve, { once: true })
        })
  const timeout = new Promise((resolve) => setTimeout(resolve, 15000))
  await Promise.race([Promise.all(imgs.map(settle)), timeout])
  window.__shotReady = true
}
</script>

<template>
  <div id="shot-root" class="shot-root">
    <div v-if="title" class="shot-header">
      <div class="shot-title">{{ title }}</div>
      <div v-if="subtitle" class="shot-subtitle">{{ subtitle }}</div>
    </div>
    <MessageList
      :conversation="conv"
      :staticRange="staticRange"
      :selfUidOverride="selfUid"
      @staticLoaded="onStaticLoaded"
    />
  </div>
</template>

<style scoped>
.shot-root {
  background: var(--bg-primary);
}
.shot-header {
  padding: 22px 20px 14px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}
.shot-title {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.12em;
  color: var(--accent);
  display: flex;
  align-items: center;
  gap: 8px;
}
.shot-title::before {
  content: "";
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 20%, transparent);
}
.shot-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-muted);
  letter-spacing: 0.06em;
}
</style>
