/** 浏览器本地记忆身份。登录接入后,user_id 应改为由鉴权层提供。 */

const SESSION_KEY = 'tripPlannerSessionId'
const USER_KEY = 'tripPlannerUserId'

function randomId(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid) return `${prefix}-${uuid}`
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function getOrCreate(key: string, prefix: string): string {
  const existing = localStorage.getItem(key)?.trim()
  if (existing) return existing
  const created = randomId(prefix)
  localStorage.setItem(key, created)
  return created
}

export interface MemoryIdentity {
  session_id: string
  user_id: string
}

export function getMemoryIdentity(): MemoryIdentity {
  return {
    session_id: getOrCreate(SESSION_KEY, 'session'),
    user_id: getOrCreate(USER_KEY, 'local')
  }
}

export function resetMemorySession(): string {
  const sessionId = randomId('session')
  localStorage.setItem(SESSION_KEY, sessionId)
  return sessionId
}