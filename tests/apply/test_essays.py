"""Essays and cover letters: drafted locally per posting, polished anonymised (SPEC FA-1, FA-2)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bandwidth.ai import Ledger, Provider, Router
from bandwidth.apply.essays import Limit, anonymise, draft, fit, limit_of, polish, restore, style_problems
from bandwidth.apply.vault import Vault
from tests.apply.test_vault import PDF, PROFILE

CLOUD = "https://cloud.example.com/v1"
LOCAL = "http://localhost:11434"
POSTING = "Acme builds search for hospitals. We want an intern who ships. Arabic is a plus."


def run(coro):
    import asyncio
    return asyncio.run(coro)


@pytest.fixture
def vault(tmp_path):
    d = tmp_path / "vault"
    d.mkdir(mode=0o700)
    (d / "profile.yaml").write_text(PROFILE)
    (d / "cv.pdf").write_bytes(PDF)
    (d / "about-me.md").write_text("Built a bilingual RAG assistant. 94% top-3 recall.")
    (d / "style.md").write_text("<!-- guidance -->\nShort sentences. Lead with the verb.")
    return Vault.load(d)


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_KEY", "k-123456789")
    providers = [Provider("cloud", "openai", CLOUD, "m", key_env="CLOUD_KEY"), Provider("local", "ollama", LOCAL, "q")]
    return Router(providers, ledger=Ledger(tmp_path / "agent.db"), private_terms=["Example", "Freedonia"])


def local_says(text):
    return httpx.Response(200, json={"message": {"content": text}, "done": True})


def cloud_says(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


@pytest.mark.parametrize("label,description,limit", [
    ("Why Acme? (150 words max)", "", Limit(150, "words")),
    ("Cover letter", "Please keep it under 300 words.", Limit(300, "words")),
    ("Tell us about a project", "Maximum 1000 characters", Limit(1000, "characters")),
    ("Why Acme?", "", Limit(150, "words")),                    # default
    ("Describe yourself in up to 2,000 characters", "", Limit(2000, "characters")),
])
def test_length_limit_is_found_by_code(label, description, limit):
    """FA-1"""
    assert limit_of(label, description) == limit


def test_fit_cuts_at_a_sentence_end():
    """FA-1"""
    text = "One two three. Four five six. Seven eight nine."
    assert fit(text, Limit(7, "words")) == "One two three. Four five six."
    assert fit(text, Limit(20, "characters")) == "One two three."
    assert fit(text, Limit(150, "words")) == text


@pytest.mark.parametrize("text,problem", [
    ("Leveraging my skills, I built things.", "-ing"),
    ("I built a bot, ensuring quality.", "-ing"),
    ("I am passionate about AI.", "passionate"),
])
def test_style_problems_are_found_by_code(text, problem):
    """FA-1: the owner's style.md rules that code can check."""
    assert any(problem in p for p in style_problems(text))


def test_clean_text_has_no_style_problems():
    assert style_problems("Built a RAG assistant. It answers in Arabic. Recall hit 94%.") == []


def test_anonymise_and_restore(vault):
    """FA-2"""
    text = "I am Ada Example (ada@example.org), from Freedonia, at Example University."
    masked, back = anonymise(text, vault, ["Freedonia"])
    for secret in ("Ada", "Example", "ada@example.org", "Freedonia"):
        assert secret not in masked
    assert restore(masked, back) == text


@respx.mock
def test_polish_sends_only_the_anonymised_draft_and_restores_it(vault, router):
    """FA-2"""
    def answer(request):
        sent = json.loads(request.content)["messages"][-1]["content"]
        assert "Ada" not in sent and "Freedonia" not in sent and "ada@example.org" not in sent
        masked = sent.split("<draft>")[1].split("</draft>")[0].strip()
        return cloud_says(masked.replace("I built", "Built"))

    respx.post(f"{CLOUD}/chat/completions").mock(side_effect=answer)
    out = run(polish(router, vault, "I built a bot for Freedonia. Ada Example wrote it.", ["Freedonia"]))
    assert out == "Built a bot for Freedonia. Ada Example wrote it."


@respx.mock
def test_polish_that_drops_a_placeholder_keeps_the_draft(vault, router):
    """FA-2: a name the model lost can't be put back, so the draft stays."""
    respx.post(f"{CLOUD}/chat/completions").mock(return_value=cloud_says("A nicer text with no names."))
    draft_text = "Ada Example built a bot."
    assert run(polish(router, vault, draft_text, [])) == draft_text


