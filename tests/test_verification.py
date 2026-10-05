"""Verification is the thing standing between "the agent said so" and "it is true".

These tests cover the three outcomes that matter:

* a false claim is caught,
* being caught sends the work back to the worker rather than ending the run,
* the verifier physically cannot write, so it can never "fix" a failure into a pass.
"""

from __future__ import annotations

import json

import pytest

from app.agent.orchestrator import Orchestrator
from app.agent.scripted_brain import ScriptedBrain, scripted_brain_factory
from app.runtime.events import EventBus
from app.runtime.store import RunStore
from app.sandbox.data import WORLD
from app.tools.registry import verifier_toolbelt

GOAL = "Record Northwind invoice INV-2047 in the finance system."


# --- a short worker plan: write through the API, then claim ------------------
def _create_entry(obs: str, brain: ScriptedBrain):
    return "http_request", {
        "method": "POST",
        "url": "/finance/api/entries",
        "body": json.dumps(
            {
                "vendor": "Northwind Supplies",
                "invoice_number": "INV-2047",
                "amount": "7312.45",
                "due_date": "2026-08-24",
                "notes": "created via API",
            }
        ),
    }


def _claim(obs: str, brain: ScriptedBrain):
    return "finish", {
        "outcome": "Recorded INV-2047.",
        "summary": "Posted the AP entry through the finance API.",
        "claims": json.dumps(
            {"ap_entry_invoice_number": "INV-2047", "ap_entry_amount": "7312.45"}
        ),
    }


def _claim_again(obs: str, brain: ScriptedBrain):
    brain.notes["saw_feedback"] = "VERIFICATION FAILED" in obs
    return "finish", {
        "outcome": "Re-checked and resubmitted INV-2047.",
        "summary": "Confirmed the entry exists after the verifier pushed back.",
        "claims": json.dumps(
            {"ap_entry_invoice_number": "INV-2047", "ap_entry_amount": "7312.45"}
        ),
    }


WORKER = [_create_entry, _claim, _claim_again]


def _verdict_plan(verdict: str):
    def rule(obs: str, brain: ScriptedBrain):
        return "report_verification", {
            "verdict": verdict,
            "reasoning": f"scripted {verdict}",
            "checks": json.dumps(
                [
                    {
                        "key": "ap_entry_amount",
                        "claimed": "7312.45",
                        "observed": "73.12" if verdict == "refuted" else "7312.45",
                        "ok": verdict == "verified",
                        "note": "GET /finance/api/entries",
                    }
                ]
            ),
        }

    return [rule]


@pytest.mark.asyncio
async def test_refuted_verification_sends_work_back_then_passes(settings):
    settings.max_verification_rounds = 2
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings,
        store,
        EventBus(),
        brain_factory=scripted_brain_factory(
            worker_plan=WORKER,
            verifier_plans=[_verdict_plan("refuted"), _verdict_plan("verified")],
        ),
    )
    record = orchestrator.create_run(GOAL)
    session = store.session(record.id)
    await orchestrator._drive(session)

    assert [r.verdict for r in record.verification_history] == ["refuted", "verified"]
    assert record.status == "succeeded"
    # The worker was actually told what was wrong, not just re-run.
    assert session.brain.notes.get("saw_feedback") is True


@pytest.mark.asyncio
async def test_persistent_refutation_fails_the_run(settings):
    settings.max_verification_rounds = 2
    store = RunStore(settings)
    orchestrator = Orchestrator(
        settings,
        store,
        EventBus(),
        brain_factory=scripted_brain_factory(
            worker_plan=WORKER, verifier_plans=[_verdict_plan("refuted")]
        ),
    )
    record = orchestrator.create_run(GOAL)
    await orchestrator._drive(store.session(record.id))

    assert record.status == "failed"
    assert len(record.verification_history) == 2
    assert "Verification did not pass" in record.failure_reason
    # The side effect still happened - the run reports honestly rather than
    # pretending nothing occurred.
    assert WORLD.find_entry_by_invoice("INV-2047") is not None


@pytest.mark.asyncio
async def test_a_false_claim_is_caught_by_the_real_verifier(settings):
    """No scripted verdict here - the real comparison logic does the work."""

    def _lie(obs: str, brain: ScriptedBrain):
        return "finish", {
            "outcome": "Recorded it.",
            "summary": "Claiming an entry that was never created.",
            "claims": json.dumps({"ap_entry_invoice_number": "INV-9999"}),
        }

    store = RunStore(settings)
    settings.max_verification_rounds = 1
    orchestrator = Orchestrator(
        settings, store, EventBus(), brain_factory=scripted_brain_factory(worker_plan=[_lie])
    )
    record = orchestrator.create_run("Record invoice INV-9999.")
    await orchestrator._drive(store.session(record.id))

    assert record.status == "failed"
    assert record.verification.verdict == "refuted"
    assert any(not c.ok for c in record.verification.checks)


def test_verifier_toolbelt_cannot_mutate():
    belt = verifier_toolbelt()
    assert "finish" not in belt, "the verifier must not be able to end the run itself"
    assert "remember" not in belt
    assert "file_write" not in belt
    assert "browser_click" not in belt
    assert "report_verification" in belt
    assert "http_request" in belt  # read access to the source of truth
    for spec in belt.specs():
        assert spec.read_only, f"{spec.name} is not read-only"
