"""
DEPRECATED — use sundrieswebapp_refactored.py instead.

This file is retained only because export_forecast_package.py previously depended on
it directly. That dependency has now been removed (export_forecast_package.py imports
sundrieswebapp_refactored). This file should not be run or imported in new code.

Retirement checklist:
  [x] export_forecast_package.py migrated to sundrieswebapp_refactored
  [x] date_diagnostic.py migrated to sundrieswebapp_refactored
  [ ] Delete this file once all downstream callers have been verified

---

Complete Inventory Planning System - SUNDRIES VERSION (LEGACY)
This version is specifically tailored for items that DO NOT have "SF" as their sales unit of measurement.
Designed for sundries buyers to manage non-flooring items.

Modified from OMPforecasting5.py to handle various unit types (EA, BOX, CASE, etc.)

Key Differences from Flooring Version:
- Filters for items where sales_unit_of_measurement != 'SF'
- Uses IMLT field from ITEMMAST table directly for lead times (no Excel file needed)
- Handles multiple unit types (EA, BOX, CASE, ROLL, etc.)
- Separate output files (inventory_plan_sundries_*.xlsx)
- Includes vendor names from VENDMAST table
"""

from __future__ import annotations
import math, os, warnings, sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import pyodbc
from scipy import stats
from statsmodels.tsa.holtwinters import ExponentialSmoothing, SimpleExpSmoothing
from sklearn.neural_network import MLPRegressor
from sklearn.ensemble import RandomForestRegressor
from xgboost import XGBRegressor

# Fix Windows console encoding for unicode characters
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# Suppress all warnings
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", module="sklearn")
warnings.filterwarnings("ignore", module="statsmodels")

# Suppress sklearn parallel and convergence warnings via environment
os.environ['PYTHONWARNINGS'] = 'ignore::UserWarning'
os.environ['LOKY_MAX_CPU_COUNT'] = '4'  # Limit CPU usage for parallel processing
warnings.filterwarnings("ignore")

# ============================================================
# CONFIGURATION - CHANGE THIS TO PROCESS SPECIFIC SKU OR ALL
# ============================================================
FOCUS_SKU = ""  # Leave blank "" to process all SKUs in the Sundries List
USE_GLOBAL_MODEL = True  # Set to True to train a single XGBoost model across ALL SKUs
SUNDRIES_LIST_FILE = "Sundries List.xlsx"  # Excel file with list of sundries SKUs to forecast

# ============================================================
# ACTIVE ITEM FILTERING
# ============================================================
# Items are filtered DURING processing (not upfront)
# Filter: Skip items where last sale > 365 days ago
# This prevents forecasting inactive/dead stock items
# ============================================================


# ============================================================
# GLOBAL ENDING INVENTORY CAP (prevents warehouse capacity issues)
# ============================================================
ENABLE_GLOBAL_INV_CAP = True
GLOBAL_INV_CAP_UNITS = 350000  # Maximum total ending inventory across all sundries SKUs (in UNITS)

# ABC-based Months of Inventory caps (applied before global cap)
ABC_MOI_CAPS = {
    "A": 3.0,  # A items: max 3 months of inventory
    "B": 2.0,  # B items: max 2 months of inventory
    "C": 1.0   # C items: max 1 month of inventory
}
# ============================================================
# ============================================================
# SAFETY STOCK TUNING (ABC-based uplift)
# ============================================================
ABC_BREAK_A = 0.80   # top 80% of cumulative volume
ABC_BREAK_B = 0.90   # next 10% (80% -> 90%)
SS_UPLIFT_SCALAR = {"A": 1.0, "B": 0.35, "C": 0.00}  # Reduced from 1.00/0.35/0.00

# Lumpy buffer uses percentile demand instead of max demand
LUMPY_PCTL_NONZERO = {"A": 0.95, "B": 0.90, "C": 0.85}
LUMPY_LT_BUFFER_FRAC = 0.10  # 10% of percentile demand over lead time

# Optional global clamp: cap total uplift as % of trailing-12-month volume
ENABLE_GLOBAL_UPLIFT_BUDGET = True
GLOBAL_UPLIFT_BUDGET_PCT = 0.02  # 2% of trailing 12m shipped units (reduced from 8%)

# Order-up-to (s,S) coverage horizon beyond lead time, by ABC class (days)
#   A items: 90 days (~3 months) — high movers sell fast, larger orders are low risk
#   B items: 60 days (~2 months) — moderate coverage
#   C items: 30 days (~1 month) — slow movers, keep orders small to limit risk
COVERAGE_HORIZON_DAYS = {"A": 90, "B": 60, "C": 30}
MIN_LEAD_TIME_DAYS = 7  # Floor when IMLT is 0 or missing
# ============================================================

DSN_NAME = "Gartman"
FUTURE_FORECAST_DAYS = 365
FUTURE_FORECAST_WEEKS = 52  # 1 year of weekly forecasts
DAYS_PER_WEEK = 7
DAYS_PER_MONTH = 30.4
WEEKS_PER_MONTH = DAYS_PER_MONTH / DAYS_PER_WEEK
SERVICE_LEVEL = 0.95  # 90% service level
Z_SCORE = stats.norm.ppf(SERVICE_LEVEL)

# Lead times are read directly from IMLT field in ITEMMAST table (no Excel file needed)
CUTOFF_DATE = "2020-06-01"
AS_OF_DATE: Optional[pd.Timestamp] = None
AS_OF_DATE_OVERRIDE = ""  # Optional: "YYYY-MM-DD" to force as-of date

def _get_db_as_of_date(conn) -> pd.Timestamp:
    """Use the DB server date as the 'today' reference to avoid client clock drift."""
    try:
        df = pd.read_sql("SELECT CURRENT DATE AS TODAY FROM SYSIBM.SYSDUMMY1", conn)
        if not df.empty and pd.notna(df.loc[0, "TODAY"]):
            return pd.Timestamp(df.loc[0, "TODAY"]).normalize()
    except Exception as e:
        print(f"  WARNING: Failed to read DB current date, using local clock. Error: {e}")
    return pd.Timestamp.now().normalize()

def _resolve_as_of_date(conn) -> pd.Timestamp:
    """Resolve as-of date using override, otherwise max(local, DB) to handle clock drift."""
    if AS_OF_DATE_OVERRIDE:
        try:
            override = pd.Timestamp(AS_OF_DATE_OVERRIDE).normalize()
            print(f"  Using as-of date override: {override.date()}")
            return override
        except Exception as e:
            print(f"  WARNING: Invalid AS_OF_DATE_OVERRIDE='{AS_OF_DATE_OVERRIDE}', using fallback. Error: {e}")

    db_date = _get_db_as_of_date(conn)
    local_date = pd.Timestamp.now().normalize()
    chosen = max(db_date, local_date)
    print(f"  Local date: {local_date.date()}, DB date: {db_date.date()}")
    print(f"  Using as-of date: {chosen.date()} (max of local/DB)")
    return chosen

def _load_credentials():
    print("  Loading credentials...")
    
    # Check environment variables first
    uid_env = os.getenv("GARTMAN_UID", "").strip()
    pwd_env = os.getenv("GARTMAN_PWD", "").strip()
    
    # Load from secrets file
    secrets_path = Path(__file__).resolve().parent / "OMP_secrets.py"
    uid_file = ""
    pwd_file = ""
    
    if secrets_path.exists():
        # Use a clean namespace to avoid pollution
        namespace = {}
        with open(secrets_path, 'r', encoding='utf-8') as f:
            exec(f.read(), namespace, namespace)
        uid_file = str(namespace.get("GARTMAN_UID", "")).strip()
        pwd_file = str(namespace.get("GARTMAN_PWD", "")).strip()
    
    # Decide which credentials to use
    if uid_env and pwd_env:
        # Validate env vars aren't swapped by comparing to file
        if uid_file and pwd_file:
            if uid_env == pwd_file and pwd_env == uid_file:
                print(f"  ⚠ WARNING: Environment variables appear SWAPPED!")
                print(f"  ⚠ Using credentials from secrets file instead")
                uid, pwd = uid_file, pwd_file
                print(f"  Loaded from secrets file: UID={uid}, PWD={'*' * len(pwd)}")
            else:
                uid, pwd = uid_env, pwd_env
                print(f"  Using environment variables: UID={uid}, PWD={'*' * len(pwd)}")
        else:
            uid, pwd = uid_env, pwd_env
            print(f"  Using environment variables: UID={uid}, PWD={'*' * len(pwd)}")
    elif uid_file and pwd_file:
        uid, pwd = uid_file, pwd_file
        print(f"  Loaded from secrets file: UID={uid}, PWD={'*' * len(pwd)}")
    else:
        raise RuntimeError("Missing credentials")
    
    # Final validation: username and password should NOT be the same
    if uid == pwd:
        raise RuntimeError(f"ERROR: Username and password are identical ('{uid}'). This is incorrect!")
    
    return uid, pwd

def _connect():
    uid, pwd = _load_credentials()
    conn_str = f"DSN={DSN_NAME};UID={uid};PWD={pwd};"
    print(f"  Attempting database connection...")
    try:
        conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
        print(f"  ✓ Database connection successful!")
        return conn
    except pyodbc.Error as e:
        print(f"  ✗ Database connection failed!")
        print(f"  Error: {e}")
        raise

def load_sundries_list(xlsx_path: Path) -> set:
    """
    Load list of sundries SKUs to forecast from Excel file.
    This file is REQUIRED - the program will only forecast SKUs in this list.
    """
    print(f"\nLoading sundries SKU list from {xlsx_path}...")
    
    if not xlsx_path.exists():
        print(f"\n{'='*70}")
        print(f"ERROR: Sundries List file not found!")
        print(f"{'='*70}")
        print(f"  Required file: {xlsx_path}")
        print(f"  This file must contain a list of sundries SKUs to forecast.")
        print(f"  Create an Excel file with SKU numbers in the first column.")
        print(f"{'='*70}")
        raise FileNotFoundError(f"Required file not found: {xlsx_path}")
    
    try:
        df = pd.read_excel(xlsx_path, header=0)
        if df.shape[0] == 0 or df.shape[1] < 1:
            print(f"\n{'='*70}")
            print(f"ERROR: Sundries List file is empty!")
            print(f"{'='*70}")
            raise ValueError(f"File is empty: {xlsx_path}")
        
        sku_col = df.columns[0]
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        
        # Remove any blank/null values
        sku_list = set(df[sku_col].dropna())
        sku_list = {sku for sku in sku_list if sku and sku != 'NAN' and len(sku) > 0}
        
        if len(sku_list) == 0:
            print(f"\n{'='*70}")
            print(f"ERROR: No valid SKUs found in Sundries List!")
            print(f"{'='*70}")
            raise ValueError(f"No valid SKUs found in: {xlsx_path}")
        
        print(f"  ✓ Loaded {len(sku_list)} sundries SKUs from list")
        return sku_list
    except Exception as e:
        if isinstance(e, (FileNotFoundError, ValueError)):
            raise
        print(f"\n{'='*70}")
        print(f"ERROR: Failed to read Sundries List file!")
        print(f"{'='*70}")
        print(f"  File: {xlsx_path}")
        print(f"  Error: {e}")
        print(f"{'='*70}")
        raise
def _sql_escape(value: str) -> str:
    """Escape single quotes for safe SQL string literal usage."""
    return value.replace("'", "''")

