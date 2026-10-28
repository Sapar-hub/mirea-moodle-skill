---
name: syncshare-audit
description: Audit SyncShare coverage for MIREA Moodle quizzes and, only with explicit consent, contribute your own attempt data — enumerate quizzes, open attempts only with permission, extract questionIdentity with the real extension inside a local mock, query the public quiz/solution API anonymously, and optionally replay captured quiz/review, quiz/attempt, quiz/attempt/summary or quiz/solve payloads to production. Use this skill whenever the user mentions SyncShare, проверка ответов, покрытие тестов, «почему не все тесты выдают ответы», сравнение тестов и практических работ, questionIdentity, quiz/solution, шаринг/заливка ответов в SyncShare, or asks to repeat the dynamic part of the SyncShare security report.
---

# SyncShare coverage audit

This skill reproduces the dynamic part of the SyncShare review: how much of a
Moodle quiz bank is present in SyncShare's crowd-sourced database, what the
returned payload looks like, and why some quizzes produce answers while others
produce nothing.

It also has an **opt-in write path** (`export` → `share` → `solve`). That path
replays payloads the real extension built from the user's own attempt. It sends
question text and answers to SyncShare and cannot be undone. It is never part of
`run` and does nothing without `--send`.

The hard part is not the HTTP call. It is computing the exact
`questionIdentity` that the extension would compute, without sending question
text to the third party. The bundled harness runs the real extension against
saved Moodle pages inside a local mock, captures the identity, and only then
queries production with `host` + ids + hash.

## Non-negotiable rules

Read these before touching the account. They are not decoration: violating them
turns a measurement into an attack or into academic damage.

- **Authorization**: use only an account the user owns, on the user's own
  Moodle host, and only after the user explicitly agrees to open quiz attempts.
  Opening an attempt is a real Moodle action even if no answer is submitted.
- **GET is read-only**: `query` and `probe` call only
  `GET /api/v2/quiz/solution`; they send `host`, ids and `questionIdentity`,
  never question text.
- **Production writes are opt-in**: the only write command is
  `audit.py share`, and only with `--send` after the user has explicitly agreed.
  It replays payloads captured from the user's own account and host. Never call
  a write endpoint from `run`, from tests, or with another person's data.
- **`--send` is irreversible**: the payload includes question HTML, option
  labels, the selected answers, grades/feedback, and `moodleId`/course/quiz ids.
  There is no documented delete endpoint. Show the dry-run output and say this
  to the user before the first real send.
- **No Moodle submission**: `share` talks to SyncShare only. `solve` prints the
  returned suggestions and stops. Never press "Finish attempt", never fill
  Moodle answers, and never use `solve` to auto-submit a quiz.
- **Essay questions are skipped** by `share` unless `--include-essay` is passed;
  their text can contain personal free-form answers.
- **Single-attempt quizzes**: if `Разрешено попыток: 1` and there is no finished
  review, do not start it without explicit confirmation. Starting consumes the
  only attempt and it will be graded 0 when the timer expires. The fetch
  command refuses unless `--allow-single-attempt` is passed.
- **Do not submit answers** and do not press "Finish attempt" on the user's
  behalf. Leave attempts to expire, or close them only if the user asks.
- **Rate limit**: GETs one per second; writes one per 2 s, with no automatic
  retry. Stop on 429/401/403 instead of hammering a third-party service.
- **Report aggregates, not people**: the API returns answer distributions, not
  student identities. Keep it that way; do not publish other users' data.

If any of these cannot be satisfied, stop and explain why instead of
improvising.

## Dependencies

- The sibling `mirea-moodle` skill for login and `sesskey` (this skill does not
  implement SSO again).
- `bash`, `curl`, `python3`, `node`/`npm`, `openssl`, `unzip`.
- Chrome/Chromium with extension support. On a headless machine, `xvfb-run`.
- `playwright` (installed by `scripts/setup.sh` into the audit directory).

## Setup

```bash
# 1. Log in with the mirea-moodle skill (OTP is asked from the user).
export KC_USER='<student>@edu.mirea.ru' KC_PASS='...'
bash ../mirea-moodle/scripts/mirea.sh start
bash ../mirea-moodle/scripts/mirea.sh otp <code>
bash ../mirea-moodle/scripts/mirea.sh courses

# 2. Prepare the local harness (extension, browser, node deps, TLS cert).
bash scripts/setup.sh
```

