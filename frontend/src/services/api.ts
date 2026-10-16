import axios from 'axios'
import type { TripFormData, TripPlanResponse, TripStreamEvent, TripProgressEvent } from '@/types'

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
    return Promise.reject(error)
  }
)

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
 * 生成旅行计划(流式进度)
 *
 * 返回值和 generateTripPlan 完全一样,区别是过程中会通过 onProgress 回调
 * 把**真实进度**推出来:每完成一步(景点检索 / 天气查询 / 酒店检索 / 生成行程)
 * 回调一次。这样前端就不用像以前那样拿 setInterval 假装进度了。
 *
 * 用 fetch + ReadableStream 手工解析 SSE —— axios 拿不到增量响应体。
 */
export async function generateTripPlanStream(
  formData: TripFormData,
  onProgress: (event: TripProgressEvent) => void
): Promise<TripPlanResponse> {
  const response = await fetch(`${API_BASE_URL}/api/trip/plan/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(formData)
  })

  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(detail || `请求失败(${response.status})`)
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: TripPlanResponse | null = null

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

      const event: TripStreamEvent = JSON.parse(line.slice(5).trim())

      if (event.type === 'progress') {
        onProgress(event)
      } else if (event.type === 'result') {
        result = {
          success: event.success,
          degraded: event.degraded,
          message: event.message,
          data: event.data
        }
      } else {
        throw new Error(event.message || '生成旅行计划失败')
      }
    }
  }

  if (!result) {
    throw new Error('连接中断,未收到最终计划')
  }
  return result
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

