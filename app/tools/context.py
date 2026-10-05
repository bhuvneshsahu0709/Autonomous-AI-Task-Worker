"""Shared state handed to every tool call."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.agent.redaction import SecretRegistry
from app.agent.schemas import RunRecord, StepPhase
from app.config import Settings

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from app.tools.browser import BrowserSession


@dataclass
class ToolContext:
    run: RunRecord
    settings: Settings
    run_dir: Path
    workspace: Path
    base_url: str
    browser: "BrowserSession"
    http: "httpx.AsyncClient"
    emit: Callable[[str, dict[str, Any]], None]
    secrets: SecretRegistry = field(default_factory=SecretRegistry)
    phase: StepPhase = "execute"
    step_index: int = 0
    scratch: dict[str, Any] = field(default_factory=dict)

    def artifact_path(self, filename: str) -> Path:
        out = self.run_dir / "artifacts"
        out.mkdir(parents=True, exist_ok=True)
        return out / filename
