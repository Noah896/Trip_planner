<template>
  <div class="trip-plan-view">
    <div class="plan-head">
      <span class="plan-title">🗺️ {{ plan.city }} {{ plan.days.length }} 日游</span>
      <span class="plan-dates">{{ plan.start_date }} ~ {{ plan.end_date }}</span>
      <span v-if="plan.budget" class="plan-total">预估 ¥{{ plan.budget.total }}</span>
    </div>

    <p v-if="plan.overall_suggestions" class="plan-suggestions">
      {{ plan.overall_suggestions }}
    </p>

    <div v-if="plan.weather_info && plan.weather_info.length" class="weather-row">
      <span v-for="w in plan.weather_info" :key="w.date" class="weather-chip">
        {{ w.date.slice(5) }} {{ w.day_weather }} {{ w.day_temp }}°/{{ w.night_temp }}°
      </span>
    </div>

    <div class="days">
      <div v-for="day in plan.days" :key="day.day_index" class="day">
        <div class="day-head">
          <span class="day-title">第{{ day.day_index + 1 }}天</span>
          <span class="day-date">{{ day.date }}</span>
        </div>
        <p v-if="day.description" class="day-desc">{{ day.description }}</p>

        <div v-for="(a, i) in day.attractions" :key="`a-${i}`" class="line">
          <span class="line-icon">🎯</span>
          <span class="line-name">{{ a.name }}</span>
          <span class="line-meta">
            {{ a.visit_duration }}分钟<template v-if="a.ticket_price"> · ¥{{ a.ticket_price }}</template>
          </span>
        </div>

        <div v-if="day.hotel" class="line">
          <span class="line-icon">🏨</span>
          <span class="line-name">{{ day.hotel.name }}</span>
          <span class="line-meta">{{ day.hotel.price_range || day.hotel.type }}</span>
        </div>

        <div v-for="(m, i) in day.meals" :key="`m-${i}`" class="line">
          <span class="line-icon">{{ mealIcon(m.type) }}</span>
          <span class="line-name">{{ m.name }}</span>
        </div>
      </div>
    </div>

    <div v-if="plan.budget" class="budget-row">
      <span>门票 ¥{{ plan.budget.total_attractions }}</span>
      <span>住宿 ¥{{ plan.budget.total_hotels }}</span>
      <span>餐饮 ¥{{ plan.budget.total_meals }}</span>
      <span>交通 ¥{{ plan.budget.total_transportation }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { TripPlan } from '@/types'

// 只读的紧凑行程卡片:聊天里嵌在助手消息里用。
// 它**不是** Result.vue 的复制品 —— 那个是整页(侧边导航/编辑/导出/地图),
// 这里是"一眼能看完"的版本,自带样式、不依赖任何外部 class 名。
defineProps<{ plan: TripPlan }>()

const MEAL_ICONS: Record<string, string> = {
  breakfast: '🍳',
  lunch: '🍜',
  dinner: '🍽️',
  snack: '🍡'
}

const mealIcon = (type: string): string => MEAL_ICONS[type] ?? '🍽️'
</script>

<style scoped>
.trip-plan-view {
  background: #fff;
  border: 1px solid #e8e8ef;
  border-radius: 12px;
  padding: 14px 16px;
  font-size: 13px;
  color: #333;
}

.plan-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid #f0f0f5;
}

.plan-title {
  font-size: 15px;
  font-weight: 600;
  color: #2b2b3c;
}

.plan-dates {
  color: #8c8c9a;
}

.plan-total {
  margin-left: auto;
  color: #d4380d;
  font-weight: 600;
}

.plan-suggestions {
  margin: 10px 0 0;
  color: #6b6b7b;
  line-height: 1.6;
}

.weather-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.weather-chip {
  background: #f2f5ff;
  color: #4664d6;
  border-radius: 20px;
  padding: 2px 10px;
  font-size: 12px;
}

.days {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.day {
  border-left: 3px solid #667eea;
  padding-left: 10px;
}

.day-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.day-title {
  font-weight: 600;
  color: #4664d6;
}

.day-date {
  color: #9aa0b4;
  font-size: 12px;
}

.day-desc {
  margin: 4px 0 6px;
  color: #6b6b7b;
  line-height: 1.5;
}

.line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 2px 0;
}

.line-icon {
  flex: none;
}

.line-name {
  color: #2b2b3c;
}

.line-meta {
  margin-left: auto;
  color: #9aa0b4;
  font-size: 12px;
  white-space: nowrap;
}

.budget-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid #f0f0f5;
  color: #8c8c9a;
  font-size: 12px;
}
</style>