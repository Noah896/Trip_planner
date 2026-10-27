// 类型定义

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
}

export interface TripFormData {
  session_id?: string
  user_id?: string
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
  free_text_input: string
}

export interface AuthUser {
  user_id: string
  username: string
  created_at: string
}

export interface AuthTokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_at: string
  user: AuthUser
}

export interface AuthStatus {
  enabled: boolean
  registration_enabled: boolean
  registration_requires_key: boolean
}

export interface UsedMemoryFact {
  category: string
  key: string
  value: string
  source_text: string
}

export interface KnowledgeSource {
  id: string
  chunk_id: string
  title: string
  section: string
  publisher: string
  source_url: string
  verified_at: string
  category: string
}

export interface HistoryUsage {
  history_id: number
  city: string
  start_date: string
  end_date: string
  travel_days: number
  preferences: string[]
}

export interface TripPlanResponse {
  success: boolean
  // 为 true 时表示后端返回的是兜底占位行程(AI 规划未成功)
  degraded?: boolean
  message: string
  data?: TripPlan
  session_id?: string
  memory_saved?: boolean
  memory_version?: number
  used_memory?: UsedMemoryFact[]
  used_history?: HistoryUsage | null
  knowledge_used?: boolean
  knowledge_degraded?: boolean
  knowledge_status?: string
  knowledge_sources?: KnowledgeSource[]
}



export interface HistoryTripSummary {
  id: number
  session_id: string
  city: string
  start_date: string
  end_date: string
  travel_days: number
  preferences: string[]
  overview: string
  attraction_names: string[]
  created_at: string
}

export interface HistoryTripListResponse {
  total: number
  items: HistoryTripSummary[]
}

export interface HistoryTripDetail extends HistoryTripSummary {
  request: TripFormData
  plan: TripPlan
}
// ---------- /api/trip/plan/stream 的 SSE 事件 ----------

/** 某一步的进度(景点检索 / 天气查询 / 酒店检索 / 生成行程) */
export interface TripProgressEvent {
  type: 'progress'
  stage: string
  label: string
  status: 'running' | 'done'
  elapsed: number
}

/** 最后一条:与 TripPlanResponse 内容一致 */
export interface TripStreamResult {
  type: 'result'
  success: boolean
  degraded: boolean
  reason: string
  message: string
  elapsed: number
  data: TripPlan
  memory_saved?: boolean
  memory_version?: number
  used_memory?: UsedMemoryFact[]
  used_history?: HistoryUsage | null
  knowledge_used?: boolean
  knowledge_degraded?: boolean
  knowledge_status?: string
  knowledge_sources?: KnowledgeSource[]
}

/** 流中途出错(此时 HTTP 状态码已经是 200,只能靠事件报错) */
export interface TripStreamError {
  type: 'error'
  message: string
}

export type TripStreamEvent = TripProgressEvent | TripStreamResult | TripStreamError

// ---------- /api/chat/stream 的事件 ----------

/** 助手说的一句话。**text 是增量**:一次回复会被拆成很多条,前端按顺序追加 */
export interface ChatMessageEvent {
  type: 'message'
  text: string
}

/** 结构化行程:前端渲染成卡片,内容与结果页一致(天气 / 景点 / 酒店 / 预算) */
export interface ChatArtifactEvent {
  type: 'artifact'
  kind: 'trip_plan'
  degraded: boolean
  reason?: string
  message?: string
  elapsed?: number
  /** 后端解析出来的条件,后续"改行程"要用 */
  request?: TripFormData
  data: TripPlan
  used_memory?: UsedMemoryFact[]
  used_history?: HistoryUsage | null
  knowledge_used?: boolean
  knowledge_degraded?: boolean
  knowledge_status?: string
  knowledge_sources?: KnowledgeSource[]
}

export interface ChatDoneEvent {
  type: 'done'
  request?: TripFormData
  session_id?: string
  memory_saved?: boolean
  memory_version?: number
  used_memory?: UsedMemoryFact[]
  used_history?: HistoryUsage | null
  knowledge_used?: boolean
  knowledge_degraded?: boolean
  knowledge_status?: string
  knowledge_sources?: KnowledgeSource[]
}

export interface ChatErrorEvent {
  type: 'error'
  message: string
}

export type ChatEvent =
  | ChatMessageEvent
  | TripProgressEvent
  | ChatArtifactEvent
  | ChatDoneEvent
  | ChatErrorEvent

/** 一轮对话里发给后端的上下文 */
export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
}
