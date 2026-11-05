<script setup>
import { leaveTypeLabels, statusLabels, statusTagType, formatDateTime } from '../../utils/format'

defineProps({ rows: { type: Array, default: () => [] }, loading: Boolean, showEmployee: Boolean, showActions: Boolean })
</script>

<template>
  <el-table :data="rows" v-loading="loading" stripe style="width: 100%">
    <template #empty><el-empty description="暂无请假记录" :image-size="70" /></template>
    <el-table-column v-if="showEmployee" label="员工" width="110"><template #default="{ row }">员工 #{{ row.employee_id }}</template></el-table-column>
    <el-table-column label="申请编号" min-width="230"><template #default="{ row }"><span class="table-id">{{ row.id }}</span></template></el-table-column>
    <el-table-column label="类型" width="85"><template #default="{ row }">{{ leaveTypeLabels[row.leave_type] || row.leave_type }}</template></el-table-column>
    <el-table-column prop="start_date" label="开始日期" width="115" />
    <el-table-column prop="end_date" label="结束日期" width="115" />
    <el-table-column prop="leave_days" label="天数" width="70" />
    <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ statusLabels[row.status] || row.status }}</el-tag></template></el-table-column>
    <el-table-column label="主管" width="90"><template #default="{ row }"><el-tag :type="statusTagType(row.manager_status)" size="small">{{ statusLabels[row.manager_status] || row.manager_status }}</el-tag></template></el-table-column>
    <el-table-column label="HR" width="90"><template #default="{ row }"><el-tag :type="statusTagType(row.hr_status)" size="small">{{ statusLabels[row.hr_status] || row.hr_status }}</el-tag></template></el-table-column>
    <el-table-column label="提交时间" width="155"><template #default="{ row }">{{ formatDateTime(row.submitted_at || row.created_at) }}</template></el-table-column>
    <el-table-column v-if="showActions" label="操作" width="150" fixed="right"><template #default="scope"><slot name="actions" v-bind="scope" /></template></el-table-column>
  </el-table>
</template>
