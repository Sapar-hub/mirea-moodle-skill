#!/usr/bin/env node
// Резюме под конкретный отклик: YAML -> HTML -> PDF (Chromium из Playwright).
//
//   resume.mjs render <tailored.yaml> [out.pdf]   по умолчанию рядом: <name>.pdf
//   resume.mjs check  <tailored.yaml>             что из файла не находится в мастере (защита от выдумок)
//   resume.mjs tailor <spec.yaml> [out.yaml]      собрать tailored-файл из мастера по спецификации:
//     headline, about, ats_keywords, ats_hidden — как есть
//     projects / experience: [id | {id, bullets: [индексы], add_metrics: false}] — порядок = порядок в резюме
//     skills: [группы мастера по порядку]; publications: [id]; courses: [порядок профильных курсов]
//
// Мастер с фактами: $JOB_APPLY_RESUME (по умолчанию ~/.config/job-apply/resume/master.yaml).
// Tailored-файл — самодостаточный, той же схемы, что пример в SKILL.md:
//   name, contacts, headline, about, projects[], experience[], education[], publications[],
//   conferences[], skills{группа: [...]}, languages[], ats_keywords[], ats_hidden
import { chromium } from 'playwright';
import YAML from 'yaml';
import { readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const MASTER = process.env.JOB_APPLY_RESUME || join(homedir(), '.config/job-apply/resume/master.yaml');
const die = (m) => { console.error(`ERR: ${m}`); process.exit(1); };
const load = (p) => YAML.parse(readFileSync(p, 'utf8'));
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

const SKILL_LABELS = {
  languages_prog: 'Языки', ml_dl: 'ML / DL', cv: 'Computer Vision', nlp_llm: 'NLP / LLM', data: 'Данные',
  stats: 'Статистика', sql: 'SQL', infra: 'Инфраструктура', ai_agents: 'AI-агенты',
};

function entry(it, kind) {
  const head = kind === 'exp'
    ? `<b>${esc(it.org)}</b>${it.role ? ` — ${esc(it.role)}` : ''}`
    : `<b>${esc(it.title)}</b>`;
  const meta = esc(it.period || '');
  const sub = [...(it.stack || []), ...(it.links || [])].map(esc).join(' · ');
  const bullets = [...(it.bullets || []), ...(it.metrics ? [`Результат: ${it.metrics}`] : [])];
  return `<div class="entry"><div class="eh"><span>${head}</span><span class="meta">${meta}</span></div>
    ${sub ? `<div class="stack">${sub}</div>` : ''}
    ${bullets.length ? `<ul>${bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}</div>`;
}

function html(r) {
  const c = r.contacts || {};
  const contacts = [c.city, c.phone, c.email, c.telegram && `Telegram ${c.telegram}`, c.github, c.orcid && `ORCID ${c.orcid}`]
    .filter(Boolean).map(esc).join(' · ');
  const sec = (title, body) => (body ? `<h2>${title}</h2>${body}` : '');
  const list = (xs) => (xs?.length ? `<ul class="plain">${xs.map((x) => `<li>${esc(x.text ?? x)}</li>`).join('')}</ul>` : '');
  const edu = (r.education || []).map((e) => `<div class="entry"><div class="eh"><span><b>${esc(e.org)}</b></span>
      <span class="meta">${esc(e.period)}</span></div><div>${esc(e.program)}; ${esc(e.degree)}${e.gpa ? `; средний балл ${esc(e.gpa)}` : ''}</div>
      ${e.courses?.length ? `<div class="stack">Профильные курсы: ${e.courses.map(esc).join(', ')}</div>` : ''}</div>`).join('');
  const skills = Object.entries(r.skills || {}).map(([k, v]) =>
    `<tr><td class="sk">${esc(SKILL_LABELS[k] || k)}</td><td>${v.map(esc).join(', ')}</td></tr>`).join('');
  const ats = r.ats_keywords?.length
    ? (r.ats_hidden
      ? `<div class="ats" aria-hidden="true">${r.ats_keywords.map(esc).join(' ')}</div>`
      : sec('Ключевые слова', `<div class="stack">${r.ats_keywords.map(esc).join(' · ')}</div>`))
    : '';
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>${esc(r.name.last)} ${esc(r.name.first)}</title><style>
  @page { size: A4; margin: 14mm 15mm; }
  body { font: 9.6pt/1.38 'Noto Sans', 'Roboto', sans-serif; color: #1a1a1a; margin: 0; }
  h1 { font-size: 19pt; margin: 0; letter-spacing: -.2px; }
  .headline { font-size: 11pt; color: #2b4a8b; margin: 2px 0 4px; font-weight: 600; }
  .contacts { color: #444; font-size: 8.8pt; }
  h2 { font-size: 9.6pt; text-transform: uppercase; letter-spacing: .6px; color: #2b4a8b;
       border-bottom: 1px solid #c9d3e6; padding-bottom: 2px; margin: 11px 0 5px; break-after: avoid; }
  .entry { margin: 0 0 6px; break-inside: avoid; }
  .eh { display: flex; justify-content: space-between; gap: 12px; }
  .meta { color: #555; font-size: 8.6pt; white-space: nowrap; }
  .stack { color: #555; font-size: 8.6pt; }
  ul { margin: 2px 0 0; padding-left: 15px; } li { margin: 1px 0; }
  ul.plain { padding-left: 15px; }
  table { border-collapse: collapse; } td { padding: 1px 0; vertical-align: top; }
  td.sk { font-weight: 600; padding-right: 10px; white-space: nowrap; }
  p.about { margin: 0; }
  .ats { color: #fff; font-size: 1px; line-height: 1px; height: 1px; overflow: hidden; }
  </style></head><body>
  <h1>${esc(r.name.first)} ${esc(r.name.last)}</h1>
  ${r.headline ? `<div class="headline">${esc(r.headline)}</div>` : ''}
  <div class="contacts">${contacts}</div>
  ${sec('О себе', r.about && `<p class="about">${esc(r.about)}</p>`)}
  ${sec('Проекты', (r.projects || []).map((p) => entry(p, 'proj')).join(''))}
  ${sec('Опыт', (r.experience || []).map((p) => entry(p, 'exp')).join(''))}
  ${sec('Навыки', skills && `<table>${skills}</table>`)}
  ${sec('Образование', edu)}
  ${sec('Публикации', list(r.publications))}
  ${sec('Конференции', list(r.conferences))}
  ${sec('Языки', r.languages?.length && `<div>${r.languages.map(esc).join(' · ')}</div>`)}
  ${ats}
  </body></html>`;
}

// Каждое значение-строку tailored-файла ищем в мастере (по нормализованным словам).
// Не найдено -> печатаем: либо переформулировка (проверь глазами), либо выдумка.
function check(r, m) {
  const norm = (s) => String(s).toLowerCase().replace(/\.(?!\d)/g, ' ').replace(/[^\p{L}\p{N}.%+]+/gu, ' ').trim();
  const masterText = norm(JSON.stringify(m));
  const masterWords = new Set(masterText.split(' '));
  const skip = new Set(['headline', 'about', 'ats_hidden', 'ats_keywords']);
  const out = [];
  const walk = (v, path) => {
    if (typeof v === 'string') {
      const n = norm(v);
      if (!n || masterText.includes(n)) return;
      const miss = n.split(' ').filter((w) => w.length > 3 && !masterWords.has(w));
      if (miss.length) out.push(`${path}: [${miss.join(', ')}] — «${v.slice(0, 90)}»`);
    } else if (Array.isArray(v)) v.forEach((x, i) => walk(x, `${path}[${i}]`));
    else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) if (!skip.has(k)) walk(x, path ? `${path}.${k}` : k);
  };
  walk(r, '');
  for (const [k, v] of [['headline', r.headline], ['about', r.about], ['ats_keywords', r.ats_keywords?.join(', ')]])
    if (v) out.push(`${k} (сверь вручную): «${v}»`);
  return out;
}

