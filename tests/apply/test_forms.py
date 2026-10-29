"""Reading application forms and deciding who fills each field (SPEC APP-1, APP-2, FA-3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from bandwidth.ai import Ledger, Provider, Router
from bandwidth.apply.forms import Field, posting_ref, read_form
from bandwidth.apply.plan import match_option, plan_form
from bandwidth.apply.vault import Vault
from bandwidth.radar.http import Fetcher
from tests.apply.test_vault import PDF, PROFILE

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_job.json"
API = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/5456619008"
LOCAL = "http://localhost:11434"
URL = "https://job-boards.greenhouse.io/anthropic/jobs/5456619008"


def run(coro):
    import asyncio
    return asyncio.run(coro)


async def read(url=URL):
    async with Fetcher() as fx:
        return await read_form(fx, url)


@pytest.mark.parametrize("url,ref", [
    (URL, ("greenhouse", "anthropic", "5456619008")),
    ("https://boards.greenhouse.io/stripe/jobs/7123456?gh_src=abc", ("greenhouse", "stripe", "7123456")),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/42", ("greenhouse", "acme", "42")),
    ("https://jobs.lever.co/palantir/0b7c-11aa", ("lever", "palantir", "0b7c-11aa")),
    ("https://jobs.ashbyhq.com/ramp/5f1e-22bb/application", ("ashby", "ramp", "5f1e-22bb")),
    ("https://www.linkedin.com/jobs/view/4483589179", None),
    ("https://example.com/careers/1", None),
])
def test_posting_urls_are_recognised(url, ref):
    """APP-1, JOB-4"""
    got = posting_ref(url)
    assert (got and (got.platform, got.board, got.job_id)) == ref if ref else got is None


@respx.mock
def test_greenhouse_form_is_read_from_the_public_api():
    """APP-1"""
    route = respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    form = run(read())
    assert "questions=true" in str(route.calls[0].request.url)
    assert form.platform == "greenhouse" and form.company == "Anthropic" and "Revenue" in form.title
    by = {f.name: f for f in form.fields}
    assert by["first_name"].kind == "text" and by["first_name"].required
    assert by["resume"].kind == "file" and "resume_text" not in by  # the paste-instead box is an alternative
    assert by["question_19018743008"].kind == "textarea" and by["question_19018743008"].label == "Why Anthropic?"
    assert "value this response highly" in by["question_19018743008"].description
    assert by["question_19018744008"].kind == "select" and by["question_19018744008"].options == ["Yes", "No"]
    assert by["veteran_status"].section == "eeoc" and not by["veteran_status"].required
    assert by["country"].section == "page" and by["country"].options == []  # on the page, not in the API
    assert "Anthropic" in form.posting and "<" not in form.posting


@respx.mock
def test_unsupported_platform_is_a_clear_error():
    """APP-1"""
    with pytest.raises(ValueError, match="lever"):
        run(read("https://jobs.lever.co/palantir/0b7c-11aa"))


# ------------------------------------------------------------------ the plan (APP-2)


@pytest.fixture
def vault(tmp_path):
    d = tmp_path / "vault"
    d.mkdir(mode=0o700)
    (d / "profile.yaml").write_text(PROFILE)
    (d / "cv.pdf").write_bytes(PDF)
    v = Vault.load(d)
    v.save_answer("Are you open to relocation for this role?", "yes")
    return v


@pytest.fixture
def router(tmp_path):
    return Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))


def mapped(*pairs):
    return httpx.Response(200, json={"message": {"content": json.dumps(
        {"mappings": [{"name": n, "key": k} for n, k in pairs]})}, "done": True})


@respx.mock
def test_every_field_of_the_real_form_gets_the_right_source(vault, router):
    """APP-2: legal and sensitive are never filled by the agent; identity and saved come from code."""
    respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=mapped(
        ("question_19018749008", "location"), ("question_19018740008", "none"), ("question_19018739008", "none"),
        ("question_19018737008", "none")))
    form = run(read())
    steps = {s.field.name: s for s in run(plan_form(form, vault, router))}

    def src(name):
        return steps[name].source

    assert (src("first_name"), steps["first_name"].value) == ("identity", "Ada")
    assert steps["email"].value == "ada@example.org" and steps["phone"].value == "+44 7700 900123"
    assert steps["resume"].value == str(vault.cv) and steps["country"].value == "United Kingdom"  # phone_country
    assert steps["question_19018747008"].value == "https://www.linkedin.com/in/ada-example"   # LinkedIn Profile
    assert src("question_19018738008") == "owner"                    # Website: none in the vault
    assert src("question_19018742008") == "legal"                    # AI Policy for Application (FA-3)
    assert src("question_19018751008") == src("question_19018752008") == "legal"   # arbitration
    assert src("question_19018744008") == src("question_19018745008") == "sensitive"  # visa sponsorship
    assert src("question_19018750008") == "sensitive"                # interviewed here before
    assert src("veteran_status") == "sensitive"
    assert src("question_19018743008") == "essay"                    # Why Anthropic?
    assert (src("question_19018748008"), steps["question_19018748008"].value) == ("saved", "Yes")  # matched option
    assert (src("question_19018749008"), steps["question_19018749008"].value) == ("mapped", "London, United Kingdom")
    assert src("question_19018740008") == "owner"
    assert src("question_19018737008") != "sensitive"  # "How do you pronounce your name?" is not about pronouns
    # the model only ever saw labels, never a vault value
    sent = json.dumps(json.loads(local.calls[0].request.content))
    assert "ada@example.org" not in sent and "Ada" not in sent and "address" in sent


@respx.mock
def test_a_sensitive_question_is_filled_only_from_a_saved_answer(vault, router):
    """APP-2, VLT-4"""
    respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    respx.post(f"{LOCAL}/api/chat").mock(return_value=mapped())
    vault.save_answer("Do you require visa sponsorship?", "Yes")
    steps = {s.field.name: s for s in run(plan_form(run(read()), vault, router))}
    assert (steps["question_19018744008"].source, steps["question_19018744008"].value) == ("sensitive", "Yes")
    assert steps["question_19018744008"].fillable


@respx.mock
def test_legal_questions_ignore_saved_answers(vault, router):
    """APP-2: the agent never agrees to anything, even if an answer was saved."""
    respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    respx.post(f"{LOCAL}/api/chat").mock(return_value=mapped())
    vault.save_answer("Agreement to Arbitrate", "I understand and agree")
    steps = {s.field.name: s for s in run(plan_form(run(read()), vault, router))}
    assert steps["question_19018752008"].source == "legal" and not steps["question_19018752008"].fillable


@respx.mock
def test_a_mapped_key_that_is_not_allowed_or_empty_goes_to_the_owner(vault, router):
    """APP-2: the model picks a key; code reads it; anything else is left to the owner."""
    respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    respx.post(f"{LOCAL}/api/chat").mock(return_value=mapped(
        ("question_19018749008", "password"), ("question_19018740008", "links.website")))
    steps = {s.field.name: s for s in run(plan_form(run(read()), vault, router))}
    assert steps["question_19018749008"].source == "owner" and steps["question_19018740008"].source == "owner"


@respx.mock
def test_no_local_model_means_unknown_fields_go_to_the_owner(vault, router):
    """APP-2"""
    respx.get(url__startswith=API).respond(200, text=FIXTURE.read_text())
    respx.post(f"{LOCAL}/api/chat").mock(side_effect=httpx.ConnectError("down"))
    steps = run(plan_form(run(read()), vault, router))
    assert steps and all(s.source != "mapped" for s in steps)
    assert {s.field.name: s for s in steps}["first_name"].value == "Ada"


@pytest.mark.parametrize("value,options,chosen", [
    ("yes", ["Yes", "No"], "Yes"),
    ("Y", ["Yes", "No"], "Yes"),
    ("United Kingdom", [], "United Kingdom"),          # a search box: the value is typed
    ("Maybe", ["Yes", "No"], None),
    ("London", ["London, UK", "Paris"], None),         # no guessing between near matches
])
def test_select_values_must_be_one_of_the_options(value, options, chosen):
    """APP-2"""
    assert match_option(value, options) == chosen


def test_fields_without_a_name_are_refused():
    with pytest.raises(ValueError):
        Field(name="", label="x", kind="text")


def test_terms_of_availability_are_not_legal_and_location_is_the_page_search(vault):
    """APP-2, seen live on Cloudflare 28 Oct 2026: "select terms that fit your availability"."""
    from bandwidth.apply.plan import classify
    f = Field(name="q[]", label="Which term(s) are you available to intern?", kind="multiselect",
              options=["Winter/Spring", "Summer"], description="Please only select terms that fit your availability.")
    assert classify(f, vault) is None or classify(f, vault).source != "legal"
    f = Field(name="q", label="I accept the terms and conditions", kind="select", options=["Yes"])
    assert classify(f, vault).source == "legal"
    from bandwidth.apply.forms import _gh_field
    loc = _gh_field({"label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text"}]}, "questions")
    assert (loc.name, loc.kind) == ("candidate-location", "select")


def test_enrolment_questions_are_not_the_school_name(vault):
    """APP-2, seen live on Cloudflare."""
    from bandwidth.apply.plan import _identity_key
    assert _identity_key(Field(name="q", label="Are you currently enrolled in a university or program?", kind="select")) is None
    assert _identity_key(Field(name="q", label="School", kind="text")) == "education.school"
    assert _identity_key(Field(name="q", label="University name", kind="text")) == "education.school"
