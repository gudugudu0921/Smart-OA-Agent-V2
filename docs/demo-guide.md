# Smart OA Agent V2 本地部署与开发指南

本指南用于 GitHub 下载者进行本地部署、功能体验和代码开发。所有示例账号、姓名、部门、日期和请假原因都应使用虚构数据。

## 1. 下载源码与前置要求

需要：

- Git，或能够下载并解压 ZIP
- Docker Desktop，且 Linux Container Engine 已运行
- 可访问 DeepSeek API 的网络环境
- 使用者自己的 DeepSeek API Key

完整 Compose 方式不要求宿主机安装 Python、Node.js、MySQL 或 Redis。

使用 Git：

```powershell
git clone https://github.com/gudugudu0921/Smart-OA-Agent-V2.git
cd Smart-OA-Agent-V2
```

如果下载 ZIP，请解压后进入包含 `compose.yml` 的项目根目录。

## 2. 先选择一种运行方式

不要把两种方式混在一起：

| 方式 | 适用场景 | MySQL / Redis |
| --- | --- | --- |
| 完整 Docker Compose | 最接近项目交付形态，推荐演示使用 | 仅在 Compose 网络内使用，不向宿主机发布数据库端口 |
| 本地 FastAPI + Vite | 调试 Python/Vue 代码 | 需要另外准备并核验可从 Windows 访问的 MySQL/Redis |

## 3. Windows 与 Docker Desktop

### 3.1 启动顺序

1. 启动 Docker Desktop。
2. 等待 Linux Engine 完全可用。
3. 在仓库根目录执行：

   ```powershell
   docker version
   docker compose version
   ```

4. 完成下一节的 `.env` 配置后，再检查并启动项目服务。

Docker 容器依赖 Docker Desktop 提供的 Linux VM/Engine。容器 restart policy 只能在 Engine 已启动后生效，不能自行启动 Docker Desktop。

标准 [`compose.yml`](../compose.yml) 当前使用 `restart: unless-stopped`。某些一次性或隔离测试容器可能使用 `restart=no`，Docker Desktop 或主机重启后不会自动恢复，应先用 `docker ps -a` 核对，不要擅自更改策略。

## 4. 推荐方式：完整 Docker Compose

### 4.1 复制根环境模板

在仓库根目录：

```powershell
Copy-Item .env.example .env
```

Linux/macOS：

```bash
cp .env.example .env
```

### 4.2 生成并填写本地密钥

根 `.env` 中需要确认以下变量：

- `MYSQL_USER`：默认可保留 `smart_oa`
- `MYSQL_PASSWORD`：MySQL 应用账号随机密码
- `MYSQL_ROOT_PASSWORD`：MySQL root 随机密码
- `REDIS_PASSWORD`：Redis 随机密码
- `JWT_SECRET_KEY`：至少 32 字符的随机 Secret
- `LLM_API_KEY`：使用者自己的 DeepSeek API Key

这些值不能使用仓库公开固定密码。Windows PowerShell 可先定义以下函数：

```powershell
function New-SmartOaSecret {
  $buffer = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
  [Convert]::ToBase64String($buffer)
}
```

执行 `New-SmartOaSecret` 四次，分别粘贴到 MySQL 应用密码、MySQL root 密码、Redis 密码和 JWT Secret。命令只输出到当前终端，不会写入仓库或项目日志。

Linux/macOS 可分别执行四次：

```bash
openssl rand -base64 32
```

不要提交 `.env`，不要把内容投屏、截图或粘贴到公开日志。项目 [.gitignore](../.gitignore) 已忽略 `.env`，但仍应在提交前主动核对。

### 4.3 配置 DeepSeek

根模板默认配置为项目真实联调使用过的：

```dotenv
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
```

只把自己的 Key 填入本地 `.env`。Base URL 和模型允许高级用户覆盖，但应使用本人账号实际可用的 DeepSeek 配置。Key 为空时，`docker compose config --quiet` 会在创建容器前直接失败。

如 8080 被占用，可同时修改 `APP_HTTP_PORT`；默认浏览器入口随之变化。

### 4.4 检查并启动

```powershell
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
```

当前标准 Compose：

- MySQL 使用 `mysql:8.4.11`
- Redis 使用 `redis:8.10.1-alpine`，开启 AOF
- Backend 在容器内监听 `8000`
- Frontend 使用 Nginx 对外发布 `${APP_HTTP_PORT:-8080}`
- `/api/` 由 Nginx 转发到 Backend
- MySQL、Redis 不发布宿主机端口

浏览器入口应以 `docker compose ps` 的实际映射为准。默认配置下为：

```text
http://localhost:8080
```

健康检查：

```text
http://localhost:8080/api/health
http://localhost:8080/api/ready
```

`/health` 同时报告 MySQL 和 Redis；`/ready` 以 MySQL 是否可用决定是否就绪。

它们不会请求 DeepSeek，也不证明模型账号、余额或模型名可用。完成账号首次改密后，执行一条只读自然语言查询才是模型验证；该操作可能消耗 API 额度。

### 4.5 Fresh Install 与演示账号：二选一

