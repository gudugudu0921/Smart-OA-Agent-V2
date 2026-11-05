import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as defaultChatApi from '../api/chat.js'
import { getApiErrorCode, getApiErrorMessage } from '../api/request.js'
import {
  chatOutbox as defaultOutbox,
  inspectOutboxRecords,
  OUTBOX_STATUS,
  OutboxUnavailableError,
} from '../services/chatOutbox.js'
import { createChatCoordination } from '../services/chatCoordination.js'
import { selectChatResultData } from '../utils/leaveResult.js'
import {
  buildChatRequest,
  ChatProtocolError,
  createDistinctUuid,
  createUuidV4,
  isActionableConfirmation,
  isUncertainChatResponse,
  isUuidV4,
  validateChatResponse,
  validateOperationStatusResponse,
  validateSessionTaskResponse,
} from '../utils/chatProtocol.js'

const CONFIRM_MESSAGE = '确认提交当前请假申请'
const CANCEL_MESSAGE = '取消当前请假任务'

const UNCERTAIN_ERROR_CODES = new Set([
  'CHAT_TURN_IN_PROGRESS',
  'CHAT_TURN_LOCK_LOST',
  'CHAT_MESSAGE_REQUEST_CONFLICT',
  'CHAT_MESSAGE_RESULT_UNKNOWN',
  'OPERATION_IN_PROGRESS',
  'OPERATION_RESULT_UNKNOWN',
  'REDIS_UNAVAILABLE',
])

const STALE_TASK_ERROR_CODES = new Set([
  'CHAT_MESSAGE_REQUEST_CONFLICT',
  'CHAT_OPERATION_ID_CONFLICT',
  'CHAT_TASK_REVISION_CONFLICT',
  'IDEMPOTENCY_CONFLICT',
])

const TERMINAL_FAILURE_ERROR_CODES = new Set([
  'OPERATION_FAILED',
])

const ERROR_MESSAGES = Object.freeze({
  CHAT_TURN_IN_PROGRESS: '当前会话正在处理另一条消息，本次请求不会自动重试。',
  CHAT_TURN_LOCK_LOST: '会话安全锁已失效，结果待核查，请勿重复提交。',
  CHAT_MESSAGE_REQUEST_CONFLICT: '消息标识与原请求不一致，已阻止重复处理。',
  CHAT_MESSAGE_RESULT_UNKNOWN: '消息结果尚未确认，已保留原始标识等待核查。',
  CHAT_OPERATION_ID_CONFLICT: '业务操作标识与当前任务不一致，已阻止确认。',
  CHAT_TASK_REVISION_CONFLICT: '任务参数已更新，请使用最新确认信息。',
  CHAT_OPERATION_ID_REQUIRED: '创建请假需要安全业务操作标识，请重新开始该任务。',
  IDEMPOTENCY_CONFLICT: '该业务操作标识已绑定到不同参数，无法继续。',
  OPERATION_IN_PROGRESS: '请假操作仍在处理中，请勿再次确认。',
  OPERATION_FAILED: '请假操作已明确失败，不会自动重新提交。',
  OPERATION_RESULT_UNKNOWN: '请假结果尚不能确认，请保留本次操作等待核查。',
  REDIS_UNAVAILABLE: '会话服务暂时不可用，原始请求已保留，不会自动重试。',
})

function emptyTask(overrides = {}) {
  return {
    operation_id: null,
    task_status: 'idle',
    task_revision: 0,
    requires_confirmation: false,
    confirmation: null,
    operation_status: null,
    operation_type: null,
    code: null,
    local_status: 'idle',
    application_id: null,
    can_reconcile: false,
    reconciling: false,
    recovery_record_count: 0,
    recovery_message_request_id: null,
    ...overrides,
  }
}

function actorId(value) {
  const normalized = String(value ?? '').trim()
  if (!normalized) throw new ChatProtocolError('无法确定当前登录用户。', 'CHAT_ACTOR_REQUIRED')
  return normalized
}

function isUncertainError(error) {
  if (!error?.response) return true
  const code = getApiErrorCode(error)
  return UNCERTAIN_ERROR_CODES.has(code) || Number(error.response.status) >= 500
}