def fetch_last_sale_dates_for_skus(
    conn, sku_list: set, chunk_size: int = 800, as_of_date: Optional[pd.Timestamp] = None
) -> Dict[str, pd.Timestamp]:
    """
    Fetch last sale date per SKU in as few queries as possible.
    Returns: {SKU: last_sale_timestamp}
    Notes:
      - Uses SHHEAD/SHLINE, consistent with _fetch_sales_history join.
      - Filters to non-SF lines (sundries).
      - Filters to shipped qty > 0.
    """
    if not sku_list:
        return {}

    skus = sorted({str(s).strip().upper() for s in sku_list if str(s).strip()})
    results: Dict[str, pd.Timestamp] = {}

    # Chunk to avoid overly long IN clauses
    for i in range(0, len(skus), chunk_size):
        chunk = skus[i:i + chunk_size]
        in_list = ",".join([f"'{_sql_escape(s)}'" for s in chunk])

        query = f"""
            SELECT
                TRIM(L.SLITEM) AS SKU,
                MAX(H.SHIDAT)  AS LAST_SALE_YYYYMMDD
            FROM GSFL2K.SHLINE L
            JOIN GSFL2K.SHHEAD H
              ON H.SHCO   = L.SLCO
             AND H.SHLOC  = L.SLLOC
             AND H.SHORD# = L.SLORD#
             AND H.SHINV# = L.SLINV#
            WHERE TRIM(L.SLITEM) IN ({in_list})
              AND L.SLUM2 NOT LIKE '%SF%'
              AND COALESCE(L.SLBLUS, 0) > 0
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
            # Parse per-row to handle DATE, YYYYMMDD ints, or ISO strings
            dt = pd.to_datetime(r["LAST_SALE_YYYYMMDD"], errors="coerce")
            if pd.notna(dt):
                results[sku] = pd.Timestamp(dt)

    return results

def filter_skus_to_active_last_365_days(
    conn, sku_list: set, days: int = 365, as_of_date: Optional[pd.Timestamp] = None
) -> Tuple[set, Dict[str, pd.Timestamp]]:
    """
    Filters the provided SKU list to only SKUs with a sale in the last `days`.
    Returns: (active_sku_set, last_sale_map)
    Any SKU missing from last_sale_map is treated as having no sales and is excluded.
    """
    last_sale_map = fetch_last_sale_dates_for_skus(conn, sku_list, as_of_date=as_of_date)

    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    cutoff = as_of_date - pd.Timedelta(days=days)

    active = {sku for sku, dt in last_sale_map.items() if pd.notna(dt) and dt >= cutoff}
    return active, last_sale_map


def _fetch_sku_master(conn, focus_sku: str = "", cutoff_date_str: str = CUTOFF_DATE, sundries_sku_list: set = None) -> pd.DataFrame:
    """
    Fetch active SKUs with recent sales - MODIFIED FOR SUNDRIES (NON-SF ITEMS)
    Now filters for items where sales_unit_of_measurement != 'SF'
    Uses the actual GSFL2K database schema and tables
    
    REQUIRES: sundries_sku_list must be provided (from Sundries List.xlsx)
    """
    print("\nFetching sundries SKU master...")
    
    if not sundries_sku_list or len(sundries_sku_list) == 0:
        raise ValueError("Sundries SKU list is required but was not provided or is empty")
    
    # Build SKU filter - always uses the sundries list unless a specific SKU is requested
    if focus_sku:
        sku_filter = f"AND TRIM(M.IMITEM) = '{focus_sku.strip().upper()}'"
        print(f"  Filtering for single SKU: {focus_sku}")
    else:
        # Clean and uppercase all SKUs in the list
        sku_list_clean = [s.strip().upper() for s in sundries_sku_list]
        sku_list_str = "'" + "','".join(sku_list_clean) + "'"
        sku_filter = f"AND TRIM(M.IMITEM) IN ({sku_list_str})"
        print(f"  Filtering for {len(sundries_sku_list)} SKUs from Sundries List")
    
    query = f"""
    WITH
    INV AS (
      SELECT B.IBITEM, 
             SUM(B.IBQOH) AS QTY_ON_HAND, 
             SUM(B.IBQOO) AS QTY_COMMITTED
      FROM GSFL2K.ITEMBAL B
      WHERE B.IBLOC NOT IN (90, 17, 41, 46)
      GROUP BY B.IBITEM
    ),
    PO AS (
      SELECT PLITEM, SUM(PLBLUO) AS ON_PO_QTY
      FROM GSFL2K.POLINE
      WHERE PLDELT LIKE '%A%'
      GROUP BY PLITEM
    ),
    BO AS (
      SELECT OLITEM, SUM(OLBLUB) AS BO_QTY
      FROM GSFL2K.OOLINE
      WHERE OLCUST NOT LIKE '%TRANSFER%'
        AND OLCUST NOT LIKE '%OMP000%'
        AND OLCUST NOT LIKE '%INV000%'
        AND OLCUST NOT LIKE '%OLD001%'
        AND OLLOC <> 90
        AND OLBO LIKE 'Y'
      GROUP BY OLITEM
    )
    SELECT
      TRIM(M.IMITEM) AS ITEM_NUMBER,
      TRIM(M.IMDESC) AS DESCRIPTION,
      TRIM(M.IMUM2) AS UNIT_OF_MEASURE,
      TRIM(M.IMVEND) AS VENDOR_NUMBER,
      TRIM(V.VMNAME) AS VENDOR_NAME,
      TRIM(X.IMCOLLECT) AS COLLECTION,
      ((COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0)) * COALESCE(M.IMFACT, 1)) AS AVAILABLE_QTY,
      COALESCE(PO.ON_PO_QTY, 0) AS ON_PO_QTY,
      COALESCE(BO.BO_QTY, 0) AS BACKORDER_QTY,
      COALESCE(M.IMLT, 30) AS LEAD_TIME_IMLT
    FROM GSFL2K.ITEMMAST M
    LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
    LEFT JOIN GSFL2K.VENDMAST V ON V.VMVEND = M.IMVEND
    LEFT JOIN INV ON INV.IBITEM = M.IMITEM
    LEFT JOIN PO ON PO.PLITEM = M.IMITEM
    LEFT JOIN BO ON BO.OLITEM = M.IMITEM
    WHERE M.IMUM2 NOT LIKE '%SF%'
      {sku_filter}
    ORDER BY TRIM(M.IMITEM)
    """
    
    df = pd.read_sql(query, conn)
    
    if df.empty:
        print("  No sundries SKUs found matching criteria")
        return pd.DataFrame()
    
    # Rename columns to match expected format
    df = df.rename(columns={
        'ITEM_NUMBER': 'sku',
        'UNIT_OF_MEASURE': 'uom',
        'VENDOR_NUMBER': 'vendor_number',
        'VENDOR_NAME': 'vendor_name',
        'COLLECTION': 'collection',
        'AVAILABLE_QTY': 'available',
        'ON_PO_QTY': 'on_order',
        'BACKORDER_QTY': 'backorder',
        'LEAD_TIME_IMLT': 'lead_time'
    })
    
    # Calculate Inventory Position = Available + On Order - Backorder
    df['inventory_position'] = df['available'] + df['on_order'] - df['backorder']
    
    # Clean up data types
    df['sku'] = df['sku'].astype(str).str.strip().str.upper()
    df['available'] = pd.to_numeric(df['available'], errors='coerce').fillna(0.0)
    df['on_order'] = pd.to_numeric(df['on_order'], errors='coerce').fillna(0.0)
    df['backorder'] = pd.to_numeric(df['backorder'], errors='coerce').fillna(0.0)
    df['inventory_position'] = pd.to_numeric(df['inventory_position'], errors='coerce').fillna(0.0)
    df['lead_time'] = pd.to_numeric(df['lead_time'], errors='coerce').fillna(30.0)
    
    # Show UOM distribution
    if 'uom' in df.columns:
        print(f"\n  Unit of Measurement Distribution:")
        uom_counts = df['uom'].value_counts()
        for uom, count in uom_counts.items():
            pct = 100 * count / len(df)
            print(f"    {uom}: {count} SKUs ({pct:.1f}%)")
    
    print(f"  Loaded {len(df)} sundries SKUs from master data")
    return df

def _fetch_sales_history(conn, sku: str, cutoff_date_str: str = CUTOFF_DATE) -> pd.DataFrame:
    """Fetch sales history - SIMPLIFIED: Get all sales, filter dates in Python"""
    
    # SIMPLE: Just get ALL sales for this item
    sku_clean = sku.strip().upper()
    query = f"""
    SELECT 
        H.SHIDAT AS SALES_DATE, 
        COALESCE(L.SLBLUS, 0) AS QTY_SOLD
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H 
        ON H.SHCO = L.SLCO 
        AND H.SHLOC = L.SLLOC 
        AND H.SHORD# = L.SLORD#
        AND H.SHINV# = L.SLINV#
    WHERE TRIM(L.SLITEM) = '{_sql_escape(sku_clean)}'
        AND L.SLUM2 NOT LIKE '%SF%'
        AND COALESCE(L.SLBLUS, 0) > 0
        AND H.SHCUST NOT LIKE '%TRANSFER%'
        AND H.SHCUST NOT LIKE '%OMP000%'
        AND H.SHCUST NOT LIKE '%INV000%'
        AND H.SHCUST NOT LIKE '%OLD001%'
    """
    
    try:
        df = pd.read_sql_query(query, conn)
        if df.empty:
            return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])
        
        # Convert SALES_DATE (handles YYYYMMDD ints or DATE strings)
        raw = df['SALES_DATE']
        raw_str = raw.astype(str).str.strip()
        is_yyyymmdd = raw_str.str.fullmatch(r"\d{8}")
        dt = pd.to_datetime(raw_str.where(is_yyyymmdd), format='%Y%m%d', errors='coerce')
        dt = dt.fillna(pd.to_datetime(raw_str, errors='coerce'))
        df['SALES_DATE'] = dt
        df = df.dropna(subset=['SALES_DATE'])
        
        if df.empty:
            return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])
        
        # Filter by cutoff date in Python
        cutoff_dt = pd.to_datetime(cutoff_date_str)
        df = df[df['SALES_DATE'] >= cutoff_dt]
        
        # Aggregate by date
        df = df.groupby('SALES_DATE', as_index=False).agg({'QTY_SOLD': 'sum'})
        df = df.rename(columns={'SALES_DATE': 'transaction_date', 'QTY_SOLD': 'quantity_shipped'})
        df['uom'] = 'UNITS'
        
        return df.sort_values('transaction_date')
    except Exception as e:
        print(f"ERROR fetching sales for {sku}: {e}")
        return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])

def compute_abc_classification(df_master: pd.DataFrame, shipped_12m_map: Dict[str, float]) -> Dict[str, str]:
    """ABC classification based on 12-month shipped volume"""
    print("\nComputing ABC classification...")
    
    if df_master.empty:
        return {}
    
    volumes = []
    for sku in df_master['sku']:
        vol = shipped_12m_map.get(sku, 0.0)
        volumes.append((sku, vol))
    
    volumes.sort(key=lambda x: x[1], reverse=True)
    total_volume = sum(v for _, v in volumes)
    
    if total_volume == 0:
        return {sku: 'C' for sku, _ in volumes}
    
    cumulative = 0.0
    abc_map = {}
    for sku, vol in volumes:
        cumulative += vol
        pct = cumulative / total_volume
        if pct <= ABC_BREAK_A:
            abc_map[sku] = 'A'
        elif pct <= ABC_BREAK_B:
            abc_map[sku] = 'B'
        else:
            abc_map[sku] = 'C'
    
    a_count = sum(1 for c in abc_map.values() if c == 'A')
    b_count = sum(1 for c in abc_map.values() if c == 'B')
    c_count = sum(1 for c in abc_map.values() if c == 'C')
    
    print(f"  A: {a_count} SKUs")
    print(f"  B: {b_count} SKUs")
    print(f"  C: {c_count} SKUs")
    
    return abc_map

def load_trailing_12m_volume_sundries(
    conn, sku_list: List[str], as_of_date: Optional[pd.Timestamp] = None
) -> Tuple[Dict[str, float], float]:
    """
    Returns {SKU: VOL_12M} and total volume for non-SF items over trailing 12 months.
    """
    print("\nLoading trailing 12-month volume for ABC classification...")

    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    start_date = (as_of_date - pd.Timedelta(days=365)).date()
    end_date = as_of_date.date()

    sku_filter = ""
    params: List = [start_date, end_date]

    if sku_list:
        placeholders = ",".join(["?"] * len(sku_list))
        sku_filter = f" AND TRIM(L.SLITEM) IN ({placeholders}) "
        params.extend([s for s in sku_list])

    sql = f"""
    SELECT
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        SUM(COALESCE(L.SLBLUS,0)) AS VOL_12M
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE L.SLUM2 NOT LIKE '%SF%'
      AND H.SHIDAT >= ?
      AND H.SHIDAT <= ?
      {sku_filter}
    GROUP BY TRIM(L.SLITEM)
    """

    df = pd.read_sql_query(sql, conn, params=params)
    df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    df["VOL_12M"] = pd.to_numeric(df["VOL_12M"], errors="coerce").fillna(0.0)

    vol_map = dict(zip(df["ITEM_NUMBER"], df["VOL_12M"]))
    total_vol = float(df["VOL_12M"].sum()) if not df.empty else 0.0

    print(f"  Loaded volume data for {len(df)} SKUs")
    return vol_map, total_vol

def aggregate_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily data to weekly buckets, filling zero-demand weeks.

    CRITICAL: The full weekly index (first sale to last sale) must include
    zero-demand weeks.  Without them ADI is always ~1.0 and every SKU gets
    mis-classified as SMOOTH, causing hugely inflated forecasts for sparse items.
    """
    if df.empty:
        return df

    df = df.copy()
    df['week'] = df['transaction_date'].dt.to_period('W').dt.to_timestamp()

    weekly = df.groupby('week').agg({
        'quantity_shipped': 'sum'
    }).reset_index()
    weekly.columns = ['week', 'quantity']

    # Reindex to include ALL weeks between first and last sale (fill gaps with 0)
    if len(weekly) >= 2:
        full_weeks = pd.date_range(
            start=weekly['week'].min(),
            end=weekly['week'].max(),
            freq='W-MON',
        )
        weekly = (
            weekly.set_index('week')
            .reindex(full_weeks, fill_value=0.0)
            .rename_axis('week')
            .reset_index()
        )

    return weekly

