import request from './request.js'

export const createSession = () => request.post('/chat/sessions')
export const listSessions = () => request.get('/chat/sessions')
export const getSession = (threadId) => request.get(`/chat/sessions/${threadId}`)
export const deleteSession = (threadId) => request.delete(`/chat/sessions/${threadId}`)
export const getSessionMessages = (threadId) => request.get(`/chat/sessions/${threadId}/messages`)
export const getSessionTask = (threadId) => request.get(`/chat/sessions/${threadId}/task`, { silentError: true })
export const getOperation = (operationId) => request.get(`/operations/${operationId}`, { silentError: true })
export const sendChat = (payload) => request.post('/chat', payload, { silentError: true })
