<template>
  <div class="history-page">
    <div class="history-hero">
      <div>
        <a-button type="link" class="back-link" @click="goHome">← 返回规划</a-button>
        <h1>🧭 历史行程</h1>
        <p>这里只展示你自己成功生成过的行程，可按城市筛选并查看完整安排。</p>
      </div>
      <a-input-search
        v-model:value="city"
        class="city-search"
        size="large"
        allow-clear
        placeholder="按城市筛选，例如：北京"
        enter-button="筛选"
        @search="applySearch"
        @change="handleSearchChange"
      />
    </div>

    <a-spin :spinning="loading">
      <a-empty v-if="!loading && !items.length" class="empty-state">
        <template #description>
          <span>{{ city.trim() ? `没有找到 ${city.trim()} 的历史行程` : '还没有历史行程' }}</span>
        </template>
        <a-button type="primary" @click="goHome">去规划一次</a-button>
      </a-empty>

      <a-row v-else :gutter="[16, 16]">
        <a-col
          v-for="item in items"
          :key="item.id"
          :xs="24"
          :md="12"
          :xl="8"
        >
          <a-card class="history-card" :bordered="false" hoverable @click="openDetail(item)">
            <div class="card-head">
              <div>
                <div class="city-title">📍 {{ item.city }}</div>
                <div class="date-range">{{ item.start_date }} ~ {{ item.end_date }}</div>
              </div>
              <a-tag color="purple">{{ item.travel_days }} 天</a-tag>
            </div>

            <div v-if="item.preferences.length" class="preference-row">
              <a-tag v-for="preference in item.preferences" :key="preference">
                {{ preference }}
              </a-tag>
            </div>

            <p class="overview">{{ item.overview || '这份行程没有额外总体建议。' }}</p>

            <div v-if="item.attraction_names.length" class="attraction-row">
              <span class="attraction-label">主要景点</span>
              <span class="attraction-names">{{ item.attraction_names.join(' · ') }}</span>
            </div>

            <div class="card-foot">
              <span class="saved-at">保存于 {{ formatSavedAt(item.created_at) }}</span>
              <div class="card-actions" @click.stop>
                <a-button type="link" @click="openDetail(item)">查看详情</a-button>
                <a-popconfirm
                  title="确定删除这条历史行程吗？"
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="handleDelete(item.id)"
                >
                  <a-button type="link" danger>删除</a-button>
                </a-popconfirm>
              </div>
            </div>
          </a-card>
        </a-col>
      </a-row>

      <div v-if="total > pageSize" class="pagination-row">
        <a-pagination
          :current="page"
          :page-size="pageSize"
          :total="total"
          :show-size-changer="false"
          show-less-items
          @change="handlePageChange"
        />
      </div>
    </a-spin>

    <a-drawer
      v-model:open="drawerOpen"
      title="历史行程详情"
      width="min(820px, 96vw)"
      destroy-on-close
    >
      <template #extra>
        <a-popconfirm
          v-if="detail"
          title="确定删除这条历史行程吗？"
          ok-text="删除"
          cancel-text="取消"
          @confirm="handleDelete(detail.id)"
        >
          <a-button danger>删除这条行程</a-button>
        </a-popconfirm>
      </template>

      <a-spin :spinning="detailLoading">
        <template v-if="detail">
          <a-descriptions :column="2" size="small" class="detail-meta">
            <a-descriptions-item label="城市">{{ detail.city }}</a-descriptions-item>
            <a-descriptions-item label="天数">{{ detail.travel_days }} 天</a-descriptions-item>
            <a-descriptions-item label="日期" :span="2">
              {{ detail.start_date }} ~ {{ detail.end_date }}
            </a-descriptions-item>
            <a-descriptions-item label="交通">
              {{ detail.request.transportation || '未记录' }}
            </a-descriptions-item>
            <a-descriptions-item label="住宿">
              {{ detail.request.accommodation || '未记录' }}
            </a-descriptions-item>
            <a-descriptions-item label="额外要求" :span="2">
              {{ detail.request.free_text_input || '无' }}
            </a-descriptions-item>
          </a-descriptions>

          <div v-if="detail.preferences.length" class="detail-preferences">
            <span>旅行偏好：</span>
            <a-tag v-for="preference in detail.preferences" :key="preference">
              {{ preference }}
            </a-tag>
          </div>

          <TripPlanView :plan="detail.plan" />
        </template>
        <a-empty v-else-if="!detailLoading" description="历史行程不存在或已被删除" />
      </a-spin>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import TripPlanView from '@/components/TripPlanView.vue'
