<script setup>
// 「火花图标 + 连续天数」：会话列表、聊天标题、数据洞察共用。
// 图标是采集时从抖音接口按官方文件名存下来的本地图；下载失败时退回一个火苗。
//
// note 是「1 天后消失」「重燃中 2/3」这类状态原文：那时数字是倒计时或重燃进度、
// 不是天数，按抖音网页版的样子只画火花图标 + 这句话，不写数字。
import { computed, ref, watch } from 'vue'
import { sparkColor, sparkIconUrl } from '@/lib/spark.js'

const props = defineProps({
  days: { type: Number, default: 0 },
  icon: { type: String, default: '' },
  note: { type: String, default: '' },
})

const failed = ref(false)
watch(() => props.icon, () => { failed.value = false })

const url = computed(() => (failed.value ? '' : sparkIconUrl(props.icon)))
const note = computed(() => String(props.note || '').trim())
// 提示语一直是灰色（和抖音网页版的 --color-TextTertiary 一致），
// 正常天数则跟图标颜色走。
const color = computed(() => (note.value ? sparkColor('gray') : sparkColor(props.icon) || 'var(--accent)'))
const title = computed(() => (note.value ? `火花${note.value}` : `连续聊天 ${props.days} 天`))
</script>

<template>
  <span v-if="note || days > 0" class="spark-badge" :title="title">
    <img
      v-if="url"
      class="spark-icon"
      :src="url"
      alt=""
      loading="lazy"
      @error="failed = true"
    />
    <span v-else class="spark-icon spark-icon-fallback" :style="{ color }" aria-hidden="true">🔥</span>
    <span v-if="note" class="spark-note" :style="{ color }">{{ note }}</span>
    <span v-else class="spark-days" :style="{ color }">{{ days }}</span>
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
/* 「1 天后消失」「重燃中 2/3」比天数长一点，字号小半号、不要加粗，别把昵称挤出去 */
.spark-note {
  font-size: 0.92em;
  white-space: nowrap;
}
</style>
