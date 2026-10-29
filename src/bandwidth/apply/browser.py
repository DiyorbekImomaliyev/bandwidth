"""The agent's own Chrome: fills a planned form and reads every value back (SPEC APP-3..5). Only
`submit` clicks Submit, and only Fast apply calls it, after the owner's tap (FA-5).

Chrome runs with its own profile in data/browser/ (Chrome 136+ refuses automation of the default
profile), in a visible window so the owner can watch and take over. Pages stay open after filling:
the owner finishes the form there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from .forms import CHALLENGE_JS, FRAME_STRIDE, settle
from .plan import Step

log = logging.getLogger(__name__)

FORM_TIMEOUT_MS = 20_000
OPTION_TIMEOUT_MS = 5_000

UNKNOWN_REQUIRED_JS = """(known) => [...document.querySelectorAll(
    'input:not([type=hidden]):not([type=file]), textarea, select, [role=combobox]')]
  .filter(e => e.id && !known.includes(e.id) && !known.includes('@' + (e.closest('[data-bw]')?.dataset.bw || ''))
          && (e.required || e.getAttribute('aria-required') === 'true'))
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .filter(e => !e.value && !e.closest('.select__control')?.querySelector('.select__single-value'))
  .map(e => (document.querySelector(`label[for="${CSS.escape(e.id)}"]`)?.innerText || e.getAttribute('aria-label') || e.id).trim())"""

CAPTCHA_JS = """() => [...document.querySelectorAll('iframe')].some(f => {
  const r = f.getBoundingClientRect();
  const s = `${f.title} ${f.src}`.toLowerCase();
  // Seen live on Lever: hCaptcha keeps full-size frames on every form with visibility: hidden.
  const shown = f.checkVisibility ? f.checkVisibility({ visibilityProperty: true, opacityProperty: true })
                                  : getComputedStyle(f).visibility !== 'hidden';
  return shown && r.width > 100 && r.height > 100 && (s.includes('challenge') || s.includes('bframe') || s.includes('captcha'));
})"""

# A password box, or a sign-up wall in a dialog (Meetup's "Sign up to attend this event": Continue with
# Google, no password box; seen live 29 Oct 2026).
SIGN_IN_JS = """() => {
  const shown = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  if ([...document.querySelectorAll('input[type=password]')].some(shown)) return true;
  return [...document.querySelectorAll('[role=dialog], [aria-modal=true], dialog[open]')].filter(shown)
    .some(d => /sign (?:up|in) to|log ?in to|continue with (?:google|apple|facebook|email)/i.test(d.innerText || ''));
}"""

# A checkout that costs money. The agent never pays (Eventbrite "Get tickets" on a paid event, 29 Oct 2026).
PAID_JS = r"""() => {
  const text = document.body.innerText || '';
  if (!/order summary|checkout|place order|pay now|payment method/i.test(text)) return null;
  const m = text.match(/\b(?:order )?total\b\s*:?\s*((?:[A-Z]{3}\s*)?[$€£]?\s*(\d[\d,]*(?:\.\d+)?))/i);
  return m && parseFloat(m[2].replace(/,/g, '')) > 0 ? m[1].trim() : null;
}"""


@dataclass
class FillResult:
    filled: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)   # field name → why
    unknown_required: list[str] = field(default_factory=list)  # labels of required fields not in the plan
    stop: str | None = None   # captcha | sign-in | page
    screenshot: Path | None = None
    shown: dict[str, str] = field(default_factory=dict)  # field name → what the page shows after filling
    pages: int = 1  # pages of a multi-page form filled (APP-12)
    earlier: list[str] = field(default_factory=list)  # fields on pages already left behind


class Browser:
    """One persistent Chrome profile; pages stay open until `close`."""

    def __init__(self, profile: Path, headless: bool = False):
        self.profile, self.headless = profile, headless
        self._pw: Any = None
        self._ctx: Any = None

    async def start(self) -> Browser:
        from playwright.async_api import async_playwright
        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(self.profile), channel="chrome", headless=self.headless, no_viewport=not self.headless,
            locale="en-US")
        return self

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()
        self._ctx = self._pw = None

    async def __aenter__(self) -> Browser:
        return await self.start()

    async def __aexit__(self, *exc: type[BaseException] | BaseException | TracebackType | None) -> None:
        await self.close()

    async def open(self, url: str) -> Any:
        if self._ctx is None:
            await self.start()
        page = await self._ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001 - busy pages never go idle; the form is checked next
            pass
        await settle(page)
        return page


def _scope(page: Any, name: str) -> Any:
    """The page, or the frame a field lives in (tag k*10000+n is in frame k)."""
    if name.startswith("@") and name[1:].isdigit() and (k := int(name[1:]) // FRAME_STRIDE) and k < len(page.frames):
        return page.frames[k]
    return page


def _loc(page: Any, name: str) -> Any:
    """A field by its id (Greenhouse), or by the data-bw tag the page reader gave it (APP-9). CSS
    locators pierce open shadow roots, so web-component fields are found too."""
    if name.startswith("@"):
        return _scope(page, name).locator(f'[data-bw="{name[1:]}"]')
    return page.locator(f'[id="{name}"]')


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


async def _visible_enabled(loc: Any) -> bool:
    return await loc.count() > 0 and await loc.first.is_visible() and await loc.first.is_enabled()


async def _fill_text(page: Any, s: Step) -> str | None:
    loc = _loc(page, s.field.name)
    if not await _visible_enabled(loc):
        return "not visible on the page" if await loc.count() else "not on the page"
    await loc.first.fill(s.value or "")
    got = await loc.first.input_value()
    if got == s.value:
        return None
    phone = s.field.name == "phone" or re.search(r"phone|mobile", s.field.label, re.I)
    if phone and _digits(got) and _digits(s.value or "").endswith(_digits(got)[-7:]):
        return None  # phone widgets reformat the number
    if phone or await loc.first.get_attribute("type") == "tel":
        # Tally pre-fills a country code and ignores a pasted value: type it like a person.
        await loc.first.click()
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")
        await loc.first.press_sequentially(s.value or "", delay=20)
        got = await loc.first.input_value()
        if _digits(got) and _digits(s.value or "").endswith(_digits(got)[-7:]):
            return None
    return f"reads back as {got!r}"


async def _fill_select(page: Any, s: Step) -> str | None:
    loc = _loc(page, s.field.name)
    if not await _visible_enabled(loc):
        return "not visible on the page" if await loc.count() else "not on the page"
    value = s.value or ""
    search = not s.field.options
    # Pages also keep hidden lists (a phone-country picker); a field in a frame has its list there.
    options = _scope(page, s.field.name).locator("[role=option]:visible, [role=listbox] li:visible")
    if not await loc.first.evaluate("e => e.tagName === 'INPUT' || e.isContentEditable"):
        return await _pick(loc, options, value, s.field.fallback)
    # A search box (a place search) reacts to key presses, not pasted text, and to "London" more than
    # "London, United Kingdom" (seen live): type it, then the city alone if nothing came up.
    tries = [(value, value)] + ([(value.split(",")[0].strip(), value)] if search and "," in value else [])
    if s.field.fallback:
        tries.append((s.field.fallback, s.field.fallback))  # "Other" when the school isn't in the page's list
    texts: list[str] = []
    index = None
    for typed, target in tries:
        await loc.first.click()
        await loc.first.fill("")
        if search:
            await loc.first.press_sequentially(typed, delay=40)
        else:
            await loc.first.fill(typed)
        try:
            await options.first.wait_for(timeout=OPTION_TIMEOUT_MS)
            texts = [" ".join(t.split()) for t in await options.all_inner_texts()]
        except Exception:  # noqa: BLE001
            texts = []
        if (index := _choose(target, texts, search=search)) is not None:
            break
    if index is None:
        await loc.first.press("Escape")
        await loc.first.fill("")
        return f"\"{value}\" is not one of the options on the page"
    await options.nth(index).click()
    shown = loc.first.locator("xpath=ancestor::div[contains(@class,'select__control')][1]//*[contains(@class,'single-value')]")
    got = (await shown.first.inner_text()).strip() if await shown.count() else ""
    # Seen live: a phone-country picker shows only "+44" once "United Kingdom +44" is chosen.
    return None if got and got.lower() in texts[index].lower() else f"shows {got!r}"


async def _pick(loc: Any, options: Any, value: str, fallback: str) -> str | None:
    """Any click-to-open dropdown (a button or div, not a text box): open it, click the option."""
    for target in [value] + ([fallback] if fallback else []):
        await loc.first.click()
        try:
            await options.first.wait_for(timeout=OPTION_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            continue
        texts = [" ".join(t.split()) for t in await options.all_inner_texts()]
        if (index := _choose(target, texts, search=True)) is not None:
            await options.nth(index).click()
            got = " ".join((await loc.first.inner_text()).split())
            return None if texts[index].lower() in got.lower() or got.lower() in texts[index].lower() and got \
                else f"shows {got!r}"
        await loc.first.press("Escape")
    return f"\"{value}\" is not one of the options on the page"


def _choose(value: str, texts: list[str], *, search: bool) -> int | None:
    """The option equal to the value; in a search box (no options known in advance), also the one
    option that starts with it ("United Kingdom" → "United Kingdom +44"). Never a guess between two."""
    v = value.strip().lower()
    exact = [i for i, t in enumerate(texts) if t.lower() == v]
    if exact:
        return exact[0]
    if search:
        starts = [i for i, t in enumerate(texts) if t.lower().startswith(v) and not t[len(v):len(v) + 1].isalpha()]
        if len(starts) == 1:
            return starts[0]
        # A place search: "London, United Kingdom" → the one result naming both the city and the country.
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if len(parts) > 1:
            named = [i for i, t in enumerate(texts) if t.lower().split(",")[0].strip() == parts[0]
                     and all(re.search(rf"\b{re.escape(p)}\b", t.lower()) for p in parts[1:])]
            if len(named) == 1:
                return named[0]
    return None


async def _fill_file(page: Any, s: Step) -> str | None:
    loc = _loc(page, s.field.name)
    if not await loc.count():
        return "not on the page"
    if not s.value or not Path(s.value).exists():
        return "the file is missing"
    await loc.first.set_input_files(s.value)
    name = Path(s.value).name
    for _ in range(25):  # seen live: the input is replaced by the file's name once attached
        if await page.get_by_text(name).count():
            return None
        if await loc.count() and await loc.first.evaluate("e => e.files ? e.files.length : 0", timeout=1000):
            return None
        await page.wait_for_timeout(200)
    return "the file was not attached"


CHECKED_JS = """(box) => [...box.querySelectorAll('input[type=checkbox]:checked')]
  .map(i => (document.querySelector(`label[for="${CSS.escape(i.id)}"]`)?.innerText || i.value).trim())"""


async def _fill_checks(page: Any, s: Step) -> str | None:
    """A checkbox group (Greenhouse: a fieldset with the field's name as id). The value lists the
    labels to tick, separated by "; "."""
    box = _loc(page, s.field.name)
    if not await box.count():
        return "not on the page"
    wanted = [v.strip() for v in (s.value or "").split(";") if v.strip()]
    for label in wanted:
        option = box.first.get_by_label(label, exact=True)
        if not await option.count():
            return f"\"{label}\" is not one of the boxes on the page"
        await option.first.check()
    got = await box.first.evaluate(CHECKED_JS)
    return None if sorted(got) == sorted(wanted) else f"ticked {got!r}"


async def _fill_native(page: Any, s: Step) -> str | None:
    loc = _loc(page, s.field.name)
    if not await _visible_enabled(loc):
        return "not visible on the page"
    wanted = [v.strip() for v in (s.value or "").split(";") if v.strip()]
    await loc.first.select_option(label=wanted if s.field.kind == "multiselect" else wanted[0])
    got = await read(page, s)
    return None if got == "; ".join(wanted) else f"shows {got!r}"


async def _fill_group(page: Any, s: Step) -> str | None:
    """Radios or checkboxes: tick the members whose label is one of the values."""
    wanted = {v.strip().lower() for v in (s.value or "").split(";") if v.strip()}
    found = set()
    for tag, label in zip(s.field.members, s.field.options, strict=False):
        box = _scope(page, s.field.name).locator(f'[data-bw="{tag}"]')
        if label.strip().lower() in wanted:
            try:
                await box.first.check(force=True)
            except Exception:  # noqa: BLE001 - styled boxes: click the label instead
                await page.locator(f'label:has([data-bw="{tag}"]), label[for="{await box.first.get_attribute("id") or "-"}"]').first.click()
            found.add(label.strip().lower())
    if found != wanted:
        return f"no box for {sorted(wanted - found)}"
    got = await read(page, s)
    return None if {g.strip().lower() for g in got.split(";") if g.strip()} == wanted else f"ticked {got!r}"


async def _fill_listbox(page: Any, s: Step) -> str | None:
    """Google Forms dropdown: open it, then click the option in the popup."""
    loc = _loc(page, s.field.name)
    await loc.first.click()
    await page.wait_for_timeout(400)
    option = _scope(page, s.field.name).locator(f'[role=option][data-value="{s.value}"]:visible')
    if not await option.count():
        await page.keyboard.press("Escape")
        return f"no option {s.value!r}"
    await option.last.click()
    await page.wait_for_timeout(400)
    got = await read(page, s)
    return None if got == s.value else f"shows {got!r}"


async def _fill_yesno(page: Any, s: Step) -> str | None:
    box = _loc(page, s.field.name)
    option = box.first.locator("button[data-option]").filter(has_text=re.compile(rf"^\s*{re.escape(s.value or '')}\s*$", re.I))
    if not await option.count():
        return f"\"{s.value}\" is not one of the buttons"
    await option.first.click()
    return None if (await read(page, s)).lower() == (s.value or "").lower() else "the button did not stay pressed"


async def _fill_editable(page: Any, s: Step) -> str | None:
    loc = _loc(page, s.field.name)
    await loc.first.click()
    await loc.first.fill(s.value or "")
    return None if (await read(page, s)).strip() == (s.value or "").strip() else "the text did not stick"


FILLERS = {"text": _fill_text, "textarea": _fill_text, "select": _fill_select, "file": _fill_file,
           "multiselect": _fill_checks}
WIDGETS = {"native": _fill_native, "radio": _fill_group, "checkbox": _fill_group, "yesno": _fill_yesno,
           "editable": _fill_editable, "listbox": _fill_listbox}
CONFIRMED = re.compile(r"thank you for (?:applying|your application|your interest|registering)|application (?:has been |was )?"
                       r"(?:submitted|received)|we(?:'ve| have) received your application|your response has been recorded"
                       r"|you(?:'re| are) (?:registered|going|in)\b|registration (?:is )?(?:confirmed|complete)", re.I)
# Multi-page forms (Google Forms, Eventbrite checkout, Devpost registration): only these move on.
NEXT_WORDS = r"^(?:next|next page|next step|continue|save (?:and|&) continue|proceed)$"
FINAL_WORDS = r"^(?:submit(?: application| form)?|send|register|apply|complete registration|finish|place order|pay(?: now)?)$"
BUTTONS_JS = """(pattern) => [...document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], a')]
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.disabled
                 && !e.closest('header, nav, footer, [role=navigation]'); })
  .filter(e => new RegExp(pattern, 'i').test((e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim()))
  .map((e, i) => { e.dataset.bwButton = pattern.length + ':' + i; return e.dataset.bwButton; })"""
MAX_PAGES = 8
# Google Forms puts hidden inputs first: wait for any control a person can see.
ANY_CONTROL = ("input:not([type=hidden]):visible, textarea:visible, select:visible, [role=radio]:visible, "
               "[role=checkbox]:visible, [role=listbox]:visible, [role=combobox]:visible")
SUBMIT_WAIT_MS = 30_000


async def read(page: Any, s: Step) -> str:
    """What the page shows for a field now: typed text, the chosen option, or the attached file's name."""
    loc = _loc(page, s.field.name)
    w = s.field.widget
    if w == "native":
        return "; ".join(t.strip() for t in await loc.first.evaluate("e => [...e.selectedOptions].map(o => o.text)"))
    if w == "listbox":  # Google Forms dropdown
        return (await loc.first.evaluate(
            "e => e.querySelector('[role=option][aria-selected=true]')?.dataset.value || ''")).strip()
    if w in ("radio", "checkbox"):
        ticked = []
        for tag, label in zip(s.field.members, s.field.options, strict=False):
            box = _scope(page, s.field.name).locator(f'[data-bw="{tag}"]')
            if await box.count() and await box.first.is_checked():
                ticked.append(label)
        return "; ".join(ticked)
    if w == "yesno":
        pressed = loc.first.locator('button[aria-pressed="true"], button[class*=active], button[class*=selected]')
        return (await pressed.first.inner_text()).strip() if await pressed.count() else ""
    if w == "editable":
        return (await loc.first.inner_text()).strip()
    if s.field.kind in ("text", "textarea"):
        return await loc.first.input_value() if await loc.count() else ""
    if s.field.kind == "select":
        if not await loc.count():
            return ""
        shown = loc.first.locator("xpath=ancestor::div[contains(@class,'select__control')][1]"
                                  "//*[contains(@class,'single-value')]")
        if await shown.count():  # react-select (Greenhouse)
            return " ".join((await shown.first.inner_text()).split())
        if await loc.first.evaluate("e => e.tagName === 'INPUT'"):
            return (await loc.first.input_value()).strip()
        return " ".join((await loc.first.inner_text()).split())  # any other dropdown shows its choice
    if s.field.kind == "multiselect":
        return "; ".join(await loc.first.evaluate(CHECKED_JS)) if await loc.count() else ""
    if s.field.kind == "file" and s.value:
        name = Path(s.value).name
        return name if await page.get_by_text(name).count() else ""
    return ""


async def fill_one(page: Any, s: Step) -> str | None:
    """Fill one field (an essay, or a legal statement after I agree); None when it reads back right."""
    filler = WIDGETS.get(s.field.widget) or FILLERS.get(s.field.kind)
    if filler is None:
        return f"{s.field.kind} fields are filled by you"
    try:
        return await filler(page, s)
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}"