@respx.mock
def test_polish_never_runs_locally(vault, router):
    """FA-2: no cloud provider → the draft stays as it is."""
    respx.post(f"{CLOUD}/chat/completions").mock(return_value=httpx.Response(503))
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=local_says("x"))
    assert run(polish(router, vault, "Built a bot.", [])) == "Built a bot."
    assert not local.called


@pytest.mark.parametrize("text,claim", [
    ("Problems I have spent years solving.", "years"),
    ("I have 5 years of experience.", "5"),
    ("Recall hit 97%.", "97"),
])
def test_claims_missing_from_the_facts_are_found(text, claim):
    """FA-1, principle 2: numbers and durations must come from the facts or the posting."""
    from bandwidth.apply.essays import unsupported
    assert any(claim in c for c in unsupported(text, "Recall hit 94% on 50 questions.", POSTING))


def test_supported_numbers_pass():
    from bandwidth.apply.essays import unsupported
    assert unsupported("Recall hit 94% on 50 questions.", "Recall hit 94% on 50 questions.", POSTING) == []


@respx.mock
def test_polish_that_breaks_the_style_keeps_the_draft(vault, router):
    """FA-2: seen live, a polish added ", showing the rigor…"."""
    respx.post(f"{CLOUD}/chat/completions").mock(return_value=cloud_says("Built a model, showing rigor."))
    assert run(polish(router, vault, "Built a model. It was rigorous.", [])) == "Built a model. It was rigorous."


def test_odd_characters_from_models_are_made_plain():
    """FA-2: seen live, non-breaking hyphens and narrow spaces ("$275 B")."""
    from bandwidth.apply.essays import _clean
    assert _clean("cross‑lingual $275 B spend") == "cross-lingual $275B spend"


# ------------------------------------------------------------------ cloud drafting (FA-1)


@pytest.mark.parametrize("text,flagged", [
    ("I am comfortable with PyTorch and SQL.", ["PyTorch"]),       # SQL is backed by PostgreSQL
    ("Shipped Docker images to AWS.", []),
    ("I want to work at Databricks.", []),                          # the company, not a claim
])
def test_skills_not_in_the_facts_are_found(text, flagged):
    """FA-1: seen live, the posting's requirements claimed as the owner's skills."""
    from bandwidth.apply.essays import unsupported_skills
    facts = "Engineering: Python, PostgreSQL, Docker, AWS."
    assert unsupported_skills(text, facts, company="Databricks") == flagged


def test_apply_settings_defaults_and_validation(tmp_path):
    """FA-1, FA-6"""
    from bandwidth.apply.settings import load_settings
    s = load_settings(tmp_path / "missing.yaml")
    assert s.drafting == "cloud_anonymised" and s.daily_cap == 5
    (tmp_path / "apply.yaml").write_text("drafting: local\ndaily_cap: 3\n")
    s = load_settings(tmp_path / "apply.yaml")
    assert s.drafting == "local" and s.daily_cap == 3
    (tmp_path / "bad.yaml").write_text("drafting: gpt\n")
    with pytest.raises(ValueError):
        load_settings(tmp_path / "bad.yaml")


def test_narrow_spaces_inside_words_become_spaces():
    """FA-1: seen live, "Winter 2027" came out as "Winter2027"."""
    from bandwidth.apply.essays import _clean
    assert _clean("Winter 2027 and $275 B and 85 000") == "Winter 2027 and $275B and 85 000"


def test_adjectives_ending_in_ing_are_not_style_problems():
    """FA-1: seen live, ", unforgiving" (an adjective in a quoted list) was flagged."""
    assert style_problems("They call it data-dense, unforgiving work. It is interesting, demanding work.") == []


# ------------------------------------------------------------------ cited drafts (FA-1)

FACTS = """<!-- guidance -->
# Work
## Acme Labs
- Built a bilingual RAG assistant. 94% top-3 recall on 50 questions.
## Side projects
- Built payment verification that cut manual checks by 85%.
# Skills
- Python, Docker, PostgreSQL.
"""


def says(*paragraphs) -> str:
    return json.dumps({"paragraphs": [[{"text": t, "sources": src} for t, src in para] for para in paragraphs]})


@pytest.fixture
def cited(vault):
    vault.about_me = FACTS.split("-->", 1)[1].strip()
    return vault


