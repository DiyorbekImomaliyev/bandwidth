from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from bandwidth.planner import render
from bandwidth.planner.config import redact
from bandwidth.planner.ics import Deadline
from bandwidth.planner.plan import build_day, week_view
from bandwidth.planner.store import Store
from bandwidth.planner.telegram import Telegram, TelegramError, keyboard

SUN = date(2026, 10, 25)


def test_caption_format(settings):  # MSG-3
    st = Store(":memory:")
    due = [Deadline("u1", "comp2201", "lab 7 - stack & queue", "lab", datetime(2026, 10, 26, 20, 59, tzinfo=UTC))]
    text = render.caption(settings, build_day(settings, st, SUN), due, set(), 2)
    assert text.splitlines()[:4] == ["<b>sun 25 oct</b> - day 2", "", "<b>classes</b>",
                                     "- 08:00 hist1001 lecture - online"]
    assert "- tomorrow - comp2201 lab 7 - stack &amp; queue - 23:59" in text
    assert "- leetcode 2h - contains duplicate, valid anagram" in text
    assert "- learn 5.5h" in text and text.endswith("finals in 44 days")


def test_caption_stays_under_limit(settings):
    st = Store(":memory:")
    due = [Deadline(f"u{i}", "data3201", "x" * 150, "lab", datetime(2026, 10, 26, tzinfo=UTC) + timedelta(hours=i))
           for i in range(20)]
    text = render.caption(settings, build_day(settings, st, SUN), due, set(), 2)
    assert len(text) <= render.CAPTION_LIMIT and "<b>musts</b>" in text


def test_buttons_fit_telegram_limit():
    kb = render.must_buttons(SUN, {"uni"})
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["leetcode", "learn", "✓ uni", "push"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in kb["inline_keyboard"][0])
    with pytest.raises(ValueError):
        keyboard([("x", "y" * 65)])


def test_image_html_escapes_and_marks_today(settings):  # MSG-2
    st = Store(":memory:")
    st.sync_d2l([Deadline("u1", "comp2201", "lab <7>", "lab", datetime(2026, 10, 26, 20, 59, tzinfo=UTC))],
                datetime(2026, 10, 25, tzinfo=UTC))
    page, height = render.image_html(settings, build_day(settings, st, SUN), week_view(settings, st, SUN), 2)
    assert "lab &lt;7&gt;" in page and "<7>" not in page
    assert page.count('class="c now"') == 1 and "62.5h focus" in page
    assert 700 < height < 1000


def test_telegram_errors_never_show_the_token(monkeypatch):  # SAF-1
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    tg = Telegram("123:SECRET", "1", httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as e:
        tg.send("hi")
    assert "SECRET" not in str(e.value)


def test_redact_hides_feed_token(monkeypatch):
    monkeypatch.setenv("D2L_ICS_URL", "https://d2l.example/feed.ics?token=ABCDEF123")
    assert "ABCDEF123" not in redact("GET https://other/?token=ABCDEF123 failed")
