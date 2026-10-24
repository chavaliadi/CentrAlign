"""Provider-abstraction tests. No network, no API key.

The provider layer is where a subtle bug would be most expensive: a tool schema
converted wrongly means every tool call fails, and a JSON reply parsed loosely
means a run dies on a model's stray commentary. Both are checked here against a
stub provider.

Run:  python -m tests.test_llm_provider
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent import llm  # noqa: E402
from agent.config import SETTINGS  # noqa: E402
from agent.llm import Completion, ModelError, ToolCall, _openai_tools, extract_json  # noqa: E402
from agent.tools.base import ToolRegistry, obj_schema, string  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


class StubProvider:
    """Returns scripted completions and records what it was asked for."""

    name = "stub"

    def __init__(self, replies: list[Completion]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, messages: list[dict[str, Any]], **kwargs: Any) -> Completion:
        self.calls.append({"system": system, "messages": messages, **kwargs})
        return self.replies.pop(0) if self.replies else Completion(text="{}")

    def transient_errors(self) -> tuple[type[Exception], ...]:
        return ()

    def fatal_errors(self) -> tuple[type[Exception], ...]:
        return ()


def with_stub(replies: list[Completion]) -> StubProvider:
    stub = StubProvider(replies)
    llm._PROVIDER = stub  # noqa: SLF001 - injecting the provider is the point
    return stub


def run() -> int:
    print("\n[1] tool schemas convert to OpenAI function-calling shape")
    registry = ToolRegistry()

    @registry.tool("do_thing", "Does a thing.\nSecond line ignored in summaries.",
                   obj_schema({"target": string("what to act on")}, ["target"]))
    def do_thing(target: str) -> Any:  # type: ignore[misc]
        return None

    @registry.tool("no_args", "Takes nothing.", obj_schema({}))
    def no_args() -> Any:  # type: ignore[misc]
        return None

    anthropic_shape = registry.api_schemas()
    converted = _openai_tools(anthropic_shape)

    check("every tool survives conversion", len(converted) == 2, str(len(converted)))
    first = next(c for c in converted if c["function"]["name"] == "do_thing")
    check("wrapped as type=function", first["type"] == "function", str(first["type"]))
    check("input_schema becomes function.parameters",
          first["function"]["parameters"]["properties"]["target"]["type"] == "string",
          str(first["function"]["parameters"]))
    check("required list is preserved",
          first["function"]["parameters"]["required"] == ["target"],
          str(first["function"]["parameters"].get("required")))
    empty = next(c for c in converted if c["function"]["name"] == "no_args")
    check("a no-argument tool still declares an object schema with properties",
          empty["function"]["parameters"]["type"] == "object"
          and empty["function"]["parameters"]["properties"] == {},
          str(empty["function"]["parameters"]))
    check("conversion does not mutate the original schemas",
          "input_schema" in anthropic_shape[0], str(anthropic_shape[0].keys()))

    print("\n[2] JSON extraction survives how models actually reply")
    cases = {
        "bare object": '{"verdict": "confirmed"}',
        "fenced": '```json\n{"verdict": "confirmed"}\n```',
        "unlabelled fence": '```\n{"verdict": "confirmed"}\n```',
        "prefaced with prose": 'Sure, here you go:\n{"verdict": "confirmed"}',
        "trailing prose": '{"verdict": "confirmed"}\nHope that helps!',
    }
    for label, raw in cases.items():
        try:
            parsed = extract_json(raw)
            ok = parsed.get("verdict") == "confirmed"
            detail = str(parsed)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, repr(exc)
        check(f"parses a reply {label}", ok, detail)

    try:
        extract_json("there is no json here at all")
        check("a reply with no object raises rather than guessing", False, "did not raise")
    except ModelError:
        check("a reply with no object raises rather than guessing", True)

    print("\n[3] complete_json repairs one malformed reply instead of failing the run")
    stub = with_stub([
        Completion(text="Here's my answer: {verdict: confirmed,}"),   # invalid JSON
        Completion(text='{"verdict": "confirmed"}'),                   # the repair
    ])
    result = llm.complete_json("sys", "user")
    check("the malformed reply did not end the run", result.get("verdict") == "confirmed", str(result))
    check("exactly one repair call was made", len(stub.calls) == 2, str(len(stub.calls)))
    check("the repair prompt asks for JSON only",
          "nothing else" in stub.calls[1]["system"].lower(), stub.calls[1]["system"][:80])

    stub = with_stub([Completion(text="no json"), Completion(text="still no json")])
    try:
        llm.complete_json("sys", "user")
        check("a second failure raises ModelError", False, "did not raise")
    except ModelError as exc:
        check("a second failure raises ModelError", True)
        check("the error names the provider so it is debuggable",
              "stub" in str(exc), str(exc)[:120])

    print("\n[4] tool calls are returned to the planner intact")
    stub = with_stub([
        Completion(text="I'll read the page.",
                   tool_calls=[ToolCall("browser_read_page", {"url": "/portal"})])
    ])
    completion = llm.complete("sys", "user", tools=registry.api_schemas())
    call = completion.first_tool_call
    check("tool name round-trips", call and call.name == "browser_read_page", str(call))
    check("tool arguments round-trip as a dict",
          call and call.arguments == {"url": "/portal"}, str(call))
    check("accompanying text is kept for the trace",
          "read the page" in completion.text, completion.text)
    check("tools were passed through to the provider",
          stub.calls[0].get("tools") is not None, "tools missing")

    print("\n[5] json_mode is requested for structured calls, not for tool calls")
    stub = with_stub([Completion(text='{"ok": true}')])
    llm.complete_json("sys", "user")
    check("structured calls ask for json mode", stub.calls[0].get("json_mode") is True,
          str(stub.calls[0].get("json_mode")))
    stub = with_stub([Completion(tool_calls=[ToolCall("no_args", {})])])
    llm.complete("sys", "user", tools=registry.api_schemas())
    check("tool calls do not ask for json mode", not stub.calls[0].get("json_mode"),
          str(stub.calls[0].get("json_mode")))

    print("\n[6] provider selection follows the keys present")
    import os

    from agent.config import _default_provider

    saved = {k: os.environ.get(k) for k in
             ("AGENT_PROVIDER", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ["GROQ_API_KEY"] = "x"
        check("groq key alone selects groq", _default_provider() == "groq", _default_provider())
        os.environ["ANTHROPIC_API_KEY"] = "y"
        check("anthropic wins when both keys are set",
              _default_provider() == "anthropic", _default_provider())
        os.environ["AGENT_PROVIDER"] = "groq"
        check("an explicit AGENT_PROVIDER overrides key inference",
              _default_provider() == "groq", _default_provider())
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        llm.reset_provider()

    print("\n[7] model resolution against a real Groq catalogue")
    # The exact catalogue one account was offered. Speech, moderation and
    # guard models sit alongside two usable chat models.
    REAL_GROQ_CATALOGUE = [
        "allam-2-7b",
        "canopylabs/orpheus-arabic-saudi",
        "canopylabs/orpheus-v1-english",
        "meta-llama/llama-prompt-guard-2-22m",
        "meta-llama/llama-prompt-guard-2-86m",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-safeguard-20b",
        "qwen/qwen3.8-27b",
        "whisper-large-v3",
        "whisper-large-v3-turbo",
    ]

    saved_model, saved_provider = SETTINGS.model, SETTINGS.provider

    def resolve_against(catalogue: list[str]) -> str:
        SETTINGS.model = ""
        SETTINGS.provider = "groq"
        llm.available_models = lambda **_kw: catalogue  # type: ignore[assignment]
        return llm.resolve_model()

    real_available = llm.available_models
    try:
        picked = resolve_against(REAL_GROQ_CATALOGUE)
        check("picks the strongest ranked chat model on offer",
              picked == "openai/gpt-oss-120b", picked)

        picked = resolve_against([m for m in REAL_GROQ_CATALOGUE if "120b" not in m])
        check("falls back down the ranking, not to the top of the list",
              picked == "openai/gpt-oss-20b", picked)

        picked = resolve_against(
            [m for m in REAL_GROQ_CATALOGUE if "gpt-oss" not in m]
        )
        check("reaches qwen3 when no gpt-oss model is offered",
              picked == "qwen/qwen3.8-27b", picked)

        # Only unusable models left: must not silently pick Whisper.
        picked = resolve_against(["whisper-large-v3", "meta-llama/llama-prompt-guard-2-22m"])
        check("never selects a speech or guard model", False, f"selected {picked}")
    except ModelError as exc:
        check("never selects a speech or guard model",
              "not chat models" in str(exc), str(exc)[:140])
    finally:
        llm.available_models = real_available  # type: ignore[assignment]
        SETTINGS.model, SETTINGS.provider = saved_model, saved_provider
        llm.reset_provider()

    print("\n[8] rejected parameters are repaired, not fatal")
    # Reasoning models are picky: some want max_completion_tokens instead of
    # max_tokens, some reject a non-default temperature. Neither should end a
    # run, and the repairs must be tried in order and only once each.
    class FakeBadRequest(Exception):
        pass

    class FakeSDK:
        BadRequestError = FakeBadRequest
        APIConnectionError = type("A", (Exception,), {})
        RateLimitError = type("B", (Exception,), {})
        InternalServerError = type("C", (Exception,), {})
        APIStatusError = type("D", (Exception,), {})

    class FakeCompletions:
        def __init__(self, reject: list[str]) -> None:
            self.reject = list(reject)
            self.seen: list[dict[str, Any]] = []

        def create(self, **kw: Any) -> Any:
            self.seen.append(dict(kw))
            for marker in self.reject:
                if marker in kw:
                    raise FakeBadRequest(f"Unsupported parameter: '{marker}' is not supported")

            class M:
                content = "ok"
                tool_calls = None

            class C:
                message = M()

            class R:
                choices = [C()]

            return R()

    def fake_provider(reject: list[str]) -> Any:
        p = llm.OpenAICompatibleProvider.__new__(llm.OpenAICompatibleProvider)
        p.name = "fake"
        p._sdk = FakeSDK()  # noqa: SLF001

        class Holder:
            pass

        p._client = Holder()  # noqa: SLF001
        p._client.chat = Holder()  # noqa: SLF001
        p._client.chat.completions = FakeCompletions(reject)  # noqa: SLF001
        return p

    provider_obj = fake_provider(["max_tokens", "temperature"])
    result = provider_obj.complete(
        "sys", [{"role": "user", "content": "hi"}], model="m", max_tokens=100, temperature=0.0
    )
    seen = provider_obj._client.chat.completions.seen  # noqa: SLF001
    check("the call eventually succeeded", result.text == "ok", result.text)
    check("it took exactly one attempt per repair", len(seen) == 3, str(len(seen)))
    check("max_tokens was renamed to max_completion_tokens",
          "max_completion_tokens" in seen[1] and "max_tokens" not in seen[1],
          str(sorted(seen[1])))
    check("then the rejected temperature was dropped",
          "temperature" not in seen[2], str(sorted(seen[2])))

    provider_obj = fake_provider(["some_unknown_param"])
    provider_obj._client.chat.completions.reject = ["model"]  # noqa: SLF001
    try:
        provider_obj.complete("sys", [{"role": "user", "content": "hi"}],
                              model="m", max_tokens=10, temperature=0.0)
        check("an unrepairable rejection raises instead of looping", False, "did not raise")
    except ModelError as exc:
        check("an unrepairable rejection raises instead of looping", True)
        check("and the error says what was already tried", "already tried" in str(exc),
              str(exc)[:140])

    print("\n" + "=" * 60)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())
