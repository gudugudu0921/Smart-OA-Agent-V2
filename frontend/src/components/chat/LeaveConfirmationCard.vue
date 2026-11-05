<script setup>
import { computed } from 'vue'
import {
  confirmationReason,
  isActionableConfirmation,
  leaveTypeLabel,
} from '../../utils/chatProtocol.js'

const props = defineProps({
  task: { type: Object, required: true },
  sending: Boolean,
  activeAction: String,
})
const emit = defineEmits(['confirm', 'cancel', 'reconcile'])

const actionable = computed(() => isActionableConfirmation(props.task))
const confirmation = computed(() => props.task.confirmation || {})
const isCancellation = computed(() => confirmation.value.operation_type === 'CANCEL_LEAVE')
const isManagerReview = computed(() => confirmation.value.operation_type === 'MANAGER_REVIEW')
const isHrReview = computed(() => confirmation.value.operation_type === 'HR_REVIEW')
const isApprovalReview = computed(() => isManagerReview.value || isHrReview.value)
const decisionLabel = computed(() => confirmation.value.decision === 'approved' ? '同意' : '拒绝')
const statusType = computed(() => {
  if (props.task.task_status === 'failed' || props.task.local_status === 'corrupt') return 'danger'
  if (props.task.local_status === 'verified_success') return 'success'
  if (['executing', 'unknown'].includes(props.task.task_status)) return 'warning'
  return 'info'
})
const statusText = computed(() => {
  if (props.task.local_status === 'outbox_unavailable') return '本地安全存储不可用，已停止发送。'
  if (props.task.local_status === 'corrupt') return '本地待核查记录损坏，已隔离且不会自动重发。'
  if (props.task.local_status === 'multiple_pending') return '该会话存在多笔待核查记录，已停止新的危险操作。'
  if (props.task.local_status === 'remote_confirming') return '另一标签页可能正在确认该任务，请等待并重新核查。'
  if (props.task.local_status === 'not_found') return '暂未查到该操作；这不代表业务一定未执行，不会自动重试。'
  if (props.task.local_status === 'lookup_failed') return '操作状态查询失败，原始操作ID与请求已保留。'
  if (props.task.local_status === 'operation_unknown') return '主库记录显示结果仍不确定，不会自动重复提交。'
  if (props.task.local_status === 'verified_failure') return '主库记录显示操作已失败，不会自动重新提交。'
  if (props.task.local_status === 'verified_success') {
    const action = props.task.operation_type === 'CANCEL_LEAVE'
      ? '撤销'
      : (props.task.operation_type === 'MANAGER_REVIEW'
        ? '完成主管审批'
        : (props.task.operation_type === 'HR_REVIEW' ? '完成 HR 终审' : '创建'))
    return `申请已确认${action}${props.task.application_id ? `（${props.task.application_id}）` : ''}。`
  }
  if (props.task.local_status === 'restore_required') return '发现本地未完成任务，服务端状态暂不可用；不会自动重发。'
  if (props.task.task_status === 'executing') return '申请正在处理，请勿重复确认。'
  if (props.task.task_status === 'unknown') return '结果尚未确认，已保留原操作ID等待核查。'
  if (props.task.task_status === 'failed') return '操作已明确失败，不会自动重新提交。'
  return ''
})
</script>

