"""Tool execution: timeouts, retries, and turning failures into usable feedback.

The distinction this module exists to make: **some failures should cost the
agent a reasoning step, and some should not.**

A 503 or a dropped connection carries no information - making the model think
about it wastes a step and pollutes the transcript with noise. The executor
silently retries those itself, with backoff, and the model only ever sees the
outcome.

A rejected form value is the opposite: it is the most useful thing that could
have happened, because it says exactly what the right value looks like. That one
goes straight back to the model, verbatim, with its remediation hint attached.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.agent.schemas import Artifact, ErrorKind
from app.config import Settings
from app.tools.base import ToolError, ToolResult, ToolSpec
from app.tools.context import ToolContext

log = logging.getLogger(__name__)

TOOL_TIMEOUT_SECONDS = 60.0

# How many times the executor silently retries before handing the failure to
# the model. Only transient failures are retried at all.
TRANSIENT_RETRIES = 2
RETRY_BACKOFF_SECONDS = (1.0, 3.0)


@dataclass
class ExecOutcome:
    result: ToolResult
    error_kind: ErrorKind | None = None
    retries: int = 0
    artifacts: list[Artifact] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.result.is_error


class Executor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, ctx: ToolContext, spec: ToolSpec, args: dict) -> ExecOutcome:
        attempt = 0
        while True:
            try:
                result = await asyncio.wait_for(
                    spec.handler(ctx, **args), timeout=TOOL_TIMEOUT_SECONDS
                )
                return ExecOutcome(
                    result=result,
                    error_kind=result.error_kind,
                    retries=attempt,
                    artifacts=list(result.artifacts),
                )

            except asyncio.TimeoutError:
                error = ToolError(
                    f"`{spec.name}` did not finish within {TOOL_TIMEOUT_SECONDS:.0f}s.",
                    kind="transient",
                    remediation="The system may be slow. Retry once, or take a different route.",
                )
            except ToolError as exc:
                error = exc
            except TypeError as exc:
                # Wrong/missing arguments: a schema-shaped problem, not a world
                # problem. Hand it back so the model can correct the call.
                error = ToolError(
                    f"`{spec.name}` was called with invalid arguments: {exc}",
                    kind="invalid_input",
                    remediation="Check the tool's required parameters and call it again.",
                )
            except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the run
                log.exception("Unhandled error in tool %s", spec.name)
                error = ToolError(
                    f"`{spec.name}` failed unexpectedly: {exc}",
                    kind="transient",
                    remediation="Retry once; if it fails again, try a different approach.",
                )

            if error.kind == "transient" and attempt < TRANSIENT_RETRIES:
                delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                log.info(
                    "Transient failure in %s (attempt %d), retrying in %.1fs: %s",
                    spec.name, attempt + 1, delay, error.message,
                )
                ctx.emit(
                    "step.retry",
                    {
                        "tool": spec.name,
                        "attempt": attempt + 1,
                        "error": error.message,
                        "step_index": ctx.step_index,
                    },
                )
                attempt += 1
                await asyncio.sleep(delay)
                continue

            observation = error.to_observation()
            if attempt:
                observation += f"\n(This was retried {attempt} time(s) automatically.)"
            return ExecOutcome(
                result=ToolResult(
                    content=observation, is_error=True, error_kind=error.kind
                ),
                error_kind=error.kind,
                retries=attempt,
            )
