# sapar-skills — скиллы для AI-агентов

Маркетплейс из двух плагинов:

| Плагин | Скиллы | Что делает |
|---|---|---|
| `mirea-moodle` | `mirea-moodle`, `syncshare-audit` | **online-edu.mirea.ru** (Moodle РТУ МИРЭА): вход через SSO с кодом 2FA, курсы, папки, скачивание методичек и датасетов, docx → pdf; read-only аудит покрытия тестов SyncShare для своего аккаунта |
| `job-search` | `job-digest`, `job-apply` | ежедневный дайджест вакансий из Telegram/RSS в папку Mail.ru и отклики на сайтах самих работодателей с резюме под вакансию |

Просто скажи агенту «скачай методичку по ИАД», «что нового по вакансиям» или «откликнись на эту стажировку» — нужный скилл подхватится сам.

Формат — открытый стандарт [Agent Skills](https://agentskills.io) (`SKILL.md`), работает в Claude Code, OpenCode и других агентах.

## Установка

**Claude Code (через marketplace):**
```
/plugin marketplace add Sapar-hub/mirea-moodle-skill
/plugin install mirea-moodle@sapar-skills
/plugin install job-search@sapar-skills
```

**Claude Code / OpenCode (вручную):**
```bash
git clone https://github.com/Sapar-hub/mirea-moodle-skill
# Claude Code: ~/.claude/skills/   OpenCode: ~/.config/opencode/skills/
cp -r mirea-moodle-skill/plugins/*/skills/* ~/.claude/skills/
```

Нужны: `bash`, `curl`, `python3`; для pdf — `libreoffice`. Для аудита SyncShare дополнительно Node.js, Chrome/Chromium, `openssl`, `unzip` и `xvfb-run` на headless-машине. Для `job-search` — Docker (RSSHub), systemd `--user`, Node.js и Chromium (Playwright), ящик Mail.ru с паролем для внешних приложений.

## MIREA Moodle

Логин и пароль передаются только через переменные окружения:
```bash
export KC_USER='<student>@edu.mirea.ru' KC_PASS='...'
```
Дальше агент сам запускает `scripts/mirea.sh` (`check` → `start` → `otp <код>` → `courses` → `course <id>` → `folder <id>` → `get <url>`). Скрипт можно запускать и руками, без агента.

## SyncShare audit

`syncshare-audit` (в плагине `mirea-moodle`, использует его `mirea.sh` для логина) воспроизводит read-only аудит покрытия тестов SyncShare:

1. инвентаризация тестов курса (окно, попытки, review);
2. сохранение страниц попыток/просмотра — новая попытка только с явного согласия, для тестов с одной попыткой есть блокировка;
3. прогон **настоящего** расширения в локальном моке: `questionIdentity` считает само расширение, текст вопросов никуда не отправляется;
4. один анонимный `GET /api/v2/quiz/solution` на вопрос (rate limit, только чтение);
5. отчёт: сколько вопросов есть в базе и почему часть тестов возвращает пусто.

Пошаговый сценарий — в `plugins/mirea-moodle/skills/syncshare-audit/SKILL.md`. Скрипт `scripts/setup.sh` скачивает расширение, ставит Playwright, находит Chrome и генерирует локальный сертификат. Состояние по умолчанию — `~/.syncshare-audit`.

`setup.sh` также применяет к распакованной копии расширения небольшой локальный fallback настроек (telemetry off), чтобы обойти гонку инициализации в 2.19.0. CRX, парсер вопросов и вычисление `questionIdentity` не меняются.

Жёсткие правила: только свой аккаунт и свой Moodle-хост, только `GET quiz/solution` в production, без отправки текста вопросов, без POST в `quiz/attempt`/`quiz/review` в production, без сдачи попыток.

## Job search

**job-digest** — `feeds.txt` (Telegram-каналы и RSS) → локальный RSSHub в Docker → фильтр по регуляркам из `keywords.txt` (уровень: стажёр/junior; направление: DS/ML/аналитика) → дедуп в sqlite → письмо-дайджест через IMAP APPEND в папку «Вакансии» → systemd-таймер раз в день. Заодно `mailsort.py` раскладывает входящие по папкам по `mail-rules.json` (только MOVE, ничего не удаляет).

```bash
plugins/job-search/skills/job-digest/scripts/install.sh   # RSSHub + конфиг в ~/.config/job-digest + таймер
export MAILRU_USER=... MAILRU_APPPASS=...                   # или ~/.config/job-digest/env (chmod 600)
```

**job-apply** — тонкий CLI над долгоживущим Chromium (`scripts/browser.mjs`: `goto`, `text`, `snap`, `fill`, `upload`, `click`, …): агент читает форму текстом и заполняет её из `~/.config/job-apply/profile.yaml`. Резюме собирается под каждую вакансию из одного мастер-файла (`resume.mjs check` ловит факты, которых нет в мастере; `render` → PDF).

Правила скилла: отклик только на сайте работодателя (без hh.ru и посредников), никаких выдуманных навыков, зарплату не указывать, финальная отправка — только после явного «да» от пользователя. Профиль и резюме лежат вне репозитория.

## Безопасность

- Пароль нигде не сохраняется. Куки лежат локально в `~/.mirea` (`MIREA_DIR`).
- Пароль Mail.ru — только из переменных окружения или `~/.config/job-digest/env`, в репозиторий не попадает.
- 2FA **не обходится**: код каждый раз вводишь ты сам.
- `otp-email` (черновик, по умолчанию выключен) читает код из ящика Mail.ru по app-password и **удаляет письмо с кодом**; чтобы не удалял — `MIREA_KEEP_OTP=1`.

## Дисклеймер

Неофициальные инструменты, с РТУ МИРЭА, SyncShare и работодателями не связаны. Работает только с твоим собственным аккаунтом и делает то же, что ты делаешь в браузере. Если вёрстка SSO/Moodle поменяется, что-то может сломаться — issues и PR приветствуются.

MIT © Sapar
