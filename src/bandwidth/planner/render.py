"""The daily caption, the daily image (HTML -> PNG with headless Chrome) and the weekly text (SPEC MSG-2, MSG-3, WK-1)."""

from __future__ import annotations

import html
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .ics import Deadline
from .plan import MUSTS, DayPlan, finals_countdown, weekday

CAPTION_LIMIT = 1024  # Telegram photo caption limit
COLORS = {"focus": "#2f9e6b", "class": "#4f7fe8", "exam": "#c0392b", "break": "#f4b183", "sleep": "#344161"}
CHROME_PATHS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium")


def e(text: str) -> str:
    return html.escape(text, quote=False)


def hhmm(t) -> str:
    return t.strftime("%H:%M")


def hours(h: float) -> str:
    return f"{h:g}h"


def day_title(day: date) -> str:
    return f"{weekday(day)} {day.day} {day.strftime('%b').lower()}"


def when_label(due_day: date, today: date) -> str:
    if due_day == today:
        return "today"
    if due_day == today + timedelta(days=1):
        return "tomorrow"
    return weekday(due_day) if (due_day - today).days < 7 else f"{due_day.day} {due_day.strftime('%b').lower()}"


def due_lines(due: list[Deadline], s: Settings, today: date, limit: int = 6) -> list[str]:
    out = []
    for d in due[:limit]:
        local = d.due.astimezone(s.tz)
        name = d.title if d.course in ("school", "other") else f"{d.course} {d.title}"
        out.append(f"- {when_label(local.date(), today)} - {e(name)} - {local:%H:%M}")
    if len(due) > limit:
        out.append(f"- and {len(due) - limit} more - /list")
    return out


def caption(s: Settings, plan: DayPlan, due: list[Deadline], done: set[str], day_no: int,
            notes: list[str] | None = None) -> str:
    """The text under the image. Lowercase, '-' bullets. Fewer due lines if it would pass the limit."""
    for limit in range(6, -1, -1):
        text = _caption(s, plan, due, done, day_no, notes or [], limit)
        if len(text) <= CAPTION_LIMIT:
            return text
    return text[:CAPTION_LIMIT]


def _caption(s: Settings, plan: DayPlan, due: list[Deadline], done: set[str], day_no: int,
             notes: list[str], limit: int) -> str:
    head = f"<b>{day_title(plan.day)}</b>" + (f" - day {day_no}" if day_no >= 1 else "")
    if done:
        head += f" - {len(done & set(MUSTS))}/4 done"
    lines = [head, *notes, "", "<b>classes</b>"]
    fixed = [b for b in plan.blocks if b.kind in ("class", "exam")]
    lines += [f"- {hhmm(b.start)} {e(b.text)}" for b in fixed] or ["- no classes"]
    lines += ["", "<b>due</b>"]
    lines += due_lines(due, s, plan.day, limit) or ["- nothing in the next 7 days"]
    lc = next((b.text for b in plan.blocks if "leetcode" in b.tasks), "leetcode")
    lines += ["", "<b>musts</b>",
              f"- leetcode {hours(plan.hours('leetcode'))} - {e(lc.removeprefix('leetcode - '))}",
              f"- learn {hours(plan.hours('learn'))}",
              f"- uni {hours(plan.hours('uni'))} - {e(plan.uni_task)}",
              "- push to github",
              f"- sleep {hhmm(s.quiet_from)}"]
    if countdown := finals_countdown(s, plan.day):
        lines += ["", countdown]
    return "\n".join(lines)


def must_buttons(day: date, done: set[str]) -> dict:
    return {"inline_keyboard": [[{"text": ("✓ " if m in done else "") + m, "callback_data": f"m:{day.isoformat()}:{m}"}
                                 for m in MUSTS]]}


# ---- image ------------------------------------------------------------------------------------

