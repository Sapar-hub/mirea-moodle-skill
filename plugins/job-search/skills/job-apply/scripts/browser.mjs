#!/usr/bin/env node
// Управление настоящим Chromium через CDP: агент читает страницу текстом и жмёт по ref.
// Браузер живёт между командами (профиль с куками в $JOB_APPLY_HOME/profile).
//
//   browser.mjs start [--headless] [--direct]   запустить (--direct: мимо прокси из env)
//   browser.mjs stop
//   browser.mjs goto <url>
//   browser.mjs live <url>          вакансия ещё открыта? LIVE | CLOSED | UNSURE + улики
//   browser.mjs snap                интерактивные элементы: ref, роль, подпись, значение
//   browser.mjs text [maxChars]     видимый текст страницы
//   browser.mjs click <ref>
//   browser.mjs antselect <ref> <text>  выпадашка Ant Design: открыть, (поиск), выбрать пункт
//   browser.mjs pick <text>        клик по видимому элементу с этим текстом (выпадашки из div без ролей)
//   browser.mjs fill <ref> <text>   (--type: печатать посимвольно, для полей с маской)
//   browser.mjs select <ref> <option label|value>
//   browser.mjs check <ref> | uncheck <ref>
//   browser.mjs upload <ref> <file>
//   browser.mjs press <key>         Enter, Tab, Escape…
//   browser.mjs tabs | tab <n>      вкладки / переключиться
//   browser.mjs shot [file.png]     скриншот (по умолчанию $JOB_APPLY_HOME/shot.png)
//   browser.mjs back | wait <ms>
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, existsSync, openSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const HOME = process.env.JOB_APPLY_HOME || join(homedir(), '.local/share/job-apply');
const PORT = Number(process.env.JOB_APPLY_PORT || 9333);
const CDP = `http://127.0.0.1:${PORT}`;
const TAB_FILE = join(HOME, 'tab');

const die = (m) => { console.error(`ERR: ${m}`); process.exit(1); };

// Выполняется в каждом фрейме: размечает элементы data-jref и возвращает их описание.
const SNAP_JS = (prefix) => {
  document.querySelectorAll('[data-jref]').forEach((e) => e.removeAttribute('data-jref'));
  const SEL = 'a[href],button,input,textarea,select,[role=button],[role=link],[role=checkbox],[role=radio],' +
    '[role=combobox],[role=option],[role=tab],[role=menuitem],[role=switch],[contenteditable=true],summary';
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => {
    const by = el.getAttribute('aria-labelledby');
    return clean(el.getAttribute('aria-label') ||
      (by && by.split(' ').map((id) => document.getElementById(id)?.innerText).join(' ')) ||
      (el.labels && el.labels[0]?.innerText) || el.closest('label')?.innerText ||
      el.getAttribute('placeholder') || el.innerText || el.value || el.getAttribute('title') ||
      el.getAttribute('name') || el.getAttribute('alt'));
  };
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const t = (el.getAttribute('type') || '').toLowerCase();
    // file/checkbox/radio часто спрятаны под кастомной вёрсткой — их берём всегда
    if (!visible(el) && !['file', 'checkbox', 'radio'].includes(t)) continue;
    if (el.disabled || t === 'hidden') continue;
    const ref = `${prefix}e${++n}`;
    el.setAttribute('data-jref', ref);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'input' ? (t || 'text') : tag === 'a' ? 'link' : tag);
    let extra = '';
    if (tag === 'select') extra = ' options=[' + [...el.options].slice(0, 15).map((o) => clean(o.text)).join(' | ') + ']' +
      ` selected="${clean(el.selectedOptions[0]?.text)}"`;
    else if (['checkbox', 'radio'].includes(t) || ['checkbox', 'radio', 'switch'].includes(role))
      extra = (el.checked || el.getAttribute('aria-checked') === 'true') ? ' [x]' : ' [ ]';
    else if (['input', 'textarea'].includes(tag) && el.value) extra = ` value="${clean(el.value)}"`;
    if (el.required || el.getAttribute('aria-required') === 'true') extra += ' *';
    if (tag === 'a') extra += ` -> ${el.getAttribute('href').slice(0, 80)}`;
    out.push(`${ref} ${role} "${label(el)}"${extra}`);
  }
  return out;
};

