const DB_NAME = 'smart_oa_chat_outbox_v1'
const DB_VERSION = 1
const STORE_NAME = 'requests'
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const ALLOWED_PAYLOAD_FIELDS = new Set([
  'thread_id',
  'message',
  'message_request_id',
  'operation_id',
  'task_action',
  'task_revision',
])

export const OUTBOX_STATUS = Object.freeze({
  SENDING: 'SENDING',
  COMPLETED: 'COMPLETED',
  NEEDS_RECONCILIATION: 'NEEDS_RECONCILIATION',
})

export class OutboxUnavailableError extends Error {
  constructor(message = '浏览器本地安全存储不可用。') {
    super(message)
    this.name = 'OutboxUnavailableError'
  }
}

export class InvalidOutboxRecordError extends Error {
  constructor(message = '本地待处理记录格式无效。') {
    super(message)
    this.name = 'InvalidOutboxRecordError'
  }
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

function scopePart(value, field) {
  const normalized = String(value ?? '').trim()
  if (!normalized) throw new TypeError(`${field} is required`)
  return normalized
}

function recordKey(userId, threadId, requestId) {
  return [
    scopePart(userId, 'userId'),
    scopePart(threadId, 'threadId'),
    scopePart(requestId, 'messageRequestId'),
  ].join(':')
}

function validUuid(value) {
  return typeof value === 'string' && UUID_PATTERN.test(value)
}

export function validateOutboxRecord(record, { userId, threadId } = {}) {
  if (!record || typeof record !== 'object' || !record.payload || typeof record.payload !== 'object') {
    throw new InvalidOutboxRecordError()
  }
  const expectedUser = scopePart(userId ?? record.user_id, 'userId')
  const expectedThread = scopePart(threadId ?? record.thread_id, 'threadId')
  if (record.user_id !== expectedUser || record.thread_id !== expectedThread) {
    throw new InvalidOutboxRecordError('本地待处理记录不属于当前用户或会话。')
  }
  if (!validUuid(record.thread_id)
    || !validUuid(record.message_request_id)
    || !validUuid(record.operation_id)) {
    throw new InvalidOutboxRecordError('本地待处理记录包含无效UUID。')
  }
  if (new Set([record.thread_id, record.message_request_id, record.operation_id]).size !== 3) {
    throw new InvalidOutboxRecordError('本地待处理记录的UUID必须彼此不同。')
  }
  if (!Object.values(OUTBOX_STATUS).includes(record.status)
    || !['message', 'confirm', 'cancel'].includes(record.task_action)
    || !Number.isInteger(record.task_revision)
    || record.task_revision < 0) {
    throw new InvalidOutboxRecordError('本地待处理记录状态或任务版本无效。')
  }
  const payloadKeys = Object.keys(record.payload)
  if (payloadKeys.some((key) => !ALLOWED_PAYLOAD_FIELDS.has(key))) {
    throw new InvalidOutboxRecordError('本地待处理请求包含未允许字段。')
  }
  const payload = record.payload
  if (payload.thread_id !== record.thread_id
    || payload.message_request_id !== record.message_request_id
    || payload.operation_id !== record.operation_id
    || payload.task_action !== record.task_action
    || payload.task_revision !== record.task_revision
    || typeof payload.message !== 'string'
    || !payload.message.trim()
    || payload.message.length > 2000) {
    throw new InvalidOutboxRecordError('本地待处理记录与原始请求不一致。')
  }
  if (record.key !== recordKey(record.user_id, record.thread_id, record.message_request_id)) {
    throw new InvalidOutboxRecordError('本地待处理记录主键不一致。')
  }
  return clone(record)
}

export function inspectOutboxRecords(records, scope) {
  const valid = []
  const invalid = []
  for (const record of records) {
    try {
      valid.push(validateOutboxRecord(record, scope))
    } catch (error) {
      invalid.push({
        key: typeof record?.key === 'string' ? record.key : null,
        reason: error instanceof InvalidOutboxRecordError ? error.message : '本地待处理记录无法读取。',
      })
    }
  }
  return { valid, invalid }
}

function buildRecord({ userId, threadId, payload, now }) {
  if (!payload || typeof payload !== 'object') throw new TypeError('payload is required')
  const messageRequestId = scopePart(payload.message_request_id, 'messageRequestId')
  const operationId = scopePart(payload.operation_id, 'operationId')
  const timestamp = now()
  return {
    key: recordKey(userId, threadId, messageRequestId),
    user_id: scopePart(userId, 'userId'),
    thread_id: scopePart(threadId, 'threadId'),
    message_request_id: messageRequestId,
    operation_id: operationId,
    task_action: payload.task_action || 'message',
    task_revision: payload.task_revision ?? null,
    payload: clone(payload),
    status: OUTBOX_STATUS.SENDING,
    error_code: null,
    created_at: timestamp,
    updated_at: timestamp,
  }
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new OutboxUnavailableError())
  })
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error || new OutboxUnavailableError())
    transaction.onabort = () => reject(transaction.error || new OutboxUnavailableError())
  })
}

export class IndexedDbChatOutbox {
  constructor({ indexedDBFactory = globalThis.indexedDB, now = () => new Date().toISOString() } = {}) {
    this.indexedDBFactory = indexedDBFactory
    this.now = now
    this.databasePromise = null
  }

