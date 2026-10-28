#!/usr/bin/env python3
"""Раскладывает входящие Mail.ru по папкам по правилам из mail-rules.json.

  mailsort.py [--dry-run]

Правила: $JOB_DIGEST_CONFIG/mail-rules.json (по умолчанию ~/.config/job-digest). Только MOVE
и \\Seen — ничего не удаляет. Почта: MAILRU_USER, MAILRU_APPPASS.
"""
import base64, datetime as dt, imaplib, json, os, socket, sys
from pathlib import Path

CONF = Path(os.environ.get("JOB_DIGEST_CONFIG", "~/.config/job-digest")).expanduser()
DRY = "--dry-run" in sys.argv


def utf7(s):
    """Имя папки в modified UTF-7 (RFC 3501)."""
    out, buf = [], ""
    def flush():
        if buf:
            out.append("&" + base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-")
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7e:
            flush(); buf = ""
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return '"' + "".join(out) + '"'


def search(m, *crit):
    typ, d = m.uid("SEARCH", "CHARSET", "UTF-8", *crit)
    return d[0].split() if typ == "OK" and d[0] else []


def main():
    rules = json.loads((CONF / "mail-rules.json").read_text(encoding="utf-8"))["folders"]
    socket.setdefaulttimeout(120)
    m = imaplib.IMAP4_SSL("imap.mail.ru")
    m.login(os.environ["MAILRU_USER"], os.environ["MAILRU_APPPASS"])
    existing = {l.decode().rsplit(' "/" ', 1)[-1] for l in m.list()[1]}
    total = 0
    for f in rules:
        box = utf7(f["name"])
        if box not in existing and box.strip('"') not in existing and not DRY:
            m.create(box)
        m.select("INBOX", readonly=DRY)
        uids = set()
        for r in f["rules"]:
            subj = ["SUBJECT", r["subject"].encode()] if r.get("subject") else []
            for frm in r["from"]:
                uids |= set(search(m, "FROM", f'"{frm}"'.encode(), *subj))
        uids = sorted(uids, key=int)
        if uids and not DRY:
            for i in range(0, len(uids), 200):
                typ, _ = m.uid("MOVE", b",".join(uids[i:i + 200]), box)
                if typ != "OK":
                    sys.exit(f"MOVE в {f['name']} не прошёл")
        total += len(uids)
        read = 0
        if f.get("mark_read_after_days"):
            before = (dt.date.today() - dt.timedelta(days=f["mark_read_after_days"])).strftime("%d-%b-%Y")
            typ, _ = m.select(box, readonly=DRY)
            old = search(m, "UNSEEN", "BEFORE", before) if typ == "OK" else []   # в dry-run новой папки ещё нет
            read = len(old)
            if old and not DRY:
                for i in range(0, len(old), 200):
                    m.uid("STORE", b",".join(old[i:i + 200]), "+FLAGS", "(\\Seen)")
        print(f"{f['name']:24s} перенесено {len(uids):4d}   прочитано (старше {f.get('mark_read_after_days', '-')} дн) {read}")
    print(f"ИТОГО перенесено {total}" + (" (dry-run)" if DRY else ""))


if __name__ == "__main__":
    main()
