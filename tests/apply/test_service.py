"""Partial help end to end: ledger, checklist, /answer (SPEC APP-6, APP-7, APP-8, VLT-4)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from bandwidth.ai import Ledger, Provider, Router
from bandwidth.apply.applications import Applications
from bandwidth.apply.browser import FillResult
from bandwidth.apply.service import ApplyService
from tests.apply.test_vault import PDF, PROFILE

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_job.json"
API = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/5456619008"
URL = "https://job-boards.greenhouse.io/anthropic/jobs/5456619008"
LOCAL = "http://localhost:11434"


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ the ledger (APP-7)


def test_states_move_only_by_compare_and_set(tmp_path):
    """APP-7"""
    apps = Applications(tmp_path / "agent.db")
    a = apps.create(URL, "greenhouse", "Anthropic", "Fellow", "partial")
    assert apps.get(a)["state"] == "planned"
    assert apps.move(a, {"planned"}, "filling") and apps.get(a)["state"] == "filling"
    assert not apps.move(a, {"planned"}, "filling")          # a second move from the old state does nothing
    assert apps.move(a, {"filling"}, "handed_over")


def test_unknown_states_are_refused(tmp_path):
    apps = Applications(tmp_path / "agent.db")
    a = apps.create(URL, "greenhouse", "Anthropic", "Fellow", "partial")
    with pytest.raises(ValueError):
        apps.move(a, {"planned"}, "teleported")


def test_a_submitted_posting_is_never_started_again(tmp_path):
    """APP-7"""
    apps = Applications(tmp_path / "agent.db")
    a = apps.create(URL, "greenhouse", "Anthropic", "Fellow", "fast")
    for frm, to in [("planned", "filling"), ("filling", "ready"), ("ready", "submitting"), ("submitting", "submitted")]:
        assert apps.move(a, {frm}, to)
    assert apps.submitted(URL)
    assert apps.submitted("https://job-boards.greenhouse.io/anthropic/jobs/1") is None


# ------------------------------------------------------------------ the service


class FakePage:
    pass


class FakeBrowser:
    def __init__(self):
        self.opened = []

    async def open(self, url):
        self.opened.append(url)
        return FakePage()


class Outbox:
    def __init__(self):
        self.sent = []

    async def __call__(self, text, markup=None):
        self.sent.append(text)
        return len(self.sent)


@pytest.fixture
def service(tmp_path):
    vdir = tmp_path / "vault"
    vdir.mkdir(mode=0o700)
    (vdir / "profile.yaml").write_text(PROFILE)
    (vdir / "cv.pdf").write_bytes(PDF)
    router = Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))
    outbox, browser = Outbox(), FakeBrowser()

    async def fake_fill(page, steps, shots=None):
        shots.mkdir(parents=True, exist_ok=True)
        (shots / "after-fill.png").write_bytes(b"png")
        return FillResult(filled=[s.field.name for s in steps if s.fillable], failed={}, unknown_required=["Pronouns*"],
                          screenshot=shots / "after-fill.png")

    svc = ApplyService(vault_dir=vdir, router=router, apps=Applications(tmp_path / "agent.db"), send=outbox,
                       browser=browser, audit_dir=tmp_path / "applications", fill=fake_fill)
    return svc, outbox, browser, vdir


def mock_api():
    respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    respx.post(f"{LOCAL}/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": json.dumps({"mappings": []})}, "done": True}))


@respx.mock
def test_partial_help_fills_hands_over_and_sends_a_numbered_checklist(service, tmp_path):
    """APP-6, APP-7, APP-8"""
    mock_api()
    svc, outbox, browser, _ = service
    app_id = run(svc.help(URL))
    row = svc.apps.get(app_id)
    assert row["state"] == "handed_over" and row["mode"] == "partial" and browser.opened == [URL]
    text = outbox.sent[-1]
    assert "Anthropic" in text and "open on your Mac" in text and "press Submit there" in text
    assert "1. " in text and "Why Anthropic?" in text and "essay" in text
    assert "Do you require visa sponsorship?" in text and "sensitive" in text
    assert "Agreement to Arbitrate" in text and "only you can agree" in text
    assert "Pronouns" in text and "not known to the plan" in text
    assert "/answer" in text
    audit = tmp_path / "applications" / str(app_id)
    plan = json.loads((audit / "plan.json").read_text())
    assert plan["url"] == URL and any(s["value"] == "Ada" for s in plan["steps"])
    assert (audit / "after-fill.png").exists()


@respx.mock
def test_required_questions_are_listed_before_optional_ones(service):
    """APP-6"""
    mock_api()
    svc, outbox, _, _ = service
    run(svc.help(URL))
    text = outbox.sent[-1]
    assert text.index("Why Anthropic?") < text.index("Additional Information")
    assert "(optional)" in text.split("Additional Information")[1].splitlines()[0]
    assert all(ln.lower().count("optional") <= 1 for ln in text.splitlines())


@respx.mock
def test_the_answer_hint_points_at_a_question_answer_can_save(service):
    """VLT-4: never an essay or a legal statement."""
    mock_api()
    svc, outbox, _, _ = service
    run(svc.help(URL))
    text = outbox.sent[-1]
    n = int(text.split("/answer ")[1].split()[0])
    item = next(ln for ln in text.splitlines() if ln.startswith(f"{n}. "))
    assert "essay" not in item and "only you can agree" not in item


@respx.mock
def test_answer_saves_the_exact_question_for_next_time(service):
    """VLT-4"""
    mock_api()
    svc, outbox, _, vdir = service
    run(svc.help(URL))
    lines = [ln for ln in outbox.sent[-1].splitlines() if "Do you require visa sponsorship?" in ln]
    n = int(lines[0].split(".")[0].strip())
    reply = run(svc.answer(f"{n} No"))
    assert "Saved" in reply and "visa sponsorship" in reply
    from bandwidth.apply.vault import Vault
    assert Vault.load(vdir).answer_for("Do you require visa sponsorship?") == "No"


@respx.mock
def test_answers_to_legal_statements_are_refused(service):
    """APP-2: the agent never agrees on the owner's behalf, so it never stores an agreement."""
    mock_api()
    svc, outbox, _, vdir = service
    run(svc.help(URL))
    n = next(int(ln.split(".")[0]) for ln in outbox.sent[-1].splitlines() if "Agreement to Arbitrate" in ln)
    assert "only you can agree" in run(svc.answer(f"{n} yes"))
    from bandwidth.apply.vault import Vault
    assert Vault.load(vdir).answer_for("Agreement to Arbitrate") is None


