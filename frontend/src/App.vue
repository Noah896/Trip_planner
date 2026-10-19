<template>
  <div id="app">
    <a-layout style="min-height: 100vh">
      <a-layout-header class="app-header">
        <div class="app-brand">
          🌍 HelloAgents智能旅行助手
        </div>
        <div v-if="authUser" class="account-box">
          <span class="account-name">👤 {{ authUser.username }}</span>
          <a-button type="link" class="logout-button" @click="handleLogout">
            退出登录
          </a-button>
        </div>
      </a-layout-header>
      <a-layout-content style="padding: 24px">
        <router-view />
      </a-layout-content>
      <a-layout-footer style="text-align: center">
        HelloAgents智能旅行助手 ©2025 基于HelloAgents框架
      </a-layout-footer>
    </a-layout>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { logoutAccount } from '@/services/api'
import {
  AUTH_CHANGED_EVENT,
  getStoredAuthUser
} from '@/services/auth'
import type { AuthUser } from '@/types'

const router = useRouter()
const authUser = ref<AuthUser | null>(getStoredAuthUser())

const syncAuthUser = () => {
  authUser.value = getStoredAuthUser()
}

const handleLogout = async () => {
  try {
    await logoutAccount()
  } finally {
    authUser.value = null
    router.push('/login')
  }
}

onMounted(() => {
  window.addEventListener(AUTH_CHANGED_EVENT, syncAuthUser)
})

onUnmounted(() => {
  window.removeEventListener(AUTH_CHANGED_EVENT, syncAuthUser)
})
</script>

<style>
#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'Noto Sans', sans-serif;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #001529;
  padding: 0 50px;
}

.app-brand {
  color: white;
  font-size: 24px;
  font-weight: bold;
}

.account-box {
  display: flex;
  align-items: center;
  gap: 8px;
}

.account-name {
  color: #d9e2ff;
}

.logout-button {
  color: #b8c6e8;
}
</style>

