"""Drive a full run through the console and screenshot the finished state.

Run with the server up:  python scripts/capture_run.py
Uses the scripted planner so it needs no API key.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

OUT = Path("docs/screenshots")


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    base = get_settings().base_url

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await (await browser.new_context(viewport={"width": 1500, "height": 1050})).new_page()
        await page.goto(f"{base}/console")
        await page.wait_for_selector("#examples .example")

        await page.click("#examples .example")
        await page.click(".opts > summary")
        await page.check("#scripted")
        await page.click("#run-btn")
        await page.wait_for_selector(".step", timeout=45_000)

        # Catch it mid-flight for the "live trace" shot.
        await asyncio.sleep(6)
        await page.screenshot(path=str(OUT / "console-running.png"))
        print(f"  wrote {OUT / 'console-running.png'}")

        # Clear the approval gate(s), then capture the finished state.
        for _ in range(6):
            await page.wait_for_selector(
                "#panel-human:not([hidden]), #panel-result:not([hidden])", timeout=60_000
            )
            if await page.locator("#panel-result:not([hidden])").count():
                break
            # The trace auto-scrolls to the newest step; scroll back so the
            # approval panel is actually in frame.
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.4)
            await page.screenshot(path=str(OUT / "console-approval.png"))
            print(f"  wrote {OUT / 'console-approval.png'}")
            await page.click("#human-actions .btn.ok")
            await page.wait_for_selector("#panel-human", state="hidden", timeout=60_000)

        await page.wait_for_selector("#panel-result:not([hidden])", timeout=90_000)
        await asyncio.sleep(1)
        await page.screenshot(path=str(OUT / "console-result.png"), full_page=True)
        print(f"  wrote {OUT / 'console-result.png'}")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
