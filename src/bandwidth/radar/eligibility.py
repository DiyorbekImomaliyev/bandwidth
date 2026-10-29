"""Who may apply: rules extracted from a posting by a model, decided by code (SPEC RAD-2).

The model only reads public postings and reports the rules they state, with the sentence it took
them from. This module compares those rules with the owner's private facts, which never reach a
model. When anything is unclear, the decision leans towards "eligible": a hidden opportunity costs
more than one extra card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import LEVELS, PrivateFacts

Level = Literal[LEVELS]  # type: ignore[valid-type]

# Words around a country name that don't change its meaning ("Chinese nationals" → "chinese").
NOISE = re.compile(r"\b(the|nationals?|citizens?|citizenship|people|passport holders?)\b")
WORD = re.compile(r"[a-z0-9]+")
# Values that mean "no restriction"; a model sometimes writes them into a list instead of leaving it empty.
WILDCARDS = {"all", "any", "all countries", "any country", "all nationalities", "any nationality", "international",
             "worldwide", "global", "everyone", "none", "n/a", "not specified", "unspecified"}
MIN_PREFIX = 5           # "uzbek" ↔ "uzbekistan", "qatar" ↔ "qatari"
EVIDENCE_OVERLAP = 0.85  # share of the quote's words that must appear in the text, in order-free form


def _norm(name: str) -> str:
    return " ".join(NOISE.sub(" ", name.lower()).split()).strip(" .,")


def _real(names: list[str]) -> list[str]:
    return [n for n in names if _norm(n) and _norm(n) not in WILDCARDS]


class Rules(BaseModel):
    id: str
    open_to_nationalities: list[str] = Field(default_factory=list, description="ONLY citizens of these countries may apply; [] if not stated")
    excluded_nationalities: list[str] = Field(default_factory=list, description="citizens of these countries may NOT apply")
    must_live_in: list[str] = Field(default_factory=list, description="applicants must live or hold residency here")
    must_study_in: list[str] = Field(default_factory=list, description="applicants must be enrolled at a university here")
    applicant_levels: list[Level] = Field(default_factory=list, description="who may apply; [] if anyone or not stated")
    languages: list[str] = Field(default_factory=list, description="languages applicants must speak")
    evidence: str = Field(default="", description="the exact sentence(s) stating these rules, copied word for word")

    @field_validator("open_to_nationalities", "excluded_nationalities", "must_live_in", "must_study_in", "languages")
    @classmethod
    def _drop_wildcards(cls, v: list[str]) -> list[str]:
        return _real(v)

    @property
    def restricts(self) -> bool:
        return any((self.open_to_nationalities, self.excluded_nationalities, self.must_live_in,
                    self.must_study_in, self.applicant_levels, self.languages))


class RulesBatch(BaseModel):
    results: list[Rules]


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str = ""
    unverified: bool = False  # the model stated rules it could not quote from the text


def _same(a: str, b: str, prefix: bool) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return prefix and min(len(a), len(b)) >= MIN_PREFIX and (a.startswith(b) or b.startswith(a))


def _any(names: list[str], mine: list[str], prefix: bool) -> bool:
    return any(_same(n, m, prefix) for n in names for m in mine)


def evidence_found(quote: str, text: str) -> bool:
    """True when the quote is in the text: verbatim (ignoring case, spacing and punctuation), or
    nearly so (≥ 85 % of its words appear in the text)."""
    q, t = WORD.findall(quote.lower()), WORD.findall(text.lower())
    if not q:
        return False
    if " ".join(q) in " ".join(t):
        return True
    words = set(t)
    return sum(w in words for w in q) / len(q) >= EVIDENCE_OVERLAP


def decide(rules: Rules, facts: PrivateFacts | None, text: str) -> Decision:
    if facts is None or not rules.restricts:
        return Decision(True)
    if not evidence_found(rules.evidence, text):
        return Decision(True, unverified=True)
    # Prefix matching where a miss would wrongly exclude the owner; exact matching where a match would.
    if rules.open_to_nationalities and not _any(rules.open_to_nationalities, facts.citizenship, prefix=True):
        return Decision(False, f"only for {', '.join(rules.open_to_nationalities)} nationals")
    if (hit := [n for n in rules.excluded_nationalities if _any([n], facts.citizenship, prefix=False)]):
        return Decision(False, f"not open to {hit[0]} nationals")
    if rules.must_live_in and not _any(rules.must_live_in, facts.lives_in, prefix=True):
        return Decision(False, f"must live in {', '.join(rules.must_live_in)}")
    if rules.must_study_in and not _any(rules.must_study_in, facts.studies_in, prefix=True):
        return Decision(False, f"must study in {', '.join(rules.must_study_in)}")
    if rules.applicant_levels and facts.level and facts.level not in rules.applicant_levels:
        return Decision(False, f"for {', '.join(rules.applicant_levels)} applicants")
    if missing := [lang for lang in rules.languages if not _any([lang], facts.languages, prefix=False)]:
        return Decision(False, f"needs {', '.join(missing)}")
    return Decision(True)