`backend/app/db/init_mysql.sql` 只在全新空 MySQL volume 第一次启动时，由 MySQL 官方入口自动执行。它已经包含当前五张业务表，无需再执行 migration，但不会创建默认登录账号。

**路径 A：快速本地演示。** 仅用于全新、独立、可丢弃的本地 Compose 演示库：

```powershell
docker compose exec -e APP_ENV=development backend python -m app.db.seed_users
```

这是现有 seed 工具的单次进程环境覆盖，不会改变正在运行的 Backend 容器配置。它写入虚构 admin、manager、hr 和 employee，建立员工—主管关系，并一次性显示随机临时密码。重复运行不会重复创建账号，也不会覆盖已有账号密码。

安全保存临时密码，各账号首次登录时必须修改。不要把密码提交到 GitHub，也不要在已有业务数据库或来源不明的 volume 上运行。

**路径 B：从单个管理员开始。** 在 `users=0` 的 Fresh Install 中运行：

```powershell
docker compose exec backend python -m app.db.create_admin
```

限制：

- 只允许 `users=0`
- 角色固定为 `admin`
- 临时密码只显示一次
- 首次登录必须修改密码
- 数据库已有任意用户时命令拒绝执行

管理员改密后，可在用户管理页面创建 manager、hr 和 employee 虚构账号，并为 employee 选择直属主管。系统会为新用户生成一次性临时密码；不要把密码写入文档或截图。

路径 A 已经包含管理员，普通演示用户不应再运行路径 B；两条路径不要同时执行。

### 4.6 已有数据库与 volume

Compose 不会自动迁移已有 volume。项目没有通用迁移框架或自动升级命令。已有数据库必须：

1. 明确核对目标实例和数据库。
2. 在受控环境中备份。
3. 按版本审阅 [`backend/app/db/migrations/`](../backend/app/db/migrations/) 中的 SQL。
4. 使用已确认的 MySQL 客户端和账号执行所需迁移。

不要通过删除 volume 代替迁移；不要在包含重要数据的环境执行 `docker compose down -v`。

## 5. 本地开发：FastAPI + Vite

### 5.1 数据库与 Redis 前提

标准 Compose 没有把 MySQL/Redis 端口发布到 Windows，所以不能仅运行 `docker compose up mysql redis`，然后假设本地 FastAPI 能访问 `127.0.0.1:3306/6379`。

本地 FastAPI 需要另外准备可访问的开发实例，并在任何初始化或写操作前核对：

- 实际 host 和当前端口
- MySQL server UUID / hostname
- 当前 database 和 current user
- Redis endpoint 与 PING
- 该实例是否确实为可写的个人演示环境

MySQL 可使用只读身份查询确认目标：

```sql
SELECT @@server_uuid, @@hostname, DATABASE(), CURRENT_USER();
```

不要复用来源不明、用途不明或未经确认的业务数据库。动态映射端口必须查询当前实例，不能把历史端口写死。

### 5.2 后端环境

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

在 `backend/.env` 中显式配置已核验的 MySQL/Redis、CORS、JWT 与 LLM。`Settings` 会从当前进程环境或 `.env` 读取 `MYSQL_HOST`、`MYSQL_PORT` 等离散字段；项目不使用通用 `DATABASE_URL` 配置。

空开发数据库可审阅并手动应用 [`backend/app/db/init_mysql.sql`](../backend/app/db/init_mysql.sql)。该脚本固定创建 `smart_oa`，因此执行前必须确认这正是你的隔离开发目标。项目没有安全的“任意数据库自动初始化器”。

启动 FastAPI：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

检查：

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/ready
http://127.0.0.1:8000/docs
```

### 5.3 开发账号

仅在 `APP_ENV=development` 且已确认使用隔离演示数据库时，可以运行：

```powershell
python -m app.db.seed_users
```

该模块按当前代码准备 admin、manager、hr 和 employee 虚构账号，随机临时密码只显示一次，已有账号密码不会被覆盖。所有新账号都需要首次改密。不要在 production 环境或未知数据库运行；代码会拒绝非 development 环境。

另一种方式是运行 `python -m app.db.create_admin` 创建空库首个管理员，再通过页面创建其他角色。

### 5.4 前端环境

新终端中执行：

```powershell
cd frontend
pnpm install --frozen-lockfile
Copy-Item .env.example .env
pnpm run dev --host 127.0.0.1 --port 5173
```

`frontend/.env.example` 把 `VITE_API_BASE_URL` 指向 `http://127.0.0.1:8000`。Vite 配置没有开发代理；如后端换端口，必须同步修改该变量并确认后端 `CORS_ORIGINS` 允许实际前端 Origin。

访问：

```text
http://127.0.0.1:5173
```

## 6. 演示账号与完整业务体验

建议准备完全虚构的：

- 一名 manager
- 一名直属于该 manager 的 employee
- 一名 hr
- 两条日期互不冲突的未来请假：一条用于完整审批，一条用于撤销

账号必须已完成首次改密，否则业务接口会返回 `PASSWORD_CHANGE_REQUIRED`。不要把密码、Token、真实邮箱、API Key 或数据库地址写入演示稿。