<template>
  <section v-if="actionable" class="confirmation-card" :aria-label="isHrReview ? 'HR 终审确认' : (isManagerReview ? '主管审批确认' : (isCancellation ? '撤销请假确认' : '请假确认'))">
    <header>
      <div><strong>{{ isHrReview ? '请确认 HR 终审' : (isManagerReview ? '请确认主管审批' : (isCancellation ? '请确认撤销请假' : '请确认请假信息')) }}</strong><span>任务版本 {{ task.task_revision }}</span></div>
      <el-tag type="warning" effect="plain">待确认</el-tag>
    </header>
    <dl>
      <div v-if="isCancellation || isApprovalReview" class="application"><dt>申请编号</dt><dd>{{ confirmation.application_id }}</dd></div>
      <div v-if="isApprovalReview"><dt>员工</dt><dd>{{ confirmation.employee_name }}（{{ confirmation.employee_no }}）</dd></div>
      <div><dt>请假类型</dt><dd>{{ leaveTypeLabel(confirmation.leave_type) }}</dd></div>
      <div><dt>开始日期</dt><dd>{{ confirmation.start_date }}</dd></div>
      <div><dt>结束日期</dt><dd>{{ confirmation.end_date }}</dd></div>
      <div v-if="!isCancellation && !isApprovalReview" class="reason"><dt>请假原因</dt><dd>{{ confirmationReason(confirmation.reason) }}</dd></div>
      <div v-if="isApprovalReview"><dt>{{ isHrReview ? '终审决定' : '审批决定' }}</dt><dd>{{ decisionLabel }}</dd></div>
      <div v-if="isApprovalReview" class="reason"><dt>{{ isHrReview ? '终审意见' : '审批意见' }}</dt><dd>{{ confirmationReason(confirmation.comment) }}</dd></div>
    </dl>
    <p class="notice">{{ isHrReview ? (confirmation.decision === 'approved' ? '确认后申请将进入批准终态。目标或决定如有误，请取消当前任务。' : '确认后申请将进入拒绝终态。目标或决定如有误，请取消当前任务。') : (isManagerReview ? (confirmation.decision === 'approved' ? '确认后申请将进入 HR 审批阶段。目标或决定如有误，请取消当前任务。' : '确认后申请将进入拒绝终态。目标或决定如有误，请取消当前任务。') : (isCancellation ? '确认后将撤销此申请。目标如有误，请取消当前任务。' : '确认后将以当前参数创建申请。参数如有变化，请先在聊天中修改并等待新的任务版本。')) }}</p>
    <footer>
      <el-button :disabled="sending" :loading="sending && activeAction === 'cancel'" @click="emit('cancel')">取消任务</el-button>
      <el-button type="primary" :disabled="sending || !actionable" :loading="sending && activeAction === 'confirm'" @click="emit('confirm')">{{ isApprovalReview ? `确认${decisionLabel}` : (isCancellation ? '确认撤销' : '确认提交') }}</el-button>
    </footer>
  </section>
  <el-alert
    v-else-if="statusText"
    class="task-status"
    :title="statusText"
    :type="statusType"
    show-icon
    :closable="false"
  >
    <template v-if="task.can_reconcile" #default>
      <el-button
        size="small"
        type="warning"
        plain
        :loading="task.reconciling"
        :disabled="sending || task.reconciling"
        @click="emit('reconcile')"
      >
        查询操作结果
      </el-button>
    </template>
  </el-alert>
</template>

<style scoped>
.confirmation-card { margin: 0 24px 14px; padding: 16px 18px; border: 1px solid #b9d7fb; border-radius: 10px; background: #f7fbff; box-shadow: 0 4px 14px rgba(40, 119, 213, .08); }
.confirmation-card header { display: flex; align-items: center; justify-content: space-between; }
.confirmation-card header strong, .confirmation-card header span { display: block; }
.confirmation-card header strong { color: #26364e; }
.confirmation-card header span { margin-top: 4px; color: #7b8798; font-size: 12px; }
dl { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 14px 0 10px; }
dl div { padding: 10px 12px; border-radius: 7px; background: #fff; }
dl .reason, dl .application { grid-column: 1 / -1; }
dt { color: #8a96a6; font-size: 12px; }
dd { margin: 5px 0 0; color: #34445a; font-weight: 600; overflow-wrap: anywhere; }
.notice { margin: 0; color: #6f7d90; font-size: 12px; line-height: 1.6; }
footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.task-status { margin: 0 24px 14px; }
@media (max-width: 900px) { dl { grid-template-columns: 1fr; } dl .reason, dl .application { grid-column: auto; } }
</style>