function tailor(spec, m) {
  const pick = (list, refs, kind) => (refs || []).map((ref) => {
    const id = typeof ref === 'string' ? ref : ref.id;
    const it = list.find((x) => x.id === id);
    if (!it) die(`${kind}: нет id ${id} в мастере`);
    if (it.verify) die(`${kind}.${id} помечен verify в мастере — сначала подтверди`);
    const { id: _, tags, verify, ...rest } = it;
    if (ref.bullets) rest.bullets = ref.bullets.map((i) => it.bullets[i] ?? die(`${id}: нет пункта ${i}`));
    if (ref.add_metrics === false) delete rest.metrics;
    return rest;
  });
  const e = m.education[0];
  return {
    name: { first: m.name.first, last: m.name.last },
    contacts: { city: m.contacts.city, phone: m.contacts.phone, email: m.contacts.email,
      telegram: m.contacts.telegram, github: m.contacts.github },
    headline: spec.headline, about: spec.about,
    projects: pick(m.projects, spec.projects, 'projects'),
    experience: pick(m.experience, spec.experience, 'experience'),
    skills: Object.fromEntries((spec.skills || Object.keys(m.skills)).map((k) => [k, m.skills[k] ?? die(`нет группы навыков ${k}`)])),
    education: [{ ...e, degree: e.degree.replace(/, бюджет/, ''), courses: spec.courses || e.courses }],
    publications: (spec.publications || []).map((id) => (m.publications.find((p) => p.id === id) ?? die(`нет публикации ${id}`)).text),
    languages: m.languages,
    ats_hidden: spec.ats_hidden ?? true,
    ats_keywords: spec.ats_keywords || [],
  };
}

