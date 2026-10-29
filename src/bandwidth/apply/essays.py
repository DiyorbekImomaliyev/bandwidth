"""Essays, cover letters and short answers for one posting (SPEC FA-1, FA-2).

Drafts are new for every posting, written from the vault by a cloud model that sees it anonymised,
or by the local model. Each sentence cites the numbered facts it uses, and code checks it against
only those lines. Code also finds the length limit, checks the style rules it can, and cuts at a
sentence end. The optional polish sends an anonymised draft to a cloud model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from ..ai import AIError, Router
from .vault import Vault

log = logging.getLogger(__name__)

DEFAULT_LIMIT_WORDS = 150
COVER_LETTER_WORDS = 250   # measured: letters drafted at 150 came back at 32-75 words, too thin to read
MIN_SHARE = 0.6            # a cover letter shorter than this share of its limit is sent back once
LONG_SENTENCE = 25         # words; "simple, clear English" in the owner's style guide

ESSAY_SYSTEM = """You write one application text for the owner, in the first person, as the owner.

Rules:
- Use only the numbered facts (F1, F2…) and the posting. Never invent a number, a project, a skill,
  a date or a feeling about the company. If a fact is missing, write around it.
- Never mix facts: a number or result belongs to the project in its own fact line.
- Follow the owner's style guide below exactly.
- Answer the question that was asked, for this company and this role. Say one specific thing about
  them, taken from the posting.
- Make one argument, not a list. Pick the two or three facts that matter most for this role and
  say why they matter here. Leave the rest out: the CV has them.
- Build each sentence as verb + object + detail: the action, what it acted on, then where, how
  or with what result ("I built a bilingual search tool for a ministry." "Cut manual checks by 85%.").
  Put no long clause before the verb. Most sentences lead with the verb, as the owner asks; open
  every paragraph with a full "I" + verb sentence, so the reader always knows who acted.
- Simple, clear English: common words, one idea per sentence, most sentences under 15 words.
- Stay within the length limit. No greeting or sign-off: they are added for you.

Answer as JSON: {"paragraphs": [[{"text": "One sentence.", "sources": ["F3"]}, ...], ...]}.
Each sentence lists every fact id it relies on, and "P" when it uses the posting. A sentence that
states no fact (a wish, a reason, a joke) has "sources": []. Keep [[P1]]-style placeholders as they
are.

# The owner's style guide
"""

POLISH_SYSTEM = """You improve the wording of an application text without changing what it says.

Rules:
- Keep every placeholder like [[P1]] exactly as written, the same number of times.
- Keep every fact and number. Add none.
- Shorter sentences in verb + object + detail order, in simple English. Keep the mix: some lead
  with the verb, most start with "I" + verb. Never turn them into CV bullet fragments. No "-ing"
  openers or tails.
  No "passionate", "leverage", "synergy", "dynamic", "spearheaded", "delve".
