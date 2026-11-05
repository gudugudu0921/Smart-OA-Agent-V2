<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { useNotificationStore } from '../stores/notification'
import { formatDateTime, roleLabels } from '../utils/format'

const auth = useAuthStore()
const chat = useChatStore()
const notification = useNotificationStore()
const route = useRoute()
const router = useRouter()
const drawerVisible = ref(false)

const menuByRole = {
  employee: [
    { index: '/agent', label: 'Agent 助手' },
    { index: '/leave/my', label: '我的请假' },
  ],
  manager: [
    { index: '/agent', label: 'Agent 助手' },
    { index: '/manager/approvals?tab=pending', label: '待我审批' },
    { index: '/manager/approvals?tab=team', label: '团队请假' },
  ],
  hr: [
    { index: '/agent', label: 'Agent 助手' },
    { index: '/hr/approvals?tab=pending', label: 'HR 审批' },
    { index: '/hr/approvals?tab=all', label: '全部请假' },
  ],
  admin: [
    { index: '/agent', label: 'Agent 助手' },
    { index: '/admin/users', label: '用户管理' },
    { index: '/admin/audit', label: '审计日志' },
  ],
}

const menus = computed(() => menuByRole[auth.currentUser?.role] || [])
const activeMenu = computed(() => route.fullPath.startsWith('/agent') ? '/agent' : route.fullPath)
const todoLink = computed(() => {
  if (auth.currentUser?.role === 'manager' && notification.todos.items.manager_approvals > 0) {
    return { label: `待主管审批：${notification.todos.items.manager_approvals}`, path: '/manager/approvals?tab=pending' }
  }
  if (auth.currentUser?.role === 'hr' && notification.todos.items.hr_approvals > 0) {
    return { label: `待 HR 审批：${notification.todos.items.hr_approvals}`, path: '/hr/approvals?tab=pending' }
  }
  return null
})

async function refreshSummary() {
  try {
    await notification.refreshSummary()
  } catch (error) {
    console.error('Failed to refresh notification summary', error)
  }
}

async function openNotifications() {
  drawerVisible.value = true
  try {
    await notification.refresh()
  } catch (error) {
    console.error('Failed to load notifications', error)
  }
}

async function markRead(item) {
  if (item.is_read) return
  try {
    await notification.markRead(item.id)
  } catch (error) {
    console.error('Failed to mark notification read', error)
  }
}

async function markAllRead() {
  try {
    await notification.markAllRead()
  } catch (error) {
    console.error('Failed to mark notifications read', error)
  }
}

async function openTodo() {
  if (!todoLink.value) return
  drawerVisible.value = false
  await router.push(todoLink.value.path)
}

async function logout() {
  try {
    await ElMessageBox.confirm('确定退出当前账号吗？', '退出登录', { type: 'warning' })
  } catch {
    return
  }
  auth.logout()
  chat.reset()
  notification.reset()
  router.replace('/login')
}

function openChangePassword() {
  router.push('/change-password')
}

onMounted(refreshSummary)
</script>

