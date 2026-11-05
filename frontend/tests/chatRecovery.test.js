import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'

import { MemoryChatOutbox, OUTBOX_STATUS } from '../src/services/chatOutbox.js'
import { createChatStoreDefinition } from '../src/stores/chat.js'

const THREAD = '11111111-1111-4111-8111-111111111111'
const OPERATION = '22222222-2222-4222-8222-222222222222'
const MESSAGE = '33333333-3333-4333-8333-333333333333'
const CONFIRM = '44444444-4444-4444-8444-444444444444'
const APPLICATION = '55555555-5555-4555-8555-555555555555'

let serial = 0

function requestPayload({ action = 'message', requestId = MESSAGE, revision = 0 } = {}) {
  return {
    thread_id: THREAD,
    message: action === 'confirm' ? '确认提交当前请假申请' : '我要请假',
    message_request_id: requestId,
    operation_id: OPERATION,
    task_action: action,
    task_revision: revision,
  }
}

function idleTask() {
  return {
    thread_id: THREAD,
    operation_id: null,
    task_status: 'idle',
    task_revision: 0,
    requires_confirmation: false,
    confirmation: null,
    operation_status: null,
    code: 'CHAT_TASK_IDLE',
  }
}

function awaitingTask(revision = 2) {
  return {
    thread_id: THREAD,
    operation_id: OPERATION,
    task_status: 'awaiting_confirmation',
    task_revision: revision,
    requires_confirmation: true,
    confirmation: {
      leave_type: 'annual',
      start_date: '2030-01-01',
      end_date: '2030-01-02',
      reason: null,
      task_revision: revision,
    },
    operation_status: null,
    code: 'CHAT_CONFIRMATION_REQUIRED',
  }
}

function operation(status, overrides = {}) {
  return {
    operation_id: OPERATION,
    operation_type: 'SUBMIT_LEAVE',
    status,
    resource_id: status === 'SUCCEEDED' ? APPLICATION : null,
    result_payload: status === 'SUCCEEDED' ? { application_id: APPLICATION } : null,
    error_code: status === 'FAILED' ? 'LEAVE_DATE_CONFLICT' : null,
    ...overrides,
  }
}

function noCoordination() {
  return {
    open() {},
    close() {},
    claimConfirm: async () => true,
    notifyStateChanged() {},
  }
}

function makeStore({ outbox = new MemoryChatOutbox(), task = idleTask(), getOperation, sendChat } = {}) {
  let postCalls = 0
  let operationCalls = 0
  const api = {
    sendChat: async (...args) => {
      postCalls += 1
      if (sendChat) return sendChat(...args)
      throw new Error('POST /chat must not run during recovery')
    },
    listSessions: async () => ({ data: { sessions: [] } }),
    createSession: async () => ({ data: { thread_id: THREAD } }),
    getSession: async () => ({ data: { thread_id: THREAD } }),
    getSessionMessages: async () => ({ data: { thread_id: THREAD, messages: [] } }),
    getSessionTask: async () => ({ data: task }),
    getOperation: async (operationId) => {
      operationCalls += 1
      if (!getOperation) throw new Error('GET operation not expected')
      return getOperation(operationId)
    },
    deleteSession: async () => ({ data: null }),
  }
  setActivePinia(createPinia())
  serial += 1
  const useStore = createChatStoreDefinition({
    storeId: `chat-recovery-${serial}`,
    api,
    outbox,
    coordinationFactory: noCoordination,
  })
  const store = useStore()
  store.currentThreadId = THREAD
  return {
    store,
    outbox,
    calls: {
      get post() { return postCalls },
      get operation() { return operationCalls },
    },
  }
}

async function enqueue(outbox, userId, payload, status = OUTBOX_STATUS.SENDING) {
  await outbox.enqueue({ userId, threadId: THREAD, payload })
  if (status === OUTBOX_STATUS.COMPLETED) {
    await outbox.markCompleted(userId, THREAD, payload.message_request_id)
  } else if (status === OUTBOX_STATUS.NEEDS_RECONCILIATION) {
    await outbox.markNeedsReconciliation(userId, THREAD, payload.message_request_id, 'NETWORK_ERROR')
  }
}

test('refresh preserves immutable SENDING request as unknown and never POSTs it', async () => {
  const outbox = new MemoryChatOutbox()
  const original = requestPayload()
  await enqueue(outbox, 7, original)
  const { store, calls } = makeStore({ outbox })

  await store.restoreThread(7, THREAD)

  const [record] = await outbox.listScope(7, THREAD)
  assert.equal(calls.post, 0)
  assert.equal(record.status, OUTBOX_STATUS.NEEDS_RECONCILIATION)
  assert.deepEqual(record.payload, original)
  assert.equal(record.message_request_id, MESSAGE)
  assert.equal(record.operation_id, OPERATION)
  assert.equal(store.currentTask.task_status, 'unknown')
  assert.equal(store.inputBlocked, true)
})

test('trusted Redis awaiting task restores structured confirmation from the server', async () => {
  const outbox = new MemoryChatOutbox()
  await enqueue(outbox, 7, requestPayload(), OUTBOX_STATUS.COMPLETED)
  const { store, calls } = makeStore({ outbox, task: awaitingTask(2) })

  await store.restoreThread(7, THREAD)

  assert.equal(calls.post, 0)
  assert.equal(calls.operation, 0)
  assert.equal(store.currentTask.task_status, 'awaiting_confirmation')
  assert.equal(store.currentTask.requires_confirmation, true)
  assert.equal(store.currentTask.confirmation.task_revision, 2)
  assert.equal(store.currentTask.confirmation.reason, null)
})

