<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import LeaveTable from '../components/leave/LeaveTable.vue'
import LeaveFormDialog from '../components/leave/LeaveFormDialog.vue'
import { cancelLeave, getMyLeaves } from '../api/leave'
import { useNotificationStore } from '../stores/notification'

const rows = ref([])
const loading = ref(false)
const cancellingId = ref('')
const formVisible = ref(false)
const notification = useNotificationStore()

async function load() {
  loading.value = true
  try { rows.value = (await getMyLeaves()).data } finally { loading.value = false }
}

async function cancel(row) {
  try {
    await ElMessageBox.confirm('确定撤销这条尚未审批的请假申请吗？', '撤销申请', { type: 'warning' })
  } catch {
    return
  }
  cancellingId.value = row.id
  try {
    await cancelLeave(row.id)
    ElMessage.success('申请已撤销')
    await load()
  } finally { cancellingId.value = '' }
}

async function onFormSuccess() {
  await Promise.all([load(), notification.refreshSummary()])
}

onMounted(load)
</script>

<template>
  <div class="page-card">
    <div class="page-heading"><div><h2>我的请假</h2><p>查看申请进度，或使用传统表单提交请假。</p></div><el-button type="primary" @click="formVisible = true">新建请假</el-button></div>
    <LeaveTable :rows="rows" :loading="loading" show-actions>
      <template #actions="{ row }"><el-button v-if="row.status === 'pending' && row.manager_status === 'pending'" type="danger" link :loading="cancellingId === row.id" @click="cancel(row)">撤销</el-button><span v-else class="muted">—</span></template>
    </LeaveTable>
    <LeaveFormDialog v-model="formVisible" @success="onFormSuccess" />
  </div>
</template>
