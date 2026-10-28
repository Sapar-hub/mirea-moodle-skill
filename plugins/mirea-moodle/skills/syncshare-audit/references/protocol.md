# Protocol notes — SyncShare coverage audit

This is the long-form reference for `SKILL.md`. Read it before changing the
harness, adding a request, or interpreting an empty response.

## 1. What is being measured

SyncShare is a crowd-sourced answer database for Moodle quizzes. Its client
sends, for every question, a content-derived `questionIdentity` plus Moodle
ids. Other clients read aggregated answers with
`GET /api/v2/quiz/solution`.

The audit answers three questions:

1. Is a given question present in the database?
2. What exactly does the response contain (a suggested answer, a distribution,
   both, or nothing)?
3. If nothing: is it missing from the database, unsupported by the extension,
   or not a quiz at all?

The default audit is **GET-only**. `query` and `probe` never write. The local
mock accepts the extension's `POST quiz/review`, `quiz/attempt`,
`quiz/attempt/summary` and `quiz/solve` so the harness can capture identities
and complete write payloads, but that traffic is mapped to `127.0.0.1` and never
reaches the real service.

An explicit opt-in path (`export` → `share`) can replay those captured payloads
to production. It is described in section 11 and is never called by `run`. That
path sends question text and answers and has no undo.

## 2. Why the real extension computes the identity

`questionIdentity` is a hash over the parsed question: formulation text,
option labels, and some DOM details. Re-implementing that parser in Python is
possible but fragile: whitespace handling, `answernumber` removal, images, and
Moodle's markup variations all change the hash. A wrong hash produces `200 []`,
which is indistinguishable from "not in the database".

The harness therefore runs the published extension in a local browser against
saved pages. The extension computes the identity, sends it to a local mock
(`syncshare.ru` mapped to `127.0.0.1`), and the mock records the request. No
question text leaves the machine.

Validated example from a synthetic page: identity `37566892` (question
"Сколько будет 2 + 2?" with options 3/4). The harness reproduces it.

### Startup settings race in 2.19.0

The content script calls `getSettings` before parsing. If the background has
not bound the `get-settings` topic yet, the bus returns `null`; the script then
throws on `allowTechnicalDataCollection` and never sends anything. The debug
log shows `Got settings {settings: null}` followed by a page error. This is a
race in the extension, not a page problem.

`setup.sh` applies a local fallback to `getSettings`: if the background does
not return a valid settings object, the content script uses telemetry-off
defaults instead of crashing. It touches only settings retrieval; the parser
and identity hash are untouched. With the patch, a 10-question attempt page
produced 10 solution GETs on every smoke run; without it, the same page
alternated between 10 and 0.

## 3. Data flow

```
Moodle (online-edu.mirea.ru)
  │  curl with the mirea-moodle cookie jar
  ▼
saved pages  pages/<cmid>/<attempt>_p<N>.html
  │
  ▼
local HTTP server 127.0.0.1:8081
  │  injects data-userid + breadcrumb (review pages lack them)
  ▼
Chrome + unpacked extension
  │  parses .que, computes questionIdentity
  │  attempt: GET  quiz/solution per question
  │  review:  POST quiz/review (all questions in one body)
  ▼
local HTTPS mock syncshare.ru:8443  ──►  capture.jsonl
                                              │
                                              ▼
                              audit.py identities / query
                                              │
                                              ▼
                              production GET quiz/solution (anonymous)
```

Only the last step talks to the real service, and it sends ids + hash only.

## 4. Production request (GET, read-only)

```
GET https://syncshare.ru/api/v2/quiz/solution
  ?client=2.19.0
  &host=online-edu.mirea.ru
  &courseId=<course>
  &quizId=<quiz>
  &attemptId=<attempt>
  &moodleId=<user id>
  &questionIdentity=<hash>
  &questionType=multichoice
  &questionId=<qid>
```

Headers used by the extension: `accept: */*`, `sec-fetch-site: none`. No
cookies, no `X-Api-Key` for anonymous mode.

Response for a covered multiple-choice question:

```json
[{
  "anchor": ["..."],
  "suggestions": [{"correctness": 2, "confidence": 0.92, "label": "...",
                   "data": {"anchor": ["..."]}}],
  "submissions": [{"correctness": 2, "count": 92, "label": "..."},
                  {"correctness": 0, "count": 1,  "label": "..."}]
}]
```

- `suggestions` — recommended answer(s) with confidence. This is what the
  extension offers to autofill.
- `submissions` — how many clients chose each label and with what outcome.
- `correctness`: `2` = correct, `1` = partially correct, `0` = incorrect.
- Empty array — no row for this identity.

## 5. Lookup scope (measured)

The API accepts `courseId`, `quizId`, `attemptId`, and `moodleId`, but they do
not affect the lookup. Verified with a covered question:

| Variation | Result |
| --- | --- |
| baseline | data |
| `quizId=1` | same data |
| `courseId=1` | same data |
| `moodleId=1` | same data |
| `host=example.com` | `[]` |
| bogus `X-Api-Key` | same data |

Therefore the key is effectively `host` + content hash. Consequences:

- coverage is per question, not per course or quiz;
- a different institution's host does not see this corpus;
- randomised question text never matches, because every variant hashes
  differently;
- an invalid token does not downgrade to an error; it is treated as anonymous.

