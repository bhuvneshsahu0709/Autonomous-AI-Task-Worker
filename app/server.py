"""HTTP control plane + static console + the simulated world, in one process.

One process is a deliberate simplification for a prototype: it means `python
-m app` gives you a working demo with no orchestration, and the agent's browser
talks to a real server over real HTTP rather than to a mock. The seam between
"agent runtime" and "simulated world" is the network boundary, so splitting them
later is a deployment change, not a rewrite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.orchestrator import Orchestrator
from app.agent.scripted_brain import scripted_brain_factory
from app.config import get_settings
from app.runtime.events import BUS
from app.runtime.store import RunStore
from app.sandbox import faults
from app.sandbox.data import reset_world
from app.sandbox.routes import router as sandbox_router
from app.sandbox.workspace import seed_workspace

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

EXAMPLE_TASKS: list[dict[str, str]] = [
    {
        "title": "Invoice → internal system",
        "goal": (
            "Find the latest invoice from Northwind Supplies, extract the amount and "
            "due date, enter it into our internal finance system, and tell me once it "
            "is done."
        ),
        "note": "The headline scenario: read, decide, act across two systems, then verify.",
    },
    {
        "title": "Reconcile vendor invoices",
        "goal": (
            "Check every Northwind Supplies invoice in the supplier portal against our "
            "finance system, create AP entries for any that are missing, and give me a "
            "reconciliation summary."
        ),
        "note": "Same code, different shape of work — loops over an unknown number of records.",
    },
    {
        "title": "Export a report to the workspace",
        "goal": (
            "Write a CSV to the workspace at outbox/payables.csv listing every AP entry "
            "in the finance system with vendor, invoice number, amount and due date, "
            "then tell me the total value."
        ),
        "note": "Exercises the API and file tools with no browser work at all.",
    },
    {
        "title": "Ambiguous on purpose",
        "goal": "Pay the Northwind invoice.",
        "note": "Under-specified — a good agent should ask which invoice rather than guess.",
    },
]


class StartRunBody(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    allow_human: bool = True
    reset_sandbox: bool = True
    faults: list[str] | None = None
    # "llm" is the real agent. "scripted" replays a fixed decision sequence for
    # the headline task through the identical runtime - it is how the harness is
    # tested, and it lets the console be demonstrated with no API key. It is not
    # an agent: it cannot handle a task it was not written for.
    planner: Literal["llm", "scripted"] = "llm"


class RespondBody(BaseModel):
    answer: str | None = None
    approved: bool | None = None


def create_app() -> FastAPI:
    settings = get_settings()
    store = RunStore(settings)
    # Two orchestrators over one store/bus: identical runtime, different planner.
    orchestrators = {
        "llm": Orchestrator(settings, store, BUS),
        "scripted": Orchestrator(
            settings, store, BUS, brain_factory=scripted_brain_factory()
        ),
    }
    orchestrator = orchestrators["llm"]

    def runner_for(run_id: str) -> Orchestrator:
        """Route follow-up calls back to the orchestrator that owns the run.

        Resuming a scripted run through the LLM orchestrator would build an LLM
        brain for its verification pass - same store, wrong planner.
        """
        record = store.record(run_id)
        planner = (record.config.get("planner") if record else None) or "llm"
        return orchestrators.get(planner, orchestrator)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        created = seed_workspace(settings.workspace_dir)
        if created:
            log.info("Seeded workspace files: %s", ", ".join(created))
        log.info("Sandbox faults active: %s", sorted(faults.active_faults()) or "none")
        yield
        for run_id in list(store._sessions):  # noqa: SLF001 - shutdown cleanup
            await store.release(run_id)

    app = FastAPI(
        title="Autonomous AI Task Worker",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
    )

    app.state.settings = settings
    app.state.store = store
    app.state.orchestrator = orchestrator

    # ------------------------------------------------------------------
    # Agent control plane
    # ------------------------------------------------------------------
    @app.get("/api/config")
    def api_config() -> dict:
        return {
            "settings": settings.public_snapshot(),
            "has_api_key": bool(settings.anthropic_api_key),
            "examples": EXAMPLE_TASKS,
            "available_faults": faults.ALL_FAULTS,
            "active_faults": sorted(faults.active_faults()),
            "base_url": settings.base_url,
        }

    @app.post("/api/runs", status_code=201)
    async def api_start_run(body: StartRunBody) -> dict:
        if body.planner == "llm" and not settings.anthropic_api_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No ANTHROPIC_API_KEY configured. Copy .env.example to .env and add "
                    "a key, then restart the server — or run the scripted demo instead."
                ),
            )
        if body.reset_sandbox:
            reset_world()
        if body.faults is not None:
            faults.set_faults(body.faults)

        runner = orchestrators[body.planner]
        record = runner.create_run(body.goal, allow_human=body.allow_human)
        record.config["planner"] = body.planner
        runner.launch(record.id)
        return {"run": record.model_dump()}

    @app.get("/api/runs")
    def api_list_runs(limit: int = 30) -> dict:
        records = store.list_records(limit=limit)
        return {
            "runs": [
                {
                    "id": r.id,
                    "goal": r.goal,
                    "status": r.status,
                    "created_at": r.created_at,
                    "steps": len(r.steps),
                    "verdict": r.verification.verdict if r.verification else None,
                }
                for r in records
            ]
        }

    @app.get("/api/runs/{run_id}")
    def api_get_run(run_id: str) -> dict:
        record = store.record(run_id)
        if record is None:
            raise HTTPException(404, "No such run.")
        return {"run": record.model_dump()}

    @app.post("/api/runs/{run_id}/respond")
    async def api_respond(run_id: str, body: RespondBody) -> dict:
        try:
            record = await runner_for(run_id).respond(
                run_id, answer=body.answer, approved=body.approved
            )
        except KeyError:
            raise HTTPException(404, "No such run, or it is no longer in memory.") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"run": record.model_dump()}

    @app.post("/api/runs/{run_id}/cancel")
    async def api_cancel(run_id: str) -> dict:
        ok = await runner_for(run_id).cancel(run_id)
        if not ok:
            raise HTTPException(404, "No such active run.")
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def api_events(run_id: str, request: Request) -> StreamingResponse:
        async def generator():
            try:
                async for event in BUS.stream(run_id):
                    if await request.is_disconnected():
                        break
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:  # pragma: no cover
                raise

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/artifacts/{path:path}")
    def api_artifact(run_id: str, path: str) -> FileResponse:
        root = store.run_dir(run_id).resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "No such artifact.")
        return FileResponse(target)

    # ------------------------------------------------------------------
    # Console + simulated world
    # ------------------------------------------------------------------
    @app.get("/console")
    def console() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    app.include_router(sandbox_router)
    return app


app = create_app()
