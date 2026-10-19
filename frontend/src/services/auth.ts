import type { AuthTokenResponse, AuthUser } from '@/types'

const TOKEN_KEY = 'tripPlannerAuthToken'
const USER_KEY = 'tripPlannerAuthUser'
const EXPIRES_KEY = 'tripPlannerAuthExpiresAt'
const AUTH_DISABLED_KEY = 'tripPlannerAuthDisabled'

export const AUTH_CHANGED_EVENT = 'trip-planner-auth-changed'

function emitAuthChanged(): void {
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT))
}

function clearTransientTripData(): void {
  sessionStorage.removeItem('tripPlan')
  sessionStorage.removeItem('tripPlanDegraded')
  sessionStorage.removeItem('tripPlanMemoryReceipt')
}

export function getAuthToken(): string {
  const token = localStorage.getItem(TOKEN_KEY)?.trim() ?? ''
  const expiresAt = localStorage.getItem(EXPIRES_KEY) ?? ''
  if (!token) return ''
  if (expiresAt && Date.parse(expiresAt) <= Date.now()) {
    clearAuthSession()
    return ''
  }
  return token
}

export function getStoredAuthUser(): AuthUser | null {
  if (!getAuthToken()) return null
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as AuthUser
  } catch {
    clearAuthSession()
    return null
  }
}

export function saveAuthSession(response: AuthTokenResponse): void {
  clearTransientTripData()
  localStorage.removeItem(AUTH_DISABLED_KEY)
  localStorage.setItem(TOKEN_KEY, response.access_token)
  localStorage.setItem(USER_KEY, JSON.stringify(response.user))
  localStorage.setItem(EXPIRES_KEY, response.expires_at)
  emitAuthChanged()
}

export function setAuthDisabled(disabled: boolean): void {
  if (disabled) {
    localStorage.setItem(AUTH_DISABLED_KEY, '1')
  } else {
    localStorage.removeItem(AUTH_DISABLED_KEY)
  }
}

export function isAuthDisabled(): boolean {
  return localStorage.getItem(AUTH_DISABLED_KEY) === '1'
}

export function clearAuthSession(): void {
  clearTransientTripData()
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(EXPIRES_KEY)
  emitAuthChanged()
}