test('unknown local confirm suppresses confirmation and checks MySQL before any retry', async () => {
  const outbox = new MemoryChatOutbox()
  await enqueue(outbox, 7, requestPayload(), OUTBOX_STATUS.COMPLETED)
  await enqueue(outbox, 7, requestPayload({ action: 'confirm', requestId: CONFIRM, revision: 2 }))
  const { store, calls } = makeStore({
    outbox,
    task: awaitingTask(2),
    getOperation: async () => ({ data: operation('PROCESSING') }),
  })

  await store.restoreThread(7, THREAD)

  assert.equal(calls.post, 0)
  assert.equal(calls.operation, 1)
  assert.equal(store.currentTask.task_status, 'executing')
  assert.equal(store.currentTask.operation_status, 'PROCESSING')
  assert.equal(store.currentTask.requires_confirmation, false)
  assert.equal(store.currentTask.confirmation, null)
  assert.equal(store.inputBlocked, true)
})

test('SUCCEEDED reconciliation restores application id and clears only its operation records', async () => {
  const outbox = new MemoryChatOutbox()
  await enqueue(outbox, 7, requestPayload({ action: 'confirm', requestId: CONFIRM, revision: 2 }))
  const { store, calls } = makeStore({
    outbox,
    getOperation: async () => ({ data: operation('SUCCEEDED') }),
  })

  await store.restoreThread(7, THREAD)

  assert.equal(calls.post, 0)
  assert.equal(store.currentTask.local_status, 'verified_success')
  assert.equal(store.currentTask.application_id, APPLICATION)
  assert.equal(store.currentTask.operation_status, 'SUCCEEDED')
  assert.equal((await outbox.listScope(7, THREAD)).length, 0)
})

for (const scenario of [
  { name: 'PROCESSING', response: operation('PROCESSING'), status: 'processing', retained: OUTBOX_STATUS.NEEDS_RECONCILIATION },
  { name: 'UNKNOWN', response: operation('UNKNOWN'), status: 'operation_unknown', retained: OUTBOX_STATUS.NEEDS_RECONCILIATION },
  { name: 'FAILED', response: operation('FAILED'), status: 'verified_failure', retained: OUTBOX_STATUS.COMPLETED },
]) {
  test(`GET ${scenario.name} maps conservatively without a write retry`, async () => {
    const outbox = new MemoryChatOutbox()
    await enqueue(outbox, 7, requestPayload({ action: 'confirm', requestId: CONFIRM, revision: 2 }))
    const { store, calls } = makeStore({
      outbox,
      getOperation: async () => ({ data: scenario.response }),
    })

    await store.restoreThread(7, THREAD)

    assert.equal(calls.post, 0)
    assert.equal(store.currentTask.local_status, scenario.status)
    assert.equal((await outbox.listScope(7, THREAD))[0].status, scenario.retained)
  })
}

for (const scenario of [
  { name: '404', status: 404, code: 'OPERATION_NOT_FOUND', local: 'not_found' },
  { name: '5xx', status: 503, code: 'SERVICE_UNAVAILABLE', local: 'lookup_failed' },
]) {
  test(`GET ${scenario.name} preserves Outbox and never treats lookup failure as safe retry`, async () => {
    const outbox = new MemoryChatOutbox()
    await enqueue(outbox, 7, requestPayload({ action: 'confirm', requestId: CONFIRM, revision: 2 }))
    const error = new Error(scenario.name)
    error.response = { status: scenario.status, data: { code: scenario.code } }
    const { store, calls } = makeStore({
      outbox,
      getOperation: async () => { throw error },
    })

    await store.restoreThread(7, THREAD)

    assert.equal(calls.post, 0)
    assert.equal(store.currentTask.local_status, scenario.local)
    assert.equal(store.currentTask.can_reconcile, true)
    assert.equal((await outbox.listScope(7, THREAD))[0].status, OUTBOX_STATUS.NEEDS_RECONCILIATION)
  })
}

test('Outbox recovery is isolated by authenticated actor and thread', async () => {
  const outbox = new MemoryChatOutbox()
  await enqueue(outbox, 8, requestPayload({ action: 'confirm', requestId: CONFIRM }))
  const { store, calls } = makeStore({ outbox })

  await store.restoreThread(7, THREAD)

  assert.equal(store.currentTask.task_status, 'idle')
  assert.equal(store.currentTask.operation_id, null)
  assert.equal(calls.operation, 0)
  assert.equal((await outbox.listScope(8, THREAD)).length, 1)
})

test('corrupt local record is isolated without calling Chat or operation endpoints', async () => {
  const outbox = new MemoryChatOutbox()
  await enqueue(outbox, 7, requestPayload())
  const key = [...outbox.records.keys()][0]
  outbox.records.get(key).payload.operation_id = APPLICATION
  const { store, calls } = makeStore({ outbox })

  await store.restoreThread(7, THREAD)

  assert.equal(store.currentTask.local_status, 'corrupt')
  assert.equal(store.currentTask.operation_id, null)
  assert.equal(calls.post, 0)
  assert.equal(calls.operation, 0)
})
