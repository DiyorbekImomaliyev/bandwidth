"""Build a day's plan and the week view from the routine, classes, exams and deadlines (SPEC PLAN-1..7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from importlib import resources

from .config import DAYS, Settings
from .ics import Deadline
from .store import Store

REVIEW_DAYS = (3, 10)  # a solved problem comes back on these days (PLAN-5)
MUSTS = ("leetcode", "learn", "uni", "push")


@cache
def neetcode() -> list[tuple[str, str]]:
    """(topic, title) for the 150 problems, in order."""
    text = resources.files("bandwidth.planner").joinpath("data/neetcode150.txt").read_text()
    out = []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            topic, _, title = line.partition("|")
            out.append((topic.strip(), title.strip()))
    return out


@dataclass(frozen=True)
class Block:
    start: time
    end: time
    kind: str  # focus | class | exam | break
    text: str
    tasks: tuple[str, ...] = ()

    @property
    def minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)


@dataclass
class DayPlan:
    day: date
    blocks: list[Block]
    exam_mode: bool
    leetcode: tuple[list[int], int | None] = ([], None)
    uni_task: str = ""
    classes: list[Block] = field(default_factory=list)

    def hours(self, task: str) -> float:
        return sum(b.minutes for b in self.blocks if task in b.tasks) / 60

    @property
    def focus_hours(self) -> float:
        return sum(b.minutes for b in self.blocks if b.kind == "focus") / 60

    @property
    def class_hours(self) -> float:
        return sum(b.minutes for b in self.blocks if b.kind in ("class", "exam")) / 60


def weekday(day: date) -> str:
    return DAYS[day.weekday()]


def in_exam_mode(s: Settings, day: date) -> bool:
    return bool(s.exam_mode and s.exam_mode[0] <= day <= s.exam_mode[1])


def _overlaps(a: Block, b: Block) -> bool:
    return a.start < b.end and b.start < a.end


def fixed_blocks(s: Settings, day: date) -> list[Block]:
    """Classes (until classes_end, inclusive) and exams on that day."""
    out = []
    if day <= s.classes_end:
        for c in s.classes:
            if c.day == weekday(day):
                out.append(Block(c.start, c.end, "class", f"{c.course} {c.kind} - {c.room}"))
    for e in s.exams:
        if e.day == day:
            out.append(Block(e.start, e.end, "exam", f"{e.course} final exam - {e.room}"))
    return out


def local_day_bounds(s: Settings, day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), s.tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def leetcode_for(s: Settings, store: Store, day: date, save: bool = False) -> tuple[list[int], int | None]:
    """The day's problems: next unsolved ones (undone ones carry over) plus one due review.
    Saved the first time so the plan does not change during the day."""
    if (got := store.lc_day(day)) is not None:
        return got
    solved = store.solved()
    n_new = 1 if in_exam_mode(s, day) else 2
    new = [i for i in range(len(neetcode())) if i not in solved][:n_new]
    review = store.next_review(day)
    if save:
        store.set_lc_day(day, new, review)
    return new, review


def uni_task_for(s: Settings, store: Store, day: date, has_class: bool) -> str:
    if in_exam_mode(s, day):
        # On an exam day the revision after the exam is for the next one.
        nxt = sorted((e for e in s.exams if e.day > day), key=lambda e: (e.day, e.start)) or \
            sorted((e for e in s.exams if e.day == day), key=lambda e: e.start)
        if nxt:
            e = nxt[0]
            when = "today" if e.day == day else "tomorrow" if e.day == day + timedelta(days=1) else weekday(e.day)
            return f"revise {e.course} - exam {when}"
    start, _ = local_day_bounds(s, day)
    soon = [d for d in store.upcoming(start, start + timedelta(days=3), include_done=False) if d.course != "school"]
    if soon:
        return f"{soon[0].course} {soon[0].title}"
    return "review today's lectures" if has_class else "prep next class"


def build_day(s: Settings, store: Store, day: date, detail: bool = True, save: bool = False) -> DayPlan:
    """The day's blocks in time order. `detail=False` skips task picking (used for the week view).
    `save=True` (only the real daily send) fixes the day's leetcode problems for the rest of the day."""
    exam = in_exam_mode(s, day)
    fixed = fixed_blocks(s, day)
    routine = s.routine.get("exam" if exam else weekday(day), [])
    blocks = list(fixed)
    for r in routine:
        b = Block(r.start, r.end, r.kind, r.label, r.tasks)
        if not any(_overlaps(b, f) for f in fixed):
            blocks.append(b)
    blocks.sort(key=lambda b: b.start)
    plan = DayPlan(day, blocks, exam, classes=[b for b in fixed if b.kind == "class"])
    if not detail:
        return plan
    plan.leetcode = leetcode_for(s, store, day, save=save)
    plan.uni_task = uni_task_for(s, store, day, bool(plan.classes))
    plan.blocks = [Block(b.start, b.end, b.kind, focus_text(plan, b) if b.kind == "focus" else b.text, b.tasks)
                   for b in blocks]
    return plan


def leetcode_text(plan: DayPlan) -> str:
    names = neetcode()
    new, review = plan.leetcode
    parts = [names[i][1] for i in new]
    if review is not None:
        parts.append(f"review {names[review][1]}")
    return ", ".join(parts) if parts else "all 150 done - free practice"


def focus_text(plan: DayPlan, b: Block) -> str:
    if "leetcode" in b.tasks:
        return f"leetcode - {leetcode_text(plan)}"
    if "uni" in b.tasks:
        return f"uni - {plan.uni_task}"
    if "learn" in b.tasks:
        return "learn + push" if "push" in b.tasks else "learn"
    return " + ".join(b.tasks)


def week_start(day: date) -> date:
    """The Sunday on or before `day`."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def short_title(d: Deadline) -> str:
    """'lab 7 - stack' -> 'lab 7'; university dates keep a few words."""
    head = d.title.split(" - ")[0]
    return " ".join(head.split()[:3])


def week_view(s: Settings, store: Store, day: date) -> list[tuple[date, DayPlan, list[str]]]:
    """Sun..Sat around `day`: (date, plan without task detail, short due labels)."""
    out = []
    first = week_start(day)
    for i in range(7):
        d = first + timedelta(days=i)
        a, b = local_day_bounds(s, d)
        due = [short_title(x) for x in store.upcoming(a, b, include_done=False)]
        due += [f"{e.course} exam" for e in s.exams if e.day == d]
        out.append((d, build_day(s, store, d, detail=False), due))
    return out


def finals_countdown(s: Settings, day: date) -> str:
    left = sorted(e.day for e in s.exams if e.day >= day)
    if not left:
        return ""
    n = (left[0] - day).days
    return "exam today" if n == 0 else "finals tomorrow" if n == 1 else f"finals in {n} days"


def day_number(s: Settings, day: date) -> int:
    return (day - s.start_date).days + 1