`setup.sh` writes everything to `$SYNC_AUDIT_DIR` (default
`~/.syncshare-audit`) and prints an `env.sh` with `CHROME_PATH`,
`SYNC_EXT_DIR`, and `SYNC_AUDIT_DIR`. Source it in later shells.

Everything under `$SYNC_AUDIT_DIR` — saved pages, `capture.jsonl`, identities —
contains course content. Keep it local, never commit it, and delete the
directory when the audit is finished.

Pinned extension: Chrome Web Store id `lngijbnmdkejbgnkakeiapeppbpaapib`.
The setup script downloads the current CRX, unpacks it, and warns if the
SHA-256 differs from the reviewed `2.19.0` build
(`2a91e8ff6335166e45a87a4300785c467f601de7a3c44bd93804280b1a2b7aee`).
A different version may compute a different identity; re-validate before
trusting the numbers.

`setup.sh` also applies one small **local compatibility patch** to the unpacked
copy: 2.19.0 requests its settings from the background at content-script init,
and can get `null` before the background has bound the `get-settings` topic,
which aborts the page before parsing. The patch falls back to telemetry-off
defaults when the background does not answer; it does not touch the question
parser or `questionIdentity`. The CRX hash and the reviewed build are unchanged.

## Workflow

All commands are run from the skill directory, with `SYNC_AUDIT_DIR` exported.
The CLI is `scripts/audit.py`; every subcommand writes JSON under
`$SYNC_AUDIT_DIR`.

### 1. Inventory the quizzes

```bash
python3 scripts/audit.py inventory --all
# or a subset:
python3 scripts/audit.py inventory --course 18561 --course 18855
```

It reads `$MIREA_DIR/courses.json`, fetches each `course/view.php`, extracts
every `mod/quiz/view.php?id=...`, and records title, attempts limit, time limit,
open/close window, whether a start form exists, finished review attempts, and
the advertised number of questions.

Check the table before continuing: a quiz without a start form is closed or
restricted, and no amount of retrying will open it. Quizzes with finished
reviews can be measured without creating a new attempt.

### 2. Fetch questions

```bash
# Preferred: a quiz that already has a review attempt — nothing new is created.
python3 scripts/audit.py fetch --quiz 943704

# Open quizzes: this creates an attempt. Confirm with the user first.
python3 scripts/audit.py fetch --quiz 931670
```

For a review attempt the page is saved as-is. Otherwise the command posts the
Moodle start form (`cmid`, `sesskey`,
`_qf__mod_quiz_form_preflight_check_form=1`), follows the redirect, and saves
every page of the attempt under `$SYNC_AUDIT_DIR/pages/<cmid>/`.

The command refuses 1-attempt quizzes by default. Only add
`--allow-single-attempt` after the user has explicitly accepted that the only
attempt will be spent and graded 0.

Pages are saved for local parsing only. **Do not open them in a browser with
the extension while logged in** — the extension would upload the real content.
The harness deliberately serves them from `127.0.0.1` with all outside
requests blocked.

### 3. Run the extension in the mock

```bash
# GET identities (default)
python3 scripts/audit.py harness --mode review

# also capture the attempt POST and the quiz-board summary
python3 scripts/audit.py harness --mode attempt
python3 scripts/audit.py harness --mode summary
python3 scripts/audit.py harness --mode all
```

`--mode` selects which request path to exercise: `review` (default, POST
`quiz/review`), `attempt` (POST `quiz/attempt` on `beforeunload`), `summary`
(POST `quiz/attempt/summary` from a saved `view.php`), `both` or `all`.

What this does, and why it matters:

- starts a local HTTP server that serves the saved pages and injects the
  profile id + breadcrumb that review pages lack;
- answers SyncShare feature-flag (`OFREP`) probes with `200 {"flags":[]}`;
- starts an HTTPS mock on `127.0.0.1:8443` with a self-signed certificate for
  `syncshare.ru`, mapped via `--host-resolver-rules`;
- launches headed Chrome with the unpacked extension and loads every saved
  page, bringing each tab to the foreground so Chrome does not throttle the
  content script;
- records every extension request to `$SYNC_AUDIT_DIR/capture.jsonl` and counts
  `quiz/solution` GETs plus `quiz/review`, `quiz/attempt`, `quiz/attempt/summary`
  and `quiz/solve` POSTs, so OFREP telemetry cannot masquerade as a successful
  page;
- archives the previous `capture.jsonl` under `$SYNC_AUDIT_DIR/captures/`
  before a new run, so exported write payloads are not lost;
