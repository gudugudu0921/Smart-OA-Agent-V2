# 更新记录

## [Unreleased]

首个公开版本准备中，尚未创建 GitHub Release 或版本标签。

### 功能

- FastAPI + Vue 3 的智能 OA 请假审批系统。
- 员工、主管、HR、管理员四角色权限及用户管理。
- 临时密码、首次登录改密、密码变更后旧 Token 失效。
- 员工申请与撤销、主管初审、HR 终审、通知与待办。
- LangGraph Agent、多轮参数收集、Redis 会话与 Checkpoint。
- 审计日志、Request ID、结构化日志及依赖降级处理。
- MySQL 初始化与迁移、Docker Compose、首个管理员引导。

### 发布配套

- MIT 许可证、安全说明、贡献指南、Issue 与 PR 模板。
- GitHub Actions：后端测试、前端构建、Compose 启动与入口健康检查。
- 数据库支持的审批流程回归测试，以及无需真实模型调用的 Agent 多轮测试。

### 已知限制

- 默认部署使用 HTTP；公网部署需额外配置 HTTPS。
- 浏览器 Token 使用 localStorage，尚未完成生产环境安全加固。
- 外部模型质量、费用、可用性和数据处理策略由模型供应商决定。
- 尚未完成高并发压测、集中日志及前端主 bundle 优化。
