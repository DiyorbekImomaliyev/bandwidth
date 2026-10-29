from datetime import datetime

import pytest
from tests.planner.conftest import NOW, QATAR

from bandwidth.planner.parse import parse_item, parse_log


@pytest.mark.parametrize("text,want", [
    ("capstone 2h", ("capstone", 2)), ("2h capstone", ("capstone", 2)), ("football 1.5h", ("football", 1.5)),
    ("capstone 90m", ("capstone", 1.5)), ("capstone 1h 30m", ("capstone", 1.5)), ("Capstone  3 hours", ("capstone", 3)),
])
def test_logs(text, want):  # IN-2
    assert parse_log(text) == want


@pytest.mark.parametrize("text", ["2h", "hist essay due nov 2", "os midterm nov 5 10am", "capstone 40h", "hello"])
def test_not_logs(text):
    assert parse_log(text) is None


def local(item):
    return item.due.astimezone(QATAR).strftime("%Y-%m-%d %H:%M")


def test_essay_with_alias_and_no_time(settings):  # IN-1
    it = parse_item("hist essay due nov 2", settings, NOW)
    assert (it.course, it.title, it.kind, local(it)) == ("hist1001", "essay", "assignment", "2026-11-02 23:59")


@pytest.mark.parametrize("text,when", [
    ("os midterm nov 5 10am", "2026-11-05 10:00"), ("os midterm 5 nov at 2pm", "2026-11-05 14:00"),
    ("os midterm november 5th 10:30", "2026-11-05 10:30"), ("os midterm tomorrow 9am", "2026-10-26 09:00"),
    ("os midterm thu", "2026-10-29 23:59"),
])
def test_dates_and_times(settings, text, when):
    it = parse_item(text, settings, NOW)
    assert (it.course, it.title, it.kind, local(it)) == ("comp2201", "midterm", "exam", when)


def test_course_code_and_past_month_rolls_to_next_year(settings):
    it = parse_item("data3201 project report jan 10", settings, NOW)
    assert (it.course, it.title, local(it)) == ("data3201", "project report", "2027-01-10 23:59")


def test_unknown_course_is_other(settings):
    it = parse_item("visa appointment nov 12 9am", settings, NOW)
    assert (it.course, it.title, it.kind) == ("other", "visa appointment", "other")


@pytest.mark.parametrize("text", ["hello there", "nov 2", "hist essay"])
def test_not_items(settings, text):
    assert parse_item(text, settings, datetime(2026, 10, 25, tzinfo=QATAR)) is None
