"""The model-facing side of the agent: understanding, planning, and choosing
the next action.

These are three separate calls with three separate prompts rather than one big
conversation, for a reason that matters when things go wrong:

* **understand** turns a short human request into an explicit *outcome
  contract* - success criteria, required evidence, whether a write is involved.
  Everything downstream, including verification, is judged against this
  contract rather than against the loose wording of the request.
* **plan** produces an intended route. It is advice to the executor, not a
  script: no step of it is ever executed directly.
* **next_action** chooses one action at a time from the live observation
  history. This is where autonomy actually lives - the model is never told
  "now do step 3", it is shown what has happened and asked what to do next.

Re-planning is triggered by the controller, not by the model, when the
evidence says the current route is not working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .llm import ModelError, complete, complete_json


# --- outcome contract --------------------------------------------------------


@dataclass
class OutcomeContract:
    """What 'done' means for this task, fixed before any action is taken."""

    goal: str
    success_criteria: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    requires_write: bool = False
    systems_of_record: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "required_evidence": self.required_evidence,
            "requires_write": self.requires_write,
            "systems_of_record": self.systems_of_record,
            "unknowns": self.unknowns,
            "out_of_scope": self.out_of_scope,
        }

    def prompt_block(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


UNDERSTAND_SYSTEM = """You turn a short, under-specified company request into an explicit outcome contract.

A request like "find the latest record from X, enter its authoritative amount into our system and tell me when it's done" leaves the real requirements unstated. Your job is to make them explicit and checkable BEFORE any work starts.

Rules:
- Success criteria must be checkable by re-reading the company's systems afterwards, not by trusting a narration. Write them as assertions about end state ("a record exists for <n> with an amount equal to the authoritative value in the source document"), not as instructions ("open the summary view").
- Infer what "correct" means where the request is silent, but list anything genuinely ambiguous under unknowns.
- requires_write is true only if the task changes state in a system. A question that only reads data is not a write.
- Only assert things the described systems actually record. Do not invent fields, statuses or approval flags that were not described to you - a criterion like "the record is marked as approved" is unsatisfiable if no such field exists, and will fail a task that was in fact done correctly.
- Judge only the work this task is asking for. If a system already contains records, do not write criteria about pre-existing data that the task was not asked to change.
- Scope the criteria to the request, and do not enlarge it. A request to correct a discrepancy is not a request to reconcile an entire system. If someone says one record looks wrong, the criteria are about that record being right afterwards - not about every related record existing, matching, or being created. Inventing scope this way is worse than missing a criterion: it sends the worker off making changes nobody asked for, and those changes are real.
- A criterion must never require creating or altering data the request did not ask about. When in doubt about scope, write the narrow criterion and put the broader reading in `unknowns`.
- Prefer few, sharp criteria over many vague ones.

Reply with JSON only:
{
  "goal": "one sentence restating the intended outcome",
  "success_criteria": ["..."],
  "required_evidence": ["what a reviewer should be shown to believe it"],
  "requires_write": true/false,
  "systems_of_record": ["which system holds the truth for each criterion"],
  "unknowns": ["genuinely ambiguous points, empty if none"],
  "out_of_scope": ["things a reader might expect that this task does not cover"]
}"""


def understand(task: str, environment_notes: str) -> OutcomeContract:
    data = complete_json(
        UNDERSTAND_SYSTEM,
        f"Company request:\n{task}\n\n"
        f"What is known about the environment:\n{environment_notes}",
    )
    return OutcomeContract(
        goal=str(data.get("goal", task)),
        success_criteria=[str(x) for x in data.get("success_criteria", [])],
        required_evidence=[str(x) for x in data.get("required_evidence", [])],
        requires_write=bool(data.get("requires_write", False)),
        systems_of_record=[str(x) for x in data.get("systems_of_record", [])],
        unknowns=[str(x) for x in data.get("unknowns", [])],
        out_of_scope=[str(x) for x in data.get("out_of_scope", [])],
    )


# --- planning ----------------------------------------------------------------

PLAN_SYSTEM = """You produce a short intended route for an agent that operates a company's web systems through a browser.

This is advice, not a script. The executor re-decides every action from live observations, and the environment is unreliable on purpose: pages intermittently return 503, sessions expire mid-task, and the DOM is rendered in more than one layout. A plan that depends on an exact sequence or on memorised selectors is worthless.

So: name the information you need and where it is likely to live, in order of dependency. Flag where the authoritative value may differ from the obvious one.

