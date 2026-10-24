"""Provider-agnostic model access.

The agent needs three things from a model: text, structured JSON, and a tool
call. Two wire formats provide them - Anthropic's Messages API and the
OpenAI-compatible Chat Completions API that Groq, OpenAI, Together and most
local servers speak. This module normalises both behind one interface so
nothing above it knows which is in use.

That is not abstraction for its own sake. CentrAlign's problem statement
explicitly does not prescribe a model, and a control loop whose reliability
depends on one vendor's quirks is not a control loop worth submitting. Swapping
provider is `AGENT_PROVIDER=groq` and nothing else.

Two differences are worth knowing about when reading this:

* **Tool schemas.** Anthropic takes ``{name, description, input_schema}``;
  OpenAI-compatible takes ``{type: "function", function: {name, description,
  parameters}}``. Converted in :func:`_openai_tools`.
* **JSON reliability.** Open-weight models are markedly worse at "reply with
  JSON only" than frontier ones. Where the server supports it we ask for
  ``response_format={"type": "json_object"}``, and :func:`complete_json` keeps a
  repair round so one malformed reply does not fail a run.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import SETTINGS


class ModelError(RuntimeError):
    pass


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None

    @property
    def first_tool_call(self) -> ToolCall | None:
        return self.tool_calls[0] if self.tool_calls else None


# --- schema conversion -------------------------------------------------------


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped tool schemas -> OpenAI function-calling shape.

    Tools are declared once, in Anthropic's shape, because that is what the
    registry emits; this is the only place that has to know the other dialect.
    """
    converted = []
    for tool in tools:
        parameters = dict(tool.get("input_schema") or {"type": "object", "properties": {}})
        # Some OpenAI-compatible servers reject a schema without an explicit
        # properties map, which Anthropic tolerates.
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": parameters,
                },
            }
        )
    return converted


# --- providers ---------------------------------------------------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic

        self._sdk = anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ModelError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        response = self._client.messages.create(**kwargs)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [
            ToolCall(name=b.name, arguments=dict(b.input or {}))
            for b in response.content
            if b.type == "tool_use"
        ]
        return Completion(text=text, tool_calls=calls, raw=response)

    def transient_errors(self) -> tuple[type[Exception], ...]:
        return (
            self._sdk.APIConnectionError,
            self._sdk.RateLimitError,
            self._sdk.InternalServerError,
        )

    def fatal_errors(self) -> tuple[type[Exception], ...]:
        return (self._sdk.APIStatusError,)


