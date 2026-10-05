"""The tool contract.

A tool is the only way the agent can affect or observe anything. Keeping that
surface narrow and uniform is what makes the rest of the system generic: the
orchestrator never knows what a browser or an invoice is, it only knows how to
call a ``Tool`` and read a ``ToolResult``.

Two flags on each tool carry real weight:

* ``read_only`` - the verifier is handed *only* read-only tools, so it is
  structurally incapable of fixing a problem it is supposed to be detecting.
* ``mutating`` - changes something in the world, so the approval policy gets a
  say before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from app.agent.schemas import Artifact, ErrorKind, Fact

if TYPE_CHECKING:  # pragma: no cover
    from app.tools.context import ToolContext


class ToolError(Exception):
    """Raised by a tool when the call cannot be completed.

    ``kind`` tells the executor *how* to react (retry, re-observe, abort);
    ``remediation`` is plain-English advice handed to the model so it can adapt
    rather than repeat itself. Writing a good remediation string is the single
    highest-leverage thing you can do for agent reliability.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind = "invalid_input",
        remediation: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind: ErrorKind = kind
        self.remediation = remediation

    def to_observation(self) -> str:
        text = f"ERROR: {self.message}"
        if self.remediation:
            text += f"\nWhat to do instead: {self.remediation}"
        return text


@dataclass
class ToolResult:
    """What a tool hands back.

    ``content`` is verbatim what the model sees. ``control`` is an out-of-band
    signal to the orchestrator for tools that change the shape of the run
    (finishing, or suspending for a human).
    """

    content: str
    artifacts: list[Artifact] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    is_error: bool = False
    error_kind: ErrorKind | None = None
    control: str | None = None  # "finish" | "ask_human" | "request_approval"
    payload: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool
    mutating: bool

    async def run(self, ctx: "ToolContext", **kwargs: Any) -> ToolResult: ...


@dataclass
class ToolSpec:
    """Concrete tool implementation: metadata plus an async callable."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Any
    read_only: bool = True
    mutating: bool = False
    # Tools the verifier must never see, even if read-only (e.g. `finish`).
    verifier_safe: bool = True

    def anthropic_schema(self) -> dict[str, Any]:
        """Tool definition in Messages API shape.

        ``strict: True`` guarantees the arguments validate against the schema,
        which matters here because forced tool choice is not available on the
        current models - we rely on prompt steering plus schema enforcement
        instead of ``tool_choice: any``.
        """
        properties = self.parameters.get("properties", {})
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": self.parameters.get("required", []),
                "additionalProperties": False,
            },
            "strict": True,
        }


def prop(
    type_: str,
    description: str,
    **extra: Any,
) -> dict[str, Any]:
    """Small helper so schemas below stay readable."""
    return {"type": type_, "description": description, **extra}
