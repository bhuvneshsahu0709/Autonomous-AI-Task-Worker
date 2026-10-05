"""In-memory state for the simulated company world.

Two systems live here:

* **Northwind Supplies vendor portal** - an external supplier's billing site the
  agent has to log into and read invoices from.
* **Acme Finance (Accounts Payable)** - our "internal system", with both a web UI
  and a REST API. The REST API is what the *verifier* reads, deliberately a
  different surface than the one the worker writes through.

State is process-local and resettable (``reset_world``) so a demo is repeatable.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock

PORTAL_USERNAME = "ap.clerk@acme.test"
PORTAL_PASSWORD = "Nw!nd-2026"

AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Invoice:
    number: str
    vendor: str
    issue_date: str
    due_date: str
    amount: float
    currency: str
    status: str
    po_number: str
    line_items: tuple[tuple[str, int, float], ...]

    @property
    def amount_display(self) -> str:
        return f"${self.amount:,.2f}"


@dataclass
class APEntry:
    id: int
    vendor: str
    invoice_number: str
    amount: float
    due_date: str
    notes: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    created_by: str = "agent"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
# NOTE: invoice numbers are deliberately NOT in issue-date order (INV-2047 was
# back-dated after a credit re-issue). The portal lists invoices by number, so
# "the latest invoice" cannot be answered by grabbing the last row - the agent
# has to compare the dates. This is the cheapest honest test of reasoning I
# could bake into the world.
SEED_INVOICES: tuple[Invoice, ...] = (
    Invoice(
        number="INV-2041",
        vendor="Northwind Supplies",
        issue_date="2026-06-02",
        due_date="2026-07-02",
        amount=4250.00,
        currency="USD",
        status="Paid",
        po_number="PO-88120",
        line_items=(("Standing desk frame", 5, 620.00), ("Shipping", 1, 1150.00)),
    ),
    Invoice(
        number="INV-2043",
        vendor="Northwind Supplies",
        issue_date="2026-09-18",
        due_date="2026-10-18",
        amount=12480.00,
        currency="USD",
        status="Open",
        po_number="PO-88341",
        line_items=(
            ("Ergonomic chair (Model NW-9)", 24, 480.00),
            ("Assembly service", 24, 40.00),
        ),
    ),
    Invoice(
        number="INV-2047",
        vendor="Northwind Supplies",
        issue_date="2026-07-25",
        due_date="2026-08-24",
        amount=7312.45,
        currency="USD",
        status="Open",
        po_number="PO-88260",
        line_items=(("Acoustic panel set", 15, 412.83), ("Installation", 1, 1120.00)),
    ),
)

SEED_AP_ENTRIES: tuple[dict, ...] = (
    {
        "id": 1,
        "vendor": "Globex Logistics",
        "invoice_number": "GLX-7781",
        "amount": 2210.00,
        "due_date": "2026-09-30",
        "notes": "Freight, Q3",
        "created_at": "2026-09-02T09:14:00+00:00",
        "created_by": "j.okafor",
    },
    {
        "id": 2,
        "vendor": "Northwind Supplies",
        "invoice_number": "INV-2041",
        "amount": 4250.00,
        "due_date": "2026-07-02",
        "notes": "Paid 2026-06-28",
        "created_at": "2026-06-05T11:40:00+00:00",
        "created_by": "j.okafor",
    },
)


class World:
    """Mutable world state.

    Guarded by a lock because the agent's browser and the verifier's API client
    reach it from different threads.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self.reset()

    def reset(self) -> None:
        self.invoices: list[Invoice] = list(SEED_INVOICES)
        self.ap_entries: list[APEntry] = [APEntry(**e) for e in SEED_AP_ENTRIES]
        self._id_seq = itertools.count(len(SEED_AP_ENTRIES) + 1)
        self.sessions: set[str] = set()
        self.login_attempts: int = 0
        self.api_calls: int = 0
        self.audit: list[dict] = []
        # Counters for one-shot faults ("fail the FIRST login"). These live on
        # the world so that resetting the world also re-arms the faults -
        # otherwise a second run in the same process silently gets an easier
        # environment than the first.
        self.fault_counters: dict[str, int] = {}

    def bump(self, key: str) -> int:
        self.fault_counters[key] = self.fault_counters.get(key, 0) + 1
        return self.fault_counters[key]

    # ------------------------------------------------------------------
    # Vendor portal
    # ------------------------------------------------------------------
    def check_credentials(self, username: str, password: str) -> bool:
        return username.strip() == PORTAL_USERNAME and password == PORTAL_PASSWORD

    def invoice(self, number: str) -> Invoice | None:
        return next((i for i in self.invoices if i.number == number), None)

    # ------------------------------------------------------------------
    # Finance system
    # ------------------------------------------------------------------
    def list_entries(self, vendor: str | None = None) -> list[APEntry]:
        with self._lock:
            entries = list(self.ap_entries)
        if vendor:
            needle = vendor.strip().lower()
            entries = [e for e in entries if needle in e.vendor.lower()]
        return entries

    def find_entry_by_invoice(self, invoice_number: str) -> APEntry | None:
        needle = invoice_number.strip().lower()
        with self._lock:
            return next(
                (e for e in self.ap_entries if e.invoice_number.strip().lower() == needle),
                None,
            )

    def create_entry(
        self,
        *,
        vendor: str,
        invoice_number: str,
        amount: str,
        due_date: str,
        notes: str,
        strict: bool,
        created_by: str,
    ) -> tuple[APEntry | None, dict[str, str]]:
        """Create an AP entry, or return field-level validation errors.

        Validation is the main source of *recoverable* failure in the demo, so
        the messages are deliberately actionable - they state the correct
        format. An agent that reads its error output can fix itself; one that
        blindly retries cannot.
        """
        errors: dict[str, str] = {}
        vendor = (vendor or "").strip()
        invoice_number = (invoice_number or "").strip()
        amount_raw = (amount or "").strip()
        due_raw = (due_date or "").strip()

        if not vendor:
            errors["vendor"] = "Vendor is required."
        if not invoice_number:
            errors["invoice_number"] = "Invoice number is required."

        normalised_amount = amount_raw
        if strict:
            if not AMOUNT_RE.match(amount_raw):
                errors["amount"] = (
                    "Amount must be a plain decimal number with no currency symbol "
                    "and no thousands separator (example: 12480.00)."
                )
        else:
            normalised_amount = amount_raw.replace("$", "").replace(",", "").strip()
            if not AMOUNT_RE.match(normalised_amount):
                errors["amount"] = "Amount must be a number (example: 12480.00)."

        if not ISO_DATE_RE.match(due_raw):
            errors["due_date"] = (
                "Due date must use ISO format YYYY-MM-DD (example: 2026-10-18)."
            )
        else:
            try:
                datetime.strptime(due_raw, "%Y-%m-%d")
            except ValueError:
                errors["due_date"] = f"'{due_raw}' is not a real calendar date."

        if invoice_number and self.find_entry_by_invoice(invoice_number):
            errors["invoice_number"] = (
                f"Invoice {invoice_number} has already been recorded. "
                "Duplicate AP entries are not allowed."
            )

        if errors:
            self.audit.append(
                {
                    "event": "ap_entry_rejected",
                    "invoice_number": invoice_number,
                    "errors": errors,
                    "at": _now(),
                }
            )
            return None, errors

        with self._lock:
            entry = APEntry(
                id=next(self._id_seq),
                vendor=vendor,
                invoice_number=invoice_number,
                amount=float(normalised_amount),
                due_date=due_raw,
                notes=(notes or "").strip(),
                created_by=created_by,
            )
            self.ap_entries.append(entry)
            self.audit.append(
                {
                    "event": "ap_entry_created",
                    "id": entry.id,
                    "invoice_number": entry.invoice_number,
                    "amount": entry.amount,
                    "at": entry.created_at,
                }
            )
        return entry, {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


WORLD = World()


def reset_world() -> None:
    WORLD.reset()
