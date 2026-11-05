import request from './request'

export const getNotifications = (params = {}) => request.get('/notifications', { params })
export const getUnreadCount = () => request.get('/notifications/unread-count')
export const getTodos = () => request.get('/notifications/todos')
export const markNotificationRead = (notificationId) => request.post(`/notifications/${notificationId}/read`)
export const markAllNotificationsRead = () => request.post('/notifications/read-all')
