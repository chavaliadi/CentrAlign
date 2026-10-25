"""Focused invoice verification tests with the verifier model stubbed out."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent import verifier  # noqa: E402
from agent.planner import OutcomeContract  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

CONTRACT = OutcomeContract(
    goal="a payables record exists for INV-1067 from Northwind Traders",
    success_criteria=[
        "a payables row exists for INV-1067",
        "its amount equals the total payable on the invoice PDF (7390.40)",
        "its due date equals 2026-10-28",
    ],
    required_evidence=["the payables row", "the invoice source"],
    requires_write=True,
    systems_of_record=["payables"],
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def state(
    *,
    amount: Any = 7390.40,
    due_date: str = "2026-10-28",
    vendor: str = "Northwind Traders",
    invoice_number: str = "INV-1067",
    extra_payables: list[dict[str, Any]] | None = None,
    invoices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    invoice = {
        "number": "INV-1067",
        "vendor": "Northwind Traders",
        "amount": 7390.40,
        "issued_date": "2026-09-28",
        "due_date": "2026-10-28",
    }
    payable = {
        "id": 1,
        "vendor": vendor,
        "invoice_number": invoice_number,
        "amount": amount,
        "due_date": due_date,
    }
    return {
        "payables_system": {"payables": [payable, *(extra_payables or [])]},
        "vendor_invoices": {"invoices": invoices if invoices is not None else [invoice]},
    }


def model_confirmation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "verdict": "confirmed",
        "criteria": [
            {"criterion": "row exists", "met": True, "evidence": "payable row"},
            {"criterion": "amount", "met": True, "evidence": "7390.40"},
            {"criterion": "due date", "met": True, "evidence": "2026-10-28"},
        ],
        "reason": "model confirmation",
        "what_to_fix": "",
    }


def run_case(name: str, expected: str, snapshot: dict[str, Any]) -> None:
    original = verifier.complete_json
    verifier.complete_json = model_confirmation
    try:
        result = verifier.verify(CONTRACT, {"outcome": "done"}, lambda: snapshot)
    finally:
        verifier.complete_json = original
    check(name, result.verdict == expected, result.as_dict())


def run() -> int:
    print("\n[1] exact invoice state is confirmed")
    run_case("matching invoice is confirmed", "confirmed", state())

    print("\n[2] mismatched persisted fields are rejected")
    run_case("incorrect amount is rejected", "refuted", state(amount=7390.41))
    run_case("incorrect due date is rejected", "refuted", state(due_date="2026-10-29"))
    run_case("incorrect vendor is rejected", "refuted", state(vendor="Globex Corporation"))

    print("\n[3] identity and evidence failures are not accepted")
    run_case("missing invoice is rejected", "refuted", state(invoices=[]))
    run_case("incomplete state is inconclusive", "inconclusive", {})
    run_case(
        "multiple plausible invoices are inconclusive",
        "inconclusive",
        state(
            invoice_number="INV-1067",
            invoices=[
                {
                    "number": "INV-1067",
                    "vendor": "Northwind Traders",
                    "amount": 7390.40,
                    "issued_date": "2026-09-28",
                    "due_date": "2026-10-28",
                },
                {
                    "number": "INV-1067",
                    "vendor": "Northwind Traders",
                    "amount": 7390.40,
                    "issued_date": "2026-09-28",
                    "due_date": "2026-10-28",
                },
            ],
        ),
    )

    original = verifier.complete_json
    verifier.complete_json = model_confirmation
    try:
        result = verifier.verify(
            CONTRACT, {"outcome": "done"}, lambda: (_ for _ in ()).throw(OSError("offline"))
        )
    finally:
        verifier.complete_json = original
    check(
        "unavailable verification source is inconclusive",
        result.verdict == "inconclusive" and "offline" in result.discrepancies[0],
        result.as_dict(),
    )

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAIL  {failure}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run())
