"""Fast apply leaves almost nothing: rules for stated facts, ranges, the education block, chosen
dropdowns, and answers learned from the window (SPEC FA-10, FA-11; owner's request 29 Oct 2026)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from bandwidth.ai import Ledger, Provider, Router
from bandwidth.apply.forms import Field, Form, gh_education
from bandwidth.apply.guess import guess
from bandwidth.apply.plan import Step, classify, match_option, match_options, rule_answer
from bandwidth.apply.vault import Vault
from tests.apply.test_vault import PDF, PROFILE

LOCAL = "http://localhost:11434"
STATED = PROFILE.replace("city: London\n", "city: London\ncitizenship: Uzbekistan\nneeds_sponsorship: true\n") \
                .replace("  graduation: 2027-06\n", "  start: 2023-09\n  graduation: 2027-06\n  gpa: \"3.87\"\n")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def vault(tmp_path):
    d = tmp_path / "vault"
    d.mkdir(mode=0o700)
    (d / "profile.yaml").write_text(STATED)
    (d / "cv.pdf").write_bytes(PDF)
    (d / "about-me.md").write_text("# Skills\n- Python, SQL.\n")
    return Vault.load(d)


def sel(label, options, **kw):
    return Field(name="q", label=label, kind="select", options=options, **kw)


YN = ["Yes", "No"]


@pytest.mark.parametrize("label,options,answer", [
    ("Will you require immigration sponsorship to begin working for IMC?", ["Yes", "No", "Maybe/I don’t know"], "Yes"),
    ("Will you now or in the future require visa sponsorship?", YN, "Yes"),
    ("Do you currently have the right to work in the UK?", YN, "Yes"),            # lives in the UK (fixture)
    ("Are you legally authorized to work in the United States?", YN, "No"),
    ("Are you authorized to work in Uzbekistan?", YN, "Yes"),                     # citizen
    ("Are you authorized to work in the country of this job?", YN, None),         # which country? not guessed
    ("What is your citizenship?", ["Uzbekistan", "Other"], "Uzbekistan"),
    ("Gender", ["Male", "Female", "Decline to self identify"], "Decline to self identify"),
    ("Veteran status", ["I am a veteran", "I am not a veteran"], None),
])
def test_sensitive_questions_follow_the_owners_stated_facts(vault, label, options, answer):
    """FA-10"""
    assert rule_answer(sel(label, options), vault) == answer


def test_rule_answers_never_go_into_a_text_box(vault):
    f = Field(name="q", label="Any other context about your visa sponsorship needs?", kind="textarea")
    assert rule_answer(f, vault) is None and classify(f, vault).value is None


def test_a_saved_answer_beats_a_rule(vault):
    vault.save_answer("Do you require visa sponsorship?", "No")
    assert classify(sel("Do you require visa sponsorship?", YN), vault).value == "No"


@pytest.mark.parametrize("value,options,chosen", [
    ("3.87", ["Below 3.2", "3.71 - 3.8", "3.81 - 3.9", "3.91 - 4.0", "Above 4.0"], "3.81 - 3.9"),
    ("3.1", ["Below 3.2", "3.21 - 3.3"], "Below 3.2"),
    ("4.2", ["3.91 - 4.0", "Above 4.0"], "Above 4.0"),
    ("1450", ["<1200", "1201 - 1300", "1401 - 1500"], "1401 - 1500"),
    ("abc", ["1 - 2"], None),
])
def test_numbers_fall_into_their_range(value, options, chosen):
    """FA-10: seen live, GPA 3.87 against IMC's ranges."""
    assert match_option(value, options) == chosen


def test_multi_select_values_each_match():
    assert match_options("python; java", ["Java", "C++", "Python"]) == "Python; Java"
    assert match_options("python; rust", ["Java", "Python"]) is None


def test_the_education_block_is_filled_from_the_vault(vault):
    """FA-10: School/Degree/Discipline/dates on Greenhouse's page."""
    steps = {f.name: classify(f, vault) for f in gh_education(True)}
    assert steps["school--0"].value == "Example University" and steps["school--0"].field.fallback == "Other"
    assert steps["degree--0"].value == "Bachelor's Degree"
    assert steps["start-month--0"].value == "September" and steps["start-year--0"].value == "2023"
    assert steps["end-month--0"].value == "June" and steps["end-year--0"].value == "2027"


def test_education_read_from_the_api_only_when_asked(tmp_path):
    from bandwidth.apply.forms import read_greenhouse, PostingRef
    from bandwidth.radar.http import Fetcher

    async def go(education):
        with respx.mock:
            respx.get(url__startswith="https://boards-api.greenhouse.io").respond(200, json={
                "title": "Intern", "company_name": "Acme", "questions": [], "education": education})
            async with Fetcher() as fx:
                return [f.name for f in (await read_greenhouse(fx, PostingRef("greenhouse", "acme", "1"))).fields]
    assert "school--0" in run(go("education_required")) and "school--0" not in run(go(None))


