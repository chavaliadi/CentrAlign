"""Independent verification.

The verifier is the part of this prototype I would defend hardest, because it
is what separates "the agent said it was done" from "it was done".

Two properties make it independent:

1. **It does not see the agent's work.** It is given the outcome contract, the
   agent's declared result, and a *freshly read* snapshot of the company's
   systems - and nothing else. No step log, no reasoning, no narration. There
   is therefore nothing for the agent to talk it into.
2. **It reads state by a different path.** The agent acts through the browser
   UI; the verifier reads the JSON state endpoints. A UI-level illusion of
   success - a flash message, a filled form that never submitted - does not
   reach it.

Its verdict is binding. ``refuted`` or ``inconclusive`` sends the controller
back to re-plan with the verifier's stated reason.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .config import SETTINGS
from .llm import complete_json
from .planner import OutcomeContract

StateReader = Callable[[], dict[str, Any]]

VERIFIER_SYSTEM = """You verify whether a task was actually completed. You are deliberately given no access to how the work was done - only what was claimed, what completion was defined to mean, and the current state of the company's systems read directly from their data stores.

Judge the claim against the success criteria and the state. Nothing else.

Be strict in specific ways:
- A claimed value must match the state exactly. Amounts must agree to the cent; dates must agree exactly. A plausible but different number is a FAIL, not a rounding question - it usually means a summary view was used instead of the authoritative document, or a figure was copied from the wrong record.
- If a criterion requires a record to exist, find it in the state. If it requires a record to be corrected, confirm the new value is present and the old one is gone.
- Duplicates are a failure: if the task was to create one record and the state has two matching records, say so.
- If the agent reports being blocked, verify that it is genuinely blocked rather than mistaken, and say which criteria remain unmet.
- "inconclusive" is for when the state provided does not contain enough information to decide. It is not a polite way of saying fail.