def classify_demand_pattern(df_weekly: pd.DataFrame) -> Tuple[str, float, float]:
    """
    Classify demand pattern based on ADI and CV²
    
    Returns: (demand_class, adi, cv2)
    
    Classification:
    - SMOOTH: ADI < 1.32 and CV² < 0.49
    - INTERMITTENT: ADI >= 1.32 and CV² < 0.49
    - ERRATIC: ADI < 1.32 and CV² >= 0.49
    - LUMPY: ADI >= 1.32 and CV² >= 0.49
    """
    if df_weekly.empty or len(df_weekly) < 4:
        return 'UNKNOWN', 0.0, 0.0
    
    demand = df_weekly['quantity'].values
    
    # ADI (Average Demand Interval)
    nonzero_demand = demand[demand > 0]
    if len(nonzero_demand) == 0:
        return 'NO_DEMAND', 0.0, 0.0
    
    # Find intervals between non-zero demands
    nonzero_indices = np.where(demand > 0)[0]
    if len(nonzero_indices) <= 1:
        adi = len(demand)  # All zeros except one
    else:
        intervals = np.diff(nonzero_indices)
        adi = float(np.mean(intervals))
    
    # CV² (Coefficient of Variation squared)
    if len(nonzero_demand) < 2:
        cv2 = 0.0
    else:
        mean_demand = float(np.mean(nonzero_demand))
        std_demand = float(np.std(nonzero_demand, ddof=1))
        if mean_demand > 0:
            cv2 = (std_demand / mean_demand) ** 2
        else:
            cv2 = 0.0
    
    # Classification thresholds
    ADI_THRESHOLD = 1.32
    CV2_THRESHOLD = 0.49
    
    if adi < ADI_THRESHOLD and cv2 < CV2_THRESHOLD:
        demand_class = 'SMOOTH'
    elif adi >= ADI_THRESHOLD and cv2 < CV2_THRESHOLD:
        demand_class = 'INTERMITTENT'
    elif adi < ADI_THRESHOLD and cv2 >= CV2_THRESHOLD:
        demand_class = 'ERRATIC'
    else:  # adi >= ADI_THRESHOLD and cv2 >= CV2_THRESHOLD
        demand_class = 'LUMPY'
    
    return demand_class, adi, cv2

def safe_exponential_smoothing(y_train, seasonal_periods=None, trend=None, seasonal=None, damped_trend=False, max_attempts=3):
    """Try exponential smoothing with fallback options - improved for sparse data"""
    
    # Check if data has enough variation
    if len(y_train) < 3:
        return None
    
    # For very sparse data (mostly zeros), exponential smoothing may fail
    nonzero_count = np.sum(y_train > 0)
    if nonzero_count < 2:
        return None  # Not enough data for exponential smoothing
    
    for attempt in range(max_attempts):
        try:
            if attempt == 0:
                # First attempt: use requested parameters
                model = ExponentialSmoothing(
                    y_train,
                    seasonal_periods=seasonal_periods,
                    trend=trend,
                    seasonal=seasonal,
                    damped_trend=damped_trend,
                    initialization_method="estimated"
                )
            elif attempt == 1:
                # Second attempt: simplify - remove seasonality, keep damping if requested
                model = ExponentialSmoothing(
                    y_train,
                    trend=trend,
                    seasonal=None,
                    damped_trend=damped_trend,
                    initialization_method="heuristic"
                )
            else:
                # Third attempt: simplest - Simple Exponential Smoothing (no trend, no damping)
                model = SimpleExpSmoothing(y_train, initialization_method="heuristic")
            
            # Fit with different optimization options
            if attempt == 0:
                fit = model.fit(optimized=True, use_brute=False)
            else:
                # Use less aggressive optimization for fallback attempts
                fit = model.fit(optimized=False)
            
            return fit
        except Exception as e:
            if attempt == max_attempts - 1:
                # All attempts failed
                return None
            continue
    
    return None

def wmape(y_true, y_pred):
    """
    Calculate weighted Mean Absolute Percentage Error (wMAPE)
    This is the correct metric for intermittent demand forecasting.
    Unlike MAPE, it doesn't give artificially low scores to zero-forecasts.

    Formula: sum(|actual - forecast|) / sum(|actual|) * 100
    """
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        # If all actuals are zero, return 0 if forecast is also all zeros, else return a large penalty
        return 0.0 if np.sum(np.abs(y_pred)) == 0 else 1000.0
    return float(np.sum(np.abs(y_true - y_pred)) / denom) * 100

def forecast_holt_damped(y_train, y_val, h_future):
    """Holt's damped trend - prevents exponential growth by damping the trend"""
    try:
        # Use damped_trend=True to get actual damped exponential smoothing
        fit = safe_exponential_smoothing(y_train, trend='add', seasonal=None, damped_trend=True)
        if fit is None:
            return None, None, None

        # Forecast total steps needed (validation + future)
        total_steps = len(y_val) + h_future
        fc_all = fit.forecast(total_steps)

        # Split into validation and future
        fc_val = fc_all[:len(y_val)]
        fc_future = fc_all[len(y_val):]

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except Exception as e:
        return None, None, None

def forecast_croston(y_train, y_val, h_future, alpha=0.1):
    """Croston's method for intermittent demand"""
    try:
        demand = y_train.copy()
        nonzero_indices = np.where(demand > 0)[0]

        if len(nonzero_indices) < 2:
            return None, None, None

        # Initialize
        z = demand[nonzero_indices[0]]  # Demand size
        p = nonzero_indices[0] + 1      # Interval

        # Update estimates
        for i in range(1, len(nonzero_indices)):
            idx = nonzero_indices[i]
            z = alpha * demand[idx] + (1 - alpha) * z
            interval = idx - nonzero_indices[i-1]
            p = alpha * interval + (1 - alpha) * p

        forecast_value = z / p if p > 0 else 0

        fc_val = np.full(len(y_val), forecast_value)
        fc_future = np.full(h_future, forecast_value)

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except:
        return None, None, None

def forecast_tsb(y_train, y_val, h_future, alpha=0.1, beta=0.1):
    """
    Teunter-Syntetos-Babai (TSB) method for intermittent demand
    CORRECTED VERSION - Fixed probability tracking and size estimation
    """
    try:
        demand = y_train.copy()
        n = len(demand)
        
        if n < 2:
            return None, None, None
        
        # Initialize with first observation
        if demand[0] > 0:
            z_t = demand[0]  # demand size
            p_t = 1.0        # probability of demand
        else:
            # Find first non-zero demand
            first_nonzero = np.where(demand > 0)[0]
            if len(first_nonzero) == 0:
                return None, None, None
            z_t = demand[first_nonzero[0]]
            p_t = 0.5  # neutral starting probability
        
        # Track estimates over time
        z_estimates = [z_t]
        p_estimates = [p_t]
        
        # Update estimates for each period
        for t in range(1, n):
            if demand[t] > 0:
                # Demand occurred
                z_t = alpha * demand[t] + (1 - alpha) * z_t
                p_t = alpha * 1.0 + (1 - alpha) * p_t
            else:
                # No demand
                p_t = alpha * 0.0 + (1 - alpha) * p_t
                # z_t stays the same
            
            z_estimates.append(z_t)
            p_estimates.append(p_t)
        
        # Final forecast
        z_final = z_estimates[-1]
        p_final = p_estimates[-1]
        
        # Prevent unrealistic forecasts
        if p_final > 0.95:
            p_final = 0.95
        if p_final < 0.05:
            p_final = 0.05
        
        forecast_value = z_final * p_final
        
        # Additional validation: check if forecast is reasonable
        mean_nonzero = np.mean(demand[demand > 0]) if np.any(demand > 0) else 0
        mean_all = np.mean(demand)
        
        # If forecast is more than 3x the mean or less than 0.1x the mean, fall back to simple average
        if mean_all > 0 and (forecast_value > 3 * mean_all or forecast_value < 0.1 * mean_all):
            forecast_value = mean_all
        
        fc_val = np.full(len(y_val), forecast_value)
        fc_future = np.full(h_future, forecast_value)

        error = wmape(y_val, fc_val)

        # If wMAPE is absurdly high, reject this method
        if error > 200:
            return None, None, None

        return fc_val, fc_future, error
    except:
        return None, None, None

def forecast_sba(y_train, y_val, h_future, alpha=0.1):
    """Syntetos-Boylan Approximation"""
    try:
        demand = y_train.copy()
        nonzero_demand = demand[demand > 0]
        
        if len(nonzero_demand) < 2:
            return None, None, None
        
        # Croston estimates
        z = np.mean(nonzero_demand)
        nonzero_indices = np.where(demand > 0)[0]
        if len(nonzero_indices) < 2:
            return None, None, None
        
        intervals = np.diff(nonzero_indices)
        p = np.mean(intervals) if len(intervals) > 0 else 1
        
        # SBA correction
        cv2 = (np.std(intervals) / p) ** 2 if p > 0 else 0
        forecast_value = (z / p) * (1 - alpha / 2) if p > 0 else 0
        
        fc_val = np.full(len(y_val), forecast_value)
        fc_future = np.full(h_future, forecast_value)

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except:
        return None, None, None

