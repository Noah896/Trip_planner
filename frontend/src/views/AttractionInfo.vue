<template>
  <div class="attraction-info-page">
    <a-button class="back-button" @click="goBack">← 返回行程</a-button>

    <a-card v-if="name" :bordered="false" class="info-card">
      <div class="info-hero">
        <div class="info-kicker">{{ city || '旅行目的地' }} · 景点介绍</div>
        <h1>{{ name }}</h1>
        <p class="info-address">📍 {{ address || '地址信息暂未提供' }}</p>
      </div>

      <div class="info-summary">
        <a-tag v-if="category" color="blue">{{ category }}</a-tag>
        <a-tag v-if="rating" color="gold">评分 {{ rating }}</a-tag>
        <a-tag v-if="visitDuration" color="green">
          建议游览 {{ visitDuration }} 分钟
        </a-tag>
      </div>

      <a-alert
        class="info-notice"
        type="info"
        show-icon
        message="暂未找到可验证的景点官网"
        description="这里展示本次行程中的景点资料，避免自动跳转到广告、地图或无关搜索结果。"
      />

      <section class="info-section">
        <h2>景点介绍</h2>
        <p>{{ description || '本次行程暂未提供更详细的景点介绍。' }}</p>
      </section>

      <div class="info-actions">
        <a-button type="primary" :href="searchUrl" target="_blank" rel="noopener noreferrer">
          搜索更多介绍
        </a-button>
        <a-button @click="goBack">返回行程</a-button>
      </div>
    </a-card>

    <a-empty v-else description="没有找到景点信息">
      <a-button type="primary" @click="goBack">返回行程</a-button>
    </a-empty>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const queryText = (key: string): string => {
  const value = route.query[key]
  return Array.isArray(value) ? String(value[0] || '') : String(value || '')
}

const name = computed(() => queryText('name'))
const city = computed(() => queryText('city'))
const address = computed(() => queryText('address'))
const description = computed(() => queryText('description'))
const category = computed(() => queryText('category'))
const rating = computed(() => queryText('rating'))
const visitDuration = computed(() => queryText('visit_duration'))
const searchUrl = computed(() => {
  const query = [name.value, city.value].filter(Boolean).join(' ')
  return `https://www.so.com/s?q=${encodeURIComponent(query)}`
})

const goBack = () => {
  // 页面通常由 API 307 跳转进入新标签页，浏览器历史里没有原结果页。
  router.push('/result')
}
</script>

<style scoped>
.attraction-info-page {
  max-width: 960px;
  margin: 0 auto;
}

.back-button {
  margin-bottom: 16px;
}

.info-card {
  overflow: hidden;
  border-radius: 18px;
  box-shadow: 0 14px 44px rgba(31, 55, 91, 0.12);
}

.info-hero {
  margin: -24px -24px 24px;
  padding: 46px 36px 38px;
  color: #fff;
  background: linear-gradient(135deg, #244b8f 0%, #367c88 60%, #52a37e 100%);
}

.info-kicker {
  margin-bottom: 12px;
  font-size: 13px;
  letter-spacing: 0.12em;
  opacity: 0.82;
}

.info-hero h1 {
  margin: 0 0 14px;
  color: #fff;
  font-size: clamp(28px, 5vw, 44px);
}

.info-address {
  margin: 0;
  font-size: 15px;
  opacity: 0.9;
}

.info-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 22px;
}

.info-notice {
  margin-bottom: 24px;
}

.info-section {
  margin-bottom: 28px;
  padding: 22px 24px;
  border-radius: 14px;
  background: #f7f9fc;
}

.info-section h2 {
  margin: 0 0 12px;
  font-size: 18px;
}

.info-section p {
  margin: 0;
  color: #475467;
  line-height: 1.85;
  white-space: pre-wrap;
}

.info-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

@media (max-width: 640px) {
  .info-hero {
    padding: 36px 24px 30px;
  }
}
</style>