def test_answer_without_a_checklist_or_with_a_bad_number(service):
    """VLT-4"""
    svc, *_ = service
    assert "No checklist" in run(svc.answer("1 yes"))


@respx.mock
def test_a_submitted_posting_is_not_started_again(service):
    """APP-7"""
    mock_api()
    svc, outbox, browser, _ = service
    a = svc.apps.create(URL, "greenhouse", "Anthropic", "x", "fast")
    for frm, to in [("planned", "filling"), ("filling", "ready"), ("ready", "submitting"), ("submitting", "submitted")]:
        svc.apps.move(a, {frm}, to)
    assert run(svc.help(URL)) is None
    assert "already sent" in outbox.sent[-1] and browser.opened == []


def test_linkedin_is_never_automated(service):
    """APP-1, the owner's rule"""
    svc, outbox, browser, _ = service
    assert run(svc.help("https://www.linkedin.com/jobs/view/123")) is None
    assert "LinkedIn is never automated" in outbox.sent[-1] and browser.opened == []


@respx.mock
def test_a_missing_vault_is_explained_not_crashed(service, tmp_path):
    """VLT-1"""
    svc, outbox, _, vdir = service
    (vdir / "profile.yaml").unlink()
    assert run(svc.help(URL)) is None
    assert "vault" in outbox.sent[-1]


@respx.mock
def test_a_stop_is_reported_and_the_window_left_open(service):
    """APP-4"""
    mock_api()
    svc, outbox, _, _ = service

    async def captcha(page, steps, shots=None):
        return FillResult(stop="captcha")

    svc.fill = captcha
    app_id = run(svc.help(URL))
    assert svc.apps.get(app_id)["state"] == "handed_over"
    assert "CAPTCHA" in outbox.sent[-1] and "open on your Mac" in outbox.sent[-1]


# ------------------------------------------------------------------ Fast apply (SPEC §11)

from bandwidth.apply import service as service_module  # noqa: E402
from bandwidth.apply.forms import Field, Form  # noqa: E402
from bandwidth.apply.plan import agree_option, ai_policy  # noqa: E402


def fast_job() -> str:
    """The real Anthropic job without its AI-policy question, plus a cover letter, as many forms have."""
    d = json.loads(FIXTURE.read_text())
    d["questions"] = [q for q in d["questions"] if "AI Policy" not in q["label"]]
    d["questions"].insert(5, {"label": "Cover Letter", "required": False, "description": None, "fields": [
        {"name": "cover_letter", "type": "input_file", "values": []},
        {"name": "cover_letter_text", "type": "textarea", "values": []}]})
    return json.dumps(d)


