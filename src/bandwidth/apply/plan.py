"""Who fills each field of a form, decided by code first (SPEC APP-2).

Order: legal → sensitive → identity → saved answer → essay → the local model points the rest to a
vault key. Values always come from code: the model picks a key, never writes a value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from ..ai import AIError, Router
from .forms import Field, Form
from .vault import KEYS, Vault

log = logging.getLogger(__name__)

Source = Literal["identity", "saved", "mapped", "sensitive", "legal", "essay", "owner", "guessed"]

LEGAL = re.compile(
    r"\b(agree|agreement|consent|acknowledg|arbitrat|certif|attest|declar|terms (?:and conditions|of (?:use|service))"
    r"|privacy|policy|policies|signature"
    r"|e-?sign|i understand|use of ai|ai use|generative ai|artificial intelligence)", re.I)
SENSITIVE = re.compile(
    r"\b(visa|sponsor|work authori[sz]|authori[sz]ed to work|right to work|work permit|citizen|nationalit|immigration"
    r"|gender|\bsex\b|pronouns?(?!\w)|race|racial|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|transgender"
    r"|salary|compensation|pay expectation|expected pay|criminal|convict|background check|date of birth|\bage\b"
    r"|marital|religio|interviewed (?:at|with)|(?:worked|employed) (?:at|for|with) .{0,40}before"
    r"|previously (?:worked|employed|applied)|related to anyone|relatives?)", re.I)
ESSAY = re.compile(r"^(why|describe|tell us|explain|what (?:excites|interests|motivates)|how would you|share)\b"
                   r"|cover letter|additional information|anything else"
                   # Ashby asks these in one-line boxes (seen live, 29 Oct 2026).
                   r"|^what(?: is|'s| was| has been) your (?:most|proudest|favou?rite|biggest|greatest|best)\b"
                   r"|^(?:please )?(?:provide|give) (?:an? |some )?(?:example|evidence)", re.I)
NAME_KEYS = {"first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone",
             "country": "country", "location": "location", "candidate-location": "location"}
LABEL_KEYS = [
    (r"\blinked\s?in\b", "links.linkedin"), (r"\bgithub\b|samples? of (?:your )?code|code samples?", "links.github"),
    (r"\b(website|portfolio|personal site)\b", "links.website"),
    (r"learn more about you|online profile|links? (?:to|about) (?:you|your work)", "links.any"),
    (r"^(?:name of )?(?:your )?(?:current )?(?:school|university|college|institution)\b|\b(?:school|university|college)"
     r" (?:name|attended)\b|^(?:what|which) (?:school|university|college)"
     r"|^(?:affiliation|institution|organi[sz]ation)\b", "education.school"),  # a student's affiliation is the school (r"\bdegree\b", "education.degree"),
    (r"\b(major|discipline|field of study)\b", "education.major"), (r"^(?!.*high school).*\bgraduat", "education.graduation"),
    (r"\bgpa\b", "education.gpa"), (r"\bpreferred (?:first )?name\b", "preferred_name"),
    (r"\bfull name\b|^(?:legal |your )?name$", "full_name"),
    (r"^(?:current |your )?location\b|where are you (?:currently )?(?:based|located)", "location"), (r"^first name", "first_name"), (r"^(last name|surname|family name)", "last_name"),
    (r"^e-?mail", "email"), (r"^(phone|mobile)", "phone"), (r"^(current )?city\b", "city"),
]
# Who the question is about, on hackathon and event forms (seen live on Google Forms, 29 Oct 2026).
LABEL_PREFIX = re.compile(r"^(?:(?:team )?(?:leader|lead|captain)(?:'s)?|participant(?:'s)?|applicant(?:'s)?|attendee(?:'s)?"
                          r"|student(?:'s)?|your|contact|primary)\s+", re.I)
YES, NO = {"yes", "y", "true"}, {"no", "n", "false"}
EDU_KEYS = {"school--0": "education.school", "degree--0": "education.degree", "discipline--0": "education.major",
            "start-month--0": "education.start_month", "start-year--0": "education.start_year",
            "end-month--0": "education.end_month", "end-year--0": "education.end_year"}
DEGREES = [(r"^bachelor|^b\.?s|^b\.?a\b|^bsc", "Bachelor's Degree"), (r"^master|^m\.?s|^msc|^m\.?a\b", "Master's Degree"),
           (r"ph\.?d|doctor of philosophy", "Doctor of Philosophy (Ph.D.)"), (r"^associate", "Associate's Degree")]

# Sensitive questions answered by code from the owner's stated facts (owner's request, 29 Oct 2026).
WORK_AUTH = re.compile(r"authori[sz]ed to work|right to work|work authori[sz]ation|legally (?:able|eligible|permitted|allowed)"
                       r" to work|eligible to work|permitted to work|work permit", re.I)
SPONSOR = re.compile(r"sponsor|\bvisa\b", re.I)
NATIONALITY = re.compile(r"citizenship|nationalit|citizen of", re.I)
DEMOGRAPHIC = re.compile(r"gender|\bsex\b|pronoun|\brace\b|racial|ethnic|hispanic|latin[oax]|veteran|disab"
                         r"|sexual orientation|transgender", re.I)
DECLINE = re.compile(r"decline|don.?t wish|do not wish|prefer not|not to (?:answer|say|disclose|self)|choose not"
                     r"|rather not|i don.?t want", re.I)
# Abbreviations are case-sensitive: "join us" is not the US (seen live on Lever, 29 Oct 2026).
COUNTRIES = {
    "United States": r"(?-i:\bU\.?S\.?A?\b)|united states|\bamerica", "United Kingdom": r"(?-i:\bU\.?K\.?\b)|united kingdom|britain|england",
    "European Union": r"(?-i:\bEU\b|\bEEA\b)|european union", "United Arab Emirates": r"(?-i:\bUAE\b)|united arab emirates",
    **{c: rf"\b{c}\b" for c in ["Canada", "Netherlands", "Australia", "Singapore", "Hong Kong", "Switzerland", "Denmark",
                                "India", "Japan", "France", "Germany", "Ireland", "Spain", "Poland", "China", "Qatar",
                                "Saudi Arabia", "Uzbekistan", "Israel", "Brazil", "Mexico", "Korea", "Taiwan"]}}
NUMBER = re.compile(r"\d+(?:\.\d+)?")
COVER_LETTER = re.compile(r"cover\s*letter", re.I)
# FA-3: a form question about using AI, or a posting that asks for answers written without it.
AI_FIELD = re.compile(r"\b(?:use of ai|ai use|ai policy|ai[- ]generated|generative ai|artificial intelligence"
                      r"|ai (?:tools?|assistants?|systems?))\b", re.I)
AI_POSTING = re.compile(r"\b(?:without|not to use|do not use|don't use|refrain from using|must not use|no use of)"
                        r"\b[^.]{0,60}\b(?:ai|generative|chatgpt|llms?|language models?)\b", re.I)
AGREE = re.compile(r"^(?:yes|i agree|agree|i accept|accept|i acknowledge|acknowledged?|i understand|i confirm|i will read"
                   r"|confirmed?|i have read)\b", re.I)

MAP_SYSTEM = f"""You match application-form questions to the one piece of profile data that answers them.

