"""Evidence capture.

Every run writes a self-contained folder: the full trace, the screenshots, any
downloaded documents, and a human-readable report. The task is not considered
delivered because the agent said something in a terminal - it is delivered
because there is a folder a colleague can open and check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SETTINGS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunRecorder:
    task_id: str
    task_text: str
    run_dir: Path = field(init=False)
    started_at: str = field(default_factory=_now)
    events: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_dir = SETTINGS.runs_dir / f"{stamp}-{self.task_id}"
        (self.run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "downloads").mkdir(parents=True, exist_ok=True)

    @property
    def screenshots_dir(self) -> Path:
        return self.run_dir / "screenshots"

    @property
    def downloads_dir(self) -> Path:
        return self.run_dir / "downloads"

    def event(self, kind: str, **payload: Any) -> None:
        self.events.append({"at": _now(), "kind": kind, **payload})

    # --- outputs -------------------------------------------------------------

    def write_trace(self, **extra: Any) -> Path:
        path = self.run_dir / "trace.json"
        payload = {
            "task_id": self.task_id,
            "task": self.task_text,
            "started_at": self.started_at,
            "finished_at": _now(),
            "settings": {
                "provider": SETTINGS.provider,
                "model": SETTINGS.model,
                "verifier_model": SETTINGS.resolved_verifier_model(),
                "max_steps": SETTINGS.max_steps,
                "max_replans": SETTINGS.max_replans,
                "auto_approve": SETTINGS.auto_approve,
                "base_url": SETTINGS.base_url,
            },
            **self.meta,
            **extra,
            "events": self.events,
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def write_report(self, result: dict[str, Any]) -> Path:
        path = self.run_dir / "report.md"
        verdict = result.get("verification") or {}
        declared = result.get("declared") or {}
        lines: list[str] = [
            f"# Run report - {self.task_id}",
            "",
            f"**Task.** {self.task_text}",
            "",
            f"- Status: **{result.get('status', 'unknown')}**",
            f"- Model: `{SETTINGS.provider}:{SETTINGS.model or '(unset)'}`",
            f"- Steps used: {result.get('steps_used')} of {SETTINGS.max_steps}",
            f"- Re-plans: {result.get('replans', 0)}",
            f"- Verification: **{verdict.get('verdict', 'not run')}**",
            "",
        ]

        contract = result.get("contract") or {}
        if contract:
            lines += [
                "## Definition of done",
                "",
                "This is the outcome contract used to plan, execute, and verify the run.",
                "",
                "```json",
                json.dumps(contract, indent=2, default=str),
                "```",
                "",
            ]

        # A run that errored must say why here. Printing only the status left a
        # reader with "status: error" and nowhere to look, which is worse than
        # no report at all.
        if result.get("error"):
            lines += [
                "## Error",
                "",
                "This run did not complete. The failure was:",
                "",
                "```",
                str(result["error"]),
                "```",
                "",
            ]
            errors = [e for e in self.events if e["kind"] == "error"]
            if errors:
                lines += ["Recorded error events:", ""]
                lines += [f"- step {e.get('step', '-')}: {e.get('error')}" for e in errors]
                lines.append("")

        if declared.get("answer"):
            lines += ["## Answer", "", str(declared["answer"]), ""]
        if declared.get("outcome"):
            lines += ["## What the worker did", "", str(declared["outcome"]), ""]

        if verdict:
            lines += ["## Independent verification", "", f"{verdict.get('reason', '')}", ""]
            criteria = verdict.get("criteria") or []
            if criteria:
                lines.append("| Criterion | Met | Evidence in system state |")
                lines.append("| --- | --- | --- |")
                for c in criteria:
                    mark = "yes" if c.get("met") else "NO"
                    crit = str(c.get("criterion", "")).replace("|", "\\|")
                    ev = str(c.get("evidence", "")).replace("|", "\\|")
                    lines.append(f"| {crit} | {mark} | {ev} |")
                lines.append("")
            if verdict.get("discrepancies"):
                lines += ["**Discrepancies found:**", ""]
                lines += [f"- {d}" for d in verdict["discrepancies"]]
                lines.append("")

        approvals = [e for e in self.events if e["kind"] == "approval"]
        if approvals:
            lines += ["## Approvals requested", ""]
            for a in approvals:
                lines.append(
                    f"- `{a.get('tool')}` - {a.get('decision')} "
                    f"({'auto-approved' if a.get('auto') else 'human decision'})"
                )
            lines.append("")

        questions = [e for e in self.events if e["kind"] == "human_question"]
        if questions:
            lines += ["## Questions put to the human", ""]
            for q in questions:
                lines.append(f"- {q.get('question')} -> {q.get('answer') or '(no answer)'}")
            lines.append("")

        shots = sorted(p.name for p in self.screenshots_dir.glob("*.png"))
        if shots:
            lines += ["## Screenshots", ""]
            lines += [f"![{s}](screenshots/{s})" for s in shots]
            lines.append("")

        downloads = sorted(p.name for p in self.downloads_dir.glob("*"))
        if downloads:
            lines += ["## Documents retrieved", ""]
            lines += [f"- `downloads/{d}`" for d in downloads]
            lines.append("")

        lines += ["## Step log", ""]
        for e in self.events:
            if e["kind"] != "action":
                continue
            status = "ok" if e.get("ok") else "FAILED"
            lines.append(f"**{e.get('step')}. `{e.get('tool')}`** - {status}")
            if e.get("arguments"):
                lines.append(f"    args: `{json.dumps(e['arguments'], default=str)[:220]}`")
            lines.append(f"    {str(e.get('summary', ''))[:400]}")
            lines.append("")

        lines += ["---", "", "Full machine-readable trace: `trace.json`."]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