CSS = """
body{margin:0;width:760px;background:#f7f7f8;font:14px -apple-system,"SF Pro Text",Helvetica,Arial;color:#111;overflow:hidden}
.w{padding:30px 30px 24px}.top{display:flex;justify-content:space-between;align-items:flex-end}
h1{margin:0;font-size:26px;letter-spacing:-.02em}.sub{color:#8a8a8e;margin-top:3px}
.big{text-align:right}.big b{font-size:30px;color:#2f9e6b;letter-spacing:-.02em}.big span{display:block;color:#8a8a8e;font-size:12px}
.card{background:#fff;border-radius:14px;padding:14px 18px;margin-top:16px}.h2{font-size:12px;color:#8a8a8e;margin-bottom:6px}
.r{display:flex;align-items:center;height:30px;border-bottom:1px solid #f0f0f2}.r:last-child{border:0}
.t{width:112px;color:#8a8a8e;font-size:13px;font-variant-numeric:tabular-nums;flex:none}
.r i{width:4px;height:18px;border-radius:2px;margin-right:12px;flex:none}
.r b{width:52px;font-weight:600;font-size:13.5px;flex:none}.x{flex:1;color:#333;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.d{color:#8a8a8e;font-size:12.5px;margin-left:10px}.r.break b,.r.break .x{color:#8a8a8e;font-weight:400}
.wk{display:flex;gap:8px}.c{flex:1;min-width:0}.dh{text-align:center;font-size:12.5px;font-weight:600;padding:2px 0;border-radius:6px}
.now .dh{background:#111;color:#fff}.due{text-align:center;font-size:11px;color:#c0392b;height:16px;margin:2px 0;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.g{position:relative;background:#f2f3f5;border-radius:6px}.bk{position:absolute;left:2px;right:2px;border-radius:3px}
.ft{text-align:center;font-size:12px;color:#2f9e6b;font-weight:600;margin-top:5px}
.lg{margin-top:10px;font-size:11.5px;color:#8a8a8e}.lg span{margin-right:14px}
.lg i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
"""
GRID_FROM, GRID_TO, PX = 5 * 60, 22 * 60, 0.24  # week grid: 05:00-22:00, px per minute


def _mins(t) -> int:
    return t.hour * 60 + t.minute


def _dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h and m else f"{h}h" if h else f"{m}m"


def image_html(s: Settings, plan: DayPlan, week: list[tuple[date, DayPlan, list[str]]], day_no: int) -> tuple[str, int]:
    """(html, height in css px)."""
    rows = []
    for b in plan.blocks:
        label = {"focus": "focus", "class": "class", "exam": "exam", "break": "break"}[b.kind]
        rows.append(f'<div class="r {b.kind}"><span class="t">{hhmm(b.start)} - {hhmm(b.end)}</span>'
                    f'<i style="background:{COLORS[b.kind]}"></i><b style="color:{COLORS[b.kind] if b.kind != "break" else ""}">'
                    f'{label}</b><span class="x">{e(b.text)}</span><span class="d">{_dur(b.minutes)}</span></div>')
    sleep_h = (24 * 60 - _mins(s.quiet_from) + _mins(s.quiet_until)) // 60
    rows.append(f'<div class="r"><span class="t">{hhmm(s.quiet_from)} - {hhmm(s.quiet_until)}</span>'
                f'<i style="background:{COLORS["sleep"]}"></i><b style="color:{COLORS["sleep"]}">sleep</b>'
                f'<span class="x"></span><span class="d">{sleep_h}h</span></div>')
    cols, total = [], 0.0
    grid_h = (GRID_TO - GRID_FROM) * PX
    for d, p, due in week:
        total += p.focus_hours
        bks = "".join(
            f'<div class="bk" style="top:{(_mins(b.start) - GRID_FROM) * PX:.1f}px;'
            f'height:{max(b.minutes * PX - 1.5, 2):.1f}px;background:{COLORS[b.kind]}"></div>'
            for b in p.blocks if _mins(b.end) > GRID_FROM and _mins(b.start) < GRID_TO)
        cols.append(f'<div class="c{" now" if d == plan.day else ""}"><div class="dh">{weekday(d)} {d.day}</div>'
                    f'<div class="due">{e(", ".join(due))}</div><div class="g" style="height:{grid_h:.0f}px">{bks}</div>'
                    f'<div class="ft">{hours(p.focus_hours)}</div></div>')
    kinds = ["focus", "class", "break", "sleep"] + (["exam"] if any(b.kind == "exam" for _, p, _ in week
                                                                       for b in p.blocks) else [])
    legend = "".join(f'<span><i style="background:{COLORS[k]}"></i>{k}</span>' for k in kinds)
    sub = " - ".join(x for x in (f"day {day_no}" if day_no >= 1 else "", finals_countdown(s, plan.day)) if x)
    body = (f'<div class="w"><div class="top"><div><h1>{day_title(plan.day)}</h1><div class="sub">{sub}</div></div>'
            f'<div class="big"><b>{hours(plan.focus_hours)}</b><span>focus today</span></div></div>'
            f'<div class="card"><div class="h2">today</div>{"".join(rows)}</div>'
            f'<div class="card"><div class="h2">this week - {hours(total)} focus</div><div class="wk">{"".join(cols)}</div>'
            f'<div class="lg">{legend}<span style="color:#c0392b">red = due</span></div></div></div>')
    height = 30 + 62 + 16 + 28 + 20 + 30 * len(rows) + 16 + 28 + 20 + 20 + grid_h + 22 + 24 + 28 + 24
    return f'<!doctype html><meta charset="utf-8"><style>{CSS}</style>{body}', int(height)


