<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createUser, getUsers, resetUserPassword, setUserStatus } from '../api/users'
import { useAuthStore } from '../stores/auth'
import { roleLabels, formatDateTime } from '../utils/format'

const auth = useAuthStore()
const rows = ref([])
const loading = ref(false)
const actionUserId = ref(null)
const createVisible = ref(false)
const createLoading = ref(false)
const formRef = ref()
const createdCredential = ref(null)
const credentialVisible = ref(false)
const credentialMode = ref('create')

const emptyForm = () => ({ username: '', employee_no: '', name: '', email: '', role: 'employee', department: '', position: '', manager_id: null })
const form = reactive(emptyForm())
const managers = computed(() => rows.value.filter((user) => user.role === 'manager' && user.is_active))

async function load() {
  loading.value = true
  try { rows.value = (await getUsers()).data } finally { loading.value = false }
}

function openCreate() {
  Object.assign(form, emptyForm())
  createVisible.value = true
}

function roleChanged(role) {
  if (role !== 'employee') form.manager_id = null
}

async function submitCreate() {
  await formRef.value.validate()
  createLoading.value = true
  try {
    const payload = { ...form, email: form.email || null, position: form.position || null, manager_id: form.role === 'employee' ? form.manager_id : null }
    const { data } = await createUser(payload)
    createdCredential.value = { username: data.user.username, temporaryPassword: data.temporary_password }
    credentialMode.value = 'create'
    createVisible.value = false
    credentialVisible.value = true
    await load()
  } finally { createLoading.value = false }
}

function clearCredential() { createdCredential.value = null }

async function copyTemporaryPassword() {
  if (!createdCredential.value) return
  try {
    await navigator.clipboard.writeText(createdCredential.value.temporaryPassword)
    ElMessage.success('临时密码已复制')
  } catch { ElMessage.warning('无法自动复制，请手动复制临时密码') }
}

async function toggleStatus(user) {
  const action = user.is_active ? '停用' : '启用'
  await ElMessageBox.confirm(`确定${action}账号 ${user.username} 吗？`, `${action}用户`, { type: user.is_active ? 'warning' : 'info' })
  actionUserId.value = user.id
  try {
    const { data } = await setUserStatus(user.id, !user.is_active)
    const index = rows.value.findIndex((item) => item.id === user.id)
    if (index >= 0) rows.value[index] = data
    ElMessage.success(`账号已${action}`)
  } finally { actionUserId.value = null }
}

async function resetPassword(user) {
  await ElMessageBox.confirm(`确定重置账号 ${user.username} 的密码吗？该用户当前登录状态将失效。`, '重置密码', { type: 'warning' })
  actionUserId.value = user.id
  try {
    const { data } = await resetUserPassword(user.id)
    createdCredential.value = { username: data.username, temporaryPassword: data.temporary_password }
    credentialMode.value = 'reset'
    credentialVisible.value = true
    user.must_change_password = true
  } finally { actionUserId.value = null }
}

onMounted(load)
</script>

<template>
  <div class="page-card">
    <div class="page-heading">
      <div><h2>用户管理</h2><p>创建和管理测试账号、角色及组织关系。</p></div>
      <el-button type="primary" @click="openCreate">创建用户</el-button>
    </div>
    <el-table :data="rows" v-loading="loading" stripe>
      <template #empty><el-empty description="没有用户数据" /></template>
      <el-table-column prop="employee_no" label="员工号" width="120" />
      <el-table-column prop="username" label="用户名" width="150" />
      <el-table-column prop="name" label="姓名" width="100" />
      <el-table-column label="角色" width="100"><template #default="{ row }"><el-tag>{{ roleLabels[row.role] || row.role }}</el-tag></template></el-table-column>
      <el-table-column prop="department" label="部门" width="120" />
      <el-table-column prop="position" label="职位" min-width="130" />
      <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '启用' : '停用' }}</el-tag></template></el-table-column>
      <el-table-column label="首次改密" width="100"><template #default="{ row }"><el-tag :type="row.must_change_password ? 'warning' : 'success'">{{ row.must_change_password ? '待完成' : '已完成' }}</el-tag></template></el-table-column>
      <el-table-column label="最后登录" width="170"><template #default="{ row }">{{ formatDateTime(row.last_login_at) }}</template></el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button text :loading="actionUserId === row.id" :disabled="row.id === auth.currentUser?.id" @click="resetPassword(row)">重置密码</el-button>
          <el-button text :type="row.is_active ? 'danger' : 'primary'" :loading="actionUserId === row.id" :disabled="row.id === auth.currentUser?.id && row.is_active" @click="toggleStatus(row)">{{ row.is_active ? '停用' : '启用' }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="createVisible" title="创建用户" width="560px">
      <el-form ref="formRef" :model="form" label-width="92px">
        <el-form-item label="用户名" prop="username" :rules="[{ required: true, message: '请输入用户名' }, { pattern: /^[A-Za-z0-9_.-]{3,64}$/, message: '仅支持字母、数字、点、下划线和短横线' }]"><el-input v-model="form.username" autocomplete="off" /></el-form-item>
        <el-form-item label="员工号" prop="employee_no" :rules="[{ required: true, message: '请输入员工号' }]"><el-input v-model="form.employee_no" /></el-form-item>
        <el-form-item label="姓名" prop="name" :rules="[{ required: true, message: '请输入姓名' }]"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="邮箱" prop="email" :rules="[{ type: 'email', message: '邮箱格式不正确' }]"><el-input v-model="form.email" /></el-form-item>
        <el-form-item label="角色" prop="role" :rules="[{ required: true }]"><el-select v-model="form.role" style="width: 100%" @change="roleChanged"><el-option label="员工" value="employee" /><el-option label="主管" value="manager" /><el-option label="HR" value="hr" /><el-option label="管理员" value="admin" /></el-select></el-form-item>
        <el-form-item label="部门" prop="department" :rules="[{ required: true, message: '请输入部门' }]"><el-input v-model="form.department" /></el-form-item>
        <el-form-item label="职位" prop="position"><el-input v-model="form.position" /></el-form-item>
        <el-form-item v-if="form.role === 'employee'" label="直属主管" prop="manager_id" :rules="[{ required: true, message: '请选择直属主管' }]"><el-select v-model="form.manager_id" style="width: 100%" placeholder="选择启用状态的主管"><el-option v-for="manager in managers" :key="manager.id" :label="`${manager.name}（${manager.username}）`" :value="manager.id" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button @click="createVisible = false">取消</el-button><el-button type="primary" :loading="createLoading" @click="submitCreate">创建</el-button></template>
    </el-dialog>

    <el-dialog v-model="credentialVisible" :title="credentialMode === 'reset' ? '密码重置成功' : '用户创建成功'" width="500px" :close-on-click-modal="false" @closed="clearCredential">
      <el-alert type="warning" :closable="false" :title="credentialMode === 'reset' ? '临时密码仅显示一次，该用户下次登录后必须修改密码。' : '该密码仅显示一次，请安全地发送给测试用户。'" />
      <div v-if="createdCredential" class="credential-box"><p><strong>用户名：</strong>{{ createdCredential.username }}</p><el-input :model-value="createdCredential.temporaryPassword" readonly><template #append><el-button @click="copyTemporaryPassword">复制</el-button></template></el-input></div>
      <template #footer><el-button type="primary" @click="credentialVisible = false">我已保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.credential-box { margin-top: 18px; }
.credential-box p { margin: 0 0 10px; color: #38465a; }
</style>
