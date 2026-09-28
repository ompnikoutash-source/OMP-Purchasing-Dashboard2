"""
Replenishment forecasting for SKU list with location-level demand and inventory.

Reads SKUs from ReplenishmentSKUList.xlsx, calculates trailing-demand by location,
computes 2-month and 3-month demand targets, and recommends replenishment when
available inventory is below 2 months of demand (targeting 3 months on hand).
Focuses on specific locations and caps replenishment so the supply location
does not go negative or get depleted below a minimum.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.db_connection import connect, _resolve_as_of_date
from core.data_loader import load_sku_list_from_excel, build_in_list


# ===============================
# CONFIGURATION
# ===============================
SKU_LIST_FILE = "ReplenishmentSKUList.xlsx"
DSN_NAME = "Gartman"

# Demand window
LOOKBACK_MONTHS = 12

# Use SHHEAD.SHLOC for location (recommended). If False, uses SHLINE.SLLOC.
USE_SHLOC = True

# Focus only on these locations
FOCUS_LOCS = [1, 3, 4, 5, 6, 8, 9]
SUPPLY_LOC = 1

# Minimum inventory to retain at the supply location, expressed in months of demand
SUPPLY_LOC_MIN_MONTHS = 1.0

# Output files
OUTPUT_XLSX = "replenishment_recommendations.xlsx"
OUTPUT_CSV = "replenishment_recommendations.csv"


# ===============================
# DATA ACCESS
# ===============================
def _fetch_sales_by_location(
    conn,
    sku_list: List[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    focus_locs: Optional[List[int]] = None,
    chunk_size: int = 500,
) -> pd.DataFrame:
    """Fetch sales history by SKU and location for the lookback window."""
    if not sku_list:
        return pd.DataFrame(columns=["SKU", "LOC", "SALES_DATE", "QTY_SOLD"])

    loc_col = "H.SHLOC" if USE_SHLOC else "L.SLLOC"
    loc_filter = ""
    if focus_locs:
        loc_filter = f"AND {loc_col} IN ({','.join([str(x) for x in focus_locs])})"

    results = []
    for i in range(0, len(sku_list), chunk_size):
        chunk = sku_list[i:i + chunk_size]
        placeholders, values = build_in_list(chunk)

        query = f"""
        SELECT
            TRIM(L.SLITEM) AS SKU,
            {loc_col} AS LOC,
            H.SHIDAT AS SALES_DATE,
            COALESCE(L.SLBLUO, 0) AS QTY_SOLD
        FROM GSFL2K.SHLINE L
        JOIN GSFL2K.SHHEAD H
          ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC
         AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
        WHERE TRIM(L.SLITEM) IN ({placeholders})
          AND H.SHIDAT >= ? AND H.SHIDAT <= ?
          AND COALESCE(L.SLBLUO, 0) <> 0
          AND H.SHCUST NOT LIKE '%TRANSFER%'
          AND H.SHCUST NOT LIKE '%OMP000%'
          AND H.SHCUST NOT LIKE '%INV000%'
          AND H.SHCUST NOT LIKE '%OLD001%'
          {loc_filter}
        """
        params = values + [start_date.date(), end_date.date()]
        df = pd.read_sql_query(query, conn, params=params)
        if df.empty:
            continue

        # Parse date column (handles yyyymmdd or date types)
        raw = df["SALES_DATE"].astype(str).str.strip()
        is_yyyymmdd = raw.str.fullmatch(r"\d{8}")
        dt = pd.to_datetime(raw.where(is_yyyymmdd), format="%Y%m%d", errors="coerce")
        dt = dt.fillna(pd.to_datetime(raw, errors="coerce"))
        df["SALES_DATE"] = dt
        df = df.dropna(subset=["SALES_DATE"])

        results.append(df)

    if not results:
        return pd.DataFrame(columns=["SKU", "LOC", "SALES_DATE", "QTY_SOLD"])

    return pd.concat(results, ignore_index=True)


def _fetch_inventory_by_location(
    conn,
    sku_list: List[str],
    focus_locs: Optional[List[int]] = None,
    chunk_size: int = 800,
) -> pd.DataFrame:
    """Fetch on-hand and committed inventory by SKU and location."""
    if not sku_list:
        return pd.DataFrame(columns=["SKU", "LOC", "QTY_ON_HAND", "QTY_COMMITTED"])

    results = []
    loc_filter = ""
    if focus_locs:
        loc_filter = f"AND B.IBLOC IN ({','.join([str(x) for x in focus_locs])})"

    for i in range(0, len(sku_list), chunk_size):
        chunk = sku_list[i:i + chunk_size]
        placeholders, values = build_in_list(chunk)

        query = f"""
        SELECT
            TRIM(B.IBITEM) AS SKU,
            B.IBLOC AS LOC,
            SUM(B.IBQOH) AS QTY_ON_HAND,
            SUM(B.IBQOO) AS QTY_COMMITTED
        FROM GSFL2K.ITEMBAL B
        WHERE TRIM(B.IBITEM) IN ({placeholders})
          {loc_filter}
        GROUP BY TRIM(B.IBITEM), B.IBLOC
        """
        df = pd.read_sql_query(query, conn, params=values)
        if df.empty:
            continue
        results.append(df)

    if not results:
        return pd.DataFrame(columns=["SKU", "LOC", "QTY_ON_HAND", "QTY_COMMITTED"])

    return pd.concat(results, ignore_index=True)


def _fetch_sku_descriptions(
    conn,
    sku_list: List[str],
    chunk_size: int = 1000,
) -> pd.DataFrame:
    """Fetch SKU descriptions from ITEMMAST."""
    if not sku_list:
        return pd.DataFrame(columns=["SKU", "DESCRIPTION"])

    results = []
    for i in range(0, len(sku_list), chunk_size):
        chunk = sku_list[i:i + chunk_size]
        placeholders, values = build_in_list(chunk)

        query = f"""
        SELECT TRIM(M.IMITEM) AS SKU, TRIM(M.IMDESC) AS DESCRIPTION
        FROM GSFL2K.ITEMMAST M
        WHERE TRIM(M.IMITEM) IN ({placeholders})
        """
        df = pd.read_sql_query(query, conn, params=values)
        if df.empty:
            continue
        results.append(df)

    if not results:
        return pd.DataFrame(columns=["SKU", "DESCRIPTION"])

    return pd.concat(results, ignore_index=True)


# ===============================
# BUSINESS LOGIC
# ===============================
def _compute_replenishment(
    demand_df: pd.DataFrame,
    inv_df: pd.DataFrame,
    lookback_months: int,
    supply_loc: int,
    supply_loc_min_months: float,
) -> pd.DataFrame:
    """Compute demand targets and replenishment recommendations."""
    if demand_df.empty and inv_df.empty:
        return pd.DataFrame(
            columns=[
                "SKU",
                "LOCATION",
                "TOTAL_DEMAND",
                "AVG_MONTHLY_DEMAND",
                "DEMAND_2_MO",
                "DEMAND_3_MO",
                "QTY_ON_HAND",
                "QTY_COMMITTED",
                "AVAILABLE_QTY",
                "MONTHS_OF_SUPPLY",
                "NEED_REPLENISH",
                "REPLENISH_QTY",
            ]
        )

    # Aggregate demand by SKU + LOC
    demand_agg = (
        demand_df.groupby(["SKU", "LOC"], as_index=False)
        .agg(TOTAL_DEMAND=("QTY_SOLD", "sum"))
    )
    demand_agg["AVG_MONTHLY_DEMAND"] = demand_agg["TOTAL_DEMAND"] / float(lookback_months)
    demand_agg["DEMAND_2_MO"] = demand_agg["AVG_MONTHLY_DEMAND"] * 2.0
    demand_agg["DEMAND_3_MO"] = demand_agg["AVG_MONTHLY_DEMAND"] * 3.0

    inv_df = inv_df.copy()
    if not inv_df.empty:
        inv_df["QTY_ON_HAND"] = pd.to_numeric(inv_df["QTY_ON_HAND"], errors="coerce").fillna(0.0)
        inv_df["QTY_COMMITTED"] = pd.to_numeric(inv_df["QTY_COMMITTED"], errors="coerce").fillna(0.0)
        inv_df["AVAILABLE_QTY"] = inv_df["QTY_ON_HAND"] - inv_df["QTY_COMMITTED"]
        inv_df["AVAILABLE_QTY"] = inv_df["AVAILABLE_QTY"].clip(lower=0.0)
    else:
        inv_df = pd.DataFrame(
            columns=["SKU", "LOC", "QTY_ON_HAND", "QTY_COMMITTED", "AVAILABLE_QTY"]
        )

    merged = pd.merge(
        demand_agg,
        inv_df,
        how="outer",
        on=["SKU", "LOC"],
    )

    merged["TOTAL_DEMAND"] = pd.to_numeric(merged["TOTAL_DEMAND"], errors="coerce").fillna(0.0)
    merged["AVG_MONTHLY_DEMAND"] = pd.to_numeric(merged["AVG_MONTHLY_DEMAND"], errors="coerce").fillna(0.0)
    merged["DEMAND_2_MO"] = pd.to_numeric(merged["DEMAND_2_MO"], errors="coerce").fillna(0.0)
    merged["DEMAND_3_MO"] = pd.to_numeric(merged["DEMAND_3_MO"], errors="coerce").fillna(0.0)
    merged["QTY_ON_HAND"] = pd.to_numeric(merged["QTY_ON_HAND"], errors="coerce").fillna(0.0)
    merged["QTY_COMMITTED"] = pd.to_numeric(merged["QTY_COMMITTED"], errors="coerce").fillna(0.0)
    merged["AVAILABLE_QTY"] = pd.to_numeric(merged["AVAILABLE_QTY"], errors="coerce").fillna(
        merged["QTY_ON_HAND"] - merged["QTY_COMMITTED"]
    )
    merged["AVAILABLE_QTY"] = merged["AVAILABLE_QTY"].clip(lower=0.0)

    # Months of supply
    merged["MONTHS_OF_SUPPLY"] = np.where(
        merged["AVG_MONTHLY_DEMAND"] > 0,
        merged["AVAILABLE_QTY"] / merged["AVG_MONTHLY_DEMAND"],
        np.inf,
    )

    # Replenishment logic
    merged["NEED_REPLENISH"] = merged["AVAILABLE_QTY"] < merged["DEMAND_2_MO"]
    merged["REPLENISH_QTY"] = np.where(
        merged["NEED_REPLENISH"],
        np.maximum(0.0, merged["DEMAND_3_MO"] - merged["AVAILABLE_QTY"]),
        0.0,
    )

    # Do not replenish the supply location, and cap replenishment by supply availability
    merged.loc[merged["LOC"] == supply_loc, ["NEED_REPLENISH", "REPLENISH_QTY"]] = [False, 0.0]

    # For each SKU, cap total replenishment to avoid depleting supply location
    def _cap_by_supply(group: pd.DataFrame) -> pd.DataFrame:
        supply_row = group[group["LOC"] == supply_loc]
        if supply_row.empty:
            return group
        supply_available = float(supply_row["AVAILABLE_QTY"].iloc[0])
        supply_avg_monthly = float(supply_row["AVG_MONTHLY_DEMAND"].iloc[0])
        supply_min_keep = max(0.0, supply_avg_monthly * float(supply_loc_min_months))
        max_supply = max(0.0, supply_available - supply_min_keep)
        if max_supply <= 0:
            group.loc[group["LOC"] != supply_loc, "REPLENISH_QTY"] = 0.0
            group.loc[group["LOC"] != supply_loc, "NEED_REPLENISH"] = False
            return group

        total_needed = float(group.loc[group["LOC"] != supply_loc, "REPLENISH_QTY"].sum())
        if total_needed <= max_supply or total_needed <= 0:
            return group

        scale = max_supply / total_needed
        group.loc[group["LOC"] != supply_loc, "REPLENISH_QTY"] *= scale
        return group

    merged = merged.groupby("SKU", group_keys=False).apply(_cap_by_supply)

    merged = merged.rename(columns={"LOC": "LOCATION"})
    merged = merged.sort_values(["SKU", "LOCATION"])

    return merged


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    sku_list_path = base_dir / SKU_LIST_FILE

    print(f"Loading SKU list from {sku_list_path}...")
    sku_set = load_sku_list_from_excel(sku_list_path, product_type="replenishment")
    sku_list = sorted(sku_set)
    print(f"  Loaded {len(sku_list)} SKUs")

    print("Connecting to database...")
    conn = connect(DSN_NAME)
    as_of_date = _resolve_as_of_date(conn)

    start_date = (as_of_date - pd.DateOffset(months=LOOKBACK_MONTHS)).normalize()
    end_date = as_of_date.normalize()
    print(f"Demand window: {start_date.date()} to {end_date.date()} ({LOOKBACK_MONTHS} months)")

    print("Fetching sales history by location...")
    demand_df = _fetch_sales_by_location(conn, sku_list, start_date, end_date, focus_locs=FOCUS_LOCS)
    print(f"  Sales rows: {len(demand_df):,}")

    print("Fetching inventory by location...")
    inv_df = _fetch_inventory_by_location(conn, sku_list, focus_locs=FOCUS_LOCS)
    print(f"  Inventory rows: {len(inv_df):,}")

    print("Computing replenishment recommendations...")
    result = _compute_replenishment(
        demand_df,
        inv_df,
        LOOKBACK_MONTHS,
        supply_loc=SUPPLY_LOC,
        supply_loc_min_months=SUPPLY_LOC_MIN_MONTHS,
    )

    print("Fetching SKU descriptions...")
    desc_df = _fetch_sku_descriptions(conn, sku_list)
    if not desc_df.empty:
        desc_df["SKU"] = desc_df["SKU"].astype(str).str.strip().str.upper()
        result = pd.merge(result, desc_df, how="left", on="SKU")
        if "DESCRIPTION" in result.columns:
            insert_at = list(result.columns).index("SKU") + 1
            cols = list(result.columns)
            cols.insert(insert_at, cols.pop(cols.index("DESCRIPTION")))
            result = result[cols]

    # Attach metadata columns
    result.insert(2, "LOOKBACK_START", start_date.date())
    result.insert(3, "LOOKBACK_END", end_date.date())

    # Write outputs
    out_xlsx = base_dir / OUTPUT_XLSX
    out_csv = base_dir / OUTPUT_CSV

    def _write_excel(path: Path) -> None:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            result.to_excel(writer, index=False, sheet_name="All Locations")

            branch_cols = [
                "SKU",
                "DESCRIPTION",
                "TOTAL_DEMAND",
                "DEMAND_3_MO",
                "AVAILABLE_QTY",
                "MONTHS_OF_SUPPLY",
            ]
            for loc in [3, 4, 5, 6, 8, 9]:
                subset = result[
                    (result["LOCATION"] == loc) & (result["NEED_REPLENISH"] == True)
                ].copy()
                if subset.empty:
                    subset = pd.DataFrame(columns=branch_cols + ["REORDER"])
                else:
                    subset = subset[branch_cols]
                    subset["REORDER"] = ""
                subset.to_excel(writer, index=False, sheet_name=f"Loc {loc}")

    try:
        _write_excel(out_xlsx)
        print(f"Saved results to {out_xlsx}")
    except PermissionError:
        fallback = base_dir / f"replenishment_recommendations_{as_of_date.date()}.xlsx"
        _write_excel(fallback)
        print(f"WARNING: Could not write {out_xlsx} (permission denied).")
        print(f"Saved results to {fallback}")

    try:
        result.to_csv(out_csv, index=False)
        print(f"Saved results to {out_csv}")
    except PermissionError:
        fallback = base_dir / f"replenishment_recommendations_{as_of_date.date()}.csv"
        result.to_csv(fallback, index=False)
        print(f"WARNING: Could not write {out_csv} (permission denied).")
        print(f"Saved results to {fallback}")


if __name__ == "__main__":
    main()