建议体验路线：

1. 使用虚构 employee 登录，按提示修改临时密码。
2. 员工可在左侧“我的请假”进入传统页面，点击右上角“新建请假”；也可在 Agent 中先发送只读查询，例如“查询我的请假”，确认真实 DeepSeek 调用可用。
3. 员工用未来日期提交一条虚构请假；使用 Agent 时需明确确认写操作，使用传统页面时直接调用 REST API。
4. 使用直属 manager 登录并完成首次改密，在主管审批页面查看通知和待办后进行审批。
5. 使用 hr 登录并完成首次改密，在 HR 审批页面查看待终审通知并完成终审。
6. 回到 employee 查看最终状态和两级审批通知。

真实聊天会消耗 API 额度；不要重复确认、点击重试或为了演示自动生成新的 `operation_id`。

## 7. 演示前检查清单

- [ ] Docker Desktop Linux Engine 已运行
- [ ] `docker compose ps` 或本地进程显示且仅显示预期服务
- [ ] 当前 MySQL/Redis 身份与端点已核验
- [ ] `/health` 与 `/ready` 正常
- [ ] 前端能访问实际 API Base URL
- [ ] employee / manager / hr 虚构账号已准备并完成首次改密
- [ ] employee 的 `manager_id` 指向演示主管
- [ ] LLM Provider、模型和余额/额度已确认
- [ ] 使用未来日期和虚构原因
- [ ] 浏览器中没有其他真实账号或敏感标签页
- [ ] 不会执行重复确认或盲目更换 `operation_id`

## 8. 常见问题

### Docker 命令提示无法连接 Engine

先打开 Docker Desktop，等待 Linux Engine ready。容器重启策略不能启动 Desktop 本身。

### 容器存在但没有运行

执行 `docker compose ps -a` 核对服务和退出原因。标准 Compose 是 `unless-stopped`；手动停止过的服务或 `restart=no` 的隔离容器不会自动恢复。不要在不明容器上修改策略。

### 8080 被占用

修改根 `.env` 的 `APP_HTTP_PORT`，重新启动 Compose，并以 `docker compose ps` 为准。不要假设历史端口仍有效。

### `docker compose config --quiet` 提示缺少 LLM_API_KEY

确认已经复制根 `.env.example` 为 `.env`，并在本地 `.env` 填入自己的 DeepSeek API Key。不要把 Key 写入 `compose.yml`、README 或提交记录。

### 本地前端能打开但 API 失败

核对 `VITE_API_BASE_URL`、FastAPI 端口和 `CORS_ORIGINS`。修改前端环境变量后需要重新启动 Vite。

### `/health` degraded，但 `/ready` 正常

通常表示 Redis 不可用、MySQL 仍正常。项目已在独立环境用真实不可达 Redis 端点验证：Chat 返回 `503`，`/health/redis` 返回 `503`，而登录、请假创建/查询/撤销、主管审批、HR 终审和通知等传统 REST 操作仍可用。该结果只覆盖 DNS/连接不可达，不代表 Redis 恢复、网络抖动或部分丢包也已验证。

### `/ready` 返回 503

先停止业务演示，核对 MySQL 地址、端口、账号、数据库和目标实例；不要直接运行初始化脚本试错。

### Agent 返回 LLM 未配置或模型不可用

核对 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，确认当前 DeepSeek 账号可访问所选模型且额度可用，但不要在日志或屏幕共享中展示 Key。`/health` 正常不代表外部模型调用成功；Provider 不可用时不要把失败描述为 Tool 或数据库成功。

### Fresh Install 没有创建表或数据库初始化失败

先查看 `docker compose ps` 和 MySQL 容器日志，确认这是全新空 volume，并检查根 `.env` 中 MySQL 三个变量。`init_mysql.sql` 只在 MySQL 数据目录为空时执行；不要在不明数据上反复初始化或直接删除 volume。

### 演示账号无法登录

确认使用的是 seed/bootstrap 当次输出的临时密码，并按页面要求先完成首次改密。重复运行 seed 不会覆盖已有账号密码；如果密码已经丢失，应使用管理员页面的受控密码重置功能，而不是重新 seed。

### 重新下载源码后仍看到旧数据

Compose project 名为 `smart-oa`，同一 Docker 主机上已有的 named volume 会继续保留数据。Fresh Install 指“新的空 volume”，不是“新的源码目录”。不要把 `docker compose down -v` 当作常规修复；只有明确确认当前演示数据可全部丢弃时，才应单独规划重建。

### 确认后结果不明确

停止重复点击，保留原 `operation_id` 和 Request ID，通过现有 Operation 核查入口确认状态。不要自动生成新 ID 重试。

## 9. 停止与再次启动

标准 Compose 正常停止：

```powershell
docker compose down
```

这会停止并移除当前项目容器和网络，但不会删除 named volume。再次启动并复用数据：

```powershell
docker compose up -d --wait
```

除非你明确要删除整个演示环境且已确认数据可丢弃，否则不要使用 `-v`；不要清理其他项目的容器、网络或卷。

返回 [README](../README.md)。
