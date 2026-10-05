"""Durable types for a run.

Everything the agent does is recorded as one of these and persisted to
``runs/<run_id>/run.json``. The operator console, the evidence bundle and the
tests all read the same structures - there is no second, parallel representation
of what happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal[
    "queued",
    "running",
    "awaiting_human",
    "verifying",
    "succeeded",
    "failed",
    "cancelled",
]

StepPhase = Literal["execute", "verify"]
StepStatus = Literal["ok", "error", "blocked", "awaiting_human"]

# How a failed tool call should be handled. The executor decides this; the
# model never sees the label, only the remediation text.
ErrorKind = Literal[
    "transient",     # retry verbatim - network blip, 503, 429
    "not_found",     # the thing referenced is gone - re-observe, then adapt
    "invalid_input", # the agent's arguments were wrong - fix and resubmit
    "blocked",       # policy refused - needs approval or a different route
    "fatal",         # unrecoverable - abort the run
]

Verdict = Literal["verified", "refuted", "inconclusive"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Artifact(BaseModel):
    """A file on disk produced during a run - the 'evidence' half of the brief."""

    kind: Literal["screenshot", "file", "json", "html"]
    label: str
    path: str                 # relative to the run directory
    step_index: int | None = None
    created_at: str = Field(default_factory=_now)


class Fact(BaseModel):
    """Something the agent learned and chose to keep.

    Facts are the agent's *durable* memory: they are re-rendered into every
    subsequent prompt, so they survive even when older transcript detail is no
    longer being attended to, and they are what the verifier checks against.
    """

    key: str
    value: str
    note: str = ""
    step_index: int | None = None
    recorded_at: str = Field(default_factory=_now)


class Step(BaseModel):
    index: int
    phase: StepPhase = "execute"
    thought: str = ""                 # the model's own words before acting
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = "ok"
    observation: str = ""             # exactly what was fed back to the model
    error_kind: ErrorKind | None = None
    retries: int = 0
    duration_ms: int = 0
    artifacts: list[Artifact] = Field(default_factory=list)
    started_at: str = Field(default_factory=_now)
    ended_at: str | None = None


class HumanPrompt(BaseModel):
    """A question or approval request that has suspended the run."""

    id: str = Field(default_factory=lambda: new_id("hp"))
    kind: Literal["question", "approval"]
    question: str
    details: str = ""
    options: list[str] = Field(default_factory=list)
    risk: str = ""
    asked_at: str = Field(default_factory=_now)
    answer: str | None = None
    approved: bool | None = None
    answered_at: str | None = None


class ClaimCheck(BaseModel):
    key: str
    claimed: str
    observed: str = ""
    ok: bool = False
    note: str = ""


class VerificationReport(BaseModel):
    verdict: Verdict = "inconclusive"
    round: int = 1
    checks: list[ClaimCheck] = Field(default_factory=list)
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
    checked_at: str = Field(default_factory=_now)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    llm_calls: int = 0

    def add(self, usage: Any) -> None:
        self.llm_calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


class RunRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    goal: str
    status: RunStatus = "queued"
    created_at: str = Field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    config: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)

    claims: dict[str, str] = Field(default_factory=dict)
    agent_summary: str = ""
    verification: VerificationReport | None = None
    verification_history: list[VerificationReport] = Field(default_factory=list)

    pending_prompt: HumanPrompt | None = None
    human_exchanges: list[HumanPrompt] = Field(default_factory=list)

    outcome: str = ""            # the final answer handed back to the user
    failure_reason: str = ""
    usage: Usage = Field(default_factory=Usage)

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def fact_dict(self) -> dict[str, str]:
        return {f.key: f.value for f in self.facts}
