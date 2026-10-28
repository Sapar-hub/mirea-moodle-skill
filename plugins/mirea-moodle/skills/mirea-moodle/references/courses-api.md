# Как `mirea.sh courses` получает список курсов

Один POST во внутренний AJAX API Moodle — тот же, что использует блок
«Обзор курсов» на `/my/`.

## Запрос

```
POST https://online-edu.mirea.ru/lib/ajax/service.php
     ?sesskey=<sesskey>
     &info=core_course_get_enrolled_courses_by_timeline_classification
Cookie: MoodleSession=…        ← из $MIREA_DIR/jar.txt
Content-Type: application/json

[{"index":0,
  "methodname":"core_course_get_enrolled_courses_by_timeline_classification",
  "args":{"offset":0,"limit":0,"classification":"all",
          "sort":"fullname","customfieldname":"","customfieldvalue":""}}]
```

- **Авторизация** — двухфакторная по сути:
  - cookie `MoodleSession` — кто ты;
  - `sesskey` в URL — CSRF-токен; достаётся при логине из `M.cfg` на `/my/`
    и хранится в `$MIREA_DIR/sesskey`.
  - Токен `webservice` не нужен: это не REST API (`/webservice/rest/server.php`),
    а внутренний AJAX веб-интерфейса. Доступны только функции с `ajax=true`.
- **Тело** — массив вызовов; можно батчить несколько методов с разными `index`.
- **`args`**:
  - `classification`: `all | inprogress | future | past | favourites | hidden` —
    те же вкладки, что в UI;
  - `limit:0` — без пагинации, все курсы сразу;
  - `sort:"fullname"` — по алфавиту.

## Ответ

```json
[{"error":false,
  "data":{"nextoffset":9,
          "courses":[{"id":18561,"fullname":"Безопасность жизнедеятельности_Зачет …",
                      "shortname":"…_[I.26-27]_148","progress":60,
                      "startdate":1788210000,"enddate":0,"visible":true,
                      "viewurl":"https://online-edu.mirea.ru/course/view.php?id=18561",
                      "summary":"<html…>","courseimage":"data:image/svg+xml;base64,…", …}]}}]
```

- Поля курса: `id, fullname, shortname, idnumber, summary, summaryformat,
  startdate, enddate, visible, showactivitydates, showcompletionconditions,
  pdfexportfont, fullnamedisplay, viewurl, courseimage, progress, hasprogress,
  isfavourite, hidden, showshortname, coursecategory`.
- Скрипт печатает только `id :: fullname`.
- Основной вес — `courseimage` (base64) и `summary` (HTML): ~115 КБ на 9 курсов.
- Ошибка: `{"error":true,"exception":{"errorcode":"servicerequireslogin"}}` —
  сессия протухла.

## Логика `cmd_courses`

1. `need_sesskey` — читает `$MIREA_DIR/sesskey`; нет файла → FAIL «войди».
2. Запрос через `cj` (curl `--noproxy '*' -4`, общий cookie-jar) →
   `courses.tmp.json`.
3. Python разбирает `d[0]`:
   - `error` → `FAIL: <errorcode>` в stderr, exit 1, `courses.json` не трогается;
   - иначе печать курсов и `mv courses.tmp.json courses.json`.

## Дальше

`course <id>` уже не API, а парсинг HTML `/course/view.php?id=…` (ссылки
`mod/resource` и `mod/folder`). Надёжнее было бы перевести его на
`core_courseformat_get_state` или `core_course_get_contents`.

## Логин (для контекста)

| Шаг | Запрос | Что происходит |
|---|---|---|
| 1 | `GET /auth/oidc/?source=loginpage` | `auth_oidc` → редирект на `sso.mirea.ru`; в `kcContext` — `loginAction` |
| 2 | `POST <loginAction>`: `username`, `password`, `credentialId=` | Страница 2FA `login-max-otp` (+ SMS/МАКС уходит сразу) |
| 3a | `POST <loginAction>`: `code` | МАКС-код → cookie `KEYCLOAK_IDENTITY` + form_post |
| 3b | `POST`: `authenticationExecution=<email authExecId>`, затем `emailCode` | Код из почты (автоматически, если заданы `MAILRU_*`) |
| 4 | `POST /auth/oidc/`: `code, iss, state, session_state` | Moodle ставит `MoodleSession`; `sesskey` в `M.cfg` на `/my/` |
