"""Any site: the page reader and the fillers for every kind of control (SPEC APP-9), in real Chrome."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from bandwidth.planner.render import chrome_path  # noqa: E402

from bandwidth.apply.browser import Browser, fill  # noqa: E402
from bandwidth.apply.forms import page_url, platform_of, read_page  # noqa: E402
from bandwidth.apply.plan import Step, classify  # noqa: E402

if not chrome_path():
    pytest.skip("Google Chrome is not installed", allow_module_level=True)

PAGE = (Path(__file__).parent / "fixtures" / "generic_form.html").as_uri()


def run(coro):
    return asyncio.run(coro)


async def read(tmp_path):
    async with Browser(tmp_path / "profile", headless=True) as browser:
        page = await browser.open(PAGE)
        return await read_page(page, PAGE)


def test_every_control_is_read_with_its_label_options_and_required_mark(tmp_path):
    """APP-9"""
    form = run(read(tmp_path))
    by = {f.label: f for f in form.fields}
    assert list(by) == ["Resume/CV", "Full name", "Email", "Current location", "Which university are you attending?",
                        "Are you legally authorized to work in the United States?", "Language Skill(s)",
                        "Do you have 7 years of experience?", "Why Acme?"]           # no autofill upload
    assert by["Full name"].required and not by["Why Acme?"].required
    assert (by["Which university are you attending?"].widget, by["Which university are you attending?"].options) == \
        ("native", ["Aalto University", "Other"])
    assert by["Are you legally authorized to work in the United States?"].options == ["Yes", "No"]
    assert by["Language Skill(s)"].widget == "checkbox" and by["Language Skill(s)"].kind == "multiselect"
    assert by["Do you have 7 years of experience?"].widget == "yesno"
    assert form.title == "Acme - Software Intern" and form.platform == platform_of(PAGE)


def test_every_kind_of_control_is_filled_and_read_back(tmp_path):
    """APP-9"""
    async def go():
        async with Browser(tmp_path / "profile", headless=True) as browser:
            page = await browser.open(PAGE)
            form = await read_page(page, PAGE)
            by = {f.label: f for f in form.fields}
            values = {"Full name": "Ada Example", "Which university are you attending?": "Other",
                      "Are you legally authorized to work in the United States?": "No", "Language Skill(s)": "English",
                      "Do you have 7 years of experience?": "No", "Why Acme?": "I build tools."}
            steps = [Step(by[k], "identity", v) for k, v in values.items()]
            result = await fill(page, steps)
            return result, values
    result, values = run(go())
    assert result.failed == {} and len(result.filled) == len(values)
    assert set(result.shown.values()) == set(values.values())


def test_vault_answers_the_page_labels(tmp_path):
    """APP-9: Lever/Ashby labels map to the vault like Greenhouse's."""
    from tests.apply.test_fill_more import STATED
    from bandwidth.apply.vault import Vault
    d = tmp_path / "vault"
    d.mkdir(mode=0o700)
    (d / "profile.yaml").write_text(STATED)
    (d / "cv.pdf").write_bytes(b"%PDF-1.7\n")
    vault = Vault.load(d)
    by = {f.label: f for f in run(read(tmp_path)).fields}
    assert classify(by["Full name"], vault).value == "Ada Example"
    assert classify(by["Current location"], vault).value == "London, United Kingdom"
    assert classify(by["Which university are you attending?"], vault).value == "Other"   # not in the list
    assert classify(by["Are you legally authorized to work in the United States?"], vault).value == "No"
    assert classify(by["Resume/CV"], vault).value.endswith("cv.pdf")


