"""Applying, start to finish: read the form, plan it, draft the essays, fill it in the agent's Chrome,
then either hand it over with a checklist (Partial help, SPEC §10) or send a preview that ends in
Submit (Fast apply, §11). /answer teaches the vault new answers (VLT-4).

One tap of ⚡ Apply picks the mode:
- Partial help when the form or posting rules out AI-written answers (FA-3) or a stop came up;
- a dry run while the platform has fewer than 5 clean dry runs (FA-7): the preview, no Submit;
- Fast apply after that: the preview with I agree buttons and one Submit (FA-4, FA-5).
Nothing is ever submitted without the owner's tap on that preview's Submit.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bandwidth.radar.http import Fetcher

from ..ai import Router
from . import essays
from .applications import Applications
from .browser import CLOSED_JS, MAX_PAGES, PAID_JS, FillResult, blocked, fill, fill_one, next_page, read, submit, verify
from .essays import LIMIT, limit_of
from .fit import fit_report
from .forms import READERS, Form, page_url, platform_of, read_form, read_page
from .guess import guess
from .letter import write_letter
from .plan import Step, agree_option, ai_policy, plan_form
from .settings import ApplySettings
from .vault import Vault, VaultError

log = logging.getLogger(__name__)

Send = Callable[..., Awaitable[Any]]
CHECKLIST_KEY = "last_checklist"
DRY_RUNS = 5                       # FA-7: clean dry runs a platform needs before Submit appears
PREVIEW_TTL = timedelta(hours=24)  # FA-6
TG_LIMIT = 4000
STOPS = {"stuck": "The form didn't move to its next page: a required answer is missing (the window shows which).",
         "captcha": "The page has a CAPTCHA: everything else is filled. Solve it in the window and press Submit there.",
         "sign-in": "The page asks you to sign in, so I stopped.", "verify": "The page wants a human check.", "paid": "This costs money, and I never pay.",
         "page": "I couldn't read the form on the page."}
CLOSED = "That form's window is closed. Tap ⚡ Apply again."
HANDOVER = ("sign-in", "verify")  # the owner acts in the window, then taps Try again (APP-10)
ONE_LINE_WORDS = 80
# Optional text boxes that are not questions to the owner's story: left empty.
SKIP_ESSAY = re.compile(r"additional information|anything else|other (?:information|context|comments)|if you answered"
                        r"|if yes|if so|please (?:specify|explain) (?:if|any)|sponsorship needs|pronunciation", re.I)
STATE_WORDS = {"planned": "⏳ reading the form", "filling": "⏳ filling", "handed_over": "🖐 open on your Mac",
               "ready": "👀 waiting for you in the preview", "submitting": "📨 submitting", "submitted": "✅ sent",
               "needs_check": "⚠️ check the window", "failed": "❌ failed"}


@dataclass
class Run:
    """One application's live state on this Mac: gone when the bot restarts (the window is too)."""
    form: Form
    steps: list[Step]
    page: Any
    shown: dict[str, str] = field(default_factory=dict)
    legal: list[Step] = field(default_factory=list)
    agreed: dict[str, Step] = field(default_factory=dict)
    earlier: set[str] = field(default_factory=set)  # on pages already left: no longer readable


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(text: str, n: int = TG_LIMIT) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class ApplyService:
    def __init__(self, *, vault_dir: Path, router: Router, apps: Applications, send: Send, browser: Any,
                 audit_dir: Path, fill: Callable[..., Awaitable[FillResult]] = fill,
                 settings: ApplySettings | None = None, draft: Callable[..., Awaitable[str]] = essays.draft):
        self.vault_dir, self.router, self.apps, self.send = vault_dir, router, apps, send
        self.browser, self.audit_dir, self.fill = browser, audit_dir, fill
        self.settings, self.draft = settings or ApplySettings(), draft
        self.runs: dict[int, Run] = {}
        self.waiting: dict[int, tuple[str, str, Any]] = {}  # waiting for a sign-in or a click: url, mode, page
        self.tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ ⚡ Fast apply / 🖐 Help me apply

    async def help(self, url: str, mode: str = "fast") -> int | None:
        """Read the form on any site, fill everything that can be filled, then preview (Fast apply)
        or hand over with a fit report (Help me apply, JOB-5). Returns the application id."""
        url = url.strip()
        if "linkedin.com" in urlsplit(url).netloc.lower():
            await self.send("LinkedIn is never automated: their rules forbid it, and the account is worth more than "
                            "one application. Open the job and use the company's own site if it links one; send me that link.")
            return None
        if when := self.apps.submitted(url):
            await self.send(f"You already sent this application on {when[:10]}.")
            return None
        try:
            vault = Vault.load(self.vault_dir)
        except VaultError as e:
            await self.send(f"Applying needs your vault first: {html.escape(str(e))}")
            return None
        platform = platform_of(url)
        page = None
        try:
            if platform in READERS:
                async with Fetcher() as fx:
                    form = await read_form(fx, url)
            else:
                page = await self.browser.open(page_url(url))
                if (stop := await blocked(page)) in HANDOVER:
                    return await self._wait_for_sign_in(url, mode, page, why=stop)
                async with Fetcher() as fx:
                    form = await read_page(page, url, fx)
                if not form.fields and (stop := await blocked(page)) in (*HANDOVER, "paid"):
                    if stop in HANDOVER:  # the wall came up after the Attend/Register click
                        return await self._wait_for_sign_in(url, mode, page, why=stop)
                    price = await page.evaluate(PAID_JS)
                    await self.send(f"💳 <b>{html.escape(form.title or url)}</b> costs {html.escape(price or 'money')}. "
                                    "I never pay; it's open in the window if you want it.")
                    return None
        except Exception as e:  # noqa: BLE001
            log.exception("reading %s failed", url)
            await self.send(f"I couldn't read that page: {html.escape(str(e))[:300]}")
            return None
        if not form.fields:
            if page is not None and (why := await page.evaluate(CLOSED_JS)):
                await self.send(f"⛔ <b>{html.escape(form.title or url)}</b>: the page says \"{html.escape(why)}\". "
                                "Nothing to fill.")
                return None
            return await self._wait_for_sign_in(url, mode, page, why="no-form", title=form.title)
        return await self._apply(url, form, vault, mode, page)

    async def _wait_for_sign_in(self, url: str, mode: str, page: Any, app_id: int | None = None,
                                why: str = "sign-in", title: str = "") -> int:
        """APP-10: the window stays open for the owner to sign in once, or to pass a bot check."""
        site = urlsplit(url).netloc.removeprefix("www.")
        if app_id is None:
            app_id = self.apps.create(url, platform_of(url), site, url, mode)
        self.waiting[app_id] = (url, mode, page)
        text = (f"👆 I found no form on <b>{html.escape(title or site)}</b>. Do the step it needs in the Chrome "
                "window on your Mac (pick a date, Register, Join, Apply), then tap Try again; I fill the rest."
                if why == "no-form" else
                f"🛡 <b>{html.escape(site)}</b> wants to check you're human. Tick \"Verify you are human\" in the "
                "Chrome window on your Mac, then tap Try again." if why == "verify" else
                f"🔑 <b>{html.escape(site)}</b> asks you to sign in. Sign in in the Chrome window on your Mac "
                "(I never see your password; the window remembers it next time), then tap Try again.")
        await self.send(text, keyboard([("🔄 Try again", f"r:{app_id}")]))
        return app_id

    async def retry(self, app_id: int) -> str:
        """After the owner signed in, or clicked through to the form, in the window."""
        waiting = self.waiting.pop(app_id, None)
        if not waiting:
            return CLOSED
        url, mode, page = waiting
        task = asyncio.create_task(self._retry(app_id, url, mode, page))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return "🔄 Reading the form again…"

    async def _retry(self, app_id: int, url: str, mode: str, page: Any) -> None:
        try:
            if page is None or await blocked(page) in HANDOVER:
                page = page or await self.browser.open(page_url(url))
                await self._wait_for_sign_in(url, mode, page, app_id or None, why=(await blocked(page)) or "sign-in")
                return
            vault = Vault.load(self.vault_dir)
            async with Fetcher() as fx:
                form = await read_page(page, url, fx)
            if not form.fields:
                self.waiting[app_id] = (url, mode, page)
                await self.send("Still no form on that page. Open the form in the window, then tap Try again.",
                                keyboard([("🔄 Try again", f"r:{app_id}")]))
                return
            await self._apply(url, form, vault, mode, page, app_id=app_id or None)
        except Exception as e:  # noqa: BLE001
            log.exception("retry failed for %s", url)
            await self.send(f"I couldn't read the form: {html.escape(type(e).__name__)}. It's still open on your Mac.")

    async def _apply(self, url: str, form: Form, vault: Vault, want: str, page: Any = None,
                     app_id: int | None = None) -> int:
        policy = ai_policy(form)
        mode = "partial" if (policy or want == "partial") else ("fast" if self.earned(form.platform) else "dry")
        if app_id is None:
            app_id = self.apps.create(url, form.platform, form.company, form.title, mode)
        else:
            self.apps.update(app_id, company=form.company, title=form.title, mode=mode)
        audit = self.audit_dir / str(app_id)
        cloud = self.settings.drafting == "cloud_anonymised"
        try:
            steps, drafted, result, page = await self._fill_pages(app_id, url, form, vault, page, audit, policy, cloud)
        except Exception as e:  # noqa: BLE001 - the owner hears about it; the bot keeps running
            log.exception("applying failed for %s", url)
            self.apps.update(app_id, error=f"{type(e).__name__}: {e}"[:500])
            self.apps.move(app_id, {"planned", "filling"}, "failed")
            await self.send(f"Something went wrong while filling <b>{html.escape(form.title)}</b>: "
                            f"{html.escape(type(e).__name__)}. Nothing was sent.")
            return app_id
        run = Run(form, steps, page, dict(result.shown), earlier=set(result.earlier),
                  legal=[s for s in steps if s.source == "legal"])
        self.runs[app_id] = run
        if result.stop in HANDOVER:
            self.apps.move(app_id, {"filling"}, "planned")
            await self._wait_for_sign_in(url, want, page, app_id, why=result.stop)
            return app_id
        if policy or result.stop:
            self.apps.move(app_id, {"filling"}, "handed_over")
            await self.send(self._checklist(app_id, form, steps, result, note=policy))
            if mode == "partial" and result.stop in (None, "captcha"):
                await self._fit(form, vault, cloud)
            return app_id
        if mode == "partial":
            self.apps.move(app_id, {"filling"}, "handed_over")
            await self._preview(app_id, run, result, drafted, "partial")
            await self._fit(form, vault, cloud)
            return app_id
        self.apps.move(app_id, {"filling"}, "ready")
        await self._preview(app_id, run, result, drafted, mode)
        return app_id

    async def _fill_pages(self, app_id: int, url: str, form: Form, vault: Vault, page: Any, audit: Path,
                          policy: str | None, cloud: bool) -> tuple[list[Step], dict[str, str], FillResult, Any]:
        """Plan and fill each page of the form; a multi-page form moves on with its Next button (APP-12)."""
        steps: list[Step] = []
        drafted: dict[str, str] = {}
        result = FillResult()
        current = form
        for n in range(MAX_PAGES):
            these = await plan_form(current, vault, self.router)
            await guess(self.router, vault, current, these, cloud=cloud,
                        applied_before=self._applied_before(form.company, app_id))
            if not policy:
                drafted |= await self._draft_essays(current, vault, these, audit)
            steps += these
            self._save_plan(app_id, audit, url, form, steps)
            if n == 0:
                self.apps.move(app_id, {"planned"}, "filling")
                page = page or await self.browser.open(form.url)
            got = await self.fill(page, these, shots=audit)
            result.filled += got.filled
            result.failed |= got.failed
            result.shown |= got.shown
            result.unknown_required = got.unknown_required
            result.stop, result.screenshot = got.stop, got.screenshot or result.screenshot
            if got.stop or not hasattr(page, "evaluate"):
                break
            moved = await next_page(page)
            if moved == "submitted":  # never expected: Next is not clicked beside a Submit button
                log.warning("app %d: a Next button sent the form", app_id)
                await self.send("⚠️ The form's Continue button sent it. Check the window.")
                result.stop = "page"
                break
            if moved != "moved":
                result.stop = "stuck" if moved == "stuck" else result.stop
                break
            if (stop := await blocked(page)) not in (None, "captcha"):
                result.stop = stop
                break
            result.earlier += [s.field.name for s in these]
            known = {s.field.name for s in steps}
            fresh = await read_page(page, url, None)
            # The new page's questions, drafted with the first page's posting, company and role.
            current = replace(form, fields=[f for f in fresh.fields if f.name not in known])
            result.pages = n + 2
            if not current.fields:
                break
        return steps, drafted, result, page

    async def _fit(self, form: Form, vault: Vault, cloud: bool) -> None:
        try:
            text = await fit_report(self.router, vault, form, cloud=cloud)
        except Exception:  # noqa: BLE001 - the report is advice; the form is already filled
            log.exception("fit report failed")
            return
        if text:
            await self.send(_cut(text))

    def _applied_before(self, company: str, app_id: int) -> bool:
        since = (_now() - timedelta(days=550)).isoformat(timespec="seconds")
        return any(r["company"] == company and r["id"] != app_id and r["state"] in ("submitted", "needs_check")
                   and r["created_at"] >= since for r in self.apps.recent(200))

    def earned(self, platform: str) -> bool:
        return int(self.apps.kv_get(f"dry_ok:{platform}") or 0) >= DRY_RUNS

    async def _draft_essays(self, form: Form, vault: Vault, steps: list[Step], audit: Path) -> dict[str, str]:
        """FA-1: a new text for every essay question and every cover letter, optional ones too (the
        owner wants everything filled); "additional information" and follow-ups to another answer are
        left. A file field gets the letter as a PDF (FA-8). Returns field name → text."""
        drafted: dict[str, str] = {}
        cloud = self.settings.drafting == "cloud_anonymised"
        for s in steps:
            if s.source != "essay" or (not s.field.required and SKIP_ESSAY.search(s.field.label)):
                continue
            description = s.field.description
            if s.field.kind == "text" and not LIMIT.search(f"{s.field.label}\n{description}"):
                description = f"{description}\n{ONE_LINE_WORDS} words".strip()  # a one-line box: one short paragraph
            try:
                text = await self.draft(self.router, vault, s.field.label, description, form.posting,
                                        company=form.company, role=form.title, cloud=cloud)
            except Exception as e:  # noqa: BLE001 - one failed draft leaves that question to the owner
                log.warning("draft failed for %r: %s", s.field.label, e)
                s.reason = "essay: the draft failed, so it is yours"
                continue
            if not text.strip():
                continue
            if s.field.kind == "text":
                text = " ".join(text.split())
            drafted[s.field.name] = text
            if s.field.kind == "file":
                p = vault.profile
                name = f"{p.first_name} {p.last_name}".strip()
                contacts = [p.email, p.phone, p.links.linkedin or ""]
                s.value = str(write_letter(audit / f"{name.replace(' ', '-')}-Cover-Letter.pdf", text,
                                           name=name, contacts=contacts))
            else:
                s.value = text
        return drafted

    def _save_plan(self, app_id: int, audit: Path, url: str, form: Form, steps: list[Step]) -> None:
        plan = {"url": url, "form_url": form.url, "company": form.company, "title": form.title,
                "steps": [{"name": s.field.name, "label": s.field.label, "required": s.field.required,
                           "source": s.source, "value": s.value, "reason": s.reason} for s in steps]}
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
        self.apps.update(app_id, plan=json.dumps(plan, ensure_ascii=False))

    # ------------------------------------------------------------------ what is left (APP-6)

    def _items(self, app_id: int, steps: list[Step], result: FillResult, *, skip_agreeable: bool) -> list[dict[str, Any]]:
        """The numbered questions left to the owner, required first; saved for /answer."""
        filled = set(result.filled)
        items: list[dict[str, Any]] = []
        for s in steps:
            if s.field.name in filled or (skip_agreeable and s.source == "legal" and agree_option(s.field)):
                continue
            items.append({"label": s.field.label, "source": s.source, "required": s.field.required,
                          "reason": _reason(s, result.failed.get(s.field.name))})
        items += [{"label": label.rstrip("*").strip(), "source": "unknown", "required": True,
                   "reason": "not known to the plan; check it on the page"} for label in result.unknown_required]
        items.sort(key=lambda i: not i["required"])  # stable: the form's order within each group
        for n, item in enumerate(items, 1):
            item["n"] = n
        self.apps.kv_set(CHECKLIST_KEY, {"app": app_id, "items": items})
        return items

    def _item_lines(self, items: list[dict[str, Any]]) -> list[str]:
        if not items:
            return []
        lines = ["\n<b>Left to you</b>"]
        lines += [f"{i['n']}. {html.escape(i['label'])}{_optional(i)} — {html.escape(i['reason'])}" for i in items]
        if example := next((i["n"] for i in items if i["source"] not in ("legal", "essay", "unknown")), None):
            lines.append(f"\nReply <code>/answer {example} Yes</code> to save your answer to question {example}; "
                         "next time I fill it.")
        return lines

    def _checklist(self, app_id: int, form: Form, steps: list[Step], result: FillResult, note: str | None = None) -> str:
        items = self._items(app_id, steps, result, skip_agreeable=False)
        lines = [f"⚡ <b>{html.escape(form.title)}</b> · {html.escape(form.company)}",
                 f"Filled {len(result.filled)} of {len(steps)} fields."]
        if note:
            lines.append(f"I left the essays to you: {html.escape(note)}.")
        if result.stop:
            lines.append(STOPS.get(result.stop, "I stopped early."))
        lines.append("The form is open on your Mac: finish it and press Submit there. I never submit in this mode.")
        return "\n".join(lines + self._item_lines(items))

    # ------------------------------------------------------------------ the preview (FA-4, FA-7)

    async def _preview(self, app_id: int, run: Run, result: FillResult, drafted: dict[str, str], mode: str) -> None:
        form, steps = run.form, run.steps
        items = self._items(app_id, steps, result, skip_agreeable=mode != "partial")
        by_name = {s.field.name: s for s in steps}
        values, guessed = [], []
        for name in result.filled:
            s = by_name[name]
            if s.source == "guessed":
                guessed.append(f"• {html.escape(s.field.label)}: <b>{html.escape(_cut(s.value or '', 120))}</b>"
                               f" <i>({html.escape(s.reason)})</i>")
                continue
            shown = "see below" if name in drafted else (Path(s.value or "").name if s.field.kind == "file"
                                                         else result.shown.get(name, s.value or ""))
            values.append(f"• {html.escape(s.field.label)}: {html.escape(_cut(shown, 120))}")
        dry = int(self.apps.kv_get(f"dry_ok:{form.platform}") or 0)
        head = {"fast": "Fast apply", "partial": "🖐 Help me apply: I never submit in this mode"}.get(
            mode, f"Dry run {dry + 1} of {DRY_RUNS} for {form.platform}: nothing will be sent")
        lines = [f"⚡ <b>{html.escape(form.title)}</b> · {html.escape(form.company)}", head,
                 f"These {len(result.filled)} of {len(steps)} fields are in the form on your Mac now"
                 + (f" ({result.pages} pages; the window shows the last one):" if result.pages > 1 else ":"), *values]
        if guessed:
            lines += ["", "🤖 <b>I chose these for you: check them</b>", *guessed]
        await self.send(_cut("\n".join(lines + self._item_lines(items))))

        for name, text in drafted.items():
            if name in result.filled:
                label = by_name[name].field.label
                await self.send(_cut(f"✍️ <b>{html.escape(label)}</b>\n\n{html.escape(text)}"))
        if mode == "partial":
            await self.send("Everything I could fill is in the window. Read the 🤖 choices and the texts, finish "
                            "what's left, and press Submit there. Tap below when you're done, so I learn your answers.",
                            keyboard([("📚 Learn my answers", f"l:{app_id}")]))
            return
        for n, s in enumerate(run.legal):
            quote = html.escape(_cut(f"{s.field.label}\n{s.field.description}".strip(), 3000))
            if agree_option(s.field):
                await self.send(f"⚖️ <blockquote>{quote}</blockquote>", keyboard([("✅ I agree", f"g:{app_id}:{n}")]))

        digest = hashlib.sha256(json.dumps(sorted(result.shown.items())).encode()).hexdigest()[:12]
        self.apps.update(app_id, preview_hash=digest)
        self.apps.kv_set(f"preview_at:{app_id}", _now().isoformat())
        gaps = [i for i in items if i["required"]]
        if mode == "dry":
            await self.send("Check every value in the window. Is it all right?",
                            keyboard([("✅ Values right", f"v:{app_id}:y"), ("❌ Something wrong", f"v:{app_id}:n")]))
        elif gaps:
            self.apps.move(app_id, {"ready"}, "handed_over")
            await self.send(f"I can't submit this one: {len(gaps)} required question(s) are still empty (above). "
                            "Fill them in the window and press Submit there.")
        else:
            await self.send("Tap ✅ I agree on each statement, then Submit. I read every field again before I click.",
                            keyboard([("📨 Submit", f"s:{app_id}:{digest}")]))

    # ------------------------------------------------------------------ buttons

    async def on_button(self, data: str) -> str:
        kind, _, rest = data.partition(":")
        sid, _, arg = rest.partition(":")
        if not sid.isdigit():
            return ""
        app_id = int(sid)
        if kind == "g":
            return await self.agree(app_id, int(arg) if arg.isdigit() else -1)
        if kind == "v":
            return await self.verdict(app_id, arg == "y")
        if kind == "s":
            return await self.start_submit(app_id, arg)
        if kind == "r":
            return await self.retry(app_id)
        if kind == "l":
            learned = await self.learn(app_id, confirm_guesses=True)
            return f"📚 Learned {len(learned)} answer(s)" if learned else "Nothing new to learn in the window."
        return ""

    async def agree(self, app_id: int, n: int) -> str:
        """FA-4: tick this statement in this application's form, and nothing else."""
        run, app = self.runs.get(app_id), self.apps.get(app_id)
        if not run or not app:
            return CLOSED
        if app["state"] != "ready":
            return "This application is past the preview."
        if not 0 <= n < len(run.legal):
            return ""
        f = run.legal[n].field
        option = agree_option(f)
        if option is None:
            return "Tick that one yourself in the window."
        step = Step(f, "legal", option)
        if why := await fill_one(run.page, step):
            return f"I couldn't tick it: {why}"
        run.agreed[f.name] = step
        run.shown[f.name] = await read(run.page, step)
        return "✅ Agreed: ticked in the form"

    async def verdict(self, app_id: int, right: bool) -> str:
        """FA-7: a clean dry run counts toward Fast apply; a wrong one starts the count again. Either
        way the answers the owner gave in the window are learned (FA-11)."""
        app = self.apps.get(app_id)
        if not app or app["mode"] != "dry":
            return ""
        if not self.apps.move(app_id, {"ready"}, "handed_over"):
            return "Already noted."
        await self._learn_and_tell(app_id, confirm_guesses=right)
        key = f"dry_ok:{app['platform']}"
        ok = int(self.apps.kv_get(key) or 0) + 1 if right else 0
        self.apps.kv_set(key, ok)
        platform = app["platform"].title()
        if not right:
            return f"❌ Noted. {platform} dry runs start again at 0. The form stays open for you."
        if ok >= DRY_RUNS:
            return f"✅ {ok} clean dry runs: Fast apply is on for {platform}."
        return f"✅ Noted: {ok} of {DRY_RUNS} clean dry runs for {platform}."

    async def start_submit(self, app_id: int, digest: str) -> str:
        """FA-5, FA-6: every check before the one compare-and-set; the click runs in the background."""
        app, run = self.apps.get(app_id), self.runs.get(app_id)
        if not app:
            return ""
        if app["mode"] != "fast":
            return "Dry runs never submit."
        if app["state"] == "submitted":
            return "Already sent."
        if app["state"] != "ready":
            return f"Can't submit now: it is {app['state'].replace('_', ' ')}."
        if digest != app["preview_hash"]:
            return "That is an older preview. Use the newest one."
        at = self.apps.kv_get(f"preview_at:{app_id}")
        if not at or _now() - datetime.fromisoformat(at) > PREVIEW_TTL:
            return "That preview is over 24 hours old. Tap ⚡ Apply again."
        cap = self.settings.daily_cap
        if self.apps.submitted_since((_now() - timedelta(days=1)).isoformat(timespec="seconds")) >= cap:
            return f"Daily cap reached: {cap} a day. Press Submit in the window if you want this one today."
        if not run:
            return CLOSED
        missing = [s.field.label for s in run.legal if s.field.required and s.field.name not in run.agreed]
        if missing:
            return _cut("Tap ✅ I agree first: " + "; ".join(missing), 190)
        if not self.apps.move(app_id, {"ready"}, "submitting"):
            return "Already being submitted."
        task = asyncio.create_task(self._submit(app_id, run))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return "📨 Checking the form, then submitting…"

    async def _submit(self, app_id: int, run: Run) -> None:
        title = f"<b>{html.escape(run.form.title)}</b> · {html.escape(run.form.company)}"
        try:
            steps = [*run.steps, *run.agreed.values()]
            last_page = {k: v for k, v in run.shown.items() if k not in run.earlier}
            if diff := await verify(run.page, steps, last_page):
                self.apps.move(app_id, {"submitting"}, "handed_over")
                labels = {s.field.name: s.field.label for s in steps}
                lines = "\n".join(f"• {html.escape(labels.get(k, k))}: {html.escape(v)}" for k, v in diff.items())
                await self.send(_cut(f"✋ Cancelled {title}: the form changed after the preview.\n{lines}\n"
                                     "Nothing was sent. Check the window and press Submit there if it is right."))
                return
            outcome = await submit(run.page)
            try:
                await run.page.screenshot(path=str(self.audit_dir / str(app_id) / "after-submit.png"), full_page=True)
            except Exception:  # noqa: BLE001 - the audit shot is a nicety
                pass
        except Exception as e:  # noqa: BLE001
            log.exception("submit failed for application %d", app_id)
            outcome = f"error: {type(e).__name__}"
        if outcome == "submitted":
            self.apps.move(app_id, {"submitting"}, "submitted")
            await self.send(f"✅ Sent: {title}. I saw the confirmation page.")
        elif outcome == "no-button":
            self.apps.move(app_id, {"submitting"}, "handed_over")
            await self.send(f"I couldn't find the Submit button on {title}. Nothing was sent; it's open on your Mac.")
        else:
            self.apps.move(app_id, {"submitting"}, "needs_check")
            await self.send(f"⚠️ I clicked Submit on {title} but saw no confirmation ({html.escape(outcome)}). "
                            "Check the window. I won't click again.")

    # ------------------------------------------------------------------ learning from the window (FA-11)

    async def learn(self, app_id: int, *, confirm_guesses: bool) -> list[tuple[str, str]]:
        """Read what the owner chose or typed in the window and save it as answers for next time.
        Guesses are saved only when the owner confirms them; a sensitive answer the rules gave is
        saved only when the owner changed it."""
        run = self.runs.get(app_id)
        if not run:
            return []
        try:
            vault = Vault.load(self.vault_dir)
        except VaultError:
            return []
        learned = []
        for s in run.steps:
            if s.source in ("legal", "essay", "identity", "mapped") or s.field.kind == "file" or s.field.name in run.earlier:
                continue
            try:
                now = await read(run.page, s)
            except Exception:  # noqa: BLE001 - the window may be closed
                continue
            before = run.shown.get(s.field.name)
            if not now or (now == before and not (s.source == "guessed" and confirm_guesses)):
                continue
            vault.save_answer(s.field.label, now)
            learned.append((s.field.label, now))
        return learned

    async def _learn_and_tell(self, app_id: int, *, confirm_guesses: bool) -> None:
        learned = await self.learn(app_id, confirm_guesses=confirm_guesses)
        if learned:
            lines = "\n".join(f"• {html.escape(_cut(q, 90))} → <b>{html.escape(_cut(a, 60))}</b>" for q, a in learned)
            await self.send(_cut(f"📚 Learned {len(learned)} answer(s) from the window; next time I fill them:\n{lines}"))

    async def command(self, cmd: str, arg: str, busy: str | None = None) -> str:
        """/answer, /applications, /learn."""
        if cmd == "/answer":
            return await self.answer(arg)
        if cmd == "/applications":
            return self.status(busy)
        if cmd == "/signin":
            if not arg.startswith("http"):
                return "Use: /signin https://devpost.com (any site you apply through)"
            await self.browser.open(arg)
            return ("🔑 Opened in the agent's Chrome on your Mac. Sign in there once; it remembers you, "
                    "so forms on that site fill without stopping.")
        if cmd == "/retry":
            if not self.waiting:
                return "Nothing is waiting."
            app_id = max(self.waiting)  # the latest page waiting for the owner
            url, mode, page = self.waiting.pop(app_id)
            await self._retry(app_id, url, mode, page)
            return "🔄 Done."
        if cmd == "/learn":
            if not self.runs:
                return "No open form to learn from. Tap ⚡ Apply first."
            app_id = max(self.runs)
            learned = await self.learn(app_id, confirm_guesses=True)
            if not learned:
                return "Nothing new in the window to learn."
            lines = "\n".join(f"• {html.escape(_cut(q, 90))} → <b>{html.escape(_cut(a, 60))}</b>" for q, a in learned)
            return _cut(f"📚 Learned {len(learned)} answer(s); next time I fill them:\n{lines}")
        return ""

    # ------------------------------------------------------------------ /applications

    def status(self, busy: str | None = None) -> str:
        """What each recent application is doing, in plain words (APP-7)."""
        rows = self.apps.recent()
        if not rows and not busy:
            return "No applications yet. Tap ⚡ Apply under a job."
        lines = ["<b>Your applications</b>"]
        if busy:
            lines.append(f"⏳ Filling now: {html.escape(busy)}")
        for r in rows:
            mode = {"partial": "help", "dry": "dry run", "fast": "fast apply"}.get(r["mode"], r["mode"])
            lines.append(f"{STATE_WORDS.get(r['state'], r['state'])} · <b>{html.escape(r['title'])}</b> · "
                         f"{html.escape(r['company'])} ({mode}, {r['created_at'][:10]})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ /answer (VLT-4)

    async def answer(self, text: str) -> str:
        last = self.apps.kv_get(CHECKLIST_KEY)
        if not last:
            return "No checklist to answer yet. Tap ⚡ Apply on a job first."
        m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", text, re.S)
        if not m:
            return "Use: /answer &lt;number&gt; &lt;your answer&gt;, e.g. <code>/answer 3 No</code>"
        n, value = int(m.group(1)), m.group(2)
        item = next((i for i in last["items"] if i["n"] == n), None)
        if item is None:
            return f"There is no question {n} in the last checklist."
        if item["source"] == "legal":
            return "That is a legal statement: only you can agree to it, on the page. I don't store agreements."
        try:
            vault = Vault.load(self.vault_dir)
        except VaultError as e:
            return f"Your vault can't be read: {html.escape(str(e))}"
        vault.save_answer(item["label"], value)
        return (f"Saved for “{html.escape(item['label'])}”: <b>{html.escape(value)}</b>. "
                "Next time a form asks this, I fill it.")


def _optional(item: dict[str, Any]) -> str:
    return "" if item["required"] or "optional" in item["label"].lower() else " (optional)"


def _reason(s: Step, failed: str | None) -> str:
    if failed:
        return f"I couldn't fill it: {failed}"
    if s.source == "essay":
        if s.reason.startswith("essay: the draft failed"):
            return s.reason
        limit = limit_of(s.field.label, s.field.description)
        stated = re.search(r"\d", f"{s.field.label} {s.field.description}")
        return f"essay ({limit.n} {limit.unit} max)" if stated else "essay"
    if s.source == "sensitive":
        return "sensitive: I fill it only from an answer you saved"
    if s.source == "legal":
        return "a legal statement: only you can agree to it"
    return s.reason or "not in your vault"
