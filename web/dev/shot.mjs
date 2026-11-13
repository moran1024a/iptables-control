// Take screenshots of the running demo panel with the system Chrome.
//   node web/dev/shot.mjs <panel-url> <out-dir> <name>=<hash>[@WxH][!dark] ...
// Example: node web/dev/shot.mjs http://127.0.0.1:8731/abc/ /tmp/shots nodes=#/nodes mobile=#/nodes@390x800 dark=#/nodes!dark
// Optional env: ICTL_CLICK (selector to click before the shot), ICTL_FULL=1 (full page), ICTL_WAIT (ms).
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';

const [base, out, ...shots] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const page = await context.newPage();
const errors = [];
let current = '';
page.on('console', (m) => m.type() === 'error' && !m.text().includes('401') && errors.push(`${current}: ${m.text()}`));
page.on('pageerror', (e) => errors.push(`${current}: ${e.message}`));
await page.goto(base);
await page.waitForSelector('input[autocomplete="username"], nav[aria-label="主导航"]');
if (await page.locator('input[autocomplete="username"]').isVisible()) {
  await page.locator('input[autocomplete="username"]').fill(process.env.ICTL_USER || 'admin');
  await page.locator('input[type="password"]').fill(process.env.ICTL_PASS || 'ictl-demo-pass');
  await page.locator('button[type="submit"]').click();
  await page.waitForSelector('nav[aria-label="主导航"]', { state: 'attached' });
}
for (const spec of shots) {
  const dark = spec.endsWith('!dark');
  const [name, rest] = spec.replace(/!dark$/, '').split('=');
  const [hash, size = '1440x900'] = rest.split('@');
  const [width, height] = size.split('x').map(Number);
  current = name;
  await page.setViewportSize({ width, height });
  await page.evaluate((d) => (document.documentElement.dataset.theme = d ? 'dark' : 'light'), dark);
  await page.evaluate((h) => (location.hash = h), hash);
  await page.mouse.move(2, 2);
  await page.waitForTimeout(Number(process.env.ICTL_WAIT || 900));
  if (process.env.ICTL_CLICK) {
    await page.locator(process.env.ICTL_CLICK).first().click();
    await page.waitForTimeout(500);
  }
  await page.evaluate((d) => (document.documentElement.dataset.theme = d ? 'dark' : 'light'), dark);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: process.env.ICTL_FULL === '1' });
}
await browser.close();
console.log(errors.length ? 'CONSOLE ERRORS:\n' + errors.join('\n') : 'ok, no console errors');
