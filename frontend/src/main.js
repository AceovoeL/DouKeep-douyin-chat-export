import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import { applyAppearance, cachedAppearance, themeOverride } from './lib/appearance.js'

// 挂载前先用本机缓存的外观上色：没有缓存时 style.css 里的默认值就是默认外观，
// 这样不会出现"先闪一下默认样式再跳成服务器样式"。
const cached = cachedAppearance()
if (cached) applyAppearance(cached, { theme: themeOverride() || undefined })

// 老版本把主题存在 'theme' 里；现在以服务器设置为准，这条本机记录不再使用。
localStorage.removeItem('theme')

createApp(App).mount('#app')