def test_a_learned_answer_fills_a_vault_gap(vault):
    """FA-11: no start date in the vault, but the owner chose it in the window once."""
    vault.profile.education.start = None
    vault.save_answer("Start date month", "September")
    step = classify(gh_education(True)[3], vault)
    assert (step.source, step.value) == ("saved", "September")


# ------------------------------------------------------------------ chosen dropdowns


def form_with(*fields):
    return Form("greenhouse", "IMC", "ML Intern", "https://x", "posting", list(fields))


def owner(f):
    return Step(f, "owner", reason="no saved answer")


@respx.mock
def test_dropdowns_are_chosen_and_only_exact_options_kept(vault, tmp_path):
    """FA-10"""
    offers = owner(sel("Do you currently have any offers?", YN))
    sat = owner(sel("Provide your best result on SAT:", ["I don't have SAT score", "1401 - 1500"]))
    langs = owner(Field(name="langs", label="Languages you know", kind="multiselect", options=["Java", "Python"]))
    made_up = owner(Field(name="made", label="Favourite colour", kind="select", options=["Red", "Blue"]))
    history = owner(Field(name="hist", label="Have you applied to IMC within the last 12 months?", kind="select", options=YN))
    for s, n in ((offers, "offers"), (sat, "sat")):
        s.field = s.field.model_copy(update={"name": n})
    answer = {"answers": [{"name": "offers", "choice": ["No"], "basis": "likely"},
                          {"name": "sat", "choice": ["1600"], "basis": "likely"},          # not an option: dropped
                          {"name": "langs", "choice": ["Python"], "basis": "F1"},
                          {"name": "made", "choice": ["Red"], "basis": "unknown"}]}      # unknown: dropped
    respx.post(f"{LOCAL}/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": json.dumps(answer)}, "done": True}))
    router = Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))
    steps = [offers, sat, langs, made_up, history]
    assert run(guess(router, vault, form_with(*[s.field for s in steps]), steps, cloud=False)) == 3
    assert (offers.source, offers.value) == ("guessed", "No") and "most likely" in offers.reason
    assert langs.value == "Python" and "your facts" in langs.reason
    assert sat.value is None and made_up.value is None
    assert history.value == "No" and "records" in history.reason


@respx.mock
def test_cloud_choosing_sends_anonymised_facts(vault, tmp_path):
    """FA-10, VLT-2"""
    seen = []

    def answer(request):
        seen.append(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"answers": []})}}]})

    respx.post("https://cloud.example.com/v1/chat/completions").mock(side_effect=answer)
    router = Router([Provider("cloud", "openai", "https://cloud.example.com/v1", "m")],
                    ledger=Ledger(tmp_path / "agent.db"))
    s = owner(sel("Do you have offers?", YN))
    run(guess(router, vault, form_with(s.field), [s], cloud=True))
    assert seen and "Example University" not in seen[0] and "Uzbekistan" in seen[0]


def test_high_school_graduation_is_not_the_degree(vault):
    """Seen live on IMC: "When did you graduate from High School?" got the degree's 2027-06."""
    from bandwidth.apply.plan import _identity_key
    assert _identity_key(sel("When did you graduate from High School?", ["2021", "2022"])) is None
    assert _identity_key(Field(name="g", label="Expected graduation date", kind="text")) == "education.graduation"


@pytest.mark.parametrize("value,options,chosen", [
    ("2028-07", ["I've already graduated", "August 2027 - December 2027", "January 2028 - July 2028",
                 "August 2028 - December 2028"], "January 2028 - July 2028"),
    ("2028-07", ["June 2028", "July 2028"], "July 2028"),
    ("2028-07", ["2027", "2028", "2029"], "2028"),
    ("2028-07", ["Spring 2028", "Fall 2028"], None),
])
def test_a_graduation_date_falls_into_its_option(value, options, chosen):
    """FA-10: seen live, IMC's graduation ranges."""
    assert match_option(value, options) == chosen


# ------------------------------------------------------------------ fit report (APP-11)


def test_the_fit_report_keeps_only_what_the_facts_support():
    """APP-11"""
    from bandwidth.apply.fit import Fit, check, render
    facts = {"F1": "Projects / Orbit: Built a Go cache; p95 latency fell from 900 ms to 120 ms.",
             "F2": "Skills: Python, SQL."}
    posting = "We want Go, Kubernetes and Python. Experience with latency work. Visa sponsorship not available."
    got = Fit.model_validate({
        "requirements": [{"requirement": "Go", "status": "met", "sources": ["F1"]},
                         {"requirement": "Kubernetes", "status": "met", "sources": []},         # no proof → missing
                         {"requirement": "Python", "status": "partly", "sources": ["F2"]}],
        "keywords": ["Kubernetes", "Python", "Rust"],      # Python is in the CV; Rust isn't in the posting
        "rewrites": [{"text": "Cut p95 latency from 900 ms to 120 ms with a Go cache.", "sources": ["F1"]},
                     {"text": "Ran Kubernetes clusters for 3 years.", "sources": ["F2"]}],   # invented → dropped
        "risks": ["The posting offers no visa sponsorship."]})
    fit = check(got, facts, posting, company="Acme", role="Intern")
    assert [r.status for r in fit.requirements] == ["met", "missing", "partly"]
    assert fit.keywords == ["Kubernetes"] and len(fit.rewrites) == 1
    from bandwidth.apply.forms import Form
    text = render(fit, Form("page", "Acme", "Intern", "https://x", posting))
    assert "1.5 of 3" in text and "Cut p95 latency" in text and "no visa sponsorship" in text


