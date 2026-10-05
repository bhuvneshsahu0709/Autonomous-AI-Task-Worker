"""A deterministic stand-in for the model, used by the integration tests.

**This is a test double, not a second product.** The autonomy in this system
comes from the LLM planner in ``brain.py``; this file contains no intelligence,
only a hand-written sequence of decisions for one known scenario.

It exists because the interesting failure modes of an agent runtime are not in
the model - they are in the harness around it: does a transient failure get
retried silently, does the approval gate actually suspend the run, does a
refuted verification feed back and re-enter the execute loop, does the evidence
bundle come out intact. Testing those against a live model would be slow,
expensive and non-deterministic. Testing them against this is none of those
things.

It implements exactly the same interface as ``Brain`` - ``await decide(messages)
-> Decision`` - which is the point: the orchestrator cannot tell the difference,
so what the tests exercise is the real loop.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Callable

from app.agent.brain import Decision
from app.agent.schemas import Usage

ELEMENT_RE = re.compile(r"^\s*\[(e\d+)\]\s+(\S+)\s*(?:\"([^\"]*)\")?(.*)$", re.MULTILINE)


def parse_elements(observation: str) -> list[dict[str, str]]:
    """Pull the element table back out of a rendered snapshot."""
    found: list[dict[str, str]] = []
    for ref, role, name, rest in ELEMENT_RE.findall(observation or ""):
        found.append({"ref": ref, "role": role, "name": name or "", "rest": rest})
    return found


def find_ref(observation: str, *, role: str | None = None, name_contains: str = "") -> str | None:
    needle = name_contains.lower()
    for element in parse_elements(observation):
        if role and element["role"] != role:
            continue
        if needle and needle not in element["name"].lower():
            continue
        return element["ref"]
    return None


def row_ref(observation: str, row_contains: str) -> str | None:
    """Find the ref whose row context contains a string.

    Mirrors how the real model disambiguates three identical 'View invoice'
    links: by the row text attached to each one.
    """
    lines = (observation or "").splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*\[(e\d+)\]", line)
        if not match:
            continue
        context = lines[index + 1] if index + 1 < len(lines) else ""
        if row_contains.lower() in context.lower():
            return match.group(1)
    return None


def last_observation(messages: list[dict[str, Any]]) -> str:
    """Text of the most recent tool result (or plain user message)."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    value = block.get("content")
                    return value if isinstance(value, str) else str(value)
    return ""


Rule = Callable[[str, "ScriptedBrain"], tuple[str, dict[str, Any]] | None]


class ScriptedBrain:
    """Replays a fixed decision sequence, reading refs out of live observations.

    The sequence is not blind: refs, and the choice of which invoice is latest,
    are read from the observations the real tools produced. So the test still
    fails if the snapshot layer, the sandbox or the executor break.
    """

    def __init__(
        self,
        role: str,
        *,
        usage: Usage | None = None,
        plan: list[Rule] | None = None,
    ) -> None:
        self.role = role
        self.usage = usage or Usage()
        self.stage = 0
        self.ids = itertools.count(1)
        self.plan: list[Rule] = plan if plan is not None else (
            VERIFIER_PLAN if role == "verifier" else WORKER_PLAN
        )
        self.notes: dict[str, Any] = {}

    async def decide(self, messages: list[dict[str, Any]]) -> Decision:
        self.usage.llm_calls += 1
        observation = last_observation(messages)

        while self.stage < len(self.plan):
            rule = self.plan[self.stage]
            self.stage += 1
            produced = rule(observation, self)
            if produced is None:
                continue  # rule decided it is not applicable; fall through
            tool, args = produced
            return self._decision(tool, args)

        return self._decision(
            "finish",
            {
                "outcome": "Scripted plan exhausted.",
                "summary": "The scripted brain ran out of steps.",
                "claims": "{}",
            },
        )

    def _decision(self, tool: str, args: dict[str, Any]) -> Decision:
        call_id = f"scripted_{next(self.ids)}"
        return Decision(
            thought=f"[scripted] step {self.stage}: {tool}",
            tool_name=tool,
            tool_args=args,
            tool_use_id=call_id,
            raw_content=[
                {"type": "text", "text": f"[scripted] {tool}"},
                {"type": "tool_use", "id": call_id, "name": tool, "input": args},
            ],
            stop_reason="tool_use",
        )


# ---------------------------------------------------------------------------
# The invoice scenario, as a sequence of rules.
# ---------------------------------------------------------------------------
def _read_credentials(obs: str, brain: ScriptedBrain):
    return "file_read", {"path": "credentials.md"}


