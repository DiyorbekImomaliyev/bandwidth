from datetime import UTC, datetime

from tests.planner.conftest import QATAR, fixture_text

from bandwidth.planner.ics import deadlines_from_feed, parse_events, to_ics, unfold


def by_uid():
    return {d.uid: d for d in deadlines_from_feed(fixture_text("feed.ics"), QATAR, "CAMPUS")}


def test_unfold_joins_continuation_lines():
    assert unfold("SUMMARY:ab\r\n cd\r\nX:1") == ["SUMMARY:abcd", "X:1"]


def test_parse_events_unescapes_values():
    ev = parse_events(fixture_text("feed.ics"))[0]
    assert ev["DESCRIPTION"] == "<p>Submit the &amp; report</p>"


def test_due_items_are_cleaned():  # SRC-3
    d = by_uid()["1-100@d2l.example"]
    assert (d.course, d.title, d.kind) == ("comp2201", "lab 6 - ipc", "lab")
    assert d.due == datetime(2026, 10, 19, 20, 59, 59, tzinfo=UTC)  # 23:59:59 Doha


def test_folded_quiz_drops_course_prefix_and_lockdown_note():
    d = by_uid()["1-101@d2l.example"]
    assert (d.course, d.title, d.kind) == ("data3201", "quiz 3", "quiz")


def test_available_and_plain_course_events_are_skipped():  # SRC-7
    uids = by_uid()
    assert "1-102@d2l.example" not in uids  # "- Available"
    assert "1-105@d2l.example" not in uids  # no "- Due"


def test_university_all_day_date_is_due_end_of_day():
    d = by_uid()["1-103@d2l.example"]
    assert (d.course, d.title, d.kind) == ("school", "last day to withdraw", "other")
    assert d.due.astimezone(QATAR).strftime("%Y-%m-%d %H:%M") == "2026-11-05 23:59"


def test_tzid_time_is_converted():
    d = by_uid()["1-104@d2l.example"]
    assert d.kind == "assignment"
    assert d.due.astimezone(QATAR).strftime("%Y-%m-%d %H:%M") == "2026-11-01 23:59"


def test_export_round_trips_and_folds():  # ICS-1
    items = list(by_uid().values())
    out = to_ics(items, [("comp3104", datetime(2026, 12, 8, 5, tzinfo=UTC), datetime(2026, 12, 8, 7, tzinfo=UTC),
                          "10.2.01")], datetime(2026, 10, 25, tzinfo=UTC))
    assert all(len(line.encode()) <= 75 for line in out.split("\r\n"))
    events = parse_events(out)
    assert len(events) == len(items) + 1
    assert any(e["SUMMARY"] == "comp3104 final exam" and e["LOCATION"] == "10.2.01" for e in events)
