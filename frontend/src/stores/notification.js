import { ref } from 'vue'
import { defineStore } from 'pinia'
import * as notificationApi from '../api/notifications'

const emptyTodos = () => ({ role: '', total: 0, items: { manager_approvals: 0, hr_approvals: 0 } })

export const useNotificationStore = defineStore('notification', () => {
  const notifications = ref([])
  const unreadCount = ref(0)
  const todos = ref(emptyTodos())
  const loading = ref(false)

  async function loadNotifications(params = {}) {
    loading.value = true
    try {
      const { data } = await notificationApi.getNotifications({ limit: 50, ...params })
      notifications.value = data
      return data
    } finally {
      loading.value = false
    }
  }

  async function loadUnreadCount() {
    const { data } = await notificationApi.getUnreadCount()
    unreadCount.value = data.unread_count
    return unreadCount.value
  }

  async function loadTodos() {
    const { data } = await notificationApi.getTodos()
    todos.value = data
    return data
  }

  async function markRead(notificationId) {
    await notificationApi.markNotificationRead(notificationId)
    await Promise.all([loadNotifications(), loadUnreadCount()])
  }

  async function markAllRead() {
    const { data } = await notificationApi.markAllNotificationsRead()
    await Promise.all([loadNotifications(), loadUnreadCount()])
    return data.updated
  }

  async function refresh() {
    await Promise.all([loadNotifications(), loadUnreadCount(), loadTodos()])
  }

  async function refreshSummary() {
    await Promise.all([loadUnreadCount(), loadTodos()])
  }

  function reset() {
    notifications.value = []
    unreadCount.value = 0
    todos.value = emptyTodos()
  }

  return {
    notifications,
    unreadCount,
    todos,
    loading,
    loadNotifications,
    loadUnreadCount,
    loadTodos,
    markRead,
    markAllRead,
    refresh,
    refreshSummary,
    reset,
  }
})