def _feature_row(time_idx: int, buffer: List[float]) -> List[float]:
    lags = [0.0, 0.0, 0.0, 0.0]
    for i in range(1, 5):
        if len(buffer) >= i:
            lags[i - 1] = float(buffer[-i])
    recent = buffer[-4:] if buffer else []
    rolling_mean = float(np.mean(recent)) if recent else 0.0
    rolling_std = float(np.std(recent)) if recent else 0.0
    return [time_idx, lags[0], lags[1], lags[2], lags[3], rolling_mean, rolling_std]


def _recursive_forecast(model, future_state: Dict[str, Any], h_future: int) -> np.ndarray:
    buffer = list(future_state.get("last_values", []))
    last_idx = int(future_state.get("last_idx", 0))
    preds: List[float] = []

    for i in range(h_future):
        time_idx = last_idx + i + 1
        X_row = np.array([_feature_row(time_idx, buffer)])
        pred = float(model.predict(X_row)[0])
        pred = max(pred, 0.0)
        preds.append(pred)
        buffer.append(pred)

    return np.array(preds)


def forecast_hurdle(X_train, y_train, X_val, y_val, future_state, h_future):
    """Two-part hurdle model: logistic for occurrence, regressor for size"""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.ensemble import RandomForestRegressor
        
        # Part 1: Logistic regression for demand occurrence
        y_occur_train = (y_train > 0).astype(int)
        y_occur_val = (y_val > 0).astype(int)
        
        log_model = LogisticRegression(max_iter=1000, random_state=42)
        log_model.fit(X_train, y_occur_train)
        
        p_occur_val = log_model.predict_proba(X_val)[:, 1]
        buffer = list(future_state.get("last_values", []))
        last_idx = int(future_state.get("last_idx", 0))
        fc_future = []
        for i in range(h_future):
            time_idx = last_idx + i + 1
            X_row = np.array([_feature_row(time_idx, buffer)])
            p_occur = float(log_model.predict_proba(X_row)[:, 1][0])
            size = float(rf_model.predict(X_row)[0])
            size = max(size, 0.0)
            fc = max(p_occur * size, 0.0)
            fc_future.append(fc)
            buffer.append(fc)
        fc_future = np.array(fc_future)
        
        # Part 2: Random Forest for demand size (when demand > 0)
        nonzero_mask = y_train > 0
        if nonzero_mask.sum() < 5:
            return None, None, None
        
        X_train_nonzero = X_train[nonzero_mask]
        y_train_nonzero = y_train[nonzero_mask]
        
        rf_model = RandomForestRegressor(n_estimators=50, max_depth=5, random_state=42, n_jobs=1)
        rf_model.fit(X_train_nonzero, y_train_nonzero)
        
        size_val = rf_model.predict(X_val)
        # Combine: E[Y] = P(Y > 0) * E[Y | Y > 0]
        fc_val = p_occur_val * size_val

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except:
        return None, None, None

def forecast_neural_network(X_train, y_train, X_val, y_val, future_state, h_future):
    """Neural network forecast"""
    try:
        model = MLPRegressor(
            hidden_layer_sizes=(50, 25),
            activation='relu',
            solver='adam',
            max_iter=500,
            random_state=42,
            early_stopping=True,
            validation_fraction=0.1
        )
        model.fit(X_train, y_train)

        fc_val = model.predict(X_val)
        fc_future = _recursive_forecast(model, future_state, h_future)

        fc_val = np.maximum(fc_val, 0)
        fc_future = np.maximum(fc_future, 0)

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except:
        return None, None, None

def forecast_random_forest(X_train, y_train, X_val, y_val, future_state, h_future):
    """Random forest forecast"""
    try:
        model = RandomForestRegressor(
            n_estimators=100,
            max_depth=10,
            min_samples_split=5,
            random_state=42,
            n_jobs=1
        )
        model.fit(X_train, y_train)

        fc_val = model.predict(X_val)
        fc_future = _recursive_forecast(model, future_state, h_future)

        fc_val = np.maximum(fc_val, 0)
        fc_future = np.maximum(fc_future, 0)

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except:
        return None, None, None

def forecast_xgboost(X_train, y_train, X_val, y_val, future_state, h_future):
    """XGBoost forecast"""
    try:
        model = XGBRegressor(
            n_estimators=100,
            max_depth=6,
            learning_rate=0.1,
            random_state=42,
            n_jobs=1,
            verbosity=0
        )
        model.fit(X_train, y_train, verbose=False)

        fc_val = model.predict(X_val)
        fc_future = _recursive_forecast(model, future_state, h_future)

        fc_val = np.maximum(fc_val, 0)
        fc_future = np.maximum(fc_future, 0)

        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except:
        return None, None, None

