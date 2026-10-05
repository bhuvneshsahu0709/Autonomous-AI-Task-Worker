"""End-to-end: the real loop completes the headline task.

What this proves, with no model involved:

* the browser tools drive a real site through sign-in, navigation and a form,
* a transient 503 is survived,
* a rejected form value is read and corrected,
* facts are committed to memory and survive to the end,
* the verifier independently confirms the result through the JSON API,
* the run lands on `succeeded` with an evidence bundle on disk.
"""

from __future__ import annotations

import json

import pytest

from app.agent.orchestrator import Orchestrator
from app.agent.scripted_brain import scripted_brain_factory
from app.runtime.events import EventBus
from app.runtime.store import RunStore
from app.sandbox.data import WORLD

GOAL = (
    "Find the latest invoice from Northwind Supplies, extract the amount and due "
    "date, enter it into our internal finance system, and tell me once it is done."
)


@pytest.mark.asyncio
async def test_invoice_task_completes_and_verifies(settings):
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings, store, EventBus(), brain_factory=scripted_brain_factory()
    )

    record = orchestrator.create_run(GOAL)
    session = store.session(record.id)
    await orchestrator._drive(session)

    assert record.status == "succeeded", (
        f"{record.status}: {record.failure_reason}\n"
        + "\n".join(f"{s.index} {s.tool} {s.status} {s.observation[:120]}" for s in record.steps)
    )

    # --- the work actually happened in the world ---
    entry = WORLD.find_entry_by_invoice("INV-2043")
    assert entry is not None, "no AP entry was created"
    assert entry.amount == 12480.00
    assert entry.due_date == "2026-10-18"
    assert entry.vendor == "Northwind Supplies"

    # --- it picked the latest by date, not the last row ---
    assert record.claims["ap_entry_invoice_number"] == "INV-2043"

    # --- memory ---
    facts = record.fact_dict()
    assert facts["invoice_amount"] == "12,480.00"
    assert facts["invoice_due_date"] == "2026-10-18"

    # --- verification was independent and passed ---
    assert record.verification is not None
    assert record.verification.verdict == "verified"
    assert record.verification.checks, "verifier recorded no per-claim checks"
    assert all(c.ok for c in record.verification.checks)
    verify_steps = [s for s in record.steps if s.phase == "verify"]
    assert verify_steps, "verification ran no steps"
    assert any(
        s.tool == "http_request" and s.args.get("method") == "GET" for s in verify_steps
    ), "verifier did not read the API"


@pytest.mark.asyncio
async def test_recovers_from_transient_outage_and_validation_error(settings):
    """The two injected faults must both be hit *and* survived."""
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings, store, EventBus(), brain_factory=scripted_brain_factory()
    )
    record = orchestrator.create_run(GOAL)
    await orchestrator._drive(store.session(record.id))

    assert record.status == "succeeded"

    observations = "\n".join(s.observation for s in record.steps)
    assert "503" in observations, "the flaky-login fault never fired"
    assert "plain decimal number" in observations, "the validation fault never fired"

    # And the rejection was genuinely recovered from, not skipped.
    rejected = WORLD.audit and any(a["event"] == "ap_entry_rejected" for a in WORLD.audit)
    created = any(a["event"] == "ap_entry_created" for a in WORLD.audit)
    assert rejected and created, f"expected reject-then-create, got {WORLD.audit}"


@pytest.mark.asyncio
async def test_evidence_bundle_is_written(settings):
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings, store, EventBus(), brain_factory=scripted_brain_factory()
    )
    record = orchestrator.create_run(GOAL)
    await orchestrator._drive(store.session(record.id))

    run_dir = store.run_dir(record.id)
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "transcript.json").is_file()

    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Verification" in report and "verified" in report
    assert "INV-2043" in report

    shots = list((run_dir / "artifacts").glob("*.png"))
    assert len(shots) >= 5, f"expected screenshots as evidence, found {len(shots)}"

    saved = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert saved["status"] == "succeeded"
    assert saved["verification"]["verdict"] == "verified"