- Keep at most one light, dry joke if the text has one; do not add new ones.
- Output only the improved text, nothing else.
"""


@dataclass(frozen=True)
class Limit:
    n: int
    unit: str  # "words" or "characters"

    def size(self, text: str) -> int:
        return len(text.split()) if self.unit == "words" else len(text)


LIMIT = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(words?|characters?|chars?)\b", re.I)


def limit_of(label: str, description: str = "") -> Limit:
    """FA-1: a limit stated in the question ("150 words max", "1000 characters"), else 150 words
    (250 for a cover letter)."""
    if m := LIMIT.search(f"{label}\n{description}"):
        unit = "words" if m.group(2).lower().startswith("word") else "characters"
        return Limit(int(m.group(1).replace(",", "")), unit)
    return Limit(COVER_LETTER_WORDS if is_cover_letter(label) else DEFAULT_LIMIT_WORDS, "words")


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def fit(text: str, limit: Limit) -> str:
    """Cut at the last sentence end within the limit (FA-1)."""
    text = text.strip()
    if limit.size(text) <= limit.n:
        return text
    kept = ""
    for sentence in SENTENCE_END.split(text):
        candidate = f"{kept} {sentence}".strip()
        if limit.size(candidate) > limit.n:
            break
        kept = candidate
    if kept:
        return kept
    words = text.split()  # one sentence longer than the limit: cut by words
    return " ".join(words[: limit.n]) if limit.unit == "words" else text[: limit.n].rsplit(" ", 1)[0]


# --------------------------------------------------------------- style rules code can check

NOT_VERBS = {"during", "nothing", "something", "anything", "everything", "thing", "things", "string", "strings",
             "morning", "evening", "spring", "king", "ceiling", "bring", "sing", "ring", "wing",
             # adjectives: "data-dense, unforgiving work" is not an -ing phrase
             "unforgiving", "interesting", "exciting", "challenging", "demanding", "outstanding", "leading",
             "growing", "missing", "existing", "emerging", "ongoing", "upcoming", "promising", "compelling",
             "engaging", "amazing", "boring", "willing"}
ING_OPENER = re.compile(r"(?:^|(?<=[.!?])\s+)([A-Z][a-z]+ing)\b[^.!?]*?,")
ING_TAIL = re.compile(r",\s+([a-z]{3,}ing)\b(?!-)")  # not "cutting-edge"
BANNED = ["passionate", "leverage", "leveraged", "leverages", "synergy", "dynamic", "spearheaded", "spearhead",
          "delve", "delved", "i am excited to", "i'm excited to"]


FRAGMENT_VERBS = {"cut", "built", "won", "wrote", "led", "ran", "made", "hold", "held", "shipped", "taught", "set",
                  "grew", "drove", "kept", "found", "gave", "beat", "got", "did", "brought", "took", "sold", "began"}
FRAGMENT_SHARE = 0.75  # the owner wants mostly verb-led sentences; only near-all-fragment texts go back


def fragments(text: str) -> tuple[int, int]:
    """(sentences with no subject, all sentences): "Reduced latency…", "Won 2nd place…"."""
    sentences = [s for s in SENTENCE_END.split(text) if s.strip()]
    def bare(s: str) -> bool:
        first = re.sub(r"[^a-z]", "", s.split()[0].lower())
        return first in FRAGMENT_VERBS or (first.endswith("ed") and first not in {"need", "indeed", "speed", "seed"}) \
            or (first.endswith("es") and len(first) > 4 and first not in {"times", "sometimes", "notes", "rules"})
    return sum(bare(s) for s in sentences), len(sentences)


def cut_ing_tails(sentence: str) -> str:
    """Code's last word on the -ing rule: ", proving I can…" goes, the sentence's point stays."""
    def cut(m: re.Match[str]) -> str:
        return m.group(0) if m.group(1).lower() in NOT_VERBS else ""
    out = re.sub(r",\s+([a-z]{3,}ing)\b(?!-)[^.!?]*", cut, sentence)
    return out if out.strip() else sentence


def fix_tense(sentence: str, graduation: str | None, today: date | None = None) -> str:
    """"I graduated … in 2028" said before 2028: the date stays, the verb turns to the future."""
    today = today or date.today()
    if not graduation or graduation[:7] < today.isoformat()[:7]:
        return sentence
    sentence = re.sub(r"\b(I|and) graduated\b", r"\1 will graduate", sentence)
    return re.sub(r"\b(I|and) (?:earned|completed|received|obtained|hold)\b(?= (?:a|an|my) (?:B\.?Sc?|B\.?A|M\.?Sc|bachelor|master|degree))",
                  r"\1 will earn", sentence, flags=re.I)


def _banned_phrase(sentence: str) -> bool:
    lower = sentence.replace("\u2019", "'").lower()
    return any(" " in w and w in lower for w in BANNED)


def style_problems(text: str) -> list[str]:
    text = text.replace("\u2019", "'")  # "I’m excited to" is still banned
    problems = []
    for pattern in (ING_OPENER, ING_TAIL):
        for m in pattern.finditer(text):
            if m.group(1).lower() not in NOT_VERBS:
                problems.append(f"an -ing phrase: \"{m.group(0).strip()}\"")
    for sentence in SENTENCE_END.split(text):
        if (n := len(sentence.split())) > LONG_SENTENCE:
            problems.append(f"a {n}-word sentence; split it (simple English: under {LONG_SENTENCE} words)")
    lower = text.lower()
    problems += [f"the word \"{w}\"" for w in BANNED if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", lower)]
    return problems


NUMBER = re.compile(r"\d[\d,.]*\d|\d")
DURATION = re.compile(r"\b(?:for|spent|over|many|several|\d+\+?)\s+(?:\w+\s+)?years\b|\byears of\b", re.I)
# Claims about the owner's availability: seen live, "I can work full-time in London" citing only the
# posting. Each needs its key word in the facts the sentence cites.
AVAILABILITY = re.compile(r"\b(?:full[- ]time|part[- ]time|relocat\w*|on[- ]site|in[- ]person|remote(?:ly)?"
                          r"|(?:can|could|am able to|am available to|am ready to|am willing to|will)\s+(?:work|start|move|join|fit|commit|attend)"
                          r"(?:\s+(?:in|from|at|on)\s+\w+)?)\b", re.I)


def unsupported(text: str, facts: str, posting: str) -> list[str]:
    """Principle 2: numbers must come from the facts or the posting, time claims from the facts."""
    source = (facts + "\n" + posting).replace(",", "")
    found = []
    for n in dict.fromkeys(m.group(0).replace(",", "").rstrip(".") for m in NUMBER.finditer(text)):
        if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", source):
            found.append(f"a number that is not in my facts or the posting: {n}")
    found += [f"a claim that is not in my facts: \"{m.group(0)}\"" for m in DURATION.finditer(text)
              if m.group(0).lower() not in facts.lower()]
    if re.search(r"\b(?:I|I'm|I am|my|me)\b", text.replace("\u2019", "'")):
        found += [f"a claim about my availability that is not in my facts: \"{m.group(0)}\""
                  for m in AVAILABILITY.finditer(text) if not _avail_backed(m.group(0), facts)]
    return found


def _avail_backed(claim: str, facts: str) -> bool:
    if re.match(r"(?:can|could|am|will)\b", claim, re.I):  # "can work", "will start": any stated availability
        return bool(re.search(r"availab|full[- ]?time|part[- ]?time|relocat|start", facts, re.I))
    key = re.sub(r"[- ]", "", claim.lower())
    flat = re.sub(r"[- ]", "", facts.lower())
    return key in flat or (key.startswith("relocat") and "relocat" in flat)


PLAIN = str.maketrans({"\u2010": "-", "\u2011": "-", "\u202f": " ", "\u00a0": " "})
UNIT_GAP = re.compile(r"(\d)\u202f(?=[KMBT]\b)")  # "$275\u202fB" → "$275B"


def _clean(text: str) -> str:
    text = UNIT_GAP.sub(r"\1", text).translate(PLAIN).strip().strip('"').strip()
    return re.sub(r"</?draft>", "", text).strip()


GREETING = re.compile(r"^(?:dear|hello|hi|to)\b[^\n]*\n+", re.I)
SIGN_OFF = re.compile(r"\n+(?:best|best regards|kind regards|regards|sincerely|thank you|thanks)[^\n]*,?\s*(?:\n[^\n]*)?$", re.I)


# Tools and skills a posting often asks for. A draft may name one only if the facts do (FA-1).
SKILLS = ["PyTorch", "TensorFlow", "Keras", "JAX", "SQL", "Spark", "PySpark", "Hadoop", "Kubernetes", "Terraform",
          "Airflow", "dbt", "Snowflake", "BigQuery", "Redshift", "Databricks", "Kafka", "Java", "C++", "C#",
          "Golang", "Rust", "Scala", "TypeScript", "Node.js", "Django", "FastAPI", "Flask", "React", "Vue", "Angular",
          "Tableau", "Power BI", "Excel", "MATLAB", "Hugging Face", "LangChain", "LlamaIndex", "OpenCV",
          "XGBoost", "LightGBM", "GCP", "Azure", "AWS", "Docker", "MongoDB", "Redis", "GraphQL", "Swift", "Kotlin",
          "Ruby", "Rails", "PHP", "Linux", "CUDA", "Pandas", "NumPy", "scikit-learn", "Statistics", "A/B testing"]


def unsupported_skills(text: str, facts: str, *, company: str = "") -> list[str]:
    """Skills the draft names that the facts don't: "SQL" is backed by "PostgreSQL", the company's
    own name ("Databricks") is not a claim."""
    lower_facts, lower_company = facts.lower(), company.lower()
    return [skill for skill in SKILLS
            if re.search(rf"(?<![\w+#]){re.escape(skill)}(?![\w+#])", text, re.I if len(skill) > 2 else 0)
            and skill.lower() not in lower_facts and skill.lower() not in lower_company]


# --------------------------------------------------------------- cited drafts (FA-1)


class Sentence(BaseModel):
    text: str
    sources: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    paragraphs: list[list[Sentence]]


def numbered_facts(about_me: str) -> dict[str, str]:
    """One fact per line of about-me.md, F1, F2…, each carrying its section headings."""
    facts: dict[str, str] = {}
    headings: list[str] = []
    for raw in about_me.splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := re.match(r"(#+)\s*(.*)", line):
            headings = [*headings[: len(m.group(1)) - 1], m.group(2).strip()]
            continue
        context = " / ".join(h for h in headings if h)
        text = line.lstrip("-*• ").strip()
        facts[f"F{len(facts) + 1}"] = f"{context}: {text}" if context else text
    return facts


TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#-]*[A-Za-z0-9+#]|[A-Za-z]{3,}")
STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "over", "your", "you", "our", "their", "them",
        "they", "are", "was", "were", "has", "have", "had", "not", "but", "can", "will", "would", "who", "what",
        "why", "how", "all", "any", "each", "its", "it's", "one", "two", "out", "about", "than", "then", "also",
        "more", "most", "just", "like", "want", "built", "build", "work", "made", "make", "used", "using"}


def _has(word: str, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.I))


def _is_name(word: str) -> bool:
    """RAG, UPDIVE, OrbiDefence, n8n: capitals after the first letter, or a digit. Ordinary words
    ("model") and sentence-initial capitals ("Built") are not names."""
    return any(c.isupper() for c in word[1:]) or any(c.isdigit() for c in word)


def distinctive(facts: dict[str, str]) -> set[str]:
    """Names in only one or two fact lines: a project, a company, a tool tied to one result."""
    words = {w.lower() for line in facts.values() for w in TERM.findall(line) if _is_name(w)} - STOP
    return {w for w in words if 1 <= sum(_has(w, line) for line in facts.values()) <= 2}


def sentence_problems(s: Sentence, facts: dict[str, str], special: set[str], posting: str, *,
                      company: str, role: str) -> tuple[list[str], list[str]]:
    """(claim problems, style problems) of one sentence, checked against only what it cites."""
    claims = [f"cites {i}, which does not exist" for i in s.sources if i != "P" and i not in facts]
    cited = "\n".join(facts[i] for i in s.sources if i in facts)
    cited_posting = posting if "P" in s.sources else ""
    claims += unsupported(s.text, cited, cited_posting)
    claims += [f"names {k}, which is not in the facts it cites" for k in unsupported_skills(s.text, cited, company=company)]
    for word in sorted({w.lower() for w in TERM.findall(s.text)} & special):
        if not (_has(word, cited) or _has(word, cited_posting) or _has(word, f"{company} {role}")):
            claims.append(f"mentions \"{word}\" but cites no fact that contains it")
    return claims, style_problems(s.text)


def render(d: Draft) -> str:
    return "\n\n".join(" ".join(_clean(s.text) for s in para) for para in d.paragraphs if para).strip()


def is_cover_letter(question: str) -> bool:
    return bool(re.search(r"cover\s*letter", question, re.I))


def mask(text: str, back: dict[str, str]) -> str:
    """Put placeholders back in place of details already mapped by `anonymise`."""
    for placeholder, original in sorted(back.items(), key=lambda kv: len(kv[1]), reverse=True):
        text = re.sub(re.escape(original), placeholder, text, flags=re.I)
    return text


async def draft(router: Router, vault: Vault, question: str, description: str, posting: str, *,
                company: str, role: str, cloud: bool = False, terms: list[str] | None = None) -> str:
    """FA-1: a new text for this posting, from the vault only, every sentence citing its facts.
    `cloud`: anonymised on the public task `draft`, details put back here; the local model drafts if
    the cloud can't."""
    limit = limit_of(question, description)
    facts = numbered_facts(vault.about_me)
    special = distinctive(facts)
    system = ESSAY_SYSTEM + (vault.style or "Short, plain sentences.")
    samples = "\n\n---\n\n".join(vault.samples[:3])
    listed = "\n".join(f"{i}: {text}" for i, text in facts.items()) or "(none given)"
    user = (f"Company: {company}\nRole: {role}\nQuestion: {question}\n"
            + (f"Details: {description}\n" if description else "")
            + f"Length: at most {limit.n} {limit.unit}.\n\n# Facts about me\n{listed}\n"
            + (f"\n# My writing samples (copy the tone, not the content)\n{samples}\n" if samples else ""))
    back: dict[str, str] = {}
    if cloud:
        user, back = anonymise(user, vault, [*(terms or []), *router.private_terms])

    async def ask(prompt: str) -> Draft:
        nonlocal cloud
        if cloud:
            try:
                d = await router.json("draft", system, prompt, Draft, data=posting)
                return Draft(paragraphs=[[Sentence(text=restore(s.text, back), sources=s.sources) for s in para]
                                         for para in d.paragraphs])
            except AIError as e:
                log.warning("cloud draft failed (%s); drafting locally", e)
                cloud = False
        return await router.json("essay", system, restore(prompt, back), Draft, data=posting)

    def check(d: Draft) -> list[tuple[int, list[str], list[str]]]:
        sentences = [s for para in d.paragraphs for s in para]
        out = []
        for n, s in enumerate(sentences, 1):
            claims, style = sentence_problems(s, facts, special, posting, company=company, role=role)
            out.append((n, claims, style))
        return out

    def too_short(d: Draft) -> list[str]:
        if not is_cover_letter(question) or (size := limit.size(render(d))) >= limit.n * MIN_SHARE:
            return []
        return [f"- the whole text: {size} {limit.unit}; write at least {int(limit.n * MIN_SHARE)}, "
                f"with one more paragraph built on a fact you haven't used"]

    def cv_like(d: Draft) -> list[str]:
        out = [f"- paragraph {n}: it opens without a subject; open it with \"I\" + verb"
               for n, para in enumerate(d.paragraphs, 1) if para and fragments(_clean(para[0].text))[0]]
        bare, total = fragments(render(d))
        if total >= 3 and bare > total * FRAGMENT_SHARE:
            out.append(f"- the whole text: {bare} of {total} sentences have no subject, so it reads like a CV. "
                       "Give a few more of them \"I\" as the subject")
        return out

    def whole(d: Draft) -> list[str]:
        return too_short(d) + cv_like(d)

    d = await ask(user)
    found = check(d)
    if any(claims or style for _, claims, style in found) or whole(d):
        lines = [f"- sentence {n}: {p}" for n, claims, style in found for p in [*claims, *style]] + whole(d)
        log.info("draft has %d problem(s); asking for one rewrite", len(lines))
        previous = mask(d.model_dump_json(), back)
        d = await ask(f"{user}\n# Your previous answer\n{previous}\n\n# Problems\n{mask(chr(10).join(lines), back)}\n\n"
                      "Answer again with every problem fixed.")
        found = check(d)
    # Principle 2: a sentence whose claims are still unsupported is dropped, not sent.
    drop = {n for n, claims, _ in found if claims}
    if drop:
        log.warning("dropped %d sentence(s) with unsupported claims: %s", len(drop),
                    [c for n, claims, _ in found if n in drop for c in claims])
    # A banned phrase that survived the rewrite ("I am excited to join…") goes too: it is filler.
    numbered = [s.text for para in d.paragraphs for s in para]
    filler = {n for n, t in enumerate(numbered, 1) if _banned_phrase(t)} - drop
    if filler and len(numbered) - len(drop) - len(filler) >= 2:
        drop |= filler
    counter = iter(range(1, 10_000))
    grad = vault.profile.education.graduation
    d = Draft(paragraphs=[[Sentence(text=fix_tense(cut_ing_tails(s.text), grad), sources=s.sources) for s in para
                           if next(counter) not in drop] for para in d.paragraphs])
    text = render(d)
    if not is_cover_letter(question):
        return fit(text, limit)
    # Greeting and sign-off come from code, so the model can't get the name or company wrong.
    body = SIGN_OFF.sub("", GREETING.sub("", text)).strip()
    name = vault.profile.preferred_name or vault.profile.first_name
    return f"Dear {company} team,\n\n{fit(body, limit)}\n\nBest,\n{name}"


