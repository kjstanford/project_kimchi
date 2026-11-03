// End-to-end: a user in Chromium picks a config, previews the plan, runs a 3-device dry-run and sees the result.
// Run from the repo root:  node webapp/tests/e2e.mjs [python]   (starts its own server on port 8317)
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const PY = process.argv[2] || 'python';
const PORT = 8317;
const URL = `http://127.0.0.1:${PORT}/`;
const shot = process.env.E2E_SCREENSHOT;

const server = spawn(PY, ['-B', '-m', 'webapp', '--port', String(PORT)], { stdio: ['ignore', 'pipe', 'inherit'] });
const check = (ok, msg) => { if (!ok) throw new Error(`E2E FAILED: ${msg}`); console.log(`ok - ${msg}`); };

async function waitServer() {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${URL}api/health`)).ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error('server did not start');
}

let browser;
try {
  await waitServer();
  const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY, bypass: '127.0.0.1,localhost' } : undefined;
  browser = await chromium.launch(proxy ? { proxy } : {});
  const page = await (await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 1000 } })).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(URL);
  await page.waitForFunction(() => document.getElementById('cfg-select').value === 'lc500_idvg.json');
  check(/Dry-run only/.test(await page.textContent('#mode')), 'the page says it is dry-run only');
  check(await page.isDisabled('#opt-live'), 'the Live switch is disabled without --allow-live');

  await page.fill('#opt-limit', '3');
  await page.click('#btn-preview');
  await page.waitForFunction(() => /3 devices to visit/.test(document.getElementById('plan-summary').textContent));
  check(true, 'the preview plans 3 devices');
  check(!(await page.isDisabled('#btn-start')), 'Start is enabled after a valid preview');

  await page.click('#btn-start');
  await page.waitForFunction(() => document.getElementById('run-state').textContent === 'done', null, { timeout: 120000 });
  const nRows = await page.$$eval('#rows tr', (trs) => trs.length - 1);
  check(nRows === 3, `3 summary rows shown (got ${nRows})`);
  const statuses = await page.$$eval('#rows td.measured', (tds) => tds.map((t) => t.textContent));
  check(statuses.length === 3, 'all 3 devices measured');
  check(/Summary: /.test(await page.textContent('#log')), 'the log streamed to the end');
  const measuredOnMap = await page.evaluate(() => {
    const d = document.getElementById('map').data || [];
    const t = d.find((x) => x.name === 'measured');
    return t ? t.x.length : 0;
  });
  check(measuredOnMap === 3, `3 devices coloured measured on the map (got ${measuredOnMap})`);
  const latest = await page.evaluate(() => (document.getElementById('latest').data || []).length);
  check(latest >= 1, 'the latest device curve is drawn');

  await page.click('.tab[data-tab="data"]');
  await page.waitForFunction(() => [...document.querySelectorAll('#data-root option')].some((o) => o.value === '.ignore/web_dryrun'));
  check(true, 'the data tab lists the dry-run folder');
  if (shot) {
    await page.click('.tab[data-tab="run"]');
    await page.screenshot({ path: shot, fullPage: true });
  }
  check(errors.length === 0, `no page errors ${errors.join('; ')}`);
  console.log('E2E PASSED');
} finally {
  if (browser) await browser.close();
  server.kill();
}