async function connect() {
  let browser;
  try { browser = await chromium.connectOverCDP(CDP); } catch { die('браузер не запущен: browser.mjs start'); }
  const ctx = browser.contexts()[0];
  const pages = ctx.pages();
  if (!pages.length) pages.push(await ctx.newPage());
  let i = existsSync(TAB_FILE) ? Number(readFileSync(TAB_FILE, 'utf8')) : pages.length - 1;
  if (!(i >= 0 && i < pages.length)) i = pages.length - 1;
  // узкое окно = мобильная вёрстка с баннерами «скачайте приложение», перекрывающими формы
  await pages[i].setViewportSize({ width: 1366, height: 900 }).catch(() => {});
  return { browser, ctx, pages, page: pages[i] };
}

// ref вида f2e14 -> фрейм №2; e14 -> главный фрейм
function locate(page, ref) {
  const m = /^(?:f(\d+))?e\d+$/.exec(ref || '');
  if (!m) die(`плохой ref: ${ref}`);
  const frame = m[1] ? page.frames()[Number(m[1])] : page.mainFrame();
  if (!frame) die(`нет фрейма для ${ref} — сделай snap заново`);
  return frame.locator(`[data-jref="${ref}"]`);
}

// --- iframe из другого процесса (forms.ozon.ru и т.п.) ---
// Playwright по CDP-подключению их не видит, а отладочный порт отдаёт каждый как отдельную цель.
// Работаем с ними напрямую: Runtime.evaluate в цели iframe. Ref таких элементов: x<номер>e<n>.
async function oopifs(page) {
  const s = await page.context().newCDPSession(page);
  const { targetInfo } = await s.send('Target.getTargetInfo');
  await s.detach();
  const list = await (await fetch(`${CDP}/json/list`)).json();
  return list.filter((t) => t.type === 'iframe' && t.parentId === targetInfo.targetId);
}

function cdpEval(wsUrl, expression) {
  return new Promise((ok, fail) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => { ws.close(); fail(new Error('iframe не ответил за 15 с')); }, 15000);
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate',
      params: { expression, returnByValue: true, awaitPromise: true, userGesture: true } }));
    ws.onmessage = (m) => {
      const r = JSON.parse(m.data);
      if (r.id !== 1) return;
      clearTimeout(timer); ws.close();
      if (r.result?.exceptionDetails) fail(new Error(r.result.exceptionDetails.exception?.description || 'ошибка в iframe'));
      else ok(r.result?.result?.value);
    };
    ws.onerror = () => { clearTimeout(timer); fail(new Error('нет связи с iframe')); };
  });
}

// действие над элементом в iframe; el — элемент по data-jref
const OOPIF_ACT = (ref, action, value) => `(() => {
  const el = document.querySelector('[data-jref="${ref}"]');
  if (!el) throw new Error('нет ${ref} — сделай snap заново');
  el.scrollIntoView({ block: 'center' });
  const act = ${JSON.stringify(action)}, val = ${JSON.stringify(value ?? '')};
  if (act === 'click') { el.click(); return 'OK'; }
  if (act === 'check' || act === 'uncheck') {
    const on = el.checked || el.getAttribute('aria-checked') === 'true';
    if (on !== (act === 'check')) el.click();
    return 'OK';
  }
  if (act === 'focus') { el.focus(); return 'OK'; }
  if (act === 'fill') {
    el.focus();
    if (el.isContentEditable) el.textContent = val;
    else {   // нативный сеттер, иначе React/Vue не заметят изменение
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
    return el.value ?? el.textContent;
  }
  throw new Error('неизвестное действие ' + act);
})()`;

async function oopifAct(page, ref, action, value) {
  const n = Number(/^x(\d+)e\d+$/.exec(ref)[1]);
  const t = (await oopifs(page))[n];
  if (!t) die(`нет iframe x${n} — сделай snap заново`);
  return cdpEval(t.webSocketDebuggerUrl, OOPIF_ACT(ref, action, value));
}
const isX = (ref) => /^x\d+e\d+$/.test(ref || '');

async function settle(page) {
  await page.waitForLoadState('load', { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1200);
}

// сайты любят перезагружать страницу сразу после load — тогда просто пробуем ещё раз
async function retry(fn, page) {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) {
      if (i >= 3 || !/context was destroyed|navigat/i.test(e.message)) throw e;
      await settle(page);
    }
  }
}

