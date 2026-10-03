<script setup>
import { computed, ref, onMounted, onUnmounted, nextTick } from 'vue'
import ConversationList from './components/ConversationList.vue'
import MessageList from './components/MessageList.vue'
import SearchBar from './components/SearchBar.vue'
import ScreenshotView from './components/ScreenshotView.vue'
import {
  applyAppearance,
  cachedAppearance,
  fetchAppearance,
  setThemeOverride,
  themeOverride,
} from './lib/appearance.js'

// 截图模式：后端 headless 浏览器访问 /screenshot?...&token=<临时token>
const isScreenshotMode = location.pathname === '/screenshot'

const activeConversation = ref(null)
const searchHighlight = ref('')
const jumpToSeq = ref(null)
const mobileLayout = window.matchMedia('(max-width: 768px)')
const sidebarOpen = ref(!mobileLayout.matches)
const syncSidebarLayout = () => { sidebarOpen.value = !mobileLayout.matches }
mobileLayout.addEventListener('change', syncSidebarLayout)
onUnmounted(() => mobileLayout.removeEventListener('change', syncSidebarLayout))
const searchOpen = ref(false)
const readerLayout = ref(null)
async function setSearchOpen(open) {
  const list = readerLayout.value?.querySelector('.msg-list')
  const bottom = list && list.scrollHeight - list.scrollTop - list.clientHeight < 2
  const top = list?.getBoundingClientRect().top || 0
  const anchor = list && [...list.querySelectorAll(':scope > .msg-item')].find(row => row.getBoundingClientRect().bottom > top)
  const offset = anchor?.getBoundingClientRect().top
  searchOpen.value = open
  await nextTick()
  if (!list?.isConnected) return
  if (bottom) list.scrollTop = list.scrollHeight - list.clientHeight
  else if (anchor?.isConnected) list.scrollTop += anchor.getBoundingClientRect().top - offset
}

// Auth
const authChecking = ref(true)
const authenticated = ref(false)
const loginPassword = ref('')
const loginError = ref('')
const authToken = ref(localStorage.getItem('authToken') || '')
if (isScreenshotMode) {
  const qsToken = new URLSearchParams(location.search).get('token')
  if (qsToken) authToken.value = qsToken
}

async function checkAuth() {
  try {
    const headers = authToken.value ? { Authorization: `Bearer ${authToken.value}` } : {}
    const res = await fetch('/api/auth/check', { headers })
    const data = await res.json()
    if (!data.need_password || data.authenticated) {
      authenticated.value = true
    }
  } catch {}
  authChecking.value = false
}

async function doLogin() {
  loginError.value = ''
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: loginPassword.value }),
    })
    if (!res.ok) {
      loginError.value = '密码错误'
      return
    }
    const data = await res.json()
    authToken.value = data.token
    localStorage.setItem('authToken', data.token)
    authenticated.value = true
  } catch {
    loginError.value = '登录失败'
  }
}

// Inject auth token into all fetch requests
const _origFetch = window.fetch
window.fetch = function(url, opts = {}) {
  if (authToken.value && typeof url === 'string' && url.startsWith('/api/')) {
    opts.headers = opts.headers || {}
    if (opts.headers instanceof Headers) {
      opts.headers.set('Authorization', `Bearer ${authToken.value}`)
    } else {
      opts.headers['Authorization'] = `Bearer ${authToken.value}`
    }
  }
  return _origFetch.call(this, url, opts)
}

onMounted(async () => {
  if (isScreenshotMode) return  // 截图模式由 ScreenshotView 按 theme 参数自己套用外观
  checkAuth()
  const payload = await fetchAppearance()
  if (!payload) return
  appearance.value = payload
  applyAppearance(payload, { theme: overrideId.value || undefined })
})

const themes = [
  { id: 'dark',   label: '深蓝', color: '#1a1a2e' },
  { id: 'wechat', label: '微信', color: '#95ec69' },
  { id: 'light',  label: '浅色', color: '#ffffff' },
  { id: 'warm',   label: '暖棕', color: '#8b5e3c' },
  { id: 'purple', label: '紫夜', color: '#6a2fad' },
]
themes.unshift({ id: null, label: '服务器样式', color: '#8a8f9c' })

const BUILTIN_DOT_COLORS = { dark: '#1a1a2e', wechat: '#95ec69', light: '#ffffff', warm: '#8b5e3c', purple: '#6a2fad' }

// 服务器上的外观配置（接口返回的 {appearance, defaults, fonts}）。拿不到时保持
// style.css 里的默认外观，不影响使用。
const appearance = ref(cachedAppearance())
const overrideId = ref(themeOverride())
const currentTheme = computed(() => overrideId.value || appearance.value?.appearance?.viewer?.theme || 'dark')

// 右上角圆点：第一颗是「跟随服务器样式」，其余是内置主题的本机临时覆盖。
function themeDotColor(t) {
  if (t.id) return t.color
  const viewer = appearance.value?.appearance?.viewer
  if (viewer?.theme === 'custom') return viewer.colors?.accent || '#8a8f9c'
  return BUILTIN_DOT_COLORS[viewer?.theme] || '#8a8f9c'
}

