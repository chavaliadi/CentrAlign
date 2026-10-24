"""SQLite persistence for the AcmeOps mock company environment.

This is deliberately a *real* database with real state. The agent's writes
persist, which is what makes independent verification meaningful: the verifier
re-reads this store through the UI rather than trusting the agent's own report.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(os.environ.get("ACMEOPS_DB", Path(__file__).parent / "acmeops.sqlite3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY,
    number       TEXT NOT NULL UNIQUE,
    vendor       TEXT NOT NULL,
    -- `amount` is the authoritative total payable, as printed on the PDF.
    -- `net_amount` is the pre-tax subtotal shown in the portal's summary list.
    -- They differ on purpose: an agent that scrapes the list view gets a
    -- plausible-looking wrong number, and verification has to catch it.
    amount       REAL NOT NULL,
    net_amount   REAL NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    issued_date  TEXT NOT NULL,
    due_date     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'unpaid',
    pdf_filename TEXT
);

CREATE TABLE IF NOT EXISTS payables (
    id             INTEGER PRIMARY KEY,
    vendor         TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    amount         REAL NOT NULL,
    due_date       TEXT NOT NULL,
    entered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    entered_by     TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Seeded so a reset always produces the same world. Two vendors with several
# invoices each; dates chosen so "latest" is unambiguous but requires reading
# more than the first row of the table.
SEED_VENDORS = ["Northwind Traders", "Globex Corporation", "Initech Supplies"]

SEED_INVOICES = [
    # number,    vendor,               total,    net,      issued,       due,          status
    ("INV-1031", "Northwind Traders", 4820.00, 4462.96, "2026-08-14", "2026-09-13", "paid"),
    ("INV-1042", "Northwind Traders", 11250.75, 10417.36, "2026-09-02", "2026-10-02", "unpaid"),
    ("INV-1067", "Northwind Traders", 7390.40, 6843.00, "2026-09-28", "2026-10-28", "unpaid"),
    ("INV-2210", "Globex Corporation", 15600.00, 14444.44, "2026-08-30", "2026-09-29", "unpaid"),
    ("INV-2244", "Globex Corporation", 2310.50, 2139.35, "2026-09-15", "2026-10-15", "unpaid"),
    ("INV-2301", "Globex Corporation", 980.00, 907.41, "2026-10-01", "2026-10-31", "paid"),
    ("INV-3390", "Initech Supplies", 6125.25, 5671.53, "2026-09-20", "2026-10-20", "unpaid"),
]

# One payable pre-entered with a WRONG due date, for the correction task.
SEED_PAYABLES = [
    ("Northwind Traders", "INV-1042", 11250.75, "2026-11-02", "j.okafor@acme.example"),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def reset(seed: bool = True, *, include_payables: bool = True) -> None:
    """Drop and rebuild the world. Called before every evaluation run.

    ``include_payables`` controls whether the pre-existing payables row is
    seeded. It exists because that row is a *fixture for one task* - the
    correction task needs a record with a wrong due date to find - and leaving
    it in place for the data-entry task gave the verifier someone else's
    mistake to judge, which it reasonably reported as a failure. Each task now
    resets to the world its own contract is written against.
    """
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if seed:
            conn.executemany("INSERT INTO vendors (name) VALUES (?)", [(v,) for v in SEED_VENDORS])
            conn.executemany(
                "INSERT INTO invoices"
                " (number, vendor, amount, net_amount, issued_date, due_date, status, pdf_filename)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(n, v, a, net, i, d, s, f"{n}.pdf") for (n, v, a, net, i, d, s) in SEED_INVOICES],
            )
            if include_payables:
                conn.executemany(
                    "INSERT INTO payables (vendor, invoice_number, amount, due_date, entered_by)"
                    " VALUES (?, ?, ?, ?, ?)",
                    SEED_PAYABLES,
                )
        conn.commit()
    finally:
        conn.close()


def ensure_ready() -> None:
    if not DB_PATH.exists():
        reset(seed=True)


# --- queries -----------------------------------------------------------------

def list_invoices(vendor: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM invoices WHERE 1=1"
    args: list[Any] = []
    if vendor:
        sql += " AND vendor = ?"
        args.append(vendor)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY issued_date ASC"
    with cursor() as cur:
        return [dict(r) for r in cur.execute(sql, args).fetchall()]


def get_invoice(number: str) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()
        return dict(row) if row else None


def list_payables() -> list[dict[str, Any]]:
    with cursor() as cur:
        return [dict(r) for r in cur.execute("SELECT * FROM payables ORDER BY id ASC").fetchall()]


def get_payable(payable_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute("SELECT * FROM payables WHERE id = ?", (payable_id,)).fetchone()
        return dict(row) if row else None


def insert_payable(vendor: str, invoice_number: str, amount: float, due_date: str, actor: str) -> int:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO payables (vendor, invoice_number, amount, due_date, entered_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (vendor, invoice_number, amount, due_date, actor),
        )
        new_id = int(cur.lastrowid or 0)
        cur.execute(
            "INSERT INTO audit_log (actor, action, detail) VALUES (?, ?, ?)",
            (actor, "payable.create", f"id={new_id} {invoice_number} {amount} due {due_date}"),
        )
        return new_id


def update_payable(payable_id: int, amount: float, due_date: str, actor: str) -> bool:
    with cursor() as cur:
        cur.execute(
            "UPDATE payables SET amount = ?, due_date = ? WHERE id = ?",
            (amount, due_date, payable_id),
        )
        changed = cur.rowcount > 0
        if changed:
            cur.execute(
                "INSERT INTO audit_log (actor, action, detail) VALUES (?, ?, ?)",
                (actor, "payable.update", f"id={payable_id} amount={amount} due={due_date}"),
            )
        return changed


def audit_log() -> list[dict[str, Any]]:
    with cursor() as cur:
        return [dict(r) for r in cur.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()]
