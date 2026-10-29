# Opportunity Radar — Specification

Source of truth for the code. Every requirement has an ID (`R-…`) that tests cite in their
docstrings: `grep -rho 'R-[A-Z]*-[0-9]*' tests/ | sort -u` lists what is covered

## 1. Purpose

Once a day, send the owner one Telegram message with the **few most important new
opportunities**: in-person events near them (meetups, pitch nights, conferences, hackathons),
internships, posts from LinkedIn pages they follow, and the strongest online hackathons and events
(big-company or interactive ones where they can meet professionals). Missing a
real opportunity is worse than one extra line, but the digest must stay short.

Non-goals: logged-in scraping, credential storage for third-party sites, auto-apply, a web app.

## 2. Domain model

**R-MODEL-1** Every source produces `Item`s: `source`, `kind` (`event`, `hackathon`, `job`,
`post`, `program`), `title` (non-empty, plain text, entities decoded), `url` (absolute http(s)),
`summary` (plain text ≤ 600 chars), `starts_at`, `ends_at` (deadline for hackathons),
`published_at`, `location`, `is_online` (true/false/unknown), `organizer`, `tags`. Datetimes are
timezone-aware and stored in UTC; naive datetimes are rejected.
**R-MODEL-2** `uid` = first 16 hex chars of SHA-256 of the canonical URL.
**R-MODEL-3** Canonical URL: lowercase scheme/host, no `www.`, no fragment, no tracking params
(`utm_*`, `fbclid`, `gclid`, `ref`, `trk`, `trackingId`, `refId`, `position`, `pageNum`, `aff`…),
sorted query, no trailing `/`. LinkedIn jobs → `linkedin.com/jobs/view/<id>`; LinkedIn posts →
`linkedin.com/feed/update/urn:li:activity:<id>`; Eventbrite (any country domain) → `eventbrite.com/e/<id>`.

## 3. Sources

Each source is an adapter (`async fetch(fetcher) -> list[Item]`) configured in
`config/sources.yaml` by `type` + params. Adding a site of an existing type is config only.
Adapters with a `timezone` param use the profile timezone when it is unset.

**R-SRC-1 `wordpress`** — `/wp-json/wp/v2/<post_type>`. Optional `listing_url`: each item's date is
read from the text nearest its link on that page (link text first, then up to 3 ancestors; an
ancestor that links to another item of the same path is a list, not a card, and stops the climb).
`default_location` is overridden when the item says it happens elsewhere (see §6, places).
WordPress `tag-*` classes become tags.
**R-SRC-2 `bevy`** — Bevy communities (Google Developer Groups, Startup Grind):
`{base_url}/api/event_slim/for_chapter/{chapter_id}/?status=Live`. `VIRTUAL` → online;
`organizer` overrides the chapter name.
**R-SRC-3 `jsonld`** — schema.org `Event` objects from any page (nested lists, `@graph`,
`itemListElement`). Invalid blocks are skipped. Relative URLs are resolved against the page. A
bare midnight timestamp is a date in the local timezone. Repeated address parts are removed.
**R-SRC-4 `devpost`** — Devpost API (open + upcoming hackathons), paginated; submission period →
`starts_at`/`ends_at`; prize, registrations and themes in the summary; optional `themes` filter.
**R-SRC-5 `linkedin_jobs`** — LinkedIn public guest job search, no login: one query per
(keyword, location), paginated; `f_WT=2` filter marks results remote.
**R-SRC-6 `linkedin_posts`** — public logged-out company/profile pages. Strategies: JSON-LD
postings, then feed cards with an activity URN, then links to posts. Auth wall, HTTP 999 or no
posts → that page fails, the others continue.
**R-SRC-7 `rss`** — RSS/Atom. `date_is_event_start: true` for event feeds whose pubDate is the
event date.
**R-SRC-9** Realistic browser headers, 20 s timeout; one malformed record is skipped and
counted, never fatal.
**R-SRC-10 `tribe_events`** — WordPress "The Events Calendar" API
(`/wp-json/tribe/events/v1/events`), paginated; UTC fields; all-day events span whole local days;
`is_virtual` → online; venue → location; categories → tags.

## 4. Fetching

**R-NET-1** Sources run concurrently, at most 6 at once.
**R-NET-2** Connect errors, timeouts, 429 and 5xx are retried up to 3 attempts with exponential
backoff and jitter; `Retry-After` honoured up to 60 s. Other 4xx are not retried; their response
text is kept in the error.
**R-NET-3** At least 1 s between requests to one host (LinkedIn 2.5 s).
**R-NET-4** A failing source never aborts the run.
**R-NET-5** A host that resolves to `0.0.0.0`/`::`/loopback is reported as blocked on this
machine (`/etc/hosts` or a site blocker).

## 5. Storage (SQLite, `data/radar.db`)

**R-DB-1** `items` keeps every item with first/last seen, score, reasons, AI summary, duplicate
link and `sent_at`.
**R-DB-2** Upsert never resets `first_seen` or `sent_at`.
**R-DB-3** `runs` and `source_runs` record every run and every source result.
**R-DB-4** Schema is created/migrated on open (`PRAGMA user_version`; v2 adds `ai_version`)
without losing rows.
Unsent items that are over, or unseen for 60 days, are pruned; sent items are kept.

