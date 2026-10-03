<template>
  <div class="conv-list">
    <div class="conv-header">
      <h2>会话</h2>
      <a class="panel-link" href="/panel">管理面板</a>
    </div>
    <div class="conv-search">
      <input
        v-model="searchQuery"
        placeholder="搜索会话..."
        @input="onSearch"
      />
    </div>
    <!-- 会话增删走 TransitionGroup：删除时相邻条目平滑上移，新增（如搜索结果）淡入 -->
    <TransitionGroup name="conv" tag="div" class="conv-items">
      <div
        v-for="conv in conversations"
        :key="conv.conv_id"
        class="conv-item"
        :class="{ active: conv.conv_id === activeId }"
        @click="$emit('select', conv)"
      >
        <div class="conv-avatar">
          <img v-if="getConvAvatar(conv)" :src="getConvAvatar(conv)" @error="e => e.target.style.display='none'" />
          <span v-else>{{ (conv.name || '?')[0] }}</span>
        </div>
        <div class="conv-info">
          <div class="conv-name">{{ conv.name || '未命名' }}</div>
          <div class="conv-meta">
            <span>{{ shownCount(conv) }} 条消息</span>
          </div>
        </div>
        <button
          class="conv-delete"
          title="删除该会话数据"
          @click.stop="requestDelete(conv)"
        >×</button>
      </div>
      <div v-if="conversations.length === 0" key="conv-empty" class="conv-empty">
        暂无会话数据
      </div>
    </TransitionGroup>

    <!-- Custom confirm modal -->
    <Transition name="modal">
      <div v-if="pendingDelete" class="modal-backdrop" @click.self="cancelDelete">
        <div class="modal-box">
          <div class="modal-title">删除会话</div>
          <div class="modal-body">
            确定删除会话「<strong>{{ pendingDelete.name || '未命名' }}</strong>」的所有数据？
            <div class="modal-sub">
              共 {{ rawCount(pendingDelete) }} 条原始记录<template
                v-if="rawCount(pendingDelete) !== shownCount(pendingDelete)"
              >（界面上显示 {{ shownCount(pendingDelete) }} 条）</template>，此操作不可恢复。
            </div>
          </div>
          <div class="modal-actions">
            <button class="btn btn-cancel" @click="cancelDelete" :disabled="deleting">取消</button>
            <button class="btn btn-danger" @click="confirmDelete" :disabled="deleting">
              <span v-if="deleting" class="ui-spinner"></span>
              {{ deleting ? '删除中' : '删除' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { resolveAvatarUrl } from '@/lib/media'

const props = defineProps({
  activeId: String,
})
const emit = defineEmits(['select', 'deleted'])

const conversations = ref([])
const searchQuery = ref('')
const pendingDelete = ref(null)
const deleting = ref(false)
let searchTimeout = null

function getConvAvatar(conv) {
  return resolveAvatarUrl(conv.avatar_url)
}

// 列表里显示「真正画得出来的条数」：抖音给双方各发一份的镜像、空载荷等不算。
function shownCount(conv) {
  const display = conv?.display_count
  if (display === null || display === undefined) return conv?.message_count || 0
  return display
}

// 删除时按数据库里的原始记录数说，避免"删了 6 条却说 2 条"的误会。
function rawCount(conv) {
  return conv?.message_count || 0
}

async function fetchConversations(search = '') {
  const params = new URLSearchParams({ page_size: '200' })
  if (search) params.set('search', search)
  const res = await fetch(`/api/conversations?${params}`)
  const data = await res.json()
  conversations.value = data.items
}

function onSearch() {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    fetchConversations(searchQuery.value)
  }, 300)
}

function requestDelete(conv) {
  pendingDelete.value = conv
}

function cancelDelete() {
  if (deleting.value) return
  pendingDelete.value = null
}

async function confirmDelete() {
  const conv = pendingDelete.value
  if (!conv) return
  deleting.value = true
  try {
    // Use POST alias to avoid reverse proxies that block DELETE
    const res = await fetch(`/api/conversations/${encodeURIComponent(conv.conv_id)}/delete`, {
      method: 'POST',
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      alert(`删除失败：${res.status} ${body}`)
      return
    }
    conversations.value = conversations.value.filter(c => c.conv_id !== conv.conv_id)
    emit('deleted', conv.conv_id)
    pendingDelete.value = null
  } catch (e) {
    alert(`删除失败：${e.message || e}`)
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  fetchConversations()
})
</script>

<style scoped>
.conv-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border-color);
}

