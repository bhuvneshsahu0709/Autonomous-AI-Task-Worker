"""Budgets and stuck-detection.

An autonomous loop needs something outside the model deciding when it has gone
wrong, because a model that is confused is usually confidently confused. These
checks are cheap, deterministic, and run every iteration.

Two kinds of response:

* **Interventions** - a supervisor note injected into the transcript. The run
  continues, but the model is told plainly that it is repeating itself or that
  everything is failing. In practice this is what breaks most doom-loops.
* **Stops** - the run is over. Budget exhausted, or the failure rate says the
  environment is not in a state the agent can recover from.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from app.agent import prompts
from app.config import Settings


@dataclass
class StopReason:
    code: str
    message: str


@dataclass
class Guardrails:
    settings: Settings
    started_at: float = field(default_factory=time.monotonic)

    steps_used: int = 0
    consecutive_errors: int = 0
    call_counts: dict[str, int] = field(default_factory=dict)
    _warned_budget: bool = False
    _intervened: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    @staticmethod
    def call_signature(tool: str, args: dict) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except TypeError:
            payload = str(sorted(args.items()))
        digest = hashlib.sha1(f"{tool}:{payload}".encode()).hexdigest()[:12]
        return f"{tool}:{digest}"

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def steps_remaining(self) -> int:
        return max(0, self.settings.max_steps - self.steps_used)

    # ------------------------------------------------------------------
    def should_stop(self) -> StopReason | None:
        if self.steps_used >= self.settings.max_steps:
            return StopReason(
                "step_budget",
                f"Step budget exhausted after {self.steps_used} actions.",
            )
        if self.elapsed_seconds >= self.settings.max_seconds:
            return StopReason(
                "time_budget",
                f"Time budget exhausted after {int(self.elapsed_seconds)}s.",
            )
        if self.consecutive_errors >= self.settings.max_consecutive_errors:
            return StopReason(
                "error_rate",
                f"{self.consecutive_errors} actions failed in a row; the agent is not making progress.",
            )
        return None

    def record_call(self, tool: str, args: dict) -> int:
        signature = self.call_signature(tool, args)
        self.call_counts[signature] = self.call_counts.get(signature, 0) + 1
        return self.call_counts[signature]

    def record_outcome(self, *, ok: bool) -> None:
        self.steps_used += 1
        self.consecutive_errors = 0 if ok else self.consecutive_errors + 1

    # ------------------------------------------------------------------
    def intervention(self, tool: str, args: dict) -> str | None:
        """A note to inject into the transcript, or None.

        Each distinct intervention fires once - repeating the same nag every
        turn would just become noise the model learns to ignore.
        """
        signature = self.call_signature(tool, args)
        repeats = self.call_counts.get(signature, 0)
        limit = self.settings.max_repeat_identical_calls

        if repeats >= limit and f"repeat:{signature}" not in self._intervened:
            self._intervened.add(f"repeat:{signature}")
            return prompts.intervention_repeat(tool, repeats)

        if self.consecutive_errors >= 2:
            key = f"errors:{self.consecutive_errors}"
            if key not in self._intervened:
                self._intervened.add(key)
                return prompts.intervention_errors(self.consecutive_errors)

        remaining = self.steps_remaining
        if remaining <= max(5, self.settings.max_steps // 5) and not self._warned_budget:
            self._warned_budget = True
            return prompts.budget_warning(remaining)

        return None