def test_facts_are_numbered_with_their_headings():
    """FA-1"""
    from bandwidth.apply.essays import numbered_facts
    facts = numbered_facts(FACTS.split("-->", 1)[1])
    assert facts == {"F1": "Work / Acme Labs: Built a bilingual RAG assistant. 94% top-3 recall on 50 questions.",
                     "F2": "Work / Side projects: Built payment verification that cut manual checks by 85%.",
                     "F3": "Skills: Python, Docker, PostgreSQL."}


@pytest.mark.parametrize("text,sources,problem", [
    ("My RAG assistant cut manual checks by 85%.", ["F2"], "rag"),        # seen live: a result moved to another project
    ("My RAG assistant hit 85% recall.", ["F1"], "85"),
    ("I know PyTorch.", [], "PyTorch"),
    ("I built RAG at Acme Labs.", ["F9"], "F9"),
])
def test_each_sentence_is_checked_against_only_what_it_cites(text, sources, problem):
    """FA-1"""
    from bandwidth.apply.essays import Sentence, distinctive, numbered_facts, sentence_problems
    facts = numbered_facts(FACTS.split("-->", 1)[1])
    claims, _ = sentence_problems(Sentence(text=text, sources=sources), facts, distinctive(facts), POSTING,
                                  company="Hooli", role="Intern")
    assert any(problem in c for c in claims)


@pytest.mark.parametrize("text,sources", [
    ("My RAG assistant hit 94% top-3 recall.", ["F1"]),
    ("Payment checks fell by 85%.", ["F2"]),
    ("I want to build search for hospitals.", ["P"]),
    ("Hooli ships fast, and so do I.", []),
])
def test_supported_sentences_pass(text, sources):
    """FA-1"""
    from bandwidth.apply.essays import Sentence, distinctive, numbered_facts, sentence_problems
    facts = numbered_facts(FACTS.split("-->", 1)[1])
    claims, _ = sentence_problems(Sentence(text=text, sources=sources), facts, distinctive(facts), POSTING,
                                  company="Hooli", role="Intern")
    assert claims == []


@respx.mock
def test_local_draft_cites_facts_and_never_reaches_the_cloud(cited, router):
    """FA-1, VLT-2"""
    cloud = respx.post(f"{CLOUD}/chat/completions").mock(return_value=cloud_says("x"))
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=local_says(says(
        [("Built a bilingual RAG assistant.", ["F1"]), ("It hit 94% top-3 recall.", ["F1"])],
        [("Hooli wants search for hospitals.", ["P"])])))
    out = run(draft(router, cited, "Why Hooli? (50 words)", "", POSTING, company="Hooli", role="Intern"))
    assert out == "Built a bilingual RAG assistant. It hit 94% top-3 recall.\n\nHooli wants search for hospitals."
    assert not cloud.called
    body = json.dumps(json.loads(local.calls[0].request.content))
    assert "F1: Work / Acme Labs" in body and "Lead with the verb" in body and "hospitals" in body
    assert "guidance" not in body and "50 words" in body


@respx.mock
def test_problems_get_one_rewrite_that_names_them(cited, router):
    """FA-1"""
    local = respx.post(f"{LOCAL}/api/chat").mock(side_effect=[
        local_says(says([("My RAG assistant cut manual checks by 85%.", ["F2"])])),
        local_says(says([("Cut manual checks by 85%.", ["F2"])]))])
    assert run(draft(router, cited, "Why Hooli?", "", POSTING, company="Hooli", role="Intern")) == "Cut manual checks by 85%."
    second = json.dumps(json.loads(local.calls[1].request.content))
    assert "sentence 1" in second and "rag" in second


@respx.mock
def test_sentences_still_unsupported_after_the_rewrite_are_dropped(cited, router):
    """FA-1, principle 2: an invented claim is never sent."""
    bad = says([("Built a RAG assistant.", ["F1"]), ("I have 5 years of PyTorch.", [])])
    respx.post(f"{LOCAL}/api/chat").mock(side_effect=[local_says(bad), local_says(bad)])
    assert run(draft(router, cited, "Why Hooli?", "", POSTING, company="Hooli", role="Intern")) == "Built a RAG assistant."


@respx.mock
def test_style_only_problems_survive_the_rewrite_except_ing_tails(cited, router):
    """FA-1: style is asked for once; only unsupported claims are dropped, and an -ing tail that is
    still there is cut by code (the owner's rule outranks the detail in the tail)."""
    styled = says([("Built a RAG assistant, achieving 94% top-3 recall.", ["F1"])])
    respx.post(f"{LOCAL}/api/chat").mock(side_effect=[local_says(styled), local_says(styled)])
    out = run(draft(router, cited, "Why Hooli?", "", POSTING, company="Hooli", role="Intern"))
    assert out == "Built a RAG assistant."