- if a quiz produced no capture, reruns just those pages in a fresh browser
  session (`--retries`, default 2). The settings fallback patch removes the
  known 2.19.0 race; these whole-page retries remain a safety net for Chrome or
  extension startup hiccups.

The extension computes `questionIdentity` itself. This is the point: a
hand-rolled parser is easy to get subtly wrong (whitespace, `answernumber`,
images), and a wrong hash silently reads as "no data".

### 4. Export the captured write payloads

```bash
python3 scripts/audit.py export
# -> $SYNC_AUDIT_DIR/posts.json
```

`export` turns the POST bodies in `capture.jsonl` into replayable JSON. It
deduplicates identical bodies, drops `essay` questions by default, and can
strip the attempt summary (`--strip-summary`). It never talks to the network.

### 5. Share to production (opt-in, irreversible)

```bash
# 1) inspect first; this sends nothing
python3 scripts/audit.py share --endpoint review

# 2) one small real send after the user agrees
python3 scripts/audit.py share --endpoint review --quiz 943704 --limit 1 --send

# 3) the rest
python3 scripts/audit.py share --endpoint review --send --delay 2
```

`share` replays payloads from `posts.json` to
`https://syncshare.ru/api/v2/quiz/{review|attempt|attempt/summary}`. Without
`--send` it is a dry-run. The ledger `$SYNC_AUDIT_DIR/shared.json` records the
sha256 of every body and skips payloads already marked `ok`/`duplicate`;
`--force` re-sends them.

- It refuses a host that is not in `inventory.json` / `online-edu.mirea.ru`.
- `--include-essay` is required to send essay text; it is off by default.
- On `401/403`, `429` or a network error it stops instead of retrying. Fix the
  key/rate limit, check the ledger, then re-run. Do not loop `--force`.
- `SYNCSHARE_API_KEY` (or `--api-key`) is optional; without it the request is
  anonymous and may be rejected.

### 6. AI solve (opt-in, API only)

```bash
python3 scripts/audit.py solve --quiz 943704 --question-id 12345 --api-key "$SYNCSHARE_API_KEY"
```

`solve` takes the question object that the extension already serialized in
`posts.json`, sends it to `POST /api/v2/quiz/solve`, and prints the returned
suggestions as JSON. It **does not touch Moodle**: no autofill, no attempt
submission. The endpoint normally needs a signed-in, paid SyncShare account;
without a key expect `401/403`.

### 7. Query production, read-only

```bash
python3 scripts/audit.py query
# optional: --host online-edu.mirea.ru --delay 1.0 --limit 20
```

It extracts identities from `capture.jsonl`, groups them by `quizId`, and sends
anonymous GETs to `https://syncshare.ru/api/v2/quiz/solution`. Both transports
are parsed: attempt pages send one `quiz/solution` GET per question, review
pages send a single `POST quiz/review` whose body contains
`questions[].identity`. Results are classified as:

- `empty` — `200 []`, the hash is not in the database;
- `suggestions+submissions` — a recommended answer plus the distribution;
- `suggestions` / `submissions` — partial payload;
- no request at all — the extension does not handle that question type
  (for example `essay`).

### 8. Report

```bash
python3 scripts/audit.py report
python3 scripts/audit.py report --full > report.md
```

`report` prints a per-quiz coverage table and lists the attempts created. Use
`references/report-template.md` when writing the prose part; keep the
methodology and limitations sections honest.

### Smoke check

Before a full run, validate the harness on one quiz that already has a review
(no new attempt):

```bash
python3 scripts/audit.py fetch --quiz <review-quiz>
python3 scripts/audit.py harness --quiz <review-quiz>
python3 scripts/audit.py identities --quiz <review-quiz>
python3 scripts/audit.py query --quiz <review-quiz> --limit 1
python3 scripts/audit.py export --quiz <review-quiz>
python3 scripts/audit.py share --quiz <review-quiz>   # dry-run, sends nothing
```

A non-empty `identities.json`, one classified GET and one dry-run payload mean
the extension, mock, parser and exporter agree. Do not use a 1-attempt quiz for
this check.

## Reading the result

Payload shape for a covered multiple-choice question:

```json
[{
  "suggestions": [{"correctness": 2, "confidence": 0.92, "label": "..."}],
  "submissions": [
    {"label": "...", "count": 92, "correctness": 2},
    {"label": "...", "count": 1,  "correctness": 0}
  ]
}]
```

