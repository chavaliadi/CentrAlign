"""Full pipeline, real environment, scripted agent. No API key needed.

This is the test that says how much of the system is actually proven. It runs
the real controller against the real AcmeOps over a real Chromium: real login,
real PDF download and extraction, a real write through the form, the real
approval gate, and the real state read the verifier uses. The ONLY thing
stubbed is the model's judgement - the choice of which action to take next.

So if this passes, every moving part except the model's decision-making is
known to work end to end. That is a precise and honest claim, and it is the
one I would make in an interview rather than "it works".

Run:  python -m tests.test_end_to_end_scripted
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent import loop as loop_module  # noqa: E402
from agent.config import SETTINGS  # noqa: E402
from agent.loop import TaskWorker  # noqa: E402
from agent.planner import OutcomeContract  # noqa: E402
from agent.verifier import Verdict  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASSED: list[str] = []
FAILED: list[str] = []

# The authoritative facts of the seeded world. INV-1067 is the most recently
# issued Northwind invoice; its total payable (7390.40) appears only on the
# PDF, while the portal list shows the 6843.00 subtotal.
LATEST_INVOICE = "INV-1067"
TOTAL_PAYABLE = 7390.40
SUBTOTAL_TRAP = 6843.00
DUE_DATE = "2026-10-28"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def api(path: str, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(SETTINGS.base_url + path, method=method)
    with OPENER.open(request, timeout=15) as response:
        body = response.read().decode()
    return json.loads(body) if body.strip() else {}


def read_state() -> dict[str, Any]:
    return {
        "payables_system": api("/api/payables"),
        "vendor_invoices": api("/api/invoices"),
    }


CONTRACT = OutcomeContract(
    goal="a payables record exists for the latest Northwind invoice with its tax-inclusive total",
    success_criteria=[
        f"a payables row exists for {LATEST_INVOICE}",
        f"its amount equals the total payable on the invoice PDF ({TOTAL_PAYABLE})",
        f"its due date equals {DUE_DATE}",
    ],
    required_evidence=["the payables row", "the PDF the total was read from"],
    requires_write=True,
    systems_of_record=["payables"],
)

# What a competent agent would do. Note it addresses everything semantically -
# field names and link text - never a CSS selector or a row position.
SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("browser_open", {"url": "/portal"}),
    ("browser_fill_form", {
        "values": {"email": "ap.clerk@acme.example", "password": "Vendor#2026"},
        "submit": True,
    }),
    ("browser_open", {"url": "/portal?vendor=Northwind+Traders"}),
    ("browser_open", {"url": f"/portal/invoice/{LATEST_INVOICE}"}),
    ("browser_download", {"url": f"/portal/invoice/{LATEST_INVOICE}/pdf"}),
    ("read_pdf", {"path": "RUNTIME_PDF_PATH"}),
    ("remember", {
        "key": "invoice.total_payable",
        "value": str(TOTAL_PAYABLE),
        "source": f"/portal/invoice/{LATEST_INVOICE}/pdf",
        "note": "tax-inclusive total from the PDF, not the list subtotal",
    }),
    ("browser_screenshot", {"label": "authoritative total on the invoice PDF page"}),
    ("browser_open", {"url": "/payables/new"}),
    ("browser_fill_form", {
        "values": {
            "vendor": "Northwind Traders",
            "invoice_number": LATEST_INVOICE,
            "amount": f"{TOTAL_PAYABLE:.2f}",
            "due_date": DUE_DATE,
        },
        "submit": True,
    }),
    ("browser_screenshot", {"label": "payables table after the write"}),
    ("finish_task", {
        "outcome": f"Entered {LATEST_INVOICE} into payables at the PDF's total payable.",
        "answer": f"USD {TOTAL_PAYABLE:.2f}, due {DUE_DATE}",
        "evidence": "/payables",
    }),
]


def deterministic_verify(contract: Any, declared: dict[str, Any], reader: Any) -> Verdict:
    """Stands in for the model verifier, checking the same thing mechanically.

    It reads state through the SAME path the real verifier uses, so what is
    being proven here is the plumbing - that state reaches verification and a
    verdict steers the controller - not the model's comparison.
    """
    state = reader()
    rows = state["payables_system"]["payables"]
    match = [r for r in rows if r["invoice_number"] == LATEST_INVOICE]
    criteria = [
        {"criterion": "row exists", "met": len(match) == 1,
         "evidence": f"{len(match)} row(s) for {LATEST_INVOICE}"},
        {"criterion": "amount is the total payable", "met": bool(match)
         and abs(match[0]["amount"] - TOTAL_PAYABLE) < 0.005,
         "evidence": str(match[0]["amount"]) if match else "no row"},
        {"criterion": "due date correct", "met": bool(match)
         and match[0]["due_date"] == DUE_DATE,
         "evidence": match[0]["due_date"] if match else "no row"},
    ]
    met = all(c["met"] for c in criteria)
    return Verdict(
        "confirmed" if met else "refuted",
        criteria,
        [] if met else ["state does not match the claim"],
        "checked mechanically against the payables store",
        "" if met else "correct the payables row",
        state,
    )


def run() -> int:
    try:
        health = api("/healthz")
    except Exception as exc:  # noqa: BLE001
        print(
            f"AcmeOps is not running at {SETTINGS.base_url} ({type(exc).__name__}).\n"
            "This suite drives the real environment, so start it first:\n\n"
            "    ACMEOPS_CHAOS=off python -m uvicorn acmeops.app:app --port 8099 &\n",
            file=sys.stderr,
        )
        return 2
    if health.get("chaos", {}).get("enabled"):
        print(
            "This suite follows a fixed script, so it needs failure injection off -\n"
            "a script cannot re-authenticate after an injected session expiry.\n"
            "Recovery under chaos is covered by test_loop_offline instead.\n\n"
            "    kill %1\n"
            "    ACMEOPS_CHAOS=off python -m uvicorn acmeops.app:app --port 8099 &",
            file=sys.stderr,
        )
        return 2

    api("/admin/reset?profile=clean", method="POST")

    saved = {
        "understand": loop_module.understand,
        "make_plan": loop_module.make_plan,
        "next_action": loop_module.next_action,
        "verify": loop_module.verify,
        "auto": SETTINGS.auto_approve,
    }
    SETTINGS.auto_approve = True

    index = {"i": 0}
    pdf_path = {"path": ""}

    def scripted_next_action(*_args: Any, **kwargs: Any) -> tuple[str, dict[str, Any], str]:
        # Assert the wiring fix stays fixed: the executor must be handed the
        # environment notes every turn, credentials included.
        notes = kwargs.get("environment_notes", "")
        if "ap.clerk@acme.example" not in notes:
            raise AssertionError("environment notes did not reach the executor")
        i = index["i"]
        if i >= len(SCRIPT):
            return "finish_task", {"outcome": "script exhausted", "blocked": True}, ""
        index["i"] += 1
        tool, arguments = SCRIPT[i]
        arguments = dict(arguments)
        if arguments.get("path") == "RUNTIME_PDF_PATH":
            arguments["path"] = pdf_path["path"]
        return tool, arguments, "scripted"

    loop_module.understand = lambda task, notes: CONTRACT
    loop_module.make_plan = lambda *a, **k: {"steps": [], "risks": []}
    loop_module.next_action = scripted_next_action
    loop_module.verify = deterministic_verify

    worker = TaskWorker(
        task_id="e2e",
        task="Find the latest invoice from Northwind Traders and enter it into payables.",
        environment_notes=(
            "AcmeOps at " + SETTINGS.base_url + ". Portal at /portal requires sign-in. "
            "Payables at /payables is the system of record. Sandbox credentials: "
            "email ap.clerk@acme.example, password Vendor#2026. Policy: record the "
            "tax-inclusive total payable as printed on the invoice document."
        ),
        read_state=read_state,
        responder=lambda q, c: "",
    )

    try:
        # Capture the downloaded PDF path so the script can read it back.
        original_invoke = worker._invoke  # noqa: SLF001

        def invoke(tool_name: str, arguments: dict[str, Any]):
            observation = original_invoke(tool_name, arguments)
            if tool_name == "browser_download" and observation.ok:
                pdf_path["path"] = observation.data.get("path", "")
            return observation

        worker._invoke = invoke  # type: ignore[method-assign]  # noqa: SLF001
        result = worker.run()
    finally:
        loop_module.understand = saved["understand"]
        loop_module.make_plan = saved["make_plan"]
        loop_module.next_action = saved["next_action"]
        loop_module.verify = saved["verify"]
        SETTINGS.auto_approve = saved["auto"]

    print()
    check("the run completed", result.status == "completed", result.status)
    check("verification confirmed it", result.verification.get("verdict") == "confirmed",
          str(result.verification.get("reason")))

    rows = api("/api/payables")["payables"]
    match = [r for r in rows if r["invoice_number"] == LATEST_INVOICE]
    check("exactly one payables row was created, no duplicate", len(match) == 1, str(rows))
    check("the persisted amount is the PDF total, not the list subtotal",
          bool(match) and abs(match[0]["amount"] - TOTAL_PAYABLE) < 0.005,
          f"got {match[0]['amount'] if match else 'nothing'}, trap was {SUBTOTAL_TRAP}")
    check("the persisted due date is correct",
          bool(match) and match[0]["due_date"] == DUE_DATE,
          match[0]["due_date"] if match else "no row")
    check("the write is attributed in the audit log",
          any(a["action"] == "payable.create" for a in api("/api/payables")["audit"]),
          "no audit row")

    run_dir = Path(result.run_dir)
    check("an evidence folder was written", run_dir.exists(), str(run_dir))
    check("report.md exists", (run_dir / "report.md").exists(), "missing")
    check("trace.json exists", (run_dir / "trace.json").exists(), "missing")
    shots = list((run_dir / "screenshots").glob("*.png"))
    check("screenshots were captured as evidence", len(shots) >= 2, str(len(shots)))
    pdfs = list((run_dir / "downloads").glob("*.pdf"))
    check("the invoice PDF was retained as evidence", len(pdfs) == 1, str(pdfs))
    check("and named after the invoice, not the URL's last segment",
          bool(pdfs) and pdfs[0].name == f"{LATEST_INVOICE}.pdf",
          pdfs[0].name if pdfs else "none")

    report = (run_dir / "report.md").read_text()
    check("the report states the answer", f"{TOTAL_PAYABLE:.2f}" in report, "answer missing")
    check("the report shows the verification table", "| Criterion |" in report, "table missing")
    check("the report records that approval was sought for the write",
          "Approvals requested" in report, "approvals section missing")

    trace = json.loads((run_dir / "trace.json").read_text())
    approvals = [e for e in trace["events"] if e["kind"] == "approval"]
    check("every mutating action went through the approval gate",
          len(approvals) == 2, f"{len(approvals)} approvals for 2 form submissions")
    check("approvals are recorded as auto-granted, not silently skipped",
          all(a.get("auto") for a in approvals), str(approvals))
    facts = trace.get("memory", {}).get("facts", {})
    check("the established total is in working memory with its provenance",
          "invoice.total_payable" in facts
          and "pdf" in facts["invoice.total_payable"]["source"].lower(),
          str(facts))

    print("\n" + "=" * 60)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    print(f"\nevidence folder: {result.run_dir}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())
