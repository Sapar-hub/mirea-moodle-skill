# mirea-moodle — скилл для AI-агентов

Скилл для работы с **online-edu.mirea.ru** (Moodle РТУ МИРЭА): вход через SSO с кодом 2FA, список курсов, папки, скачивание методичек и датасетов, конвертация docx → pdf.

Просто скажи агенту «скачай методичку по ИАД» или «найди курс по БЖД» — он сам залогинится (код 2FA попросит у тебя) и скачает файлы.

Формат — открытый стандарт [Agent Skills](https://agentskills.io) (`SKILL.md`), работает в Claude Code, OpenCode и других агентах.

## Установка

**Claude Code (через marketplace):**
```
/plugin marketplace add Sapar-hub/mirea-moodle-skill
/plugin install mirea-moodle@sapar-skills
```

**Claude Code / OpenCode (вручную):**
```bash
git clone https://github.com/Sapar-hub/mirea-moodle-skill
# Claude Code
cp -r mirea-moodle-skill/skills/mirea-moodle ~/.claude/skills/
# OpenCode
cp -r mirea-moodle-skill/skills/mirea-moodle ~/.config/opencode/skills/
```

Нужны: `bash`, `curl`, `python3`; для pdf — `libreoffice`.

## Использование

Логин и пароль передаются только через переменные окружения:
```bash
export KC_USER='<student>@edu.mirea.ru' KC_PASS='...'
```
Дальше агент сам запускает `scripts/mirea.sh` (`check` → `start` → `otp <код>` → `courses` → `course <id>` → `folder <id>` → `get <url>`). Скрипт можно запускать и руками, без агента.

## Безопасность

- Пароль нигде не сохраняется. Куки лежат локально в `~/.mirea` (`MIREA_DIR`).
- 2FA **не обходится**: код каждый раз вводишь ты сам.
- `otp-email` (черновик, по умолчанию выключен) читает код из ящика Mail.ru по app-password и **удаляет письмо с кодом**; чтобы не удалял — `MIREA_KEEP_OTP=1`.

## Дисклеймер

Неофициальный инструмент, с РТУ МИРЭА не связан. Работает только с твоим собственным аккаунтом и делает то же, что ты делаешь в браузере. Если вёрстка SSO/Moodle поменяется, что-то может сломаться — issues и PR приветствуются.

MIT © Sapar
