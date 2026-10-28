# SyncShare coverage report — template

Use this after `audit.py report` has produced the raw table. Keep the method and
limitations sections; they are what makes the numbers reproducible.

## Scope

- Moodle host: `online-edu.mirea.ru`
- Account: test account owned by the requester
- Measured: anonymous `GET /api/v2/quiz/solution` only
- Not measured: write endpoints (`quiz/attempt`, `quiz/review`), rate limits,
  server-side storage

## Method

1. Logged in with the `mirea-moodle` skill; quiz inventory from
   `course/view.php` and `mod/quiz/view.php`.
2. Saved review pages where they existed; for open quizzes without a review,
   started an attempt and saved every page.
3. Ran the published SyncShare extension against the saved pages in a local
   mock. Only the extension computed `questionIdentity`; no question text was
   sent to SyncShare.
4. Queried production anonymously with `host`, ids, and hash. One request per
   second.
5. Classified: `empty` / `suggestions+submissions` / `suggestions` /
   `submissions` / no request (unsupported type).

## Coverage

| Дисциплина | Тест | Тип | Вопросов | С данными | Ответов |
|---|---|---|---|---|---|
| ... | ... | multichoice | 10 | 10/10 | 82–98 |

## What the payload contains

```json
[{
  "suggestions": [{"correctness": 2, "confidence": 0.92, "label": "..."}],
  "submissions": [{"label": "...", "count": 92, "correctness": 2}]
}]
```

- `suggestions` — autofill candidate.
- `submissions` — aggregate distribution.
- `correctness`: 2/1/0 = correct/partial/incorrect.
- Empty array = hash absent.

## Why some tests return nothing

State which case applies to each empty quiz:

- **Not in the database** — static text, `200 []`, zero submissions. Nothing is
  wrong with the client; nobody uploaded this bank.
- **Unsupported type** — `essay`/open answer: the extension sends no request,
  so there is nothing to look up even if the text exists.
- **Not a quiz** — assignment, lab, workbook: content scripts run only on
  `mod/quiz/*`.
- **Randomised text** — each variant hashes differently, so matches are
  unlikely even if the bank is partially covered.

## Scope of the lookup

`host` + content hash is the key. `courseId`, `quizId`, `attemptId`, and
`moodleId` do not change the result; a foreign host returns `[]`. Include the
probe table if it was run.

## Attempts created

List attempt ids, quizzes, and whether the quiz allows one attempt. A
single-attempt quiz that was opened will be graded 0 unless the user finishes
it; say so explicitly.

## Limitations

- Coverage changes as other users upload; these are point-in-time numbers.
- Extension version changes can change `questionIdentity`.
- Only questions reachable from this account/window were measured.
- Aggregate answers only; no student identities are exposed by the API.
- No write-path or rate-limit testing.
