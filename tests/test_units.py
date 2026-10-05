"""Unit tests for the pieces that decide how the agent behaves under stress."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.executor import Executor
from app.agent.guardrails import Guardrails
from app.agent.policy import ApprovalPolicy, _largest_amount
from app.tools.base import ToolError, ToolResult, ToolSpec, prop
from app.tools.browser import render_snapshot
from app.tools.context import ToolContext
from app.tools.files import file_read, file_write
from app.tools.registry import worker_toolbelt
from app.tools.web_http import http_request


# ---------------------------------------------------------------------------
# Snapshot rendering
# ---------------------------------------------------------------------------
def test_snapshot_renders_refs_roles_and_row_context():
    snap = {
        "url": "http://x/invoices",
        "title": "Invoices",
        "elements": [
            {"ref": "e1", "role": "link", "name": "View invoice", "context": "INV-2041 2026-06-02"},
            {"ref": "e2", "role": "link", "name": "View invoice", "context": "INV-2043 2026-09-18"},
            {"ref": "e3", "role": "disclosure", "name": "Show billing", "expanded": False},
        ],
        "text": "INVOICE\tISSUE DATE",
    }
    out = render_snapshot(snap, status=200)

    assert "[e1] link \"View invoice\"" in out
    # The row context is what makes two identical links distinguishable.
    assert "in: INV-2041 2026-06-02" in out
    assert "in: INV-2043 2026-09-18" in out
    assert "expanded=False" in out


def test_snapshot_surfaces_non_200_status():
    snap = {"url": "http://x", "title": "Oops", "elements": [], "text": "down"}
    assert "HTTP STATUS: 503" in render_snapshot(snap, status=503)
    assert "HTTP STATUS" not in render_snapshot(snap, status=200)


# ---------------------------------------------------------------------------
# Filesystem sandbox
# ---------------------------------------------------------------------------
def _ctx(tmp_path: Path, settings, phase="execute") -> ToolContext:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        run=None,  # type: ignore[arg-type]
        settings=settings,
        run_dir=tmp_path / "run",
        workspace=workspace,
        base_url="http://127.0.0.1:9",
        browser=None,  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        emit=lambda *_: None,
        phase=phase,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "../../etc/passwd", "sub/../../outside.txt", "/absolute.txt"],
)
@pytest.mark.asyncio
async def test_file_tools_refuse_to_leave_the_workspace(tmp_path, settings, escape):
    ctx = _ctx(tmp_path, settings)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        await file_read(ctx, escape)
    assert excinfo.value.kind in {"blocked", "not_found"}
    # Crucially: never the contents of the file outside the sandbox.
    assert "secret" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_file_write_then_read_roundtrip(tmp_path, settings):
    ctx = _ctx(tmp_path, settings)
    await file_write(ctx, "outbox/report.csv", "a,b\n1,2\n")
    result = await file_read(ctx, "outbox/report.csv")
    assert "a,b" in result.content


# ---------------------------------------------------------------------------
# HTTP allow-list
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_http_tool_blocks_the_open_internet(tmp_path, settings):
    ctx = _ctx(tmp_path, settings)
    with pytest.raises(ToolError) as excinfo:
        await http_request(ctx, "GET", "https://example.com/data", "")
    assert excinfo.value.kind == "blocked"


@pytest.mark.asyncio
async def test_http_tool_is_read_only_during_verification(tmp_path, settings):
    ctx = _ctx(tmp_path, settings, phase="verify")
    with pytest.raises(ToolError) as excinfo:
        await http_request(ctx, "POST", "/finance/api/entries", "{}")
    assert excinfo.value.kind == "blocked"
    assert "read-only" in excinfo.value.remediation.lower()


# ---------------------------------------------------------------------------
# Approval policy
# ---------------------------------------------------------------------------
def test_amount_detection_handles_currency_formatting():
    assert _largest_amount(["$12,480.00"]) == 12480.00
    assert _largest_amount(["7312.45", "99.00"]) == 7312.45
    assert _largest_amount(["no numbers here"]) is None


def test_policy_ignores_refs_when_looking_for_money(settings):
    """An element ref must never be mistaken for an amount."""
    settings.autonomy_level = "standard"
    settings.approval_amount_threshold = 10_000
    policy = ApprovalPolicy(settings)
    spec = worker_toolbelt().get("browser_click")

    # No facts: a submit click carries no amount, so nothing to gate on.
    low = policy.evaluate(spec, {"ref": "e8"}, element_label="button 'Save AP entry'")
    assert low.amount_in_play != 8.0
    assert not low.requires_approval

    # With the invoice total in memory, the same click is gated.
    high = policy.evaluate(
        spec,
        {"ref": "e8"},
        element_label="button 'Save AP entry'",
        known_facts={"invoice_amount": "12,480.00"},
    )
    assert high.requires_approval
    assert high.amount_in_play == 12480.00


def test_policy_does_not_gate_plain_navigation(settings):
    settings.autonomy_level = "supervised"
    policy = ApprovalPolicy(settings)
    belt = worker_toolbelt()

    nav = policy.evaluate(belt.get("browser_navigate"), {"url": "/finance"})
    assert not nav.requires_approval

    read = policy.evaluate(belt.get("http_request"), {"method": "GET", "url": "/finance/api/entries"})
    assert not read.requires_approval

    write = policy.evaluate(belt.get("http_request"), {"method": "POST", "url": "/finance/api/entries"})
    assert write.requires_approval


def test_supervised_mode_gates_every_write(settings):
    settings.autonomy_level = "supervised"
    settings.approval_amount_threshold = 1_000_000  # far above anything in play
    policy = ApprovalPolicy(settings)
    decision = policy.evaluate(worker_toolbelt().get("file_write"), {"path": "a.txt", "content": "x"})
    assert decision.requires_approval
    assert "supervised_mode" in decision.rules_fired


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
def test_identical_calls_trigger_an_intervention(settings):
    rails = Guardrails(settings)
    args = {"ref": "e4"}
    note = None
    for _ in range(settings.max_repeat_identical_calls):
        rails.record_call("browser_click", args)
        note = rails.intervention("browser_click", args)
    assert note is not None and "identical arguments" in note
    # It fires once, then stops nagging.
    assert rails.intervention("browser_click", args) is None


def test_different_calls_do_not_trigger_an_intervention(settings):
    rails = Guardrails(settings)
    for ref in ("e1", "e2", "e3", "e4"):
        rails.record_call("browser_click", {"ref": ref})
        assert rails.intervention("browser_click", {"ref": ref}) is None


def test_budgets_stop_the_run(settings):
    settings.max_steps = 3
    rails = Guardrails(settings)
    for _ in range(3):
        assert rails.should_stop() is None
        rails.record_outcome(ok=True)
    stop = rails.should_stop()
    assert stop is not None and stop.code == "step_budget"


def test_consecutive_errors_stop_the_run(settings):
    settings.max_consecutive_errors = 3
    rails = Guardrails(settings)
    for _ in range(3):
        rails.record_outcome(ok=False)
    stop = rails.should_stop()
    assert stop is not None and stop.code == "error_rate"

    rails.record_outcome(ok=True)  # a success resets the streak
    assert rails.consecutive_errors == 0


# ---------------------------------------------------------------------------
# Executor retry behaviour
# ---------------------------------------------------------------------------
def _spec(handler, name="flaky") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test",
        parameters={"properties": {}, "required": []},
        handler=handler,
    )


@pytest.mark.asyncio
async def test_transient_failures_are_retried_without_costing_a_step(tmp_path, settings):
    calls = {"n": 0}

    async def handler(ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ToolError("upstream 503", kind="transient")
        return ToolResult(content="recovered")

    outcome = await Executor(settings).run(_ctx(tmp_path, settings), _spec(handler), {})
    assert outcome.ok
    assert outcome.result.content == "recovered"
    assert outcome.retries == 2
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_validation_failures_go_straight_back_to_the_model(tmp_path, settings):
    """A rejected value is information - retrying it verbatim is pointless."""
    calls = {"n": 0}

    async def handler(ctx):
        calls["n"] += 1
        raise ToolError(
            "Amount must be a plain decimal number.",
            kind="invalid_input",
            remediation="Send 12480.00, not $12,480.00.",
        )

    outcome = await Executor(settings).run(_ctx(tmp_path, settings), _spec(handler), {})
    assert not outcome.ok
    assert calls["n"] == 1, "an invalid-input error must not be auto-retried"
    assert outcome.error_kind == "invalid_input"
    assert "Send 12480.00" in outcome.result.content


@pytest.mark.asyncio
async def test_a_crashing_tool_does_not_kill_the_run(tmp_path, settings):
    async def handler(ctx):
        raise RuntimeError("boom")

    outcome = await Executor(settings).run(_ctx(tmp_path, settings), _spec(handler), {})
    assert not outcome.ok
    assert "boom" in outcome.result.content


@pytest.mark.asyncio
async def test_bad_arguments_are_reported_as_fixable(tmp_path, settings):
    async def handler(ctx, required_arg):
        return ToolResult(content="never reached")

    outcome = await Executor(settings).run(_ctx(tmp_path, settings), _spec(handler), {})
    assert not outcome.ok
    assert outcome.error_kind == "invalid_input"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
def test_every_tool_exposes_a_strict_schema():
    for spec in worker_toolbelt().specs():
        schema = spec.anthropic_schema()
        assert schema["strict"] is True
        assert schema["input_schema"]["additionalProperties"] is False
        # `strict` requires every declared property to be required.
        declared = set(schema["input_schema"]["properties"])
        required = set(schema["input_schema"]["required"])
        assert declared == required, f"{spec.name}: {declared ^ required}"
        assert spec.description.strip(), f"{spec.name} has no description"


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
def test_secret_registry_learns_declared_credentials():
    from app.agent.redaction import MASK, SecretRegistry

    registry = SecretRegistry()
    registry.learn_from(
        "# Credentials\n"
        "- Username: `ap.clerk@acme.test`\n"
        "- Password: `Nw!nd-2026`\n"
        "api_key = sk-test-abcdef\n"
    )
    assert len(registry) == 2  # username is not a secret marker

    scrubbed = registry.scrub("signing in with Nw!nd-2026 and sk-test-abcdef")
    assert "Nw!nd-2026" not in scrubbed
    assert "sk-test-abcdef" not in scrubbed
    assert scrubbed.count(MASK) == 2


def test_secret_registry_ignores_trivial_values_and_is_a_no_op_when_empty():
    from app.agent.redaction import SecretRegistry

    registry = SecretRegistry()
    registry.learn_from("password: ab")       # too short to be meaningful
    assert len(registry) == 0
    assert registry.scrub("ab cd") == "ab cd"
