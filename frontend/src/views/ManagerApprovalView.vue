<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import LeaveTable from '../components/leave/LeaveTable.vue'
import ReviewDialog from '../components/leave/ReviewDialog.vue'
import { getManagerPending, getTeamLeaves, managerReview } from '../api/leave'
import { useNotificationStore } from '../stores/notification'

const route = useRoute()
const router = useRouter()
const notification = useNotificationStore()
const activeTab = ref(route.query.tab === 'team' ? 'team' : 'pending')
const pending = ref([])
const team = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const selected = ref(null)
const defaultDecision = ref('approved')
const submitting = ref(false)

async function load() {
  loading.value = true
  try {
    const [pendingRes, teamRes] = await Promise.all([getManagerPending(), getTeamLeaves()])
    pending.value = pendingRes.data
    team.value = teamRes.data
  } finally { loading.value = false }
}

function openReview(row, decision) {
  selected.value = row
  defaultDecision.value = decision
  dialogVisible.value = true
}

async function review(payload) {
  submitting.value = true
  try {
    await managerReview(selected.value.id, payload)
    ElMessage.success('主管审批已完成')
    dialogVisible.value = false
    await Promise.all([load(), notification.refreshSummary()])
  } finally { submitting.value = false }
}

watch(activeTab, (tab) => router.replace({ path: route.path, query: { tab } }))
watch(() => route.query.tab, (tab) => { if (tab === 'pending' || tab === 'team') activeTab.value = tab })
onMounted(load)
</script>

<template>
  <div class="page-card">
    <div class="page-heading"><div><h2>主管审批</h2><p>处理直属员工申请，并查看团队请假历史。</p></div></div>
    <el-tabs v-model="activeTab">
      <el-tab-pane label="待审批" name="pending">
        <LeaveTable :rows="pending" :loading="loading" show-employee show-actions>
          <template #actions="{ row }"><el-button type="success" link @click="openReview(row, 'approved')">批准</el-button><el-button type="danger" link @click="openReview(row, 'rejected')">拒绝</el-button></template>
        </LeaveTable>
      </el-tab-pane>
      <el-tab-pane label="团队历史" name="team"><LeaveTable :rows="team" :loading="loading" show-employee /></el-tab-pane>
    </el-tabs>
    <ReviewDialog v-model="dialogVisible" :application="selected" :default-decision="defaultDecision" :submitting="submitting" title="主管审批" @submit="review" />
  </div>
</template>
