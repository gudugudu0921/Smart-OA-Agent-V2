import { formatDate } from './format.js'

function hasValue(value) {
  return value !== null && value !== undefined && String(value).trim() !== ''
}

export function buildLeaveResultView(item = {}) {
  const applicationId = hasValue(item.application_id)
    ? String(item.application_id)
    : (hasValue(item.id) ? String(item.id) : '')
  const leaveType = hasValue(item.leave_type) ? item.leave_type : ''
  const status = hasValue(item.status) ? item.status : ''
  const rows = []

  if (hasValue(item.start_date) && hasValue(item.end_date)) {
    rows.push({
      key: 'date',
      label: '日期',
      value: `${formatDate(item.start_date)} 至 ${formatDate(item.end_date)}`,
      kind: 'text',
    })
  }
  if (hasValue(item.leave_days)) {
    rows.push({
      key: 'leave_days',
      label: '天数',
      value: `${item.leave_days} 天`,
      kind: 'text',
    })
  }
  if (hasValue(item.manager_status)) {
    rows.push({
      key: 'manager_status',
      label: '主管',
      value: item.manager_status,
      kind: 'status',
    })
  }
  if (hasValue(item.hr_status)) {
    rows.push({
      key: 'hr_status',
      label: 'HR',
      value: item.hr_status,
      kind: 'status',
    })
  }

  return {
    applicationId,
    leaveType,
    status,
    rows,
    hasHeader: Boolean(leaveType || status),
    hasContent: Boolean(applicationId || leaveType || status || rows.length),
  }
}

export function selectChatResultData(intent, data) {
  if (!['manager_review', 'hr_review'].includes(intent) || data == null) return data
  const items = Array.isArray(data) ? data : [data]
  const hasDetails = items.some((item) => {
    const view = buildLeaveResultView(item)
    return view.hasHeader || view.rows.length > 0
  })
  return hasDetails ? data : null
}
