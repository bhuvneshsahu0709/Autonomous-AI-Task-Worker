"""Screenshot the console and the simulated apps (for the README / demo).

Run with the server up:  python scripts/capture_ui.py [out_dir]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

PAGES = [
    ("console", "/console", 1500),
    ("portal-login", "/portal/login", 900),
    ("portal-invoices", "/portal/invoices", 900),
    ("finance-dashboard", "/finance", 900),
    ("finance-form", "/finance/entries/new", 900),
]


async def main() -> int:
    from playwright.async_api import async_playwright

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
    out.mkdir(parents=True, exist_ok=True)
    base = get_settings().base_url

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 950})
        page = await context.new_page()

        # Sign in once so the portal pages render authenticated.
        await page.goto(f"{base}/portal/login")
        await page.fill("#username", "ap.clerk@acme.test")
        await page.fill("#password", "Nw!nd-2026")
        await page.click("button[type=submit]")
        await page.wait_for_load_state("networkidle")
        if "login" in page.url:  # the flaky fault fired
            await page.fill("#username", "ap.clerk@acme.test")
            await page.fill("#password", "Nw!nd-2026")
            await page.click("button[type=submit]")
            await page.wait_for_load_state("networkidle")

        for name, path, height in PAGES:
            await page.set_viewport_size({"width": 1440, "height": height})
            await page.goto(f"{base}{path}")
            await page.wait_for_load_state("networkidle")
            target = out / f"{name}.png"
            await page.screenshot(path=str(target))
            print(f"  wrote {target}")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
