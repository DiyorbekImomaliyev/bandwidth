# Bandwidth

A private assistant on Telegram for university students. It **plans** your study day, **finds**
opportunities that fit you (events, hackathons, programs, internships, jobs), and **fills in the
applications** for you. It runs on your Mac, costs nothing to run, and your personal data stays on
your Mac.

## What it does

- **Study Planner.** One message each morning: today's plan, the week, deadlines from your
  Brightspace (D2L) calendar, and buttons for your daily musts. Alerts before every deadline.
- **Opportunity Radar.** A daily digest from about 40 sources (university and community event pages,
  Devpost, company job lists, job boards, funded-program feeds), ranked by AI against what you care
  about, without the ones you can't apply for.
- **Applying.** Every opportunity with a form gets two buttons:
  - **⚡ Fast apply:** the agent opens the form in its own Chrome window, fills every field, drafts
    the essays and the cover letter, and sends you a preview. You tap **📨 Submit**, or nothing is
    sent.
  - **🖐 Help me apply:** the same filling, never submits, plus a **fit report**: which of the job's
    main requirements you meet, CV lines rewritten for this job from your own facts, keywords to
    add if true, and the risks (visa, level, dates).

  It works on any site: Greenhouse, Lever, Ashby, Google Forms, Tally, Jotform, multi-page forms,
  embedded forms, and other pages. Send it any link and it offers both buttons.

### Applying rules, enforced in code

- Nothing is submitted without your tap. A site's Fast apply is unlocked only after 5 clean dry runs.
- Legal statements are never ticked for you; you tap **I agree** for each.
- It never pays, never solves CAPTCHAs or bot checks, and never automates LinkedIn.
- Sensitive questions (visa, work rights, citizenship, demographics) are answered only from facts you
  stated in your vault or answers you saved; everything it chose itself is marked 🤖 for you to check.
- Every sentence it writes cites one of your facts; a sentence with an unsupported claim is dropped.
- It learns: answers you give in the window or with `/answer` are saved and reused.

## Privacy

Every AI call is either **private** or **public**, and the code enforces the difference:

| Data | Where it goes |
|---|---|
| Your messages to the bot | the local model on your Mac only |
| Your citizenship, where you live and study, your level, languages | nowhere: only code reads them |
| Your public profile and public postings | free cloud models, to rank and read postings |
| Your facts for essays and the fit report | free cloud models only after anonymising (names, schools, places replaced) |
| Your name, email, phone and other private terms | blocked: a cloud call that contains one is not sent |

A private call can only reach a model whose address is this computer, checked on every call.
`bw ai-stats` shows every AI call by provider and task.

## Setup (macOS)

1. **Python 3.11+ and the app**

   ```
   python3 -m venv .venv
   .venv/bin/pip install -e '.[apply]'
   ```

2. **The local model.** Install [Ollama](https://ollama.com) (`brew install ollama`,
   `brew services start ollama`), then `ollama pull qwen3:8b` (about 6 GB of memory).

3. **Keys.** Copy `.env.example` to `.env`, run `chmod 600 .env`, and fill it in: a Telegram bot from
   [@BotFather](https://t.me/BotFather) and your chat id; free keys from at least two of Groq,
   OpenRouter and Gemini; your D2L calendar feed link.

4. **Config.** Copy each `config/*.example.*` file to the same name without `.example` and edit:
   `profile.yaml` (what you want, and your private facts), `sources.yaml` (where to look),
   `planner.toml` (timetable, exams, daily template), `ai.yaml` (providers and private terms),
   `apply.yaml` (drafting mode, daily cap).

5. **Vault, for applying.** Copy `vault.example/` to `vault/`, run `chmod 700 vault`, fill in
   `vault/profile.yaml` and `vault/about-me.md`, put your CV in `vault/cv.pdf`, then run `bw vault`.

6. **Check and install**

   ```
   .venv/bin/bw doctor          # keys, Telegram, each AI provider, configs
   deploy/install_launchd.sh    # keeps the bot running while you are logged in
   ```

7. **Sign in once** to the sites you register on (Devpost, Luma, Meetup, Eventbrite, Google) in the
   agent's Chrome window: send `/signin <site>` to the bot. The window remembers you.

## Commands

In Telegram, send `/help`. The main ones:

| | |
|---|---|
| `/today`, `/list`, `/calendar` | today's plan, deadlines in the next 14 days, a calendar file |
| `/saved`, `/upcoming`, `/review` | opportunities you kept, what's coming up, what you haven't marked |
| `/applications` | your applications and their state |
| `/answer 2 Yes` | answers question 2 of the last form; saved for next time |
| `/learn` | saves the answers you typed in the open form window |
| `/signin <url>`, `/retry` | open a site to sign in; read the form again after you did a step |
| `history essay due nov 2` | adds a deadline (you confirm it) |
| a link | keeps it, reminds you, and offers ⚡ Fast apply / 🖐 Help me apply |

On the Mac:

```
bw bot                     # the bot and all schedules (launchd runs this)
bw doctor                  # health check
bw vault                   # check the vault applications are filled from
bw ai-stats [--days 7]     # AI calls, failures and tokens per provider and task
bw radar run --dry-run     # build today's digest without sending it
bw planner run --dry-run   # build today's plan without sending it
```

## Project layout

```
src/bandwidth/
  cli.py, gateway.py      the `bw` command and the one Telegram bot that routes to each part
  ai.py                   AI router: private (local only) and public (cloud with fallback) tasks
  buttons.py, paths.py    shared Telegram buttons and file locations
  radar/                  Opportunity Radar: sources, ranking, eligibility, digest
  planner/                Study Planner: calendar feed, daily plan, alerts
  apply/                  Applying
    forms.py              reading a form (Greenhouse API, or any page)
    js/read_form.js       the page reader that runs in the browser
    plan.py, guess.py     what to put in each field: vault, rules, saved answers, 🤖 choices
    essays.py, letter.py  cited essays and cover letters (PDF)
    fit.py                the fit report
    browser.py            filling, multi-page forms, sign-in and bot-check handover, submit
    service.py            the flow behind the buttons
    applications.py       the ledger of applications
    vault.py, settings.py your facts and applying settings
tests/                    core/, radar/, planner/, apply/ (mirrors src)
config/*.example.*        config templates;   vault.example/   vault template
SPEC.md                   the specification; docs/ holds the two original tools' specs
```

## Develop

```
.venv/bin/pip install -e '.[apply,dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Read `SPEC.md` first. Every requirement has an ID, and the tests cite it.

## Limits

- The bot answers only while your Mac is on. Telegram keeps messages for 24 hours.
- The local model is slow when memory is tight; it handles only small private tasks.
- Free cloud tiers have rate limits; the next provider takes over, and drafts may be weaker then.
- Sites with a sign-in or a "Verify you are human" check need you once in the agent's window.
- Typeform (one question at a time) and Workday (an account per company) are not supported yet.
