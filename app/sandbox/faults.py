"""Deliberate, controllable failure modes in the simulated world.

A prototype that only ever runs the happy path proves nothing about reliability.
These faults make the *world* unreliable in realistic ways so the agent's
retry / re-plan / self-correction behaviour is exercised on every demo run
rather than being a code path nobody ever sees.

Faults are process-global and settable at runtime (the operator console exposes
them) so the same task can be replayed with and without a given failure.
"""

from __future__ import annotations

from app.config import get_settings

ALL_FAULTS: dict[str, str] = {
    "flaky_login": "The portal's first sign-in attempt returns HTTP 503 (transient outage).",
    "slow_invoice": "The first invoice detail page load stalls for ~3s (near-timeout).",
    "strict_validation": "The finance form rejects '$12,480.00' style amounts and non-ISO dates.",
    "api_rate_limit": "Every 4th finance API call returns HTTP 429.",
}

_active: set[str] | None = None


def active_faults() -> set[str]:
    global _active
    if _active is None:
        _active = set(get_settings().sandbox_faults) & set(ALL_FAULTS)
    return _active


def set_faults(faults: list[str] | set[str]) -> set[str]:
    global _active
    _active = set(faults) & set(ALL_FAULTS)
    return _active


def is_active(name: str) -> bool:
    return name in active_faults()
