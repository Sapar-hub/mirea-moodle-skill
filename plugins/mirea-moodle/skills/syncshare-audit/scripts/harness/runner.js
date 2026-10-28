// Playwright runner for the SyncShare audit harness.
//
// Loads saved Moodle pages in headed Chrome with the unpacked SyncShare
// extension. All requests except the local page server and the mocked
// syncshare.ru are aborted, so the extension cannot reach production.
//
// Arguments: one URL per saved page, e.g.
//   http://127.0.0.1:8081/mod/quiz/review.php?attempt=1&cmid=2&page=0

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const EXT = process.env.SYNC_EXT_DIR;
const CHROME = process.env.CHROME_PATH;
const CAPTURE = process.env.CAPTURE || path.join(__dirname, 'capture.jsonl');
const PAGE_PORT = process.env.PAGE_PORT || '8081';
const API_PORT = process.env.API_PORT || '8443';
const PROFILE = path.join(path.dirname(CAPTURE), 'profile-' + Date.now());

if (!EXT || !CHROME) {
  console.error('FATAL: SYNC_EXT_DIR and CHROME_PATH are required (run scripts/setup.sh)');
  process.exit(2);
}

const targets = process.argv.slice(2);
if (!targets.length) {
  console.error('usage: node runner.js <url> [url ...]');
  process.exit(2);
}

function captureCount() {
  try {
    return fs
      .readFileSync(CAPTURE, 'utf8')
      .split('\n')
      .filter((l) => /\/api\/v2\/quiz\/(solution|review|attempt|solve)/.test(l)).length;
  } catch {
    return 0;
  }
}

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: CHROME,
    headless: false,
    viewport: { width: 1280, height: 900 },
    args: [
      `--disable-extensions-except=${EXT}`,
      `--load-extension=${EXT}`,
      `--host-resolver-rules=MAP syncshare.ru 127.0.0.1:${API_PORT}`,
      '--ignore-certificate-errors',
      '--no-sandbox',
      '--no-first-run',
    ],
  });

  let sw = ctx.serviceWorkers()[0];
  if (!sw) sw = await ctx.waitForEvent('serviceworker', { timeout: 20000 });

  await ctx.route('**/*', (route) => {
    const u = route.request().url();
    if (u.startsWith(`http://127.0.0.1:${PAGE_PORT}`) ||
        u.startsWith('https://syncshare.ru') ||
        u.startsWith('chrome-extension://')) {
      return route.continue();
    }
    return route.abort();
  });

  for (const url of targets) {
    const parsed = new URL(url);
    const q = parsed.searchParams;
    const goal = (q.get('as') || '').toLowerCase();
    const isAttempt = goal === 'attempt' || parsed.pathname.endsWith('/mod/quiz/attempt.php');
    const isSummary = parsed.pathname.endsWith('/mod/quiz/view.php');
    const label = `${q.get('cmid') || q.get('id')}/${q.get('attempt') || q.get('attemptId')} ` +
      `p${q.get('page') || '0'} ${isAttempt ? 'attempt' : isSummary ? 'summary' : 'review'}`;
    let lastDelta = 0;

    // Up to three loads: Chrome/extension startup can still be flaky (Moodle's
    // own JS errors under aborted external assets), so a reload is cheap
    // insurance and never touches production.
    for (let attempt = 1; attempt <= 3; attempt++) {
      const before = captureCount();
      const page = await ctx.newPage();
      await page.bringToFront();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message.slice(0, 80)));
      if (process.env.DEBUG_HARNESS) {
        page.on('console', (m) => {
          console.log(`    [console:${m.type()}] ${m.text().slice(0, 300)}`);
        });
      }
      await page.goto(url, { waitUntil: 'load', timeout: 40000 });
      await page.waitForTimeout(12000);

      const info = await page.evaluate(() => {
        const que = [...document.querySelectorAll('.que')];
        const types = [...new Set(que.map((el) => el.className.split(/\s+/)[1] || '?'))];
        return { n: que.length, types };
      });

      // The attempt payload is published by the content script on
      // `beforeunload`, not on load. Closing the page normally does not fire
      // the handler under Playwright, so dispatch it explicitly and give the
      // background worker time to POST.
      if (isAttempt) {
        await page.evaluate(() => window.dispatchEvent(new Event('beforeunload')));
        await page.waitForTimeout(3000);
      }

      const delta = captureCount() - before;
      lastDelta = delta;
      console.log(
        `${label} que=${info.n} type=${info.types.join(',') || '-'} ` +
        `captured=${delta > 0 ? '+' + delta : '0'} load#${attempt}` +
        (errors.length && !delta ? ` ERR:${errors[0]}` : '')
      );
      await page.close();
      if (delta > 0) break;
      // A view page has no `.que`; retrying is still useful if the summary
      // payload did not arrive. For attempt/review, an essay-only page sends
      // nothing by design, so do not burn another load.
      if (!isSummary && (!info.n || info.types.every((t) => t === 'essay'))) break;
    }
    if (lastDelta === 0) console.log(`${label} WARN: no requests captured`);
  }

  await ctx.close();
  try {
    fs.rmSync(PROFILE, { recursive: true, force: true });
  } catch {
    // The profile is disposable; a locked file must not fail the audit.
  }
})().catch((e) => {
  console.error('FATAL', e);
  process.exit(1);
});
