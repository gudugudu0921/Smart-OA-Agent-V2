import request from './request'

export const getUsers = () => request.get('/users/')
export const createUser = (payload) => request.post('/users/', payload)
export const setUserStatus = (userId, isActive) => request.patch(`/users/${userId}/status`, { is_active: isActive })
export const resetUserPassword = (userId) => request.post(`/users/${userId}/reset-password`)
