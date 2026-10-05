"""HTTP surface of the simulated company world.

These are ordinary server-rendered web apps. Nothing here knows an agent exists,
which is the point: the agent drives them through a real browser exactly as a
person would, and the verifier reads the REST API exactly as an auditor would.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.sandbox import faults
from app.sandbox.data import WORLD, PORTAL_USERNAME, reset_world

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PORTAL_BRAND = {"app_name": "Northwind Supplies · Supplier Portal", "brand_color": "#1f5f8b"}
FINANCE_BRAND = {"app_name": "Acme Finance", "brand_color": "#3c3f8f"}

SESSION_COOKIE = "nw_session"

router = APIRouter()

def _render(
    request: Request, template: str, brand: dict, status_code: int = 200, **ctx
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, template, {**brand, **ctx}, status_code=status_code
    )


def _authed(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token) and token in WORLD.sessions


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><meta charset=utf-8><title>Autonomous AI Task Worker</title>
        <style>body{font:16px/1.6 "Segoe UI",system-ui,sans-serif;max-width:720px;margin:60px auto;padding:0 24px;color:#10151c}
        a{color:#3c3f8f} li{margin:8px 0} code{background:#f0f2f7;padding:2px 6px;border-radius:4px}</style>
        <h1>Autonomous AI Task Worker</h1>
        <p>This process hosts three things:</p>
        <ul>
          <li><a href="/console"><strong>Operator console</strong></a> &mdash; give the agent a task, watch it work.</li>
          <li><a href="/portal/invoices"><strong>Northwind Supplies portal</strong></a> &mdash; simulated external vendor site (login required).</li>
          <li><a href="/finance"><strong>Acme Finance</strong></a> &mdash; simulated internal AP system (<a href="/finance/api/entries">JSON API</a>).</li>
        </ul>
        <p>Sandbox controls: <code>GET /sandbox/state</code>, <code>POST /sandbox/reset</code>.</p>"""
    )


# ---------------------------------------------------------------------------
# Northwind Supplies — vendor portal
# ---------------------------------------------------------------------------
@router.get("/portal", response_class=HTMLResponse)
def portal_root() -> RedirectResponse:
    return RedirectResponse("/portal/invoices", status_code=302)


@router.get("/portal/login", response_class=HTMLResponse)
def portal_login_form(request: Request) -> HTMLResponse:
    return _render(request, "portal_login.html", PORTAL_BRAND, error=None, username="")