class OpenAICompatibleProvider:
    """Groq, OpenAI, Together, vLLM, Ollama - anything speaking /chat/completions."""

    def __init__(self, name: str, base_url: str, api_key_env: str) -> None:
        from openai import OpenAI
        import openai

        self.name = name
        self._sdk = openai
        key = os.environ.get(api_key_env)
        if not key:
            raise ModelError(f"{api_key_env} is not set")
        self._client = OpenAI(api_key=key, base_url=base_url or None)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Completion:
        payload = [{"role": "system", "content": system}, *messages]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": payload,
        }
        if tools:
            kwargs["tools"] = _openai_tools(tools)
            # Force a tool call: the executor's contract is one action per turn,
            # and open models otherwise tend to narrate instead of acting.
            kwargs["tool_choice"] = "required"
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        # Servers and models disagree about which optional parameters they
        # accept, and reasoning models are the worst offenders: some reject a
        # non-default temperature outright, some want max_completion_tokens
        # instead of max_tokens, some refuse tool_choice="required". None of
        # that should end a run, so a rejection is repaired by dropping or
        # renaming the offending parameter and trying again. Each repair is
        # applied at most once, and a rejection we cannot explain is raised.
        repairs: list[tuple[str, Any]] = [
            ("max_tokens", lambda k: k.update(
                {"max_completion_tokens": k.pop("max_tokens")}
            ) if "max_tokens" in k else None),
            ("max_completion_tokens", lambda k: k.pop("max_completion_tokens", None)),
            ("temperature", lambda k: k.pop("temperature", None)),
            ("response_format", lambda k: k.pop("response_format", None)),
            ("json", lambda k: k.pop("response_format", None)),
            # Groq phrases this as "Tool choice is required, but model did not
            # call a tool" - with a space, not an underscore - so matching only
            # on "tool_choice" missed it entirely and the run died. Match the
            # prose and the error code as well as the parameter name.
            ("tool_choice", lambda k: k.update({"tool_choice": "auto"})),
            ("tool choice", lambda k: k.update({"tool_choice": "auto"})),
            ("tool_use_failed", lambda k: k.update({"tool_choice": "auto"})),
            ("did not call a tool", lambda k: k.update({"tool_choice": "auto"})),
        ]
        applied: set[str] = set()

        while True:
            try:
                response = self._client.chat.completions.create(**kwargs)
                break
            except self._sdk.BadRequestError as exc:
                message = str(exc).lower()
                for marker, repair in repairs:
                    if marker in message and marker not in applied:
                        applied.add(marker)
                        repair(kwargs)
                        break
                else:
                    raise ModelError(
                        f"{self.name} rejected the request and no known repair applies "
                        f"(already tried: {sorted(applied) or 'none'}): {exc}"
                    ) from exc

        choice = response.choices[0].message
        text = (choice.content or "").strip()
        calls: list[ToolCall] = []
        for call in choice.tool_calls or []:
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                # Some models emit arguments as a JSON-encoded string, or with
                # trailing prose. Salvage the object rather than lose the turn.
                match = re.search(r"\{.*\}", raw_args, re.S)
                if not match:
                    raise ModelError(
                        f"tool call {call.function.name} had unparseable arguments: {raw_args[:200]}"
                    ) from None
                arguments = json.loads(match.group(0))
            if not isinstance(arguments, dict):
                raise ModelError(f"tool call {call.function.name} arguments were not an object")
            calls.append(ToolCall(name=call.function.name, arguments=arguments))
        return Completion(text=text, tool_calls=calls, raw=response)

    def transient_errors(self) -> tuple[type[Exception], ...]:
        return (
            self._sdk.APIConnectionError,
            self._sdk.RateLimitError,
            self._sdk.InternalServerError,
        )

    def fatal_errors(self) -> tuple[type[Exception], ...]:
        return (self._sdk.APIStatusError,)


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "anthropic": {},
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "local": {
        "base_url": os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:11434/v1"),
        "api_key_env": "AGENT_API_KEY",
    },
}

_PROVIDER: Any = None


def provider() -> Any:
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    name = SETTINGS.provider.lower()
    if name not in PROVIDER_PRESETS:
        raise ModelError(
            f"unknown provider {name!r}. Choose one of {sorted(PROVIDER_PRESETS)}"
        )
    if name == "anthropic":
        _PROVIDER = AnthropicProvider()
    else:
        preset = PROVIDER_PRESETS[name]
        _PROVIDER = OpenAICompatibleProvider(
            name=name,
            base_url=os.environ.get("AGENT_BASE_URL", preset["base_url"]),
            api_key_env=preset["api_key_env"],
        )
    return _PROVIDER


def reset_provider() -> None:
    """Used by tests, and after changing provider settings at runtime."""
    global _PROVIDER
    _PROVIDER = None


def available_models(*, strict: bool = False) -> list[str]:
    """Model ids the configured server actually offers.

    Anthropic has no discovery endpoint on this SDK path, so it returns an empty
    list. ``strict=True`` surfaces the underlying error instead of swallowing it
    - used by ``--list-models``, where "no models" and "your key is rejected"
    must not look the same. Model *resolution* stays non-strict, because
    discovery there is only a convenience.
    """
    backend = provider()
    client = getattr(backend, "_client", None)
    if backend.name == "anthropic" or client is None:
        return []
    try:
        return sorted(m.id for m in client.models.list().data)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise ModelError(
                f"could not list models from {backend.name}: {type(exc).__name__}: {exc}"
            ) from exc
        return []


