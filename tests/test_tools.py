"""Tool-level tests against a live AcmeOps. No model and no API key needed.

These check the claims the architecture rests on:

* the semantic page digest exposes form fields by ``name`` and table rows by
  their keys, in BOTH layout variants - which is why layout shift is survivable;
* authenticated download plus PDF extraction reaches the authoritative total,
  and that total differs from the subtotal shown in the list view;
* a write through the form actually lands in the system of record.

Run:  python -m tests.test_tools
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.config import SETTINGS  # noqa: E402
from agent.tools.base import ToolRegistry  # noqa: E402
from agent.tools.browser import BrowserSession, register_browser_tools  # noqa: E402
from agent.tools.files import register_file_tools  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def api(path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(SETTINGS.base_url + path, method=method)
    with OPENER.open(request, timeout=10) as response:
        body = response.read().decode()
    return json.loads(body) if body.strip() else {}


def run() -> int:
    # This suite asserts exact page contents, so it needs a deterministic
    # environment. With chaos on, an injected 503 makes a tool return no page
    # and the failure looks like a code bug rather than a misconfigured run.
    try:
        health = api("/healthz")
    except Exception as exc:  # noqa: BLE001
        print(
            f"AcmeOps is not running at {SETTINGS.base_url} ({type(exc).__name__}).\n"
            "Start it with:\n\n"
            "    ACMEOPS_CHAOS=off python -m uvicorn acmeops.app:app --port 8099 &\n",
            file=sys.stderr,
        )
        return 2
    if health.get("chaos", {}).get("enabled"):
        print(
            "This suite needs failure injection disabled, because it asserts exact page\n"
            "contents and an injected 503 would look like a code failure.\n\n"
            "Restart the environment with chaos off:\n\n"
            "    kill %1\n"
            "    ACMEOPS_CHAOS=off python -m uvicorn acmeops.app:app --port 8099 &\n\n"
            "(Chaos belongs in the agent runs, where recovery is the point, not here.)",
            file=sys.stderr,
        )
        return 2

    api("/admin/reset", method="POST")
    tmp = REPO_ROOT / "runs" / "_toolcheck"
    session = BrowserSession(tmp / "downloads", tmp / "screenshots")
    session.start()
    registry = ToolRegistry()
    register_browser_tools(registry, session)
    register_file_tools(registry)

    try:
        call = lambda name, **kw: registry.get(name).fn(**kw)  # noqa: E731

        print("\n[1] login flow")
        obs = call("browser_open", url="/portal")
        check("portal redirects to login when unauthenticated",
              "sign in" in json.dumps(obs.data).lower(), obs.summary[:120])

        digest = obs.data.get("page") or {}
        if not digest:
            check("login page returned a readable page", False, obs.summary[:160])
            raise SystemExit(1)
        field_names = [f["name"] for f in digest["forms"][0]["fields"]]
        check("login form exposes email and password by name",
              set(field_names) >= {"email", "password"}, str(field_names))

        obs = call("browser_fill_form",
                   values={"email": "ap.clerk@acme.example", "password": "Vendor#2026"},
                   submit=True)
        check("login submits and lands on the invoice list",
              obs.ok and "invoice" in json.dumps(obs.data).lower(), obs.summary[:160])

        print("\n[2] semantic digest of the invoice list")
        obs = call("browser_open", url="/portal?vendor=Northwind+Traders")
        page = obs.data["page"]
        table = page["tables"][0]
        row_keys = [r["row_key"] for r in table["rows"]]
        check("rows are addressable by invoice number regardless of column order",
              {"INV-1031", "INV-1042", "INV-1067"} <= set(row_keys), str(row_keys))
        check("list view is explicitly labelled as pre-tax subtotal",
              any("subtotal" in h.lower() for h in table["headers"]), str(table["headers"]))

        # The model only ever sees Observation.summary, so whatever is missing
        # from it the agent is blind to. This is the regression that matters
        # most: a one-line summary had the agent re-reading pages forever.
        summary = obs.summary
        check("the summary the MODEL sees contains the invoice numbers",
              all(n in summary for n in ("INV-1031", "INV-1042", "INV-1067")), summary[:300])
        check("...their due dates", "2026-10-28" in summary, summary[:300])
        check("...their amounts", "6843.00" in summary, summary[:300])
        check("...and the link target for each row",
              "/portal/invoice/INV-1067" in summary, summary[:300])

        obs_form = call("browser_open", url="/payables/new")
        check("a form page's summary names every field the agent must fill",
              all(f"name={n!r}" in obs_form.summary
                  for n in ("vendor", "invoice_number", "amount", "due_date")),
              obs_form.summary[:400])
        check("and the submit button labels",
              "submit button labels" in obs_form.summary, obs_form.summary[:400])

        obs_detail = call("browser_open", url="/portal/invoice/INV-1067")
        check("a detail page's summary carries the semantic field values",
              "due_date='2026-10-28'" in obs_detail.summary, obs_detail.summary[:400])

        rereads = call("browser_read_page")
        check("re-reading an unchanged page says so instead of resending it",
              "identical to the one you have already read" in rereads.summary,
              rereads.summary[:200])

        print("\n[3] both layout variants stay readable")
        shapes = {}
        for variant in ("a", "b"):
            obs = call("browser_open", url=f"/portal/invoice/INV-1067?layout={variant}")
            page = obs.data["page"]
            labelled = {lv["field"]: lv["value"] for lv in page["labelled_values"]}
            shapes[variant] = (
                len(page["tables"]),
                len(page["definition_lists"]),
                [f["label"] for f in page["forms"][0]["fields"]] if page["forms"] else [],
            )
            check(f"variant {variant}: due date reachable by semantic field name",
                  labelled.get("due_date") == "2026-10-28", str(labelled))
            check(f"variant {variant}: web view withholds the total payable",
                  "pdf" in (labelled.get("amount_note", "")).lower(), str(labelled))
        check("the two variants really are structurally different markup",
              shapes["a"] != shapes["b"], f"a={shapes['a']} b={shapes['b']}")

        # Same assertion for the write form, where variant B reorders the fields
        # and renames the labels. Field `name` attributes are what stay stable.
        form_fields = {}
        for variant in ("a", "b"):
            obs = call("browser_open", url=f"/payables/new?layout={variant}")
            fields = obs.data["page"]["forms"][0]["fields"]
            form_fields[variant] = [(f["name"], f["label"]) for f in fields]
        check("field order and labels differ between variants",
              form_fields["a"] != form_fields["b"],
              f"a={form_fields['a']} b={form_fields['b']}")
        check("but the set of field names is identical in both",
              {n for n, _ in form_fields["a"]} == {n for n, _ in form_fields["b"]},
              f"a={form_fields['a']} b={form_fields['b']}")

        print("\n[4] authoritative total comes from the PDF, not the page")
        obs = call("browser_download", url="/portal/invoice/INV-1067/pdf")
        check("authenticated PDF download succeeds", obs.ok, obs.error)
        pdf_path = obs.data.get("path", "")
        obs = call("read_pdf", path=pdf_path)
        check("PDF text extracted", obs.ok, obs.error)
        text = obs.data.get("text", "")
        check("PDF carries the TOTAL PAYABLE line", "TOTAL PAYABLE" in text, text[:160])
        check("PDF total (7,390.40) differs from the subtotal shown in the list (6,843.00)",
              "7,390.40" in text and "6,843.00" in text, text[-400:])

        print("\n[5] a write through the UI lands in the system of record")
        before = len(api("/api/payables")["payables"])
        call("browser_open", url="/payables/new")
        obs = call("browser_fill_form",
                   values={
                       "vendor": "Northwind Traders",
                       "invoice_number": "INV-1067",
                       "amount": "7390.40",
                       "due_date": "2026-10-28",
                   },
                   submit=True)
        check("form submitted", obs.ok, obs.error)
        payables = api("/api/payables")["payables"]
        check("record count increased by one", len(payables) == before + 1,
              f"{before} -> {len(payables)}")
        created = payables[-1]
        check("persisted amount is the authoritative total",
              abs(created["amount"] - 7390.40) < 0.005, str(created))
        check("write is attributed in the audit log",
              any(a["action"] == "payable.create" for a in api("/api/payables")["audit"]),
              "no audit row")

        print("\n[6] a submit label that does not match is survivable, not fatal")
        # Variant B labels the login button "Continue", variant A "Sign in".
        # An agent carrying the wrong label must still get the form submitted.
        api("/admin/reset", method="POST")
        call("browser_open", url="/portal/login?layout=b")
        page = call("browser_read_page").data["page"]
        check("variant B really does use a different submit label",
              "Continue" in page["forms"][0]["submit_labels"],
              str(page["forms"][0]["submit_labels"]))
        obs = call("browser_fill_form",
                   values={"email": "ap.clerk@acme.example", "password": "Vendor#2026"},
                   submit=True, submit_label="Sign In")   # deliberately wrong for this layout
        check("submitted anyway via the form's own button", obs.ok, obs.error)
        check("the mismatch is reported back with the real labels",
              "Labels actually present" in obs.summary, obs.summary[:200])
        check("and the login actually succeeded",
              "invoice" in json.dumps(obs.data).lower(), obs.summary[:200])

        print("\n[7] unknown field fails loudly instead of silently")
        call("browser_open", url="/payables/new")
        obs = call("browser_fill_form", values={"nonexistent_field": "x"})
        check("missing field reported with the available names",
              (not obs.ok) and "available" in obs.summary.lower(), obs.summary[:160])
        check("that failure is marked non-retryable", not obs.retryable, "marked retryable")

    finally:
        session.stop()

    print("\n" + "=" * 60)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())