Reply with JSON only:
{
  "verdict": "confirmed" | "refuted" | "inconclusive",
  "criteria": [{"criterion": "...", "met": true/false, "evidence": "the specific value in the state that settles it"}],
  "discrepancies": ["precise mismatches, with both values"],
  "reason": "one or two sentences a reviewer could act on",
  "what_to_fix": "if not confirmed, the specific correction needed"
}"""


STATE_CHAR_LIMIT = 12000

_INVOICE_NUMBER = re.compile(r"\b[A-Z]{2,}(?:-\d+)+\b")
_AMOUNT = re.compile(
    r"(?:amount|total payable|total)[^0-9]{0,80}"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_DUE_DATE = re.compile(r"due date[^0-9]{0,40}(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _invoice_verification(
    contract: OutcomeContract, state: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Check invoice requirements that can be established from persisted state."""
    if not contract.requires_write:
        return [], [], None

    criteria_text = " ".join(
        [contract.goal, *contract.success_criteria, *contract.required_evidence]
    )
    if not any(word in criteria_text.lower() for word in ("invoice", "payable")):
        return [], [], None

    payables = state.get("payables_system", {}).get("payables")
    invoices = state.get("vendor_invoices", {}).get("invoices")
    if not isinstance(payables, list) or not isinstance(invoices, list):
        return (
            [],
            ["invoice verification requires payables and invoice records in state"],
            "inconclusive",
        )

    invoice_numbers = set(_INVOICE_NUMBER.findall(criteria_text))
    vendors = {str(invoice.get("vendor")) for invoice in invoices if invoice.get("vendor")}
    mentioned_vendors = {vendor for vendor in vendors if vendor.lower() in criteria_text.lower()}
    if len(invoice_numbers) > 1:
        return (
            [],
            ["the contract names more than one invoice and cannot identify one target"],
            "inconclusive",
        )

    target_number = next(iter(invoice_numbers), None)
    if target_number:
        source_matches = [i for i in invoices if i.get("number") == target_number]
    elif len(mentioned_vendors) == 1:
        vendor = next(iter(mentioned_vendors))
        candidates = [i for i in invoices if i.get("vendor") == vendor]
        if "latest" in criteria_text.lower() or "newest" in criteria_text.lower():
            latest_date = max((str(i.get("issued_date", "")) for i in candidates), default="")
            source_matches = [i for i in candidates if str(i.get("issued_date", "")) == latest_date]
        else:
            source_matches = candidates
    else:
        return (
            [],
            ["the requested invoice could not be identified from the contract and state"],
            "inconclusive",
        )

    if len(source_matches) != 1:
        if not source_matches:
            return [
                {"criterion": "the requested invoice exists in the vendor records",
                 "met": False, "evidence": "no matching invoice"}
            ], ["the requested invoice was not found in the vendor records"], "refuted"
        return [
            {"criterion": "the requested invoice can be identified uniquely",
             "met": False, "evidence": f"{len(source_matches)} plausible invoice records"}
        ], ["the contract matches multiple invoice records"], "inconclusive"

    source = source_matches[0]
    target_number = str(source.get("number", target_number or ""))
    payable_matches = [p for p in payables if p.get("invoice_number") == target_number]
    findings: list[dict[str, Any]] = [
        {
            "criterion": f"exactly one persisted payable exists for {target_number}",
            "met": len(payable_matches) == 1,
            "evidence": f"{len(payable_matches)} matching payable row(s)",
        }
    ]
    discrepancies: list[str] = []
    if len(payable_matches) != 1:
        if len(payable_matches) == 0:
            discrepancies.append(f"no persisted payable exists for {target_number}")
        else:
            discrepancies.append(f"{len(payable_matches)} persisted payables exist for {target_number}")
        return findings, discrepancies, "refuted"

    payable = payable_matches[0]
    expected_vendor = next(iter(mentioned_vendors), str(source.get("vendor", "")))
    vendor_ok = payable.get("vendor") == expected_vendor and source.get("vendor") == expected_vendor
    findings.append({
        "criterion": "the payable and source invoice have the expected vendor",
        "met": vendor_ok,
        "evidence": f"payable={payable.get('vendor')!r}, source={source.get('vendor')!r}",
    })
    if not vendor_ok:
        discrepancies.append(
            f"vendor mismatch: payable={payable.get('vendor')!r}, source={source.get('vendor')!r}"
        )

    amount_match = _AMOUNT.search(criteria_text)
    expected_amount = _decimal(amount_match.group(1)) if amount_match else _decimal(source.get("amount"))
    payable_amount = _decimal(payable.get("amount"))
    source_amount = _decimal(source.get("amount"))
    amount_ok = (
        expected_amount is not None
        and payable_amount == expected_amount
        and source_amount == expected_amount
    )
    findings.append({
        "criterion": "the payable amount matches the authoritative invoice amount",
        "met": amount_ok,
        "evidence": f"payable={payable.get('amount')!r}, source={source.get('amount')!r}",
    })
    if not amount_ok:
        discrepancies.append(
            f"amount mismatch: payable={payable.get('amount')!r}, source={source.get('amount')!r}"
        )

    date_match = _DUE_DATE.search(criteria_text)
    expected_due_date = date_match.group(1) if date_match else str(source.get("due_date", ""))
    due_date_ok = payable.get("due_date") == expected_due_date == source.get("due_date")
    findings.append({
        "criterion": "the payable due date matches the invoice due date",
        "met": due_date_ok,
        "evidence": f"payable={payable.get('due_date')!r}, source={source.get('due_date')!r}",
    })
    if not due_date_ok:
        discrepancies.append(
            f"due date mismatch: payable={payable.get('due_date')!r}, source={source.get('due_date')!r}"
        )

    return findings, discrepancies, "confirmed" if all(c["met"] for c in findings) else "refuted"


@dataclass
class Verdict:
    verdict: str
    criteria: list[dict[str, Any]]
    discrepancies: list[str]
    reason: str
    what_to_fix: str
    state_snapshot: dict[str, Any]
    # Set when the controller overrode the model's verdict, and why. A
    # reviewer should be able to see that this happened.
    overrides: list[str] = field(default_factory=list)
    state_truncated: bool = False

    @property
    def confirmed(self) -> bool:
        return self.verdict == "confirmed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "criteria": self.criteria,
            "discrepancies": self.discrepancies,
            "reason": self.reason,
            "what_to_fix": self.what_to_fix,
            "overrides": self.overrides,
            "state_truncated": self.state_truncated,
            "state_snapshot": self.state_snapshot,
        }


