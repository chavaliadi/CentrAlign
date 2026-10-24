"""The controller: Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete.

This file knows about contracts, tools, observations, budgets, approval and
verification, and nothing else. It does not know what the tools are: the
toolset is built by an injected factory (see `toolsets.py`), so the controller
never imports a browser, a PDF reader or anything else domain-specific. That
is what lets the same controller run a data-entry task, a read-only reporting
task and a cross-system correction task unchanged - and it is also why a
capability can be added without editing this file.

(An earlier version imported and wired the browser/file/human tools here,
which made exactly that claim untrue. An external reviewer caught it.)

The loop's own responsibilities, as opposed to the model's:

* enforcing the step budget, so a confused run ends in a reported failure
  rather than an infinite one;
* retrying actions the tool marked transient, with backoff;
* noticing when the agent is stuck - a run of failures, or repeating itself -
  and injecting a correction or forcing a re-plan. The model is not asked to
  notice its own loops;
* standing between the agent and every mutating action (the approval gate);
* refusing to accept the agent's own word for completion.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import SETTINGS
from .evidence import RunRecorder
from .memory import Episode, Memory
from .planner import ModelError, OutcomeContract, make_plan, next_action, understand
from .tools.base import Observation, Tool, ToolRegistry
from .toolsets import ToolContext, Toolset, ToolsetFactory, browser_toolset
from .verifier import StateReader, Verdict, verify

# A responder puts a question to the human and returns their answer.
Responder = Callable[[str, dict[str, Any]], str]

# How many consecutive failed actions before the controller intervenes.
STUCK_THRESHOLD = 3
# How many times the same action may be repeated before the controller says so.
# Two is a legitimate retry; three is a loop.
REPEAT_THRESHOLD = 3
# Consecutive no-op observations (a re-read that changed nothing) tolerated.
NO_PROGRESS_THRESHOLD = 2
# Consecutive turns where the model answered without calling any tool before
# the run is abandoned. Weaker models do this occasionally; it should cost a
# step, not the run.
MAX_NO_ACTION_FAILURES = 3


@dataclass
class RunResult:
    task_id: str
    status: str                      # completed | failed_verification | blocked | budget_exhausted | error
    steps_used: int
    replans: int
    contract: dict[str, Any]
    declared: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "steps_used": self.steps_used,
            "replans": self.replans,
            "contract": self.contract,
            "declared": self.declared,
            "verification": self.verification,
            "run_dir": self.run_dir,
            "error": self.error,
        }


class TaskWorker:
    def __init__(
        self,
        task_id: str,
        task: str,
        environment_notes: str,
        read_state: StateReader,
        responder: Responder,
        approval_hook: Callable[[str, dict[str, Any]], bool] | None = None,
        toolset_factory: ToolsetFactory = browser_toolset,
        review_contract: bool = False,
    ) -> None:
        self.task_id = task_id
        self.task = task
        self.environment_notes = environment_notes
        self.read_state = read_state
        self.responder = responder
        self.approval_hook = approval_hook or self._console_approval
        self.review_contract = review_contract
        # What the agent can do is decided outside the controller.
        self.toolset_factory = toolset_factory
        self.memory = Memory()
        self.recorder = RunRecorder(task_id=task_id, task_text=task)
        self.registry = ToolRegistry()
        self._mutating_calls: list[str] = []
        self._step = 0

    # --- wiring --------------------------------------------------------------

    def _tool_context(self) -> ToolContext:
        """What a toolset may need from the run, with no tool specifics."""

        def remember_fact(key: str, value: Any, source: str, note: str) -> None:
            self.memory.remember(key, value, source, self._step, note)

        def recording_responder(question: str, context: dict[str, Any]) -> str:
            answer = self.responder(question, context)
            self.recorder.event("human_question", question=question, answer=answer, step=self._step)
            return answer

        return ToolContext(
            downloads_dir=self.recorder.downloads_dir,
            screenshots_dir=self.recorder.screenshots_dir,
            remember=remember_fact,
            ask_human=recording_responder,
        )

    # --- approval gate -------------------------------------------------------

    @staticmethod
    def _console_approval(tool_name: str, arguments: dict[str, Any]) -> bool:
        print("\n" + "!" * 70)
        print("APPROVAL REQUIRED - the agent wants to change company data")
        print("!" * 70)
        print(f"action: {tool_name}")
        print(f"details: {json.dumps(arguments, indent=2, default=str)[:1200]}")
        print("-" * 70)
        sys.stdout.write("approve? [y/N]: ")
        sys.stdout.flush()
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    def _seek_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        force_prompt: bool = False,
    ) -> tuple[bool, str]:
        signature = f"{tool_name}:{json.dumps(arguments, sort_keys=True, default=str)}"
        repeat = signature in self._mutating_calls

        if SETTINGS.auto_approve and not force_prompt:
            approved = True
            self.recorder.event(
                "approval",
                step=self._step,
                tool=tool_name,
                arguments=arguments,
                decision="approved",
                auto=True,
                repeat_of_earlier_write=repeat,
            )
        else:
            approved = self.approval_hook(tool_name, arguments)
            self.recorder.event(
                "approval",
                step=self._step,
                tool=tool_name,
                arguments=arguments,
                decision="approved" if approved else "denied",
                auto=False,
                repeat_of_earlier_write=repeat,
            )

        if approved:
            self._mutating_calls.append(signature)
        note = ""
        if repeat:
            note = (
                "Note: this is byte-identical to a write you already performed in this run. "
                "Before repeating it, check whether the earlier write actually landed - "
                "writing twice would create a duplicate record, which fails verification."
            )
        return approved, note

    # --- execution -----------------------------------------------------------

    def _invoke(self, tool_name: str, arguments: dict[str, Any]) -> Observation:
        tool = self.registry.get(tool_name)
        if tool is None:
            return Observation.failure(
                f"no such tool {tool_name!r}. Available: {self.registry.names()}"
            )

        if tool.mutating:
            approved, note = self._seek_approval(tool_name, arguments)
            if not approved:
                return Observation(
                    ok=False,
                    summary=(
                        "The human denied approval for this write. Do not retry it. "
                        "Either establish what is actually required and propose a different "
                        "action, or stop with finish_task(blocked=true)."
                    ),
                    error="approval denied",
                    retryable=False,
                )
            if note:
                observation = self._call_with_retries(tool, arguments)
                observation.summary = f"{note}\n\n{observation.summary}"
                return observation

        return self._call_with_retries(tool, arguments)

    def _call_with_retries(self, tool: Tool, arguments: dict[str, Any]) -> Observation:
        """Retry only what the tool itself declared transient, and never a
        mutating action.

        A tool that says 'unknown field' is not retried - repeating it cannot
        change the result and only wastes budget.

        Mutating actions are never retried at all, whatever they declare. A
        form submission that times out may well have committed server-side
        before the response was lost, so retrying it risks a duplicate record -
        and a duplicate is a verification failure, i.e. silently wrong data
        rather than an honest error. The agent is told to go and check whether
        the write landed instead. This is a controller guarantee, not advice in
        a prompt.
        """
        attempt = 0
        max_attempts = 0 if tool.mutating else SETTINGS.max_action_retries
        while True:
            try:
                observation = tool.fn(**arguments)
            except TypeError as exc:
                return Observation.failure(f"bad arguments for {tool.name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - a tool crash is an observation
                # An unexpected exception is an implementation defect, not an
                # operational failure. The agent still gets an observation so a
                # run can continue, but the traceback goes into the evidence so
                # the defect is visible to a reviewer instead of being
                # laundered into a normal task failure.
                import traceback

                self.recorder.event(
                    "tool_exception",
                    step=self._step,
                    tool=tool.name,
                    arguments=arguments,
                    exception=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc()[:4000],
                )
                observation = Observation.failure(
                    f"{tool.name} failed with an internal error ({type(exc).__name__}: {exc}). "
                    "This is a defect in the tool, not something you did wrong - do not retry it, "
                    "use a different approach."
                )

            if observation.ok or not observation.retryable or attempt >= max_attempts:
                if attempt and observation.ok:
                    observation.summary = (
                        f"(recovered after {attempt} retry/retries) {observation.summary}"
                    )
                return observation

            attempt += 1
            self.recorder.event(
                "retry",
                step=self._step,
                tool=tool.name,
                attempt=attempt,
                error=observation.error,
            )
            time.sleep(SETTINGS.retry_backoff_seconds * attempt)

    # --- the loop ------------------------------------------------------------

    def run(self) -> RunResult:
        try:
            toolset: Toolset = self.toolset_factory(self._tool_context())
        except Exception as exc:  # noqa: BLE001 - provisioning failure ends the run
            return self._fail(f"could not provision tools: {type(exc).__name__}: {exc}")

        self.registry = toolset.registry
        self.recorder.meta["tools"] = self.registry.names()
        try:
            return self._run_phases()
        except ModelError as exc:
            return self._fail(f"model error: {exc}")
        finally:
            try:
                toolset.teardown()
            except Exception:  # noqa: BLE001 - teardown must not mask a result
                pass

    def _run_phases(self) -> RunResult:
        # --- Understand ------------------------------------------------------
        contract = understand(self.task, self.environment_notes)
        self.recorder.event("understand", contract=contract.as_dict())
        self.recorder.meta["contract"] = contract.as_dict()
        print(f"\n[understand] {contract.goal}")
        print("[contract] Definition of done, subject to review before any action:")
        print(json.dumps(contract.as_dict(), indent=2))
        for criterion in contract.success_criteria:
            print(f"   done when: {criterion}")
        if contract.unknowns:
            print(f"   unknowns: {contract.unknowns}")

        if self.review_contract:
            approved, _ = self._seek_approval(
                "review_contract",
                contract.as_dict(),
                force_prompt=True,
            )
            if not approved:
                result = RunResult(
                    task_id=self.task_id,
                    status="blocked",
                    steps_used=self._step,
                    replans=0,
                    contract=contract.as_dict(),
                    run_dir=str(self.recorder.run_dir),
                )
                self._finalise(result)
                return result

        # --- Plan ------------------------------------------------------------
        plan = make_plan(contract, self.registry.describe(), self.environment_notes)
        self.recorder.event("plan", plan=plan, revision=0)
        print(f"[plan] {len(plan.get('steps', []))} intended step(s)")

        replans = 0
        declared: dict[str, Any] = {}
        verdict: Verdict | None = None
        nudge = ""
        status = "budget_exhausted"
        fatal_error = ""
        no_action_failures = 0

        while self._step < SETTINGS.max_steps:
            self._step += 1
            steps_left = SETTINGS.max_steps - self._step

            # --- Adapt: controller-level intervention before asking again ----
            streak = self.memory.failure_streak()
            if streak >= STUCK_THRESHOLD and not nudge:
                nudge = (
                    f"The last {streak} actions all failed. Stop repeating the current approach. "
                    "Re-read the page you are on to establish where you actually are, then take a "
                    "different route to the same goal. If the obstacle is a login page, log in "
                    "again and resume."
                )
                self.recorder.event(
                    "intervention", step=self._step, reason="stuck_nudge", streak=streak
                )

            # --- Execute: one action ----------------------------------------
            try:
                tool_name, arguments, reasoning = next_action(
                    contract,
                    plan,
                    self.memory.compacted_history(SETTINGS.verbatim_observation_window),
                    self.memory.facts_block(),
                    self.registry.api_schemas(),
                    environment_notes=self.environment_notes,
                    step=self._step,
                    steps_left=steps_left,
                    nudge=nudge,
                )
            except ModelError as exc:
                # A model that answers with prose instead of a tool call, or a
                # provider that rejects the request, should cost one step - not
                # the whole run. Weaker models do this regularly, and ending a
                # twenty-step run on it throws away all the work so far.
                no_action_failures += 1
                self.recorder.event(
                    "model_no_action",
                    step=self._step,
                    error=str(exc),
                    consecutive=no_action_failures,
                )
                if no_action_failures >= MAX_NO_ACTION_FAILURES:
                    fatal_error = str(exc)
                    self.recorder.event("error", step=self._step, error=fatal_error)
                    status = "error"
                    break
                print(f"[{self._step:02d}] model produced no action; retrying")
                nudge = (
                    "Your previous reply did not call a tool, so nothing happened. You must "
                    "answer by calling exactly one tool - that is the only way to act or to "
                    "finish. If you believe the task is complete, call finish_task. If you are "
                    "stuck, call finish_task with blocked=true and say what stopped you. Do not "
                    "reply with prose."
                )
                continue
            nudge = ""
            no_action_failures = 0

            print(f"[{self._step:02d}] {tool_name} {json.dumps(arguments, default=str)[:140]}")
            observation = self._invoke(tool_name, arguments)

            # --- Observe -----------------------------------------------------
            # Truncation must be visible. Silently cutting an observation lets
            # the agent act on a partial page while believing it saw all of it.
            summary = observation.summary
            if len(summary) > SETTINGS.max_observation_chars:
                summary = (
                    summary[: SETTINGS.max_observation_chars]
                    + f"\n[... this observation was truncated at "
                    f"{SETTINGS.max_observation_chars} characters. You have NOT seen all of it. "
                    "Narrow what you are looking at - filter the list, or open a single record - "
                    "rather than assuming the rest is absent.]"
                )
            self.memory.log(
                Episode(
                    step=self._step,
                    action=tool_name,
                    arguments=arguments,
                    ok=observation.ok,
                    summary=summary,
                    error=observation.error,
                )
            )
            for key, (value, note) in observation.facts.items():
                self.memory.remember(key, value, tool_name, self._step, note)

            # --- Adapt: catch a loop that is "succeeding" at nothing ---------
            # A failure streak is the obvious stuck signal, but the expensive
            # one is an agent repeating a successful read that tells it nothing
            # new. Both are the controller's job to notice.
            # Only for actions that SUCCEED. A repeated failing action is the
            # older, more diagnostic case and is handled by the failure-streak
            # branch at the top of the loop; letting this one claim it first
            # would replace a useful message with a vaguer one.
            repeats = self.memory.repeat_count(tool_name, arguments)
            no_progress = self.memory.no_progress_streak()
            if observation.ok and repeats >= REPEAT_THRESHOLD:
                nudge = (
                    f"You have now called {tool_name} with these exact arguments {repeats} times. "
                    "It will keep returning the same thing. Whatever you are waiting to learn is "
                    "not going to appear: re-read your earlier observations, which already contain "
                    "the page contents, and take a different action that moves toward the goal."
                )
                self.recorder.event(
                    "intervention", step=self._step, reason="repeated_action",
                    tool=tool_name, repeats=repeats,
                )
            elif no_progress >= NO_PROGRESS_THRESHOLD:
                nudge = (
                    f"Your last {no_progress} actions changed nothing and revealed nothing new. "
                    "Stop observing and start acting: follow a link from the page contents in your "
                    "earlier observations, or move on to the next step of the goal."
                )
                self.recorder.event(
                    "intervention", step=self._step, reason="no_progress",
                    streak=no_progress,
                )

            self.recorder.event(
                "action",
                step=self._step,
                tool=tool_name,
                arguments=arguments,
                reasoning=reasoning[:1200],
                ok=observation.ok,
                summary=summary,
                error=observation.error,
                artifacts=observation.artifacts,
            )
            print(f"     -> {'ok' if observation.ok else 'FAILED'}: {summary[:160]}")

            if not observation.data.get("_terminal"):
                continue

            # --- Verify ------------------------------------------------------
            declared = {k: v for k, v in observation.data.items() if k != "_terminal"}
            print("[verify] re-reading company systems independently...")
            verdict = verify(contract, declared, self.read_state)
            self.recorder.event("verification", step=self._step, attempt=replans + 1, **verdict.as_dict())
            print(f"[verify] {verdict.verdict}: {verdict.reason}")

            if verdict.confirmed:
                status = "blocked" if declared.get("blocked") else "completed"
                break

            if replans >= SETTINGS.max_replans:
                status = "blocked" if declared.get("blocked") else "failed_verification"
                break

            # --- Adapt: re-plan against the verifier's specific objection ----
            replans += 1
            failure_note = (
                f"Verifier verdict: {verdict.verdict}. Reason: {verdict.reason}. "
                f"Discrepancies: {verdict.discrepancies}. Required fix: {verdict.what_to_fix}"
            )
            plan = make_plan(
                contract,
                self.registry.describe(),
                self.environment_notes,
                previous_attempt=failure_note,
            )
            self.recorder.event("plan", plan=plan, revision=replans, triggered_by=failure_note)
            nudge = (
                "Your completion claim was REJECTED by independent verification. "
                f"{failure_note} Fix exactly this, then call finish_task again. "
                "Do not re-declare completion without correcting the problem, and do not create "
                "a duplicate record - correct the existing one if that is what is wrong."
            )
            print(f"[adapt] re-planning (revision {replans})")

        result = RunResult(
            task_id=self.task_id,
            status=status,
            steps_used=self._step,
            replans=replans,
            contract=contract.as_dict(),
            declared=declared,
            verification=verdict.as_dict() if verdict else {},
            run_dir=str(self.recorder.run_dir),
            error=fatal_error,
        )
        self._finalise(result)
        return result

    # --- finishing -----------------------------------------------------------

    def _finalise(self, result: RunResult) -> None:
        self.recorder.meta["memory"] = self.memory.as_dict()
        self.recorder.write_trace(result=result.as_dict())
        self.recorder.write_report(result.as_dict())

    def _fail(self, error: str) -> RunResult:
        self.recorder.event("error", error=error)
        result = RunResult(
            task_id=self.task_id,
            status="error",
            steps_used=self._step,
            replans=0,
            contract={},
            error=error,
            run_dir=str(self.recorder.run_dir),
        )
        self._finalise(result)
        return result