## 6. Deduplication and relevance

**R-DEDUP-1** Same canonical URL → same item.
**R-DEDUP-2** Near duplicates across sources (same kind, same local day, title token
containment ≥ 0.85) keep the item from the source listed first; an already-sent item is never
replaced.

**R-REL-1** `config/profile.yaml`: `timezone`, `home_label`, `about`, `interests` (topics with
weights; an item must match one), `formats` (boost only), `locations` (home region keywords),
`exclude`, thresholds and caps below. Keywords are normalised to lowercase.
**R-REL-2** Dropped before scoring: events already over; events more than `lookahead_days` ahead;
in-person events outside the home region; online events (not hackathons) when
`online_events: false`; posts/jobs older than `max_post_age_days`; undated program pages older
than `max_program_age_days`; jobs whose title has none of `job_title_keywords`; titles with an
`exclude` keyword.
**Places**: a source's `default_location` is replaced when the item names another place in its
title, uses one as a location in its text ("in Paris", "to Sarajevo"), or announces travel
("heading to …") to a destination outside the default location. Words of the default location
count as home, so this works in any city.
**R-REL-3** Rule score 0–10: topics in title ×2, in text ×1; formats ×1 / ×0.5; +3 home region;
+1 online when `remote_ok`; +2 when a `notable_organizers` name (Google, NVIDIA…) appears in the
organizer, title or text, shown first as "by NVIDIA" (as the item writes it). No topic match → 0. Keywords ≤ 3 letters only match in capitals;
plurals match. LinkedIn posts need a topic or an opportunity word ("register", "apply",
"hackathon", …). Known location decides home; the title is used only when location is unknown.
**R-REL-4** AI scoring (when `GEMINI_API_KEY` is set): candidates with rule score > 0 not judged
before are sent to Gemini in batches of ≤ 20 with the profile; structured JSON output; the key
only in a header. Models in `llm_models` are tried in order per batch. The AI's score replaces the
rule score and its one-line `about` replaces the summary. The prompt rates online items 8+ only
when run by a major organisation, large, or interactive. Each verdict stores a fingerprint of the
prompt and the profile fields it uses; when that changes, unsent items are judged again. If every
model fails, rule scores are kept and the digest footer says so.
**R-REL-5** Thresholds: `min_score` (rules) or `min_ai_score` (AI); online items need
`min_online_score` (default 8). Below them, items are stored, not sent.
**R-REL-6** At most `max_items` (default 15) per digest, by tier, then score, then soonest date:
1) in person in the home region, 2) internships/programs, 3) LinkedIn posts, 4) online hackathons
and events (at most `max_online`, default 6; notable organisers first), 5) other. Held-back items compete again later; the footer
says how many.

## 7. Digest

**R-DIG-1** Only items never sent and not duplicates; marked sent after delivery; a dry run
marks nothing.
**R-DIG-2** Sections: 🔴 Closing soon (≤ 3 days), `home_label`, 🏆 Hackathons, 💼 Internships &
programs, 🌐 Online events, 💬 LinkedIn posts. Within a section: score, then soonest.
**R-DIG-3** Each item: linked bold title, "when · where", one line ≤ 160 chars, then the source
and "AI rated N/10" or the top matched reasons. Dynamic text is HTML-escaped.
**R-DIG-4** Header: local date and count. Footer: AI note, held-back count, source health
(failures; ⚠️ after 3 consecutive; sources that went silent).
**R-DIG-5** Messages ≤ 4000 chars, split only between items.
**R-DIG-6** Nothing new → a short "No new opportunities today" with the footer.

## 8. Delivery and setup

**R-OUT-1** Telegram `sendMessage` (HTML, no link previews) with `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`.
**R-OUT-2** Without Telegram settings, or with `--dry-run`, the digest is printed and saved to
`data/last_digest.html`; nothing is marked sent.
**R-OUT-3** `radar telegram-setup` prints the chat id of whoever messaged the bot.

## 9. Configuration and secrets

**R-CFG-1** Config files are validated on load; an unknown source type, a bad parameter, a
duplicate name or an unknown timezone is a clear error. A missing file points to its
`*.example.yaml`.
**R-CFG-2** Secrets come from the environment or a git-ignored `.env`, and are redacted from
every error, log line and message.
**R-CFG-3** Sources can be `enabled: false`.

## 10. Observability

**R-OBS-1** JSON-lines log `data/logs/radar.log` (5 × 1 MB) plus readable stderr; HTTP client
logging is silenced because URLs can contain tokens.
**R-OBS-2** `radar health`: last status, last success, consecutive failures, average items,
"went silent" warning, last delivery.
**R-OBS-3** Exit code 2 when every source failed or delivery failed; otherwise 0.

## 11. Scheduling (macOS)

**R-SCHED-1** `radar run --scheduled [--not-before HH:MM]` sends at most one digest per local day:
it does nothing before `not-before`, after a successful delivery that day, or when offline.
launchd runs it at login and every 30 minutes, so the digest arrives the first time the Mac is on
and online each day.
**R-SCHED-2** An exclusive lock prevents overlapping runs.

## 12. Known limitations

- Logged-out LinkedIn shows only recent public posts; some pages are hidden without login.
- Pages built only with JavaScript and sites that block bots can't be read; follow those on LinkedIn.