# --------------------------------------------------------------- polish (FA-2)


def anonymise(text: str, vault: Vault, terms: list[str]) -> tuple[str, dict[str, str]]:
    """Replace the owner's details with [[P1]], [[P2]]…; returns the text and how to put them back."""
    keys = ["full_name", "first_name", "last_name", "preferred_name", "email", "phone", "education.school",
            "links.linkedin", "links.github", "links.website"]
    secrets = {s.strip() for s in [*(vault.value(k) for k in keys), *terms] if s and len(s.strip()) >= 3}
    if not secrets:
        return text, {}
    # Longest first, whole words, and the rest of a word that starts with one ("Uzbek" → "Uzbekistan").
    pattern = re.compile("|".join(rf"(?<!\w){re.escape(s)}\w*" for s in sorted(secrets, key=len, reverse=True)), re.I)
    back: dict[str, str] = {}
    seen: dict[str, str] = {}

    def swap(m: re.Match[str]) -> str:
        found = m.group(0)
        if found not in seen:
            seen[found] = f"[[P{len(seen) + 1}]]"
            back[seen[found]] = found
        return seen[found]

    return pattern.sub(swap, text), back


def restore(text: str, back: dict[str, str]) -> str:
    for placeholder, original in back.items():
        text = text.replace(placeholder, original)
    return text


async def polish(router: Router, vault: Vault, text: str, terms: list[str]) -> str:
    """FA-2: better wording from a cloud model that never sees who the owner is. Any doubt → the draft."""
    masked, back = anonymise(text, vault, [*terms, *router.private_terms])
    system = POLISH_SYSTEM + (f"\n# Style guide\n{vault.style}" if vault.style else "")
    try:
        out = _clean(await router.text("polish", system, f"<draft>\n{masked}\n</draft>"))
    except AIError as e:  # includes PrivacyError: something private was left in the prompt
        log.warning("polish skipped: %s", e)
        return text
    if any(out.count(p) < masked.count(p) for p in back) or not out:
        log.warning("polish dropped a placeholder; keeping the draft")
        return text
    better = restore(out, back)
    if len(style_problems(better)) > len(style_problems(text)) or unsupported(better, text, ""):
        log.warning("polish broke a style rule or added a claim; keeping the draft")
        return text
    return better
