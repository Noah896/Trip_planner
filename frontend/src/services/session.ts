/** 浏览器本地会话 ID;用户身份统一来自服务端签发的登录令牌。 */

import { getStoredAuthUser } from '@/services/auth'

const SESSION_KEY_PREFIX = 'tripPlannerSessionId'

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
  user_id?: string
}

function currentSessionKey(): string {
  const user = getStoredAuthUser()
  return `${SESSION_KEY_PREFIX}:${user?.user_id ?? 'anonymous'}`
}

export function getMemoryIdentity(): MemoryIdentity {
  const user = getStoredAuthUser()
  return {
    session_id: getOrCreate(currentSessionKey(), 'session'),
    user_id: user?.user_id
  }
}

export function resetMemorySession(): string {
  const sessionId = randomId('session')
  localStorage.setItem(currentSessionKey(), sessionId)
  return sessionId
}
