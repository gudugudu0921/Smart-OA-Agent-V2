import request from './request'

export const createLeave = (payload) => request.post('/leave/', payload)
export const getMyLeaves = () => request.get('/leave/my')
export const cancelLeave = (id) => request.post(`/leave/${id}/cancel`)
export const getManagerPending = () => request.get('/leave/pending/manager')
export const getTeamLeaves = () => request.get('/leave/team')
export const managerReview = (id, payload) => request.post(`/leave/${id}/manager-review`, payload)
export const getHrPending = () => request.get('/leave/pending/hr')
export const getAllLeaves = (params = {}) => request.get('/leave/all', { params })
export const hrReview = (id, payload) => request.post(`/leave/${id}/hr-review`, payload)
