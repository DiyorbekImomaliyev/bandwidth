from datetime import UTC, date, datetime

from bandwidth.planner.ics import Deadline
from bandwidth.planner.plan import (
    build_day,
    finals_countdown,
    leetcode_for,
    neetcode,
    week_start,
    week_view,
)
from bandwidth.planner.store import Store

SUN, THU, TUE = date(2026, 10, 25), date(2026, 10, 29), date(2026, 10, 27)


def test_neetcode_has_150_unique_problems():
    assert len(neetcode()) == 150 and len({t for _, t in neetcode()}) == 150


def test_sunday_blocks_and_hours(settings):  # PLAN-1..3
    p = build_day(settings, Store(":memory:"), SUN)
    assert [(b.start.strftime("%H:%M"), b.kind) for b in p.blocks] == [
        ("05:15", "focus"), ("08:00", "class"), ("10:00", "focus"), ("12:00", "class"), ("14:30", "focus"),
        ("16:00", "focus"), ("18:00", "break"), ("19:30", "focus")]
    assert (p.hours("leetcode"), p.hours("learn"), p.hours("uni")) == (2, 5.5, 1)
    assert p.focus_hours == 8.5 and p.class_hours == 4


def test_thursday_is_the_short_day(settings):
    p = build_day(settings, Store(":memory:"), THU)
    assert p.hours("learn") == 2 and len(p.classes) == 3


def test_no_classes_after_classes_end(settings):
    p = build_day(settings, Store(":memory:"), date(2026, 12, 6))  # a Sunday after 3 Dec
    assert not p.classes


def test_exam_mode_uses_exam_routine_and_drops_overlaps(settings):  # PLAN-7
    p = build_day(settings, Store(":memory:"), date(2026, 12, 8))  # comp3104 exam 08:00-10:00
    kinds = [(b.start.strftime("%H:%M"), b.kind) for b in p.blocks]
    assert ("08:00", "exam") in kinds and ("07:00", "focus") not in kinds
    assert p.hours("leetcode") == 1 and len(p.leetcode[0]) == 1
    assert p.uni_task == "revise comp2201 - exam tomorrow"


def test_leetcode_carries_over_until_solved(settings):  # PLAN-5, MSG-5
    st = Store(":memory:")
    assert leetcode_for(settings, st, SUN, save=True) == ([0, 1], None)
    assert leetcode_for(settings, st, SUN) == ([0, 1], None)  # fixed for the day
    assert leetcode_for(settings, st, SUN.replace(day=26)) == ([0, 1], None)  # not solved -> again
    st.solve(0, SUN, (3, 10))
    st.solve(1, SUN, (3, 10))
    assert leetcode_for(settings, st, date(2026, 10, 27)) == ([2, 3], None)
    assert leetcode_for(settings, st, date(2026, 10, 28)) == ([2, 3], 0)  # review 3 days later


def test_future_preview_does_not_fix_problems(settings):
    st = Store(":memory:")
    build_day(settings, st, SUN)
    assert st.lc_day(SUN) is None


def test_uni_task_names_nearest_deadline(settings):  # PLAN-6
    st = Store(":memory:")
    st.sync_d2l([Deadline("u1", "comp2201", "lab 7 - stack", "lab", datetime(2026, 10, 26, 20, 59, tzinfo=UTC))],
                datetime(2026, 10, 25, tzinfo=UTC))
    assert build_day(settings, st, SUN).uni_task == "comp2201 lab 7 - stack"
    assert build_day(settings, st, TUE).uni_task == "prep next class"


def test_week_view_runs_sunday_to_saturday_with_due_labels(settings):
    st = Store(":memory:")
    st.sync_d2l([Deadline("u1", "comp2201", "lab 7 - stack", "lab", datetime(2026, 10, 26, 20, 59, tzinfo=UTC))],
                datetime(2026, 10, 25, tzinfo=UTC))
    week = week_view(settings, st, TUE)
    assert [d.isoformat() for d, _, _ in week][::6] == ["2026-10-25", "2026-10-31"]
    assert week[1][2] == ["lab 7"]
    assert sum(p.focus_hours for _, p, _ in week) == 62.5
    assert week_start(date(2026, 10, 31)) == SUN


def test_finals_countdown(settings):
    assert finals_countdown(settings, SUN) == "finals in 44 days"
    assert finals_countdown(settings, date(2026, 12, 8)) == "exam today"
    assert finals_countdown(settings, date(2026, 12, 13)) == ""
