"""Run persistence and live session registry.

Two different lifetimes are tracked here, deliberately kept apart:

* ``RunRecord`` - the durable account of what happened. Written to
  ``runs/<id>/run.json`` after every step, so a crashed process still leaves a
  complete, inspectable trail (and the evidence bundle survives it).
* ``RunSession`` - the live, in-memory execution state: the message transcript,
  the browser, the HTTP client, the guardrail counters. This cannot be
  serialised meaningfully and does not outlive the process.

A run suspended for human approval keeps its ``RunSession`` resident, which is
why approval resume works across minutes without holding any request open.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.schemas import RunRecord
from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class RunSession:
    """Everything needed to continue a run that is mid-flight."""

    record: RunRecord
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Set when the run suspended on a policy gate: the call to perform (or
    # refuse) once the operator answers.
    pending_tool_call: dict[str, Any] | None = None
    browser: Any = None
    # The verifier gets its own browser so it cannot inherit the worker's
    # logged-in session and mistake leftover state for independent evidence.
    verifier_browser: Any = None
    http: Any = None
    guardrails: Any = None
    secrets: Any = None
    brain: Any = None
    task: Any = None
    cancelled: bool = False

    async def aclose(self) -> None:
        for attr in ("browser", "verifier_browser"):
            browser = getattr(self, attr)
            if browser is None:
                continue
            try:
                await browser.stop()
            except Exception:  # pragma: no cover
                log.exception("Error closing %s for run %s", attr, self.record.id)
            setattr(self, attr, None)
        if self.http is not None:
            try:
                await self.http.aclose()
            except Exception:  # pragma: no cover
                log.exception("Error closing http client for run %s", self.record.id)
            self.http = None


class RunStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.runs_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, RunSession] = {}

    # ------------------------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create(self, record: RunRecord) -> RunSession:
        session = RunSession(record=record)
        self._sessions[record.id] = session
        self.run_dir(record.id)
        self.persist(record)
        return session

    def session(self, run_id: str) -> RunSession | None:
        return self._sessions.get(run_id)

    def record(self, run_id: str) -> RunRecord | None:
        session = self._sessions.get(run_id)
        if session is not None:
            return session.record
        return self.load(run_id)

    def list_records(self, limit: int = 50) -> list[RunRecord]:
        records: list[RunRecord] = []
        for path in sorted(self.root.glob("*/run.json"), reverse=True):
            record = self._read(path)
            if record is not None:
                records.append(record)
            if len(records) >= limit:
                break
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    # ------------------------------------------------------------------
    def persist(self, record: RunRecord) -> None:
        path = self.run_dir(record.id) / "run.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(record.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)  # atomic: never leave a half-written run.json
        except Exception:  # pragma: no cover
            log.exception("Failed to persist run %s", record.id)

    def persist_transcript(self, session: RunSession) -> None:
        """Dump the raw model transcript next to the run, for debugging.

        This is the single most useful artefact when an agent does something
        baffling: it is exactly what the model saw, in order.
        """
        path = self.run_dir(session.record.id) / "transcript.json"
        try:
            path.write_text(
                json.dumps(_jsonable(session.messages), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # pragma: no cover
            log.exception("Failed to persist transcript for %s", session.record.id)

    def load(self, run_id: str) -> RunRecord | None:
        return self._read(self.root / run_id / "run.json")

    @staticmethod
    def _read(path: Path) -> RunRecord | None:
        if not path.is_file():
            return None
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover
            log.exception("Could not read run file %s", path)
            return None

    async def release(self, run_id: str) -> None:
        session = self._sessions.pop(run_id, None)
        if session is not None:
            await session.aclose()


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of SDK content blocks into plain JSON."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump())
        except Exception:
            return str(value)
    return str(value)
