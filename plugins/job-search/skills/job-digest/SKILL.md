---
name: job-digest
description: Use this skill whenever the user asks about job/internship postings from subscribed sources — «что нового по вакансиям», «есть что в Changellenge», «покажи стажировки за неделю», «добавь канал с вакансиями», «подпишись на ещё один tg-канал», or wants the daily vacancy digest set up, changed, or debugged. It pulls Telegram channels and RSS feeds through a local RSSHub, filters them by keywords, and drops a daily digest into the user's Mail.ru folder «Вакансии».
---

# job-digest

Pipeline: `feeds.txt` → RSSHub (`127.0.0.1:1200`, docker) → `scripts/digest.py`
(keyword filter, dedup in sqlite) → IMAP APPEND into the Mail.ru folder
`Вакансии` → systemd `--user` timer once a day.

Files:
- config: `~/.config/job-digest/feeds.txt`, `keywords.txt` (regexes, one per line)
- state: `~/.local/state/job-digest/items.db` (every item seen, `sent_at` once mailed)
- credentials: `MAILRU_USER`, `MAILRU_APPPASS` (Mail.ru app password) from the
  environment (fish universal vars on the author's machine) or `~/.config/job-digest/env`.
  Never print, echo, or commit the password.

## Answering "what's new"

Read the local base first; it needs no network:

```bash
python3 -I scripts/digest.py show --days 7         # what passed the filter
python3 -I scripts/digest.py show --days 7 --all   # including noise
```

Summarise per vacancy (role, company, conditions, link) and point out the ones
that match the user's profile: DS / ML / data analytics / product analytics
internships and junior roles, Moscow or remote. For fresh data, run
`digest.py run` first: it fetches and mails new items.

## Adding a source

- Telegram channel: append `http://127.0.0.1:1200/telegram/channel/<username>`
  to `feeds.txt` (the channel must be public: `https://t.me/s/<username>` opens without login).
- Any other site: find the RSSHub route at https://docs.rsshub.app or use the site's own RSS/Atom URL.
- Check with `digest.py feeds`, then `digest.py run --dry-run`.
- The first run on a new feed mails its whole backlog (~20 posts). That is expected.

## Tuning the filter

A post is "matching" if a line of it is ≤140 chars, has ≥2 words and hits a
regex from `keywords.txt`. The short-line rule keeps vacancy lists in and
pulls story paragraphs out. Non-matching new posts still go into the next digest
under «Прочее», as titles only. A digest is sent only when at least one post matches
(`--force` sends anyway).

Noisy feeds (middle/senior, every profession) get `strict` after the URL in `feeds.txt`.
There a post needs a level word (`+regex` in `keywords.txt`: стажёр, junior, младший…) and
a vacancy line that hits a topic word; a one-vacancy post is judged by its first line, and its
«Офис (город)» must be Moscow or remote. Strict posts that miss are archived without going to «Прочее».
Prefer tightening the filter over dropping a source. Posts older than 14 days
(`JOB_DIGEST_MAX_AGE_DAYS`) are stored but not mailed, so a new feed's backlog stays out.
The same vacancy link reposted by several channels is shown once.

## Install / debug

```bash
scripts/install.sh                      # rsshub + config + timer (daily 09:13)
ONCALENDAR='*-*-* 09,19:13' scripts/install.sh
systemctl --user list-timers job-digest.timer
journalctl --user -u job-digest -n 50   # last runs
docker logs --tail 30 rsshub            # feed errors
```

- `ConnectTimeoutError t.me` in rsshub logs: Telegram is reachable only through
  the proxy. Recreate the container with `RSSHUB_PROXY=http://127.0.0.1:18080`
  (`docker rm -f rsshub && scripts/install.sh`). The container uses `--network host`
  so it can see a proxy on host loopback.
- IMAP `AUTHENTICATIONFAILED`: the app password was revoked; ask the user for a new one.
