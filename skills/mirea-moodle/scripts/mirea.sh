#!/usr/bin/env bash
# mirea.sh — автоматизация online-edu.mirea.ru (Moodle + Keycloak SSO)
# Креды берутся из окружения: KC_USER, KC_PASS. Состояние в $MIREA_DIR (по умолч. ~/.mirea).
#
# Порядок работы:
#   export KC_USER='<student>@edu.mirea.ru' KC_PASS='...'
#   ./mirea.sh check                 # проверить сеть
#   ./mirea.sh start                 # логин+пароль -> код уходит тебе на телефон/МАКС
#                                    # (если заданы MAILRU_USER+MAILRU_APPPASS — сразу вход через код из почты)
#   ./mirea.sh otp 123456            # подставить код -> войти в Moodle
#   ./mirea.sh courses               # список курсов (id :: название)
#   ./mirea.sh course <course-id>          # папки/файлы курса
#   ./mirea.sh folder <folder-id>         # файлы в папке (имя + прямой URL)
#   ./mirea.sh get '<pluginfile-url>' [outdir]
#   ./mirea.sh topdf file.docx [outdir]
set -euo pipefail

BASE="https://online-edu.mirea.ru"
SSO="https://sso.mirea.ru"
DIR="${MIREA_DIR:-$HOME/.mirea}"
JAR="$DIR/jar.txt"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
mkdir -p "$DIR"

# curl: ВСЕГДА в обход egress-прокси и по IPv4, общий cookie-jar
cj() { curl --noproxy '*' -4 -sS -m 40 -A "$UA" -c "$JAR" -b "$JAR" "$@"; }

die() { echo "FAIL: $*" >&2; exit 1; }

# достать "loginAction" из kcContext на странице Keycloak
extract_la() { grep -oE '"loginAction": *"https://[^"]*"' "$1" | head -1 | sed -E 's/.*"loginAction": *"//; s/"$//'; }

cmd_check() {
  for h in "$BASE" "$SSO"; do
    code=$(curl --noproxy '*' -4 -sS -m 15 -o /dev/null -w '%{http_code}' "$h/" || echo 000)
    echo "$h -> HTTP $code"
  done
}

cmd_start() {
  : "${KC_USER:?export KC_USER}"; : "${KC_PASS:?export KC_PASS}"
  rm -f "$JAR"
  cj -L -o "$DIR/login.html" "$BASE/auth/oidc/?source=loginpage" >/dev/null
  local la; la=$(extract_la "$DIR/login.html")
  [ -n "$la" ] || die "не нашёл loginAction (форма логина изменилась)"
  cj -e "$SSO/" \
     --data-urlencode "username=$KC_USER" \
     --data-urlencode "password=$KC_PASS" \
     --data-urlencode "credentialId=" \
     -o "$DIR/otp.html" "$la" >/dev/null
  if grep -q 'login-max-otp' "$DIR/otp.html"; then
    extract_la "$DIR/otp.html" > "$DIR/otp_action"
    [ -s "$DIR/otp_action" ] || die "нет loginAction на шаге OTP"
    local ph; ph=$(grep -oE 'maskedPhone": *"[^"]*"' "$DIR/otp.html" | head -1 | sed -E 's/maskedPhone": *"//; s/"$//')
    if [ -n "${MAILRU_USER:-}" ] && [ -n "${MAILRU_APPPASS:-}" ]; then
      echo "OK: пароль принят. MAILRU_* заданы — переключаюсь на код из почты."
      cmd_otp_email
    else
      echo "OK: пароль принят. Код отправлен на ${ph:-телефон/МАКС}."
      echo "    Дальше: ./mirea.sh otp <КОД>"
    fi
  elif grep -q KEYCLOAK_IDENTITY "$JAR"; then
    echo "OK: вошли без запроса OTP (сессия ещё жива) — можно сразу otp-callback не нужен; пробуй courses."
  else
    local pid; pid=$(grep -oE 'kcContext.pageId = "[^"]*"' "$DIR/otp.html" | head -1)
    die "логин/пароль не прошли (нет шага OTP). ${pid:-}"
  fi
}

