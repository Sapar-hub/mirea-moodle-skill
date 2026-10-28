#!/usr/bin/env node
// Проверка перед откликом: чёрный список и прошлые отклики. Только читает и ничего не блокирует:
// печатает находки, решает пользователь.
//
//   precheck.mjs --company "Ozon Tech" --title "Стажёр аналитик данных" [--url <url>]
//
// Источники (все необязательны):
//   ~/.config/job-apply/blacklist.tsv      scope<TAB>pattern<TAB>since<TAB>reason
//        scope: company — все слова pattern есть в названии компании (по началу слова: «телеком» ловит
//                         «телекома»; регистр, ё/е и пунктуация не важны)
//               title   — то же по «компания + должность», для отдельной команды внутри компании
//               domain  — хост ссылки или его поддомен (mts.ru ловит job.mts.ru, но не notmts.ru)
//   $JOB_APPLY_HOME/applied.tsv, history.tsv   date<TAB>company<TAB>title<TAB>url<TAB>resume<TAB>status
//
// Первая строка вывода: OK | WARN | BLACKLIST. Код выхода 0 всегда.
import { readFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const HOME = process.env.JOB_APPLY_HOME || join(homedir(), '.local/share/job-apply');
const CONF = process.env.JOB_APPLY_CONFIG || join(homedir(), '.config/job-apply');

const arg = (k) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] || '' : ''; };
const company = arg('company'), title = arg('title'), url = arg('url');
if (!company) { console.error('ERR: нужен --company (и желательно --title, --url)'); process.exit(1); }

const norm = (s) => (s || '').toLowerCase().replace(/ё/g, 'е').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
const STOP = new Set(['стажер', 'стажировка', 'intern', 'junior', 'младший', 'в', 'и', 'по', 'для', 'на', 'с', 'the', 'of']);
const words = (s) => new Set(norm(s).split(' ').filter((w) => w.length > 1 && !STOP.has(w)));
const jaccard = (a, b) => {
  const A = words(a), B = words(b);
  if (!A.size || !B.size) return 0;
  let n = 0; for (const w of A) if (B.has(w)) n++;
  return n / (A.size + B.size - n);
};
const host = (u) => { try { return new URL(/^https?:/.test(u) ? u : `https://${u}`).hostname.replace(/\.$/, '').toLowerCase(); } catch { return ''; } };
const urlKey = (u) => { try { const x = new URL(u); return (x.hostname + x.pathname).toLowerCase().replace(/\/+$/, ''); } catch { return ''; } };
// «Ozon Tech» и «Ozon Банк» — одна группа, но разные компании; бренд — первое слово
const brand = (s) => norm(s).split(' ')[0] || '';

const rows = (file) => !existsSync(file) ? [] : readFileSync(file, 'utf8').split('\n')
  .filter((l) => l.trim() && !l.startsWith('#')).map((l) => l.split('\t'));

const out = [];
let level = 'OK';

for (const [scope, pattern, since, reason] of rows(join(CONF, 'blacklist.tsv'))) {
  const p = norm(pattern).split(' ').filter(Boolean);
  if (!p.length) continue;
  const hit = scope === 'domain'
    ? (() => { const h = host(url), d = pattern.toLowerCase().replace(/\.$/, ''); return h && (h === d || h.endsWith(`.${d}`)); })()
    : (() => { const ws = norm(scope === 'title' ? `${company} ${title}` : company).split(' ');
        return p.every((w) => ws.some((x) => x.startsWith(w))); })();
  if (hit) { level = 'BLACKLIST'; out.push(`BLACKLIST ${scope} «${pattern}» с ${since || '?'}: ${reason || 'без причины'}`); }
}

const past = [...rows(join(HOME, 'applied.tsv')), ...rows(join(HOME, 'history.tsv'))];
const key = urlKey(url), co = norm(company), br = brand(company);
const coLines = [];
for (const [date, c, t, u, , status] of past) {
  const line = `${date} | ${c} | ${t} | ${status || '?'}${u ? ` | ${u}` : ''}`;
  if (key && urlKey(u) === key) { out.push(`DUP та же ссылка: ${line}`); continue; }
  const sameCo = norm(c) === co || (br && brand(c) === br);
  if (!sameCo) continue;
  const sim = title ? jaccard(title, t) : 0;
  if (sim >= 0.5) out.push(`SIMILAR (${sim.toFixed(2)}) возможно та же вакансия или репост: ${line}`);
  else coLines.push(line);
}
const sameCompany = coLines.length + out.filter((l) => /^(DUP|SIMILAR)/.test(l)).length;
for (const l of coLines.sort().reverse().slice(0, 3)) out.push(`COMPANY ${l}`);
if (coLines.length > 3) out.push(`COMPANY … и ещё ${coLines.length - 3}`);
if (sameCompany >= 2) out.push(`NOTE в эту компанию уже ${sameCompany} откликов: проверь, что резюме и ответы не противоречат прошлым`);
if (level === 'OK' && out.some((l) => /^(DUP|SIMILAR)/.test(l))) level = 'WARN';
else if (level === 'OK' && out.length) level = 'OK (были отклики в эту компанию)';

console.log(level);
for (const l of out) console.log(`  ${l}`);
