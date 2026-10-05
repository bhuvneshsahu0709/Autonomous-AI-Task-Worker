"""Run-control tools: how the worker finishes, and how the verifier rules.

``finish`` deliberately does *not* end the run. It ends the worker's turn and
hands its claims to verification. The agent does not get to mark its own
homework - the only way a run reaches ``succeeded`` is for an independent pass
to confirm the claims against the systems of record.
"""

from __future__ import annotations

import json

from app.tools.base import ToolError, ToolResult, ToolSpec, prop
from app.tools.context import ToolContext


async def finish(
    ctx: ToolContext, summary: str, claims: str, outcome: str
) -> ToolResult:
    try:
        parsed = json.loads(claims) if claims.strip() else {}
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"`claims` must be a JSON object: {exc}",
            kind="invalid_input",
            remediation=(
                'Pass claims as a JSON object of checkable key/value strings, e.g. '
                '{"ap_entry_invoice_number":"INV-2043","ap_entry_amount":"12480.00"}'
            ),
        ) from exc
    if not isinstance(parsed, dict):
        raise ToolError(
            "`claims` must be a JSON object, not a list or scalar.",
            kind="invalid_input",
            remediation='Example: {"invoice_number":"INV-2043","amount":"12480.00"}',
        )

    return ToolResult(
        content="(submitted for verification)",
        control="finish",
        payload={
            "summary": summary.strip(),
            "outcome": outcome.strip(),
            "claims": {str(k): str(v) for k, v in parsed.items()},
        },
    )


async def report_verification(
    ctx: ToolContext, verdict: str, reasoning: str, checks: str
) -> ToolResult:
    verdict = verdict.strip().lower()
    if verdict not in {"verified", "refuted", "inconclusive"}:
        raise ToolError(
            f"'{verdict}' is not a valid verdict.",
            kind="invalid_input",
            remediation="Use exactly one of: verified, refuted, inconclusive.",
        )
    try:
        parsed = json.loads(checks) if checks.strip() else []
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"`checks` must be a JSON array: {exc}",
            kind="invalid_input",
            remediation=(
                'Example: [{"key":"amount","claimed":"12480.00","observed":"12480.00",'
                '"ok":true,"note":"GET /finance/api/entries"}]'
            ),
        ) from exc
    if not isinstance(parsed, list):
        raise ToolError("`checks` must be a JSON array.", kind="invalid_input")

    return ToolResult(
        content="(verdict recorded)",
        control="verdict",
        payload={"verdict": verdict, "reasoning": reasoning.strip(), "checks": parsed},
    )


FINISH_TOOL = ToolSpec(
    name="finish",
    description=(
        "Declare the task complete and submit your result for verification. "
        "`claims` must be the specific, checkable facts an auditor would confirm "
        "in the systems of record - not a description of what you did. "
        "An independent verification pass will re-read those systems and compare. "
        "If a claim does not hold, the task comes back to you to fix."
    ),
    parameters={
        "properties": {
            "outcome": prop("string", "One or two sentences answering the user's original request."),
            "summary": prop("string", "What you did, in order, including anything that went wrong and how you handled it."),
            "claims": prop(
                "string",
                'JSON object of checkable key/value pairs, e.g. {"ap_entry_invoice_number":"INV-2043","ap_entry_amount":"12480.00","ap_entry_due_date":"2026-10-18"}',
            ),
        },
        "required": ["outcome", "summary", "claims"],
    },
    handler=finish,
    read_only=True,
    verifier_safe=False,
)

VERDICT_TOOL = ToolSpec(
    name="report_verification",
    description=(
        "Record your verification verdict and stop. Call this once you have "
        "independently checked every claim against the systems of record."
    ),
    parameters={
        "properties": {
            "verdict": prop(
                "string",
                "verified = every claim confirmed; refuted = at least one claim is wrong or "
                "the work was not actually done; inconclusive = you could not check.",
                enum=["verified", "refuted", "inconclusive"],
            ),
            "reasoning": prop("string", "What you checked, where, and what you saw."),
            "checks": prop(
                "string",
                'JSON array of {"key","claimed","observed","ok","note"} - one per claim.',
            ),
        },
        "required": ["verdict", "reasoning", "checks"],
    },
    handler=report_verification,
    read_only=True,
)
