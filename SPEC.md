# Bandwidth: specification

Status: **v1.0** (29 Oct 2026). Stages 1 to 4 are built and run live (trial next to the old bots):
finding, planning, and applying on any site (Greenhouse, Lever, Ashby, Google Forms, Tally, Jotform
and other pages). Stage 5 and the browser extension (§12) are designed.

Source of truth for the code. Every requirement has an ID; tests cite it in their docstring, so
`grep -rhoE '\b[A-Z]+-[0-9]+\b' tests/ | sort -u` lists what is covered. The two existing parts keep
their own specs and IDs: Opportunity Radar (`R-…`, `docs/radar-spec.md`) and Study Planner
(`SRC-/PLAN-/MSG-/ALR-/IN-/WK-/ICS-/RUN-/DB-/SAF-…`, `docs/planner-spec.md`). Where this file
changes one of them, it says so.

## 1. What it is

A private assistant on Telegram for one student. It **finds** opportunities (events, hackathons,
internships, jobs), **checks whether there is time for them** next to coursework, and **does the
applying** under rules the owner controls. It runs on the owner's Mac, costs nothing to run, and
keeps personal data on that Mac.

It joins two tools that already work: Opportunity Radar (a daily digest of opportunities) and Study
Planner (a daily study plan, deadlines from the university's D2L calendar, alerts).

Non-goals: storing passwords, solving CAPTCHAs, automating LinkedIn, paying for anything, ticking
legal declarations for the owner, a web UI, more than one user.

## 2. Principles

1. **Personal data never reaches a cloud service that may keep it.** This is enforced in code
   (§4), not by habit.
2. **Numbers and facts come from code; models write and judge.** A model never invents a date, a
   time slot or a field value.
3. **Nothing goes out in the owner's name without a tap.** Submitting, registering and sending
   need an explicit confirmation of exactly what will be sent.
4. **Each stage is useful on its own** and is finished (tested, running live) before the next one
   starts.
5. **Free to run.** Every dependency has a free tier or is open source; when a free tier runs out,
   the next provider takes over.

## 3. Stages

| Stage | Delivers | Status |
|---|---|---|
| 1. Foundation | One bot for both tools; the AI router with the privacy rule; Radar's AI moved onto it | live (trial) |
| 2. Applyable jobs | Official public job lists (Greenhouse, Lever, Ashby) for chosen companies; an **⚡ Apply** button on jobs whose form the agent can read | built (§8) |
| 3. Help me apply | Vault, agent browser profile, every field filled (rules, saved answers, 🤖 choices, drafts), the checklist and the fit report; never submits | built: any site (§9, §10, APP-9…15) |
| 4. Fast apply | Essays and cover letters drafted per job (anonymised cloud by default), preview, I agree, Submit; one platform at a time, each after 5 clean dry runs | built: any site (§11) |
| 5. More finding, "can I afford this?" | Tavily search, page reader, ranking from marks; each opportunity checked against the study plan | designed (§12) |

*Order changed in v0.4:* the owner wants applying first. Measured on 30 days of Radar data, 1 of
8,613 postings linked straight to a form an agent can fill (the rest: Arbeitnow pages, LinkedIn,
articles), so stage 2 exists to give applying something to apply to.

## 4. AI router (stage 1)

All model calls go through one router. Every provider speaks the OpenAI-compatible chat API, so
one client covers all of them.

### 4.1 Privacy

- **AI-1** Every call names a **task**, and every task has a privacy class: `private` (may contain
  personal data: the owner's messages, the profile, the vault, deadlines, essays) or `public`
  (only public text such as postings, plus the public part of the profile).
- **AI-2** A `private` call can only go to a provider whose URL host is the local machine
  (`localhost`, `127.0.0.0/8`, `::1`). This is checked from the URL itself on every call, not from a
  config flag. With no local provider available the call fails; it never falls back to the cloud.
- **AI-3** Before a `public` call is sent, its text is checked against the owner's **private
  terms** (name, email, phone, citizenship and any extra terms in config). A match blocks the call
  and logs which term matched (not the text). This catches mistakes in prompt building.
- **AI-4** Profile text is split: `about` is public (interests, level, goals, home city);
  `private_about` (citizenship, visa needs, anything personal) is sent only to private tasks.
- **AI-5** Tasks in stage 1:

  | Task | Class | Used for |
  |---|---|---|
  | `rank` | public | Radar: score and summarise items against the public profile (replaces Gemini in R-REL-4) |
  | `requirements` | public | Radar: extract who may apply from a posting's text (RAD-2); no personal data involved |
  | `chat` | private | Radar: understand a plain-language message (replaces Gemini in R-BOT-6) |
  | `ping`, `ping_private` | public, private | `bw doctor` health checks |
  | `map_field` | private | Applying: point a form question to one vault key, from labels only (APP-2) |
  | `essay` | private | Applying: draft an essay or cover letter from the vault on the local model (FA-1, `drafting: local`, and the fallback) |
  | `draft` | public, no local fallback | Applying: the same draft from an anonymised prompt (FA-1, `drafting: cloud_anonymised`) |
  | `polish` | public, no local fallback | Applying: improve an anonymised draft's wording (FA-2); the essay sits in the prompt, so the private-term guard checks it |

- **AI-15** Third-party text (postings, pages) is passed separately from the prompt, inside a
  `<data>` block that the system prompt marks as information, never instructions. The private-term
  check (AI-3) applies to the prompt, not to this data: a posting that mentions the owner's country
  is public text, not the owner's data.
- **AI-16** A public task can opt out of the local fallback. Two do:
  - `rank`: 20-item batches take longer than the timeout locally (measured: about 5 tokens/s on a
    16 GB M2 under memory pressure). When every cloud provider fails, Radar keeps its rule scores.
  - `requirements`: a wrong rule hides an opportunity. Measured on 6 real postings, `qwen3:8b`
    without reasoning inverted or invented a rule in every one (e.g. "at least 60% must not be
    Chinese nationals" → "open only to Chinese nationals"; "open to: all countries"). With
    reasoning it was right, but took 100 s per posting.

### 4.2 Providers and fallback

- **AI-6** Providers are listed in `config/ai.yaml` in order of preference: name, base URL, model,
  the name of the environment variable that holds the key (none for local), and options. Keys live
  only in `.env`. Options: `timeout`, `num_ctx` (local), `no_think` (body fields that turn
  reasoning down for tasks that don't think) and `token_budget` (the least `max_tokens` sent to this
  provider: reasoning models spend thousands of tokens thinking before they answer).
- **AI-7** A `public` call tries the cloud providers in order, then the local one. A provider is
  skipped when its key is missing or it is cooling down.
- **AI-8** Cooling down: after HTTP 429, a provider rests for the `Retry-After` time (default 60 s,
  at most 1 hour). After 3 failures in a row of any kind (timeouts, 5xx, bad output), it rests for 5
  minutes. A success resets the count. When no provider answered and the soonest one is back within
  20 seconds (a per-minute limit, e.g. Groq's 8,000 tokens a minute), the router waits for it and
  tries once more instead of failing the call.
- **AI-9** Structured output: the request asks for a JSON object and the system prompt carries the
  JSON schema. The answer is validated with pydantic. Invalid output gets one repair request that
  quotes the error; if that fails too, the next provider is tried.
- **AI-10** Reasoning models: `<think>…</think>` blocks are removed from answers. Tasks that do not
  need reasoning switch it off: Ollama's `think: false`, and per provider `no_think` parameters
  (e.g. Gemini `reasoning_effort: none`, without which 2.5 models spend the token budget thinking).
  An answer cut off by the token limit is an error (`truncated`), not something to repair.
- **AI-11** When every provider fails, the router raises one error that lists each provider's
  failure (keys redacted). Callers keep their rule-based path, so an AI outage never stops a run
  (as R-REL-4).

### 4.3 Accounting

- **AI-12** Every attempt is logged in `data/agent.db` table `ai_calls`: time, task, class,
  provider, model, ok, error class, latency, tokens in and out. Prompt text is not stored.
- **AI-13** `bw ai-stats` shows calls, failures, tokens and median latency per provider and task
  for today and the last 7 days, and the number of private calls that went anywhere but the local
  machine (always 0; a test checks it).
- **AI-14** Keys are redacted from every error, log line and message (R-CFG-2).

### 4.4 Default providers

| Order | Provider | Use | Why |
|---|---|---|---|
| 1 | Cerebras (free) | public | fast; its terms do not let it train on your content |
| 2 | Groq (free) | public | fast |
| 3 | OpenRouter free models | public | many models behind one key |
| 4 | Gemini (free) | public only | its free tier may use what you send, so never private data |
| local | Ollama, `qwen3:8b` | private tasks; fallback for small public ones | free, unlimited, nothing leaves the Mac; slow under memory pressure |

A provider with no key in `.env` is skipped, so the router works with any subset. With only Ollama,
private tasks work, ranking falls back to rule scores and eligibility is not checked (AI-16).
Measured: Gemini's free tier allowed about 5 ranking calls before answering 429 for the day, so at
least two of the first three cloud providers should have keys.

## 5. Changes to Opportunity Radar (stage 1)

- **RAD-1** R-REL-4 changes: ranking uses the router task `rank` with the public profile only. The
  batch, fingerprint, time-budget and "keep rule scores on failure" behaviour stays.
- **RAD-2** New, eligibility. **The model extracts rules; code decides.** For items the `rank` step
  would send, the public task `requirements` reads each posting and returns who may apply:
  allowed and excluded nationalities, required residence or place of study, applicant levels
  (undergraduate, graduate, PhD…), required languages, and `evidence`: the exact sentence it took
  the rules from. Code then compares the rules with the owner's private facts (RAD-4), so no model
  ever sees them. Rules whose evidence is not found in the posting are ignored (no invented
  restrictions). An item that fails a rule gets score `min(score, 3)` and the reason ("only for
  Qatar nationals"). Values that mean "anyone" ("all countries", "international") are not
  restrictions. If no cloud provider can extract the rules, nothing is hidden: items keep their
  `rank` score, the digest footer says eligibility was not checked, and they are checked again next
  run. When in doubt, an opportunity is shown, never hidden.
  *Why:* measured on real postings, an 8B local model asked "can this person apply?" misapplied a
  negated rule and excluded an Uzbek citizen from a competition open to them. Splitting the job
  keeps the private facts away from every model and makes the decision testable code.
- **RAD-3** R-BOT-6 changes: plain-language messages are understood by the private task `chat`.
- **RAD-4** The profile gains `private_facts` (citizenship and its adjectives, the countries the
  owner lives and studies in, applicant level, languages). They never leave the machine: only code
  reads them. `about` stays public and must contain no private terms (checked by `bw doctor`).
  Private terms live in `config/ai.yaml` (AI-3).

## 6. One bot (stage 1)

- **BOT-1** `bw bot` is one process with one Telegram bot that serves both tools to the owner's
  chat only (IN-4, R-BOT owner rule).
- **BOT-2** Callback data is namespaced: buttons sent by Radar start with `rd:`, by the planner with
  `pl:`. The prefix is added by each tool's Telegram client when it sends, and removed by the bot
  before the tool's own handler sees the tap, so neither tool's handlers change. Taps without a
  known prefix are answered with "this button is from an older version".
- **BOT-3** Commands: `/today`, `/list`, `/calendar` → planner; `/saved`, `/upcoming`, `/review`,
  `/exclude`, `/follow`, `/unfollow`, `/quota` → Radar; `/help` and `/start` → one merged help.
- **BOT-4** Plain messages, in order: text the planner's rules parse as a deadline or a work log
  (IN-1, IN-2) → planner; a message with a link → Radar's "keep this?" flow; anything else →
  Radar's plain-language handling (RAD-3).
- **BOT-5** Schedules inside the same process: the planner's `tick` every 60 s (daily message,
  alerts, feed, weekly summary); Radar's due reminders every poll; Radar's scheduled digest check
  every 30 minutes with its once-a-day rule (R-SCHED-1). Each runs on its own; one failing never
  stops the others or the polling.
- **BOT-6** The planner's blocking code runs in a worker thread so a slow image render never delays
  replies.
- **BOT-7** One bad update is logged and skipped; polling backs off when offline (as both bots do).

## 7. Running (stage 1)

- **RUN-1** One CLI, `bw`: `bot`, `radar …` and `planner …` (the old commands), `doctor`,
  `ai-stats`. The old `radar` and `planner` entry points still work.
- **RUN-2** Data folders: `data/radar/`, `data/planner/`, `data/agent.db`. `bw migrate` copies the
  databases from the two old project folders once (it never moves or deletes them).
- **RUN-3** `bw doctor` checks: `.env` keys present, Telegram token and owner chat, each AI provider
  with a one-line `ping` (local and cloud separately), configs valid, Chrome for the plan image.
  Exit code is non-zero when anything required fails.
- **RUN-4** launchd: one agent keeps `bw bot` alive. `deploy/install_launchd.sh` installs it and
  unloads the four old agents (`com.opportunityradar.{bot,daily}`, `com.studyplanner.{bot,run}`);
  `--keep-old` leaves them running for a side-by-side trial (the bots use different tokens and
  the new one works on copies of the data); `--uninstall` restores nothing automatically but prints
  how to.
- **RUN-5** Secrets only in `.env` (mode 600). JSON-lines logs in `data/logs/` with HTTP client
  logging silenced (R-OBS-1).

## 8. Applyable jobs (stage 2)

- **JOB-1** Radar source types `greenhouse`, `lever` and `ashby` read each platform's official public
  job list for the companies named in the source's `companies` (board names, e.g. `anthropic`). No
  login, no key. Endpoints: `boards-api.greenhouse.io/v1/boards/<co>/jobs?content=true`,
  `api.lever.co/v0/postings/<co>?mode=json`, `api.ashbyhq.com/posting-api/job-board/<co>`.
- **JOB-2** Only titles that match the source's `titles` (default: intern, internship, graduate,
  new grad, junior, entry, student, trainee, apprentice, working student, research assistant,
  fellow) become items, so a 700-job board costs a handful of ranking calls. `exclude_titles`
  (default: senior, staff, principal, lead, manager, director, head of) removes more.
- **JOB-3** Items are `job`s with the posting URL, company, location, first-published time and the
  description as summary, and the tag `apply:<platform>`. One company failing (unknown board, HTTP
  error) is a warning on the source; the others still count. A board name that does not exist is
  reported by name.
- **JOB-4** A digest card for an item tagged `apply:<platform>` gets an **⚡ Apply** button. A link
  the owner sends that is a posting on a supported platform gets **⚡ Help me apply** next to the
  usual "keep this?" buttons. Both start Partial help (§10). The buttons appear only when
  applying is set up (the vault exists, VLT-1), and only for platforms whose forms can be read
  today (Greenhouse); Lever and Ashby jobs show without the button until their readers exist.
  Filling runs in the background, one form at a time; a tap during a fill is answered "still
  filling". Radar sends them (`rd:a:<id>`) and hands the tap
  to the apply module.

## 9. Vault (stage 3)

- **VLT-1** `vault/` (git-ignored, folder mode 700, `BW_VAULT_DIR` to move it) holds what forms ask
  for: `profile.yaml` (name, preferred name, email, phone and its country, country and city of
  residence, links, education: school, degree, major, graduation month, optional GPA),
  `answers.yaml` (saved answers to questions, by question text), `cv.pdf`, optionally
  `about-me.md` (projects, experience, what the owner wants: the only source of facts for essays),
  `style.md` (how the owner writes: structure, sentence rules, tone; included word for word in
  every drafting prompt) and `samples/` (the owner's own writing, for tone). Text inside
  `<!-- -->` is guidance and is never used. No passwords, ever.
- **VLT-2** The vault is personal data: only code and `private` tasks read it (AI-1). The one
  exception is the polish step (FA-2), which sends an anonymised essay.
- **VLT-3** `bw vault check` (also part of `bw doctor` once `vault/` exists) reports missing
  required fields, a missing or non-PDF CV, a CV over 5 MB, and a folder readable by others.
- **VLT-4** Answers grow from use. The checklist (APP-6) numbers each question left to the owner;
  replying `/answer 3 No` saves "No" for that question's exact text, and the next form that asks
  the same question (same words, ignoring case and punctuation) is filled from it. Answers to
  sensitive questions (APP-2) are saved only this way, by the owner, one by one.
- **VLT-5** A new CV: the owner sends a PDF to the bot (or runs `bw vault cv <file>`). It becomes
  `vault/cv.pdf`; the old one moves to `vault/history/` with its date. Code reads the PDF's text
  and links; the private task `cv_update` (local model) proposes changes to `profile.yaml` and new
  facts for `about-me.md`. The owner sees the changes and taps **Apply** or **Skip**; nothing in
  the vault changes without that tap.

## 10. Partial help (stage 3)

- **APP-1** Reading the form. Greenhouse: the public job API with `questions=true` (labels, field
  names, types, options, required). Lever and Ashby: the form page, read in the agent's browser
  (labels, inputs, options, required marks). Result: a list of fields, each with name, label,
  type (text, textarea, select, multi-select, checkbox, file), options and required.
- **APP-2** Every field gets one source, decided **by code first**, in this order:
  1. *legal*: agreements, consent, declarations, arbitration, certify/attest, terms, privacy
     notices, signatures, **AI-use policies** → always left to the owner (non-goal: the agent never
     agrees to anything on the owner's behalf);
  2. *sensitive*: visa and sponsorship, work authorisation and right to work, citizenship,
     demographics (gender, race, ethnicity, veteran, disability), salary, criminal record,
     "worked/interviewed here before" → only from an answer the owner saved for that exact
     question (VLT-4), else left to the owner;
  3. *identity*: name, email, phone, location, links, school, degree, CV and cover-letter files →
     from `profile.yaml` by the field's name or label;
  4. *saved*: the label equals a saved question (VLT-4) → that answer;
  5. *essay*: free-text areas and "why…/describe…/tell us…" questions → left to the owner in
     Partial help, drafted in Fast apply (FA-1);
  6. anything else → the private task `map_field` (local model) may point it to **one vault key**
     from a fixed list, or to none. It never writes a value (principle 2): code reads the value
     from the vault, and for a select it must equal one of the options (ignoring case), else the
     field is left to the owner.
- **APP-3** The agent's browser: Chrome with its own profile in `data/browser/` (Chrome 136+ refuses
  automation of the default profile), in a visible window on the Mac so the owner can watch and
  take over. It fills only visible, enabled fields that the plan lists, uploads the CV, then reads
  every filled field back and compares it with the plan; a mismatch is reported, never retried
  blindly.
- **APP-4** It stops and hands over, leaving the window open, when: a CAPTCHA challenge is visible,
  the page asks to sign in, the page shows a required field the plan doesn't know (e.g. a
  country picker that is not in the API), the page cannot be read, or anything fails.
- **APP-5** **Partial help never submits.** The submit button is never clicked in this mode.
- **APP-6** The checklist message: the job, how many fields were filled, then each field left to
  the owner, numbered, with its reason (essay, sensitive, legal, unknown, not on the page), and
  "the form is open on your Mac; finish it and press Submit there". Each `/answer n …` (VLT-4)
  saves an answer for next time.
- **APP-7** Each application is a row in `data/agent.db` table `applications`: posting, platform,
  company, title, mode (`partial`, `dry`, `fast`), state, the fill plan, preview hash, times, error.
  States: `planned → filling → handed_over` (Partial help) and `planned → filling → ready →
  submitting → submitted | needs_check | failed` (Fast apply). A posting already `submitted` is
  never started again; the owner is told when it was sent.
- **APP-8** Audit folder `data/applications/<id>/`: the plan with every value, screenshots after
  filling, before and after submitting, and the confirmation page's text.

- **APP-9** Any page: forms outside Greenhouse's API are read from the page itself. A script lists
  every visible control (text, text area, native and searchable dropdowns, radio and checkbox
  groups, yes/no buttons, file inputs) with its nearest label, options and required mark, and tags
  each with `data-bw` so the filler finds it again. Lever opens at `<posting>/apply`, Ashby at
  `<posting>/application`; any other page is read as it is. The site's own furniture is skipped:
  controls in headers, menus and footers, search boxes, newsletter sign-ups. A page with no form
  follows its one Apply / Register / RSVP / Get tickets / "official website to apply" button or
  link, up to two hops (listing site → organiser → form), in the same tab; a page with several
  such links (a round-up of many opportunities) is not followed. An "autofill from resume" upload
  is skipped; checkboxes in one fieldset are one question, headed by the fieldset's label.
- **APP-10** Sign-in handover: a page that asks to sign in (a visible password field, or a dialog
  such as "Sign up to attend this event" / "Continue with Google") stops the filling and keeps the window open: "sign in there, then tap 🔄 Try
  again". The agent's Chrome profile keeps the session, so each site needs one sign-in. `/signin
  <url>` opens a site in that window ahead of time. Passwords never pass through the bot.
  A checkout whose total is above zero is never filled: "this costs money, and I never pay". A
  visible CAPTCHA does not stop the filling; the owner solves it and submits in the window
  (hidden CAPTCHA frames, as on every Lever form, are ignored).
- **APP-12** Multi-page forms (Google Forms, Tally, Eventbrite checkout, Devpost registration): after a
  page is filled, code clicks its one "Next" / "Continue" / "Next step" / "Save and continue"
  button, never when the page also shows a Submit / Register / Apply / Pay button, and plans,
  drafts and fills the new page's questions. At most 8 pages. A page that doesn't move ("stuck":
  a required answer is missing) or asks for sign-in stops with the checklist. Before Submit,
  only the last page's fields are read again; earlier pages are no longer on screen.
- **APP-14** Google Forms: opened with `hl=en` (it otherwise follows the viewer's location: Arabic in
  Qatar, which hides the Next button and the closed-form text). Choices drawn as ARIA widgets
  (role=radio, role=checkbox, role=listbox) are read and filled like native ones; the "Other:"
  text box belongs to its choice; an email box labelled in another language is "Email".
  Affiliation / institution / organisation = the vault's school; "Team leader …", "Participant …"
  prefixes are ignored when matching vault keys.
- **APP-15** One engine for any site; per-site code only as a speed-up (Greenhouse's API). The reader
  also enters open shadow roots (web components) and every visible frame that isn't an ad, a
  CAPTCHA or a video (embedded HubSpot/Typeform/checkout forms); a field in frame k is tagged
  k×10000+n so the filler acts in that frame. Any click-to-open dropdown (a button or div with
  role=combobox) is opened and its option clicked; what it then shows is read back.
- **APP-13** Pages that can't be filled say why: a bot check (Cloudflare "Verify you are human") is
  waited out for 25 s, then handed to the owner like a sign-in (the agent never ticks it); a page
  that says the event ended, sales ended, applications closed or the form no longer accepts
  responses gets "⛔ the page says …"; any other page with no form keeps the window open with
  🔄 Try again ("pick a date, Register, Join, then tap Try again"). On a form builder (Tally,
  Jotform, Google Forms, Typeform…) the organiser is taken from the form's title, not the site.
  Labels follow the browser's accessible-name order (aria-labelledby, aria-label, label[for]),
  then the autocomplete hint (given-name, family-name, email, tel).
- **APP-11** Fit report (Help me apply): the posting and the owner's numbered facts, anonymised as
  in FA-1, go to the public task `fit`. It returns the posting's main requirements, each marked
  met / partly / missing with the facts that show it, the posting's keywords the CV lacks, up to
  three CV lines to rewrite for this job (each citing facts; code checks them like essay
  sentences and drops unsupported ones), and risks (visa, location, level). Sent after the
  checklist.
- **JOB-5** Two buttons replace ⚡ Apply: **⚡ Fast apply** (§11) and **🖐 Help me apply**, which
  fills everything Fast apply would (drafts, rules, 🤖 choices), never submits, and adds the fit
  report. A link the owner sends gets both, on any site.

## 11. Fast apply (stage 4)

One tap of **⚡ Apply** picks the mode: Partial help when FA-3 applies or the filling stopped
(CAPTCHA, sign-in); a dry run while the platform has fewer than 5 clean dry runs (FA-7); Fast
apply after that. Required essays and every cover letter are drafted; optional essays are left to
the owner and listed.

- **FA-1** Essays: each essay field (cover letter, personal statement, "why us", short answers) is
  drafted new for this posting, from the question, the posting (as data), `about-me.md`, `style.md`
  and the writing samples. Who drafts is set in `config/apply.yaml` (`drafting`):
  `cloud_anonymised` (default) sends the prompt anonymised as in FA-2 to the public task `draft`
  and puts the details back locally; `local` uses the private task `essay`. If the cloud fails,
  the local model drafts. **Every sentence cites its sources:** the facts are numbered by code
  (F1, F2… one per line of `about-me.md`, each with its section heading), and the draft comes back
  as paragraphs of sentences, each listing the facts it uses and `P` when it uses the posting.
  Code checks each sentence against **only the lines it cites**: numbers, time claims ("for
  years"), skills and tools (a posting's requirements are not the owner's skills), and distinctive
  words (words that occur in only one or two fact lines, such as a project's name), so a number
  can't move to another project. It also checks the style rules it can test (no "-ing" openers or
  tails, banned words). Any problem gets one rewrite, asked the same way; a sentence whose claims
  are still unsupported after it is dropped. A sentence with no sources may state no facts (a
  wish, a reason, a joke).
  *Why cloud by default (measured 28 Oct 2026, Ramp Applied Scientist Intern):* qwen3:8b moved a
  number to the wrong project and broke the style rules after a rewrite; gpt-oss-120b kept every
  fact with its project, but claimed the posting's skills, which the skills check now catches.
  The owner approved sending anonymised facts. Length: a limit stated in the label
  or description ("150 words", "1000 characters"), found by code, else 150 words (250 for a cover
  letter). Code checks the limit and cuts at a sentence end if the draft is over. A cover letter
  under 60% of its limit goes back once for one more paragraph (measured: letters came back at
  32–75 words). Sentences follow the owner's pattern, verb + object + detail, in simple English:
  most lead with the verb, and every paragraph opens with "I" + verb (a paragraph that doesn't, or a
  text with over 75% subjectless sentences, goes back once). Code flags sentences over 25 words,
  and claims about the owner's availability ("can work full-time", "will relocate") that the cited
  facts don't state. An "-ing" tail still there after the rewrite is cut by code. The owner can replace a draft by replying
  to it with new text.
- **FA-2** Polish (optional, a button per essay): code replaces the owner's name, email, phone,
  school and every private term with placeholders, the router's guard confirms none is left
  (AI-3), the public task `polish` improves the wording, and code puts the details back. If a
  placeholder is missing from the result, the draft stays as it was.
- **FA-3** AI-use policy: if the form has a question about using AI, or the posting asks for
  answers without AI tools, essays are not drafted for that application; it runs as Partial help
  and the checklist says why.
- **FA-4** Preview: a message with every value now in the form (the CV by file name) and the
  numbered questions left to the owner; then each drafted essay in full, one message each; then
  each legal statement quoted (up to 3,000 characters; the window has the rest), each with an
  **I agree** button when the question has exactly one agreeing option. The agent ticks a legal
  box only after that tap, for that application only. Submit appears only when every required
  question has a value and every required statement can be agreed in the chat; otherwise the
  application is handed over ("fill them in the window and press Submit there").
- **FA-5** **Submit** carries the preview's hash. It works once: the state moves `ready →
  submitting` by compare-and-set, so a second tap, or a tap on an older preview, does nothing but
  say so. Before clicking, code reads every field on the page again and checks it equals the
  preview; any difference cancels the submit, lists the differences, and hands the window over. After the click: a
  confirmation page → `submitted`; a CAPTCHA → `needs_check` and the owner finishes in the window;
  anything else → `needs_check`. A submit is never retried automatically.
- **FA-6** Caps: at most 5 submits a day (`daily_cap` in `config/apply.yaml`); a preview older than 24 hours cannot
  be submitted.
- **FA-7** A platform earns Fast apply after **5 clean dry runs**: a dry run fills the whole form
  and sends the preview without a Submit button; the owner taps **✅ Values right** or **❌ Something
  wrong** (which sets the count back to 0). Until a platform has 5 right in a row, its previews
  offer only the dry run. The form stays open either way: the owner may submit it by hand. Order:
  Greenhouse, then Lever and Ashby, then others. LinkedIn is never automated.
- **FA-8** A cover letter asked for as a file is uploaded as a one-page PDF made on this Mac: the
  owner's name, a contact line (email, phone, LinkedIn), the date, then the letter, named
  `First-Last-Cover-Letter.pdf`. It stays in the application's audit folder.
- **FA-10** Fast apply leaves almost nothing (owner's request, 29 Oct 2026):
  - *Stated facts answer sensitive questions.* `citizenship` and `needs_sponsorship` in the vault
    answer visa and sponsorship questions (Yes/No), right-to-work questions that name a country
    (Yes for the country of residence or citizenship, No for any other; "the country of this job"
    → the one country the title and posting name, else left), "Are you a <country> citizen?"
    (Yes only for the citizenship), "<country> permanent resident?" (No unless it is home), and
    nationality questions. Country abbreviations match only in capitals ("us" is not the US). Demographic questions take the form's "decline to self-identify"
    option. A saved answer always wins; a text box never gets a rule's answer.
  - *Ranges.* A number fits the one option whose range holds it (GPA 3.87 → "3.81 - 3.9").
  - *Education block.* Greenhouse's School, Degree, Discipline and start/end month and year are
    filled from the vault (`education.start`, `education.graduation`); a school or major missing
    from the page's own list takes "Other".
  - *Chosen dropdowns.* The remaining select questions go to the task `choose` (anonymised, like
    FA-1; local `choose_private` when drafting is local) with the owner's facts and the options.
    Code keeps a choice only when it is exactly an option and the model didn't say "unknown";
    "applied/interviewed before" questions are answered from the ledger instead, and "how did you
    hear about us" from the company's website / careers option (job boards next; a tick list
    never gets "Other"). Every choice is listed under 🤖 in the preview for the owner to check.
  - *Every essay.* Optional essay questions are drafted too, also when asked in a one-line box
    ("most significant achievement?", "provide evidence of…": one paragraph, 80 words); left
    empty: "additional information" and follow-ups to another answer. A future graduation is
    never written in the past tense. Preferred name = first name; "where can we learn more
    about you" = website, else GitHub, else LinkedIn; "samples of your code" = GitHub.
- **FA-11** Learning from the window: at ✅/❌ on a dry run, and on `/learn`, code reads each
  question the owner answered or changed in the window and saves it as an answer (VLT-4). A 🤖
  choice is saved only after ✅; a rule's answer only when the owner changed it. Legal statements,
  essays and files are never saved. A learned answer also fills a vault gap (a missing start date).
  `/applications` lists what each application is doing.
- **FA-9** Apply buttons carry the `ap:` prefix and go from the gateway to the apply module. The
  live window of each application is kept in memory only: after a restart, a tap on an old preview
  says the window is closed.

## 12. Later (designed)

**Two sides: Telegram and a browser extension** (owner's request, 29 Oct 2026; next to build).
- *Telegram side (built):* ⚡ Fast apply / 🖐 Help me apply on a card; the agent's own Chrome window
  does the work; the owner never has to open the site.
- *Extension side (to build):* a Chrome extension in the owner's everyday Chrome, with one
  **Fill** button on any page. It runs the same reader (`apply/js/read_form.js`) in the page and
  asks the bot on the Mac (localhost) for the plan, rules, 🤖 choices, drafts and fit report, then
  fills in place. It uses the owner's own sign-ins, so no sign-in handover and fewer bot checks.
  Never submits; the owner presses Submit. Shares the ledger, saved answers and learning (FA-11)
  with the Telegram side.

**More finding.** Tavily search limited to job platforms; a page reader (Crawl4AI, local) for
postings; the eligibility check (RAD-2) on postings' full text; ranking examples from the owner's
marks.

**"Can I afford this?"** Free hours come from the planner's flexible blocks; deadlines cost hours
by kind; the opportunity's effort is estimated and correctable; the verdict (fits / tight /
doesn't fit) is computed by code; the model explains it and picks one of the free slots the code
lists. **Yes** adds it to the plan, the calendar file and the alerts in one transaction.

## 13. Known limits

- The bot answers only while the Mac is on; Telegram keeps undelivered messages for 24 hours.
- An 8B local model is weaker than cloud models; private tasks are kept small and structured.
- Free tiers change their limits; the fallback order absorbs this, and `bw ai-stats` shows it.
