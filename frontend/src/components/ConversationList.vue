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
    <TransitionGroup v-if="!loading || conversations.length" name="conv" tag="div" class="conv-items">
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
      <div v-if="!loading && !loadError && conversations.length === 0" key="conv-empty" class="conv-empty">
        {{ searchQuery ? '没有匹配的会话' : '暂无会话数据' }}
      </div>
    </TransitionGroup>

    <!-- 首次加载：只有**确认**一条会话都没有时才显示「暂无会话数据」。
         数据量大时这一栏要等一会儿（要数每个会话显示出来的条数），
         以前这段等待也被画成了同一句，看着像数据丢了。 -->
    <div v-if="loading && conversations.length === 0" class="conv-empty conv-loading" role="status">
      <span class="ui-spinner" aria-hidden="true"></span>
      <span>正在加载会话…</span>
    </div>
    <div v-else-if="loadError && conversations.length === 0" class="conv-empty conv-error" role="alert">
      {{ loadError }}
      <button class="conv-retry" @click="fetchConversations(searchQuery)">重试</button>
    </div>

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
              >（实际显示 {{ shownCount(pendingDelete) }} 条）</template>，此操作不可恢复。
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
// 首次加载中：这时候「一条会话都没有」还**不能**断言，界面要画加载态而不是
// 「暂无会话数据」。抓取刚结束时后端要为每个会话数一遍显示条数，这一等可能是
// 几秒到几十秒（见 backend/database.py 里 display_count 的说明）。
const loading = ref(false)
const loadError = ref('')
let searchTimeout = null
// 搜索/重试的并发保护：慢请求后回来时，不能把更新的结果覆盖掉。
let fetchId = 0

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
  const requestId = ++fetchId
  loading.value = true
  loadError.value = ''
  const params = new URLSearchParams({ page_size: '200' })
  if (search) params.set('search', search)
  try {
    const res = await fetch(`/api/conversations?${params}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    if (!Array.isArray(data.items)) throw new Error('会话列表响应格式错误')
    if (requestId !== fetchId) return
    conversations.value = data.items
  } catch (e) {
    if (requestId !== fetchId) return
    // 失败时保留原有列表：宁可显示旧数据，也不要让用户以为会话被清空了。
    loadError.value = `会话列表加载失败（${e.message || e}）`
    console.error('加载会话列表失败', e)
  } finally {
    if (requestId === fetchId) loading.value = false
  }
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

/* 加载中：转圈 + 一句说明。首次加载和「真的没有会话」必须长得不一样，
   否则慢的时候用户会以为数据没了。 */
.conv-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
}

.conv-error {
  color: var(--text-muted);
  font-size: 13px;
}

.conv-retry {
  margin-left: 8px;
  padding: 3px 10px;
  border: 1px solid var(--border-color);
  border-radius: 5px;
  background: var(--bg-primary);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.conv-retry:hover {
  color: var(--accent);
  border-color: var(--accent);
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

/* ── 手机：抽屉里一行行拉开一点，手指点得准，也不会误碰 ──
   删除按钮在桌面靠 hover 才出现，手机上根本没有 hover，不常显就等于没有删除功能。 */
@media (max-width: 768px) {
  .conv-header { padding: 14px 16px; }
  .conv-search { padding: 10px 14px; }
  .conv-item { padding: 13px 14px; gap: 12px; }
  .conv-delete { opacity: 1; width: 28px; height: 28px; }
}
</style>
