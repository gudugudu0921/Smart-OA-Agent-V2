<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const formRef = ref()
const loading = ref(false)
const form = reactive({ old_password: '', new_password: '', confirm_password: '' })
const forced = computed(() => Boolean(auth.currentUser?.must_change_password))

const errorMessages = {
  CURRENT_PASSWORD_INCORRECT: '当前密码不正确',
  NEW_PASSWORD_MUST_DIFFER: '新密码不能与当前密码相同',
  PASSWORD_TOO_SHORT: '新密码至少需要 8 个字符',
  PASSWORD_TOO_LONG: '新密码过长',
  PASSWORD_TOO_WEAK: '新密码至少需要包含字母、数字、特殊字符中的两类',
}

function validateConfirmation(_rule, value, callback) {
  if (value !== form.new_password) callback(new Error('两次输入的新密码不一致'))
  else callback()
}

async function submit() {
  await formRef.value.validate()
  loading.value = true
  try {
    await auth.changePassword({ old_password: form.old_password, new_password: form.new_password })
    ElMessage.success('密码修改成功，请使用新密码重新登录')
    await router.replace('/login')
  } catch (error) {
    const detail = error?.response?.data?.detail
    ElMessage.error(errorMessages[detail] || '密码修改失败，请稍后重试')
  } finally { loading.value = false }
}

function logout() {
  auth.logout()
  router.replace('/login')
}
</script>

<template>
  <div class="password-page">
    <div class="password-card">
      <div class="password-heading">
        <div class="brand-mark">OA</div>
        <div><h2>{{ forced ? '首次登录，请修改密码' : '修改密码' }}</h2><p>{{ forced ? '临时密码修改完成后才能进入 OA 系统。' : '修改成功后需要重新登录。' }}</p></div>
      </div>
      <el-alert v-if="forced" type="warning" :closable="false" title="为了账号安全，首次登录必须先设置自己的密码。" />
      <el-form ref="formRef" :model="form" label-position="top" class="password-form">
        <el-form-item label="当前密码" prop="old_password" :rules="[{ required: true, message: '请输入当前密码' }]"><el-input v-model="form.old_password" type="password" show-password autocomplete="current-password" /></el-form-item>
        <el-form-item label="新密码" prop="new_password" :rules="[{ required: true, message: '请输入新密码' }, { min: 8, message: '新密码至少需要 8 个字符' }]"><el-input v-model="form.new_password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <el-form-item label="确认新密码" prop="confirm_password" :rules="[{ required: true, message: '请再次输入新密码' }, { validator: validateConfirmation, trigger: 'blur' }]"><el-input v-model="form.confirm_password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <p class="policy">密码需为 8～128 个字符，并至少包含字母、数字、特殊字符中的两类。</p>
        <el-button type="primary" class="submit-button" :loading="loading" @click="submit">确认修改</el-button>
        <el-button class="logout-button" @click="logout">退出登录</el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.password-page { min-height: 100%; display: grid; place-items: center; background: #f1f5fa; }
.password-card { width: 460px; padding: 36px; border-radius: 14px; background: #fff; box-shadow: 0 16px 48px rgba(29,48,76,.12); }
.password-heading { display: flex; align-items: center; gap: 14px; margin-bottom: 24px; }
.brand-mark { width: 46px; height: 46px; display: grid; place-items: center; border-radius: 10px; background: #2678d8; color: #fff; font-weight: 800; }
.password-heading h2 { margin: 0; color: #1b2a40; }
.password-heading p { margin: 6px 0 0; color: #7d8999; font-size: 13px; }
.password-form { margin-top: 22px; }
.policy { margin: -3px 0 18px; color: #8491a3; font-size: 12px; line-height: 1.6; }
.submit-button, .logout-button { width: 100%; margin: 0; }
.logout-button { margin-top: 10px; }
</style>