cmd_otp() {
  local code="${1:?usage: otp <code>}"
  [ -s "$DIR/otp_action" ] || die "нет сохранённого шага OTP — сначала ./mirea.sh start"
  local oa; oa=$(cat "$DIR/otp_action")
  # поле называется именно 'code'
  cj -e "$SSO/" --data-urlencode "code=$code" -o "$DIR/fp.html" "$oa" >/dev/null
  grep -q KEYCLOAK_IDENTITY "$JAR" || die "код не принят или просрочен — повтори ./mirea.sh start и запроси новый"
  # разобрать form_post (атрибуты в ВЕРХНЕМ регистре) и вернуть код в Moodle
  eval "$(python3 -I - "$DIR/fp.html" <<'PY'
import re,sys,html,shlex
h=open(sys.argv[1],encoding='utf-8').read()
f={}
for m in re.finditer(r'<INPUT[^>]*NAME="([^"]+)"[^>]*VALUE="([^"]*)"', h, re.I):
    f[m.group(1)]=html.unescape(m.group(2))
for k in ('code','iss','state','session_state'):
    print(f'FP_{k}={shlex.quote(f.get(k,""))}')
PY
)"
  [ -n "${FP_code:-}" ] || die "не распарсил form_post"
  cj -L -e "$SSO/" \
     --data-urlencode "code=$FP_code" \
     --data-urlencode "iss=$FP_iss" \
     --data-urlencode "state=$FP_state" \
     --data-urlencode "session_state=$FP_session_state" \
     -o "$DIR/my.html" "$BASE/auth/oidc/" >/dev/null
  grep -q 'Выход' "$DIR/my.html" || die "OIDC callback не завершился"
  grep -oE '"sesskey":"[^"]*"' "$DIR/my.html" | head -1 | sed -E 's/"sesskey":"//; s/"//' > "$DIR/sesskey"
  echo "OK: вошли в Moodle. sesskey=$(cat "$DIR/sesskey")"
}

need_sesskey() { [ -s "$DIR/sesskey" ] || die "нет sesskey — сначала войди (start + otp)"; cat "$DIR/sesskey"; }

cmd_courses() {
  local sk; sk=$(need_sesskey)
  cj -H "Content-Type: application/json" -o "$DIR/courses.tmp.json" \
    "$BASE/lib/ajax/service.php?sesskey=$sk&info=core_course_get_enrolled_courses_by_timeline_classification" \
    --data '[{"index":0,"methodname":"core_course_get_enrolled_courses_by_timeline_classification","args":{"offset":0,"limit":0,"classification":"all","sort":"fullname","customfieldname":"","customfieldvalue":""}}]' >/dev/null
  # при ошибке (протухла сессия) не затираем прошлый courses.json
  python3 -I -c "import json,sys;d=json.load(open(sys.argv[1]))[0]
if d.get('error'): print('FAIL:',d.get('exception',{}).get('errorcode','?'),'— сессия протухла? повтори ./mirea.sh start',file=sys.stderr); sys.exit(1)
cs=d.get('data',{}).get('courses',[]);[print(c['id'],'::',c['fullname']) for c in cs] or print('(пусто)')" "$DIR/courses.tmp.json" \
    && mv "$DIR/courses.tmp.json" "$DIR/courses.json"
}

cmd_course() {
  local cid="${1:?usage: course <id>}"
  cj -L -o "$DIR/course_$cid.html" "$BASE/course/view.php?id=$cid" >/dev/null
  python3 -I - "$DIR/course_$cid.html" <<'PY'
import re,sys,html
h=open(sys.argv[1],encoding='utf-8').read()
for m in re.finditer(r'<a[^>]*href="(https://online-edu\.mirea\.ru/mod/(?:resource|folder)/view\.php\?id=\d+)"[^>]*>(.*?)</a>',h,re.S|re.I):
    t=html.unescape(re.sub(r'\s+',' ',re.sub(r'<[^>]+>',' ',m.group(2)))).strip()
    if t:
        kind='FOLDER' if '/folder/' in m.group(1) else 'file  '
        print(m.group(1).rsplit('=',1)[1], kind, '::', t[:90])
PY
}

