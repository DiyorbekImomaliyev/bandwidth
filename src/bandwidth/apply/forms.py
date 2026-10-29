"""Reading an application form: which fields it has, of what kind, with which options (SPEC APP-1).

Greenhouse publishes every job's form in its public API. Its job page also shows fields the API
leaves out (a country picker next to the phone, the demographic section), which are added here so
the filler knows them. Lever and Ashby forms are read from the page (not built yet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator

from bandwidth.radar.http import Fetcher
from bandwidth.radar.textutil import html_to_text

Kind = Literal["text", "textarea", "select", "multiselect", "checkbox", "file"]
READERS = ("greenhouse",)  # platforms whose forms are read from an API
PAGE_READERS = ("lever", "ashby", "page")  # read from the page itself (APP-9); "page" is any other site
JS_DIR = Path(__file__).parent / "js"


class Field(BaseModel):
    name: str            # the input's id on the page
    label: str
    kind: Kind
    required: bool = False
    options: list[str] = []   # for selects; [] = a search box where the value is typed
    description: str = ""
    section: str = "questions"  # questions | eeoc | page | education
    fallback: str = ""        # the option to take when the value isn't offered ("Other" for a school)
    widget: str = ""          # how the page draws it (APP-9): native, radio, checkbox, yesno, combobox, editable…
    members: list[str] = []   # a radio or checkbox group: each box's data-bw tag

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a field needs a name")
        return v


@dataclass
class Form:
    platform: str
    company: str
    title: str
    url: str          # the page with the form
    posting: str      # the job description as text
    fields: list[Field] = field(default_factory=list)


@dataclass(frozen=True)
class PostingRef:
    platform: str
    board: str
    job_id: str


REFS = [
    ("greenhouse", re.compile(r"^(?:job-boards|boards)(?:\.[a-z]{2})?\.greenhouse\.io$"), re.compile(r"^/([^/]+)/jobs/(\d+)")),
    ("lever", re.compile(r"^jobs\.(?:eu\.)?lever\.co$"), re.compile(r"^/([^/]+)/([0-9a-f-]{6,})")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$"), re.compile(r"^/([^/]+)/([0-9a-f-]{6,})")),
]


def posting_ref(url: str) -> PostingRef | None:
    """The platform, board and job of a posting URL, or None for anything else (LinkedIn included)."""
    parts = urlsplit(url.strip())
    for platform, host, path in REFS:
        if host.match(parts.netloc.lower()) and (m := path.match(parts.path)):
            return PostingRef(platform, m.group(1), m.group(2))
    return None


GH_KINDS: dict[str, Kind] = {"input_text": "text", "textarea": "textarea", "input_file": "file",
                             "multi_value_single_select": "select", "multi_value_multi_select": "multiselect",
                             "input_hidden": "text"}
# On every job-boards.greenhouse.io form, but not in the API: the phone number's country picker
# (labelled "Country", it shows "+44" once chosen; seen live 28 Oct 2026).
GH_PAGE_FIELDS = [Field(name="country", label="Phone country", kind="select", section="page")]
# The API's "location" is a place search on the page (id candidate-location; seen live 28 Oct 2026).
GH_PAGE_IDS = {"location": ("candidate-location", "select")}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def gh_education(required: bool) -> list[Field]:
    """The page's education block, when the job asks for it (API "education": "education_required";
    ids seen live 29 Oct 2026). School and discipline search Greenhouse's own lists: "Other" when
    the owner's school or major isn't there."""
    def f(name: str, label: str, kind: Kind = "select", **kw: object) -> Field:
        return Field(name=name, label=label, kind=kind, required=required, section="education", **kw)
    return [f("school--0", "School", fallback="Other"), f("degree--0", "Degree", fallback="Other"),
            f("discipline--0", "Discipline", fallback="Other"), f("start-month--0", "Start date month", options=MONTHS),
            f("start-year--0", "Start date year", "text"), f("end-month--0", "End date month", options=MONTHS),
            f("end-year--0", "End date year", "text")]


def _gh_field(q: dict, section: str) -> Field | None:
    fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]
    if not fields:
        return None
    f = fields[0]  # "resume" + "resume_text": the second is a paste-instead alternative
    name, kind = GH_PAGE_IDS.get(f["name"], (f["name"], GH_KINDS.get(f["type"], "text")))
    return Field(name=name, label=html_to_text(q.get("label") or f["name"]), kind=kind,
                 required=bool(q.get("required")), options=[html_to_text(v["label"]) for v in f.get("values") or []],
                 description=html_to_text(html_to_text(q.get("description"))), section=section)


async def read_greenhouse(fx: Fetcher, ref: PostingRef) -> Form:
    url = f"https://boards-api.greenhouse.io/v1/boards/{ref.board}/jobs/{ref.job_id}"
    data = await fx.get_json(url, params={"questions": "true"})
    fields = [f for q in data.get("questions") or [] if (f := _gh_field(q, "questions"))]
    fields += [f for q in data.get("location_questions") or [] if (f := _gh_field(q, "questions"))]
    for block in data.get("compliance") or []:
        fields += [f for q in block.get("questions") or [] if (f := _gh_field(q, block.get("type") or "eeoc"))]
    if data.get("education") in ("education_required", "education_optional"):
        fields += gh_education(data["education"] == "education_required")
    names = {f.name for f in fields}
    fields += [f for f in GH_PAGE_FIELDS if f.name not in names]
    return Form(platform="greenhouse", company=data.get("company_name") or ref.board, title=data.get("title") or "",
                url=data.get("absolute_url") or f"https://job-boards.greenhouse.io/{ref.board}/jobs/{ref.job_id}",
                posting=html_to_text(html_to_text(data.get("content"))), fields=fields)


def page_url(url: str) -> str:
    """Where the form is: Lever's /apply and Ashby's /application tabs, else the page itself."""
    ref = posting_ref(url)
    base = url.split("?")[0].split("#")[0].rstrip("/")
    if ref and ref.platform == "lever" and not base.endswith("/apply"):
        return base + "/apply"
    if ref and ref.platform == "ashby" and not base.endswith("/application"):
        return base + "/application"
    return english(url)


def english(url: str) -> str:
    """Google Forms in English: it follows the viewer's location otherwise (Arabic in Qatar), and the
    Next button and closed-form text are matched in English."""
    if "docs.google.com/forms" in url and "hl=" not in url:
        return url + ("&" if "?" in url else "?") + "hl=en"
    return url


def platform_of(url: str) -> str:
    """greenhouse, lever, ashby, or the site's own domain ("devpost.com"): each earns Fast apply on its own."""
    ref = posting_ref(url)
    if ref:
        return ref.platform
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return ".".join(host.split(".")[-2:]) if host.count(".") >= 2 and not host.endswith((".qa", ".uk", ".au")) else host


