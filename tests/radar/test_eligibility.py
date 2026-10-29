"""Eligibility: the model extracts a posting's rules, code compares them with private facts (RAD-2, RAD-4)."""

import pytest

from bandwidth.radar.config import PrivateFacts
from bandwidth.radar.eligibility import Rules, decide, evidence_found

ME = PrivateFacts(citizenship=["Uzbekistan", "Uzbek"], lives_in=["Qatar", "Doha"], studies_in=["Qatar"],
                  level="undergraduate", languages=["English"])

SHENZHEN = ("Eligibility: Institutions in Mainland China, Hong Kong SAR, Macao SAR, and Chinese Taipei are not "
            "eligible Each team must have 2 to 5 members At least 60% of team members must not be Chinese nationals")
MEXT = ("Eligibility: The scholarship is open to all Nationals (except Japanese). Applicants who have graduated "
        "with a bachelor's degree can apply.")
KAWADER = "This program is open to Qatari nationals only. Applicants must be Qatari citizens."


def rules(**kw) -> Rules:
    return Rules(id="x", **kw)


@pytest.mark.parametrize("r,text,eligible,reason", [
    # the real case that an 8B model got wrong: a rule about other people is not a rule about me
    (rules(excluded_nationalities=["China"], evidence="At least 60% of team members must not be Chinese nationals"),
     SHENZHEN, True, ""),
    (rules(open_to_nationalities=["Qatar"], evidence="open to Qatari nationals only"), KAWADER, False, "only for Qatar nationals"),
    (rules(excluded_nationalities=["Japan"], applicant_levels=["graduate"],
           evidence="Applicants who have graduated with a bachelor's degree can apply."), MEXT, False, "for graduate applicants"),
    (rules(excluded_nationalities=["Uzbekistan"], evidence="open to Qatari nationals only"), KAWADER, False,
     "not open to Uzbekistan nationals"),
    (rules(open_to_nationalities=["Uzbek"], evidence="open to Qatari nationals only"), KAWADER, True, ""),
    (rules(must_live_in=["United Kingdom"], evidence="open to Qatari nationals only"), KAWADER, False, "must live in United Kingdom"),
    (rules(must_live_in=["Qatar"], evidence="open to Qatari nationals only"), KAWADER, True, ""),
    (rules(must_study_in=["Germany"], evidence="open to Qatari nationals only"), KAWADER, False, "must study in Germany"),
    (rules(languages=["German"], evidence="open to Qatari nationals only"), KAWADER, False, "needs German"),
    (rules(languages=["english"], evidence="open to Qatari nationals only"), KAWADER, True, ""),
    (rules(applicant_levels=["undergraduate", "graduate"], evidence="open to Qatari nationals only"), KAWADER, True, ""),
    (rules(), KAWADER, True, ""),
])
def test_decide(r, text, eligible, reason):
    """RAD-2: code decides from the extracted rules and the private facts."""
    d = decide(r, ME, text)
    assert d.eligible is eligible and d.reason == reason


def test_rules_without_evidence_in_the_text_are_ignored():
    """RAD-2: no invented restrictions — a quote the posting doesn't contain voids the rules."""
    r = rules(open_to_nationalities=["Qatar"], evidence="Only Qatari citizens may apply.")
    d = decide(r, ME, "A great internship for students in Doha. Apply by Friday.")
    assert d.eligible and d.unverified


def test_restrictions_with_no_evidence_at_all_are_ignored():
    """RAD-2"""
    d = decide(rules(open_to_nationalities=["Qatar"], evidence=""), ME, KAWADER)
    assert d.eligible and d.unverified


@pytest.mark.parametrize("quote,found", [
    ("open to Qatari nationals only", True),
    ("OPEN TO  qatari\nnationals ONLY", True),                      # case and whitespace
    ("This program is open to Qatari nationals only, applicants must be Qatari citizens", True),  # punctuation, near-verbatim
    ("The program is only for citizens of the State of Qatar", False),  # paraphrase
    ("", False),
])
def test_evidence_matching(quote, found):
    """RAD-2"""
    assert evidence_found(quote, KAWADER) is found


def test_no_private_facts_means_no_decision():
    """RAD-4: without facts there is nothing to compare; nothing is held back."""
    d = decide(rules(open_to_nationalities=["Qatar"], evidence="open to Qatari nationals only"), None, KAWADER)
    assert d.eligible


def test_unknown_level_does_not_exclude():
    """RAD-4: an owner who didn't state a level is never excluded by level rules."""
    facts = ME.model_copy(update={"level": None})
    assert decide(rules(applicant_levels=["phd"], evidence="open to Qatari nationals only"), facts, KAWADER).eligible


@pytest.mark.parametrize("value", ["all countries", "International", "any nationality", "Worldwide", "N/A"])
def test_wildcards_are_not_restrictions(value):
    """RAD-2: seen live — a local model wrote "all countries" as the only allowed nationality."""
    r = rules(open_to_nationalities=[value], must_live_in=[value], evidence="open to Qatari nationals only")
    assert r.open_to_nationalities == [] and not r.restricts
    assert decide(r, ME, KAWADER).eligible
