"""Memory for a single run.

Two kinds, because they answer different questions:

* **Working memory** - discrete facts the agent discovered, each with the step
  and source it came from. This is what lets a later step use something learnt
  earlier ("the total payable I read off the PDF") without re-deriving it, and
  what gives the final report its provenance.
* **Episodic memory** - the ordered log of actions and observations. Recent
  entries go to the planner verbatim; older ones are compacted into a summary
  so a long run stays inside the context window.

Nothing here is persisted across runs. Cross-run company memory is a stated
non-goal of this prototype and is called out in the README.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Fact:
    key: str
    value: Any
    source: str          # where it came from: a URL, a file path, a tool name
    step: int            # which step established it
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Episode:
    step: int
    action: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Memory:
    facts: dict[str, Fact] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)

    # --- working memory ------------------------------------------------------

    def remember(self, key: str, value: Any, source: str, step: int, note: str = "") -> None:
        self.facts[key] = Fact(key=key, value=value, source=source, step=step, note=note)

    def recall(self, key: str) -> Any | None:
        fact = self.facts.get(key)
        return fact.value if fact else None

    def facts_block(self) -> str:
        if not self.facts:
            return "(nothing recorded yet)"
        lines = []
        for fact in self.facts.values():
            note = f"  // {fact.note}" if fact.note else ""
            lines.append(
                f"- {fact.key} = {json.dumps(fact.value, default=str)}"
                f"  [step {fact.step}, source: {fact.source}]{note}"
            )
        return "\n".join(lines)

    # --- episodic memory -----------------------------------------------------

    def log(self, episode: Episode) -> None:
        self.episodes.append(episode)

    def compacted_history(self, verbatim_window: int) -> str:
        """Recent steps in full, earlier ones as a one-line-each digest."""
        if not self.episodes:
            return "(no actions taken yet)"
        older = self.episodes[:-verbatim_window] if verbatim_window else self.episodes
        recent = self.episodes[-verbatim_window:] if verbatim_window else []

        parts: list[str] = []
        if older:
            digest = "; ".join(
                f"#{e.step} {e.action}{'' if e.ok else ' (FAILED)'}" for e in older
            )
            parts.append(f"Earlier steps (compacted): {digest}")
        for e in recent:
            status = "ok" if e.ok else f"FAILED: {e.error}"
            parts.append(
                f"#{e.step} {e.action}({json.dumps(e.arguments, default=str)[:300]}) -> {status}\n"
                f"   observed: {e.summary}"
            )
        return "\n".join(parts)

    def failure_streak(self) -> int:
        streak = 0
        for episode in reversed(self.episodes):
            if episode.ok:
                break
            streak += 1
        return streak

    def repeat_count(self, action: str, arguments: dict[str, Any]) -> int:
        """How many times this exact action has already been taken.

        Counts successes as well as failures, which is the point: an agent can
        burn its entire budget on actions that all return ok and change
        nothing - re-reading the same page, re-opening the same URL. A
        failure-only stuck detector never sees it.
        """
        signature = (action, json.dumps(arguments, sort_keys=True, default=str))
        return sum(
            1
            for e in self.episodes
            if (e.action, json.dumps(e.arguments, sort_keys=True, default=str)) == signature
        )

    def no_progress_streak(self) -> int:
        """Consecutive recent actions that established no new fact and changed
        no state - read-only actions repeating themselves."""
        streak = 0
        for episode in reversed(self.episodes):
            if not episode.ok:
                break
            if "identical to the one you have already read" in episode.summary:
                streak += 1
                continue
            break
        return streak

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": {k: v.as_dict() for k, v in self.facts.items()},
            "episodes": [e.as_dict() for e in self.episodes],
        }