class RenderError(Exception):
    pass


def chrome_path() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    return shutil.which("google-chrome") or shutil.which("chromium")


def html_to_png(page: str, height: int, out: Path, width: int = 760, scale: int = 2) -> Path:
    """Screenshot the page with headless Chrome in a throwaway profile (RUN-4)."""
    chrome = chrome_path()
    if not chrome:
        raise RenderError("Google Chrome not found")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.html"
        src.write_text(page)
        cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
               "--no-default-browser-check", f"--user-data-dir={tmp}/profile", f"--force-device-scale-factor={scale}",
               f"--window-size={width},{height}", f"--screenshot={out}", src.as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except subprocess.TimeoutExpired as ex:
            raise RenderError("Chrome timed out") from ex
    if not out.exists() or out.stat().st_size < 1000:
        raise RenderError("Chrome wrote no image")
    return out


# ---- weekly summary ---------------------------------------------------------------------------

def weekly_text(start: date, end: date, musts: dict[str, set[str]], days: int, lc_week: int, lc_total: int,
                focus_done: float, focus_plan: float, deadlines_done: int, deadlines_total: int,
                countdown: str) -> str:
    def count(m: str) -> int:
        return sum(1 for v in musts.values() if m in v)

    per_day = {d: len(v & set(MUSTS)) for d, v in musts.items()}
    lines = [f"<b>week {start.day} - {end.day} {end.strftime('%b').lower()}</b>", "",
             f"- leetcode - {count('leetcode')}/{days} days - {lc_week} solved - {lc_total}/150 total",
             f"- learn - {count('learn')}/{days} days",
             f"- uni - {count('uni')}/{days} days",
             f"- push - {count('push')}/{days} days",
             f"- focus - {focus_done:g}h of {focus_plan:g}h",
             f"- deadlines - {deadlines_done}/{deadlines_total} done"]
    if per_day:
        best = max(per_day, key=lambda d: (per_day[d], d))
        lines.append(f"- best day - {weekday(date.fromisoformat(best))}")
    if countdown:
        lines += ["", countdown]
    return "\n".join(lines)


def now_label(now: datetime, s: Settings) -> str:
    return now.astimezone(s.tz).strftime("%d %b %H:%M").lower()
