<script setup>
const model = defineModel({ type: String, default: '' })
defineProps({ sending: Boolean, disabled: Boolean })
const emit = defineEmits(['send'])

function keydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    emit('send')
  }
}
</script>

<template>
  <div class="input-wrap">
    <el-input v-model="model" type="textarea" :rows="2" resize="none" maxlength="2000" show-word-limit placeholder="请输入请假或审批需求，Enter 发送，Shift+Enter 换行" :disabled="disabled || sending" @keydown="keydown" />
    <el-button type="primary" :loading="sending" :disabled="disabled || !model.trim()" @click="emit('send')">发送</el-button>
  </div>
</template>

<style scoped>
.input-wrap { display: flex; gap: 12px; align-items: flex-end; padding: 15px 20px; border-top: 1px solid #e8edf3; background: #fff; }
.input-wrap .el-button { height: 54px; width: 84px; }
</style>
