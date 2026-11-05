<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const formRef = ref()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

async function submit() {
  await formRef.value.validate()
  loading.value = true
  try {
    await auth.login(form)
    ElMessage.success('登录成功')
    router.replace(auth.currentUser?.must_change_password ? '/change-password' : '/agent')
  } catch {
    ElMessage.error('用户名或密码错误')
  } finally { loading.value = false }
}
</script>

<template>
  <div class="login-page">
    <div class="login-brand">
      <div class="brand-badge">OA</div>
      <h1>Smart OA Agent</h1>
      <p>基于 LangGraph 的智能办公审批系统</p>
      <div class="feature-list"><span>智能请假 Agent</span><span>多角色审批闭环</span><span>多轮会话恢复</span></div>
    </div>
    <div class="login-card">
      <div class="login-title"><h2>欢迎登录</h2><p>使用您的企业账号进入工作台</p></div>
      <el-form ref="formRef" :model="form" label-position="top" @keyup.enter="submit">
        <el-form-item label="用户名" prop="username" :rules="[{ required: true, message: '请输入用户名' }]"><el-input v-model="form.username" size="large" placeholder="请输入用户名" autocomplete="username" /></el-form-item>
        <el-form-item label="密码" prop="password" :rules="[{ required: true, message: '请输入密码' }]"><el-input v-model="form.password" type="password" size="large" placeholder="请输入密码" show-password autocomplete="current-password" /></el-form-item>
        <el-button type="primary" size="large" class="login-button" :loading="loading" @click="submit">登录</el-button>
      </el-form>
      <p class="security-note">账号权限由后端 JWT 与 RBAC 统一验证</p>
    </div>
  </div>
</template>

<style scoped>
.login-page { min-height: 100%; display: flex; align-items: center; justify-content: center; gap: 110px; background: #f1f5fa; }
.login-brand { width: 420px; }
.brand-badge { width: 64px; height: 64px; display: grid; place-items: center; border-radius: 15px; background: #2678d8; color: #fff; font-size: 24px; font-weight: 800; box-shadow: 0 12px 28px rgba(38,120,216,.24); }
.login-brand h1 { margin: 24px 0 10px; color: #162740; font-size: 38px; letter-spacing: -.5px; }
.login-brand p { color: #6f7f94; font-size: 17px; }
.feature-list { display: flex; gap: 10px; margin-top: 28px; flex-wrap: wrap; }
.feature-list span { padding: 8px 12px; border-radius: 6px; background: #fff; color: #58708f; font-size: 12px; border: 1px solid #e3eaf2; }
.login-card { width: 420px; padding: 38px; border-radius: 14px; background: #fff; box-shadow: 0 16px 48px rgba(29,48,76,.12); }
.login-title h2 { margin: 0; font-size: 25px; color: #1b2a40; }
.login-title p { margin: 8px 0 27px; color: #8a96a6; }
.login-button { width: 100%; margin-top: 8px; }
.security-note { margin: 22px 0 0; color: #a0a9b5; text-align: center; font-size: 11px; }
</style>
