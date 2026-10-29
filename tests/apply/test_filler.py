"""The agent's browser fills a form and never submits it (SPEC APP-3, APP-4, APP-5).

Runs real Chrome, headless, on a local copy of Greenhouse's form markup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from bandwidth.planner.render import chrome_path  # noqa: E402

from bandwidth.apply.browser import Browser, fill, fill_one, submit, verify  # noqa: E402
from bandwidth.apply.forms import Field  # noqa: E402
from bandwidth.apply.plan import Step  # noqa: E402

if not chrome_path():
    pytest.skip("Google Chrome is not installed", allow_module_level=True)

PAGE = (Path(__file__).parent / "fixtures" / "greenhouse_form.html").as_uri()


def run(coro):
    return asyncio.run(coro)


def step(name, value, kind="text", source="identity", label=None):
    return Step(Field(name=name, label=label or name, kind=kind), source, value)


@pytest.fixture
def cv(tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    return path


async def fill_page(tmp_path, steps, html_uri=PAGE):
    async with Browser(tmp_path / "profile", headless=True) as browser:
        page = await browser.open(html_uri)
        result = await fill(page, steps, shots=tmp_path / "shots")
        state = await page.evaluate("""() => ({
            submitted: window.submitted === true,
            values: Object.fromEntries([...document.querySelectorAll('input[type=text], input[type=tel], textarea')]
                                       .map(e => [e.id, e.value])),
            chosen: [...document.querySelectorAll('.select__single-value')].map(e => e.textContent),
            resume: document.getElementById('resume-name')?.textContent})""")
        return result, state


def test_fills_text_selects_and_the_cv_then_stops_before_submitting(tmp_path, cv):
    """APP-3, APP-5"""
    steps = [
        step("first_name", "Ada"), step("email", "ada@example.org"), step("phone", "+44 7700 900123"),
        step("country", "United Kingdom", kind="select"), step("resume", str(cv), kind="file"),
        step("question_1", "https://www.linkedin.com/in/ada"),
        step("question_2", "Yes", kind="select", source="saved"),
        Step(Field(name="question_3", label="Why Acme?", kind="textarea"), "essay"),
    ]
    result, state = run(fill_page(tmp_path, steps))
    assert set(result.filled) == {"first_name", "email", "phone", "country", "resume", "question_1", "question_2"}
    assert result.failed == {} and result.stop is None
    assert state["values"]["first_name"] == "Ada" and state["values"]["question_3"] == ""  # the essay is left
    assert state["chosen"] == ["United Kingdom +44", "Yes"] and state["resume"] == "cv.pdf"
    assert not state["submitted"]
    assert result.screenshot and result.screenshot.exists()


def test_required_fields_the_plan_does_not_know_are_reported(tmp_path):
    """APP-4: e.g. a field on the page that is not in the API."""
    result, _ = run(fill_page(tmp_path, [step("first_name", "Ada")]))
    assert "How did you hear about us?*" in result.unknown_required
    assert "First Name*" not in result.unknown_required


def test_a_value_that_is_not_an_option_is_not_forced(tmp_path):
    """APP-3: nothing is filled blindly; the failure is reported."""
    result, state = run(fill_page(tmp_path, [step("question_2", "Maybe", kind="select")]))
    assert "question_2" in result.failed and state["chosen"] == []


def test_hidden_and_missing_fields_are_not_filled(tmp_path):
    """APP-3: only visible, enabled fields."""
    result, state = run(fill_page(tmp_path, [step("question_hidden", "x"), step("no_such_field", "x")]))
    assert set(result.failed) == {"question_hidden", "no_such_field"} and state["values"]["question_hidden"] == ""


def test_a_visible_captcha_is_left_to_the_owner_after_filling(tmp_path):
    """APP-4: everything is filled; the owner solves the CAPTCHA and submits."""
    html = tmp_path / "captcha.html"
    html.write_text('<input id="first_name"><iframe title="recaptcha challenge expires in two minutes" '
                    'style="width:300px;height:300px" srcdoc="solve me"></iframe>')
    result, state = run(fill_page(tmp_path, [step("first_name", "Ada")], html.as_uri()))
    assert result.stop == "captcha" and result.filled == ["first_name"]


def test_a_hidden_captcha_frame_is_ignored(tmp_path):
    """Lever keeps full-size hCaptcha frames with visibility: hidden on every form."""
    html = tmp_path / "hidden.html"
    html.write_text('<input id="first_name"><iframe title="hCaptcha security challenge" '
                    'style="width:300px;height:300px;visibility:hidden" srcdoc="x"></iframe>')
    result, _ = run(fill_page(tmp_path, [step("first_name", "Ada")], html.as_uri()))
    assert result.stop is None and result.filled == ["first_name"]


def test_a_sign_up_wall_counts_as_sign_in(tmp_path):
    """Meetup: "Sign up to attend this event" with Continue with Google, no password box."""
    html = tmp_path / "wall.html"
    html.write_text('<input id="first_name"><div role="dialog"><h2>Sign up to attend this event</h2>'
                    '<button>Continue with Google</button></div>')
    result, _ = run(fill_page(tmp_path, [step("first_name", "Ada")], html.as_uri()))
    assert result.stop == "sign-in"


def test_a_paid_checkout_is_never_filled(tmp_path):
    html = tmp_path / "paid.html"
    html.write_text('<input id="first_name"><h3>Order summary</h3><p>Total</p><p>$16.78</p><button>Checkout</button>')
    result, _ = run(fill_page(tmp_path, [step("first_name", "Ada")], html.as_uri()))
    assert result.stop == "paid" and result.filled == []


def test_a_free_checkout_is_filled(tmp_path):
    html = tmp_path / "free.html"
    html.write_text('<input id="first_name"><h3>Order summary</h3><p>Total</p><p>$0.00</p><button>Checkout</button>')
    result, _ = run(fill_page(tmp_path, [step("first_name", "Ada")], html.as_uri()))
    assert result.stop is None


def test_a_sign_in_page_stops_the_filling(tmp_path):
    """APP-4"""
    html = tmp_path / "login.html"
    html.write_text('<h1>Sign in</h1><input id="first_name"><input type="password" id="pw">')
    result, _ = run(fill_page(tmp_path, [step("first_name", "Ada")], html.as_uri()))
    assert result.stop == "sign-in" and result.filled == []


def test_a_search_box_takes_the_one_option_that_starts_with_the_value(tmp_path):
    """APP-3: seen live, "United Kingdom" is listed as "United Kingdom +44"."""
    result, state = run(fill_page(tmp_path, [step("country", "United", kind="select")]))
    assert "country" in result.failed and state["chosen"] == []          # two options start with it: no guess
    result, state = run(fill_page(tmp_path, [step("country", "Qatar", kind="select")]))
    assert result.filled == ["country"] and state["chosen"] == ["Qatar +974"]


def test_essays_and_agreed_statements_are_filled_when_they_have_a_value(tmp_path):
    """FA-1, FA-4: Fast apply gives essays a value; Partial help leaves them empty."""
    steps = [Step(Field(name="question_3", label="Why Acme?", kind="textarea"), "essay", "I build search tools."),
             Step(Field(name="question_2", label="I agree to the terms", kind="select"), "legal", "Yes")]
    result, state = run(fill_page(tmp_path, steps))
    assert state["values"]["question_3"] == "I build search tools." and state["chosen"] == ["Yes"]
    assert result.shown == {"question_3": "I build search tools.", "question_2": "Yes"}


def test_verify_finds_a_value_changed_after_the_preview(tmp_path):
    """FA-5: any difference cancels the submit."""
    async def go():
        async with Browser(tmp_path / "profile", headless=True) as browser:
            page = await browser.open(PAGE)
            steps = [step("first_name", "Ada"), step("country", "United Kingdom", kind="select")]
            result = await fill(page, steps)
            assert await verify(page, steps, result.shown) == {}
            await page.fill("#first_name", "Bob")
            return await verify(page, steps, result.shown)
    assert list(run(go())) == ["first_name"]


def test_submit_clicks_once_and_reads_the_confirmation(tmp_path):
    """FA-5"""
    async def go(confirm):
        async with Browser(tmp_path / "profile", headless=True) as browser:
            page = await browser.open(PAGE)
            await page.evaluate("""(confirm) => document.getElementById('application-form').onsubmit = () => {
                window.clicks = (window.clicks || 0) + 1;
                if (confirm) document.body.innerHTML = '<h1>Thank you for applying!</h1>';
                return false; }""", confirm)
            await page.check('[id="question_7[]_1"]')  # a required box blocks the browser's submit
            outcome = await submit(page, wait_ms=1500)
            clicks = await page.evaluate("window.clicks")
            return outcome, clicks
    assert run(go(True)) == ("submitted", 1)
    assert run(go(False)) == ("unclear", 1)


def test_fill_one_ticks_a_single_statement(tmp_path):
    """FA-4: the box is ticked only after I agree, for that application."""
    async def go():
        async with Browser(tmp_path / "profile", headless=True) as browser:
            page = await browser.open(PAGE)
            why = await fill_one(page, step("question_2", "Yes", kind="select", source="legal"))
            return why, await page.evaluate("[...document.querySelectorAll('.select__single-value')].map(e => e.textContent)")
    assert run(go()) == (None, ["Yes"])


def test_a_lone_acknowledge_box_is_ticked_after_i_agree(tmp_path):
    """FA-4: Greenhouse shows a one-option multi-select as a checkbox."""
    async def go():
        async with Browser(tmp_path / "profile", headless=True) as browser:
            page = await browser.open(PAGE)
            s = step("question_7[]", "Acknowledge/Confirm", kind="multiselect", source="legal")
            why = await fill_one(page, s)
            from bandwidth.apply.browser import read
            return why, await read(page, s), await page.is_checked('[id="question_7[]_1"]')
    assert run(go()) == (None, "Acknowledge/Confirm", True)


def test_a_place_search_takes_the_result_naming_city_and_country():
    """APP-3, seen live: "London" has several results."""
    from bandwidth.apply.browser import _choose
    texts = ["London, Ontario, Canada", "London, England, United Kingdom", "Londonderry, United Kingdom",
             "London Colney, England, United Kingdom"]
    assert _choose("London, United Kingdom", texts, search=True) == 1
    assert _choose("London, United Kingdom", texts, search=False) is None


def test_a_school_not_in_the_list_takes_other(tmp_path):
    """FA-10: seen live, CAMPUS is not in Greenhouse's school list."""
    f = Field(name="school--0", label="School", kind="select", fallback="Other")
    result, state = run(fill_page(tmp_path, [Step(f, "identity", "Example Institute of Technology")]))
    assert result.filled == ["school--0"] and "Other" in state["chosen"]


def test_a_closed_page_says_why(tmp_path):
    from bandwidth.apply.browser import CLOSED_JS, Browser
    html = tmp_path / "ended.html"
    html.write_text("<h1>Free Speech Symposium</h1><p>EVENT ENDED</p><p>Sales ended</p>")

    async def go():
        async with Browser(tmp_path / "profile", headless=True) as b:
            return await (await b.open(html.as_uri())).evaluate(CLOSED_JS)
    assert run(go()).lower() == "event ended"
