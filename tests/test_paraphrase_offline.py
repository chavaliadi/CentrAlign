"""Outcome contract checks for differently worded requests.

The model is replaced with a deterministic response function, so this suite
checks the understand interface and the contract invariants without a network
or an API key.

Run: python -m tests.test_paraphrase_offline
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent import planner  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


WRITE_REQUESTS = [
    "Log the newest Northwind bill in payables.",
    (
        "Could you please take care of the latest bill that Northwind sent us, and, "
        "when you have a moment, make sure the corresponding amount is entered in the "
        "payables system using the authoritative figure from the source document, then "
        "let me know once the books reflect it?"
    ),
    (
        "First find Northwind's newest bill, then open its source document, read the "
        "authoritative amount, open payables, enter the record, and confirm it."
    ),
    "Make sure our books reflect Northwind's latest invoice.",
    "Finance says the Northwind invoice isn't in the system yet - can you sort that out?",
]


def fake_complete_json(system: str, user: str, **_kwargs: Any) -> dict[str, Any]:
    request = user.split("Company request:\n", 1)[1].split(
        "\n\nWhat is known about the environment:", 1
    )[0]
    if "how much do we owe" in request.lower():
        return {
            "goal": "report the total owed",
            "success_criteria": ["the requested total is reported from the source records"],
            "required_evidence": ["the matching source records"],
            "requires_write": False,
            "systems_of_record": ["payables"],
            "unknowns": [],
            "out_of_scope": ["changing any records"],
        }
    if request.lower() == "please fix the books.":
        return {
            "goal": "make the requested Northwind record reflect the latest source document",
            "success_criteria": [],
            "required_evidence": [],
            "requires_write": True,
            "systems_of_record": ["payables"],
            "unknowns": ["which Northwind record and source value are intended"],
            "out_of_scope": ["other vendors and records"],
        }
    return {
        "goal": "record Northwind's latest document in the books",
        "success_criteria": [
            "a payables record exists for Northwind's latest document",
            "the record amount equals the authoritative amount in the source document",
        ],
        "required_evidence": ["the source document", "the resulting record"],
        "requires_write": True,
        "systems_of_record": ["payables"],
        "unknowns": [],
        "out_of_scope": ["other vendors", "other documents"],
    }


def run() -> int:
    original = planner.complete_json
    planner.complete_json = fake_complete_json
    try:
        for index, request in enumerate(WRITE_REQUESTS, 1):
            contract = planner.understand(request, "The payables system stores records.")
            criteria = " ".join(contract.success_criteria).lower()
            scope = " ".join(contract.out_of_scope).lower()
            check(f"write wording {index}: same system of record",
                  "payables" in {value.lower() for value in contract.systems_of_record},
                  str(contract.systems_of_record))
            check(f"write wording {index}: requires a write",
                  contract.requires_write is True, str(contract.requires_write))
            check(f"write wording {index}: checks authoritative amount",
                  "record" in criteria and "authoritative" in criteria and "amount" in criteria,
                  criteria)
            check(f"write wording {index}: keeps scope narrow",
                  "other" in scope and "vendor" in scope and "document" in scope,
                  scope)

        readonly = planner.understand(
            "How much do we owe Globex across its unpaid records?",
            "The payables system stores records.",
        )
        check("read only wording: does not require a write",
              readonly.requires_write is False, str(readonly.requires_write))

        ambiguous = planner.understand(
            "Please fix the books.",
            "The payables system stores records.",
        )
        check("ambiguous wording: records unknowns",
              bool(ambiguous.unknowns), str(ambiguous.unknowns))
        check("ambiguous wording: does not invent criteria",
              not ambiguous.success_criteria, str(ambiguous.success_criteria))
    finally:
        planner.complete_json = original

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run())