def build_features(df_weekly: pd.DataFrame, h_future: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, Dict[str, Any], np.ndarray]:
    """Build time-series features for ML models"""
    df = df_weekly.copy()
    df = df.sort_values('week').reset_index(drop=True)
    
    # Create features
    df['time_idx'] = range(len(df))
    df['lag_1'] = df['quantity'].shift(1)
    df['lag_2'] = df['quantity'].shift(2)
    df['lag_3'] = df['quantity'].shift(3)
    df['lag_4'] = df['quantity'].shift(4)
    df['rolling_mean_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).mean()
    df['rolling_std_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).std()
    
    df = df.fillna(0)
    
    feature_cols = ['time_idx', 'lag_1', 'lag_2', 'lag_3', 'lag_4', 'rolling_mean_4', 'rolling_std_4']
    
    # Train/val split (70/30)
    split_idx = int(len(df) * 0.7)
    if split_idx < 4:
        split_idx = max(4, len(df) - 4)
    
    train_df = df.iloc[:split_idx]
    val_df = df.iloc[split_idx:]
    
    X_train = train_df[feature_cols].values
    y_train = train_df['quantity'].values
    X_val = val_df[feature_cols].values
    y_val = val_df['quantity'].values
    
    # Future state for recursive forecasting
    last_idx = int(df['time_idx'].iloc[-1])
    last_values = df['quantity'].iloc[-4:].tolist()
    future_state = {
        "last_idx": last_idx,
        "last_values": last_values,
    }

    return X_train, y_train, X_val, y_val, future_state, val_df['quantity'].values

def validate_forecast_reasonableness(fc_future: np.ndarray, y_train: np.ndarray, method_name: str) -> Tuple[bool, str]:
    """
    Validate that a forecast is reasonable compared to historical data.
    Returns (is_valid, reason_for_rejection)

    Rejects forecasts that are:
    - More than 10x the historical maximum
    - More than 20x the historical mean (for non-zero periods)
    - Contain values > 100,000 (absolute sanity check for sundries)
    - Predict meaningful demand when there's been no sales in last 6 months
    - Show exponential growth patterns (compounding effect)
    """
    if fc_future is None or len(fc_future) == 0:
        return False, "No forecast produced"

    # Ensure fc_future and y_train are numpy arrays with proper indexing
    fc_future = np.asarray(fc_future)
    y_train = np.asarray(y_train)

    # Get historical statistics
    historical_max = float(np.max(y_train))
    historical_mean = float(np.mean(y_train))
    nonzero_data = y_train[y_train > 0]
    historical_nonzero_mean = float(np.mean(nonzero_data)) if len(nonzero_data) > 0 else 0
    
    # Check if there's been any recent sales (last 6 months = ~26 weeks)
    recent_weeks = min(26, len(y_train) // 2)
    recent_sales = y_train[-recent_weeks:]
    recent_total = float(np.sum(recent_sales))
    recent_max = float(np.max(recent_sales))
    
    # Get forecast statistics
    forecast_max = float(np.max(fc_future))
    forecast_mean = float(np.mean(fc_future))
    forecast_min = float(np.min(fc_future))
    
    # RULE 1: Absolute insanity check - no sundries item should forecast > 100,000
    if forecast_max > 100_000:
        return False, f"Forecast exceeds 100K ({forecast_max:,.0f}) - clearly unrealistic for sundries"
    
    # RULE 2: No sales in recent history (last 6 months) - should forecast near zero
    if recent_total == 0:
        if forecast_mean > 0.5:  # Allow rounding error, but not meaningful forecasts
            return False, f"No sales in last {recent_weeks} weeks but forecasting {forecast_mean:.1f} per week"
    
    # RULE 3: Very minimal recent sales - should forecast conservatively
    if recent_total > 0 and recent_total < 5:
        # Less than 5 units sold in 6 months
        if forecast_mean > 2:
            return False, f"Only {recent_total:.0f} units in last {recent_weeks} weeks but forecasting {forecast_mean:.1f} per week"
    
    # RULE 4: Forecast is more than 10x historical maximum
    if historical_max > 0 and forecast_max > (historical_max * 10):
        return False, f"Forecast max ({forecast_max:.0f}) > 10x historical max ({historical_max:.0f})"
    
    # RULE 5: Forecast mean is more than 10x historical mean (stricter for sundries)
    if historical_mean > 0 and forecast_mean > (historical_mean * 10):
        return False, f"Forecast mean ({forecast_mean:.1f}) > 10x historical mean ({historical_mean:.1f})"
    
    # RULE 6: For slow-moving items, be extra conservative
    if historical_nonzero_mean > 0 and historical_nonzero_mean < 10:
        # For slow movers, forecast shouldn't exceed 3x the non-zero mean
        if forecast_mean > (historical_nonzero_mean * 3):
            return False, f"Slow mover: forecast ({forecast_mean:.1f}) > 3x non-zero mean ({historical_nonzero_mean:.1f})"
    
    # RULE 7: Check for exponential growth pattern (the 3 trillion scenario)
    # Compare first quarter to last quarter of forecast
    if len(fc_future) >= 12:
        q1 = fc_future[:len(fc_future)//4]
        q4 = fc_future[-len(fc_future)//4:]
        q1_mean = np.mean(q1)
        q4_mean = np.mean(q4)

        if q1_mean > 0 and q4_mean > (q1_mean * 3):
            return False, f"Exponential growth: {q1_mean:.1f} → {q4_mean:.1f} (3x+ increase)"

        # Check for sudden jumps - adaptive based on historical volatility
        # Calculate historical CV (coefficient of variation)
        historical_cv = np.std(y_train) / np.mean(y_train) if np.mean(y_train) > 0 else 0

        # For high-volatility items (CV > 1), allow larger jumps
        # For erratic/lumpy demand, week-to-week doubling is normal
        max_jump_ratio = 3.0 if historical_cv > 1.0 else 2.0

        for i in range(1, len(fc_future)):
            if fc_future[i-1] > 0 and fc_future[i] > (fc_future[i-1] * max_jump_ratio):
                return False, f"Sudden jump at period {i}: {fc_future[i-1]:.1f} → {fc_future[i]:.1f} (max allowed: {max_jump_ratio}x for CV={historical_cv:.2f})"
    
    # RULE 8: Negative forecasts are invalid
    if forecast_min < 0:
        return False, f"Negative forecast detected: {forecast_min:.1f}"
    
    # RULE 9: For items with recent sales, forecast shouldn't be > 5x recent max
    if recent_max > 0 and forecast_max > (recent_max * 5):
        return False, f"Forecast max ({forecast_max:.0f}) > 5x recent max ({recent_max:.0f})"
    
    return True, "Valid"

def select_best_forecast(methods_dict: Dict, y_train_series: pd.Series, demand_class: str) -> Tuple[str, np.ndarray, float]:
    """
    Select the best forecasting method based on validation wMAPE
    NOW WITH VALIDATION: Rejects methods that produce unrealistic forecasts

    Returns: (method_name, forecast_future, validation_wmape)
    """
    y_train = y_train_series.values
    valid_methods = {}
    rejected_methods = {}

    for method_name, (fc_val, fc_future, error_metric) in methods_dict.items():
        if fc_val is None or fc_future is None or error_metric is None:
            rejected_methods[method_name] = "Failed to produce forecast"
            continue

        # Skip methods with absurdly high wMAPE
        if error_metric >= 200:
            rejected_methods[method_name] = f"wMAPE too high ({error_metric:.0f}%)"
            continue

        # Warn about methods with poor but acceptable wMAPE
        if error_metric >= 100:
            print(f"    ⚠ {method_name} has high wMAPE ({error_metric:.1f}%) - forecast quality may be poor")

        # VALIDATE REASONABLENESS
        is_valid, reason = validate_forecast_reasonableness(fc_future, y_train, method_name)
        if not is_valid:
            rejected_methods[method_name] = reason
            continue

        # Passed all checks
        valid_methods[method_name] = (fc_val, fc_future, error_metric)
    
    # Print rejection summary if any methods were rejected
    if rejected_methods:
        print(f"    Rejected methods:")
        for method, reason in rejected_methods.items():
            print(f"      {method}: {reason}")
    
    if not valid_methods:
        # ALL methods failed validation - use safe fallback
        print(f"    ⚠ ALL methods rejected - using SAFE FALLBACK")
        
        # Check recent sales (last 6 months)
        recent_weeks = min(26, len(y_train) // 2)
        recent_sales = y_train[-recent_weeks:]
        recent_total = float(np.sum(recent_sales))
        
        h_future = FUTURE_FORECAST_WEEKS
        
        # For items with no recent sales, forecast zero
        if recent_total == 0:
            print(f"    → No sales in last {recent_weeks} weeks, forecasting ZERO")
            fc_future = np.zeros(h_future)
            return 'ZERO_FORECAST', fc_future, 0.0
        
        # For items with minimal recent sales, use recent average (very conservative)
        if recent_total < 10:
            recent_avg = recent_total / recent_weeks
            print(f"    → Minimal recent sales ({recent_total:.0f} in {recent_weeks} weeks)")
            print(f"    → Using recent average: {recent_avg:.3f} per week")
            fc_future = np.full(h_future, recent_avg)
            return 'RECENT_AVG', fc_future, 0.0
        
        # Otherwise use historical mean, but capped at historical max
        mean_demand = float(np.mean(y_train))
        historical_max = float(np.max(y_train))
        
        # Cap mean at historical max to prevent unrealistic forecasts
        safe_forecast = min(mean_demand, historical_max)
        
        print(f"    → Using capped mean: {safe_forecast:.2f} (mean={mean_demand:.2f}, max={historical_max:.2f})")
        fc_future = np.full(h_future, safe_forecast)
        return 'SAFE_MEAN', fc_future, 0.0
    
    # Select method with business logic adjustments for inventory planning
    # Pure wMAPE can favor under-forecasting, which is dangerous for inventory
    historical_mean = float(np.mean(y_train))
    methods_with_scores = []

    for method_name, (fc_val, fc_future, wmape_score) in valid_methods.items():
        forecast_mean = float(np.mean(fc_future))
        forecast_bias = forecast_mean / historical_mean if historical_mean > 0 else 1.0

        # Apply business logic penalties
        # Under-forecasting leads to stockouts (very bad)
        # Over-forecasting leads to excess inventory (costly but safer)
        if forecast_bias < 0.5:  # Forecasting less than 50% of historical
            adjusted_score = wmape_score * 1.5  # 50% penalty
            bias_note = "severe under-forecast"
        elif forecast_bias < 0.8:  # Forecasting 50-80% of historical
            adjusted_score = wmape_score * 1.2  # 20% penalty
            bias_note = "under-forecast"
        elif forecast_bias > 2.0:  # Forecasting more than 200% of historical
            adjusted_score = wmape_score * 1.1  # 10% penalty
            bias_note = "over-forecast"
        else:
            adjusted_score = wmape_score
            bias_note = "reasonable"

        methods_with_scores.append({
            'name': method_name,
            'fc_val': fc_val,
            'fc_future': fc_future,
            'wmape': wmape_score,
            'adjusted_score': adjusted_score,
            'bias': forecast_bias,
            'bias_note': bias_note
        })

    # Select method with best adjusted score
    best_method = min(methods_with_scores, key=lambda x: x['adjusted_score'])

    print(f"    Valid methods: {len(valid_methods)}/{len(methods_dict)}")
    print(f"    Best method: {best_method['name']} (Score={best_method['adjusted_score']:.1f}, wMAPE={best_method['wmape']:.1f}%)")

    return best_method['name'], best_method['fc_future'], best_method['wmape']

def build_global_training_data(conn, sku_master: pd.DataFrame, cutoff_date: pd.Timestamp) -> Tuple[pd.DataFrame, Dict]:
    """Build training dataset across all SKUs for global XGBoost model"""
    print("\nBuilding global training dataset...")
    
    all_rows = []
    sku_encodings = {}
    
    for idx, (sku_idx, row) in enumerate(sku_master.iterrows()):
        sku = row['sku']
        sku_encodings[sku] = idx
        
        try:
            df_sales = _fetch_sales_history(conn, sku, cutoff_date.strftime('%Y-%m-%d'))
            if df_sales.empty or len(df_sales) < 8:
                continue
            
            df_weekly = aggregate_to_weekly(df_sales)
            if len(df_weekly) < 8:
                continue
            
            # Build features
            df = df_weekly.copy()
            df = df.sort_values('week').reset_index(drop=True)
            df['time_idx'] = range(len(df))
            df['lag_1'] = df['quantity'].shift(1)
            df['lag_2'] = df['quantity'].shift(2)
            df['lag_3'] = df['quantity'].shift(3)
            df['lag_4'] = df['quantity'].shift(4)
            df['rolling_mean_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).mean()
            df['rolling_std_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).std()
            df['sku_encoded'] = idx
            
            df = df.dropna()
            
            if len(df) < 4:
                continue
            
            all_rows.append(df)
        except:
            continue
    
    if not all_rows:
        return pd.DataFrame(), sku_encodings
    
    df_global = pd.concat(all_rows, ignore_index=True)
    print(f"  Built global dataset: {len(df_global)} rows from {len(all_rows)} SKUs")
    
    return df_global, sku_encodings

def train_global_xgboost(df_global: pd.DataFrame) -> Tuple[Optional[XGBRegressor], List[str]]:
    """Train a single XGBoost model across all SKUs"""
    print("\nTraining global XGBoost model...")
    
    try:
        feature_cols = ['time_idx', 'lag_1', 'lag_2', 'lag_3', 'lag_4', 
                       'rolling_mean_4', 'rolling_std_4', 'sku_encoded']
        
        X = df_global[feature_cols].values
        y = df_global['quantity'].values
        
        model = XGBRegressor(
            n_estimators=200,
            max_depth=8,
            learning_rate=0.05,
            random_state=42,
            n_jobs=4,
            verbosity=0
        )
        
        model.fit(X, y, verbose=False)
        
        print(f"  ✓ Global model trained on {len(X)} samples")
        return model, feature_cols
    except Exception as e:
        print(f"  ✗ Global model training failed: {e}")
        return None, []

def forecast_with_global_model(global_model, feature_cols: List[str], sku_encoding: int,
                               df_weekly: pd.DataFrame, h_val: int, h_future: int):
    """Use global model to forecast a specific SKU"""
    try:
        df = df_weekly.copy()
        df = df.sort_values('week').reset_index(drop=True)
        
        df['time_idx'] = range(len(df))
        df['lag_1'] = df['quantity'].shift(1)
        df['lag_2'] = df['quantity'].shift(2)
        df['lag_3'] = df['quantity'].shift(3)
        df['lag_4'] = df['quantity'].shift(4)
        df['rolling_mean_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).mean()
        df['rolling_std_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).std()
        df['sku_encoded'] = sku_encoding
        
        df = df.fillna(0)
        
        # Validation split
        split_idx = len(df) - h_val
        if split_idx < 4:
            return None, None, None
        
        X_val = df.iloc[split_idx:][feature_cols].values
        y_val = df.iloc[split_idx:]['quantity'].values
        
        fc_val = global_model.predict(X_val)
        fc_val = np.maximum(fc_val, 0)
        
        # Future forecast
        last_row = df.iloc[-1]
        X_future = []
        for i in range(h_future):
            future_row = [
                last_row['time_idx'] + i + 1,
                last_row['lag_1'],
                last_row['lag_2'],
                last_row['lag_3'],
                last_row['lag_4'],
                last_row['rolling_mean_4'],
                last_row['rolling_std_4'],
                sku_encoding
            ]
            X_future.append(future_row)
        
        X_future = np.array(X_future)
        fc_future = global_model.predict(X_future)
        fc_future = np.maximum(fc_future, 0)

        error = wmape(y_val, fc_val)

        return fc_val, fc_future, error
    except:
        return None, None, None

def compute_safety_stock(mean_demand: float, std_demand: float, lead_time_weeks: float,
                        demand_class: str, abc_class: str, y_train: np.ndarray) -> Dict[str, float]:
    """
    Compute safety stock with ABC-based uplift and lumpy demand buffer
    
    Returns dict with:
    - ss_base: base safety stock (z * σ * √LT)
    - ss_uplift_raw: raw uplift before budget constraint
    - ss_uplift_applied: actual uplift after budget (set later in main)
    - safety_stock: total safety stock (set later in main after budget)
    """
    
    # Base safety stock
    if std_demand > 0 and lead_time_weeks > 0:
        ss_base = Z_SCORE * std_demand * math.sqrt(lead_time_weeks)
    else:
        ss_base = 0.0
    
    # ABC uplift
    uplift_scalar = SS_UPLIFT_SCALAR.get(abc_class, 0.0)
    ss_uplift = ss_base * uplift_scalar
    
    # Lumpy demand buffer (percentile-based)
    lumpy_buffer = 0.0
    if demand_class == 'LUMPY':
        nonzero_demand = y_train[y_train > 0]
        if len(nonzero_demand) > 0:
            pctl = LUMPY_PCTL_NONZERO.get(abc_class, 0.90)
            percentile_demand = float(np.percentile(nonzero_demand, pctl * 100))
            lumpy_buffer = percentile_demand * lead_time_weeks * LUMPY_LT_BUFFER_FRAC
    
    # Total uplift (before budget constraint)
    total_uplift = ss_uplift + lumpy_buffer
    
    # Cap safety stock at 3 months of average demand
    max_ss = mean_demand * WEEKS_PER_MONTH * 3
    ss_base = min(ss_base, max_ss)
    total_uplift = min(total_uplift, max_ss - ss_base)
    
    return {
        'ss_base': ss_base,
        'ss_uplift_raw': total_uplift,
        'ss_uplift_applied_pre_budget': total_uplift,  # Will be adjusted later
        'safety_stock': ss_base + total_uplift  # Will be recalculated later
    }

def compute_reorder_metrics(weekly_forecast: np.ndarray, lead_time_weeks: float,
                           demand_class: str, abc_class: str, y_train: np.ndarray) -> Dict[str, float]:
    """Compute reorder point and order-up-to level using (s, S) policy.

    Formulas:
        ROP  = μ_L + SS                          (reorder point)
        S    = μ_(L+T) + z × σ_(L+T) × uplift    (order-up-to level)
        Q    = max(0, S − IP)                     (dynamic, computed at reorder time)

    T (coverage horizon) is ABC-tiered:
        A → 30 days, B → 60 days, C → 90 days
    """
    mean_weekly = np.mean(weekly_forecast)

    # CRITICAL: Use HISTORICAL std dev, not forecast std (Croston etc. would be 0)
    std_weekly = np.std(y_train) if len(y_train) > 0 else 0.0
    mean_daily = mean_weekly / DAYS_PER_WEEK
    std_daily = std_weekly / DAYS_PER_WEEK

    # Lead time with floor
    lead_time_days_raw = lead_time_weeks * DAYS_PER_WEEK
    lead_time_days = max(lead_time_days_raw, MIN_LEAD_TIME_DAYS)
    lead_time_weeks_floored = lead_time_days / DAYS_PER_WEEK

    # μ_L = expected demand over lead time
    mu_L = mean_daily * lead_time_days

    # Safety stock (uses floored lead time)
    ss_dict = compute_safety_stock(mean_weekly, std_weekly, lead_time_weeks_floored,
                                   demand_class, abc_class, y_train)

    # Reorder point: ROP = μ_L + SS
    reorder_point = mu_L + ss_dict['safety_stock']

    # Order-up-to level: S = μ_(L+T) + z × σ_(L+T) × (1 + uplift)
    coverage_days = COVERAGE_HORIZON_DAYS.get(abc_class, 60)
    total_horizon_days = lead_time_days + coverage_days
    mu_LT = mean_daily * total_horizon_days
    sigma_LT = std_daily * math.sqrt(total_horizon_days) if total_horizon_days > 0 else 0.0

    uplift_scalar = SS_UPLIFT_SCALAR.get(abc_class, 0.0)
    buffer_LT = Z_SCORE * sigma_LT * (1 + uplift_scalar)

    order_up_to_level = mu_LT + buffer_LT

    # Reference Q for display (nominal order size = S − ROP)
    reorder_quantity = max(0.0, order_up_to_level - reorder_point)

    return {
        'lead_time_mean_demand': mu_L,
        'reorder_point': reorder_point,
        'order_up_to_level': order_up_to_level,
        'reorder_quantity': reorder_quantity,
        'coverage_horizon_days': coverage_days,
        'daily_mean_demand': mean_daily,
        **ss_dict
    }

def apply_global_inv_cap(df_results: pd.DataFrame, df_monthly: pd.DataFrame, 
                        global_cap_units: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply global inventory capacity cap by scaling down ending inventory
    while respecting ABC-based MOI caps and preserving minimum (lead time demand)
    """
    print(f"\n{'='*70}")
    print("APPLYING GLOBAL INVENTORY CAP")
    print(f"{'='*70}")
    
    # Check if we exceed the cap
    if 'Ending Inventory' not in df_monthly.columns:
        print("  ⚠ No Ending Inventory column found, skipping cap")
        return df_results, df_monthly
    
    # Get the maximum ending inventory across all months/SKUs
    max_ending_inv = df_monthly['Ending Inventory'].max()
    total_ending_inv = df_monthly.groupby('Month')['Ending Inventory'].sum().max()
    
    print(f"  Current max total ending inventory: {total_ending_inv:,.0f} units")
    print(f"  Global cap: {global_cap_units:,.0f} units")
    
    if total_ending_inv <= global_cap_units:
        print(f"  ✓ Under capacity - no adjustment needed")
        return df_results, df_monthly
    
    print(f"  ⚠ OVER CAPACITY by {total_ending_inv - global_cap_units:,.0f} units ({((total_ending_inv/global_cap_units - 1)*100):.1f}%)")
    print(f"\n  Applying ABC-based MOI caps first...")
    
    # Step 1: Apply ABC-based MOI caps to create "min" and "flex" components
    df_monthly = df_monthly.copy()
    
    # Merge ABC classification
    sku_abc_map = dict(zip(df_results['sku'], df_results['abc_class']))
    df_monthly['abc_class'] = df_monthly['SKU'].map(sku_abc_map)
    
    # Calculate average monthly demand for each SKU
    sku_avg_demand = df_monthly.groupby('SKU')['Forecast'].mean().to_dict()
    df_monthly['avg_monthly_demand'] = df_monthly['SKU'].map(sku_avg_demand)
    
    # Calculate MOI cap for each SKU
    df_monthly['moi_cap_months'] = df_monthly['abc_class'].map(ABC_MOI_CAPS)
    df_monthly['moi_cap_inventory'] = df_monthly['avg_monthly_demand'] * df_monthly['moi_cap_months']
    
    # Split into MIN (lead time demand) and FLEX (rest up to MOI cap)
    # Merge lead time demand from df_results
    sku_lt_demand_map = dict(zip(df_results['sku'], df_results['lead_time_mean_demand']))
    df_monthly['lead_time_demand'] = df_monthly['SKU'].map(sku_lt_demand_map)
    
    df_monthly['min_inventory'] = df_monthly['lead_time_demand']
    df_monthly['flex_inventory'] = np.maximum(
        0, 
        np.minimum(
            df_monthly['Ending Inventory'] - df_monthly['min_inventory'],
            df_monthly['moi_cap_inventory'] - df_monthly['min_inventory']
        )
    )
    
    # Current inventory after MOI caps
    df_monthly['capped_ending_inventory'] = df_monthly['min_inventory'] + df_monthly['flex_inventory']
    
    # Check total after MOI caps
    total_after_moi_cap = df_monthly.groupby('Month')['capped_ending_inventory'].sum().max()
    print(f"  After ABC MOI caps: {total_after_moi_cap:,.0f} units")
    
    if total_after_moi_cap <= global_cap_units:
        print(f"  ✓ Under capacity after MOI caps - no further adjustment needed")
        df_monthly['Ending Inventory'] = df_monthly['capped_ending_inventory']

        # Update df_results with new reorder points
        # Note: We preserve the original safety stock calculation
        for idx, row in df_results.iterrows():
            sku = row['sku']
            sku_monthly = df_monthly[df_monthly['SKU'] == sku]
            if not sku_monthly.empty:
                new_ending_inv = sku_monthly['Ending Inventory'].iloc[0]
                df_results.at[idx, 'reorder_point'] = new_ending_inv

        return df_results, df_monthly
    
    # Step 2: Scale down FLEX component to fit under global cap
    print(f"  Still over capacity - scaling flex component...")
    
    # Calculate total MIN and FLEX across all months
    month_totals = df_monthly.groupby('Month').agg({
        'min_inventory': 'sum',
        'flex_inventory': 'sum'
    }).reset_index()
    
    # Find the month with maximum total inventory
    month_totals['total_inventory'] = month_totals['min_inventory'] + month_totals['flex_inventory']
    peak_month_idx = month_totals['total_inventory'].idxmax()
    peak_month = month_totals.loc[peak_month_idx]
    
    total_min = peak_month['min_inventory']
    total_flex = peak_month['flex_inventory']
    
    print(f"    Peak month MIN: {total_min:,.0f} units")
    print(f"    Peak month FLEX: {total_flex:,.0f} units")
    print(f"    Peak month TOTAL: {total_min + total_flex:,.0f} units")
    
    # Calculate scaling factor for FLEX
    available_for_flex = global_cap_units - total_min
    if available_for_flex < 0:
        print(f"  ⚠ WARNING: MIN inventory exceeds global cap!")
        print(f"  Consider increasing global cap or reducing lead times")
        scale_factor = 0.5  # Emergency scaling
    else:
        scale_factor = available_for_flex / total_flex if total_flex > 0 else 1.0
    
    scale_factor = min(scale_factor, 1.0)  # Never scale up
    print(f"    Scaling FLEX by factor: {scale_factor:.3f}")
    
    # Apply scaling to FLEX component
    df_monthly['scaled_flex_inventory'] = df_monthly['flex_inventory'] * scale_factor
    df_monthly['Ending Inventory'] = df_monthly['min_inventory'] + df_monthly['scaled_flex_inventory']
    
    # Verify we're under cap
    final_total = df_monthly.groupby('Month')['Ending Inventory'].sum().max()
    print(f"    Final max total ending inventory: {final_total:,.0f} units")
    print(f"    Utilization: {(final_total/global_cap_units*100):.1f}%")
    
    # Update df_results with new reorder points (use first month's ending inventory)
    # Note: We preserve the original safety stock calculation and only update reorder point
    for idx, row in df_results.iterrows():
        sku = row['sku']
        sku_monthly = df_monthly[df_monthly['SKU'] == sku]
        if not sku_monthly.empty:
            new_ending_inv = sku_monthly['Ending Inventory'].iloc[0]
            df_results.at[idx, 'reorder_point'] = new_ending_inv

            # Preserve original safety stock calculation
            # The reorder point represents the capped target inventory level
            # Safety stock remains as the statistical buffer calculation
    
    print(f"  ✓ Global inventory cap applied successfully")
    print(f"{'='*70}")
    
    return df_results, df_monthly

def process_sku(conn, sku_row: pd.Series, abc_map: Dict[str, str], 
               global_model=None, global_feature_cols=None, 
               sku_encodings=None) -> Optional[Tuple[Dict, pd.DataFrame]]:
    """Process a single SKU and return inventory metrics + monthly projections"""
    
    sku = sku_row['sku']
    uom = sku_row.get('uom', 'UNITS')
    vendor = sku_row.get('vendor_number', '')
    vendor_name = sku_row.get('vendor_name', '')
    collection = sku_row.get('collection', '')
    available = float(sku_row.get('available', 0))
    on_order = float(sku_row.get('on_order', 0))
    backorder = float(sku_row.get('backorder', 0))
    inventory_position = float(sku_row.get('inventory_position', 0))
    
    print(f"\n{'='*70}")
    print(f"Processing: {sku} ({uom})")
    print(f"{'='*70}")
    
    # Get lead time from database (IMLT field from ITEMMAST)
    lead_time_days = float(sku_row.get('lead_time', 30))
    lead_time_weeks = lead_time_days / DAYS_PER_WEEK
    
    print(f"  Lead Time: {lead_time_days:.1f} days ({lead_time_weeks:.1f} weeks) [from IMLT]")
    print(f"  Vendor: {vendor} - {vendor_name}")
    print(f"  Collection: {collection}")
    print(f"  Available: {available:,.0f}, On Order: {on_order:,.0f}, Backorder: {backorder:,.0f}")
    print(f"  Inventory Position: {inventory_position:,.0f}")
    
    # Fetch sales history
    df_sales = _fetch_sales_history(conn, sku, CUTOFF_DATE)
    
    if df_sales.empty:
        print(f"  ⚠ No sales history - SKIPPING")
        return None
    
    # VERIFICATION: Ensure all sales are actually for this SKU
    if 'item_number' in df_sales.columns:
        unique_items = df_sales['item_number'].unique()
        if len(unique_items) > 1 or unique_items[0] != sku.strip().upper():
            print(f"  ⚠ ERROR: Sales data contains wrong item numbers!")
            print(f"    Expected: {sku}")
            print(f"    Got: {unique_items}")
            print(f"  → SKIPPING to prevent incorrect forecasts")
            return None
    
    # Data quality check: Remove future dates
    today = AS_OF_DATE or pd.Timestamp.now()
    df_sales['transaction_date'] = pd.to_datetime(df_sales['transaction_date'], errors='coerce')
    df_sales = df_sales.dropna(subset=['transaction_date'])
    
    future_dated = df_sales[df_sales['transaction_date'] > today]
    if len(future_dated) > 0:
        print(f"  ⚠ WARNING: Found {len(future_dated)} future-dated transactions - excluding from analysis")
        print(f"    Future dates: {future_dated['transaction_date'].min()} to {future_dated['transaction_date'].max()}")
        df_sales = df_sales[df_sales['transaction_date'] <= today]
    
    if df_sales.empty:
        print(f"  ⚠ No valid sales data after cleaning - SKIPPING")
        return None
    
    # USER REQUIREMENT: Check if most recent sale is within 365 days
    most_recent_sale = df_sales['transaction_date'].max()
    days_since_last_sale = (today - most_recent_sale).days
    
    print(f"  Sales Records: {len(df_sales)}")
    print(f"  Date Range: {df_sales['transaction_date'].min().date()} to {df_sales['transaction_date'].max().date()}")
    print(f"  Most Recent Sale: {most_recent_sale.date()} ({days_since_last_sale} days ago)")
    
    # FILTER: Skip items where last sale > 365 days ago
    if days_since_last_sale > 365:
        print(f"  ✗ SKIPPING - Last sale more than 365 days ago ({days_since_last_sale} days)")
        print(f"    Item has not sold in over a year - not forecasting")
        return None
    
    print(f"  ✓ Item is active (last sale within 365 days)")
    print(f"  Total Shipped (all time): {df_sales['quantity_shipped'].sum():,.0f} {uom}")
    
    # Aggregate to weekly
    df_weekly = aggregate_to_weekly(df_sales)
    
    if len(df_weekly) < 8:
        print(f"  ⚠ Insufficient weekly data ({len(df_weekly)} weeks)")
        return None
    
    print(f"  Weekly Data Points: {len(df_weekly)}")
    
    # Classify demand pattern
    demand_class, adi, cv2 = classify_demand_pattern(df_weekly)
    print(f"  Demand Pattern: {demand_class} (ADI={adi:.2f}, CV²={cv2:.2f})")
    
    # Get ABC classification
    abc_class = abc_map.get(sku, 'C')
    print(f"  ABC Class: {abc_class}")
    
    # Train/val split
    split_idx = int(len(df_weekly) * 0.7)
    if split_idx < 4:
        split_idx = max(4, len(df_weekly) - 4)
    
    y_train = df_weekly['quantity'].values[:split_idx]
    y_val = df_weekly['quantity'].values[split_idx:]
    h_val = len(y_val)
    h_future = FUTURE_FORECAST_WEEKS
    
    # Build features for ML models
    X_train, y_train_ml, X_val, y_val_ml, future_state, _ = build_features(df_weekly, h_future)
    
    # Test all forecasting methods
    print(f"\n  Testing forecasting methods...")
    
    # Determine if this is a very slow mover (disable complex methods for these)
    total_sales = df_sales['quantity_shipped'].sum()
    weeks_of_data = len(df_weekly)
    avg_weekly_sales = total_sales / weeks_of_data if weeks_of_data > 0 else 0

    is_very_slow_mover = avg_weekly_sales < 1  # Less than 1 unit per week on average
    
    methods = {}
    
    # Classical methods (always test these)
    methods['Holt_Damped'] = forecast_holt_damped(pd.Series(y_train), pd.Series(y_val), h_future)
    methods['Croston'] = forecast_croston(y_train, y_val, h_future)
    methods['TSB'] = forecast_tsb(y_train, y_val, h_future)
    methods['SBA'] = forecast_sba(y_train, y_val, h_future)
    
    # ML methods (only for items with sufficient data AND not very slow movers)
    if len(X_train) >= 10:
        if not is_very_slow_mover:
            # Complex methods tend to over-predict for slow movers
            methods['Hurdle'] = forecast_hurdle(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
            methods['Neural_Network'] = forecast_neural_network(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
            methods['XGBoost'] = forecast_xgboost(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
        
        # Random Forest is more conservative, allow it for slow movers
        methods['Random_Forest'] = forecast_random_forest(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
    
    # Global model (if available)
    if global_model is not None and sku_encodings is not None and sku in sku_encodings:
        sku_encoding = sku_encodings[sku]
        methods['Global_XGBoost'] = forecast_with_global_model(
            global_model, global_feature_cols, sku_encoding, df_weekly, h_val, h_future
        )
    
    # Select best method
    best_method, weekly_forecast, best_mape = select_best_forecast(
        methods, pd.Series(y_train), demand_class
    )
    
    print(f"  ✓ Selected Method: {best_method} (wMAPE: {best_mape:.2f}%)")
    
    # Compute reorder metrics
    reorder_metrics = compute_reorder_metrics(
        weekly_forecast, lead_time_weeks, demand_class, abc_class, y_train
    )
    
    print(f"\n  Inventory Metrics:")
    print(f"    Mean Weekly Demand: {np.mean(weekly_forecast):.2f}")
    print(f"    Lead Time Demand: {reorder_metrics['lead_time_mean_demand']:.0f}")
    print(f"    Safety Stock (Base): {reorder_metrics['ss_base']:.0f}")
    print(f"    Safety Stock (Uplift): {reorder_metrics['ss_uplift_applied_pre_budget']:.0f}")
    print(f"    Safety Stock (Total): {reorder_metrics['safety_stock']:.0f}")
    print(f"    Reorder Point (s): {reorder_metrics['reorder_point']:.0f}")
    print(f"    Order-Up-To (S): {reorder_metrics['order_up_to_level']:.0f}")
    print(f"    Coverage Horizon: {reorder_metrics['coverage_horizon_days']} days ({abc_class}-class)")
    print(f"    Nominal Q (S-s): {reorder_metrics['reorder_quantity']:.0f}")
    
    # Aggregate weekly forecast to monthly
    weeks_to_months = []
    for i in range(0, len(weekly_forecast), 4):
        month_demand = weekly_forecast[i:i+4].sum()
        weeks_to_months.append(month_demand)
    
    num_months = 12
    monthly_forecast = weeks_to_months[:num_months]
    
    # Calculate starting month-year (first day of next month)
    today = AS_OF_DATE or pd.Timestamp.today()
    start_month = today.replace(day=1) + pd.DateOffset(months=1)
    
    # Monthly projections with order-up-to (s, S) reorder logic
    monthly_data = []
    current_inv = inventory_position  # Start with inventory position (available + on order - backorder)
    reorder_point = reorder_metrics['reorder_point']
    order_up_to = reorder_metrics['order_up_to_level']

    # Catch-up row for current month (MTD actuals + forecast remainder)
    month_start = today.replace(day=1).normalize()
    month_end = (month_start + pd.offsets.MonthEnd(0)).normalize()
    mtd_actual = float(df_sales[df_sales['transaction_date'] >= month_start]['quantity_shipped'].sum())
    remaining_days = max(0, (month_end - today.normalize()).days + 1)
    remainder_fc = float(np.mean(weekly_forecast)) * (remaining_days / DAYS_PER_WEEK)
    catchup_forecast = mtd_actual + remainder_fc

    # Dynamic Q: order up to S when IP drops to or below reorder point
    catchup_order_qty = max(0, order_up_to - current_inv) if current_inv <= reorder_point else 0
    catchup_ending_inv = max(0, current_inv - catchup_forecast + catchup_order_qty)

    monthly_data.append({
        'Month': month_start.strftime('%m/%d/%Y'),
        'Historical Demand': mtd_actual,
        'Beginning Inventory': current_inv,
        'Forecast': catchup_forecast,
        'Order Quantity': catchup_order_qty,
        'Ending Inventory': catchup_ending_inv,
        'SKU': sku,
        'Vendor Number': vendor,
        'Vendor Name': vendor_name,
        'Safety Stock': reorder_metrics['safety_stock']
    })

    current_inv = catchup_ending_inv

    for month_idx in range(num_months):
        # Calculate month-year string
        month_date = start_month + pd.DateOffset(months=month_idx)
        month_str = month_date.strftime('%m/%d/%Y')
        
        if month_idx < len(monthly_forecast):
            forecast_demand = monthly_forecast[month_idx]
        else:
            forecast_demand = np.mean(weekly_forecast) * 4
        
        # Beginning inventory for this month
        beginning_inv = current_inv

        # Order-up-to (s, S): when IP <= s, order Q = max(0, S - IP)
        if beginning_inv <= reorder_point:
            order_quantity = max(0, order_up_to - beginning_inv)
        else:
            order_quantity = 0

        # Calculate ending inventory
        ending_inv = max(0, beginning_inv - forecast_demand + order_quantity)
        
        monthly_data.append({
            'Month': month_str,
            'Historical Demand': np.nan,
            'Beginning Inventory': beginning_inv,
            'Forecast': forecast_demand,
            'Order Quantity': order_quantity,
            'Ending Inventory': ending_inv,
            'SKU': sku,
            'Vendor Number': vendor,
            'Vendor Name': vendor_name,
            'Safety Stock': reorder_metrics['safety_stock']
        })
        
        current_inv = ending_inv
    
    df_monthly = pd.DataFrame(monthly_data)

    # Historical monthly demand rows (placed before forecast rows)
    # Exclude current month since it's handled by the catch-up row
    df_hist = df_sales.copy()
    df_hist['month'] = df_hist['transaction_date'].dt.to_period('M').dt.to_timestamp()
    df_hist = df_hist.groupby('month', as_index=False)['quantity_shipped'].sum()
    df_hist = df_hist[df_hist['month'] < month_start]  # Exclude current month
    df_hist = df_hist.sort_values('month')

    if not df_hist.empty:
        hist_rows = []
        for _, r in df_hist.iterrows():
            hist_rows.append({
                'Month': r['month'].strftime('%m/%d/%Y'),
                'Historical Demand': r['quantity_shipped'],
                'Beginning Inventory': np.nan,
                'Forecast': np.nan,
                'Order Quantity': np.nan,
                'Ending Inventory': np.nan,
                'SKU': sku,
                'Vendor Number': vendor,
                'Vendor Name': vendor_name,
                'Safety Stock': reorder_metrics['safety_stock']
            })
        df_hist = pd.DataFrame(hist_rows)
        df_monthly = pd.concat([df_hist, df_monthly], ignore_index=True)

    # Enforce column order for output
    df_monthly = df_monthly[[
        'Month',
        'Historical Demand',
        'Safety Stock',
        'Beginning Inventory',
        'Forecast',
        'Order Quantity',
        'Ending Inventory',
        'SKU',
        'Vendor Number',
        'Vendor Name'
    ]]
    
    # Compile metrics
    metrics = {
        'sku': sku,
        'uom': uom,
        'vendor_number': vendor,
        'vendor_name': vendor_name,
        'collection': collection,
        'available': available,
        'backorder': backorder,
        'on_order': on_order,
        'inventory_position': inventory_position,
        'demand_class': demand_class,
        'adi': adi,
        'cv2': cv2,
        'abc_class': abc_class,
        'forecast_method': best_method,
        'forecast_wmape': best_mape,
        'lead_time_days': lead_time_days,
        'lead_time_weeks': lead_time_weeks,
        'mean_weekly_demand': np.mean(weekly_forecast),
        **reorder_metrics
    }
    
    return metrics, df_monthly

def run_inventory_planning():
    """Main execution function"""
    output_dir = Path(__file__).resolve().parent

    # Load sundries SKU list (REQUIRED)
    sku_list_path = output_dir / SUNDRIES_LIST_FILE
    sundries_sku_list = load_sundries_list(sku_list_path)

    # Connect to database
    conn = _connect()
    global AS_OF_DATE
    AS_OF_DATE = _resolve_as_of_date(conn)

    # Determine mode
    single_sku = bool(FOCUS_SKU)

    # NEW: Upfront filter to only SKUs with sales in the last 365 days
    # If a single SKU is requested, we still validate it before doing any work.
    if single_sku:
        focus = FOCUS_SKU.strip().upper()
        active_set, last_sale_map = filter_skus_to_active_last_365_days(
            conn, {focus}, days=365, as_of_date=AS_OF_DATE
        )
        if focus not in active_set:
            last_dt = last_sale_map.get(focus)
            if last_dt is None:
                print(f"\n✗ SKIPPING {focus}: No sales history found (last 365 days requirement).")
            else:
                days_ago = (AS_OF_DATE.normalize() - last_dt.normalize()).days
                print(f"\n✗ SKIPPING {focus}: Last sale was {last_dt.date()} ({days_ago} days ago), exceeds 365-day limit.")
            conn.close()
            return
        sundries_sku_list = {focus}
    else:
        before_ct = len(sundries_sku_list)
        active_set, last_sale_map = filter_skus_to_active_last_365_days(
            conn, sundries_sku_list, days=365, as_of_date=AS_OF_DATE
        )
        sundries_sku_list = active_set
        after_ct = len(sundries_sku_list)

        print(f"\n{'='*70}")
        print("ACTIVE SKU FILTER (LAST 365 DAYS)")
        print(f"{'='*70}")
        print(f"  From spreadsheet: {before_ct:,}")
        print(f"  Active (sold in last 365 days): {after_ct:,}")
        print(f"  Removed as inactive/no-sales: {(before_ct - after_ct):,}")
        print(f"{'='*70}")

        if after_ct == 0:
            print("\n⚠ No active SKUs found in the Sundries List after applying the last-365-days filter.")
            conn.close()
            return

    # Fetch SKU master (now filtered for non-SF items and limited to ACTIVE Sundries List)
    sku_master = _fetch_sku_master(conn, FOCUS_SKU, CUTOFF_DATE, sundries_sku_list)

    if sku_master.empty:
        print("\n⚠ No sundries SKUs to process")
        conn.close()
        return

    # Mode description
    if single_sku:
        mode_desc = f"Single SKU: {FOCUS_SKU.strip().upper()}"
    else:
        mode_desc = f"Sundries List: {len(sku_master)} SKUs"

    model_desc = "Global XGBoost Model" if USE_GLOBAL_MODEL and not single_sku else "Per-SKU Models"
    print(f"\nMODE: {mode_desc}")
    print(f"FORECAST MODEL: {model_desc}")

    # Build ABC segmentation based on trailing 12-month volume
    print(f"\n{'='*70}")
    print("BUILDING ABC SEGMENTATION")
    print(f"{'='*70}")
    sku_list_for_volume = sku_master["sku"].astype(str).str.upper().tolist()
    shipped_12m_map, total_vol_12m = load_trailing_12m_volume_sundries(
        conn, sku_list_for_volume, as_of_date=AS_OF_DATE
    )
    abc_map = compute_abc_classification(sku_master, shipped_12m_map)

    uplift_budget = GLOBAL_UPLIFT_BUDGET_PCT * total_vol_12m if ENABLE_GLOBAL_UPLIFT_BUDGET else None
    if ENABLE_GLOBAL_UPLIFT_BUDGET and uplift_budget is not None:
        print(f"\n  Global Uplift Budget: {uplift_budget:,.2f} units ({GLOBAL_UPLIFT_BUDGET_PCT*100:.1f}% of trailing 12m volume)")
    print(f"{'='*70}")

    # Train global model if enabled and processing multiple SKUs
    global_model = None
    global_feature_cols = None
    sku_encodings = None

    if USE_GLOBAL_MODEL and not single_sku:
        cutoff_date = pd.Timestamp(CUTOFF_DATE)
        df_global, sku_encodings = build_global_training_data(conn, sku_master, cutoff_date)

        if len(df_global) > 1000:
            global_model, global_feature_cols = train_global_xgboost(df_global)
        else:
            print("ƒsÿ Insufficient data for global model, falling back to per-SKU models")

    results = []
    all_monthly_projs = []

    for _, row in sku_master.iterrows():
        try:
            result = process_sku(
                conn, row, abc_map, global_model, global_feature_cols, sku_encodings
            )
            if result:
                metrics, monthly_proj = result
                results.append(metrics)
                all_monthly_projs.append(monthly_proj)
        except Exception as e:
            print(f"  ERROR: {str(e)}")
            import traceback
            traceback.print_exc()

    conn.close()

    if not results:
        print("\nNo SKUs processed")
        return

    df_results = pd.DataFrame(results)
    df_monthly = pd.concat(all_monthly_projs, ignore_index=True) if all_monthly_projs else pd.DataFrame()

    # APPLY GLOBAL UPLIFT BUDGET if enabled
    if ENABLE_GLOBAL_UPLIFT_BUDGET and uplift_budget is not None and uplift_budget > 0 and not df_results.empty:
        total_uplift = float(df_results["ss_uplift_applied_pre_budget"].sum())
        if total_uplift > uplift_budget:
            k = uplift_budget / total_uplift
            print(f"\n{'='*70}")
            print(f"ƒsÿ GLOBAL UPLIFT BUDGET ADJUSTMENT")
            print(f"{'='*70}")
            print(f"  Total uplift={total_uplift:,.2f} units")
            print(f"  Budget={uplift_budget:,.2f} units")
            print(f"  Scaling factor k={k:.3f}")
            print(f"  All uplifts will be scaled down proportionally")

            # Scale only the uplift, keep base intact
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"] * k
            df_results["safety_stock"] = df_results["ss_base"] + df_results["ss_uplift_applied"]
            df_results["reorder_point"] = df_results["lead_time_mean_demand"] + df_results["safety_stock"]

            print(f"  ƒo Adjusted safety stock for all SKUs")
            print(f"{'='*70}")
        else:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]
            print(f"\n  ƒo Global uplift budget NOT exceeded (Total={total_uplift:,.2f} units, Budget={uplift_budget:,.2f} units)")
    else:
        if not df_results.empty and "ss_uplift_applied_pre_budget" in df_results.columns:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]

    # APPLY GLOBAL ENDING INVENTORY CAP if enabled
    if ENABLE_GLOBAL_INV_CAP and not df_results.empty and not df_monthly.empty:
        df_results, df_monthly = apply_global_inv_cap(df_results, df_monthly, GLOBAL_INV_CAP_UNITS)

    # Determine output filename
    if single_sku:
        base_filename = "inventory_plan_sundries_single.xlsx"
    else:
        base_filename = "inventory_plan_sundries_list.xlsx"

    output_file = output_dir / base_filename

    # Try writing to the file
    saved = False
    for attempt in range(2):
        try:
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                df_results.to_excel(writer, sheet_name='Inventory_Metrics', index=False)
                if not df_monthly.empty:
                    df_monthly.to_excel(writer, sheet_name='Monthly_Projections', index=False)
            saved = True
            print(f"\nƒo Saved: {output_file}")
            print(f"  Processed: {len(results)} SKUs")

            # Show method selection summary
            if len(df_results) > 0:
                print(f"\n{'='*70}")
                print("DEMAND CLASSIFICATION SUMMARY")
                print(f"{'='*70}")
                demand_counts = df_results['demand_class'].value_counts()
                for demand_class, count in demand_counts.items():
                    pct = 100 * count / len(df_results)
                    avg_adi = df_results[df_results['demand_class'] == demand_class]['adi'].mean()
                    avg_cv2 = df_results[df_results['demand_class'] == demand_class]['cv2'].mean()
                    print(f"  {demand_class}: {count} SKUs ({pct:.1f}%) - Avg ADI={avg_adi:.2f}, Avg CV2={avg_cv2:.2f}")

                print(f"\n{'='*70}")
                print("FORECAST METHOD SELECTION SUMMARY")
                print(f"{'='*70}")
                method_counts = df_results['forecast_method'].value_counts()
                for method, count in method_counts.items():
                    pct = 100 * count / len(df_results)
                    avg_wmape = df_results[df_results['forecast_method'] == method]['forecast_wmape'].mean()
                    print(f"  {method}: {count} SKUs ({pct:.1f}%) - Avg wMAPE: {avg_wmape:.2f}%")
                print(f"{'='*70}")

            break
        except PermissionError:
            if attempt == 0:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                timestamped_filename = base_filename.replace('.xlsx', f'_{timestamp}.xlsx')
                output_file = output_dir / timestamped_filename
                print(f"\nƒsÿ Original file is locked. Trying: {timestamped_filename}")
            else:
                print(f"\nƒo- ERROR: Could not save file. Please close Excel and try again.")
                print(f"  File path: {output_file}")
                break

    if not saved:
        print(f"\nƒsÿ WARNING: Results were not saved to disk!")
        print(f"  Please close the Excel file and run again.")

if __name__ == "__main__":
    print("="*70)
    print("INVENTORY PLANNING - SUNDRIES VERSION (NON-SF ITEMS)")
    print("="*70)
    print(f"Mode: {'Single SKU - ' + FOCUS_SKU if FOCUS_SKU else 'Sundries List SKUs'}")
    print(f"Input File: {SUNDRIES_LIST_FILE}")
    print(f"Date Filter: Only SKUs with sales since {CUTOFF_DATE}")
    print(f"Unit Filter: Only items where sales_unit_of_measurement != 'SF'")
    print(f"\nWARNING - ACTIVE ITEM FILTERING:")
    print(f"  Items are filtered DURING processing (not upfront)")
    print(f"  Filter: Skip items where last sale > 365 days ago")
    print(f"  Inactive items will NOT appear in output")
    print(f"  This prevents ordering dead stock")
    print(f"\nData Aggregation: WEEKLY (reduces intermittency, improves accuracy)")
    print(f"Historical Data: Each SKU uses data from its first sale month forward")
    print(f"\nService Level: {SERVICE_LEVEL*100:.0f}%")
    print(f"Safety Stock Cap: Maximum 3 months of average demand")
    print(f"\nForecast Method Selection:")
    print(f"  ALL 9+ models tested per SKU (Neural Network, XGBoost, Random Forest,")
    print(f"  Holt Damped, SARIMA, TSB, SBA, Croston, Hurdle, + Global models)")
    print(f"  Best method selected using composite score (35% wMAPE, 20% MASE, 20% RMSSE, 15% UF, 10% Bias)")
    print(f"  Methods that produce unrealistic forecasts are automatically rejected")
    print(f"\nDemand Classification (ADI/CV²): SMOOTH, INTERMITTENT, ERRATIC, LUMPY")
    print(f"\nABC Uplift Scalars: A={SS_UPLIFT_SCALAR['A']:.1f}, B={SS_UPLIFT_SCALAR['B']:.1f}, C={SS_UPLIFT_SCALAR['C']:.1f}")
    print(f"Global Uplift Budget: {GLOBAL_UPLIFT_BUDGET_PCT*100:.0f}% of trailing 12m volume")
    
    if ENABLE_GLOBAL_INV_CAP:
        print(f"\n{'='*70}")
        print("GLOBAL INVENTORY CAP ENABLED")
        print(f"{'='*70}")
        print(f"  Max Total Ending Inventory: {GLOBAL_INV_CAP_UNITS:,.0f} UNITS")
        print(f"  ABC MOI Caps: A={ABC_MOI_CAPS['A']:.1f}, B={ABC_MOI_CAPS['B']:.1f}, C={ABC_MOI_CAPS['C']:.1f} months")
        print(f"  Strategy: Min (lead time demand) + Flex (scaled to fit capacity)")
    
    print("="*70)
    run_inventory_planning()
