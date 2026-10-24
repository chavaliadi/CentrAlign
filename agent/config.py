"""Runtime configuration for the task worker."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_provider() -> str:
    """Pick a provider from whichever key is actually present.

    Explicit AGENT_PROVIDER always wins. Otherwise prefer Anthropic if its key
    is set, then Groq, then OpenAI - so a contributor with one key set does not
    have to configure anything.
    """
    explicit = os.environ.get("AGENT_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    for key, name in (
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("GROQ_API_KEY", "groq"),
        ("OPENAI_API_KEY", "openai"),
    ):
        if os.environ.get(key):
            return name
    return "anthropic"


# Default model per provider when AGENT_MODEL is unset. For the
# OpenAI-compatible providers these are preference *rankings* matched by
# substring against the server's own /models list, because the available
# open-weight models change often and a hard-coded id goes stale.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4.1",
}

MODEL_PREFERENCES: dict[str, list[str]] = {
    # Ranked by how well they hold a multi-turn tool-use loop, best first.
    # Matched as substrings, so entries for models a given account cannot see
    # are simply skipped - the list is a preference, not a requirement.
    "groq": [
        "kimi-k2",
        "gpt-oss-120b",
        "llama-4-maverick",
        "llama-4-scout",
        "llama-3.3-70b",
        "gpt-oss-20b",
        "qwen3",
    ],
    "local": ["qwen", "llama", "mistral"],
}

# A /models list mixes in speech, moderation and embedding models that cannot
# hold a conversation, let alone call a tool. They must never be chosen as a
# last-resort fallback - picking a Whisper model would fail in a way that looks
# like an agent bug rather than a configuration one.
NON_CHAT_MODEL_MARKERS: tuple[str, ...] = (
    "whisper",
    "orpheus",
    "tts",
    "prompt-guard",
    "safeguard",
    "guard",
    "embed",
    "rerank",
    "moderation",
    "distil-whisper",
)


@dataclass
class Settings:
    # --- model ---------------------------------------------------------------
    provider: str = field(default_factory=_default_provider)
    # Empty means "resolve at startup": the preset default for Anthropic/OpenAI,
    # or the best available match for Groq and local servers.
    model: str = os.environ.get("AGENT_MODEL", "")
    # A deliberately cheaper model for the verifier is fine - verification is a
    # narrower judgement than planning - but defaults to the same one so a
    # reviewer isn't comparing two variables at once.
    verifier_model: str = os.environ.get("AGENT_VERIFIER_MODEL", "")
    max_tokens: int = 2048
    temperature: float = 0.0

    # --- budgets -------------------------------------------------------------
    # A step is one action. The budget is what stops a confused agent from
    # looping forever; hitting it is reported as a failure, not a success.
    max_steps: int = int(os.environ.get("AGENT_MAX_STEPS", "28"))
    # How many times the whole plan may be revised after a failed verification.
    max_replans: int = int(os.environ.get("AGENT_MAX_REPLANS", "2"))
    # Per-action retries for errors the tool marked retryable.
    max_action_retries: int = 2
    retry_backoff_seconds: float = 1.5

       # --- context management --------------------------------------------------
    # Observations older than this many steps are replaced by a compacted
    # summary so a long run does not blow the context window - or the budget.
    #
    # These numbers matter more than they look. The whole recent history is
    # re-sent on every step, so token cost grows with the SQUARE of the run
    # length: a window of 8 observations at 6000 characters each meant ~12k
    # tokens of history per call, and a 30-step run cost over 300k tokens.
    # That exhausts a free-tier daily allowance in a single run. Four
    # observations at 2500 characters keeps a step near 4k tokens, which is
    # enough context to recover from a failure and cheap enough to iterate on.
    verbatim_observation_window: int = 4
    max_observation_chars: int = 2500

    # --- environment ---------------------------------------------------------
    base_url: str = os.environ.get("ACMEOPS_URL", "http://127.0.0.1:8099")
    headless: bool = os.environ.get("AGENT_HEADLESS", "1") not in ("0", "false", "False")
    browser_executable: str = os.environ.get(
        "PLAYWRIGHT_CHROMIUM_PATH", "/opt/pw-browsers/chromium/chrome-linux/chrome"
    )
    nav_timeout_ms: int = 15000

    # --- human in the loop ---------------------------------------------------
    # When False, every mutating action pauses for a typed y/n at the console.
    # When True, approvals are auto-granted but still recorded in the trace, so
    # an unattended run is reproducible and the gate remains auditable.
    auto_approve: bool = False

    # --- output --------------------------------------------------------------
    runs_dir: Path = field(default_factory=lambda: REPO_ROOT / "runs")

    def resolved_verifier_model(self) -> str:
        return self.verifier_model or self.model

    def describe(self) -> str:
        return f"{self.provider}:{self.model or '(unresolved)'}"


SETTINGS = Settings()
