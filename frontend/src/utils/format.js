import dayjs from 'dayjs'

export const leaveTypeLabels = {
  annual: '年假', sick: '病假', personal: '事假', marriage: '婚假',
  maternity: '产假', bereavement: '丧假', other: '其他',
}

export const statusLabels = {
  pending: '待处理', approved: '已批准', rejected: '已拒绝', cancelled: '已撤销',
}

export const roleLabels = { employee: '员工', manager: '主管', hr: 'HR', admin: '管理员' }

export const statusTagType = (status) => ({
  pending: 'warning', approved: 'success', rejected: 'danger', cancelled: 'info',
}[status] || 'info')

export const formatDateTime = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—')
export const formatDate = (value) => (value ? dayjs(value).format('YYYY-MM-DD') : '—')
