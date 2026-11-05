export const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export const TERMINAL_TASK_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])
export const UNCERTAIN_OPERATION_STATUSES = new Set(['PROCESSING', 'UNKNOWN'])

const LEAVE_TYPE_LABELS = Object.freeze({
  annual: '年假',
  sick: '病假',
  personal: '事假',
  other: '其他',
})

export class ChatProtocolError extends Error {
  constructor(message, code = 'CHAT_CLIENT_PROTOCOL_CONFLICT') {
    super(message)
    this.name = 'ChatProtocolError'
    this.code = code
  }
}

export function isUuidV4(value) {
  return typeof value === 'string' && UUID_V4_PATTERN.test(value)
}

export function createUuidV4(cryptoApi = globalThis.crypto) {
  if (!cryptoApi || typeof cryptoApi.randomUUID !== 'function') {
    throw new ChatProtocolError('当前浏览器不支持安全UUID生成。', 'CHAT_UUID_UNAVAILABLE')
  }
  const value = cryptoApi.randomUUID().toLowerCase()
  if (!isUuidV4(value)) throw new ChatProtocolError('浏览器生成了无效UUID。', 'CHAT_UUID_INVALID')
  return value
}

export function createDistinctUuid(excluded, uuidFactory = createUuidV4) {
  const blocked = new Set([...excluded].filter(Boolean).map((item) => String(item).toLowerCase()))
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const value = uuidFactory().toLowerCase()
    if (isUuidV4(value) && !blocked.has(value)) return value
  }
  throw new ChatProtocolError('无法生成彼此独立的请求标识。', 'CHAT_UUID_COLLISION')
}

export function buildChatRequest({
  threadId,
  message,
  operationId,
  taskAction = 'message',
  taskRevision = 0,
  uuidFactory = createUuidV4,
}) {
  if (!isUuidV4(threadId)) throw new ChatProtocolError('会话ID无效。')
  if (!isUuidV4(operationId)) throw new ChatProtocolError('业务操作ID无效。')
  const normalizedMessage = String(message ?? '').trim()
  if (!normalizedMessage) throw new ChatProtocolError('消息不能为空。')
  if (!['message', 'confirm', 'cancel'].includes(taskAction)) {
    throw new ChatProtocolError('任务动作无效。')
  }
  if (!Number.isInteger(taskRevision) || taskRevision < 0) {
    throw new ChatProtocolError('任务版本无效。')
  }
  const messageRequestId = createDistinctUuid([threadId, operationId], uuidFactory)
  return {
    thread_id: threadId,
    message: normalizedMessage,
    message_request_id: messageRequestId,
    operation_id: operationId,
    task_action: taskAction,
    task_revision: taskRevision,
  }
}

export function isActionableConfirmation(task) {
  const confirmation = task?.confirmation
  const isSubmitConfirmation = confirmation
    && confirmation.operation_type == null
    && typeof confirmation.leave_type === 'string'
    && typeof confirmation.start_date === 'string'
    && typeof confirmation.end_date === 'string'
  const isCancelConfirmation = confirmation?.operation_type === 'CANCEL_LEAVE'
    && isUuidV4(confirmation.application_id)
    && typeof confirmation.leave_type === 'string'
    && typeof confirmation.start_date === 'string'
    && typeof confirmation.end_date === 'string'
    && confirmation.status === 'pending'
  const isReviewConfirmation = ['MANAGER_REVIEW', 'HR_REVIEW'].includes(confirmation?.operation_type)
    && isUuidV4(confirmation.application_id)
    && typeof confirmation.employee_name === 'string'
    && confirmation.employee_name.length > 0
    && typeof confirmation.employee_no === 'string'
    && confirmation.employee_no.length > 0
    && typeof confirmation.leave_type === 'string'
    && typeof confirmation.start_date === 'string'
    && typeof confirmation.end_date === 'string'
    && confirmation.status === 'pending'
    && ['approved', 'rejected'].includes(confirmation.decision)
    && (confirmation.comment == null || typeof confirmation.comment === 'string')
  return Boolean(
    task?.task_status === 'awaiting_confirmation'
      && task?.requires_confirmation === true
      && isUuidV4(task?.operation_id)
      && Number.isInteger(task?.task_revision)
      && task.task_revision >= 0
      && confirmation
      && confirmation.task_revision === task.task_revision
      && (isSubmitConfirmation || isCancelConfirmation || isReviewConfirmation)
      && !['needs_reconciliation', 'restore_required', 'remote_confirming', 'lookup_failed', 'not_found', 'multiple_pending', 'corrupt'].includes(task?.local_status),
  )
}

