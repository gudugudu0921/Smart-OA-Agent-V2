# 项目截图

截图采集日期：2026-10-27。

| 文件 | 页面 |
| --- | --- |
| login.png | 登录 |
| agent.png | Agent 多轮请假对话 |
| manager-approval.png | 主管审批 |
| user-management.png | 管理员用户管理 |
| audit-log.png | 审计日志 |

这些图片由 Playwright 在 Edge 无头模式中采集，分辨率为 1600 × 1000。
页面来自本仓库真实的 Vue 生产构建，没有改写页面布局。
所有用户、申请、会话、对话和审计记录均为脚本内的虚构接口数据，未连接真实后端、数据库、Redis 或 LLM。
页面显示的“Redis 会话”和对话结果属于界面展示，不构成服务可用性或模型效果证明。
截图不包含真实密码、Token、邮箱、IP 或个人资料。

## 重新生成

1. 在 `frontend` 中运行 `pnpm install --frozen-lockfile` 和 `pnpm run build`。
2. 在独立工具目录安装 Playwright 及 Chromium（避免修改项目的前端依赖）：

   ```bash
   npm install --prefix /path/to/screenshot-tools playwright
   /path/to/screenshot-tools/node_modules/.bin/playwright install chromium
   ```

3. 从仓库根目录运行脚本，并通过 `PLAYWRIGHT_MODULE_PATH` 指定 Playwright 包的绝对路径：

   ```bash
   PLAYWRIGHT_MODULE_PATH=/path/to/screenshot-tools/node_modules/playwright node scripts/capture-screenshots.cjs
   ```

   PowerShell 示例（请替换工具目录）：

   ```powershell
   $env:PLAYWRIGHT_MODULE_PATH = 'C:\tools\screenshot-tools\node_modules\playwright'
   node scripts/capture-screenshots.cjs
   ```

   若使用已安装的 Edge，额外设置 `PLAYWRIGHT_CHANNEL=msedge`，即可省略 Chromium 下载步骤。

脚本启动临时本地静态服务，以隔离浏览器上下文拦截接口请求，并在结束后关闭浏览器和服务。
仅把虚构占位 Token 写入临时浏览器上下文，不读取本机浏览器配置、真实账号或环境文件。
不支持的接口或非预期外部网络请求会导致采集失败。

## 新截图检查

- 使用虚构姓名、部门、员工号和审批内容。
- 隐藏密码、Token、Cookie、API Key、请求头与本地路径。
- 避免真实邮箱、内网地址或模型供应商账号信息。
- 逐张检查最终图片后再提交，并在数据来源变化时更新此说明。