Allowed keys: {", ".join(KEYS)}, none.
For each question return the key whose value answers it exactly, or "none" when no key does
(opinions, dates, availability, yes/no questions, anything not in the list). A key answers only
when its value could be typed or chosen as the answer itself. Never guess.
Answer as JSON: {{"mappings": [{{"name": "<field name>", "key": "<key or none>"}}]}}."""


@dataclass
class Step:
    field: Field
    source: Source
    value: str | None = None
    reason: str = ""

    @property
    def fillable(self) -> bool:
        """Has a value to type. Essays get one only in Fast apply (FA-1), legal statements only
        after the owner's I agree (FA-4)."""
        return self.value is not None and self.source in ("identity", "saved", "mapped", "sensitive", "essay", "legal",
                                                          "guessed")


class Mapping(BaseModel):
    name: str
    key: str


class Mappings(BaseModel):
    mappings: list[Mapping]


def match_option(value: str, options: list[str]) -> str | None:
    """The option equal to the value (ignoring case; yes/y/true are Yes), or for a number the one
    range that holds it ("3.87" → "3.81 - 3.9"). [] = a search box: typed as is."""
    if not options:
        return value
    v = value.strip().lower()
    for option in options:
        o = option.strip().lower()
        if o == v or (o in YES and v in YES) or (o in NO and v in NO):
            return option
    return _in_range(v, options) or _in_dates(v, options)


MONTH_NO = {m[:3]: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                            "september", "october", "november", "december"], 1)}
DATE = re.compile(r"(?:([a-z]{3})[a-z]*\.?\s+)?(\d{4})")


