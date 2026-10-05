"""The approval gate and the suspend/resume cycle.

The property under test is the one that makes the gate meaningful: it is
enforced in the executor, so an action that needs approval **cannot happen**
before a human answers - regardless of what the model decided to do.
"""

from __future__ import annotations

import pytest

from app.agent.orchestrator import Orchestrator
from app.agent.scripted_brain import scripted_brain_factory
from app.runtime.events import EventBus
from app.runtime.store import RunStore
from app.sandbox.data import WORLD

GOAL = "Record the latest Northwind Supplies invoice in the finance system."


def _supervised(settings):
    """Standard autonomy + a threshold the invoice exceeds."""
    settings.autonomy_level = "standard"
    settings.approval_amount_threshold = 10_000.0
    return settings


@pytest.mark.asyncio
async def test_high_value_write_suspends_until_approved(settings):
    _supervised(settings)
    store = RunStore(settings)
    bus = EventBus()
    orchestrator = Orchestrator(settings, store, bus, brain_factory=scripted_brain_factory())

    record = orchestrator.create_run(GOAL)
    session = store.session(record.id)
    await orchestrator._drive(session)

    # Parked, with nothing written yet.
    assert record.status == "awaiting_human"
    assert record.pending_prompt is not None
    assert record.pending_prompt.kind == "approval"
    assert "12480" in record.pending_prompt.details.replace(",", "")
    assert WORLD.find_entry_by_invoice("INV-2043") is None, (
        "the write happened before approval - the gate did not hold"
    )

    # The console was told.
    assert any(e["type"] == "human.prompt" for e in bus.history(record.id))

    # Approve each gated action. The plan submits the form twice (the first
    # amount is rejected by validation), and the gate must hold on *both* - an
    # approval is for one action, not a blanket grant for the rest of the run.
    approvals = 0
    while record.status == "awaiting_human" and approvals < 5:
        await orchestrator.respond(
            record.id, approved=True, answer="Checked against PO-88341."
        )
        approvals += 1
        await session.task

    assert approvals == 2, f"expected the gate to fire on both submits, fired {approvals}"
    assert record.status == "succeeded", record.failure_reason
    assert WORLD.find_entry_by_invoice("INV-2043") is not None
    assert all(e.approved for e in record.human_exchanges)


@pytest.mark.asyncio
async def test_declined_approval_blocks_the_write(settings):
    _supervised(settings)
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings, store, EventBus(), brain_factory=scripted_brain_factory()
    )

    record = orchestrator.create_run(GOAL)
    session = store.session(record.id)
    await orchestrator._drive(session)
    assert record.status == "awaiting_human"

    await orchestrator.respond(
        record.id, approved=False, answer="Wrong cost centre - do not record this."
    )
    await session.task

    # The run ends without the side effect, and the refusal is on the record.
    assert WORLD.find_entry_by_invoice("INV-2043") is None
    assert record.status in {"failed", "succeeded"}
    assert record.human_exchanges[0].approved is False
    assert record.status != "succeeded" or record.verification.verdict != "verified"


@pytest.mark.asyncio
async def test_autonomous_level_does_not_gate(settings):
    settings.autonomy_level = "autonomous"
    settings.approval_amount_threshold = 1.0  # would gate everything if consulted
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings, store, EventBus(), brain_factory=scripted_brain_factory()
    )
    record = orchestrator.create_run(GOAL)
    await orchestrator._drive(store.session(record.id))

    assert record.status == "succeeded", record.failure_reason
    assert record.pending_prompt is None
    assert not record.human_exchanges