@router.post("/portal/login", response_class=HTMLResponse)
def portal_login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
) -> Response:
    WORLD.login_attempts += 1

    # Fault: the very first sign-in attempt fails with a transient 503. A
    # correct agent notices the page is not the invoice list and retries.
    if faults.is_active("flaky_login") and WORLD.bump("login") == 1:
        return _render(
            request, "portal_maintenance.html", PORTAL_BRAND, status_code=503
        )

    if not WORLD.check_credentials(username, password):
        return _render(
            request,
            "portal_login.html",
            PORTAL_BRAND,
            error="Email address or password is incorrect.",
            username=username,
        )

    token = secrets.token_urlsafe(16)
    WORLD.sessions.add(token)
    response = RedirectResponse("/portal/invoices", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@router.get("/portal/logout")
def portal_logout(request: Request) -> RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        WORLD.sessions.discard(token)
    response = RedirectResponse("/portal/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/portal/invoices", response_class=HTMLResponse)
def portal_invoices(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/portal/login", status_code=303)
    invoices = sorted(WORLD.invoices, key=lambda i: i.number)
    return _render(request, "portal_invoices.html", PORTAL_BRAND, invoices=invoices)


@router.get("/portal/invoices/{number}", response_class=HTMLResponse)
def portal_invoice_detail(request: Request, number: str) -> Response:
    if not _authed(request):
        return RedirectResponse("/portal/login", status_code=303)

    # Fault: first detail view is slow enough to approach the browser timeout.
    if faults.is_active("slow_invoice") and WORLD.bump("invoice_detail") == 1:
        time.sleep(3.0)

    invoice = WORLD.invoice(number)
    if invoice is None:
        return HTMLResponse(
            f"<h1>404 - no invoice {number}</h1><p><a href='/portal/invoices'>Back</a></p>",
            status_code=404,
        )
    return _render(request, "portal_invoice_detail.html", PORTAL_BRAND, invoice=invoice)


# ---------------------------------------------------------------------------
# Acme Finance — internal AP system (UI)
# ---------------------------------------------------------------------------
@router.get("/finance", response_class=HTMLResponse)
def finance_dashboard(request: Request, created: int | None = None) -> HTMLResponse:
    created_entry = next((e for e in WORLD.ap_entries if e.id == created), None)
    return _render(
        request,
        "finance_dashboard.html",
        FINANCE_BRAND,
        entries=WORLD.list_entries(),
        created=created_entry,
    )


@router.get("/finance/entries/new", response_class=HTMLResponse)
def finance_new_form(request: Request) -> HTMLResponse:
    return _render(
        request, "finance_new.html", FINANCE_BRAND, form={}, errors={}
    )


@router.post("/finance/entries/new", response_class=HTMLResponse)
def finance_new_submit(
    request: Request,
    vendor: str = Form(""),
    invoice_number: str = Form(""),
    amount: str = Form(""),
    due_date: str = Form(""),
    notes: str = Form(""),
) -> Response:
    entry, errors = WORLD.create_entry(
        vendor=vendor,
        invoice_number=invoice_number,
        amount=amount,
        due_date=due_date,
        notes=notes,
        strict=faults.is_active("strict_validation"),
        created_by="ai-worker",
    )
    if errors:
        form = {
            "vendor": vendor,
            "invoice_number": invoice_number,
            "amount": amount,
            "due_date": due_date,
            "notes": notes,
        }
        return _render(
            request, "finance_new.html", FINANCE_BRAND, form=form, errors=errors
        )
    assert entry is not None
    return RedirectResponse(f"/finance?created={entry.id}", status_code=303)


# ---------------------------------------------------------------------------
# Acme Finance — REST API
# ---------------------------------------------------------------------------
def _rate_limited() -> JSONResponse | None:
    WORLD.api_calls += 1
    if faults.is_active("api_rate_limit") and WORLD.api_calls % 4 == 0:
        return JSONResponse(
            {"error": "rate_limited", "detail": "Too many requests. Retry shortly."},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    return None


@router.get("/finance/api/entries")
def api_list_entries(vendor: str | None = None) -> Response:
    if (limited := _rate_limited()) is not None:
        return limited
    entries = WORLD.list_entries(vendor)
    return JSONResponse(
        {"count": len(entries), "entries": [e.to_dict() for e in entries]}
    )


@router.get("/finance/api/entries/{entry_id}")
def api_get_entry(entry_id: int) -> Response:
    if (limited := _rate_limited()) is not None:
        return limited
    entry = next((e for e in WORLD.ap_entries if e.id == entry_id), None)
    if entry is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(entry.to_dict())


@router.post("/finance/api/entries")
async def api_create_entry(request: Request) -> Response:
    if (limited := _rate_limited()) is not None:
        return limited
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    entry, errors = WORLD.create_entry(
        vendor=str(payload.get("vendor", "")),
        invoice_number=str(payload.get("invoice_number", "")),
        amount=str(payload.get("amount", "")),
        due_date=str(payload.get("due_date", "")),
        notes=str(payload.get("notes", "")),
        strict=faults.is_active("strict_validation"),
        created_by="ai-worker-api",
    )
    if errors:
        return JSONResponse({"error": "validation_failed", "fields": errors}, status_code=422)
    assert entry is not None
    return JSONResponse(entry.to_dict(), status_code=201)


# ---------------------------------------------------------------------------
# Sandbox control
# ---------------------------------------------------------------------------
@router.get("/sandbox/state")
def sandbox_state() -> dict:
    return {
        "invoices": [
            {
                "number": i.number,
                "issue_date": i.issue_date,
                "due_date": i.due_date,
                "amount": i.amount,
                "status": i.status,
            }
            for i in WORLD.invoices
        ],
        "ap_entries": [e.to_dict() for e in WORLD.ap_entries],
        "audit": WORLD.audit,
        "active_faults": sorted(faults.active_faults()),
        "available_faults": faults.ALL_FAULTS,
        "portal_username": PORTAL_USERNAME,
    }


@router.post("/sandbox/reset")
def sandbox_reset(payload: dict | None = None) -> dict:
    reset_world()
    if payload and "faults" in payload:
        faults.set_faults(payload["faults"])
    return {"ok": True, "active_faults": sorted(faults.active_faults())}