.conv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
  border-bottom: 1px solid var(--border-color);
}
.conv-header h2 {
  font-size: 16px;
  font-weight: 600;
}
.panel-link { margin-right: auto; margin-left: 10px; padding: 4px 7px; border: 1px solid var(--border-color); border-radius: 4px; font-size: 12px; text-decoration: none; color: var(--text-secondary); }
.panel-link:hover { color: var(--accent); border-color: var(--accent); }


.conv-search {
  padding: 8px 12px;
}
.conv-search input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}
.conv-search input:focus {
  border-color: var(--accent);
}

.conv-items {
  flex: 1;
  overflow-y: auto;
  position: relative;
}

.conv-item {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 11px 14px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.15s, border-color 0.15s, transform 0.15s var(--ease-out);
}
.conv-item:hover {
  background: color-mix(in srgb, var(--text-primary) 5%, transparent);
}
.conv-item:active {
  transform: scale(0.994);
}
.conv-item.active {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-left: 3px solid var(--accent);
}

.conv-avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: var(--bg-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 600;
  flex-shrink: 0;
  color: var(--text-secondary);
  overflow: hidden;
  transition: box-shadow var(--dur-fast) var(--ease-out), transform var(--dur-fast) var(--ease-out);
}
.conv-item.active .conv-avatar {
  box-shadow: 0 0 0 2px var(--accent);
}
.conv-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.conv-avatar span {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.conv-info {
  flex: 1;
  min-width: 0;
}
.conv-name {
  font-size: var(--conv-name-size);
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-meta {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}

.conv-delete {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, background 0.15s, color 0.15s, transform 0.15s var(--ease-out);
  display: flex;
  align-items: center;
  justify-content: center;
}
.conv-item:hover .conv-delete {
  opacity: 1;
}
.conv-delete:hover {
  background: #e53935;
  color: #fff;
  transform: scale(1.12);
}

.conv-empty {
  padding: 30px;
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}

/* 会话条目的增删过渡：插入淡入，删除时离场条目脱离文档流、其余条目平滑上移。
   必须写在 .conv-item 之后 —— 展开后的 transition 属性与条目自身的过渡同权重。 */
.conv-enter-active, .conv-leave-active {
  transition: opacity var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out);
}
.conv-enter-from, .conv-leave-to {
  opacity: 0;
  transform: translateX(-12px);
}
.conv-leave-active {
  position: absolute;
  left: 0;
  right: 0;
}
.conv-move {
  transition: transform var(--dur-base) var(--ease-out);
}

/* Confirm modal */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}

.modal-box {
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  min-width: 320px;
  max-width: 90vw;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.4);
  overflow: hidden;
}

/* 弹窗进出场：遮罩淡入淡出，卡片轻微缩放（关闭时同样有过渡） */
.modal-enter-active, .modal-leave-active {
  transition: opacity var(--dur-fast) var(--ease-out);
}
.modal-enter-active .modal-box, .modal-leave-active .modal-box {
  transition: transform var(--dur-fast) var(--ease-out), opacity var(--dur-fast) var(--ease-out);
}
.modal-enter-from, .modal-leave-to {
  opacity: 0;
}
.modal-enter-from .modal-box, .modal-leave-to .modal-box {
  opacity: 0;
  transform: scale(0.94);
}

.modal-title {
  padding: 16px 20px;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border-color);
}

.modal-body {
  padding: 20px;
  font-size: 14px;
  color: var(--text-primary);
  line-height: 1.6;
}
.modal-body strong {
  color: var(--accent);
  word-break: break-all;
}
.modal-sub {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-muted);
}

.modal-actions {
  display: flex;
  gap: 10px;
  padding: 12px 20px 18px;
  justify-content: flex-end;
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 18px;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: filter 0.15s, background 0.15s, transform 0.15s var(--ease-out);
}
.btn:active:not(:disabled) {
  transform: scale(0.97);
}
.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn-cancel {
  background: var(--bg-tertiary);
  color: var(--text-primary);
}
.btn-cancel:hover:not(:disabled) {
  filter: brightness(1.15);
}
.btn-danger {
  background: #e53935;
  color: #fff;
}
.btn-danger:hover:not(:disabled) {
  background: #c62828;
}
</style>
