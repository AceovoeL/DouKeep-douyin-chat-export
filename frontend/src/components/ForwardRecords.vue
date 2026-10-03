<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { getForwardInfo } from '@/lib/douyinMessage'
import { renderRichText } from '@/lib/highlight'
import { stopDialogVideos } from '@/lib/mediaPlayback'
import MessageList from './MessageList.vue'

const props = defineProps({ message: Object, selfUid: String })
const info = computed(() => getForwardInfo(props.message))
const dialog = ref(null)
const detail = ref(null)
const loading = ref(false)
const error = ref('')
function onClosed() {
  stopDialogVideos(dialog.value)
}
// 合并转发的三行预览里同样可能有 [钱] 这类文字式表情，跟正文一样换成图片。
function previewText(item) {
  const name = item.nick_name ? `${item.nick_name}：` : ''
  return renderRichText(`${name}${item.text ?? ''}`)
}
onUnmounted(onClosed)
async function open(event) {
  const source = event?.currentTarget?.closest('.forward-card')?.getBoundingClientRect()
  const opening = !dialog.value.open
  dialog.value.showModal()
  if (opening && source && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const target = dialog.value.getBoundingClientRect()
    const x = source.left + source.width / 2 - target.left - target.width / 2
    const y = source.top + source.height / 2 - target.top - target.height / 2
    dialog.value.animate([
      { transform: `translate(${x}px, ${y}px) scale(${source.width / target.width}, ${source.height / target.height})`, opacity: 0 },
      { transform: 'none', opacity: 1 },
    ], { duration: 240, easing: 'cubic-bezier(.2,.8,.2,1)' })
  }
  if (detail.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    if (props.message.forward_detail) detail.value = props.message.forward_detail
    else {
      const res = await fetch(`/api/messages/${encodeURIComponent(props.message.msg_id)}/forward`)
      if (!res.ok) throw new Error()
      detail.value = await res.json()
    }
  } catch { error.value = '加载失败，请重试' }
  finally { loading.value = false }
}
</script>

<template>
  <button class="forward-card" @click="open">
    <strong>{{ info.title }}</strong>
    <span v-for="(item, index) in info.preview.slice(0, 3)" :key="index" v-html="previewText(item)"></span>
    <small>聊天记录<span v-if="info.count !== null"> · {{ info.count }} 条</span> · 点击查看</small>
  </button>
  <dialog ref="dialog" class="forward-dialog" @close="onClosed" @click="e => { if (e.target === dialog) dialog.close() }">
    <header><strong>{{ info.title }}</strong><button aria-label="关闭聊天记录" @click="dialog.close()">关闭</button></header>
    <p v-if="loading">加载中...</p>
    <p v-else-if="error" role="alert">{{ error }} <button @click="open">重试</button></p>
    <template v-else-if="detail">
      <p v-if="!detail.complete" class="forward-notice">已取得 {{ detail.available }}/{{ detail.total }} 条详情，其余仅保留摘要或尚未采集。</p>
      <MessageList :conversation="{ conv_id: message.conv_id || '', name: detail.title }" :embeddedMessages="detail.items" :selfUidOverride="selfUid" />
    </template>
  </dialog>
</template>

<style scoped>
.forward-card { width: 280px; max-width: 100%; display: flex; flex-direction: column; gap: 8px; padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: var(--radius); text-align: left; cursor: pointer; transition: border-color var(--dur-fast), transform var(--dur-fast) var(--ease-out); }
.forward-card:hover { border-color: color-mix(in srgb, var(--accent) 55%, var(--border-color)); transform: translateY(-1px); }
.forward-card:active { transform: scale(0.99); }
.forward-card > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; font-size: 12px; color: var(--text-secondary); }
.forward-card small { width: 100%; padding-top: 8px; border-top: 1px solid var(--border-color); color: var(--text-muted); }
.forward-dialog { margin: auto; width: min(760px, 94vw); max-height: 85vh; padding: 0; border: 1px solid var(--border-color); border-radius: 12px; background: var(--bg-primary); color: var(--text-primary); }
.forward-dialog[open] { display: flex; flex-direction: column; height: min(760px, 85vh); }
.forward-dialog > .msg-panel { min-height: 0; }
.forward-dialog::backdrop { background: #0008; animation: backdrop-in var(--dur-base) var(--ease-out); }
@keyframes backdrop-in { from { opacity: 0; } to { opacity: 1; } }
.forward-dialog header { display: flex; justify-content: space-between; gap: 12px; padding: 16px; position: sticky; top: 0; background: var(--bg-secondary); z-index: 1; }
.forward-dialog header button { cursor: pointer; background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 4px; padding: 4px 10px; }
.forward-dialog p { padding: 12px; font-size: 13px; }
.forward-notice { color: var(--text-secondary); }
</style>
