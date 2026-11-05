import request from './request'

export const login = (payload) => request.post('/auth/login', payload, { silentError: true })
export const getMe = () => request.get('/users/me', { silentError: true })
export const changePassword = (payload) => request.post('/auth/change-password', payload, { silentError: true })