export function leaveTypeLabel(value) {
  return LEAVE_TYPE_LABELS[value] || value || '未知'
}

export function confirmationReason(value) {
  return typeof value === 'string' && value.trim() ? value : '未填写'
}

export function isUncertainChatResponse(data) {
  return Boolean(
    UNCERTAIN_OPERATION_STATUSES.has(data?.operation_status)
      || ['executing', 'unknown'].includes(data?.task_status)
      || ['CHAT_MESSAGE_RESULT_UNKNOWN', 'OPERATION_IN_PROGRESS', 'OPERATION_RESULT_UNKNOWN'].includes(data?.code),
  )
}

export function validateChatResponse(data, payload) {
  if (!data || typeof data !== 'object') throw new ChatProtocolError('聊天响应格式无效。')
  if (data.thread_id !== payload.thread_id) throw new ChatProtocolError('响应会话ID不匹配。')
  if (data.message_request_id && data.message_request_id !== payload.message_request_id) {
    throw new ChatProtocolError('响应消息ID不匹配。')
  }
  if (data.operation_id && data.operation_id !== payload.operation_id) {
    throw new ChatProtocolError('响应业务操作ID不匹配。', 'CHAT_OPERATION_ID_CONFLICT')
  }
  const hasTask = typeof data.task_status === 'string'
  if (hasTask && data.task_status !== 'cancelled' && !isUuidV4(data.operation_id)) {
    throw new ChatProtocolError('任务响应缺少有效业务操作ID。')
  }
  if (data.task_status === 'awaiting_confirmation' && !isActionableConfirmation(data)) {
    throw new ChatProtocolError('服务端确认摘要与任务版本不一致。')
  }
  return data
}

export function validateSessionTaskResponse(data, threadId) {
  if (!data || typeof data !== 'object' || data.thread_id !== threadId) {
    throw new ChatProtocolError('恢复任务的会话ID不匹配。')
  }
  const statuses = new Set([
    'idle', 'collecting', 'awaiting_confirmation', 'executing',
    'succeeded', 'failed', 'unknown',
  ])
  if (!statuses.has(data.task_status)
    || !Number.isInteger(data.task_revision)
    || data.task_revision < 0
    || typeof data.requires_confirmation !== 'boolean'
    || typeof data.code !== 'string') {
    throw new ChatProtocolError('恢复任务响应格式无效。')
  }
  if (data.task_status === 'idle') {
    if (data.operation_id != null || data.requires_confirmation || data.confirmation != null) {
      throw new ChatProtocolError('空闲任务包含不应存在的业务绑定。')
    }
    return data
  }
  if (data.operation_id != null && !isUuidV4(data.operation_id)) {
    throw new ChatProtocolError('恢复任务包含无效业务操作ID。')
  }
  if (data.task_status === 'awaiting_confirmation') {
    if (!isActionableConfirmation({ ...data, local_status: 'active' })) {
      throw new ChatProtocolError('恢复确认摘要与任务版本不一致。')
    }
  } else if (data.requires_confirmation || data.confirmation != null) {
    throw new ChatProtocolError('非待确认任务不得返回可执行确认摘要。')
  }
  if (data.operation_status != null) {
    throw new ChatProtocolError('Redis任务投影不得声明MySQL业务结果。')
  }
  return data
}

export function validateOperationStatusResponse(data, operationId) {
  if (!data || typeof data !== 'object'
    || data.operation_id !== operationId
    || !isUuidV4(data.operation_id)
    || !['SUBMIT_LEAVE', 'CANCEL_LEAVE', 'MANAGER_REVIEW', 'HR_REVIEW'].includes(data.operation_type)
    || !['PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN'].includes(data.status)) {
    throw new ChatProtocolError('操作状态响应格式无效。')
  }
  if (data.status === 'SUCCEEDED') {
    const applicationId = data.result_payload?.application_id
    if (!isUuidV4(data.resource_id)
      || !isUuidV4(applicationId)
      || data.resource_id !== applicationId) {
      throw new ChatProtocolError('成功操作缺少可验证的业务资源。')
    }
  } else if (data.result_payload != null) {
    throw new ChatProtocolError('未成功操作不得返回可重放业务结果。')
  }
  return data
}
