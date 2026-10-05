"""Browser control: a real Chromium instance, driven by element ref.

Design notes worth defending:

* **One page per run.** Cookies, session and history persist across steps, so
  "log in, then browse" works the way it does for a person.
* **Act by ref, never by selector.** The model is only ever given refs that the
  snapshot just minted (``e7``), which it cannot hallucinate meaningfully - a
  wrong ref fails loudly and recoverably instead of silently matching the wrong
  node.
* **Every action returns the resulting snapshot.** Observation is not a separate
  step the model has to remember to take; "what happened" is the return value of
  "do the thing".
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from app.agent.schemas import Artifact
from app.config import Settings
from app.tools.base import ToolError, ToolResult, ToolSpec, prop
from app.tools.context import ToolContext

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text(encoding="utf-8")

_REF_RE = re.compile(r"^e\d+$")


class BrowserSession:
    """Owns the Playwright lifecycle for a single run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self.last_status: int | None = None
        self.last_status_url: str = ""
        # ref -> {role, name, ...} from the most recent snapshot. The approval
        # policy reads this to tell "click the Save button" apart from "click a
        # link", which it cannot do from the tool arguments alone (`ref: e12`).
        self.last_elements: dict[str, dict[str, Any]] = {}
        self._screenshot_seq = 0

    @property
    def started(self) -> bool:
        return self._page is not None

    async def start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - install-time problem
            raise ToolError(
                "Playwright is not installed.",
                kind="fatal",
                remediation="Run: pip install playwright && playwright install chromium",
            ) from exc

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.browser_headless,
                slow_mo=self.settings.browser_slow_mo_ms or 0,
            )
        except Exception as exc:  # pragma: no cover
            raise ToolError(
                f"Could not launch Chromium: {exc}",
                kind="fatal",
                remediation="Run `playwright install chromium` and retry.",
            ) from exc

        self._context = await self._browser.new_context(
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        self._context.set_default_timeout(self.settings.browser_timeout_ms)
        self._page = await self._context.new_page()
        self._page.on("response", self._record_response)

    def _record_response(self, response: Any) -> None:
        """Track the status of the main document so the agent can see a 503.

        Without this an error page just looks like "some other page" - with it,
        the observation literally says ``HTTP 503``, which is what turns a
        transient outage into a retry instead of a wrong conclusion.
        """
        try:
            if response.request.resource_type == "document" and response.frame == self._page.main_frame:
                self.last_status = response.status
                self.last_status_url = response.url
        except Exception:  # pragma: no cover - listener must never raise
            pass

    async def stop(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # pragma: no cover
                pass
        self._playwright = self._browser = self._context = self._page = None

    async def require_page(self) -> Any:
        if self._page is None:
            await self.start()
        return self._page

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    async def snapshot(self) -> dict[str, Any]:
        page = await self.require_page()
        try:
            snap = await page.evaluate(SNAPSHOT_JS)
            self.last_elements = {
                el["ref"]: el for el in snap.get("elements", []) if el.get("ref")
            }
            return snap
        except Exception as exc:
            raise ToolError(
                f"Could not read the page: {exc}",
                kind="transient",
                remediation="The page may still be loading. Try the action again.",
            ) from exc

    async def settle(self) -> None:
        """Best-effort wait for the page to stop moving after an action."""
        page = await self.require_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=2_000)
        except Exception:
            pass

    async def locator_for(self, ref: str) -> Any:
        if not _REF_RE.match(ref or ""):
            raise ToolError(
                f"'{ref}' is not a valid element ref.",
                kind="invalid_input",
                remediation="Refs look like 'e7' and come from the most recent page snapshot.",
            )
        page = await self.require_page()
        locator = page.locator(f"[data-agentref='{ref}']")
        try:
            count = await locator.count()
        except Exception as exc:
            raise ToolError(
                f"Could not resolve {ref}: {exc}",
                kind="transient",
                remediation="Call browser_read_page to take a fresh snapshot, then retry.",
            ) from exc
        if count == 0:
            raise ToolError(
                f"Element {ref} is not on the current page.",
                kind="not_found",
                remediation=(
                    "Element refs are only valid for the most recent snapshot. The page has "
                    "changed since then - call browser_read_page and use a ref from the new list."
                ),
            )
        return locator.first

    def is_secret_ref(self, ref: str) -> bool:
        """True if this element is a password field.

        Secrets reach the agent legitimately (it reads them from the workspace),
        but they must not end up in the durable run record, the console timeline
        or a screenshot caption. The snapshot already withholds password
        *values*; this covers what we write *about* the action.
        """
        return (self.last_elements.get(ref) or {}).get("role") == "password"

    def describe_ref(self, ref: str) -> str:
        """Human-readable label for a ref, for policy decisions and the UI."""
        el = self.last_elements.get(ref)
        if not el:
            return ref
        name = el.get("name") or ""
        return f"{el.get('role', 'element')} {name!r}".strip()

    async def screenshot(self, ctx: ToolContext, label: str) -> Artifact:
        page = await self.require_page()
        self._screenshot_seq += 1
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "shot"
        filename = f"{self._screenshot_seq:03d}-{safe}.png"
        path = ctx.artifact_path(filename)
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception as exc:
            raise ToolError(f"Screenshot failed: {exc}", kind="transient") from exc
        return Artifact(
            kind="screenshot",
            label=label,
            path=f"artifacts/{filename}",
            step_index=ctx.step_index,
        )


# ---------------------------------------------------------------------------
# Rendering a snapshot into the text the model actually reads
# ---------------------------------------------------------------------------
def render_snapshot(snap: dict[str, Any], status: int | None = None) -> str:
    lines: list[str] = []
    header = f"PAGE: {snap.get('title') or '(untitled)'}\nURL: {snap.get('url', '')}"
    if status is not None and status != 200:
        header += f"\nHTTP STATUS: {status}"
    lines.append(header)

    elements = snap.get("elements") or []
    if elements:
        lines.append("\nINTERACTIVE ELEMENTS (act on these by ref):")
        for el in elements:
            bits = [f"  [{el['ref']}] {el['role']}"]
            if el.get("name"):
                bits.append(f'"{el["name"]}"')
            if el.get("value"):
                bits.append(f'value="{el["value"]}"')
            if el.get("checked") is not None:
                bits.append(f"checked={el['checked']}")
            if el.get("expanded") is not None:
                bits.append(f"expanded={el['expanded']}")
            if el.get("options"):
                bits.append(f"options={el['options']}")
            if el.get("disabled"):
                bits.append("DISABLED")
            line = " ".join(bits)
            if el.get("context"):
                line += f"\n        in: {el['context']}"
            lines.append(line)
        if snap.get("elementsTruncated"):
            lines.append("  … element list truncated.")
    else:
        lines.append("\n(no interactive elements found)")

    text = (snap.get("text") or "").strip()
    if text:
        lines.append("\nPAGE TEXT:\n" + text)
        if snap.get("textTruncated"):
            lines.append("… page text truncated.")
    return "\n".join(lines)


async def _observe(ctx: ToolContext, action_note: str, label: str) -> ToolResult:
    """Shared tail of every browser action: settle, snapshot, screenshot."""
    await ctx.browser.settle()
    snap = await ctx.browser.snapshot()
    body = render_snapshot(snap, ctx.browser.last_status)
    artifacts: list[Artifact] = []
    try:
        artifacts.append(await ctx.browser.screenshot(ctx, label))
    except ToolError:
        pass  # evidence capture must never fail the actual work
    return ToolResult(content=f"{action_note}\n\n{body}", artifacts=artifacts)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
async def browser_navigate(ctx: ToolContext, url: str) -> ToolResult:
    page = await ctx.browser.require_page()
    if url.startswith("/"):
        url = ctx.base_url.rstrip("/") + url
    if not url.startswith(("http://", "https://")):
        raise ToolError(
            f"'{url}' is not an absolute URL.",
            kind="invalid_input",
            remediation="Use a full URL like http://127.0.0.1:8000/portal/login, or a path starting with '/'.",
        )
    try:
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None:
            ctx.browser.last_status = response.status
    except Exception as exc:
        raise ToolError(
            f"Navigation to {url} failed: {exc}",
            kind="transient",
            remediation="The server may be slow or briefly down. Retry once; if it keeps failing, try a different route.",
        ) from exc
    return await _observe(ctx, f"Navigated to {url}.", f"nav-{url.rsplit('/', 1)[-1] or 'root'}")


async def browser_click(ctx: ToolContext, ref: str) -> ToolResult:
    locator = await ctx.browser.locator_for(ref)
    try:
        label = (await locator.inner_text())[:60].strip() or ref
    except Exception:
        label = ref
    try:
        await locator.click(timeout=ctx.settings.browser_timeout_ms)
    except Exception as exc:
        raise ToolError(
            f"Could not click {ref}: {exc}",
            kind="transient",
            remediation="The element may be covered or the page may have moved. Take a fresh snapshot with browser_read_page and retry.",
        ) from exc
    return await _observe(ctx, f"Clicked {ref} ({label!r}).", f"click-{label}")


async def browser_fill(ctx: ToolContext, ref: str, value: str) -> ToolResult:
    locator = await ctx.browser.locator_for(ref)
    try:
        await locator.fill(value, timeout=ctx.settings.browser_timeout_ms)
    except Exception as exc:
        raise ToolError(
            f"Could not type into {ref}: {exc}",
            kind="invalid_input",
            remediation="That element may not accept text. Check the snapshot - only 'textbox' roles are fillable.",
        ) from exc
    if ctx.browser.is_secret_ref(ref):
        ctx.secrets.add(value)
        shown = "********"
    else:
        shown = value if len(value) < 40 else value[:40] + "…"
    return await _observe(ctx, f"Typed {shown!r} into {ref}.", f"fill-{ref}")


async def browser_select(ctx: ToolContext, ref: str, value: str) -> ToolResult:
    locator = await ctx.browser.locator_for(ref)
    try:
        await locator.select_option(value, timeout=ctx.settings.browser_timeout_ms)
    except Exception as exc:
        raise ToolError(
            f"Could not select {value!r} in {ref}: {exc}",
            kind="invalid_input",
            remediation="Use one of the values listed in the snapshot's options=[...] for that element.",
        ) from exc
    return await _observe(ctx, f"Selected {value!r} in {ref}.", f"select-{ref}")


async def browser_read_page(ctx: ToolContext) -> ToolResult:
    snap = await ctx.browser.snapshot()
    return ToolResult(content=render_snapshot(snap, ctx.browser.last_status))


async def browser_screenshot(ctx: ToolContext, label: str) -> ToolResult:
    artifact = await ctx.browser.screenshot(ctx, label)
    return ToolResult(
        content=f"Screenshot captured as evidence: {artifact.path} ({label}).",
        artifacts=[artifact],
    )


async def browser_wait(ctx: ToolContext, seconds: float) -> ToolResult:
    seconds = max(0.1, min(float(seconds), 10.0))
    await asyncio.sleep(seconds)
    snap = await ctx.browser.snapshot()
    return ToolResult(
        content=f"Waited {seconds:.1f}s.\n\n{render_snapshot(snap, ctx.browser.last_status)}"
    )


BROWSER_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="browser_navigate",
        description=(
            "Open a URL in the browser and return the resulting page snapshot. "
            "Accepts an absolute URL or a site-relative path like '/finance'."
        ),
        parameters={
            "properties": {"url": prop("string", "Absolute URL or path starting with '/'.")},
            "required": ["url"],
        },
        handler=browser_navigate,
        read_only=False,
    ),
    ToolSpec(
        name="browser_click",
        description=(
            "Click an element by its ref from the most recent snapshot, then return "
            "the new page state. Use this for links, buttons, and disclosure toggles."
        ),
        parameters={
            "properties": {"ref": prop("string", "Element ref from the latest snapshot, e.g. 'e7'.")},
            "required": ["ref"],
        },
        handler=browser_click,
        read_only=False,
        mutating=True,
    ),
    ToolSpec(
        name="browser_fill",
        description=(
            "Replace the contents of a text field with the given value, then return "
            "the new page state. Clears any existing text first."
        ),
        parameters={
            "properties": {
                "ref": prop("string", "Element ref of a textbox, e.g. 'e3'."),
                "value": prop("string", "Exact text to enter."),
            },
            "required": ["ref", "value"],
        },
        handler=browser_fill,
        read_only=False,
    ),
    ToolSpec(
        name="browser_select",
        description="Choose an option in a <select> dropdown by its value.",
        parameters={
            "properties": {
                "ref": prop("string", "Element ref of a select."),
                "value": prop("string", "Option value to choose."),
            },
            "required": ["ref", "value"],
        },
        handler=browser_select,
        read_only=False,
    ),
    ToolSpec(
        name="browser_read_page",
        description=(
            "Re-read the current page and return a fresh snapshot with new element refs. "
            "Use this when refs have gone stale or you need to re-check the page."
        ),
        parameters={"properties": {}, "required": []},
        handler=browser_read_page,
        read_only=True,
    ),
    ToolSpec(
        name="browser_screenshot",
        description=(
            "Capture the current page as a PNG attached to the run as evidence. "
            "Use this to prove a result, e.g. a confirmation screen."
        ),
        parameters={
            "properties": {"label": prop("string", "Short description of what this shows.")},
            "required": ["label"],
        },
        handler=browser_screenshot,
        read_only=True,
    ),
    ToolSpec(
        name="browser_wait",
        description="Pause briefly, then re-read the page. Use when something is still loading.",
        parameters={
            "properties": {"seconds": prop("number", "Seconds to wait (0.1-10).")},
            "required": ["seconds"],
        },
        handler=browser_wait,
        read_only=True,
    ),
]