def _in_dates(value: str, options: list[str]) -> str | None:
    """A year-month in the option that holds it: "2028-07" → "January 2028 - July 2028", "July 2028", "2028"."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", value)
    if not m:
        return None
    x = int(m.group(1)) * 12 + int(m.group(2))

    def bound(mon: str | None, year: str, end: bool) -> int:
        return int(year) * 12 + (MONTH_NO.get(mon or "", 12 if end else 1))

    hits = []
    for option in options:
        dates = [(d.group(1), d.group(2)) for d in DATE.finditer(option.lower()) if not d.group(1) or d.group(1) in MONTH_NO]
        if len(dates) == 2 and bound(*dates[0], False) <= x <= bound(*dates[1], True):
            hits.append(option)
        elif len(dates) == 1 and bound(*dates[0], False) <= x <= bound(*dates[0], True) \
                and not re.search(r"before|after|already", option, re.I):
            hits.append(option)
    return hits[0] if len(hits) == 1 else None


def _in_range(value: str, options: list[str]) -> str | None:
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    x = float(value)
    if re.search(r"[a-z]{3,}\s+\d{4}", " ".join(options).lower()):
        return None  # date options: not number ranges
    hits = []
    for option in options:
        o = option.lower().replace(",", "")
        nums = [float(n) for n in NUMBER.findall(o)]
        if len(nums) == 2 and re.search(r"\d\s*(?:-|–|to)\s*\d", o):
            if nums[0] <= x <= nums[1]:
                hits.append(option)
        elif len(nums) == 1 and (re.search(r"below|under|less than|<", o)):
            if x < nums[0]:
                hits.append(option)
        elif len(nums) == 1 and re.search(r"above|over|more than|>|\+", o):
            if x > nums[0] or (o.endswith("+") and x >= nums[0]):
                hits.append(option)
    return hits[0] if len(hits) == 1 else None


def match_options(value: str, options: list[str]) -> str | None:
    """A multi-select: "Python; Java" → each must be an option; joined back with "; "."""
    chosen = [match_option(v, options) for v in value.split(";") if v.strip()]
    return "; ".join(c for c in chosen if c) if chosen and all(chosen) else None


def countries_in(text: str) -> list[str]:
    return [c for c, pattern in COUNTRIES.items() if re.search(pattern, text, re.I)]


def rule_answer(f: Field, vault: Vault, where: str = "") -> str | None:
    """A sensitive question answered from what the owner stated in the vault, or None. `where`: the
    job's title and location, for "the country you are applying to"."""
    if f.kind in ("textarea", "file"):
        return None  # "any other context about your visa?" is the owner's to write
    p = vault.profile
    text = _text(f)
    home = {c.lower() for c in (p.country, p.citizenship) if c}
    if WORK_AUTH.search(text):
        named = countries_in(text)
        if not named and re.search(r"\b(?:the|this) (?:country|location)|where (?:this|the) (?:job|role)", text, re.I):
            named = countries_in(where)[:1] if len(countries_in(where)) == 1 else []
        if named and home:
            return "Yes" if any(c.lower() in home for c in named) else "No"
        return None
    if SPONSOR.search(text) and p.needs_sponsorship is not None:
        return "Yes" if p.needs_sponsorship else "No"
    resident = re.search(r"\b(citizen|national|permanent resident)\b", text, re.I)
    if resident and set(f.options) <= {"Yes", "No"} and f.options and (named := countries_in(text)):
        if resident.group(1).lower() == "permanent resident":  # PR is not residence: only "No" is sure
            return "No" if all(c.lower() not in home for c in named) else None
        return "Yes" if p.citizenship and any(c.lower() == p.citizenship.lower() for c in named) else "No"
    if NATIONALITY.search(text) and p.citizenship:
        stem = p.citizenship.lower()[:5]
        hits = [o for o in f.options if stem in o.lower()]
        return hits[0] if len(hits) == 1 else (p.citizenship if not f.options else None)
    if DEMOGRAPHIC.search(text) or f.section == "eeoc":
        hits = [o for o in f.options if DECLINE.search(o)]
        return hits[0] if hits else None
    return None


def degree_option(value: str) -> str:
    return next((name for pattern, name in DEGREES if re.search(pattern, value.strip(), re.I)), value)


def _text(f: Field) -> str:
    return f"{f.label}\n{f.description}"


def _identity_key(f: Field) -> str | None:
    if f.section == "page" and f.label == "Phone country":
        return "phone_country"
    if f.name in EDU_KEYS:
        return EDU_KEYS[f.name]
    if f.name in NAME_KEYS:
        return NAME_KEYS[f.name]
    label = LABEL_PREFIX.sub("", f.label.strip())  # "Team Leader Email" → "Email"
    return next((key for pattern, key in LABEL_KEYS if re.search(pattern, label, re.I)), None)