REQUIRED_ANSWERS = ["Are you open to working in-person in one of our offices 25% of the time?",
                    "Do you require visa sponsorship?", "Have you ever interviewed at Anthropic before?",
                    "Are you open to relocation for this role?",
                    "Will you now or will you in the future require employment visa sponsorship to work in the "
                    "country in which the job you're applying for is located?"]


class Page:
    async def screenshot(self, path, full_page=False):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"png")


class FakeBrowser2(FakeBrowser):
    async def open(self, url):
        self.opened.append(url)
        return Page()


class Outbox2(Outbox):
    def __init__(self):
        super().__init__()
        self.markups = []

    async def __call__(self, text, markup=None):
        self.markups.append(markup)
        return await super().__call__(text, markup)

    def buttons(self):
        return [b["callback_data"] for m in self.markups if m for row in m["inline_keyboard"] for b in row]


@pytest.fixture
def fast(tmp_path, monkeypatch):
    from bandwidth.apply.vault import Vault
    vdir = tmp_path / "vault"
    vdir.mkdir(mode=0o700)
    (vdir / "profile.yaml").write_text(PROFILE)
    (vdir / "cv.pdf").write_bytes(PDF)
    (vdir / "about-me.md").write_text("# Work\n- Built a search tool.\n")
    vault = Vault.load(vdir)
    for q in REQUIRED_ANSWERS:
        vault.save_answer(q, "Yes")
    router = Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))
    outbox, browser = Outbox2(), FakeBrowser2()
    drafted = []

    async def fake_draft(router, vault, question, description, posting, *, company, role, cloud):
        drafted.append((question, cloud))
        body = "I build search tools. They answer in two languages."
        return f"Dear {company} team,\n\n{body}\n\nBest,\nAda" if "Cover" in question else body

    async def fake_fill(page, steps, shots=None):
        filled = [s.field.name for s in steps if s.fillable]
        return FillResult(filled=filled, shown={s.field.name: s.value for s in steps if s.fillable})

    clicks = []

    async def fake_submit(page):
        clicks.append(page)
        return "submitted"

    async def same(page, steps, shown):
        return {}

    async def tick(page, step):
        return None

    async def read_back(page, step):
        return step.value

    monkeypatch.setattr(service_module, "submit", fake_submit)
    monkeypatch.setattr(service_module, "verify", same)
    monkeypatch.setattr(service_module, "fill_one", tick)
    monkeypatch.setattr(service_module, "read", read_back)
    svc = ApplyService(vault_dir=vdir, router=router, apps=Applications(tmp_path / "agent.db"), send=outbox,
                       browser=browser, audit_dir=tmp_path / "applications", fill=fake_fill, draft=fake_draft)
    respx.get(url__startswith=API).respond(200, text=fast_job())
    respx.post(f"{LOCAL}/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": json.dumps({"mappings": []})}, "done": True}))
    return svc, outbox, drafted, clicks


def tap(svc, data):
    async def go():
        toast = await svc.on_button(data)
        if svc.tasks:
            await asyncio.gather(*svc.tasks)
        return toast
    return run(go())


def earn(svc):
    svc.apps.kv_set("dry_ok:greenhouse", 5)


def test_agree_option_and_ai_policy():
    """FA-3, FA-4"""
    f = Field(name="q", label="Agreement to Arbitrate", kind="select",
              options=["I understand and agree to the terms of the Agreement to Arbitrate set forth above."])
    assert agree_option(f).startswith("I understand")
    assert agree_option(Field(name="q", label="x", kind="select", options=["Yes", "No"])) == "Yes"
    assert agree_option(Field(name="q", label="x", kind="select", options=["Acme", "Other"])) is None
    assert agree_option(Field(name="q", label="Privacy", kind="multiselect", options=["Acknowledge/Confirm"])) \
        == "Acknowledge/Confirm"
    assert agree_option(Field(name="q", label="x", kind="multiselect", options=["Yes", "Other"])) is None
    form = Form("greenhouse", "Acme", "Intern", URL, "Please write your answers without the use of AI tools.")
    assert "posting" in ai_policy(form)
    form = Form("greenhouse", "Acme", "Intern", URL, "", [Field(name="q", label="AI Policy for Application", kind="select")])
    assert "AI Policy" in ai_policy(form)


@respx.mock
def test_a_form_with_an_ai_policy_runs_as_partial_help_and_says_why(service):
    """FA-3"""
    mock_api()
    svc, outbox, _, _ = service
    app_id = run(svc.help(URL))
    assert svc.apps.get(app_id)["mode"] == "partial"
    assert "left the essays to you" in outbox.sent[-1] and "AI Policy" in outbox.sent[-1]


