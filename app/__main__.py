"""Entry point: ``python -m app``."""

from __future__ import annotations

import logging

import uvicorn

from app.config import get_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = get_settings()
    print(f"\n  Operator console   {settings.base_url}/console")
    print(f"  Simulated systems  {settings.base_url}/\n")
    uvicorn.run(
        "app.server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