def _fit(f: Field, value: str | None, source: Source, missing: str) -> Step:
    if value is None:
        return Step(f, "owner", reason=missing)
    if f.name == "degree--0":
        value = degree_option(value)
    if f.kind in ("select", "multiselect"):
        chosen = match_options(value, f.options) if f.kind == "multiselect" and f.options else match_option(value, f.options)
        if chosen is None and source == "identity" and _identity_key(f) == "education.school":
            chosen = next((o for o in f.options if re.match(r"other\b", o.strip(), re.I)), None)
        if chosen is None:
            # A mapped key whose value isn't an option was the wrong key: just ask, don't show it.
            return Step(f, "owner", reason="no saved answer" if source == "mapped"
                        else f"\"{value}\" is not one of its options")
        return Step(f, source, chosen)
    return Step(f, source, value)


def classify(f: Field, vault: Vault, where: str = "") -> Step | None:
    """Everything code can decide alone; None = ask the local model."""
    if f.section == "eeoc" or SENSITIVE.search(_text(f)) and not LEGAL.search(f.label):
        saved = vault.answer_for(f.label)
        if saved is None:
            saved = rule_answer(f, vault, where)
        if saved is None:
            return Step(f, "sensitive", reason="sensitive: only an answer you saved is used")
        step = _fit(f, saved, "sensitive", "")
        return step if step.source == "sensitive" else Step(f, "sensitive", reason=step.reason)
    if LEGAL.search(_text(f)):
        return Step(f, "legal", reason="a legal statement: only you can agree to it")
    if f.kind == "file":
        if COVER_LETTER.search(f"{f.name} {f.label}"):
            return Step(f, "essay", reason="a cover letter: written by you, or drafted in Fast apply")
        if re.search(r"resume|\bcv\b", f"{f.name} {f.label}", re.I):
            return Step(f, "identity", str(vault.cv)) if vault.cv else Step(f, "owner", reason="no vault/cv.pdf")
        return Step(f, "owner", reason="a file the vault doesn't have")
    if key := _identity_key(f):
        if (value := vault.value(key)) is None and (saved := vault.answer_for(f.label)) is not None:
            return _fit(f, saved, "saved", "")  # learned from the window (FA-11)
        return _fit(f, value, "identity", f"not in your vault ({key})")
    if (saved := vault.answer_for(f.label)) is not None:
        return _fit(f, saved, "saved", "")
    if f.kind == "textarea" or f.kind == "text" and ESSAY.search(f.label.strip()):  # a question with options never is
        return Step(f, "essay", reason="an essay: written by you, or drafted in Fast apply")
    return None


async def plan_form(form: Form, vault: Vault, router: Router) -> list[Step]:
    """APP-2: one step per field. The local model sees labels only, never vault values."""
    where = f"{form.title}\n{form.posting[:800]}"
    steps: dict[str, Step | None] = {f.name: classify(f, vault, where) for f in form.fields}
    left = [f for f in form.fields if steps[f.name] is None]
    keys: dict[str, str] = {}
    if left:
        listing = "\n".join(f"- name: {f.name}\n  question: {f.label}" + (f"\n  details: {f.description[:200]}" if f.description else "")
                            + (f"\n  options: {', '.join(f.options)}" if f.options else "") for f in left)
        try:
            got = await router.json("map_field", MAP_SYSTEM, listing, Mappings)
            keys = {m.name: m.key for m in got.mappings}
        except AIError as e:
            log.warning("map_field failed (%s); %d field(s) left to the owner", e, len(left))
    for f in left:
        key = keys.get(f.name, "none")
        if key not in KEYS:
            steps[f.name] = Step(f, "owner", reason="no saved answer")
        else:
            steps[f.name] = _fit(f, vault.value(key), "mapped", f"not in your vault ({key})")
    return [s for s in steps.values() if s is not None]


def ai_policy(form: Form) -> str | None:
    """FA-3: why essays must not be drafted for this form, or None."""
    for f in form.fields:
        if AI_FIELD.search(_text(f)):
            return f"the form asks about using AI (\"{f.label}\")"
    if m := AI_POSTING.search(form.posting):
        return f"the posting says \"{m.group(0).strip()}\""
    return None


def agree_option(f: Field) -> str | None:
    """The one option of a legal question that means "I agree", or None (then the owner ticks it)."""
    found = [o for o in f.options if AGREE.match(o.strip())]
    if len(found) != 1 or f.kind not in ("select", "multiselect"):
        return None
    return found[0] if f.kind == "select" or len(f.options) == 1 else None  # a lone checkbox: "Acknowledge/Confirm"
