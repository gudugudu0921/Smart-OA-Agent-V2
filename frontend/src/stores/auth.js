import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '../api/auth'

const TOKEN_KEY = 'smart_oa_access_token'
const USER_KEY = 'smart_oa_current_user'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) || '')
  const currentUser = ref(JSON.parse(localStorage.getItem(USER_KEY) || 'null'))
  const initialized = ref(false)
  const isLoggedIn = computed(() => Boolean(token.value && currentUser.value))

  function persistUser(user) {
    currentUser.value = user
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  }

  function clearAuth() {
    token.value = ''
    currentUser.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  async function login(credentials) {
    const { data } = await authApi.login(credentials)
    token.value = data.access_token
    localStorage.setItem(TOKEN_KEY, token.value)
    const me = await authApi.getMe()
    persistUser(me.data)
  }

  async function fetchCurrentUser() {
    const { data } = await authApi.getMe()
    persistUser(data)
    return data
  }

  async function changePassword(payload) {
    await authApi.changePassword(payload)
    clearAuth()
  }

  async function restoreAuth() {
    if (initialized.value) return isLoggedIn.value
    try {
      if (!token.value) return false
      await fetchCurrentUser()
      return true
    } catch {
      clearAuth()
      return false
    } finally {
      initialized.value = true
    }
  }

  function logout() {
    clearAuth()
  }

  return { token, currentUser, initialized, isLoggedIn, login, logout, changePassword, fetchCurrentUser, restoreAuth }
})
