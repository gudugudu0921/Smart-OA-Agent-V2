import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'

import { MemoryChatOutbox, OutboxUnavailableError, OUTBOX_STATUS } from '../src/services/chatOutbox.js'
import { createChatStoreDefinition } from '../src/stores/chat.js'

const THREAD_ID = '11111111-1111-4111-8111-111111111111'
const OPERATION_ONE = '22222222-2222-4222-8222-222222222222'
const MESSAGE_ONE = '33333333-3333-4333-8333-333333333333'
const MESSAGE_TWO = '44444444-4444-4444-8444-444444444444'
const MESSAGE_THREE = '55555555-5555-4555-8555-555555555555'
const OPERATION_TWO = '66666666-6666-4666-8666-666666666666'

let storeNumber = 0

function uuidSequence(...values) {
  const queue = [...values]
  return () => {
    if (!queue.length) throw new Error('UUID queue exhausted')
    return queue.shift()
  }
}

function createApi(sendChat, overrides = {}) {
  return {
    sendChat,
    listSessions: async () => ({ data: { sessions: [] } }),
    createSession: async () => ({ data: { thread_id: THREAD_ID } }),
    getSession: async () => ({ data: { thread_id: THREAD_ID } }),
    getSessionMessages: async () => ({ data: { thread_id: THREAD_ID, messages: [] } }),
    getSessionTask: async () => ({
      data: {
        thread_id: THREAD_ID,
        operation_id: null,
        task_status: 'idle',
        task_revision: 0,
        requires_confirmation: false,
        confirmation: null,
        operation_status: null,
        code: 'CHAT_TASK_IDLE',
      },
    }),
    getOperation: async () => { throw new Error('operation lookup not expected') },
    deleteSession: async () => ({ data: null }),
    ...overrides,
  }
}

function createStore({ sendChat, outbox = new MemoryChatOutbox(), uuids, api = {}, coordinationFactory = null }) {
  setActivePinia(createPinia())
  storeNumber += 1
  const useStore = createChatStoreDefinition({
    storeId: `chat-test-${storeNumber}`,
    api: createApi(sendChat, api),
    outbox,
    uuidFactory: uuidSequence(...uuids),
    coordinationFactory: coordinationFactory || (() => ({
      open() {},
      close() {},
      claimConfirm: async () => true,
      notifyStateChanged() {},
    })),
  })
  const store = useStore()
  store.currentThreadId = THREAD_ID
  return { store, outbox }
}

function ordinaryResponse(payload, intent = 'general_chat') {
  return {
    data: {
      success: true,
      thread_id: THREAD_ID,
      intent,
      response: '正常回复',
      data: intent === 'query_my_leave' ? [] : null,
      message_request_id: payload.message_request_id,
      replayed: false,
    },
  }
}

function awaitingResponse(payload, revision = 0) {
  return {
    data: {
      success: false,
      thread_id: THREAD_ID,
      intent: 'submit_leave',
      response: '请确认请假信息',
      message_request_id: payload.message_request_id,
      operation_id: payload.operation_id,
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
      code: 'CHAT_CONFIRMATION_REQUIRED',
      replayed: false,
    },
  }
}

test('candidate and exact request are persisted before Axios is called', async () => {
  const outbox = new MemoryChatOutbox()
  let captured
  const { store } = createStore({
    outbox,
    uuids: [OPERATION_ONE, MESSAGE_ONE],
    sendChat: async (payload) => {
      captured = payload
      assert.ok(await outbox.get(7, THREAD_ID, payload.message_request_id))
      return ordinaryResponse(payload)
    },
  })
  await store.sendMessage('你好', 7)
  assert.equal(captured.operation_id, OPERATION_ONE)
  assert.equal(captured.message_request_id, MESSAGE_ONE)
  assert.equal(store.currentTask.task_status, 'idle')
  assert.equal((await outbox.listScope(7, THREAD_ID)).length, 0)
})

test('ordinary query does not activate the candidate operation', async () => {
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE],
    sendChat: async (payload) => ordinaryResponse(payload, 'query_my_leave'),
  })
  await store.sendMessage('查询我的请假记录', 7)
  assert.equal(store.currentTask.operation_id, null)
  assert.equal(store.currentTask.requires_confirmation, false)
})