cmd_folder() {
  local fid="${1:?usage: folder <id>}"
  cj -L -o "$DIR/folder_$fid.html" "$BASE/mod/folder/view.php?id=$fid" >/dev/null
  python3 -I - "$DIR/folder_$fid.html" <<'PY'
import re,sys,html,urllib.parse
h=open(sys.argv[1],encoding='utf-8').read()
seen=set()
for m in re.finditer(r'href="(https?://online-edu\.mirea\.ru/pluginfile\.php/[^"]+)"',h):
    u=html.unescape(m.group(1))
    if u in seen: continue
    seen.add(u)
    name=urllib.parse.unquote(u.rsplit('/',1)[-1].split('?')[0])
    if name.lower().endswith('.ico'): continue
    print(name)
    print('   ',u)
PY
}

cmd_get() {
  local url="${1:?usage: get <url> [outdir]}"; local out="${2:-.}"
  mkdir -p "$out"
  ( cd "$out" && cj -L -OJ -w 'saved: %{filename_effective} (%{size_download} B, %{content_type})\n' "$url" )
}

cmd_topdf() {
  local f="${1:?usage: topdf <file.docx> [outdir]}"; local out="${2:-.}"
  soffice --headless --convert-to pdf --outdir "$out" "$f"
}

# --- Полностью автоматический вход через email-OTP (Mail.ru IMAP) ---
# ЧЕРНОВИК: протестировать в живой сессии. Нужен app-password Mail.ru.
#   env: MAILRU_USER (адрес mail.ru), MAILRU_APPPASS (пароль для внешних приложений)
# Запуск: ./mirea.sh start && ./mirea.sh otp-email
_finish_oidc() {  # аргумент: файл form_post-страницы
  eval "$(python3 -I - "$1" <<'PY'
import re,sys,html,shlex
h=open(sys.argv[1],encoding='utf-8').read(); f={}
for m in re.finditer(r'<INPUT[^>]*NAME="([^"]+)"[^>]*VALUE="([^"]*)"',h,re.I): f[m.group(1)]=html.unescape(m.group(2))
for k in ('code','iss','state','session_state'): print(f'FP_{k}={shlex.quote(f.get(k,""))}')
PY
)"
  [ -n "${FP_code:-}" ] || die "не распарсил form_post"
  cj -L -e "$SSO/" --data-urlencode "code=$FP_code" --data-urlencode "iss=$FP_iss" \
     --data-urlencode "state=$FP_state" --data-urlencode "session_state=$FP_session_state" \
     -o "$DIR/my.html" "$BASE/auth/oidc/" >/dev/null
  grep -q 'Выход' "$DIR/my.html" || die "OIDC callback не завершился"
  grep -oE '"sesskey":"[^"]*"' "$DIR/my.html" | head -1 | sed -E 's/"sesskey":"//; s/"//' > "$DIR/sesskey"
  echo "OK: вошли в Moodle. sesskey=$(cat "$DIR/sesskey")"
}

