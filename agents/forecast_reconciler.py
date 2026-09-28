"""
Forecast Accuracy History -- reconciliation against actual Gartman sales.

For every forecast_accuracy row whose target_period_start has already
passed, looks up what actually happened and fills in actual_demand /
realized_abs_pct_error (agents/forecast_history_db.record_actuals() owns the
error formula).

Built and verified for flooring first (per plan): flooring's sales query
mirrors flooringwebapp.py's bulk_load_sales_history() exactly (same tables,
same SLUM2 LIKE '%SF%' filter, same SLBLUO column) rather than inventing a
new convention. Sundries/moulding use billing-UOM quantities from the same
SHLINE/SHHEAD join, without the SF filter, matching their own webapp scripts'
sales-history conventions -- extend/verify those the same way before trusting
them at scale (see purchasing_plan.py's docstring on UOM/location mismatches
between product lines).

Usage:
    .venv\\Scripts\\python.exe -m agents.forecast_reconciler --dataset flooring
    .venv\\Scripts\\python.exe -m agents.forecast_reconciler --dataset flooring --dry-run
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from agents import forecast_history_db as db
from core.db_connection import connect

# Same location set purchasing_plan.py documents as the standard demand-health
# extract scope (1,3,4,5,6,8,9,51) -- kept consistent rather than inventing a
# different one for this new tool.
STANDARD_LOCATIONS = (1, 3, 4, 5, 6, 8, 9, 51)


def _month_bounds(dates: List[str]) -> Tuple[str, str]:
    """Earliest month start / latest month-end (exclusive) covering the given
    ISO target_period_start values, so one query can cover every pending
    period instead of one query per period."""
    parsed = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)
    start = parsed[0].replace(day=1)
    last = parsed[-1]
    end = date(last.year + (1 if last.month == 12 else 0), (last.month % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


SQL_BATCH_SIZE = 500  # same ODBC parameter-limit batching flooringwebapp.py's bulk_load_sales_history() uses


def _fetch_actual_monthly(conn, item_numbers: List[str], start: str, end: str, sf_only: bool) -> pd.DataFrame:
    """Actual sold quantity per item per calendar month, batched to stay
    within ODBC parameter limits. Same SHLINE/SHHEAD join
    flooringwebapp.py's bulk_load_sales_history() uses; `sf_only` reproduces
    its `SLUM2 LIKE '%SF%'` filter for flooring, omitted for sundries/moulding
    (EA/BOX/CASE billing units, not SF).

    Excludes internal transfer/inventory-adjustment "customers" (SHCUST
    TRANSFER/OMP000/INV000/OLD001) -- the same filter core.data_loader's
    fetch_sales_history() and sundrieswebapp_refactored.py's own sales
    queries use, so "actual demand" here means real customer sales, not
    warehouse-to-warehouse moves. Note: flooringwebapp.py's own
    bulk_load_sales_history() (which feeds the flooring forecast itself)
    does NOT apply this filter -- so a forecast trained on transfer-inclusive
    history being reconciled against transfer-excluded actuals could show a
    small, consistent bias for flooring specifically. Flagged here rather
    than silently matched to the less-clean convention, since "did the
    customer actually buy this" is the more defensible ground truth for an
    accuracy tracker.
    """
    if not item_numbers:
        return pd.DataFrame(columns=["ITEM_NUMBER", "MONTH_START", "ACTUAL_QTY"])

    sf_filter = "L.SLUM2 LIKE '%SF%' AND " if sf_only else ""
    frames = []
    for batch_start in range(0, len(item_numbers), SQL_BATCH_SIZE):
        batch = item_numbers[batch_start: batch_start + SQL_BATCH_SIZE]
        placeholders = ", ".join(["?" for _ in batch])
        sql = f"""
        SELECT TRIM(L.SLITEM) AS ITEM_NUMBER,
               H.SHIDAT        AS SALES_DATE,
               SUM(COALESCE(L.SLBLUO, 0)) AS QTY_SOLD
        FROM GSFL2K.SHLINE L
        JOIN GSFL2K.SHHEAD H
          ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC
         AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
        WHERE {sf_filter}TRIM(L.SLITEM) IN ({placeholders})
          AND H.SHIDAT >= ?
          AND H.SHIDAT < ?
          AND H.SHCUST NOT LIKE '%TRANSFER%'
          AND H.SHCUST NOT LIKE '%OMP000%'
          AND H.SHCUST NOT LIKE '%INV000%'
          AND H.SHCUST NOT LIKE '%OLD001%'
        GROUP BY TRIM(L.SLITEM), H.SHIDAT
        """
        params = [*batch, start, end]
        df_batch = pd.read_sql(sql, conn, params=params)
        if not df_batch.empty:
            frames.append(df_batch)

    if not frames:
        return pd.DataFrame(columns=["ITEM_NUMBER", "MONTH_START", "ACTUAL_QTY"])
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.upper() for c in df.columns]
    df["SALES_DATE"] = pd.to_datetime(df["SALES_DATE"])
    df["MONTH_START"] = df["SALES_DATE"].dt.to_period("M").dt.to_timestamp().dt.strftime("%Y-%m-%d")
    return df.groupby(["ITEM_NUMBER", "MONTH_START"], as_index=False)["QTY_SOLD"].sum().rename(
        columns={"QTY_SOLD": "ACTUAL_QTY"}
    )


def fetch_actual_monthly_flooring(conn, item_numbers: List[str], start: str, end: str) -> pd.DataFrame:
    return _fetch_actual_monthly(conn, item_numbers, start, end, sf_only=True)


def fetch_actual_monthly_generic(conn, item_numbers: List[str], start: str, end: str) -> pd.DataFrame:
    return _fetch_actual_monthly(conn, item_numbers, start, end, sf_only=False)


FETCHERS = {
    "flooring": fetch_actual_monthly_flooring,
    "sundries": fetch_actual_monthly_generic,
    "moulding": fetch_actual_monthly_generic,
}


def reconcile_dataset(conn_db, conn_gartman, dataset: str, as_of: str, dry_run: bool = False) -> Dict[str, int]:
    pending = db.pending_reconciliations(conn_db, as_of=as_of, product_type=dataset)
    monthly_pending = [p for p in pending if p["period_granularity"] == "month"]
    # "week" rows are intentionally skipped, not an unhandled case: checked
    # directly against the raw historical JSON (2026-08-18/19) for both
    # sundries and moulding, at snapshots months apart -- every "week" series
    # found was a flat, constant value repeated across all weeks (the
    # forecast_series chart-reference line, e.g. mean_weekly_demand), not a
    # genuinely time-varying prediction. It's numerically redundant with the
    # "month" series (same underlying rate, different bucketing), which for
    # every product line IS the real, meaningful forecast (moulding's month
    # rows come from simulate_monthly_projection's actual day-by-day
    # simulation; sundries' from its 30/90/365-day moving-average method;
    # flooring never produces "week" rows at all -- its forecast_series is
    # month-cadence throughout). Reconciling "week" would double-count the
    # same signal, not add a new one. If a future forecast_series ever
    # becomes genuinely time-varying at weekly grain, re-verify this
    # assumption before continuing to skip it.
    skipped_weekly = len(pending) - len(monthly_pending)

    # Consolidated-group display names (e.g. "RH112SRSNB/RH112SRSNB-15/RH112SRSNB-24"
    # -- one forecast row covering several real item numbers that share
    # inventory, per flooringwebapp.py's SKU_CONSOLIDATION_GROUPS) aren't real
    # Gartman item codes and are long enough to break the SLITEM column bind
    # (confirmed: ODBC "Input data is too big to fit into field" on a 39-char
    # value). Skipped here rather than reconciled against their expanded
    # members -- known limitation, not silently papered over.
    is_group = lambda item: "/" in item
    skipped_groups = len({p["item_number"] for p in monthly_pending if is_group(p["item_number"])})
    monthly_pending = [p for p in monthly_pending if not is_group(p["item_number"])]

    if not monthly_pending:
        return {"pending": len(pending), "reconciled": 0, "skipped_weekly": skipped_weekly, "skipped_groups": skipped_groups}

    items = sorted({p["item_number"] for p in monthly_pending})
    start, end = _month_bounds([p["target_period_start"] for p in monthly_pending])
    fetcher = FETCHERS[dataset]
    actuals = fetcher(conn_gartman, items, start, end)
    actual_lookup = {
        (row["ITEM_NUMBER"], row["MONTH_START"]): float(row["ACTUAL_QTY"])
        for _, row in actuals.iterrows()
    }

    updates = []
    for p in monthly_pending:
        key = (p["item_number"], p["target_period_start"])
        # No matching sales row for a month that has fully passed means zero
        # units sold, not "unknown" -- the period is over, there's nothing
        # left to arrive.
        actual = actual_lookup.get(key, 0.0)
        updates.append({"id": p["id"], "actual_demand": actual, "predicted_demand": p["predicted_demand"]})

    if dry_run:
        return {"pending": len(pending), "reconciled": 0, "skipped_weekly": skipped_weekly,
                "skipped_groups": skipped_groups, "would_reconcile": len(updates)}

    n = db.record_actuals(conn_db, updates, reconciled_at=datetime.now().isoformat())
    return {"pending": len(pending), "reconciled": n, "skipped_weekly": skipped_weekly, "skipped_groups": skipped_groups}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile forecast_accuracy predictions against actual Gartman sales.")
    parser.add_argument("--dataset", choices=list(FETCHERS.keys()), default=None,
                         help="Limit to one dataset (default: all three)")
    parser.add_argument("--as-of", default=None, help="ISO date; only reconcile periods before this (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be reconciled without writing")
    args = parser.parse_args()

    as_of = args.as_of or date.today().isoformat()
    datasets = [args.dataset] if args.dataset else list(FETCHERS.keys())

    conn_db = db.connect()
    conn_gartman = connect()
    try:
        for dataset in datasets:
            stats = reconcile_dataset(conn_db, conn_gartman, dataset, as_of, dry_run=args.dry_run)
            print(f"{dataset}: {stats}")
    finally:
        conn_gartman.close()
        conn_db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
