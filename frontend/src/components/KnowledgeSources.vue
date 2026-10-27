<template>
  <section v-if="sources.length || degraded" class="knowledge-sources">
    <div class="knowledge-head">
      <span class="knowledge-title">📚 知识与核验来源</span>
      <span v-if="degraded" class="knowledge-status">{{ statusText }}</span>
      <span v-else class="knowledge-count">{{ sources.length }} 条</span>
    </div>

    <div v-if="sources.length" class="source-list">
      <component
        :is="source.source_url ? 'a' : 'div'"
        v-for="source in sources"
        :key="source.chunk_id"
        class="source-item"
        :href="source.source_url || undefined"
        :target="source.source_url ? '_blank' : undefined"
        :rel="source.source_url ? 'noopener noreferrer' : undefined"
      >
        <span class="source-id">{{ source.id }}</span>
        <span class="source-copy">
          <span class="source-title">{{ source.title }}</span>
          <span class="source-meta">
            {{ source.publisher || '未标注发布方' }}
            <template v-if="source.section"> · {{ source.section }}</template>
            <template v-if="source.verified_at"> · 核验 {{ source.verified_at }}</template>
          </span>
        </span>
        <span v-if="source.source_url" class="source-link">查看来源 ↗</span>
      </component>
    </div>

    <p v-else class="knowledge-fallback">
      知识库本次未提供可用证据，行程仍基于高德实时检索结果生成。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { KnowledgeSource } from '@/types'

const props = withDefaults(
  defineProps<{
    sources?: KnowledgeSource[]
    degraded?: boolean
    status?: string
  }>(),
  {
    sources: () => [],
    degraded: false,
    status: 'disabled'
  }
)

const statusText = computed(() => {
  const labels: Record<string, string> = {
    no_match: '未命中相关知识',
    timeout: '知识检索超时,已继续规划',
    error: '知识检索不可用,已继续规划',
    disabled: '知识检索未启用'
  }
  return labels[props.status] ?? '知识检索已降级'
})
</script>

<style scoped>
.knowledge-sources {
  background: #f7f9ff;
  border: 1px solid #dfe6ff;
  border-radius: 12px;
  padding: 12px 14px;
  font-size: 13px;
  color: #333;
}

.knowledge-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.knowledge-title {
  color: #3554c7;
  font-weight: 600;
}

.knowledge-count,
.knowledge-status {
  margin-left: auto;
  color: #7b849e;
  font-size: 12px;
}

.source-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.source-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 7px 8px;
  color: inherit;
  text-decoration: none;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #edf0fa;
}

a.source-item:hover {
  border-color: #9dafef;
  background: #fbfcff;
}

.source-id {
  flex: none;
  min-width: 26px;
  color: #4664d6;
  font-weight: 700;
}

.source-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.source-title {
  color: #2b2b3c;
  font-weight: 500;
}

.source-meta {
  color: #8c8c9a;
  font-size: 12px;
}

.source-link {
  margin-left: auto;
  flex: none;
  color: #4664d6;
  font-size: 12px;
}

.knowledge-fallback {
  margin: 0;
  color: #8c6d1f;
  line-height: 1.5;
}
</style>