@pytest.mark.parametrize("url,where,platform", [
    ("https://jobs.lever.co/acme/0b7c-11aa-22bb", "https://jobs.lever.co/acme/0b7c-11aa-22bb/apply", "lever"),
    ("https://jobs.ashbyhq.com/ramp/34413f8d-26bf", "https://jobs.ashbyhq.com/ramp/34413f8d-26bf/application", "ashby"),
    ("https://amazonappdev2026.devpost.com/", "https://amazonappdev2026.devpost.com/", "devpost.com"),
    ("https://docs.google.com/forms/d/e/x/viewform", "https://docs.google.com/forms/d/e/x/viewform?hl=en", "google.com"),
    ("https://www.qstp.qa/events/1", "https://www.qstp.qa/events/1", "qstp.qa"),
])
def test_where_each_site_keeps_its_form(url, where, platform):
    """APP-9; each site earns Fast apply on its own (FA-7)."""
    assert (page_url(url), platform_of(url)) == (where, platform)


def test_the_company_is_spelled_as_the_page_writes_it():
    from bandwidth.apply.forms import spelled
    assert spelled("openai", " Research Engineer @ OpenAI") == "OpenAI"
    assert spelled("scaleai", "Scale AI | Careers") == "Scale AI"
    assert spelled("acme-corp", "Jobs") == "Acme Corp"


async def read_html(tmp_path, pages: dict[str, str], start: str):
    for name, body in pages.items():
        (tmp_path / name).write_text(body)
    url = (tmp_path / start).as_uri()
    async with Browser(tmp_path / "profile", headless=True) as browser:
        page = await browser.open(url)
        return await read_page(page, url)


def test_site_furniture_is_not_the_form(tmp_path):
    """Meetup, Eventbrite, listing sites: search boxes and newsletter sign-ups are skipped."""
    form = run(read_html(tmp_path, {"p.html": """
        <header><input placeholder="Search events..."></header>
        <form action="/newsletter/subscribe"><input type="email" placeholder="Enter your Email address"></form>
        <main><label for="n">Full name</label><input id="n"></main>"""}, "p.html"))
    assert [f.label for f in form.fields] == ["Full name"]


def test_a_listing_page_is_followed_to_the_form(tmp_path):
    """Listing site → "Visit official website to apply" → the organiser's form."""
    form = run(read_html(tmp_path, {
        "listing.html": '<nav><a href="other.html">Apply</a></nav><article><p>A fellowship.</p>'
                        '<a href="form.html" target="_blank">VISIT OFFICIAL WEBSITE TO APPLY</a></article>',
        "form.html": '<label for="e">Email</label><input id="e" required>'}, "listing.html"))
    assert [f.label for f in form.fields] == ["Email"] and form.url.endswith("form.html")


def test_a_page_listing_many_opportunities_is_not_followed(tmp_path):
    form = run(read_html(tmp_path, {
        "roundup.html": '<a href="a.html">Apply here</a><a href="b.html">Apply here</a>',
        "a.html": '<input id="x">'}, "roundup.html"))
    assert form.fields == [] and form.url.endswith("roundup.html")


# ------------------------------------------------------------------ multi-page forms (APP-12)

PAGE_ONE = """<label for="n">Full name</label><input id="n">
<div role="button" onclick="document.body.innerHTML = '<label for=e>Email</label><input id=e><div role=button>Submit</div>'">Next</div>"""


async def pages(tmp_path, files: dict[str, str], start: str, steps=None):
    from bandwidth.apply.browser import next_page
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    url = (tmp_path / start).as_uri()
    async with Browser(tmp_path / "profile", headless=True) as browser:
        page = await browser.open(url)
        first = await read_page(page, url)
        moved = await next_page(page)
        second = await read_page(page, url)
        return first, moved, second


def test_next_moves_to_the_second_page_and_its_fields_get_new_names(tmp_path):
    first, moved, second = run(pages(tmp_path, {"f.html": PAGE_ONE}, "f.html"))
    assert moved == "moved" and [f.label for f in second.fields] == ["Email"]
    assert {f.name for f in first.fields}.isdisjoint(f.name for f in second.fields)


def test_a_reloaded_page_keeps_numbering_on(tmp_path):
    """Google Forms loads each page as a new document."""
    first, moved, second = run(pages(tmp_path, {
        "a.html": '<label for="n">Full name</label><input id="n"><a role="button" href="b.html">Next</a>',
        "b.html": '<label for="e">Email</label><input id="e"><div role="button">Submit</div>'}, "a.html"))
    assert moved == "moved" and [f.label for f in second.fields] == ["Email"]
    assert {f.name for f in first.fields}.isdisjoint(f.name for f in second.fields)


