<script setup>import { useRouter } from 'vue-router'; const router = useRouter()</script>
<template><div class="not-found"><strong>404</strong><h2>页面不存在</h2><p>您访问的页面可能已移动或不存在。</p><el-button type="primary" @click="router.replace('/agent')">返回 Agent</el-button></div></template>
<style scoped>.not-found { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #8090a4; }.not-found strong { color: #409eff; font-size: 72px; }.not-found h2 { margin: 8px 0; color: #26364e; }.not-found p { margin: 0 0 22px; }</style>