@respx.mock
def test_draft_over_the_limit_is_cut(cited, router):
    """FA-1"""
    respx.post(f"{LOCAL}/api/chat").mock(return_value=local_says(says(
        [("Built a RAG assistant.", ["F1"]), ("It hit 94% top-3 recall.", ["F1"])])))
    assert run(draft(router, cited, "Why? (4 words)", "", POSTING, company="Hooli", role="Intern")) == "Built a RAG assistant."


@respx.mock
def test_a_cover_letter_gets_greeting_and_sign_off_from_code(cited, router):
    """FA-1"""
    respx.post(f"{LOCAL}/api/chat").mock(return_value=local_says(says([("Built a RAG assistant.", ["F1"])])))
    out = run(draft(router, cited, "Cover letter", "", POSTING, company="Hooli", role="Intern"))
    assert out == "Dear Hooli team,\n\nBuilt a RAG assistant.\n\nBest,\nAda"


@respx.mock
def test_cloud_draft_is_anonymised_and_restored(cited, router):
    """FA-1, FA-2: the cloud sees placeholders, the owner gets their details back."""
    cited.about_me = "# Work\n- Ada Example built a RAG assistant at Example University. 94% recall."

    def answer(request):
        sent = json.dumps(json.loads(request.content))
        assert "Ada" not in sent and "Example" not in sent and "94% recall" in sent
        return cloud_says(says([("Built a RAG assistant at [[P2]].", ["F1"])]))

    cloud = respx.post(f"{CLOUD}/chat/completions").mock(side_effect=answer)
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=local_says("x"))
    out = run(draft(router, cited, "Why Hooli?", "", POSTING, company="Hooli", role="Intern", cloud=True))
    assert cloud.called and not local.called and out == "Built a RAG assistant at Example University."


@respx.mock
def test_cloud_draft_falls_back_to_the_local_model(cited, router):
    """FA-1"""
    respx.post(f"{CLOUD}/chat/completions").mock(return_value=httpx.Response(503))
    respx.post(f"{LOCAL}/api/chat").mock(return_value=local_says(says([("Built a RAG assistant.", ["F1"])])))
    assert run(draft(router, cited, "Why Hooli?", "", POSTING, company="Hooli", role="Intern", cloud=True)) \
        == "Built a RAG assistant."


@respx.mock
def test_cloud_rewrite_is_anonymised_too(cited, router):
    """FA-1: the second request carries the first draft, still without the owner's details."""
    cited.about_me = "# Work\n- Ada Example built search."
    calls = []

    def answer(request):
        sent = json.dumps(json.loads(request.content))
        calls.append(sent)
        assert "Ada" not in sent and "ada@example.org" not in sent
        if len(calls) == 1:
            return cloud_says(says([("[[P1]] knows PyTorch.", ["F1"])]))
        return cloud_says(says([("[[P1]] built search.", ["F1"])]))

    respx.post(f"{CLOUD}/chat/completions").mock(side_effect=answer)
    out = run(draft(router, cited, "Why Hooli?", "", POSTING, company="Hooli", role="Intern", cloud=True))
    assert len(calls) == 2 and "PyTorch" in calls[1] and out == "Ada Example built search."


def test_only_names_are_distinctive():
    """FA-1: seen live, a sentence was dropped for the ordinary word "model"."""
    from bandwidth.apply.essays import distinctive, numbered_facts
    facts = numbered_facts("- Built a fraud model at OrbiDefence.\n- Built an n8n pipeline.\n- Built RAG search.")
    assert distinctive(facts) == {"orbidefence", "n8n", "rag"}


def test_long_sentences_are_a_style_problem():
    """FA-1: simple English, most sentences short."""
    long = " ".join(["word"] * 30) + "."
    assert any("30-word sentence" in p for p in style_problems(long))
    assert not style_problems("I built a search tool for a ministry. It answers in Arabic and English.")


def test_cover_letters_default_to_250_words():
    """FA-1"""
    assert limit_of("Cover Letter").n == 250 and limit_of("Why us?").n == 150
    assert limit_of("Cover letter (100 words max)").n == 100


