"""Official public job lists: Greenhouse, Lever, Ashby (SPEC JOB-1..JOB-3)."""

from __future__ import annotations

import json

import pytest
import respx

from bandwidth.radar.models import Kind
from bandwidth.radar.sources import REGISTRY
from bandwidth.radar.sources.ats import title_wanted
from tests.radar.conftest import NOW, fixture_text, make_fetcher, run

GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
LV = "https://api.lever.co/v0/postings/{}"
AB = "https://api.ashbyhq.com/posting-api/job-board/{}"


def fetch(type_, params):
    src = REGISTRY[type_]("test", params, now=NOW)
    return src, run(src.fetch(make_fetcher()))


@pytest.mark.parametrize("title,wanted", [
    ("Software Engineering Intern", True),
    ("Internship – Data Science (Summer 2027)", True),
    ("Anthropic Fellows Program, AI Safety & Security", True),
    ("American Tech Fellowship", True),
    ("New Grad Software Engineer", True),
    ("Working Student AI", True),
    ("Internal Communications, Enterprise & Growth", False),  # seen live: "intern" inside "Internal"
    ("International Tax Manager", False),
    ("Senior Research Intern", False),                        # excluded word wins
    ("Activation Manager, Beneficial Deployments", False),
])
def test_titles_match_whole_words(title, wanted):
    """JOB-2"""
    assert title_wanted(title, None, None) is wanted


def test_custom_titles_replace_the_defaults():
    """JOB-2"""
    assert title_wanted("Research Engineer", ["research engineer"], [])
    assert not title_wanted("Software Engineering Intern", ["research engineer"], [])


@respx.mock
def test_greenhouse_board():
    """JOB-1, JOB-3"""
    route = respx.get(url__startswith=GH.format("anthropic")).respond(200, text=fixture_text("greenhouse_board.json"))
    src, items = fetch("greenhouse", {"companies": ["anthropic"]})
    assert "content=true" in str(route.calls[0].request.url)
    assert [i.title for i in items] == ["Anthropic Fellows Program, AI Safety & Security"]
    job = items[0]
    assert job.kind == Kind.JOB and job.organizer == "Anthropic"
    assert job.url.startswith("https://job-boards.greenhouse.io/anthropic/jobs/")
    assert "apply:greenhouse" in job.tags and job.location and job.published_at
    assert "<" not in job.summary and "&lt;" not in job.summary and "Anthropic" in job.summary


@respx.mock
def test_lever_postings():
    """JOB-1, JOB-3"""
    respx.get(url__startswith=LV.format("palantir")).respond(200, text=fixture_text("lever_postings.json"))
    src, items = fetch("lever", {"companies": ["palantir"]})
    assert [i.title for i in items] == ["American Tech Fellowship"]
    job = items[0]
    assert job.url.startswith("https://jobs.lever.co/palantir/") and "apply:lever" in job.tags
    assert job.organizer == "palantir" and job.published_at and job.location


@respx.mock
def test_ashby_board_with_custom_titles():
    """JOB-1, JOB-2, JOB-3"""
    respx.get(url__startswith=AB.format("openai")).respond(200, text=fixture_text("ashby_board.json"))
    src, items = fetch("ashby", {"companies": ["openai"], "titles": ["research engineer"]})
    assert [i.title for i in items] == ["Research Engineer"]
    assert items[0].url.startswith("https://jobs.ashbyhq.com/openai/") and "apply:ashby" in items[0].tags


@respx.mock
def test_ashby_unlisted_jobs_are_skipped():
    """JOB-3"""
    data = json.loads(fixture_text("ashby_board.json"))
    for j in data["jobs"]:
        j["isListed"] = False
    respx.get(url__startswith=AB.format("openai")).respond(200, json=data)
    _, items = fetch("ashby", {"companies": ["openai"], "titles": ["research engineer"]})
    assert items == []


@respx.mock
def test_one_unknown_board_is_a_warning_naming_it():
    """JOB-3: the other companies still count."""
    respx.get(url__startswith=GH.format("anthropic")).respond(200, text=fixture_text("greenhouse_board.json"))
    respx.get(url__startswith=GH.format("nosuchco")).respond(404, json={"status": 404, "error": "Job not found"})
    src, items = fetch("greenhouse", {"companies": ["anthropic", "nosuchco"]})
    assert items and any("nosuchco" in w for w in src.warnings)


@respx.mock
def test_every_board_failing_is_an_error():
    """JOB-3"""
    respx.get(url__startswith=LV.format("nosuchco")).respond(404, json={"ok": False, "error": "Document not found"})
    with pytest.raises(RuntimeError, match="nosuchco"):
        fetch("lever", {"companies": ["nosuchco"]})


def test_companies_are_required():
    """JOB-1"""
    with pytest.raises(ValueError):
        REGISTRY["greenhouse"]("test", {}, now=NOW)