def _go_to_login(obs: str, brain: ScriptedBrain):
    match = re.search(r"Password: `([^`]+)`", obs)
    brain.notes["password"] = match.group(1) if match else "Nw!nd-2026"
    match = re.search(r"Username: `([^`]+)`", obs)
    brain.notes["username"] = match.group(1) if match else "ap.clerk@acme.test"
    return "browser_navigate", {"url": "/portal/login"}


def _fill_username(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="textbox", name_contains="email")
    return "browser_fill", {"ref": ref or "e1", "value": brain.notes["username"]}


def _fill_password(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="password")
    return "browser_fill", {"ref": ref or "e2", "value": brain.notes["password"]}


def _submit_login(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="button", name_contains="sign in")
    return "browser_click", {"ref": ref or "e3"}


def _recover_login(obs: str, brain: ScriptedBrain):
    """Only fires if the transient 503 fault hit - exactly like the real agent."""
    if "503" not in obs and "unavailable" not in obs.lower():
        return None
    return "browser_navigate", {"url": "/portal/login"}


def _retry_username(obs: str, brain: ScriptedBrain):
    if "/portal/login" not in obs:
        return None
    ref = find_ref(obs, role="textbox", name_contains="email")
    if ref is None:
        return None
    return "browser_fill", {"ref": ref, "value": brain.notes["username"]}


def _retry_password(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="password")
    if ref is None:
        return None
    return "browser_fill", {"ref": ref, "value": brain.notes["password"]}


def _retry_submit(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="button", name_contains="sign in")
    if ref is None:
        return None
    return "browser_click", {"ref": ref}


def _open_latest_invoice(obs: str, brain: ScriptedBrain):
    """Pick the newest invoice by issue date, read from the live page."""
    rows = re.findall(r"(INV-\d+)\s+(PO-\d+)\s+(\d{4}-\d{2}-\d{2})", obs)
    if not rows:
        return "browser_read_page", {}
    latest = max(rows, key=lambda r: r[2])
    brain.notes["invoice_number"] = latest[0]
    ref = row_ref(obs, latest[0])
    return "browser_click", {"ref": ref or "e4"}


def _expand_billing(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="disclosure")
    return "browser_click", {"ref": ref or "e4"}


def _remember_amount(obs: str, brain: ScriptedBrain):
    match = re.search(r"Total amount due\s*\n?\s*\$([\d,]+\.\d{2})", obs)
    raw = match.group(1) if match else "0.00"
    brain.notes["amount_raw"] = raw
    brain.notes["amount"] = raw.replace(",", "")
    return "remember", {
        "key": "invoice_amount",
        "value": raw,
        "note": "Total amount due on the invoice detail page",
    }


def _remember_due_date(obs: str, brain: ScriptedBrain):
    source = obs + " " + str(brain.notes)
    match = re.search(r"Payment due date\s*\n?\s*(\d{4}-\d{2}-\d{2})", source)
    brain.notes["due_date"] = match.group(1) if match else "2026-10-18"
    return "remember", {
        "key": "invoice_due_date",
        "value": brain.notes["due_date"],
        "note": "Payment due date on the invoice detail page",
    }


def _remember_number(obs: str, brain: ScriptedBrain):
    return "remember", {
        "key": "invoice_number",
        "value": brain.notes.get("invoice_number", "INV-2043"),
        "note": "Latest invoice by issue date",
    }


def _open_form(obs: str, brain: ScriptedBrain):
    return "browser_navigate", {"url": "/finance/entries/new"}


def _fill_vendor(obs: str, brain: ScriptedBrain):
    return "browser_fill", {
        "ref": find_ref(obs, name_contains="vendor") or "e3",
        "value": "Northwind Supplies",
    }


def _fill_invoice_number(obs: str, brain: ScriptedBrain):
    return "browser_fill", {
        "ref": find_ref(obs, name_contains="invoice number") or "e4",
        "value": brain.notes.get("invoice_number", "INV-2043"),
    }


def _fill_amount_badly(obs: str, brain: ScriptedBrain):
    """Deliberately submits the currency-formatted amount.

    This is the whole point of the scripted run: it reproduces the mistake a
    real agent makes on its first attempt, so the test proves the system reads
    the validation error and recovers.
    """
    return "browser_fill", {
        "ref": find_ref(obs, name_contains="amount") or "e5",
        "value": f"${brain.notes.get('amount_raw', '12,480.00')}",
    }


def _fill_due_date(obs: str, brain: ScriptedBrain):
    return "browser_fill", {
        "ref": find_ref(obs, name_contains="due date") or "e6",
        "value": brain.notes.get("due_date", "2026-10-18"),
    }


