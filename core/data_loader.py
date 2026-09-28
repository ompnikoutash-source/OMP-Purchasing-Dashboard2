"""
Shared data loading functions for OMP Forecasting applications.

This module contains functions for loading SKU lists, fetching sales history,
and loading trailing volume data from the database.
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from .db_connection import sql_escape
from .demand_adjustment import make_return_aware_daily_series


def load_sku_list_from_excel(
    xlsx_path: Path,
    sheet_name: str = None,
    sku_column: str = None,
    product_type: str = "items",
) -> set:
    """
    Load a set of SKUs from an Excel file.

    Args:
        xlsx_path: Path to the Excel file
        sheet_name: Sheet name to read (default: first sheet or 'items')
        sku_column: Column name containing SKUs (default: first column)
        product_type: Description for logging (e.g., 'sundries', 'moulding')

    Returns:
        Set of SKU strings (uppercase, stripped)
    """
    print(f"\nLoading {product_type} SKU list from {xlsx_path}...")

    if not xlsx_path.exists():
        raise FileNotFoundError(f"Required file not found: {xlsx_path}")

    try:
        # Try to read with specified sheet name, fall back to first sheet
        if sheet_name:
            df = pd.read_excel(xlsx_path, sheet_name=sheet_name, header=0)
        else:
            df = pd.read_excel(xlsx_path, header=0)

        if df.shape[0] == 0 or df.shape[1] < 1:
            raise ValueError(f"File is empty: {xlsx_path}")

        # Use specified column or first column
        if sku_column and sku_column in df.columns:
            col = sku_column
        else:
            col = df.columns[0]

        df[col] = df[col].astype(str).str.strip().str.upper()
        sku_list = set(df[col].dropna())
        sku_list = {sku for sku in sku_list if sku and sku != 'NAN' and len(sku) > 0}

        if len(sku_list) == 0:
            raise ValueError(f"No valid SKUs found in: {xlsx_path}")

        print(f"  Loaded {len(sku_list)} {product_type} SKUs from list")
        return sku_list

    except Exception as e:
        if isinstance(e, (FileNotFoundError, ValueError)):
            raise
        raise


def fetch_last_sale_dates_for_skus(
    conn,
    sku_list: set,
    chunk_size: int = 800,
    exclude_sf: bool = True,
    as_of_date: Optional[pd.Timestamp] = None,
) -> Dict[str, pd.Timestamp]:
    """
    Fetch the last sale date for each SKU.

    Args:
        conn: Database connection
        sku_list: Set of SKUs to query
        chunk_size: Number of SKUs per query batch
        exclude_sf: If True, exclude SF unit of measure (for sundries/moulding)
        as_of_date: Optional date limit for sales

    Returns:
        Dict mapping SKU -> last sale Timestamp
    """
    if not sku_list:
        return {}

    skus = sorted({str(s).strip().upper() for s in sku_list if str(s).strip()})
    results: Dict[str, pd.Timestamp] = {}

    sf_filter = "AND L.SLUM2 NOT LIKE '%SF%'" if exclude_sf else ""

    for i in range(0, len(skus), chunk_size):
        chunk = skus[i:i + chunk_size]
        in_list = ",".join([f"'{sql_escape(s)}'" for s in chunk])

        query = f"""
            SELECT TRIM(L.SLITEM) AS SKU, MAX(H.SHIDAT) AS LAST_SALE_YYYYMMDD
            FROM GSFL2K.SHLINE L
            JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
            WHERE TRIM(L.SLITEM) IN ({in_list})
              {sf_filter}
              AND COALESCE(L.SLBLUo, 0) > 0
              AND H.SHCUST NOT LIKE '%TRANSFER%'
              AND H.SHCUST NOT LIKE '%OMP000%'
              AND H.SHCUST NOT LIKE '%INV000%'
              AND H.SHCUST NOT LIKE '%OLD001%'
            GROUP BY TRIM(L.SLITEM)
        """

        df = pd.read_sql(query, conn)
        if df.empty:
            continue

        for _, r in df.iterrows():
            sku = str(r["SKU"]).strip().upper()
            dt = pd.to_datetime(r["LAST_SALE_YYYYMMDD"], errors="coerce")
            if pd.notna(dt):
                results[sku] = pd.Timestamp(dt)

    return results


def filter_skus_to_active_last_n_days(
    conn,
    sku_list: set,
    days: int = 365,
    exclude_sf: bool = True,
    as_of_date: Optional[pd.Timestamp] = None,
) -> Tuple[set, Dict[str, pd.Timestamp]]:
    """
    Filter SKUs to only those with sales in the last N days.

    Args:
        conn: Database connection
        sku_list: Set of SKUs to filter
        days: Number of days to look back
        exclude_sf: If True, exclude SF unit of measure
        as_of_date: Reference date for the lookback

    Returns:
        Tuple of (active SKU set, last sale date map)
    """
    last_sale_map = fetch_last_sale_dates_for_skus(
        conn, sku_list, exclude_sf=exclude_sf, as_of_date=as_of_date
    )

    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()

    cutoff = as_of_date - pd.Timedelta(days=days)
    active = {sku for sku, dt in last_sale_map.items() if pd.notna(dt) and dt >= cutoff}

    return active, last_sale_map


def load_trailing_12m_volume(
    conn,
    sku_list: List[str],
    exclude_sf: bool = True,
    as_of_date: Optional[pd.Timestamp] = None,
) -> Tuple[Dict[str, float], float]:
    """
    Load trailing 12-month volume for ABC classification.

    Args:
        conn: Database connection
        sku_list: List of SKUs to query
        exclude_sf: If True, exclude SF unit of measure
        as_of_date: Reference date for the 12-month window

    Returns:
        Tuple of (volume map by SKU, total volume)
    """
    print("\nLoading trailing 12-month volume for ABC classification...")

    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()

    start_date = (as_of_date - pd.Timedelta(days=365)).date()
    end_date = as_of_date.date()

    sf_filter = "AND L.SLUM2 NOT LIKE '%SF%'" if exclude_sf else ""
    sku_filter = ""
    params: List = [start_date, end_date]

    if sku_list:
        placeholders = ",".join(["?"] * len(sku_list))
        sku_filter = f" AND TRIM(L.SLITEM) IN ({placeholders}) "
        params.extend([s for s in sku_list])

    sql = f"""
    SELECT TRIM(L.SLITEM) AS ITEM_NUMBER, SUM(COALESCE(L.SLBLUO,0)) AS VOL_12M
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE H.SHIDAT >= ? AND H.SHIDAT <= ? {sf_filter} {sku_filter}
    GROUP BY TRIM(L.SLITEM)
    """

    df = pd.read_sql_query(sql, conn, params=params)
    df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    df["VOL_12M"] = pd.to_numeric(df["VOL_12M"], errors="coerce").fillna(0.0)

    vol_map = dict(zip(df["ITEM_NUMBER"], df["VOL_12M"]))
    total_vol = float(df["VOL_12M"].sum()) if not df.empty else 0.0

    print(f"  Loaded volume data for {len(df)} SKUs")
    return vol_map, total_vol


def fetch_sales_history(
    conn,
    sku: str,
    cutoff_date_str: str = "2020-06-01",
    exclude_sf: bool = True,
) -> pd.DataFrame:
    """
    Fetch sales history for a single SKU.

    Args:
        conn: Database connection
        sku: SKU to query
        cutoff_date_str: Earliest date to include
        exclude_sf: If True, exclude SF unit of measure

    Returns:
        DataFrame with transaction_date and quantity_shipped columns
    """
    sku_clean = sku.strip().upper()
    sf_filter = "AND L.SLUM2 NOT LIKE '%SF%'" if exclude_sf else ""

    query = f"""
    SELECT H.SHIDAT AS SALES_DATE, COALESCE(L.SLBLUO, 0) AS QTY_SOLD
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE TRIM(L.SLITEM) = '{sql_escape(sku_clean)}'
      {sf_filter}
      AND COALESCE(L.SLBLUO, 0) <> 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
    """

    try:
        df = pd.read_sql_query(query, conn)
        if df.empty:
            return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])

        # Parse date column
        raw = df['SALES_DATE']
        raw_str = raw.astype(str).str.strip()
        is_yyyymmdd = raw_str.str.fullmatch(r"\d{8}")
        dt = pd.to_datetime(raw_str.where(is_yyyymmdd), format='%Y%m%d', errors='coerce')
        dt = dt.fillna(pd.to_datetime(raw_str, errors='coerce'))
        df['SALES_DATE'] = dt
        df = df.dropna(subset=['SALES_DATE'])

        if df.empty:
            return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])

        # Apply cutoff
        cutoff_dt = pd.to_datetime(cutoff_date_str)
        df = df[df['SALES_DATE'] >= cutoff_dt]

        # Aggregate by date, then net returns against prior sales so returns
        # erase the original demand signal instead of creating negative spikes.
        df = make_return_aware_daily_series(df, date_col='SALES_DATE', qty_col='QTY_SOLD')
        df = df.rename(columns={'SALES_DATE': 'transaction_date', 'QTY_SOLD': 'quantity_shipped'})
        df['uom'] = 'UNITS'

        return df.sort_values('transaction_date')

    except Exception as e:
        print(f"ERROR fetching sales for {sku}: {e}")
        return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])


def build_in_list(values: List[str]) -> Tuple[str, List[str]]:
    """
    Build a parameterized IN list for SQL queries.

    Args:
        values: List of values to include

    Returns:
        Tuple of (placeholders string, cleaned values list)
    """
    safe = [str(v).strip().upper() for v in values if v]
    if not safe:
        return "''", []
    placeholders = ",".join(["?"] * len(safe))
    return placeholders, safe