APPLY_BUTTON = re.compile(r"^(?:apply(?: now| here| online| for this (?:job|position|role))?|register(?: now| here)?|sign up"
                          r"|join(?: hackathon| now)?|rsvp|get tickets|reserve (?:a |your )?(?:spot|seat)|attend|request to join"
                          r"|(?:click here to |visit (?:the )?official (?:website|site|page) to )apply|official (?:website|link|page)"
                          r"|apply (?:on|at|via) (?:the )?(?:company|official|employer).{0,20})$", re.I)
# Buttons and links in the page's content (not its menus) that lead to the form; tagged for the click.
GO_JS = """(pattern) => {
  const re = new RegExp(pattern, 'i'); const seen = new Set(); const out = [];
  for (const e of document.querySelectorAll('a, button, [role=button], input[type=submit], input[type=button]')) {
    const text = (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    const r = e.getBoundingClientRect();
    if (!re.test(text) || r.width === 0 || e.closest('header, nav, footer, [role=navigation], [role=banner], [role=contentinfo]')) continue;
    const href = e.tagName === 'A' ? e.href : '';
    const key = href || text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key); e.dataset.bwGo = String(out.length); out.push({ text, href });
  }
  return out;
}"""
FORM_BUILDERS = ("tally.so", "jotform.com", "docs.google.com", "forms.gle", "typeform.com", "airtable.com",
                 "forms.office.com", "forms.microsoft.com", "fillout.com", "paperform.co", "formstack.com")
FOLLOW_HOPS = 2  # listing page → organiser's page → form

# Cloudflare's "Just a moment… Verifying you are human" (Devpost, 29 Oct 2026). In a real window it
# usually clears itself in a few seconds; when it doesn't, the owner ticks the box (APP-10).
CHALLENGE_JS = r"""() => /^just a moment/i.test(document.title)
  || /verifying you are human|verify you are human|performing security verification|checking your browser|checking if the site connection is secure/i
     .test((document.body && document.body.innerText || '').slice(0, 2000))"""
SETTLE_SECONDS = 25


async def settle(page: Any, seconds: int = SETTLE_SECONDS) -> bool:
    """Wait out a bot check. True when the page is clear."""
    for _ in range(seconds * 2):
        try:
            if not await page.evaluate(CHALLENGE_JS):
                return True
        except Exception:  # noqa: BLE001 - the check reloads the page when it passes
            pass
        await page.wait_for_timeout(500)
    return False
TITLE_JS = """() => ({ title: (document.querySelector('meta[property="og:title"]')?.content || document.querySelector('h1')?.innerText
  || document.title || '').trim(), site: (document.querySelector('meta[property="og:site_name"]')?.content || '').trim(),
  doc: document.title })"""