# ------------------------------------------------------------------ found on Lever (29 Oct 2026)


@pytest.mark.parametrize("label,answer", [
    ("Are you a U.S. citizen?", "No"),
    ("Are you a citizen of Uzbekistan?", "Yes"),
    ("Are you a U.S. permanent resident?", "No"),
    ("Are you a UK permanent resident?", None),        # lives in the UK; residence is not PR, so not guessed
])
def test_citizen_and_permanent_resident_questions(vault, label, answer):
    assert rule_answer(sel(label, YN), vault) == answer


def test_the_job_country_is_used_when_the_question_names_none(vault):
    q = sel("Are you authorized to work in the country where this job is located?", YN)
    assert rule_answer(q, vault, "Software Engineer, New York\nWe are hiring in New York, United States.") == "No"
    assert rule_answer(q, vault, "Engineer\nOffices in the United Kingdom and the United States") is None  # which one?


def test_a_school_not_in_the_list_falls_back_to_other(vault):
    school = Field(name="school", label="School", kind="select",
                   options=["Harvard University", "Other (School Not Listed)"])
    step = classify(school, vault)
    assert step.value == "Other (School Not Listed)"


def test_how_did_you_hear_is_answered_without_a_model(vault, tmp_path):
    heard = owner(sel("How did you hear about this opportunity?", ["Referral", "Company Website", "LinkedIn", "Other"]))
    router = Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))
    assert run(guess(router, vault, form_with(heard.field), [heard], cloud=False)) == 1
    assert heard.value == "Company Website"


def test_captcha_check_ignores_hidden_frames():
    """Lever keeps full-size hCaptcha frames with visibility: hidden on every form."""
    from bandwidth.apply.browser import CAPTCHA_JS
    assert "checkVisibility" in CAPTCHA_JS and "visibility" in CAPTCHA_JS


def test_us_in_a_sentence_is_not_the_united_states():
    from bandwidth.apply.plan import countries_in
    assert countries_in("Join us in Singapore. Tell us more.") == ["Singapore"]
    assert countries_in("Based in the US or UK") == ["United States", "United Kingdom"]


def test_how_you_heard_prefers_the_company_website(vault, tmp_path):
    heard = owner(sel("Please tell us how you heard about this opportunity.",
                      ["Job Board (Indeed, Monster, etc.)", "LinkedIn", "Palantir Website", "Other"]))
    router = Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))
    run(guess(router, vault, form_with(heard.field), [heard], cloud=False))
    assert heard.value == "Palantir Website"


@pytest.mark.parametrize("label", ["What is your most significant technical achievement?",
                                   "Please provide an example or evidence of your exceptional ability."])
def test_questions_in_one_line_boxes_are_essays(vault, label):
    assert classify(Field(name="q", label=label, kind="text"), vault).source == "essay"


def test_a_code_samples_question_takes_the_github_link(vault):
    step = classify(Field(name="q", label="Where can we find samples of your code?", kind="text"), vault)
    assert (step.source, step.value) == ("identity", "https://github.com/ada-example")


def test_learn_more_about_you_takes_the_best_link(vault):
    step = classify(Field(name="q", label="Where can we learn more about you?", kind="text"), vault)
    links = vault.profile.links
    assert step.value and step.value == (links.website or links.github or links.linkedin)


def test_a_heard_tick_list_never_ticks_other(vault, tmp_path):
    heard = owner(Field(name="h", label="How did you hear about OpenAI?", kind="multiselect",
                        options=["Other source (indicate in Additional Information)", "Twitter", "OpenAI Blog"]))
    router = Router([Provider("local", "ollama", LOCAL, "q")], ledger=Ledger(tmp_path / "agent.db"))
    with respx.mock:
        respx.post(f"{LOCAL}/api/chat").respond(200, json={"message": {"content": '{"answers": []}'}, "done": True})
        run(guess(router, vault, form_with(heard.field), [heard], cloud=False))
    assert heard.value is None


def test_a_question_with_options_is_never_an_essay(vault):
    """Tally: "How would you like to participate?" is a radio question."""
    q = Field(name="q", label="How would you like to participate?", kind="select", options=["Alone", "With a team"])
    step = classify(q, vault)
    assert step is None or step.source != "essay"


@pytest.mark.parametrize("label", ["Affiliation / Institution", "Team Leader College / Organization", "Organisation"])
def test_an_affiliation_is_the_school(vault, label):
    assert classify(Field(name="q", label=label, kind="text"), vault).value == "Example University"
