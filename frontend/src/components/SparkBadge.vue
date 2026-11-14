<script setup>
// 「火花图标 + 连续天数」：会话列表、聊天标题、数据洞察共用。
// 图标是采集时从抖音接口按官方文件名存下来的本地图；下载失败时退回一个火苗。
import { computed, ref, watch } from 'vue'
import { sparkColor, sparkIconUrl } from '@/lib/spark.js'

const props = defineProps({
  days: { type: Number, default: 0 },
  icon: { type: String, default: '' },
})

const failed = ref(false)
watch(() => props.icon, () => { failed.value = false })

const url = computed(() => (failed.value ? '' : sparkIconUrl(props.icon)))
const color = computed(() => sparkColor(props.icon) || 'var(--accent)')
</script>

<template>
  <span v-if="days > 0" class="spark-badge" :title="`连续聊天 ${days} 天`">
    <img
      v-if="url"
      class="spark-icon"
      :src="url"
      alt=""
      loading="lazy"
      @error="failed = true"
    />
    <span v-else class="spark-icon spark-icon-fallback" :style="{ color }" aria-hidden="true">🔥</span>
    <span class="spark-days" :style="{ color }">{{ days }}</span>
  </span>
</template>

<style scoped>
.spark-badge {
  display: inline-flex;
  align-items: center;
  gap: 1px;
  flex: 0 0 auto;
  line-height: 1;
  font-size: 13px;
}
/* 官方图是 48×60，四周留着透明边（实体火焰只占中间 36×48）。
   画框得比看得见的那颗大一圈：16×20 铺进去，实际火焰约 12×16，
   和抖音 Web 端差不多大，透明边也不会再把昵称和天数撑开。 */
.spark-icon {
  width: 16px;
  height: 20px;
  object-fit: contain;
  display: block;
}
.spark-icon-fallback {
  font-size: 15px;
  line-height: 20px;
}
.spark-days {
  font-size: 1em;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
</style>
