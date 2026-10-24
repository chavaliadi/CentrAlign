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
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import SETTINGS
from .llm import complete_json
from .planner import OutcomeContract

StateReader = Callable[[], dict[str, Any]]

VERIFIER_SYSTEM = """You verify whether a task was actually completed. You are deliberately given no access to how the work was done - only what was claimed, what completion was defined to mean, and the current state of the company's systems read directly from their data stores.

Judge the claim against the success criteria and the state. Nothing else.

Be strict in specific ways:
- A claimed value must match the state exactly. Amounts must agree to the cent; dates must agree exactly. A plausible but different number is a FAIL, not a rounding question - it usually means a pre-tax subtotal was read instead of a total payable, or a figure was copied from the wrong row.
- If a criterion requires a record to exist, find it in the state. If it requires a record to be corrected, confirm the new value is present and the old one is gone.
- Duplicates are a failure: if the task was to create one record and the state has two for the same invoice, say so.
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
    state = read_state()
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
    verdict, overrides = _enforce(verdict, criteria, contract, truncated=truncated)
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