def _submit_form(obs: str, brain: ScriptedBrain):
    return "browser_click", {"ref": find_ref(obs, role="button", name_contains="save") or "e8"}


def _fix_amount(obs: str, brain: ScriptedBrain):
    if "plain decimal" not in obs and "must be a number" not in obs:
        return None  # validation passed first time; nothing to fix
    return "browser_fill", {
        "ref": find_ref(obs, name_contains="amount") or "e5",
        "value": brain.notes.get("amount", "12480.00"),
    }


def _resubmit_form(obs: str, brain: ScriptedBrain):
    ref = find_ref(obs, role="button", name_contains="save")
    if ref is None:
        return None
    return "browser_click", {"ref": ref}


def _finish(obs: str, brain: ScriptedBrain):
    import json

    claims = {
        "ap_entry_invoice_number": brain.notes.get("invoice_number", "INV-2043"),
        "ap_entry_amount": brain.notes.get("amount", "12480.00"),
        "ap_entry_due_date": brain.notes.get("due_date", "2026-10-18"),
        "ap_entry_vendor": "Northwind Supplies",
    }
    return "finish", {
        "outcome": (
            f"Recorded invoice {claims['ap_entry_invoice_number']} from Northwind Supplies "
            f"for {claims['ap_entry_amount']} USD, due {claims['ap_entry_due_date']}, "
            "in Acme Finance."
        ),
        "summary": (
            "Read the portal credentials from the workspace, signed in (retrying once "
            "after a transient 503), identified the newest invoice by issue date, "
            "expanded the billing summary to read the total and due date, then created "
            "the AP entry - correcting the amount format after the form rejected it."
        ),
        "claims": json.dumps(claims),
    }


WORKER_PLAN: list[Rule] = [
    lambda obs, b: ("file_list", {"path": "."}),
    _read_credentials,
    _go_to_login,
    _fill_username,
    _fill_password,
    _submit_login,
    _recover_login,
    _retry_username,
    _retry_password,
    _retry_submit,
    _open_latest_invoice,
    _expand_billing,
    _remember_amount,
    _remember_due_date,
    _remember_number,
    _open_form,
    _fill_vendor,
    _fill_invoice_number,
    _fill_amount_badly,
    _fill_due_date,
    _submit_form,
    _fix_amount,
    _resubmit_form,
    _finish,
]


# ---------------------------------------------------------------------------
# Verifier plan: read the API and compare.
# ---------------------------------------------------------------------------
def _verifier_fetch(obs: str, brain: ScriptedBrain):
    # On the verifier's first turn the "observation" is the task briefing, which
    # is where the claims to check are listed.
    brain.notes["task_text"] = obs
    return "http_request", {"method": "GET", "url": "/finance/api/entries", "body": ""}


def _verifier_verdict(obs: str, brain: ScriptedBrain):
    import json

    task = brain.notes.get("task_text", "")
    claims = dict(re.findall(r"- (\w+): (.+)", task))
    checks = []
    all_ok = True
    for key, claimed in claims.items():
        claimed = claimed.strip()
        needle = claimed.rstrip("0").rstrip(".") if "." in claimed else claimed
        ok = needle in obs
        all_ok = all_ok and ok
        checks.append(
            {
                "key": key,
                "claimed": claimed,
                "observed": claimed if ok else "not found",
                "ok": ok,
                "note": "GET /finance/api/entries",
            }
        )
    return "report_verification", {
        "verdict": "verified" if all_ok and checks else "refuted",
        "reasoning": (
            "Read the AP entries straight from the finance API and compared each "
            "claimed value against the stored record."
        ),
        "checks": json.dumps(checks),
    }


VERIFIER_PLAN: list[Rule] = [_verifier_fetch, _verifier_verdict]


def scripted_brain_factory(
    worker_plan: list[Rule] | None = None,
    verifier_plans: list[list[Rule]] | None = None,
):
    """Factory matching the orchestrator's ``brain_factory`` contract.

    ``verifier_plans`` takes a list so a test can make the first verification
    round refute and the second pass - which is how the "verification failed,
    go back and fix it" path gets covered.
    """
    verifier_round = itertools.count()

    def make(role: str, toolbelt: Any, system_prompt: str, usage: Usage):
        if role == "verifier":
            plan = None
            if verifier_plans:
                index = min(next(verifier_round), len(verifier_plans) - 1)
                plan = verifier_plans[index]
            return ScriptedBrain(role, usage=usage, plan=plan)
        return ScriptedBrain(role, usage=usage, plan=worker_plan)

    return make
