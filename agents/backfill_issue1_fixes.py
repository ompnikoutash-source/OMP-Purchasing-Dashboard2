"""
Forecast Accuracy History -- one-off maintenance for the first report's
findings (2026-08-19).

Three independent fixes against the EXISTING forecast_history.db, on top of
the code fixes already made to sundrieswebapp_refactored.py, agents/utils.py,
and agents/forecast_history_db.py:

1. Adds the scoring_method/used_fallback_average columns (new in this pass)
   to the existing DB file via ALTER TABLE, then backfills them for every
   already-extracted row from its forecast_method string via
   agents.utils.split_fallback_method() -- see that function's docstring for
   why "XGBoost (Global) (fallback to avg)" was misattributing a
   historical-average fallback's error to XGBoost.
2. Deletes sundries' month rows whose target_period_start didn't land on a
   real calendar month-start (day != 01) -- the fingerprint of the
   day-of-month-carried-forward bug (fixed at its source in
   sundrieswebapp_refactored.py, and defensively in
   agents.utils.parse_snapshot_payload()'s _floor_to_month_start()). These
   rows must be DELETEd, not UPDATEd, since target_period_start is part of
   forecast_accuracy's UNIQUE key -- re-running forecast_history_extractor
   afterward cleanly reinserts them with corrected dates.
3. Resets (back to unreconciled) any month row that was reconciled before its
   target period had actually fully elapsed -- the premature-reconciliation
   bug fixed at its source in forecast_history_db.pending_reconciliations().
   This is an UPDATE, not a delete, since it only touches derived columns.

Run once, then re-run forecast_history_extractor -> forecast_reconciler ->
forecast_accuracy_report to regenerate a trustworthy report. Respects the
project's standing no-concurrent-access rule for forecast_history.db --
don't run this alongside the extractor/reconciler/report.

Usage:
    .venv\\Scripts\\python.exe -m agents.backfill_issue1_fixes --dry-run
    .venv\\Scripts\\python.exe -m agents.backfill_issue1_fixes
    .venv\\Scripts\\python.exe -m agents.backfill_issue1_fixes --dataset sundries
"""

from __future__ import annotations

import argparse
from typing import Optional

import pandas as pd

from agents import forecast_history_db as db
from agents.utils import split_fallback_method


def ensure_columns(conn) -> bool:
    """Idempotent ALTER TABLE -- only runs once ever. Deliberately NOT folded
    into forecast_history_db.connect(): that function's own docstring
    explains why DDL against sqlite_master must never run unconditionally on
    every connection (it needs a lock stronger than busy_timeout covers on
    this network share), so schema migrations belong in one-off scripts like
    this one, run under the same non-concurrent-access discipline."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forecast_accuracy)").fetchall()}
    added = False
    if "scoring_method" not in cols:
        conn.execute("ALTER TABLE forecast_accuracy ADD COLUMN scoring_method TEXT")
        added = True
    if "used_fallback_average" not in cols:
        conn.execute("ALTER TABLE forecast_accuracy ADD COLUMN used_fallback_average INTEGER")
        added = True
    if added:
        conn.commit()
    return added


def backfill_scoring_method(conn, dry_run: bool) -> int:
    rows = conn.execute(
        "SELECT DISTINCT forecast_method FROM forecast_accuracy WHERE scoring_method IS NULL"
    ).fetchall()
    methods = [r[0] for r in rows]
    if not methods:
        return 0
    updates = [
        {"forecast_method": m, "scoring_method": sm, "used_fallback_average": int(ufa)}
        for m in methods
        for sm, ufa in [split_fallback_method(m)]
    ]
    print(f"  {len(updates)} distinct forecast_method values to backfill")
    if dry_run:
        return len(updates)
    conn.executemany(
        "UPDATE forecast_accuracy SET scoring_method = :scoring_method, "
        "used_fallback_average = :used_fallback_average "
        "WHERE forecast_method = :forecast_method AND scoring_method IS NULL",
        updates,
    )
    conn.commit()
    return len(updates)


def purge_sundries_malformed_dates(conn, dry_run: bool) -> int:
    count = conn.execute(
        "SELECT COUNT(*) FROM forecast_accuracy WHERE product_type = 'sundries' "
        "AND period_granularity = 'month' AND strftime('%d', target_period_start) != '01'"
    ).fetchone()[0]
    print(f"  {count} sundries month rows with malformed target_period_start")
    if not dry_run and count:
        conn.execute(
            "DELETE FROM forecast_accuracy WHERE product_type = 'sundries' "
            "AND period_granularity = 'month' AND strftime('%d', target_period_start) != '01'"
        )
        conn.commit()
    return count


def reset_premature_reconciliations(conn, dataset: Optional[str], dry_run: bool) -> int:
    sql = (
        "SELECT id, product_type, target_period_start, reconciled_at FROM forecast_accuracy "
        "WHERE period_granularity = 'month' AND actual_demand IS NOT NULL"
    )
    params = []
    if dataset:
        sql += " AND product_type = ?"
        params.append(dataset)
    df = pd.read_sql(sql, conn, params=params or None)
    if df.empty:
        print("  0 reconciled month rows to check")
        return 0

    df["target_period_start"] = pd.to_datetime(df["target_period_start"])
    df["reconciled_at"] = pd.to_datetime(df["reconciled_at"])
    df["period_end"] = df["target_period_start"] + pd.offsets.MonthBegin(1)
    bad_ids = df.loc[df["reconciled_at"] < df["period_end"], "id"].tolist()
    print(f"  {len(bad_ids)} of {len(df)} reconciled month rows were reconciled before their period fully elapsed")
    if not dry_run and bad_ids:
        conn.executemany(
            "UPDATE forecast_accuracy SET actual_demand = NULL, realized_abs_pct_error = NULL, "
            "reconciled_at = NULL WHERE id = ?",
            [(int(i),) for i in bad_ids],
        )
        conn.commit()
    return len(bad_ids)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-off maintenance: backfill scoring_method, purge sundries date-bug "
                     "rows, reset premature reconciliations. See module docstring."
    )
    parser.add_argument("--dataset", choices=["flooring", "sundries", "moulding"], default=None,
                         help="Limit the premature-reconciliation reset to one dataset (other steps always run for all)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    args = parser.parse_args()

    conn = db.connect()
    try:
        print("1. Ensuring scoring_method/used_fallback_average columns exist...")
        added = ensure_columns(conn)
        print(f"   columns added this run: {added}")

        print("2. Backfilling scoring_method/used_fallback_average from forecast_method...")
        backfill_scoring_method(conn, args.dry_run)

        print("3. Purging sundries rows with malformed (non-month-start) target_period_start...")
        purge_sundries_malformed_dates(conn, args.dry_run)

        print("4. Resetting month rows reconciled before their period fully elapsed...")
        reset_premature_reconciliations(conn, args.dataset, args.dry_run)

        if args.dry_run:
            print("\nDry run -- no changes written. Re-run without --dry-run to apply.")
        else:
            print("\nDone. Next: re-run forecast_history_extractor, then forecast_reconciler, then forecast_accuracy_report.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