def resolve_model() -> str:
    """Settle SETTINGS.model at startup.

    For Anthropic and OpenAI it is a preset id. For Groq and local servers the
    catalogue changes often, so the ranked preference list in config is matched
    by substring against what the server reports. A hard-coded open-weight
    model id would be stale within months; this will not be.
    """
    from .config import DEFAULT_MODELS, MODEL_PREFERENCES, NON_CHAT_MODEL_MARKERS

    if SETTINGS.model:
        return SETTINGS.model

    name = SETTINGS.provider.lower()
    if name in DEFAULT_MODELS:
        SETTINGS.model = DEFAULT_MODELS[name]
        return SETTINGS.model

    offered = available_models()
    chat_models = [
        m for m in offered
        if not any(marker in m.lower() for marker in NON_CHAT_MODEL_MARKERS)
    ]

    for preference in MODEL_PREFERENCES.get(name, []):
        for candidate in chat_models:
            if preference in candidate.lower():
                SETTINGS.model = candidate
                return candidate

    if chat_models:
        # No preferred model is available. Take the first usable one, but say so
        # loudly: an unranked model may well be too weak to hold the loop, and
        # that should look like a configuration choice, not a mystery failure.
        SETTINGS.model = chat_models[0]
        print(
            f"warning: none of the preferred models are available on {name}. "
            f"Falling back to {SETTINGS.model!r}, which is untested for this loop. "
            f"Available: {chat_models}"
        )
        return SETTINGS.model

    raise ModelError(
        f"could not determine a model for provider {name!r}"
        + (f" - the only models offered are not chat models: {offered}" if offered else "")
        + ". Set AGENT_MODEL explicitly, or run `python run.py --list-models`."
    )


# --- the call ----------------------------------------------------------------


def complete(
    system: str,
    user: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
    attempts: int = 3,
) -> Completion:
    """One model call, retried on transient API failures."""
    backend = provider()
    messages = [{"role": "user", "content": user}]
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return backend.complete(
                system,
                messages,
                model=model or SETTINGS.model,
                max_tokens=max_tokens or SETTINGS.max_tokens,
                temperature=SETTINGS.temperature,
                tools=tools,
                json_mode=json_mode,
            )
        except backend.transient_errors() as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
        except backend.fatal_errors() as exc:
            raise ModelError(f"model call failed: {exc}") from exc
    raise ModelError(f"model call failed after {attempts} attempts: {last}")


def extract_json(raw: str) -> dict[str, Any]:
    """Pull an object out of a model reply that may be fenced or prefaced."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidate = fenced.group(1) if fenced else raw
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ModelError(f"expected JSON, got: {raw[:300]}")
    return json.loads(candidate[start : end + 1])


def complete_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """A call that must return a JSON object, with one repair round.

    The repair round exists for open-weight models, which fence, preface and
    trail prose far more often than frontier ones. Failing a whole run because
    a model wrapped its answer in commentary would be a harness bug, not a
    model limitation.
    """
    completion = complete(
        system, user, model=model, max_tokens=max_tokens, json_mode=True
    )
    try:
        return extract_json(completion.text)
    except (ModelError, json.JSONDecodeError) as first_error:
        repair = complete(
            "You convert text into a single valid JSON object. Reply with the JSON object "
            "and nothing else: no code fence, no commentary before or after.",
            f"Rewrite this as one valid JSON object matching the schema it was meant to follow:\n\n"
            f"{completion.text[:4000]}",
            model=model,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            return extract_json(repair.text)
        except (ModelError, json.JSONDecodeError) as exc:
            raise ModelError(
                f"could not get JSON from {provider().name}: {first_error}; repair also failed: {exc}"
            ) from exc
