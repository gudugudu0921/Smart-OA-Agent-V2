// Render the real production UI with fictional, intercepted API responses.
// Requires Playwright + Chromium installed separately. No live backend is used.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');

const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'frontend', 'dist');
const output = path.join(root, 'docs', 'screenshots');
const stamp = '2026-10-27T09:30:00';
const applicationId = '11111111-1111-4111-8111-111111111111';
const names = { employee: '演示员工', manager: '演示主管', hr: '演示HR', admin: '演示管理员' };
const user = (role, id) => ({ id, employee_no: `DEMO00${id}`, username: `demo_${role}`,
  name: names[role], role, department: '演示部门', position: '演示岗位',
  manager_id: role === 'employee' ? 2 : null, is_active: true,
  must_change_password: false, email: null, created_at: stamp, updated_at: stamp });
const users = [user('employee', 1), user('manager', 2), user('hr', 3), user('admin', 4)];
const leave = { id: applicationId, employee_id: 1, leave_type: 'annual',
  start_date: '2026-11-02', end_date: '2026-11-03', leave_days: 2,
  reason: '虚构演示数据', status: 'pending', manager_status: 'pending',
  manager_id: 2, hr_status: 'pending', submitted_at: stamp, created_at: stamp, updated_at: stamp };
const session = { thread_id: 'demo-session', title: '年假申请演示',
  last_message_preview: '请补充开始日期和结束日期。', updated_at: stamp };
const messages = [
  { role: 'user', content: '我想申请年假。' },
  { role: 'assistant', content: '请补充开始日期、结束日期。' },
  { role: 'user', content: '2026年11月2日到11月3日，共两天。' },
  { role: 'assistant', content: `请假申请已提交成功，申请编号为 ${applicationId}，当前状态为等待主管审批。` },
];

(async () => {
  await fs.access(path.join(dist, 'index.html'));
  await fs.mkdir(output, { recursive: true });
  const server = http.createServer(async (req, res) => {
    try {
      const relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
      let target = path.resolve(dist, `.${relative}`);
      if (!target.startsWith(dist + path.sep)) target = path.join(dist, 'index.html');
      try { if (!(await fs.stat(target)).isFile()) throw new Error('SPA route'); }
      catch { target = path.join(dist, 'index.html'); }
      const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
      res.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
      res.end(await fs.readFile(target));
    } catch { res.writeHead(500); res.end('Static preview failed'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const [filename, routePath, role, ready] of [
      ['login.png', '/login', null, '用户名'],
      ['agent.png', '/agent', 'employee', '请假申请已提交成功'],
      ['manager-approval.png', '/manager/approvals', 'manager', '处理直属员工申请'],
      ['user-management.png', '/admin/users', 'admin', 'demo_employee'],
      ['audit-log.png', '/admin/audit', 'admin', 'CREATE_LEAVE'],
    ]) {
      const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      if (role) await context.addInitScript(() => localStorage.setItem('smart_oa_access_token', 'fictional-screenshot-token'));
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        const isApi = url.pathname.startsWith('/api/') || url.port === '8000';
        if (!isApi && url.origin === base) return route.continue();
        if (!isApi) { errors.push(`Unexpected network destination: ${url.origin}`); return route.abort(); }
        const endpoint = url.pathname.replace(/^\/api/, '').replace(/\/$/, '');
        const replies = {
          '/users/me': users.find(item => item.role === role), '/users': users,
          '/notifications': [], '/notifications/unread-count': { unread_count: 0 },
          '/notifications/todos': { role, total: role === 'manager' ? 1 : 0, items: { manager_approvals: 1, hr_approvals: 0 } },
          '/leave/pending/manager': [leave], '/leave/team': [leave],
          '/chat/sessions': { sessions: [session] }, '/chat/sessions/demo-session': session,
          '/chat/sessions/demo-session/messages': { messages },
          '/audit/logs': ['CREATE_LEAVE', 'MANAGER_APPROVE_LEAVE', 'HR_APPROVE_LEAVE'].map((action, index) => ({
            id: index + 1, user_id: index + 1, name: users[index].name,
            employee_no: users[index].employee_no, action, resource_type: 'leave_application',
            resource_id: applicationId, request_id: `demo-request-${index + 1}`,
            ip_address: null, created_at: stamp, detail: { source: 'fictional screenshot fixture' },
          })),
        };
        if (!(endpoint in replies)) { errors.push(`Unexpected API: ${endpoint}`); return route.abort(); }
        return route.fulfill({ json: replies[endpoint], headers: { 'access-control-allow-origin': '*' } });
      });
      await page.goto(base + routePath, { waitUntil: 'networkidle' });
      await page.getByText(ready, { exact: false }).and(page.locator(':visible')).first().waitFor();
      await page.evaluate(() => document.fonts.ready);
      if (errors.length) throw new Error(errors.join('\n'));
      await page.screenshot({ path: path.join(output, filename), fullPage: true });
      console.log(`Captured ${filename}`);
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
