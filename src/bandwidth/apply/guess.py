"""The owner's remaining dropdowns, chosen from their facts so Fast apply leaves almost nothing
(SPEC FA-10). The model sees the questions, their options and the owner's facts anonymised; code
keeps a choice only when it is exactly one of the options. Every choice is shown in the preview
as 🤖 for the owner to check, and becomes a saved answer once the owner confirms the dry run.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from ..ai import AIError, Router
from .essays import anonymise, numbered_facts, restore
from .forms import Form
from .plan import LEGAL, Step, match_option, match_options
from .vault import Vault

log = logging.getLogger(__name__)

# History questions are answered from the application ledger, not by a model.
HISTORY = re.compile(r"\bhave you (?:ever |previously )?(?:applied|interviewed|worked)\b|\bapplied (?:to|for) .{0,40}"
                     r"\b(?:before|previously|within|in the (?:last|past))\b", re.I)

# "How did you hear about us?": the honest answer for a job found on the company's own job list.
HEARD = re.compile(r"how (?:did )?you (?:hear|heard|find|found|learn|learned)|where did you (?:hear|find|see)"
                   r"|source of (?:this )?application", re.I)
HEARD_PREFERENCE = [r"company(?:'s)? (?:web)?site|careers? (?:site|page|website)|(?<!job )\bwebsite\b",
                    r"job board|online job|internet|online", r"^other\b"]

CHOOSE_SYSTEM = """You answer the dropdown questions of a job application for the owner.

For each question, pick the option(s) that are true for the owner, using only the facts given.
- A fact states it → "basis": the fact id (F3) or "profile".
- No fact states it, but one option is what a student with these facts would most likely answer
  (e.g. no standardized test listed → "I don't have ... score"; no offers mentioned → "No") →
  "basis": "likely".
- You can't tell → "choice": [] and "basis": "unknown". Never guess a score, a date or a name.
- "Prefer not to say" / "Choose not to disclose" only for questions about identity (gender, race,
  disability, veteran). For skills, languages or experience, list what the facts show or say unknown.
Copy option text exactly. One option for single choice; any number for "multi".
Answer as JSON: {"answers": [{"name": "<field name>", "choice": ["<option>"], "basis": "F3|profile|likely|unknown"}]}."""


class Choice(BaseModel):
    name: str
    choice: list[str] = Field(default_factory=list)
    basis: str = "unknown"


class Choices(BaseModel):
    answers: list[Choice]


def _heard(options: list[str], multi: bool = False) -> str | None:
    # A tick list's "Other" usually asks for more text elsewhere: left to the owner.
    for pattern in HEARD_PREFERENCE[:-1] if multi else HEARD_PREFERENCE:
        hits = [o for o in options if re.search(pattern, o, re.I)]
        if hits:
            return hits[0]
    return None


def profile_facts(vault: Vault) -> str:
    p, e = vault.profile, vault.profile.education
    lines = [f"Lives in: {', '.join(x for x in (p.city, p.country) if x)}"]
    if p.citizenship:
        lines.append(f"Citizenship: {p.citizenship}")
    if p.needs_sponsorship is not None:
        lines.append(f"Needs visa sponsorship to work abroad: {'yes' if p.needs_sponsorship else 'no'}")
    lines += [f"{k}: {v}" for k, v in (("Degree", e.degree), ("Major", e.major), ("GPA", e.gpa),
                                        ("Started", e.start), ("Graduates", e.graduation)) if v]
    return "\n".join(lines)


async def guess(router: Router, vault: Vault, form: Form, steps: list[Step], *, cloud: bool,
                applied_before: bool = False) -> int:
    """Fill the owner's remaining select and multi-select questions in place. Returns how many."""
    open_steps = [s for s in steps if s.source == "owner" and s.value is None and s.field.options
                  and s.field.kind in ("select", "multiselect") and not LEGAL.search(s.field.label)]
    done = 0
    rest = []
    for s in open_steps:
        if HISTORY.search(s.field.label) and (v := match_option("Yes" if applied_before else "No", s.field.options)):
            s.source, s.value, s.reason = "guessed", v, "from my records of your applications"
            done += 1
        elif HEARD.search(s.field.label) and (v := _heard(s.field.options, multi=s.field.kind == "multiselect")):
            s.source, s.value, s.reason = "guessed", v, "found on the company's own job list"
            done += 1
        else:
            rest.append(s)
    if not rest:
        return done
    facts = numbered_facts(vault.about_me)
    listed = "\n".join(f"{i}: {t}" for i, t in facts.items())
    questions = "\n".join(f"- name: {s.field.name}\n  question: {s.field.label}\n  type: "
                          f"{'multi' if s.field.kind == 'multiselect' else 'single'}\n  options: {s.field.options}"
                          for s in rest)
    user = f"# Facts\nprofile:\n{profile_facts(vault)}\n{listed}\n\n# Questions\n{questions}"
    try:
        if cloud:
            masked, back = anonymise(user, vault, router.private_terms)
            got = await router.json("choose", CHOOSE_SYSTEM, masked, Choices)
            got = Choices(answers=[Choice(name=c.name, choice=[restore(x, back) for x in c.choice], basis=c.basis)
                                   for c in got.answers])
        else:
            got = await router.json("choose_private", CHOOSE_SYSTEM, user, Choices)
    except AIError as e:
        log.warning("choosing options failed (%s); %d question(s) left to the owner", e, len(rest))
        return done
    by_name = {c.name: c for c in got.answers}
    for s in rest:
        c = by_name.get(s.field.name)
        if not c or not c.choice or c.basis == "unknown":
            continue
        if s.field.kind == "select" and len(c.choice) != 1:
            continue
        value = "; ".join(c.choice)
        chosen = match_options(value, s.field.options) if s.field.kind == "multiselect" else match_option(value, s.field.options)
        if chosen is None:
            continue
        basis = "from your facts" if c.basis != "likely" else "most likely; not in your facts"
        s.source, s.value, s.reason = "guessed", chosen, basis
        done += 1
    log.info("chose %d of %d open question(s)", done, len(open_steps))
    return done
