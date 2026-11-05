import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildLeaveResultView,
  selectChatResultData,
} from '../src/utils/leaveResult.js'

const APPLICATION_ID = '11111111-1111-4111-8111-111111111111'

function fullLeave(overrides = {}) {
  return {
    application_id: APPLICATION_ID,
    leave_type: 'annual',
    start_date: '2038-04-10',
    end_date: '2038-04-11',
    leave_days: 2,
    status: 'pending',
    manager_status: 'pending',
    hr_status: 'pending',
    ...overrides,
  }
}

test('manager review result renders real dates, days and approved status', () => {
  const view = buildLeaveResultView(fullLeave({ manager_status: 'approved' }))

  assert.equal(view.applicationId, APPLICATION_ID)
  assert.deepEqual(view.rows, [
    { key: 'date', label: '日期', value: '2038-04-10 至 2038-04-11', kind: 'text' },
    { key: 'leave_days', label: '天数', value: '2 天', kind: 'text' },
    { key: 'manager_status', label: '主管', value: 'approved', kind: 'status' },
    { key: 'hr_status', label: 'HR', value: 'pending', kind: 'status' },
  ])
})

test('HR review result preserves approved and rejected decision states', () => {
  const approved = buildLeaveResultView(fullLeave({
    status: 'approved',
    manager_status: 'approved',
    hr_status: 'approved',
  }))
  const rejected = buildLeaveResultView(fullLeave({
    status: 'rejected',
    manager_status: 'approved',
    hr_status: 'rejected',
  }))

  assert.equal(approved.status, 'approved')
  assert.equal(approved.rows.find((row) => row.key === 'hr_status').value, 'approved')
  assert.equal(rejected.status, 'rejected')
  assert.equal(rejected.rows.find((row) => row.key === 'hr_status').value, 'rejected')
})

test('partial result never renders an incomplete date range or empty values', () => {
  const view = buildLeaveResultView({
    application_id: APPLICATION_ID,
    leave_type: 'annual',
    start_date: '2038-04-10',
    end_date: undefined,
    leave_days: null,
    manager_status: 'approved',
    hr_status: '',
  })

  assert.deepEqual(view.rows, [
    { key: 'manager_status', label: '主管', value: 'approved', kind: 'status' },
  ])
  assert.equal(view.rows.some((row) => String(row.value).includes('undefined')), false)
})

test('application-id-only write result hides every unavailable detail row', () => {
  const view = buildLeaveResultView({ application_id: APPLICATION_ID })

  assert.equal(view.applicationId, APPLICATION_ID)
  assert.equal(view.hasContent, true)
  assert.equal(view.hasHeader, false)
  assert.deepEqual(view.rows, [])
})

test('empty or restored text-only message produces no empty result card', () => {
  assert.equal(buildLeaveResultView({}).hasContent, false)
  assert.equal(buildLeaveResultView().hasContent, false)
})

test('application-id-only approval matches restored text by omitting an empty card', () => {
  const managerData = { application_id: APPLICATION_ID }
  const hrData = { application_id: APPLICATION_ID }

  assert.equal(selectChatResultData('manager_review', managerData), null)
  assert.equal(selectChatResultData('hr_review', hrData), null)
  assert.equal(selectChatResultData('manager_review', null), null)
})

test('approval keeps real details while submit and cancel mappings remain untouched', () => {
  const managerData = fullLeave({ manager_status: 'approved' })
  const submitData = { application_id: APPLICATION_ID }
  const cancelData = { application_id: APPLICATION_ID }

  assert.equal(selectChatResultData('manager_review', managerData), managerData)
  assert.equal(selectChatResultData('submit_leave', submitData), submitData)
  assert.equal(selectChatResultData('cancel_leave', cancelData), cancelData)
})

test('submit and cancel compatible result shapes retain their available fields', () => {
  const submit = buildLeaveResultView(fullLeave())
  const cancel = buildLeaveResultView({
    application_id: APPLICATION_ID,
    leave_type: 'annual',
    status: 'cancelled',
  })

  assert.equal(submit.rows.find((row) => row.key === 'date').value, '2038-04-10 至 2038-04-11')
  assert.equal(submit.rows.find((row) => row.key === 'leave_days').value, '2 天')
  assert.equal(cancel.applicationId, APPLICATION_ID)
  assert.equal(cancel.status, 'cancelled')
  assert.deepEqual(cancel.rows, [])
})