def _enforce(
    verdict: str,
    criteria: list[dict[str, Any]],
    contract: OutcomeContract,
    *,
    truncated: bool,
) -> tuple[str, list[str]]:
    """Mechanical checks the model's verdict has to survive.

    "Binding verification" means nothing if a model can return
    ``{"verdict": "confirmed"}`` with no criteria and have it accepted - which
    is exactly what this used to do. A confirmation now has to be *structurally*
    supported, and the controller downgrades it when it is not. These are the
    checks that do not need judgement, so they do not get to depend on any.
    """
    overrides: list[str] = []

    if verdict != "confirmed":
        return verdict, overrides

    if not criteria:
        overrides.append(
            "model returned 'confirmed' with no per-criterion findings; "
            "a confirmation with no evidence is not a confirmation"
        )
        return "inconclusive", overrides

    unmet = [c for c in criteria if not c.get("met")]
    if unmet:
        overrides.append(
            "model returned 'confirmed' while reporting "
            f"{len(unmet)} unmet criterion/criteria: "
            + "; ".join(str(c.get("criterion", "?")) for c in unmet)
        )
        return "refuted", overrides

    expected = len(contract.success_criteria)
    if expected and len(criteria) < expected:
        overrides.append(
            f"model judged only {len(criteria)} of the contract's {expected} success "
            "criteria; the rest were not assessed"
        )
        return "inconclusive", overrides

    if truncated:
        overrides.append(
            "the system state was too large to show the verifier in full, so a "
            "confirmation cannot be trusted: records beyond the cutoff were not seen"
        )
        return "inconclusive", overrides

    return verdict, overrides


def verify(
    contract: OutcomeContract,
    declared: dict[str, Any],
    read_state: StateReader,
) -> Verdict:
    try:
        state = read_state()
    except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
        return Verdict(
            verdict="inconclusive",
            criteria=[],
            discrepancies=[
                f"independent verification could not read application state: "
                f"{type(exc).__name__}: {exc}"
            ],
            reason="The independent application state source was unavailable.",
            what_to_fix="Restore access to the application state and verify again.",
            state_snapshot={},
            overrides=["state read failed; confirmation is not possible"],
        )
    rendered = json.dumps(state, indent=2, default=str)
    truncated = len(rendered) > STATE_CHAR_LIMIT
    if truncated:
        rendered = rendered[:STATE_CHAR_LIMIT]

    user = (
        f"Definition of done (the outcome contract):\n{contract.prompt_block()}\n\n"
        f"What the worker claims it achieved:\n{json.dumps(declared, indent=2, default=str)}\n\n"
        f"Current state of the company's systems, read directly just now:\n{rendered}"
    )
    if truncated:
        # Never let the verifier confirm against evidence it could not see in
        # full without knowing that is what it is doing.
        user += (
            "\n\nWARNING: this state listing was truncated and is INCOMPLETE. Records beyond "
            "the cutoff are not shown. Do not treat an absence as proof of absence, and do not "
            "return 'confirmed' on the strength of a partial listing - return 'inconclusive' "
            "and say what you could not see."
        )
    data = complete_json(
        VERIFIER_SYSTEM,
        user,
        model=SETTINGS.resolved_verifier_model(),
        max_tokens=1600,
    )
    verdict = str(data.get("verdict", "inconclusive")).lower()
    if verdict not in ("confirmed", "refuted", "inconclusive"):
        verdict = "inconclusive"

    # Keep only well-formed criterion objects - a bare string, or a dict with
    # no `met` field, cannot support a confirmation.
    criteria: list[dict[str, Any]] = []
    for item in data.get("criteria", []) or []:
        if isinstance(item, dict) and "met" in item:
            entry = dict(item)
            entry["met"] = bool(entry["met"])
            criteria.append(entry)

    discrepancies = [str(x) for x in data.get("discrepancies", []) or []]
    invoice_criteria, invoice_discrepancies, invoice_verdict = _invoice_verification(
        contract, state
    )
    criteria.extend(invoice_criteria)
    discrepancies.extend(invoice_discrepancies)
    invoice_overrides: list[str] = []
    if verdict == "confirmed" and invoice_verdict in ("refuted", "inconclusive"):
        verdict = invoice_verdict
        invoice_overrides.append(
            f"mechanical invoice checks returned {invoice_verdict}; "
            "the model cannot confirm an invoice outcome that persisted state does not support"
        )
    verdict, overrides = _enforce(verdict, criteria, contract, truncated=truncated)
    overrides.extend(invoice_overrides)
    discrepancies.extend(overrides)

    return Verdict(
        verdict=verdict,
        criteria=criteria,
        discrepancies=discrepancies,
        reason=str(data.get("reason", "")),
        what_to_fix=str(data.get("what_to_fix", "")),
        state_snapshot=state,
        overrides=overrides,
        state_truncated=truncated,
    )
