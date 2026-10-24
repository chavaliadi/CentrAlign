"""Human-in-the-loop tools and the state tool.

Two separate ideas that are easy to confuse:

* ``ask_human`` is the agent *choosing* to stop because the task is genuinely
  under-specified or unsafe to continue. The model decides to call it.
* the **approval gate** in ``loop.py`` is the *framework* stopping the agent
  before any tool marked ``mutating`` runs, whether the model wanted to stop or
  not. The agent cannot opt out of it.

Keeping them apart matters: the first is judgement, the second is a guarantee.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .base import Observation, ToolRegistry, boolean, obj_schema, string

# A console prompt by default; swapped for a scripted responder in automated
# runs so the recorded demo and the test suite stay reproducible.
Responder = Callable[[str, dict[str, Any]], str]


def console_responder(question: str, context: dict[str, Any]) -> str:
    print("\n" + "=" * 70)
    print("AGENT NEEDS HUMAN INPUT")
    print("=" * 70)
    print(question)
    if context:
        print(f"\ncontext: {json.dumps(context, indent=2, default=str)[:1200]}")
    print("-" * 70)
    sys.stdout.write("your answer: ")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


class ScriptedResponder:
    """Answers questions from a dict supplied by the task file, falling back to
    a stated default. Every answer is recorded in the trace, so a reviewer can
    see exactly what the agent was told and when."""

    def __init__(self, answers: dict[str, str] | None = None, default: str = "") -> None:
        self.answers = answers or {}
        self.default = default
        self.asked: list[dict[str, str]] = []

    def __call__(self, question: str, context: dict[str, Any]) -> str:
        answer = self.default
        lowered = question.lower()
        for key, value in self.answers.items():
            if key.lower() in lowered:
                answer = value
                break
        self.asked.append({"question": question, "answer": answer})
        return answer


def register_human_tools(
    registry: ToolRegistry,
    responder: Responder,
    memory_writer: Callable[[str, Any, str, str], None],
) -> None:
    @registry.tool(
        "ask_human",
        "Ask the human for information or a decision you cannot safely determine "
        "yourself - genuinely ambiguous instructions, missing credentials, or a choice "
        "with consequences. Do not use it for things you could find out by looking.",
        obj_schema(
            {
                "question": string("The specific question to put to the human"),
                "why": string("Why you cannot proceed without this"),
            },
            ["question"],
        ),
    )
    def ask_human(question: str, why: str = "") -> Observation:  # type: ignore[misc]
        answer = responder(question, {"why": why})
        if not answer:
            return Observation(
                ok=False,
                summary="The human did not provide an answer. Do not ask the same question again; "
                "either find the information yourself or stop and report what is blocking you.",
                error="no answer from human",
            )
        return Observation(
            ok=True,
            summary=f"Human answered: {answer}",
            data={"question": question, "answer": answer},
            facts={f"human_answer:{question[:40]}": (answer, "supplied by human")},
        )

    @registry.tool(
        "remember",
        "Record a fact you have established, so later steps and the final report can "
        "use it with its provenance. Record the authoritative value, not a guess.",
        obj_schema(
            {
                "key": string("Short stable key, e.g. 'invoice.total_payable'"),
                "value": string("The value to record"),
                "source": string("Where it came from: a URL or file path"),
                "note": string("Optional qualifier, e.g. 'from PDF, incl. tax'"),
            },
            ["key", "value", "source"],
        ),
    )
    def remember(key: str, value: str, source: str, note: str = "") -> Observation:  # type: ignore[misc]
        memory_writer(key, value, source, note)
        return Observation(
            ok=True,
            summary=f"Recorded {key} = {value} (source: {source})",
            data={"key": key, "value": value},
        )

    @registry.tool(
        "finish_task",
        "Declare the task complete. State the outcome, the evidence supporting it, and "
        "the values you established. This does NOT end the run on its own: an "
        "independent verifier re-reads the company system to confirm the outcome, and "
        "will send you back to work if it cannot confirm it.",
        obj_schema(
            {
                "outcome": string("What was accomplished, in two or three sentences"),
                "answer": string(
                    "The direct answer or result, if the task asked a question "
                    "(amounts, dates, record ids)"
                ),
                "evidence": string("Where the outcome can be checked - URLs, record ids, files"),
                "blocked": boolean("True if you are stopping without completing the task"),
                "blocker": string("If blocked, what stopped you"),
            },
            ["outcome"],
        ),
    )
    def finish_task(  # type: ignore[misc]
        outcome: str,
        answer: str = "",
        evidence: str = "",
        blocked: bool = False,
        blocker: str = "",
    ) -> Observation:
        return Observation(
            ok=True,
            summary="Completion declared; handing over to independent verification.",
            data={
                "outcome": outcome,
                "answer": answer,
                "evidence": evidence,
                "blocked": bool(blocked),
                "blocker": blocker,
                "_terminal": True,
            },
        )
