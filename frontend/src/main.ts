import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import Home from './views/Home.vue'
import Result from './views/Result.vue'
import Chat from './views/Chat.vue'
import Login from './views/Login.vue'
import { getStoredAuthUser, isAuthDisabled } from './services/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: Login,
      meta: { public: true }
    },
    {
      path: '/',
      name: 'Home',
      component: Home
    },
    {
      path: '/result',
      name: 'Result',
      component: Result
    },
    {
      path: '/chat',
      name: 'Chat',
      component: Chat
    }
  ]
})

router.beforeEach((to) => {
  const user = getStoredAuthUser()
  if (!to.meta.public && !user && !isAuthDisabled()) {
    return {
      name: 'Login',
      query: { redirect: to.fullPath }
    }
  }
  if (to.name === 'Login' && user) {
    return { name: 'Home' }
  }
  return true
})

const app = createApp(App)

app.use(router)
app.use(Antd)

app.mount('#app')
