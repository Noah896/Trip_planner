<template>
  <div v-if="hasReceipt" class="memory-receipt" :class="{ compact }">
    <div v-if="usedHistory" class="history-line">
      <span class="receipt-icon">🧠</span>
      <span class="history-title">
        参考上次 {{ usedHistory.city }} {{ usedHistory.travel_days }} 日游（{{ usedHistory.start_date }}）
      </span>
      <span v-if="historyPreferences" class="history-preferences">
        偏好：{{ historyPreferences }}
      </span>
    </div>

    <div v-if="memoryItems.length" class="memory-line">
      <span class="receipt-icon">✨</span>
      <span class="memory-title">沿用长期偏好</span>
      <a-space :size="[4, 4]" wrap>
        <a-tag
          v-for="item in memoryItems"
          :key="`${item.category}:${item.key}`"
          class="memory-tag"
          :title="item.source_text"
        >
          {{ item.value }}
        </a-tag>
      </a-space>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { HistoryUsage, UsedMemoryFact } from '@/types'

const props = withDefaults(
  defineProps<{
    usedMemory?: UsedMemoryFact[] | null
    usedHistory?: HistoryUsage | null
    compact?: boolean
  }>(),
  {
    usedMemory: () => [],
    usedHistory: null,
    compact: false
  }
)

const memoryItems = computed(() => {
  const seen = new Set<string>()
  return (props.usedMemory ?? []).filter((item) => {
    const key = `${item.category}:${item.key}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
})

const historyPreferences = computed(() => {
  const preferences = props.usedHistory?.preferences ?? []
  return preferences.filter(Boolean).join('、')
})

const hasReceipt = computed(
  () => Boolean(props.usedHistory) || memoryItems.value.length > 0
)
</script>

<style scoped>
.memory-receipt {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 10px;
  padding: 10px 12px;
  border: 1px solid #dfe5ff;
  border-radius: 10px;
  background: linear-gradient(135deg, #f7f9ff, #fbf7ff);
  color: #4c5674;
  font-size: 13px;
}

.memory-receipt.compact {
  padding: 8px 10px;
  margin-bottom: 8px;
  font-size: 12px;
}

.history-line,
.memory-line {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
}

.receipt-icon {
  flex: none;
}

.history-title,
.memory-title {
  font-weight: 600;
  color: #4056b4;
}

.history-preferences {
  color: #7b8197;
}

.memory-tag {
  margin-inline-end: 0;
  color: #5f46a3;
  border-color: #ddd1ff;
  background: #f5efff;
}
</style>