Reply with JSON only:
{
  "steps": [{"intent": "what to establish or change", "where": "which system or page"}],
  "risks": ["what could make this go wrong, and the tell that it has"]
}"""


def make_plan(
    contract: OutcomeContract,
    tool_descriptions: str,
    environment_notes: str,
    *,
    previous_attempt: str = "",
) -> dict[str, Any]:
    user = (
        f"Outcome contract:\n{contract.prompt_block()}\n\n"
        f"Available tools:\n{tool_descriptions}\n\n"
        f"Environment:\n{environment_notes}"
    )
    if previous_attempt:
        user += (
            "\n\nA previous attempt did not satisfy verification. "
            f"What went wrong:\n{previous_attempt}\n\n"
            "Produce a different route that addresses this specifically. Do not repeat the "
            "approach that failed."
        )
    data = complete_json(PLAN_SYSTEM, user)
    return {
        "steps": data.get("steps", []),
        "risks": data.get("risks", []),
    }


def plan_block(plan: dict[str, Any]) -> str:
    steps = "\n".join(
        f"  {i + 1}. {s.get('intent', '')} ({s.get('where', 'unspecified')})"
        for i, s in enumerate(plan.get("steps", []))
    )
    risks = "\n".join(f"  - {r}" for r in plan.get("risks", []))
    return f"Intended route (advice, not a script):\n{steps}\n\nKnown risks:\n{risks}"


# --- action selection --------------------------------------------------------

EXECUTOR_SYSTEM = """You are an autonomous task worker operating a company's internal web systems through a browser, on behalf of a colleague who is not watching.

You choose ONE action per turn by calling exactly one tool. You will then be shown what actually happened and asked again. Nobody will tell you which step you are on.

How to work:
- Every observation you receive already contains the full contents of the page: table rows, form fields with their names, and links with their targets. Read your own earlier observations before asking for the page again. `browser_open`, `browser_click` and a submitted `browser_fill_form` all return the complete page, so calling `browser_read_page` straight after one of them tells you nothing you were not just given.
- Observe before acting where it earns its step: after an action whose result you cannot predict. Repeating an identical call never produces a different answer - if you find yourself about to repeat one, the information you want is already in your history, or you need a different action entirely.
- Address page elements by their semantics - a field's `name`, a link's visible text - never by position or remembered CSS. The layout genuinely changes between page loads.
- Only click text that actually appears in the links or button labels of your latest observation. Do not go looking for a feature you expect to exist - an export button, a download-all, a search box. If it is not listed, it is not there, and hunting for it wastes steps. Do the work from the data you can already see instead: the observation gives you every row and every value on the page.
- Treat failures as information. An HTTP 503 is transient: retry, possibly after re-reading. Being bounced to a login page means your session expired: log in again and resume where you were, do not restart the task. A missing field means you are on the wrong page or the layout differs: re-read and look again.
- Find the authoritative value, not the first plausible one. Summary lists, totals and detail pages can disagree; the task's systems_of_record tells you which to trust, and documents usually outrank web summaries.
- Record established values with `remember` as you go, with their source. Your final report needs provenance.
- Capture a screenshot at the moments that matter: the authoritative value you read, and the state after a write. These are the evidence a reviewer sees.
- Read the environment notes before concluding you are missing something. Credentials, system addresses and the policies that decide which of two figures is correct are given to you there. Use them.
- NEVER invent a credential, and never try a guessed email or password. If a credential you need genuinely is not in the environment notes, that is a real blocker: say so and stop. A fabricated login is worse than an honest failure.
- Use `ask_human` only for things you genuinely cannot determine by looking or from the notes. An instruction that is ambiguous in a way that changes the outcome qualifies. Being unsure which link to click does not. If you have already asked once and the answer did not supply what you needed, do not ask again - act on what you have, or stop and report the blocker.
- Writes require human approval, which the framework handles. Never attempt to work around it, and never write the same record twice - check whether your write already landed before retrying it.
- When the outcome is achieved, call `finish_task` with the outcome, the answer, and where it can be checked. An independent verifier will re-read the company systems; if it cannot confirm your claim it will send you back. So do not declare completion you have not actually checked.
- If you truly cannot proceed, call `finish_task` with blocked=true and say what stopped you. A clear stop is a better outcome than a false success.

You have a bounded number of steps. Spend them on establishing facts and changing state, not on re-reading pages you have already understood."""


def next_action(
    contract: OutcomeContract,
    plan: dict[str, Any],
    history: str,
    facts: str,
    tool_schemas: list[dict[str, Any]],
    *,
    environment_notes: str,
    step: int,
    steps_left: int,
    nudge: str = "",
) -> tuple[str, dict[str, Any], str]:
    """Returns (tool_name, tool_input, model_reasoning_text)."""
    user = (
        f"Outcome contract:\n{contract.prompt_block()}\n\n"
        # The executor needs this every turn, not just at planning time. It
        # carries the systems' addresses, the credentials it has been issued,
        # and any policy that decides which of two numbers is the right one.
        # Omitting it here was a wiring bug: the agent asked a human for a
        # password that was sitting in the task file all along.
        f"What you know about the environment:\n{environment_notes}\n\n"
        f"{plan_block(plan)}\n\n"
        f"Established facts:\n{facts}\n\n"
        f"What has happened so far:\n{history}\n\n"
        f"You are on step {step}. You have {steps_left} step(s) left before the budget runs out."
    )
    if nudge:
        user += f"\n\nController note: {nudge}"
    user += "\n\nChoose the single next action. Reply by calling exactly one tool."

    completion = complete(EXECUTOR_SYSTEM, user, tools=tool_schemas, max_tokens=1600)
    call = completion.first_tool_call
    if call is None:
        raise ModelError(f"model chose no tool. It said: {completion.text[:400]}")
    return call.name, call.arguments, completion.text