def test_next_is_never_clicked_beside_a_submit_button(tmp_path):
    _, moved, _ = run(pages(tmp_path, {"s.html": '<input id="n"><button>Next</button><button>Submit</button>'}, "s.html"))
    assert moved == "none"


def test_a_page_that_does_not_move_is_stuck(tmp_path):
    _, moved, _ = run(pages(tmp_path, {"x.html": '<label for="n">Name</label><input id="n" required><button>Next</button>'},
                            "x.html"))
    assert moved == "stuck"


def test_a_form_builder_is_not_the_organiser(tmp_path):
    from bandwidth.apply.forms import FORM_BUILDERS
    assert "tally.so" in FORM_BUILDERS
    form = run(read_html(tmp_path, {"t.html": '<title>Aeronautics Hackathon 2026 - registration</title>'
                                              '<meta property="og:site_name" content="Tally Forms">'
                                              '<label for="n">Full name</label><input id="n">'}, "t.html"))
    assert form.company == "Tally Forms"  # a local file is not tally.so: the site name stays


def test_jotform_names_and_dropdowns_are_read_once(tmp_path):
    """Jotform: a question label plus sub-labels, and a styled face over each native select."""
    form = run(read_html(tmp_path, {"j.html": """
      <label id="label_7" for="first_7">Full Name*</label>
      <div role="group" aria-labelledby="label_7">
        <input id="first_7" aria-labelledby="sublabel_7_first" required><label id="sublabel_7_first" for="first_7">First Name</label>
        <input id="last_7" aria-labelledby="sublabel_7_last" required><label id="sublabel_7_last" for="last_7">Last Name</label>
      </div>
      <label id="label_38">Do you have a team?</label>
      <div><select id="input_38" aria-label="Do you have a team?" style="position:absolute;width:1px;height:1px;opacity:0">
        <option value="">Please Select</option><option>Yes</option><option>No</option></select>
        <div role="combobox" aria-labelledby="label_38">Please Select</div></div>"""}, "j.html"))
    assert [(f.label, f.widget) for f in form.fields] == [("First Name", "input"), ("Last Name", "input"),
                                                           ("Do you have a team?", "native")]


GOOGLE_FORM = """
<div role="list">
 <div role="listitem"><div role="heading" id="h1">بريدك الإلكتروني <span aria-label="Required question">*</span></div>
   <input type="email" aria-labelledby="h1"></div>
 <div role="listitem"><div role="heading" id="h2">Role <span aria-label="Required question">*</span></div>
   <div role="radiogroup" aria-labelledby="h2">
     <div role="radio" aria-label="Student" data-value="Student" aria-checked="false" tabindex="0"
          onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(r => r.setAttribute('aria-checked', r === this))">Student</div>
     <div role="radio" aria-label="Faculty" data-value="Faculty" aria-checked="false" tabindex="0"
          onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(r => r.setAttribute('aria-checked', r === this))">Faculty</div>
   </div></div>
 <div role="listitem"><div role="heading" id="h3">Track</div>
   <div role="list" aria-labelledby="h3">
     <div role="checkbox" aria-label="AI" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') !== 'true')">AI</div>
     <div role="checkbox" aria-label="Web" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') !== 'true')">Web</div>
   </div></div>
 <div role="listitem"><div role="heading" id="h4">Year</div>
   <div role="listbox" aria-labelledby="h4" tabindex="0"
        onclick="document.getElementById('pop').style.display = 'block'">
     <div role="option" data-value="" aria-selected="true">Choose</div>
     <div role="option" data-value="2027" aria-selected="false">2027</div>
     <div role="option" data-value="2028" aria-selected="false">2028</div>
   </div>
   <div id="pop" style="display:none">
     <div role="option" data-value="2028" onclick="
        const lb = document.querySelector('[role=listbox]');
        lb.querySelectorAll('[role=option]').forEach(o => o.setAttribute('aria-selected', o.dataset.value === '2028'));
        document.getElementById('pop').style.display = 'none'">2028</div>
   </div></div>
</div>"""