`audit.py probe` reproduces the table for the first question of each quiz.

## 6. Coverage taxonomy

`audit.py query` classifies every question as one of:

| status | meaning |
| --- | --- |
| `empty` | `200 []`; the hash is not in the database |
| `suggestions+submissions` | full answer key + distribution |
| `suggestions` | suggested answer only |
| `submissions` | distribution only |
| `anchor-only` | neither, but a row exists |
| `error` | network/HTTP failure after retries |

A page with `.que` but no captured request is a different case: the extension
does not support that type. The current bundle has no `essay` handling, so
open-answer questions produce zero requests. Non-quiz activities (assignments,
labs, workbooks) never run the content script at all.

Report these three cases separately:

1. missing from DB (`empty`);
2. unsupported question type (`essay`, etc.);
3. not a quiz (`mod/assign`, workbook, lab).

## 7. Moodle page quirks

- **Secure review layout**: `review.php` and `attempt.php` often have no
  breadcrumb and no profile link. The extension then reports "page is not
  compatible" and sends nothing. The mock injects `<div class="breadcrumb">`
  and `data-userid` before loading the page. These affect only `quizMeta`, not
  the identity.
- **Pagination**: Moodle may put one question per page (10–20 pages) or all
  questions on one page. The fetch command uses `page=` links when present,
  otherwise the advertised question count.
- **Essay pages**: one question per page, no `page=` links, no options. Fetch
  by total count; expect zero solution requests.
- **Closed windows**: no `startattempt.php` form. The view page still shows
  attempts/time, but there is no way to fetch questions without teacher access.
- **Single attempt**: `Разрешено попыток: 1` plus no review means starting the
  quiz spends the only attempt. The CLI refuses unless explicitly overridden.
- **In-progress attempts**: the view page contains
  `attempt.php?attempt=N`. The fetch command resumes that attempt instead of
  starting another.

## 8. Harness details

- The extension is pinned by Web Store id; `setup.sh` verifies the SHA-256 of
  the CRX against the reviewed 2.19.0 build and warns on mismatch.
- The mock must answer `/api/ofrep/v1/evaluate/flags` with
  `200 {"flags":[]}`; otherwise the content script crashes on
  `allowTechnicalDataCollection`.
- External assets must be aborted. Otherwise the page waits on CORS failures
  and the settings message races; the symptom is
  `Cannot read properties of null (reading 'allowTechnicalDataCollection')`.
- The runner detects progress by counting `quiz/solution` GETs and
  `quiz/review`, `quiz/attempt`, `quiz/attempt/summary` and `quiz/solve` POSTs
  in `capture.jsonl`, not by page request events: those requests originate in
  the service worker. Counting lines rather than bytes prevents an OFREP request
  from looking like a successful page.
- Review pages publish everything in one `POST /api/v2/quiz/review` with
  `quizMeta` and `questions[].identity`; attempt pages send one GET per
  question. `captured_questions()` parses both.
- `harness --mode review` (default) loads `review.php`. `--mode attempt` serves
  the same saved HTML as `attempt.php` and the runner dispatches `beforeunload`
  explicitly, because the attempt payload is published from that handler and
  Playwright's normal `page.close()` does not fire it. `--mode summary` loads a
  saved `view.php` with URL meta params; the board script POSTs
  `quiz/attempt/summary` when the page has `.quizreviewsummary` or
  `.quizattemptsummary`.
- Before a new run the CLI copies the previous `capture.jsonl` to
  `$SYNC_AUDIT_DIR/captures/<timestamp>.jsonl`. `export` reads the current file,
  so this prevents an earlier write payload from being lost.
- Each tab is brought to the foreground; background tabs are throttled by
  Chrome and the extension's settings round-trip then times out.
- The content script occasionally loses the settings race even so. The runner
  reloads a page up to three times, and `audit.py harness` retries pages that
  still produced nothing in a fresh browser session (`--retries`, default 2).
- Chrome must run headed (or under `xvfb-run`); extension loading is disabled in
  old headless mode.
- The mock writes `capture.jsonl` on every HTTPS request, including OFREP and
  auth probes. `identities` filters only the two identity transports.

## 9. Reproducing the original measurements

The 2026-10-26 run produced:

- БЖД `943704`: 10/10, 82–98 submissions; `943705`: 1/1, 108.
- МП `938176`: 5/5, 93; `938177`: 6/6, 55–56.
- ИО `931671`: 0/10 (`[]`); `931670` and `931674`: 10/10, 38–39 (same bank).
- ПСИИ `933035`: 10/10, 73; `933036`: 10/10, 47.
- ИАД `937792` and `960136`: 20/20 essay each, zero solution requests.
- Closed/restricted: ИИС `931554/960123/960125`, ИАД `937796`,
  БЖД `943709/943710`, МП `938180`, ПСИИ `933039`.

These numbers depend on other users' uploads and will drift. Re-run rather than
quoting them as constants.

## 10. Limitations

- The identity is whatever the installed extension version computes. A version
  change can change the hash and silently invalidate comparisons.
- The audit sees only questions reachable from the user's account and only
  while the quiz window is open (or a review exists).
- The response is aggregate; it does not prove who uploaded a question or when.
- No write-path test, no rate-limit measurement, no server-side storage
  inspection.
- Starting attempts is visible in Moodle logs and may consume attempts. Do not
  do it on a production account without the user's explicit consent.
