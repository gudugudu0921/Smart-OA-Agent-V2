<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import LeaveTable from '../components/leave/LeaveTable.vue'
import ReviewDialog from '../components/leave/ReviewDialog.vue'
import { getAllLeaves, getHrPending, hrReview } from '../api/leave'
import { useNotificationStore } from '../stores/notification'

const route = useRoute()
const router = useRouter()
const notification = useNotificationStore()
const activeTab = ref(route.query.tab === 'all' ? 'all' : 'pending')
const pending = ref([])
const allRows = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const selected = ref(null)
const defaultDecision = ref('approved')
const submitting = ref(false)
const filters = reactive({ status: '', leave_type: '', department: '' })

async function loadPending() { pending.value = (await getHrPending()).data }
async function loadAll() {
  const params = Object.fromEntries(Object.entries(filters).filter(([, value]) => value))
  allRows.value = (await getAllLeaves(params)).data
}
async function load() {
  loading.value = true
  try { await Promise.all([loadPending(), loadAll()]) } finally { loading.value = false }
}
function openReview(row, decision) { selected.value = row; defaultDecision.value = decision; dialogVisible.value = true }
async function review(payload) {
  submitting.value = true
  try {
    await hrReview(selected.value.id, payload)
    ElMessage.success('HR审批已完成')
    dialogVisible.value = false
    await Promise.all([load(), notification.refreshSummary()])
  } finally { submitting.value = false }
}
async function search() { loading.value = true; try { await loadAll() } finally { loading.value = false } }
async function reset() { Object.assign(filters, { status: '', leave_type: '', department: '' }); await search() }
watch(activeTab, (tab) => router.replace({ path: route.path, query: { tab } }))
watch(() => route.query.tab, (tab) => { if (tab === 'pending' || tab === 'all') activeTab.value = tab })
onMounted(load)
</script>

<template>
  <div class="page-card">
    <div class="page-heading"><div><h2>HR 审批</h2><p>完成终审，并查询全公司的请假申请。</p></div></div>
    <el-tabs v-model="activeTab">
      <el-tab-pane label="待HR审批" name="pending">
        <LeaveTable :rows="pending" :loading="loading" show-employee show-actions>
          <template #actions="{ row }"><el-button type="success" link @click="openReview(row, 'approved')">批准</el-button><el-button type="danger" link @click="openReview(row, 'rejected')">拒绝</el-button></template>
        </LeaveTable>
      </el-tab-pane>
      <el-tab-pane label="全部申请" name="all">
        <el-form inline class="filter-bar">
          <el-form-item label="状态"><el-select v-model="filters.status" clearable style="width: 130px"><el-option label="待处理" value="pending" /><el-option label="已批准" value="approved" /><el-option label="已拒绝" value="rejected" /><el-option label="已撤销" value="cancelled" /></el-select></el-form-item>
          <el-form-item label="类型"><el-select v-model="filters.leave_type" clearable style="width: 130px"><el-option label="年假" value="annual" /><el-option label="病假" value="sick" /><el-option label="事假" value="personal" /><el-option label="其他" value="other" /></el-select></el-form-item>
          <el-form-item label="部门"><el-input v-model="filters.department" clearable placeholder="部门名称" style="width: 150px" /></el-form-item>
          <el-form-item><el-button type="primary" @click="search">查询</el-button><el-button @click="reset">重置</el-button></el-form-item>
        </el-form>
        <LeaveTable :rows="allRows" :loading="loading" show-employee />
      </el-tab-pane>
    </el-tabs>
    <ReviewDialog v-model="dialogVisible" :application="selected" :default-decision="defaultDecision" :submitting="submitting" title="HR审批" @submit="review" />
  </div>
</template>

<style scoped>.filter-bar { padding: 12px 14px 2px; margin-bottom: 14px; background: #f7f9fc; border-radius: 8px; }</style>
