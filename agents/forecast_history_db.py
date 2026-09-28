"""
Forecast Accuracy History -- SQLite storage.

One fact table, `forecast_accuracy`: one row per (item, snapshot_date,
forecasted target period). Populated by forecast_history_extractor.py from
git history; `actual_demand` / `realized_abs_pct_error` start NULL and get
filled in by forecast_reconciler.py once the target period has passed.

The DB file itself (agents/forecast_history.db) is gitignored -- it's derived
from git history + Gartman, fully reproducible, and would just bloat the repo
with binary diffs if committed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

DB_PATH = Path(__file__).resolve().parent / "forecast_history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_accuracy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    product_type TEXT NOT NULL,
    item_number TEXT NOT NULL,
    forecast_method TEXT,
    scoring_method TEXT,
    used_fallback_average INTEGER,
    demand_class TEXT,
    abc_class TEXT,
    period_granularity TEXT NOT NULL,
    period_index INTEGER NOT NULL,
    target_period_start TEXT NOT NULL,
    predicted_demand REAL,
    actual_demand REAL,
    realized_abs_pct_error REAL,
    reconciled_at TEXT,
    UNIQUE(product_type, item_number, snapshot_date, target_period_start, period_granularity)
);
CREATE INDEX IF NOT EXISTS idx_fa_pending ON forecast_accuracy(target_period_start)
    WHERE actual_demand IS NULL;
CREATE INDEX IF NOT EXISTS idx_fa_item ON forecast_accuracy(product_type, item_number);
CREATE INDEX IF NOT EXISTS idx_fa_snapshot ON forecast_accuracy(snapshot_date);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    # journal_mode is a property of the DB FILE, not the connection -- it's
    # sticky once set and does not need to be (and must not be) re-issued on
    # every connect(). This file was switched from WAL to DELETE mode once,
    # manually, on 2026-08-18: WAL's shared-memory locking is unreliable over
    # this network share (\\server\...) and caused a "database is locked"
    # crash. Re-issuing `PRAGMA journal_mode=...` here on every connection
    # was itself a second bug -- that specific pragma needs a lock stronger
    # than busy_timeout will wait for, so it broke immediately whenever
    # another connection had an open write transaction (e.g. a status check
    # run while the extractor was mid-batch). Do not add it back.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    # executescript()'s CREATE TABLE/INDEX IF NOT EXISTS is DDL against
    # sqlite_master -- it needs a lock busy_timeout doesn't reliably cover,
    # so (like the journal_mode pragma above) it must not run unconditionally
    # on every connect(). Only run it the first time the file has no tables
    # yet; every later connect() -- including read-only status checks made
    # while another connection is mid-write -- skips it entirely.
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_accuracy'"
    ).fetchone()
    if not exists:
        conn.executescript(SCHEMA)
    return conn


def upsert_predictions(conn: sqlite3.Connection, rows: Iterable[Dict]) -> int:
    """Insert prediction rows, skipping any that already exist (idempotent --
    safe to re-run the extractor over the same git history). Returns the
    number of rows actually inserted (excluding skipped duplicates)."""
    sql = """
    INSERT OR IGNORE INTO forecast_accuracy
        (snapshot_date, commit_sha, product_type, item_number, forecast_method,
         scoring_method, used_fallback_average,
         demand_class, abc_class, period_granularity, period_index,
         target_period_start, predicted_demand)
    VALUES (:snapshot_date, :commit_sha, :product_type, :item_number, :forecast_method,
            :scoring_method, :used_fallback_average,
            :demand_class, :abc_class, :period_granularity, :period_index,
            :target_period_start, :predicted_demand)
    """
    rows = list(rows)
    if not rows:
        return 0
    cur = conn.executemany(sql, rows)
    conn.commit()
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)


def pending_reconciliations(
    conn: sqlite3.Connection, as_of: str, product_type: Optional[str] = None, limit: Optional[int] = None
) -> List[Dict]:
    """Rows whose target_period_start has already passed (< as_of, ISO date)
    but haven't been reconciled against actual sales yet."""
    # A period is only reconciled once it has fully elapsed relative to
    # as_of -- comparing target_period_start alone (as this used to) would
    # match the current, still-in-progress month/week too, comparing a
    # full-period forecast against a partial period of actual sales and
    # inflating error for no real reason.
    sql = """
    SELECT id, snapshot_date, product_type, item_number, period_granularity,
           target_period_start, predicted_demand
    FROM forecast_accuracy
    WHERE actual_demand IS NULL
      AND (
            (period_granularity = 'month' AND :as_of >= date(target_period_start, 'start of month', '+1 month'))
         OR (period_granularity = 'week'  AND :as_of >= date(target_period_start, '+7 days'))
      )
    """
    params: Dict = {"as_of": as_of}
    if product_type:
        sql += " AND product_type = :product_type"
        params["product_type"] = product_type
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def record_actuals(conn: sqlite3.Connection, updates: Iterable[Dict], reconciled_at: str) -> int:
    """updates: iterable of {"id": ..., "actual_demand": ..., "predicted_demand": ...}.
    Computes realized_abs_pct_error here (not left to the caller) so the
    formula is defined in exactly one place."""
    sql = """
    UPDATE forecast_accuracy
    SET actual_demand = :actual_demand,
        realized_abs_pct_error = :realized_abs_pct_error,
        reconciled_at = :reconciled_at
    WHERE id = :id
    """
    params = []
    for u in updates:
        actual = u["actual_demand"]
        predicted = u.get("predicted_demand")
        if actual and actual != 0:
            err = abs(actual - (predicted or 0.0)) / abs(actual)
        elif predicted:
            err = 1.0  # actual was zero but something was predicted -- 100% miss
        else:
            err = 0.0  # both zero -- a correct "no demand" call
        params.append({
            "id": u["id"],
            "actual_demand": actual,
            "realized_abs_pct_error": err,
            "reconciled_at": reconciled_at,
        })
    if not params:
        return 0
    cur = conn.executemany(sql, params)
    conn.commit()
    return len(params)
