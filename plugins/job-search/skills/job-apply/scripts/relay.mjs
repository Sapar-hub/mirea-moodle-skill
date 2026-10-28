#!/usr/bin/env node
// Капчу проходит человек: страница с живым скриншотом текущей вкладки, тап по картинке = клик в браузере.
// Слушает только 127.0.0.1; наружу выводить через `cloudflared tunnel --url http://127.0.0.1:<port>`.
// Доступ — по случайному токену в пути. Агент сам по капче не кликает, только показывает её пользователю.
//
//   relay.mjs [port]     печатает локальный адрес с токеном; Ctrl+C — остановить
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { randomBytes } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const HOME = process.env.JOB_APPLY_HOME || join(homedir(), '.local/share/job-apply');
const CDP = `http://127.0.0.1:${Number(process.env.JOB_APPLY_PORT || 9333)}`;
const PORT = Number(process.argv[2] || 9444);
const TOKEN = randomBytes(16).toString('hex');

const browser = await chromium.connectOverCDP(CDP);
const page = () => {
  const pages = browser.contexts()[0].pages();
  const f = join(HOME, 'tab');
  const i = existsSync(f) ? Number(readFileSync(f, 'utf8')) : pages.length - 1;
  return pages[i >= 0 && i < pages.length ? i : pages.length - 1];
};

const HTML = `<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Капча</title><style>body{margin:0;font:15px sans-serif;background:#111;color:#eee}
p{margin:8px}img{width:100%;display:block;touch-action:manipulation}button{font-size:15px;margin:4px 8px}</style>
<p>Тап по картинке = клик в браузере. <button id=d>перетаскивание: выкл</button></p><img id=s>
<script>
const s=document.getElementById('s'),d=document.getElementById('d');let drag=false,from=null;
const load=()=>{const i=new Image();i.onload=()=>{s.src=i.src;setTimeout(load,1200)};i.onerror=()=>setTimeout(load,3000);i.src='shot.png?'+Date.now()};load();
d.onclick=()=>{drag=!drag;from=null;d.textContent='перетаскивание: '+(drag?'вкл (тап «откуда», потом «куда»)':'выкл')};
s.onclick=e=>{const r=s.getBoundingClientRect(),p=[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];
 if(!drag)return fetch('click?x='+p[0]+'&y='+p[1],{method:'POST'});
 if(!from){from=p;return}fetch('drag?x='+from[0]+'&y='+from[1]+'&x2='+p[0]+'&y2='+p[1],{method:'POST'});from=null};
</script>`;

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const [, tok, act = ''] = url.pathname.split('/');
  if (tok !== TOKEN) return res.writeHead(404).end();
  try {
    const p = page();
    const vp = p.viewportSize() || { width: 1366, height: 900 };
    const at = (k) => Number(url.searchParams.get(k));
    if (act === '') return res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }).end(HTML);
    if (act === 'shot.png')
      return res.writeHead(200, { 'content-type': 'image/png', 'cache-control': 'no-store' }).end(await p.screenshot());
    if (req.method === 'POST' && act === 'click') {
      await p.mouse.click(at('x') * vp.width, at('y') * vp.height);
      return res.writeHead(204).end();
    }
    if (req.method === 'POST' && act === 'drag') {
      await p.mouse.move(at('x') * vp.width, at('y') * vp.height);
      await p.mouse.down();
      await p.mouse.move(at('x2') * vp.width, at('y2') * vp.height, { steps: 25 });
      await p.mouse.up();
      return res.writeHead(204).end();
    }
    res.writeHead(404).end();
  } catch (e) {
    res.writeHead(500).end(String(e));
  }
}).listen(PORT, '127.0.0.1', () => console.log(`OK: http://127.0.0.1:${PORT}/${TOKEN}/`));