applyAppearance(appearance.value, { theme: overrideId.value || undefined })

// 主题切换时临时开启颜色过渡，避免首次渲染和后续交互都被 transition 影响
let themeAnimTimer = null
function applyTheme(id, animate = false) {
  // id 为 null 表示「跟随服务器样式」；否则只是本机临时覆盖，不影响别人。
  overrideId.value = id || null
  setThemeOverride(id || null)
  applyAppearance(appearance.value, { theme: id || undefined })
  if (!animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  const root = document.documentElement
  root.classList.add('theme-anim')
  clearTimeout(themeAnimTimer)
  themeAnimTimer = setTimeout(() => root.classList.remove('theme-anim'), 320)
}
onUnmounted(() => {
  clearTimeout(themeAnimTimer)
  document.documentElement.classList.remove('theme-anim')
})

function selectConversation(conv) {
  activeConversation.value = conv
  searchHighlight.value = ''
  if (mobileLayout.matches) sidebarOpen.value = false
}

function onConversationDeleted(convId) {
  if (activeConversation.value?.conv_id === convId) {
    activeConversation.value = null
  }
}

function navigateToMessage(item) {
  activeConversation.value = {
    conv_id: item.conv_id,
    name: item.conv_name || '未知',
  }
  searchHighlight.value = item.search_query ?? ''
  jumpToSeq.value = item.seq || null
}
</script>

<template>
  <!-- Screenshot mode: chrome-less static render for headless capture -->
  <ScreenshotView v-if="isScreenshotMode" />
  <!-- Loading -->
  <div v-else-if="authChecking" class="login-screen">
    <div class="login-box">
      <div class="login-loading"><span class="ui-spinner"></span>正在检查登录状态</div>
    </div>
  </div>
  <!-- Login -->
  <div v-else-if="!authenticated" class="login-screen">
    <div class="login-box">
      <div class="login-title">抖音聊天记录</div>
      <div class="login-subtitle">请输入密码</div>
      <form @submit.prevent="doLogin" class="login-form">
        <input
          v-model="loginPassword"
          type="password"
          placeholder="密码"
          class="login-input"
          autofocus
        />
        <button type="submit" class="login-btn">登录</button>
      </form>
      <div v-if="loginError" class="login-error" role="alert">{{ loginError }}</div>
    </div>
  </div>
  <!-- Main app -->
  <div v-else class="app-layout">
    <div class="sidebar-overlay" :class="{ visible: sidebarOpen }" @click="sidebarOpen = false"></div>
    <div id="conversation-sidebar" class="app-sidebar" :class="{ open: sidebarOpen }" :inert="!sidebarOpen">
      <ConversationList
        :activeId="activeConversation?.conv_id"
        @select="selectConversation"
        @deleted="onConversationDeleted"
      />
    </div>
    <div class="app-main">
      <div class="app-toolbar">
        <button class="sidebar-toggle" :aria-label="sidebarOpen ? '收起会话列表' : '展开会话列表'" :title="sidebarOpen ? '收起会话列表' : '展开会话列表'" :aria-expanded="sidebarOpen" aria-controls="conversation-sidebar" @click="sidebarOpen = !sidebarOpen">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>
        </button>
        <div class="app-title">抖音聊天记录</div>
        <button class="search-toggle" :disabled="!activeConversation" :aria-expanded="searchOpen" @click="setSearchOpen(!searchOpen)">⌕ 查找聊天记录</button>
        <div class="theme-switcher">
          <button
            v-for="t in themes"
            :key="t.id || 'server'"
            class="theme-btn"
            :class="{ active: currentTheme === t.id }"
            :title="t.label"
            :style="{ background: themeDotColor(t) }"
            @click="applyTheme(t.id, true)"
          />
        </div>
      </div>
      <div ref="readerLayout" class="reader-layout">
        <MessageList
          :conversation="activeConversation"
          :searchHighlight="searchHighlight"
          :jumpToSeq="jumpToSeq"
          @jumped="jumpToSeq = null"
        />
        <div v-if="searchOpen && activeConversation" class="search-slot">
          <SearchBar :convId="activeConversation.conv_id" :convName="activeConversation.name" @navigate="navigateToMessage" @close="setSearchOpen(false)" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Allocate the panel once; animate only its contents, without reflowing chat. */
.search-slot > .search-panel { --panel-offset: 12px; animation: panel-enter .16s cubic-bezier(.2, 0, .2, 1); }
.app-sidebar.open > :deep(.conv-list) { --panel-offset: -12px; animation: panel-enter .16s cubic-bezier(.2, 0, .2, 1); }
@keyframes panel-enter {
  from { opacity: .75; transform: translateX(var(--panel-offset)); }
  to { opacity: 1; transform: translateX(0); }
}
@media (prefers-reduced-motion: reduce) {
  .search-slot > .search-panel, .app-sidebar.open > :deep(.conv-list) { animation: none; }
}

.reader-layout { display: flex; flex: 1; min-height: 0; overflow: hidden; position: relative; }
.reader-layout > .msg-panel { min-width: 0; }
.search-slot { flex: 0 0 320px; width: 320px; min-height: 0; overflow: hidden; }
@media (max-width: 800px) {
  .search-slot { position: absolute; inset: 0 0 0 auto; width: min(340px, 100%); z-index: 120; box-shadow: -8px 0 28px #0003; }
}
.search-toggle { padding: 5px 10px; background: var(--bg-secondary); color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; transition: color var(--dur-fast), border-color var(--dur-fast), transform var(--dur-fast); }
.search-toggle:hover:not(:disabled), .search-toggle[aria-expanded="true"] { color: var(--accent); border-color: var(--accent); }
.search-toggle:active:not(:disabled) { transform: scale(0.97); }
.search-toggle:disabled { opacity: .4; cursor: default; }

.login-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: var(--bg-primary);
}
.login-box {
  text-align: center;
  padding: 40px;
  background: var(--bg-secondary);
  border-radius: 16px;
  border: 1px solid var(--border-color);
  min-width: 300px;
  animation: login-in var(--dur-base) var(--ease-out) both;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
}
@keyframes login-in {
  from { opacity: 0; transform: translateY(10px) scale(0.985); }
  to { opacity: 1; transform: none; }
}
.login-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--accent);
  margin-bottom: 6px;
}
.login-subtitle {
  font-size: 14px;
  color: var(--text-muted);
  margin-bottom: 24px;
}
.login-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.login-input {
  padding: 10px 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: 15px;
  outline: none;
  text-align: center;
  transition: border-color var(--dur-fast), box-shadow var(--dur-fast);
}
.login-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
}
.login-btn {
  padding: 10px;
  border: none;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: filter 0.15s, transform 0.15s;
}
.login-btn:hover {
  filter: brightness(1.15);
}
.login-btn:active {
  transform: scale(0.985);
}
.login-error {
  margin-top: 12px;
  color: #ff4d4f;
  font-size: 13px;
  animation: login-shake 0.32s var(--ease-out);
}
@keyframes login-shake {
  0%, 100% { transform: translateX(0); }
  20% { transform: translateX(-5px); }
  40% { transform: translateX(4px); }
  60% { transform: translateX(-3px); }
  80% { transform: translateX(2px); }
}
.login-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 14px;
}