const cmds = {
  async start(args) {
    mkdirSync(join(HOME, 'profile'), { recursive: true });
    try { await (await chromium.connectOverCDP(CDP)).close(); return console.log('OK: уже запущен'); } catch {}
    const flags = [`--remote-debugging-port=${PORT}`, '--remote-debugging-address=127.0.0.1',
      `--user-data-dir=${join(HOME, 'profile')}`, '--no-first-run', '--no-default-browser-check', '--lang=ru-RU', '--window-size=1400,1000'];
    if (args.includes('--headless')) flags.push('--headless=new');
    if (args.includes('--direct')) flags.push('--no-proxy-server');
    const log = openSync(join(HOME, 'chrome.log'), 'a');
    spawn(chromium.executablePath(), [...flags, 'about:blank'], { detached: true, stdio: ['ignore', log, log] }).unref();
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 250));
      try { await (await chromium.connectOverCDP(CDP)).close(); return console.log(`OK: браузер на ${CDP}`); } catch {}
    }
    die(`не поднялся, смотри ${join(HOME, 'chrome.log')}`);
  },
  async stop() {
    const { browser } = await connect();
    const s = await browser.newBrowserCDPSession();
    await s.send('Browser.close').catch(() => {});
    console.log('OK: закрыт');
  },
  async goto([url], { page }) {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await settle(page);
    console.log(`OK: ${await page.title()} — ${page.url()}`);
  },
  async live([url], { ctx }) {
    if (!url) die('live <url>');
    // отдельная вкладка: текущая может держать недозаполненную анкету
    const page = await ctx.newPage();
    try { await cmds._live(url, page); } finally { await page.close().catch(() => {}); }
  },
  async _live(url, page) {
    const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await settle(page);
    const status = resp?.status() ?? 0, final = page.url();
    const info = await page.evaluate(() => {
      const t = document.body.innerText.replace(/\s+/g, ' ');
      const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const btn = [...document.querySelectorAll('a,button,[role=button],input[type=submit]')].filter(vis)
        .map((el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim())
        .find((s) => /^(откликнуться|подать (заявку|отклик)|отправить (заявку|отклик|резюме)|хочу (к вам|в команду)|apply( now| for this job)?|submit application)/i.test(s));
      const fields = [...document.querySelectorAll('input:not([type=hidden]),textarea,select')].filter(vis).length;
      return { text: t, chars: t.length, btn, fields, file: !!document.querySelector('input[type=file]') };
    });
    const CLOSED = /(ваканси[яи] (закрыта|в архиве|неактуальна|больше не актуальна|не найдена|снята)|архивная вакансия|при[её]м (заявок|откликов|резюме) (завершен|завершён|закрыт|окончен)|набор (закрыт|завершен|завершён)|срок подачи (заявок )?(ист[её]к|закончился)|больше не принима|страница не найдена|no longer (accepting|available)|position (has been )?filled|job (is )?(closed|expired)|this (job|posting) (has )?expired|page not found)/i;
    const m = CLOSED.exec(info.text);
    const pathOf = (u) => { try { return new URL(u).pathname.replace(/\/+$/, ''); } catch { return ''; } };
    // ушли с карточки на общий список вакансий или на главную
    const listing = pathOf(url).length > 1 && pathOf(final) !== pathOf(url) &&
      /^(|\/(ru|en))?(\/(vacancies|vacancy|jobs|careers?|career))?$/i.test(pathOf(final));
    const ev = [`HTTP ${status}`, `final ${final}`, `текст ${info.chars} симв.`, `полей ${info.fields}${info.file ? ' (+файл)' : ''}`,
      info.btn ? `кнопка «${info.btn.slice(0, 40)}»` : 'кнопки отклика не видно'];
    if (m) ev.push(`маркер «${info.text.slice(Math.max(0, m.index - 40), m.index + m[0].length + 40).trim()}»`);
    if (listing) ev.push('редирект на общий список');
    const verdict = status >= 400 || listing || m ? 'CLOSED'
      : (info.btn || info.fields >= 3 || info.file) && info.chars > 400 ? 'LIVE' : 'UNSURE';
    console.log(verdict);
    for (const e of ev) console.log(`  ${e}`);
  },
  async snap(_, { page }) {
    console.log(`# ${await page.title()} — ${page.url()}`);
    const frames = page.frames();
    for (const [i, f] of frames.entries()) {
      const lines = await f.evaluate(SNAP_JS, i ? `f${i}` : '').catch(() => []);
      if (!lines.length) continue;
      if (i) console.log(`## frame f${i}: ${f.url().slice(0, 100)}`);
      console.log(lines.join('\n'));
    }
    for (const [j, t] of (await oopifs(page)).entries()) {
      const lines = await cdpEval(t.webSocketDebuggerUrl, `(${SNAP_JS})('x${j}')`).catch(() => []);
      console.log(`## iframe x${j}: ${t.url.slice(0, 100)}`);
      if (lines?.length) console.log(lines.join('\n'));
    }
  },
  async text([max], { page }) {
    let t = await page.evaluate(() => document.body.innerText);
    for (const [j, f] of (await oopifs(page)).entries())
      t += `\n\n## iframe x${j}\n` + await cdpEval(f.webSocketDebuggerUrl, 'document.body.innerText').catch(() => '');
    console.log(t.replace(/\n{3,}/g, '\n\n').slice(0, Number(max) || 6000));
  },
  async antselect([ref, ...v], { page }) {
    const text = v.join(' ');
    const sel = locate(page, ref).locator('xpath=ancestor::div[contains(concat(" ", @class, " "), " ant-select ")][1]');
    await sel.evaluate((el) => el.scrollIntoView({ block: 'center' }));
    await sel.click({ timeout: 5000 }).catch(() => sel.click({ force: true }));   // липкие шапки перекрывают поле
    const input = locate(page, ref);
    if (!(await input.getAttribute('readonly'))) {                    // с поиском: печатаем и ждём фильтрации
      await input.pressSequentially(text, { delay: 30 });
      await page.waitForTimeout(1200);
    }
    // все выпадашки Ant Design живут в DOM — берём только список этого поля
    const id = await input.getAttribute('id');
    const opts = page.locator(`.ant-select-dropdown:has([id="${id}_list"]) .ant-select-item-option`);
    await opts.first().waitFor({ timeout: 10000 });
    const exact = opts.filter({ has: page.locator(`xpath=self::*[@title=${JSON.stringify(text)}]`) });
    const pickIt = (await exact.count()) ? exact.first() : opts.filter({ hasText: text }).first();
    if (!(await pickIt.count())) die(`нет пункта «${text}»; есть: ${(await opts.allInnerTexts()).slice(0, 15).join(' | ')}`);
    await pickIt.click();
    await page.waitForTimeout(400);
    console.log(`OK: ${await sel.locator('.ant-select-selection-item').first().innerText().catch(() => '?')}`);
  },
  async pick(args, { page }) {
    const text = args.join(' ');
    const loc = page.getByText(text, { exact: true }).locator('visible=true');
    const n = await loc.count();
    if (!n) die(`не вижу «${text}» — проверь shot/text`);
    await loc.last().click({ timeout: 10000 });     // last: подсказка рисуется поверх, после поля ввода
    await settle(page);
    console.log(`OK${n > 1 ? ` (совпадений ${n}, взял последнее)` : ''}`);
  },
  async click([ref], { page }) {
    if (isX(ref)) await oopifAct(page, ref, 'click');
    else await locate(page, ref).click({ timeout: 10000 });
    await settle(page);
    console.log('OK');
  },
  async fill(args, { page }) {
    const typed = args.includes('--type');
    const [ref, ...rest] = args.filter((a) => a !== '--type');
    if (isX(ref)) {
      if (!typed) return console.log(`OK: ${ref} = "${await oopifAct(page, ref, 'fill', rest.join(' '))}"`);
      await oopifAct(page, ref, 'fill', '');          // посимвольно: очистить, сфокусировать, ввести с клавиатуры страницы
      await oopifAct(page, ref, 'focus');
      await page.keyboard.type(rest.join(' '), { delay: 40 });
      return console.log(`OK: ${ref} напечатано`);
    }
    const loc = locate(page, ref);
    if (typed) {                      // поля с маской: стереть как человек и напечатать посимвольно
      await loc.click();
      await page.keyboard.press('Control+A');
      await page.keyboard.press('Backspace');
      await loc.pressSequentially(rest.join(' '), { delay: 40 });
    }
    else await loc.fill(rest.join(' '));
    console.log(`OK: ${ref} = "${await loc.inputValue().catch(() => rest.join(' '))}"`);
  },
  async select([ref, ...v], { page }) {
    const val = v.join(' ');
    const loc = locate(page, ref);
    await loc.selectOption({ label: val }).catch(() => loc.selectOption(val));
    console.log('OK');
  },
  async check([ref], { page }) {
    if (isX(ref)) await oopifAct(page, ref, 'check');
    else {
      const loc = locate(page, ref);
      await loc.scrollIntoViewIfNeeded().catch(() => {});
      // кастомные чекбоксы: input спрятан под вёрсткой — тогда кликаем его из JS
      await loc.check({ force: true, timeout: 5000 }).catch(() =>
        loc.evaluate((el) => { if (!(el.checked || el.getAttribute('aria-checked') === 'true')) el.click(); }));
      if (!(await loc.evaluate((el) => el.checked || el.getAttribute('aria-checked') === 'true'))) die(`${ref} не отметился`);
    }
    console.log('OK');
  },
  async uncheck([ref], { page }) {
    if (isX(ref)) await oopifAct(page, ref, 'uncheck');
    else {
      const loc = locate(page, ref);
      await loc.scrollIntoViewIfNeeded().catch(() => {});
      await loc.uncheck({ force: true, timeout: 5000 }).catch(() =>
        loc.evaluate((el) => { if (el.checked || el.getAttribute('aria-checked') === 'true') el.click(); }));
      if (await loc.evaluate((el) => el.checked || el.getAttribute('aria-checked') === 'true')) die(`${ref} не снялся`);
    }
    console.log('OK');
  },
  async upload([ref, file], { page }) {
    if (!existsSync(file)) die(`нет файла ${file}`);
    await locate(page, ref).setInputFiles(resolve(file));
    await settle(page);
    console.log('OK');
  },
  async press([key], { page }) { await page.keyboard.press(key); await settle(page); console.log('OK'); },
  async back(_, { page }) { await page.goBack(); await settle(page); console.log(`OK: ${page.url()}`); },
  async wait([ms], { page }) { await page.waitForTimeout(Number(ms) || 1000); console.log('OK'); },
  async shot([file], { page }) {
    const out = resolve(file || join(HOME, 'shot.png'));
    await page.screenshot({ path: out });
    console.log(out);
  },
  async tabs(_, { pages, page }) {
    for (const [i, p] of pages.entries()) console.log(`${p === page ? '*' : ' '}${i} ${await p.title()} — ${p.url()}`);
  },
  async tab([n], { pages }) {
    if (!pages[Number(n)]) die(`нет вкладки ${n}`);
    writeFileSync(TAB_FILE, String(n));
    await pages[Number(n)].bringToFront();
    console.log(`OK: ${pages[Number(n)].url()}`);
  },
};

