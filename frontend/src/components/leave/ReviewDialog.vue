<script setup>
import { reactive, ref, watch } from 'vue'

const visible = defineModel({ type: Boolean, default: false })
const props = defineProps({ application: Object, defaultDecision: { type: String, default: 'approved' }, submitting: Boolean, title: { type: String, default: '审批申请' } })
const emit = defineEmits(['submit'])
const form = reactive({ decision: 'approved', comment: '' })
const formRef = ref()
watch(visible, (value) => { if (value) Object.assign(form, { decision: props.defaultDecision, comment: '' }) })

async function submit() {
  await formRef.value.validate()
  emit('submit', { decision: form.decision, comment: form.comment || null })
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="500px" :close-on-click-modal="false">
    <p class="muted">申请编号：{{ application?.id }}</p>
    <el-form ref="formRef" :model="form" label-width="88px">
      <el-form-item label="审批结果" prop="decision" :rules="[{ required: true, message: '请选择审批结果' }]">
        <el-radio-group v-model="form.decision"><el-radio value="approved">批准</el-radio><el-radio value="rejected">拒绝</el-radio></el-radio-group>
      </el-form-item>
      <el-form-item label="审批意见"><el-input v-model="form.comment" type="textarea" :rows="3" maxlength="500" show-word-limit /></el-form-item>
    </el-form>
    <template #footer><el-button @click="visible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="submit">确认审批</el-button></template>
  </el-dialog>
</template>