<template>
  <el-container class="app-layout">
    <el-aside width="224px" class="sidebar">
      <div class="brand">
        <div class="brand-mark">OA</div>
        <div><strong>Smart OA</strong><small>智能审批系统</small></div>
      </div>
      <el-menu :default-active="activeMenu" router class="side-menu">
        <el-menu-item v-for="item in menus" :key="item.index" :index="item.index">
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-foot">LangGraph · FastAPI · Vue 3</div>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <div class="topbar-title">智能办公审批工作台</div>
        <div class="user-area">
          <el-badge :value="notification.unreadCount" :hidden="notification.unreadCount === 0" :max="99">
            <el-button class="notification-button" circle aria-label="通知" @click="openNotifications">🔔</el-button>
          </el-badge>
          <div class="avatar">{{ auth.currentUser?.name?.slice(0, 1) }}</div>
          <div class="user-meta">
            <strong>{{ auth.currentUser?.name }}</strong>
            <span>{{ roleLabels[auth.currentUser?.role] }} · {{ auth.currentUser?.department }}</span>
          </div>
          <el-button text @click="openChangePassword">修改密码</el-button>
          <el-button text @click="logout">退出登录</el-button>
        </div>
      </el-header>
      <el-main class="content"><router-view /></el-main>
    </el-container>
    <el-drawer v-model="drawerVisible" title="通知" size="420px">
      <template #header>
        <div class="drawer-head"><strong>通知</strong><el-button v-if="notification.unreadCount" text type="primary" @click="markAllRead">全部已读</el-button></div>
      </template>
      <button v-if="todoLink" class="todo-card" @click="openTodo">{{ todoLink.label }}<span>查看待办 →</span></button>
      <div v-loading="notification.loading" class="notification-list">
        <el-empty v-if="!notification.notifications.length && !notification.loading" description="暂无通知" />
        <button
          v-for="item in notification.notifications"
          :key="item.id"
          class="notification-item"
          :class="{ unread: !item.is_read }"
          @click="markRead(item)"
        >
          <span class="notification-dot" />
          <span class="notification-body">
            <strong>{{ item.title }}</strong>
            <span>{{ item.content }}</span>
            <small>{{ formatDateTime(item.created_at) }}<template v-if="item.related_application_id"> · 申请 {{ item.related_application_id.slice(0, 8) }}</template></small>
          </span>
        </button>
      </div>
    </el-drawer>
  </el-container>
</template>

<style scoped>
.app-layout { height: 100%; }
.sidebar { position: relative; background: #17243a; color: #fff; }
.brand { height: 72px; padding: 0 22px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid rgba(255,255,255,.08); }
.brand-mark { width: 38px; height: 38px; border-radius: 9px; display: grid; place-items: center; background: #409eff; font-weight: 800; }
.brand strong, .brand small { display: block; }
.brand small { margin-top: 3px; color: #92a2ba; font-size: 11px; }
.side-menu { border: 0; background: transparent; --el-menu-text-color: #b9c4d4; --el-menu-hover-bg-color: #22324d; --el-menu-active-color: #fff; }
.side-menu :deep(.el-menu-item.is-active) { background: #2877d5; }
.sidebar-foot { position: absolute; bottom: 20px; width: 100%; text-align: center; color: #71819a; font-size: 11px; }
.topbar { height: 72px; display: flex; align-items: center; justify-content: space-between; background: #fff; border-bottom: 1px solid #e8edf3; padding: 0 28px; }
.topbar-title { font-size: 18px; font-weight: 650; color: #1c2b41; }
.user-area { display: flex; align-items: center; gap: 11px; }
.notification-button { font-size: 16px; }
.avatar { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; color: #fff; background: #409eff; }
.user-meta strong, .user-meta span { display: block; }
.user-meta span { margin-top: 2px; color: #8a96a6; font-size: 12px; }
.content { padding: 22px; overflow: auto; background: #f3f6fa; }
.drawer-head { width: 100%; display: flex; align-items: center; justify-content: space-between; }
.todo-card { width: 100%; border: 1px solid #b9d7fb; background: #eef6ff; border-radius: 9px; padding: 13px 15px; margin-bottom: 14px; display: flex; justify-content: space-between; color: #2369b5; cursor: pointer; }
.notification-list { min-height: 180px; }
.notification-item { width: 100%; display: flex; gap: 10px; border: 0; border-bottom: 1px solid #edf0f4; background: #fff; padding: 14px 8px; text-align: left; cursor: pointer; }
.notification-item.unread { background: #f1f7ff; }
.notification-dot { width: 7px; height: 7px; margin-top: 7px; border-radius: 50%; background: transparent; flex: 0 0 auto; }
.notification-item.unread .notification-dot { background: #409eff; }
.notification-body, .notification-body strong, .notification-body span, .notification-body small { display: block; }
.notification-body { min-width: 0; }
.notification-body span { margin-top: 6px; line-height: 1.55; color: #56657a; }
.notification-body small { margin-top: 8px; color: #97a2b1; }
</style>
