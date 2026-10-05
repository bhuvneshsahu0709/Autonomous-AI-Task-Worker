"""Secrets must not leak into anything durable or visible.

The agent legitimately handles the portal password - it reads it out of the
workspace in order to sign in. What must not happen is that password ending up
in the run record, the console event stream, or the evidence bundle.
"""

from __future__ import annotations

import json

import pytest

from app.agent.orchestrator import Orchestrator
from app.agent.scripted_brain import scripted_brain_factory
from app.runtime.events import EventBus
from app.runtime.store import RunStore
from app.sandbox.data import PORTAL_PASSWORD

GOAL = "Find the latest Northwind invoice and record it in finance."


@pytest.mark.asyncio
async def test_password_is_redacted_everywhere_durable(settings):
    store = RunStore(settings)
    bus = EventBus()
    orchestrator = Orchestrator(
        settings, store, bus, brain_factory=scripted_brain_factory()
    )
    record = orchestrator.create_run(GOAL)
    await orchestrator._drive(store.session(record.id))
    assert record.status == "succeeded", record.failure_reason

    # It really did sign in, so the secret really was in play.
    assert any(s.tool == "browser_fill" for s in record.steps)
    assert any("Invoices issued to Acme" in s.observation for s in record.steps)

    # ...but it is not in the step arguments.
    for step in record.steps:
        assert PORTAL_PASSWORD not in json.dumps(step.args), f"leaked in step {step.index} args"
        assert PORTAL_PASSWORD not in step.observation, f"leaked in step {step.index} observation"

    # ...nor in the persisted record or the human-readable report.
    run_dir = store.run_dir(record.id)
    assert PORTAL_PASSWORD not in (run_dir / "run.json").read_text(encoding="utf-8")
    assert PORTAL_PASSWORD not in (run_dir / "report.md").read_text(encoding="utf-8")

    # ...nor in anything streamed to the console.
    events = json.dumps(bus.history(record.id))
    assert PORTAL_PASSWORD not in events

    # The masked form is what gets recorded instead.
    fills = [s for s in record.steps if s.tool == "browser_fill"]
    assert any(s.args.get("value") == "********" for s in fills)
