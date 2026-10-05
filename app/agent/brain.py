"""The model interface - the only component in the system that knows about Claude.

Everything above this file deals in ``Decision`` objects; everything below deals
in tools. Swapping the model, or putting a scripted planner behind the same
interface for tests, touches nothing else.

Three decisions in here are load-bearing:

**A manual loop, not the SDK tool runner.** The runner drives the loop for you,
which is exactly what we cannot allow: this agent has to be able to *suspend
mid-loop* for human approval, persist, and resume minutes later. It also needs
per-step policy checks, retry classification and budget accounting between the
tool call and its execution. Those hooks are the whole product.

**One action per turn.** ``disable_parallel_tool_use`` keeps the model to a
single tool call per step. Parallel calls are a throughput win for independent
reads, but every action here mutates shared state (a browser page, a form), so
interleaving them makes observations ambiguous and failures hard to attribute.

**Optional features degrade instead of crashing.** Prompt caching, refusal
fallbacks and summarised thinking are all nice-to-have. If the API rejects one,
we drop that feature and retry rather than failing the run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.schemas import Usage
from app.config import Settings
from app.tools.registry import Toolbelt

log = logging.getLogger(__name__)

# Beta flag for the scalar `fallbacks: "default"` form, which reroutes a
# safety-classifier decline to a suitable fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

MAX_LLM_ATTEMPTS = 4


class BrainError(RuntimeError):
    """Unrecoverable problem talking to the model."""


@dataclass
class Decision:
    """One turn of the model's output."""

    thought: str = ""                       # visible text + summarised reasoning
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str | None = None
    raw_content: list[Any] = field(default_factory=list)  # echoed back verbatim
    stop_reason: str | None = None
    refusal: str = ""

    @property
    def has_action(self) -> bool:
        return self.tool_name is not None


class Brain:
    def __init__(
        self,
        settings: Settings,
        toolbelt: Toolbelt,
        system_prompt: str,
        *,
        usage: Usage | None = None,
    ) -> None:
        self.settings = settings
        self.toolbelt = toolbelt
        self.system_prompt = system_prompt
        self.usage = usage or Usage()
        self._client: Any = None
        # Optional request features, dropped individually if the API objects.
        self._features = {"caching", "fallbacks", "thinking_display"}

    # ------------------------------------------------------------------
    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise BrainError("The `anthropic` package is not installed.") from exc

        if not self.settings.anthropic_api_key:
            raise BrainError(
                "No ANTHROPIC_API_KEY is set. Copy .env.example to .env and add your key."
            )
        self._client = AsyncAnthropic(
            api_key=self.settings.anthropic_api_key,
            timeout=180.0,
            max_retries=3,
        )
        return self._client

    def _build_kwargs(self, messages: list[dict]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.settings.agent_model,
            "max_tokens": self.settings.agent_max_tokens,
            "system": self.system_prompt,
            "messages": messages,
            "tools": self.toolbelt.anthropic_schemas(),
            # Forced tool choice is rejected on current models; `auto` plus a
            # prompt that names the expectation, plus `strict` schemas, is the
            # supported equivalent. The loop handles a no-tool turn explicitly.
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "output_config": {"effort": self.settings.agent_effort},
        }
        if "thinking_display" in self._features:
            # Adaptive thinking is on by default on this model family; asking for
            # a summary is what lets the console show real reasoning rather than
            # an empty block.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        if "caching" in self._features:
            # Caches the longest stable prefix - system + tools + the transcript
            # so far. In a 30-step run the same prefix is re-sent 30 times.
            kwargs["cache_control"] = {"type": "ephemeral"}
        if "fallbacks" in self._features:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _drop_feature_for(self, message: str) -> bool:
        """Turn off whichever optional feature the API just complained about."""
        lowered = message.lower()
        for feature, needles in (
            ("fallbacks", ("fallback", FALLBACK_BETA)),
            ("caching", ("cache_control", "cache control")),
            ("thinking_display", ("thinking", "display")),
        ):
            if feature in self._features and any(n in lowered for n in needles):
                self._features.discard(feature)
                log.warning("Disabling optional request feature %r: %s", feature, message)
                return True
        return False

    # ------------------------------------------------------------------
    async def decide(self, messages: list[dict]) -> Decision:
        """Ask the model for its next action."""
        import anthropic

        client = self._client_or_raise()
        last_error: Exception | None = None

        for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
            kwargs = self._build_kwargs(messages)
            try:
                # Streaming avoids request timeouts when adaptive thinking runs
                # long at high effort; we only need the assembled message.
                async with client.beta.messages.stream(**kwargs) as stream:
                    message = await stream.get_final_message()
                self.usage.add(message.usage)
                return self._to_decision(message)

            except anthropic.BadRequestError as exc:
                detail = str(exc)
                if self._drop_feature_for(detail):
                    continue  # retry immediately without that feature
                raise BrainError(f"The model rejected the request: {detail}") from exc

            except anthropic.AuthenticationError as exc:
                raise BrainError(
                    "Authentication with the Claude API failed - check ANTHROPIC_API_KEY."
                ) from exc

            except (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.InternalServerError,
            ) as exc:
                last_error = exc
                backoff = min(2 ** attempt, 20)
                log.warning(
                    "LLM call failed (%s), attempt %d/%d; retrying in %ss",
                    type(exc).__name__, attempt, MAX_LLM_ATTEMPTS, backoff,
                )
                await asyncio.sleep(backoff)

        raise BrainError(
            f"The model was unreachable after {MAX_LLM_ATTEMPTS} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _to_decision(message: Any) -> Decision:
        decision = Decision(
            raw_content=list(message.content),
            stop_reason=message.stop_reason,
        )

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            explanation = getattr(details, "explanation", "") or ""
            decision.refusal = f"The model declined this request ({category}). {explanation}".strip()
            return decision

        thoughts: list[str] = []
        for block in message.content:
            btype = getattr(block, "type", None)
            if btype == "text" and getattr(block, "text", "").strip():
                thoughts.append(block.text.strip())
            elif btype == "thinking":
                summary = (getattr(block, "thinking", "") or "").strip()
                if summary:
                    thoughts.append(summary)
            elif btype == "tool_use" and decision.tool_name is None:
                decision.tool_name = block.name
                decision.tool_args = dict(block.input or {})
                decision.tool_use_id = block.id

        decision.thought = "\n\n".join(thoughts).strip()
        return decision
