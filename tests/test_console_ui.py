"""Drive the operator console in a real browser.

The console is how a human supervises the agent, so it gets tested like any other
interface: start a run from the UI, watch the live trace fill in over SSE, clear
the approval gate when it fires, and read the verdict off the page.

Uses the scripted planner, so it is deterministic and costs nothing.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

STEP_TIMEOUT = 45_000


@pytest.fixture
async def page(live_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1500, "height": 1000})
        page = await context.new_page()
        await page.goto(f"http://127.0.0.1:{live_server.port}/console")
        await page.wait_for_selector("#examples .example")
        yield page
        await browser.close()


async def _run_to_completion(page, approvals: list[bool]) -> int:
    """Wait for the result panel, answering approval prompts as they arrive.

    Returns how many times the gate fired - which is itself the assertion that
    an approval covers one action rather than the rest of the run.
    """
    fired = 0
    for _ in range(8):
        await page.wait_for_selector(
            "#panel-human:not([hidden]), #panel-result:not([hidden])", timeout=STEP_TIMEOUT
        )
        if await page.locator("#panel-result:not([hidden])").count():
            return fired
        approve = approvals[fired] if fired < len(approvals) else approvals[-1]
        await page.click("#human-actions .btn.ok" if approve else "#human-actions .btn.danger")
        fired += 1
        # `state="hidden"` - the default waits for *visible*, which a hidden
        # element never becomes.
        await page.wait_for_selector("#panel-human", state="hidden", timeout=STEP_TIMEOUT)
    raise AssertionError("run never reached a result")


async def _start(page, goal: str) -> None:
    await page.fill("#goal", goal)
    await page.click(".opts > summary")
    await page.check("#scripted")
    await page.click("#run-btn")
    await page.wait_for_selector(".step", timeout=STEP_TIMEOUT)


async def test_console_runs_a_task_and_shows_a_verified_result(page):
    await page.click("#examples .example")          # the headline example
    assert "Northwind" in await page.input_value("#goal")
    await page.click(".opts > summary")
    await page.check("#scripted")
    await page.click("#run-btn")
    await page.wait_for_selector(".step", timeout=STEP_TIMEOUT)

    gates = await _run_to_completion(page, [True])

    # The gate guards each mutating action, not the run as a whole.
    assert gates >= 1, "the approval gate never fired on a $12,480 write"

    verdict = await page.inner_text(".verdict")
    assert "verified" in verdict.lower(), verdict

    facts = await page.inner_text("#facts")
    assert "12,480.00" in facts and "2026-10-18" in facts

    claims = await page.inner_text(".claims")
    assert "INV-2043" in claims and "✕" not in claims

    assert await page.locator(".evidence img").count() > 0

    # Both injected faults are visible in the trace. Note neither is a *tool*
    # error - the click succeeded, the server returned a page saying no. They
    # are semantic failures the agent has to read, which is exactly why the
    # observation text is in the trace rather than just a status badge.
    # textContent, not inner_text: step bodies are collapsed by default, and
    # inner_text only returns what is visible.
    trace = await page.eval_on_selector("#timeline", "el => el.textContent")
    assert "503" in trace, "the transient outage is not visible in the trace"
    assert "plain decimal number" in trace, "the validation rejection is not visible"


async def test_declining_the_gate_prevents_the_write(page):
    await _start(page, "Record the latest Northwind invoice in the finance system.")
    await page.wait_for_selector("#panel-human:not([hidden])", timeout=STEP_TIMEOUT)

    assert await page.locator("#human-actions .btn.ok").count() == 1
    assert await page.locator("#human-actions .btn.danger").count() == 1
    details = await page.inner_text("#human-details")
    assert "12,480.00" in details or "12480" in details.replace(",", "")

    await page.click("#human-actions .btn.danger")
    await page.wait_for_selector("#panel-result:not([hidden])", timeout=STEP_TIMEOUT * 3)

    verdict = await page.inner_text(".verdict")
    assert "verified complete" not in verdict.lower()