const [cmd, ...args] = process.argv.slice(2);
if (!cmds[cmd] || cmd.startsWith('_')) die(`команды: ${Object.keys(cmds).filter((k) => !k.startsWith('_')).join(', ')}`);
mkdirSync(HOME, { recursive: true });
if (cmd === 'start') await cmds.start(args);
else {
  const c = await connect();
  // новая вкладка, открытая кликом (target=_blank), становится текущей
  if (cmd === 'click') c.ctx.once('page', (p) => writeFileSync(TAB_FILE, String(c.ctx.pages().indexOf(p))));
  // повторяем только чтение: повторный click/fill может, например, отправить анкету дважды
  const run = () => cmds[cmd](args, c);
  try { await (['snap', 'text', 'tabs', 'shot'].includes(cmd) ? retry(run, c.page) : run()); }
  catch (e) {
    // лог Playwright на 50 строк агенту не нужен: суть + что перекрывает элемент
    const lines = e.message.split('\n');
    const cover = lines.find((l) => l.includes('intercepts pointer events'));
    die(lines[0] + (cover ? `\n  перекрыто: ${cover.trim().slice(0, 160)}` : ''));
  } finally { await c.browser.close().catch(() => {}); }
}
// browser.close() у CDP-подключения только отключается — сам Chromium продолжает жить
process.exit(0);