import {
  deleteTripHistory,
  getTripHistoryDetail,
  listTripHistory
} from '@/services/api'
import type { HistoryTripDetail, HistoryTripSummary } from '@/types'

const router = useRouter()
const city = ref('')
const page = ref(1)
const pageSize = 12
const total = ref(0)
const items = ref<HistoryTripSummary[]>([])
const loading = ref(false)
const drawerOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<HistoryTripDetail | null>(null)

const errorText = (error: any): string => {
  const detailText = error?.response?.data?.detail
  if (typeof detailText === 'string' && detailText) return detailText
  return error?.message || '请求失败，请稍后重试'
}

const loadHistory = async () => {
  loading.value = true
  try {
    const response = await listTripHistory({
      city: city.value.trim() || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize
    })
    items.value = response.items
    total.value = response.total
  } catch (error: any) {
    message.error(errorText(error))
  } finally {
    loading.value = false
  }
}

const applySearch = () => {
  page.value = 1
  void loadHistory()
}

const handleSearchChange = (event: Event) => {
  const value = (event.target as HTMLInputElement).value
  city.value = value
  if (!value.trim()) applySearch()
}

const handlePageChange = (nextPage: number) => {
  page.value = nextPage
  void loadHistory()
}

const openDetail = async (item: HistoryTripSummary) => {
  drawerOpen.value = true
  detail.value = null
  detailLoading.value = true
  try {
    detail.value = await getTripHistoryDetail(item.id)
  } catch (error: any) {
    message.error(errorText(error))
    drawerOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

const handleDelete = async (id: number) => {
  try {
    await deleteTripHistory(id)
    message.success('历史行程已删除')
    if (detail.value?.id === id) {
      drawerOpen.value = false
      detail.value = null
    }
    if (items.value.length === 1 && page.value > 1) page.value -= 1
    await loadHistory()
  } catch (error: any) {
    message.error(errorText(error))
  }
}

const formatSavedAt = (value: string): string => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

const goHome = () => router.push('/')

onMounted(loadHistory)
</script>

<style scoped>
.history-page {
  max-width: 1180px;
  margin: 0 auto;
  min-height: calc(100vh - 190px);
}

.history-hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 28px;
  padding: 34px 38px;
  margin-bottom: 22px;
  color: #fff;
  border-radius: 22px;
  background: linear-gradient(135deg, #5969c7 0%, #764ba2 100%);
  box-shadow: 0 18px 50px rgba(72, 78, 145, 0.24);
}

.history-hero h1 {
  margin: 4px 0 8px;
  color: #fff;
  font-size: 32px;
}

.history-hero p {
  margin: 0;
  color: rgba(255, 255, 255, 0.82);
  line-height: 1.6;
}

.back-link {
  padding: 0;
  color: rgba(255, 255, 255, 0.85);
}

.city-search {
  width: min(360px, 100%);
}

.history-card {
  height: 100%;
  border-radius: 16px;
  box-shadow: 0 8px 26px rgba(31, 40, 80, 0.08);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}

.history-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 14px 34px rgba(31, 40, 80, 0.14);
}

.card-head,
.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.city-title {
  color: #30364f;
  font-size: 19px;
  font-weight: 700;
}

.date-range,
.saved-at {
  margin-top: 4px;
  color: #9298aa;
  font-size: 12px;
}

.preference-row {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 12px;
}

.overview {
  min-height: 44px;
  margin: 14px 0;
  color: #606779;
  line-height: 1.65;
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.attraction-row {
  padding: 10px 12px;
  border-radius: 10px;
  background: #f6f7fc;
  color: #687087;
  font-size: 12px;
  line-height: 1.6;
}

.attraction-label {
  margin-right: 8px;
  color: #667eea;
  font-weight: 600;
}

.card-foot {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #f0f1f6;
}

.card-actions {
  display: flex;
  align-items: center;
}

.empty-state {
  padding: 90px 20px;
  border-radius: 18px;
  background: #fff;
}

.pagination-row {
  display: flex;
  justify-content: center;
  margin-top: 28px;
}

.detail-meta {
  margin-bottom: 16px;
}

.detail-preferences {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 16px;
  color: #737a8d;
}

@media (max-width: 760px) {
  .history-hero {
    align-items: stretch;
    flex-direction: column;
    padding: 26px 22px;
  }

  .city-search {
    width: 100%;
  }

  .card-foot {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>