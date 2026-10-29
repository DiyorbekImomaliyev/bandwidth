from datetime import UTC, date, datetime, timedelta

from bandwidth.planner.ics import Deadline
from bandwidth.planner.store import Store

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def dl(uid, days, title="lab 1"):
    return Deadline(uid, "comp2201", title, "lab", NOW + timedelta(days=days))


def test_sync_upserts_keeps_done_and_removes_missing_future_items():  # DB-1
    st = Store(":memory:")
    st.sync_d2l([dl("a", 1), dl("b", 2)], NOW)
    st.set_done("a")
    st.sync_d2l([dl("a", 1, "lab 1 - renamed")], NOW)
    assert st.deadline("a").title == "lab 1 - renamed" and st.is_done("a")
    assert [d.uid for d in st.upcoming(NOW, NOW + timedelta(days=7))] == ["a"]


def test_manual_items_survive_feed_sync():
    st = Store(":memory:")
    st.add_manual(Deadline("m1", "hist1001", "essay", "assignment", NOW + timedelta(days=3), "manual"))
    st.sync_d2l([], NOW)
    assert [d.uid for d in st.upcoming(NOW, NOW + timedelta(days=7))] == ["m1"]


def test_unsolve_removes_reviews():
    st = Store(":memory:")
    d = date(2026, 10, 25)
    st.solve(5, d, (3, 10))
    assert st.next_review(d + timedelta(days=3)) == 5
    st.unsolve(5, d)
    assert st.solved() == {} and st.next_review(d + timedelta(days=30)) is None


def test_review_done_and_undo():
    st = Store(":memory:")
    d = date(2026, 10, 25)
    st.solve(5, d, (3, 10))
    st.review_done(5, d + timedelta(days=3), True)
    assert st.next_review(d + timedelta(days=9)) is None
    assert st.next_review(d + timedelta(days=10)) == 5
    st.review_done(5, d + timedelta(days=3), False)
    assert st.next_review(d + timedelta(days=3)) == 5


def test_keys_and_pending_round_trip():
    st = Store(":memory:")
    st.sync_d2l([dl("a" * 60, 1)], NOW)
    assert st.uid_for(st.key("a" * 60)) == "a" * 60
    st.put_pending(7, {"x": 1})
    assert st.pop_pending(7) == {"x": 1} and st.pop_pending(7) is None
