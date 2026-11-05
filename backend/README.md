# Smart OA Agent Backend

FastAPI 后端负责认证、RBAC、用户管理、请假状态机、通知、审计、Agent 编排以及 MySQL/Redis 集成。

## 架构

```text
app/
├── agents/       LangGraph 状态、意图识别和工作流
├── api/routes/   HTTP API 与依赖注入
├── core/         配置、安全、异常、日志和 Request ID
├── db/           MySQL、Redis、初始化、迁移与 Bootstrap
├── models/       SQLAlchemy Models
├── schemas/      Pydantic 请求/响应模型
├── services/     事务与业务规则
└── tools/        Agent 到 Service 的受控适配层
```

业务规则集中在 Service；Agent Tool 不绕过 Service 直接写数据库。MySQL 是业务事实源，Redis 保存聊天 Session 和 LangGraph Checkpoint。

## 环境要求

- Python 3.11
- MySQL 8.x
- Redis 8.x 或与当前客户端兼容的版本

## 本地安装

```bash
cd backend
python -m venv .venv
# 激活虚拟环境
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

修改本地 `.env`，配置 MySQL、Redis、JWT、CORS 和 LLM。不要提交 `.env`。

## 启动

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

本地 API 文档：

```text
http://127.0.0.1:8000/docs
```

## API 结构

- `/auth`：登录与修改密码
- `/users`：当前用户、管理员账号管理
- `/leave`：申请、查询、撤销、主管审批、HR 审批
- `/notifications`：通知、未读数和待办
- `/audit`：管理员只读审计查询
- `/chat`：Agent 与 Redis 会话
- `/health`：整体依赖状态
- `/ready`：数据库就绪状态

## Bootstrap Admin

Compose Fresh Install：

```bash
docker compose exec backend python -m app.db.create_admin
```

直接在已配置的后端环境中也可运行：

```bash
python -m app.db.create_admin
```

Bootstrap 仅允许在 `users=0` 时创建首个 `admin`。临时密码只显示一次，首次登录必须修改；数据库非空时命令拒绝执行。Bootstrap 不会自动运行，也没有固定默认密码。

开发环境的 `app.db.seed_users` 与生产 Bootstrap 是两个独立入口。Seed 只允许 `APP_ENV=development`，不要用它初始化部署环境。

## 数据库初始化与迁移

- Fresh Install：`app/db/init_mysql.sql`
- Existing Database：`app/db/migrations/`

初始化 SQL 只会在空 MySQL volume 首次启动时自动执行。已有数据库应先备份，再按版本审阅并执行迁移；不要通过删除 volume 代替迁移。

## Health

- `GET /health`：应用、MySQL、Redis 综合状态
- `GET /ready`：MySQL 核心依赖就绪状态
- `GET /health/redis`：Redis 专项状态

Redis 不可用时，传统 OA 业务可继续运行，聊天相关接口会不可用；MySQL 不可用时 Readiness 失败。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用 `requirements-dev.txt`，当前覆盖认证、RBAC、密码与 Token、用户管理、审计、Request ID、异常安全、依赖降级、Health 和 Bootstrap Admin。

## 安全注意事项

- 不要提交 `.env`、数据库 dump、日志、Token 或真实测试数据
- 不要在异常响应和结构化日志中记录请求体或凭据
- 公开问题报告只提供脱敏日志与 Request ID
- 公网部署前配置 HTTPS、强 Secret、访问控制和可恢复备份
