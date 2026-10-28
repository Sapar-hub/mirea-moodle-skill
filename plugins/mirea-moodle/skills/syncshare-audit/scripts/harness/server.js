// Local mock for the SyncShare audit harness.
//
// Serves saved Moodle pages on http://127.0.0.1:$PAGE_PORT and impersonates
// syncshare.ru over HTTPS on 127.0.0.1:$API_PORT (mapped by the runner via
// --host-resolver-rules). Every request that reaches the HTTPS side is logged
// to $CAPTURE as JSONL; the audit CLI extracts questionIdentity from it.
//
// The mock never talks to production. It only answers the extension locally.

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');

const PAGE_DIR = process.env.PAGE_DIR || path.join(__dirname, 'pages');
const VIEW_DIR = process.env.VIEW_DIR || path.join(__dirname, 'view');
const MOODLE_ID = process.env.MOODLE_ID || '1';
const CAPTURE = process.env.CAPTURE || path.join(__dirname, 'capture.jsonl');
const CERT = process.env.CERT || path.join(__dirname, 'cert.pem');
const KEY = process.env.KEY || path.join(__dirname, 'key.pem');
const PAGE_PORT = parseInt(process.env.PAGE_PORT || '8081', 10);
const API_PORT = parseInt(process.env.API_PORT || '8443', 10);

fs.mkdirSync(path.dirname(CAPTURE), { recursive: true });
fs.writeFileSync(CAPTURE, '');

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function inject(html, q) {
  const cmid = q.get('cmid') || q.get('id') || '';
  const course = q.get('course') || q.get('courseId') || '1';
  const qname = escapeHtml(q.get('qname') || q.get('quizName') || 'Quiz');
  const cname = escapeHtml(q.get('cname') || q.get('courseName') || 'Course');

  // A review page and an attempt page contain the same questions and options.
  // Serving review HTML as an attempt makes the client use the per-question
  // solution path instead of the single quiz/review POST, which is more
  // reliable in the mock and produces the same identity hash. The review POST
  // path remains available via ?as=review for comparison.
  if (q.get('as') === 'attempt') {
    html = html.replace(/page-mod-quiz-review/g, 'page-mod-quiz-attempt');
  }

  const crumb =
    `<div class="breadcrumb">` +
    `<a href="/course/view.php?id=${escapeHtml(course)}" title="${cname}">${cname}</a>` +
    `<a href="/mod/quiz/view.php?id=${escapeHtml(cmid)}" title="${qname}">${qname}</a>` +
    `</div>`;
  return html.replace(/<body([^>]*)>/i, `<body$1 data-userid="${escapeHtml(MOODLE_ID)}">${crumb}`);
}

const pageServer = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  if (u.pathname === '/__health') {
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify({
      ok: true, pageDir: PAGE_DIR, viewDir: VIEW_DIR,
      moodleId: MOODLE_ID, capture: CAPTURE,
    }));
  }
  if (u.pathname.endsWith('/mod/quiz/view.php')) {
    const cmid = u.searchParams.get('id') || u.searchParams.get('cmid') || '';
    const file = path.join(VIEW_DIR, `${cmid}.html`);
    if (!cmid || !fs.existsSync(file)) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      return res.end(`no saved view page: ${file}`);
    }
    const html = inject(fs.readFileSync(file, 'utf8'), u.searchParams);
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    return res.end(html);
  }
  if (u.pathname.endsWith('/mod/quiz/review.php') || u.pathname.endsWith('/mod/quiz/attempt.php')) {
    const cmid = u.searchParams.get('cmid') || '';
    const attempt = u.searchParams.get('attempt') || '';
    const page = u.searchParams.get('page') || '0';
    const file = path.join(PAGE_DIR, cmid, `${attempt}_p${page}.html`);
    if (!cmid || !attempt || !fs.existsSync(file)) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      return res.end(`no saved page: ${file}`);
    }
    const html = inject(fs.readFileSync(file, 'utf8'), u.searchParams);
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    return res.end(html);
  }
  res.writeHead(404, { 'content-type': 'text/plain' });
  res.end('not found');
}).listen(PAGE_PORT, '127.0.0.1');

function record(req, body) {
  const entry = {
    ts: new Date().toISOString(),
    method: req.method,
    url: req.url,
    headers: req.headers,
    body: body || null,
  };
  fs.appendFileSync(CAPTURE, JSON.stringify(entry) + '\n');
  console.log(`[CAPTURE] ${req.method} https://syncshare.ru${req.url}`);
}

const apiServer = https.createServer(
  { key: fs.readFileSync(KEY), cert: fs.readFileSync(CERT) },
  (req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      record(req, body || null);
      const p = new URL(req.url, 'https://syncshare.ru').pathname;
      const json = (code, obj) => {
        res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify(obj));
      };
      // The extension treats any 2xx as success and ignores the body. Returning
      // 200 here lets the write path complete locally instead of looking like a
      // failed request in the extension's log.
      if (req.method === 'POST') {
        if (p === '/api/v2/quiz/attempt') return json(200, { ok: true });
        if (p === '/api/v2/quiz/review') return json(200, { ok: true });
        if (p === '/api/v2/quiz/attempt/summary') return json(200, { ok: true });
        if (p === '/api/v2/quiz/solve') return json(200, []);
        return json(404, { message: 'Not Found', statusCode: 404 });
      }
      if (p === '/api/v2/quiz/solution') return json(200, []);
      if (p === '/api/v2/quiz/completion') return json(200, []);
      if (p === '/api/v2/status') return json(200, { maintenance: false, highDemand: false });
      if (p === '/api/v2/update') return json(200, { updateRequired: false, latestVersion: '2.19.0' });
      if (p === '/api/v2/account/user') {
        if (req.headers['x-api-key']) {
          return json(200, {
            username: 'tester', plan: 1,
            expire_at: Math.floor(Date.now() / 1000) + 3600, ai_enabled: false,
          });
        }
        return json(401, { detail: 'Authentication credentials were not provided.' });
      }
      if (p.includes('/ofrep/')) return json(200, { flags: [] });
      return json(404, { message: 'Not Found', statusCode: 404 });
    });
  }
).listen(API_PORT, '127.0.0.1');

console.log(`page server: http://127.0.0.1:${PAGE_PORT} (PAGE_DIR=${PAGE_DIR})`);
console.log(`api mock:    https://syncshare.ru:${API_PORT} (capture=${CAPTURE})`);

function shutdown() {
  pageServer.close();
  apiServer.close();
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
