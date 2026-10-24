"""Tool protocol and registry.

The loop never names a tool. It asks the registry for schemas, hands them to
the model, and dispatches whatever the model picks. Adding a capability is
therefore a new file plus one ``register()`` call - no change to the planner or
the controller. That is the whole of the generalisation story, and it is the
reason ``loop.py`` contains no mention of browsers, PDFs or payables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class Observation:
    """The result of one action, as the loop and the model both see it."""

    ok: bool
    summary: str                       # short natural-language result, goes to the model
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False            # transient (5xx, timeout) vs. a real dead end
    # Facts worth putting in working memory: {key: (value, note)}
    facts: dict[str, tuple[Any, str]] = field(default_factory=dict)
    # Path to a screenshot or downloaded artefact produced by this action.
    artifacts: list[str] = field(default_factory=list)

    @classmethod
    def failure(cls, error: str, *, retryable: bool = False, summary: str = "") -> "Observation":
        return cls(ok=False, summary=summary or f"Action failed: {error}", error=error, retryable=retryable)


class ToolCallable(Protocol):
    def __call__(self, **kwargs: Any) -> Observation: ...


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolCallable
    # Mutating tools change state outside the agent. The loop routes these
    # through the approval gate before they run.
    mutating: bool = False

    def to_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        mutating: bool = False,
    ) -> Callable[[ToolCallable], ToolCallable]:
        """Decorator form, so a tool module reads as a list of capabilities."""

        def decorator(fn: ToolCallable) -> ToolCallable:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    fn=fn,
                    mutating=mutating,
                )
            )
            return fn

        return decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def api_schemas(self) -> list[dict[str, Any]]:
        return [self._tools[n].to_api_schema() for n in self.names()]

    def describe(self) -> str:
        return "\n".join(
            f"- {t.name}{' [mutating, needs approval]' if t.mutating else ''}: "
            f"{t.description.strip().splitlines()[0]}"
            for t in (self._tools[n] for n in self.names())
        )


def obj_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def string(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def number(desc: str) -> dict[str, Any]:
    return {"type": "number", "description": desc}


def boolean(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}