def test_google_forms_choices_are_read_and_filled(tmp_path):
    """Google Forms draws choices as ARIA widgets; the email question is labelled in the viewer's language."""
    async def go():
        (tmp_path / "g.html").write_text(GOOGLE_FORM)
        url = (tmp_path / "g.html").as_uri()
        async with Browser(tmp_path / "profile", headless=True) as browser:
            page = await browser.open(url)
            form = await read_page(page, url)
            by = {f.label: f for f in form.fields}
            values = {"Email": "ada@example.org", "Role": "Student", "Track": "AI; Web", "Year": "2028"}
            steps = [Step(f, "identity", values[f.label]) for f in form.fields]
            return by, await fill(page, steps)
    by, result = run(go())
    assert [(k, f.widget, f.required) for k, f in by.items()] == [
        ("Email", "input", True), ("Role", "radio", True), ("Track", "checkbox", False), ("Year", "listbox", False)]
    assert by["Role"].options == ["Student", "Faculty"] and by["Year"].options == ["2027", "2028"]
    assert result.failed == {} and len(result.filled) == 4


# ------------------------------------------------------------------ any site: web components, frames, dropdowns


async def read_and_fill(tmp_path, files: dict[str, str], start: str, values: dict[str, str]):
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    url = (tmp_path / start).as_uri()
    async with Browser(tmp_path / "profile", headless=True) as browser:
        page = await browser.open(url)
        await page.wait_for_timeout(300)
        form = await read_page(page, url)
        steps = [Step(f, "identity", values[f.label]) for f in form.fields if f.label in values]
        return form, await fill(page, steps)


def test_a_web_component_form_is_read_and_filled(tmp_path):
    """Shadow DOM: the controls are inside a custom element."""
    form, result = run(read_and_fill(tmp_path, {"w.html": """
      <apply-form></apply-form>
      <script>customElements.define('apply-form', class extends HTMLElement {
        connectedCallback() { this.attachShadow({mode: 'open'}).innerHTML =
          '<label for="n">Full name</label><input id="n" required>'; } });</script>"""},
        "w.html", {"Full name": "Ada Example"}))
    assert [f.label for f in form.fields] == ["Full name"] and result.filled and not result.failed


def test_a_form_in_a_frame_is_read_and_filled(tmp_path):
    """An embedded form (HubSpot, Typeform, an event checkout)."""
    form, result = run(read_and_fill(tmp_path, {
        "host.html": '<h1>Register</h1><iframe src="inner.html" style="width:600px;height:400px"></iframe>',
        "inner.html": '<label for="e">Email</label><input id="e" type="email" required>'},
        "host.html", {"Email": "ada@example.org"}))
    assert [f.label for f in form.fields] == ["Email"] and form.fields[0].name.startswith("@1")
    assert result.filled and not result.failed


def test_a_button_dropdown_is_opened_and_chosen(tmp_path):
    """Any brand of click-to-open dropdown: a button, a list of role=option."""
    form, result = run(read_and_fill(tmp_path, {"d.html": """
      <label id="l">Graduation year</label>
      <button role="combobox" aria-labelledby="l" aria-haspopup="listbox"
              onclick="document.getElementById('list').hidden = false">Select…</button>
      <ul id="list" role="listbox" hidden>
        <li role="option" onclick="pick(this)">2027</li><li role="option" onclick="pick(this)">2028</li></ul>
      <script>function pick(o) { document.querySelector('button').innerText = o.innerText;
                                 document.getElementById('list').hidden = true; }</script>"""},
        "d.html", {"Graduation year": "2028"}))
    assert [(f.label, f.widget) for f in form.fields] == [("Graduation year", "combobox")]
    assert result.filled and not result.failed