@respx.mock
def test_a_dry_run_drafts_previews_and_never_offers_submit(fast):
    """FA-1, FA-4, FA-7, FA-8"""
    svc, outbox, drafted, clicks = fast
    app_id = run(svc.help(URL))
    row = svc.apps.get(app_id)
    assert row["mode"] == "dry" and row["state"] == "ready"
    assert [q for q, _ in drafted] == ["Cover Letter", "Why Anthropic?"] and all(cloud for _, cloud in drafted)
    pdf = next((svc.audit_dir / str(app_id)).glob("*Cover-Letter.pdf"))
    assert pdf.read_bytes().startswith(b"%PDF")
    text = "\n".join(outbox.sent)
    assert "Dry run 1 of 5" in text and "Why Anthropic?" in text and "I build search tools." in text
    assert "Agreement to Arbitrate" in text and "Additional Information" in text   # optional essay: left to the owner
    assert not any(b.startswith("s:") for b in outbox.buttons())
    assert f"v:{app_id}:y" in outbox.buttons() and f"g:{app_id}:0" in outbox.buttons()
    assert "1 of 5" in tap(svc, f"v:{app_id}:y") and "Already noted" in tap(svc, f"v:{app_id}:y")
    assert svc.apps.get(app_id)["state"] == "handed_over" and not clicks


@respx.mock
def test_a_wrong_dry_run_starts_the_count_again(fast):
    """FA-7"""
    svc, outbox, _, _ = fast
    svc.apps.kv_set("dry_ok:greenhouse", 4)
    app_id = run(svc.help(URL))
    assert "start again at 0" in tap(svc, f"v:{app_id}:n") and not svc.earned("greenhouse")


@respx.mock
def test_fast_apply_submits_once_after_every_agreement(fast):
    """FA-4, FA-5"""
    svc, outbox, _, clicks = fast
    earn(svc)
    app_id = run(svc.help(URL))
    submit_data = next(b for b in outbox.buttons() if b.startswith(f"s:{app_id}:"))
    assert "I agree first" in tap(svc, submit_data) and not clicks
    agrees = [b for b in outbox.buttons() if b.startswith(f"g:{app_id}:")]
    assert len(agrees) == 2
    assert all("Agreed" in tap(svc, b) for b in agrees)
    assert "Checking the form" in tap(svc, submit_data)
    assert len(clicks) == 1 and svc.apps.get(app_id)["state"] == "submitted" and "✅ Sent" in outbox.sent[-1]
    assert "Already sent" in tap(svc, submit_data) and len(clicks) == 1
    assert run(svc.help(URL)) is None and "already sent" in outbox.sent[-1]


def agree_all(svc, outbox, app_id):
    for b in outbox.buttons():
        if b.startswith(f"g:{app_id}:"):
            tap(svc, b)
    return next(b for b in outbox.buttons() if b.startswith(f"s:{app_id}:"))


@respx.mock
def test_an_older_or_stale_preview_cannot_submit(fast):
    """FA-5, FA-6"""
    svc, outbox, _, clicks = fast
    earn(svc)
    app_id = run(svc.help(URL))
    data = agree_all(svc, outbox, app_id)
    assert "older preview" in tap(svc, f"s:{app_id}:000000000000")
    svc.apps.kv_set(f"preview_at:{app_id}", "2020-01-01T00:00:00+00:00")
    assert "over 24 hours" in tap(svc, data) and not clicks and svc.apps.get(app_id)["state"] == "ready"


@respx.mock
def test_the_daily_cap_holds(fast):
    """FA-6"""
    svc, outbox, _, clicks = fast
    earn(svc)
    svc.settings.daily_cap = 0
    app_id = run(svc.help(URL))
    assert "Daily cap" in tap(svc, agree_all(svc, outbox, app_id)) and not clicks


@respx.mock
def test_a_form_changed_after_the_preview_is_not_submitted(fast, monkeypatch):
    """FA-5"""
    svc, outbox, _, clicks = fast
    earn(svc)

    async def changed(page, steps, shown):
        return {"first_name": "was 'Ada', now 'Bob'"}

    monkeypatch.setattr(service_module, "verify", changed)
    app_id = run(svc.help(URL))
    tap(svc, agree_all(svc, outbox, app_id))
    assert not clicks and svc.apps.get(app_id)["state"] == "handed_over"
    assert "Cancelled" in outbox.sent[-1] and "First Name" in outbox.sent[-1] and "Nothing was sent" in outbox.sent[-1]


