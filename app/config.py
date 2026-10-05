"""Central configuration.

Every knob the agent has is declared here and sourced from the environment, so a
run is reproducible from its recorded settings snapshot (see ``RunRecord.config``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AutonomyLevel = Literal["supervised", "standard", "autonomous"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- model ----
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    agent_model: str = Field(default="claude-opus-5-5", alias="AGENT_MODEL")
    agent_effort: EffortLevel = Field(default="high", alias="AGENT_EFFORT")
    agent_max_tokens: int = Field(default=8000, alias="AGENT_MAX_TOKENS")

    # ---- autonomy / safety ----
    autonomy_level: AutonomyLevel = Field(default="standard", alias="AUTONOMY_LEVEL")
    approval_amount_threshold: float = Field(
        default=10_000.0, alias="APPROVAL_AMOUNT_THRESHOLD"
    )

    # ---- budgets ----
    max_steps: int = Field(default=40, alias="MAX_STEPS")
    max_seconds: int = Field(default=900, alias="MAX_SECONDS")
    max_consecutive_errors: int = Field(default=4, alias="MAX_CONSECUTIVE_ERRORS")
    max_verification_rounds: int = Field(default=2, alias="MAX_VERIFICATION_ROUNDS")
    max_repeat_identical_calls: int = Field(default=3, alias="MAX_REPEAT_IDENTICAL_CALLS")

    # ---- browser ----
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_slow_mo_ms: int = Field(default=0, alias="BROWSER_SLOW_MO_MS")
    browser_timeout_ms: int = Field(default=15_000, alias="BROWSER_TIMEOUT_MS")

    # ---- sandbox ----
    sandbox_faults_raw: str = Field(
        default="flaky_login,strict_validation", alias="SANDBOX_FAULTS"
    )

    # ---- server ----
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # ---- paths ----
    runs_dir: Path = PROJECT_ROOT / "runs"
    workspace_dir: Path = PROJECT_ROOT / "workspace"

    @field_validator("sandbox_faults_raw", mode="before")
    @classmethod
    def _coerce_none(cls, v: object) -> str:
        return "" if v is None else str(v)

    @property
    def sandbox_faults(self) -> set[str]:
        return {f.strip() for f in self.sandbox_faults_raw.split(",") if f.strip()}

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def public_snapshot(self) -> dict:
        """Config as recorded on a run - never includes the API key."""
        return {
            "model": self.agent_model,
            "effort": self.agent_effort,
            "max_tokens": self.agent_max_tokens,
            "autonomy_level": self.autonomy_level,
            "approval_amount_threshold": self.approval_amount_threshold,
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_verification_rounds": self.max_verification_rounds,
            "browser_headless": self.browser_headless,
            "sandbox_faults": sorted(self.sandbox_faults),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
