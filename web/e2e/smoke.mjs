// End-to-end smoke test: the built panel against the real server with sample data.
//   npm run build && npm run e2e
// Starts web/dev/demo_server.py on a free port, drives the system Chrome through the main
// flows of every page and fails on any assertion, uncaught page error or console error.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../..');
const python = process.env.ICTL_PYTHON || 'python3';
const webDir = process.env.ICTL_WEB_OUT || resolve(repo, 'server/web');

const freePort = () =>
  new Promise((done) => {
    const probe = createServer().listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => done(port));
    });
  });

async function startServer() {
  const port = await freePort();
  const child = spawn(python, [resolve(repo, 'web/dev/demo_server.py'), '--port', String(port), '--web', webDir], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
  let log = '';
  child.stderr.on('data', (chunk) => (log += chunk));
  const url = await new Promise((done, fail) => {
    const timer = setTimeout(() => fail(new Error('demo server did not start:\n' + log)), 30000);
    child.stdout.on('data', (chunk) => {
      const match = /PANEL (\S+)/.exec(String(chunk));
      if (match) {
        clearTimeout(timer);
        done(match[1]);
      }
    });
    child.on('exit', (code) => fail(new Error(`demo server exited (${code}):\n` + log)));
  });
  return { url, stop: () => child.kill() };
}

function assert(condition, message) {
  if (!condition) throw new Error('assertion failed: ' + message);
}

const server = await startServer();
// The system Chrome by default; ICTL_BROWSER_CHANNEL=bundled uses a browser installed by playwright-core.
const channel = process.env.ICTL_BROWSER_CHANNEL || 'chrome';
const browser = await chromium.launch({ channel: channel === 'bundled' ? undefined : channel, headless: true });
const problems = [];
let step = 'start';
try {
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
  page.setDefaultTimeout(10000);
  page.on('pageerror', (error) => problems.push(`[${step}] page error: ${error.message}`));
  page.on('console', (message) => {
    // Failed sign-in and the signed-out probe are expected to log a 401 resource error.
    if (message.type() === 'error' && !/status of 401/.test(message.text())) problems.push(`[${step}] console: ${message.text()}`);
  });
  const go = async (hash) => {
    await page.evaluate((h) => (location.hash = h), hash);
  };
  const heading = (name) => page.getByRole('heading', { name, level: 1 });

  step = 'static files';
  const root = await page.request.get(server.url);
  assert(root.status() === 200 && /script-src 'self'/.test(root.headers()['content-security-policy'] ?? ''), 'entry document with CSP');
  assert((await page.request.get(server.url + 'assets/../../etc/passwd')).status() === 404, 'no path traversal');
  assert((await page.request.get(server.url + 'nope.js')).status() === 404, 'unknown file is 404');

  step = 'sign in';
  await page.goto(server.url);
  await page.getByLabel('用户名').fill('admin');
  await page.getByLabel('密码').fill('wrong-password');
  await page.getByRole('button', { name: '登录' }).click();
  await page.getByRole('alert').waitFor();
  await page.getByLabel('密码').fill('ictl-demo-pass');
  await page.getByRole('button', { name: '登录' }).click();
  await heading('总览').waitFor();

  step = 'overview';
  await page.getByRole('heading', { name: '需要处理' }).waitFor();
  await page.getByRole('main').locator('a[href="#/nodes"]').first().click();
  await heading('节点').waitFor();

  step = 'nodes';
  await page.getByRole('link', { name: /香港入口/ }).click();
  await page.getByText('接入信息').waitFor();
  await page.getByText('转发规则').first().waitFor();
  await go('#/nodes');
  await page.getByPlaceholder('搜索名称或标签').fill('东京');
  await page.getByRole('link', { name: /东京中转/ }).waitFor();
  assert((await page.getByRole('link', { name: /香港入口/ }).count()) === 0, 'search filters the node list');
  await page.getByRole('button', { name: '清除', exact: true }).click();

  step = 'create node';
  await page.getByRole('button', { name: '新增节点' }).click();
  const nodeDialog = page.getByRole('dialog', { name: '新增节点' });
  await nodeDialog.getByLabel('名称', { exact: true }).fill('冒烟测试节点');
  await nodeDialog.getByLabel('IP 地址或域名').fill('192.0.2.200');
  await nodeDialog.getByRole('button', { name: '保存' }).click();
  await heading('冒烟测试节点').waitFor();
  await page.getByText('一键接入').first().waitFor();
  assert(/#\/nodes\/\d+$/.test(page.url()), 'new node opens its detail page');

  step = 'targets';
  await go('#/targets');
  await heading('目标').waitFor();
  await page.getByText('业务服务').first().waitFor();
  await page.getByRole('button', { name: '添加目标' }).click();
  const targetDialog = page.getByRole('dialog');
  await targetDialog.getByLabel(/^名称/).fill('冒烟测试目标');
  await targetDialog.getByPlaceholder('203.0.113.10').fill('198.51.100.200');
  await targetDialog.getByPlaceholder('443', { exact: true }).fill('8443');
  await targetDialog.locator('button[type="submit"]').click();
  await page.getByText('冒烟测试目标').first().waitFor();

  step = 'links';
  await go('#/routes');
  await heading('链路').waitFor();
  await page.getByRole('link', { name: /主线路/ }).first().click();
  await heading('主线路').waitFor();
  await page.getByText('东京中转').first().waitFor();
  await page.getByText('路径状态').waitFor();
  const linkId = /#\/routes\/(\d+)/.exec(page.url())[1];

  step = 'link editor';
  await page.getByRole('button', { name: '编辑' }).click();
  await page.getByRole('button', { name: '保存', exact: true }).waitFor();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '编辑' }).waitFor();

  step = 'quality';
  await go(`#/routes/${linkId}/quality`);
  await page.getByRole('heading', { name: /网络质量/ }).waitFor();
  await page.getByRole('tab', { name: '1 小时' }).click();

  step = 'configs';
  await go('#/configs');
  await heading('配置').waitFor();
  await page.getByText('配置 0001').first().waitFor();

  step = 'subscriptions';
  await go('#/subscriptions');
  await heading('订阅').waitFor();
  await page.getByText('手机客户端').first().waitFor();
  const subscription = await page.locator('input[readonly]').first().inputValue();
  assert(/\/sub\/[0-9a-f]{64}/.test(subscription), 'subscription URL is shown');
  const fetched = await page.request.get(subscription.replace(/^https?:\/\/[^/]+/, new URL(server.url).origin));
  assert(fetched.status() === 200 && (await fetched.text()).length > 20, 'subscription URL returns content');

  step = 'settings';
  await go('#/settings');
  await heading('设置').waitFor();
  await page.getByText(/0\.6\.\d/).first().waitFor();

  step = 'subaccount';
  await page.getByRole('button', { name: '新建子账户' }).click();
  const accountDialog = page.getByRole('dialog', { name: '新建子账户' });
  await accountDialog.getByLabel('用户名').fill('smoke-member');
  await accountDialog.getByRole('button', { name: '生成', exact: true }).click();
  assert((await accountDialog.getByLabel('密码', { exact: false }).inputValue()).length >= 16, 'password generation');
  await accountDialog.getByRole('button', { name: '隐藏', exact: true }).click();
  await accountDialog.getByLabel('密码').fill('smoke-member-password');
  const expiry = new Date();
  expiry.setDate(expiry.getDate() + 7);
  const localExpiry = new Date(expiry.getTime() - expiry.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  await accountDialog.getByLabel('到期时间').fill(localExpiry);
  await accountDialog.getByLabel('配置数量上限').fill('2');
  await accountDialog.getByRole('checkbox', { name: /香港入口/ }).check();
  await accountDialog.getByLabel('香港入口 流量额度（GB）').fill('10');
  await accountDialog.getByRole('checkbox', { name: /东京中转/ }).check();
  await accountDialog.getByLabel('隐藏 东京中转 地址', { exact: true }).check();
  await accountDialog.getByRole('button', { name: '保存' }).click();
  await page.getByText('smoke-member', { exact: true }).waitFor();
  const accounts = await (await page.request.get(server.url + 'api/accounts')).json();
  assert(accounts.accounts.find((a) => a.username === 'smoke-member')?.expires_at === Math.floor(expiry.getTime() / 1000),
    'account expiry is saved in epoch seconds');
  const member = await browser.newPage();
  try {
    await member.goto(server.url);
    await member.getByLabel('用户名').fill('smoke-member');
    await member.getByLabel('密码').fill('smoke-member-password');
    await member.getByRole('button', { name: '登录' }).click();
    await member.getByRole('heading', { name: '总览', level: 1 }).waitFor();
    await member.evaluate(() => (location.hash = '#/nodes'));
    await member.getByRole('link', { name: /香港入口/ }).click();
    await member.getByRole('heading', { name: '连接地址' }).waitFor();
    await member.getByText(/剩余 10 GB/).waitFor();
    assert(!(await member.getByText('接入信息').count()), 'shared node has read-only details');
    await member.evaluate(() => (location.hash = '#/nodes'));
    await member.getByRole('link', { name: /东京中转/ }).click();
    await member.getByText('地址已隐藏 · 仅中转').waitFor();
    assert(!(await member.getByText('192.0.2.2', { exact: true }).count()), 'relay IP is hidden');
    await member.evaluate(() => (location.hash = '#/targets'));
    await member.getByRole('heading', { name: '目标', level: 1 }).waitFor();
    const targets = await member.request.get(server.url + 'api/targets');
    assert(targets.ok() && (await targets.json()).targets.length === 0, 'subaccount has independent targets');
    await member.evaluate(() => (location.hash = '#/configs'));
    await member.getByText('0 / 2 项', { exact: true }).waitFor();
    await member.evaluate(() => (location.hash = '#/settings'));
    await member.getByRole('heading', { name: '账号', exact: true }).waitFor();
    assert(!(await member.getByRole('button', { name: '新建子账户' }).count()), 'subaccount cannot manage accounts');
    await member.evaluate(() => (location.hash = '#/routes/new'));
    await member.getByRole('heading', { name: '资源库' }).waitFor();
    await member.getByRole('tab', { name: '入口', exact: true }).click();
    assert(!(await member.getByRole('button', { name: /东京中转/ }).count()), 'private relay stays out of entry picker');
    await member.getByRole('tab', { name: '节点', exact: true }).click();
    await member.getByRole('button', { name: /东京中转/ }).click();
    await member.getByText('地址 1', { exact: true }).first().waitFor();
  } finally {
    await member.close();
  }

  step = 'unknown route';
  await go('#/does-not-exist');
  await heading('总览').waitFor();

  step = 'mobile layout';
  await page.setViewportSize({ width: 390, height: 800 });
  await go('#/routes');
  await heading('链路').waitFor();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(overflow <= 1, `no horizontal overflow on a phone (got ${overflow}px)`);
  await page.setViewportSize({ width: 1366, height: 900 });

  step = 'sign out';
  await page.getByRole('button', { name: '退出登录' }).first().click();
  await page.getByRole('button', { name: '登录' }).waitFor();
  assert((await page.request.get(server.url + 'api/nodes')).status() === 401, 'session ended on the server');
} catch (error) {
  problems.push(`[${step}] ${error.message.split('\n')[0]}`);
} finally {
  await browser.close();
  server.stop();
}
if (problems.length) {
  console.error('SMOKE TEST FAILED\n' + problems.join('\n'));
  process.exit(1);
}
console.log('smoke test passed');
