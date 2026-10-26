---
name: mirea-moodle
description: Use this skill whenever a task involves online-edu.mirea.ru or MIREA Moodle — logging in through Keycloak SSO with a one-time code, listing enrolled courses, browsing course folders, downloading lectures/practice files or datasets, or converting downloaded assignments to PDF. Trigger it even when the user only says «скачай методичку», «найди курс», «сделай рабочую тетрадь», «МИРЭА», or names a MIREA course/subject, because the login and download flow is non-obvious and requires the bundled script.
---

# MIREA Moodle (online-edu.mirea.ru)

The site is Moodle behind Keycloak SSO with mandatory 2FA. The bundled
`scripts/mirea.sh` already implements the whole curl flow — use it instead of
re-deriving the OIDC dance. It keeps cookies and intermediate pages in
`$MIREA_DIR` (default `~/.mirea`).

## Before starting

- **Credentials come from the environment**: `KC_USER` and `KC_PASS`. Ask the
  user for them if they are missing. Never write the password into a file, a
  commit, or a command that will be echoed back.
- **2FA**: if `MAILRU_USER` and `MAILRU_APPPASS` (Mail.ru app password) are
  set, `start` switches 2FA to email and reads the code over IMAP itself —
  login is one command, no human needed. Otherwise the code goes to the
  user's phone/MAX: ask for the 6 digits and pass them to `otp`. The code
  lives ~2–5 minutes, so request it immediately. Never try to bypass or
  guess it.
- **Network**: some sandboxes/containers route traffic through an egress proxy
  that blocks `mirea.ru`, and IPv6 can hang. The script always uses
  `curl --noproxy '*' -4`. If you write any raw curl yourself, keep both flags.
- Work in a user-visible output directory, e.g. `./downloads`, and report
  absolute paths. Don't paste binary files into chat.

## Workflow

Paths below are relative to this skill's base directory, which the agent
(Claude Code / OpenCode) prints when the skill loads. Run the script from there (or use its absolute path):

```bash
bash scripts/mirea.sh check
```

1. **Verify connectivity** — `check` prints HTTP codes for Moodle and SSO.
   `000` means the proxy/IPv4 flags were lost or the network is down.
2. **Start login** — `start` submits the username/password and moves to the OTP
   page. It prints the masked phone number.
3. **Ask the user for the OTP**, then run `otp <code>`. Success prints
   `OK: вошли в Moodle` and stores the `sesskey`.
4. **Find the course** — `courses`, then `course <course-id>` to list folders
   and resources.
5. **List a folder** — `folder <folder-id>` prints filenames with direct
   `pluginfile.php` URLs.
6. **Download** — `get '<url>' [outdir]`. The URL must be downloaded with the
   session cookie jar; a plain `curl` without the jar returns a login page.
7. **Convert if needed** — `topdf file.docx [outdir]` (LibreOffice headless).

```bash
export KC_USER='...' KC_PASS='...'
bash scripts/mirea.sh check
bash scripts/mirea.sh start
bash scripts/mirea.sh otp 123456
bash scripts/mirea.sh courses
bash scripts/mirea.sh course <course-id>
bash scripts/mirea.sh folder <folder-id>
bash scripts/mirea.sh get 'https://online-edu.mirea.ru/pluginfile.php/.../file.docx' downloads
bash scripts/mirea.sh topdf downloads/file.docx downloads
```

## Finding IDs

Course and folder IDs are per-student and change between semesters. Always
discover them: `courses` → pick the course id → `course <id>` → pick the
folder id → `folder <id>`. Don't hardcode IDs.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `HTTP 000` from `check` | Proxy or IPv6; keep `--noproxy '*' -4` |
| `не нашёл loginAction` | Keycloak form changed; inspect `$MIREA_DIR/login.html` |
| `логин/пароль не прошли` | Bad credentials or account locked; ask the user |
| `код не принят или просрочен` | OTP expired; rerun `start` and ask for a fresh code |
| `FAIL: servicerequireslogin` | Moodle session expired; rerun `start` |
| `нет sesskey` | Login did not finish; complete `start` + `otp` first |
| Downloaded file is HTML | URL fetched without the cookie jar; use `get` |
| Cyrillic filename looks percent-encoded | `curl -OJ` saved it encoded; decode with `python3 -c "import urllib.parse,sys;print(urllib.parse.unquote(sys.argv[1]))"` |

## Optional: automatic email OTP

`otp-email` is a draft feature that reads the code from a Mail.ru inbox. It
needs `MAILRU_USER` and `MAILRU_APPPASS` (an app password). By default it
**deletes the OTP email** from INBOX and Trash after reading it; set
`MIREA_KEEP_OTP=1` to keep it. Only use this if the user explicitly asks for
automated OTP and understands the mailbox behavior.

## Reference

`references/runbook.md` contains the full protocol notes: OIDC endpoints,
`loginAction` extraction, form_post field names, AJAX course-list request, and
the original delegation template. Read it only when the script fails or you
need to modify the flow.