FRAME_STRIDE = 10_000  # a field in frame k is tagged k*10000+n, so the filler knows its frame
SKIP_FRAMES = re.compile(r"captcha|challenge|turnstile|doubleclick|googlesyndication|rokt|ads?\.|analytics|stripe\.com|youtube|vimeo|maps",
                         re.I)


async def read_frames(page: Any, js: str) -> list[dict]:
    """Forms embedded in frames (HubSpot, Typeform, an event checkout): each visible frame is read."""
    out: list[dict] = []
    for k, frame in enumerate(page.frames):
        if k == 0 or SKIP_FRAMES.search(frame.url or "") or frame.url in ("", "about:blank"):
            continue
        try:
            box = await (await frame.frame_element()).bounding_box()
            if not box or box["width"] < 250 or box["height"] < 150:
                continue
            await frame.evaluate("n => { if (!document.body.dataset.bwNext) document.body.dataset.bwNext = n; }",
                                 str(k * FRAME_STRIDE))
            out += await frame.evaluate(js)
        except Exception:  # noqa: BLE001 - a frame that navigates away or refuses scripts is skipped
            continue
    return out


def spelled(board: str, text: str) -> str:
    """The company as the page writes it ("openai" → "OpenAI" from "Research Engineer @ OpenAI")."""
    words = re.findall(r"[\w&.'-]+", text)
    key = re.sub(r"\W", "", board).lower()
    for size in (1, 2, 3):
        for i in range(len(words) - size + 1):
            name = " ".join(words[i:i + size])
            if re.sub(r"\W", "", name).lower() == key:
                return name
    return board.replace("-", " ").title()


async def read_page(page: Any, url: str, fx: Fetcher | None = None) -> Form:
    """APP-9: the form on an open page. With no form, its one Apply/Register button is clicked first."""
    js = (JS_DIR / "read_form.js").read_text()
    if english(page.url) != page.url:  # a forms.gle link lands on the form without hl=en
        await page.goto(english(page.url))
    await settle(page)
    raw = await page.evaluate(js)
    for _ in range(FOLLOW_HOPS):
        if [f for f in raw if f["kind"] != "file"]:
            break
        go = await page.evaluate(GO_JS, APPLY_BUTTON.pattern)
        if len(go) != 1:  # none, or a list of many opportunities: nothing to choose blindly
            break
        if go[0]["href"] and not go[0]["href"].startswith(("javascript:", "#")) and "#" not in go[0]["href"][-1:]:
            await page.goto(go[0]["href"])  # same tab, even for target=_blank
        else:
            try:
                await page.locator('[data-bw-go="0"]').click(timeout=5_000)
            except Exception:  # noqa: BLE001 - covered by a banner or a sticky bar: the page's own click still works
                await page.evaluate("() => document.querySelector('[data-bw-go=\"0\"]').click()")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:  # noqa: BLE001
            pass
        await settle(page)
        raw = await page.evaluate(js)
    raw += await read_frames(page, js)
    fields = [Field(name=f["name"], label=f["label"] or f["name"], kind=f["kind"], required=f["required"],
                    options=f.get("options") or [], widget=f.get("widget") or "", members=f.get("members") or [])
              for f in raw if f["name"] and f["kind"] in ("text", "textarea", "select", "multiselect", "file")]
    meta = await page.evaluate(TITLE_JS)
    ref = posting_ref(url)
    host = urlsplit(page.url).netloc.removeprefix("www.")
    if ref:
        company = spelled(ref.board, f"{meta['site']} {meta['title']} {meta['doc']}")
    elif any(host == b or host.endswith("." + b) for b in FORM_BUILDERS):
        # The form builder is not the organiser: "Aeronautics Hackathon 2026 - registration" → the event.
        company = re.split(r"\s+[-–|:]\s+", meta["title"])[0].strip() or host
    else:
        company = meta["site"] or urlsplit(url).netloc.removeprefix("www.")
    posting = ""
    if fx is not None:
        try:
            posting = html_to_text(await fx.get_text(url.split("?")[0].removesuffix("/apply").removesuffix("/application")))[:20_000]
        except Exception:  # noqa: BLE001 - the posting text is for drafting; the form is what counts
            posting = ""
    if not posting:
        posting = (await page.inner_text("body"))[:20_000]
    title = re.sub(r"\s+\|\s+[^|]*$", "", meta["title"]).strip() or meta["title"]  # "Role | Lever" → "Role"
    # Followed a link to another site: dry runs count for the site whose form it is.
    return Form(platform=platform_of(page.url if fields else url), company=company, title=title, url=page.url,
                posting=posting, fields=fields)


async def read_form(fx: Fetcher, url: str) -> Form:
    ref = posting_ref(url)
    if ref is None:
        raise ValueError(f"not a posting on a supported platform: {url}")
    if ref.platform not in READERS:
        raise ValueError(f"{ref.platform} forms can't be read yet (supported: {', '.join(READERS)})")
    return await read_greenhouse(fx, ref)