`correctness`: `2` = correct, `1` = partially correct, `0` = incorrect. The
suggestion is what the extension would autofill; the submissions are the
aggregate distribution from other clients.

The lookup key is `host` + content hash. `courseId`, `quizId`, `attemptId`, and
`moodleId` are accepted but ignored for lookup; a foreign `host` returns `[]`.
This is why coverage is per-question, not per-course: one control test can be
fully covered while the next one in the same course is empty. It also means
randomised question text never matches, because every variant hashes
differently.

Three different reasons look identical in the UI ("no answers"):

1. the question is simply not in the crowd-sourced database (`[]`);
2. the question type is unsupported by the extension (`essay`/open answer, so
   no request is even sent);
3. the activity is not a quiz (lab, assignment, workbook) — content scripts
   are declared only for `mod/quiz/*`, so nothing is parsed.

Say which one applies; do not report all three as "SyncShare has no data".

For the write path, the response body is not the result: `share` classifies the
HTTP status only. `2xx` = accepted, `409/422` = duplicate, `401/403` = auth
problem, `429` = rate limit, anything else/network = unknown. A timeout is
recorded as `unknown` and is **not** retried automatically, because a retry can
duplicate a write that actually succeeded.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `no sesskey` | Run `mirea-moodle` `start` + `otp` first |
| `quiz has no start form` | Closed by date or restricted by prerequisites; skip it |
| `refusing 1-attempt quiz` | Expected guard; get explicit consent, then `--allow-single-attempt` |
| Start redirects to login | Moodle session expired; re-login |
| Harness page has `que=0` | Saved page is an error/login page; re-fetch it |
| Extension reports "not compatible" | Review pages lack profile/breadcrumb; the mock injects them — check `MOODLE_ID` |
| `allowTechnicalDataCollection` crash | 2.19.0 settings race; `setup.sh` applies a telemetry-off fallback. Re-run `setup.sh` to patch the unpacked copy, then retry. |
| Empty capture for a page with `.que` | Unsupported type (essay) or a Chrome/extension startup hiccup; the CLI retried already, check the printed type and retry after freeing resources |
| Every pass prints `no capture`, page error mentions `js_pending` | Chrome's content scripts are not initializing. This is an environment failure (low memory / too many Chrome processes), not evidence that the question is missing from SyncShare. Close unused Chrome instances, check `free -h`, retry. |
| `EADDRINUSE` | A stale mock is running; the CLI reuses a healthy one and refuses a foreign listener |
| No OFREP in capture / extension not loaded | Chrome ignored `--load-extension` (snap Chromium or a build with the flag removed); point `CHROME_PATH` at Chrome for Testing or reinstall via `setup.sh` |
| `share` prints `no exported payloads` | Run `harness` first, then `export`; capture is archived per run |
| `share` sends nothing with `--send` | Payload is already `ok`/`duplicate` in `shared.json`; use `--force` only after checking the ledger |
| `share` stops with `auth` | Set `SYNCSHARE_API_KEY` / `--api-key`; anonymous writes may be rejected |
| `share` stops with `rate_limited` | Wait for `Retry-After`, then re-run; do not loop |
| `share` returns `error`/`unknown` | Inspect `shared.json`; the write may have landed. Do not `--force` blindly |
| `summary skipped — no finished/in-progress attempt` | The saved `view.php` has no attempt to report; use a quiz with a finished review |
| `summary table not found` | Saved `view.php` lacks `.quizreviewsummary`/`.quizattemptsummary`; re-fetch the view page or skip summary |
| `attempt` POST missing | Use `harness --mode attempt`; the runner dispatches `beforeunload` explicitly |
| `solve` returns 401/403 | Needs a signed-in, paid SyncShare account and `X-Api-Key` |

## References

- `references/protocol.md` — identity algorithm, GET/write API schemas, scope
  experiments, review-page quirks, and the exact limitations of the method.
- `references/report-template.md` — coverage table + honest limitations.
- `scripts/audit.py` — inventory/fetch/harness/identities/export/share/solve/
  query/report.
- `scripts/setup.sh` — extension, browser, cert, node deps.
- `scripts/harness/` — mock server and Playwright runner.
- `scripts/tests/test_share.py` — offline export/share/solve tests.

Read `protocol.md` before changing the harness or adding a new endpoint. The
short version is: GET is hash-only and read-only; writes are opt-in, one per
2 s, and never submitted to Moodle.
