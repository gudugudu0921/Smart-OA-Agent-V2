<script setup>
import { nextTick, ref, watch } from 'vue'
import LeaveResultCard from './LeaveResultCard.vue'

const props = defineProps({ messages: { type: Array, default: () => [] }, sending: Boolean })
const scrollBox = ref()
watch(() => [props.messages.length, props.sending], async () => {
  await nextTick()
  if (scrollBox.value) scrollBox.value.scrollTop = scrollBox.value.scrollHeight
})

const asItems = (data) => Array.isArray(data) ? data : data ? [data] : []
</script>

<template>
  <div ref="scrollBox" class="message-scroll">
    <div v-for="(message, index) in messages" :key="index" class="message-row" :class="message.role">
      <div class="message-avatar">{{ message.role === 'user' ? '我' : 'OA' }}</div>
      <div class="message-body" :class="{ error: message.error }">
        <div class="message-text">{{ message.content }}</div>
        <LeaveResultCard v-for="item in asItems(message.data)" :key="item.application_id || item.id" :item="item" />
      </div>
    </div>
    <div v-if="sending" class="message-row assistant">
      <div class="message-avatar">OA</div><div class="message-body typing">Agent 正在处理<span>...</span></div>
    </div>
  </div>
</template>

<style scoped>
.message-scroll { flex: 1; overflow-y: auto; padding: 24px 28px; }
.message-row { display: flex; gap: 10px; margin-bottom: 20px; align-items: flex-start; }
.message-row.user { flex-direction: row-reverse; }
.message-avatar { flex: 0 0 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; background: #e6eef8; color: #45617f; font-size: 12px; font-weight: 700; }
.user .message-avatar { background: #409eff; color: #fff; }
.message-body { max-width: 72%; padding: 11px 14px; border-radius: 4px 12px 12px 12px; background: #f0f3f7; line-height: 1.65; font-size: 14px; white-space: pre-wrap; }
.user .message-body { color: #fff; background: #409eff; border-radius: 12px 4px 12px 12px; }
.message-body.error { color: #c45656; background: #fef0f0; }
.typing { color: #7b8798; }
</style>
