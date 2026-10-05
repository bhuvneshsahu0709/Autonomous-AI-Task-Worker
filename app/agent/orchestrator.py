"""The run orchestrator.

A run is a small state machine:

    EXECUTE ──finish()──> VERIFY ──verified──> SUCCEEDED
       ▲                    │
       │                    └──refuted──> (feedback injected, rounds remaining)
       │                                         │
       └─────────────────────────────────────────┘
       │
       ├──policy gate / ask_human──> AWAITING_HUMAN ──answer──> EXECUTE
       └──budget or error-rate stop──> FAILED

Three properties are worth calling out, because they are the ones that make this
an autonomous *worker* rather than a chat loop:

1. **The agent cannot mark its own homework.** ``finish`` does not end the run;
   it submits claims to an independent verification pass that re-reads the
   systems of record through a different surface. Only that pass can produce
   ``succeeded``.

2. **A refuted verification is not a failure, it is feedback.** The discrepancy
   is injected into the worker's transcript and it gets to fix the problem.

3. **Suspension is cheap.** Waiting on a human releases the loop entirely - no
   open request, no held thread - and resumes by appending the answer as the
   pending tool's result. The model experiences it as a slow tool call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from app.agent import prompts
from app.agent.brain import Brain, BrainError, Decision
from app.agent.executor import Executor
from app.agent.guardrails import Guardrails, StopReason
from app.agent.policy import ApprovalPolicy, summarise_call
from app.agent.redaction import SecretRegistry
from app.agent.schemas import (
    ClaimCheck,
    HumanPrompt,
    RunRecord,
    Step,
    VerificationReport,
)
from app.config import Settings
from app.runtime.events import EventBus
from app.runtime.store import RunSession, RunStore
from app.tools.base import ToolSpec
from app.tools.browser import BrowserSession
from app.tools.context import ToolContext
from app.tools.registry import Toolbelt, verifier_toolbelt, worker_toolbelt

log = logging.getLogger(__name__)

MAX_OBSERVATION_CHARS = 12_000
MAX_STORED_OBSERVATION = 4_000
MAX_NO_ACTION_TURNS = 3
MAX_VERIFY_STEPS = 14


class PhaseOutcome(str, Enum):
    FINISHED = "finished"
    SUSPENDED = "suspended"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


BrainFactory = Any  # (role, toolbelt, system_prompt, usage) -> brain with .decide()


def _default_brain_factory(
    settings: Settings,
) -> Any:
    def make(role: str, toolbelt: Toolbelt, system_prompt: str, usage: Any) -> Brain:
        return Brain(settings, toolbelt, system_prompt, usage=usage)

    return make


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        bus: EventBus,
        *,
        brain_factory: BrainFactory | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.executor = Executor(settings)
        self.policy = ApprovalPolicy(settings)
        # Injected so the integration tests can drive the real loop with a
        # deterministic planner instead of the model. Nothing else changes.
        self.brain_factory = brain_factory or _default_brain_factory(settings)

    # ==================================================================
    # Public API
    # ==================================================================
    def create_run(self, goal: str, *, allow_human: bool = True) -> RunRecord:
        record = RunRecord(goal=goal.strip(), config=self.settings.public_snapshot())
        record.config["allow_human"] = allow_human
        session = self.store.create(record)
        session.messages = [{"role": "user", "content": prompts.goal_message(record.goal)}]
        self._emit(record.id, "run.created", {"run": record.model_dump()})
        return record

    def launch(self, run_id: str) -> None:
        session = self.store.session(run_id)
        if session is None:
            raise KeyError(run_id)
        session.task = asyncio.create_task(self._drive(session))

    async def cancel(self, run_id: str) -> bool:
        session = self.store.session(run_id)
        if session is None:
            return False
        session.cancelled = True
        if session.task is not None:
            session.task.cancel()
        record = session.record
        if record.status not in {"succeeded", "failed", "cancelled"}:
            record.status = "cancelled"
            record.failure_reason = "Cancelled by the operator."
            record.finished_at = _now()
            self._finish_bookkeeping(session)
        await self.store.release(run_id)
        return True

    async def respond(
        self,
        run_id: str,
        *,
        answer: str | None = None,
        approved: bool | None = None,
    ) -> RunRecord:
        """Deliver a human answer and resume the run."""
        session = self.store.session(run_id)
        if session is None:
            raise KeyError(run_id)
        record = session.record
        prompt = record.pending_prompt
        if prompt is None or record.status != "awaiting_human":
            raise ValueError("This run is not waiting for an answer.")

        prompt.answer = answer
        prompt.approved = approved
        prompt.answered_at = _now()
        record.human_exchanges.append(prompt)
        record.pending_prompt = None
        self._emit(run_id, "human.answered", {"prompt": prompt.model_dump()})

        pending = session.pending_tool_call or {}
        session.pending_tool_call = None
        record.status = "running"
        self._emit(run_id, "run.status", {"status": record.status})

        await self._deliver_human_result(session, pending, prompt)
        self.store.persist(record)
        session.task = asyncio.create_task(self._drive(session))
        return record

    # ==================================================================
    # Driver
    # ==================================================================
    async def _drive(self, session: RunSession) -> None:
        record = session.record
        try:
            if record.started_at is None:
                record.started_at = _now()
            record.status = "running"
            self._emit(record.id, "run.status", {"status": record.status})

            await self._ensure_resources(session)

            rounds = 0
            while True:
                outcome, payload = await self._execute_phase(session)

                if outcome is PhaseOutcome.SUSPENDED:
                    return  # the run is parked; `respond` will restart the driver
                if outcome is PhaseOutcome.CANCELLED:
                    return
                if outcome is PhaseOutcome.STOPPED:
                    await self._fail(session, payload)
                    return

                # FINISHED: the worker submitted claims. Verify them.
                rounds += 1
                report = await self._verify_phase(session, rounds)
                record.verification = report
                record.verification_history.append(report)
                self._emit(
                    record.id, "verification.completed", {"report": report.model_dump()}
                )

                if report.verdict == "verified":
                    await self._succeed(session)
                    return

                if rounds >= self.settings.max_verification_rounds:
                    await self._fail(
                        session,
                        f"Verification did not pass after {rounds} round(s): {report.reasoning}",
                        status="failed",
                    )
                    return

                # Hand the discrepancy back and let the worker fix it.
                session.messages.append(
                    {
                        "role": "user",
                        "content": prompts.verification_feedback(
                            self._render_report(report), rounds
                        ),
                    }
                )
                record.status = "running"
                self._emit(
                    record.id,
                    "log",
                    {
                        "level": "warn",
                        "message": f"Verification round {rounds} {report.verdict}; returning to the worker.",
                    },
                )

        except asyncio.CancelledError:
            log.info("Run %s cancelled", record.id)
            raise
        except BrainError as exc:
            await self._fail(session, f"Model error: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Run %s crashed", record.id)
            await self._fail(session, f"Internal error: {exc}")

    # ==================================================================
    # Execute phase
    # ==================================================================
    async def _execute_phase(self, session: RunSession) -> tuple[PhaseOutcome, str]:
        record = session.record
        toolbelt = worker_toolbelt(allow_human=bool(record.config.get("allow_human", True)))
        if session.brain is None:
            session.brain = self.brain_factory(
                "worker",
                toolbelt,
                prompts.worker_system(self.settings.base_url),
                record.usage,
            )
        brain = session.brain
        guardrails: Guardrails = session.guardrails
        no_action_turns = 0

        while True:
            if session.cancelled:
                return PhaseOutcome.CANCELLED, ""

            stop = guardrails.should_stop()
            if stop is not None:
                return PhaseOutcome.STOPPED, stop.message

            self._inject_memory(session)
            decision = await brain.decide(session.messages)

            if decision.refusal:
                return PhaseOutcome.STOPPED, decision.refusal

            session.messages.append({"role": "assistant", "content": decision.raw_content})

            if not decision.has_action:
                no_action_turns += 1
                if no_action_turns >= MAX_NO_ACTION_TURNS:
                    return (
                        PhaseOutcome.STOPPED,
                        "The agent stopped taking actions without finishing the task.",
                    )
                # `tool_choice: any` is rejected on current models, so a turn
                # with no tool call is possible and has to be handled, not
                # assumed away.
                session.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You did not take an action. Every turn must call exactly one tool. "
                            "If the task is complete, call `finish`. If you are blocked, call "
                            "`ask_human`. Otherwise take the next concrete step."
                        ),
                    }
                )
                self._emit(record.id, "log", {"level": "warn", "message": "Model turn had no tool call; nudged."})
                continue
            no_action_turns = 0

            spec = toolbelt.get(decision.tool_name or "")
            if spec is None:
                self._append_tool_result(
                    session,
                    decision.tool_use_id,
                    f"ERROR: `{decision.tool_name}` is not an available tool. "
                    f"Available tools: {', '.join(toolbelt.names)}.",
                    is_error=True,
                )
                continue

            # ---- approval gate (code, not prompt) ----
            gate = self.policy.evaluate(
                spec,
                decision.tool_args,
                element_label=self._element_label(session, spec, decision.tool_args),
                known_facts=record.fact_dict(),
            )
            if gate.requires_approval:
                self._suspend_for_policy(session, decision, spec, gate)
                return PhaseOutcome.SUSPENDED, ""

            # ---- run it ----
            step, outcome = await self._run_step(session, spec, decision, phase="execute")

            control = outcome.result.control
            if control == "finish":
                payload = outcome.result.payload
                record.claims = payload.get("claims", {})
                record.agent_summary = payload.get("summary", "")
                record.outcome = payload.get("outcome", "")
                step.observation = "Submitted for verification."
                self._emit(record.id, "step.completed", self._step_event(step))
                self.store.persist(record)
                return PhaseOutcome.FINISHED, ""

            if control in {"ask_human", "request_approval"}:
                self._suspend_for_model(session, decision, outcome.result.payload["prompt"])
                return PhaseOutcome.SUSPENDED, ""

            self._append_tool_result(
                session,
                decision.tool_use_id,
                outcome.result.content,
                is_error=outcome.result.is_error,
            )

            note = guardrails.intervention(spec.name, decision.tool_args)
            if note:
                session.messages.append({"role": "user", "content": note})
                self._emit(record.id, "log", {"level": "warn", "message": note})

            self.store.persist(record)
            self.store.persist_transcript(session)

    # ==================================================================
    # Verify phase
    # ==================================================================
    async def _verify_phase(self, session: RunSession, round_no: int) -> VerificationReport:
        record = session.record
        record.status = "verifying"
        self._emit(record.id, "run.status", {"status": record.status})
        self._emit(record.id, "verification.started", {"round": round_no, "claims": record.claims})

        toolbelt: Toolbelt = verifier_toolbelt()
        brain = self.brain_factory(
            "verifier", toolbelt, prompts.VERIFIER_SYSTEM, record.usage
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": prompts.verifier_task(
                    record.goal, record.agent_summary, record.claims
                ),
            }
        ]

        # The verifier gets its own browser, so it does not inherit the worker's
        # logged-in session and cannot mistake the worker's leftover page for
        # independent evidence.
        if session.verifier_browser is None:
            session.verifier_browser = BrowserSession(self.settings)

        for _ in range(MAX_VERIFY_STEPS):
            if session.cancelled:
                break
            try:
                decision = await brain.decide(messages)
            except BrainError as exc:
                return VerificationReport(
                    verdict="inconclusive",
                    round=round_no,
                    reasoning=f"The verifier could not run: {exc}",
                )

            messages.append({"role": "assistant", "content": decision.raw_content})
            if not decision.has_action:
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue checking, then call `report_verification`.",
                    }
                )
                continue

            spec = toolbelt.get(decision.tool_name or "")
            if spec is None:
                messages.append(
                    self._tool_result_message(
                        decision.tool_use_id,
                        f"ERROR: `{decision.tool_name}` is not available during verification. "
                        f"Available: {', '.join(toolbelt.names)}.",
                        is_error=True,
                    )
                )
                continue

            step, outcome = await self._run_step(
                session, spec, decision, phase="verify", browser=session.verifier_browser
            )

            if outcome.result.control == "verdict":
                payload = outcome.result.payload
                report = VerificationReport(
                    verdict=payload["verdict"],
                    round=round_no,
                    reasoning=payload["reasoning"],
                    checks=[
                        ClaimCheck(
                            key=str(c.get("key", "")),
                            claimed=str(c.get("claimed", "")),
                            observed=str(c.get("observed", "")),
                            ok=bool(c.get("ok", False)),
                            note=str(c.get("note", "")),
                        )
                        for c in payload.get("checks", [])
                        if isinstance(c, dict)
                    ],
                    evidence=[a.path for a in record.artifacts if a.step_index == step.index],
                )
                step.observation = f"Verdict: {report.verdict}"
                self._emit(record.id, "step.completed", self._step_event(step))
                return report

            messages.append(
                self._tool_result_message(
                    decision.tool_use_id,
                    outcome.result.content,
                    is_error=outcome.result.is_error,
                )
            )

        return VerificationReport(
            verdict="inconclusive",
            round=round_no,
            reasoning=(
                "The verifier ran out of steps before reaching a verdict. "
                "Treat this as unverified."
            ),
        )

    # ==================================================================
    # Step mechanics
    # ==================================================================
    async def _run_step(
        self,
        session: RunSession,
        spec: ToolSpec,
        decision: Decision,
        *,
        phase: str,
        browser: BrowserSession | None = None,
    ):
        record = session.record
        index = len(record.steps) + 1
        # The agent legitimately handles secrets (it reads credentials from the
        # workspace to sign in), but they must not be written into the durable
        # record or streamed to the console. Redact before anything is kept.
        safe_args = session.secrets.scrub_args(
            self._redact(browser or session.browser, spec, decision.tool_args)
        )
        step = Step(
            index=index,
            phase=phase,  # type: ignore[arg-type]
            thought=decision.thought,
            tool=spec.name,
            args=safe_args,
        )
        record.steps.append(step)
        self._emit(
            record.id,
            "step.started",
            {
                "index": index,
                "phase": phase,
                "tool": spec.name,
                "summary": summarise_call(spec, safe_args),
                "thought": decision.thought,
            },
        )

        ctx = ToolContext(
            run=record,
            settings=self.settings,
            run_dir=self.store.run_dir(record.id),
            workspace=self.settings.workspace_dir,
            base_url=self.settings.base_url,
            browser=browser or session.browser,
            http=session.http,
            emit=lambda t, d: self._emit(record.id, t, d),
            secrets=session.secrets,
            phase=phase,  # type: ignore[arg-type]
            step_index=index,
        )

        started = asyncio.get_event_loop().time()
        outcome = await self.executor.run(ctx, spec, decision.tool_args)
        step.duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        step.ended_at = _now()
        step.retries = outcome.retries
        step.status = "ok" if outcome.ok else "error"
        step.error_kind = outcome.error_kind
        # Scrub on the way out, after the registry has seen this step's own
        # output - so the credentials file redacts itself in the same step that
        # revealed it.
        step.observation = session.secrets.scrub(
            outcome.result.content[:MAX_STORED_OBSERVATION]
        )
        step.artifacts = outcome.artifacts
        record.artifacts.extend(outcome.artifacts)

        if phase == "execute":
            session.guardrails.record_call(spec.name, decision.tool_args)
            session.guardrails.record_outcome(ok=outcome.ok)

        self._emit(record.id, "step.completed", self._step_event(step))
        return step, outcome

    def _append_tool_result(
        self, session: RunSession, tool_use_id: str | None, content: str, *, is_error: bool
    ) -> None:
        session.messages.append(
            self._tool_result_message(tool_use_id, content, is_error=is_error)
        )

    @staticmethod
    def _tool_result_message(
        tool_use_id: str | None, content: str, *, is_error: bool = False
    ) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content[:MAX_OBSERVATION_CHARS],
        }
        if is_error:
            block["is_error"] = True
        return {"role": "user", "content": [block]}

    def _inject_memory(self, session: RunSession) -> None:
        """Keep committed facts in front of the model.

        Appended as a fresh message rather than by editing history: the
        transcript stays append-only, which is what keeps previously returned
        reasoning blocks valid on replay.
        """
        facts = session.record.fact_dict()
        if not facts:
            return
        rendered = prompts.memory_block(facts)
        if rendered == session.record.config.get("_last_memory_block"):
            return
        session.record.config["_last_memory_block"] = rendered
        session.messages.append({"role": "user", "content": rendered})

    # ==================================================================
    # Suspension / resume
    # ==================================================================
    def _suspend_for_policy(
        self, session: RunSession, decision: Decision, spec: ToolSpec, gate: Any
    ) -> None:
        record = session.record
        prompt = HumanPrompt(
            kind="approval",
            question=gate.action_summary or f"Approve `{spec.name}`?",
            details=gate.details,
            risk=gate.reason,
        )
        session.pending_tool_call = {
            "gate": "policy",
            "tool_use_id": decision.tool_use_id,
            "tool_name": spec.name,
            "args": decision.tool_args,
        }
        self._park(session, prompt, reason="policy")

    def _suspend_for_model(
        self, session: RunSession, decision: Decision, prompt: HumanPrompt
    ) -> None:
        session.pending_tool_call = {
            "gate": "model",
            "tool_use_id": decision.tool_use_id,
            "tool_name": decision.tool_name,
            "args": decision.tool_args,
        }
        self._park(session, prompt, reason="agent_request")

    def _park(self, session: RunSession, prompt: HumanPrompt, *, reason: str) -> None:
        record = session.record
        record.pending_prompt = prompt
        record.status = "awaiting_human"
        self.store.persist(record)
        self._emit(record.id, "run.status", {"status": record.status})
        self._emit(
            record.id,
            "human.prompt",
            {"prompt": prompt.model_dump(), "reason": reason},
        )

    async def _deliver_human_result(
        self, session: RunSession, pending: dict[str, Any], prompt: HumanPrompt
    ) -> None:
        """Turn the operator's answer into the pending tool call's result."""
        tool_use_id = pending.get("tool_use_id")

        if pending.get("gate") == "policy":
            if prompt.approved:
                toolbelt = worker_toolbelt()
                spec = toolbelt.get(pending["tool_name"])
                if spec is None:  # pragma: no cover - defensive
                    self._append_tool_result(
                        session, tool_use_id, "ERROR: tool no longer available.", is_error=True
                    )
                    return
                decision = Decision(
                    tool_name=spec.name,
                    tool_args=pending["args"],
                    tool_use_id=tool_use_id,
                    thought="(operator approved this action)",
                )
                _, outcome = await self._run_step(session, spec, decision, phase="execute")
                if outcome.result.control == "finish":
                    payload = outcome.result.payload
                    session.record.claims = payload.get("claims", {})
                    session.record.agent_summary = payload.get("summary", "")
                    session.record.outcome = payload.get("outcome", "")
                self._append_tool_result(
                    session,
                    tool_use_id,
                    f"[Operator approved this action.]\n\n{outcome.result.content}",
                    is_error=outcome.result.is_error,
                )
            else:
                note = prompt.answer or "No reason given."
                self._append_tool_result(
                    session,
                    tool_use_id,
                    (
                        f"BLOCKED: the operator declined this action. Reason: {note}\n"
                        "Do not retry it as-is. Either adjust the action to address the "
                        "objection, take a different route, or call `finish` explaining "
                        "what remains undone."
                    ),
                    is_error=True,
                )
            return

        # Model-initiated question or approval request.
        if prompt.kind == "approval":
            verdict = "approved" if prompt.approved else "declined"
            extra = f" Note from the operator: {prompt.answer}" if prompt.answer else ""
            body = f"The operator {verdict} the action.{extra}"
            if not prompt.approved:
                body += " Do not proceed with it; choose another approach or finish and report."
        else:
            body = f"The operator replied: {prompt.answer or '(no answer given)'}"

        self._append_tool_result(session, tool_use_id, body, is_error=False)

    # ==================================================================
    # Termination
    # ==================================================================
    async def _succeed(self, session: RunSession) -> None:
        record = session.record
        record.status = "succeeded"
        record.finished_at = _now()
        self._finish_bookkeeping(session)
        await self.store.release(record.id)

    async def _fail(
        self, session: RunSession, reason: str, *, status: str = "failed"
    ) -> None:
        record = session.record
        record.status = status  # type: ignore[assignment]
        record.failure_reason = reason
        record.finished_at = _now()
        if not record.outcome:
            record.outcome = f"Could not complete the task: {reason}"
        self._finish_bookkeeping(session)
        await self.store.release(record.id)

    def _finish_bookkeeping(self, session: RunSession) -> None:
        record = session.record
        self.store.persist(record)
        self.store.persist_transcript(session)
        self._write_report(session)
        self._emit(
            record.id,
            "run.finished",
            {
                "status": record.status,
                "outcome": record.outcome,
                "failure_reason": record.failure_reason,
                "verification": record.verification.model_dump() if record.verification else None,
                "usage": record.usage.model_dump(),
            },
        )

    def _write_report(self, session: RunSession) -> None:
        """Human-readable evidence bundle entry point."""
        record = session.record
        lines = [
            f"# Run {record.id}",
            "",
            f"**Goal:** {record.goal}",
            f"**Status:** {record.status}",
            f"**Started:** {record.started_at}   **Finished:** {record.finished_at}",
            f"**Steps:** {len(record.steps)}   **LLM calls:** {record.usage.llm_calls}",
            "",
            "## Outcome",
            record.outcome or "(none)",
            "",
        ]
        if record.failure_reason:
            lines += ["## Why it did not complete", record.failure_reason, ""]
        if record.facts:
            lines += ["## Facts learned"]
            lines += [f"- **{f.key}** = `{f.value}`  _{f.note}_" for f in record.facts]
            lines.append("")
        if record.claims:
            lines += ["## Claims submitted"]
            lines += [f"- **{k}**: `{v}`" for k, v in record.claims.items()]
            lines.append("")
        if record.verification:
            report = record.verification
            lines += [
                "## Verification",
                f"**Verdict:** {report.verdict}",
                "",
                report.reasoning,
                "",
            ]
            for check in report.checks:
                mark = "PASS" if check.ok else "FAIL"
                lines.append(
                    f"- [{mark}] **{check.key}** claimed `{check.claimed}` / observed `{check.observed}` — {check.note}"
                )
            lines.append("")
        if record.human_exchanges:
            lines += ["## Operator interactions"]
            for ex in record.human_exchanges:
                verdict = (
                    "approved" if ex.approved else "declined"
                ) if ex.kind == "approval" else (ex.answer or "")
                lines.append(f"- ({ex.kind}) {ex.question} → {verdict}")
            lines.append("")
        screenshots = [a for a in record.artifacts if a.kind == "screenshot"]
        if screenshots:
            lines += ["## Evidence"]
            lines += [f"- `{a.path}` — {a.label}" for a in screenshots]
            lines.append("")
        lines += ["## Action log"]
        for step in record.steps:
            flag = "" if step.status == "ok" else f" [{step.status}:{step.error_kind}]"
            lines.append(f"{step.index}. ({step.phase}) `{step.tool}`{flag} — {summarise_step(step)}")

        try:
            (self.store.run_dir(record.id) / "report.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        except Exception:  # pragma: no cover
            log.exception("Could not write report for %s", record.id)

    # ==================================================================
    # Helpers
    # ==================================================================
    async def _ensure_resources(self, session: RunSession) -> None:
        if session.guardrails is None:
            session.guardrails = Guardrails(self.settings)
        if session.http is None:
            session.http = httpx.AsyncClient(follow_redirects=True)
        if session.browser is None:
            session.browser = BrowserSession(self.settings)
        if session.secrets is None:
            session.secrets = SecretRegistry()

    @staticmethod
    def _redact(
        browser: BrowserSession | None, spec: ToolSpec, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Mask secrets in the copy of the arguments we keep."""
        if spec.name != "browser_fill" or browser is None:
            return dict(args)
        if not browser.is_secret_ref(str(args.get("ref", ""))):
            return dict(args)
        return {**args, "value": "********"}

    def _element_label(
        self, session: RunSession, spec: ToolSpec, args: dict[str, Any]
    ) -> str:
        if spec.name != "browser_click" or session.browser is None:
            return ""
        return session.browser.describe_ref(str(args.get("ref", "")))

    def _emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        self.bus.publish(run_id, event_type, data)

    @staticmethod
    def _step_event(step: Step) -> dict[str, Any]:
        return {
            "index": step.index,
            "phase": step.phase,
            "tool": step.tool,
            "status": step.status,
            "error_kind": step.error_kind,
            "retries": step.retries,
            "duration_ms": step.duration_ms,
            "observation": step.observation[:1500],
            "artifacts": [a.model_dump() for a in step.artifacts],
        }

    @staticmethod
    def _render_report(report: VerificationReport) -> str:
        lines = [f"Verdict: {report.verdict}", report.reasoning, ""]
        for check in report.checks:
            mark = "OK" if check.ok else "MISMATCH"
            lines.append(
                f"- [{mark}] {check.key}: you claimed '{check.claimed}', "
                f"the system shows '{check.observed}'. {check.note}"
            )
        return "\n".join(lines).strip()


def summarise_step(step: Step) -> str:
    try:
        compact = json.dumps(step.args, ensure_ascii=False)
    except TypeError:
        compact = str(step.args)
    return compact[:160]
