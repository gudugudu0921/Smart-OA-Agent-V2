<script setup>
import { onMounted, reactive, ref } from 'vue'
import { getAuditLogs } from '../api/audit'
import { formatDateTime } from '../utils/format'

const rows = ref([])
const loading = ref(false)
const detailVisible = ref(false)
const selectedDetail = ref(null)
const filters = reactive({ action: '', resource_type: '', user_id: null })

async function load() {
  loading.value = true
  try {
    const params = { limit: 100 }
    if (filters.action.trim()) params.action = filters.action.trim()
    if (filters.resource_type.trim()) params.resource_type = filters.resource_type.trim()
    if (filters.user_id) params.user_id = filters.user_id
    rows.value = (await getAuditLogs(params)).data
  } finally {
    loading.value = false
  }
}

async function reset() {
  Object.assign(filters, { action: '', resource_type: '', user_id: null })
  await load()
}

function showDetail(row) {
  selectedDetail.value = row.detail
  detailVisible.value = true
}

const prettyDetail = () => JSON.stringify(selectedDetail.value ?? {}, null, 2)

onMounted(load)
</script>

<template>
  <div class="page-card">
    <div class="page-heading"><div><h2>审计日志</h2><p>只读查看关键业务操作与状态变化。</p></div></div>
    <el-form inline class="filter-bar">
      <el-form-item label="Action"><el-input v-model="filters.action" clearable placeholder="如 CREATE_LEAVE" style="width: 190px" /></el-form-item>
      <el-form-item label="资源类型"><el-input v-model="filters.resource_type" clearable placeholder="leave_application" style="width: 180px" /></el-form-item>
      <el-form-item label="用户ID"><el-input-number v-model="filters.user_id" :min="1" :controls="false" style="width: 120px" /></el-form-item>
      <el-form-item><el-button type="primary" @click="load">查询</el-button><el-button @click="reset">重置</el-button></el-form-item>
    </el-form>
    <el-table v-loading="loading" :data="rows" stripe>
      <el-table-column label="时间" width="160"><template #default="{ row }">{{ formatDateTime(row.created_at) }}</template></el-table-column>
      <el-table-column label="操作用户" min-width="145"><template #default="{ row }"><span>{{ row.name || '系统' }}</span><small v-if="row.employee_no" class="employee-no">{{ row.employee_no }} · ID {{ row.user_id }}</small></template></el-table-column>
      <el-table-column prop="action" label="Action" min-width="210" />
      <el-table-column prop="resource_type" label="资源类型" min-width="150" />
      <el-table-column prop="resource_id" label="资源ID" min-width="265" show-overflow-tooltip />
      <el-table-column prop="request_id" label="Request ID" min-width="230" show-overflow-tooltip />
      <el-table-column prop="ip_address" label="IP" width="120" />
      <el-table-column label="Detail" width="105"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">查看详情</el-button></template></el-table-column>
    </el-table>
    <el-dialog v-model="detailVisible" title="审计详情" width="600px"><pre class="detail-json">{{ prettyDetail() }}</pre></el-dialog>
  </div>
</template>

<style scoped>
.filter-bar { padding: 12px 14px 2px; margin-bottom: 14px; background: #f7f9fc; border-radius: 8px; }
.employee-no { display: block; margin-top: 3px; color: #9099a7; }
.detail-json { margin: 0; max-height: 480px; overflow: auto; padding: 15px; border-radius: 8px; background: #17243a; color: #d7e3f4; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
</style>
