# Smart OA Agent Frontend

Vue 3 前端提供登录、首次改密、Agent Chat、请假、审批、通知、用户管理和审计日志界面。

## 技术栈

- Vue 3
- Vite
- Vue Router
- Pinia
- Element Plus
- Axios
- dayjs
- pnpm 11.19.0

## 本地安装

```bash
cd frontend
pnpm install --frozen-lockfile
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

## 环境配置

本地开发使用：

```text
VITE_API_BASE_URL=http://127.0.0.1:8000
```

该变量会进入浏览器构建产物，不能存放 API Key、密码或其他 Secret。

Docker production build 使用相对路径：

```text
VITE_API_BASE_URL=/api
```

浏览器请求先到 Nginx，再由 `/api` 代理到 Compose 内部的 Backend。

## 开发服务器

```bash
pnpm run dev --host 127.0.0.1 --port 5173
```

## Production Build

```bash
pnpm run build
```

构建输出位于 `dist/`，该目录属于生成物，不进入 Git。

## Nginx 行为

[`nginx/default.conf`](nginx/default.conf) 提供：

- Vue 静态文件
- `/api/` 到 `backend:8000` 的反向代理
- Vue Router history fallback
- 容器健康检查所需的根页面

直接访问 `/agent`、`/leave/my` 或管理页面时，Nginx 会回退到 `index.html`，再由 Vue Router 执行登录和角色守卫。

## 认证与权限

- Pinia auth store 管理 JWT 与当前用户
- 首次登录用户只能进入 `/change-password`
- 前端菜单按角色展示
- 最终权限始终由 Backend RBAC 校验，前端守卫不能替代服务端授权

## 安全注意事项

- 不要在前端环境变量或源码中保存 LLM Key、JWT Secret 或数据库密码
- 当前 JWT 使用 localStorage，适合学习、演示和封闭测试
- 公网部署前需要 HTTPS，并应重新评估浏览器 Token 存储策略
- Issue 或截图中不要包含 Token、密码、邮箱和真实人员信息
