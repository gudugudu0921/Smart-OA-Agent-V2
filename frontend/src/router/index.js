import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
  { path: '/change-password', name: 'change-password', component: () => import('../views/ChangePasswordView.vue'), meta: { requiresAuth: true } },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: '/agent' },
      { path: 'agent', name: 'agent', component: () => import('../views/AgentView.vue'), meta: { requiresAuth: true } },
      { path: 'leave/my', name: 'my-leave', component: () => import('../views/MyLeaveView.vue'), meta: { requiresAuth: true, roles: ['employee'] } },
      { path: 'manager/approvals', name: 'manager-approvals', component: () => import('../views/ManagerApprovalView.vue'), meta: { requiresAuth: true, roles: ['manager'] } },
      { path: 'hr/approvals', name: 'hr-approvals', component: () => import('../views/HrApprovalView.vue'), meta: { requiresAuth: true, roles: ['hr'] } },
      { path: 'admin/users', name: 'admin-users', component: () => import('../views/UserManagementView.vue'), meta: { requiresAuth: true, roles: ['admin'] } },
      { path: 'admin/audit', name: 'admin-audit', component: () => import('../views/AuditLogView.vue'), meta: { requiresAuth: true, roles: ['admin'] } },
    ],
  },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('../views/NotFoundView.vue') },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.initialized) await auth.restoreAuth()
  if (to.meta.requiresAuth && !auth.isLoggedIn) return { path: '/login', query: { redirect: to.fullPath } }
  if (auth.isLoggedIn && auth.currentUser?.must_change_password && to.path !== '/change-password') return '/change-password'
  if (to.path === '/login' && auth.isLoggedIn) return '/agent'
  if (to.meta.roles && !to.meta.roles.includes(auth.currentUser?.role)) {
    ElMessage.error('没有权限访问该页面')
    return '/agent'
  }
  return true
})

export default router
