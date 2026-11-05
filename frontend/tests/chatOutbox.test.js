import assert from 'node:assert/strict'
import test from 'node:test'

import {
  inspectOutboxRecords,
  MemoryChatOutbox,
  OUTBOX_STATUS,
} from '../src/services/chatOutbox.js'

const THREAD_A = '11111111-1111-4111-8111-111111111111'
const THREAD_B = '22222222-2222-4222-8222-222222222222'
const OPERATION = '33333333-3333-4333-8333-333333333333'

function payload(requestId) {
  return {
    thread_id: THREAD_A,
    message: '测试消息',
    message_request_id: requestId,
    operation_id: OPERATION,
    task_action: 'message',
    task_revision: 0,
  }
}

test('outbox stores the exact payload and status before transport', async () => {
  const outbox = new MemoryChatOutbox({ now: () => '2030-01-01T00:00:00.000Z' })
  const requestId = '44444444-4444-4444-8444-444444444444'
  await outbox.enqueue({ userId: 7, threadId: THREAD_A, payload: payload(requestId) })
  const stored = await outbox.get(7, THREAD_A, requestId)
  assert.deepEqual(stored.payload, payload(requestId))
  assert.equal(stored.status, OUTBOX_STATUS.SENDING)
  assert.equal(stored.user_id, '7')
  assert.equal(stored.thread_id, THREAD_A)
})

test('outbox isolates records by authenticated user and thread', async () => {
  const outbox = new MemoryChatOutbox()
  await outbox.enqueue({
    userId: 7,
    threadId: THREAD_A,
    payload: payload('44444444-4444-4444-8444-444444444444'),
  })
  await outbox.enqueue({
    userId: 8,
    threadId: THREAD_A,
    payload: payload('55555555-5555-4555-8555-555555555555'),
  })
  await outbox.enqueue({
    userId: 7,
    threadId: THREAD_B,
    payload: { ...payload('66666666-6666-4666-8666-666666666666'), thread_id: THREAD_B },
  })
  assert.equal((await outbox.listScope(7, THREAD_A)).length, 1)
  assert.equal((await outbox.listScope(8, THREAD_A)).length, 1)
  assert.equal((await outbox.listScope(7, THREAD_B)).length, 1)
})

test('uncertain records retain original ids and payload', async () => {
  const outbox = new MemoryChatOutbox()
  const requestId = '77777777-7777-4777-8777-777777777777'
  await outbox.enqueue({ userId: 7, threadId: THREAD_A, payload: payload(requestId) })
  await outbox.markNeedsReconciliation(7, THREAD_A, requestId, 'OPERATION_RESULT_UNKNOWN')
  const stored = await outbox.get(7, THREAD_A, requestId)
  assert.equal(stored.status, OUTBOX_STATUS.NEEDS_RECONCILIATION)
  assert.equal(stored.operation_id, OPERATION)
  assert.deepEqual(stored.payload, payload(requestId))
})

test('recovery rejects a record whose immutable payload was changed', async () => {
  const outbox = new MemoryChatOutbox()
  const requestId = '77777777-7777-4777-8777-777777777777'
  await outbox.enqueue({ userId: 7, threadId: THREAD_A, payload: payload(requestId) })
  const [record] = await outbox.listScope(7, THREAD_A)
  record.payload.operation_id = '88888888-8888-4888-8888-888888888888'

  const inspected = inspectOutboxRecords([record], { userId: 7, threadId: THREAD_A })

  assert.equal(inspected.valid.length, 0)
  assert.equal(inspected.invalid.length, 1)
})