.app-layout {
  display: flex;
  height: 100vh;
}

.app-sidebar {
  width: var(--sidebar-width);
  flex-shrink: 0;
}

.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.app-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 11px 20px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}

.app-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  color: var(--text-primary);
  letter-spacing: 0.01em;
}
.app-title::before {
  content: "";
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 20%, transparent);
}


.theme-switcher {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-left: auto;
}

.theme-btn {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.15);
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
  padding: 0;
}

.theme-btn:hover {
  transform: scale(1.15);
}

.theme-btn.active {
  transform: scale(1.1);
  box-shadow: 0 0 0 2px var(--bg-secondary), 0 0 0 4px var(--accent);
}
.theme-btn:active {
  transform: scale(0.88);
}
/* 选中瞬间的轻微回弹，让换肤动作有反馈 */
.theme-btn.active {
  animation: theme-pop 0.3s var(--ease-out);
}
@keyframes theme-pop {
  0% { transform: scale(0.75); }
  60% { transform: scale(1.22); }
  100% { transform: scale(1.1); }
}

.sidebar-toggle {
  display: flex;
  background: none;
  border: none;
  color: var(--text-primary);
  font-size: 22px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  flex-shrink: 0;
  transition: background var(--dur-fast), transform var(--dur-fast);
}
.sidebar-toggle:hover {
  background: var(--bg-tertiary);
}
.sidebar-toggle:active {
  transform: scale(0.94);
}

.sidebar-overlay {
  display: none;
}

@media (min-width: 769px) {
  .app-sidebar:not(.open) { display: none; }
}

@media (max-width: 768px) {
  .sidebar-toggle {
    display: block;
  }

  .app-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 1000;
    transform: translateX(-100%);
    width: 280px;
    transition: transform var(--dur-base) var(--ease-out), box-shadow var(--dur-base) var(--ease-out);
  }

  .app-sidebar.open {
    transform: translateX(0);
    box-shadow: 0 0 44px rgba(0, 0, 0, 0.4);
  }

  .sidebar-overlay {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 999;
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--dur-base) var(--ease-out);
  }

  .sidebar-overlay.visible {
    opacity: 1;
    pointer-events: auto;
  }

  .app-toolbar {
    flex-wrap: wrap;
    padding: 8px 12px;
    gap: 8px;
  }

  .app-title {
    flex: 1;
    font-size: 14px;
  }

  .theme-switcher {
    order: 0;
    margin-left: 0;
    gap: 5px;
  }

}
</style>
