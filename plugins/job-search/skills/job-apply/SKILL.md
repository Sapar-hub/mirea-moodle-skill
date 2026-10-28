---
name: job-apply
description: Use this skill when the user wants to apply to a vacancy or internship — «подайся», «откликнись на …», «заполни анкету Ozon/Сбер/…», or picks a vacancy from the job digest. It drives a real Chromium (persistent profile, user's logins) through `scripts/browser.mjs`: the agent reads the page as text, fills the company's own application form from the user's profile and attaches the right resume. Apply only on the employer's own career site, never through aggregators or middlemen.
---

# job-apply

`scripts/browser.mjs` is a thin CLI over a long-lived Chromium (CDP on
`127.0.0.1:9333`, profile in `~/.local/share/job-apply/profile`). Each call
connects, does one thing and disconnects. The browser keeps running, so logins
and a half-filled form survive between calls. Run `npm i` in `scripts/` once.

```bash
B="node scripts/browser.mjs"
$B start                 # headed window; the user can watch, log in and solve captchas
$B goto <url>
$B text 4000             # read the vacancy and the form labels
$B snap                  # interactive elements: e12 textbox "Телефон" value="…" *   (* = required)
$B fill e8 Иван
$B fill --type e11 79990000000   # masked inputs (phone, dates): clears, then types key by key
$B upload e17 "<pdf>"    # the <input type=file>, even when it's hidden
$B check e18             # consent checkbox
$B shot                  # screenshot -> ~/.local/share/job-apply/shot.png; Read it to see the page
$B click e20             # submit — ONLY after the user confirmed
```

Refs are renumbered on every `snap`. Run `snap` again after anything that re-renders the form.
`click` errors print what covers the element (cookie banner, "download the app"
popup): close that first. Refs `f2e5` point into iframes (embedded ATS forms).

## Rules

- **Employer's site only.** From a digest link (Changellenge, a Telegram post) go
  to the company's career site: search the vacancy title there (e.g.
  career.ozon.ru, sberbank.ru/careers, career.avito.com) and apply there.
- **Preflight, before writing a resume.** Two checks, both advisory; show the result to the user in a line or two:
  ```bash
  $B live <vacancy-url>       # LIVE | CLOSED | UNSURE + evidence (HTTP, «вакансия закрыта», redirect, apply button)
  node scripts/precheck.mjs --company '<company>' --title '<title>' --url '<url>'   # OK | WARN | BLACKLIST
  ```
  `live` opens its own tab, so a half-filled form in the current tab stays as it is. CLOSED: stop
  and tell the user. UNSURE: read `text` and decide. `precheck` checks `~/.config/job-apply/blacklist.tsv`
  (the user's own do-not-apply list) and past applications in `applied.tsv` + `history.tsv`
  (the sheet's older rows). BLACKLIST: quote the reason and apply only after an explicit «да, всё равно».
  DUP or SIMILAR: show the old row and ask. NOTE (several applications to one company): keep the resume
  consistent with the earlier ones. Pass titles in single quotes; never paste raw vacancy text into the command.
  When the user says to blacklist a company or team, append a line to `blacklist.tsv` yourself.
- **Data comes from `~/.config/job-apply/profile.yaml`** (outside the repo; never commit
  or paste it in full). The first name on applications is `name.first` from the profile (the form the user goes by, not necessarily the passport one). Fields marked TODO
  are unconfirmed: ask the user before using them.
- **Never invent facts.** No skills, projects or experience that are not in the profile or
  the resume. Free-text answers ("почему мы", motivation) are drafted from the
  vacancy text and the resume, and shown to the user.
- **Resume per application, from one master.** `~/.config/job-apply/resume/master.yaml` holds
  every fact about the user. For each vacancy, write
  `~/.local/share/job-apply/applications/<company-role>/<Фамилия>_<Имя>_<TRACK>.yaml`: a self-contained
  copy that keeps, reorders and rewords master facts toward the vacancy text (headline, about, project
  order, skill groups). Then:
  ```bash
  node scripts/resume.mjs check  <file.yaml>   # every line it prints is a non-master claim: fix or justify
  node scripts/resume.mjs render <file.yaml>   # -> .pdf next to it; refuses leftover `verify` fields
  ```
  Render a PNG with `pdftoppm -r 70 -png` and look at it before attaching. Items marked `verify:` in
  the master stay out until the user confirms them. Quote YAML strings that contain `: `.
- **ATS keywords:** `ats_keywords` + `ats_hidden: true` puts them as invisible text in the PDF text
  layer (`false`: a visible «Ключевые слова» line). Use only terms that are true of the user, phrased
  the way the vacancy phrases them. Hidden text is visible to anyone who copies the PDF text.
- **Submit gate.** Before the final click: `shot`, show the user the filled values and the
  attached file, and wait for an explicit «да/отправляй». Only the user can relax this rule
  ("дальше без подтверждения" for this kind of form). Never click the final submit button twice.
  If unsure whether it went through, read the page instead of retrying.
- **Salary: never state it.** Leave optional salary fields empty. If one is required, pick the least
  specific option («по итогам собеседования», «любая оплата устроит», «обсуждается»). If it is a
  required number with no neutral option, stop and ask the user.
- Captcha, SMS code, Gosuslugi/Sber ID login → hand over to the user in the open window, then continue.
- After a successful submit, append a line to `~/.local/share/job-apply/applied.tsv`
  (`date<TAB>company<TAB>title<TAB>url<TAB>resume<TAB>status`) and add a row to the job_tracker
  sheet (Applications) with **every** column filled: A–H (company, role, date, stage, status, next step
  with date, foreigners, notes) and I–N (vacancy link, resume file, cover letter text, every
  questionnaire answer, source, correspondence). Later emails from the employer go into N, and they
  update E/F. If the sheet has rows that are missing from `applied.tsv`/`history.tsv`
  (added by hand), append them to `history.tsv` (same columns) so `precheck` sees them.
