"""Controller tests with the model replaced by a script. No API key needed.

The point of these is that the behaviour CentrAlign asks about - recovery,
approval, verification, not looping forever - belongs to the controller, not to
the model's good intentions. So they are testable deterministically, and they
are tested here with the model stubbed out entirely.

Each case scripts a sequence of actions and asserts what the loop does around
them:

1. transient failure is retried and the run completes;
2. a wrong value is caught by verification, re-planned, corrected, confirmed;
3. a denied approval does not write, and the agent is told not to retry it;
4. a repeated identical write is flagged before it can duplicate a record;
5. the step budget ends a hopeless run as a reported failure;
6. three consecutive failures trigger a controller intervention.

Run:  python -m tests.test_loop_offline
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent import loop as loop_module  # noqa: E402
from agent.config import SETTINGS  # noqa: E402
from agent.loop import TaskWorker  # noqa: E402
from agent.planner import OutcomeContract  # noqa: E402
from agent.tools.base import Observation, ToolRegistry, obj_schema, string  # noqa: E402
from agent.tools.human import register_human_tools  # noqa: E402
from agent.verifier import Verdict  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


CONTRACT = OutcomeContract(
    goal="a payables record exists for INV-1067 with the authoritative total",
    success_criteria=["a payables row for INV-1067 with amount 7390.40 exists"],
    required_evidence=["the payables row"],
    requires_write=True,
    systems_of_record=["payables"],
)


class Harness:
    """Builds a TaskWorker whose model calls are scripted and whose tools are
    stubs, so no browser and no network are involved."""

    def __init__(
        self,
        actions: Callable[[int, str], tuple[str, dict[str, Any]]],
        verifier: Callable[[dict[str, Any], int], Verdict],
        *,
        auto_approve: bool = True,
        approval_hook: Callable[[str, dict[str, Any]], bool] | None = None,
        max_steps: int = 12,
    ) -> None:
        self.actions = actions
        self.verifier = verifier
        self.verify_calls = 0
        self.writes: list[dict[str, Any]] = []
        self.nudges: list[str] = []
        self.plans = 0

        self._prev = {
            "understand": loop_module.understand,
            "make_plan": loop_module.make_plan,
            "next_action": loop_module.next_action,
            "verify": loop_module.verify,
            "auto": SETTINGS.auto_approve,
            "steps": SETTINGS.max_steps,
            "backoff": SETTINGS.retry_backoff_seconds,
        }
        SETTINGS.auto_approve = auto_approve
        SETTINGS.max_steps = max_steps
        SETTINGS.retry_backoff_seconds = 0.01

        loop_module.understand = lambda task, notes: CONTRACT
        loop_module.make_plan = self._make_plan
        loop_module.next_action = self._next_action
        loop_module.verify = self._verify

        self.worker = TaskWorker(
            task_id="offline",
            task="enter the invoice",
            environment_notes="stub",
            read_state=lambda: {"payables": list(self.writes)},
            responder=lambda q, c: "proceed",
            approval_hook=approval_hook,
        )
        self.worker.registry = self._build_registry()

    # --- stubs ---------------------------------------------------------------

    def _make_plan(self, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        self.plans += 1
        return {"steps": [{"intent": "stub", "where": "stub"}], "risks": []}

    def _next_action(self, *args: Any, **kwargs: Any) -> tuple[str, dict[str, Any], str]:
        step = kwargs["step"]
        nudge = kwargs.get("nudge", "") or ""
        if nudge:
            self.nudges.append(nudge)
        tool, arguments = self.actions(step, nudge)
        return tool, arguments, "scripted"

    def _verify(self, contract: Any, declared: dict[str, Any], read_state: Any) -> Verdict:
        self.verify_calls += 1
        return self.verifier(declared, self.verify_calls)

    def _build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        self.flaky_calls = 0

        @registry.tool("flaky_read", "A read that fails twice then succeeds.", obj_schema({}))
        def flaky_read() -> Observation:  # type: ignore[misc]
            self.flaky_calls += 1
            if self.flaky_calls <= 2:
                return Observation.failure("HTTP 503", retryable=True)
            return Observation(ok=True, summary="read succeeded", data={"value": "7390.40"})

        @registry.tool("dead_end", "A read that always fails permanently.", obj_schema({}))
        def dead_end() -> Observation:  # type: ignore[misc]
            return Observation.failure("element not found", retryable=False)

        @registry.tool(
            "write_payable",
            "Write a payables record.",
            obj_schema({"invoice": string("invoice"), "amount": string("amount")},
                       ["invoice", "amount"]),
            mutating=True,
        )
        def write_payable(invoice: str, amount: str) -> Observation:  # type: ignore[misc]
            self.writes.append({"invoice": invoice, "amount": float(amount)})
            return Observation(ok=True, summary=f"wrote {invoice} {amount}")

        @registry.tool(
            "fix_payable",
            "Correct the most recent payables record.",
            obj_schema({"amount": string("amount")}, ["amount"]),
            mutating=True,
        )
        def fix_payable(amount: str) -> Observation:  # type: ignore[misc]
            if self.writes:
                self.writes[-1]["amount"] = float(amount)
            return Observation(ok=True, summary=f"corrected to {amount}")

        register_human_tools(
            registry,
            lambda q, c: "proceed",
            lambda k, v, s, n: self.worker.memory.remember(k, v, s, 0, n),
        )
        return registry

    def run(self) -> Any:
        try:
            return self.worker._run_phases()  # noqa: SLF001 - bypasses browser setup by design
        finally:
            loop_module.understand = self._prev["understand"]
            loop_module.make_plan = self._prev["make_plan"]
            loop_module.next_action = self._prev["next_action"]
            loop_module.verify = self._prev["verify"]
            SETTINGS.auto_approve = self._prev["auto"]
            SETTINGS.max_steps = self._prev["steps"]
            SETTINGS.retry_backoff_seconds = self._prev["backoff"]


def confirmed(_declared: dict[str, Any], _n: int) -> Verdict:
    return Verdict("confirmed", [], [], "state matches", "", {})


# --- cases -------------------------------------------------------------------


def case_transient_retry() -> None:
    print("\n[1] a transient failure is retried, not treated as a dead end")

    def actions(step: int, _nudge: str) -> tuple[str, dict[str, Any]]:
        if step == 1:
            return "flaky_read", {}
        if step == 2:
            return "write_payable", {"invoice": "INV-1067", "amount": "7390.40"}
        return "finish_task", {"outcome": "entered", "answer": "7390.40"}

    harness = Harness(actions, confirmed)
    result = harness.run()
    check("run completed", result.status == "completed", result.status)
    check("the retry happened inside one step, not three",
          result.steps_used == 3, f"steps={result.steps_used}")
    check("the flaky tool was actually called three times",
          harness.flaky_calls == 3, str(harness.flaky_calls))
    retries = [e for e in harness.worker.recorder.events if e["kind"] == "retry"]
    check("both retries are recorded in the trace", len(retries) == 2, str(len(retries)))
    check("the recovery is visible in the observation",
          "recovered after" in harness.worker.memory.episodes[0].summary,
          harness.worker.memory.episodes[0].summary[:80])


def case_verification_catches_wrong_value() -> None:
    print("\n[2] verification catches a plausible-but-wrong value and forces a correction")

    def verifier(declared: dict[str, Any], call: int) -> Verdict:
        if call == 1:
            return Verdict(
                "refuted",
                [{"criterion": "amount is 7390.40", "met": False, "evidence": "state has 6843.00"}],
                ["claimed 6843.00, invoice total payable is 7390.40"],
                "the pre-tax subtotal was entered instead of the total payable",
                "correct the amount to 7390.40",
                {},
            )
        return confirmed(declared, call)

    def actions(step: int, nudge: str) -> tuple[str, dict[str, Any]]:
        if step == 1:
            return "write_payable", {"invoice": "INV-1067", "amount": "6843.00"}
        if step == 2:
            return "finish_task", {"outcome": "entered", "answer": "6843.00"}
        if "REJECTED" in nudge or step == 3:
            return "fix_payable", {"amount": "7390.40"}
        return "finish_task", {"outcome": "corrected", "answer": "7390.40"}

    harness = Harness(actions, verifier)
    result = harness.run()
    check("the first completion claim was not accepted", harness.verify_calls == 2,
          f"verify calls={harness.verify_calls}")
    check("the run re-planned exactly once", result.replans == 1, str(result.replans))
    check("the executor was told specifically what was wrong",
          any("REJECTED" in n and "7390.40" in n for n in harness.nudges), str(harness.nudges)[:200])
    check("the stored value ended up corrected, not duplicated",
          len(harness.writes) == 1 and harness.writes[0]["amount"] == 7390.40, str(harness.writes))
    check("final status is completed", result.status == "completed", result.status)


def case_denied_approval() -> None:
    print("\n[3] a denied approval blocks the write and is not retried")
    denials: list[str] = []

    def deny(tool: str, _args: dict[str, Any]) -> bool:
        denials.append(tool)
        return False

    def actions(step: int, _nudge: str) -> tuple[str, dict[str, Any]]:
        if step == 1:
            return "write_payable", {"invoice": "INV-1067", "amount": "7390.40"}
        return "finish_task", {"outcome": "could not write", "blocked": True,
                               "blocker": "approval denied by the human"}

    harness = Harness(
        actions,
        lambda d, n: Verdict("confirmed", [], [], "agent is genuinely blocked", "", {}),
        auto_approve=False,
        approval_hook=deny,
    )
    result = harness.run()
    check("approval was sought before the write", denials == ["write_payable"], str(denials))
    check("nothing was written", harness.writes == [], str(harness.writes))
    check("the denial is recorded in the trace",
          any(e["kind"] == "approval" and e["decision"] == "denied"
              for e in harness.worker.recorder.events), "no approval event")
    check("the agent was told not to retry the denied write",
          "Do not retry" in harness.worker.memory.episodes[0].summary,
          harness.worker.memory.episodes[0].summary[:100])
    check("the run ends as blocked, not as success", result.status == "blocked", result.status)


def case_duplicate_write_guard() -> None:
    print("\n[4] an identical repeated write is flagged before it can duplicate a record")

    def actions(step: int, _nudge: str) -> tuple[str, dict[str, Any]]:
        if step in (1, 2):
            return "write_payable", {"invoice": "INV-1067", "amount": "7390.40"}
        return "finish_task", {"outcome": "entered", "answer": "7390.40"}

    harness = Harness(actions, confirmed)
    harness.run()
    second = harness.worker.memory.episodes[1].summary
    check("the second identical write is marked as a repeat",
          "already performed in this run" in second, second[:140])
    check("the repeat is flagged in the approval record",
          any(e["kind"] == "approval" and e.get("repeat_of_earlier_write")
              for e in harness.worker.recorder.events), "not flagged")


def case_repeated_successful_action() -> None:
    print("\n[7] a loop of SUCCESSFUL no-op actions is caught, not just a failure streak")

    def actions(step: int, nudge: str) -> tuple[str, dict[str, Any]]:
        if step <= 5:
            return "idle_read", {}
        return "finish_task", {"outcome": "stopped", "blocked": True, "blocker": "looping"}

    harness = Harness(
        actions,
        lambda d, n: Verdict("confirmed", [], [], "genuinely blocked", "", {}),
        max_steps=8,
    )

    @harness.worker.registry.tool(
        "idle_read", "A read that always succeeds and changes nothing.", obj_schema({})
    )
    def idle_read() -> Observation:  # type: ignore[misc]
        return Observation(ok=True, summary="the same page as before")

    harness.run()
    interventions = [
        e for e in harness.worker.recorder.events
        if e["kind"] == "intervention" and e.get("reason") == "repeated_action"
    ]
    check("the controller intervened despite every action succeeding",
          len(interventions) >= 1, str(interventions))
    check("it named the tool being repeated",
          bool(interventions) and interventions[0].get("tool") == "idle_read",
          str(interventions[:1]))
    check("the executor was told the repeat is pointless",
          any("same thing" in n or "different action" in n for n in harness.nudges),
          str(harness.nudges)[:200])
    check("a failure-streak detector alone would have missed this",
          harness.worker.memory.failure_streak() == 0,
          str(harness.worker.memory.failure_streak()))


def case_budget_exhaustion() -> None:
    print("\n[5] a hopeless run ends as a reported failure, not an infinite loop")

    harness = Harness(lambda step, nudge: ("dead_end", {}), confirmed, max_steps=6)
    result = harness.run()
    check("the loop stopped at the budget", result.steps_used == 6, str(result.steps_used))
    check("status says budget_exhausted", result.status == "budget_exhausted", result.status)
    check("it is not reported as success", not result.ok, "reported ok")
    check("no verification was claimed", harness.verify_calls == 0, str(harness.verify_calls))


def case_stuck_intervention() -> None:
    print("\n[6] consecutive failures trigger a controller intervention")

    def actions(step: int, _nudge: str) -> tuple[str, dict[str, Any]]:
        if step <= 4:
            return "dead_end", {}
        return "finish_task", {"outcome": "gave up", "blocked": True, "blocker": "stuck"}

    harness = Harness(
        actions,
        lambda d, n: Verdict("confirmed", [], [], "genuinely blocked", "", {}),
        max_steps=8,
    )
    harness.run()
    interventions = [
        e for e in harness.worker.recorder.events
        if e["kind"] == "intervention" and e.get("reason") == "stuck_nudge"
    ]
    check("an intervention was raised after repeated failures",
          len(interventions) >= 1, str(interventions))
    check("the intervention told the agent to change approach",
          any("different route" in n for n in harness.nudges), str(harness.nudges)[:200])
    check("the controller noticed the streak, the model was not asked to",
          interventions[0].get("streak", 0) >= 3 if interventions else False,
          str(interventions[:1]))


def run() -> int:
    for case in (
        case_transient_retry,
        case_verification_catches_wrong_value,
        case_denied_approval,
        case_duplicate_write_guard,
        case_repeated_successful_action,
        case_budget_exhaustion,
        case_stuck_intervention,
    ):
        case()
    print("\n" + "=" * 60)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())
