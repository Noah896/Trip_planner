import axios from 'axios'
import type {
  AuthStatus,
  AuthTokenResponse,
  AuthUser,
  ChatEvent,
  ChatTurn,
  HistoryTripDetail,
  HistoryTripListResponse,
  TripFormData,
  TripPlanResponse,
  TripProgressEvent,
  TripStreamEvent
} from '@/types'
import { clearAuthSession, getAuthToken } from '@/services/auth'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000, // 2分钟超时
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    const token = getAuthToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    const url = String(error.config?.url ?? '')
    const isLoginRequest =
      url.includes('/api/auth/login') || url.includes('/api/auth/register')
    if (error.response?.status === 401 && !isLoginRequest) {
      clearAuthSession()
      if (window.location.pathname !== '/login') {
        window.location.assign('/login')
      }
    }
    return Promise.reject(error)
  }
)

/**
 * 把一个 SSE 响应体按帧喂给回调。
 *
 * 为什么不用 EventSource:`/plan/stream` 和 `/chat/stream` 都是 POST,
 * 而 EventSource 只支持 GET。所以用 fetch + ReadableStream 手工解析。
 */
async function readSSE<T>(response: Response, onEvent: (event: T) => void): Promise<void> {
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')

    // SSE 用空行分帧;最后一段可能被截断,留在 buffer 里等下一块
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''

    for (const frame of frames) {
      const line = frame.split('\n').find((item) => item.startsWith('data:'))
      if (!line) continue
      onEvent(JSON.parse(line.slice(5).trim()) as T)
    }
  }
}

async function postStream(path: string, body: unknown): Promise<Response> {
  const token = getAuthToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body)
  })

  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    if (response.status === 401) {
      clearAuthSession()
      if (window.location.pathname !== '/login') {
        window.location.assign('/login')
      }
    }
    throw new Error(detail || `请求失败(${response.status})`)
  }
  return response
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const response = await apiClient.get<AuthStatus>('/api/auth/status')
  return response.data
}

export async function registerAccount(
  username: string,
  password: string,
  registrationKey?: string
): Promise<AuthTokenResponse> {
  const response = await apiClient.post<AuthTokenResponse>('/api/auth/register', {
    username,
    password,
    registration_key: registrationKey?.trim() || undefined
  })
  return response.data
}

export async function loginAccount(
  username: string,
  password: string
): Promise<AuthTokenResponse> {
  const response = await apiClient.post<AuthTokenResponse>('/api/auth/login', {
    username,
    password
  })
  return response.data
}

export async function getCurrentUser(): Promise<AuthUser> {
  const response = await apiClient.get<AuthUser>('/api/auth/me')
  return response.data
}

export async function logoutAccount(): Promise<void> {
  try {
    await apiClient.post('/api/auth/logout')
  } finally {
    clearAuthSession()
  }
}

export interface HistoryTripQuery {
  city?: string
  limit?: number
  offset?: number
}

export async function listTripHistory(
  query: HistoryTripQuery = {}
): Promise<HistoryTripListResponse> {
  const response = await apiClient.get<HistoryTripListResponse>('/api/history', {
    params: query
  })
  return response.data
}

export async function getTripHistoryDetail(id: number): Promise<HistoryTripDetail> {
  const response = await apiClient.get<HistoryTripDetail>(`/api/history/${id}`)
  return response.data
}

export async function deleteTripHistory(id: number): Promise<void> {
  await apiClient.delete(`/api/history/${id}`)
}
/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

/**
 * 生成旅行计划(流式进度,表单入口)
 *
 * 返回值和 generateTripPlan 完全一样,区别是过程中会通过 onProgress 回调
 * 把**真实进度**推出来:每完成一步(景点检索 / 天气查询 / 酒店检索 / 生成行程)
 * 回调一次。这样前端就不用像以前那样拿 setInterval 假装进度了。
 */
export async function generateTripPlanStream(
  formData: TripFormData,
  onProgress: (event: TripProgressEvent) => void
): Promise<TripPlanResponse> {
  const response = await postStream('/api/trip/plan/stream', formData)

  let result: TripPlanResponse | null = null

  await readSSE<TripStreamEvent>(response, (event) => {
    if (event.type === 'progress') {
      onProgress(event)
    } else if (event.type === 'result') {
      result = {
        success: event.success,
        degraded: event.degraded,
        message: event.message,
        data: event.data,
        memory_saved: event.memory_saved,
        memory_version: event.memory_version,
        used_memory: event.used_memory,
        used_history: event.used_history ?? null,
        knowledge_used: event.knowledge_used ?? false,
        knowledge_degraded: event.knowledge_degraded ?? false,
        knowledge_status: event.knowledge_status ?? 'disabled',
        knowledge_sources: event.knowledge_sources ?? []
      }
    } else if (event.type === 'error') {
      throw new Error(event.message || '生成旅行计划失败')
    }
  })

  if (!result) {
    throw new Error('连接中断,未收到最终计划')
  }
  return result
}

export interface ChatStreamHandlers {
  /** 助手说的一句话(追问 / 复述需求) */
  onMessage?: (text: string) => void
  /** 规划过程中的节点进度 */
  onProgress?: (event: TripProgressEvent) => void
  /** 结构化行程,渲染成卡片 */
  onArtifact?: (event: Extract<ChatEvent, { type: 'artifact' }>) => void
  /** 本轮结束;request 是后端解析出来的条件 */
  onDone?: (request?: TripFormData) => void
}

/**
 * 对话式规划(聊天入口)
 *
 * 只给 message(可带 history)时,后端会先判断意图、抽取条件:信息不够就追问,
 * 够了就直接生成行程。若已经填好表单,可以直接传 trip —— 那样会跳过意图识别。
 */
export async function chatStream(
  body: {
    message: string
    history?: ChatTurn[]
    trip?: TripFormData
    session_id?: string
    user_id?: string
  },
  handlers: ChatStreamHandlers
): Promise<void> {
  const response = await postStream('/api/chat/stream', body)

  await readSSE<ChatEvent>(response, (event) => {
    if (event.type === 'message') {
      handlers.onMessage?.(event.text)
    } else if (event.type === 'progress') {
      handlers.onProgress?.(event)
    } else if (event.type === 'artifact') {
      handlers.onArtifact?.(event)
    } else if (event.type === 'done') {
      handlers.onDone?.(event.request)
    } else if (event.type === 'error') {
      throw new Error(event.message || '对话式规划失败')
    }
  })
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

export default apiClient