function displayError(error) {
  const code = error?.code || getApiErrorCode(error)
  const base = ERROR_MESSAGES[code]
    || (error instanceof OutboxUnavailableError
      ? '本地安全存储不可用，本次消息未发送。'
      : getApiErrorMessage(error))
  return error?.requestId ? `${base} 请求编号：${error.requestId}` : base
}

export function createChatStoreDefinition({
  storeId = 'chat',
  api = defaultChatApi,
  outbox = defaultOutbox,
  uuidFactory = createUuidV4,
  coordinationFactory = createChatCoordination,
} = {}) {
  return defineStore(storeId, () => {
    const sessions = ref([])
    const currentThreadId = ref('')
    const messages = ref([])
    const loading = ref(false)
    const sendingThreadId = ref('')
    const taskStates = ref({})
    const activeAction = ref(null)
    let coordinator = null
    let coordinatorActor = null
    const restoringThreads = new Map()

    const sending = computed(() => Boolean(sendingThreadId.value))
    const currentTask = computed(
      () => taskStates.value[currentThreadId.value] || emptyTask(),
    )
    const inputBlocked = computed(() => {
      const task = currentTask.value
      return ['executing', 'unknown'].includes(task.task_status)
        || [
          'needs_reconciliation', 'restore_required', 'outbox_unavailable',
          'remote_confirming', 'lookup_failed', 'not_found', 'multiple_pending',
          'corrupt',
        ].includes(task.local_status)
    })

    function setTask(threadId, task) {
      taskStates.value = { ...taskStates.value, [threadId]: emptyTask(task) }
    }

    function clearTask(threadId) {
      const next = { ...taskStates.value }
      delete next[threadId]
      taskStates.value = next
    }

    function releaseCoordination() {
      coordinator?.close()
      coordinator = null
      coordinatorActor = null
    }

    function ensureCoordination(userId) {
      const normalizedActor = actorId(userId)
      if (coordinator && coordinatorActor === normalizedActor) return coordinator
      releaseCoordination()
      coordinator = coordinationFactory()
      coordinatorActor = normalizedActor
      coordinator.open(normalizedActor, (event) => {
        if (event.type === 'confirm_claim') {
          const task = taskStates.value[event.thread_id]
          if (task?.operation_id === event.operation_id
            && task.task_status === 'awaiting_confirmation') {
            setTask(event.thread_id, {
              ...task,
              requires_confirmation: false,
              local_status: 'remote_confirming',
              code: 'CHAT_CROSS_TAB_CONFIRM_IN_PROGRESS',
            })
          }
          return
        }
        if (event.type === 'state_changed') {
          void restoreThread(normalizedActor, event.thread_id)
        }
      })
      return coordinator
    }

    function recordPriority(record) {
      const action = { confirm: 3, cancel: 2, message: 1 }[record.task_action] || 0
      const status = record.status === OUTBOX_STATUS.COMPLETED ? 0 : 10
      return status + action
    }

    async function loadLocalRecords(userId, threadId) {
      const raw = await outbox.listScope(userId, threadId)
      const inspected = inspectOutboxRecords(raw, { userId, threadId })
      const valid = []
      for (const record of inspected.valid) {
        if (record.status === OUTBOX_STATUS.SENDING) {
          valid.push(await outbox.markNeedsReconciliation(
            userId,
            threadId,
            record.message_request_id,
            'BROWSER_RESTART_RESULT_UNKNOWN',
          ))
        } else {
          valid.push(record)
        }
      }
      valid.sort((left, right) => {
        const priority = recordPriority(right) - recordPriority(left)
        return priority || right.created_at.localeCompare(left.created_at)
      })
      return { valid, invalid: inspected.invalid }
    }

    function localRecovery(records) {
      const unresolved = records.filter((record) => record.status !== OUTBOX_STATUS.COMPLETED)
      const operationIds = new Set(unresolved.map((record) => record.operation_id))
      return {
        unresolved,
        operationIds,
        primary: unresolved[0] || records[0] || null,
        confirm: unresolved.find((record) => record.task_action === 'confirm') || null,
      }
    }

    async function reconcileDuringRestore(userId, threadId) {
      try {
        await reconcileOperation(userId, threadId, { broadcast: false })
      } catch {
        // The read-only lookup already published a conservative local state.
        // Session/history loading must remain usable when MySQL status is
        // unavailable, 404, forbidden, or temporarily unreachable.
      }
    }

    async function restoreThreadInternal(userId, threadId) {
      const normalizedActor = actorId(userId)
      ensureCoordination(normalizedActor)
      let local
      try {
        local = await loadLocalRecords(normalizedActor, threadId)
      } catch {
        setTask(threadId, {
          task_status: 'unknown',
          code: 'CHAT_OUTBOX_UNAVAILABLE',
          local_status: 'outbox_unavailable',
        })
        return
      }
      if (local.invalid.length) {
        setTask(threadId, {
          task_status: 'unknown',
          code: 'CHAT_OUTBOX_CORRUPT',
          local_status: 'corrupt',
          recovery_record_count: local.invalid.length,
        })
        return
      }
      const recovery = localRecovery(local.valid)
      if (recovery.operationIds.size > 1) {
        setTask(threadId, {
          task_status: 'unknown',
          code: 'CHAT_MULTIPLE_PENDING_OPERATIONS',
          local_status: 'multiple_pending',
          recovery_record_count: recovery.unresolved.length,
        })
        return
      }

      let serverTask
      try {
        const { data } = await api.getSessionTask(threadId)
        serverTask = validateSessionTaskResponse(data, threadId)
      } catch (error) {
        const primary = recovery.primary
        if (primary) {
          setTask(threadId, {
            operation_id: primary.operation_id,
            task_status: 'unknown',
            task_revision: primary.task_revision,
            code: getApiErrorCode(error) || 'CHAT_TASK_RECOVERY_UNAVAILABLE',
            local_status: 'restore_required',
            can_reconcile: primary.task_action === 'confirm',
            recovery_record_count: local.valid.length,
            recovery_message_request_id: primary.message_request_id,
          })
        } else {
          setTask(threadId, {
            task_status: 'unknown',
            code: 'CHAT_TASK_RECOVERY_UNAVAILABLE',
            local_status: 'restore_required',
          })
        }
        return
      }

      if (serverTask.task_status === 'idle') {
        if (!recovery.primary) {
          clearTask(threadId)
          return
        }
        setTask(threadId, {
          operation_id: recovery.primary.operation_id,
          task_status: 'unknown',
          task_revision: recovery.primary.task_revision,
          code: 'CHAT_LOCAL_RESULT_REQUIRES_LOOKUP',
          local_status: 'needs_reconciliation',
          can_reconcile: recovery.primary.task_action === 'confirm',
          recovery_record_count: local.valid.length,
          recovery_message_request_id: recovery.primary.message_request_id,
        })
        if (recovery.primary.task_action === 'confirm') {
          await reconcileDuringRestore(normalizedActor, threadId)
        }
        return
      }

      if (!isUuidV4(serverTask.operation_id)) {
        setTask(threadId, {
          task_status: 'unknown',
          code: serverTask.code || 'CHAT_TASK_PROTOCOL_UPGRADE_REQUIRED',
          local_status: 'restore_required',
          recovery_record_count: local.valid.length,
        })
        return
      }
      const boundRecords = local.valid.filter(
        (record) => record.operation_id === serverTask.operation_id,
      )
      const conflicting = recovery.unresolved.some(
        (record) => record.operation_id !== serverTask.operation_id,
      )
      const serverTerminal = ['executing', 'succeeded', 'failed', 'unknown']
        .includes(serverTask.task_status)
      if (conflicting || (!boundRecords.length && !serverTerminal)) {
        setTask(threadId, {
          operation_id: serverTask.operation_id,
          task_status: 'unknown',
          task_revision: serverTask.task_revision,
          code: conflicting ? 'CHAT_LOCAL_OPERATION_CONFLICT' : 'CHAT_LOCAL_BINDING_MISSING',
          local_status: conflicting ? 'multiple_pending' : 'restore_required',
          recovery_record_count: local.valid.length,
        })
        return
      }
      if (!boundRecords.length && serverTerminal) {
        setTask(threadId, {
          operation_id: serverTask.operation_id,
          task_status: serverTask.task_status,
          task_revision: serverTask.task_revision,
          code: serverTask.code,
          local_status: 'server_terminal',
          can_reconcile: true,
          recovery_record_count: 0,
        })
        await reconcileDuringRestore(normalizedActor, threadId)
        return
      }
      const unknownConfirm = recovery.confirm?.operation_id === serverTask.operation_id
        ? recovery.confirm
        : null
      if (unknownConfirm) {
        setTask(threadId, {
          operation_id: serverTask.operation_id,
          task_status: 'unknown',
          task_revision: unknownConfirm.task_revision,
          code: 'CHAT_CONFIRM_RESULT_REQUIRES_LOOKUP',
          local_status: 'needs_reconciliation',
          can_reconcile: true,
          recovery_record_count: local.valid.length,
          recovery_message_request_id: unknownConfirm.message_request_id,
        })
        await reconcileDuringRestore(normalizedActor, threadId)
        return
      }

      if (['collecting', 'awaiting_confirmation'].includes(serverTask.task_status)) {
        setTask(threadId, {
          ...serverTask,
          local_status: 'active',
          can_reconcile: false,
          recovery_record_count: local.valid.length,
        })
        return
      }

      const confirmRecord = boundRecords.find((record) => record.task_action === 'confirm')
      setTask(threadId, {
        operation_id: serverTask.operation_id,
        task_status: serverTask.task_status,
        task_revision: serverTask.task_revision,
        code: serverTask.code,
        local_status: 'needs_reconciliation',
        can_reconcile: Boolean(confirmRecord),
        recovery_record_count: local.valid.length,
        recovery_message_request_id: confirmRecord?.message_request_id || null,
      })
      if (confirmRecord) {
        await reconcileDuringRestore(normalizedActor, threadId)
      }
    }

    async function restoreThread(userId, threadId) {
      const key = `${actorId(userId)}:${threadId}`
      if (restoringThreads.has(key)) return restoringThreads.get(key)
      const pending = restoreThreadInternal(userId, threadId)
        .finally(() => restoringThreads.delete(key))
      restoringThreads.set(key, pending)
      return pending
    }

    async function reconcileOperation(userId, threadId = currentThreadId.value, { broadcast = true } = {}) {
      const normalizedActor = actorId(userId)
      const task = taskStates.value[threadId]
      if (!task?.can_reconcile || !isUuidV4(task.operation_id)) {
        throw new ChatProtocolError('当前没有可安全核查的已确认业务操作。')
      }
      const local = await loadLocalRecords(normalizedActor, threadId)
      if (local.invalid.length) {
        setTask(threadId, { ...task, task_status: 'unknown', local_status: 'corrupt', can_reconcile: false })
        throw new ChatProtocolError('本地待处理记录损坏，已停止核查。')
      }
      const confirmRecord = local.valid.find(
        (record) => record.operation_id === task.operation_id
          && record.task_action === 'confirm',
      )
      const trustedServerTerminal = task.local_status === 'server_terminal'
        && ['executing', 'succeeded', 'failed', 'unknown'].includes(task.task_status)
      if (!confirmRecord && !trustedServerTerminal) {
        setTask(threadId, { ...task, task_status: 'unknown', local_status: 'restore_required', can_reconcile: false })
        throw new ChatProtocolError('缺少可信confirm请求绑定，不能查询业务结果。')
      }
      setTask(threadId, { ...task, reconciling: true, confirmation: null, requires_confirmation: false })
      try {
        const { data } = await api.getOperation(task.operation_id)
        const operation = validateOperationStatusResponse(data, task.operation_id)
        if (operation.status === 'SUCCEEDED') {
          const applicationId = operation.result_payload.application_id
          setTask(threadId, {
            operation_id: task.operation_id,
            operation_type: operation.operation_type,
            task_status: 'succeeded',
            task_revision: task.task_revision,
            operation_status: 'SUCCEEDED',
            application_id: applicationId,
            code: 'OPERATION_SUCCEEDED',
            local_status: 'verified_success',
            can_reconcile: false,
            reconciling: false,
          })
          await clearOperationOutbox(normalizedActor, threadId, task.operation_id)
        } else if (operation.status === 'PROCESSING') {
          setTask(threadId, {
            ...task,
            task_status: 'executing',
            operation_status: 'PROCESSING',
            code: 'OPERATION_IN_PROGRESS',
            local_status: 'processing',
            can_reconcile: true,
            reconciling: false,
            confirmation: null,
            requires_confirmation: false,
          })
        } else if (operation.status === 'FAILED') {
          setTask(threadId, {
            ...task,
            task_status: 'failed',
            operation_status: 'FAILED',
            code: operation.error_code || 'OPERATION_FAILED',
            local_status: 'verified_failure',
            can_reconcile: true,
            reconciling: false,
            confirmation: null,
            requires_confirmation: false,
          })
          await completeOperationOutbox(normalizedActor, threadId, task.operation_id)
        } else {
          setTask(threadId, {
            ...task,
            task_status: 'unknown',
            operation_status: 'UNKNOWN',
            code: operation.error_code || 'OPERATION_RESULT_UNKNOWN',
            local_status: 'operation_unknown',
            can_reconcile: true,
            reconciling: false,
            confirmation: null,
            requires_confirmation: false,
          })
        }
        if (broadcast) ensureCoordination(normalizedActor).notifyStateChanged(threadId, task.operation_id)
        return operation
      } catch (error) {
        if (error instanceof ChatProtocolError) {
          setTask(threadId, {
            ...task,
            task_status: 'unknown',
            code: error.code,
            local_status: 'lookup_failed',
            can_reconcile: true,
            reconciling: false,
            confirmation: null,
            requires_confirmation: false,
          })
          throw error
        }
        const status = Number(error?.response?.status)
        const code = getApiErrorCode(error)
        const localStatus = status === 404
          ? 'not_found'
          : code === 'OPERATION_RESULT_UNKNOWN' ? 'operation_unknown' : 'lookup_failed'
        setTask(threadId, {
          ...task,
          task_status: 'unknown',
          code: code || 'OPERATION_LOOKUP_FAILED',
          local_status: localStatus,
          can_reconcile: true,
          reconciling: false,
          confirmation: null,
          requires_confirmation: false,
        })
        throw error
      }
    }

    function addAssistantMessage(threadId, content, { data = null, success = false, error = false } = {}) {
      if (currentThreadId.value !== threadId) return
      messages.value.push({ role: 'assistant', content, data, success, error })
    }

    async function updateOutbox(operation) {
      try {
        await operation()
        return true
      } catch {
        return false
      }
    }

    async function clearOperationOutbox(userId, threadId, operationId) {
      await updateOutbox(() => outbox.removeOperation(userId, threadId, operationId))
    }

    async function completeOperationOutbox(userId, threadId, operationId) {
      const records = await outbox.listScope(userId, threadId)
      await Promise.all(
        records
          .filter((record) => record.operation_id === operationId)
          .map((record) => outbox.markCompleted(userId, threadId, record.message_request_id)),
      )
    }

    async function clearScopeOutbox(userId, threadId) {
      const records = await outbox.listScope(userId, threadId)
      await Promise.all(
        records.map((record) => outbox.remove(userId, threadId, record.message_request_id)),
      )
    }

    async function loadSessions() {
      loading.value = true
      try {
        const { data } = await api.listSessions()
        sessions.value = data.sessions
        return sessions.value
      } finally {
        loading.value = false
      }
    }

    async function createSession(userId = null) {
      const { data } = await api.createSession()
      await loadSessions()
      currentThreadId.value = data.thread_id
      messages.value = []
      clearTask(data.thread_id)
      if (userId != null) ensureCoordination(userId)
      return data
    }

    async function loadMessages(threadId, userId = null) {
      loading.value = true
      try {
        await api.getSession(threadId)
        const { data } = await api.getSessionMessages(threadId)
        currentThreadId.value = threadId
        messages.value = data.messages
        if (userId != null) await restoreThread(userId, threadId)
        return data.messages
      } finally {
        loading.value = false
      }
    }

    async function selectSession(threadId, userId = null) {
      return loadMessages(threadId, userId)
    }

    async function deleteSessionById(threadId, userId = null) {
      const task = taskStates.value[threadId]
      if (task && ['executing', 'unknown'].includes(task.task_status)) {
        throw new ChatProtocolError('该会话存在结果待核查的操作，暂不能删除。')
      }
      await api.deleteSession(threadId)
      sessions.value = sessions.value.filter((item) => item.thread_id !== threadId)
      if (userId != null) await updateOutbox(() => clearScopeOutbox(actorId(userId), threadId))
      clearTask(threadId)
      if (currentThreadId.value === threadId) {
        currentThreadId.value = ''
        messages.value = []
      }
    }

    function messageTask(threadId) {
      const task = taskStates.value[threadId]
      if (!task || !isUuidV4(task.operation_id)) return null
      return ['collecting', 'awaiting_confirmation'].includes(task.task_status) ? task : null
    }

    function createPayload(threadId, content, taskAction) {
      const task = taskStates.value[threadId]
      if (taskAction === 'confirm') {
        if (!isActionableConfirmation(task)) {
          throw new ChatProtocolError('当前确认卡片已失效，不能提交。')
        }
        return buildChatRequest({
          threadId,
          message: CONFIRM_MESSAGE,
          operationId: task.operation_id,
          taskAction,
          taskRevision: task.task_revision,
          uuidFactory,
        })
      }
      if (taskAction === 'cancel') {
        if (!task || !isUuidV4(task.operation_id) || !['collecting', 'awaiting_confirmation'].includes(task.task_status)) {
          throw new ChatProtocolError('当前没有可取消的未提交任务。')
        }
        return buildChatRequest({
          threadId,
          message: CANCEL_MESSAGE,
          operationId: task.operation_id,
          taskAction,
          taskRevision: task.task_revision,
          uuidFactory,
        })
      }
      if (inputBlocked.value) {
        throw new ChatProtocolError('当前会话存在结果待核查的请求，不能开始新的操作。')
      }
      const active = messageTask(threadId)
      const operationId = active?.operation_id || createDistinctUuid([threadId], uuidFactory)
      return buildChatRequest({
        threadId,
        message: content,
        operationId,
        taskAction: 'message',
        taskRevision: active?.task_revision ?? 0,
        uuidFactory,
      })
    }

    async function applyResponse(userId, threadId, payload, data) {
      validateChatResponse(data, payload)
      const uncertain = isUncertainChatResponse(data)
      if (uncertain) {
        await updateOutbox(() => outbox.markNeedsReconciliation(
          userId,
          threadId,
          payload.message_request_id,
          data.code || data.operation_status,
        ))
      } else {
        await updateOutbox(() => outbox.markCompleted(userId, threadId, payload.message_request_id))
      }

      addAssistantMessage(threadId, data.response, {
        data: selectChatResultData(data.intent, data.data),
        success: data.success,
        error: !data.success && data.task_status !== 'awaiting_confirmation',
      })

      if (!data.operation_id && !data.task_status) {
        if (messageTask(threadId)) throw new ChatProtocolError('活动任务响应缺少任务状态。')
        clearTask(threadId)
        await updateOutbox(() => outbox.remove(userId, threadId, payload.message_request_id))
        return
      }

      if (data.task_status === 'cancelled') {
        clearTask(threadId)
        await clearOperationOutbox(userId, threadId, payload.operation_id)
        return
      }

      if (data.task_status === 'succeeded') {
        if (data.success !== true || data.operation_status !== 'SUCCEEDED') {
          throw new ChatProtocolError('服务端成功状态缺少MySQL事实确认。')
        }
        clearTask(threadId)
        await clearOperationOutbox(userId, threadId, payload.operation_id)
        return
      }

      setTask(threadId, {
        operation_id: data.operation_id,
        task_status: data.task_status || 'unknown',
        task_revision: data.task_revision ?? payload.task_revision,
        requires_confirmation: data.requires_confirmation === true,
        confirmation: data.confirmation || null,
        operation_status: data.operation_status || null,
        code: data.code || null,
        local_status: uncertain ? 'needs_reconciliation' : 'active',
      })
    }

    async function execute(content, userId, taskAction = 'message') {
      const normalizedActor = actorId(userId)
      const threadId = currentThreadId.value
      if (!threadId || sending.value) return undefined
      ensureCoordination(normalizedActor)
      const payload = createPayload(threadId, content, taskAction)
      activeAction.value = taskAction

      try {
        await outbox.enqueue({ userId: normalizedActor, threadId, payload })
      } catch (error) {
        addAssistantMessage(threadId, displayError(error), { error: true })
        activeAction.value = null
        throw error
      }

      const visibleMessage = taskAction === 'message'
        ? payload.message
        : taskAction === 'confirm' ? '确认提交' : '取消任务'
      if (currentThreadId.value === threadId) {
        messages.value.push({ role: 'user', content: visibleMessage })
      }
      sendingThreadId.value = threadId

      try {
        const { data } = await api.sendChat(payload)
        await applyResponse(normalizedActor, threadId, payload, data)
        try {
          await loadSessions()
        } catch {
          // Session list refresh is auxiliary. A successful Chat response must
          // never be downgraded to an unknown business result by this failure.
        }
        return data
      } catch (error) {
        const errorCode = error.code || getApiErrorCode(error)
        const staleTask = STALE_TASK_ERROR_CODES.has(errorCode)
        const terminalFailure = TERMINAL_FAILURE_ERROR_CODES.has(errorCode)
        const uncertain = error instanceof ChatProtocolError || isUncertainError(error)
        if (uncertain) {
          await updateOutbox(() => outbox.markNeedsReconciliation(
            normalizedActor,
            threadId,
            payload.message_request_id,
            errorCode || 'NETWORK_ERROR',
          ))
        } else {
          await updateOutbox(() => outbox.markCompleted(
            normalizedActor,
            threadId,
            payload.message_request_id,
          ))
        }
        if (uncertain || staleTask || terminalFailure) {
          const current = taskStates.value[threadId]
          setTask(threadId, {
            operation_id: current?.operation_id || payload.operation_id,
            task_status: terminalFailure ? 'failed' : 'unknown',
            task_revision: current?.task_revision ?? payload.task_revision,
            code: errorCode || 'CHAT_RESULT_UNKNOWN',
            local_status: terminalFailure
              ? 'terminal'
              : uncertain ? 'needs_reconciliation' : 'restore_required',
            can_reconcile: taskAction === 'confirm',
            recovery_message_request_id: payload.message_request_id,
          })
        }
        addAssistantMessage(threadId, displayError(error), { error: true })
        throw error
      } finally {
        ensureCoordination(normalizedActor).notifyStateChanged(threadId, payload.operation_id)
        sendingThreadId.value = ''
        activeAction.value = null
      }
    }

    async function sendMessage(content, userId) {
      const normalized = String(content ?? '').trim()
      if (!normalized) return undefined
      return execute(normalized, userId, 'message')
    }

    async function confirmTask(userId) {
      const normalizedActor = actorId(userId)
      const threadId = currentThreadId.value
      const task = taskStates.value[threadId]
      if (!isActionableConfirmation(task)) {
        throw new ChatProtocolError('当前确认卡片已失效，不能提交。')
      }
      const claimed = await ensureCoordination(normalizedActor)
        .claimConfirm(threadId, task.operation_id)
      if (!claimed) {
        setTask(threadId, {
          ...task,
          requires_confirmation: false,
          local_status: 'remote_confirming',
          code: 'CHAT_CROSS_TAB_CONFIRM_IN_PROGRESS',
        })
        throw new ChatProtocolError(
          '另一标签页正在确认该任务，请等待状态同步。',
          'CHAT_CROSS_TAB_CONFIRM_IN_PROGRESS',
        )
      }
      const latest = taskStates.value[threadId]
      if (latest?.operation_id !== task.operation_id
        || latest?.task_revision !== task.task_revision) {
        throw new ChatProtocolError('确认竞争期间任务已变化，已停止提交。')
      }
      setTask(threadId, {
        ...latest,
        requires_confirmation: true,
        local_status: 'active',
        code: task.code,
      })
      return execute(CONFIRM_MESSAGE, normalizedActor, 'confirm')
    }

    function cancelTask(userId) {
      return execute(CANCEL_MESSAGE, userId, 'cancel')
    }

    function reset() {
      releaseCoordination()
      sessions.value = []
      currentThreadId.value = ''
      messages.value = []
      taskStates.value = {}
      sendingThreadId.value = ''
      activeAction.value = null
    }

    return {
      sessions,
      currentThreadId,
      messages,
      loading,
      sending,
      currentTask,
      inputBlocked,
      activeAction,
      loadSessions,
      createSession,
      selectSession,
      deleteSession: deleteSessionById,
      loadMessages,
      sendMessage,
      confirmTask,
      cancelTask,
      restoreThread,
      reconcileOperation,
      releaseCoordination,
      reset,
    }
  })
}

export const useChatStore = createChatStoreDefinition()
