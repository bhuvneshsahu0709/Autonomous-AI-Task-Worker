"""Show exactly what the agent sees when it looks at a page.

Run (with the server already up):  python scripts/smoke_browser.py

This is the fastest way to debug agent behaviour: if the agent did something
inexplicable, look at the snapshot it was reasoning over. Nine times out of ten
the answer is visible here.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows consoles default to cp1252 and will blow up on the en-dashes and
# arrows in these pages. Force UTF-8 so the tool is usable on every platform.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.tools.browser import BrowserSession, render_snapshot  # noqa: E402


async def sign_in(page, session, snap) -> None:
    """Fill and submit the sign-in form using refs from `snap`."""
    def ref_of(role: str) -> str:
        return next(e["ref"] for e in snap["elements"] if e["role"] == role)

    await page.fill(f"[data-agentref='{ref_of('textbox')}']", "ap.clerk@acme.test")
    await page.fill(f"[data-agentref='{ref_of('password')}']", "Nw!nd-2026")
    await page.click(f"[data-agentref='{ref_of('button')}']")


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def main() -> int:
    settings = get_settings()
    session = BrowserSession(settings)
    await session.start()
    page = await session.require_page()

    try:
        await page.goto(f"{settings.base_url}/portal/login")
        snap = await session.snapshot()
        banner("1. Sign-in page — what the agent receives")
        print(render_snapshot(snap, session.last_status))

        await sign_in(page, session, snap)
        await session.settle()

        snap = await session.snapshot()
        banner(f"2. After sign-in (HTTP {session.last_status}) — flaky-login fault visible?")
        print(render_snapshot(snap, session.last_status)[:900])

        # Retry if the transient outage fired.
        if session.last_status and session.last_status >= 500:
            await page.goto(f"{settings.base_url}/portal/login")
            snap = await session.snapshot()
            await sign_in(page, session, snap)
            await session.settle()
            snap = await session.snapshot()
            banner("2b. After retry")
            print(render_snapshot(snap, session.last_status))

        banner("3. Invoice detail BEFORE expanding the billing section")
        await page.goto(f"{settings.base_url}/portal/invoices/INV-2043")
        await session.settle()
        snap = await session.snapshot()
        text = render_snapshot(snap, session.last_status)
        print(text)
        print("\n>>> Is the amount visible yet?  ", "12,480.00" in text)

        banner("4. After clicking the disclosure control")
        disclosure = next(e for e in snap["elements"] if e["role"] == "disclosure")
        print(f"(clicking {disclosure['ref']} — {disclosure['name']!r})")
        await page.click(f"[data-agentref='{disclosure['ref']}']")
        await session.settle()
        snap = await session.snapshot()
        text = render_snapshot(snap, session.last_status)
        print(text)
        print("\n>>> Amount now visible?  ", "12,480.00" in text)
        print(">>> Due date now visible?", "2026-10-18" in text)

        banner("5. Finance form — field labels the agent must map to")
        await page.goto(f"{settings.base_url}/finance/entries/new")
        await session.settle()
        snap = await session.snapshot()
        for el in snap["elements"]:
            print(f"  [{el['ref']}] {el['role']:<11} {el.get('name', '')!r}")
        return 0
    finally:
        await session.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
