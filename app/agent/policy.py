"""Approval policy.

The important property here is that this is a **gate, not a suggestion**. The
model has a `request_approval` tool it may choose to call, but that is a
courtesy path. This module runs in the executor *before every tool call*, and an
action it blocks cannot happen - there is no prompt phrasing that routes around
it, because the model is not the thing making the decision.

Three autonomy levels:

* ``supervised``  - every change to the world needs a human OK.
* ``standard``    - changes need an OK when real money or an irreversible write
                    is involved; routine navigation and reads do not.
* ``autonomous``  - no gates; everything is still logged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.tools.base import ToolSpec

# Buttons whose labels mean "this commits something".
COMMIT_LABEL_RE = re.compile(
    r"\b(save|submit|confirm|create|record|pay|approve|send|delete|remove|post)\b",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")

# Argument names that are never monetary. Without this, an element ref like
# `e8` or a URL containing an id reads as the number 8 and masks the real
# amount - which silently disables the threshold rule.
NON_MONETARY_ARGS = frozenset(
    {"ref", "url", "path", "method", "key", "seconds", "label", "note", "options"}
)


@dataclass
class PolicyDecision:
    requires_approval: bool = False
    reason: str = ""
    action_summary: str = ""
    details: str = ""
    amount_in_play: float | None = None
    rules_fired: list[str] = field(default_factory=list)

    @property
    def allowed_without_human(self) -> bool:
        return not self.requires_approval


def _largest_amount(values: list[str]) -> float | None:
    """Biggest money-shaped number in the given strings.

    Deliberately permissive: '12,480.00' and '$12480' both count. Over-detecting
    costs an approval prompt; under-detecting costs an unreviewed payment.
    """
    best: float | None = None
    for raw in values:
        for match in _NUMBER_RE.finditer(raw or ""):
            try:
                value = float(match.group(0).replace(",", ""))
            except ValueError:
                continue
            if best is None or value > best:
                best = value
    return best


class ApprovalPolicy:
    def __init__(self, settings: Settings) -> None:
        self.level = settings.autonomy_level
        self.threshold = settings.approval_amount_threshold

    def evaluate(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        *,
        element_label: str = "",
        known_facts: dict[str, str] | None = None,
    ) -> PolicyDecision:
        decision = PolicyDecision()
        if self.level == "autonomous":
            return decision

        is_mutation, what = self._classify(spec, args, element_label)
        if not is_mutation:
            return decision

        # Money in play: from the call's own arguments, and from what the agent
        # has committed to memory (a form submit carries no amount in its args,
        # but the agent knows the invoice total it just typed in).
        arg_values = [
            str(v) for k, v in args.items() if k not in NON_MONETARY_ARGS
        ]
        fact_values = list((known_facts or {}).values())
        candidates = [
            value
            for value in (_largest_amount(arg_values), _largest_amount(fact_values))
            if value is not None
        ]
        amount = max(candidates) if candidates else None
        decision.amount_in_play = amount

        if self.level == "supervised":
            decision.requires_approval = True
            decision.rules_fired.append("supervised_mode")
            decision.reason = "Autonomy level is 'supervised': all changes need approval."
        elif amount is not None and amount >= self.threshold:
            decision.requires_approval = True
            decision.rules_fired.append("amount_threshold")
            decision.reason = (
                f"The value in play (~{amount:,.2f}) is at or above the "
                f"{self.threshold:,.0f} approval threshold."
            )

        if decision.requires_approval:
            decision.action_summary = what
            decision.details = self._details(spec, args, element_label, amount)
        return decision

    # ------------------------------------------------------------------
    def _classify(
        self, spec: ToolSpec, args: dict[str, Any], element_label: str
    ) -> tuple[bool, str]:
        """Is this call about to change the world, and how would you describe it?"""
        if spec.name == "http_request":
            method = str(args.get("method", "GET")).upper()
            if method == "GET":
                return False, ""
            return True, f"{method} {args.get('url', '')} against the internal API"

        if spec.name == "browser_click":
            # A click is only a mutation if it is a commit control. Navigating
            # between pages is not something to wake a human for.
            if COMMIT_LABEL_RE.search(element_label or ""):
                return True, f"Submit the form by clicking {element_label}"
            return False, ""

        if spec.name == "file_write":
            return True, f"Write the file {args.get('path', '')}"

        if spec.mutating:
            return True, f"Run {spec.name}"
        return False, ""

    def _details(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        element_label: str,
        amount: float | None,
    ) -> str:
        lines = [f"Tool: {spec.name}"]
        for key, value in args.items():
            text = str(value)
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"{key}: {text}")
        if element_label:
            lines.append(f"Target element: {element_label}")
        if amount is not None:
            lines.append(f"Largest value involved: {amount:,.2f}")
        return "\n".join(lines)


def summarise_call(spec: ToolSpec, args: dict[str, Any]) -> str:
    """Compact one-liner used in the console timeline."""
    if spec.name == "http_request":
        return f"{str(args.get('method', '')).upper()} {args.get('url', '')}"
    if spec.name in {"browser_navigate"}:
        return str(args.get("url", ""))
    if spec.name == "browser_fill":
        value = str(args.get("value", ""))
        return f"{args.get('ref', '')} ← {value[:60]}"
    if spec.name in {"file_read", "file_list", "file_write"}:
        return str(args.get("path", ""))
    if spec.name == "remember":
        return f"{args.get('key', '')} = {str(args.get('value', ''))[:60]}"
    try:
        compact = json.dumps(args, ensure_ascii=False)
    except TypeError:
        compact = str(args)
    return compact[:120]
