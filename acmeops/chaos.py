"""Deterministic failure injection for AcmeOps.

Reliability is one of CentrAlign's eight evaluation criteria, and a prototype
can only *claim* reliability unless the environment actually misbehaves. So the
environment misbehaves on purpose, in four ways a real enterprise app does:

1. ``transient_5xx``  - the server intermittently returns 503 on reads.
2. ``session_expiry`` - the portal logs you out part-way through a task.
3. ``layout_shift``  - the DOM is rendered in one of two variants, so any agent
   that memorised CSS selectors breaks and one that reads the page adapts.
4. ``slow_response``  - some requests take long enough to look like a hang.

Everything is driven by a seeded RNG, so a failing run is reproducible from the
seed printed in the trace. ``--chaos off`` disables it for a clean baseline.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field


@dataclass
class ChaosConfig:
    enabled: bool = True
    seed: int = 7
    transient_5xx_rate: float = 0.18
    slow_response_rate: float = 0.12
    slow_response_seconds: float = 2.5
    # Number of successful authenticated requests before the session is killed
    # once. Set to 0 to disable. One expiry per run is enough to prove recovery.
    session_expiry_after: int = 6
    layout_variant: str = "auto"  # "auto" | "a" | "b"

    _rng: random.Random = field(default_factory=random.Random, repr=False)
    _authed_requests: int = field(default=0, repr=False)
    _expiries_fired: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # --- decisions -----------------------------------------------------------

    def should_fail_read(self, path: str) -> bool:
        """Transient 503s on GETs only. Writes are never silently failed after
        committing - a half-applied write would make the task unverifiable
        rather than merely hard, which is a different (and unfair) problem."""
        if not self.enabled:
            return False
        if not path.startswith(("/portal", "/payables")):
            return False
        return self._rng.random() < self.transient_5xx_rate

    def should_be_slow(self) -> bool:
        return self.enabled and self._rng.random() < self.slow_response_rate

    def maybe_sleep(self) -> None:
        if self.should_be_slow():
            time.sleep(self.slow_response_seconds)

    def should_expire_session(self) -> bool:
        if not self.enabled or self.session_expiry_after <= 0:
            return False
        if self._expiries_fired >= 1:
            return False
        self._authed_requests += 1
        if self._authed_requests >= self.session_expiry_after:
            self._expiries_fired += 1
            return True
        return False

    def variant_for(self, path: str) -> str:
        """Which DOM variant to render. Stable per path within a run so a page
        doesn't flip shape between a read and the click that follows it."""
        if self.layout_variant in ("a", "b"):
            return self.layout_variant
        if not self.enabled:
            return "a"
        return "b" if (hash((self.seed, path)) % 2) else "a"

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "seed": self.seed,
            "transient_5xx_rate": self.transient_5xx_rate,
            "session_expiry_after": self.session_expiry_after,
            "layout_variant": self.layout_variant,
            "authed_requests_seen": self._authed_requests,
            "expiries_fired": self._expiries_fired,
        }


def from_env() -> ChaosConfig:
    mode = os.environ.get("ACMEOPS_CHAOS", "on").lower()
    seed = int(os.environ.get("ACMEOPS_CHAOS_SEED", "7"))
    return ChaosConfig(enabled=mode not in ("off", "0", "false"), seed=seed)


CHAOS = from_env()
