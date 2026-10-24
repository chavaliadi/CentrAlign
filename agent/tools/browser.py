"""Browser tools, backed by a real Chromium through Playwright.

The important design decision lives in :func:`page_digest`. The agent is never
given raw HTML and never asked to invent CSS selectors. It gets a semantic
digest of the page - headings, tables as rows, form fields with their labels
and ``name`` attributes, links with their text - and it acts on those semantic
handles ("fill the field named ``due_date``", "click the link whose text is
``INV-1067``").

That is what makes the layout-shift chaos survivable: when AcmeOps renders its
variant B - columns reordered, labels reworded, fields in a different order -
the digest changes shape but the semantic handles still resolve, so the agent
adapts without any selector list to update.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from ..config import SETTINGS
from .base import Observation, ToolRegistry, number, obj_schema, string

# JavaScript that walks the rendered DOM and returns a semantic summary.
DIGEST_JS = r"""
() => {
  const text = (el) => (el ? (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ') : '');

  const labelFor = (input) => {
    if (input.id) {
      const l = document.querySelector(`label[for="${input.id}"]`);
      if (l) return text(l);
    }
    const wrap = input.closest('label');
    if (wrap) return text(wrap);
    const prev = input.previousElementSibling;
    if (prev && prev.tagName === 'LABEL') return text(prev);
    return '';
  };

  const forms = [...document.querySelectorAll('form')].map((form) => ({
    id: form.id || null,
    action: form.getAttribute('action'),
    method: (form.getAttribute('method') || 'get').toUpperCase(),
    fields: [...form.querySelectorAll('input, select, textarea')]
      .filter((el) => el.type !== 'hidden')
      .map((el) => ({
        name: el.getAttribute('name'),
        label: labelFor(el),
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || el.tagName.toLowerCase(),
        value: el.value || '',
        required: el.required || false,
        options: el.tagName === 'SELECT' ? [...el.options].map((o) => o.value) : undefined,
      })),
    submit_labels: [...form.querySelectorAll('button, input[type=submit]')].map(
      (b) => text(b) || b.value || 'submit'
    ),
  }));

  const tables = [...document.querySelectorAll('table')].map((t) => ({
    id: t.id || null,
    headers: [...t.querySelectorAll('thead th, tr:first-child th')].map(text),
    rows: [...t.querySelectorAll('tbody tr')].slice(0, 60).map((tr) => ({
      cells: [...tr.querySelectorAll('td, th')].map(text),
      row_key: tr.dataset.invoice || tr.dataset.payable || null,
      links: [...tr.querySelectorAll('a')].map((a) => ({ text: text(a), href: a.getAttribute('href') })),
    })),
  }));

  // Definition lists carry the same information as detail tables in variant B.
  const definition_lists = [...document.querySelectorAll('dl')].map((dl) => {
    const pairs = [];
    let key = null;
    [...dl.children].forEach((child) => {
      if (child.tagName === 'DT') key = text(child);
      else if (child.tagName === 'DD') pairs.push({ label: key, value: text(child) });
    });
    return { id: dl.id || null, pairs };
  });

  const labelled_values = [...document.querySelectorAll('[data-field]')].map((el) => ({
    field: el.dataset.field,
    value: text(el),
  }));

  return {
    url: location.href,
    title: document.title,
    headings: [...document.querySelectorAll('h1, h2')].map(text).filter(Boolean),
    notices: [...document.querySelectorAll('.notice, #form-error, #login-message, #err')].map(text).filter(Boolean),
    links: [...document.querySelectorAll('a')]
      .map((a) => ({ text: text(a), href: a.getAttribute('href') }))
      .filter((l) => l.text && l.href)
      .slice(0, 80),
    forms,
    tables,
    definition_lists,
    labelled_values,
    body_text: (document.body.innerText || '').trim().replace(/\n{3,}/g, '\n\n').slice(0, 2500),
  };
}
"""


class BrowserSession:
    """One Chromium context for the whole run, so cookies and login state
    persist across steps exactly as they would for a person."""

    def __init__(self, downloads_dir: Path, screenshots_dir: Path) -> None:
        self.downloads_dir = downloads_dir
        self.screenshots_dir = screenshots_dir
        downloads_dir.mkdir(parents=True, exist_ok=True)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._shot_counter = 0
        self.last_status: int | None = None
        # Remembered so a re-read of an unchanged page can say so in one line
        # instead of re-sending the whole page and inviting another re-read.
        self.last_digest_signature: str | None = None

    def start(self) -> None:
        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": SETTINGS.headless}
        if Path(SETTINGS.browser_executable).exists():
            launch_kwargs["executable_path"] = SETTINGS.browser_executable
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._context.set_default_timeout(SETTINGS.nav_timeout_ms)
        self.page = self._context.new_page()

    def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001 - teardown must never mask a result
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # --- helpers -------------------------------------------------------------

    def digest(self) -> dict[str, Any]:
        return self.page.evaluate(DIGEST_JS)

    def digest_signature(self, digest: dict[str, Any]) -> str:
        import hashlib
        import json as _json

        return hashlib.sha1(
            _json.dumps(digest, sort_keys=True, default=str).encode()
        ).hexdigest()

    def screenshot(self, label: str) -> str:
        self._shot_counter += 1
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "step"
        path = self.screenshots_dir / f"{self._shot_counter:02d}-{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except PlaywrightError:
            return ""
        return str(path)

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _summarise(digest: dict[str, Any], *, max_rows: int = 20, max_links: int = 12) -> str:
    """Render the page digest as the text the MODEL actually reads.

    This function is load-bearing in a way that is easy to miss. The controller
    passes only ``Observation.summary`` into the planner's context - the
    structured ``data`` never reaches the model. So whatever this leaves out,
    the agent cannot see.

    An earlier version of this returned a one-line gist ("7 table row(s)"),
    which meant the agent was blind to every invoice number, amount and due
    date on the page. It responded by re-reading the same page over and over
    and then guessing at link text, which is exactly what you would do if
    someone described a table to you without reading out any of it. Rendering
    the rows, the fields and the links is therefore not a nicety - it is the
    difference between an agent that can work and one that cannot.
    """
    out: list[str] = [f"{digest.get('title') or 'untitled'} <{digest.get('url')}>"]

    if digest.get("notices"):
        out.append("NOTICES: " + " | ".join(digest["notices"]))
    if digest.get("headings"):
        out.append("HEADINGS: " + " / ".join(digest["headings"][:4]))

    for table in digest.get("tables") or []:
        rows = table.get("rows") or []
        label = f"TABLE{' #' + table['id'] if table.get('id') else ''} ({len(rows)} row(s))"
        out.append(label)
        if table.get("headers"):
            out.append("  columns: " + " | ".join(table["headers"]))
        for row in rows[:max_rows]:
            cells = " | ".join(row.get("cells") or [])
            key = f"[{row['row_key']}] " if row.get("row_key") else ""
            line = f"  {key}{cells}"
            for link in row.get("links") or []:
                line += f"   (link '{link['text']}' -> {link['href']})"
            out.append(line)
        if len(rows) > max_rows:
            out.append(f"  ... and {len(rows) - max_rows} more row(s)")

    for dl in digest.get("definition_lists") or []:
        pairs = dl.get("pairs") or []
        if not pairs:
            continue
        out.append(f"DETAILS{' #' + dl['id'] if dl.get('id') else ''}")
        for pair in pairs:
            out.append(f"  {pair.get('label')}: {pair.get('value')}")

    if digest.get("labelled_values"):
        out.append(
            "SEMANTIC FIELDS: "
            + ", ".join(
                f"{lv['field']}={lv['value']!r}" for lv in digest["labelled_values"]
            )
        )

    for form in digest.get("forms") or []:
        out.append(
            f"FORM{' #' + form['id'] if form.get('id') else ''} "
            f"{form.get('method', 'GET')} {form.get('action')}"
        )
        for field in form.get("fields") or []:
            extra = ""
            if field.get("options"):
                extra += f" options={field['options']}"
            if field.get("value"):
                extra += f" current={field['value']!r}"
            if field.get("required"):
                extra += " required"
            out.append(
                f"  name={field.get('name')!r} label={field.get('label')!r} "
                f"type={field.get('type')}{extra}"
            )
        if form.get("submit_labels"):
            out.append(f"  submit button labels: {form['submit_labels']}")

    links = digest.get("links") or []
    if links:
        shown = links[:max_links]
        out.append(
            "LINKS: " + "; ".join(f"'{l['text']}' -> {l['href']}" for l in shown)
        )
        if len(links) > max_links:
            out.append(f"  ... and {len(links) - max_links} more link(s)")

    # Only fall back to raw body text when there was no structure to render.
    if not any(digest.get(k) for k in ("tables", "forms", "definition_lists")):
        body = (digest.get("body_text") or "").strip()
        if body:
            out.append("TEXT: " + body[:800])

    return "\n".join(out)


def register_browser_tools(registry: ToolRegistry, session: BrowserSession) -> None:
    base = SETTINGS.base_url.rstrip("/")

    def absolute(url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return base + ("" if url.startswith("/") else "/") + url

    @registry.tool(
        "browser_open",
        "Navigate the browser to a URL (absolute, or a path like /portal) and return a "
        "semantic digest of the page that loaded: headings, notices, tables with their "
        "rows, form fields with labels and names, and links.",
        obj_schema({"url": string("Absolute URL or site-relative path")}, ["url"]),
    )
    def browser_open(url: str) -> Observation:  # type: ignore[misc]
        target = absolute(url)
        try:
            response = session.page.goto(target, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            return Observation.failure(f"timed out loading {target}", retryable=True)
        except PlaywrightError as exc:
            return Observation.failure(f"navigation error for {target}: {exc}", retryable=True)

        status = response.status if response else 0
        session.last_status = status
        digest = session.digest()

        if status >= 500:
            return Observation(
                ok=False,
                summary=f"HTTP {status} from {target}. Server-side transient error; retrying is reasonable.",
                data={"status": status, "url": digest.get("url")},
                error=f"HTTP {status}",
                retryable=True,
            )
        if status >= 400:
            return Observation(
                ok=False,
                summary=f"HTTP {status} from {target}: {digest.get('body_text', '')[:200]}",
                data={"status": status},
                error=f"HTTP {status}",
                retryable=False,
            )

        session.last_digest_signature = session.digest_signature(digest)
        return Observation(
            ok=True,
            summary=f"Loaded (HTTP {status}): " + _summarise(digest),
            data={"status": status, "page": digest},
        )

    @registry.tool(
        "browser_read_page",
        "Re-read the page currently open in the browser. Only useful when the page may have "
        "changed since your last observation - navigation and form submission already return "
        "the full page, so calling this straight afterwards returns nothing new.",
        obj_schema({}),
    )
    def browser_read_page() -> Observation:  # type: ignore[misc]
        try:
            digest = session.digest()
        except PlaywrightError as exc:
            return Observation.failure(f"could not read page: {exc}", retryable=True)
        signature = session.digest_signature(digest)
        if signature == session.last_digest_signature:
            # Re-reading an unchanged page is how a run burns its budget. Say
            # so plainly rather than re-sending a page the agent already has.
            return Observation(
                ok=True,
                summary=(
                    "This page is byte-for-byte identical to the one you have already read. "
                    "Nothing has changed, and reading it again will not reveal anything new. "
                    "Act on what you already have: follow one of the links listed in your "
                    "earlier observation, or take the next step toward the goal."
                ),
                data={"page": digest, "unchanged": True},
            )
        session.last_digest_signature = signature
        return Observation(ok=True, summary=_summarise(digest), data={"page": digest})

    @registry.tool(
        "browser_click",
        "Click a link or button identified by its visible text. Matching is "
        "case-insensitive and accepts a partial match. Returns the digest of whatever "
        "page results.",
        obj_schema(
            {
                "text": string("Visible text of the link or button to click"),
                "nth": number("Which match to click if several, 0-based. Defaults to 0."),
            },
            ["text"],
        ),
    )
    def browser_click(text: str, nth: float | int = 0) -> Observation:  # type: ignore[misc]
        index = int(nth or 0)
        try:
            locator = session.page.get_by_text(text, exact=False)
            count = locator.count()
            if count == 0:
                locator = session.page.locator(
                    f"a:has-text('{text}'), button:has-text('{text}')"
                )
                count = locator.count()
            if count == 0:
                digest = session.digest()
                available = [l["text"] for l in digest.get("links", [])][:25]
                return Observation(
                    ok=False,
                    summary=(
                        f"No clickable element matching {text!r}. "
                        f"Clickable text currently on the page: {available}"
                    ),
                    data={"page": digest},
                    error="no match",
                    retryable=False,
                )
            locator.nth(min(index, count - 1)).click()
            session.page.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeout:
            return Observation.failure(f"timed out clicking {text!r}", retryable=True)
        except PlaywrightError as exc:
            return Observation.failure(f"click failed for {text!r}: {exc}", retryable=True)

        digest = session.digest()
        # This observation already carries the full render, so an immediate
        # re-read should be told the page is unchanged rather than handed it
        # again.
        session.last_digest_signature = session.digest_signature(digest)
        return Observation(
            ok=True,
            summary=f"Clicked {text!r}. Now at: " + _summarise(digest),
            data={"page": digest},
        )

    @registry.tool(
        "browser_fill_form",
        "Fill fields in a form on the current page and optionally submit it. Address "
        "fields by their `name` attribute as shown in the page digest, not by position. "
        "Submitting a form writes to the company system, so this tool requires approval.",
        obj_schema(
            {
                "values": {
                    "type": "object",
                    "description": "Map of field name -> value to type or select",
                    "additionalProperties": {"type": "string"},
                },
                "submit": {
                    "type": "boolean",
                    "description": "Whether to submit the form after filling it",
                },
                "submit_label": string("Visible text of the submit button, if submitting"),
            },
            ["values"],
        ),
        mutating=True,
    )
    def browser_fill_form(  # type: ignore[misc]
        values: dict[str, str],
        submit: bool = False,
        submit_label: str = "",
    ) -> Observation:
        filled: list[str] = []
        try:
            for name, value in values.items():
                locator = session.page.locator(f"[name='{name}']")
                if locator.count() == 0:
                    digest = session.digest()
                    names = [
                        f.get("name")
                        for form in digest.get("forms", [])
                        for f in form.get("fields", [])
                    ]
                    return Observation(
                        ok=False,
                        summary=(
                            f"No field named {name!r} on this page. "
                            f"Fields available: {[n for n in names if n]}"
                        ),
                        data={"page": digest},
                        error="unknown field",
                        retryable=False,
                    )
                element = locator.first
                tag = element.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    element.select_option(str(value))
                else:
                    element.fill(str(value))
                filled.append(f"{name}={value}")

            if not submit:
                digest = session.digest()
                return Observation(
                    ok=True,
                    summary=f"Filled {', '.join(filled)} without submitting.",
                    data={"page": digest, "filled": filled},
                )

            # Find the submit control. A label that does not match must not be
            # fatal: the whole point of addressing things semantically is that
            # button text shifts between layouts - variant B says "Continue"
            # where variant A says "Sign in". Falling back to the form's own
            # submit button, and telling the model what the labels actually
            # were, keeps a layout shift survivable instead of burning a
            # fifteen-second timeout on a selector that can never match.
            fallback_note = ""
            submit = None
            if submit_label:
                submit = session.page.locator(
                    f"form button:has-text('{submit_label}'), "
                    f"input[type=submit][value='{submit_label}']"
                )
                if submit.count() == 0:
                    digest = session.digest()
                    labels = [
                        label
                        for form in digest.get("forms", [])
                        for label in form.get("submit_labels", [])
                    ]
                    fallback_note = (
                        f"No submit control labelled {submit_label!r} on this page; used the "
                        f"form's own submit button instead. Labels actually present: {labels}. "
                        "The page layout varies, so prefer omitting submit_label."
                    )
                    submit = None
            if submit is None:
                submit = session.page.locator(
                    "form button[type=submit], form button, form input[type=submit]"
                )
            if submit.count() == 0:
                return Observation(
                    ok=False,
                    summary=(
                        "The fields were filled but this page has no submit control, so nothing "
                        "was submitted. Check you are on the form page you intended."
                    ),
                    data={"page": session.digest(), "filled": filled},
                    error="no submit control",
                )
            try:
                submit.first.click()
                session.page.wait_for_load_state("domcontentloaded")
            except (PlaywrightTimeout, PlaywrightError) as exc:
                # The POST may well have reached the server and committed
                # before the response was lost. Never present this as
                # retryable: resubmitting risks a duplicate record, which is
                # silently wrong data rather than an honest failure.
                return Observation(
                    ok=False,
                    summary=(
                        f"The form was filled ({', '.join(filled)}) and submitted, but the "
                        f"response never arrived ({type(exc).__name__}). The write MAY ALREADY "
                        "HAVE SUCCEEDED. Do not submit it again. Go and read the records in the "
                        "target system first: if the record is there, the write landed and you "
                        "should carry on; only if it is absent should you submit again."
                    ),
                    data={"filled": filled, "submitted": "unknown"},
                    error=f"submit response lost: {exc}",
                    retryable=False,
                )
        except PlaywrightTimeout:
            return Observation.failure("timed out filling or submitting the form", retryable=True)
        except PlaywrightError as exc:
            return Observation.failure(f"form interaction failed: {exc}", retryable=True)

        digest = session.digest()
        session.last_digest_signature = session.digest_signature(digest)
        summary = f"Filled {', '.join(filled)} and submitted. Result page: " + _summarise(digest)
        if fallback_note:
            summary = f"{fallback_note}\n{summary}"
        return Observation(
            ok=True,
            summary=summary,
            data={"page": digest, "filled": filled, "submitted": True},
        )

    @registry.tool(
        "browser_download",
        "Download a file (such as an invoice PDF) using the browser's own session, so "
        "authenticated downloads work. Returns the local path; read it with read_pdf.",
        obj_schema(
            {
                "url": string("Absolute URL or site-relative path of the file"),
                "filename": string("Optional filename to save as"),
            },
            ["url"],
        ),
    )
    def browser_download(url: str, filename: str = "") -> Observation:  # type: ignore[misc]
        target = absolute(url)
        try:
            response = session.page.context.request.get(target)
        except PlaywrightError as exc:
            return Observation.failure(f"download request failed: {exc}", retryable=True)
        if response.status >= 500:
            return Observation.failure(f"HTTP {response.status} downloading {target}", retryable=True)
        if response.status >= 400:
            return Observation.failure(f"HTTP {response.status} downloading {target}")
        # Name the file after what it IS, not after the URL's last segment.
        # "/portal/invoice/INV-1067/pdf" ends in "pdf", which would have saved
        # the evidence as "pdf.pdf" - useless to a reviewer opening the folder.

        # A download that returns a web page is almost always a redirect to a
        # login screen after the session expired. The request succeeds with
        # HTTP 200, so without this check the HTML gets saved under a .pdf name
        # and the failure only surfaces later as "invalid pdf header" - by
        # which point the agent has lost the thread of what went wrong.
        content_type = (response.headers or {}).get("content-type", "")
        body = response.body()
        looks_like_html = body[:200].lstrip().lower().startswith((b"<!doc", b"<html"))
        if "text/html" in content_type.lower() or looks_like_html:
            return Observation(
                ok=False,
                summary=(
                    f"{target} returned a web page, not a document. You are almost certainly "
                    "signed out - downloads follow the redirect to the login page and succeed "
                    "with HTTP 200. Sign in again, then retry this download."
                ),
                data={"content_type": content_type},
                error="download returned HTML, not a document",
                retryable=False,
            )
        
        if filename:
            name = filename
        else:
            segments = [s for s in target.split("?")[0].rstrip("/").split("/") if s]
            last = segments[-1] if segments else "download"
            if "." in last:
                name = last
            elif len(segments) >= 2:
                name = f"{segments[-2]}.{last}"
            else:
                name = f"{last}.pdf"
        if "." not in name:
            name += ".pdf"
        path = session.downloads_dir / name
        path.write_bytes(response.body())
        return Observation(
            ok=True,
            summary=f"Downloaded {target} to {path} ({path.stat().st_size} bytes).",
            data={"path": str(path), "bytes": path.stat().st_size},
            artifacts=[str(path)],
        )

    @registry.tool(
        "browser_screenshot",
        "Capture a screenshot of the current page as evidence of what was seen or done.",
        obj_schema({"label": string("Short label describing what this shows")}, ["label"]),
    )
    def browser_screenshot(label: str) -> Observation:  # type: ignore[misc]
        path = session.screenshot(label)
        if not path:
            return Observation.failure("screenshot failed")
        return Observation(
            ok=True,
            summary=f"Screenshot saved for evidence: {path}",
            data={"path": path},
            artifacts=[path],
        )
