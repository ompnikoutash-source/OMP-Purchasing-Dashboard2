"""
Persistent store for vendor-quoted item costs (Veronica / Strip tab).

Vendor quotes are seeded from the "Unfinished Pricing.xlsx" workbook the first
time a (sku, vendor) cell is seen, then edited directly in the dashboard from
that point on. Every edit is appended to vendor_quote_history so a mistake can
be traced to when it happened and reversed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "vendor_quotes.db"


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_quotes (
            sku        TEXT NOT NULL,
            vendor     TEXT NOT NULL,
            price      REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (sku, vendor)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_quote_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sku        TEXT NOT NULL,
            vendor     TEXT NOT NULL,
            old_price  REAL,
            new_price  REAL,
            changed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed_from_workbook(prices_by_sku: Dict[str, Dict[str, float]]) -> int:
    """Insert workbook prices for (sku, vendor) cells that have no stored value yet.

    Never overwrites a cell that already has a row, so edits made in the
    dashboard are never clobbered by re-running this on a later page load.
    Returns the number of cells seeded.
    """
    conn = _connect()
    try:
        now = _now()
        seeded = 0
        for sku, vendor_prices in (prices_by_sku or {}).items():
            for vendor, price in (vendor_prices or {}).items():
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO vendor_quotes (sku, vendor, price, updated_at) VALUES (?, ?, ?, ?)",
                    (sku, vendor, price, now),
                )
                if cur.rowcount:
                    seeded += 1
        conn.commit()
        return seeded
    finally:
        conn.close()


def load_quotes() -> Dict[str, Dict[str, float]]:
    """sku -> vendor -> price, for every stored quote."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT sku, vendor, price FROM vendor_quotes").fetchall()
    finally:
        conn.close()
    result: Dict[str, Dict[str, float]] = {}
    for sku, vendor, price in rows:
        result.setdefault(sku, {})[vendor] = price
    return result


def apply_quote_changes(changes: List[Tuple[str, str, Optional[float], Optional[float]]]) -> None:
    """Upsert new prices and append one history row per (sku, vendor, old, new)."""
    if not changes:
        return
    conn = _connect()
    try:
        now = _now()
        for sku, vendor, old_price, new_price in changes:
            conn.execute(
                """
                INSERT INTO vendor_quotes (sku, vendor, price, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(sku, vendor) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at
                """,
                (sku, vendor, new_price, now),
            )
            conn.execute(
                "INSERT INTO vendor_quote_history (sku, vendor, old_price, new_price, changed_at) VALUES (?, ?, ?, ?, ?)",
                (sku, vendor, old_price, new_price, now),
            )
        conn.commit()
    finally:
        conn.close()


def load_history(limit: int = 300) -> pd.DataFrame:
    conn = _connect()
    try:
        df = pd.read_sql_query(
            """
            SELECT id, sku, vendor, old_price, new_price, changed_at
            FROM vendor_quote_history
            ORDER BY id DESC
            LIMIT ?
            """,
            conn,
            params=(limit,),
        )
    finally:
        conn.close()
    return df


def revert_history_entry(history_id: int) -> Optional[Tuple[str, str, Any, Any]]:
    """Set (sku, vendor) back to the old_price recorded on a history row.

    The revert itself is logged as a new history entry (old_price = whatever
    was live right before the revert), so the log stays append-only and the
    revert can itself be inspected or undone later.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT sku, vendor, old_price FROM vendor_quote_history WHERE id = ?",
            (history_id,),
        ).fetchone()
        if not row:
            return None
        sku, vendor, old_price = row

        current_row = conn.execute(
            "SELECT price FROM vendor_quotes WHERE sku = ? AND vendor = ?",
            (sku, vendor),
        ).fetchone()
        current_price = current_row[0] if current_row else None

        now = _now()
        conn.execute(
            """
            INSERT INTO vendor_quotes (sku, vendor, price, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(sku, vendor) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at
            """,
            (sku, vendor, old_price, now),
        )
        conn.execute(
            "INSERT INTO vendor_quote_history (sku, vendor, old_price, new_price, changed_at) VALUES (?, ?, ?, ?, ?)",
            (sku, vendor, current_price, old_price, now),
        )
        conn.commit()
        return (sku, vendor, old_price, current_price)
    finally:
        conn.close()
