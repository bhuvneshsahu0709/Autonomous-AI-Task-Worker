"""Direct HTTP access to the simulated company's APIs.

Why give the agent an API tool when it already has a browser? Two reasons, and
both are the point of the exercise:

1. **An alternative route.** When the web form keeps rejecting input, a capable
   worker tries the other door. This tool is what makes "attempt a reasonable
   alternative" possible rather than aspirational.
2. **Independent verification.** The verifier reads results back through the API
   - a different surface from the browser the worker wrote through - so a
   verification pass cannot be fooled by a stale rendered page.

Requests are allow-listed to the sandbox origin: the agent cannot reach the open
internet.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from app.tools.base import ToolError, ToolResult, ToolSpec, prop
from app.tools.context import ToolContext

MAX_BODY_CHARS = 12_000
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _check_origin(ctx: ToolContext, url: str) -> str:
    if url.startswith("/"):
        return ctx.base_url.rstrip("/") + url
    parsed = urlparse(url)
    allowed = urlparse(ctx.base_url)
    if (parsed.scheme, parsed.hostname, parsed.port) != (
        allowed.scheme,
        allowed.hostname,
        allowed.port,
    ):
        raise ToolError(
            f"Requests to {parsed.hostname} are not permitted.",
            kind="blocked",
            remediation=(
                f"Only the internal systems at {ctx.base_url} are reachable. "
                "Use a path like '/finance/api/entries'."
            ),
        )
    return url


async def http_request(
    ctx: ToolContext,
    method: str,
    url: str,
    body: str = "",
) -> ToolResult:
    method = (method or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise ToolError(
            f"Unsupported HTTP method '{method}'.",
            kind="invalid_input",
            remediation=f"Use one of: {', '.join(sorted(ALLOWED_METHODS))}.",
        )
    # The verifier runs with the same tool but must stay an observer. Enforcing
    # it here (not just by convention) means a verification pass can never
    # "fix" the thing it was asked to check.
    if ctx.phase == "verify" and method != "GET":
        raise ToolError(
            f"{method} is not permitted during verification.",
            kind="blocked",
            remediation="Verification is read-only. Use GET to inspect the current state.",
        )

    full_url = _check_origin(ctx, url)

    json_body: Any = None
    if body:
        try:
            json_body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"The body is not valid JSON: {exc}",
                kind="invalid_input",
                remediation='Pass a JSON object as a string, e.g. {"vendor":"Acme","amount":"10.00"}.',
            ) from exc

    try:
        response = await ctx.http.request(method, full_url, json=json_body, timeout=20.0)
    except Exception as exc:
        raise ToolError(
            f"{method} {full_url} failed: {exc}",
            kind="transient",
            remediation="The service may be briefly unavailable. Retry once before trying another route.",
        ) from exc

    text = response.text
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n… response truncated."

    # 429/5xx are surfaced as retryable errors so the executor backs off for the
    # model rather than burning a reasoning step on it.
    if response.status_code == 429 or response.status_code >= 500:
        raise ToolError(
            f"{method} {full_url} returned HTTP {response.status_code}.\n{text}",
            kind="transient",
            remediation="This is usually temporary - the same request should be retried.",
        )

    summary = f"{method} {full_url} -> HTTP {response.status_code}\n\n{text}"
    if response.status_code >= 400:
        return ToolResult(
            content=summary
            + "\n\n(The request was rejected. Read the response body - it usually names the exact problem.)",
            is_error=True,
            error_kind="invalid_input",
        )
    return ToolResult(content=summary)


HTTP_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="http_request",
        description=(
            "Call an internal HTTP API directly, bypassing the browser. "
            "Only the company's own systems are reachable. "
            "The finance system exposes GET/POST /finance/api/entries and "
            "GET /finance/api/entries/{id}."
        ),
        parameters={
            "properties": {
                "method": prop("string", "HTTP method.", enum=sorted(ALLOWED_METHODS)),
                "url": prop("string", "Path like '/finance/api/entries' or a full internal URL."),
                "body": prop("string", "JSON request body as a string. Empty for GET."),
            },
            "required": ["method", "url", "body"],
        },
        handler=http_request,
        # Read-only-ness depends on the method, not the tool, so the flag says
        # "may be used read-only" and the policy layer inspects the arguments.
        read_only=True,
        mutating=True,
    ),
]
