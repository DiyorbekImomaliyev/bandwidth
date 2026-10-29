# Study Planner v1: spec

Status: v1, implemented.

## Goal

Send the owner one clean message each morning on Telegram: today's plan, this week at a glance,
and what is due. Track the daily musts with one tap each. Alert before every deadline.

Timezone: set in `config/planner.toml` (example: Asia/Qatar). Week starts on Sunday.

## 1. Sources

- **SRC-1** Fetch the D2L calendar feed (`D2L_ICS_URL`) at most every 6 hours and on start.
- **SRC-2** Parse the feed: unfold lines, read UTC and all-day dates, turn the HTML description
  into plain text, keep UID, SUMMARY, LOCATION, DTSTART.
- **SRC-3** Map each event to a deadline: course code (from LOCATION or SUMMARY), clean title
  (fix double spaces and dashes, drop "- Due"), kind (lab, quiz, assignment, exam, other), due time.
- **SRC-4** Keep only items due from today onward. Skip past items.
- **SRC-5** If the fetch fails, keep the last good data and add one line to the next message:
  "d2l feed failed - using data from <date>".
- **SRC-6** Read the class timetable and final exams from `config/planner.toml`
  (course, kind, weekday, start, end, room). Fall 2026 classes end 3 Dec.
- **SRC-7** Skip items that only say when something opens ("- Available") and course events
  without "- Due". Keep university-wide dates (LOCATION equals `school_location`, e.g. `CAMPUS`).

## 2. Daily plan

- **PLAN-1** Build each day from a fixed template for that weekday. The layout and the fixed
  anchors never change: sleep 22:00-05:00, leetcode 05:15-07:15, family dinner 18:00-19:30.
- **PLAN-2** Use 4 block kinds only: focus, class, break, sleep.
- **PLAN-3** Daily musts: leetcode 2h, learn 6h, uni 1h, push to github. Fit learn hours around
  classes; show the real number when a day cannot fit 6h (for example Thursday).
- **PLAN-4** Learn is one simple task: "learn <hours>". Do not track playlists or lessons.
- **PLAN-5** LeetCode follows the NeetCode 150 list in order, starting from problem 1.
  Plan 2 new problems and 1 review a day. A solved problem comes back as a review 3 days and
  10 days later.
- **PLAN-6** Uni study names the nearest open deadline. With none in the next 3 days, use
  "review today's lectures" (class days) or "prep next class" (other days).
- **PLAN-7** Exam mode, 3-12 Dec: uni revision becomes the main focus (about 5h), in exam order
  COMP3104 (8 Dec), COMP2201 (9 Dec), DATA3201 (12 Dec). LeetCode and push stay at about 1h each.
  Learn drops to about 1h.

## 3. Daily message

- **MSG-1** Send one message at 05:00: an image with the text as its caption, plus 4 buttons.
- **MSG-2** Image (portrait, approved design v5):
  - header: date, day number, finals countdown, "focus today" hours;
  - today: one row per block (time range, kind, task, duration), then sleep;
  - this week: 7 colour columns 05:00-22:00, due items in red under each date, focus hours per day;
  - colours: focus green, class blue, break orange, sleep navy.
- **MSG-3** Caption (lowercase, "-" bullets): date and day, classes with time and room, due items,
  musts, finals countdown. Keep it under 1024 characters (Telegram caption limit).
- **MSG-4** Buttons: `leetcode`, `learn`, `uni`, `push`. A tap marks that must done for today and
  edits the same message to show a check mark. A second tap undoes it.
- **MSG-5** If yesterday had undone musts, add one line: "yesterday - 3/4 done".
  Undone leetcode problems carry over (at most 3 a day in total).
- **MSG-6** Send at most once per day, even if the program runs many times.

## 4. Deadline alerts

- **ALR-1** Alert 3 days, 1 day and 3 hours before each deadline.
- **ALR-2** Each alert has buttons: `done`, `snooze 3h`, `snooze to tomorrow 08:00`.
- **ALR-3** `done` stops all later alerts for that item. Never send the same alert twice.
- **ALR-4** Do not send alerts between 22:00 and 05:00. A slot that falls in these hours moves
  earlier, to 21:00 that evening (so "due in 1 day" for a 23:59 deadline comes the evening before).

## 5. Messages from the owner

- **IN-1** Add a deadline or date: "hist essay due nov 2", "os midterm nov 5 10am".
  Reply with what was understood and a `save` / `cancel` button.
- **IN-2** Log extra work: "capstone 2h", "football 1.5h". It counts toward focus hours.
- **IN-3** List and remove items: `/list`, then a `remove` button on each manual item.
- **IN-4** Answer only the owner's chat id (`TELEGRAM_CHAT_ID`). Ignore everyone else.

## 6. Weekly summary

- **WK-1** Saturday 20:00: one short message. Musts done (x/7 each), leetcode solved this week and
  in total (x/150), focus hours, deadlines done.

## 7. Calendar export

- **ICS-1** Write `data/planner.ics` with all deadlines (D2L + manual) and exams.
- **ICS-2** Send the file with `/calendar` so the owner can add it to the phone.
  (A live subscribe link needs hosting; out of scope for v1.)

## 8. Running

- **RUN-1** `planner run [--dry-run]`: fetch, plan, send what is due now. `--dry-run` writes the
  image and text to `data/out/` and sends nothing.
- **RUN-2** `planner bot`: long-poll Telegram for buttons and messages.
- **RUN-3** launchd runs `planner run` every 30 minutes and keeps `planner bot` alive.
  A lock file stops two copies running at once.
- **RUN-4** Render the image from HTML to PNG with the installed Google Chrome (headless).
  If Chrome fails, send the caption text alone and log the error.

## 9. Storage

- **DB-1** SQLite at `data/planner.db`: deadlines (upsert by UID), manual items, daily musts,
  leetcode progress, alerts sent, work logs, a kv table for "last sent" dates, schema version.

## 10. Safety and quality

- **SAF-1** Secrets only in `.env` (chmod 600). Redact the bot token and feed token from all logs
  and errors.
- **SAF-2** Never ask for or store university passwords or 2FA codes.
- **SAF-3** Tests never touch the real feed, real Telegram, the owner's config or running services.
- **SAF-4** Verify on the real feed and the real bot before calling a feature done.

## Out of scope for v1

- Email reading and Gemini date extraction (v2: screenshots and syllabus PDFs sent to the bot).
- Tracking playlists, lessons or GitHub repos.
- Sleep tracking.

## Decisions

- Daily message at 05:00.
- Keep the Saturday summary.
