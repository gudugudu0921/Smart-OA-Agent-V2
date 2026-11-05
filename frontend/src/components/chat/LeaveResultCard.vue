<script setup>
import { computed } from 'vue'
import { leaveTypeLabels, statusLabels, statusTagType } from '../../utils/format'
import { buildLeaveResultView } from '../../utils/leaveResult.js'

const props = defineProps({ item: { type: Object, required: true } })
const view = computed(() => buildLeaveResultView(props.item))
</script>

<template>
  <div v-if="view.hasContent" class="leave-card">
    <div v-if="view.hasHeader" class="card-head">
      <strong v-if="view.leaveType">{{ leaveTypeLabels[view.leaveType] || view.leaveType }}</strong>
      <el-tag v-if="view.status" :type="statusTagType(view.status)" size="small">{{ statusLabels[view.status] || view.status }}</el-tag>
    </div>
    <div v-if="view.applicationId" class="card-id">{{ view.applicationId }}</div>
    <div v-if="view.rows.length" class="card-grid">
      <template v-for="row in view.rows" :key="row.key">
        <span>{{ row.label }}</span>
        <el-tag v-if="row.kind === 'status'" :type="statusTagType(row.value)" size="small">{{ statusLabels[row.value] || row.value }}</el-tag>
        <b v-else>{{ row.value }}</b>
      </template>
    </div>
  </div>
</template>

<style scoped>
.leave-card { width: 100%; max-width: 520px; margin-top: 10px; padding: 13px; border: 1px solid #dfe7f1; border-radius: 8px; background: #fff; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.card-id { margin: 6px 0 10px; color: #7b8798; font: 11px ui-monospace, monospace; }
.card-id:last-child { margin-bottom: 0; }
.card-grid { display: grid; grid-template-columns: 54px 1fr; gap: 7px 10px; align-items: center; font-size: 12px; }
.card-grid span { color: #8793a4; }
</style>
