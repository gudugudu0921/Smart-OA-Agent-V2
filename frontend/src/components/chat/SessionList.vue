<script setup>
import { formatDateTime } from '../../utils/format'

defineProps({ sessions: { type: Array, default: () => [] }, currentThreadId: String, loading: Boolean })
const emit = defineEmits(['create', 'select', 'delete'])
</script>

<template>
  <aside class="session-panel">
    <el-button type="primary" class="new-button" :loading="loading" @click="emit('create')">+ 新建会话</el-button>
    <div class="session-scroll" v-loading="loading">
      <el-empty v-if="!sessions.length && !loading" description="没有聊天会话" :image-size="64" />
      <button
        v-for="session in sessions"
        :key="session.thread_id"
        class="session-item"
        :class="{ active: session.thread_id === currentThreadId }"
        @click="emit('select', session.thread_id)"
      >
        <span class="session-title">{{ session.title }}</span>
        <span class="session-preview">{{ session.last_message_preview || '尚未发送消息' }}</span>
        <span class="session-time">{{ formatDateTime(session.updated_at) }}</span>
        <span class="delete" title="删除会话" @click.stop="emit('delete', session.thread_id)">×</span>
      </button>
    </div>
  </aside>
</template>

<style scoped>
.session-panel { width: 252px; border-right: 1px solid #e8edf3; background: #f8fafc; padding: 16px 12px; display: flex; flex-direction: column; }
.new-button { width: 100%; margin-bottom: 14px; }
.session-scroll { flex: 1; overflow-y: auto; }
.session-item { position: relative; width: 100%; border: 1px solid transparent; background: transparent; text-align: left; border-radius: 8px; padding: 12px 28px 12px 12px; margin-bottom: 8px; cursor: pointer; color: inherit; }
.session-item:hover { background: #edf3fa; }
.session-item.active { border-color: #b9d7fb; background: #eaf3ff; }
.session-title, .session-preview, .session-time { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.session-title { font-weight: 650; font-size: 14px; }
.session-preview { margin-top: 6px; color: #6f7d90; font-size: 12px; }
.session-time { margin-top: 7px; color: #9aa5b3; font-size: 11px; }
.delete { position: absolute; right: 9px; top: 9px; color: #9aa5b3; font-size: 18px; }
.delete:hover { color: #f56c6c; }
</style>
