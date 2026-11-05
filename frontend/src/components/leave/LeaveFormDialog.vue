<script setup>
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { createLeave } from '../../api/leave'

const visible = defineModel({ type: Boolean, default: false })
const emit = defineEmits(['success'])
const submitting = ref(false)
const formRef = ref()
const form = reactive({ leave_type: '', dates: [], reason: '' })
const rules = {
  leave_type: [{ required: true, message: '请选择请假类型', trigger: 'change' }],
  dates: [{ required: true, type: 'array', min: 2, message: '请选择开始和结束日期', trigger: 'change' }],
}
watch(visible, (value) => { if (value) Object.assign(form, { leave_type: '', dates: [], reason: '' }) })

async function submit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    await createLeave({ leave_type: form.leave_type, start_date: form.dates[0], end_date: form.dates[1], reason: form.reason || null })
    ElMessage.success('请假申请已提交')
    visible.value = false
    emit('success')
  } finally { submitting.value = false }
}
</script>

<template>
  <el-dialog v-model="visible" title="新建请假" width="520px" :close-on-click-modal="false">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="88px">
      <el-form-item label="请假类型" prop="leave_type"><el-select v-model="form.leave_type" placeholder="请选择" style="width: 100%"><el-option v-for="item in [['annual','年假'],['sick','病假'],['personal','事假'],['marriage','婚假'],['maternity','产假'],['bereavement','丧假'],['other','其他']]" :key="item[0]" :label="item[1]" :value="item[0]" /></el-select></el-form-item>
      <el-form-item label="请假日期" prop="dates"><el-date-picker v-model="form.dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" style="width: 100%" /></el-form-item>
      <el-form-item label="原因"><el-input v-model="form.reason" type="textarea" :rows="3" maxlength="500" show-word-limit /></el-form-item>
    </el-form>
    <template #footer><el-button @click="visible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="submit">提交申请</el-button></template>
  </el-dialog>
</template>
