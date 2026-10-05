"""Seeds the shared workspace the agent can read.

The portal credentials live here rather than in the prompt on purpose: it forces
the agent to *go and find* something it needs before it can make progress, which
is a much better test of goal-directed behaviour than handing it everything up
front. It also mirrors how this works in reality - the information exists
somewhere in the company, and knowing where to look is part of the job.
"""

from __future__ import annotations

from pathlib import Path

from app.sandbox.data import PORTAL_PASSWORD, PORTAL_USERNAME

FILES: dict[str, str] = {
    "README.md": """\
# Acme Corp — Accounts Payable working folder

Reference material for whoever is handling supplier invoices this week.

## Systems

| System | Where | Notes |
| --- | --- | --- |
| Northwind Supplies portal | `/portal` | External supplier site. Sign-in required — see `credentials.md`. |
| Acme Finance (AP) | `/finance` | Our internal system. Web form at `/finance/entries/new`, JSON API at `/finance/api/entries`. |

## House rules for AP entries

- One AP entry per supplier invoice. Never create a duplicate for an invoice
  that is already recorded.
- Amounts are plain decimals in USD: `12480.00`, not `$12,480.00`.
- Dates are ISO: `2026-10-18`.
- Record the invoice's **due date**, not its issue date.
""",
    "credentials.md": f"""\
# Credentials — internal use only (sandbox values)

## Northwind Supplies supplier portal
- URL: `/portal/login`
- Username: `{PORTAL_USERNAME}`
- Password: `{PORTAL_PASSWORD}`

## Acme Finance
No sign-in required from inside the corporate network.
""",
    "notes/handover.md": """\
# Handover notes

Northwind re-issued one invoice last quarter after a credit, so their invoice
numbers no longer run in date order. Always check the issue date rather than
assuming the highest number is the newest document.
""",
}


def seed_workspace(workspace: Path) -> list[str]:
    """Create any missing seed files. Never overwrites agent-written files."""
    created: list[str] = []
    for relative, content in FILES.items():
        target = workspace / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(relative)
    (workspace / "outbox").mkdir(parents=True, exist_ok=True)
    return created
