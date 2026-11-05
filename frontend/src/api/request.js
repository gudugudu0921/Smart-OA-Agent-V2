import axios from 'axios'
import { ElMessage } from 'element-plus'

const TOKEN_KEY = 'smart_oa_access_token'
const runtimeEnv = import.meta.env || {}
const DEFAULT_API_BASE_URL = runtimeEnv.DEV ? 'http://127.0.0.1:8000' : '/api'

export function getApiErrorMessage(error) {
  if (!error?.response) return '无法连接服务器，请检查网络后重试。'
  const data = error.response.data
  if (typeof data?.message === 'string') return data.message
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail.message === 'string') return detail.message
  if (Array.isArray(detail)) return '参数格式错误'
  return '请求失败，请稍后重试。'
}

export const getErrorMessage = getApiErrorMessage

export function getApiErrorCode(error) {
  const code = error?.response?.data?.code
  if (typeof code === 'string') return code
  const detail = error?.response?.data?.detail
  return detail && typeof detail === 'object' ? detail.code : detail
}

const request = axios.create({
  baseURL: runtimeEnv.VITE_API_BASE_URL || DEFAULT_API_BASE_URL,
  timeout: 30000,
})

request.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

request.interceptors.response.use(
  (response) => response,
  (error) => {
    error.requestId =
      error?.response?.headers?.['x-request-id'] ||
      error?.response?.data?.request_id ||
      null
    const status = error?.response?.status
    const silent = error?.config?.silentError
    const code = getApiErrorCode(error)
    const message = getApiErrorMessage(error)
    const systemMessage = error.requestId
      ? `${message} 请求编号：${error.requestId}`
      : message
    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem('smart_oa_current_user')
      if (!silent && window.location.pathname !== '/login') {
        ElMessage.error(
          code === 'TOKEN_REVOKED_BY_PASSWORD_CHANGE'
            ? '登录状态已失效，请重新登录。'
            : message,
        )
      }
      if (window.location.pathname !== '/login') window.location.assign('/login')
    } else if (status === 403 && code === 'PASSWORD_CHANGE_REQUIRED') {
      if (!silent) ElMessage.warning(message)
      if (window.location.pathname !== '/change-password') window.location.assign('/change-password')
    } else if (!silent && status === 403) {
      ElMessage.error('没有权限执行该操作')
    } else if (!silent && status === 404) {
      ElMessage.error('请求的资源不存在')
    } else if (!silent && status === 409) {
      ElMessage.error(message)
    } else if (!silent && status === 422) {
      ElMessage.error('参数格式错误')
    } else if (!silent && (status === 500 || status === 503)) {
      ElMessage.error(systemMessage)
    } else if (!silent) {
      ElMessage.error(message)
    }
    return Promise.reject(error)
  },
)

export default request
