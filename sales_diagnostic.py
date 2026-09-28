"""
Sales Diagnostic Tool - Verify historical sales data from Gartman.

This script pulls raw sales data for specified SKUs and outputs monthly totals
to help verify that the forecasting system is correctly reading from the database.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict
import pandas as pd

# Add core module to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.db_connection import connect, sql_escape


# ============================================================================
# CONFIGURATION - Edit the SKU list here
# ============================================================================
SKU_LIST = [
    "ED3M1007",
    "HI3M5RQC1208",
    "ED3M607",
    "FIDSO85310",
    "FIDSO560",
    "UNBO602428",
]

# How far back to pull data
START_DATE = "2020-06-01"

# Whether to exclude SF unit of measure (True for sundries/moulding, False for flooring)
EXCLUDE_SF = True

# ============================================================================


def fetch_monthly_sales(conn, sku: str, start_date: str, exclude_sf: bool = True) -> pd.DataFrame:
    """
    Fetch sales data for a SKU and aggregate by month.

    Returns DataFrame with columns: SKU, Year, Month, Monthly_Total, Transaction_Count
    """
    sku_clean = sku.strip().upper()
    sf_filter = "AND L.SLUM2 NOT LIKE '%SF%'" if exclude_sf else ""

    query = f"""
    SELECT
        H.SHIDAT AS SALES_DATE,
        COALESCE(L.SLBLUO, 0) AS QTY_SOLD,
        L.SLUM2 AS UOM
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
        ON H.SHCO = L.SLCO
        AND H.SHLOC = L.SLLOC
        AND H.SHORD# = L.SLORD#
        AND H.SHINV# = L.SLINV#
    WHERE TRIM(L.SLITEM) = '{sql_escape(sku_clean)}'
      {sf_filter}
      AND COALESCE(L.SLBLUO, 0) <> 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
    ORDER BY H.SHIDAT
    """

    try:
        df = pd.read_sql_query(query, conn)
        if df.empty:
            print(f"  {sku_clean}: No sales found")
            return pd.DataFrame()

        # Parse date column
        raw = df['SALES_DATE']
        raw_str = raw.astype(str).str.strip()
        is_yyyymmdd = raw_str.str.fullmatch(r"\d{8}")
        dt = pd.to_datetime(raw_str.where(is_yyyymmdd), format='%Y%m%d', errors='coerce')
        dt = dt.fillna(pd.to_datetime(raw_str, errors='coerce'))
        df['SALES_DATE'] = dt
        df = df.dropna(subset=['SALES_DATE'])

        # Filter by start date
        start_dt = pd.to_datetime(start_date)
        df = df[df['SALES_DATE'] >= start_dt]

        if df.empty:
            print(f"  {sku_clean}: No sales after {start_date}")
            return pd.DataFrame()

        # Aggregate by month
        df['Year'] = df['SALES_DATE'].dt.year
        df['Month'] = df['SALES_DATE'].dt.month
        df['YearMonth'] = df['SALES_DATE'].dt.to_period('M')

        monthly = df.groupby(['Year', 'Month', 'YearMonth']).agg({
            'QTY_SOLD': 'sum',
            'SALES_DATE': 'count'
        }).reset_index()
        monthly.columns = ['Year', 'Month', 'YearMonth', 'Monthly_Total', 'Transaction_Count']
        monthly['SKU'] = sku_clean
        monthly = monthly[['SKU', 'Year', 'Month', 'YearMonth', 'Monthly_Total', 'Transaction_Count']]

        total = monthly['Monthly_Total'].sum()
        print(f"  {sku_clean}: {len(monthly)} months, {total:,.0f} total units")

        return monthly

    except Exception as e:
        print(f"  ERROR for {sku_clean}: {e}")
        return pd.DataFrame()


def fetch_raw_transactions(conn, sku: str, start_date: str, exclude_sf: bool = True) -> pd.DataFrame:
    """
    Fetch raw transaction-level data for a SKU.
    """
    sku_clean = sku.strip().upper()
    sf_filter = "AND L.SLUM2 NOT LIKE '%SF%'" if exclude_sf else ""

    query = f"""
    SELECT
        H.SHIDAT AS SALES_DATE,
        COALESCE(L.SLBLUO, 0) AS QTY_SOLD,
        L.SLUM2 AS UOM,
        H.SHCUST AS CUSTOMER,
        H.SHINV# AS INVOICE
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
        ON H.SHCO = L.SLCO
        AND H.SHLOC = L.SLLOC
        AND H.SHORD# = L.SLORD#
        AND H.SHINV# = L.SLINV#
    WHERE TRIM(L.SLITEM) = '{sql_escape(sku_clean)}'
      {sf_filter}
      AND COALESCE(L.SLBLUO, 0) <> 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
    ORDER BY H.SHIDAT
    """

    try:
        df = pd.read_sql_query(query, conn)
        if df.empty:
            return pd.DataFrame()

        # Parse date column
        raw = df['SALES_DATE']
        raw_str = raw.astype(str).str.strip()
        is_yyyymmdd = raw_str.str.fullmatch(r"\d{8}")
        dt = pd.to_datetime(raw_str.where(is_yyyymmdd), format='%Y%m%d', errors='coerce')
        dt = dt.fillna(pd.to_datetime(raw_str, errors='coerce'))
        df['SALES_DATE'] = dt
        df = df.dropna(subset=['SALES_DATE'])

        # Filter by start date
        start_dt = pd.to_datetime(start_date)
        df = df[df['SALES_DATE'] >= start_dt]

        df['SKU'] = sku_clean
        df = df[['SKU', 'SALES_DATE', 'QTY_SOLD', 'UOM', 'CUSTOMER', 'INVOICE']]

        return df

    except Exception as e:
        print(f"  ERROR fetching raw data for {sku_clean}: {e}")
        return pd.DataFrame()


def run_diagnostic(sku_list: List[str], start_date: str = "2024-01-01", exclude_sf: bool = True):
    """
    Run the sales diagnostic for a list of SKUs.
    """
    if not sku_list:
        print("ERROR: No SKUs provided. Edit SKU_LIST at the top of this file.")
        return

    print("=" * 70)
    print("SALES DIAGNOSTIC TOOL")
    print("=" * 70)
    print(f"SKUs to check: {len(sku_list)}")
    print(f"Start date: {start_date}")
    print(f"Exclude SF: {exclude_sf}")
    print()

    # Connect to database
    print("Connecting to database...")
    conn = connect()
    print()

    # Fetch data for each SKU
    print("Fetching monthly sales data...")
    all_monthly = []
    all_raw = []

    for sku in sku_list:
        monthly = fetch_monthly_sales(conn, sku, start_date, exclude_sf)
        if not monthly.empty:
            all_monthly.append(monthly)

        raw = fetch_raw_transactions(conn, sku, start_date, exclude_sf)
        if not raw.empty:
            all_raw.append(raw)

    conn.close()

    if not all_monthly:
        print("\nNo sales data found for any SKUs.")
        return

    # Combine results
    df_monthly = pd.concat(all_monthly, ignore_index=True)
    df_raw = pd.concat(all_raw, ignore_index=True) if all_raw else pd.DataFrame()

    # Create pivot table for easy comparison
    df_pivot = df_monthly.pivot_table(
        index='SKU',
        columns='YearMonth',
        values='Monthly_Total',
        aggfunc='sum',
        fill_value=0
    )

    # Add row totals
    df_pivot['TOTAL'] = df_pivot.sum(axis=1)

    # Save to Excel
    output_dir = Path(__file__).resolve().parent
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"sales_diagnostic_{timestamp}.xlsx"

    print(f"\nSaving results to: {output_file}")

    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        # Sheet 1: Pivot table (SKU x Month)
        df_pivot.to_excel(writer, sheet_name='Monthly_Pivot')

        # Sheet 2: Detailed monthly data
        df_monthly_out = df_monthly.copy()
        df_monthly_out['YearMonth'] = df_monthly_out['YearMonth'].astype(str)
        df_monthly_out.to_excel(writer, sheet_name='Monthly_Detail', index=False)

        # Sheet 3: Raw transactions (if not too large)
        if not df_raw.empty and len(df_raw) <= 50000:
            df_raw.to_excel(writer, sheet_name='Raw_Transactions', index=False)
        elif not df_raw.empty:
            # Too many rows, just save first 50k with a note
            df_raw.head(50000).to_excel(writer, sheet_name='Raw_Transactions', index=False)
            print(f"  Note: Raw transactions truncated to 50,000 rows (total: {len(df_raw)})")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"SKUs processed: {len(sku_list)}")
    print(f"SKUs with data: {df_monthly['SKU'].nunique()}")
    print(f"Total units sold: {df_monthly['Monthly_Total'].sum():,.0f}")
    print(f"Date range: {df_monthly['YearMonth'].min()} to {df_monthly['YearMonth'].max()}")
    print(f"\nOutput saved to: {output_file}")
    print("=" * 70)


if __name__ == "__main__":
    run_diagnostic(SKU_LIST, START_DATE, EXCLUDE_SF)