async def verify(page: Any, steps: list[Step], shown: dict[str, str]) -> dict[str, str]:
    """FA-5: read every filled field again; field name → how it differs from the preview."""
    diff = {}
    for s in steps:
        if s.field.name not in shown:
            continue
        try:
            now = await read(page, s)
        except Exception as e:  # noqa: BLE001
            now = f"unreadable ({type(e).__name__})"
        if now != shown[s.field.name]:
            diff[s.field.name] = f"was {shown[s.field.name]!r}, now {now!r}"
    return diff


async def submit(page: Any, wait_ms: int = SUBMIT_WAIT_MS) -> str:
    """FA-5: click the form's Submit once. Returns submitted | captcha | sign-in | no-button | unclear.
    Never retried: anything but a confirmation page is for the owner to check."""
    if stop := await blocked(page):
        return stop
    button = page.locator("button[type=submit]:visible, input[type=submit]:visible")
    if not await button.count():  # Google Forms: a div with role=button, "Submit"
        tags = await page.evaluate(BUTTONS_JS, FINAL_WORDS)
        if len(tags) != 1:
            return "no-button"
        button = page.locator(f'[data-bw-button="{tags[0]}"]')
    await button.first.click()
    for _ in range(wait_ms // 500):
        await page.wait_for_timeout(500)
        try:
            if CONFIRMED.search(await page.inner_text("body")):
                return "submitted"
            if stop := await blocked(page):
                return stop
        except Exception:  # noqa: BLE001 - the page is navigating
            continue
    return "unclear"


# A page that can't be applied to any more (Radar sent an Eventbrite event whose sales had ended, 29 Oct 2026).
CLOSED_JS = r"""() => {
  const m = (document.body.innerText || '').match(/\b(event (?:has )?ended|sales (?:have )?ended|registration (?:is |has )?closed|applications? (?:are |is |have )?(?:now )?closed|no longer accepting (?:applications|registrations|responses)|this (?:job|position|posting) is no longer (?:available|open)|(?:position|role) has been filled|this form is no longer accepting responses|deadline has passed)\b/i);
  return m ? m[1] : null;
}"""


async def _any_control(page: Any) -> None:
    """Wait until a control shows on the page or in one of its frames (an embedded form)."""
    for _ in range(FORM_TIMEOUT_MS // 500):
        for frame in page.frames:
            try:
                if await frame.locator(ANY_CONTROL).count():
                    return
            except Exception:  # noqa: BLE001 - a frame navigating away
                continue
        await page.wait_for_timeout(500)
    raise TimeoutError("no form control appeared")


async def next_page(page: Any) -> str:
    """APP-12: click the form's Next/Continue. Returns moved | none | stuck | submitted.
    Never clicked when the page also shows a Submit-like button: that page is the last one."""
    if await page.evaluate(BUTTONS_JS, FINAL_WORDS):
        return "none"
    tags = await page.evaluate(BUTTONS_JS, NEXT_WORDS)
    if len(tags) != 1:
        return "none"
    before = await page.evaluate("() => document.body.dataset.bwNext || '0'")
    seen = await page.evaluate("() => [...document.querySelectorAll('[data-bw]')].filter(e => e.offsetParent).length")
    await page.locator(f'[data-bw-button="{tags[0]}"]').click()
    for _ in range(20):
        await page.wait_for_timeout(500)
        try:
            if CONFIRMED.search(await page.inner_text("body")):
                return "submitted"  # it was the last page after all; reported, never hidden
            # A new document (Google Forms reloads) keeps numbering after the last page's tags.
            await page.evaluate("n => { if (!document.body.dataset.bwNext) document.body.dataset.bwNext = n; }", before)
            shown = await page.evaluate("() => [...document.querySelectorAll('[data-bw]')].filter(e => e.offsetParent).length")
            fresh = await page.evaluate("""() => [...document.querySelectorAll('input, textarea, select, [role=combobox]')]
              .some(e => !e.closest('[data-bw]') && !e.dataset.bw && e.offsetParent && !['hidden', 'submit', 'button'].includes(e.type))""")
            if fresh or shown < seen:
                await settle(page)
                return "moved"
        except Exception:  # noqa: BLE001 - mid-navigation
            continue
    return "stuck"  # a required answer is missing: the page shows why


async def blocked(page: Any) -> str | None:
    if await page.evaluate(CHALLENGE_JS):
        return "verify"
    if await page.evaluate(CAPTCHA_JS):
        return "captcha"
    if await page.evaluate(SIGN_IN_JS):
        return "sign-in"
    if await page.evaluate(PAID_JS):
        return "paid"
    return None


async def fill(page: Any, steps: list[Step], *, shots: Path | None = None) -> FillResult:
    """Fill every fillable step and read it back. There is no submit here (APP-5)."""
    result = FillResult()
    try:
        await _any_control(page)
    except Exception:  # noqa: BLE001
        result.stop = "page"
        return result
    # Only a sign-in stops filling up front; a CAPTCHA is solved by the owner after everything is filled.
    if (stop := await blocked(page)) in ("sign-in", "verify", "paid"):
        result.stop = stop
        return result
    for s in steps:
        if not s.fillable:
            continue
        if why := await fill_one(page, s):  # one field never stops the rest
            result.failed[s.field.name] = why
        else:
            result.filled.append(s.field.name)
            result.shown[s.field.name] = await read(page, s)
    result.stop = await blocked(page)
    known = [s.field.name for s in steps] + [f"@{m}" for s in steps for m in s.field.members]
    result.unknown_required = await page.evaluate(UNKNOWN_REQUIRED_JS, known)
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
        result.screenshot = shots / "after-fill.png"
        await page.screenshot(path=str(result.screenshot), full_page=True)
    log.info("filled %d field(s); %d failed; %d unknown required", len(result.filled), len(result.failed),
             len(result.unknown_required))
    return result
