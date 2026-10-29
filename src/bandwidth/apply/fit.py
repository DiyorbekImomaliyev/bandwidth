"""How well the owner fits a posting, and what to change to get the interview (SPEC APP-11).

The posting (public, sent as data) and the owner's numbered facts (anonymised, as in FA-1) go to
the public task `fit`. Code checks what comes back: a requirement counts as met only when it cites
real facts, and a suggested CV line is kept only when every claim in it is in the facts it cites,
the same check essay sentences get. Keywords are shown as "in the posting, not in your CV: add
only if true".
"""

from __future__ import annotations

import html
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from ..ai import AIError, Router
from .essays import anonymise, distinctive, numbered_facts, restore, sentence_problems, Sentence
from .forms import Form
from .guess import profile_facts
from .vault import Vault

log = logging.getLogger(__name__)

FIT_SYSTEM = """You are a senior recruiter for this role. Compare the posting with the candidate's facts.

Return JSON:
{"requirements": [{"requirement": "<short>", "status": "met|partly|missing", "sources": ["F2"]}],
 "keywords": ["<word or short phrase from the posting that a CV screener looks for>"],
 "rewrites": [{"text": "<one CV line, verb first>", "sources": ["F3"]}],
 "emphasise": ["<one short sentence: what to put first in the CV or letter for this role>"],
 "risks": ["<one short sentence: what could get the application rejected>"]}

Rules:
- 5 to 8 requirements, the ones that decide who gets an interview, most important first.
- "met" or "partly" only with the fact ids that show it. Never invent experience.
- Up to 3 rewrites: existing facts said in this posting's language, with its keywords where the
  facts support them. Every number and name must be in the cited facts.
- Keywords: up to 8, copied from the posting.
- Risks: visa or location limits, level (intern, new grad), dates, missing must-haves.
"""


class Requirement(BaseModel):
    requirement: str
    status: Literal["met", "partly", "missing"] = "missing"
    sources: list[str] = Field(default_factory=list)


class Rewrite(BaseModel):
    text: str
    sources: list[str] = Field(default_factory=list)


class Fit(BaseModel):
    requirements: list[Requirement] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    rewrites: list[Rewrite] = Field(default_factory=list)
    emphasise: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def check(fit: Fit, facts: dict[str, str], posting: str, *, company: str, role: str) -> Fit:
    """Keep only what the facts support."""
    reqs = []
    for r in fit.requirements[:8]:
        real = [i for i in r.sources if i in facts]
        reqs.append(r if r.status == "missing" or real else Requirement(requirement=r.requirement, status="missing"))
    special = distinctive(facts)
    rewrites = []
    for w in fit.rewrites[:3]:
        claims, _ = sentence_problems(Sentence(text=w.text, sources=w.sources), facts, special, "",
                                      company=company, role=role)
        if w.sources and not claims:
            rewrites.append(w)
        else:
            log.info("dropped a CV line with unsupported claims: %s", claims)
    cv = "\n".join(facts.values()).lower()
    known = f"{company} {role}".lower()  # the company and the job title are not CV keywords
    keywords = [k for k in dict.fromkeys(k.strip() for k in fit.keywords)
                if k and k.lower() in posting.lower() and k.lower() not in cv and k.lower() not in known][:8]
    return Fit(requirements=reqs, keywords=keywords, rewrites=rewrites, emphasise=fit.emphasise[:3], risks=fit.risks[:4])


def render(fit: Fit, form: Form) -> str:
    score = sum({"met": 1.0, "partly": 0.5}.get(r.status, 0) for r in fit.requirements)
    total = len(fit.requirements) or 1
    marks = {"met": "✅", "partly": "🟡", "missing": "❌"}
    lines = [f"🎯 <b>Your fit: {html.escape(form.title)}</b> · {html.escape(form.company)}",
             f"{score:g} of {total} main requirements", ""]
    lines += [f"{marks[r.status]} {html.escape(r.requirement)}" for r in fit.requirements]
    if fit.emphasise:
        lines += ["", "<b>Lead with</b>", *[f"• {html.escape(e)}" for e in fit.emphasise]]
    if fit.rewrites:
        lines += ["", "<b>CV lines for this job</b> (from your facts; paste over the closest line)",
                  *[f"• {html.escape(w.text)}" for w in fit.rewrites]]
    if fit.keywords:
        lines += ["", "<b>In the posting, not in your CV</b> (add only what is true)",
                  html.escape(", ".join(fit.keywords))]
    if fit.risks:
        lines += ["", "<b>Risks</b>", *[f"⚠️ {html.escape(r)}" for r in fit.risks]]
    return "\n".join(lines)


async def fit_report(router: Router, vault: Vault, form: Form, *, cloud: bool) -> str | None:
    facts = numbered_facts(vault.about_me)
    if not facts:
        return None
    listed = "\n".join(f"{i}: {t}" for i, t in facts.items())
    user = f"Role: {form.title}\nCompany: {form.company}\n\n# Candidate profile\n{profile_facts(vault)}\n\n# Facts\n{listed}"
    posting = re.sub(r"\n{3,}", "\n\n", form.posting)[:12_000]
    try:
        if cloud:
            masked, back = anonymise(user, vault, router.private_terms)
            got = await router.json("fit", FIT_SYSTEM, masked, Fit, data=posting)
            got = Fit.model_validate_json(restore(got.model_dump_json(), back))
        else:
            got = await router.json("fit_private", FIT_SYSTEM, user, Fit, data=posting)
    except AIError as e:
        log.warning("fit report failed: %s", e)
        return None
    return render(check(got, facts, posting, company=form.company, role=form.title), form)
