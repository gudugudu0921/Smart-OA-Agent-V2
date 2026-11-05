<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import SessionList from '../components/chat/SessionList.vue'
import ChatMessages from '../components/chat/ChatMessages.vue'
import ChatInput from '../components/chat/ChatInput.vue'
import LeaveConfirmationCard from '../components/chat/LeaveConfirmationCard.vue'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { useNotificationStore } from '../stores/notification'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const chat = useChatStore()
const notification = useNotificationStore()
const input = ref('')
const ready = ref(false)
const sessionError = ref('')

const quickByRole = {
  employee: ['我下周一请一天事假', '查询我的请假记录'],
  manager: ['查看我的待审批申请'],
  hr: ['查看需要我审批的请假'],
  admin: ['你好，你能做什么？'],
}
const quickPrompts = computed(() => quickByRole[auth.currentUser?.role] || [])
const currentSession = computed(() => chat.sessions.find((item) => item.thread_id === chat.currentThreadId))

async function updateUrl(threadId) {
  if (route.query.thread_id !== threadId) await router.replace({ path: '/agent', query: { thread_id: threadId } })
}

async function create() {
  sessionError.value = ''
  try {
    const session = await chat.createSession(auth.currentUser?.id)
    await updateUrl(session.thread_id)
  } catch {
    sessionError.value = '会话服务暂时不可用'
    ElMessage({ type: 'error', message: '会话服务暂时不可用', duration: 6000 })
  }
}

async function select(threadId) {
  if (threadId === chat.currentThreadId && ready.value) return
  await chat.selectSession(threadId, auth.currentUser?.id)
  await updateUrl(threadId)
}

async function remove(threadId) {
  try {
    await ElMessageBox.confirm('删除后聊天上下文无法恢复，但不会删除已创建的请假数据。', '删除会话', { type: 'warning' })
  } catch {
    return
  }
  try {
    await chat.deleteSession(threadId, auth.currentUser?.id)
    ElMessage.success('会话已删除')
  } catch (error) {
    ElMessage.error(error.message || '会话暂时无法删除')
    return
  }
  if (!chat.sessions.length) await create()
  else if (!chat.currentThreadId) await select(chat.sessions[0].thread_id)
}

async function send() {
  const content = input.value.trim()
  if (!content || chat.sending) return
  input.value = ''
  try {
    await chat.sendMessage(content, auth.currentUser?.id)
    await notification.refreshSummary()
  } catch (error) {
    console.error('Agent request failed', error)
  }
}

async function confirmLeave() {
  try {
    const result = await chat.confirmTask(auth.currentUser?.id)
    if (result?.success) await notification.refreshSummary()
  } catch (error) {
    console.error('Leave confirmation failed safely', error)
  }
}

async function cancelLeave() {
  try {
    await chat.cancelTask(auth.currentUser?.id)
  } catch (error) {
    console.error('Leave cancellation failed safely', error)
  }
}

async function reconcileLeave() {
  try {
    const result = await chat.reconcileOperation(auth.currentUser?.id)
    if (result?.status === 'SUCCEEDED') await notification.refreshSummary()
  } catch (error) {
    console.error('Leave operation lookup failed safely', error)
  }
}

onMounted(async () => {
  try {
    await chat.loadSessions()
    const queryThread = typeof route.query.thread_id === 'string' ? route.query.thread_id : ''
    const exists = queryThread && chat.sessions.some((item) => item.thread_id === queryThread)
    if (exists) await select(queryThread)
    else if (chat.sessions.length) await select(chat.sessions[0].thread_id)
    else await create()
    ready.value = true
  } catch {
    sessionError.value = '会话服务暂时不可用'
    ready.value = true
  }
})

watch(() => route.query.thread_id, async (threadId) => {
  if (ready.value && typeof threadId === 'string' && threadId !== chat.currentThreadId) {
    try { await chat.selectSession(threadId, auth.currentUser?.id) } catch { await router.replace('/agent') }
  }
})

onUnmounted(() => chat.releaseCoordination())
</script>

<template>
  <div class="agent-page page-card">
    <SessionList :sessions="chat.sessions" :current-thread-id="chat.currentThreadId" :loading="chat.loading && !ready" @create="create" @select="select" @delete="remove" />
    <section class="chat-panel">
      <header class="chat-head">
        <div><strong>{{ currentSession?.title || '新会话' }}</strong><span>thread · {{ chat.currentThreadId || '准备中' }}</span></div>
        <el-tag type="success" effect="plain">Redis 会话</el-tag>
      </header>
      <el-alert v-if="sessionError" :title="sessionError" type="error" show-icon :closable="false" />
      <div v-if="!chat.messages.length && !chat.loading" class="welcome">
        <div class="welcome-mark">OA</div><h2>今天需要我协助什么？</h2><p>可以用自然语言提交、查询或审批请假申请。</p>
        <div class="quick-list"><el-button v-for="item in quickPrompts" :key="item" plain @click="input = item">{{ item }}</el-button></div>
      </div>
      <ChatMessages v-else :messages="chat.messages" :sending="chat.sending" />
      <LeaveConfirmationCard
        :task="chat.currentTask"
        :sending="chat.sending"
        :active-action="chat.activeAction"
        @confirm="confirmLeave"
        @cancel="cancelLeave"
        @reconcile="reconcileLeave"
      />
      <ChatInput v-model="input" :sending="chat.sending" :disabled="!chat.currentThreadId || chat.inputBlocked" @send="send" />
    </section>
  </div>
</template>

<style scoped>
.agent-page { height: calc(100vh - 116px); min-height: 590px; padding: 0; display: flex; overflow: hidden; }
.chat-panel { flex: 1; min-width: 0; display: flex; flex-direction: column; background: #fff; }
.chat-head { height: 66px; padding: 0 22px; border-bottom: 1px solid #e8edf3; display: flex; align-items: center; justify-content: space-between; }
.chat-head strong, .chat-head span { display: block; }
.chat-head span { margin-top: 4px; color: #9aa5b3; font-size: 10px; font-family: monospace; }
.welcome { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #7a8798; }
.welcome-mark { width: 54px; height: 54px; display: grid; place-items: center; border-radius: 14px; background: #eaf3ff; color: #2877d5; font-weight: 800; }
.welcome h2 { margin: 18px 0 6px; color: #26364e; }
.welcome p { margin: 0; }
.quick-list { display: flex; gap: 10px; margin-top: 22px; }
</style>
