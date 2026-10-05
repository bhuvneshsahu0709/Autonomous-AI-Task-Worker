"""Smoke-test the simulated world without involving the LLM.

Run: python scripts/smoke_sandbox.py

Exercises exactly the paths the agent has to get through: the auth gate, the
flaky-login fault, the collapsed billing section, strict form validation, and
the JSON API the verifier reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows consoles default to cp1252 and will blow up on the en-dashes and
# arrows in these pages. Force UTF-8 so the tool is usable on every platform.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.sandbox import faults  # noqa: E402
from app.server import create_app  # noqa: E402

CREDS = {"username": "ap.clerk@acme.test", "password": "Nw!nd-2026"}

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<34} {actual!r}")
    if not ok:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    faults.set_faults(["flaky_login", "strict_validation"])
    app = create_app()

    with TestClient(app) as client:
        print("\nVendor portal")
        check("landing page", client.get("/").status_code, 200)
        check(
            "invoices require sign-in",
            client.get("/portal/invoices", follow_redirects=False).status_code,
            303,
        )
        first = client.post("/portal/login", data=CREDS, follow_redirects=False)
        check("login attempt 1 (flaky fault)", first.status_code, 503)
        second = client.post("/portal/login", data=CREDS, follow_redirects=False)
        check("login attempt 2", second.status_code, 303)

        listing = client.get("/portal/invoices")
        check("invoice list reachable", listing.status_code, 200)
        check("amount hidden on list page", "12,480.00" in listing.text, False)

        detail = client.get("/portal/invoices/INV-2043")
        check("invoice detail reachable", detail.status_code, 200)
        check("amount present in detail DOM", "12,480.00" in detail.text, True)
        check("billing section collapsed", "<details class=\"billing\">" in detail.text, True)

        print("\nFinance system")
        check("dashboard", client.get("/finance").status_code, 200)
        check("seeded entries", client.get("/finance/api/entries").json()["count"], 2)

        rejected = client.post(
            "/finance/entries/new",
            data={
                "vendor": "Northwind Supplies",
                "invoice_number": "INV-2043",
                "amount": "$12,480.00",
                "due_date": "18/10/2026",
                "notes": "",
            },
        )
        check("rejects currency-formatted amount", "plain decimal number" in rejected.text, True)
        check("rejects non-ISO date", "ISO format" in rejected.text, True)

        accepted = client.post(
            "/finance/entries/new",
            data={
                "vendor": "Northwind Supplies",
                "invoice_number": "INV-2043",
                "amount": "12480.00",
                "due_date": "2026-10-18",
                "notes": "smoke test",
            },
            follow_redirects=False,
        )
        check("accepts clean values", accepted.status_code, 303)

        entries = client.get("/finance/api/entries").json()
        check("entry visible via API", entries["count"], 3)
        created = [e for e in entries["entries"] if e["invoice_number"] == "INV-2043"][0]
        check("amount stored as number", created["amount"], 12480.00)
        check("due date stored ISO", created["due_date"], "2026-10-18")

        dup = client.post(
            "/finance/entries/new",
            data={
                "vendor": "Northwind Supplies",
                "invoice_number": "INV-2043",
                "amount": "12480.00",
                "due_date": "2026-10-18",
                "notes": "",
            },
        )
        check("duplicate invoice rejected", "already been recorded" in dup.text, True)

        print("\nWorkspace")
        creds_file = Path("workspace/credentials.md")
        check("credentials seeded", creds_file.is_file(), True)
        check("password discoverable", "Nw!nd-2026" in creds_file.read_text(encoding="utf-8"), True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All sandbox checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