@respx.mock
def test_no_confirmation_means_needs_check_and_no_second_click(fast, monkeypatch):
    """FA-5"""
    svc, outbox, _, _ = fast
    earn(svc)
    clicks = []

    async def captcha(page):
        clicks.append(1)
        return "captcha"

    monkeypatch.setattr(service_module, "submit", captcha)
    app_id = run(svc.help(URL))
    data = agree_all(svc, outbox, app_id)
    tap(svc, data)
    assert svc.apps.get(app_id)["state"] == "needs_check" and "won't click again" in outbox.sent[-1]
    tap(svc, data)
    assert clicks == [1]


@respx.mock
def test_fast_apply_with_required_gaps_hands_over(fast):
    """FA-4: Submit only when every required question has a value."""
    svc, outbox, _, _ = fast
    earn(svc)
    (svc.vault_dir / "answers.yaml").unlink()
    app_id = run(svc.help(URL))
    assert svc.apps.get(app_id)["state"] == "handed_over" and "can't submit" in outbox.sent[-1]
    assert not any(b.startswith("s:") for b in outbox.buttons())



@respx.mock
def test_applications_lists_what_each_one_is_doing(fast):
    """APP-7"""
    svc, *_ = fast
    assert "No applications yet" in svc.status()
    run(svc.help(URL))
    text = svc.status(busy="https://x")
    assert "waiting for you in the preview" in text and "dry run" in text and "Filling now" in text


@respx.mock
def test_a_dry_run_verdict_learns_what_the_owner_filled_in_the_window(fast, monkeypatch):
    """FA-11"""
    svc, outbox, _, _ = fast
    app_id = run(svc.help(URL))
    left = next(s for s in svc.runs[app_id].steps if s.source == "owner")

    async def window(page, step):
        return "June 2027" if step is left else step.value

    monkeypatch.setattr(service_module, "read", window)
    tap(svc, f"v:{app_id}:y")
    from bandwidth.apply.vault import Vault
    assert Vault.load(svc.vault_dir).answer_for(left.field.label) == "June 2027"
    assert "Learned 1 answer" in outbox.sent[-1]


@respx.mock
def test_learn_command_and_unchanged_guesses_need_a_confirmation(fast, monkeypatch):
    """FA-11"""
    svc, outbox, _, _ = fast
    assert "No open form" in run(svc.command("/learn", ""))
    app_id = run(svc.help(URL))
    s = next(s for s in svc.runs[app_id].steps if s.source == "owner")
    s.source, s.value = "guessed", "Yes"
    svc.runs[app_id].shown[s.field.name] = "Yes"
    assert run(svc.learn(app_id, confirm_guesses=False)) == []
    assert run(svc.learn(app_id, confirm_guesses=True)) == [(s.field.label, "Yes")]
    assert "Nothing new" not in run(svc.command("/learn", ""))


@respx.mock
def test_help_me_apply_fills_everything_and_never_submits(fast):
    """JOB-5: drafts and choices like Fast apply, then hands over with a learn button."""
    svc, outbox, drafted, clicks = fast
    earn(svc)                                   # even with Fast apply earned, Help me apply never submits
    app_id = run(svc.help(URL, mode="partial"))
    row = svc.apps.get(app_id)
    assert row["mode"] == "partial" and row["state"] == "handed_over"
    assert drafted and "Help me apply" in "\n".join(outbox.sent)
    assert not any(b.startswith(("s:", "v:", "g:")) for b in outbox.buttons())
    assert f"l:{app_id}" in outbox.buttons() and not clicks


def test_a_sign_in_page_waits_for_the_owner_and_try_again_resumes(fast, monkeypatch):
    """APP-10"""
    svc, outbox, _, _ = fast
    calls = []

    async def blocked(page):
        calls.append(1)
        return "sign-in" if len(calls) == 1 else None

    async def read_page(page, url, fx=None):
        return Form("devpost.com", "Devpost", "Hack", url, "posting",
                    [Field(name="@1", label="Full name", kind="text", required=True)])

    monkeypatch.setattr(service_module, "blocked", blocked)
    monkeypatch.setattr(service_module, "read_page", read_page)
    app_id = run(svc.help("https://acme.devpost.com/", mode="partial"))
    assert "sign in" in outbox.sent[-1] and f"r:{app_id}" in outbox.buttons()
    assert "Reading the form again" in tap(svc, f"r:{app_id}")
    assert svc.apps.get(app_id)["state"] == "handed_over" and svc.apps.get(app_id)["title"] == "Hack"
