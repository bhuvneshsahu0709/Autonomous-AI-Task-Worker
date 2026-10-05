"""Shared fixtures.

The integration tests run the *real* orchestrator, executor, tools, browser and
sandbox. Only the model is substituted. That boundary is chosen deliberately:
everything that can break deterministically is exercised; only the part that
cannot be made deterministic is replaced.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.config import Settings, get_settings
from app.sandbox import faults
from app.sandbox.data import reset_world
from app.sandbox.workspace import seed_workspace
from app.server import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    """A real uvicorn server on a real port.

    The agent drives a real browser; pointing it at a TestClient would not
    exercise anything that matters.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        # `create_app` reads the cached global Settings, and the orchestrator it
        # builds uses `settings.base_url` to drive the browser. Point the global
        # at this port *before* building the app, or the agent navigates to the
        # default 8000 and silently works against a different process.
        global_settings = get_settings()
        global_settings.host = "127.0.0.1"
        global_settings.port = port
        config = uvicorn.Config(
            create_app(), host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("Sandbox server did not start in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="session")
def live_server():
    port = _free_port()
    server = LiveServer(port)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def settings(tmp_path: Path, live_server) -> Settings:
    return Settings(
        ANTHROPIC_API_KEY="test-key-not-used",
        HOST="127.0.0.1",
        PORT=live_server.port,
        BROWSER_HEADLESS=True,
        MAX_STEPS=40,
        MAX_SECONDS=240,
        AUTONOMY_LEVEL="autonomous",
        SANDBOX_FAULTS="flaky_login,strict_validation",
        runs_dir=tmp_path / "runs",
        workspace_dir=tmp_path / "workspace",
    )  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def clean_world(settings: Settings):
    reset_world()
    faults.set_faults(settings.sandbox_faults)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    seed_workspace(settings.workspace_dir)
    yield
    reset_world()