const [cmd, file, outArg] = process.argv.slice(2);
if (!file) die('resume.mjs render|check <tailored.yaml> [out.pdf]');
const r = load(file);
if (cmd === 'check') {
  const m = load(MASTER);
  const issues = check(r, m);
  console.log(issues.length ? issues.join('\n') : 'OK: всё найдено в мастере');
} else if (cmd === 'tailor') {
  const out = resolve(outArg || file.replace(/\.spec\.ya?ml$|\.ya?ml$/, '') + '.yaml');
  if (out === resolve(file)) die('укажи out.yaml');
  writeFileSync(out, YAML.stringify(tailor(r, load(MASTER)), { lineWidth: 0 }));
  console.log(out);
} else if (cmd === 'render') {
  if (JSON.stringify(r).includes('"verify"')) die('в файле остались поля verify — сними их или убери пункты');
  // внутренние id мастера (projects/experience/publications) не должны попадать в текст резюме
  const m = load(MASTER);
  const ids = [...m.projects, ...m.experience, ...m.publications].map((x) => x.id).filter((id) => id.includes("_"));  // «churn», «axenix» — обычные слова
  const text = JSON.stringify(r);
  const leak = ids.filter((id) => new RegExp(`\\b${id}\\b`).test(text));
  if (leak.length) die(`в тексте внутренние id: ${leak.join(', ')}`);
  // «текст: текст» без кавычек YAML превращает в словарь — в PDF это было бы [object Object]
  const bad = [];
  const walk = (v, path) => {
    if (Array.isArray(v)) v.forEach((x, i) => (x && typeof x === 'object' && !Array.isArray(x) && !['projects', 'experience', 'education', 'publications'].includes(path)
      ? bad.push(`${path}[${i}]`) : walk(x, `${path}[${i}]`)));
    else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) walk(x, k);
  };
  walk(r, '');
  if (bad.length) die(`строка распарсилась как словарь (возьми в кавычки): ${bad.join(', ')}`);
  const out = resolve(outArg || file.replace(/\.ya?ml$/, '') + '.pdf');
  const browser = await chromium.launch({ channel: 'chromium' });   // полный Chromium, без отдельного headless_shell
  const page = await browser.newPage();
  await page.setContent(html(r), { waitUntil: 'load' });
  await page.pdf({ path: out, format: 'A4', printBackground: true, preferCSSPageSize: true });
  await browser.close();
  console.log(out);
} else die(`неизвестная команда ${cmd}`);
