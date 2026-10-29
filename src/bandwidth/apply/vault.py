"""The vault: what application forms ask for, kept on this Mac (SPEC §9).

Personal data: only code and private tasks read it (VLT-2). No passwords, ever (VLT-1).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..paths import ROOT

VAULT_DIR = Path(os.environ.get("BW_VAULT_DIR") or ROOT / "vault")
CV_MAX_BYTES = 5_000_000
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ANSWERS_HEADER = ("# Saved answers to application questions, by the question's exact text (SPEC VLT-4).\n"
                  "# The bot adds to this file when you reply /answer <n> <text> to a checklist.\n")


class VaultError(Exception):
    pass


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")
    school: str | None = None
    degree: str | None = None
    major: str | None = None
    start: str | None = None       # YYYY-MM, when the degree began
    graduation: str | None = None  # YYYY-MM
    gpa: str | None = None

    @field_validator("start", "graduation", "gpa", mode="before")
    @classmethod
    def _text(cls, v: object) -> object:
        return str(v) if v is not None else v  # YAML reads 2027-06 as text but 3.8 as a number


class VaultProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a stray "password:" is refused, not stored
    first_name: str
    last_name: str
    preferred_name: str | None = None
    email: str
    phone: str
    phone_country: str | None = None
    country: str
    city: str | None = None
    citizenship: str | None = None          # for nationality and right-to-work questions
    needs_sponsorship: bool | None = None   # "Will you require visa sponsorship?" answered from this
    links: Links = Links()
    education: Education = Education()

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not EMAIL.match(v.strip()):
            raise ValueError("email is not an email address")
        return v.strip()

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v: object) -> object:
        return str(v) if v is not None else v


# What map_field may point a form field to (APP-2). Code reads the value; the model only picks a key.
KEYS = ["first_name", "last_name", "full_name", "preferred_name", "email", "phone", "phone_country", "country",
        "city", "location", "citizenship", "links.linkedin", "links.github", "links.website", "education.school",
        "education.degree", "education.major", "education.graduation", "education.gpa",
        "education.start_month", "education.start_year", "education.end_month", "education.end_year"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


class SavedAnswer(BaseModel):
    question: str
    answer: str


def _norm(question: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", question.lower()).split())


def same_question(a: str, b: str) -> bool:
    """VLT-4: same words, ignoring case, punctuation and spacing."""
    return _norm(a) == _norm(b)


@dataclass
class Vault:
    dir: Path
    profile: VaultProfile
    answers: list[SavedAnswer] = field(default_factory=list)
    about_me: str = ""
    samples: list[str] = field(default_factory=list)
    style: str = ""  # how the owner writes (style.md), for every draft (FA-1)

    @classmethod
    def load(cls, folder: Path = VAULT_DIR) -> Vault:
        path = folder / "profile.yaml"
        if not path.exists():
            raise VaultError(f"{path} not found; copy vault.example/ to {folder} and fill it in")
        try:
            profile = VaultProfile.model_validate(yaml.safe_load(path.read_text()) or {})
        except yaml.YAMLError as e:
            raise VaultError(f"{path}: not valid YAML: {e}") from None
        except ValidationError as e:
            raise VaultError(f"{path}: " + "; ".join(_plain(err) for err in e.errors())) from None
        answers_path = folder / "answers.yaml"
        raw = yaml.safe_load(answers_path.read_text()) if answers_path.exists() else None
        answers = [SavedAnswer.model_validate(a) for a in raw or []]
        about, style = folder / "about-me.md", folder / "style.md"
        samples = sorted((folder / "samples").glob("*.*")) if (folder / "samples").is_dir() else []
        return cls(folder, profile, answers, _text(about) if about.exists() else "",
                   [_text(s) for s in samples
                    if s.suffix in (".md", ".txt") and not s.name.upper().startswith("README")],
                   _text(style) if style.exists() else "")

    @property
    def cv(self) -> Path | None:
        path = self.dir / "cv.pdf"
        return path if path.exists() else None

    def value(self, key: str) -> str | None:
        p = self.profile
        if key == "full_name":
            return f"{p.first_name} {p.last_name}"
        if key == "links.any":
            return p.links.website or p.links.github or p.links.linkedin
        if key == "preferred_name":
            return p.preferred_name or p.first_name
        if key == "location":
            return ", ".join(x for x in (p.city, p.country) if x)
        if m := re.match(r"education\.(start|end)_(month|year)$", key):
            when = p.education.start if m.group(1) == "start" else p.education.graduation
            ym = re.match(r"^(\d{4})-(\d{1,2})", when or "")
            if not ym:
                return None
            return ym.group(1) if m.group(2) == "year" else MONTHS[int(ym.group(2)) - 1]
        obj: object = p
        for part in key.split("."):
            obj = getattr(obj, part, None)
        return str(obj) if obj not in (None, "") else None

    def answer_for(self, question: str) -> str | None:
        return next((a.answer for a in self.answers if same_question(a.question, question)), None)

    def save_answer(self, question: str, answer: str) -> None:
        self.answers = [a for a in self.answers if not same_question(a.question, question)]
        self.answers.append(SavedAnswer(question=question.strip(), answer=answer.strip()))
        path = self.dir / "answers.yaml"
        body = yaml.safe_dump([a.model_dump() for a in self.answers], allow_unicode=True, sort_keys=False)
        path.write_text(ANSWERS_HEADER + body)
        path.chmod(0o600)


COMMENT = re.compile(r"<!--.*?-->", re.S)


def _text(path: Path) -> str:
    """A vault text file without <!-- guidance comments -->."""
    return COMMENT.sub("", path.read_text()).strip()


def _plain(err: dict) -> str:
    where = ".".join(str(x) for x in err["loc"])
    if err["type"] in ("missing", "string_type") and err.get("input") is None:
        return f"{where} is empty"
    if err["type"] == "extra_forbidden":
        return f"{where} is not a vault field (no passwords or other secrets here)"
    return f"{where}: {err['msg'].removeprefix('Value error, ')}"


def check(folder: Path = VAULT_DIR) -> list[str]:
    """VLT-3: everything that would stop or weaken an application, in plain words."""
    problems: list[str] = []
    try:
        Vault.load(folder)
    except VaultError as e:
        problems.append(str(e))
    cv = folder / "cv.pdf"
    if not cv.exists():
        problems.append(f"{cv} is missing (your CV, as a PDF)")
    else:
        if not cv.read_bytes()[:5].startswith(b"%PDF"):
            problems.append(f"{cv} is not a PDF")
        if cv.stat().st_size > CV_MAX_BYTES:
            problems.append(f"{cv} is over 5 MB; forms refuse it")
    if folder.exists() and folder.stat().st_mode & 0o077:
        problems.append(f"{folder} is readable by others; run: chmod 700 {folder}")
    return problems
