"""Explicit working memory.

The transcript is not memory - it is a log. Long runs accumulate thousands of
tokens of page snapshots in which a single number (the invoice total) is easy to
lose track of, and the model's attention over a long transcript is not something
to depend on for a figure that has to be exactly right.

So the agent is asked to *commit* anything load-bearing with ``remember``. Those
facts are:

* re-rendered into every subsequent prompt at full fidelity,
* shown in the operator console as the run progresses,
* handed to the verifier as the claims to check,
* persisted with the run as part of its evidence.

This is the difference between an agent that "saw" a value and one that knows it.
"""

from __future__ import annotations

from app.agent.schemas import Fact
from app.tools.base import ToolResult, ToolSpec, prop
from app.tools.context import ToolContext


async def remember(ctx: ToolContext, key: str, value: str, note: str = "") -> ToolResult:
    key = key.strip()
    fact = Fact(key=key, value=value.strip(), note=note.strip(), step_index=ctx.step_index)

    # Re-remembering a key overwrites it: the agent is allowed to correct itself.
    existing = next((f for f in ctx.run.facts if f.key == key), None)
    if existing is not None:
        ctx.run.facts.remove(existing)
        verb = "Updated"
    else:
        verb = "Recorded"
    ctx.run.facts.append(fact)
    ctx.emit("fact.added", fact.model_dump())

    return ToolResult(
        content=f"{verb} fact {key} = {fact.value!r}. It will stay available for the rest of this task.",
        facts=[fact],
    )


MEMORY_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="remember",
        description=(
            "Commit a fact you have confirmed to durable memory for this task "
            "(for example an invoice total, a due date, or a record id). "
            "Remembered facts stay visible for the whole run and are what your "
            "final result is checked against. Record values exactly as the source "
            "showed them. Re-using a key overwrites the previous value."
        ),
        parameters={
            "properties": {
                "key": prop("string", "Short snake_case identifier, e.g. 'invoice_total'."),
                "value": prop("string", "The exact value."),
                "note": prop("string", "Where it came from, e.g. the URL or page it was read on."),
            },
            "required": ["key", "value", "note"],
        },
        handler=remember,
        read_only=True,
        verifier_safe=False,
    ),
]