@respx.mock
def test_a_thin_cover_letter_is_sent_back_once_for_more(cited, router):
    """FA-1: measured, letters came back at a fifth of their length."""
    long_para = [("I built a bilingual RAG assistant.", ["F1"])] * 13
    local = respx.post(f"{LOCAL}/api/chat").mock(side_effect=[
        local_says(says([("Built a bilingual RAG assistant.", ["F1"])])),
        local_says(says(long_para, [("Hooli wants search for hospitals.", ["P"])]))])
    out = run(draft(router, cited, "Cover Letter (100 words)", "", POSTING, company="Hooli", role="Intern"))
    assert local.call_count == 2 and "write at least 60" in json.dumps(json.loads(local.calls[1].request.content))
    assert out.startswith("Dear Hooli team,") and "hospitals" in out


def test_the_prompt_asks_for_verb_object_detail_in_simple_english():
    """FA-1: the owner's sentence pattern."""
    from bandwidth.apply.essays import ESSAY_SYSTEM
    assert "verb + object + detail" in ESSAY_SYSTEM and "Simple, clear English" in ESSAY_SYSTEM


def test_curly_apostrophes_do_not_hide_banned_phrases():
    """FA-1: seen live, "I’m excited to" from a model."""
    assert style_problems("I\u2019m excited to join.")


@pytest.mark.parametrize("text,flagged", [
    ("I graduate in 2028 and can work full-time in London.", True),
    ("I am ready for full-time work.", True),
    ("I will relocate to London.", True),
    ("I can fit a 12 to 14 week internship.", True),
    ("Cloudflare hires full-time engineers in London.", False),      # about them, not me
    ("I built a Go service.", False),
])
def test_availability_claims_need_a_fact(text, flagged):
    """FA-1: seen live, an availability claim citing only the posting."""
    from bandwidth.apply.essays import unsupported
    assert bool(unsupported(text, "Built a Go service; graduating 2028.", "London full-time")) == flagged


def test_availability_backed_by_a_fact_passes():
    from bandwidth.apply.essays import unsupported
    assert not unsupported("I can work full-time from June.", "Available to work full-time from June.", "")


def test_ing_tails_are_cut_by_code():
    """FA-1: seen live, the rewrite kept ", proving I can shave milliseconds"."""
    from bandwidth.apply.essays import cut_ing_tails
    assert cut_ing_tails("Cut latency to 120 ms, proving I can work at scale.") == "Cut latency to 120 ms."
    assert cut_ing_tails("It was hard, interesting work.") == "It was hard, interesting work."


def test_cv_like_fragments_are_counted():
    """FA-1: seen live, a letter made of "Reduced…", "Achieved…", "Won…"."""
    from bandwidth.apply.essays import fragments
    assert fragments("Reduced latency. Achieved 94%. Won 2nd place. I like networks.") == (3, 4)
    assert fragments("I built a cache. It cut latency. Built it in Go.") == (1, 3)
    assert fragments("Pushes code to millions. I like it.") == (1, 2)


def test_cutting_is_a_verb_but_cutting_edge_is_not():
    """FA-1: seen live, ", cutting p95 latency" got through."""
    from bandwidth.apply.essays import cut_ing_tails
    assert cut_ing_tails("I built a cache, cutting latency to 120 ms.") == "I built a cache."
    assert cut_ing_tails("I like new, cutting-edge tools.") == "I like new, cutting-edge tools."
    assert style_problems("I built a cache, cutting latency to 120 ms.")


def test_the_prompt_follows_the_owners_mostly_verb_led_rule():
    """FA-1: the owner asked for mostly verb-led sentences; paragraphs open with "I" + verb."""
    from bandwidth.apply.essays import ESSAY_SYSTEM
    assert "Most sentences lead with the verb" in ESSAY_SYSTEM and "open\n  every paragraph" in ESSAY_SYSTEM


def test_a_future_graduation_is_not_in_the_past_tense():
    from datetime import date
    from bandwidth.apply.essays import fix_tense
    s = "I graduated with a BSc in Data Science in 2028."
    assert fix_tense(s, "2028-07", date(2026, 10, 29)) == "I will graduate with a BSc in Data Science in 2028."
    assert fix_tense(s, "2025-06", date(2026, 10, 29)) == s


def test_a_degree_not_yet_earned_is_in_the_future():
    from datetime import date
    from bandwidth.apply.essays import fix_tense
    assert fix_tense("I earned a BSc in Data Science with a 3.8 GPA.", "2028-07", date(2026, 10, 29)) \
        == "I will earn a BSc in Data Science with a 3.8 GPA."


def test_a_banned_phrase_marks_filler():
    from bandwidth.apply.essays import _banned_phrase
    assert _banned_phrase("I am excited to join the hackathon.") and _banned_phrase("I’m excited to apply.")
    assert not _banned_phrase("I built a RAG assistant.")