for (const scenario of [
  {
    intent: 'manager_review',
    response: '请假申请已通过主管审批，现等待 HR 审批。',
  },
  {
    intent: 'hr_review',
    response: '请假申请已通过 HR 终审。',
  },
]) {
  test(`${scenario.intent} application-id-only success keeps text and omits empty card data`, async () => {
    const { store } = createStore({
      uuids: [OPERATION_ONE, MESSAGE_ONE],
      sendChat: async (payload) => ({
        data: {
          success: true,
          thread_id: THREAD_ID,
          intent: scenario.intent,
          response: scenario.response,
          data: { application_id: '77777777-7777-4777-8777-777777777777' },
          message_request_id: payload.message_request_id,
          replayed: false,
        },
      }),
    })

    await store.sendMessage('审批请假申请', 7)

    const assistantMessage = store.messages.at(-1)
    assert.equal(assistantMessage.content, scenario.response)
    assert.equal(assistantMessage.data, null)
    assert.equal(store.currentTask.task_status, 'idle')
  })
}

test('multiturn submit reuses operation id and advances message id and revision', async () => {
  const sent = []
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE, MESSAGE_TWO],
    sendChat: async (payload) => {
      sent.push(payload)
      if (sent.length === 1) {
        return {
          data: {
            success: false,
            thread_id: THREAD_ID,
            intent: 'submit_leave',
            response: '请补充日期',
            message_request_id: payload.message_request_id,
            operation_id: payload.operation_id,
            task_status: 'collecting',
            task_revision: 0,
            requires_confirmation: false,
          },
        }
      }
      return awaitingResponse(payload, 1)
    },
  })
  await store.sendMessage('我要请年假', 7)
  await store.sendMessage('2030年1月1日到2日', 7)
  assert.equal(sent[0].operation_id, sent[1].operation_id)
  assert.notEqual(sent[0].message_request_id, sent[1].message_request_id)
  assert.equal(sent[1].task_revision, 0)
  assert.equal(store.currentTask.task_revision, 1)
  assert.equal(store.currentTask.confirmation.reason, null)
})

test('confirm uses structured action, current operation and revision', async () => {
  const sent = []
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE, MESSAGE_TWO],
    sendChat: async (payload) => {
      sent.push(payload)
      if (payload.task_action === 'message') return awaitingResponse(payload, 2)
      return {
        data: {
          success: true,
          thread_id: THREAD_ID,
          intent: 'submit_leave',
          response: '申请已创建',
          data: { application_id: '77777777-7777-4777-8777-777777777777' },
          message_request_id: payload.message_request_id,
          operation_id: payload.operation_id,
          task_status: 'succeeded',
          task_revision: 2,
          requires_confirmation: false,
          operation_status: 'SUCCEEDED',
          code: 'CHAT_OPERATION_SUCCEEDED',
        },
      }
    },
  })
  await store.sendMessage('完整请假参数', 7)
  await store.confirmTask(7)
  assert.equal(sent[1].task_action, 'confirm')
  assert.equal(sent[1].operation_id, OPERATION_ONE)
  assert.equal(sent[1].task_revision, 2)
  assert.equal(sent[1].message_request_id, MESSAGE_TWO)
  assert.equal(store.currentTask.task_status, 'idle')
})

test('cancel clears task only after a successful backend response', async () => {
  let taskWasStillActiveDuringRequest = false
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE, MESSAGE_TWO],
    sendChat: async (payload) => {
      if (payload.task_action === 'message') return awaitingResponse(payload, 1)
      taskWasStillActiveDuringRequest = store.currentTask.task_status === 'awaiting_confirmation'
      return {
        data: {
          success: true,
          thread_id: THREAD_ID,
          intent: 'general_chat',
          response: '任务已取消',
          message_request_id: payload.message_request_id,
          operation_id: payload.operation_id,
          task_status: 'cancelled',
          task_revision: 1,
          code: 'CHAT_TASK_CANCELLED',
        },
      }
    },
  })
  await store.sendMessage('完整请假参数', 7)
  await store.cancelTask(7)
  assert.equal(taskWasStillActiveDuringRequest, true)
  assert.equal(store.currentTask.task_status, 'idle')
})

test('network timeout retains original ids and never resends automatically', async () => {
  let calls = 0
  const { store, outbox } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE],
    sendChat: async () => {
      calls += 1
      throw new Error('timeout')
    },
  })
  await assert.rejects(store.sendMessage('我要请假', 7), /timeout/)
  assert.equal(calls, 1)
  const records = await outbox.listScope(7, THREAD_ID)
  assert.equal(records[0].status, OUTBOX_STATUS.NEEDS_RECONCILIATION)
  assert.equal(records[0].operation_id, OPERATION_ONE)
  assert.equal(records[0].message_request_id, MESSAGE_ONE)
  assert.equal(store.currentTask.task_status, 'unknown')
})

