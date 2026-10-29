"""Official public job lists of applicant-tracking systems: Greenhouse, Lever, Ashby (SPEC JOB-1..3).

No login or key. Each company's whole board comes back in one request, so titles are filtered in
code before anything is ranked (a board can list 700 jobs).
"""

from __future__ import annotations

import re
from abc import abstractmethod
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from ..http import Fetcher
from ..models import Item, Kind
from ..textutil import html_to_text, parse_iso
from .base import Source, SourceParams, register, summarize_errors

TITLES = ["intern", "internship", "graduate", "new grad", "junior", "entry level", "entry-level", "student",
          "trainee", "apprentice", "working student", "research assistant", "fellow", "fellowship", "residency"]
EXCLUDE_TITLES = ["senior", "sr", "staff", "principal", "lead", "manager", "director", "head of", "vp"]


def _words(terms: list[str]) -> re.Pattern[str]:
    # Whole words, with plural/-ship endings: "intern" → interns, internship; never "Internal".
    alts = "|".join(re.escape(t.strip()) for t in terms if t.strip())
    return re.compile(rf"\b(?:{alts})(?:s|ships?)?\b", re.I) if alts else re.compile(r"(?!)")


def title_wanted(title: str, titles: list[str] | None, exclude: list[str] | None) -> bool:
    """JOB-2: a title is kept when it has a wanted word and no excluded one."""
    wanted = _words(TITLES if titles is None else titles)
    unwanted = _words(EXCLUDE_TITLES if exclude is None else exclude)
    return bool(wanted.search(title)) and not unwanted.search(title)


class AtsParams(SourceParams):
    companies: list[str] = Field(min_length=1)  # board names as in the job-board URL, e.g. "anthropic"
    titles: list[str] | None = None              # None = TITLES
    exclude_titles: list[str] | None = None      # None = EXCLUDE_TITLES


class AtsSource(Source):
    Params = AtsParams
    p: AtsParams
    platform: str

    async def fetch(self, fx: Fetcher) -> list[Item]:
        items: list[Item | None] = []
        errors: list[tuple[str, str]] = []
        for company in self.p.companies:
            try:
                jobs = await self.jobs(fx, company)
            except Exception as e:
                errors.append((company, str(e)))
                continue
            for job in jobs:
                if isinstance(job, dict) and title_wanted(self.title(job), self.p.titles, self.p.exclude_titles):
                    items.append(self.item(company, job))
        if errors and len(errors) == len(self.p.companies):
            raise RuntimeError(summarize_errors(errors, "board"))
        if errors:
            self.warnings.append(summarize_errors(errors, "board"))
        return self.collect(items)

    def job(self, **fields: Any) -> Item | None:
        return self.make(kind=Kind.JOB, tags=[f"apply:{self.platform}"], **fields)

    @abstractmethod
    async def jobs(self, fx: Fetcher, company: str) -> list[Any]: ...

    @abstractmethod
    def title(self, job: dict) -> str: ...

    @abstractmethod
    def item(self, company: str, job: dict) -> Item | None: ...


@register
class GreenhouseSource(AtsSource):
    """boards-api.greenhouse.io: descriptions arrive as escaped HTML."""

    type = "greenhouse"
    platform = "greenhouse"

    async def jobs(self, fx: Fetcher, company: str) -> list[Any]:
        data = await fx.get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs", params={"content": "true"})
        return data.get("jobs") or [] if isinstance(data, dict) else []

    def title(self, job: dict) -> str:
        return job.get("title") or ""

    def item(self, company: str, job: dict) -> Item | None:
        org = job.get("company_name") or company
        location = (job.get("location") or {}).get("name")
        text = html_to_text(html_to_text(job.get("content")))  # once to unescape, once to strip tags
        return self.job(
            title=self.title(job), url=job.get("absolute_url") or "", organizer=org, location=location,
            summary=" · ".join(b for b in (org, location, text) if b),
            published_at=parse_iso(job.get("first_published") or job.get("updated_at")),
        )


@register
class LeverSource(AtsSource):
    """api.lever.co: a plain list; the company's display name is not in it, so the board name is used."""

    type = "lever"
    platform = "lever"

    async def jobs(self, fx: Fetcher, company: str) -> list[Any]:
        data = await fx.get_json(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
        if not isinstance(data, list):  # {"ok": false, "error": "Document not found"} comes with HTTP 200 at times
            raise RuntimeError(f"no Lever board named {company!r}")
        return data

    def title(self, job: dict) -> str:
        return job.get("text") or ""

    def item(self, company: str, job: dict) -> Item | None:
        cats = job.get("categories") or {}
        created = job.get("createdAt")
        location = cats.get("location")
        return self.job(
            title=self.title(job), url=job.get("hostedUrl") or "", organizer=company, location=location,
            summary=" · ".join(b for b in (company, location, cats.get("commitment"), job.get("descriptionPlain")) if b),
            published_at=datetime.fromtimestamp(created / 1000, UTC) if isinstance(created, (int, float)) else None,
            is_online=(job.get("workplaceType") == "remote") or None,
        )


@register
class AshbySource(AtsSource):
    """api.ashbyhq.com posting API: only listed jobs are public."""

    type = "ashby"
    platform = "ashby"

    async def jobs(self, fx: Fetcher, company: str) -> list[Any]:
        data = await fx.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{company}")
        return [j for j in (data.get("jobs") or []) if j.get("isListed", True)] if isinstance(data, dict) else []

    def title(self, job: dict) -> str:
        return job.get("title") or ""

    def item(self, company: str, job: dict) -> Item | None:
        location = job.get("location")
        return self.job(
            title=self.title(job), url=job.get("jobUrl") or "", organizer=company, location=location,
            summary=" · ".join(b for b in (company, location, job.get("descriptionPlain")) if b),
            published_at=parse_iso(job.get("publishedAt")), is_online=job.get("isRemote") or None,
        )
