"""Human-in-the-loop tools.

Both of these *suspend the run*. They return a ``control`` signal rather than a
result; the orchestrator persists the run, marks it ``awaiting_human`` and
returns. When the operator answers through the console, the run is rehydrated
and the answer is delivered as the tool's result - so from the model's point of
view it simply called a tool that took a long time to come back.

That design matters: a run can wait hours for an approval without holding a
thread, a socket, or an in-flight API call open.
"""

from __future__ import annotations

from app.agent.schemas import HumanPrompt
from app.tools.base import ToolResult, ToolSpec, prop
from app.tools.context import ToolContext


async def ask_human(ctx: ToolContext, question: str, options: str = "") -> ToolResult:
    choices = [o.strip() for o in options.split("|") if o.strip()] if options else []
    prompt = HumanPrompt(kind="question", question=question.strip(), options=choices)
    return ToolResult(
        content="(waiting for the operator to answer)",
        control="ask_human",
        payload={"prompt": prompt},
    )


async def request_approval(
    ctx: ToolContext, action: str, details: str, risk: str = ""
) -> ToolResult:
    prompt = HumanPrompt(
        kind="approval",
        question=action.strip(),
        details=details.strip(),
        risk=risk.strip(),
    )
    return ToolResult(
        content="(waiting for the operator to approve or reject)",
        control="request_approval",
        payload={"prompt": prompt},
    )


HUMAN_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="ask_human",
        description=(
            "Ask the operator a question and wait for their answer. Use this only when "
            "the task is genuinely ambiguous and guessing could produce the wrong "
            "outcome - for example two records both plausibly match the request. "
            "Do not use it to ask permission for work you were already asked to do."
        ),
        parameters={
            "properties": {
                "question": prop("string", "A specific question, including the context needed to answer it."),
                "options": prop("string", "Optional choices separated by '|'. Empty string if open-ended."),
            },
            "required": ["question", "options"],
        },
        handler=ask_human,
        read_only=True,
        verifier_safe=False,
    ),
    ToolSpec(
        name="request_approval",
        description=(
            "Ask the operator to approve an action before you take it. Required for "
            "irreversible or high-value changes. State plainly what you are about to "
            "do and what the consequence is."
        ),
        parameters={
            "properties": {
                "action": prop("string", "One line: the action you want to take."),
                "details": prop("string", "The exact values involved, so the operator can check them."),
                "risk": prop("string", "Why this needs approval / what happens if it is wrong."),
            },
            "required": ["action", "details", "risk"],
        },
        handler=request_approval,
        read_only=True,
        verifier_safe=False,
    ),
]