test('outbox failure prevents unsafe HTTP send', async () => {
  let calls = 0
  const outbox = new MemoryChatOutbox({
    beforeEnqueue: async () => { throw new OutboxUnavailableError() },
  })
  const { store } = createStore({
    outbox,
    uuids: [OPERATION_ONE, MESSAGE_ONE],
    sendChat: async () => { calls += 1 },
  })
  await assert.rejects(store.sendMessage('我要请假', 7), OutboxUnavailableError)
  assert.equal(calls, 0)
})

test('operation mismatch invalidates confirmation and blocks a second call', async () => {
  let calls = 0
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE],
    sendChat: async (payload) => {
      calls += 1
      const response = awaitingResponse(payload, 0)
      response.data.operation_id = OPERATION_TWO
      return response
    },
  })
  await assert.rejects(store.sendMessage('完整请假参数', 7), /业务操作ID不匹配/)
  await assert.rejects(store.confirmTask(7), /确认卡片已失效/)
  assert.equal(calls, 1)
})

test('server revision conflict invalidates a previously actionable card', async () => {
  let calls = 0
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE, MESSAGE_TWO],
    sendChat: async (payload) => {
      calls += 1
      if (calls === 1) return awaitingResponse(payload, 2)
      const error = new Error('revision conflict')
      error.response = {
        status: 409,
        data: { code: 'CHAT_TASK_REVISION_CONFLICT', message: '任务版本已变化' },
      }
      throw error
    },
  })
  await store.sendMessage('完整请假参数', 7)
  await assert.rejects(store.sendMessage('修改请假原因', 7), /revision conflict/)
  assert.equal(store.currentTask.task_status, 'unknown')
  assert.equal(store.currentTask.confirmation, null)
  await assert.rejects(store.confirmTask(7), /确认卡片已失效/)
  assert.equal(calls, 2)
})

test('definitive operation failure removes the actionable confirmation', async () => {
  let calls = 0
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE, MESSAGE_TWO],
    sendChat: async (payload) => {
      calls += 1
      if (calls === 1) return awaitingResponse(payload, 1)
      const error = new Error('operation failed')
      error.response = {
        status: 409,
        data: { code: 'OPERATION_FAILED', message: '操作已明确失败' },
      }
      throw error
    },
  })
  await store.sendMessage('完整请假参数', 7)
  await assert.rejects(store.confirmTask(7), /operation failed/)
  assert.equal(store.currentTask.task_status, 'failed')
  assert.equal(store.currentTask.confirmation, null)
  await assert.rejects(store.confirmTask(7), /确认卡片已失效/)
  assert.equal(calls, 2)
})

test('new independent task receives a new operation id after success', async () => {
  const sent = []
  const { store } = createStore({
    uuids: [OPERATION_ONE, MESSAGE_ONE, MESSAGE_TWO, OPERATION_TWO, MESSAGE_THREE],
    sendChat: async (payload) => {
      sent.push(payload)
      if (payload.task_action === 'message') return awaitingResponse(payload, 0)
      return {
        data: {
          success: true,
          thread_id: THREAD_ID,
          intent: 'submit_leave',
          response: '申请已创建',
          data: { application_id: '77777777-7777-4777-8777-777777777777' },
          message_request_id: payload.message_request_id,
          operation_id: payload.operation_id,
          task_status: 'succeeded',
          task_revision: 0,
          operation_status: 'SUCCEEDED',
        },
      }
    },
  })
  await store.sendMessage('第一笔请假', 7)
  await store.confirmTask(7)
  await store.sendMessage('第二笔请假', 7)
  assert.equal(sent[0].operation_id, OPERATION_ONE)
  assert.equal(sent[2].operation_id, OPERATION_TWO)
})

test('history loading remains compatible and local records are scoped', async () => {
  const outbox = new MemoryChatOutbox()
  await outbox.enqueue({
    userId: 8,
    threadId: THREAD_ID,
    payload: {
      thread_id: THREAD_ID,
      message: '别的用户',
      message_request_id: MESSAGE_ONE,
      operation_id: OPERATION_ONE,
      task_action: 'message',
      task_revision: 0,
    },
  })
  const { store } = createStore({
    outbox,
    uuids: [],
    sendChat: async () => { throw new Error('not called') },
  })
  await store.selectSession(THREAD_ID, 7)
  assert.deepEqual(store.messages, [])
  assert.equal(store.currentTask.task_status, 'idle')
})