cmd_otp_email() {
  : "${MAILRU_USER:?export MAILRU_USER}"; : "${MAILRU_APPPASS:?export MAILRU_APPPASS}"
  [ -s "$DIR/otp.html" ] || die "сначала ./mirea.sh start"
  local la eid ts
  la=$(extract_la "$DIR/otp.html"); [ -n "$la" ] || die "нет loginAction на шаге 2FA"
  eid=$(python3 -I - "$DIR/otp.html" <<'PY'
import re,sys
h=open(sys.argv[1],encoding='utf-8').read()
m=re.search(r'"authenticationSelections"\s*:\s*\[(.*?)\]',h,re.S); eid=''
if m:
    for blk in re.split(r'\}\s*,\s*\{',m.group(1)):
        if 'email-authenticator' in blk:
            mm=re.search(r'"authExecId"\s*:\s*"([^"]+)"',blk)
            if mm: eid=mm.group(1)
print(eid)
PY
)
  [ -n "$eid" ] || die "не нашёл authExecId метода 'почта'"
  ts=$(date +%s)
  cj -e "$SSO/" --data-urlencode "authenticationExecution=$eid" -o "$DIR/EC.html" "$la" >/dev/null
  grep -q 'email-code-form' "$DIR/EC.html" || die "не попали на форму кода из письма"
  echo "код отправлен на $MAILRU_USER, жду письмо (до 90с)..."
  local code
  code=$(MAILRU_USER="$MAILRU_USER" MAILRU_APPPASS="$MAILRU_APPPASS" OTP_SINCE="$ts" python3 -I - <<'PY'
import imaplib,os,time,email,email.header,re
U=os.environ['MAILRU_USER'];P=os.environ['MAILRU_APPPASS'];since=int(os.environ['OTP_SINCE'])
def extract(msg):
    # Узко: только письма кода от sso@mirea.ru, код берём из темы ("648702 – ваш код … для входа")
    frm=str(email.header.make_header(email.header.decode_header(msg.get('From',''))))
    subj=str(email.header.make_header(email.header.decode_header(msg.get('Subject',''))))
    if 'sso@mirea.ru' not in frm.lower(): return None
    if not re.search(r'код|code',subj,re.I): return None
    m=re.search(r'(?<!\d)(\d{6})(?!\d)',subj)
    return m.group(1) if m else None
try:
    M=imaplib.IMAP4_SSL('imap.mail.ru',993,timeout=20); M.login(U,P)
except Exception as e:
    print('',end='');import sys;print('ERR:',e,file=sys.stderr);raise SystemExit(1)
KEEP=os.environ.get('MIREA_KEEP_OTP')=='1'
found=''; fid=None; deadline=time.time()+90
while time.time()<deadline and not found:
    M.select('INBOX')
    typ,d=M.search(None,'ALL')
    for i in reversed(d[0].split()[-12:]):
        typ,r=M.fetch(i,'(INTERNALDATE RFC822)')
        hdr=r[0][0] if r and r[0] else b''
        raw=b''.join(x[1] for x in r if isinstance(x,tuple))
        if not raw: continue
        try:
            idt=imaplib.Internaldate2tuple(hdr)
            if idt and time.mktime(idt)<since-10: continue
        except Exception: pass
        c=extract(email.message_from_bytes(raw))
        if c: found=c; fid=i; break
    if not found: time.sleep(6)
# удалить письмо с кодом из INBOX и из Корзины (если не просили сохранить через MIREA_KEEP_OTP=1)
if found and not KEEP:
    try:
        M.select('INBOX'); M.store(fid,'+FLAGS','\\Deleted'); M.expunge()
    except Exception: pass
    try:
        typ,boxes=M.list()
        for b in (boxes or []):
            s=b.decode('utf-8','replace'); low=s.lower()
            if '\\trash' not in low and 'trash' not in low and 'корзин' not in low: continue
            nm=s.split(' "/" ')[-1].strip().strip('"') if ' "/" ' in s else s.split()[-1].strip('"')
            try:
                if M.select(nm)[0]!='OK': continue
                typ,s2=M.search(None,'ALL'); dc=0
                for j in reversed((s2[0].split() if s2 and s2[0] else [])[-20:]):
                    typ,r=M.fetch(j,'(RFC822)')
                    raw=b''.join(x[1] for x in r if isinstance(x,tuple))
                    if raw and extract(email.message_from_bytes(raw))==found:
                        M.store(j,'+FLAGS','\\Deleted'); dc+=1
                if dc: M.expunge()
            except Exception: pass
    except Exception: pass
try: M.logout()
except Exception: pass
print(found)
PY
)
  [ -n "$code" ] || die "код из письма не пришёл за 90с (проверь app-password/папку Спам)"
  if [ "${MIREA_KEEP_OTP:-0}" = "1" ]; then
    echo "код из письма: $code (письмо оставлено: MIREA_KEEP_OTP=1)"
  else
    echo "код из письма: $code (письмо удалено из почты; сохранить — MIREA_KEEP_OTP=1)"
  fi
  local ea; ea=$(extract_la "$DIR/EC.html")
  cj -e "$SSO/" --data-urlencode "emailCode=$code" -o "$DIR/fp.html" "$ea" >/dev/null
  grep -q KEYCLOAK_IDENTITY "$JAR" || cj -e "$SSO/" --data-urlencode "code=$code" -o "$DIR/fp.html" "$ea" >/dev/null
  grep -q KEYCLOAK_IDENTITY "$JAR" || die "код не принят/просрочен"
  _finish_oidc "$DIR/fp.html"
}

case "${1:-}" in
  check)   cmd_check ;;
  start)   cmd_start ;;
  otp)       shift; cmd_otp "$@" ;;
  otp-email) cmd_otp_email ;;
  courses) cmd_courses ;;
  course)  shift; cmd_course "$@" ;;
  folder)  shift; cmd_folder "$@" ;;
  get)     shift; cmd_get "$@" ;;
  topdf)   shift; cmd_topdf "$@" ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
