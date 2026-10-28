# Runbook: автоматизация работы с online-edu.mirea.ru (Moodle + Keycloak SSO)

## 0. Траектория успеха (что поручать в новой сессии)
Команда вида: «Сделай рабочую тетрадь №N по предмету X» →
1. логин в SSO (нужен твой OTP-код вживую);
2. найти курс по названию;
3. найти папку с практиками, скачать нужный .docx + датасет;
4. выполнить работу / сконвертировать / разобрать;
5. отдать результат.

## 1. Сетевое условие (ОБЯЗАТЕЛЬНО)
В песочницах/контейнерах с egress-прокси домены mirea.ru бывают заблокированы, IPv6 может висеть.
Все запросы слать В ОБХОД него и по IPv4:
  curl --noproxy '*' -4 ...  (+ один cookie-jar на весь флоу, напр. -c jar.txt -b jar.txt)
Проверка связи: curl --noproxy '*' -4 -sS -m15 -o /dev/null -w '%{http_code}\n' https://online-edu.mirea.ru/

## 2. Аутентификация (Keycloak OIDC, realm=mirea, client=moodle)
Шаг A. Старт OIDC (ведёт на форму Keycloak):
  GET https://online-edu.mirea.ru/auth/oidc/?source=loginpage   (-L, cookie-jar)
  → из тела вытащить "loginAction" (URL с session_code + execution).

Шаг B. Логин/пароль (поля: username, password, credentialId=""):
  POST <loginAction>
  --data-urlencode username=$KC_USER   # e.g. <student>@edu.mirea.ru
  --data-urlencode password=$KC_PASS
  --data-urlencode credentialId=
  → при успехе страница ПЕРЕХОДИТ на 2-й фактор (pageId меняется на "login-max-otp",
    появляется "attemptedUsername" и maskedPhone). Ошибки поля НЕТ — это нормально.

Шаг C. 2FA / OTP (ЧЕЛОВЕК В ЦИКЛЕ):
  Код приходит на МАКС/телефон (или почту). Пользователь присылает 6 цифр.
  Из текущей страницы снова вытащить свежий "loginAction", затем:
  POST <loginAction>  --data-urlencode code=<OTP>      # ВАЖНО: имя поля именно "code"
  → успех = в cookie-jar появляется KEYCLOAK_IDENTITY, тело = авто-сабмит form_post
    (атрибуты в ВЕРХНЕМ регистре: NAME="code"/"iss"/"state"/"session_state").

Шаг D. Возврат кода в Moodle (завершение OIDC):
  POST https://online-edu.mirea.ru/auth/oidc/
  --data-urlencode code=... --data-urlencode iss=https://sso.mirea.ru/realms/mirea
  --data-urlencode state=... --data-urlencode session_state=...   (-L, тот же jar)
  → редирект на /my/ , в теле есть "Выход" и data-userid → залогинен.

## 3. Навигация и скачивание
- sesskey: grep '"sesskey":"..."' с любой страницы Moodle.
- Список курсов (AJAX):
  POST https://online-edu.mirea.ru/lib/ajax/service.php?sesskey=<S>&info=core_course_get_enrolled_courses_by_timeline_classification
  body: [{"index":0,"methodname":"core_course_get_enrolled_courses_by_timeline_classification",
          "args":{"offset":0,"limit":0,"classification":"all","sort":"fullname",
                  "customfieldname":"","customfieldvalue":""}}]
  → data.courses[].{id,fullname}
- Курс: GET course/view.php?id=<CID> → ссылки mod/folder/view.php?id=<FID> и mod/resource/view.php?id=<RID>
- Папка: GET mod/folder/view.php?id=<FID> → ссылки pluginfile.php/.../*.ext
- Скачать файл: curl --noproxy '*' -4 -L -OJ '<pluginfile-url>'  (имена кириллица — %-encoded)
- docx→pdf: soffice --headless --convert-to pdf --outdir . file.docx   (есть libreoffice/pandoc)

## 4. Ограничения (почему «куки на 24ч» пока не работает)
- 2FA обязателен на КАЖДЫЙ вход → нужен живой OTP, сессию нельзя оформить заранее.
- OTP живёт ~2–5 мин → код присылать сразу, иначе повторяем шаги A–B и просим новый.
- Куки Keycloak/Moodle не переживают смену контейнера/сессии.
- Выгрузка файлов в чат недоступна в сессиях без привязки к проекту →
  отдавать прямыми ссылками Moodle, либо класть в git-репозиторий сессии, либо
  работать с содержимым прямо в чате.

## 5. Шаблон делегирования (вставить в новую сессию)
«Вот runbook по online-edu.mirea.ru (ниже). Креды: KC_USER=..., KC_PASS=...
Задача: открой курс "<название>", в папке практик скачай Работу №N и датасет,
[выполни / сконвертируй в PDF / разбери по шагам]. Логинься по runbook,
OTP я пришлю, как только попросишь — буду у телефона.»

