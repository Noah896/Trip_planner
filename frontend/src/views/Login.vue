<template>
  <div class="login-page">
    <a-card class="login-card" :bordered="false">
      <div class="brand">
        <div class="brand-icon">🌍</div>
        <h1>智能旅行规划助手</h1>
        <p>登录后才能保存和复用你的旅行偏好与历史行程</p>
      </div>

      <a-spin :spinning="checkingStatus">
        <a-alert
          v-if="status && !status.enabled"
          type="info"
          show-icon
          message="当前服务未开启登录"
          description="正在进入旅行规划页面。"
        />

        <a-tabs v-else v-model:activeKey="activeTab" centered>
          <a-tab-pane key="login" tab="登录">
            <a-form layout="vertical" :model="loginForm" @finish="handleLogin">
              <a-form-item label="用户名" name="username">
                <a-input
                  v-model:value="loginForm.username"
                  size="large"
                  autocomplete="username"
                  placeholder="请输入用户名"
                />
              </a-form-item>
              <a-form-item label="密码" name="password">
                <a-input-password
                  v-model:value="loginForm.password"
                  size="large"
                  autocomplete="current-password"
                  placeholder="请输入密码"
                />
              </a-form-item>
              <a-button
                type="primary"
                html-type="submit"
                block
                size="large"
                :loading="submitting"
              >
                登录
              </a-button>
            </a-form>
          </a-tab-pane>

          <a-tab-pane
            key="register"
            tab="注册"
            :disabled="status ? !status.registration_enabled : false"
          >
            <a-form layout="vertical" :model="registerForm" @finish="handleRegister">
              <a-form-item label="用户名" name="username">
                <a-input
                  v-model:value="registerForm.username"
                  size="large"
                  autocomplete="username"
                  placeholder="3~32 位字母、数字、下划线、点或短横线"
                />
              </a-form-item>
              <a-form-item label="密码" name="password">
                <a-input-password
                  v-model:value="registerForm.password"
                  size="large"
                  autocomplete="new-password"
                  placeholder="至少 8 位"
                />
              </a-form-item>
              <a-form-item label="确认密码">
                <a-input-password
                  v-model:value="registerForm.confirmPassword"
                  size="large"
                  autocomplete="new-password"
                  placeholder="再次输入密码"
                />
              </a-form-item>
              <a-form-item
                v-if="status?.registration_requires_key"
                label="注册邀请码"
              >
                <a-input-password
                  v-model:value="registerForm.registrationKey"
                  size="large"
                  autocomplete="off"
                  placeholder="请输入服务提供方给出的邀请码"
                />
              </a-form-item>
              <a-button
                type="primary"
                html-type="submit"
                block
                size="large"
                :loading="submitting"
              >
                创建账号
              </a-button>
            </a-form>
          </a-tab-pane>
        </a-tabs>
      </a-spin>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  getAuthStatus,
  loginAccount,
  registerAccount
} from '@/services/api'
import {
  getStoredAuthUser,
  saveAuthSession,
  setAuthDisabled
} from '@/services/auth'
import type { AuthStatus } from '@/types'

const route = useRoute()
const router = useRouter()
const activeTab = ref<'login' | 'register'>('login')
const checkingStatus = ref(true)
const submitting = ref(false)
const status = ref<AuthStatus | null>(null)

const loginForm = reactive({
  username: '',
  password: ''
})

const registerForm = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  registrationKey: ''
})

const redirectTarget = computed(() => {
  const redirect = String(route.query.redirect ?? '/')
  return redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
})

const errorText = (error: any): string => {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  return error?.message || '请求失败,请稍后重试'
}

const finishLogin = (response: Parameters<typeof saveAuthSession>[0]) => {
  saveAuthSession(response)
  message.success(`欢迎回来,${response.user.username}`)
  router.replace(redirectTarget.value)
}

const handleLogin = async () => {
  if (!loginForm.username.trim() || !loginForm.password) {
    message.warning('请输入用户名和密码')
    return
  }
  submitting.value = true
  try {
    const response = await loginAccount(loginForm.username, loginForm.password)
    finishLogin(response)
  } catch (error: any) {
    message.error(errorText(error))
  } finally {
    submitting.value = false
  }
}

const handleRegister = async () => {
  if (!registerForm.username.trim() || !registerForm.password) {
    message.warning('请输入用户名和密码')
    return
  }
  if (registerForm.password !== registerForm.confirmPassword) {
    message.warning('两次输入的密码不一致')
    return
  }
  if (
    status.value?.registration_requires_key &&
    !registerForm.registrationKey.trim()
  ) {
    message.warning('请输入注册邀请码')
    return
  }

  submitting.value = true
  try {
    const response = await registerAccount(
      registerForm.username,
      registerForm.password,
      registerForm.registrationKey
    )
    finishLogin(response)
  } catch (error: any) {
    message.error(errorText(error))
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  if (getStoredAuthUser()) {
    router.replace(redirectTarget.value)
    return
  }
  try {
    status.value = await getAuthStatus()
    if (!status.value.enabled) {
      setAuthDisabled(true)
      router.replace(redirectTarget.value)
    } else {
      setAuthDisabled(false)
    }
  } catch (error: any) {
    message.error(errorText(error))
  } finally {
    checkingStatus.value = false
  }
})
</script>

<style scoped>
.login-page {
  min-height: calc(100vh - 190px);
  margin: -24px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}

.login-card {
  width: min(440px, 100%);
  border-radius: 20px;
  box-shadow: 0 18px 60px rgba(34, 28, 75, 0.22);
}

.brand {
  text-align: center;
  margin-bottom: 20px;
}

.brand-icon {
  font-size: 42px;
}

.brand h1 {
  margin: 8px 0 4px;
  color: #38406b;
  font-size: 24px;
}

.brand p {
  margin: 0;
  color: #858ba1;
  line-height: 1.6;
}
</style>