  async open() {
    if (!this.indexedDBFactory) throw new OutboxUnavailableError()
    if (!this.databasePromise) {
      this.databasePromise = new Promise((resolve, reject) => {
        const request = this.indexedDBFactory.open(DB_NAME, DB_VERSION)
        request.onupgradeneeded = () => {
          const database = request.result
          if (!database.objectStoreNames.contains(STORE_NAME)) {
            database.createObjectStore(STORE_NAME, { keyPath: 'key' })
          }
        }
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error || new OutboxUnavailableError())
        request.onblocked = () => reject(new OutboxUnavailableError('本地安全存储升级被其他页面阻塞。'))
      }).catch((error) => {
        this.databasePromise = null
        throw error instanceof OutboxUnavailableError
          ? error
          : new OutboxUnavailableError()
      })
    }
    return this.databasePromise
  }

  async withStore(mode, operation) {
    try {
      const database = await this.open()
      const transaction = database.transaction(STORE_NAME, mode)
      const store = transaction.objectStore(STORE_NAME)
      const completed = transactionDone(transaction)
      const result = await operation(store)
      await completed
      return result
    } catch (error) {
      if (error instanceof OutboxUnavailableError) throw error
      throw new OutboxUnavailableError()
    }
  }

  async enqueue({ userId, threadId, payload }) {
    const record = buildRecord({ userId, threadId, payload, now: this.now })
    await this.withStore('readwrite', (store) => requestResult(store.add(record)))
    return clone(record)
  }

  async get(userId, threadId, messageRequestId) {
    const key = recordKey(userId, threadId, messageRequestId)
    const record = await this.withStore('readonly', (store) => requestResult(store.get(key)))
    return clone(record) || null
  }

  async listScope(userId, threadId) {
    const normalizedUser = scopePart(userId, 'userId')
    const normalizedThread = scopePart(threadId, 'threadId')
    const records = await this.withStore('readonly', (store) => requestResult(store.getAll()))
    return records
      .filter((item) => item.user_id === normalizedUser && item.thread_id === normalizedThread)
      .sort((left, right) => left.created_at.localeCompare(right.created_at))
      .map(clone)
  }

  async updateStatus(userId, threadId, messageRequestId, status, errorCode = null) {
    const key = recordKey(userId, threadId, messageRequestId)
    return this.withStore('readwrite', async (store) => {
      const record = await requestResult(store.get(key))
      if (!record) throw new OutboxUnavailableError('找不到待处理的本地消息记录。')
      record.status = status
      record.error_code = errorCode
      record.updated_at = this.now()
      await requestResult(store.put(record))
      return clone(record)
    })
  }

  markCompleted(userId, threadId, messageRequestId) {
    return this.updateStatus(userId, threadId, messageRequestId, OUTBOX_STATUS.COMPLETED)
  }

  markNeedsReconciliation(userId, threadId, messageRequestId, errorCode = null) {
    return this.updateStatus(
      userId,
      threadId,
      messageRequestId,
      OUTBOX_STATUS.NEEDS_RECONCILIATION,
      errorCode,
    )
  }

  async remove(userId, threadId, messageRequestId) {
    const key = recordKey(userId, threadId, messageRequestId)
    await this.withStore('readwrite', (store) => requestResult(store.delete(key)))
  }

  async removeOperation(userId, threadId, operationId) {
    const records = await this.listScope(userId, threadId)
    const matching = records.filter((item) => item.operation_id === String(operationId))
    await Promise.all(matching.map((item) => this.remove(userId, threadId, item.message_request_id)))
  }
}

export class MemoryChatOutbox {
  constructor({ now = () => new Date().toISOString(), beforeEnqueue = null } = {}) {
    this.now = now
    this.beforeEnqueue = beforeEnqueue
    this.records = new Map()
  }

  async enqueue({ userId, threadId, payload }) {
    if (this.beforeEnqueue) await this.beforeEnqueue({ userId, threadId, payload })
    const record = buildRecord({ userId, threadId, payload, now: this.now })
    if (this.records.has(record.key)) throw new OutboxUnavailableError('消息已存在于本地Outbox。')
    this.records.set(record.key, record)
    return clone(record)
  }

  async get(userId, threadId, messageRequestId) {
    return clone(this.records.get(recordKey(userId, threadId, messageRequestId))) || null
  }

  async listScope(userId, threadId) {
    const prefix = `${scopePart(userId, 'userId')}:${scopePart(threadId, 'threadId')}:`
    return [...this.records.entries()]
      .filter(([key]) => key.startsWith(prefix))
      .map(([, value]) => clone(value))
      .sort((left, right) => left.created_at.localeCompare(right.created_at))
  }

  async updateStatus(userId, threadId, messageRequestId, status, errorCode = null) {
    const key = recordKey(userId, threadId, messageRequestId)
    const record = this.records.get(key)
    if (!record) throw new OutboxUnavailableError('找不到待处理的本地消息记录。')
    record.status = status
    record.error_code = errorCode
    record.updated_at = this.now()
    return clone(record)
  }

  markCompleted(userId, threadId, messageRequestId) {
    return this.updateStatus(userId, threadId, messageRequestId, OUTBOX_STATUS.COMPLETED)
  }

  markNeedsReconciliation(userId, threadId, messageRequestId, errorCode = null) {
    return this.updateStatus(
      userId,
      threadId,
      messageRequestId,
      OUTBOX_STATUS.NEEDS_RECONCILIATION,
      errorCode,
    )
  }

  async remove(userId, threadId, messageRequestId) {
    this.records.delete(recordKey(userId, threadId, messageRequestId))
  }

  async removeOperation(userId, threadId, operationId) {
    const records = await this.listScope(userId, threadId)
    for (const record of records) {
      if (record.operation_id === String(operationId)) {
        await this.remove(userId, threadId, record.message_request_id)
      }
    }
  }
}

export const chatOutbox = new IndexedDbChatOutbox()
