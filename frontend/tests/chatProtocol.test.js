import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildChatRequest,
  confirmationReason,
  isActionableConfirmation,
  isUuidV4,
  validateChatResponse,
  validateOperationStatusResponse,
  validateSessionTaskResponse,
} from '../src/utils/chatProtocol.js'

const THREAD_ID = '11111111-1111-4111-8111-111111111111'
const OPERATION_ID = '22222222-2222-4222-8222-222222222222'
const MESSAGE_ID = '33333333-3333-4333-8333-333333333333'

test('buildChatRequest creates a distinct UUID v4 message id', () => {
  const payload = buildChatRequest({
    threadId: THREAD_ID,
    message: ' 我要请假 ',
    operationId: OPERATION_ID,
    taskRevision: 0,
    uuidFactory: () => MESSAGE_ID,
  })
  assert.equal(payload.message, '我要请假')
  assert.equal(payload.message_request_id, MESSAGE_ID)
  assert.equal(payload.operation_id, OPERATION_ID)
  assert.ok(isUuidV4(payload.message_request_id))
  assert.notEqual(payload.message_request_id, payload.operation_id)
  assert.notEqual(payload.message_request_id, payload.thread_id)
})

test('structured confirmation is actionable only when revision matches', () => {
  const task = {
    operation_id: OPERATION_ID,
    task_status: 'awaiting_confirmation',
    task_revision: 2,
    requires_confirmation: true,
    confirmation: {
      leave_type: 'annual',
      start_date: '2030-01-01',
      end_date: '2030-01-02',
      reason: null,
      task_revision: 2,
    },
  }
  assert.equal(isActionableConfirmation(task), true)
  assert.equal(isActionableConfirmation({ ...task, task_revision: 3 }), false)
  assert.equal(confirmationReason(task.confirmation.reason), '未填写')
})

test('cancel confirmation is actionable only for one validated pending target', () => {
  const task = {
    operation_id: OPERATION_ID,
    task_status: 'awaiting_confirmation',
    task_revision: 1,
    requires_confirmation: true,
    confirmation: {
      operation_type: 'CANCEL_LEAVE',
      application_id: MESSAGE_ID,
      leave_type: 'annual',
      start_date: '2030-01-01',
      end_date: '2030-01-02',
      status: 'pending',
      task_revision: 1,
    },
  }
  assert.equal(isActionableConfirmation(task), true)
  assert.equal(isActionableConfirmation({
    ...task,
    confirmation: { ...task.confirmation, application_id: 'not-a-uuid' },
  }), false)
  assert.equal(isActionableConfirmation({
    ...task,
    confirmation: { ...task.confirmation, status: 'approved' },
  }), false)
})

test('manager review confirmation binds employee target decision and comment', () => {
  const task = {
    operation_id: OPERATION_ID,
    task_status: 'awaiting_confirmation',
    task_revision: 3,
    requires_confirmation: true,
    confirmation: {
      operation_type: 'MANAGER_REVIEW',
      application_id: MESSAGE_ID,
      employee_name: 'Review Target',
      employee_no: 'TARGET091',
      leave_type: 'annual',
      start_date: '2030-01-01',
      end_date: '2030-01-02',
      status: 'pending',
      decision: 'approved',
      comment: null,
      task_revision: 3,
    },
  }
  assert.equal(isActionableConfirmation(task), true)
  assert.equal(isActionableConfirmation({
    ...task,
    confirmation: { ...task.confirmation, decision: 'approve' },
  }), false)
  assert.equal(isActionableConfirmation({
    ...task,
    confirmation: { ...task.confirmation, employee_name: '' },
  }), false)
})

test('HR review confirmation binds final decision without changing the protocol shape', () => {
  const task = {
    operation_id: OPERATION_ID,
    task_status: 'awaiting_confirmation',
    task_revision: 4,
    requires_confirmation: true,
    confirmation: {
      operation_type: 'HR_REVIEW',
      application_id: MESSAGE_ID,
      employee_name: 'Review Target',
      employee_no: 'TARGET091',
      leave_type: 'annual',
      start_date: '2030-01-01',
      end_date: '2030-01-02',
      status: 'pending',
      decision: 'rejected',
      comment: 'policy mismatch',
      task_revision: 4,
    },
  }
  assert.equal(isActionableConfirmation(task), true)
  assert.equal(isActionableConfirmation({
    ...task,
    confirmation: { ...task.confirmation, status: 'approved' },
  }), false)
})

test('response operation id mismatch is rejected before state activation', () => {
  const payload = {
    thread_id: THREAD_ID,
    message_request_id: MESSAGE_ID,
    operation_id: OPERATION_ID,
  }
  assert.throws(
    () => validateChatResponse({
      thread_id: THREAD_ID,
      message_request_id: MESSAGE_ID,
      operation_id: '44444444-4444-4444-8444-444444444444',
      task_status: 'collecting',
    }, payload),
    /业务操作ID不匹配/,
  )
})

test('session task recovery accepts only a structured matching confirmation', () => {
  const task = {
    thread_id: THREAD_ID,
    operation_id: OPERATION_ID,
    task_status: 'awaiting_confirmation',
    task_revision: 2,
    requires_confirmation: true,
    confirmation: {
      leave_type: 'annual',
      start_date: '2030-01-01',
      end_date: '2030-01-02',
      reason: null,
      task_revision: 2,
    },
    operation_status: null,
    code: 'CHAT_CONFIRMATION_REQUIRED',
  }
  assert.equal(validateSessionTaskResponse(task, THREAD_ID), task)
  assert.throws(
    () => validateSessionTaskResponse({
      ...task,
      confirmation: { ...task.confirmation, task_revision: 1 },
    }, THREAD_ID),
    /确认摘要与任务版本不一致/,
  )
  assert.throws(
    () => validateSessionTaskResponse({ ...task, operation_status: 'SUCCEEDED' }, THREAD_ID),
    /不得声明MySQL业务结果/,
  )
})

test('operation success requires a matching MySQL resource and replay payload', () => {
  const response = {
    operation_id: OPERATION_ID,
    operation_type: 'SUBMIT_LEAVE',
    status: 'SUCCEEDED',
    resource_id: MESSAGE_ID,
    result_payload: { application_id: MESSAGE_ID },
    error_code: null,
  }
  assert.equal(validateOperationStatusResponse(response, OPERATION_ID), response)
  assert.equal(
    validateOperationStatusResponse(
      { ...response, operation_type: 'CANCEL_LEAVE' },
      OPERATION_ID,
    ).operation_type,
    'CANCEL_LEAVE',
  )
  assert.equal(
    validateOperationStatusResponse(
      { ...response, operation_type: 'MANAGER_REVIEW' },
      OPERATION_ID,
    ).operation_type,
    'MANAGER_REVIEW',
  )
  assert.equal(
    validateOperationStatusResponse(
      { ...response, operation_type: 'HR_REVIEW' },
      OPERATION_ID,
    ).operation_type,
    'HR_REVIEW',
  )
  assert.throws(
    () => validateOperationStatusResponse({
      ...response,
      result_payload: { application_id: '44444444-4444-4444-8444-444444444444' },
    }, OPERATION_ID),
    /缺少可验证的业务资源/,
  )
})
