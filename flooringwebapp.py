"""
Complete Inventory Planning System - FULLY CORRECTED VERSION
Fixed Issues:
1. TSB implementation was completely wrong - fixed the probability and size tracking
2. Added fallback to simple moving average when TSB produces unrealistic results  
3. Fixed standard deviation over lead time calculation
4. Fixed reorder quantity formula
5. Added validation to prevent absurd forecasts
"""

from __future__ import annotations
import copy
import html
import io
import json
import math, os, warnings, zipfile
import concurrent.futures
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Set environment variables FIRST to suppress warnings in subprocesses
os.environ['PYTHONWARNINGS'] = 'ignore'
os.environ['SKLEARN_ALLOW_DEPRECATED_SKLEARN_PACKAGE_INSTALL'] = 'True'

# Use simplefilter as a catch-all first
warnings.simplefilter("ignore")

# Suppress all warnings BEFORE importing sklearn (to avoid joblib/delayed warnings)
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", module="sklearn")
warnings.filterwarnings("ignore", module="statsmodels")
# Specific filters for sklearn parallel/delayed warnings
warnings.filterwarnings("ignore", message=".*sklearn.utils._joblib.*")
warnings.filterwarnings("ignore", message=".*delayed.*Parallel.*")
warnings.filterwarnings("ignore", message=".*sklearn.utils.parallel.delayed.*")
warnings.filterwarnings("ignore", category=FutureWarning, module="sklearn")
warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable",
    category=UserWarning,
)

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from core.demand_adjustment import make_return_aware_daily_series
from core.inventory import simulate_monthly_projection as _core_simulate_monthly
from core.niko_pricing import (
    load_niko_pricing_inputs as _load_niko_pricing_inputs_core,
    lookup_prices_for_sku as _lookup_prices_for_sku_core,
)
from core.vendor_quotes import (
    apply_quote_changes as _apply_vendor_quote_changes,
    load_history as _load_vendor_quote_history,
    load_quotes as _load_vendor_quotes,
    revert_history_entry as _revert_vendor_quote_history_entry,
    seed_from_workbook as _seed_vendor_quotes_from_workbook,
)
from core.vendor_optimizer import (
    DEFAULT_PALLET_SF,
    MAX_PALLET_UPLIFT_PCT,
    MIN_FULL_PALLET_QTY_SF,
    MIN_PURCHASABLE_QTY_SF,
    PRICE_ALERT_ABS_THRESHOLD,
    PRICE_ALERT_PCT_THRESHOLD,
    TRUCKLOAD_CAPACITY_SF,
    PurchaseRequest,
    VendorQuote,
    apply_minimum_order_policy,
    build_cost_alerts,
    normalize_item_key,
    normalize_vendor_name,
    optimize_vendor_mix,
)
try:
    from core.forecasting import forecast_prophet as _prophet_core, is_prophet_available as _is_prophet_available
except ImportError:
    _prophet_core = None
    _is_prophet_available = lambda: False

# Import shared UI styles
try:
    from ui.styles import get_tab_button_css, COLORS
except ImportError:
    get_tab_button_css = None
    COLORS = None

try:
    import pyodbc
except Exception:
    pyodbc = None
try:
    from scipy import stats
except Exception:
    stats = None
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", module="sklearn")
warnings.filterwarnings("ignore", module="statsmodels")
# Re-apply specific sklearn parallel warnings filter after imports
warnings.filterwarnings("ignore", message=".*sklearn.utils._joblib.*")
warnings.filterwarnings("ignore", message=".*delayed.*Parallel.*")
warnings.filterwarnings("ignore", message=".*sklearn.utils.parallel.delayed.*")
warnings.filterwarnings("ignore", message=".*should be used with.*sklearn.utils.parallel.Parallel.*")

# Suppress sklearn parallel and convergence warnings via environment
# PYTHONWARNINGS already set to 'ignore' at top of file
os.environ['LOKY_MAX_CPU_COUNT'] = '4'  # Limit CPU usage for parallel processing
warnings.filterwarnings("ignore")


# Path to the SharePoint-synced purchasing folder (vendor weekly Excel files live here).
# ETA (Due to Port) and ETW (Due in Inventory) from these files override Gartman POLINE dates
# because the SharePoint files are updated more frequently than the AS400.
SHAREPOINT_PURCHASING_DIR = Path("//server/Purchasing/2026 Shipment Reports")


def _read_sql_silent(sql: str, conn, params=None) -> pd.DataFrame:
    """Execute SQL query with all warnings suppressed (especially SQLAlchemy warnings)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")
        warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")
        if params is not None:
            return pd.read_sql_query(sql, conn, params=params)
        return pd.read_sql_query(sql, conn)


# ============================================================
# CONFIGURATION - CHANGE THIS TO PROCESS SPECIFIC SKU OR ALL
# ============================================================
FOCUS_SKU = ""  # Leave blank "" to use SKU list or process ALL SKUs
USE_GLOBAL_MODEL = True  # Set to True to train a single XGBoost model across ALL SKUs
SHAREPOINT_DIR = Path(r"C:\Users\niko\OneDrive - Old Master Products\Purchasing - Flooring Reports\Dashboard Files")
FORECAST_SKU_LIST_FILE = SHAREPOINT_DIR / "Forecast SKU List.xlsx"
USE_SKU_LIST = True  # Set to True to only process SKUs in the list file
PARALLEL_WORKERS = 0  # 0 = auto (cpu_count // 2); positive int = explicit worker count
# ============================================================

# Compact SKU-level output to match moulding/sundries format
VERBOSE_SKU_LOGS = False


def _sku_log(message: str) -> None:
    if VERBOSE_SKU_LOGS:
        print(message)


# ============================================================
# GLOBAL ENDING INVENTORY CAP (prevents warehouse capacity issues)
# ============================================================
ENABLE_GLOBAL_INV_CAP = False
GLOBAL_INV_CAP_SF = 6_000_000  # Maximum total ending inventory across all SKUs (SF)

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
ABC_BREAK_A = 0.70   # top 70% of cumulative volume
ABC_BREAK_B = 0.80   # next 20% (70% -> 90%)
SS_UPLIFT_SCALAR = {"A": 1.5, "B": 0.60, "C": 0.20}  # Increased to raise target inventory

# Lumpy buffer uses percentile demand instead of max demand 95 90 85 default
LUMPY_PCTL_NONZERO = {"A": 0.95, "B": 0.90, "C": 0.85}
LUMPY_LT_BUFFER_FRAC = 0.20  # 20% of percentile demand over lead time (was 0.10)

# Safety stock cap: applied AFTER all uplifts (lumpy + ABC) so they cannot bypass the limit.
# Set to N months of average demand. Mirrors SS_CAP_MONTHS in core/config.py for sundries/moulding.
SS_CAP_MONTHS = 1  # 1 month cap for flooring (now aligned to 95% service level)

# Vendors whose PO quantities are excluded from all dashboard calculations and
# never shown as an item's vendor. Some have open POs retained for lawsuit
# purposes only; others (like 1451) are vendors we no longer do business with.
# Never source from any vendor on this list.
EXCLUDED_PO_VENDORS: set = {"3942", "394201", "435", "1059", "1262", "1244", "2401", "1451"}

# Optional global clamp: cap total uplift as % of trailing-12-month volume
ENABLE_GLOBAL_UPLIFT_BUDGET = True
GLOBAL_UPLIFT_BUDGET_PCT = 0.05  # 5% of trailing 12m shipped SF (reduced from 8%) 0.05
# ============================================================

DSN_NAME = "Gartman"
FUTURE_FORECAST_DAYS = 365
FUTURE_FORECAST_WEEKS = 52  # 1 year of weekly forecasts
DAYS_PER_WEEK = 7
DAYS_PER_MONTH = 30.4
WEEKS_PER_MONTH = DAYS_PER_MONTH / DAYS_PER_WEEK
SERVICE_LEVEL = 0.95  # 95% service level
Z_SCORE = stats.norm.ppf(SERVICE_LEVEL) if stats is not None else 1.645

LEADTIMES_XLSX = SHAREPOINT_DIR / "Lead Times.xlsx"
CUTOFF_DATE = "2020-06-01"
WEBAPP_JSON_PATH = Path(__file__).resolve().parent / "flooringwebappJSON"
SUNDRIES_JSON_PATH = Path(__file__).resolve().parent / "sundrieswebappJSON"
MOULDING_JSON_PATH = Path(__file__).resolve().parent / "mouldingwebappJSON"
FLOORING_PROGRESS_FILE = Path(__file__).resolve().parent / ".flooring_forecast_progress.json"
SUNDRIES_PROGRESS_FILE = Path(__file__).resolve().parent / ".sundries_forecast_progress.json"
MOULDING_PROGRESS_FILE = Path(__file__).resolve().parent / ".moulding_forecast_progress.json"
STRIP_SKU_LIST_FILE    = SHAREPOINT_DIR / "StripSKUList.xlsx"
MOULDING_SKU_LIST_FILE = SHAREPOINT_DIR / "MouldingSKUList.xlsx"
UNFINISHED_PRICING_FILE  = SHAREPOINT_DIR / "Unfinished Pricing.xlsx"
INTERMODAL_FREIGHT_FILE  = SHAREPOINT_DIR / "Intermodal Freight.xlsx"
LEAD_TIMES_FILE          = SHAREPOINT_DIR / "Lead Times.xlsx"
UI_BUILD = "2026-01-14T15:10:00"

# "Niko" is the internal tab identifier used throughout (session state value,
# comparisons, dict keys); this maps it to the display name shown to users.
TAB_DISPLAY_NAMES = {"Niko": "Veronica"}

STRIP_VENDOR_COLUMNS = ["Indiana", "Dayspring", "Mullican", "Macon", "Anthony", "Orillia", "Lebanon", "Merrick"]
NIKO_VENDOR_ALIASES = {"MERRICK FROM SHEET": "MERRICK"}
NIKO_TRUCK_CAPACITY_SF = TRUCKLOAD_CAPACITY_SF
NIKO_DEFAULT_PALLET_SF = DEFAULT_PALLET_SF
NIKO_MAX_PALLET_UPLIFT_PCT = MAX_PALLET_UPLIFT_PCT
NIKO_MIN_PURCHASABLE_QTY_SF = MIN_PURCHASABLE_QTY_SF
NIKO_MIN_FULL_PALLET_QTY_SF = MIN_FULL_PALLET_QTY_SF
NIKO_PRICE_ALERT_PCT_THRESHOLD = PRICE_ALERT_PCT_THRESHOLD
NIKO_PRICE_ALERT_ABS_THRESHOLD = PRICE_ALERT_ABS_THRESHOLD
# Dayspring must be at least this many $/SF cheaper than every competitor to win
NIKO_DAYSPRING_MIN_ADVANTAGE_SF = 0.50
# Per-vendor truck color shades ordered T1 (darkest) → T2 → T3 (lightest).
# Earlier truckloads are visually heavier; later fill-loads fade back.
NIKO_VENDOR_TRUCK_COLORS: Dict[str, List[str]] = {
    "INDIANA":     ["#fde047", "#fef08a", "#fef9c3"],   # Yellows
    "DAYSPRING":   ["#7dd3fc", "#bae6fd", "#e0f2fe"],   # Light Blues
    "MACON":       ["#fca5a5", "#fecaca", "#fee2e2"],   # Reddish
    "MULLICAN":    ["#4ade80", "#86efac", "#bbf7d0"],   # Darker Greens
    "MERRICK":     ["#f9a8d4", "#fbcfe8", "#fce7f3"],   # Pinkish
    "APPALACHIAN": ["#5eead4", "#99f6e4", "#ccfbf1"],   # Teal / Mint
    "ANTHONY":     ["#d1d5db", "#e5e7eb", "#f3f4f6"],   # Grays
    "ORILLIA":     ["#c4b5fd", "#ddd6fe", "#ede9fe"],   # Purples
    "LEBANON":     ["#fdba74", "#fed7aa", "#ffedd5"],   # Oranges
}
# Fallback shades for any vendor not in the map above
_NIKO_FALLBACK_VENDOR_COLORS: List[List[str]] = [
    ["#d9f99d", "#ecfccb", "#f7fee7"],   # Lime
    ["#a5f3fc", "#cffafe", "#ecfeff"],   # Cyan
    ["#c7d2fe", "#e0e7ff", "#eef2ff"],   # Indigo
]

# ============================================================
# SKU CONSOLIDATION GROUPS
# Loaded from 'Strip SKU Combination' tab of StripSKUList.xlsx.
# Groups with 2+ items sharing the same Index are consolidated.
# The key is 'SKU1/SKU2/...' for display in the Niko tab's cards.
# ============================================================
def _build_sku_consolidation_groups() -> Dict[str, List[str]]:
    """
    Load SKU consolidation groups from the 'Strip SKU Combination' tab of StripSKUList.xlsx.
    Returns a dict mapping 'SKU1/SKU2/...' -> [SKU1, SKU2, ...] for groups with 2+ members.
    """
    strip_path = Path(__file__).resolve().parent / STRIP_SKU_LIST_FILE
    if not strip_path.exists():
        return {}
    try:
        df = pd.read_excel(strip_path, sheet_name="Strip SKU Combination", header=0)
        if df.shape[0] == 0:
            return {}
        index_col = df.columns[0]  # "INDEX"
        sku_col = df.columns[1]    # "Item Number"
        df[index_col] = pd.to_numeric(df[index_col], errors="coerce")
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        groups: Dict[str, List[str]] = {}
        for idx, grp in df.dropna(subset=[index_col]).groupby(index_col):
            skus = [s for s in grp[sku_col].tolist() if s and s.upper() != "NAN"]
            if len(skus) >= 2:
                key = "/".join(skus)
                groups[key] = skus
        return groups
    except Exception:
        return {}


SKU_CONSOLIDATION_GROUPS = _build_sku_consolidation_groups()

# Build reverse lookup: individual SKU -> consolidated SKU display name
SKU_TO_CONSOLIDATED: Dict[str, str] = {}
for consolidated_name, member_skus in SKU_CONSOLIDATION_GROUPS.items():
    for sku in member_skus:
        SKU_TO_CONSOLIDATED[sku.upper()] = consolidated_name

# Set of consolidated group keys (upper-cased) for fast O(1) row-type lookups in table renderers
SKU_GROUP_KEYS: set = {k.upper() for k in SKU_CONSOLIDATION_GROUPS}


def _load_credentials():
    print("  Loading credentials...")

    # ALWAYS clear potentially corrupted environment variables first
    # This prevents issues where password gets copied into username field
    uid_env = os.getenv("GARTMAN_UID", "").strip()
    pwd_env = os.getenv("GARTMAN_PWD", "").strip()

    # If env vars are identical, swapped, or look corrupted, clear them immediately
    if uid_env and pwd_env and uid_env == pwd_env:
        print("  Clearing corrupted environment variables (UID == PWD)...")
        os.environ.pop("GARTMAN_UID", None)
        os.environ.pop("GARTMAN_PWD", None)
        uid_env = ""
        pwd_env = ""

    # Load from secrets file (always load this as the authoritative source)
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

    # Check if env vars are swapped compared to file
    if uid_env and pwd_env and uid_file and pwd_file:
        if uid_env == pwd_file and pwd_env == uid_file:
            print("  Clearing swapped environment variables...")
            os.environ.pop("GARTMAN_UID", None)
            os.environ.pop("GARTMAN_PWD", None)
            uid_env = ""
            pwd_env = ""

    # Decide which credentials to use - prefer secrets file for reliability
    if uid_file and pwd_file:
        uid, pwd = uid_file, pwd_file
        print(f"  Loaded from secrets file: UID={uid}, PWD={'*' * len(pwd)}")
    elif uid_env and pwd_env:
        uid, pwd = uid_env, pwd_env
        print(f"  Using environment variables: UID={uid}, PWD={'*' * len(pwd)}")
    else:
        raise RuntimeError("Missing credentials")

    # Final validation: username and password should NOT be the same
    if uid == pwd:
        raise RuntimeError(f"ERROR: Username and password are identical ('{uid}'). This is incorrect!")

    return uid, pwd

def _connect():
    if pyodbc is None:
        raise RuntimeError("pyodbc is not available. Install local forecasting dependencies to run forecasts.")
    uid, pwd = _load_credentials()
    conn_str = f"DSN={DSN_NAME};UID={uid};PWD={pwd};"
    print(f"  Attempting database connection...")
    try:
        conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
        print("  Database connection successful!")
        return conn
    except pyodbc.Error as e:
        print("  Database connection failed!")
        print(f"  Error: {e}")
        raise

def load_lead_times(xlsx_path: Path) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float]]:
    """Load lead times from Excel"""
    print(f"\nLoading lead times from {xlsx_path}...")
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Final", header=0)
        item_col = _get_first_column(df, ["SKU", "Item Number", "ITEM_NUMBER"])
        lt_col = _get_first_column(df, ["FINAL", "Final", "final"])
        sf_col = _get_first_column(df, ["SF per Pallet", "SF per pallet", "SF_per_Pallet"])
        pallet_col = _get_first_column(df, ["Pallets per Container", "Pallets per container", "Pallets_per_Container"])
        required_cols = {item_col, lt_col}
        if not item_col or not lt_col:
            print(f"  WARNING: Missing columns in lead time sheet: {required_cols - set(df.columns)}")
            return {}, {}, {}
        item_col, lt_col = item_col, lt_col
        df[item_col] = df[item_col].astype(str).str.strip().str.upper()
        df = df[~df[lt_col].astype(str).str.upper().str.contains("DISCONTINUED", na=False)]
        df[lt_col] = pd.to_numeric(df[lt_col], errors="coerce")
        df = df.dropna(subset=[item_col, lt_col])
        lead_times = dict(zip(df[item_col], df[lt_col]))
        sf_per_pallet = {}
        pallets_per_container = {}
        if sf_col and sf_col in df.columns:
            df[sf_col] = pd.to_numeric(df[sf_col], errors="coerce")
            sf_per_pallet = dict(zip(df[item_col], df[sf_col]))
        if pallet_col and pallet_col in df.columns:
            df[pallet_col] = pd.to_numeric(df[pallet_col], errors="coerce")
            pallets_per_container = dict(zip(df[item_col], df[pallet_col]))
        print(f"  Loaded {len(lead_times)} lead times")
        return lead_times, sf_per_pallet, pallets_per_container
    except Exception as e:
        print(f"  WARNING: {e}")
        return {}, {}, {}

def load_forecast_sku_list(xlsx_path: Path) -> set:
    """Load list of SKUs to forecast from Excel file"""
    print(f"\nLoading forecast SKU list from {xlsx_path}...")
    try:
        # Try to read the file - accept any column name
        df = pd.read_excel(xlsx_path, header=0)
        
        # Use the first column as the SKU list
        if df.shape[0] == 0:
            print(f"  WARNING: File is empty")
            return set()
        
        sku_col = df.columns[0]
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        
        # Remove any blank/null values
        sku_list = set(df[sku_col].dropna())
        sku_list = {sku for sku in sku_list if sku and sku != 'NAN' and len(sku) > 0}
        
        print(f"  Loaded {len(sku_list)} SKUs from forecast list")
        return sku_list
    except FileNotFoundError:
        print(f"  WARNING: File not found - {xlsx_path}")
        print(f"  Will process ALL SKUs instead")
        return set()
    except Exception as e:
        print(f"  WARNING: Error reading file - {e}")
        print(f"  Will process ALL SKUs instead")
        return set()

def load_trailing_12m_volume(conn, sku_list=None) -> pd.DataFrame:
    """
    Returns ITEM_NUMBER, VOL_12M for SF demand in last 12 months.
    Uses SHLINE/SHHEAD like other sales queries.
    """
    print("\n  Loading trailing 12-month volume for ABC classification...")
    
    sku_filter = ""
    params = []

    if sku_list and len(sku_list) > 0:
        placeholders = ",".join(["?"] * len(sku_list))
        sku_filter = f" AND TRIM(L.SLITEM) IN ({placeholders}) "
        params = list(sku_list)

    sql = f"""
    SELECT
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        SUM(COALESCE(L.SLBLUO,0)) AS VOL_12M
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE L.SLUM2 LIKE '%SF%'
      AND H.SHIDAT >= (CURRENT_DATE - 365 DAYS)
      AND H.SHIDAT <= CURRENT_DATE
      {sku_filter}
    GROUP BY TRIM(L.SLITEM)
    """

    df = _read_sql_silent(sql, conn, params=params if params else None)
    df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    df["VOL_12M"] = pd.to_numeric(df["VOL_12M"], errors="coerce").fillna(0.0)
    
    print(f"  [OK] Loaded volume data for {len(df)} SKUs")
    return df

def build_abc_map(volume_df: pd.DataFrame) -> Dict[str, str]:
    """
    ABC segmentation by cumulative volume share.
    A = first 70% of volume, B = next 20%, C = last 10%
    """
    if volume_df.empty:
        return {}

    df = volume_df.copy()
    df = df.sort_values("VOL_12M", ascending=False)
    total = df["VOL_12M"].sum()
    
    if total <= 0:
        return {sku: "C" for sku in df["ITEM_NUMBER"].tolist()}

    df["cum_share"] = df["VOL_12M"].cumsum() / total

    abc = {}
    for _, r in df.iterrows():
        sku = r["ITEM_NUMBER"]
        cs = float(r["cum_share"])
        if cs <= ABC_BREAK_A:
            abc[sku] = "A"
        elif cs <= ABC_BREAK_B:
            abc[sku] = "B"
        else:
            abc[sku] = "C"
    
    # Print summary
    a_count = sum(1 for v in abc.values() if v == "A")
    b_count = sum(1 for v in abc.values() if v == "B")
    c_count = sum(1 for v in abc.values() if v == "C")
    print(f"\n  ABC Classification:")
    print(f"    A items (top 70% volume): {a_count} SKUs")
    print(f"    B items (next 20% volume): {b_count} SKUs")
    print(f"    C items (last 10% volume): {c_count} SKUs")
    
    return abc

def compute_ss_lumpy_from_percentile(y_weekly: pd.Series, lead_time_weeks: float, sku_abc: str, ss_base: float) -> float:
    """
    Lumpy buffer using percentile instead of max demand (more stable).
    Uses high percentile of non-zero weekly demand * LT_weeks * fraction
    """
    nonzero = y_weekly[y_weekly > 0].values.astype(float)
    if len(nonzero) < 2 or lead_time_weeks <= 0:
        return ss_base

    q = LUMPY_PCTL_NONZERO.get(sku_abc, 0.90)
    spike = float(np.percentile(nonzero, q * 100))
    ss_buffer = spike * float(lead_time_weeks) * float(LUMPY_LT_BUFFER_FRAC)

    return max(ss_base, ss_buffer)

def apply_ss_uplift_scalar(ss_base: float, ss_lumpy: float, sku_abc: str) -> tuple:
    """
    Returns (ss_final_pre_budget, uplift_raw, uplift_applied)
    Applies ABC-based scalar to the uplift only (keeps base intact)
    """
    uplift_raw = max(0.0, ss_lumpy - ss_base)
    scalar = SS_UPLIFT_SCALAR.get(sku_abc, 0.0)
    uplift_applied = scalar * uplift_raw
    ss_final = ss_base + uplift_applied
    return ss_final, uplift_raw, uplift_applied

def _build_in_list(values: List[str]) -> str:
    safe = [str(v).replace("'", "''") for v in values if v]
    return "'" + "','".join(safe) + "'" if safe else "''"

def get_active_skus(conn, single_sku=None, sku_list=None, ensure_skus=None):
    """
    Get active SKUs based on:
      - Sales within last 3 years, OR
      - First receipt within last 1 year.

    ensure_skus: optional set/list of SKU strings that must appear in the result
      regardless of sales recency (used for strip/Niko SKUs so they always show
      up in the dashboard even if they have no recent SF sales history).
    """
    excluded_vendors_str = _build_in_list(sorted(EXCLUDED_PO_VENDORS))

    # If we have a specific SKU list, use a much simpler and faster query
    if sku_list and len(sku_list) > 0:
        print(f"  Fetching data for {len(sku_list)} specific SKUs...")
        
        # Create IN clause for the SKU list
        sku_list_str = _build_in_list(sku_list)
        
        sql = f"""
        WITH
        PO_MAX_DATE AS (
          SELECT TRIM(L.PLITEM) AS ITEM_NUMBER, MAX(H.PHDOI) AS LATEST_DATE
          FROM GSFL2K.POLINE L
          JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
          WHERE TRIM(H.PHVEND) <> ''
            AND TRIM(H.PHVEND) NOT IN ({excluded_vendors_str})
            AND TRIM(L.PLITEM) IN ({sku_list_str})
          GROUP BY TRIM(L.PLITEM)
        ),
        LATEST_PO_VENDOR AS (
          SELECT PMD.ITEM_NUMBER, MIN(TRIM(H.PHVEND)) AS VENDOR_NUMBER
          FROM PO_MAX_DATE PMD
          JOIN GSFL2K.POLINE L ON TRIM(L.PLITEM) = PMD.ITEM_NUMBER
          JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
                               AND H.PHDOI = PMD.LATEST_DATE
          WHERE TRIM(H.PHVEND) <> ''
            AND TRIM(H.PHVEND) NOT IN ({excluded_vendors_str})
          GROUP BY PMD.ITEM_NUMBER
        ),
        FIRSTREC AS (
          SELECT TRIM(R.IRITEM) AS ITEM_NUMBER, MIN(R.IRDATE) AS FIRST_RECEIPT_DATE
          FROM GSFL2K.ITEMRECH R
          WHERE R.IRLOC NOT IN (90, 17, 41, 46)
            AND TRIM(R.IRITEM) IN ({sku_list_str})
          GROUP BY TRIM(R.IRITEM)
        ),
        LAST_SALE AS (
          SELECT TRIM(L.SLITEM) AS ITEM_NUMBER, MAX(H.SHIDAT) AS LAST_SALE_DATE
          FROM GSFL2K.SHLINE L
          JOIN GSFL2K.SHHEAD H
            ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
          WHERE L.SLUM2 LIKE '%SF%'
            AND COALESCE(L.SLBLUO,0) > 0
            AND TRIM(L.SLITEM) IN ({sku_list_str})
          GROUP BY TRIM(L.SLITEM)
        ),
        INV_RAW AS (
          SELECT B.IBITEM, SUM(B.IBQOH) AS QTY_ON_HAND, SUM(B.IBQOO) AS QTY_COMMITTED_RAW
          FROM GSFL2K.ITEMBAL B
          WHERE B.IBLOC NOT IN (90, 17, 41, 46)
            AND TRIM(B.IBITEM) IN ({sku_list_str})
          GROUP BY B.IBITEM
        ),
        NON_ORDER_COMMITMENTS AS (
          SELECT
            TRIM(OLITEM) AS ITEM_NUMBER,
            SUM(COALESCE(OLQSHP, 0)) AS NON_ORDER_COMMITTED_QTY
          FROM GSFL2K.OOLINE
          WHERE (
              OLCUST LIKE '%TRANSFER%'
              OR OLCUST LIKE '%OMP000%'
              OR OLCUST LIKE '%INV000%'
              OR OLCUST LIKE '%OLD001%'
            )
            AND OLLOC <> 90
            AND TRIM(OLITEM) IN ({sku_list_str})
          GROUP BY TRIM(OLITEM)
        ),
        INV AS (
          SELECT
            IR.IBITEM,
            IR.QTY_ON_HAND,
            CASE
              WHEN COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0) < 0
                THEN 0
              ELSE COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0)
            END AS QTY_COMMITTED
          FROM INV_RAW IR
          LEFT JOIN NON_ORDER_COMMITMENTS NOC
            ON NOC.ITEM_NUMBER = TRIM(IR.IBITEM)
        ),
        PO AS (
          SELECT PLITEM, SUM(PLBLUO) AS ON_PO_SF
          FROM GSFL2K.POLINE
          WHERE PLDELT LIKE '%A%'
            AND TRIM(PLITEM) IN ({sku_list_str})
          GROUP BY PLITEM
        ),
        BO AS (
          SELECT OLITEM, SUM(OLBLUB) AS BO_SF
          FROM GSFL2K.OOLINE
          WHERE OLCUST NOT LIKE '%TRANSFER%'
            AND OLCUST NOT LIKE '%OMP000%'
            AND OLCUST NOT LIKE '%INV000%'
            AND OLCUST NOT LIKE '%OLD001%'
            AND OLLOC <> 90
            AND OLBO LIKE 'Y'
            AND OLUM2 LIKE '%SF%'
            AND TRIM(OLITEM) IN ({sku_list_str})
          GROUP BY OLITEM
        )
        SELECT
          TRIM(M.IMITEM) AS ITEM_NUMBER,
          TRIM(M.IMDESC) AS DESCRIPTION,
          COALESCE(TRIM(LPV.VENDOR_NUMBER), CASE WHEN TRIM(M.IMVEND) IN ({excluded_vendors_str}) THEN NULL ELSE TRIM(M.IMVEND) END) AS VENDOR_NUMBER,
          TRIM(V.VMNAME) AS VENDOR_NAME,
          TRIM(X.IMCOLLECT) AS COLLECTION,
          FR.FIRST_RECEIPT_DATE,
          (
            CASE
              WHEN COALESCE(M.IMFACT, 0) = 0 OR TRIM(M.IMUM1) = TRIM(M.IMUM2)
                THEN (COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0))
              ELSE (COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0)) * M.IMFACT
            END
          ) AS AVAILABLE_SF,
          COALESCE(PO.ON_PO_SF, 0) AS ON_PO_SF,
          COALESCE(BO.BO_SF, 0) AS BACKORDER_SF,
          COALESCE(M.IMLT, 0) AS LEAD_TIME_IMLT
        FROM GSFL2K.ITEMMAST M
        LEFT JOIN LATEST_PO_VENDOR LPV ON LPV.ITEM_NUMBER = TRIM(M.IMITEM)
        LEFT JOIN GSFL2K.VENDMAST V ON TRIM(V.VMVEND) = COALESCE(TRIM(LPV.VENDOR_NUMBER), CASE WHEN TRIM(M.IMVEND) IN ({excluded_vendors_str}) THEN NULL ELSE TRIM(M.IMVEND) END)
        LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
        LEFT JOIN FIRSTREC FR ON FR.ITEM_NUMBER = TRIM(M.IMITEM)
        LEFT JOIN LAST_SALE LS ON LS.ITEM_NUMBER = TRIM(M.IMITEM)
        LEFT JOIN INV ON INV.IBITEM = M.IMITEM
        LEFT JOIN PO ON PO.PLITEM = M.IMITEM
        LEFT JOIN BO ON BO.OLITEM = M.IMITEM
        WHERE TRIM(M.IMITEM) IN ({sku_list_str})
          AND (
            (LS.LAST_SALE_DATE IS NOT NULL AND LS.LAST_SALE_DATE >= (CURRENT_DATE - 3 YEARS))
            OR (FR.FIRST_RECEIPT_DATE >= (CURRENT_DATE - 1 YEAR))
          )
        ORDER BY TRIM(M.IMITEM)
        """
        
        print("  Executing SQL query to fetch SKUs...")
        df = _read_sql_silent(sql, conn)
        print(f"  [OK] Query returned {len(df)} rows")
        
    else:
        # Original query for all SKUs or single SKU
        sql = """
        WITH
        PO_MAX_DATE AS (
          SELECT TRIM(L.PLITEM) AS ITEM_NUMBER, MAX(H.PHDOI) AS LATEST_DATE
          FROM GSFL2K.POLINE L
          JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
          WHERE TRIM(H.PHVEND) <> ''
            AND TRIM(H.PHVEND) NOT IN ({excluded_vendors_str})
            {lpv_filter}
          GROUP BY TRIM(L.PLITEM)
        ),
        LATEST_PO_VENDOR AS (
          SELECT PMD.ITEM_NUMBER, MIN(TRIM(H.PHVEND)) AS VENDOR_NUMBER
          FROM PO_MAX_DATE PMD
          JOIN GSFL2K.POLINE L ON TRIM(L.PLITEM) = PMD.ITEM_NUMBER
          JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
                               AND H.PHDOI = PMD.LATEST_DATE
          WHERE TRIM(H.PHVEND) <> ''
            AND TRIM(H.PHVEND) NOT IN ({excluded_vendors_str})
          GROUP BY PMD.ITEM_NUMBER
        ),
        LAST_SALE AS (
          SELECT TRIM(L.SLITEM) AS ITEM_NUMBER, MAX(H.SHIDAT) AS LAST_SALE_DATE
          FROM GSFL2K.SHLINE L
          JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
          WHERE L.SLUM2 LIKE '%SF%'
            AND COALESCE(L.SLBLUO,0) > 0
          GROUP BY TRIM(L.SLITEM)
        ),
        FIRSTREC AS (
          SELECT TRIM(R.IRITEM) AS ITEM_NUMBER, MIN(R.IRDATE) AS FIRST_RECEIPT_DATE
          FROM GSFL2K.ITEMRECH R
          WHERE R.IRLOC NOT IN (90, 17, 41, 46)
          GROUP BY TRIM(R.IRITEM)
        ),
        INV_RAW AS (
          SELECT B.IBITEM, SUM(B.IBQOH) AS QTY_ON_HAND, SUM(B.IBQOO) AS QTY_COMMITTED_RAW
          FROM GSFL2K.ITEMBAL B
          WHERE B.IBLOC NOT IN (90, 17, 41, 46)
          GROUP BY B.IBITEM
        ),
        NON_ORDER_COMMITMENTS AS (
          SELECT
            TRIM(OLITEM) AS ITEM_NUMBER,
            SUM(COALESCE(OLQSHP, 0)) AS NON_ORDER_COMMITTED_QTY
          FROM GSFL2K.OOLINE
          WHERE (
              OLCUST LIKE '%TRANSFER%'
              OR OLCUST LIKE '%OMP000%'
              OR OLCUST LIKE '%INV000%'
              OR OLCUST LIKE '%OLD001%'
            )
            AND OLLOC <> 90
          GROUP BY TRIM(OLITEM)
        ),
        INV AS (
          SELECT
            IR.IBITEM,
            IR.QTY_ON_HAND,
            CASE
              WHEN COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0) < 0
                THEN 0
              ELSE COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0)
            END AS QTY_COMMITTED
          FROM INV_RAW IR
          LEFT JOIN NON_ORDER_COMMITMENTS NOC
            ON NOC.ITEM_NUMBER = TRIM(IR.IBITEM)
        ),
        PO AS (
          SELECT PLITEM, SUM(PLBLUO) AS ON_PO_SF
          FROM GSFL2K.POLINE
          WHERE PLDELT LIKE '%A%'
          GROUP BY PLITEM
        ),
        BO AS (
          SELECT OLITEM, SUM(OLBLUB) AS BO_SF
          FROM GSFL2K.OOLINE
          WHERE OLCUST NOT LIKE '%TRANSFER%'
            AND OLCUST NOT LIKE '%OMP000%'
            AND OLCUST NOT LIKE '%INV000%'
            AND OLCUST NOT LIKE '%OLD001%'
            AND OLLOC <> 90
            AND OLBO LIKE 'Y'
            AND OLUM2 LIKE '%SF%'
          GROUP BY OLITEM
        )
        SELECT
          TRIM(M.IMITEM) AS ITEM_NUMBER,
          TRIM(M.IMDESC) AS DESCRIPTION,
          COALESCE(TRIM(LPV.VENDOR_NUMBER), CASE WHEN TRIM(M.IMVEND) IN ({excluded_vendors_str}) THEN NULL ELSE TRIM(M.IMVEND) END) AS VENDOR_NUMBER,
          TRIM(V.VMNAME) AS VENDOR_NAME,
          TRIM(X.IMCOLLECT) AS COLLECTION,
          FR.FIRST_RECEIPT_DATE,
          (
            CASE
              WHEN COALESCE(M.IMFACT, 0) = 0 OR TRIM(M.IMUM1) = TRIM(M.IMUM2)
                THEN (COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0))
              ELSE (COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0)) * M.IMFACT
            END
          ) AS AVAILABLE_SF,
          COALESCE(PO.ON_PO_SF, 0) AS ON_PO_SF,
          COALESCE(BO.BO_SF, 0) AS BACKORDER_SF,
          COALESCE(M.IMLT, 0) AS LEAD_TIME_IMLT
        FROM GSFL2K.ITEMMAST M
        LEFT JOIN LATEST_PO_VENDOR LPV ON LPV.ITEM_NUMBER = TRIM(M.IMITEM)
        LEFT JOIN GSFL2K.VENDMAST V ON TRIM(V.VMVEND) = COALESCE(TRIM(LPV.VENDOR_NUMBER), CASE WHEN TRIM(M.IMVEND) IN ({excluded_vendors_str}) THEN NULL ELSE TRIM(M.IMVEND) END)
        LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
        LEFT JOIN FIRSTREC FR ON FR.ITEM_NUMBER = TRIM(M.IMITEM)
        LEFT JOIN LAST_SALE LS ON LS.ITEM_NUMBER = TRIM(M.IMITEM)
        LEFT JOIN INV ON INV.IBITEM = M.IMITEM
        LEFT JOIN PO ON PO.PLITEM = M.IMITEM
        LEFT JOIN BO ON BO.OLITEM = M.IMITEM
        WHERE (
          (LS.LAST_SALE_DATE IS NOT NULL AND LS.LAST_SALE_DATE >= (CURRENT_DATE - 3 YEARS))
          OR (FR.FIRST_RECEIPT_DATE >= (CURRENT_DATE - 1 YEAR))
        )
        {sku_filter}
        ORDER BY TRIM(M.IMITEM)
        """

        sku_filter = f"AND TRIM(M.IMITEM) = '{single_sku}'" if single_sku else ""
        lpv_filter = f"AND TRIM(L.PLITEM) = '{single_sku}'" if single_sku else ""
        final_sql = sql.format(sku_filter=sku_filter, lpv_filter=lpv_filter, excluded_vendors_str=excluded_vendors_str)
        
        print("  Executing SQL query to fetch SKUs...")
        df = _read_sql_silent(final_sql, conn)
        print(f"  [OK] Query returned {len(df)} rows")

    df['ITEM_NUMBER'] = df['ITEM_NUMBER'].astype(str).str.strip().str.upper()
    df['FIRST_RECEIPT_DATE'] = pd.to_datetime(df['FIRST_RECEIPT_DATE'], errors='coerce')

    # Supplementary pass: fetch inventory data for any ensure_skus that the main
    # query excluded (e.g. strip SKUs with no recent SF sales or non-SF UOM).
    # This uses ITEMMAST directly — no sales-recency or UOM filter — so items
    # with zero history still appear in the forecast run and can be displayed
    # in the Niko dashboard (they will receive no-history stubs in process_sku).
    if ensure_skus:
        found = set(df['ITEM_NUMBER'].str.strip().str.upper())
        missing = [s.strip().upper() for s in ensure_skus if s.strip().upper() not in found]
        if missing:
            print(f"  Supplementary load for {len(missing)} strip SKU(s) not in main query: {missing}")
            missing_str = _build_in_list(missing)
            supp_sql = f"""
            WITH
            PO_MAX_DATE AS (
              SELECT TRIM(L.PLITEM) AS ITEM_NUMBER, MAX(H.PHDOI) AS LATEST_DATE
              FROM GSFL2K.POLINE L
              JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
              WHERE TRIM(H.PHVEND) <> ''
                AND TRIM(H.PHVEND) NOT IN ({excluded_vendors_str})
                AND TRIM(L.PLITEM) IN ({missing_str})
              GROUP BY TRIM(L.PLITEM)
            ),
            LATEST_PO_VENDOR AS (
              SELECT PMD.ITEM_NUMBER, MIN(TRIM(H.PHVEND)) AS VENDOR_NUMBER
              FROM PO_MAX_DATE PMD
              JOIN GSFL2K.POLINE L ON TRIM(L.PLITEM) = PMD.ITEM_NUMBER
              JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
                                   AND H.PHDOI = PMD.LATEST_DATE
              WHERE TRIM(H.PHVEND) <> ''
                AND TRIM(H.PHVEND) NOT IN ({excluded_vendors_str})
              GROUP BY PMD.ITEM_NUMBER
            ),
            INV_RAW AS (
              SELECT B.IBITEM,
                     SUM(B.IBQOH) AS QTY_ON_HAND,
                     SUM(B.IBQOO) AS QTY_COMMITTED_RAW
              FROM GSFL2K.ITEMBAL B
              WHERE B.IBLOC NOT IN (90, 17, 41, 46)
                AND TRIM(B.IBITEM) IN ({missing_str})
              GROUP BY B.IBITEM
            ),
            NON_ORDER_COMMITMENTS AS (
              SELECT
                TRIM(OLITEM) AS ITEM_NUMBER,
                SUM(COALESCE(OLQSHP, 0)) AS NON_ORDER_COMMITTED_QTY
              FROM GSFL2K.OOLINE
              WHERE (
                  OLCUST LIKE '%TRANSFER%'
                  OR OLCUST LIKE '%OMP000%'
                  OR OLCUST LIKE '%INV000%'
                  OR OLCUST LIKE '%OLD001%'
                )
                AND OLLOC <> 90
                AND TRIM(OLITEM) IN ({missing_str})
              GROUP BY TRIM(OLITEM)
            ),
            INV AS (
              SELECT
                IR.IBITEM,
                IR.QTY_ON_HAND,
                CASE
                  WHEN COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0) < 0
                    THEN 0
                  ELSE COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0)
                END AS QTY_COMMITTED
              FROM INV_RAW IR
              LEFT JOIN NON_ORDER_COMMITMENTS NOC
                ON NOC.ITEM_NUMBER = TRIM(IR.IBITEM)
            ),
            PO AS (
              SELECT PLITEM, SUM(PLBLUO) AS ON_PO_SF
              FROM GSFL2K.POLINE
              WHERE PLDELT LIKE '%A%'
                AND TRIM(PLITEM) IN ({missing_str})
              GROUP BY PLITEM
            ),
            BO AS (
              SELECT OLITEM, SUM(OLBLUB) AS BO_SF
              FROM GSFL2K.OOLINE
              WHERE OLCUST NOT LIKE '%TRANSFER%'
                AND OLCUST NOT LIKE '%OMP000%'
                AND OLCUST NOT LIKE '%INV000%'
                AND OLCUST NOT LIKE '%OLD001%'
                AND OLLOC <> 90
                AND OLBO LIKE 'Y'
                AND OLUM2 LIKE '%SF%'
                AND TRIM(OLITEM) IN ({missing_str})
              GROUP BY OLITEM
            )
            SELECT
              TRIM(M.IMITEM)  AS ITEM_NUMBER,
              TRIM(M.IMDESC)  AS DESCRIPTION,
              COALESCE(TRIM(LPV.VENDOR_NUMBER), CASE WHEN TRIM(M.IMVEND) IN ({excluded_vendors_str}) THEN NULL ELSE TRIM(M.IMVEND) END)  AS VENDOR_NUMBER,
              TRIM(V.VMNAME)  AS VENDOR_NAME,
              TRIM(X.IMCOLLECT) AS COLLECTION,
              CAST(NULL AS DATE) AS FIRST_RECEIPT_DATE,
              (CASE
                WHEN COALESCE(M.IMFACT, 0) = 0 OR TRIM(M.IMUM1) = TRIM(M.IMUM2)
                  THEN (COALESCE(INV.QTY_ON_HAND, 0) - COALESCE(INV.QTY_COMMITTED, 0))
                ELSE (COALESCE(INV.QTY_ON_HAND, 0) - COALESCE(INV.QTY_COMMITTED, 0)) * M.IMFACT
               END) AS AVAILABLE_SF,
              COALESCE(PO.ON_PO_SF, 0) AS ON_PO_SF,
              COALESCE(BO.BO_SF,  0) AS BACKORDER_SF,
              COALESCE(M.IMLT, 0) AS LEAD_TIME_IMLT
            FROM GSFL2K.ITEMMAST M
            LEFT JOIN LATEST_PO_VENDOR LPV ON LPV.ITEM_NUMBER = TRIM(M.IMITEM)
            LEFT JOIN GSFL2K.VENDMAST V ON TRIM(V.VMVEND) = COALESCE(TRIM(LPV.VENDOR_NUMBER), CASE WHEN TRIM(M.IMVEND) IN ({excluded_vendors_str}) THEN NULL ELSE TRIM(M.IMVEND) END)
            LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
            LEFT JOIN INV ON INV.IBITEM = M.IMITEM
            LEFT JOIN PO ON PO.PLITEM = M.IMITEM
            LEFT JOIN BO  ON BO.OLITEM = M.IMITEM
            WHERE TRIM(M.IMITEM) IN ({missing_str})
            ORDER BY TRIM(M.IMITEM)
            """
            supp_df = _read_sql_silent(supp_sql, conn)
            if not supp_df.empty:
                supp_df['ITEM_NUMBER'] = supp_df['ITEM_NUMBER'].astype(str).str.strip().str.upper()
                supp_df['FIRST_RECEIPT_DATE'] = pd.to_datetime(supp_df['FIRST_RECEIPT_DATE'], errors='coerce')
                df = pd.concat([df, supp_df], ignore_index=True)
                print(f"  [OK] Supplementary load added {len(supp_df)} row(s)")

    # Calculate Inventory Position = Available + On PO - Backorder
    df['INVENTORY_POSITION'] = df['AVAILABLE_SF'] + df['ON_PO_SF'] - df['BACKORDER_SF']

    # Resolve lead time with hierarchical fallback to reduce hard 30-day defaults.
    if "LEAD_TIME_IMLT" in df.columns:
        lt_raw = pd.to_numeric(df["LEAD_TIME_IMLT"], errors="coerce")
        vendor_key = df["VENDOR_NUMBER"].astype(str).str.strip().str.upper()
        valid = lt_raw.notna() & (lt_raw > 0)
        non_default = valid & (~lt_raw.between(29.5, 30.5))

        vendor_medians = lt_raw[non_default].groupby(vendor_key[non_default]).median()
        if non_default.any():
            global_median = float(lt_raw[non_default].median())
        elif valid.any():
            global_median = float(lt_raw[valid].median())
        else:
            global_median = 30.0

        resolved_vals: List[float] = []
        resolved_src: List[str] = []
        for lt, vendor in zip(lt_raw, vendor_key):
            if np.isfinite(lt) and lt > 0 and not (29.5 <= lt <= 30.5 and vendor in vendor_medians.index):
                chosen = float(lt)
                source = "Gartman"
            elif vendor in vendor_medians.index and np.isfinite(vendor_medians[vendor]):
                chosen = float(vendor_medians[vendor])
                source = "VendorMedian"
            elif np.isfinite(global_median) and global_median > 0:
                chosen = float(global_median)
                source = "GlobalMedian"
            elif np.isfinite(lt) and lt > 0:
                chosen = float(lt)
                source = "Gartman"
            else:
                chosen = 30.0
                source = "Default30"

            resolved_vals.append(max(7.0, chosen))
            resolved_src.append(source)

        df["LEAD_TIME_EFFECTIVE"] = resolved_vals
        df["LEAD_TIME_SOURCE_FALLBACK"] = resolved_src
    else:
        df["LEAD_TIME_EFFECTIVE"] = 30.0
        df["LEAD_TIME_SOURCE_FALLBACK"] = "Default30"
    
    # Report results - filtering already done in SQL if sku_list was provided
    if sku_list and len(sku_list) > 0:
        print(f"\nFound {len(df)} SKUs from forecast list")
        if len(df) < len(sku_list):
            missing = len(sku_list) - len(df)
            print(f"  Note: {missing} SKUs from list not found in database")
    else:
        print(f"\nFound {len(df)} active SKUs (sales in last 3 years or first receipt within 1 year)")
    
    return df

def load_sales_history(conn, sku, start_date):
    """Load sales history using correct SQL"""
    today = pd.Timestamp.today().normalize()
    first_of_month = pd.Timestamp(year=today.year, month=today.month, day=1)
    hist_end_dt = first_of_month + pd.offsets.MonthBegin(1)
    
    sql = """
    SELECT H.SHIDAT AS SALES_DATE, SUM(COALESCE(L.SLBLUO,0)) AS QTY_SOLD_SF
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE L.SLUM2 LIKE '%SF%'
      AND TRIM(L.SLITEM) = ?
      AND H.SHIDAT >= ?
      AND H.SHIDAT < ?
    GROUP BY H.SHIDAT
    ORDER BY H.SHIDAT
    """
    
    df = _read_sql_silent(sql, conn, params=[sku, start_date.strftime("%Y-%m-%d"), hist_end_dt.strftime("%Y-%m-%d")])
    df["SALES_DATE"] = pd.to_datetime(df["SALES_DATE"], errors="coerce")
    df["QTY_SOLD_SF"] = pd.to_numeric(df["QTY_SOLD_SF"], errors="coerce").fillna(0.0)
    # Make demand return-aware: returns erase prior sales rather than forming
    # negative-demand spikes in the modeled history.
    return make_return_aware_daily_series(df, date_col="SALES_DATE", qty_col="QTY_SOLD_SF")


def bulk_load_sales_history(conn, sku_list: List[str], cutoff_date: "pd.Timestamp") -> "Dict[str, pd.DataFrame]":
    """Pre-load all SKU sales histories in a single DB round-trip.

    Returns a dict keyed by normalized (stripped, upper) SKU string.  Each
    value is the raw (SALES_DATE, QTY_SOLD_SF) DataFrame — the same rows that
    load_sales_history() would retrieve per-SKU, before make_return_aware_daily_series.
    process_sku() uses this when sales_raw_df is supplied so it never touches
    the database during the parallel phase.

    Batches the IN-list in groups of 500 to stay within ODBC parameter limits.
    """
    if not sku_list:
        return {}

    today = pd.Timestamp.today().normalize()
    first_of_month = pd.Timestamp(year=today.year, month=today.month, day=1)
    hist_end_dt = first_of_month + pd.offsets.MonthBegin(1)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")
    end_str = hist_end_dt.strftime("%Y-%m-%d")

    skus_clean = [str(s).strip().upper() for s in sku_list]
    BATCH_SIZE = 500
    result: Dict[str, pd.DataFrame] = {}

    for batch_start in range(0, len(skus_clean), BATCH_SIZE):
        batch = skus_clean[batch_start: batch_start + BATCH_SIZE]
        placeholders = ", ".join(["?" for _ in batch])
        sql = f"""
        SELECT TRIM(L.SLITEM) AS ITEM_NUMBER,
               H.SHIDAT        AS SALES_DATE,
               SUM(COALESCE(L.SLBLUO, 0)) AS QTY_SOLD_SF
        FROM GSFL2K.SHLINE L
        JOIN GSFL2K.SHHEAD H
          ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC
         AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
        WHERE L.SLUM2 LIKE '%SF%'
          AND TRIM(L.SLITEM) IN ({placeholders})
          AND H.SHIDAT >= ?
          AND H.SHIDAT < ?
        GROUP BY TRIM(L.SLITEM), H.SHIDAT
        ORDER BY TRIM(L.SLITEM), H.SHIDAT
        """
        params = batch + [cutoff_str, end_str]
        df_batch = _read_sql_silent(sql, conn, params=params)
        if df_batch.empty:
            continue
        df_batch["SALES_DATE"] = pd.to_datetime(df_batch["SALES_DATE"], errors="coerce")
        df_batch["QTY_SOLD_SF"] = pd.to_numeric(df_batch["QTY_SOLD_SF"], errors="coerce").fillna(0.0)
        df_batch["ITEM_NUMBER"] = df_batch["ITEM_NUMBER"].astype(str).str.strip().str.upper()
        for sku, grp in df_batch.groupby("ITEM_NUMBER"):
            result[sku] = grp[["SALES_DATE", "QTY_SOLD_SF"]].reset_index(drop=True)

    return result


def _find_latest_vendor_files(purchasing_dir: Path) -> Dict[str, Path]:
    """
    Scan purchasing_dir for vendor weekly files named '{PREFIX} {N}.xlsx'.
    Returns {prefix_upper: path_to_highest_week_file} for each vendor found.
    """
    import re
    latest: Dict[str, Tuple[int, Path]] = {}
    pattern = re.compile(r"^([A-Za-z]+(?:_[A-Za-z]+)?)\s+(\d+)\.xlsx$", re.IGNORECASE)
    try:
        for f in purchasing_dir.iterdir():
            m = pattern.match(f.name)
            if m:
                prefix = m.group(1).upper()
                week = int(m.group(2))
                if prefix not in latest or week > latest[prefix][0]:
                    latest[prefix] = (week, f)
    except Exception:
        return {}
    return {prefix: info[1] for prefix, info in latest.items()}


def load_sharepoint_water_dates(purchasing_dir: Path) -> pd.DataFrame:
    """
    Read the WATER sheet from the latest week file for each vendor in purchasing_dir.
    Returns a DataFrame with columns:
        ITEM_NUMBER, ETA (Due to Port), ETW (Due in Inventory), CONTAINER, PO_NUMBER
    These dates are more up-to-date than POLINE (PLPDAT/PLDDAT) in Gartman.
    """
    try:
        import openpyxl
    except ImportError:
        return pd.DataFrame()

    vendor_files = _find_latest_vendor_files(purchasing_dir)
    if not vendor_files:
        return pd.DataFrame()

    frames = []
    for prefix, filepath in vendor_files.items():
        try:
            wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
            if "WATER" not in wb.sheetnames:
                wb.close()
                continue
            ws = wb["WATER"]
            rows = list(ws.iter_rows(values_only=True))
            wb.close()

            # Locate header row (contains both 'ITEM' and 'ETA')
            header = None
            header_idx = 0
            for i, row in enumerate(rows):
                upper = [str(v).strip().upper() if v is not None else "" for v in row]
                if any("ITEM" in s for s in upper) and any("ETA" in s for s in upper):
                    header = upper
                    header_idx = i
                    break
            if header is None:
                continue

            def _col(candidates):
                for cand in candidates:
                    for i, h in enumerate(header):
                        if cand in h:
                            return i
                return None

            idx_item = _col(["ITEM #", "ITEM#"])
            idx_eta  = _col(["ETA"])
            idx_etw  = _col(["ETW"])
            idx_cont = _col(["CI NO", "CONTAINER"])
            idx_po   = _col(["PO#", "PO #"])

            if idx_item is None or idx_eta is None or idx_etw is None:
                continue

            last_container = None
            last_po = None
            for row in rows[header_idx + 1:]:
                if not any(v is not None for v in row):
                    continue
                raw_item = row[idx_item]
                if raw_item is None:
                    continue
                item = str(raw_item).strip().upper()
                if not item or item in ("", "NONE", "NAN"):
                    continue

                # Forward-fill container# and PO# — they appear only on the first
                # item of each container group; subsequent items in the same
                # container leave those cells blank.
                raw_cont = row[idx_cont] if idx_cont is not None else None
                raw_po   = row[idx_po]   if idx_po   is not None else None
                if raw_cont is not None:
                    parts = str(raw_cont).strip().split("\n")
                    last_container = parts[-1].strip()
                if raw_po is not None:
                    last_po = str(raw_po).strip()

                frames.append({
                    "ITEM_NUMBER": item,
                    "ETA":         row[idx_eta],
                    "ETW":         row[idx_etw],
                    "CONTAINER":   last_container,
                    "PO_NUMBER":   last_po,
                    "VENDOR_PREFIX": prefix,
                })
        except Exception:
            continue

    if not frames:
        return pd.DataFrame()

    df = pd.DataFrame(frames)
    df["ETA"] = pd.to_datetime(df["ETA"], errors="coerce")
    df["ETW"] = pd.to_datetime(df["ETW"], errors="coerce")
    df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    df["PO_NUMBER"] = df["PO_NUMBER"].fillna("").astype(str).str.strip()
    return df


def load_sharepoint_inventory_status(purchasing_dir: Path) -> pd.DataFrame:
    """
    Read PRODUCTION and WATER sheets from the latest week file for each vendor.
    Returns a DataFrame with per-item SF quantities broken into three buckets:
        ITEM_NUMBER, PRODUCTION_SF, READY_SF, WATER_SF

    Logic:
      - PRODUCTION sheet rows with REMARK containing 'ready' → READY_SF
      - PRODUCTION sheet rows without that remark            → PRODUCTION_SF
      - WATER sheet 'SQF or PCS' column                     → WATER_SF
    """
    try:
        import openpyxl
    except ImportError:
        return pd.DataFrame()

    vendor_files = _find_latest_vendor_files(purchasing_dir)
    if not vendor_files:
        return pd.DataFrame()

    prod_frames:  List[Dict] = []
    water_frames: List[Dict] = []

    def _header_col(header: list, candidates: list) -> Optional[int]:
        for cand in candidates:
            for i, h in enumerate(header):
                if cand in h:
                    return i
        return None

    for prefix, filepath in vendor_files.items():
        try:
            wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)

            # ── PRODUCTION sheet ────────────────────────────────────────────
            if "PRODUCTION" in wb.sheetnames:
                rows = list(wb["PRODUCTION"].iter_rows(values_only=True))
                hdr = None
                hdr_idx = 0
                for i, row in enumerate(rows):
                    upper = [str(v).strip().upper() if v is not None else "" for v in row]
                    if any("ITEM" in s for s in upper) and any("SQF" in s for s in upper):
                        hdr = upper
                        hdr_idx = i
                        break
                if hdr is not None:
                    idx_item   = _header_col(hdr, ["ITEM #", "ITEM#"])
                    idx_sqf    = _header_col(hdr, ["SQF OR PCS", "SQF"])
                    idx_remark = _header_col(hdr, ["REMARK"])
                    if idx_item is not None and idx_sqf is not None:
                        for row in rows[hdr_idx + 1:]:
                            if not any(v is not None for v in row):
                                continue
                            raw_item = row[idx_item]
                            if raw_item is None:
                                continue
                            item = str(raw_item).strip().upper()
                            if not item or item in ("", "NONE", "NAN"):
                                continue
                            try:
                                sqf = float(row[idx_sqf]) if row[idx_sqf] is not None else 0.0
                            except (TypeError, ValueError):
                                continue
                            remark = str(row[idx_remark]).strip().upper() if (idx_remark is not None and row[idx_remark] is not None) else ""
                            is_ready = "READY" in remark
                            prod_frames.append({
                                "ITEM_NUMBER": item,
                                "SQF": sqf,
                                "IS_READY": is_ready,
                            })

            # ── WATER sheet ─────────────────────────────────────────────────
            if "WATER" in wb.sheetnames:
                rows = list(wb["WATER"].iter_rows(values_only=True))
                hdr = None
                hdr_idx = 0
                for i, row in enumerate(rows):
                    upper = [str(v).strip().upper() if v is not None else "" for v in row]
                    if any("ITEM" in s for s in upper) and any("ETA" in s for s in upper):
                        hdr = upper
                        hdr_idx = i
                        break
                if hdr is not None:
                    idx_item = _header_col(hdr, ["ITEM #", "ITEM#"])
                    idx_sqf  = _header_col(hdr, ["SQF OR PCS", "SQF"])
                    if idx_item is not None and idx_sqf is not None:
                        for row in rows[hdr_idx + 1:]:
                            if not any(v is not None for v in row):
                                continue
                            raw_item = row[idx_item]
                            if raw_item is None:
                                continue
                            item = str(raw_item).strip().upper()
                            if not item or item in ("", "NONE", "NAN"):
                                continue
                            try:
                                sqf = float(row[idx_sqf]) if row[idx_sqf] is not None else 0.0
                            except (TypeError, ValueError):
                                continue
                            water_frames.append({"ITEM_NUMBER": item, "SQF": sqf})

            wb.close()
        except Exception:
            continue

    # Aggregate production/ready
    prod_agg: Dict[str, Dict] = {}
    for r in prod_frames:
        item = r["ITEM_NUMBER"]
        if item not in prod_agg:
            prod_agg[item] = {"PRODUCTION_SF": 0.0, "READY_SF": 0.0}
        if r["IS_READY"]:
            prod_agg[item]["READY_SF"] += r["SQF"]
        else:
            prod_agg[item]["PRODUCTION_SF"] += r["SQF"]

    # Aggregate water
    water_agg: Dict[str, float] = {}
    for r in water_frames:
        water_agg[r["ITEM_NUMBER"]] = water_agg.get(r["ITEM_NUMBER"], 0.0) + r["SQF"]

    all_items = set(prod_agg) | set(water_agg)
    if not all_items:
        return pd.DataFrame()

    result = []
    for item in all_items:
        p = prod_agg.get(item, {"PRODUCTION_SF": 0.0, "READY_SF": 0.0})
        result.append({
            "ITEM_NUMBER":    item,
            "PRODUCTION_SF":  p["PRODUCTION_SF"],
            "READY_SF":       p["READY_SF"],
            "WATER_SF":       water_agg.get(item, 0.0),
        })

    df = pd.DataFrame(result)
    df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    return df


@st.cache_data(ttl=3600)
def _load_collection_details() -> list:
    """
    Load collection shipping / pallet details from the COLLECTION DETAILS sheet
    of the AAA Purchasing Report.  Returns a list of dicts with keys:
        collection, name, header, shipping
    """
    try:
        import openpyxl
        path = "//server/Purchasing/2026 Shipment Reports/AAA Purchasing Report Master v10.xlsm"
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb["COLLECTION DETAILS"]
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:          # skip header row
                continue
            if not row[0]:      # skip blank rows
                continue
            rows.append({
                "collection": str(row[0]).strip() if row[0] else "",
                "name":       str(row[1]).strip() if row[1] else "",
                "header":     str(row[2]).strip() if row[2] else "",
                "shipping":   str(row[3]).strip() if row[3] else "",
            })
        wb.close()
        return rows
    except Exception:
        return []


def _get_collection_details_for(selected_collection: str) -> list:
    """
    Return COLLECTION DETAILS rows matching the dashboard's selected_collection.
    E.g. "ALLORA" → rows for "ALLORA 7.5" and "ALLORA 9.5".
    """
    if not selected_collection or selected_collection == "All Collections":
        return []
    all_details = _load_collection_details()
    sel = selected_collection.strip().upper()
    return [
        d for d in all_details
        if d["collection"].strip().upper() == sel
        or d["collection"].strip().upper().startswith(sel + " ")
        or d["collection"].strip().upper().startswith(sel + "-")
        or d["collection"].strip().upper().startswith(sel + " (")
    ]



def _load_shipping_days() -> Dict[str, int]:
    """Load per-SKU shipping days from the 'Final' sheet of Lead Times.xlsx.

    Only SKUs with a non-null Shipping Days value are included.  These are
    used to compute Due in Inventory when a PO header-level ship date is
    entered in Gartman (POHEAD.PHDDAT) instead of individual line dates.
    Returns {SKU_UPPER: shipping_days_int}.
    """
    path = Path(__file__).resolve().parent / LEAD_TIMES_FILE
    if not path.exists():
        return {}
    try:
        df = pd.read_excel(path, sheet_name="Final", header=0)
        sku_col = df.columns[0]
        days_col = "Shipping Days"
        if days_col not in df.columns:
            return {}
        df = df[[sku_col, days_col]].copy()
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        df[days_col] = pd.to_numeric(df[days_col], errors="coerce")
        df = df.dropna(subset=[days_col])
        return {row[sku_col]: int(row[days_col]) for _, row in df.iterrows()}
    except Exception:
        return {}


def _apply_shipping_days_overrides(df: pd.DataFrame, shipping_days_map: Dict[str, int]) -> pd.DataFrame:
    """Derive inventory dates for rows that use PO header ship dates.

    If the header-derived inventory date sharply disagrees with a valid line
    date, keep the line date. This avoids stale header fields moving a PO into
    the wrong year.
    """
    if not shipping_days_map or df.empty or "EST_SHIP_DATE" not in df.columns:
        return df

    out = df.copy()
    out["ITEM_NUMBER"] = out["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    out["_SHIP_DAYS"] = out["ITEM_NUMBER"].map(shipping_days_map)

    null_as400 = pd.Timestamp("0001-01-01")
    hdr_dates = pd.to_datetime(out["EST_SHIP_DATE"], errors="coerce")
    line_inv = pd.to_datetime(out["DUE_INV"], errors="coerce")
    line_port = pd.to_datetime(out["DUE_PORT"], errors="coerce")

    valid_hdr = hdr_dates.notna() & (hdr_dates > null_as400)
    valid_line_port = line_port.notna() & (line_port > null_as400)
    no_line_inv = line_inv.isna() | (line_inv <= null_as400)
    apply_mask = valid_hdr & out["_SHIP_DAYS"].notna() & no_line_inv

    if apply_mask.any():
        ship_days = pd.to_timedelta(out.loc[apply_mask, "_SHIP_DAYS"].astype(int), unit="D")
        derived_inv = hdr_dates.loc[apply_mask] + ship_days
        line_port_subset = line_port.loc[apply_mask]
        valid_line_subset = valid_line_port.loc[apply_mask]
        conflict = valid_line_subset & ((derived_inv - line_port_subset).abs() > pd.Timedelta(days=45))

        normal_idx = conflict.index[~conflict]
        conflict_idx = conflict.index[conflict]

        if len(normal_idx) > 0:
            out.loc[normal_idx, "DUE_PORT"] = hdr_dates.loc[normal_idx]
            out.loc[normal_idx, "DUE_INV"] = derived_inv.loc[normal_idx]
        if len(conflict_idx) > 0:
            out.loc[conflict_idx, "DUE_INV"] = line_port.loc[conflict_idx]

    return out.drop(columns=["_SHIP_DAYS"])


def load_arrivals(conn, sku_list: List[str]) -> pd.DataFrame:
    """Load open PO arrivals with container notes from POLINE/POTEXT."""
    if not sku_list:
        return pd.DataFrame()
    sku_list_str = _build_in_list(sku_list)
    excluded_vendors_str = _build_in_list(sorted(EXCLUDED_PO_VENDORS))
    sql = f"""
    SELECT
      TRIM(L.PLPO#) AS PO_NUMBER,
      TRIM(L.PLITEM) AS ITEM_NUMBER,
      TRIM(L.PLDESC) AS DESCRIPTION,
      COALESCE(L.PLBLUO, 0) - COALESCE(L.PLBLUR, 0) AS QUANTITY_SF,
      TRIM(H.PHVEND) AS VENDOR_NUMBER,
      TRIM(V.VMNAME) AS VENDOR_NAME,
      TRIM(X.IMCOLLECT) AS COLLECTION,
      L.PLPDAT AS DUE_PORT,
      L.PLDDAT AS DUE_INV,
      H.PHDOI AS ENTRY_DATE,
      H.PHSDAT AS EST_SHIP_DATE,
      MAX(TRIM(P.PTCMT1)) AS PTCMT1,
      MAX(TRIM(P.PTCMT2)) AS PTCMT2
    FROM GSFL2K.POLINE L
    LEFT JOIN GSFL2K.POTEXT P ON P.PTPO# = L.PLPO# AND P.PTCO = L.PLCO
    LEFT JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
    JOIN GSFL2K.ITEMMAST M ON M.IMITEM = L.PLITEM
    LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
    LEFT JOIN GSFL2K.VENDMAST V ON V.VMVEND = H.PHVEND
    WHERE L.PLDELT LIKE '%A%'
      AND TRIM(L.PLITEM) IN ({sku_list_str})
      AND TRIM(L.PLPO#) <> ''
      AND TRIM(L.PLITEM) <> ''
      AND COALESCE(L.PLBLUO, 0) > COALESCE(L.PLBLUR, 0)
      AND TRIM(COALESCE(H.PHVEND, '')) NOT IN ({excluded_vendors_str})
    GROUP BY
      TRIM(L.PLPO#),
      TRIM(L.PLITEM),
      TRIM(L.PLDESC),
      COALESCE(L.PLBLUO, 0) - COALESCE(L.PLBLUR, 0),
      TRIM(H.PHVEND),
      TRIM(V.VMNAME),
      TRIM(X.IMCOLLECT),
      L.PLPDAT,
      L.PLDDAT,
      H.PHDOI,
      H.PHSDAT
    """
    df = _read_sql_silent(sql, conn)
    df["CONTAINER"] = df["PTCMT1"].fillna("").astype(str).str.strip()
    df["CONTAINER"] = np.where(
        df["CONTAINER"] != "",
        df["CONTAINER"],
        df["PTCMT2"].fillna("").astype(str).str.strip(),
    )
    df["CONTAINER"] = df["CONTAINER"].replace("", np.nan)

    df = _apply_shipping_days_overrides(df, _load_shipping_days())
    return df

    # --- Shipping-days override for items that use a PO header ship date ---
    # Some vendors (e.g. Cork) enter dates at the PO header level (POHEAD.PHDDAT)
    # rather than at each line item.  For these items the Lead Times spreadsheet
    # carries a "Shipping Days" value.  When a valid HEADER_DUE_DATE is present
    # and the item has Shipping Days defined, we derive:
    #   DUE_PORT = HEADER_DUE_DATE  (estimated ship/departure date)
    #   DUE_INV  = HEADER_DUE_DATE + Shipping Days
    # --- Shipping-days override for items that use a PO header ship date ---
    # PHSDAT (EST_SHIP_DATE) is the "Due" date entered on the PO header in
    # Gartman (e.g. Cork items where dates are set at the header, not per line).
    # For these items the Lead Times spreadsheet carries a "Shipping Days" value.
    # When a valid EST_SHIP_DATE is present and the item has Shipping Days defined:
    #   DUE_PORT = EST_SHIP_DATE  (estimated departure date)
    #   DUE_INV  = EST_SHIP_DATE + Shipping Days
    shipping_days_map = _load_shipping_days()
    if shipping_days_map and "EST_SHIP_DATE" in df.columns and not df.empty:
        df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
        df["_SHIP_DAYS"] = df["ITEM_NUMBER"].map(shipping_days_map)
        hdr_dates = pd.to_datetime(df["EST_SHIP_DATE"], errors="coerce")
        # AS400 null date renders as year 1 (0001-01-01); treat as missing
        null_as400 = pd.Timestamp("0001-01-01")
        valid_hdr = hdr_dates.notna() & (hdr_dates > null_as400)
        # Only apply when the line-level DUE_INV is absent — if POLINE already
        # carries a specific date, leave it alone.
        line_inv = pd.to_datetime(df["DUE_INV"], errors="coerce")
        no_line_date = line_inv.isna() | (line_inv <= null_as400)
        apply_mask = valid_hdr & df["_SHIP_DAYS"].notna() & no_line_date
        if apply_mask.any():
            df.loc[apply_mask, "DUE_PORT"] = hdr_dates[apply_mask]
            df.loc[apply_mask, "DUE_INV"] = (
                hdr_dates[apply_mask] +
                pd.to_timedelta(df.loc[apply_mask, "_SHIP_DAYS"].astype(int), unit="D")
            )
        df = df.drop(columns=["_SHIP_DAYS"])

    return df

def build_daily_series(sales_df, start_date):
    """Build daily time series"""
    if sales_df.empty:
        return None
    daily_agg = make_return_aware_daily_series(sales_df, date_col='SALES_DATE', qty_col='QTY_SOLD_SF')
    end = pd.Timestamp.today().normalize()
    idx = pd.date_range(start_date, end, freq="D")
    s = daily_agg.set_index('SALES_DATE')['QTY_SOLD_SF'].reindex(idx, fill_value=0.0).astype(float)
    s.index.name = "DATE"
    return s

def build_weekly_series(sales_df, start_date):
    """Build weekly time series - aggregates to week ending on Sunday"""
    if sales_df.empty:
        return None
    
    # Aggregate to return-aware daily series first.
    daily_agg = make_return_aware_daily_series(sales_df, date_col='SALES_DATE', qty_col='QTY_SOLD_SF')
    
    # Create complete daily index
    end = pd.Timestamp.today().normalize()
    daily_idx = pd.date_range(start_date, end, freq="D")
    daily_series = daily_agg.set_index('SALES_DATE')['QTY_SOLD_SF'].reindex(daily_idx, fill_value=0.0)
    
    # Resample to weekly (Sunday week-end)
    weekly_series = daily_series.resample('W-SUN').sum()
    
    # Remove the last week if it's incomplete (partial week)
    if weekly_series.index[-1] > end:
        weekly_series = weekly_series[:-1]
    
    weekly_series.index.name = "WEEK_END"
    return weekly_series.astype(float)

def cap_outliers(series: pd.Series, n_mad: float = 10.0) -> pd.Series:
    """Cap extreme outliers using median and MAD (robust to outliers).

    Uses median and Median Absolute Deviation (MAD) instead of mean/std
    because mean/std are themselves heavily influenced by outliers.
    MAD is converted to a standard deviation equivalent using the
    normal distribution scale factor (1.4826).

    Args:
        series: Time series of demand values
        n_mad: Number of MAD-scaled deviations for the threshold (default 10.0)

    Returns:
        Series with outliers capped at median + n_mad * (1.4826 * MAD)
    """
    if series.empty:
        return series
    non_zero = series[series > 0]
    ref = non_zero if len(non_zero) >= 3 else series
    median = ref.median()
    mad = np.median(np.abs(ref - median))
    if mad == 0:
        upper_bound = ref.quantile(0.95)
        if not np.isfinite(upper_bound) or upper_bound <= 0:
            return series
        reason = "p95 of non-zero demand"
    else:
        # Convert MAD to std-equivalent (for normal distribution, std ~ 1.4826 * MAD)
        std_est = 1.4826 * mad
        upper_bound = median + n_mad * std_est
        reason = f"median + {n_mad} MAD"
    capped = series.clip(upper=upper_bound)
    n_capped = (series > upper_bound).sum()
    if n_capped > 0:
        print(f"    Capped {n_capped} outlier(s) exceeding {upper_bound:,.0f} ({reason})")
    return capped

def wmape(y_true, y_pred):
    """Calculate wMAPE"""
    denom = float(np.sum(np.abs(y_true)))
    return float(np.sum(np.abs(y_true - y_pred)) / denom) * 100 if denom != 0 else float("nan")


def _naive_scale(y_train, seasonal_period=1):
    y_train = np.asarray(y_train, dtype=float)
    n = len(y_train)
    m = int(seasonal_period) if seasonal_period and seasonal_period > 0 else 1
    if n <= m:
        return 0.0, 0.0
    diffs = y_train[m:] - y_train[:-m]
    mae_scale = float(np.mean(np.abs(diffs))) if len(diffs) > 0 else 0.0
    mse_scale = float(np.mean(diffs ** 2)) if len(diffs) > 0 else 0.0
    return mae_scale, mse_scale


def mase(y_true, y_pred, y_train, seasonal_period=1):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mae = float(np.mean(np.abs(y_true - y_pred))) if len(y_true) > 0 else 0.0
    mae_scale, _ = _naive_scale(y_train, seasonal_period)
    if mae_scale == 0:
        return float("inf") if mae > 0 else 0.0
    return mae / mae_scale


def rmsse(y_true, y_pred, y_train, seasonal_period=1):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mse = float(np.mean((y_true - y_pred) ** 2)) if len(y_true) > 0 else 0.0
    _, mse_scale = _naive_scale(y_train, seasonal_period)
    if mse_scale == 0:
        return float("inf") if mse > 0 else 0.0
    return math.sqrt(mse / mse_scale)


def bias_pct(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0
    return float(np.sum(y_pred - y_true)) / denom


def under_forecast_share(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0
    missed = np.maximum(0.0, y_true - y_pred)
    return float(np.sum(missed)) / denom


def compute_forecast_metrics(y_true, y_pred, y_train, seasonal_period=1):
    return {
        "wape": wmape(y_true, y_pred),
        "mase": mase(y_true, y_pred, y_train, seasonal_period),
        "rmsse": rmsse(y_true, y_pred, y_train, seasonal_period),
        "bias_pct": bias_pct(y_true, y_pred),
        "uf_share": under_forecast_share(y_true, y_pred),
    }


def _percentile_scores(values):
    clean = [v for v in values if np.isfinite(v)]
    if not clean:
        return [0.0 for _ in values]
    if len(set(clean)) <= 1:
        return [100.0 if np.isfinite(v) else 0.0 for v in values]
    sorted_vals = sorted(clean)
    scores = []
    for v in values:
        if not np.isfinite(v):
            scores.append(0.0)
            continue
        rank = sorted_vals.index(v)
        if len(sorted_vals) == 1:
            scores.append(100.0)
        else:
            scores.append(100.0 * (1.0 - (rank / (len(sorted_vals) - 1))))
    return scores

def calculate_demand_characteristics(y_series):
    """
    Calculate ADI (Average Demand Interval) and CV^2 (Coefficient of Variation^2)
    Used to classify demand patterns
    
    Returns:
    - adi: Average Demand Interval (avg weeks between non-zero demands)
    - cv2: Coefficient of Variation^2 (variance in demand SIZE)
    - demand_class: 'SMOOTH', 'INTERMITTENT', 'ERRATIC', or 'LUMPY'
    """
    # Find non-zero demands
    non_zero_mask = y_series > 0
    non_zero_values = y_series[non_zero_mask].values
    non_zero_indices = np.where(non_zero_mask)[0]
    
    if len(non_zero_values) < 2:
        return None, None, 'INSUFFICIENT_DATA'
    
    # Calculate ADI (Average Demand Interval)
    # Average number of periods between non-zero demands
    intervals = np.diff(non_zero_indices)
    adi = intervals.mean() if len(intervals) > 0 else 1.0
    
    # Calculate CV^2 (Coefficient of Variation squared)
    # Measures variability in demand SIZE (when non-zero)
    mean_demand = non_zero_values.mean()
    std_demand = non_zero_values.std()
    cv = std_demand / mean_demand if mean_demand > 0 else 0
    cv2 = cv ** 2
    
    # Classify demand pattern (Syntetos et al. 2005)
    # ADI threshold: 1.32 (about weekly vs less frequent)
    # CV^2 threshold: 0.49 (consistent size vs variable)
    
    if adi < 1.32 and cv2 < 0.49:
        demand_class = 'SMOOTH'
    elif adi >= 1.32 and cv2 < 0.49:
        demand_class = 'INTERMITTENT'
    elif adi < 1.32 and cv2 >= 0.49:
        demand_class = 'ERRATIC'
    else:  # adi >= 1.32 and cv2 >= 0.49
        demand_class = 'LUMPY'
    
    return adi, cv2, demand_class


def _build_rolling_origin_splits(n_points: int, demand_class: str, max_folds: int = 4) -> List[Tuple[int, int]]:
    """Create rolling-origin splits as (train_end, val_end)."""
    if n_points < 8:
        return []

    intermittent_classes = {"INTERMITTENT", "ERRATIC", "LUMPY", "INSUFFICIENT_DATA"}
    base_val = 4 if str(demand_class).upper() in intermittent_classes else 8
    val_size = max(2, min(base_val, n_points // 4 if n_points >= 12 else 2))
    min_train = max(8, val_size * 2)

    if n_points < (min_train + val_size):
        train_end = n_points - val_size
        if train_end >= 4:
            return [(train_end, train_end + val_size)]
        return []

    candidates = list(range(min_train, n_points - val_size + 1, val_size))
    if not candidates:
        return []
    if len(candidates) > max_folds:
        candidates = candidates[-max_folds:]
    return [(te, te + val_size) for te in candidates]


def _flooring_scale_ratio_threshold(demand_class: str, abc_class: str = "C") -> float:
    """Upper bound for future-vs-recent mean ratio by demand/ABC class."""
    demand_upper = str(demand_class).upper()
    abc_upper = str(abc_class).upper()
    base = {
        "SMOOTH": 2.4,
        "ERRATIC": 1.9,
        "INTERMITTENT": 1.5,
        "LUMPY": 1.3,
        "INSUFFICIENT_DATA": 1.5,
        "NO_DEMAND": 1.0,
    }.get(demand_upper, 1.8)
    base += {"A": 0.4, "B": 0.2, "C": 0.0}.get(abc_upper, 0.0)
    return base


def _flooring_fallback_recent_cap(y_train: np.ndarray, demand_class: str, abc_class: str = "C") -> float:
    """Cap fallback levels using recent-demand evidence."""
    y = np.asarray(y_train, dtype=float)
    if y.size == 0:
        return 0.0

    recent_n = min(26, y.size)
    recent = y[-recent_n:]
    recent_mean = float(np.mean(recent)) if recent_n > 0 else 0.0
    recent_nonzero = recent[recent > 0]
    p_recent_nonzero = float(np.mean(recent > 0)) if recent_n > 0 else 0.0
    recent_nonzero_mean = float(np.mean(recent_nonzero)) if recent_nonzero.size > 0 else 0.0
    recent_implied = p_recent_nonzero * recent_nonzero_mean
    baseline = max(recent_mean, recent_implied)

    if baseline > 0:
        return baseline * _flooring_scale_ratio_threshold(demand_class, abc_class)

    # No recent demand evidence: force inert fallback to zero.
    return 0.0


def _apply_flooring_fallback_cap(
    raw_level: float, y_train: np.ndarray, demand_class: str, abc_class: str = "C"
) -> float:
    """Apply recent-evidence cap to a scalar fallback demand level."""
    raw = max(float(raw_level), 0.0)
    cap = _flooring_fallback_recent_cap(y_train, demand_class=demand_class, abc_class=abc_class)
    if cap <= 0:
        return 0.0 if raw > 0 else raw
    return min(raw, cap)


def _cap_flooring_forecast_series(
    fc_series: pd.Series, y_historical: pd.Series, demand_class: str, abc_class: str = "C"
) -> Tuple[pd.Series, bool]:
    """
    Cap forecast mean against recent-demand evidence.
    Returns (possibly adjusted forecast, was_adjusted).
    """
    if fc_series is None or len(fc_series) == 0:
        return fc_series, False

    y = np.asarray(getattr(y_historical, "values", y_historical), dtype=float)
    fc = np.asarray(getattr(fc_series, "values", fc_series), dtype=float)
    if fc.size == 0:
        return fc_series, False

    future_mean = float(np.mean(fc))
    if not np.isfinite(future_mean) or future_mean <= 0:
        return fc_series, False

    recent_n = min(26, y.size)
    recent_mean = float(np.mean(y[-recent_n:])) if recent_n > 0 else 0.0
    adjusted = False

    if recent_mean <= 0:
        cap = _flooring_fallback_recent_cap(y, demand_class=demand_class, abc_class=abc_class)
        if cap <= 0:
            fc = np.zeros_like(fc)
            adjusted = True
        elif future_mean > cap:
            fc = fc * (cap / (future_mean + 1e-9))
            adjusted = True
    else:
        ratio_cap = _flooring_scale_ratio_threshold(demand_class, abc_class)
        max_future_mean = recent_mean * ratio_cap
        if future_mean > max_future_mean and max_future_mean >= 0:
            fc = fc * (max_future_mean / (future_mean + 1e-9))
            adjusted = True

    if not adjusted:
        return fc_series, False

    fc = np.maximum(fc, 0.0)
    out = pd.Series(fc, index=fc_series.index, dtype=float)
    return out, True

# ============================================================
# FOUR FORECASTING METHODS
# ============================================================

def forecast_neural_network(y_train, horizon, lags=[1, 2, 4, 8]):
    """Neural Network (MLP) - for weekly data"""
    try:
        from sklearn.neural_network import MLPRegressor
        if len(y_train) < 20:  # Need at least 20 weeks
            return None, "NN: insufficient data"
        X, y = [], []
        for i in range(max(lags), len(y_train)):
            X.append([y_train.iloc[i-lag] for lag in lags])
            y.append(y_train.iloc[i])
        model = MLPRegressor(hidden_layer_sizes=(50, 25), max_iter=1000, random_state=42, 
                            early_stopping=True, n_iter_no_change=20, validation_fraction=0.1)
        model.fit(np.array(X), np.array(y))
        preds, hist = [], y_train.values.tolist()
        for _ in range(horizon):
            x_new = [hist[-lag] for lag in lags]
            pred = max(0.0, model.predict([x_new])[0])
            preds.append(pred)
            hist.append(pred)
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series(preds, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)


def forecast_croston(y_train, horizon, alpha=0.1):
    """
    Croston's Method - Industry standard for intermittent demand
    
    Separately forecasts:
    1. Demand intervals (time between non-zero demands)
    2. Demand sizes (when demand occurs)
    
    Final forecast = (demand size) / (demand interval)
    """
    try:
        y = y_train.values.astype(float)
        
        # Find non-zero demand periods
        non_zero_idx = np.where(y > 0)[0]
        
        if len(non_zero_idx) < 2:
            # Not enough non-zero periods, use average
            avg = y_train.mean()
            idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
            return pd.Series([avg] * horizon, index=idx, dtype=float), None
        
        # Initialize demand size and interval
        demand_sizes = y[non_zero_idx]
        intervals = np.diff(non_zero_idx)
        
        # Start with first values
        z = demand_sizes[0]  # demand size estimate
        p = intervals[0] if len(intervals) > 0 else 1  # interval estimate
        
        # Apply exponential smoothing
        for i in range(1, len(non_zero_idx)):
            z = alpha * demand_sizes[i] + (1 - alpha) * z
            if i < len(intervals):
                p = alpha * intervals[i] + (1 - alpha) * p
        
        # Forecast = demand size / interval
        forecast_value = z / p if p > 0 else z
        
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series([forecast_value] * horizon, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)

def forecast_sba(y_train, horizon, alpha=0.1):
    """
    Syntetos-Boylan Approximation (SBA) - Improved Croston's Method
    
    Adds bias correction to Croston's method for better accuracy
    Based on: Syntetos & Boylan (2005)
    """
    try:
        y = y_train.values.astype(float)
        
        # Find non-zero demand periods
        non_zero_idx = np.where(y > 0)[0]
        
        if len(non_zero_idx) < 2:
            # Not enough non-zero periods, use average
            avg = y_train.mean()
            idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
            return pd.Series([avg] * horizon, index=idx, dtype=float), None
        
        # Initialize demand size and interval
        demand_sizes = y[non_zero_idx]
        intervals = np.diff(non_zero_idx)
        
        # Start with first values
        z = demand_sizes[0]
        p = intervals[0] if len(intervals) > 0 else 1
        
        # Apply exponential smoothing
        for i in range(1, len(non_zero_idx)):
            z = alpha * demand_sizes[i] + (1 - alpha) * z
            if i < len(intervals):
                p = alpha * intervals[i] + (1 - alpha) * p
        
        # SBA correction factor (reduces bias in Croston's)
        correction = 1 - (alpha / 2)
        forecast_value = (z / p) * correction if p > 0 else z
        
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series([forecast_value] * horizon, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)

def forecast_bootstrap(y_train, horizon, n_simulations=1000):
    """
    Bootstrap Forecast - BEST for LUMPY demand
    
    Instead of predicting exact values, samples from historical distribution.
    Captures both demand occurrence probability AND size variability.
    
    Perfect for project-driven, unpredictable demand patterns.
    """
    try:
        # Extract non-zero demands
        non_zero = y_train[y_train > 0].values
        
        if len(non_zero) == 0:
            # No historical demand, forecast zero
            idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
            return pd.Series([0.0] * horizon, index=idx, dtype=float), None
        
        if len(non_zero) < 3:
            # Very sparse data, use simple average
            avg = y_train.mean()
            idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
            return pd.Series([avg] * horizon, index=idx, dtype=float), None
        
        # Calculate probability of non-zero demand
        p_nonzero = len(non_zero) / len(y_train)
        
        # Run simulations
        simulations = []
        for _ in range(n_simulations):
            forecast_sim = []
            for _ in range(horizon):
                if np.random.random() < p_nonzero:
                    # Sample from historical non-zero distribution with replacement
                    demand = np.random.choice(non_zero)
                else:
                    demand = 0.0
                forecast_sim.append(demand)
            simulations.append(forecast_sim)
        
        # Average across simulations for point forecast
        simulations = np.array(simulations)
        forecast_values = simulations.mean(axis=0)
        
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series(forecast_values, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)

def forecast_tsb(y_train, horizon, alpha=0.2, beta=0.1):
    """
    TSB (Teunter-Syntetos-Babai) - PROPERLY IMPLEMENTED for WEEKLY data
    
    TSB is designed for intermittent demand but has a fatal flaw:
    if recent data is mostly zeros, p decays to zero regardless of historical sales.
    
    Fix: Use the ACTUAL probability and size from the data, not exponential smoothing.
    """
    try:
        y = y_train.values.astype(float)
        
        # Calculate actual demand probability and size
        non_zero_mask = y > 0
        non_zero_count = np.sum(non_zero_mask)
        
        if non_zero_count == 0:
            # No demand ever - return zeros
            idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
            return pd.Series([0.0] * horizon, index=idx, dtype=float), None
        
        # Use ACTUAL statistics instead of smoothing (which decays with recent zeros)
        p = non_zero_count / len(y)  # Actual probability of demand
        z = np.mean(y[non_zero_mask])  # Actual average size when demand occurs
        
        # Forecast = probability * average size
        level = p * z
        
        # This should equal y_train.mean() by definition
        # Sanity check anyway
        historical_mean = y_train.mean()
        if abs(level - historical_mean) > 0.01 * historical_mean:
            # Small numerical difference is OK, but if large, use mean
            level = historical_mean
        
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series([level] * horizon, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)

def forecast_ses(y_train, horizon):
    """Simple Exponential Smoothing - for weekly data"""
    try:
        from statsmodels.tsa.holtwinters import SimpleExpSmoothing
        model = SimpleExpSmoothing(y_train).fit(optimized=True)
        fc = model.forecast(horizon)
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series(np.maximum(0.0, fc.values), index=idx), None
    except Exception as e:
        return None, str(e)

def forecast_xgboost(y_train, horizon, lags=[1, 2, 4, 8]):
    """XGBoost - for weekly data"""
    try:
        from xgboost import XGBRegressor
        if len(y_train) < 20:  # Need at least 20 weeks
            return None, "XGB: insufficient data"
        X, y = [], []
        for i in range(max(lags), len(y_train)):
            row = [y_train.iloc[i-lag] for lag in lags]
            # Add week-based temporal features
            row.extend([y_train.index[i].isocalendar()[1], y_train.index[i].month, y_train.index[i].quarter])
            X.append(row)
            y.append(y_train.iloc[i])
        model = XGBRegressor(n_estimators=200, learning_rate=0.05, max_depth=6, random_state=42)
        model.fit(np.array(X), np.array(y))
        preds, hist = [], y_train.copy()
        for i in range(horizon):
            future_date = hist.index[-1] + pd.Timedelta(weeks=1)
            row = [hist.iloc[-lag] for lag in lags]
            row.extend([future_date.isocalendar()[1], future_date.month, future_date.quarter])
            pred = max(0.0, model.predict([row])[0])
            preds.append(pred)
            new_row = pd.Series([pred], index=[future_date])
            hist = pd.concat([hist, new_row])
        idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series(preds, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)



def forecast_random_forest(y_train, horizon, lags=[1, 2, 4, 8]):
    """Random Forest with lagged features"""
    try:
        from sklearn.ensemble import RandomForestRegressor
        if len(y_train) < max(lags) + 10:
            return None, "Insufficient data"
        
        X, y = [], []
        for i in range(max(lags), len(y_train)):
            X.append([y_train.iloc[i-lag] if i >= lag else 0 for lag in lags])
            y.append(y_train.iloc[i])
        
        # n_jobs=1: per-SKU models run inside ProcessPoolExecutor workers; letting
        # RandomForest spawn its own thread pool per worker causes severe CPU
        # oversubscription.  Outer process parallelism already saturates the cores.
        model = RandomForestRegressor(n_estimators=100, max_depth=10, random_state=42, n_jobs=1)
        model.fit(np.array(X), np.array(y))
        
        # Forecast
        preds = []
        hist = y_train.values.tolist()
        for _ in range(horizon):
            x_new = [hist[-lag] if lag <= len(hist) else 0 for lag in lags]
            pred = max(0.0, model.predict([x_new])[0])
            preds.append(pred)
            hist.append(pred)
        
        # Convert to series with proper index
        forecast_idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), 
                                     periods=horizon, freq="W-SUN")
        return pd.Series(preds, index=forecast_idx), None
    except Exception as e:
        return None, str(e)

def forecast_holt_damped(y_train, horizon):
    """Holt's method with damped trend"""
    try:
        from statsmodels.tsa.holtwinters import Holt
        model = Holt(y_train, damped_trend=True)
        fitted = model.fit()
        forecast = fitted.forecast(steps=horizon)
        return pd.Series(np.maximum(0, forecast.values), index=forecast.index), None
    except Exception as e:
        return None, str(e)

def forecast_sarima(y_train, horizon):
    """SARIMA(0,1,1) model"""
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX
        # SARIMA(0,1,1) with no seasonality for weekly data
        model = SARIMAX(y_train, order=(0,1,1), seasonal_order=(0,0,0,0))
        fitted = model.fit(disp=False, maxiter=200)
        forecast = fitted.forecast(steps=horizon)
        
        forecast_idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), 
                                     periods=horizon, freq="W-SUN")
        return pd.Series(np.maximum(0, forecast.values), index=forecast_idx), None
    except Exception as e:
        return None, str(e)

def forecast_hurdle(y_train, horizon):
    """Hurdle model - two-part: P(non-zero) * E(amount|non-zero)"""
    try:
        y = y_train.values.astype(float)
        
        # Part 1: Probability of non-zero
        non_zero_mask = y > 0
        p_nonzero = np.mean(non_zero_mask)
        
        # Part 2: Expected value given non-zero
        non_zero_values = y[non_zero_mask]
        
        if len(non_zero_values) == 0:
            return pd.Series([0] * horizon, 
                           index=pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), 
                                             periods=horizon, freq="W-SUN")), None
        
        # Use exponential smoothing for non-zero values
        if len(non_zero_values) >= 5:
            alpha = 0.3
            smoothed = non_zero_values[0]
            for val in non_zero_values[1:]:
                smoothed = alpha * val + (1 - alpha) * smoothed
            expected_nonzero = smoothed
        else:
            expected_nonzero = non_zero_values.mean()
        
        # Combine: P(Y>0) * E(Y|Y>0)
        forecast_value = p_nonzero * expected_nonzero
        
        forecast_idx = pd.date_range(y_train.index[-1] + pd.Timedelta(weeks=1), 
                                     periods=horizon, freq="W-SUN")
        return pd.Series([forecast_value] * horizon, index=forecast_idx), None
    except Exception as e:
        return None, str(e)

def select_best_forecast(y_historical, global_model=None, global_feature_cols=None,
                        sku_id=None, sku_stats=None, abc_class: str = "C"):
    """
    Test all candidate models with rolling-origin CV and select the best.

    Models tested:
    1. Neural Network (Local)
    2. XGBoost (Local)
    3. Random Forest (Local)
    4. Holt Damped
    5. SARIMA(0,1,1)
    6. Hurdle
    7. TSB
    8. SBA
    9. Croston
    10. XGBoost (Global) - if available

    Selection uses a composite score (wAPE, MASE, RMSSE, under-forecast share, bias),
    then retrains the winner on full history for final future forecast.
    """

    adi, cv2, demand_class = calculate_demand_characteristics(y_historical)

    if adi is None or cv2 is None:
        _sku_log(f"  Demand Class: {demand_class} (ADI=NA, CV2=NA)")
    else:
        _sku_log(f"  Demand Class: {demand_class} (ADI={adi:.2f}, CV2={cv2:.2f})")

    methods = {
        "Neural Network (Local)": lambda y, h: forecast_neural_network(y, h),
        "XGBoost (Local)": lambda y, h: forecast_xgboost(y, h),
        "Random Forest (Local)": lambda y, h: forecast_random_forest(y, h),
        "Holt (Damped)": lambda y, h: forecast_holt_damped(y, h),
        "SARIMA(0,1,1)": lambda y, h: forecast_sarima(y, h),
        "TSB": lambda y, h: forecast_tsb(y, h),
        "SBA": lambda y, h: forecast_sba(y, h),
        "Croston": lambda y, h: forecast_croston(y, h),
        "Hurdle": lambda y, h: forecast_hurdle(y, h),
    }

    if global_model is not None and sku_id is not None and sku_stats is not None:
        methods["XGBoost (Global)"] = lambda y, h: forecast_global_xgboost(
            global_model, global_feature_cols, y, sku_id, sku_stats, h
        )

    if _is_prophet_available() and len(y_historical) >= 20:
        def _prophet_wrapper(y_series, h):
            try:
                _, fc, _ = _prophet_core(
                    y_series.values,
                    np.array([], dtype=float),
                    h,
                    weekly_dates_train=y_series.index,
                    freq='W',
                )
                if fc is None or len(fc) < h:
                    return None, "Prophet failed"
                idx = pd.date_range(
                    y_series.index[-1] + pd.Timedelta(weeks=1), periods=h, freq="W-SUN"
                )
                return pd.Series(np.maximum(fc[:h], 0), index=idx), None
            except Exception as e:
                return None, str(e)
        methods["Prophet"] = _prophet_wrapper

    candidates = []
    best_method = None
    best_wmape = float("inf")
    best_forecast = None
    best_metrics = {}

    splits = _build_rolling_origin_splits(len(y_historical), demand_class, max_folds=4)
    if not splits and len(y_historical) >= 6:
        val_size = max(2, min(8, len(y_historical) // 3))
        train_end = len(y_historical) - val_size
        if train_end >= 4:
            splits = [(train_end, len(y_historical))]

    _sku_log("  Evaluating ALL forecast methods (rolling-origin CV):")

    for method_name, method_func in methods.items():
        try:
            fold_preds = []
            fold_actuals = []
            valid = True

            if not splits:
                valid = False

            for train_end, val_end in splits:
                y_train_fold = y_historical.iloc[:train_end]
                y_val_fold = y_historical.iloc[train_end:val_end]
                if len(y_train_fold) < 4 or len(y_val_fold) == 0:
                    valid = False
                    break

                forecast_val, err = method_func(y_train_fold, len(y_val_fold))
                if forecast_val is None or err:
                    valid = False
                    break

                forecast_arr = np.asarray(getattr(forecast_val, "values", forecast_val), dtype=float)
                actual_arr = y_val_fold.values.astype(float)
                if len(forecast_arr) != len(actual_arr):
                    valid = False
                    break

                fold_preds.append(forecast_arr)
                fold_actuals.append(actual_arr)

            if not valid or not fold_preds:
                _sku_log(f"    {method_name}: FAILED (rolling CV)")
                continue

            y_cv_pred = np.concatenate(fold_preds)
            y_cv_true = np.concatenate(fold_actuals)
            metrics = compute_forecast_metrics(y_cv_true, y_cv_pred, y_historical.values, seasonal_period=1)
            w = metrics["wape"]
            w_finite = np.isfinite(w)
            w_display = f"{w:.2f}%" if w_finite else "NA"

            forecast_avg = float(np.mean(y_cv_pred)) if len(y_cv_pred) > 0 else 0.0
            historical_avg = float(y_historical.mean())
            ratio = forecast_avg / historical_avg if historical_avg > 0 else 0.0

            _sku_log(
                f"    {method_name}: CV wMAPE={w_display}, "
                f"CV forecast avg={forecast_avg:.2f} (ratio={ratio:.2f}x)"
            )

            if not w_finite:
                adjusted = float("inf")
                bias_note = "no signal"
            elif historical_avg > 0:
                if ratio < 0.5:
                    adjusted = w * 1.5
                    bias_note = "severe under-forecast"
                elif ratio < 0.8:
                    adjusted = w * 1.2
                    bias_note = "under-forecast"
                elif ratio > 2.0:
                    adjusted = w * 1.1
                    bias_note = "over-forecast"
                else:
                    adjusted = w
                    bias_note = "reasonable"
            else:
                adjusted = w
                bias_note = "no history"

            if w_finite:
                candidates.append({
                    "name": method_name,
                    "wmape": w,
                    "adjusted": adjusted,
                    "bias_note": bias_note,
                    "metrics": metrics,
                    "ratio": ratio,
                })

        except Exception as e:
            _sku_log(f"    {method_name}: ERROR ({e})")
            continue

    if candidates:
        if demand_class in ("INTERMITTENT", "ERRATIC", "LUMPY"):
            non_global = [c for c in candidates if c["name"] != "XGBoost (Global)"]
            global_rows = [c for c in candidates if c["name"] == "XGBoost (Global)"]
            if non_global and global_rows:
                best_non_global = min(r["metrics"]["wape"] for r in non_global if np.isfinite(r["metrics"]["wape"]))
                kept = []
                for row in candidates:
                    if row["name"] != "XGBoost (Global)":
                        kept.append(row)
                        continue
                    if row["metrics"]["wape"] <= (best_non_global * 0.90) and (0.5 <= row.get("ratio", 1.0) <= 1.8):
                        kept.append(row)
                    else:
                        _sku_log("    XGBoost (Global): rejected by lumpy/intermittent guardrail")
                if kept:
                    candidates = kept

        uf_values = [c["metrics"]["uf_share"] for c in candidates]
        uf_threshold = np.percentile(uf_values, 80) if len(uf_values) >= 5 else None

        gated = []
        for c in candidates:
            if c["metrics"]["bias_pct"] < -0.15:
                continue
            if uf_threshold is not None and c["metrics"]["uf_share"] > uf_threshold:
                continue
            gated.append(c)

        scored = gated if gated else candidates
        wape_scores = _percentile_scores([r["metrics"]["wape"] for r in scored])
        mase_scores = _percentile_scores([r["metrics"]["mase"] for r in scored])
        rmsse_scores = _percentile_scores([r["metrics"]["rmsse"] for r in scored])
        uf_scores = _percentile_scores([r["metrics"]["uf_share"] for r in scored])
        bias_scores = _percentile_scores([abs(r["metrics"]["bias_pct"]) for r in scored])

        for idx, row in enumerate(scored):
            composite = (
                0.35 * wape_scores[idx]
                + 0.20 * mase_scores[idx]
                + 0.20 * rmsse_scores[idx]
                + 0.15 * uf_scores[idx]
                + 0.10 * bias_scores[idx]
            )
            row["metrics"]["composite_score"] = composite

        best = max(scored, key=lambda x: x["metrics"]["composite_score"])
        best_method = best["name"]
        best_wmape = best["metrics"]["wape"]
        best_metrics = best["metrics"]
    else:
        historical_avg = float(y_historical.mean())
        fallback = _apply_flooring_fallback_cap(
            historical_avg,
            y_historical.values,
            demand_class=demand_class,
            abc_class=abc_class,
        )
        best_forecast = pd.Series(
            [fallback] * FUTURE_FORECAST_WEEKS,
            index=pd.date_range(y_historical.index[-1] + pd.Timedelta(weeks=1), periods=FUTURE_FORECAST_WEEKS, freq="W-SUN"),
        )
        best_method = "Historical Avg (fallback)"
        if fallback < historical_avg:
            best_method = "Historical Avg (fallback capped)"
        best_wmape = float("nan")
        best_metrics = {
            "wape": float("nan"),
            "mase": float("nan"),
            "rmsse": float("nan"),
            "bias_pct": float("nan"),
            "uf_share": float("nan"),
            "composite_score": float("nan"),
        }

    if best_method and best_forecast is None:
        best_forecast, _ = methods[best_method](y_historical, FUTURE_FORECAST_WEEKS)
        if best_forecast is not None:
            best_forecast = pd.Series(
                np.maximum(0.0, np.asarray(getattr(best_forecast, "values", best_forecast), dtype=float)),
                index=pd.date_range(y_historical.index[-1] + pd.Timedelta(weeks=1), periods=FUTURE_FORECAST_WEEKS, freq="W-SUN"),
            )

        if best_wmape > 100:
            _sku_log("    WARNING: All methods have wMAPE > 100% (all performing poorly)")
            _sku_log("    Using historical average as fallback forecast")
            historical_avg = float(y_historical.mean())
            fallback = _apply_flooring_fallback_cap(
                historical_avg,
                y_historical.values,
                demand_class=demand_class,
                abc_class=abc_class,
            )
            best_forecast = pd.Series(
                [fallback] * FUTURE_FORECAST_WEEKS,
                index=pd.date_range(y_historical.index[-1] + pd.Timedelta(weeks=1), periods=FUTURE_FORECAST_WEEKS, freq="W-SUN"),
            )
            best_method = f"{best_method} (fallback to avg)"
            if fallback < historical_avg:
                best_method = f"{best_method} + cap"

    if best_forecast is None:
        historical_avg = float(y_historical.mean())
        fallback = _apply_flooring_fallback_cap(
            historical_avg,
            y_historical.values,
            demand_class=demand_class,
            abc_class=abc_class,
        )
        best_forecast = pd.Series(
            [fallback] * FUTURE_FORECAST_WEEKS,
            index=pd.date_range(y_historical.index[-1] + pd.Timedelta(weeks=1), periods=FUTURE_FORECAST_WEEKS, freq="W-SUN"),
        )
        if not best_method:
            best_method = "Historical Avg (fallback)"

    best_forecast, was_capped = _cap_flooring_forecast_series(
        best_forecast, y_historical, demand_class=demand_class, abc_class=abc_class
    )
    if was_capped:
        best_method = f"{best_method} + drift_cap"

    return best_method, best_wmape, best_forecast, adi, cv2, demand_class, best_metrics


# ============================================================
# GLOBAL XGBOOST MODEL - Train on ALL SKUs together
# ============================================================

def build_global_training_data(conn, sku_master, cutoff_date):
    """
    Load historical data for ALL SKUs and create a unified training dataset
    Each SKU uses data from its first sale month forward
    Returns: DataFrame with features for global model
    """
    print("\n" + "="*70)
    print("BUILDING GLOBAL MODEL DATASET")
    print(f"Processing SKUs with sales since {cutoff_date.date()}")
    print("Each SKU uses data from its first sale month forward")
    print("="*70)
    
    all_data = []
    sku_encodings = {}
    
    for idx, row in sku_master.iterrows():
        sku = row['ITEM_NUMBER']
        
        # Load sales from cutoff to find first sale
        sales_df_temp = load_sales_history(conn, sku, cutoff_date)
        first_sale_month = find_first_sale_month(sales_df_temp)
        
        if first_sale_month is None:
            continue
        
        # Load sales from first sale month forward (weekly pipeline)
        sales_df = load_sales_history(conn, sku, first_sale_month)
        y_historical = build_weekly_series(sales_df, first_sale_month)

        if y_historical is None or len(y_historical) < 24:
            continue

        # Cap extreme outliers before using for model training
        y_historical = cap_outliers(y_historical, n_mad=10.0)

        # Assign SKU encoding
        sku_encodings[sku] = len(sku_encodings)
        sku_id = sku_encodings[sku]
        
        # Calculate SKU-level statistics for features
        total_demand = y_historical.sum()
        avg_demand = y_historical.mean()
        std_demand = y_historical.std()
        zero_pct = (y_historical == 0).sum() / len(y_historical)
        
        # Create weekly features for each time point
        for i in range(8, len(y_historical)):  # Need at least 8 weeks of lags
            dt = y_historical.index[i]
            record = {
                'sku_id': sku_id,
                'target': y_historical.iloc[i],
                'date': y_historical.index[i],
                # Lag features
                'lag_1': y_historical.iloc[i-1],
                'lag_2': y_historical.iloc[i-2],
                'lag_4': y_historical.iloc[i-4],
                'lag_8': y_historical.iloc[i-8],
                # Rolling averages
                'roll_4': y_historical.iloc[i-4:i].mean(),
                'roll_8': y_historical.iloc[i-8:i].mean(),
                # Time features
                'weekofyear': int(dt.isocalendar().week),
                'month': dt.month,
                'quarter': dt.quarter,
                # SKU-level features
                'sku_avg_demand': avg_demand,
                'sku_std_demand': std_demand,
                'sku_zero_pct': zero_pct,
            }
            all_data.append(record)
        
        if (idx + 1) % 50 == 0:
            print(f"  Processed {idx + 1}/{len(sku_master)} SKUs...")
    
    df = pd.DataFrame(all_data)
    print(f"\n[OK] Global dataset built: {len(df):,} records from {len(sku_encodings)} SKUs")
    
    return df, sku_encodings

def train_global_xgboost(df_global):
    """
    Train a single XGBoost model on all SKUs combined
    """
    from xgboost import XGBRegressor
    print("\nTraining global XGBoost model...")
    
    # Features
    feature_cols = ['sku_id', 'lag_1', 'lag_2', 'lag_4', 'lag_8',
                    'roll_4', 'roll_8',
                    'weekofyear', 'month', 'quarter',
                    'sku_avg_demand', 'sku_std_demand', 'sku_zero_pct']
    
    X = df_global[feature_cols].values
    y = df_global['target'].values
    
    # Split into train/validation
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    
    # Train model
    model = XGBRegressor(
        n_estimators=500,
        learning_rate=0.05,
        max_depth=8,
        min_child_weight=3,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1
    )
    
    model.fit(X_train, y_train)
    
    # Evaluate
    y_pred = model.predict(X_val)
    val_wmape = wmape(y_val, y_pred)
    
    print(f"[OK] Global model trained: wMAPE={val_wmape:.2f}%")
    
    return model, feature_cols

def forecast_global_xgboost(model, feature_cols, y_historical, sku_id, sku_stats, horizon):
    """
    Generate forecast for a specific SKU using the global model
    """
    try:
        preds = []
        hist = y_historical.copy()
        
        for i in range(horizon):
            future_date = hist.index[-1] + pd.Timedelta(weeks=1)
            
            # Build feature vector
            features = {
                'sku_id': sku_id,
                'lag_1': hist.iloc[-1],
                'lag_2': hist.iloc[-2] if len(hist) >= 2 else hist.iloc[-1],
                'lag_4': hist.iloc[-4] if len(hist) >= 4 else hist.iloc[-1],
                'lag_8': hist.iloc[-8] if len(hist) >= 8 else hist.iloc[0],
                'roll_4': hist.iloc[-4:].mean() if len(hist) >= 4 else hist.mean(),
                'roll_8': hist.iloc[-8:].mean() if len(hist) >= 8 else hist.mean(),
                'weekofyear': int(future_date.isocalendar().week),
                'month': future_date.month,
                'quarter': future_date.quarter,
                'sku_avg_demand': sku_stats['avg_demand'],
                'sku_std_demand': sku_stats['std_demand'],
                'sku_zero_pct': sku_stats['zero_pct'],
            }
            
            X_new = np.array([[features[col] for col in feature_cols]])
            pred = max(0.0, model.predict(X_new)[0])
            preds.append(pred)
            
            # Add prediction to history for next iteration
            new_row = pd.Series([pred], index=[future_date])
            hist = pd.concat([hist, new_row])
        
        idx = pd.date_range(y_historical.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
        return pd.Series(preds, index=idx, dtype=float), None
    except Exception as e:
        return None, str(e)

def calculate_inventory_metrics(y_historical_weekly, lead_time_days):
    """
    Calculate metrics using WEEKLY data
    
    Parameters:
    - y_historical_weekly: Weekly demand series
    - lead_time_days: Lead time in days
    
    Returns inventory metrics in SF units
    """
    # Total demand (in SF)
    total_demand = y_historical_weekly.sum()
    total_weeks = len(y_historical_weekly)
    total_months = total_weeks / WEEKS_PER_MONTH
    
    # Convert lead time to weeks
    lead_time_weeks = lead_time_days / DAYS_PER_WEEK
    
    # Weekly demand statistics
    weekly_mean_demand = total_demand / total_weeks if total_weeks > 0 else 0
    weekly_std_demand = y_historical_weekly.std()  # Actual std from weekly data
    
    # Monthly demand (for reporting)
    avg_monthly_demand = total_demand / total_months if total_months > 0 else 0
    
    # Daily equivalents (for display only)
    daily_mean_demand = weekly_mean_demand / DAYS_PER_WEEK
    
    # Lead time demand (in SF)
    lead_time_mean_demand = weekly_mean_demand * lead_time_weeks
    
    # Standard deviation over lead time
    # Formula: sigma_LT = sigma_weekly * sqrt(LT_weeks)
    sd_demand_over_lt = weekly_std_demand * np.sqrt(lead_time_weeks)
    
    # Monthly standard deviation
    sd_demand_monthly = weekly_std_demand * np.sqrt(WEEKS_PER_MONTH)
    
    # Safety stock base: z × σ_LT  (no cap applied here — cap is enforced in
    # process_sku() after lumpy and ABC uplifts so they cannot bypass the limit)
    safety_stock = Z_SCORE * sd_demand_over_lt

    # Reorder point (in SF) = Lead time demand + Safety stock
    reorder_point = lead_time_mean_demand + safety_stock
    
    # Base reorder quantity (lead time cover from historical mean).
    # process_sku() overrides this with the forecast mean and the correct
    # (L + T) review period after safety stock is finalized — do not add
    # a phantom review period here.
    reorder_quantity = weekly_mean_demand * lead_time_weeks

    return {
        'avg_monthly_demand': avg_monthly_demand,
        'avg_weekly_demand': weekly_mean_demand,
        'daily_mean_demand': daily_mean_demand,
        'weekly_std_demand': weekly_std_demand,
        'lead_time_days': lead_time_days,
        'lead_time_weeks': lead_time_weeks,
        'lead_time_mean_demand': lead_time_mean_demand,
        'sd_demand_over_lt': sd_demand_over_lt,
        'sd_demand_monthly': sd_demand_monthly,
        'safety_stock': safety_stock,
        'reorder_point': reorder_point,
        'reorder_quantity': reorder_quantity,
    }

def simulate_monthly_projection(inventory_position, weekly_forecast, reorder_point, reorder_qty,
                                sales_df, as_of_date, safety_stock, months_ahead=12):
    """
    Simulate monthly inventory starting at current month.

    - Historical rows are handled outside this function.
    - Adds a catchup row for the current month (MTD actual + forecast remainder).
    - Forecast rows cover the next `months_ahead` months.
    """
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    else:
        as_of_date = pd.Timestamp(as_of_date).normalize()

    month_start = as_of_date.replace(day=1)
    month_end = (month_start + pd.offsets.MonthEnd(0)).normalize()
    as_of_end = as_of_date + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    # MTD actuals for current month (only through today)
    sales_df = sales_df.copy()
    sales_df['SALES_DATE'] = pd.to_datetime(sales_df['SALES_DATE'], errors='coerce')
    sales_df = sales_df[sales_df['SALES_DATE'].notna()]
    sales_df = sales_df[sales_df['SALES_DATE'] <= as_of_end]
    mtd_mask = (sales_df['SALES_DATE'] >= month_start) & (sales_df['SALES_DATE'] <= as_of_end)
    mtd_actual = float(sales_df.loc[mtd_mask, 'QTY_SOLD_SF'].sum())

    remaining_days = max(0, (month_end - as_of_date).days)
    remainder_fc = float(weekly_forecast.mean()) * (remaining_days / DAYS_PER_WEEK)
    total_month_demand = mtd_actual + remainder_fc

    # Convert weekly forecast to daily
    daily_idx = pd.date_range(
        weekly_forecast.index[0],
        weekly_forecast.index[-1] + pd.Timedelta(days=6),
        freq='D'
    )
    daily_forecast = pd.Series(index=daily_idx, dtype=float)
    for week_end, week_demand in weekly_forecast.items():
        week_start = week_end - pd.Timedelta(days=6)
        mask = (daily_idx >= week_start) & (daily_idx <= week_end)
        daily_forecast.loc[mask] = week_demand / DAYS_PER_WEEK

    df_forecast = pd.DataFrame({'Date': daily_forecast.index, 'Demand': daily_forecast.values})
    df_forecast['Month'] = pd.to_datetime(df_forecast['Date']).dt.to_period('M').dt.to_timestamp()
    monthly_demand = df_forecast.groupby('Month')['Demand'].sum().reset_index()

    # Build forecast months starting next month
    start_fc_month = (month_start + pd.offsets.MonthBegin(1)).normalize()
    monthly_demand = monthly_demand[monthly_demand['Month'] >= start_fc_month]
    monthly_demand = monthly_demand.sort_values('Month').head(months_ahead)
    if len(monthly_demand) < months_ahead:
        avg_month_demand = float(weekly_forecast.mean()) * WEEKS_PER_MONTH if len(weekly_forecast) else 0.0
        last_month = monthly_demand['Month'].max() if not monthly_demand.empty else (start_fc_month - pd.offsets.MonthBegin(1))
        extra_months = pd.date_range(
            last_month + pd.offsets.MonthBegin(1),
            periods=months_ahead - len(monthly_demand),
            freq="MS",
        )
        extra = pd.DataFrame({"Month": extra_months, "Demand": avg_month_demand})
        monthly_demand = pd.concat([monthly_demand, extra], ignore_index=True)

    projections = []
    inventory_level = inventory_position

    # Catchup row for current month
    order_up_to = reorder_point + reorder_qty  # target inventory level (S)
    beginning_inv = inventory_level
    reorder_triggered = (beginning_inv - total_month_demand) < reorder_point
    if reorder_triggered:
        actual_order = max(reorder_qty, order_up_to - beginning_inv)
        inventory_level += actual_order
    else:
        actual_order = 0
    ending_inv = max(0, inventory_level - total_month_demand)
    projections.append({
        'Month': month_start,
        'Historical Demand': mtd_actual,
        'Safety Stock': safety_stock,
        'Beginning Inventory': beginning_inv,
        'Forecast': remainder_fc,
        'Order Quantity': actual_order,
        'Ending Inventory': ending_inv,
        'Row_Type': 'CATCHUP'
    })
    inventory_level = ending_inv

    for _, row in monthly_demand.iterrows():
        month = row['Month']
        demand = row['Demand']

        beginning_inv = inventory_level
        reorder_triggered = beginning_inv <= reorder_point
        if reorder_triggered:
            reorder_qty_applied = max(reorder_qty, order_up_to - beginning_inv)
            inventory_level += reorder_qty_applied
        else:
            reorder_qty_applied = 0

        ending_inv = max(0, inventory_level - demand)

        projections.append({
            'Month': month,
            'Historical Demand': np.nan,
            'Safety Stock': safety_stock,
            'Beginning Inventory': beginning_inv,
            'Forecast': demand,
            'Order Quantity': reorder_qty_applied,
            'Ending Inventory': ending_inv,
            'Row_Type': 'FCST'
        })

        inventory_level = ending_inv

    return pd.DataFrame(projections)

def find_first_sale_month(sales_df):
    """
    Find the first month with non-zero sales
    Returns: Timestamp of first day of that month, or None if no sales
    """
    if sales_df.empty:
        return None
    
    # Filter to only non-zero sales
    sales_with_qty = sales_df[sales_df['QTY_SOLD_SF'] > 0].copy()
    
    if sales_with_qty.empty:
        return None
    
    # Get the earliest sale date
    first_sale_date = sales_with_qty['SALES_DATE'].min()
    
    # Return the first day of that month
    return pd.Timestamp(year=first_sale_date.year, month=first_sale_date.month, day=1)


def _cold_start_weekly_forecast(y_historical_weekly: pd.Series, horizon: int) -> Tuple[pd.Series, str, Dict[str, float]]:
    """Conservative fallback for sparse weekly history."""
    y = y_historical_weekly.values.astype(float)
    if len(y) == 0:
        level = 0.0
        method = "COLD_START_ZERO"
    else:
        recent = y[-min(4, len(y)):]
        trailing_zero_weeks = 0
        for val in y[::-1]:
            if val <= 0:
                trailing_zero_weeks += 1
            else:
                break

        zero_threshold = min(8, max(4, len(y) // 2))
        if trailing_zero_weeks >= zero_threshold:
            level = 0.0
            method = "COLD_START_RECENT_ZERO"
        elif float(np.sum(recent)) == 0.0:
            non_zero = y[y > 0]
            if len(non_zero) == 0:
                level = 0.0
                method = "COLD_START_RECENT_ZERO"
            else:
                overall_mean = float(np.mean(y))
                nonzero_mean = float(np.mean(non_zero))
                p_nonzero = float(np.mean(y > 0))
                conservative_mean = min(overall_mean, nonzero_mean * p_nonzero)
                decay = max(0.15, 1.0 - (trailing_zero_weeks / max(1, zero_threshold)))
                level = conservative_mean * decay
                method = "COLD_START_TRAILING_ZERO_DECAY"
        else:
            non_zero = y[y > 0]
            overall_mean = float(np.mean(y))
            nonzero_mean = float(np.mean(non_zero)) if len(non_zero) > 0 else overall_mean
            p_nonzero = float(np.mean(y > 0))
            conservative_mean = min(overall_mean, nonzero_mean * p_nonzero)
            level = min(conservative_mean, float(np.max(recent)))
            method = "COLD_START_CONSERVATIVE_MEAN"

    idx = pd.date_range(y_historical_weekly.index[-1] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN")
    forecast = pd.Series([max(0.0, level)] * horizon, index=idx, dtype=float)
    nan = float("nan")
    metrics = {
        "wape": nan,
        "mase": nan,
        "rmsse": nan,
        "bias_pct": nan,
        "uf_share": nan,
        "composite_score": 0.0,
    }
    return forecast, method, metrics

def _make_no_history_stub(sku: str, sku_row, lead_time_days: float, lt_source: str):
    """
    Build a zero-demand metrics stub for a strip SKU that exists in Gartman but
    has no SF sales history.  The stub populates the Niko dashboard with the
    item's current inventory position and zeros for all demand/reorder fields,
    making it visible so a buyer can review it manually.

    Returns (metrics_dict, empty_monthly_df) — the same shape as process_sku().
    """
    avail  = float(sku_row.get('AVAILABLE_SF', 0) or 0)
    on_po  = float(sku_row.get('ON_PO_SF', 0) or 0)
    bo     = float(sku_row.get('BACKORDER_SF', 0) or 0)
    inv_pos = avail + on_po - bo
    lt_weeks = lead_time_days / DAYS_PER_WEEK

    metrics = {
        'sku':                          sku,
        'description':                  str(sku_row.get('DESCRIPTION', '') or ''),
        'vendor_number':                str(sku_row.get('VENDOR_NUMBER', '') or ''),
        'vendor_name':                  str(sku_row.get('VENDOR_NAME', '') or ''),
        'collection':                   str(sku_row.get('COLLECTION', '') or ''),
        'available_sf':                 avail,
        'on_po_sf':                     on_po,
        'backorder_sf':                 bo,
        'inventory_position':           inv_pos,
        'lead_time_days':               lead_time_days,
        'lead_time_source':             lt_source,
        'demand_class':                 'NO_HISTORY',
        'adi':                          None,
        'cv2':                          None,
        'forecast_method':              'NO_HISTORY',
        'forecast_wmape':               None,
        'forecast_wape':                None,
        'forecast_mase':                None,
        'forecast_rmsse':               None,
        'forecast_bias_pct':            None,
        'forecast_uf_share':            None,
        'forecast_composite_score':     None,
        'weekly_fc_mean':               0.0,
        'first_reorder_month':          'None',
        'avg_monthly_demand':           0.0,
        'avg_weekly_demand':            0.0,
        'daily_mean_demand':            0.0,
        'weekly_std_demand':            0.0,
        'lead_time_weeks':              lt_weeks,
        'lead_time_mean_demand':        0.0,
        'sd_demand_over_lt':            0.0,
        'sd_demand_monthly':            0.0,
        'safety_stock':                 0.0,
        'reorder_point':                0.0,
        'order_up_to_level':            0.0,
        'reorder_quantity':             0.0,
        'sku_abc':                      'C',
        'ss_base':                      0.0,
        'ss_lumpy':                     0.0,
        'ss_uplift_raw':                0.0,
        'ss_uplift_applied_pre_budget': 0.0,
    }

    empty_monthly = pd.DataFrame(columns=[
        'Month', 'Historical Demand', 'Safety Stock',
        'Beginning Inventory', 'Forecast', 'Order Quantity',
        'Ending Inventory', 'Row_Type',
        'vendor_number', 'vendor_name', 'collection', 'description',
    ])
    return metrics, empty_monthly


def process_sku(conn, sku_row, lead_times_excel, abc_map, global_model=None, global_feature_cols=None,
                sku_encodings=None, strip_skus=None, as_of_date=None, sales_raw_df=None,
                po_arrivals_df=None):
    """Process a single SKU - optionally using global model.

    When sales_raw_df is provided (a raw (SALES_DATE, QTY_SOLD_SF) DataFrame from
    bulk_load_sales_history), conn is not used for sales queries and may be None.
    This is the offline/parallel path used by _sku_worker_fn.

    po_arrivals_df: Optional subset of load_arrivals() output for this SKU.
        Contains DUE_INV (preferred) and DUE_PORT fallback columns.
        Used to build a per-PO inbound schedule so the simulation places each
        container at its actual expected arrival date rather than a single lump
        at the generic lead time. Open POs without a usable Date to Inventory
        or Date to Port are excluded from the projection until they are dated.
    """
    sku = sku_row['ITEM_NUMBER']
    print(f"\n  Processing SKU: {sku}")

    # Lead time
    if strip_skus and any(part.strip() in strip_skus for part in sku.split('/')):
        lead_time_days = 35.0
        lt_source = "Strip Standard"
    elif sku in lead_times_excel:
        lead_time_days = lead_times_excel[sku]
        lt_source = "Excel"
    else:
        lead_time_days = float(sku_row.get('LEAD_TIME_EFFECTIVE', sku_row.get('LEAD_TIME_IMLT', 30.0)))
        lt_source = str(sku_row.get('LEAD_TIME_SOURCE_FALLBACK', "Gartman"))
    
    # Step 1: Load sales from cutoff date to find first actual sale
    cutoff_date = pd.Timestamp(CUTOFF_DATE)
    _empty_raw = pd.DataFrame(columns=["SALES_DATE", "QTY_SOLD_SF"])
    if sales_raw_df is not None:
        # Offline mode: caller supplied bulk-loaded raw data — no DB access needed
        _raw_df = sales_raw_df if not sales_raw_df.empty else _empty_raw
        sales_df_temp = make_return_aware_daily_series(_raw_df, "SALES_DATE", "QTY_SOLD_SF")
    else:
        sales_df_temp = load_sales_history(conn, sku, cutoff_date)
        _raw_df = None

    # Find first sale month
    first_sale_month = find_first_sale_month(sales_df_temp)
    
    if first_sale_month is None:
        if strip_skus and sku in strip_skus:
            # Strip SKU with no SF history — return a zero-demand stub so it
            # appears in the Niko dashboard (IP shown, no reorder recommendation
            # until actual demand data is present in Gartman).
            print(f"    No SF sales history — creating no-history stub for strip SKU")
            return _make_no_history_stub(sku, sku_row, lead_time_days, lt_source)
        print(f"    SKIPPED: No sales found since {CUTOFF_DATE}")
        return None

    # Use first sale month as start date
    start_date = first_sale_month

    # Step 2: Load sales from first sale month forward and aggregate to WEEKLY
    if sales_raw_df is not None:
        # Offline mode: slice the pre-loaded raw data from first_sale_month forward
        raw_from_start = (
            _raw_df[_raw_df["SALES_DATE"] >= start_date].copy()
            if not _raw_df.empty else _raw_df
        )
        sales_df = make_return_aware_daily_series(raw_from_start, "SALES_DATE", "QTY_SOLD_SF")
    else:
        sales_df = load_sales_history(conn, sku, start_date)
    y_historical_weekly = build_weekly_series(sales_df, start_date)

    if y_historical_weekly is None or len(y_historical_weekly) < 4:
        if strip_skus and sku in strip_skus:
            print(f"    Insufficient SF history ({len(y_historical_weekly) if y_historical_weekly is not None else 0} weeks) — creating no-history stub for strip SKU")
            return _make_no_history_stub(sku, sku_row, lead_time_days, lt_source)
        print(f"    SKIPPED: Insufficient data (need at least 4 weeks)")
        return None

    # Cap extreme outliers (e.g., data entry errors) before forecasting
    y_historical_weekly = cap_outliers(y_historical_weekly, n_mad=10.0)
    
    # Calculate statistics
    total_weeks = len(y_historical_weekly)
    total_demand = y_historical_weekly.sum()
    weekly_avg = y_historical_weekly.mean()
    daily_avg = weekly_avg / 7
    non_zero_weeks = (y_historical_weekly > 0).sum()
    non_zero_pct = 100 * non_zero_weeks / total_weeks
    
    _sku_log(f"  History: {total_weeks} weeks, Total: {total_demand:.2f} SF")
    _sku_log(f"  Weekly avg: {weekly_avg:.2f} SF/week, Daily avg: {daily_avg:.2f} SF/day")
    _sku_log(f"  Non-zero weeks: {non_zero_weeks} ({non_zero_pct:.1f}%)")

    sku_abc = abc_map.get(sku, "C")  # Default to C if not in map
    
    # Prepare parameters for global model if available
    sku_id = None
    sku_stats = None
    if global_model is not None and sku_encodings is not None and sku in sku_encodings:
        sku_id = sku_encodings[sku]
        sku_stats = {
            'avg_demand': y_historical_weekly.mean(),
            'std_demand': y_historical_weekly.std(),
            'zero_pct': (y_historical_weekly == 0).sum() / len(y_historical_weekly)
        }
    
    # Select best forecast method - includes demand classification.
    if len(y_historical_weekly) < 20:
        adi, cv2, demand_class = calculate_demand_characteristics(y_historical_weekly)
        weekly_forecast, best_method, best_metrics = _cold_start_weekly_forecast(
            y_historical_weekly, FUTURE_FORECAST_WEEKS
        )
        best_wmape = float("nan")
        print(f"    Sparse history ({len(y_historical_weekly)} weeks) -> using conservative fallback")
    else:
        best_method, best_wmape, weekly_forecast, adi, cv2, demand_class, best_metrics = select_best_forecast(
            y_historical_weekly, global_model, global_feature_cols, sku_id, sku_stats, abc_class=sku_abc
        )
    
    if weekly_forecast is None:
        print(f"    SKIPPED: All methods failed")
        return None

    # Keep final output forecast scale anchored to recent evidence.
    weekly_forecast, drift_capped = _cap_flooring_forecast_series(
        weekly_forecast, y_historical_weekly, demand_class=demand_class, abc_class=sku_abc
    )
    if drift_capped:
        best_method = f"{best_method} + drift_cap"
    
    adi_display = f"{adi:.2f}" if adi is not None else "NA"
    cv2_display = f"{cv2:.2f}" if cv2 is not None else "NA"
    print(f"    Demand pattern: {demand_class} (ADI={adi_display}, CV\u00b2={cv2_display})")
    composite = best_metrics.get('composite_score', float("nan"))
    wmape_text = f"{best_wmape:.1f}%" if np.isfinite(best_wmape) else "NA"
    print(f"    Best method: {best_method} (Score={composite:.1f}, wMAPE={wmape_text})")
    
    # Check forecast sanity (compare weekly averages)
    forecast_weekly_avg = weekly_forecast.mean()
    historical_weekly_avg = y_historical_weekly.mean()
    forecast_ratio = forecast_weekly_avg / historical_weekly_avg if historical_weekly_avg > 0 else 0
    
    # Convert to daily for display
    forecast_daily_avg = forecast_weekly_avg / 7
    total_52_weeks = weekly_forecast.sum()
    
    _sku_log(f"  Future forecast: Weekly avg={forecast_weekly_avg:.2f} SF/week, Daily avg={forecast_daily_avg:.2f} SF/day")
    _sku_log(f"  Total 52 weeks={total_52_weeks:.2f} SF")
    
    # Warn if forecast is very different from history
    if forecast_ratio < 0.5 or forecast_ratio > 2.0:
        _sku_log(f"  [!]  WARNING: Forecast ({forecast_weekly_avg:.2f}/wk) is {forecast_ratio:.2f}x historical ({historical_weekly_avg:.2f}/wk)")
        _sku_log(f"  [!]  This may indicate poor model fit for highly intermittent demand")
    
    # Calculate metrics using WEEKLY data
    metrics = calculate_inventory_metrics(y_historical_weekly, lead_time_days)

    # Issues 1+7: Replace historical mean with forward-looking forecast mean
    # for all mean-demand-based metrics (ROP, S-level, ROQ).
    # Volatility (std) stays from history — correct for safety stock sizing.
    metrics['avg_weekly_demand'] = forecast_weekly_avg
    metrics['avg_monthly_demand'] = forecast_weekly_avg * WEEKS_PER_MONTH
    metrics['lead_time_mean_demand'] = forecast_weekly_avg * metrics['lead_time_weeks']

    # ABC-BASED SAFETY STOCK TUNING
    ss_base = float(metrics["safety_stock"])
    lead_time_weeks = float(metrics["lead_time_weeks"])
    
    # Only compute lumpy SS for ERRATIC/LUMPY demand
    if demand_class in ("LUMPY", "ERRATIC"):
        ss_lumpy = compute_ss_lumpy_from_percentile(y_historical_weekly, lead_time_weeks, sku_abc, ss_base)
    else:
        ss_lumpy = ss_base
    
    # Apply ABC-based uplift scalar
    ss_final_pre, uplift_raw, uplift_applied = apply_ss_uplift_scalar(ss_base, ss_lumpy, sku_abc)

    # Apply SS cap AFTER all uplifts so lumpy/ABC adjustments cannot bypass the monthly limit.
    # Mirrors the ordering in core/forecasting.py: calculate → uplift → then cap once.
    avg_monthly = float(metrics.get("avg_monthly_demand", 0.0))
    if SS_CAP_MONTHS > 0 and avg_monthly > 0:
        max_ss = avg_monthly * SS_CAP_MONTHS
        ss_final_pre = min(ss_final_pre, max_ss)

    # Store diagnostics for Excel output
    metrics["sku_abc"] = sku_abc
    metrics["ss_base"] = ss_base
    metrics["ss_lumpy"] = ss_lumpy
    metrics["ss_uplift_raw"] = uplift_raw
    metrics["ss_uplift_applied_pre_budget"] = uplift_applied
    metrics["safety_stock"] = ss_final_pre

    # Issue 1: Forecast-based ROP (lead_time_mean_demand already uses forecast mean).
    metrics["reorder_point"] = metrics["lead_time_mean_demand"] + metrics["safety_stock"]

    # S = ROP + one month's forecast demand. Lean but always covers a full
    # average order quantity above the safety threshold, preventing the
    # demand-censoring feedback loop caused by insufficient physical stock.
    metrics["order_up_to_level"] = metrics["reorder_point"] + metrics["avg_monthly_demand"]
    metrics["reorder_quantity"] = forecast_weekly_avg * metrics["lead_time_weeks"]


    # Policy coherence guardrail:
    # if final weekly forecast is zero, do not keep non-zero reorder policy.
    fc_weekly_mean = float(np.nanmean(np.asarray(weekly_forecast.values, dtype=float))) if len(weekly_forecast) else 0.0
    if fc_weekly_mean <= 1e-9:
        metrics["ss_base"] = 0.0
        metrics["ss_lumpy"] = 0.0
        metrics["ss_uplift_raw"] = 0.0
        metrics["ss_uplift_applied_pre_budget"] = 0.0
        metrics["safety_stock"] = 0.0
        metrics["reorder_point"] = 0.0
        metrics["order_up_to_level"] = 0.0
        metrics["reorder_quantity"] = 0.0
        _sku_log("  Policy guard: zero forecast detected -> SS/ROP/ROQ/S forced to 0")
    
    if demand_class in ("LUMPY", "ERRATIC"):
        _sku_log(f"  SS tuning: ABC={sku_abc}, SS_base={ss_base:.2f}, SS_lumpy={ss_lumpy:.2f}, uplift_raw={uplift_raw:.2f}, uplift_applied={uplift_applied:.2f}")
    
    # Inventory position
    inv_position = float(sku_row['INVENTORY_POSITION'])

    # Issue 3: as_of_date is pinned to the start of the forecast run (passed in
    # from the caller) so every SKU in the same batch uses the same reference
    # date rather than drifting with wall-clock time across a long run.
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    else:
        as_of_date = pd.Timestamp(as_of_date).normalize()

    # Build per-PO inbound schedule from open PO data so each container arrives
    # on its real expected date (DUE_INV from Gartman/SharePoint, or a DUE_PORT
    # based estimate) rather than as a single lump at LT days.
    _today_norm = as_of_date  # already normalised above
    _inbound_schedule: Dict[int, float] = {}
    _undated_po_qty = 0.0
    if po_arrivals_df is not None and not po_arrivals_df.empty:
        for _, _po in po_arrivals_df.iterrows():
            _qty = float(_po.get('QUANTITY_SF', 0) or 0)
            if _qty <= 0:
                continue
            _arrival_dt = _resolve_po_projection_arrival_date(_po)
            if pd.isna(_arrival_dt):
                _undated_po_qty += _qty
                continue
            _day_offset = max(0, (_arrival_dt - _today_norm).days)
            _inbound_schedule[_day_offset] = _inbound_schedule.get(_day_offset, 0) + _qty
    if _undated_po_qty > 0:
        _sku_log(f"  Excluding undated open PO qty from projection: {_undated_po_qty:.2f} SF")

    # Monthly projection with catchup (current month) + next 12 months
    # Route through core continuous-review daily simulator (s, S policy).
    # When order_up_to_level is provided the core simulator uses simulate_daily_inventory()
    # which properly separates available/backorder/on_order and applies the (s, S) trigger.
    # inbound_schedule passes per-PO arrival dates so Expected Arrivals can be shown.
    _core_proj_raw = _core_simulate_monthly(
        inventory_position=inv_position,
        weekly_forecast=weekly_forecast.values if hasattr(weekly_forecast, "values") else weekly_forecast,
        reorder_point=metrics['reorder_point'],
        reorder_qty=metrics['reorder_quantity'],
        sales_df=pd.DataFrame(columns=['transaction_date', 'quantity_shipped']),
        as_of_date=as_of_date,
        safety_stock=metrics['safety_stock'],
        months_ahead=12,
        abc_class=sku_abc,
        order_up_to_level=metrics['order_up_to_level'],
        lead_time_weeks=float(metrics['lead_time_weeks']),
        available=float(sku_row.get('AVAILABLE_SF', 0) or 0),
        backorder=float(sku_row.get('BACKORDER_SF', 0) or 0),
        on_order=float(sku_row.get('ON_PO_SF', 0) or 0) if po_arrivals_df is None else 0.0,
        inbound_schedule=_inbound_schedule if _inbound_schedule else None,
    )
    monthly_proj = pd.DataFrame(_core_proj_raw)
    if not monthly_proj.empty:
        monthly_proj['Month'] = pd.to_datetime(monthly_proj['Month'])
    # Core simulator does not produce a Historical Demand column; add it as NaN
    # so downstream code (HIST row concat, _regenerate_projections_post_budget) works.
    monthly_proj['Historical Demand'] = np.nan

    # Add historical monthly demand rows above catchup/forecast rows
    if not sales_df.empty:
        df_hist = sales_df.copy()
        df_hist['Month'] = pd.to_datetime(df_hist['SALES_DATE']).dt.to_period('M').dt.to_timestamp()
        df_hist = df_hist.groupby('Month', as_index=False)['QTY_SOLD_SF'].sum().sort_values('Month')
        df_hist = df_hist[df_hist['Month'] < as_of_date.replace(day=1)]
        if not df_hist.empty:
            hist_series = df_hist.set_index('Month')['QTY_SOLD_SF']
            hist_capped = cap_outliers(hist_series, n_mad=10.0)
            if not np.allclose(hist_series.values, hist_capped.values, equal_nan=True):
                _sku_log(f"  Historical monthly outliers capped for {sku}")
            df_hist['QTY_SOLD_SF'] = hist_capped.values
        hist_rows = []
        for _, r in df_hist.iterrows():
            hist_rows.append({
                'Month': r['Month'],
                'Historical Demand': r['QTY_SOLD_SF'],
                'Safety Stock': metrics['safety_stock'],
                'Beginning Inventory': np.nan,
                'Forecast': np.nan,
                'Order Quantity': np.nan,
                'Ending Inventory': np.nan,
                'Row_Type': 'HIST'
            })
        if hist_rows:
            monthly_proj = pd.concat([pd.DataFrame(hist_rows), monthly_proj], ignore_index=True)

    # Add vendor fields to monthly projections (will be added alongside SKU in final output)
    monthly_proj['vendor_number'] = sku_row['VENDOR_NUMBER'] if 'VENDOR_NUMBER' in sku_row else ''
    monthly_proj['vendor_name'] = sku_row['VENDOR_NAME'] if 'VENDOR_NAME' in sku_row else ''
    monthly_proj['collection'] = sku_row['COLLECTION'] if 'COLLECTION' in sku_row else ''
    monthly_proj['description'] = sku_row['DESCRIPTION'] if 'DESCRIPTION' in sku_row else ''
    
    # First reorder month
    first_reorder = monthly_proj[pd.to_numeric(monthly_proj['Order Quantity'], errors='coerce') > 0]
    first_reorder_month = first_reorder.iloc[0]['Month'] if len(first_reorder) > 0 else 'None'
    
    _sku_log(f"  ROP: {metrics['reorder_point']:.2f}, ROQ: {metrics['reorder_quantity']:.2f}")
    _sku_log(f"  First Reorder: {first_reorder_month}")
    
    result = {
        'sku': sku,
        'description': sku_row['DESCRIPTION'],
        'vendor_number': sku_row['VENDOR_NUMBER'] if 'VENDOR_NUMBER' in sku_row else '',
        'vendor_name': sku_row['VENDOR_NAME'] if 'VENDOR_NAME' in sku_row else '',
        'collection': sku_row['COLLECTION'] if 'COLLECTION' in sku_row else '',
        'available_sf': float(sku_row['AVAILABLE_SF']),
        'on_po_sf': float(sku_row['ON_PO_SF']),
        'backorder_sf': float(sku_row['BACKORDER_SF']),
        'inventory_position': inv_position,
        'lead_time_days': lead_time_days,
        'lead_time_source': lt_source,
        'demand_class': demand_class,
        'adi': adi,
        'cv2': cv2,
        'forecast_method': best_method,
        'forecast_wmape': best_wmape,
        'forecast_wape': best_metrics.get('wape'),
        'forecast_mase': best_metrics.get('mase'),
        'forecast_rmsse': best_metrics.get('rmsse'),
        'forecast_bias_pct': best_metrics.get('bias_pct'),
        'forecast_uf_share': best_metrics.get('uf_share'),
        'forecast_composite_score': best_metrics.get('composite_score'),
        'weekly_fc_mean': float(fc_weekly_mean),
        'first_reorder_month': first_reorder_month,
        **metrics
    }
    
    return result, monthly_proj


def _regenerate_projections_post_budget(df_results, df_monthly):
    """
    Re-run the monthly projection simulation for every SKU using the
    post-budget-cap ROP and safety stock values stored in df_results.

    Called after the global uplift budget scaling is applied.  The original
    projections were generated per-SKU with pre-budget SS/ROP; this corrects
    them so the spreadsheet is internally consistent with the metrics sheet.

    Rules:
      - HIST rows (Beginning Inventory is NaN) are left untouched except that
        the Safety Stock display value is synced to the post-budget figure.
      - CATCHUP row trigger: (beginning_inv - total_month_demand) < new_rop
        where total_month_demand = Historical Demand + Forecast (remainder).
      - FCST row trigger: beginning_inv <= new_rop  (standard (s, S) check).
    """
    updated = []
    for sku in df_results['sku'].unique():
        sku_metrics = df_results[df_results['sku'] == sku].iloc[0]
        sku_monthly = df_monthly[df_monthly['SKU'] == sku].copy()
        if sku_monthly.empty:
            continue

        new_rop = float(sku_metrics['reorder_point'])
        roq = float(sku_metrics['reorder_quantity'])
        inv_pos = float(sku_metrics['inventory_position'])
        new_ss = float(sku_metrics['safety_stock'])
        lt_months = max(1, round(float(sku_metrics.get('lead_time_days', 30)) / 30.44))

        # Sync Safety Stock display column for all rows (HIST included)
        sku_monthly['Safety Stock'] = new_ss

        # HIST rows have NaN Beginning Inventory — leave simulation columns alone
        is_sim = sku_monthly['Beginning Inventory'].notna()
        hist_part = sku_monthly[~is_sim].copy()
        sim_part = sku_monthly[is_sim].copy()

        if sim_part.empty:
            updated.append(sku_monthly)
            continue

        order_up_to = new_rop + roq  # target inventory level (S)
        # Dual-tracking: display_inv = physical on-hand (shown in Beginning/Ending Inventory);
        # ip = full inventory position (on-hand + on-order - backorders) used for reorder trigger.
        # PO arrivals move units from on-order to on-hand — no net IP change (already counted).
        # Sim orders increase IP immediately; their display arrival is scheduled via pending_arrivals.
        available = float(sku_metrics.get('available_sf', inv_pos))
        backorder = float(sku_metrics.get('backorder_sf', 0.0))
        display_inv = max(0.0, available - backorder)
        ip = float(inv_pos)
        pending_arrivals: Dict[int, float] = {}
        for month_pos, (month_idx, month_row) in enumerate(sim_part.iterrows()):
            row_type = month_row['Row_Type'] if 'Row_Type' in sim_part.columns else 'FCST'
            beg_inv = display_inv

            # Use 'PO Arrivals' (real Gartman shipments) to avoid double-counting
            # sim-order arrivals already baked into 'Expected Arrivals'.
            po_arrivals = float(month_row.get('PO Arrivals', 0.0) or 0.0)
            sim_arrivals = pending_arrivals.pop(month_pos, 0.0)
            total_arrivals = po_arrivals + sim_arrivals

            # Reorder trigger uses full IP; sim orders add to IP immediately so future months
            # are less likely to trigger again (matching continuous-review (s,S) semantics).
            reorder_triggered = ip <= new_rop
            order_qty = max(roq, order_up_to - ip) if reorder_triggered else 0.0

            if order_qty > 0:
                ip += order_qty
                arrival_pos = month_pos + lt_months
                pending_arrivals[arrival_pos] = pending_arrivals.get(arrival_pos, 0.0) + order_qty

            if row_type == 'CATCHUP':
                hist_demand = float(month_row['Historical Demand']) if pd.notna(month_row['Historical Demand']) else 0.0
                remainder_fc = float(month_row['Forecast']) if pd.notna(month_row['Forecast']) else 0.0
                total_demand = hist_demand + remainder_fc
                end_inv = max(0.0, display_inv + total_arrivals - total_demand)
                ip = max(0.0, ip - total_demand)
            else:
                demand = float(month_row['Forecast']) if pd.notna(month_row['Forecast']) else 0.0
                end_inv = max(0.0, display_inv + total_arrivals - demand)
                ip = max(0.0, ip - demand)

            display_inv = end_inv

            sim_part.at[month_idx, 'Beginning Inventory'] = beg_inv
            sim_part.at[month_idx, 'Order Quantity'] = order_qty
            sim_part.at[month_idx, 'Expected Arrivals'] = total_arrivals
            sim_part.at[month_idx, 'Ending Inventory'] = end_inv

        # Recombine HIST + simulation rows in chronological order
        sku_monthly_updated = pd.concat([hist_part, sim_part]).sort_values('Month')
        updated.append(sku_monthly_updated)

    return pd.concat(updated, ignore_index=True) if updated else df_monthly


def apply_global_inv_cap(df_results, df_monthly, inv_cap_sf):
    """
    Apply global ending inventory cap using min + flex scaling
    
    Strategy:
    1. Cap individual SKUs by ABC MOI limits first
    2. Calculate minimum safe levels (lead time demand)
    3. Calculate flex inventory (nice-to-have above minimum)
    4. Scale flex proportionally to fit within global cap
    
    Parameters:
    - df_results: DataFrame with inventory metrics per SKU
    - df_monthly: DataFrame with monthly projections per SKU
    - inv_cap_sf: Global inventory cap in SF
    
    Returns:
    - df_results: Updated with adjusted targets
    - df_monthly: Updated with recalculated projections
    """
    
    print(f"\n{'='*70}")
    print("APPLYING GLOBAL ENDING INVENTORY CAP")
    print(f"{'='*70}")
    print(f"  Global Inventory Cap: {inv_cap_sf:,.0f} SF")

    df_hist = pd.DataFrame()
    if 'Row_Type' in df_monthly.columns:
        df_hist = df_monthly[df_monthly['Row_Type'] == 'HIST'].copy()
        df_monthly = df_monthly[df_monthly['Row_Type'] != 'HIST'].copy()
    if 'Month' in df_monthly.columns:
        df_monthly['Month'] = pd.to_datetime(df_monthly['Month'], errors='coerce')
        df_monthly = df_monthly.dropna(subset=['Month'])
    if not df_hist.empty and 'Month' in df_hist.columns:
        df_hist['Month'] = pd.to_datetime(df_hist['Month'], errors='coerce')
    
    # Step 1: Cap individual SKUs by ABC MOI limits
    print(f"\n  Step 1: Applying ABC MOI caps...")
    for idx, row in df_results.iterrows():
        sku_abc = row.get('sku_abc', 'C')
        avg_monthly_demand = row['avg_monthly_demand']
        moi_cap = ABC_MOI_CAPS.get(sku_abc, 3.0)
        
        # Calculate max inventory position for this SKU
        max_inv_position = avg_monthly_demand * moi_cap
        
        # Cap reorder point, but never below lead-time demand
        current_rop = row['reorder_point']
        lead_time_demand = row['lead_time_mean_demand']
        if current_rop > max_inv_position:
            new_rop = max(lead_time_demand, max_inv_position)
            df_results.at[idx, 'reorder_point'] = new_rop
            # Safety stock = ROP - lead time demand (never negative)
            new_ss = max(0, new_rop - lead_time_demand)
            df_results.at[idx, 'safety_stock'] = new_ss
    
    # Recalculate monthly projections with MOI-capped values
    print(f"    Recalculating monthly projections after MOI caps...")
    updated_monthly_projs = []
    
    for sku in df_results['sku'].unique():
        sku_metrics = df_results[df_results['sku'] == sku].iloc[0]
        sku_monthly = df_monthly[df_monthly['SKU'] == sku].copy()
        
        if sku_monthly.empty:
            continue
        
        # Get updated values
        new_rop = sku_metrics['reorder_point']
        roq = sku_metrics['reorder_quantity']
        inv_pos = sku_metrics['inventory_position']
        order_up_to = new_rop + roq  # target inventory level (S)
        lt_months = max(1, round(float(sku_metrics.get('lead_time_days', 30)) / 30.44))

        # Dual-tracking: display_inv = physical on-hand; ip = full inventory position.
        # PO arrivals don't change ip (already counted in inv_pos at start).
        # Sim orders increase ip when placed; their physical arrival is deferred via pending_arrivals.
        available = float(sku_metrics.get('available_sf', inv_pos))
        backorder = float(sku_metrics.get('backorder_sf', 0.0))
        display_inv = max(0.0, available - backorder)
        ip = float(inv_pos)
        pending_arrivals: Dict[int, float] = {}  # {month_position: qty arriving}
        for month_pos, (month_idx, month_row) in enumerate(sku_monthly.iterrows()):
            demand = float(month_row.get('Forecast', 0) or 0)
            po_arrivals = float(month_row.get('PO Arrivals', 0.0) or 0.0)
            sim_arrivals = pending_arrivals.pop(month_pos, 0.0)
            total_arrivals = po_arrivals + sim_arrivals
            beginning_inv = display_inv

            reorder_triggered = ip <= new_rop
            reorder_qty = max(roq, order_up_to - ip) if reorder_triggered else 0.0

            if reorder_qty > 0:
                ip += reorder_qty
                arrival_pos = month_pos + lt_months
                pending_arrivals[arrival_pos] = pending_arrivals.get(arrival_pos, 0.0) + reorder_qty

            end_inv = max(0.0, display_inv + total_arrivals - demand)
            ip = max(0.0, ip - demand)
            display_inv = end_inv

            sku_monthly.at[month_idx, 'Beginning Inventory'] = beginning_inv
            sku_monthly.at[month_idx, 'Order Quantity'] = reorder_qty
            sku_monthly.at[month_idx, 'Expected Arrivals'] = total_arrivals
            sku_monthly.at[month_idx, 'Ending Inventory'] = end_inv

        updated_monthly_projs.append(sku_monthly)
    
    df_monthly = pd.concat(updated_monthly_projs, ignore_index=True) if updated_monthly_projs else df_monthly
    
    # Step 2: Calculate total ending inventory and check against cap
    print(f"\n  Step 2: Checking total ending inventory...")
    
    # Get December 2026 (last month) ending inventory for each SKU
    last_month = df_monthly['Month'].max()
    df_last_month = df_monthly[df_monthly['Month'] == last_month].copy()
    total_ending_inv = df_last_month['Ending Inventory'].sum()
    
    print(f"    Current total ending inventory ({last_month}): {total_ending_inv:,.0f} SF")
    
    if total_ending_inv <= inv_cap_sf:
        print(f"    [OK] Within capacity ({total_ending_inv:,.0f} SF <= {inv_cap_sf:,.0f} SF)")
        print(f"    No further adjustment needed")
        if not df_hist.empty:
            combined = []
            for sku in df_results['sku'].unique():
                hist_rows = df_hist[df_hist['SKU'] == sku]
                fcst_rows = df_monthly[df_monthly['SKU'] == sku]
                if not hist_rows.empty or not fcst_rows.empty:
                    combined.append(pd.concat([hist_rows, fcst_rows], ignore_index=True))
            df_monthly = pd.concat(combined, ignore_index=True) if combined else df_monthly
        return df_results, df_monthly
    
    print(f"    [!]  EXCEEDS capacity by {total_ending_inv - inv_cap_sf:,.0f} SF")
    print(f"\n  Step 3: Applying min + flex scaling...")
    
    # Step 3: Define minimums and flex
    df_results['min_safe_level'] = df_results['lead_time_mean_demand']
    df_results['target_inv'] = df_results['reorder_point'] + (df_results['reorder_quantity'] * 0.5)
    df_results['flex_inv'] = (df_results['target_inv'] - df_results['min_safe_level']).clip(lower=0)
    
    sum_min = df_results['min_safe_level'].sum()
    sum_flex = df_results['flex_inv'].sum()
    
    print(f"    Sum of minimums: {sum_min:,.0f} SF")
    print(f"    Sum of flex: {sum_flex:,.0f} SF")
    print(f"    Total unconstrained: {sum_min + sum_flex:,.0f} SF")
    
    if sum_min >= inv_cap_sf:
        print(f"    [!]  WARNING: Minimums alone exceed capacity!")
        print(f"    Consider reducing lead times or raising capacity")
        # Still apply scaling, but with warning
        capacity_for_flex = inv_cap_sf * 0.5  # Allow 50% for minimums
    else:
        capacity_for_flex = inv_cap_sf - sum_min
    
    # Step 4: Scale flex to fit
    if sum_flex > capacity_for_flex:
        scale = capacity_for_flex / sum_flex
        print(f"    Scaling factor: {scale:.3f}")
        df_results['new_target_inv'] = df_results['min_safe_level'] + (df_results['flex_inv'] * scale)
    else:
        df_results['new_target_inv'] = df_results['target_inv']
        scale = 1.0
    
    # Step 5: Update reorder points to match new targets
    print(f"\n  Step 4: Updating reorder points and safety stocks...")
    for idx, row in df_results.iterrows():
        new_target = row['new_target_inv']
        lead_time_demand = row['lead_time_mean_demand']
        
        # New ROP is the new target minus half a reorder quantity
        new_rop = max(lead_time_demand, new_target - (row['reorder_quantity'] * 0.5))
        new_ss = max(0, new_rop - lead_time_demand)
        
        df_results.at[idx, 'reorder_point'] = new_rop
        df_results.at[idx, 'safety_stock'] = new_ss
    
    # Step 6: Recalculate monthly projections with scaled values
    print(f"    Recalculating monthly projections with scaled targets...")
    final_monthly_projs = []
    
    for sku in df_results['sku'].unique():
        sku_metrics = df_results[df_results['sku'] == sku].iloc[0]
        sku_monthly = df_monthly[df_monthly['SKU'] == sku].copy()
        
        if sku_monthly.empty:
            continue
        
        # Get updated values
        new_rop = sku_metrics['reorder_point']
        roq = sku_metrics['reorder_quantity']
        inv_pos = sku_metrics['inventory_position']
        order_up_to = new_rop + roq  # target inventory level (S)
        lt_months = max(1, round(float(sku_metrics.get('lead_time_days', 30)) / 30.44))

        # Dual-tracking: display_inv = physical on-hand; ip = full inventory position.
        # PO arrivals don't change ip (already counted in inv_pos at start).
        # Sim orders increase ip when placed; physical arrival is deferred via pending_arrivals.
        available = float(sku_metrics.get('available_sf', inv_pos))
        backorder = float(sku_metrics.get('backorder_sf', 0.0))
        display_inv = max(0.0, available - backorder)
        ip = float(inv_pos)
        pending_arrivals: Dict[int, float] = {}
        for month_pos, (month_idx, month_row) in enumerate(sku_monthly.iterrows()):
            demand = float(month_row.get('Forecast', 0) or 0)
            po_arrivals = float(month_row.get('PO Arrivals', 0.0) or 0.0)
            sim_arrivals = pending_arrivals.pop(month_pos, 0.0)
            total_arrivals = po_arrivals + sim_arrivals
            beginning_inv = display_inv

            reorder_triggered = ip <= new_rop
            reorder_qty = max(roq, order_up_to - ip) if reorder_triggered else 0.0

            if reorder_qty > 0:
                ip += reorder_qty
                arrival_pos = month_pos + lt_months
                pending_arrivals[arrival_pos] = pending_arrivals.get(arrival_pos, 0.0) + reorder_qty

            end_inv = max(0.0, display_inv + total_arrivals - demand)
            ip = max(0.0, ip - demand)
            display_inv = end_inv

            sku_monthly.at[month_idx, 'Beginning Inventory'] = beginning_inv
            sku_monthly.at[month_idx, 'Order Quantity'] = reorder_qty
            sku_monthly.at[month_idx, 'Expected Arrivals'] = total_arrivals
            sku_monthly.at[month_idx, 'Ending Inventory'] = end_inv

        final_monthly_projs.append(sku_monthly)
    
    df_monthly = pd.concat(final_monthly_projs, ignore_index=True) if final_monthly_projs else df_monthly
    
    # Verify final total
    df_last_month_final = df_monthly[df_monthly['Month'] == last_month].copy()
    final_total = df_last_month_final['Ending Inventory'].sum()
    
    print(f"\n  [OK] Final total ending inventory ({last_month}): {final_total:,.0f} SF")
    print(f"    Reduction: {total_ending_inv - final_total:,.0f} SF ({100*(total_ending_inv - final_total)/total_ending_inv:.1f}%)")
    print(f"    Within cap: {'YES' if final_total <= inv_cap_sf else 'NO (check minimums)'}")
    print(f"{'='*70}")

    if not df_hist.empty:
        combined = []
        for sku in df_results['sku'].unique():
            hist_rows = df_hist[df_hist['SKU'] == sku]
            fcst_rows = df_monthly[df_monthly['SKU'] == sku]
            if not hist_rows.empty or not fcst_rows.empty:
                combined.append(pd.concat([hist_rows, fcst_rows], ignore_index=True))
        df_monthly = pd.concat(combined, ignore_index=True) if combined else df_monthly

    return df_results, df_monthly


def _reapply_zero_forecast_policy(df_results: pd.DataFrame, df_monthly: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Enforce policy coherence after any global scaling step.
    If weekly forecast mean is zero, keep SS/ROP/ROQ at zero.
    """
    if df_results is None or df_results.empty:
        return df_results, df_monthly
    if "weekly_fc_mean" not in df_results.columns or "sku" not in df_results.columns:
        return df_results, df_monthly

    fc_mean = pd.to_numeric(df_results["weekly_fc_mean"], errors="coerce").fillna(0.0)
    zero_mask = fc_mean <= 1e-9
    if not bool(zero_mask.any()):
        return df_results, df_monthly

    zero_skus = set(df_results.loc[zero_mask, "sku"].astype(str))

    zero_cols = [
        "ss_base",
        "ss_lumpy",
        "ss_uplift_raw",
        "ss_uplift_applied_pre_budget",
        "ss_uplift_applied",
        "safety_stock",
        "reorder_point",
        "reorder_quantity",
    ]
    for col in zero_cols:
        if col in df_results.columns:
            df_results.loc[zero_mask, col] = 0.0
    if "first_reorder_month" in df_results.columns:
        df_results.loc[zero_mask, "first_reorder_month"] = "None"

    if df_monthly is not None and not df_monthly.empty and "SKU" in df_monthly.columns:
        month_col = "Month" if "Month" in df_monthly.columns else None
        row_type_col = "Row_Type" if "Row_Type" in df_monthly.columns else None

        if "Safety Stock" in df_monthly.columns:
            if row_type_col:
                non_hist = ~df_monthly[row_type_col].astype(str).str.upper().eq("HIST")
                df_monthly.loc[df_monthly["SKU"].astype(str).isin(zero_skus) & non_hist, "Safety Stock"] = 0.0
            else:
                df_monthly.loc[df_monthly["SKU"].astype(str).isin(zero_skus), "Safety Stock"] = 0.0
        if "Order Quantity" in df_monthly.columns:
            if row_type_col:
                non_hist = ~df_monthly[row_type_col].astype(str).str.upper().eq("HIST")
                df_monthly.loc[df_monthly["SKU"].astype(str).isin(zero_skus) & non_hist, "Order Quantity"] = 0.0
            else:
                df_monthly.loc[df_monthly["SKU"].astype(str).isin(zero_skus), "Order Quantity"] = 0.0

        # Recalculate non-historical rows without reorders so beginning/ending
        # inventory stays consistent with forced ROQ=0.
        for sku in zero_skus:
            sku_idx = df_monthly.index[df_monthly["SKU"].astype(str) == sku]
            if len(sku_idx) == 0:
                continue
            sku_rows = df_monthly.loc[sku_idx].copy()
            if row_type_col:
                sku_rows = sku_rows[~sku_rows[row_type_col].astype(str).str.upper().eq("HIST")]
            if sku_rows.empty:
                continue

            if month_col:
                order = pd.to_datetime(sku_rows[month_col], errors="coerce")
                sku_rows = sku_rows.assign(_month_order=order).sort_values("_month_order")

            inv_pos_series = df_results.loc[df_results["sku"].astype(str) == sku, "inventory_position"]
            inv = float(inv_pos_series.iloc[0]) if len(inv_pos_series) > 0 else 0.0

            for idx, row in sku_rows.iterrows():
                row_type = str(row.get("Row_Type", "")).upper()
                beginning = inv
                hist_val = pd.to_numeric(pd.Series([row.get("Historical Demand", 0.0)]), errors="coerce").fillna(0.0).iloc[0]
                fc_val = pd.to_numeric(pd.Series([row.get("Forecast", 0.0)]), errors="coerce").fillna(0.0).iloc[0]
                month_demand = float(hist_val + fc_val) if row_type == "CATCHUP" else float(fc_val)
                ending = max(0.0, beginning - max(0.0, month_demand))

                if "Beginning Inventory" in df_monthly.columns:
                    df_monthly.at[idx, "Beginning Inventory"] = beginning
                if "Order Quantity" in df_monthly.columns:
                    df_monthly.at[idx, "Order Quantity"] = 0.0
                if "Ending Inventory" in df_monthly.columns:
                    df_monthly.at[idx, "Ending Inventory"] = ending
                if "Safety Stock" in df_monthly.columns:
                    df_monthly.at[idx, "Safety Stock"] = 0.0
                inv = ending

    return df_results, df_monthly


# ---------------------------------------------------------------------------
# Parallel-processing infrastructure (ProcessPoolExecutor)
# ---------------------------------------------------------------------------
# Workers are spawned as fresh processes on Windows, so all shared state must
# be set via the initializer rather than passed per-task (avoids re-pickling
# large objects like the XGBoost model for every single SKU).

_WORKER_SHARED: Dict[str, Any] = {}


def _worker_init(
    lead_times_excel: Dict,
    abc_map: Dict,
    global_model: Any,
    global_feature_cols: Any,
    sku_encodings: Any,
    strip_skus: Any,
    as_of_date: Any,
    po_arrivals_by_sku: Dict,
) -> None:
    """Initializer for each worker process — runs once per worker, not per task."""
    _WORKER_SHARED["lead_times_excel"] = lead_times_excel
    _WORKER_SHARED["abc_map"] = abc_map
    _WORKER_SHARED["global_model"] = global_model
    _WORKER_SHARED["global_feature_cols"] = global_feature_cols
    _WORKER_SHARED["sku_encodings"] = sku_encodings
    _WORKER_SHARED["strip_skus"] = strip_skus
    _WORKER_SHARED["as_of_date"] = as_of_date
    _WORKER_SHARED["po_arrivals_by_sku"] = po_arrivals_by_sku


def _sku_worker_fn(sku_row_dict: Dict, sales_raw_df: "pd.DataFrame") -> Any:
    """Per-SKU worker submitted to ProcessPoolExecutor.

    Must be a module-level function so it is picklable on Windows (spawn).
    Shared read-only data (model, maps) lives in _WORKER_SHARED, set by
    _worker_init, so it is only pickled once per worker process rather than
    once per task.
    """
    sku_row = pd.Series(sku_row_dict)
    sku_key = str(sku_row_dict.get("ITEM_NUMBER", "")).strip().upper()
    po_by_sku = _WORKER_SHARED.get("po_arrivals_by_sku", {})
    po_arrivals_df = po_by_sku.get(sku_key)
    return process_sku(
        None,  # conn not needed in offline mode
        sku_row,
        _WORKER_SHARED["lead_times_excel"],
        _WORKER_SHARED["abc_map"],
        _WORKER_SHARED.get("global_model"),
        _WORKER_SHARED.get("global_feature_cols"),
        _WORKER_SHARED.get("sku_encodings"),
        strip_skus=_WORKER_SHARED.get("strip_skus"),
        as_of_date=_WORKER_SHARED.get("as_of_date"),
        sales_raw_df=sales_raw_df,
        po_arrivals_df=po_arrivals_df,
    )


def run_inventory_planning():
    output_dir = Path(__file__).parent
    lead_times_excel, sf_per_pallet_map, pallets_per_container_map = load_lead_times(output_dir / LEADTIMES_XLSX)
    strip_skus = _load_strip_sku_list()

    # Load forecast SKU list if enabled
    forecast_sku_list = None
    if USE_SKU_LIST and not FOCUS_SKU:
        forecast_sku_list = load_forecast_sku_list(output_dir / FORECAST_SKU_LIST_FILE)

    conn = _connect()
    single_sku = FOCUS_SKU.strip().upper() if FOCUS_SKU else None
    # Pass strip_skus as ensure_skus so strip items without recent SF sales
    # history (e.g. items sold in a non-SF UOM, or very slow movers) are still
    # fetched from Gartman and processed as no-history stubs.
    sku_master = get_active_skus(conn, single_sku, forecast_sku_list, ensure_skus=strip_skus)
    
    if sku_master.empty:
        print("ERROR: No active SKUs found")
        conn.close()
        return
    
    # Mode description
    if single_sku:
        mode_desc = f"Single SKU: {single_sku}"
    elif USE_SKU_LIST and forecast_sku_list and len(forecast_sku_list) > 0:
        mode_desc = f"SKU List: {len(sku_master)} SKUs from {FORECAST_SKU_LIST_FILE}"
    else:
        mode_desc = f"All Active SKUs: {len(sku_master)} SKUs"
    
    model_desc = "Global XGBoost Model" if USE_GLOBAL_MODEL and not single_sku else "Per-SKU Models"
    print(f"\nMODE: {mode_desc}")
    print(f"FORECAST MODEL: {model_desc}")
    
    # Build ABC segmentation based on trailing 12 month SF volume
    print(f"\n{'='*70}")
    print("BUILDING ABC SEGMENTATION")
    print(f"{'='*70}")
    sku_list_for_volume = sku_master["ITEM_NUMBER"].astype(str).str.upper().tolist()
    vol12_df = load_trailing_12m_volume(conn, sku_list_for_volume)

    # For consolidated groups, sum member volumes into a single group entry so the
    # ABC class reflects pooled demand (combined history = smoother, higher signal).
    if not single_sku and SKU_CONSOLIDATION_GROUPS:
        _all_grp_members_abc: set = {s.upper() for ms in SKU_CONSOLIDATION_GROUPS.values() for s in ms}
        _grp_vol_rows: List[Dict] = []
        for _cname, _mskus in SKU_CONSOLIDATION_GROUPS.items():
            _mu = [s.upper() for s in _mskus]
            _gvol = float(vol12_df[vol12_df["ITEM_NUMBER"].str.upper().isin(_mu)]["VOL_12M"].sum())
            if _gvol > 0:
                _grp_vol_rows.append({"ITEM_NUMBER": _cname, "VOL_12M": _gvol})
        _vol12_for_abc = vol12_df[~vol12_df["ITEM_NUMBER"].str.upper().isin(_all_grp_members_abc)].copy()
        if _grp_vol_rows:
            _vol12_for_abc = pd.concat([_vol12_for_abc, pd.DataFrame(_grp_vol_rows)], ignore_index=True)
        abc_map = build_abc_map(_vol12_for_abc)
    else:
        abc_map = build_abc_map(vol12_df)

    # Calculate total trailing 12m volume for global uplift budget
    total_vol_12m = float(vol12_df["VOL_12M"].sum()) if not vol12_df.empty else 0.0
    uplift_budget = GLOBAL_UPLIFT_BUDGET_PCT * total_vol_12m if ENABLE_GLOBAL_UPLIFT_BUDGET else None
    
    if ENABLE_GLOBAL_UPLIFT_BUDGET and uplift_budget:
        print(f"\n  Global Uplift Budget: {uplift_budget:,.2f} SF ({GLOBAL_UPLIFT_BUDGET_PCT*100:.1f}% of trailing 12m volume)")
    
    print(f"{'='*70}")
    
    # Train global model if enabled and processing multiple SKUs
    global_model = None
    global_feature_cols = None
    sku_encodings = None
    
    if USE_GLOBAL_MODEL and not single_sku:
        cutoff_date = pd.Timestamp(CUTOFF_DATE)
        df_global, sku_encodings = build_global_training_data(conn, sku_master, cutoff_date)
        
        if len(df_global) > 1000:  # Need sufficient data
            global_model, global_feature_cols = train_global_xgboost(df_global)
        else:
            print("[!]  Insufficient data for global model, falling back to per-SKU models")
    
    results = []
    all_monthly_projs = []
    total_skus = len(sku_master)

    # Issue 3: Capture the run start time once so every SKU in this batch shares
    # the same as_of_date regardless of how long the run takes.
    run_as_of_date = pd.Timestamp.now().normalize()

    # -----------------------------------------------------------------------
    # Phase 1: Bulk-load all sales histories in a single DB round-trip.
    # This collapses N per-SKU queries (one inside each process_sku call) into
    # one batched query, which is both faster and lets us close the per-SKU
    # DB dependency before spawning worker processes.
    # -----------------------------------------------------------------------
    print(f"\n{'='*70}")
    print("PHASE 1: PRE-LOADING SALES HISTORY (BULK)")
    print(f"{'='*70}")
    sku_list_bulk = sku_master["ITEM_NUMBER"].astype(str).str.strip().str.upper().tolist()
    _bulk_cutoff = pd.Timestamp(CUTOFF_DATE)
    all_sales_raw = bulk_load_sales_history(conn, sku_list_bulk, _bulk_cutoff)
    print(f"  Loaded history for {len(all_sales_raw)} / {len(sku_list_bulk)} SKUs")

    # Bulk-load PO arrival lines (creation date + DUE_INV) for all SKUs so
    # each worker gets accurate per-container arrival dates for the simulation.
    print("  Loading open PO arrival schedule...")
    try:
        _po_arrivals_all = load_arrivals(conn, sku_list_bulk)
    except Exception as _po_err:
        print(f"  WARNING: PO arrivals load failed ({_po_err}); using lump LT fallback.")
        _po_arrivals_all = pd.DataFrame()

    po_arrivals_by_sku: Dict[str, "pd.DataFrame"] = {}
    if not _po_arrivals_all.empty and "ITEM_NUMBER" in _po_arrivals_all.columns:
        _po_arrivals_all["ITEM_NUMBER"] = (
            _po_arrivals_all["ITEM_NUMBER"].astype(str).str.strip().str.upper()
        )

        # Remove lawsuit-hold vendors from the PO schedule and adjust sku_master
        # ON_PO_SF / INVENTORY_POSITION so their quantities are invisible to the simulation.
        if "VENDOR_NUMBER" in _po_arrivals_all.columns:
            _excl_mask = (
                _po_arrivals_all["VENDOR_NUMBER"].astype(str).str.strip()
                .isin(EXCLUDED_PO_VENDORS)
            )
            if _excl_mask.any():
                _excl_qty = (
                    _po_arrivals_all[_excl_mask]
                    .groupby("ITEM_NUMBER")["QUANTITY_SF"]
                    .sum()
                )
                _po_arrivals_all = _po_arrivals_all[~_excl_mask].reset_index(drop=True)
                # Subtract excluded ON_PO quantities from sku_master so inventory
                # position used by the simulation reflects only non-lawsuit POs.
                if not _excl_qty.empty and "ITEM_NUMBER" in sku_master.columns:
                    _sm_idx = sku_master["ITEM_NUMBER"].str.strip().str.upper()
                    for _excl_item, _excl_sf in _excl_qty.items():
                        _rows = _sm_idx == _excl_item
                        if _rows.any():
                            sku_master.loc[_rows, "ON_PO_SF"] = (
                                sku_master.loc[_rows, "ON_PO_SF"]
                                .astype(float)
                                .clip(lower=0)
                                .sub(_excl_sf)
                                .clip(lower=0)
                            )
                    # Recompute INVENTORY_POSITION after adjusting ON_PO_SF
                    sku_master["INVENTORY_POSITION"] = (
                        sku_master["AVAILABLE_SF"].astype(float)
                        + sku_master["ON_PO_SF"].astype(float)
                        - sku_master["BACKORDER_SF"].astype(float)
                    )
                print(
                    f"  Excluded {_excl_mask.sum()} PO lines from {len(_excl_qty)} SKU(s) "
                    f"(lawsuit-hold vendors)"
                )

        for _sku_key, _grp in _po_arrivals_all.groupby("ITEM_NUMBER"):
            po_arrivals_by_sku[_sku_key] = _grp.reset_index(drop=True)
    print(f"  PO schedule loaded for {len(po_arrivals_by_sku)} SKUs")

    # -----------------------------------------------------------------------
    # Phase 1.5: Build virtual group SKUs for consolidated forecasting.
    #
    # Each consolidation group (e.g. RH112SRSNB / RH112SRSNB-15 / RH112SRSNB-24)
    # has its member sales histories pooled into a single combined series BEFORE
    # any model fitting.  Fitting one model on the combined signal produces a
    # smoother, less lumpy demand history than fitting independently per member
    # and summing at display time.
    #
    # The virtual group row uses:
    #   - ITEM_NUMBER = consolidated display name (e.g. "RH112SRSNB")
    #   - AVAILABLE_SF / ON_PO_SF / BACKORDER_SF = sum of all members
    #   - All other fields (vendor, lead time, etc.) taken from the first member
    #
    # Individual member SKUs are excluded from Phase 2 dispatch.
    # -----------------------------------------------------------------------
    _empty_raw = pd.DataFrame(columns=["SALES_DATE", "QTY_SOLD_SF"])
    member_skus_to_skip: set = set()
    group_virtual_rows: List[pd.Series] = []
    group_virtual_sales: Dict[str, pd.DataFrame] = {}

    if not single_sku and SKU_CONSOLIDATION_GROUPS:
        print(f"\n{'='*70}")
        print("PHASE 1.5: BUILDING VIRTUAL GROUP SKUs FOR CONSOLIDATED FORECASTING")
        print(f"{'='*70}")
        _sku_master_upper = sku_master["ITEM_NUMBER"].str.strip().str.upper()

        for consolidated_name, member_skus in SKU_CONSOLIDATION_GROUPS.items():
            member_upper_list = [s.upper() for s in member_skus]
            mask = _sku_master_upper.isin(member_upper_list)
            member_master_rows = sku_master[mask]
            if member_master_rows.empty:
                continue  # No members active in this run — skip

            # Mark members for exclusion from the normal dispatch
            for s in member_upper_list:
                if s in _sku_master_upper.values:
                    member_skus_to_skip.add(s)

            # Pool sales histories: concatenate raw series, then sum by date
            _pool_dfs = [
                all_sales_raw[s]
                for s in member_upper_list
                if s in all_sales_raw and not all_sales_raw[s].empty
            ]
            if _pool_dfs:
                _pooled = pd.concat(_pool_dfs, ignore_index=True)
                _pooled = _pooled.groupby("SALES_DATE", as_index=False)["QTY_SOLD_SF"].sum()
            else:
                _pooled = _empty_raw.copy()
            group_virtual_sales[consolidated_name] = _pooled

            # Pool PO arrivals
            _pool_po = [
                po_arrivals_by_sku[s].copy()
                for s in member_upper_list
                if s in po_arrivals_by_sku
            ]
            if _pool_po:
                _pooled_po = pd.concat(_pool_po, ignore_index=True)
                _pooled_po["ITEM_NUMBER"] = consolidated_name
                po_arrivals_by_sku[consolidated_name] = _pooled_po

            # Virtual sku_row: first member's row with summed inventory fields
            virtual_row = member_master_rows.iloc[0].copy()
            virtual_row["ITEM_NUMBER"] = consolidated_name
            for _inv_col in ["AVAILABLE_SF", "ON_PO_SF", "BACKORDER_SF"]:
                if _inv_col in sku_master.columns:
                    virtual_row[_inv_col] = (
                        pd.to_numeric(member_master_rows[_inv_col], errors="coerce")
                        .fillna(0.0)
                        .sum()
                    )
            if all(c in virtual_row.index for c in ["AVAILABLE_SF", "ON_PO_SF", "BACKORDER_SF"]):
                virtual_row["INVENTORY_POSITION"] = (
                    float(virtual_row["AVAILABLE_SF"])
                    + float(virtual_row["ON_PO_SF"])
                    - float(virtual_row["BACKORDER_SF"])
                )
            group_virtual_rows.append(virtual_row)
            print(
                f"  Group '{consolidated_name}': {len(member_master_rows)} members "
                f"-> {len(_pooled)} pooled sales rows"
            )

        n_groups = len(group_virtual_rows)
        n_members_skipped = len(member_skus_to_skip)
        print(f"  {n_groups} virtual group(s) built, {n_members_skipped} member SKU(s) replaced")
    else:
        n_groups = 0
        n_members_skipped = 0

    # Capture per-member inventory + monthly sales snapshots so the Niko
    # Inventory At a Glance card can display individual (not pooled) stats.
    member_snapshots: List[Dict] = []
    if member_skus_to_skip and SKU_CONSOLIDATION_GROUPS:
        _sm_idx = sku_master["ITEM_NUMBER"].str.strip().str.upper()
        for _cname, _mskus in SKU_CONSOLIDATION_GROUPS.items():
            for _msku_raw in _mskus:
                _msku_up = _msku_raw.upper()
                _mask = _sm_idx == _msku_up
                if not _mask.any():
                    continue
                _mrow = sku_master[_mask].iloc[0]
                _msales_df = all_sales_raw.get(_msku_up, pd.DataFrame())
                _monthly_hist: Dict[str, float] = {}
                if not _msales_df.empty and "SALES_DATE" in _msales_df.columns and "QTY_SOLD_SF" in _msales_df.columns:
                    _ms = _msales_df.copy()
                    _ms["_month"] = pd.to_datetime(_ms["SALES_DATE"], errors="coerce").dt.to_period("M").dt.to_timestamp()
                    _ms = _ms.dropna(subset=["_month"])
                    _agg = _ms.groupby("_month", as_index=True)["QTY_SOLD_SF"].sum()
                    _monthly_hist = {d.strftime("%Y-%m-%d"): float(v) for d, v in _agg.items()}
                member_snapshots.append({
                    "sku": _msku_up,
                    "consolidated_name": _cname,
                    "description": str(_mrow["DESCRIPTION"]) if "DESCRIPTION" in _mrow.index else "",
                    "vendor_name": str(_mrow["VENDOR_NAME"]) if "VENDOR_NAME" in _mrow.index else "",
                    "vendor_number": str(_mrow["VENDOR_NUMBER"]) if "VENDOR_NUMBER" in _mrow.index else "",
                    "available_sf": float(_mrow["AVAILABLE_SF"]) if "AVAILABLE_SF" in _mrow.index else 0.0,
                    "on_po_sf": float(_mrow["ON_PO_SF"]) if "ON_PO_SF" in _mrow.index else 0.0,
                    "backorder_sf": float(_mrow["BACKORDER_SF"]) if "BACKORDER_SF" in _mrow.index else 0.0,
                    "monthly_sales": _monthly_hist,
                })

    # -----------------------------------------------------------------------
    # Phase 2: Parallel model fitting.
    # conn stays open so load_arrivals() can use it after the loop; worker
    # processes use only the pre-loaded sales data and never touch conn.
    # -----------------------------------------------------------------------
    n_workers = (
        PARALLEL_WORKERS if PARALLEL_WORKERS > 0
        else max(1, (os.cpu_count() or 4) // 2)
    )
    # Effective SKU count = individual non-member SKUs + virtual group SKUs
    total_skus = total_skus - n_members_skipped + n_groups
    print(f"\n{'='*70}")
    print(f"PHASE 2: PARALLEL FORECAST ({n_workers} workers, {total_skus} SKUs)")
    print(f"{'='*70}")

    futures_map: Dict[Any, str] = {}

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_worker_init,
        initargs=(
            lead_times_excel, abc_map, global_model,
            global_feature_cols, sku_encodings, strip_skus, run_as_of_date,
            po_arrivals_by_sku,
        ),
    ) as executor:
        # Submit virtual group SKUs first (pooled combined history)
        for virtual_row in group_virtual_rows:
            grp_name = str(virtual_row.get("ITEM_NUMBER", "")).strip().upper()
            raw_df = group_virtual_sales.get(grp_name, _empty_raw)
            future = executor.submit(_sku_worker_fn, virtual_row.to_dict(), raw_df)
            futures_map[future] = grp_name

        # Submit individual (non-member) SKUs
        for _, row in sku_master.iterrows():
            sku = str(row.get("ITEM_NUMBER", "")).strip().upper()
            if sku in member_skus_to_skip:
                continue
            raw_df = all_sales_raw.get(sku, _empty_raw)
            future = executor.submit(_sku_worker_fn, row.to_dict(), raw_df)
            futures_map[future] = sku

        completed = 0
        for future in concurrent.futures.as_completed(futures_map):
            sku = futures_map[future]
            completed += 1
            # Progress file is written from the main process — thread-safe by design
            try:
                with open(FLOORING_PROGRESS_FILE, "w") as f:
                    json.dump(
                        {
                            "current": completed,
                            "total": total_skus,
                            "sku": sku,
                            "status": "running",
                            "product_type": "flooring",
                        },
                        f,
                    )
            except Exception:
                pass
            try:
                result = future.result()
                if result:
                    metrics, monthly_proj = result
                    results.append(metrics)
                    monthly_proj["SKU"] = metrics["sku"]
                    all_monthly_projs.append(monthly_proj)
            except Exception as e:
                print(f"  ERROR processing {sku}: {e}")
                import traceback
                traceback.print_exc()

    # Mark progress as complete
    try:
        with open(FLOORING_PROGRESS_FILE, 'w') as f:
            json.dump({"current": total_skus, "total": total_skus, "status": "complete", "product_type": "flooring"}, f)
    except Exception:
        pass

    if not results:
        print("\nNo SKUs processed")
        if conn is not None:
            conn.close()
        return
    
    df_results = pd.DataFrame(results)
    df_monthly = pd.concat(all_monthly_projs, ignore_index=True) if all_monthly_projs else pd.DataFrame()
    df_summary = _build_inventory_summary(df_results, df_monthly)

    if not df_results.empty and "sku" in df_results.columns:
        df_results["sf_per_pallet"] = df_results["sku"].map(sf_per_pallet_map)
        df_results["pallets_per_container"] = df_results["sku"].map(pallets_per_container_map)
    if not df_monthly.empty and "SKU" in df_monthly.columns:
        df_monthly["sf_per_pallet"] = df_monthly["SKU"].map(sf_per_pallet_map)
        df_monthly["pallets_per_container"] = df_monthly["SKU"].map(pallets_per_container_map)
    
    # APPLY GLOBAL UPLIFT BUDGET if enabled
    if ENABLE_GLOBAL_UPLIFT_BUDGET and uplift_budget is not None and uplift_budget > 0 and not df_results.empty:
        total_uplift = float(df_results["ss_uplift_applied_pre_budget"].sum())
        if total_uplift > uplift_budget:
            k = uplift_budget / total_uplift
            print(f"\n{'='*70}")
            print(f"[!]  GLOBAL UPLIFT BUDGET ADJUSTMENT")
            print(f"{'='*70}")
            print(f"  Total uplift={total_uplift:,.2f} SF")
            print(f"  Budget={uplift_budget:,.2f} SF")
            print(f"  Scaling factor k={k:.3f}")
            print(f"  All uplifts will be scaled down proportionally")
            
            # Scale only the uplift, keep base intact
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"] * k
            df_results["safety_stock"] = df_results["ss_base"] + df_results["ss_uplift_applied"]
            df_results["reorder_point"] = df_results["lead_time_mean_demand"] + df_results["safety_stock"]
            # S = ROP + one month's forecast demand (post-budget recalculation).
            df_results["order_up_to_level"] = df_results["reorder_point"] + df_results["avg_monthly_demand"]

            # Regenerate projections so they match the post-budget ROP/SS now
            # stored in df_results.  Without this, the metrics sheet and the
            # monthly projection sheet are inconsistent (different ROP values).
            df_monthly = _regenerate_projections_post_budget(df_results, df_monthly)

            # Sync first_reorder_month in df_results to the updated projections
            for _sku in df_results['sku'].unique():
                _proj = df_monthly[df_monthly['SKU'] == _sku]
                _orders = _proj[pd.to_numeric(_proj['Order Quantity'], errors='coerce') > 0]
                _frm = _orders.iloc[0]['Month'] if len(_orders) > 0 else 'None'
                df_results.loc[df_results['sku'] == _sku, 'first_reorder_month'] = _frm

            print(f"  Adjusted safety stock and regenerated projections for all SKUs")
            print(f"{'='*70}")
        else:
            # No adjustment needed
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]
            print(f"\n  [OK] Global uplift budget NOT exceeded (Total={total_uplift:,.2f} SF, Budget={uplift_budget:,.2f} SF)")
    else:
        # No budget enabled, just copy pre-budget values
        if not df_results.empty and "ss_uplift_applied_pre_budget" in df_results.columns:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]
    
    # APPLY GLOBAL ENDING INVENTORY CAP if enabled
    if ENABLE_GLOBAL_INV_CAP and not df_results.empty and not df_monthly.empty:
        df_results, df_monthly = apply_global_inv_cap(df_results, df_monthly, GLOBAL_INV_CAP_SF)

    # Re-assert zero-forecast coherence after any global adjustment logic.
    df_results, df_monthly = _reapply_zero_forecast_policy(df_results, df_monthly)
    
    # Determine output filename
    if single_sku:
        base_filename = 'inventory_plan_single.xlsx'
    elif USE_SKU_LIST and forecast_sku_list and len(forecast_sku_list) > 0:
        base_filename = 'inventory_plan_list.xlsx'
    else:
        base_filename = 'inventory_plan_all.xlsx'
    
    output_file = output_dir / base_filename
    
    # Try writing to the file
    saved = False
    for attempt in range(2):
        try:
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                df_results.to_excel(writer, sheet_name='Inventory_Metrics', index=False)
                if not df_summary.empty:
                    df_summary.to_excel(writer, sheet_name='Inventory_Summary', index=False)
                if not df_monthly.empty:
                    df_monthly_out = df_monthly.copy()
                    if 'Month' in df_monthly_out.columns:
                        df_monthly_out['Month'] = pd.to_datetime(
                            df_monthly_out['Month'], errors='coerce'
                        ).dt.strftime('%m/%d/%Y')
                    if 'Row_Type' in df_monthly_out.columns:
                        df_monthly_out = df_monthly_out.drop(columns=['Row_Type'])

                    # Align column order with sundries output
                    preferred = [
                        'Month',
                        'Historical Demand',
                        'Safety Stock',
                        'Beginning Inventory',
                        'Forecast',
                        'Order Quantity',
                        'Ending Inventory',
                        'SKU',
                        'vendor_number',
                        'collection',
                    ]
                    cols = [c for c in preferred if c in df_monthly_out.columns]
                    remaining = [c for c in df_monthly_out.columns if c not in cols]
                    if cols:
                        df_monthly_out = df_monthly_out[cols + remaining]
                    df_monthly_out.to_excel(writer, sheet_name='Monthly_Projections', index=False)
            saved = True
            print(f"\n[OK] Saved: {output_file}")
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
                    print(
                        f"  {demand_class}: {count} SKUs ({pct:.1f}%) - "
                        f"Avg ADI={_format_metric(avg_adi)}, Avg CV2={_format_metric(avg_cv2)}"
                    )
                
                print(f"\n{'='*70}")
                print("FORECAST METHOD SELECTION SUMMARY")
                print(f"{'='*70}")
                method_counts = df_results['forecast_method'].value_counts()
                for method, count in method_counts.items():
                    pct = 100 * count / len(df_results)
                    avg_wmape = df_results[df_results['forecast_method'] == method]['forecast_wmape'].mean()
                    print(f"  {method}: {count} SKUs ({pct:.1f}%) - Avg wMAPE: {_format_metric(avg_wmape)}%")
                print(f"{'='*70}")
            
            break
        except PermissionError:
            if attempt == 0:
                # First attempt failed - try with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                timestamped_filename = base_filename.replace('.xlsx', f'_{timestamp}.xlsx')
                output_file = output_dir / timestamped_filename
                print(f"\n[!]  Original file is locked. Trying: {timestamped_filename}")
            else:
                print(f"\n[x] ERROR: Could not save file. Please close Excel and try again.")
                print(f"  File path: {output_file}")
                break
    
    arrivals_df = pd.DataFrame()
    if conn is not None and not df_results.empty and "sku" in df_results.columns:
        # Include individual member SKUs so POs for consolidated-group members are fetched.
        # Member SKUs are skipped during forecasting (replaced by a virtual group SKU) so
        # they never appear in df_results["sku"]; without this expansion their POs are
        # silently excluded from the arrivals query.
        _result_skus = df_results["sku"].astype(str).unique().tolist()
        _member_skus = [s.upper() for members in SKU_CONSOLIDATION_GROUPS.values() for s in members]
        _arrivals_sku_list = list(dict.fromkeys(_result_skus + _member_skus))
        arrivals_df = load_arrivals(conn, _arrivals_sku_list)
        if not arrivals_df.empty:
            lt_map = df_results[["sku", "lead_time_days"]].rename(columns={"sku": "ITEM_NUMBER"})
            arrivals_df = arrivals_df.merge(lt_map, on="ITEM_NUMBER", how="left")
    if conn is not None:
        conn.close()
    payload = _build_webapp_payload(df_results, df_monthly, arrivals_df, member_snapshots=member_snapshots)
    _write_webapp_json(payload, WEBAPP_JSON_PATH)

    if not saved:
        print(f"\n[!]  WARNING: Results were not saved to disk!")
        print(f"  Please close the Excel file and run again.")

def _format_currency(value: float, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "--"
    return f"${value:,.{digits}f}"

def _df_to_records(df: pd.DataFrame) -> List[Dict]:
    if df is None or df.empty:
        return []
    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].dt.strftime("%Y-%m-%d")
    df_out = df_out.replace({pd.NA: None, np.nan: None, np.inf: None, -np.inf: None})
    return df_out.to_dict(orient="records")

def _format_number(value: float, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "--"
    return f"{value:,.{digits}f}"

def _format_metric(value: float, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "NA"
    return f"{value:.{digits}f}"

def _filter_forecast_months(series: pd.Series) -> pd.Series:
    if series is None or series.empty:
        return series
    if not isinstance(series.index, pd.DatetimeIndex):
        return series
    current_month_start = pd.Timestamp.today().to_period("M").to_timestamp()
    next_month_start = current_month_start + pd.offsets.MonthBegin(1)
    end_exclusive = current_month_start + pd.DateOffset(months=12)
    return series[(series.index >= next_month_start) & (series.index < end_exclusive)]

def _normalize_arrival_date(value: Any) -> pd.Timestamp:
    try:
        dt = pd.to_datetime(value, errors="coerce")
    except Exception:
        return pd.NaT
    if pd.isna(dt):
        return pd.NaT
    start = pd.Timestamp("2025-01-01")
    end = pd.Timestamp.today().normalize() + pd.Timedelta(days=730)
    if dt < start or dt > end:
        return pd.NaT
    return dt


def _resolve_po_projection_arrival_date(po_row: Dict[str, Any]) -> pd.Timestamp:
    """
    Resolve the arrival date used by the projection engine for an open PO line.

    Uses Due in Inventory (ETW) as the sole source of truth. POs without an
    ETW date are treated as undated and excluded from the reorder schedule.
    """
    due_inv = _normalize_arrival_date(po_row.get("DUE_INV"))
    if not pd.isna(due_inv):
        return due_inv

    return pd.NaT

def _format_arrival_date(value: Any) -> str:
    dt = _normalize_arrival_date(value)
    if pd.isna(dt):
        return "No Date"
    return dt.strftime("%m/%d/%Y")

def _format_arrival_value(value: Any, label: str = "No Date") -> str:
    dt = _normalize_arrival_date(value)
    if pd.isna(dt):
        return label
    return dt.strftime("%m/%d/%Y")

def _month_key(value: Any) -> Optional[str]:
    try:
        dt = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(dt):
        return None
    return dt.to_period("M").to_timestamp().strftime("%Y-%m-%d")

def _add_days_safe(value: Any, days: float) -> pd.Timestamp:
    base = _normalize_arrival_date(value)
    if pd.isna(base):
        return pd.NaT
    try:
        return base + pd.Timedelta(days=float(days))
    except Exception:
        return pd.NaT

def _mask_suffix(value: str) -> str:
    if not value:
        return "000"
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits:
        return digits[-3:].zfill(3)
    return f"{abs(hash(value)) % 1000:03d}"

def _get_first_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _lead_time_file_candidates() -> List[Path]:
    """Return workbook locations in the order the dashboards should trust them."""
    candidates = [LEAD_TIMES_FILE, Path(__file__).resolve().parent / "Lead Times.xlsx"]
    unique: List[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


@st.cache_data(ttl=300)
def _load_final_lead_time_overrides() -> Dict[str, float]:
    """Load per-SKU lead-time overrides from Lead Times.xlsx Final/FINAL."""
    for path in _lead_time_file_candidates():
        if not path.exists():
            continue
        try:
            df = pd.read_excel(path, sheet_name="Final", header=0)
            sku_col = _get_first_column(df, ["SKU", "Item Number", "ITEM_NUMBER"])
            lt_col = _get_first_column(df, ["FINAL", "Final", "final"])
            if not sku_col or not lt_col:
                continue
            work = df[[sku_col, lt_col]].copy()
            work[sku_col] = work[sku_col].astype(str).str.strip().str.upper()
            work[lt_col] = pd.to_numeric(work[lt_col], errors="coerce")
            work = work.dropna(subset=[sku_col, lt_col])
            work = work[work[sku_col] != ""]
            work = work[work[lt_col] > 0]
            return dict(zip(work[sku_col], work[lt_col].astype(float)))
        except Exception:
            continue
    return {}


def _apply_lead_time_overrides_to_payload(payload: Dict, source: str = "Lead Times.xlsx:Final") -> Dict:
    """Apply Excel FINAL lead times to an already-built dashboard payload."""
    overrides = _load_final_lead_time_overrides()
    if not overrides:
        return payload

    out = copy.deepcopy(payload)

    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            num = float(value)
        except Exception:
            return default
        if not math.isfinite(num):
            return default
        return num

    def _patch_row(row: Dict[str, Any]) -> None:
        sku = str(row.get("item_number") or row.get("sku") or "").strip().upper()
        if not sku or sku not in overrides:
            return
        lead_time_days = float(overrides[sku])
        old_lead_time = _as_float(row.get("lead_time_days"), default=float("nan"))
        if math.isfinite(old_lead_time) and abs(old_lead_time - lead_time_days) < 0.005:
            if not row.get("lead_time_source"):
                row["lead_time_source"] = source
            return

        row["lead_time_days"] = lead_time_days
        row["lead_time_source"] = source
        row["lead_time_weeks"] = lead_time_days / DAYS_PER_WEEK

        daily_demand = _as_float(row.get("daily_demand", row.get("daily_mean_demand", 0.0)))
        if daily_demand <= 0:
            return

        lead_time_demand = daily_demand * lead_time_days
        safety_stock = _as_float(row.get("safety_stock"), 0.0)
        reorder_point = lead_time_demand + safety_stock
        row["lead_time_mean_demand"] = round(lead_time_demand, 1)
        row["reorder_point"] = round(reorder_point, 1)

        rate_90 = _as_float(row.get("rate_90"), 0.0)
        rate_365 = _as_float(row.get("rate_365"), 0.0)
        units_30d = _as_float(row.get("units_30d"), 0.0)
        monthly_demand = rate_90 if rate_90 > 0 else (rate_365 if rate_365 > 0 else units_30d)
        if monthly_demand > 0:
            units_90d = rate_90 * 3.0
            order_up_to = (units_90d if units_90d > 0 else monthly_demand * 3.0) + lead_time_demand
            order_up_to = min(order_up_to, monthly_demand * 4.0)
            order_up_to = max(order_up_to, reorder_point)
            row["order_up_to_level"] = round(order_up_to, 1)

    for section in ("Inventory_Metrics", "items", "queue"):
        for row in out.get(section, []) or []:
            if isinstance(row, dict):
                _patch_row(row)

    return out

def _demo_webapp_payload() -> Dict:
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "generated_at": datetime.now().isoformat(),
        "run_meta": {
            "title": "OMP Purchasing Dashboard",
            "run_timestamp_local": run_ts,
            "forecast_horizon_days": 30,
            "source": "Lead Times.xlsx:Final",
        },
        "items": [
            {
                "item_number": "GFAL09503",
                "description": "EURO OAK 9.5\" DOMA",
                "vendor_name": "FLO.I.T S.R.L",
                "vendor_number": "569",
                "available": 59731.11,
                "on_po": 0.0,
                "backorder": 0.0,
                "inventory_position": 59731.11,
                "lead_time_days": 53,
                "lead_time_source": "Excel",
                "lt_demand": 370.03,
                "suggested_order_qty": None,
                "forecast_series": {
                    "x": ["Nov 23", "Nov 30", "Dec 07", "Dec 14", "Dec 21"],
                    "y": [72.4, 74.8, 74.9, 74.9, 74.1],
                },
                "segmentation": {
                    "maturity": "Legacy",
                    "demand_pattern": "Lumpy",
                    "adi": 1.63,
                    "cv2": 1.48,
                    "first_sales_date": "2023-08-09",
                    "history_days": 862,
                },
                "daily_avg_demand": 12.33,
                "days_of_cover": 4842.7,
            }
        ],
        "arrivals": [
            {
                "PO_NUMBER": "450123",
                "ITEM_NUMBER": "GFAL09503",
                "DESCRIPTION": "EURO OAK 9.5\" DOMA",
                "QUANTITY_SF": 1200.0,
                "VENDOR_NAME": "FLO.I.T S.R.L",
                "VENDOR_NUMBER": "569",
                "COLLECTION": "ALLORA",
                "CONTAINER": "CONT-7742",
                "DUE_PORT": "2025-02-10",
                "DUE_INV": "2025-03-05",
            }
        ],
        "queue": [
            {
                "item_number": "GFAL09503",
                "description": "EURO OAK 9.5\" DOMA",
                "vendor_name": "FLO.I.T S.R.L",
                "vendor_number": "569",
                "available": 59731.11,
                "on_po": 0.0,
                "backorder": 0.0,
                "inventory_position": 59731.11,
                "lead_time_days": 53,
                "lt_demand": 370.03,
            }
        ],
        "sql_sources": [
            "gartman_sales_history_daily.sql",
        ],
    }

def _build_inventory_summary(df_results: pd.DataFrame, df_monthly: pd.DataFrame) -> pd.DataFrame:
    """Build a simple apples-to-apples summary table for Excel export."""
    if df_results is None or df_results.empty:
        return pd.DataFrame()

    total_now_inventory_position = float(df_results['inventory_position'].sum()) if 'inventory_position' in df_results.columns else 0.0

    total_year_end = 0.0
    total_year_end_backorders = 0.0
    year_end_month = ""

    if df_monthly is not None and not df_monthly.empty and 'Month' in df_monthly.columns:
        df_month = df_monthly.copy()
        if 'Row_Type' in df_month.columns:
            df_month = df_month[df_month['Row_Type'] != 'HIST']
        df_month['Month_dt'] = pd.to_datetime(df_month['Month'], errors='coerce')
        if df_month['Month_dt'].notna().any():
            last_month = df_month['Month_dt'].max()
            year_end_month = last_month.strftime("%Y-%m-%d")
            last_rows = df_month[df_month['Month_dt'] == last_month]
            if 'Ending Inventory' in last_rows.columns:
                total_year_end = float(last_rows['Ending Inventory'].sum())
            if 'Backorders' in last_rows.columns:
                total_year_end_backorders = float(last_rows['Backorders'].sum())

    delta = total_year_end - total_now_inventory_position
    pct_change = (delta / total_now_inventory_position * 100) if total_now_inventory_position != 0 else 0.0

    summary_rows = [
        {"Metric": "Total Inventory Position (Now)", "Value": round(total_now_inventory_position, 2)},
        {"Metric": "Ending Inventory (12 Months)", "Value": round(total_year_end, 2)},
        {"Metric": "Difference (12 Months - Now)", "Value": round(delta, 2)},
        {"Metric": "Percent Change", "Value": round(pct_change, 2)},
    ]
    if year_end_month:
        summary_rows.insert(2, {"Metric": "Ending Month", "Value": year_end_month})
    if total_year_end_backorders:
        summary_rows.append({"Metric": "Backorders at 12 Months", "Value": round(total_year_end_backorders, 2)})

    return pd.DataFrame(summary_rows)

def _build_webapp_payload(df_results: pd.DataFrame, df_monthly: pd.DataFrame, df_arrivals: Optional[pd.DataFrame] = None, member_snapshots: Optional[List[Dict]] = None) -> Dict:
    payload = _demo_webapp_payload()
    if df_results is None or df_results.empty:
        return payload

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload["run_meta"] = {
        "title": "OMP Purchasing Dashboard",
        "run_timestamp_local": run_ts,
        "forecast_horizon_days": FUTURE_FORECAST_DAYS,
        "source": f"{LEADTIMES_XLSX}:Final",
    }

    col_month = _get_first_column(df_monthly, ["Month", "month"]) if df_monthly is not None else None
    col_forecast = _get_first_column(df_monthly, ["Forecast", "forecast"]) if df_monthly is not None else None
    col_hist = _get_first_column(df_monthly, ["Historical Demand", "Historical", "history"]) if df_monthly is not None else None
    col_sku = _get_first_column(df_monthly, ["SKU", "sku", "Item Number", "ITEM_NUMBER"]) if df_monthly is not None else None

    sku_series: Dict[str, Dict[str, List[float]]] = {}
    sku_first_dates: Dict[str, str] = {}
    sku_history_days: Dict[str, int] = {}

    row_type_col = _get_first_column(df_monthly, ["Row_Type", "row_type"]) if df_monthly is not None else None

    if df_monthly is not None and not df_monthly.empty and col_month and col_sku and (col_forecast or col_hist):
        df_m = df_monthly.copy()
        df_m[col_month] = pd.to_datetime(df_m[col_month], errors="coerce")
        df_m = df_m[df_m[col_month].notna()]
        for sku, sku_df in df_m.groupby(col_sku):
            value_col = col_forecast if col_forecast in sku_df.columns else col_hist
            series = sku_df.groupby(col_month)[value_col].sum().sort_index()
            if series.empty:
                continue
            if value_col == col_forecast:
                series = _filter_forecast_months(series)
                if series.empty:
                    continue
            last = series.tail(6)
            series_payload = {
                "fc_x": [d.strftime("%Y-%m-%d") for d in series.index],
                "fc_y": [float(v) for v in series.values],
                "x": [d.strftime("%Y-%m-%d") for d in series.index],
                "y": [float(v) for v in series.values],
            }
            if row_type_col and row_type_col in sku_df.columns:
                hist_mask = sku_df[row_type_col].astype(str).str.upper().isin(["HIST", "CATCHUP"])
                fc_mask = sku_df[row_type_col].astype(str).str.upper().isin(["FCST", "CATCHUP"])
                hist_series = sku_df.loc[hist_mask].groupby(col_month)[col_hist].sum().sort_index() if col_hist else None
                fc_series = sku_df.loc[fc_mask].groupby(col_month)[col_forecast].sum().sort_index() if col_forecast else None
                if fc_series is not None:
                    fc_series = _filter_forecast_months(fc_series)
                if hist_series is not None and not hist_series.empty:
                    series_payload["hist_x"] = [d.strftime("%Y-%m-%d") for d in hist_series.index]
                    series_payload["hist_y"] = [float(v) for v in hist_series.values]
                if fc_series is not None and not fc_series.empty:
                    series_payload["fc_x"] = [d.strftime("%Y-%m-%d") for d in fc_series.index]
                    series_payload["fc_y"] = [float(v) for v in fc_series.values]
                    series_payload["x"] = [d.strftime("%Y-%m-%d") for d in fc_series.index]
                    series_payload["y"] = [float(v) for v in fc_series.values]
            elif col_hist and col_hist in sku_df.columns:
                hist_series = sku_df[sku_df[col_hist].notna()].groupby(col_month)[col_hist].sum().sort_index()
                if not hist_series.empty:
                    series_payload["hist_x"] = [d.strftime("%Y-%m-%d") for d in hist_series.index]
                    series_payload["hist_y"] = [float(v) for v in hist_series.values]
            sku_series[str(sku)] = series_payload
            # Use historical series for first_sales_date if available, otherwise fall back to filtered series
            if hist_series is not None and not hist_series.empty:
                first_date = hist_series.index.min()
                last_date = hist_series.index.max()
            else:
                first_date = series.index.min() if not series.empty else pd.NaT
                last_date = series.index.max() if not series.empty else pd.NaT
            if pd.notna(first_date) and pd.notna(last_date):
                sku_first_dates[str(sku)] = first_date.strftime("%Y-%m-%d")
                sku_history_days[str(sku)] = max(0, int((last_date - first_date).days))

    items: List[Dict] = []
    for _, row in df_results.iterrows():
        sku = str(row.get("sku", ""))
        daily_avg = float(row.get("daily_mean_demand", 0.0) or 0.0)
        inv_pos = float(row.get("inventory_position", 0.0) or 0.0)
        days_cover = inv_pos / daily_avg if daily_avg > 0 else 0.0
        segmentation = {
            "maturity": str(row.get("sku_abc", "")) or "Legacy",
            "demand_pattern": str(row.get("demand_class", "")),
            "adi": float(row.get("adi", 0.0) or 0.0),
            "cv2": float(row.get("cv2", 0.0) or 0.0),
            "first_sales_date": sku_first_dates.get(sku),
            "history_days": sku_history_days.get(sku, 0),
        }
        items.append(
            {
                "item_number": sku,
                "description": str(row.get("description", "")),
                "vendor_name": str(row.get("vendor_name", "")) or str(row.get("collection", "")),
                "vendor_number": str(row.get("vendor_number", "")),
                "collection": str(row.get("collection", "")),
                "available": float(row.get("available_sf", 0.0) or 0.0),
                "on_po": float(row.get("on_po_sf", 0.0) or 0.0),
                "backorder": float(row.get("backorder_sf", 0.0) or 0.0),
                "inventory_position": inv_pos,
                "lead_time_days": int(row.get("lead_time_days", 0) or 0),
                "lead_time_source": str(row.get("lead_time_source", "")),
                "lt_demand": float(row.get("lead_time_mean_demand", 0.0) or 0.0),
                "suggested_order_qty": float(row.get("reorder_quantity", 0.0) or 0.0),
                "forecast_series": sku_series.get(sku, {}),
                "segmentation": segmentation,
                "daily_avg_demand": daily_avg,
                "days_of_cover": float(days_cover),
            }
        )

    df_queue = df_results.copy()
    if "reorder_point" in df_queue.columns and "inventory_position" in df_queue.columns:
        df_queue["risk_gap"] = df_queue["inventory_position"] - df_queue["reorder_point"]
        df_queue = df_queue.sort_values("risk_gap")
    queue_rows: List[Dict] = []
    for _, row in df_queue.head(8).iterrows():
        queue_rows.append(
            {
                "item_number": str(row.get("sku", "")),
                "description": str(row.get("description", "")),
                "vendor_name": str(row.get("vendor_name", "")) or str(row.get("collection", "")),
                "vendor_number": str(row.get("vendor_number", "")),
                "collection": str(row.get("collection", "")),
                "available": float(row.get("available_sf", 0.0) or 0.0),
                "on_po": float(row.get("on_po_sf", 0.0) or 0.0),
                "backorder": float(row.get("backorder_sf", 0.0) or 0.0),
                "inventory_position": float(row.get("inventory_position", 0.0) or 0.0),
                "lead_time_days": int(row.get("lead_time_days", 0) or 0),
                "lt_demand": float(row.get("lead_time_mean_demand", 0.0) or 0.0),
            }
        )

    payload["items"] = items
    payload["queue"] = queue_rows
    payload["Inventory_Metrics"] = _df_to_records(df_results)
    payload["Monthly_Projections"] = _df_to_records(df_monthly)
    if df_arrivals is not None and not df_arrivals.empty:
        payload["arrivals"] = _df_to_records(df_arrivals)
    else:
        payload["arrivals"] = []
    payload["member_snapshots"] = member_snapshots or []
    payload["generated_at"] = datetime.now().isoformat()
    return payload

def _write_webapp_json(payload: Dict, output_path: Path) -> None:
    def _sanitize_jsonable(value: Any) -> Any:
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return value
        if isinstance(value, (pd.Timestamp, datetime, date, np.datetime64)):
            if pd.isna(value):
                return None
            try:
                return pd.to_datetime(value).strftime("%Y-%m-%d")
            except Exception:
                return None
        if value is pd.NaT:
            return None
        if isinstance(value, dict):
            return {k: _sanitize_jsonable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sanitize_jsonable(v) for v in value]
        return value

    try:
        safe_payload = _sanitize_jsonable(payload)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(safe_payload, f, indent=2, allow_nan=False)
        print(f"  Wrote webapp JSON: {output_path}")
    except Exception as exc:
        print(f"  WARNING: Failed to write webapp JSON ({exc})")

@st.cache_data(ttl=300)
def _load_webapp_payload() -> Dict:
    if WEBAPP_JSON_PATH.exists():
        try:
            with open(WEBAPP_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return _demo_webapp_payload()
    return _demo_webapp_payload()

@st.cache_data(ttl=300)
def _load_sundries_payload() -> Dict:
    if SUNDRIES_JSON_PATH.exists():
        try:
            with open(SUNDRIES_JSON_PATH, "r", encoding="utf-8") as f:
                return _apply_lead_time_overrides_to_payload(json.load(f))
        except Exception:
            return _demo_webapp_payload()
    return _demo_webapp_payload()


@st.cache_data(ttl=300)
def _load_moulding_payload() -> Dict:
    """Load moulding data for Dilan tab from mouldingwebappJSON."""
    if MOULDING_JSON_PATH.exists():
        try:
            with open(MOULDING_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return _demo_webapp_payload()
    return _demo_webapp_payload()


def _load_refresh_status() -> dict:
    """Load the status file written by refresh_forecast_data.py, if present."""
    path = Path(__file__).resolve().parent / "_refresh_status.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _get_forecast_script_and_progress_file(tab_name: str) -> Tuple[Optional[Path], Optional[Path]]:
    """Get the forecast script path and progress file path for a given tab."""
    base_dir = Path(__file__).resolve().parent

    if tab_name in ("Ilsy", "Niko"):
        # Both Ilsy and Niko use the flooring forecast (this file's run_inventory_planning)
        return (Path(__file__).resolve(), FLOORING_PROGRESS_FILE)
    elif tab_name == "Carlos":
        return (base_dir / "sundrieswebapp_refactored.py", SUNDRIES_PROGRESS_FILE)
    elif tab_name == "Dilan":
        return (base_dir / "mouldingwebapp_refactored.py", MOULDING_PROGRESS_FILE)
    return (None, None)


def _run_forecast_with_progress(tab_name: str) -> bool:
    """
    Run the forecast script for the given tab and display progress.
    Returns True if successful, False otherwise.
    """
    script_path, progress_file = _get_forecast_script_and_progress_file(tab_name)

    if script_path is None:
        st.error(f"No forecast script configured for tab: {TAB_DISPLAY_NAMES.get(tab_name, tab_name)}")
        return False

    # For Ilsy/Niko, we run this file itself but only the forecast function
    # For Carlos/Dilan, we run external scripts
    if tab_name in ("Ilsy", "Niko"):
        # Run the flooring forecast inline (same process)
        # Clear any existing progress file
        if progress_file and progress_file.exists():
            try:
                progress_file.unlink()
            except Exception:
                pass

        progress_bar = st.progress(0)
        status_text = st.empty()
        status_text.text("Starting flooring forecast...")

        # Run in a thread to allow UI updates
        import threading
        forecast_complete = threading.Event()
        forecast_error = [None]

        def run_forecast():
            try:
                run_inventory_planning()
            except Exception as e:
                forecast_error[0] = str(e)
            finally:
                forecast_complete.set()

        thread = threading.Thread(target=run_forecast)
        thread.start()

        # Poll progress file while forecast runs
        while not forecast_complete.is_set():
            if progress_file and progress_file.exists():
                try:
                    with open(progress_file, 'r') as f:
                        progress = json.load(f)
                    current = progress.get("current", 0)
                    total = progress.get("total", 1)
                    sku = progress.get("sku", "")
                    pct = current / total if total > 0 else 0
                    progress_bar.progress(pct)
                    status_text.text(f"Processing {sku} ({current}/{total})")
                except Exception:
                    pass
            time.sleep(0.3)

        thread.join()

        if forecast_error[0]:
            st.error(f"Forecast error: {forecast_error[0]}")
            return False

        progress_bar.progress(1.0)
        status_text.text("Forecast complete!")
        return True

    else:
        # Run external script as subprocess
        if not script_path.exists():
            st.error(f"Forecast script not found: {script_path}")
            return False

        # Clear any existing progress file
        if progress_file and progress_file.exists():
            try:
                progress_file.unlink()
            except Exception:
                pass

        progress_bar = st.progress(0)
        status_text = st.empty()
        status_text.text(f"Starting {tab_name.lower()} forecast...")

        # Start subprocess
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(script_path.parent)
            )
        except Exception as e:
            st.error(f"Failed to start forecast: {e}")
            return False

        # Poll progress file while subprocess runs
        while proc.poll() is None:
            if progress_file and progress_file.exists():
                try:
                    with open(progress_file, 'r') as f:
                        progress = json.load(f)
                    current = progress.get("current", 0)
                    total = progress.get("total", 1)
                    sku = progress.get("sku", "")
                    pct = current / total if total > 0 else 0
                    progress_bar.progress(pct)
                    status_text.text(f"Processing {sku} ({current}/{total})")
                except Exception:
                    pass
            time.sleep(0.3)

        # Check return code
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            st.error(f"Forecast failed with error code {proc.returncode}")
            if stderr:
                st.code(stderr[:500])  # Show first 500 chars of error
            return False

        progress_bar.progress(1.0)
        status_text.text("Forecast complete!")
        return True


@st.cache_data(ttl=300)
def _load_strip_sku_list() -> set:
    """Load list of SKUs for Niko tab from StripSKUList.xlsx"""
    strip_path = Path(__file__).resolve().parent / STRIP_SKU_LIST_FILE
    if not strip_path.exists():
        return set()
    try:
        # SKUs are in the "Who Produces" sheet, first column, starting at row 2 (skip header)
        df = pd.read_excel(strip_path, sheet_name="Who Produces", header=0)
        if df.shape[0] == 0:
            return set()
        sku_col = df.columns[0]  # "Item Number" column
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        sku_list = set(df[sku_col].dropna())
        sku_list = {sku for sku in sku_list if sku and sku != 'NAN' and len(sku) > 0}
        return sku_list
    except Exception:
        return set()

def _load_strip_sku_details() -> Dict[str, Dict]:
    """Load SKU details (Thickness, Width, Species, Grade/Cut, Edge, Bundles) from StripSKUList.xlsx"""
    strip_path = Path(__file__).resolve().parent / STRIP_SKU_LIST_FILE
    if not strip_path.exists():
        return {}
    try:
        df = pd.read_excel(strip_path, sheet_name="Who Produces", header=0)
        if df.shape[0] == 0:
            return {}
        # Normalize the Item Number column
        df["Item Number"] = df["Item Number"].astype(str).str.strip().str.upper()
        # Build a dict mapping SKU -> details
        details = {}
        for _, row in df.iterrows():
            sku = row["Item Number"]
            if sku and sku != 'NAN':
                details[sku] = {
                    "Description": str(row.get("Description", "")) if pd.notna(row.get("Description")) else "",
                    "Thickness": str(row.get("Thickness", "")) if pd.notna(row.get("Thickness")) else "",
                    "Width": str(row.get("Width", "")) if pd.notna(row.get("Width")) else "",
                    "Species": str(row.get("Species", "")) if pd.notna(row.get("Species")) else "",
                    "Grade/Cut": str(row.get("Grade/Cut", "")) if pd.notna(row.get("Grade/Cut")) else "",
                    "Edge": str(row.get("Edge", "")) if pd.notna(row.get("Edge")) else "",
                    "Bundles": str(row.get("Bundles", "")) if pd.notna(row.get("Bundles")) else "",
                }
        return details
    except Exception:
        return {}

def _load_strip_vendor_list() -> List[str]:
    """Load vendor names from StripSKUList.xlsx (for email tab)."""
    strip_path = Path(__file__).resolve().parent / STRIP_SKU_LIST_FILE
    if not strip_path.exists():
        return []
    try:
        df = pd.read_excel(strip_path, sheet_name="for email", header=0)
        if df.shape[0] == 0:
            return []
        vendor_col = _get_first_column(df, ["Vendor Name", "Vendor", "VendorName"])
        if not vendor_col:
            return []
        vendors = (
            df[vendor_col]
            .astype(str)
            .str.strip()
            .replace("nan", "")
            .replace("None", "")
        )
        vendors = sorted({v for v in vendors if v})
        return vendors
    except Exception:
        return []


def _coerce_positive_float(value: Any) -> Optional[float]:
    try:
        numeric = float(str(value).replace(",", "").strip())
    except Exception:
        return None
    if not np.isfinite(numeric) or numeric <= 0:
        return None
    return numeric


def _canonical_niko_vendor_name(value: Any) -> str:
    vendor = normalize_vendor_name(value)
    if not vendor:
        return ""
    vendor_key = vendor.upper()
    vendor_key = NIKO_VENDOR_ALIASES.get(vendor_key, vendor_key)
    preferred = {name.upper(): name for name in STRIP_VENDOR_COLUMNS}
    preferred["APPALACHIAN"] = "Appalachian"
    preferred["MERRICK"] = "Merrick"
    return preferred.get(vendor_key, vendor)


def _read_vendor_freight_from_row(row: pd.Series, vendor_column_map: Dict[str, str]) -> Dict[str, float]:
    freight_costs: Dict[str, float] = {}
    for source_col, vendor in vendor_column_map.items():
        value = _coerce_positive_float(row.get(source_col))
        if value is not None:
            freight_costs[vendor] = value
    return freight_costs


def _extract_workbook_freight_costs(
    workbook_path: Path,
    current_pricing_df: pd.DataFrame,
    item_col: Optional[str],
    vendor_column_map: Dict[str, str],
) -> Tuple[Dict[str, float], List[str]]:
    freight_costs: Dict[str, float] = {}
    warnings_out: List[str] = []

    if item_col and item_col in current_pricing_df.columns:
        item_keys = current_pricing_df[item_col].map(normalize_item_key)
        freight_row = current_pricing_df[item_keys == "FREIGHT"]
        if not freight_row.empty:
            freight_costs.update(_read_vendor_freight_from_row(freight_row.iloc[0], vendor_column_map))
            if freight_costs:
                return freight_costs, warnings_out

    try:
        workbook = pd.ExcelFile(workbook_path)
    except Exception as exc:
        return {}, [f"Could not open {UNFINISHED_PRICING_FILE} to read Freight data: {exc}"]

    for sheet_name in workbook.sheet_names:
        try:
            df = pd.read_excel(workbook_path, sheet_name=sheet_name, header=0)
        except Exception:
            continue
        if df.empty:
            continue

        freight_col = _get_first_column(df, ["Freight", "FREIGHT"])
        vendor_col = _get_first_column(df, ["Vendor Name", "Vendor", "VendorName", "VENDOR NAME", "VENDOR"])
        item_number_col = _get_first_column(df, ["Item Number", "ITEM NUMBER", "ITEM_NUMBER"])

        if freight_col and vendor_col:
            for _, row in df.iterrows():
                vendor = _canonical_niko_vendor_name(row.get(vendor_col))
                value = _coerce_positive_float(row.get(freight_col))
                if vendor and value is not None:
                    freight_costs[vendor] = value
            if freight_costs:
                return freight_costs, warnings_out

        if freight_col and item_number_col:
            freight_row = df[df[item_number_col].map(normalize_item_key) == "FREIGHT"]
            if not freight_row.empty:
                freight_costs.update(_read_vendor_freight_from_row(freight_row.iloc[0], vendor_column_map))
                if freight_costs:
                    return freight_costs, warnings_out

        if freight_col and not vendor_col and len(df.columns) >= 2:
            first_col = df.columns[0]
            vendor_matches = 0
            candidate_map: Dict[str, float] = {}
            for _, row in df.iterrows():
                vendor = _canonical_niko_vendor_name(row.get(first_col))
                value = _coerce_positive_float(row.get(freight_col))
                if vendor and value is not None:
                    vendor_matches += 1
                    candidate_map[vendor] = value
            if vendor_matches >= 2:
                freight_costs.update(candidate_map)
                return freight_costs, warnings_out

    warnings_out.append(
        f"No vendor Freight data was found in {UNFINISHED_PRICING_FILE}. "
        "You can still type freight into the Freight row before optimizing."
    )
    return freight_costs, warnings_out


def _lookup_strip_prices(sku: str, pricing_map: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    return _lookup_prices_for_sku_core(
        sku,
        pricing_map,
        sku_to_consolidated=SKU_TO_CONSOLIDATED,
        consolidation_groups=SKU_CONSOLIDATION_GROUPS,
    )


def _aggregate_group_cost_baseline(
    group_key: str,
    member_skus: List[str],
    member_baselines: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    records = [member_baselines.get(normalize_item_key(member), {}) for member in member_skus]
    records = [record for record in records if record]
    if not records:
        return {}

    weights = [max(float(record.get("current_available_sf") or 0.0), 0.0) for record in records]
    if sum(weights) <= 0:
        weights = [1.0] * len(records)

    avg_inventory_numer = 0.0
    avg_inventory_denom = 0.0
    for record, weight in zip(records, weights):
        avg_inventory_cost = record.get("avg_inventory_cost")
        if avg_inventory_cost is None:
            continue
        avg_inventory_cost = float(avg_inventory_cost)
        if avg_inventory_cost <= 0:
            continue
        avg_inventory_numer += avg_inventory_cost * weight
        avg_inventory_denom += weight

    po_records = [record for record in records if float(record.get("last_received_po_cost") or 0.0) > 0]
    latest_po = None
    if po_records:
        latest_po = max(
            po_records,
            key=lambda record: pd.Timestamp(record.get("last_received_date")) if record.get("last_received_date") else pd.Timestamp.min,
        )

    return {
        "sku": normalize_item_key(group_key),
        "last_received_po_cost": float(latest_po.get("last_received_po_cost") or 0.0) if latest_po else None,
        "last_received_date": latest_po.get("last_received_date") if latest_po else None,
        "avg_inventory_cost": (avg_inventory_numer / avg_inventory_denom) if avg_inventory_denom > 0 else None,
        "current_available_sf": sum(weights),
        "members": [normalize_item_key(member) for member in member_skus],
    }


@st.cache_data(ttl=300)
def _load_niko_pricing_inputs() -> Dict[str, Any]:
    workbook_path = Path(__file__).resolve().parent / UNFINISHED_PRICING_FILE
    return _load_niko_pricing_inputs_core(
        workbook_path=workbook_path,
        default_vendor_names=list(dict.fromkeys(STRIP_VENDOR_COLUMNS)),
        vendor_aliases=NIKO_VENDOR_ALIASES,
    )


@st.cache_data(ttl=300)
def _load_intermodal_freight_costs() -> Dict[str, float]:
    """Read per-vendor container rates from the Current Month tab of Intermodal Freight.xlsx.

    Each data row in that tab has: Origin, ST, Destination, ST, Size, Rate, Vendor.
    Only rows with a recognized canonical vendor name (STRIP_VENDOR_COLUMNS) are used.
    """
    if not INTERMODAL_FREIGHT_FILE.exists():
        return {}
    try:
        df = pd.read_excel(INTERMODAL_FREIGHT_FILE, sheet_name="Current Month", header=None)
    except Exception:
        return {}

    # Find the header row by scanning for the cell containing "RATE"
    header_row_idx: Optional[int] = None
    rate_col_idx: Optional[int] = None
    for i, row in df.iterrows():
        upper_vals = [str(v).strip().upper() for v in row.values]
        if "RATE" in upper_vals:
            header_row_idx = i
            rate_col_idx = upper_vals.index("RATE")
            break

    if header_row_idx is None or rate_col_idx is None:
        return {}

    # Vendor column is the column immediately after Rate
    vendor_col_idx = rate_col_idx + 1
    if vendor_col_idx >= len(df.columns):
        return {}

    freight_costs: Dict[str, float] = {}
    for i in range(header_row_idx + 1, len(df)):
        row = df.iloc[i]
        rate = _coerce_positive_float(row.iloc[rate_col_idx])
        if rate is None:
            continue
        raw_vendor = row.iloc[vendor_col_idx]
        if pd.isna(raw_vendor) or not str(raw_vendor).strip():
            continue
        vendor = _canonical_niko_vendor_name(raw_vendor)
        if vendor:
            freight_costs[vendor] = rate

    return freight_costs


@st.cache_data(ttl=3600)
def _load_niko_cost_baselines(selected_skus: Tuple[str, ...]) -> Dict[str, Any]:
    normalized_skus = [normalize_item_key(sku) for sku in selected_skus if normalize_item_key(sku)]
    if not normalized_skus:
        return {"baselines": {}, "error": ""}

    physical_skus: List[str] = []
    for sku in normalized_skus:
        if sku in SKU_CONSOLIDATION_GROUPS:
            physical_skus.extend(SKU_CONSOLIDATION_GROUPS[sku])
        else:
            physical_skus.append(sku)
    physical_skus = sorted({normalize_item_key(sku) for sku in physical_skus if normalize_item_key(sku)})
    sku_list_str = _build_in_list(physical_skus)

    sql = f"""
    WITH LatestLandedCost AS (
        SELECT
            TRIM(R.IRITEM) AS ITEM_NUMBER,
            R.IRDATE AS MOST_RECENT_RECEIVED_DATE,
            DECIMAL(R.IRCOST, 18, 4) AS MOST_RECENT_LANDED_COST
        FROM GSFL2K.ITEMRECH R
        WHERE R.IRCO = 1
          AND TRIM(R.IRSRC) = 'P'
          AND R.IRQTY > 0
          AND R.IRCOST > 0
          AND TRIM(R.IRITEM) IN ({sku_list_str})
          AND R.IRRECNBR = (
              SELECT MAX(R2.IRRECNBR)
              FROM GSFL2K.ITEMRECH R2
              WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
                AND R2.IRCO = 1
                AND TRIM(R2.IRSRC) = 'P'
                AND R2.IRQTY > 0
                AND R2.IRCOST > 0
                AND R2.IRRECNBR > 0
          )
    ),
    AvgInvCost AS (
        SELECT
            TRIM(D.IDITEM) AS ITEM_NUMBER,
            DECIMAL(
                DECIMAL(SUM(
                    DECIMAL(D.IDCOST, 18, 6) *
                    DECIMAL(
                        CASE
                            WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                                THEN D.IDQOH
                            ELSE D.IDQOH * IM.IMFACT
                        END,
                        18,
                        6
                    )
                ), 18, 6) /
                NULLIF(
                    DECIMAL(SUM(
                        DECIMAL(
                            CASE
                                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                                    THEN D.IDQOH
                                ELSE D.IDQOH * IM.IMFACT
                            END,
                            18,
                            6
                        )
                    ), 18, 6),
                    0
                ),
                18,
                4
            ) AS AVG_INVENTORY_COST
        FROM GSFL2K.ITEMDETL D
        JOIN GSFL2K.ITEMMAST IM
          ON TRIM(IM.IMITEM) = TRIM(D.IDITEM)
        WHERE D.IDCO = 1
          AND D.IDLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
          AND COALESCE(D.IDDELT, '') <> 'D'
          AND D.IDQOH > 0
          AND D.IDCOST IS NOT NULL
          AND D.IDCOST <> 0
          AND TRIM(D.IDITEM) IN ({sku_list_str})
        GROUP BY TRIM(D.IDITEM)
    ),
    CurrentAvailableRaw AS (
        SELECT
            TRIM(B.IBITEM) AS ITEM_NUMBER,
            SUM(B.IBQOH) AS QTY_ON_HAND,
            SUM(B.IBQOO) AS QTY_COMMITTED_RAW
        FROM GSFL2K.ITEMBAL B
        WHERE B.IBCO = 1
          AND B.IBLOC NOT IN (90, 17, 41, 46)
          AND TRIM(B.IBITEM) IN ({sku_list_str})
        GROUP BY TRIM(B.IBITEM)
    ),
    NonOrderCommitments AS (
        SELECT
            TRIM(OLITEM) AS ITEM_NUMBER,
            SUM(COALESCE(OLQSHP, 0)) AS NON_ORDER_COMMITTED_QTY
        FROM GSFL2K.OOLINE
        WHERE (
            OLCUST LIKE '%TRANSFER%'
            OR OLCUST LIKE '%OMP000%'
            OR OLCUST LIKE '%INV000%'
            OR OLCUST LIKE '%OLD001%'
          )
          AND OLLOC <> 90
          AND TRIM(OLITEM) IN ({sku_list_str})
        GROUP BY TRIM(OLITEM)
    ),
    CurrentAvailable AS (
        SELECT
            CAR.ITEM_NUMBER,
            DECIMAL(
                CASE
                    WHEN COALESCE(IM.IMFACT, 0) = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                        THEN (COALESCE(CAR.QTY_ON_HAND, 0) - CASE
                            WHEN COALESCE(CAR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0) < 0
                                THEN 0
                            ELSE COALESCE(CAR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0)
                        END)
                    ELSE (COALESCE(CAR.QTY_ON_HAND, 0) - CASE
                            WHEN COALESCE(CAR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0) < 0
                                THEN 0
                            ELSE COALESCE(CAR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0)
                        END) * IM.IMFACT
                END,
                18,
                2
            ) AS CURRENT_AVAILABLE_SF
        FROM CurrentAvailableRaw CAR
        JOIN GSFL2K.ITEMMAST IM
          ON TRIM(IM.IMITEM) = CAR.ITEM_NUMBER
        LEFT JOIN NonOrderCommitments NOC
          ON NOC.ITEM_NUMBER = CAR.ITEM_NUMBER
    )
    SELECT
        TRIM(IM.IMITEM) AS ITEM_NUMBER,
        DECIMAL(COALESCE(LC.MOST_RECENT_LANDED_COST, 0), 18, 4) AS LAST_RECEIVED_PO_COST,
        LC.MOST_RECENT_RECEIVED_DATE AS LAST_RECEIVED_DATE,
        DECIMAL(COALESCE(AC.AVG_INVENTORY_COST, 0), 18, 4) AS AVG_INVENTORY_COST,
        DECIMAL(COALESCE(CA.CURRENT_AVAILABLE_SF, 0), 18, 2) AS CURRENT_AVAILABLE_SF
    FROM GSFL2K.ITEMMAST IM
    LEFT JOIN LatestLandedCost LC
      ON TRIM(LC.ITEM_NUMBER) = TRIM(IM.IMITEM)
    LEFT JOIN AvgInvCost AC
      ON TRIM(AC.ITEM_NUMBER) = TRIM(IM.IMITEM)
    LEFT JOIN CurrentAvailable CA
      ON TRIM(CA.ITEM_NUMBER) = TRIM(IM.IMITEM)
    WHERE TRIM(IM.IMITEM) IN ({sku_list_str})
    """

    try:
        conn = _connect()
        try:
            df = _read_sql_silent(sql, conn)
        finally:
            conn.close()
    except Exception as exc:
        return {"baselines": {}, "error": f"Cost alert baselines could not be loaded: {exc}"}

    member_baselines: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        sku_key = normalize_item_key(row.get("ITEM_NUMBER"))
        if not sku_key:
            continue
        member_baselines[sku_key] = {
            "sku": sku_key,
            "last_received_po_cost": float(row.get("LAST_RECEIVED_PO_COST") or 0.0) or None,
            "last_received_date": pd.Timestamp(row.get("LAST_RECEIVED_DATE")).date().isoformat()
            if pd.notna(row.get("LAST_RECEIVED_DATE")) else None,
            "avg_inventory_cost": float(row.get("AVG_INVENTORY_COST") or 0.0) or None,
            "current_available_sf": float(row.get("CURRENT_AVAILABLE_SF") or 0.0),
        }

    baselines: Dict[str, Dict[str, Any]] = {}
    for sku in normalized_skus:
        if sku in member_baselines:
            baselines[sku] = member_baselines[sku]
        elif sku in SKU_CONSOLIDATION_GROUPS:
            aggregate = _aggregate_group_cost_baseline(sku, SKU_CONSOLIDATION_GROUPS[sku], member_baselines)
            if aggregate:
                baselines[sku] = aggregate

    return {"baselines": baselines, "error": ""}


@st.cache_data(ttl=3600, show_spinner=False)
def _load_carlos_history_margins(item_tuple: Tuple[str, ...]) -> pd.DataFrame:
    """Fetch cost, PO history, and sale price for Carlos History & Margins card."""
    if not item_tuple:
        return pd.DataFrame()
    sku_list_str = _build_in_list(list(item_tuple))
    sql = f"""
    WITH LatestLandedCost AS (
        SELECT
            TRIM(R.IRITEM)            AS ITEM_NUMBER,
            R.IRDATE                  AS LAST_PO_DATE,
            DECIMAL(R.IRCOST, 18, 4)  AS LAST_PO_COST
        FROM GSFL2K.ITEMRECH R
        WHERE R.IRCO = 1
          AND TRIM(R.IRSRC) = 'P'
          AND R.IRQTY > 0
          AND R.IRCOST > 0
          AND TRIM(R.IRITEM) IN ({sku_list_str})
          AND R.IRRECNBR = (
              SELECT MAX(R2.IRRECNBR)
              FROM GSFL2K.ITEMRECH R2
              WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
                AND R2.IRCO = 1
                AND TRIM(R2.IRSRC) = 'P'
                AND R2.IRQTY > 0
                AND R2.IRCOST > 0
                AND R2.IRRECNBR > 0
          )
    ),
    Cost30DaysAgo AS (
        SELECT
            TRIM(R.IRITEM)            AS ITEM_NUMBER,
            R.IRDATE                  AS COST_30D_DATE,
            DECIMAL(R.IRCOST, 18, 4)  AS COST_30D_AGO
        FROM GSFL2K.ITEMRECH R
        WHERE R.IRCO = 1
          AND TRIM(R.IRSRC) = 'P'
          AND R.IRQTY > 0
          AND R.IRCOST > 0
          AND R.IRDATE <= (CURRENT_DATE - 30 DAYS)
          AND TRIM(R.IRITEM) IN ({sku_list_str})
          AND R.IRRECNBR = (
              SELECT MAX(R2.IRRECNBR)
              FROM GSFL2K.ITEMRECH R2
              WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
                AND R2.IRCO = 1
                AND TRIM(R2.IRSRC) = 'P'
                AND R2.IRQTY > 0
                AND R2.IRCOST > 0
                AND R2.IRDATE <= (CURRENT_DATE - 30 DAYS)
                AND R2.IRRECNBR > 0
          )
    ),
    AvgInvCost AS (
        SELECT
            TRIM(D.IDITEM) AS ITEM_NUMBER,
            DECIMAL(
                DECIMAL(SUM(
                    DECIMAL(D.IDCOST, 18, 6) *
                    DECIMAL(
                        CASE
                            WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                                THEN D.IDQOH
                            ELSE D.IDQOH * IM.IMFACT
                        END, 18, 6
                    )
                ), 18, 6) /
                NULLIF(DECIMAL(SUM(
                    DECIMAL(
                        CASE
                            WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                                THEN D.IDQOH
                            ELSE D.IDQOH * IM.IMFACT
                        END, 18, 6
                    )
                ), 18, 6), 0),
                18, 4
            ) AS AVG_INV_COST
        FROM GSFL2K.ITEMDETL D
        JOIN GSFL2K.ITEMMAST IM ON TRIM(IM.IMITEM) = TRIM(D.IDITEM)
        WHERE D.IDCO = 1
          AND D.IDLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
          AND COALESCE(D.IDDELT, '') <> 'D'
          AND D.IDQOH > 0
          AND D.IDCOST IS NOT NULL
          AND D.IDCOST <> 0
          AND TRIM(D.IDITEM) IN ({sku_list_str})
        GROUP BY TRIM(D.IDITEM)
    )
    SELECT
        TRIM(IM.IMITEM)                               AS ITEM_NUMBER,
        DECIMAL(COALESCE(IM.IMP1, 0), 18, 4)          AS SALE_PRICE,
        DECIMAL(COALESCE(AC.AVG_INV_COST, 0), 18, 4)  AS AVG_INV_COST,
        DECIMAL(COALESCE(LC.LAST_PO_COST, 0), 18, 4)  AS LAST_PO_COST,
        LC.LAST_PO_DATE,
        DECIMAL(COALESCE(C30.COST_30D_AGO, 0), 18, 4) AS COST_30D_AGO,
        C30.COST_30D_DATE
    FROM GSFL2K.ITEMMAST IM
    LEFT JOIN AvgInvCost AC        ON TRIM(AC.ITEM_NUMBER) = TRIM(IM.IMITEM)
    LEFT JOIN LatestLandedCost LC  ON TRIM(LC.ITEM_NUMBER) = TRIM(IM.IMITEM)
    LEFT JOIN Cost30DaysAgo C30    ON TRIM(C30.ITEM_NUMBER) = TRIM(IM.IMITEM)
    WHERE TRIM(IM.IMITEM) IN ({sku_list_str})
    """
    try:
        conn = _connect()
        try:
            return _read_sql_silent(sql, conn)
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()


# ─── 3M Ordering Processor ──────────────────────────────────────────────────
_3M_BRANCH_LOCS = [3, 4, 5, 6, 8, 9]
_3M_MIN_ORDER_VALUE = 2500.0
# Sold to customers as packs of 50; 3M ships to us in boxes of 250 (5 packs).
# Order quantities for these SKUs must be rounded up to the nearest multiple of 5.
_3M_PKG250_SKUS = {"AB3M20576", "AB3M86395", "AB3M86396", "AB3M86434"}


@st.cache_data(ttl=900, show_spinner=False)
def _fetch_3m_ordering_data(as_of_date_str: str) -> dict:
    """
    Live DB fetch for the 3M Ordering Processor.  Cached 15 minutes.
    Returns dict of DataFrames: items, sales, inventory, cost.
    """
    try:
        conn = _connect()
    except Exception:
        return {}
    try:
        as_of = pd.Timestamp(as_of_date_str).normalize()
        d90   = (as_of - pd.Timedelta(days=90)).date()
        d30   = (as_of - pd.Timedelta(days=30)).date()

        df_items = _read_sql_silent("""
            SELECT TRIM(IM.IMITEM)  AS ITEM_NUMBER,
                   TRIM(IM.IMDESC) AS DESCRIPTION,
                   TRIM(IM.IMSKEY) AS VENDOR_PART_NUMBER,
                   COALESCE(IM.IMORDQ, 0) AS MIN_ORDER_QTY
            FROM GSFL2K.ITEMMAST IM
            WHERE TRIM(IM.IMVEND) = '3'
              AND TRIM(IM.IMUM2) NOT LIKE '%SF%'
              AND COALESCE(TRIM(IM.IMDELT), '') = 'A'
              AND COALESCE(TRIM(IM.IMDROP), '') = ''
              AND TRIM(IM.IMSI) = 'Y'
        """, conn)
        if df_items.empty:
            return {}

        in_clause = "'" + "','".join(df_items["ITEM_NUMBER"].str.strip().str.upper().tolist()) + "'"
        loc_list = ", ".join(str(loc) for loc in [1] + _3M_BRANCH_LOCS)
        transfer_customers = {loc: f"TRANSFER{loc:02d}" for loc in _3M_BRANCH_LOCS}
        transfer_cust_list = _build_in_list(list(transfer_customers.values()))
        transfer_dest_case = "\n".join(
            f"                       WHEN '{cust}' THEN {loc}"
            for loc, cust in sorted(transfer_customers.items())
        )

        df_sales = _read_sql_silent(f"""
            SELECT TRIM(L.SLITEM) AS ITEM_NUMBER,
                   H.SHLOC        AS LOCATION,
                   SUM(CASE WHEN H.SHIDAT >= ? THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_30D,
                   SUM(COALESCE(L.SLBLUO, 0))                                           AS UNITS_90D
            FROM GSFL2K.SHLINE L
            JOIN GSFL2K.SHHEAD H
              ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC
             AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
            WHERE H.SHIDAT >= ?
              AND TRIM(L.SLUM2) NOT LIKE '%SF%'
              AND COALESCE(L.SLBLUO, 0) <> 0
              AND H.SHLOC IN (1, 3, 4, 5, 6, 8, 9)
              AND H.SHCUST NOT LIKE '%TRANSFER%'
              AND H.SHCUST NOT LIKE '%OMP000%'
              AND H.SHCUST NOT LIKE '%INV000%'
              AND H.SHCUST NOT LIKE '%OLD001%'
              AND TRIM(L.SLITEM) IN ({in_clause})
            GROUP BY TRIM(L.SLITEM), H.SHLOC
        """, conn, params=[d30, d90])

        df_inv = _read_sql_silent(f"""
            WITH InvRaw AS (
                SELECT TRIM(B.IBITEM) AS ITEM_NUMBER,
                       B.IBLOC        AS LOCATION,
                       SUM(COALESCE(B.IBQOH, 0)) AS QTY_ON_HAND,
                       SUM(COALESCE(B.IBQOO, 0)) AS QTY_COMMITTED_RAW
                FROM GSFL2K.ITEMBAL B
                WHERE B.IBLOC IN (1, 3, 4, 5, 6, 8, 9)
                  AND TRIM(B.IBITEM) IN ({in_clause})
                GROUP BY TRIM(B.IBITEM), B.IBLOC
            ),
            NonOrderCommitments AS (
                SELECT TRIM(OLITEM) AS ITEM_NUMBER,
                       OLLOC        AS LOCATION,
                       SUM(COALESCE(OLQSHP, 0)) AS NON_ORDER_COMMITTED_QTY
                FROM GSFL2K.OOLINE
                WHERE (
                    OLCUST LIKE '%TRANSFER%'
                    OR OLCUST LIKE '%OMP000%'
                    OR OLCUST LIKE '%INV000%'
                    OR OLCUST LIKE '%OLD001%'
                  )
                  AND OLLOC IN (1, 3, 4, 5, 6, 8, 9)
                  AND TRIM(OLITEM) IN ({in_clause})
                GROUP BY TRIM(OLITEM), OLLOC
            )
            SELECT IR.ITEM_NUMBER,
                   IR.LOCATION,
                   IR.QTY_ON_HAND - CASE
                       WHEN COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0) < 0
                           THEN 0
                       ELSE COALESCE(IR.QTY_COMMITTED_RAW, 0) - COALESCE(NOC.NON_ORDER_COMMITTED_QTY, 0)
                   END AS AVAILABLE_QTY
            FROM InvRaw IR
            LEFT JOIN NonOrderCommitments NOC
              ON NOC.ITEM_NUMBER = IR.ITEM_NUMBER
             AND NOC.LOCATION = IR.LOCATION
        """, conn)

        df_cost = _read_sql_silent(f"""
            SELECT TRIM(R.IRITEM)           AS ITEM_NUMBER,
                   DECIMAL(R.IRCOST, 18, 4) AS UNIT_COST
            FROM GSFL2K.ITEMRECH R
            WHERE R.IRCO = 1
              AND TRIM(R.IRSRC) = 'P'
              AND R.IRQTY   > 0
              AND R.IRCOST  > 0
              AND TRIM(R.IRITEM) IN ({in_clause})
              AND R.IRRECNBR = (
                  SELECT MAX(R2.IRRECNBR)
                  FROM GSFL2K.ITEMRECH R2
                  WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
                    AND R2.IRCO = 1
                    AND TRIM(R2.IRSRC) = 'P'
                    AND R2.IRQTY  > 0
                    AND R2.IRCOST > 0
                    AND R2.IRRECNBR > 0
              )
        """, conn)

        df_po_loc = _read_sql_silent(f"""
            WITH OpenPoLines AS (
                SELECT TRIM(L.PLITEM) AS ITEM_NUMBER,
                       COALESCE(L.PLBLUO, 0) AS ON_PO_QTY,
                       CASE
                           WHEN COALESCE(L.PLDLVLOC, 0) IN ({loc_list}) THEN L.PLDLVLOC
                           WHEN COALESCE(L.PLILOC, 0) IN ({loc_list}) THEN L.PLILOC
                           WHEN COALESCE(H.PHDLVLOC, 0) IN ({loc_list}) THEN H.PHDLVLOC
                           WHEN COALESCE(L.PLLOC, 0) IN ({loc_list}) THEN L.PLLOC
                           WHEN COALESCE(H.PHLOC, 0) IN ({loc_list}) THEN H.PHLOC
                           ELSE NULL
                       END AS RECEIVING_LOCATION
                FROM GSFL2K.POLINE L
                LEFT JOIN GSFL2K.POHEAD H
                  ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
                WHERE L.PLDELT LIKE '%A%'
                  AND TRIM(L.PLITEM) IN ({in_clause})
                  AND (
                      COALESCE(L.PLDLVLOC, 0) IN ({loc_list})
                      OR COALESCE(L.PLILOC, 0) IN ({loc_list})
                      OR COALESCE(H.PHDLVLOC, 0) IN ({loc_list})
                      OR COALESCE(L.PLLOC, 0) IN ({loc_list})
                      OR COALESCE(H.PHLOC, 0) IN ({loc_list})
                  )
            )
            SELECT ITEM_NUMBER,
                   RECEIVING_LOCATION AS LOCATION,
                   SUM(ON_PO_QTY) AS ON_PO_QTY
            FROM OpenPoLines
            WHERE RECEIVING_LOCATION IS NOT NULL
            GROUP BY ITEM_NUMBER, RECEIVING_LOCATION
        """, conn)

        df_transfer_alloc = _read_sql_silent(f"""
            SELECT TRIM(L.OLITEM) AS ITEM_NUMBER,
                   CASE TRIM(H.OHCUST)
{transfer_dest_case}
                       ELSE 0
                   END AS LOCATION,
                   SUM(COALESCE(L.OLBLUB, 0)) AS TRANSFER_ALLOC_QTY
            FROM GSFL2K.OOLINE L
            JOIN GSFL2K.OOHEAD H
              ON H.OHCO = L.OLCO
             AND H.OHLOC = L.OLLOC
             AND H.OHORD# = L.OLORD#
            WHERE TRIM(H.OHCUST) IN ({transfer_cust_list})
              AND H.OHOTYP NOT LIKE '%RA%'
              AND COALESCE(L.OLBLUB, 0) > 0
              AND TRIM(L.OLITEM) IN ({in_clause})
            GROUP BY TRIM(L.OLITEM),
                CASE TRIM(H.OHCUST)
{transfer_dest_case}
                    ELSE 0
                END
        """, conn)

        return {
            "items": df_items,
            "sales": df_sales,
            "inventory": df_inv,
            "cost": df_cost,
            "po_by_loc": df_po_loc,
            "transfer_alloc": df_transfer_alloc,
        }
    finally:
        conn.close()


def _build_3m_order_df(raw: dict):
    """
    Process raw 3M DB data into the ordering table.
    Returns (df, loc_order_values, bundled_locs).
    """
    BRANCH_LOCS = _3M_BRANCH_LOCS
    MIN_ORDER   = _3M_MIN_ORDER_VALUE

    df_items = raw["items"].copy()
    df_sales = raw["sales"].copy()
    df_inv   = raw["inventory"].copy()
    df_cost  = raw["cost"].copy()

    for df in (df_items, df_sales, df_inv, df_cost):
        df.columns = df.columns.str.upper()

    for col in ("UNITS_30D", "UNITS_90D"):
        if col in df_sales.columns:
            df_sales[col] = pd.to_numeric(df_sales[col], errors="coerce").fillna(0)
    df_inv["AVAILABLE_QTY"] = pd.to_numeric(df_inv["AVAILABLE_QTY"],  errors="coerce").fillna(0)
    df_cost["UNIT_COST"]    = pd.to_numeric(df_cost["UNIT_COST"],     errors="coerce").fillna(0)
    df_sales["LOCATION"]    = pd.to_numeric(df_sales["LOCATION"],     errors="coerce")
    df_inv["LOCATION"]      = pd.to_numeric(df_inv["LOCATION"],       errors="coerce")

    sales30 = df_sales.pivot_table("UNITS_30D", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0)
    sales90 = df_sales.pivot_table("UNITS_90D", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0)
    inv_piv = df_inv.pivot_table("AVAILABLE_QTY", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0)
    cost_map = df_cost.set_index("ITEM_NUMBER")["UNIT_COST"].to_dict()

    df_po_loc = raw.get("po_by_loc", pd.DataFrame()).copy()
    if not df_po_loc.empty:
        df_po_loc.columns = df_po_loc.columns.str.upper()
        df_po_loc["ON_PO_QTY"] = pd.to_numeric(df_po_loc.get("ON_PO_QTY", 0), errors="coerce").fillna(0)
        df_po_loc["LOCATION"]  = pd.to_numeric(df_po_loc.get("LOCATION",  0), errors="coerce")
        po_piv = df_po_loc.pivot_table("ON_PO_QTY", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0)
    else:
        po_piv = pd.DataFrame()

    df_transfer_alloc = raw.get("transfer_alloc", pd.DataFrame()).copy()
    if not df_transfer_alloc.empty:
        df_transfer_alloc.columns = df_transfer_alloc.columns.str.upper()
        df_transfer_alloc["TRANSFER_ALLOC_QTY"] = pd.to_numeric(
            df_transfer_alloc.get("TRANSFER_ALLOC_QTY", 0), errors="coerce"
        ).fillna(0)
        df_transfer_alloc["LOCATION"] = pd.to_numeric(
            df_transfer_alloc.get("LOCATION", 0), errors="coerce"
        )
        tfr_alloc_piv = df_transfer_alloc.pivot_table(
            "TRANSFER_ALLOC_QTY",
            index="ITEM_NUMBER",
            columns="LOCATION",
            aggfunc="sum",
            fill_value=0,
        )
    else:
        tfr_alloc_piv = pd.DataFrame()

    def _g(piv, item, loc):
        try:
            return float(piv.at[item, loc]) if (item in piv.index and loc in piv.columns) else 0.0
        except Exception:
            return 0.0

    ALL_DISPLAY_LOCS = [1] + BRANCH_LOCS
    rows = []
    for _, irow in df_items.iterrows():
        sku  = str(irow["ITEM_NUMBER"]).strip().upper()
        cost = float(cost_map.get(sku, 0.0))
        moq  = float(irow.get("MIN_ORDER_QTY", 0) or 0)
        r = {
            "Item Number":  sku,
            "Description":  str(irow.get("DESCRIPTION", "")).strip(),
            "_vendor_part": str(irow.get("VENDOR_PART_NUMBER", "")).strip(),
            "_unit_cost":   cost,
            "_moq":         moq,
        }
        # Loc 1 POs can be pre-allocated to branches via transfer backorders.
        # Reassign that inbound quantity to the final destination for planning.
        direct_po = {loc: (_g(po_piv, sku, loc) if not po_piv.empty else 0.0) for loc in ALL_DISPLAY_LOCS}
        transfer_bo = {
            loc: (_g(tfr_alloc_piv, sku, loc) if not tfr_alloc_piv.empty else 0.0)
            for loc in BRANCH_LOCS
        }
        transfer_bo_total = sum(max(qty, 0.0) for qty in transfer_bo.values())
        loc1_po_for_transfer = max(direct_po.get(1, 0.0), 0.0)
        alloc_factor = min(1.0, loc1_po_for_transfer / transfer_bo_total) if transfer_bo_total > 0 else 0.0
        transfer_po_alloc = {loc: max(transfer_bo[loc], 0.0) * alloc_factor for loc in BRANCH_LOCS}
        loc1_allocated_po = sum(transfer_po_alloc.values())
        final_on_po = {1: max(direct_po.get(1, 0.0) - loc1_allocated_po, 0.0)}
        for loc in BRANCH_LOCS:
            final_on_po[loc] = direct_po.get(loc, 0.0) + transfer_po_alloc[loc]

        for loc in ALL_DISPLAY_LOCS:
            s30      = _g(sales30, sku, loc)
            s90      = _g(sales90, sku, loc)
            avail    = _g(inv_piv,  sku, loc)
            on_po    = final_on_po.get(loc, 0.0)
            inv_position = avail + on_po
            transfer = s90 - inv_position
            if sku in _3M_PKG250_SKUS and transfer > 0:
                transfer = float(math.ceil(transfer / 5) * 5)
            r[f"Loc {loc} Available"]     = round(avail)
            r[f"Loc {loc} Sales Last Mo"] = round(s30)
            r[f"Loc {loc} Avg Mo (3mo)"]  = round(s90 / 3, 1)
            r[f"Loc {loc} On PO"]         = round(on_po)
            r[f"Loc {loc} To Order"]      = round(transfer)
            r[f"_t{loc}"]                 = transfer
            r[f"_raw_avail{loc}"]         = avail
            r[f"_raw_on_po{loc}"]         = on_po
            r[f"_raw_direct_po{loc}"]     = direct_po.get(loc, 0.0)
            r[f"_raw_transfer_alloc{loc}"] = transfer_po_alloc.get(loc, 0.0)
            r[f"_raw_s90_{loc}"]          = s90
        rows.append(r)

    if not rows:
        return pd.DataFrame(), {}, set()

    df = pd.DataFrame(rows)

    # ── Internal transfer pre-pass ─────────────────────────────────────────────
    # Branches with >9 months on hand send excess to Loc 1; Loc 1 then fills
    # branches with <3 months (highest 90-day demand first).  _t{loc} and
    # "Loc {loc} To Order" are rewritten to reflect post-transfer ordering needs.
    _XFER_EXCESS_MO = 9.0   # branch returns excess to Loc 1 if months on hand > this
    _XFER_TARGET_MO = 3.0   # fill receiving branches up to this many months
    _LOC1_FLOOR_MO  = 3.0   # Loc 1 will not send stock below this many months of its own demand

    for idx, row in df.iterrows():
        avail_  = {loc: float(row[f"_raw_avail{loc}"]) for loc in ALL_DISPLAY_LOCS}
        on_po_  = {loc: float(row[f"_raw_on_po{loc}"]) for loc in ALL_DISPLAY_LOCS}
        s90_    = {loc: float(row[f"_raw_s90_{loc}"])  for loc in ALL_DISPLAY_LOCS}
        avg_mo_ = {loc: s90_[loc] / 3.0                for loc in ALL_DISPLAY_LOCS}
        xfer    = {loc: 0.0                             for loc in ALL_DISPLAY_LOCS}
        moq     = float(row.get("_moq", 0) or 0)

        # Open POs count toward inventory position for order need and transfer-in
        # decisions. Transfers out are still based on current available stock.
        position_ = {loc: avail_[loc] + on_po_[loc] for loc in ALL_DISPLAY_LOCS}

        # Step 1 — collect excess from branches (>9 months → send to Loc 1)
        loc1_pool = avail_[1]
        for loc in BRANCH_LOCS:
            if avg_mo_[loc] > 0 and avail_[loc] > _XFER_EXCESS_MO * avg_mo_[loc]:
                excess = avail_[loc] - _XFER_EXCESS_MO * avg_mo_[loc]
                xfer[loc] = -excess
                loc1_pool += excess

        # Step 2 — distribute from Loc 1 to needy branches, highest demand first.
        # Transfer quantity is floored at MOQ (IMORDQ) so branches never receive
        # a partial case quantity that can't be reordered in kind.
        loc1_floor = _LOC1_FLOOR_MO * avg_mo_[1]
        needy = sorted(
            [
                (loc, max(0.0, _XFER_TARGET_MO * avg_mo_[loc] - position_[loc]), s90_[loc])
                for loc in BRANCH_LOCS
                if avg_mo_[loc] > 0 and position_[loc] < _XFER_TARGET_MO * avg_mo_[loc]
            ],
            key=lambda x: x[2],
            reverse=True,
        )
        for loc, shortage, _ in needy:
            can_give = max(0.0, loc1_pool - loc1_floor)
            if can_give <= 0:
                break
            give = min(shortage, can_give)
            if give > 0 and moq > 0:
                give = min(max(give, moq), can_give)
            xfer[loc] = give
            loc1_pool -= give

        # Step 3 — rewrite To Order quantities using effective post-transfer inventory
        sku_id = str(row["Item Number"])
        for loc in BRANCH_LOCS:
            eff_position = avail_[loc] + on_po_[loc] + xfer[loc]
            new_t        = s90_[loc] - eff_position
            if sku_id in _3M_PKG250_SKUS and new_t > 0:
                new_t = float(math.ceil(new_t / 5) * 5)
            df.at[idx, f"_t{loc}"]             = new_t
            df.at[idx, f"Loc {loc} To Order"]  = round(new_t)

        new_t1 = s90_[1] - (loc1_pool + on_po_[1])
        df.at[idx, "_t1"]            = new_t1
        df.at[idx, "Loc 1 To Order"] = round(new_t1)

        # Signed transfer column (branch perspective: + = receive from Loc 1)
        for loc in BRANCH_LOCS:
            df.at[idx, f"Loc {loc} Transfer"] = round(xfer[loc])

    loc_order_values = {}
    for loc in BRANCH_LOCS:
        tcol = f"_t{loc}"
        df[tcol] = pd.to_numeric(df[tcol], errors="coerce").fillna(0)
        loc_order_values[loc] = float((df[tcol].clip(lower=0) * df["_unit_cost"]).sum())

    bundled_locs = {l for l in BRANCH_LOCS if 0 < loc_order_values[l] < MIN_ORDER}
    t_cols = [f"_t{l}" for l in ALL_DISPLAY_LOCS]
    df["_moq"] = pd.to_numeric(df["_moq"], errors="coerce").fillna(0)
    df["Min Order Qty"] = df["_moq"].apply(lambda x: int(x) if x > 0 else "")

    direct_locs = {l for l in BRANCH_LOCS if loc_order_values[l] >= MIN_ORDER}

    # Apply MOQ (IMORDQ) per item now that we know which locs are bundled vs. direct.
    #   Direct locs: each branch orders independently → MOQ applies per location.
    #   Bundled locs: quantities roll up into Loc 1's order → MOQ applies to the
    #                 combined (Loc 1 need + all bundled-loc needs) total.
    bundle_t_cols = ["_t1"] + [f"_t{l}" for l in sorted(bundled_locs)]
    for idx, row in df.iterrows():
        moq = float(row.get("_moq", 0) or 0)
        if moq <= 0:
            continue
        # --- Loc 1 bundle ---
        combined = sum(max(float(row.get(c, 0) or 0), 0) for c in bundle_t_cols)
        if 0 < combined < moq:
            gap  = moq - combined
            new_t1 = max(float(row.get("_t1", 0) or 0), 0) + gap
            df.at[idx, "_t1"]          = new_t1
            df.at[idx, "Loc 1 To Order"] = round(new_t1)
        # --- Direct locs ---
        for loc in direct_locs:
            val = max(float(row.get(f"_t{loc}", 0) or 0), 0)
            if 0 < val < moq:
                df.at[idx, f"_t{loc}"]             = moq
                df.at[idx, f"Loc {loc} To Order"]  = round(moq)

    # Recompute company-wide total after MOQ adjustments.
    df["_co_units_to_order"] = df[t_cols].clip(lower=0).sum(axis=1).round().astype(int)

    def _status(row):
        d        = sorted(l for l in direct_locs  if row[f"_t{l}"] > 0)
        b        = sorted(l for l in bundled_locs if row[f"_t{l}"] > 0)
        xfer_in  = sorted(l for l in BRANCH_LOCS if float(row.get(f"Loc {l} Transfer", 0) or 0) > 0)
        xfer_out = sorted(l for l in BRANCH_LOCS if float(row.get(f"Loc {l} Transfer", 0) or 0) < 0)
        parts = []
        if d:        parts.append(f"Direct: {','.join(str(l) for l in d)}")
        if b:        parts.append(f"GroupOrder:1,{','.join(str(l) for l in b)}")
        if xfer_in:  parts.append(f"TFR 1→{','.join(str(l) for l in xfer_in)}")
        if xfer_out: parts.append(f"BTS {','.join(str(l) for l in xfer_out)}→1")
        return " | ".join(parts) if parts else "Sufficient"

    df["Status"] = df.apply(_status, axis=1)

    col_order = ["Item Number", "Description", "Min Order Qty"]
    for loc in ALL_DISPLAY_LOCS:
        col_order += [f"Loc {loc} Available", f"Loc {loc} Sales Last Mo",
                      f"Loc {loc} Avg Mo (3mo)", f"Loc {loc} On PO",
                      f"Loc {loc} To Order"]
        if loc in BRANCH_LOCS:
            col_order.append(f"Loc {loc} Transfer")
    col_order.append("Status")

    keep_cols = col_order + ["_vendor_part", "_unit_cost", "_co_units_to_order", "_moq"] + t_cols
    df = df[[c for c in keep_cols if c in df.columns]]
    xfer_cols = [f"Loc {loc} Transfer" for loc in BRANCH_LOCS if f"Loc {loc} Transfer" in df.columns]
    has_transfer = (df[xfer_cols].abs() > 0).any(axis=1) if xfer_cols else pd.Series(False, index=df.index)
    df = df[(df["_co_units_to_order"] > 0) | has_transfer].reset_index(drop=True)
    return df, loc_order_values, bundled_locs


# --- Transfer Report -------------------------------------------------------
_TRANSFER_LOC1 = 1
_TRANSFER_BRANCH_LOCS = [3, 4, 5, 6, 8, 9]
_TRANSFER_ALL_LOCS = [_TRANSFER_LOC1] + _TRANSFER_BRANCH_LOCS
_TRANSFER_LOC1_FLOOR_MONTHS = 2.0
_TRANSFER_LOC1_TARGET_MONTHS = 3.0
_TRANSFER_DEST_TRIGGER_MONTHS = 2.0
_TRANSFER_DEST_TARGET_MONTHS = 3.0
_TRANSFER_BRANCH_CUSTOMERS = {
    3: "TRANSFER03",
    4: "TRANSFER04",
    5: "TRANSFER05",
    6: "TRANSFER06",
    8: "TRANSFER08",
    9: "TRANSFER09",
}


def _transfer_emu_sql_expr(alias: str = "B") -> str:
    """Gartman EMU: average of two trailing three-month sales windows."""
    m = lambda n: f"COALESCE({alias}.IBS{n}, 0)"
    return f"""
        CASE MONTH(CURRENT TIMESTAMP)
            WHEN 1  THEN ((({m(12)} + {m(11)} + {m(10)}) / 3.0) + (({m(11)} + {m(10)} + {m(9)}) / 3.0)) / 2.0
            WHEN 2  THEN ((({m(1)}  + {m(12)} + {m(11)}) / 3.0) + (({m(12)} + {m(11)} + {m(10)}) / 3.0)) / 2.0
            WHEN 3  THEN ((({m(2)}  + {m(1)}  + {m(12)}) / 3.0) + (({m(1)}  + {m(12)} + {m(11)}) / 3.0)) / 2.0
            WHEN 4  THEN ((({m(3)}  + {m(2)}  + {m(1)})  / 3.0) + (({m(2)}  + {m(1)}  + {m(12)}) / 3.0)) / 2.0
            WHEN 5  THEN ((({m(4)}  + {m(3)}  + {m(2)})  / 3.0) + (({m(3)}  + {m(2)}  + {m(1)})  / 3.0)) / 2.0
            WHEN 6  THEN ((({m(5)}  + {m(4)}  + {m(3)})  / 3.0) + (({m(4)}  + {m(3)}  + {m(2)})  / 3.0)) / 2.0
            WHEN 7  THEN ((({m(6)}  + {m(5)}  + {m(4)})  / 3.0) + (({m(5)}  + {m(4)}  + {m(3)})  / 3.0)) / 2.0
            WHEN 8  THEN ((({m(7)}  + {m(6)}  + {m(5)})  / 3.0) + (({m(6)}  + {m(5)}  + {m(4)})  / 3.0)) / 2.0
            WHEN 9  THEN ((({m(8)}  + {m(7)}  + {m(6)})  / 3.0) + (({m(7)}  + {m(6)}  + {m(5)})  / 3.0)) / 2.0
            WHEN 10 THEN ((({m(9)}  + {m(8)}  + {m(7)})  / 3.0) + (({m(8)}  + {m(7)}  + {m(6)})  / 3.0)) / 2.0
            WHEN 11 THEN ((({m(10)} + {m(9)}  + {m(8)})  / 3.0) + (({m(9)}  + {m(8)}  + {m(7)})  / 3.0)) / 2.0
            WHEN 12 THEN ((({m(11)} + {m(10)} + {m(9)})  / 3.0) + (({m(10)} + {m(9)}  + {m(8)})  / 3.0)) / 2.0
            ELSE 0
        END
    """


@st.cache_data(ttl=900, show_spinner=False)
def _fetch_transfer_report_data(as_of_date_str: str) -> dict:
    """Live Gartman fetch for the Carlos Transfer Report. Cached 15 minutes."""
    try:
        conn = _connect()
    except Exception:
        return {}
    try:
        as_of = pd.Timestamp(as_of_date_str).normalize()
        d365 = (as_of - pd.Timedelta(days=365)).date()
        loc_list = ", ".join(str(l) for l in _TRANSFER_ALL_LOCS)
        branch_loc_list = ", ".join(str(l) for l in _TRANSFER_BRANCH_LOCS)
        transfer_cust_list = _build_in_list(list(_TRANSFER_BRANCH_CUSTOMERS.values()))
        emu_expr = _transfer_emu_sql_expr("B")
        dest_case = "\n".join(
            f"                WHEN '{cust}' THEN {loc}"
            for loc, cust in sorted(_TRANSFER_BRANCH_CUSTOMERS.items())
        )

        df_inv = _read_sql_silent(f"""
            SELECT
                TRIM(B.IBITEM) AS ITEM_NUMBER,
                MAX(TRIM(M.IMDESC)) AS DESCRIPTION,
                MAX(TRIM(M.IMVEND)) AS VENDOR_NUMBER,
                MAX(COALESCE(TRIM(V.VMNAME), '')) AS VENDOR_NAME,
                B.IBLOC AS LOCATION,
                SUM(COALESCE(B.IBQOH, 0) - COALESCE(B.IBQOO, 0)) AS AVAILABLE_QTY,
                MAX(DECIMAL({emu_expr}, 18, 4)) AS EMU
            FROM GSFL2K.ITEMBAL B
            JOIN GSFL2K.ITEMMAST M
              ON TRIM(M.IMITEM) = TRIM(B.IBITEM)
            LEFT JOIN GSFL2K.VENDMAST V
              ON TRIM(V.VMVEND) = TRIM(M.IMVEND)
            WHERE B.IBLOC IN ({loc_list})
              AND COALESCE(TRIM(M.IMDELT), '') = 'A'
              AND TRIM(M.IMUM2) NOT LIKE '%SF%'
              AND M.IMDIV IN (2, 3)
            GROUP BY TRIM(B.IBITEM), B.IBLOC
        """, conn)

        df_po = _read_sql_silent(f"""
            SELECT
                TRIM(L.PLITEM) AS ITEM_NUMBER,
                L.PLLOC AS LOCATION,
                SUM(COALESCE(L.PLBLUO, 0)) AS ON_PO_QTY
            FROM GSFL2K.POLINE L
            JOIN GSFL2K.ITEMMAST M
              ON TRIM(M.IMITEM) = TRIM(L.PLITEM)
            WHERE L.PLDELT LIKE '%A%'
              AND L.PLLOC IN ({loc_list})
              AND COALESCE(TRIM(M.IMDELT), '') = 'A'
              AND TRIM(M.IMUM2) NOT LIKE '%SF%'
              AND M.IMDIV IN (2, 3)
            GROUP BY TRIM(L.PLITEM), L.PLLOC
        """, conn)

        df_tfr = _read_sql_silent(f"""
            SELECT
                TRIM(L.OLITEM) AS ITEM_NUMBER,
                CASE TRIM(H.OHCUST)
{dest_case}
                    ELSE 0
                END AS LOCATION,
                SUM(
                    CASE
                        WHEN COALESCE(L.OLBLUO, 0) - COALESCE(L.OLBLUB, 0) < 0 THEN 0
                        ELSE COALESCE(L.OLBLUO, 0) - COALESCE(L.OLBLUB, 0)
                    END
                ) AS ON_TFR_QTY
            FROM GSFL2K.OOLINE L
            JOIN GSFL2K.OOHEAD H
              ON H.OHCO = L.OLCO
             AND H.OHLOC = L.OLLOC
             AND H.OHORD# = L.OLORD#
            JOIN GSFL2K.ITEMMAST M
              ON TRIM(M.IMITEM) = TRIM(L.OLITEM)
            WHERE TRIM(H.OHCUST) IN ({transfer_cust_list})
              AND H.OHOTYP NOT LIKE '%RA%'
              AND COALESCE(L.OLBLUO, 0) > 0
              AND COALESCE(TRIM(M.IMDELT), '') = 'A'
              AND TRIM(M.IMUM2) NOT LIKE '%SF%'
              AND M.IMDIV IN (2, 3)
            GROUP BY TRIM(L.OLITEM),
                CASE TRIM(H.OHCUST)
{dest_case}
                    ELSE 0
                END
        """, conn)

        df_orders = _read_sql_silent(f"""
            SELECT
                TRIM(L.SLITEM) AS ITEM_NUMBER,
                H.SHLOC AS LOCATION,
                H.SHORD# AS ORDER_NUMBER,
                SUM(COALESCE(L.SLBLUO, 0)) AS ORDER_QTY
            FROM GSFL2K.SHLINE L
            JOIN GSFL2K.SHHEAD H
              ON H.SHCO = L.SLCO
             AND H.SHLOC = L.SLLOC
             AND H.SHORD# = L.SLORD#
             AND H.SHINV# = L.SLINV#
            JOIN GSFL2K.ITEMMAST M
              ON TRIM(M.IMITEM) = TRIM(L.SLITEM)
            WHERE H.SHIDAT >= ?
              AND H.SHLOC IN ({branch_loc_list})
              AND COALESCE(L.SLBLUO, 0) > 0
              AND TRIM(L.SLUM2) NOT LIKE '%SF%'
              AND H.SHCUST NOT LIKE '%TRANSFER%'
              AND H.SHCUST NOT LIKE '%OMP000%'
              AND H.SHCUST NOT LIKE '%INV000%'
              AND H.SHCUST NOT LIKE '%OLD001%'
              AND COALESCE(TRIM(M.IMDELT), '') = 'A'
              AND TRIM(M.IMUM2) NOT LIKE '%SF%'
              AND M.IMDIV IN (2, 3)
            GROUP BY TRIM(L.SLITEM), H.SHLOC, H.SHORD#
        """, conn, params=[d365])

        return {
            "inventory": df_inv,
            "po": df_po,
            "transfer": df_tfr,
            "orders": df_orders,
        }
    finally:
        conn.close()


def _build_transfer_report_df(raw: dict) -> pd.DataFrame:
    """Apply transfer trigger, Loc 1 cap, proportional allocation, and alerts."""
    if not raw or raw.get("inventory") is None or raw["inventory"].empty:
        return pd.DataFrame()

    df_inv = raw["inventory"].copy()
    df_po = raw.get("po", pd.DataFrame()).copy()
    df_tfr = raw.get("transfer", pd.DataFrame()).copy()
    df_orders = raw.get("orders", pd.DataFrame()).copy()

    for df in (df_inv, df_po, df_tfr, df_orders):
        if not df.empty:
            df.columns = df.columns.str.upper()

    for col in ("LOCATION", "AVAILABLE_QTY", "EMU"):
        if col in df_inv.columns:
            df_inv[col] = pd.to_numeric(df_inv[col], errors="coerce").fillna(0)
    for df, qty_col in ((df_po, "ON_PO_QTY"), (df_tfr, "ON_TFR_QTY")):
        if not df.empty:
            df["LOCATION"] = pd.to_numeric(df.get("LOCATION", 0), errors="coerce").fillna(0).astype(int)
            df[qty_col] = pd.to_numeric(df.get(qty_col, 0), errors="coerce").fillna(0)
    if not df_orders.empty:
        df_orders["LOCATION"] = pd.to_numeric(df_orders.get("LOCATION", 0), errors="coerce").fillna(0).astype(int)
        df_orders["ORDER_QTY"] = pd.to_numeric(df_orders.get("ORDER_QTY", 0), errors="coerce").fillna(0)

    df_inv["LOCATION"] = pd.to_numeric(df_inv["LOCATION"], errors="coerce").fillna(0).astype(int)
    meta = (
        df_inv.sort_values(["ITEM_NUMBER", "LOCATION"])
        .groupby("ITEM_NUMBER", as_index=False)
        .agg({
            "DESCRIPTION": "first",
            "VENDOR_NUMBER": "first",
            "VENDOR_NAME": "first",
        })
    )
    meta_map = meta.set_index("ITEM_NUMBER").to_dict("index") if not meta.empty else {}

    avail_piv = df_inv.pivot_table(
        "AVAILABLE_QTY", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0
    )
    emu_piv = df_inv.pivot_table(
        "EMU", index="ITEM_NUMBER", columns="LOCATION", aggfunc="max", fill_value=0
    )

    if not df_po.empty:
        po_piv = df_po.pivot_table("ON_PO_QTY", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0)
    else:
        po_piv = pd.DataFrame()
    if not df_tfr.empty:
        tfr_piv = df_tfr.pivot_table("ON_TFR_QTY", index="ITEM_NUMBER", columns="LOCATION", aggfunc="sum", fill_value=0)
    else:
        tfr_piv = pd.DataFrame()

    def _g(piv: pd.DataFrame, item: str, loc: int) -> float:
        try:
            return float(piv.at[item, loc]) if (item in piv.index and loc in piv.columns) else 0.0
        except Exception:
            return 0.0

    def _coverage_months(available: float, on_po_tfr: float, emu: float) -> float:
        if emu <= 0:
            return 0.0
        return (max(float(available), 0.0) + max(float(on_po_tfr), 0.0)) / float(emu)

    def _allocate_proportionally(needs: Dict[int, int], cap_units: int) -> Dict[int, int]:
        total_need = sum(max(0, int(v)) for v in needs.values())
        cap_units = max(0, int(cap_units))
        if total_need <= 0 or cap_units <= 0:
            return {loc: 0 for loc in needs}
        if cap_units >= total_need:
            return {loc: int(v) for loc, v in needs.items()}

        if cap_units >= len(needs):
            alloc = {loc: 1 for loc in needs}
            remaining = cap_units - sum(alloc.values())
            residual = {loc: max(0, int(needs[loc]) - alloc[loc]) for loc in needs}
            residual_total = sum(residual.values())
            if remaining <= 0 or residual_total <= 0:
                return alloc
            exact = {loc: (remaining * float(residual[loc]) / float(residual_total)) for loc in needs}
            for loc in needs:
                add_qty = min(residual[loc], int(math.floor(exact[loc])))
                alloc[loc] += add_qty
                remaining -= add_qty
            for loc, _rem in sorted(
                ((loc, exact[loc] - math.floor(exact[loc])) for loc in needs if residual[loc] > 0),
                key=lambda pair: (-pair[1], -residual[pair[0]], pair[0]),
            ):
                if remaining <= 0:
                    break
                if alloc[loc] < int(needs[loc]):
                    alloc[loc] += 1
                    remaining -= 1
            return alloc

        exact = {loc: (cap_units * float(need) / float(total_need)) for loc, need in needs.items()}
        alloc = {loc: min(int(needs[loc]), int(math.floor(exact[loc]))) for loc in needs}
        remaining = cap_units - sum(alloc.values())
        for loc, _rem in sorted(
            ((loc, exact[loc] - math.floor(exact[loc])) for loc in needs),
            key=lambda pair: (-pair[1], -needs[pair[0]], pair[0]),
        ):
            if remaining <= 0:
                break
            if alloc[loc] < int(needs[loc]):
                alloc[loc] += 1
                remaining -= 1
        return alloc

    rows = []
    for item in sorted(str(i).strip().upper() for i in avail_piv.index):
        loc1_available = _g(avail_piv, item, _TRANSFER_LOC1)
        loc1_emu = _g(emu_piv, item, _TRANSFER_LOC1)
        loc1_on_po = _g(po_piv, item, _TRANSFER_LOC1) if not po_piv.empty else 0.0

        raw_needs: Dict[int, int] = {}
        branch_snapshots: Dict[int, Dict[str, float]] = {}
        for loc in _TRANSFER_BRANCH_LOCS:
            loc_available = _g(avail_piv, item, loc)
            loc_emu = _g(emu_piv, item, loc)
            on_po_tfr = (_g(po_piv, item, loc) if not po_piv.empty else 0.0) + (
                _g(tfr_piv, item, loc) if not tfr_piv.empty else 0.0
            )
            coverage_units = max(loc_available, 0.0) + max(on_po_tfr, 0.0)
            coverage_months = _coverage_months(loc_available, on_po_tfr, loc_emu)
            branch_snapshots[loc] = {
                "available": loc_available,
                "emu": loc_emu,
                "on_po_tfr": on_po_tfr,
                "coverage_months": coverage_months,
            }
            if loc_emu > 0 and coverage_units < (_TRANSFER_DEST_TRIGGER_MONTHS * loc_emu):
                raw_need = int(math.ceil(max((_TRANSFER_DEST_TARGET_MONTHS * loc_emu) - coverage_units, 0.0)))
                if raw_need > 0:
                    raw_needs[loc] = raw_need

        if not raw_needs:
            continue

        sendable_units = int(math.floor(max(loc1_available - (_TRANSFER_LOC1_FLOOR_MONTHS * loc1_emu), 0.0)))
        allocations = _allocate_proportionally(raw_needs, sendable_units)
        total_transfer = sum(allocations.values())
        loc1_available_after = loc1_available - total_transfer
        total_raw_need = sum(raw_needs.values())
        unmet_transfer = max(total_raw_need - total_transfer, 0)
        loc1_to_order = int(math.ceil(max(
            ((_TRANSFER_LOC1_TARGET_MONTHS * loc1_emu) + unmet_transfer)
            - (loc1_available_after + loc1_on_po),
            0.0,
        )))
        item_meta = meta_map.get(item, {})

        for loc in sorted(raw_needs):
            snap = branch_snapshots.get(loc, {})
            alloc = int(allocations.get(loc, 0))
            raw_need = int(raw_needs.get(loc, 0))
            loc_available = float(snap.get("available", 0.0))
            coverage_months = float(snap.get("coverage_months", 0.0))
            notes = []
            notes.append(
                f"Coverage {coverage_months:,.1f} mo vs {_TRANSFER_DEST_TARGET_MONTHS:,.1f} mo target"
            )
            notes.append(
                f"Loc 1 sends {alloc:,}; total branch TFR {total_transfer:,}; Loc 1 avail {loc1_available:,.0f}"
            )
            if total_raw_need > sendable_units:
                notes.append("Transfer capped by Loc 1 two-month floor")
            if alloc < raw_need:
                notes.append(f"Partial transfer: {alloc:,} of {raw_need:,}")
            if loc1_to_order > 0:
                notes.append(f"Loc 1 PO {loc1_to_order:,} to restore {_TRANSFER_LOC1_TARGET_MONTHS:,.1f} mo target")
            if loc1_available <= 0:
                notes.append("Loc 1 has no available stock")
            if coverage_months < _TRANSFER_DEST_TRIGGER_MONTHS:
                notes.append(f"Below {_TRANSFER_DEST_TRIGGER_MONTHS:,.1f} months coverage")

            rows.append({
                "Item Number": item,
                "Description": str(item_meta.get("DESCRIPTION", "")).strip(),
                "Vendor Number": str(item_meta.get("VENDOR_NUMBER", "")).strip(),
                "Vendor Name": str(item_meta.get("VENDOR_NAME", "")).strip(),
                "Loc": loc,
                "Loc Available": loc_available,
                "On PO/TFR": float(snap.get("on_po_tfr", 0.0)),
                "EMU": float(snap.get("emu", 0.0)),
                "Months Coverage": coverage_months,
                "_Raw Need to TFR": raw_need,
                "Need to TFR": alloc,
                "Total Qty TFR": total_transfer,
                "Loc 1 Available": loc1_available,
                "_Loc 1 EMU": loc1_emu,
                "_Loc 1 On PO": loc1_on_po,
                "Loc 1 to Order": loc1_to_order,
                "Notes": " | ".join(dict.fromkeys(notes)),
            })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values(["Item Number", "Loc"], kind="mergesort").reset_index(drop=True)
    return df


def _load_who_produces() -> Dict[str, List[str]]:
    """
    Load 'Who Produces' sheet from StripSKUList.xlsx.
    Returns a dict mapping SKU -> list of vendor names that can produce it.
    """
    strip_path = Path(__file__).resolve().parent / STRIP_SKU_LIST_FILE
    if not strip_path.exists():
        return {}
    try:
        df = pd.read_excel(strip_path, sheet_name="Who Produces", header=0)
        vendor_cols = STRIP_VENDOR_COLUMNS
        result = {}
        for _, row in df.iterrows():
            sku = str(row.get("Item Number", "")).strip().upper()
            if not sku:
                continue
            vendors = []
            for vcol in vendor_cols:
                val = row.get(vcol)
                if pd.notna(val) and str(val).strip():
                    vendors.append(vcol)
            if vendors:
                result[sku] = vendors
        return result
    except Exception:
        return {}


def _load_margins() -> Dict[str, Dict]:
    """
    Load 'Margins' sheet from StripSKUList.xlsx.
    Returns a dict mapping SKU -> {"sale_price": float, "margin": float}
    The margin represents the current landed cost margin for items in inventory.
    """
    strip_path = Path(__file__).resolve().parent / STRIP_SKU_LIST_FILE
    if not strip_path.exists():
        return {}
    try:
        df = pd.read_excel(strip_path, sheet_name="Margins", header=0)
        result = {}
        for _, row in df.iterrows():
            sku = str(row.get("ITEM NUMBER", "")).strip().upper()
            if not sku:
                continue
            sale_price = float(row.get("SALE PRICE", 0)) if pd.notna(row.get("SALE PRICE")) else 0.0
            margin = float(row.get("MARGIN", 0)) if pd.notna(row.get("MARGIN")) else 0.0
            result[sku] = {"sale_price": sale_price, "margin": margin}
        return result
    except Exception:
        return {}


def _load_moulding_margins() -> Dict[str, Dict]:
    """
    Load margins from MouldingSKUList.xlsx 'items' sheet.
    Returns a dict mapping SKU -> {"sale_price": float, "margin": float}
    """
    moulding_path = Path(__file__).resolve().parent / MOULDING_SKU_LIST_FILE
    if not moulding_path.exists():
        return {}
    try:
        df = pd.read_excel(moulding_path, sheet_name="items", header=0)
        result = {}
        for _, row in df.iterrows():
            sku = str(row.get("ITEM", "")).strip().upper()
            if not sku:
                continue
            sale_price = float(row.get("SALE PRICE", 0)) if pd.notna(row.get("SALE PRICE")) else 0.0
            margin = float(row.get("MARGIN", 0)) if pd.notna(row.get("MARGIN")) else 0.0
            result[sku] = {"sale_price": sale_price, "margin": margin}
        return result
    except Exception:
        return {}


def _calculate_optimal_vendor_mix(
    edited_df: pd.DataFrame,
    margins_data: Dict[str, Dict],
    vendor_names: List[str],
) -> Dict:
    """
    Calculate the optimal vendor mix based on lowest total landed cost.

    Returns a dict with:
    - "optimal_assignments": dict of SKU -> chosen vendor name
    - "total_cost": total landed cost
    - "item_costs": dict of SKU -> {"vendor": str, "price": float, "freight_share": float, "landed_cost": float, "qty": float}
    - "margin_alerts": list of dicts with margin comparison warnings
    - "errors": list of error messages
    """
    result = {
        "optimal_assignments": {},
        "total_cost": 0.0,
        "item_costs": {},
        "margin_alerts": [],
        "errors": [],
    }

    # Extract freight costs from the first row (SKU="Freight")
    freight_costs = {}  # vendor -> freight cost for full container
    freight_row = edited_df[edited_df["SKU"] == "Freight"]
    if not freight_row.empty:
        for vname in vendor_names:
            val = freight_row.iloc[0].get(vname)
            if pd.notna(val) and val != "" and val != 0:
                try:
                    freight_costs[vname] = float(val)
                except (ValueError, TypeError):
                    pass

    # Get included items (exclude Freight row)
    item_rows = edited_df[(edited_df["SKU"] != "Freight") & (edited_df["Include"] == True)]

    if item_rows.empty:
        result["errors"].append("No items selected (Include checkbox not checked)")
        return result

    # For each vendor, calculate total SF included to determine freight share per SF
    vendor_total_sf = {v: 0.0 for v in vendor_names}
    vendor_items = {v: [] for v in vendor_names}

    # First pass: identify which items have valid prices from which vendors
    item_prices = {}  # sku -> {vendor: price}
    for _, row in item_rows.iterrows():
        sku = str(row["SKU"]).strip().upper()
        qty_str = str(row.get("Qty Needed", "")).replace(",", "").strip()
        try:
            qty = float(qty_str) if qty_str else 0.0
        except ValueError:
            qty = 0.0

        if qty <= 0:
            continue

        item_prices[sku] = {"qty": qty, "vendors": {}}

        # Accept any vendor that has a price entered by the user
        for vname in vendor_names:
            val = row.get(vname)
            if pd.notna(val) and val != "" and val != 0:
                try:
                    price = float(val)
                    if price > 0:
                        item_prices[sku]["vendors"][vname] = price
                except (ValueError, TypeError):
                    pass

    if not item_prices:
        result["errors"].append("No valid items with quantities found")
        return result

    # Calculate total SF per vendor for freight allocation
    # We need to try each possible vendor assignment to find optimal
    # For simplicity, we'll use a greedy approach: assign each item to lowest landed cost vendor

    # First, calculate freight per SF for each vendor (assuming all items go to that vendor)
    total_sf_all = sum(ip["qty"] for ip in item_prices.values())

    # For each item, calculate landed cost from each vendor and pick the lowest
    for sku, data in item_prices.items():
        qty = data["qty"]
        best_vendor = None
        best_landed_cost = float("inf")
        best_price = 0.0
        best_freight_share = 0.0

        for vendor, price in data["vendors"].items():
            freight = freight_costs.get(vendor, 0.0)
            if freight > 0 and total_sf_all > 0:
                # Freight per SF = container freight / total SF in order
                freight_per_sf = freight / total_sf_all
            else:
                # Freight baked into price or no freight
                freight_per_sf = 0.0

            landed_cost = price + freight_per_sf

            if landed_cost < best_landed_cost:
                best_landed_cost = landed_cost
                best_vendor = vendor
                best_price = price
                best_freight_share = freight_per_sf

        if best_vendor:
            result["optimal_assignments"][sku] = best_vendor
            result["item_costs"][sku] = {
                "vendor": best_vendor,
                "price": best_price,
                "freight_share": best_freight_share,
                "landed_cost": best_landed_cost,
                "qty": qty,
            }
            result["total_cost"] += best_landed_cost * qty

            # Check margin alert
            margin_info = margins_data.get(sku, {})
            if margin_info:
                sale_price = margin_info.get("sale_price", 0)
                current_margin = margin_info.get("margin", 0)

                if sale_price > 0:
                    # Current landed cost = sale_price * (1 - margin)
                    current_landed_cost = sale_price * (1 - current_margin)

                    # Calculate proposed margin
                    proposed_margin = 1 - (best_landed_cost / sale_price) if sale_price > 0 else 0

                    # Check if margin difference exceeds 5%
                    margin_diff = proposed_margin - current_margin
                    if abs(margin_diff) > 0.05:
                        result["margin_alerts"].append({
                            "sku": sku,
                            "vendor": best_vendor,
                            "sale_price": sale_price,
                            "current_margin": current_margin,
                            "proposed_margin": proposed_margin,
                            "current_landed_cost": current_landed_cost,
                            "proposed_landed_cost": best_landed_cost,
                            "margin_diff": margin_diff,
                        })
        else:
            result["errors"].append(f"No valid vendor found for {sku}")

    return result


def _calculate_niko_vendor_mix(
    edited_df: pd.DataFrame,
    vendor_names: List[str],
    baseline_costs: Dict[str, Dict[str, Any]],
    vendor_capacity_data: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
    sfpp_fallback: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """vendor_capacity_data[sku_key][vendor] = {"pallet_sf": ..., "container_sf": ...}
    sfpp_fallback: SKU-level pallet-SF fallback used when no vendor-specific value exists.
    """
    result: Dict[str, Any] = {
        "optimal_assignments": {},
        "total_cost": 0.0,
        "total_material_cost": 0.0,
        "total_freight_cost": 0.0,
        "item_costs": {},
        "vendor_summary": {},
        "cost_alerts": [],
        "omitted_items": [],
        "errors": [],
    }

    if edited_df.empty:
        result["errors"].append("No optimizer rows are available.")
        return result

    freight_costs = {vendor: 0.0 for vendor in vendor_names}
    freight_row = edited_df[edited_df["SKU"].astype(str).str.strip().str.upper() == "FREIGHT"]
    if not freight_row.empty:
        for vendor in vendor_names:
            value = _coerce_positive_float(freight_row.iloc[0].get(vendor))
            if value is not None:
                freight_costs[vendor] = value

    include_mask = edited_df["Include"] == True if "Include" in edited_df.columns else True
    item_rows = edited_df[
        (edited_df["SKU"].astype(str).str.strip().str.upper() != "FREIGHT")
        & include_mask
    ]
    if item_rows.empty:
        result["errors"].append("No items are available for optimization.")
        return result

    raw_requests: List[PurchaseRequest] = []
    missing_quotes: List[str] = []
    for _, row in item_rows.iterrows():
        sku = str(row.get("SKU", "")).strip()
        qty = _coerce_positive_float(row.get("Qty Needed"))
        if not sku or qty is None or qty <= 0:
            continue
        desc_text = str(row.get("Description", "")).strip()
        if desc_text.lower() in ("nan", "none"):
            desc_text = ""
        sku_key = normalize_item_key(sku)
        pallet_sf = (sfpp_fallback or {}).get(sku_key) or NIKO_DEFAULT_PALLET_SF
        sku_capacity = (vendor_capacity_data or {}).get(sku_key, {})
        pallet_sf_by_vendor = {v: d["pallet_sf"] for v, d in sku_capacity.items() if "pallet_sf" in d} or None
        container_sf_by_vendor = {v: d["container_sf"] for v, d in sku_capacity.items() if "container_sf" in d} or None

        if qty < NIKO_MIN_PURCHASABLE_QTY_SF:
            raw_requests.append(
                PurchaseRequest(
                    sku=sku,
                    qty_sf=qty,
                    vendor_quotes={},
                    description=desc_text,
                    pallet_sf=pallet_sf,
                    pallet_sf_by_vendor=pallet_sf_by_vendor,
                    container_sf_by_vendor=container_sf_by_vendor,
                )
            )
            continue

        vendor_quotes: Dict[str, VendorQuote] = {}
        for vendor in vendor_names:
            material_price = _coerce_positive_float(row.get(vendor))
            if material_price is None:
                continue
            vendor_quotes[vendor] = VendorQuote(
                vendor=vendor,
                material_price=material_price,
                freight_per_truck=float(freight_costs.get(vendor, 0.0) or 0.0),
            )

        if not vendor_quotes:
            missing_quotes.append(sku)
            continue

        raw_requests.append(
            PurchaseRequest(
                sku=sku,
                qty_sf=qty,
                vendor_quotes=vendor_quotes,
                description=desc_text,
                pallet_sf=pallet_sf,
                pallet_sf_by_vendor=pallet_sf_by_vendor,
                container_sf_by_vendor=container_sf_by_vendor,
            )
        )

    requests, omitted_items = apply_minimum_order_policy(
        raw_requests,
        omit_below_sf=NIKO_MIN_PURCHASABLE_QTY_SF,
        round_up_above_sf=NIKO_MIN_PURCHASABLE_QTY_SF,
        minimum_order_sf=NIKO_MIN_FULL_PALLET_QTY_SF,
    )
    result["omitted_items"] = omitted_items

    if not requests:
        if omitted_items:
            result["errors"].append(
                f"All candidate lines were omitted because they were below {NIKO_MIN_PURCHASABLE_QTY_SF:,.0f} SF."
            )
        else:
            result["errors"].append("No items remain after applying the optimizer quantity policy.")
        return result

    if missing_quotes:
        result["errors"].append(
            "No vendor pricing is available for: " + ", ".join(sorted({normalize_item_key(sku) for sku in missing_quotes}))
        )
        return result

    optimization_result = optimize_vendor_mix(
        requests,
        truck_capacity_sf=NIKO_TRUCK_CAPACITY_SF,
        allow_pallet_uplift=True,
        max_uplift_pct=NIKO_MAX_PALLET_UPLIFT_PCT,
        default_pallet_sf=NIKO_DEFAULT_PALLET_SF,
        round_to_pallet_multiples=True,
        vendor_penalties={"Dayspring": NIKO_DAYSPRING_MIN_ADVANTAGE_SF},
    )
    result["errors"].extend(optimization_result.get("errors", []))
    if optimization_result.get("status") != "ok":
        return result

    result["optimal_assignments"] = optimization_result.get("optimal_assignments", {})
    result["item_costs"] = optimization_result.get("item_costs", {})
    result["vendor_summary"] = optimization_result.get("vendor_summary", {})
    result["total_cost"] = float(optimization_result.get("total_cost", 0.0))
    result["total_material_cost"] = float(optimization_result.get("total_material_cost", 0.0))
    result["total_freight_cost"] = float(optimization_result.get("total_freight_cost", 0.0))
    result["cost_alerts"] = build_cost_alerts(
        result["item_costs"],
        {normalize_item_key(k): v for k, v in (baseline_costs or {}).items()},
        pct_threshold=NIKO_PRICE_ALERT_PCT_THRESHOLD,
        abs_threshold=NIKO_PRICE_ALERT_ABS_THRESHOLD,
    )
    return result


@st.cache_data(ttl=300)
def _load_veronica_payload() -> Dict:
    """Load flooring data filtered to only items in StripSKUList.xlsx"""
    original_data = _load_webapp_payload()
    strip_skus = _load_strip_sku_list()
    if not strip_skus:
        return original_data
    # Make a deep copy to avoid mutating the original data
    data = copy.deepcopy(original_data)

    # An item passes the filter if its item_number is either an individual strip
    # SKU OR a consolidated group key (e.g. "RH112QRSNB-15/RH112QRSNB-24") where
    # at least one member is a strip SKU.  The forecast run pools group members
    # into a single virtual entry stored under the slash-joined key, so that key
    # must also be allowed through — otherwise consolidated groups are silently
    # dropped before _consolidate_items_list() can process them.
    def _is_strip_item(item_number: str) -> bool:
        key = item_number.strip().upper()
        if key in strip_skus:
            return True
        if key in SKU_GROUP_KEYS:
            return True
        return False

    # Filter items to only those in the strip SKU list (individual or group key)
    items = data.get("items", [])
    filtered_items = [
        item for item in items
        if _is_strip_item(str(item.get("item_number", "")))
    ]
    data["items"] = filtered_items
    # Filter Inventory_Metrics
    metrics = data.get("Inventory_Metrics", [])
    filtered_metrics = [
        row for row in metrics
        if _is_strip_item(str(row.get("item_number", "") or row.get("sku", "")))
    ]
    data["Inventory_Metrics"] = filtered_metrics
    # Filter Monthly_Projections
    monthly = data.get("Monthly_Projections", [])
    filtered_monthly = [
        row for row in monthly
        if _is_strip_item(str(row.get("SKU", "")))
    ]
    data["Monthly_Projections"] = filtered_monthly
    # Filter arrivals
    arrivals = data.get("arrivals", [])
    filtered_arrivals = [
        row for row in arrivals
        if _is_strip_item(str(row.get("ITEM_NUMBER", "")))
    ]
    data["arrivals"] = filtered_arrivals
    return data

def _is_streamlit_runtime() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False

def _build_forecast_chart(series: Dict) -> go.Figure:
    hist_x = series.get("hist_x") or series.get("x") or ["2025-11-23", "2025-11-30", "2025-12-07", "2025-12-14", "2025-12-21"]
    hist_vals = series.get("hist_y") or []
    fc_x = series.get("fc_x") or series.get("x") or ["2025-11-23", "2025-11-30", "2025-12-07", "2025-12-14", "2025-12-21"]
    fc_vals = series.get("fc_y") or series.get("y") or [72.4, 74.8, 74.9, 74.9, 74.1]

    hist_x = pd.to_datetime(hist_x, errors="coerce") if hist_x else hist_x
    fc_x = pd.to_datetime(fc_x, errors="coerce") if fc_x else fc_x

    fig = go.Figure()
    if hist_vals:
        fig.add_trace(
            go.Scatter(
                x=hist_x,
                y=hist_vals,
                mode="lines+markers",
                line=dict(color="#d46a1f", width=3),
                marker=dict(size=6, color="#d46a1f"),
                name="Historical",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=fc_x,
            y=fc_vals,
            mode="lines+markers",
            line=dict(color="#2f6fdd", width=3),
            marker=dict(size=6, color="#2f6fdd"),
            name="Forecast",
        )
    )
    fig.update_layout(
        margin=dict(l=40, r=60, t=40, b=80),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", family="Manrope, sans-serif"),
        showlegend=bool(hist_vals),
        legend=dict(font=dict(color="#374151"), orientation="h", x=0.5, y=-0.05, bgcolor="rgba(0,0,0,0)", xanchor="center", yanchor="top"),
        height=1008,
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
        ticks="",
        showline=False,
        tickfont=dict(size=11, color="#6b7a90"),
        tickformat="%b '%y",
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
        zeroline=False,
        tickfont=dict(size=11, color="#6b7a90"),
    )
    return fig

def _build_forecast_chart_multi(series_list: List[Dict]) -> go.Figure:
    fig = go.Figure()
    palette = ["#2f6fdd", "#6ccf7c", "#f7b84b", "#d96fd9", "#ff8b2c", "#66d0f5"]
    if not series_list:
        series_list = [{"label": "Demand", "series": {"x": ["Nov 23", "Nov 30"], "y": [72.4, 74.8]}}]

    for idx, item in enumerate(series_list):
        series = item.get("series", {})
        label = item.get("label", f"Item {idx+1}")
        color = palette[idx % len(palette)]

        # Plot historical data if available
        hist_x = series.get("hist_x") or []
        hist_y = series.get("hist_y") or []
        if hist_x and hist_y:
            fig.add_trace(
                go.Scatter(
                    x=hist_x,
                    y=hist_y,
                    mode="lines+markers",
                    line=dict(color=color, width=3),
                    marker=dict(size=6),
                    name=f"{label} (Historical)",
                    legendgroup=label,
                )
            )

        # Plot forecast data
        fc_x = series.get("fc_x") or series.get("x") or []
        fc_y = series.get("fc_y") or series.get("y") or []
        if fc_x and fc_y:
            fig.add_trace(
                go.Scatter(
                    x=fc_x,
                    y=fc_y,
                    mode="lines+markers",
                    line=dict(color=color, width=3, dash="dash"),
                    marker=dict(size=6),
                    name=f"{label} (Forecast)",
                    legendgroup=label,
                )
            )

    fig.update_layout(
        margin=dict(l=40, r=24, t=40, b=80),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", family="Manrope, sans-serif"),
        showlegend=True,
        height=1008,
        legend=dict(orientation="h", x=0.5, y=-0.05, xanchor="center", yanchor="top", font=dict(color="#374151")),
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
        ticks="",
        showline=False,
        tickfont=dict(size=11, color="#6b7a90"),
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
        zeroline=False,
        tickfont=dict(size=11, color="#6b7a90"),
    )
    return fig

def _apply_legend_padding(fig: go.Figure, series_count: int) -> go.Figure:
    # Fixed height graph - legend will be in a scrollable container via CSS
    fig.update_layout(
        height=1008,
        margin=dict(l=40, r=24, t=40, b=80),
        legend=dict(
            orientation="h",
            x=0.5,
            y=-0.05,
            xanchor="center",
            yanchor="top",
            font=dict(color="#374151", size=10),
        ),
    )
    return fig


# ============================================================
# SKU CONSOLIDATION FUNCTIONS
# ============================================================

def _consolidate_inventory_metrics(metrics: List[Dict]) -> List[Dict]:
    """
    Consolidate inventory metrics for SKU groups.

    For consolidated SKUs:
    - available_sf: sum of all member SKUs
    - on_po_sf: sum of all member SKUs
    - backorder_sf: sum of all member SKUs
    - inventory_position: sum of all member SKUs
    - lead_time_days: minimum (shortest) of all member SKUs
    - lead_time_mean_demand: sum of all member SKUs' forecasted demand
    - safety_stock: sum of all member SKUs

    Returns original metrics plus new consolidated rows.
    Individual member SKUs are removed from the list.
    """
    if not metrics:
        return metrics

    # Build lookup by SKU
    sku_to_row = {}
    for row in metrics:
        sku = str(row.get("sku", row.get("item_number", ""))).strip().upper()
        if sku:
            sku_to_row[sku] = row

    # Track which SKUs are part of consolidation groups
    consolidated_skus = set()
    consolidated_rows = []

    for consolidated_name, member_skus in SKU_CONSOLIDATION_GROUPS.items():
        # Find all member rows that exist in the data
        member_rows = []
        for member_sku in member_skus:
            member_sku_upper = member_sku.upper()
            if member_sku_upper in sku_to_row:
                member_rows.append(sku_to_row[member_sku_upper])
                consolidated_skus.add(member_sku_upper)

        if not member_rows:
            continue

        # Aggregate the metrics
        total_available = sum(float(r.get("available_sf", 0) or 0) for r in member_rows)
        total_on_po = sum(float(r.get("on_po_sf", 0) or 0) for r in member_rows)
        total_backorder = sum(float(r.get("backorder_sf", 0) or 0) for r in member_rows)
        total_inv_position = sum(float(r.get("inventory_position", 0) or 0) for r in member_rows)
        min_lead_time = min(float(r.get("lead_time_days", 999) or 999) for r in member_rows)
        total_lt_demand = sum(float(r.get("lead_time_mean_demand", 0) or 0) for r in member_rows)
        total_safety_stock = sum(float(r.get("safety_stock", 0) or 0) for r in member_rows)
        total_reorder_point = sum(float(r.get("reorder_point", 0) or 0) for r in member_rows)
        total_avg_weekly = sum(float(r.get("avg_weekly_demand", 0) or 0) for r in member_rows)
        total_daily_demand = sum(float(r.get("daily_mean_demand", 0) or 0) for r in member_rows)
        total_reorder_qty = sum(float(r.get("reorder_quantity", 0) or 0) for r in member_rows)
        total_order_up_to = sum(float(r.get("order_up_to_level", 0) or 0) for r in member_rows)

        # Use first member's vendor info and description as base
        base_row = member_rows[0]

        consolidated_row = {
            "sku": consolidated_name,
            "item_number": consolidated_name,
            "description": base_row.get("description", ""),
            "vendor_name": base_row.get("vendor_name", ""),
            "vendor_number": base_row.get("vendor_number", ""),
            "collection": base_row.get("collection", ""),
            "available_sf": total_available,
            "on_po_sf": total_on_po,
            "backorder_sf": total_backorder,
            "inventory_position": total_inv_position,
            "lead_time_days": min_lead_time if min_lead_time < 999 else 0,
            "lead_time_mean_demand": total_lt_demand,
            "safety_stock": total_safety_stock,
            "reorder_point": total_reorder_point,
            "avg_weekly_demand": total_avg_weekly,
            "daily_mean_demand": total_daily_demand,
            "reorder_quantity": total_reorder_qty,
            "order_up_to_level": total_order_up_to,
            "_is_consolidated": True,
            "_member_skus": member_skus,
        }
        consolidated_rows.append(consolidated_row)

    # Build result: non-consolidated SKUs + consolidated rows
    result = []
    for row in metrics:
        sku = str(row.get("sku", row.get("item_number", ""))).strip().upper()
        if sku not in consolidated_skus:
            result.append(row)

    result.extend(consolidated_rows)
    return result


def _consolidate_monthly_projections(monthly_rows: List[Dict]) -> List[Dict]:
    """
    Consolidate monthly projection rows for SKU groups.

    For each month:
    - Forecast: sum of all member SKUs
    - Beginning Inventory: sum of all member SKUs
    - Ending Inventory: sum of all member SKUs
    - Order Quantity: sum of all member SKUs

    Returns original rows plus new consolidated rows.
    Individual member SKUs are removed from the list.
    """
    if not monthly_rows:
        return monthly_rows

    # Build lookup by SKU + Month
    sku_month_rows = {}
    for row in monthly_rows:
        sku = str(row.get("SKU", row.get("sku", ""))).strip().upper()
        month = row.get("Month", "")
        key = (sku, str(month))
        if key not in sku_month_rows:
            sku_month_rows[key] = []
        sku_month_rows[key].append(row)

    # Get unique months
    all_months = set()
    for row in monthly_rows:
        month = row.get("Month", "")
        if month:
            all_months.add(str(month))

    # Track which SKUs are part of consolidation groups
    consolidated_skus = set()
    consolidated_rows = []

    for consolidated_name, member_skus in SKU_CONSOLIDATION_GROUPS.items():
        member_skus_upper = [s.upper() for s in member_skus]

        # Check if any member exists in data
        has_members = any(
            (sku, month) in sku_month_rows
            for sku in member_skus_upper
            for month in all_months
        )

        if not has_members:
            continue

        for sku in member_skus_upper:
            consolidated_skus.add(sku)

        # For each month, aggregate the member SKUs
        for month in all_months:
            member_month_rows = []
            for member_sku in member_skus_upper:
                key = (member_sku, month)
                if key in sku_month_rows:
                    member_month_rows.extend(sku_month_rows[key])

            if not member_month_rows:
                continue

            # Aggregate numeric columns
            total_forecast = sum(float(r.get("Forecast", 0) or 0) for r in member_month_rows)
            total_begin_inv = sum(float(r.get("Beginning Inventory", 0) or 0) for r in member_month_rows)
            total_end_inv = sum(float(r.get("Ending Inventory", 0) or 0) for r in member_month_rows)
            total_order_qty = sum(float(r.get("Order Quantity", 0) or 0) for r in member_month_rows)

            # Use first member's metadata as base
            base_row = member_month_rows[0]

            consolidated_row = {
                "SKU": consolidated_name,
                "sku": consolidated_name,
                "Month": base_row.get("Month"),
                "Row_Type": base_row.get("Row_Type", "FCST"),
                "Forecast": total_forecast,
                "Beginning Inventory": total_begin_inv,
                "Ending Inventory": total_end_inv,
                "Order Quantity": total_order_qty,
                "vendor_name": base_row.get("vendor_name", ""),
                "collection": base_row.get("collection", ""),
                "description": base_row.get("description", ""),
                "_is_consolidated": True,
            }
            consolidated_rows.append(consolidated_row)

    # Build result: non-consolidated SKU rows + consolidated rows
    result = []
    for row in monthly_rows:
        sku = str(row.get("SKU", row.get("sku", ""))).strip().upper()
        if sku not in consolidated_skus:
            result.append(row)

    result.extend(consolidated_rows)
    return result


def _consolidate_arrivals(arrivals_rows: List[Dict]) -> List[Dict]:
    """
    Consolidate arrival rows for SKU groups.

    Arrivals are shown with consolidated SKU name but individual PO details preserved.
    """
    if not arrivals_rows:
        return arrivals_rows

    result = []
    for row in arrivals_rows:
        sku = str(row.get("ITEM_NUMBER", row.get("item_number", ""))).strip().upper()

        if sku in SKU_TO_CONSOLIDATED:
            # Create a copy with consolidated SKU name
            new_row = dict(row)
            consolidated_name = SKU_TO_CONSOLIDATED[sku]
            new_row["ITEM_NUMBER"] = consolidated_name
            new_row["item_number"] = consolidated_name
            new_row["_original_sku"] = sku
            result.append(new_row)
        else:
            result.append(row)

    return result


def _consolidate_items_list(items: List[Dict]) -> List[Dict]:
    """
    Consolidate the items list for sidebar/dropdowns.

    Creates consolidated item entries while removing individual member SKUs.
    """
    if not items:
        return items

    # Build lookup by SKU
    sku_to_item = {}
    for item in items:
        sku = str(item.get("item_number", "")).strip().upper()
        if sku:
            sku_to_item[sku] = item

    # Track which SKUs are part of consolidation groups
    consolidated_skus = set()
    consolidated_items = []

    for consolidated_name, member_skus in SKU_CONSOLIDATION_GROUPS.items():
        # Find all member items that exist
        member_items = []
        for member_sku in member_skus:
            member_sku_upper = member_sku.upper()
            if member_sku_upper in sku_to_item:
                member_items.append(sku_to_item[member_sku_upper])
                consolidated_skus.add(member_sku_upper)

        if not member_items:
            continue

        # Aggregate inventory metrics
        total_available = sum(float(i.get("available_sf", 0) or 0) for i in member_items)
        total_on_po = sum(float(i.get("on_po_sf", 0) or 0) for i in member_items)
        total_backorder = sum(float(i.get("backorder_sf", 0) or 0) for i in member_items)
        total_inv_position = sum(float(i.get("inventory_position", 0) or 0) for i in member_items)
        min_lead_time = min(float(i.get("lead_time_days", 999) or 999) for i in member_items)

        # Aggregate forecast series
        combined_series = _consolidate_forecast_series([i.get("forecast_series", {}) for i in member_items])

        # Use first member's metadata as base
        base_item = member_items[0]

        consolidated_item = {
            "item_number": consolidated_name,
            "description": base_item.get("description", ""),
            "vendor_name": base_item.get("vendor_name", ""),
            "vendor_number": base_item.get("vendor_number", ""),
            "collection": base_item.get("collection", ""),
            "available_sf": total_available,
            "on_po_sf": total_on_po,
            "backorder_sf": total_backorder,
            "inventory_position": total_inv_position,
            "lead_time_days": min_lead_time if min_lead_time < 999 else 0,
            "forecast_series": combined_series,
            "_is_consolidated": True,
            "_member_skus": member_skus,
        }
        consolidated_items.append(consolidated_item)

    # Build result: non-consolidated items + consolidated items
    result = []
    for item in items:
        sku = str(item.get("item_number", "")).strip().upper()
        if sku not in consolidated_skus:
            result.append(item)

    result.extend(consolidated_items)
    return result


def _consolidate_forecast_series(series_list: List[Dict]) -> Dict:
    """
    Combine multiple forecast series into one by summing values at each date.

    Returns a combined series with summed historical and forecast values.
    """
    if not series_list:
        return {}

    # Filter out empty series
    valid_series = [s for s in series_list if s and (s.get("hist_y") or s.get("fc_y"))]

    if not valid_series:
        return {}

    if len(valid_series) == 1:
        return valid_series[0]

    # Combine historical data
    hist_combined = {}
    for series in valid_series:
        hist_x = series.get("hist_x", [])
        hist_y = series.get("hist_y", [])
        for x, y in zip(hist_x or [], hist_y or []):
            if x not in hist_combined:
                hist_combined[x] = 0.0
            hist_combined[x] += float(y or 0)

    # Combine forecast data
    fc_combined = {}
    for series in valid_series:
        fc_x = series.get("fc_x", series.get("x", []))
        fc_y = series.get("fc_y", series.get("y", []))
        for x, y in zip(fc_x or [], fc_y or []):
            if x not in fc_combined:
                fc_combined[x] = 0.0
            fc_combined[x] += float(y or 0)

    # Sort and build result
    result = {}

    if hist_combined:
        sorted_hist = sorted(hist_combined.items())
        result["hist_x"] = [x for x, y in sorted_hist]
        result["hist_y"] = [y for x, y in sorted_hist]

    if fc_combined:
        sorted_fc = sorted(fc_combined.items())
        result["fc_x"] = [x for x, y in sorted_fc]
        result["fc_y"] = [y for x, y in sorted_fc]

    return result


def _build_queue_df_from_inventory(metrics: List[Dict]) -> pd.DataFrame:
    if not metrics:
        return pd.DataFrame()
    df = pd.DataFrame(metrics)
    if "vendor_name" not in df.columns and "collection" in df.columns:
        df["vendor_name"] = df["collection"]
    # Normalize sundries/moulding column names to match flooring convention
    col_aliases = {
        "item_number": "sku",
        "available": "available_sf",
        "on_order": "on_po_sf",
        "backorder": "backorder_sf",
    }
    for alt, canonical in col_aliases.items():
        if alt in df.columns and canonical not in df.columns:
            df[canonical] = df[alt]
    # Sundries/moulding JSON stores daily_demand instead of lead_time_mean_demand.
    # Derive it so LT Demand and Inv Days columns work for all tabs.
    if "lead_time_mean_demand" not in df.columns and "daily_demand" in df.columns and "lead_time_days" in df.columns:
        df["lead_time_mean_demand"] = (
            pd.to_numeric(df["daily_demand"], errors="coerce")
            * pd.to_numeric(df["lead_time_days"], errors="coerce")
        )

    # Months of Supply = Available / Avg Monthly Demand.
    # How many months current on-hand stock lasts before it hits zero, ignoring
    # incoming POs (those are timed events shown separately in Arrivals).
    if "available_sf" in df.columns:
        # Flooring rows carry a forecast-based avg_monthly_demand; consolidated
        # group rows and sundries/moulding rows don't, so fall back to
        # avg_weekly_demand or daily_demand, in that order, per row.
        monthly_demand = pd.Series(np.nan, index=df.index)
        if "avg_monthly_demand" in df.columns:
            monthly_demand = pd.to_numeric(df["avg_monthly_demand"], errors="coerce")
        if "avg_weekly_demand" in df.columns:
            weekly_as_monthly = pd.to_numeric(df["avg_weekly_demand"], errors="coerce") * WEEKS_PER_MONTH
            monthly_demand = monthly_demand.fillna(weekly_as_monthly)
        if "daily_demand" in df.columns:
            daily_as_monthly = pd.to_numeric(df["daily_demand"], errors="coerce") * DAYS_PER_MONTH
            monthly_demand = monthly_demand.fillna(daily_as_monthly)
        monthly_demand = monthly_demand.fillna(0.0)

        available = pd.to_numeric(df["available_sf"], errors="coerce").fillna(0.0)
        df["avg_monthly_demand_sf"] = monthly_demand
        df["months_of_supply"] = available / monthly_demand.replace(0.0, np.nan)

    rename_map = {
        "sku": "Item",
        "description": "Description",
        "vendor_name": "Vendor",
        "vendor_part_number": "Alt Item #",
        "available_sf": "Available",
        "on_po_sf": "On PO",
        "backorder_sf": "Backorder",
        "lead_time_days": "LT (days)",
        "lead_time_mean_demand": "LT Demand",
        "safety_stock": "Safety Stock",
        "reorder_point": "Reorder Point",
        "avg_monthly_demand_sf": "Avg Monthly Demand (SF)",
        "months_of_supply": "Months of Supply",
    }
    cols = [c for c in rename_map.keys() if c in df.columns]
    df = df[cols].rename(columns=rename_map)
    return df


def _build_niko_glance_df(
    metrics: List[Dict],
    raw_monthly: List[Dict],
    today: "pd.Timestamp | None" = None,
    member_snapshots: "List[Dict] | None" = None,
) -> "pd.DataFrame":
    """
    Build the Inventory At a Glance DataFrame for the Niko (Strip/Unfinished) tab.

    Columns: Item #, Description, On Hand, Available, On PO, Back Order,
             Inv. Position, EMU, Avg 3M Forecast, LT Demand, Safety Stock,
             ROP, ROQ (S).

    EMU formula (example: last completed month = March 2026):
        Window1 = (Dec + Jan + Feb) / 3
        Window2 = (Jan + Feb + Mar) / 3
        EMU = (Window1 + Window2) / 2

    Avg 3M Forecast = average of the next 3 calendar months' FCST values.
    Planning metrics (LT Demand, Safety Stock, ROP, ROQ) come from inventory
    planning results; for consolidated groups they are split equally across members.
    """
    if not metrics:
        return pd.DataFrame()

    if today is None:
        today = pd.Timestamp.today().normalize()

    # Last completed month (first day)
    if today.month == 1:
        last_month = pd.Timestamp(today.year - 1, 12, 1)
    else:
        last_month = pd.Timestamp(today.year, today.month - 1, 1)

    # Next 3 forecast months (month starts, beginning from next month)
    def _advance_month(dt: "pd.Timestamp") -> "pd.Timestamp":
        return pd.Timestamp(dt.year + 1, 1, 1) if dt.month == 12 else pd.Timestamp(dt.year, dt.month + 1, 1)

    current_month = today.replace(day=1)
    next_3_months: List["pd.Timestamp"] = []
    _m = current_month
    for _ in range(3):
        _m = _advance_month(_m)
        next_3_months.append(_m)

    # Build per-SKU monthly lookups from Monthly_Projections rows
    sku_hist: Dict[str, Dict] = {}   # {sku_upper: {month_ts: hist_demand}}
    sku_fcst: Dict[str, Dict] = {}   # {sku_upper: {month_ts: forecast}}
    for row in raw_monthly:
        sku_key = str(row.get("SKU", row.get("sku", ""))).strip().upper()
        if not sku_key:
            continue
        try:
            month_ts = pd.Timestamp(row["Month"]).normalize().replace(day=1)
        except Exception:
            continue

        hist_val = row.get("Historical Demand")
        if hist_val is not None and not (isinstance(hist_val, float) and math.isnan(hist_val)):
            sku_hist.setdefault(sku_key, {})[month_ts] = float(hist_val)

        row_type = str(row.get("Row_Type", row.get("row_type", ""))).upper()
        fc_val = row.get("Forecast")
        if row_type == "FCST" and fc_val is not None and not (isinstance(fc_val, float) and math.isnan(fc_val)):
            sku_fcst.setdefault(sku_key, {})[month_ts] = float(fc_val)

    def _get_demand(sku_key: str, month_ts: "pd.Timestamp") -> float:
        return sku_hist.get(sku_key.upper(), {}).get(month_ts, 0.0)

    def _get_forecast(sku_key: str, month_ts: "pd.Timestamp") -> float:
        return sku_fcst.get(sku_key.upper(), {}).get(month_ts, 0.0)

    def _prev_month(dt: "pd.Timestamp", n: int = 1) -> "pd.Timestamp":
        for _ in range(n):
            dt = pd.Timestamp(dt.year - 1, 12, 1) if dt.month == 1 else pd.Timestamp(dt.year, dt.month - 1, 1)
        return dt

    # EMU reference months
    M = last_month
    M1 = _prev_month(M, 1)
    M2 = _prev_month(M, 2)
    M3 = _prev_month(M, 3)

    # Build per-member snapshot lookup from the persisted individual data.
    # {sku_upper: {"available_sf": X, "on_po_sf": X, "backorder_sf": X,
    #              "description": ..., "vendor_name": ...,
    #              "monthly_sales": {"YYYY-MM-DD": qty, ...}}}
    snap_lookup: Dict[str, Dict] = {}
    for snap in (member_snapshots or []):
        snap_key = str(snap.get("sku", "")).strip().upper()
        if snap_key:
            snap_lookup[snap_key] = snap
            # Pre-parse monthly_sales into {Timestamp: float} for fast lookup
            _ms_raw = snap.get("monthly_sales") or {}
            snap["_monthly_ts"] = {
                pd.Timestamp(k).normalize().replace(day=1): v
                for k, v in _ms_raw.items()
            }

    def _snap_demand(snap: Dict, month_ts: "pd.Timestamp") -> float:
        return snap.get("_monthly_ts", {}).get(month_ts, 0.0)

    rows = []
    for m in metrics:
        group_sku = str(m.get("sku", m.get("item_number", ""))).strip().upper()

        # Expand consolidated group rows into one row per member SKU.
        is_group = group_sku in SKU_GROUP_KEYS
        if is_group:
            member_skus = [s.upper() for s in SKU_CONSOLIDATION_GROUPS.get(group_sku, [group_sku])]
        else:
            member_skus = [group_sku]

        n = max(len(member_skus), 1)

        # Group-level pooled EMU — used as the denominator for EMU-ratio scaling.
        # Computed from the pooled monthly history stored under the group SKU key.
        grp_w1 = (_get_demand(group_sku, M3) + _get_demand(group_sku, M2) + _get_demand(group_sku, M1)) / 3.0
        grp_w2 = (_get_demand(group_sku, M2) + _get_demand(group_sku, M1) + _get_demand(group_sku, M)) / 3.0
        group_emu = (grp_w1 + grp_w2) / 2.0

        # Group-level planning metrics (used as the base for scaling)
        group_3m_forecast   = sum(_get_forecast(group_sku, mt) for mt in next_3_months) / 3.0
        group_lt_demand     = float(m.get("lead_time_mean_demand", 0) or 0)
        group_ss            = float(m.get("safety_stock", 0) or 0)
        group_rop           = float(m.get("reorder_point", 0) or 0)
        group_order_up_to   = float(m.get("order_up_to_level", 0) or 0)

        for member_sku in member_skus:
            snap = snap_lookup.get(member_sku)

            # Inventory: use individual snapshot when available, else split equally
            if snap:
                inv_available = float(snap.get("available_sf", 0) or 0)
                inv_on_po     = float(snap.get("on_po_sf",    0) or 0)
                inv_backorder = float(snap.get("backorder_sf", 0) or 0)
                description   = snap.get("description") or str(m.get("description", ""))
            else:
                inv_available = float(m.get("available_sf", 0) or 0) / n
                inv_on_po     = float(m.get("on_po_sf",    0) or 0) / n
                inv_backorder = float(m.get("backorder_sf", 0) or 0) / n
                description   = str(m.get("description", ""))

            # On Hand = physical units on shelf (Available + Backorder commitments)
            inv_on_hand = inv_available + inv_backorder
            # Inventory Position = Available + On PO - Back Order
            inv_position = inv_available + inv_on_po - inv_backorder

            # Member EMU from individual snapshot history (already per-member)
            def _demand(month_ts: "pd.Timestamp") -> float:
                if snap:
                    return _snap_demand(snap, month_ts)
                return _get_demand(group_sku, month_ts) / n

            w1 = (_demand(M3) + _demand(M2) + _demand(M1)) / 3.0
            w2 = (_demand(M2) + _demand(M1) + _demand(M)) / 3.0
            member_emu = (w1 + w2) / 2.0

            # For consolidated groups: scale planning metrics by the member's EMU share
            # of the group's pooled EMU so each member reflects its own demand level.
            # For standalone SKUs (is_group=False): use group metrics directly (n=1).
            if is_group and group_emu > 0:
                ratio = member_emu / group_emu
            else:
                ratio = 1.0 / n

            avg_3m_forecast = group_3m_forecast * ratio
            lt_demand       = group_lt_demand * ratio
            safety_stock    = group_ss * ratio
            rop             = group_rop * ratio
            member_s        = group_order_up_to * ratio
            roq_s           = max(0.0, member_s - inv_position)

            rec: Dict = {
                "Item #": member_sku,
                "Description": description,
                "On Hand": inv_on_hand,
                "Available": inv_available,
                "On PO": inv_on_po,
                "Back Order": inv_backorder,
                "Inv. Position": inv_position,
                "EMU": member_emu,
                "Avg 3M Forecast": avg_3m_forecast,
                "LT Demand": lt_demand,
                "Safety Stock": safety_stock,
                "ROP": rop,
                "ROQ": roq_s,
            }
            rows.append(rec)

    return pd.DataFrame(rows)


def _build_niko_grouped_df(
    metrics: List[Dict],
    raw_monthly: List[Dict],
    today: "pd.Timestamp | None" = None,
) -> "pd.DataFrame":
    """
    Build the Grouped Demand DataFrame for the Niko tab.

    Same columns as _build_niko_glance_df but keeps consolidated group rows
    intact (does not expand into individual member SKUs).  Planning metrics
    are shown at the group level without splitting across members.
    """
    if not metrics:
        return pd.DataFrame()

    if today is None:
        today = pd.Timestamp.today().normalize()

    if today.month == 1:
        last_month = pd.Timestamp(today.year - 1, 12, 1)
    else:
        last_month = pd.Timestamp(today.year, today.month - 1, 1)

    def _advance_month(dt: "pd.Timestamp") -> "pd.Timestamp":
        return pd.Timestamp(dt.year + 1, 1, 1) if dt.month == 12 else pd.Timestamp(dt.year, dt.month + 1, 1)

    current_month = today.replace(day=1)
    next_3_months: List["pd.Timestamp"] = []
    _m = current_month
    for _ in range(3):
        _m = _advance_month(_m)
        next_3_months.append(_m)

    sku_hist: Dict[str, Dict] = {}
    sku_fcst: Dict[str, Dict] = {}
    for row in raw_monthly:
        sku_key = str(row.get("SKU", row.get("sku", ""))).strip().upper()
        if not sku_key:
            continue
        try:
            month_ts = pd.Timestamp(row["Month"]).normalize().replace(day=1)
        except Exception:
            continue
        hist_val = row.get("Historical Demand")
        if hist_val is not None and not (isinstance(hist_val, float) and math.isnan(hist_val)):
            sku_hist.setdefault(sku_key, {})[month_ts] = float(hist_val)
        row_type = str(row.get("Row_Type", row.get("row_type", ""))).upper()
        fc_val = row.get("Forecast")
        if row_type == "FCST" and fc_val is not None and not (isinstance(fc_val, float) and math.isnan(fc_val)):
            sku_fcst.setdefault(sku_key, {})[month_ts] = float(fc_val)

    def _get_demand(sku_key: str, month_ts: "pd.Timestamp") -> float:
        return sku_hist.get(sku_key.upper(), {}).get(month_ts, 0.0)

    def _get_forecast(sku_key: str, month_ts: "pd.Timestamp") -> float:
        return sku_fcst.get(sku_key.upper(), {}).get(month_ts, 0.0)

    def _prev_month(dt: "pd.Timestamp", n: int = 1) -> "pd.Timestamp":
        for _ in range(n):
            dt = pd.Timestamp(dt.year - 1, 12, 1) if dt.month == 1 else pd.Timestamp(dt.year, dt.month - 1, 1)
        return dt

    M = last_month
    M1 = _prev_month(M, 1)
    M2 = _prev_month(M, 2)
    M3 = _prev_month(M, 3)

    rows = []
    for m in metrics:
        group_sku = str(m.get("sku", m.get("item_number", ""))).strip().upper()
        inv_available = float(m.get("available_sf", 0) or 0)
        inv_on_po     = float(m.get("on_po_sf", 0) or 0)
        inv_backorder = float(m.get("backorder_sf", 0) or 0)
        description   = str(m.get("description", ""))

        inv_on_hand  = inv_available + inv_backorder
        inv_position = inv_available + inv_on_po - inv_backorder

        w1 = (_get_demand(group_sku, M3) + _get_demand(group_sku, M2) + _get_demand(group_sku, M1)) / 3.0
        w2 = (_get_demand(group_sku, M2) + _get_demand(group_sku, M1) + _get_demand(group_sku, M)) / 3.0
        emu = (w1 + w2) / 2.0

        avg_3m_forecast = sum(_get_forecast(group_sku, mt) for mt in next_3_months) / 3.0

        lt_demand     = float(m.get("lead_time_mean_demand", 0) or 0)
        safety_stock  = float(m.get("safety_stock", 0) or 0)
        rop           = float(m.get("reorder_point", 0) or 0)
        order_up_to   = float(m.get("order_up_to_level", 0) or 0)
        roq_s         = max(0.0, order_up_to - inv_position)

        rows.append({
            "Item #":          group_sku,
            "Description":     description,
            "On Hand":         inv_on_hand,
            "Available":       inv_available,
            "On PO":           inv_on_po,
            "Back Order":      inv_backorder,
            "Inv. Position":   inv_position,
            "EMU":             emu,
            "Avg 3M Forecast": avg_3m_forecast,
            "LT Demand":       lt_demand,
            "Safety Stock":    safety_stock,
            "ROP":             rop,
            "ROQ (S)":         roq_s,
        })

    return pd.DataFrame(rows)


def _render_niko_glance_table_html(df: "pd.DataFrame") -> str:
    """Render the Niko Ungrouped/Grouped Demand table.  Individual SKUs are shown;
    members of a consolidation group keep the blue row-group highlight."""
    if df is None or df.empty:
        return (
            '<div class="queue-card">'
            '<div class="queue-empty">No items for selected vendor.</div>'
            '</div>'
        )

    col_weights = {
        "Item #": "1.0fr",
        "Description": "2.2fr",
        "On Hand": "0.8fr",
        "Available": "0.8fr",
        "On PO": "0.7fr",
        "Back Order": "0.75fr",
        "Inv. Position": "0.9fr",
        "EMU": "0.8fr",
        "Avg 3M Forecast": "1.0fr",
        "LT Demand": "0.85fr",
        "Safety Stock": "0.9fr",
        "ROP": "0.7fr",
        "ROQ": "0.75fr",
    }

    headers = "".join(f"<div>{col}</div>" for col in df.columns)
    rows_html = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            value = row[col]
            if col not in ("Item #", "Description", "Vendor") and isinstance(value, (int, float)):
                fv = float(value)
                value = "" if not math.isfinite(fv) else _format_number(math.ceil(fv), 0)
            cells.append(f"<div class='text-clip'>{value}</div>")
        sku = str(row.get("Item #", "")).strip().upper()
        if sku in SKU_GROUP_KEYS or sku in SKU_TO_CONSOLIDATED:
            row_class = "queue-row row-group"
        else:
            row_class = "queue-row row-standalone"
        rows_html.append(f"<div class='{row_class}'>{''.join(cells)}</div>")

    col_count = len(df.columns)
    col_template = " ".join(col_weights.get(col, "1fr") for col in df.columns)
    return (
        f'<div class="queue-card">'
        f'<div class="queue-table" style="--col-count:{col_count}; --col-template:{col_template};">'
        f'<div class="queue-row queue-head">{headers}</div>'
        f"{''.join(rows_html)}"
        f'</div>'
        f'</div>'
    )


def _render_queue_table_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return """
        <div class="queue-card">
          <div class="queue-empty">No items for selected vendor.</div>
        </div>
        """
    col_weights = {
        "Item": "0.9fr",
        "Description": "2.6fr",
        "Vendor": "1.3fr",
        "Alt Item #": "1.0fr",
        "Available": "1.0fr",
        "On PO": "0.7fr",
        "Backorder": "0.7fr",
        "LT (days)": "0.8fr",
        "LT Demand": "0.9fr",
        "Reorder Point": "0.9fr",
        "Avg Monthly Demand (SF)": "1.1fr",
        "Months of Supply": "1.0fr",
    }
    headers = "".join(f"<div>{col}</div>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            value = row[col]
            if col == "Months of Supply" and isinstance(value, (int, float)):
                fv = float(value)
                value = "--" if not math.isfinite(fv) else _format_number(fv, 1)
            elif isinstance(value, (int, float)) and col not in ("Item", "Description", "Vendor"):
                fv = float(value)
                value = "" if not math.isfinite(fv) else _format_number(math.ceil(fv), 0)
            cells.append(f"<div class='text-clip'>{value}</div>")
        sku = str(row.get("Item", "")).strip().upper()
        if sku in SKU_GROUP_KEYS:
            row_class = "queue-row row-group"
        elif sku in SKU_TO_CONSOLIDATED:
            row_class = "queue-row row-member"
        else:
            row_class = "queue-row row-standalone"
        rows.append(f"<div class='{row_class}'>{''.join(cells)}</div>")
    col_count = len(df.columns)
    col_template = " ".join(col_weights.get(col, "1fr") for col in df.columns)
    return f"""
    <div class="queue-card">
      <div class="queue-table" style="--col-count:{col_count}; --col-template:{col_template};">
        <div class="queue-row queue-head">{headers}</div>
        {''.join(rows)}
      </div>
    </div>
    """

def _render_reorder_now_table_html(df: pd.DataFrame, month_label: str) -> str:
    if df is None or df.empty:
        return f"""
        <div class="queue-card">
          <div class="queue-empty">No reorder quantities for this month.</div>
        </div>
        """
    col_weights = {
        "Item Number":                      "1.1fr",
        "Collection":                       "1.0fr",
        "Description":                      "2.0fr",
        "Vendor #":                         "0.8fr",
        "UOM":                              "0.6fr",
        "LT":                               "0.6fr",
        "LT Demand":                        "0.8fr",
        "Available":                        "0.9fr",
        "Available (SF)":                   "0.9fr",
        "Back Orders":                      "0.9fr",
        "On PO":                            "0.7fr",
        "30D Sales":                        "0.8fr",
        "90D Avg":                          "0.8fr",
        "365D Avg":                         "0.8fr",
        "ROP":                              "0.8fr",
        "Reorder Point (SF)":               "1.0fr",
        "Reorder Quantity":                 "1.0fr",
        "Reorder Quantity (SF)":            "1.0fr",
        "Reorder Quantity (Pallets)":       "1.0fr",
        "Reorder Quantity (% of Container)":"1.1fr",
        "Lead Time (days)":                 "0.9fr",
        "LT Demand (SF)":                   "0.9fr",
        "Safety Stock (SF)":                "0.9fr",
        "Last 90 Days Sales (SF)":          "1.1fr",
        "Inventory Position":               "1.0fr",
        "IP":                               "0.7fr",
        "Order Up To":                      "0.9fr",
        "Avg Weekly Demand":                "1.0fr",
        "Actual Order":                     "1.0fr",
    }
    # Numeric columns that get whole-number (ceiling) formatting
    _whole_num_cols = {
        "Reorder Quantity (SF)", "Reorder Quantity (Pallets)",
        "Reorder Quantity", "Quantity Needed",
        "Available", "Available (SF)", "Back Orders", "On PO",
        "LT", "LT Demand", "30D Sales", "90D Avg", "365D Avg", "ROP",
        "Reorder Point (SF)",
        "Lead Time (days)", "LT Demand (SF)", "Safety Stock (SF)",
        "Last 90 Days Sales (SF)",
        "Inventory Position", "Avg Weekly Demand", "Actual Order",
        "S (Order Up to Level)", "ROQ (S \u2212 IP)",
    }
    _zero_decimal_cols = {"IP"}
    # Header cells use a wrap class so long labels break across two lines
    # without widening the column.
    headers = "".join(f"<div class='qh-cell'>{col}</div>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            value = row[col]
            if isinstance(value, (int, float)) and col in _zero_decimal_cols:
                if pd.isna(value):
                    value = ""
                else:
                    value = _format_number(float(value), 0)
            elif isinstance(value, (int, float)) and col in _whole_num_cols:
                if pd.isna(value):
                    value = ""
                else:
                    value = _format_number(math.ceil(float(value)), 0)
            elif isinstance(value, (int, float)) and col == "Reorder Quantity (% of Container)":
                if pd.isna(value):
                    value = ""
                else:
                    value = f"{float(value) * 100:.1f}%"
            elif value is None or (isinstance(value, float) and pd.isna(value)):
                value = ""
            cells.append(f"<div class='text-clip'>{value}</div>")
        sku = str(row.get("Item Number", "")).strip().upper()
        if sku == "TOTAL":
            row_class = "queue-row"
        elif sku in SKU_GROUP_KEYS:
            row_class = "queue-row row-group"
        elif sku in SKU_TO_CONSOLIDATED:
            row_class = "queue-row row-member"
        else:
            row_class = "queue-row row-standalone"
        rows.append(f"<div class='{row_class}'>{''.join(cells)}</div>")
    col_count = len(df.columns)
    col_template = " ".join(col_weights.get(col, "1fr") for col in df.columns)
    return f"""
    <style>
      .qh-cell {{
        white-space: normal;
        overflow-wrap: break-word;
        line-height: 1.25;
        display: flex;
        align-items: flex-end;
        padding-bottom: 2px;
      }}
      .queue-head {{
        align-items: stretch;
      }}
    </style>
    <div class="queue-card">
      <div class="queue-table" style="--col-count:{col_count}; --col-template:{col_template};">
        <div class="queue-row queue-head">{headers}</div>
        {''.join(rows)}
      </div>
    </div>
    """

def _render_reorder_table_html(df: pd.DataFrame, title: str) -> str:
    if df is None or df.empty:
        return f"""
        <div class="queue-card">
          <div class="queue-empty">No reordering data available.</div>
        </div>
        """
    headers = "".join(f"<div>{col}</div>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            value = row[col]
            if isinstance(value, (int, float)):
                if col == "Reorder Quantity" and abs(float(value)) < 1e-9:
                    value = ""
                else:
                    value = _format_number(float(value), 2)
            elif value is None:
                value = ""
            cells.append(f"<div class='text-clip'>{value}</div>")
        rows.append(f"<div class='queue-row'>{''.join(cells)}</div>")
    col_count = len(df.columns)
    # Extract SKU and description from title (format: "Reorder Schedule:   SKU Description")
    label, details = title.split(":", 1) if ":" in title else ("", title)
    parts = details.strip().split(" ", 1)
    sku = parts[0] if parts else ""
    desc = parts[1] if len(parts) > 1 else ""
    return f"""
    <div class="queue-card" style="max-width: 880px;">
      <div class="queue-header">
          <div class="reorder-header" style="--col-count:{col_count};">
          <div class="reorder-sku">{sku}</div>
          <div class="reorder-desc">{desc}</div>
        </div>
      </div>
      <div class="queue-table" style="--col-count:{col_count};">
        <div class="queue-row queue-head">{headers}</div>
        {''.join(rows)}
      </div>
    </div>
    """

def _margin_point_change(current_margin: float, baseline_margin: float) -> Optional[float]:
    try:
        current = float(current_margin)
        baseline = float(baseline_margin)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or not math.isfinite(baseline):
        return None
    if abs(baseline) < 1e-9:
        return None
    return current - baseline


_HISTORY_MARGIN_NUMERIC_SORT_COLS = {
    "Selling Price",
    "Cost (Avg Inv)",
    "Margin (Avg Inv)",
    "Margin Chg 30D",
    "Cost (Last PO)",
    "Margin (Last PO)",
    "Margin Chg Last PO",
}
_HISTORY_MARGIN_ZERO_IS_MISSING_COLS = {
    "Selling Price",
    "Cost (Avg Inv)",
    "Margin (Avg Inv)",
    "Cost (Last PO)",
    "Margin (Last PO)",
}


def _sort_history_margins_df(df: pd.DataFrame, sort_col: str, ascending: bool) -> pd.DataFrame:
    if df is None or df.empty or sort_col not in df.columns:
        return df

    out = df.copy()
    sort_key = "_history_margin_sort_key"
    if sort_col == "Last PO Date":
        out[sort_key] = pd.to_datetime(out[sort_col], errors="coerce")
    elif sort_col in _HISTORY_MARGIN_NUMERIC_SORT_COLS:
        values = pd.to_numeric(out[sort_col], errors="coerce")
        if sort_col in _HISTORY_MARGIN_ZERO_IS_MISSING_COLS:
            values = values.mask(values.abs() < 1e-9)
        out[sort_key] = values
    else:
        out[sort_key] = out[sort_col].fillna("").astype(str).str.lower()

    return (
        out.sort_values(sort_key, ascending=ascending, na_position="last", kind="mergesort")
        .drop(columns=[sort_key])
        .reset_index(drop=True)
    )


def _jump_anchor_id(card_key: str) -> str:
    safe_key = "".join(ch if ch.isalnum() else "-" for ch in str(card_key).lower()).strip("-")
    return f"jump-card-{safe_key}"


def _jump_anchor(card_key: str) -> None:
    st.markdown(
        f'<span id="{_jump_anchor_id(card_key)}" class="jump-anchor"></span>',
        unsafe_allow_html=True,
    )


def _jump_cards_for_tab(tab_name: str, include_3m: bool = False) -> List[Tuple[str, str]]:
    cards: List[Tuple[str, str]] = [
        ("top", "Top of Dashboard"),
        ("demand", "Demand"),
        ("arrivals", "Arrivals"),
    ]
    if tab_name == "Niko":
        cards.extend([
            ("ungrouped-demand", "Ungrouped Demand"),
            ("grouped-demand", "Grouped Demand"),
            ("vendor-quoted-costs", "Vendor Quoted Costs"),
            ("optimizer", "Optimizer"),
            ("reorder-schedule", "Reorder Schedule"),
        ])
    elif tab_name == "Carlos":
        cards.extend([
            ("reorder-now", "Reorder Now"),
            ("inventory-glance", "Inventory At a Glance"),
            ("transfer-report", "Transfer Report"),
        ])
        if include_3m:
            cards.append(("3m-ordering-processor", "3M Ordering Processor"))
        cards.append(("history-margins", "History and Margins"))
    elif tab_name == "Dilan":
        cards.extend([
            ("inventory-glance", "Inventory At a Glance"),
            ("reorder-now", "Reorder Now"),
            ("optimizer", "Optimizer"),
            ("reorder-schedule", "Reorder Schedule"),
        ])
    else:
        cards.extend([
            ("inventory-glance", "Inventory At a Glance"),
            ("reorder-now", "Reorder Now"),
            ("reorder-schedule", "Reorder Schedule"),
        ])
    return cards


def _render_sidebar_jump_control(cards: List[Tuple[str, str]], tab_name: str) -> None:
    if not cards:
        return

    labels = [label for _, label in cards]
    label_to_key = {label: key for key, label in cards}
    selected_label = st.sidebar.selectbox(
        "Jump to card",
        labels,
        index=0,
        key=f"jump_to_card_{tab_name.lower()}",
    )
    selected_key = label_to_key.get(selected_label, cards[0][0])
    st.sidebar.markdown(
        f"""
        <a class="sidebar-jump-button" href="#{_jump_anchor_id(selected_key)}">
          Jump to selected card
        </a>
        """,
        unsafe_allow_html=True,
    )


def _render_history_margins_table_html(
    df: pd.DataFrame,
    sort_col: Optional[str] = None,
    sort_ascending: bool = True,
) -> str:
    if df is None or df.empty:
        return """
        <div class="queue-card">
          <div class="queue-empty">No history and margin data available.</div>
        </div>
        """
    col_weights = {
        "Item Number":      "1.1fr",
        "Description":      "2.2fr",
        "Vendor Name":      "1.4fr",
        "Selling Price":    "0.9fr",
        "Cost (Avg Inv)":   "1.0fr",
        "Margin (Avg Inv)": "0.9fr",
        "Margin Chg 30D":   "0.85fr",
        "Cost (Last PO)":   "1.0fr",
        "Margin (Last PO)": "0.9fr",
        "Margin Chg Last PO": "0.85fr",
        "Last PO Date":     "0.9fr",
    }
    def _header_cell(col: str) -> str:
        label = html.escape(str(col))
        if col == sort_col:
            direction = "ASC" if sort_ascending else "DESC"
            label += (
                " <span style='font-size:10px;color:#1e3a5f;"
                "font-weight:700;letter-spacing:0;margin-left:4px;'>"
                f"{direction}</span>"
            )
        return f"<div>{label}</div>"

    headers = "".join(_header_cell(col) for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            value = row[col]
            if col == "Available":
                try:
                    fv = float(value)
                    value = _format_number(fv, 0) if math.isfinite(fv) else ""
                except (TypeError, ValueError):
                    value = "" if pd.isna(value) else value
            elif col == "Selling Price":
                try:
                    fv = float(value)
                    value = f"${fv:,.2f}" if fv > 0 and math.isfinite(fv) else "—"
                except (TypeError, ValueError):
                    value = "—"
            elif col in ("Cost (Avg Inv)", "Cost (Last PO)"):
                try:
                    fv = float(value)
                    value = f"${fv:,.2f}" if fv > 0 and math.isfinite(fv) else "—"
                except (TypeError, ValueError):
                    value = "—"
            elif col in ("Margin (Avg Inv)", "Margin (Last PO)"):
                try:
                    fv = float(value)
                    if not math.isfinite(fv) or fv == 0:
                        value = "—"
                    else:
                        pct = fv * 100
                        color = "#16a34a" if pct >= 40 else ("#d97706" if pct >= 20 else "#dc2626")
                        value = f"<span style='color:{color};font-weight:600;'>{pct:.1f}%</span>"
                except (TypeError, ValueError):
                    value = "—"
            elif col in ("Margin Chg 30D", "Margin Chg Last PO"):
                try:
                    fv = float(value)
                    if not math.isfinite(fv):
                        value = "â€”"
                    else:
                        pct = fv * 100
                        color = "#16a34a" if pct > 0 else ("#dc2626" if pct < 0 else "#64748b")
                        sign = "+" if pct > 0 else ""
                        value = f"<span style='color:{color};font-weight:600;'>{sign}{pct:.1f}%</span>"
                except (TypeError, ValueError):
                    value = "â€”"
            elif col == "Last PO Date":
                if pd.isna(value) or str(value).strip() in ("", "None", "NaT", "nan"):
                    value = "—"
                else:
                    try:
                        value = pd.Timestamp(value).strftime("%m/%d/%Y")
                    except Exception:
                        pass
            if col in ("Margin Chg 30D", "Margin Chg Last PO") and not str(value).startswith("<span"):
                value = "--"
            cells.append(f"<div class='text-clip'>{value}</div>")
        rows.append(f"<div class='queue-row row-standalone'>{''.join(cells)}</div>")
    col_count = len(df.columns)
    col_template = " ".join(col_weights.get(col, "1fr") for col in df.columns)
    return f"""
    <div class="queue-card">
      <div class="queue-table" style="--col-count:{col_count}; --col-template:{col_template};">
        <div class="queue-row queue-head">{headers}</div>
        {''.join(rows)}
      </div>
    </div>
    """


def _render_niko_optimizer_tables(
    optimizer_rows: List[Dict[str, Any]],
    vendor_names: List[str],
    optimization_result: Dict[str, Any],
) -> str:
    def _truck_color(vendor: str, truck_number: int) -> str:
        vendor_key = normalize_vendor_name(vendor).upper()
        shades = NIKO_VENDOR_TRUCK_COLORS.get(vendor_key)
        if shades is None:
            offset = sum(ord(ch) for ch in vendor_key) % len(_NIKO_FALLBACK_VENDOR_COLORS)
            shades = _NIKO_FALLBACK_VENDOR_COLORS[offset]
        shade_idx = max(0, int(truck_number) - 1) % len(shades)
        return shades[shade_idx]

    if not optimizer_rows:
        return """
        <div class="queue-card">
          <div class="queue-empty">No reorder quantities for this month.</div>
        </div>
        """

    item_costs = {
        normalize_item_key(sku): value
        for sku, value in (optimization_result.get("item_costs") or {}).items()
    }
    vendor_summary = optimization_result.get("vendor_summary") or {}
    omitted_items = optimization_result.get("omitted_items") or []
    total_added_qty = float(optimization_result.get("total_added_qty", 0.0) or 0.0)
    total_material_cost = float(optimization_result.get("total_material_cost", 0.0) or 0.0)
    total_freight_cost = float(optimization_result.get("total_freight_cost", 0.0) or 0.0)
    total_cost = float(optimization_result.get("total_cost", 0.0) or 0.0)

    display_rows = [
        row for row in optimizer_rows
        if normalize_item_key(row.get("sku")) in item_costs
    ]
    if not display_rows:
        return """
        <div class="queue-card">
          <div class="queue-empty">No optimizer lines remain after applying the current purchasing rules.</div>
        </div>
        """

    header_cells = "".join(f"<th>{html.escape(str(vendor))}</th>" for vendor in vendor_names)
    quote_rows_html: List[str] = []

    for row in display_rows:
        sku = normalize_item_key(row.get("sku"))
        desc = str(row.get("description", "")).strip()
        if desc.lower() in ("nan", "none"):
            desc = ""
        prices = row.get("prices", {}) or {}
        item = item_costs.get(sku, {})
        winning_vendor = item.get("vendor", "")
        label_html = (
            f"<div class='niko-opt-item'>{html.escape(sku)}</div>"
            f"<div class='niko-opt-item-sub'>{html.escape(desc)}</div>"
            if desc else f"<div class='niko-opt-item'>{html.escape(sku)}</div>"
        )

        quote_cells: List[str] = []
        for vendor in vendor_names:
            price_val = prices.get(vendor)
            if winning_vendor == vendor and price_val is not None:
                final_qty = float(item.get("qty", 0.0) or 0.0)
                original_qty = float(item.get("original_qty", final_qty) or final_qty)
                added_qty = float(item.get("added_qty", 0.0) or 0.0)
                rounded_qty = float(item.get("rounded_qty", 0.0) or 0.0)
                truck_labels = item.get("truck_labels", []) or []
                primary_truck = int(item.get("primary_truck", 1) or 1)
                chip_color = _truck_color(vendor, primary_truck)
                landed_cost_per_sf = float(item.get("landed_cost", 0.0) or 0.0)
                landed_cost_str = f" (${landed_cost_per_sf:.2f}/SF)" if landed_cost_per_sf > 0 else ""
                sub_parts = []
                if rounded_qty > 1e-6:
                    sub_parts.append(f"rounded from {original_qty:,.0f} SF")
                if added_qty > 1e-6:
                    sub_parts.append(f"+{added_qty:,.0f} SF fill")
                if truck_labels:
                    sub_parts.append(" / ".join(f"T{int(truck)}" for truck in truck_labels))
                sub_html = f"<div class='niko-opt-cell-sub'>{html.escape(' | '.join(sub_parts))}</div>" if sub_parts else ""
                quote_cells.append(
                    f"<td class='niko-opt-choice-cell' style='background:{chip_color};'>"
                    f"<div class='niko-opt-cell-main'>{final_qty:,.0f} SF @ ${float(price_val):.2f}{landed_cost_str}</div>{sub_html}</td>"
                )
            elif price_val is not None:
                quote_cells.append(
                    f"<td class='niko-opt-price-cell'>${float(price_val):.2f}</td>"
                )
            else:
                quote_cells.append("<td class='niko-opt-price-cell niko-opt-empty'>&nbsp;</td>")

        quote_rows_html.append(f"<tr><th>{label_html}</th>{''.join(quote_cells)}</tr>")

    vendor_total_sf_cells = []
    vendor_added_sf_cells = []
    vendor_freight_cells = []
    vendor_landed_total_cells = []
    for vendor in vendor_names:
        summary = vendor_summary.get(vendor, {})
        qty_sf = float(summary.get("qty_sf", 0.0) or 0.0)
        added_sf = float(summary.get("added_qty_sf", 0.0) or 0.0)
        freight_total = float(summary.get("freight_total", 0.0) or 0.0)
        landed_total = float(summary.get("landed_total", 0.0) or 0.0)
        vendor_total_sf_cells.append(
            f"<td class='niko-opt-footer-cell'>{qty_sf:,.0f} SF</td>" if qty_sf > 0 else "<td class='niko-opt-footer-cell niko-opt-empty'>-</td>"
        )
        vendor_added_sf_cells.append(
            f"<td class='niko-opt-footer-cell'>{added_sf:,.0f} SF</td>" if added_sf > 0 else "<td class='niko-opt-footer-cell niko-opt-empty'>-</td>"
        )
        vendor_freight_cells.append(
            f"<td class='niko-opt-footer-cell'>${freight_total:,.2f}</td>" if freight_total > 0 else "<td class='niko-opt-footer-cell niko-opt-empty'>-</td>"
        )
        vendor_landed_total_cells.append(
            f"<td class='niko-opt-footer-cell'>${landed_total:,.2f}</td>" if landed_total > 0 else "<td class='niko-opt-footer-cell niko-opt-empty'>-</td>"
        )

    legend_parts: List[str] = []
    seen_legends = set()
    for item in item_costs.values():
        vendor = str(item.get("vendor", ""))
        for truck in item.get("truck_labels", []) or []:
            label = f"{vendor} T{truck}"
            if label in seen_legends:
                continue
            seen_legends.add(label)
            legend_parts.append(
                f"<span class='niko-opt-legend-chip' style='background:{_truck_color(vendor, int(truck))};'>{html.escape(label)}</span>"
            )
    summary_text = (
        f"Total material ${total_material_cost:,.2f} | Freight ${total_freight_cost:,.2f} | "
        f"Total landed ${total_cost:,.2f} | Optional pallet add ${total_added_qty:,.0f} SF"
    )
    policy_bits = []
    rounded_count = sum(1 for item in item_costs.values() if float(item.get("rounded_qty", 0.0) or 0.0) > 1e-6)
    if rounded_count:
        policy_bits.append(f"{rounded_count} lines rounded to {NIKO_MIN_FULL_PALLET_QTY_SF:,.0f} SF")
    if omitted_items:
        policy_bits.append(f"{len(omitted_items)} lines omitted under {NIKO_MIN_PURCHASABLE_QTY_SF:,.0f} SF")
    policy_text = " | ".join(policy_bits)
    omitted_text = ""
    if omitted_items:
        omitted_preview = ", ".join(
            f"{item.get('sku')} ({float(item.get('original_qty', 0.0) or 0.0):,.0f} SF)"
            for item in omitted_items[:8]
        )
        if len(omitted_items) > 8:
            omitted_preview += ", ..."
        omitted_text = f"<div class='niko-opt-policy-note'>Omitted lines: {html.escape(omitted_preview)}</div>"

    return f"""
    <div class="queue-card niko-opt-card">
      <div class="niko-opt-summary">{html.escape(summary_text)}</div>
      <div class="niko-opt-sheet-title">Optimizer</div>
      {f"<div class='niko-opt-policy-note'>{html.escape(policy_text)}</div>" if policy_text else ""}
      <table class="niko-opt-table">
        <thead>
          <tr><th>Item</th>{header_cells}</tr>
        </thead>
        <tbody>
          {''.join(quote_rows_html)}
        </tbody>
        <tfoot>
          <tr><th>Vendor Total SF</th>{''.join(vendor_total_sf_cells)}</tr>
          <tr><th>Added SF</th>{''.join(vendor_added_sf_cells)}</tr>
          <tr><th>Freight</th>{''.join(vendor_freight_cells)}</tr>
          <tr><th>Landed Total</th>{''.join(vendor_landed_total_cells)}</tr>
        </tfoot>
      </table>
      <div class="niko-opt-legend">{''.join(legend_parts)}</div>
      {omitted_text}
    </div>
    """


def _render_collection_details_html(details: list) -> str:
    """
    Render collection shipping / pallet details as a row of info cards.
    Each card shows the sub-collection name, carton/pallet specs, and lead times.
    """
    if not details:
        return ""

    cards = []
    for d in details:
        # Format buyer/description line — collapse newlines to <br>
        name_lines = [ln.strip() for ln in d["name"].split("\n") if ln.strip()]
        name_html = "<br>".join(name_lines)

        # Carton/pallet spec lines
        header_lines = [ln.strip() for ln in d["header"].split("\n") if ln.strip()]
        header_html = "<br>".join(header_lines)

        # Lead-time / shipping lines
        shipping_lines = [ln.strip() for ln in d["shipping"].split("\n") if ln.strip()]
        shipping_html = "<br>".join(shipping_lines)

        cards.append(
            f'<div class="cd-card">'
            f'<div class="cd-title">{d["collection"]}</div>'
            f'<div class="cd-desc">{name_html}</div>'
            f'<div class="cd-row"><span class="cd-label">CARTON / PALLET</span><span class="cd-value">{header_html}</span></div>'
            f'<div class="cd-row"><span class="cd-label">LEAD TIMES</span><span class="cd-value">{shipping_html}</span></div>'
            f'</div>'
        )

    return (
"<style>"
".cd-wrap{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:18px;}"
".cd-card{flex:1;min-width:260px;background:#ffffff;border:1px solid #e2e8f0;"
"border-left:4px solid #1e3a5f;border-radius:12px;padding:14px 16px;"
"box-shadow:0 1px 4px rgba(0,0,0,0.05);font-family:'Manrope',sans-serif;}"
".cd-title{font-size:0.72rem;font-weight:800;letter-spacing:0.10em;"
"text-transform:uppercase;color:#1e3a5f;margin-bottom:4px;}"
".cd-desc{font-size:0.78rem;color:#6b7a90;margin-bottom:10px;line-height:1.4;}"
".cd-row{display:flex;flex-direction:column;margin-bottom:8px;}"
".cd-row:last-child{margin-bottom:0;}"
".cd-label{font-size:0.65rem;font-weight:700;letter-spacing:0.09em;"
"text-transform:uppercase;color:#6b7a90;margin-bottom:3px;}"
".cd-value{font-size:0.78rem;color:#1a1a2e;line-height:1.5;}"
"</style>"
f'<div class="cd-wrap">{"".join(cards)}</div>'
    )


def _render_arrivals_table_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return """
        <div class="queue-card">
          <div class="queue-empty">No arrivals found for selection.</div>
        </div>
        """
    headers = "".join(f"<div>{col}</div>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            value = row[col]
            if value is None:
                value = ""
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                value = ""
            if isinstance(value, str) and value.strip().lower() in ("nan", "none"):
                value = ""
            if isinstance(value, (int, float)) and col == "Quantity":
                value = _format_number(float(value), 2)
            cells.append(f"<div class='text-clip'>{value}</div>")
        rows.append(f"<div class='queue-row'>{''.join(cells)}</div>")
    col_weights = {
        "Item#": "0.9fr",
        "Description": "1.6fr",
        "PO#": "0.9fr",
        "Vendor Name": "1.2fr",
        "Quantity": "0.8fr",
        "Container#": "2.6fr",
        "Due in Inventory": "1fr",
        "Reorder Schedule": "1fr",
    }
    col_count = len(df.columns)
    col_template = " ".join(col_weights.get(col, "1fr") for col in df.columns)
    return f"""
    <div class="queue-card">
      <div class="queue-table" style="--col-count:{col_count}; --col-template:{col_template};">
        <div class="queue-row queue-head">{headers}</div>
        {''.join(rows)}
      </div>
    </div>
    """

def _prepare_arrivals_df(
    arrivals_df: pd.DataFrame,
    selected_vendor: str,
    selected_collection: str,
    sku: Optional[str] = None,
    schedule_months: Optional[set] = None,
) -> pd.DataFrame:
    if arrivals_df is None or arrivals_df.empty:
        return pd.DataFrame()
    df = arrivals_df.copy()
    col_po = _get_first_column(df, ["PO_NUMBER", "po_number", "PO#", "PLPO#"])
    col_item = _get_first_column(df, ["ITEM_NUMBER", "item_number", "PLITEM", "SKU"])
    col_desc = _get_first_column(df, ["DESCRIPTION", "description", "PLDESC"])
    col_qty = _get_first_column(df, ["QUANTITY_SF", "quantity_sf", "PLBLUO"])
    col_vendor = _get_first_column(df, ["VENDOR_NAME", "vendor_name", "Vendor"])
    col_collection = _get_first_column(df, ["COLLECTION", "collection"])
    col_due_inv = _get_first_column(df, ["DUE_INV", "due_inv", "PLDDAT"])
    col_container = _get_first_column(df, ["CONTAINER", "container"])
    if col_container is None:
        c1 = _get_first_column(df, ["PTCMT1", "ptcmt1"])
        c2 = _get_first_column(df, ["PTCMT2", "ptcmt2"])
        if c1 or c2:
            c1_vals = df[c1].fillna("").astype(str).str.strip() if c1 else ""
            c2_vals = df[c2].fillna("").astype(str).str.strip() if c2 else ""
            df["CONTAINER"] = np.where(c1_vals != "", c1_vals, c2_vals)
            col_container = "CONTAINER"

    if col_po is None:
        return pd.DataFrame()

    if col_item:
        df[col_item] = df[col_item].fillna("").astype(str).str.strip()
    if col_po:
        df[col_po] = df[col_po].fillna("").astype(str).str.strip()
    if col_item and col_po:
        keep = ~df[col_item].str.lower().isin(["", "nan", "none"])
        keep &= ~df[col_po].str.lower().isin(["", "nan", "none"])
        df = df[keep]

    if selected_vendor != "All Vendors" and col_vendor:
        df = df[df[col_vendor].astype(str).str.strip() == selected_vendor]
    if selected_collection != "All Collections" and col_collection:
        df = df[df[col_collection].astype(str).str.strip() == selected_collection]
    if sku and col_item:
        df = df[df[col_item].astype(str).str.strip() == str(sku)]

    # Include QUANTITY_SF in the dedup key so two distinct PO line items for the
    # same SKU/PO/dates/container (differing only in quantity) are both preserved.
    dedupe_cols = [c for c in [col_po, col_item, col_qty, col_due_inv, col_container] if c]
    if dedupe_cols:
        df = df.drop_duplicates(subset=dedupe_cols)

    container_series = df[col_container].fillna("").astype(str).str.strip() if col_container else ""
    container_series = container_series.replace("nan", "")

    inv_norm = df.apply(_resolve_po_projection_arrival_date, axis=1)

    item_series = df[col_item].fillna("").astype(str).str.strip() if col_item else ""
    desc_series = df[col_desc].fillna("").astype(str).str.strip() if col_desc else ""
    po_series = df[col_po].fillna("").astype(str).str.strip()
    vendor_series = df[col_vendor].fillna("").astype(str).str.strip() if col_vendor else ""
    schedule_status = None
    if schedule_months is not None:
        schedule_months = set(schedule_months)
        schedule_status = []
        for value in inv_norm:
            month_key = _month_key(value)
            if month_key is None:
                schedule_status.append("No Date")
            elif month_key in schedule_months:
                schedule_status.append("In Schedule")
            else:
                schedule_status.append("Outside Window")

    out = pd.DataFrame(
        {
            "Item#": item_series.replace("nan", ""),
            "Description": desc_series.replace("nan", ""),
            "PO#": po_series.replace("nan", ""),
            "Vendor Name": vendor_series.replace("nan", ""),
            "Quantity": df[col_qty] if col_qty else np.nan,
            "Container#": container_series,
            "Due in Inventory": inv_norm.apply(_format_arrival_value),
        }
    )
    if schedule_status is not None:
        out["Reorder Schedule"] = schedule_status
    for col in ["Item#", "Description", "PO#", "Vendor Name", "Container#"]:
        out[col] = out[col].fillna("").astype(str).str.strip()
        out[col] = out[col].replace({"nan": "", "None": ""})
    keep = ~out["Item#"].str.lower().isin(["", "nan", "none"])
    keep &= ~out["PO#"].str.lower().isin(["", "nan", "none"])
    out = out[keep].reset_index(drop=True)
    out["_sort"] = inv_norm.values
    out = out.sort_values("_sort", ascending=False, na_position="last").drop(columns=["_sort"])
    return out.reset_index(drop=True)

def _projection_schedule_months(
    monthly_rows: List[Dict],
    sku: Optional[str] = None,
) -> set:
    months = set()
    sku_norm = str(sku).strip() if sku else ""
    for row in monthly_rows or []:
        row_type = str(row.get("Row_Type", "")).upper()
        if row_type == "HIST":
            continue
        if sku_norm:
            row_sku = str(row.get("SKU", row.get("sku", ""))).strip()
            if row_sku != sku_norm:
                continue
        month_key = _month_key(row.get("Month"))
        if month_key:
            months.add(month_key)
    return months

def _render_metric_card_html(title: str, value: str, subtitle: Optional[str] = None) -> str:
    subtitle_html = f"<div class='metric-sub'>{subtitle}</div>" if subtitle else ""
    return f"""
    <div class="metric-card">
      <div class="metric-title">{title}</div>
      <div class="metric-value">{value}</div>
      {subtitle_html}
    </div>
    """


def _render_transfer_report_card(df: pd.DataFrame) -> None:
    """Render the Carlos Transfer Report table and Excel export."""
    if df.empty:
        st.info("No transfer needs found for locations 3, 4, 5, 6, 8, or 9.")
        return

    display_cols = [
        "Item Number",
        "Description",
        "Vendor Number",
        "Loc",
        "Loc Available",
        "On PO/TFR",
        "EMU",
        "Months Coverage",
        "Need to TFR",
        "Total Qty TFR",
        "Loc 1 Available",
        "Loc 1 to Order",
        "Notes",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    def _num(value: Any) -> float:
        try:
            v = float(value)
            return 0.0 if math.isnan(v) or math.isinf(v) else v
        except Exception:
            return 0.0

    total_tfr = int(round(sum(_num(v) for v in df.get("Need to TFR", pd.Series(dtype=float)))))
    item_count = int(df.get("Item Number", pd.Series(dtype=str)).astype(str).nunique())
    rows_count = len(df)
    loc1_order_items = int(
        df[df.get("Loc 1 to Order", pd.Series(dtype=float)).apply(_num) > 0]
        .get("Item Number", pd.Series(dtype=str))
        .astype(str)
        .nunique()
    )
    notes_series = df.get("Notes", pd.Series(dtype=str)).astype(str)
    capped_rows = int(notes_series.str.contains("Transfer capped", case=False, na=False).sum())
    low_coverage_rows = int(notes_series.str.contains("Below .* months coverage", case=False, na=False, regex=True).sum())

    summary_parts = [
        ("Rows", f"{rows_count:,}"),
        ("Items", f"{item_count:,}"),
        ("Total TFR", f"{total_tfr:,}"),
        ("Loc 1 PO Items", f"{loc1_order_items:,}"),
        ("Capped Rows", f"{capped_rows:,}"),
        ("Low Coverage Rows", f"{low_coverage_rows:,}"),
    ]
    summary_html = "".join(
        "<span style='margin-right:18px;font-size:0.8rem;'>"
        f"<strong>{html.escape(label)}</strong>: {html.escape(value)}</span>"
        for label, value in summary_parts
    )
    st.markdown(
        f"<div style='background:#f1f5fb;padding:8px 12px;border-radius:6px;"
        f"margin-bottom:10px;'>{summary_html}</div>",
        unsafe_allow_html=True,
    )

    shade_map = {
        "Loc": 0,
        "Loc Available": 0,
        "On PO/TFR": 0,
        "EMU": 0,
        "Months Coverage": 0,
        "Need to TFR": 1,
        "Total Qty TFR": 1,
        "Loc 1 Available": 1,
        "Loc 1 to Order": 1,
        "Notes": 2,
    }
    shade_body = ["#eff6ff", "#dbeafe", "#fff7ed"]
    shade_hdr = ["#1e3a5f", "#1e40af", "#92400e"]

    th_base = "padding:4px 8px;white-space:nowrap;color:#fff;font-size:0.7rem;text-align:center;"
    header_html = ""
    for col in display_cols:
        s = shade_map.get(col, 0)
        header_html += f"<th style='{th_base}background:{shade_hdr[s]};border-bottom:2px solid {shade_hdr[s]};'>{html.escape(col)}</th>"

    int_cols = {
        "Loc Available",
        "On PO/TFR",
        "Need to TFR",
        "Total Qty TFR",
        "Loc 1 Available",
        "Loc 1 to Order",
    }

    def _fmt_cell(col: str, value: Any) -> str:
        if col in ("EMU", "Months Coverage"):
            return html.escape(f"{_num(value):,.1f}")
        if col in int_cols:
            return html.escape(f"{int(round(_num(value))):,}")
        return html.escape("" if value is None else str(value))

    body_html = ""
    for i, (_, row) in enumerate(df[display_cols].iterrows()):
        row_bg = "#f8fafc" if i % 2 == 0 else "#ffffff"
        cells = ""
        for col in display_cols:
            val = row[col]
            s = shade_map.get(col, -1)
            bg = shade_body[s] if s >= 0 else row_bg
            td = f"padding:3px 8px;font-size:0.74rem;white-space:nowrap;background:{bg};text-align:center;"
            if col in ("Item Number", "Description", "Notes"):
                td = td.replace("text-align:center;", "text-align:left;")
            if col == "Description":
                td += "min-width:220px;max-width:340px;white-space:normal;"
            if col == "Notes":
                td += "min-width:260px;max-width:420px;white-space:normal;"
                if str(val).strip():
                    td += "color:#92400e;font-weight:600;"
            if col == "Need to TFR" and _num(val) > 0:
                td += "color:#1565c0;font-weight:700;"
            if col == "Loc 1 to Order" and _num(val) > 0:
                td += "color:#b91c1c;font-weight:700;"
            cells += f"<td style='{td}'>{_fmt_cell(col, val)}</td>"
        body_html += f"<tr>{cells}</tr>"

    st.markdown(
        f"<div style='overflow:auto;max-height:650px;border:1px solid #e2e8f0;border-radius:6px;'>"
        f"<table style='border-collapse:collapse;width:100%;'>"
        f"<thead style='position:sticky;top:0;z-index:1;'><tr>{header_html}</tr></thead>"
        f"<tbody>{body_html}</tbody>"
        f"</table></div>",
        unsafe_allow_html=True,
    )

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        file_date = pd.Timestamp.today().strftime("%Y-%m-%d")
        wb = Workbook()
        ws = wb.active
        ws.title = "Transfer Report"
        ws.append(display_cols)

        hdr_fills = [
            PatternFill("solid", fgColor="1E3A5F"),
            PatternFill("solid", fgColor="1E40AF"),
            PatternFill("solid", fgColor="92400E"),
        ]
        body_fills = [
            PatternFill("solid", fgColor="EFF6FF"),
            PatternFill("solid", fgColor="DBEAFE"),
            PatternFill("solid", fgColor="FFF7ED"),
        ]
        row_fill_a = PatternFill("solid", fgColor="F8FAFC")
        row_fill_b = PatternFill("solid", fgColor="FFFFFF")
        hdr_font = Font(name="Calibri", bold=True, color="FFFFFF", size=9)
        body_font = Font(name="Calibri", color="1A1A2E", size=9)
        tfr_font = Font(name="Calibri", bold=True, color="1565C0", size=9)
        order_font = Font(name="Calibri", bold=True, color="B91C1C", size=9)
        note_font = Font(name="Calibri", bold=True, color="92400E", size=9)
        center = Alignment(horizontal="center", vertical="center", wrap_text=False)
        left = Alignment(horizontal="left", vertical="center", wrap_text=False)
        wrap_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        border = Border(bottom=Side(style="thin", color="D1D5DB"))

        for j, col in enumerate(display_cols, start=1):
            cell = ws.cell(row=1, column=j)
            s = shade_map.get(col, 0)
            cell.fill = hdr_fills[s]
            cell.font = hdr_font
            cell.alignment = center
            cell.border = border

        for _, row in df[display_cols].iterrows():
            ws.append([
                float(row[col]) if col in int_cols or col in ("EMU", "Months Coverage") else row[col]
                for col in display_cols
            ])
            data_row_idx = ws.max_row
            base_fill = row_fill_a if data_row_idx % 2 == 0 else row_fill_b
            for j, col in enumerate(display_cols, start=1):
                cell = ws.cell(row=data_row_idx, column=j)
                s = shade_map.get(col, -1)
                cell.fill = body_fills[s] if s >= 0 else base_fill
                cell.font = body_font
                cell.alignment = center
                cell.border = border
                if col in ("Item Number", "Description"):
                    cell.alignment = left
                if col == "Description":
                    cell.alignment = wrap_left
                if col == "Notes":
                    cell.alignment = wrap_left
                    if str(cell.value or "").strip():
                        cell.font = note_font
                if col == "Need to TFR" and _num(cell.value) > 0:
                    cell.font = tfr_font
                if col == "Loc 1 to Order" and _num(cell.value) > 0:
                    cell.font = order_font
                if col in int_cols:
                    cell.number_format = '#,##0'
                if col in ("EMU", "Months Coverage"):
                    cell.number_format = '#,##0.0'

        widths = {
            "Item Number": 16,
            "Description": 34,
            "Vendor Number": 13,
            "Loc": 7,
            "Loc Available": 13,
            "On PO/TFR": 12,
            "EMU": 9,
            "Months Coverage": 15,
            "Need to TFR": 12,
            "Total Qty TFR": 13,
            "Loc 1 Available": 14,
            "Loc 1 to Order": 13,
            "Notes": 44,
        }
        for j, col in enumerate(display_cols, start=1):
            ws.column_dimensions[get_column_letter(j)].width = widths.get(col, 12)
        ws.freeze_panes = "A2"

        xl_buf = io.BytesIO()
        wb.save(xl_buf)
        xl_buf.seek(0)
        st.download_button(
            label="Download Sheet",
            data=xl_buf,
            file_name=f"Transfer_Report_{file_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="transfer_report_xlsx",
        )
    except Exception as _xl_err:
        st.warning(f"Excel export unavailable: {_xl_err}")


def _render_3m_ordering_card(df: pd.DataFrame, loc_order_values: dict, bundled_locs: set) -> None:
    """Render the 3M Ordering Processor table, location summary, and CSV export."""
    BRANCH_LOCS   = _3M_BRANCH_LOCS
    MIN_ORDER     = _3M_MIN_ORDER_VALUE
    direct_locs   = {l for l in BRANCH_LOCS if loc_order_values.get(l, 0) >= MIN_ORDER}

    if df.empty:
        st.info("No 3M items require ordering at this time.")
        return

    export_button_cols = st.columns(2)
    excel_download_slot = export_button_cols[0]
    csv_download_slot = export_button_cols[1]

    # ── Location order value summary ────────────────────────────────────────
    summary_html = ""
    for loc in BRANCH_LOCS:
        val = loc_order_values.get(loc, 0.0)
        if val <= 0:
            continue
        if loc in direct_locs:
            badge = "<span style='color:#1e6b3a;font-weight:700;'>direct ship</span>"
        else:
            badge = "<span style='color:#d97706;font-weight:700;'>→ bundle Loc 1</span>"
        summary_html += (
            f"<span style='margin-right:18px;font-size:0.8rem;'>"
            f"<strong>Loc {loc}</strong>: ${val:,.2f} ({badge})</span>"
        )
    if summary_html:
        st.markdown(
            f"<div style='background:#f1f5fb;padding:8px 12px;border-radius:6px;"
            f"margin-bottom:10px;'>{summary_html}</div>",
            unsafe_allow_html=True,
        )

    # ── Table ────────────────────────────────────────────────────────────────
    display_cols = ["Item Number", "Description", "Min Order Qty"]
    for loc in [1] + BRANCH_LOCS:
        display_cols += [f"Loc {loc} Available",
                         f"Loc {loc} Sales Last Mo",
                         f"Loc {loc} Avg Mo (3mo)",
                         f"Loc {loc} On PO",
                         f"Loc {loc} To Order"]
        if loc in BRANCH_LOCS:
            display_cols.append(f"Loc {loc} Transfer")
    display_cols.append("Status")
    display_cols = [c for c in display_cols if c in df.columns]

    # Location column-group shading — alternates between two blue tones so each
    # location's block of columns is visually distinct.
    _SHADE_BODY_CSS = ["#eff6ff", "#dbeafe"]   # blue-50 / blue-100
    _SHADE_HDR_CSS  = ["#1e3a5f", "#1e40af"]   # dark navy / blue-800
    _shade_map: dict = {}
    for _si, _sloc in enumerate([1] + BRANCH_LOCS):
        _s = _si % 2
        for _sfx in ["Available", "Sales Last Mo", "Avg Mo (3mo)", "On PO", "To Order"]:
            _shade_map[f"Loc {_sloc} {_sfx}"] = _s
        if _sloc in BRANCH_LOCS:
            _shade_map[f"Loc {_sloc} Transfer"] = _s

    TH_BASE = "padding:4px 8px;white-space:nowrap;color:#fff;font-size:0.7rem;text-align:center;"
    header_html = ""
    for _c in display_cols:
        _s   = _shade_map.get(_c, 0)
        _hbg = _SHADE_HDR_CSS[_s]
        header_html += f"<th style='{TH_BASE}background:{_hbg};border-bottom:2px solid {_hbg};'>{_c}</th>"

    body_html = ""
    for i, (_, row) in enumerate(df[display_cols].iterrows()):
        _row_bg = "#f8fafc" if i % 2 == 0 else "#ffffff"
        cells = ""
        for col in display_cols:
            val = row[col]
            _s  = _shade_map.get(col, -1)
            _bg = _SHADE_BODY_CSS[_s] if _s >= 0 else _row_bg
            td  = f"padding:3px 8px;font-size:0.74rem;white-space:nowrap;background:{_bg};text-align:center;"
            if "To Order" in col and isinstance(val, (int, float)):
                if val < 0:
                    td += "color:#d97706;font-style:italic;"
                    display_val = f"({abs(int(val))})"
                elif val > 0:
                    td += "color:#1e6b3a;font-weight:600;"
                    display_val = str(int(val))
                else:
                    display_val = "0"
            elif "Transfer" in col and isinstance(val, (int, float)):
                if val < 0:
                    td += "color:#d97706;font-style:italic;"
                    display_val = f"({abs(int(val))})"
                elif val > 0:
                    td += "color:#1565c0;font-weight:600;"
                    display_val = f"+{int(val)}"
                else:
                    display_val = "—"
            elif col in ("Item Number", "Description", "Status"):
                td = td.replace("text-align:center;", "text-align:left;")
                display_val = str(val)
            else:
                display_val = str(val)
            cells += f"<td style='{td}'>{display_val}</td>"
        body_html += f"<tr>{cells}</tr>"

    st.markdown(
        f"<div style='overflow-x:auto;'>"
        f"<table style='border-collapse:collapse;width:100%;'>"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{body_html}</tbody>"
        f"</table></div>",
        unsafe_allow_html=True,
    )

    # ── CSV export — one file per location, bundled into a ZIP ───────────────
    ALL_EXPORT_LOCS = [1] + BRANCH_LOCS
    file_date = pd.Timestamp.today().strftime("%Y-%m-%d")
    zip_buf = io.BytesIO()
    files_written = 0
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for loc in ALL_EXPORT_LOCS:
            col = f"Loc {loc} To Order"
            if col not in df.columns:
                continue
            loc_lines = []
            for _, row in df.iterrows():
                qty  = int(row.get(col, 0))
                part = str(row.get("_vendor_part", "")).strip()
                if qty > 0 and part:
                    loc_lines.append(f"{qty},{part}")
            if loc_lines:
                zf.writestr(
                    f"ROQ_Loc_{loc}_{file_date}.csv",
                    "\n".join(loc_lines),
                )
                files_written += 1

    if files_written:
        zip_buf.seek(0)
        with csv_download_slot:
            st.download_button(
                label="Export to CSV",
                data=zip_buf,
                file_name=f"3M_ROQ_{file_date}.zip",
                mime="application/zip",
                key="3m_order_csv",
            )

    # ── Excel download — matches webapp display exactly ──────────────────────
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (
            PatternFill, Font, Alignment, Border, Side, numbers
        )
        from openpyxl.utils import get_column_letter

        wb  = Workbook()
        ws  = wb.active
        ws.title = f"3M Order {file_date}"

        # ── colour palette (mirrors the HTML renderer) ───────────────────────
        HDR_FILL    = PatternFill("solid", fgColor="1E3A5F")
        ROW_FILL_A  = PatternFill("solid", fgColor="F8FAFC")
        ROW_FILL_B  = PatternFill("solid", fgColor="FFFFFF")
        SHADE_HDR_FILL  = [PatternFill("solid", fgColor="1E3A5F"),
                           PatternFill("solid", fgColor="1E40AF")]
        SHADE_BODY_FILL = [PatternFill("solid", fgColor="EFF6FF"),
                           PatternFill("solid", fgColor="DBEAFE")]
        POS_FILL    = PatternFill("solid", fgColor="E8F5E9")  # light green tint
        NEG_FILL    = PatternFill("solid", fgColor="FFF8E1")  # light amber tint
        HDR_FONT    = Font(name="Calibri", bold=True,  color="FFFFFF", size=9)
        BODY_FONT   = Font(name="Calibri", bold=False, color="1A1A2E", size=9)
        POS_FONT    = Font(name="Calibri", bold=True,  color="1E6B3A", size=9)
        NEG_FONT    = Font(name="Calibri", bold=False, color="D97706", size=9, italic=True)
        CENTER      = Alignment(horizontal="center", vertical="center", wrap_text=False)
        LEFT        = Alignment(horizontal="left",   vertical="center", wrap_text=False)
        THIN        = Side(style="thin", color="D1D5DB")
        BORDER      = Border(bottom=THIN)

        # ── location summary row (mirrors the blue summary bar) ───────────────
        summary_parts = []
        for loc in BRANCH_LOCS:
            val = loc_order_values.get(loc, 0.0)
            if val <= 0:
                continue
            tag = "direct ship" if loc not in bundled_locs else "→ bundle Loc 1"
            summary_parts.append(f"Loc {loc}: ${val:,.2f} ({tag})")
        if summary_parts:
            ws.append(["Location order values:"] + summary_parts)
            for cell in ws[1]:
                cell.font      = Font(name="Calibri", bold=True, color="1E3A5F", size=9)
                cell.alignment = LEFT
            ws.row_dimensions[1].height = 16
            ws.append([])  # blank separator row

        # ── header row ────────────────────────────────────────────────────────
        hdr_row_idx = ws.max_row + 1
        ws.append(display_cols)
        for _j_h, cell in enumerate(ws[hdr_row_idx]):
            _col_h = display_cols[_j_h] if _j_h < len(display_cols) else ""
            _s_h   = _shade_map.get(_col_h, 0)
            cell.fill      = SHADE_HDR_FILL[_s_h]
            cell.font      = HDR_FONT
            cell.alignment = CENTER
            cell.border    = BORDER
        ws.row_dimensions[hdr_row_idx].height = 18

        # ── data rows ────────────────────────────────────────────────────────
        for i, (_, row) in enumerate(df[display_cols].iterrows()):
            row_vals = []
            for col in display_cols:
                val = row[col]
                if ("To Order" in col or "Transfer" in col) and isinstance(val, (int, float)):
                    row_vals.append(int(val))
                else:
                    row_vals.append(val)
            ws.append(row_vals)

            data_row_idx = ws.max_row
            _row_fill    = ROW_FILL_A if i % 2 == 0 else ROW_FILL_B

            for j, col in enumerate(display_cols, start=1):
                cell      = ws.cell(row=data_row_idx, column=j)
                raw_val   = row[col]
                _s_xl     = _shade_map.get(col, -1)
                base_fill = SHADE_BODY_FILL[_s_xl] if _s_xl >= 0 else _row_fill

                if "To Order" in col and isinstance(raw_val, (int, float)):
                    v = int(raw_val)
                    if v < 0:
                        cell.value         = v
                        cell.font          = NEG_FONT
                        cell.fill          = NEG_FILL
                        cell.number_format = '"("0")"'
                    elif v > 0:
                        cell.value = v
                        cell.font  = POS_FONT
                        cell.fill  = POS_FILL
                    else:
                        cell.font  = BODY_FONT
                        cell.fill  = base_fill
                    cell.alignment = CENTER
                elif "Transfer" in col and isinstance(raw_val, (int, float)):
                    v = int(raw_val)
                    if v < 0:
                        cell.value         = v
                        cell.font          = NEG_FONT
                        cell.fill          = NEG_FILL
                        cell.number_format = '"("0")"'
                    elif v > 0:
                        cell.value = v
                        cell.font  = Font(name="Calibri", bold=True, color="1565C0", size=9)
                        cell.fill  = PatternFill("solid", fgColor="E3F2FD")
                    else:
                        cell.value = "—"
                        cell.font  = BODY_FONT
                        cell.fill  = base_fill
                    cell.alignment = CENTER
                elif col in ("Item Number", "Description", "Status"):
                    cell.font      = BODY_FONT
                    cell.fill      = base_fill
                    cell.alignment = LEFT
                else:
                    cell.font      = BODY_FONT
                    cell.fill      = base_fill
                    cell.alignment = CENTER
                cell.border = BORDER
            ws.row_dimensions[data_row_idx].height = 15

        # ── column widths ─────────────────────────────────────────────────────
        col_widths = {
            "Item Number": 14, "Description": 30, "Status": 22,
        }
        default_w = 13
        for j, col in enumerate(display_cols, start=1):
            w = col_widths.get(col, 11 if "Transfer" in col else default_w)
            ws.column_dimensions[get_column_letter(j)].width = w

        # ── freeze header pane ────────────────────────────────────────────────
        ws.freeze_panes = ws.cell(row=hdr_row_idx + 1, column=3)

        xl_buf = io.BytesIO()
        wb.save(xl_buf)
        xl_buf.seek(0)
        with excel_download_slot:
            st.download_button(
                label="Download Sheet",
                data=xl_buf,
                file_name=f"3M_Order_{file_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="3m_order_xlsx",
            )
    except Exception as _xl_err:
        with excel_download_slot:
            st.warning(f"Excel export unavailable: {_xl_err}")


def _render_segmentation_table_html(segmentation: Dict) -> str:
    rows = [
        ("maturity", segmentation.get("maturity", "")),
        ("demand_pattern", segmentation.get("demand_pattern", "")),
        ("ADI", _format_number(float(segmentation.get("adi", 0.0)), 6)),
        ("CV2", _format_number(float(segmentation.get("cv2", 0.0)), 6)),
        ("first_sales_date", segmentation.get("first_sales_date") or "--"),
        ("history_days", str(segmentation.get("history_days", 0))),
    ]
    body = "".join(
        f"<div class='seg-row'><div>{label}</div><div>{value}</div></div>" for label, value in rows
    )
    return f"""
    <div class="seg-card">
      <div class="seg-title">SEGMENTATION</div>
      {body}
    </div>
    """

def _aggregate_items(items: List[Dict]) -> Dict:
    if not items:
        return {}
    total_available = sum(float(item.get("available", 0.0) or 0.0) for item in items)
    total_on_po = sum(float(item.get("on_po", 0.0) or 0.0) for item in items)
    total_backorder = sum(float(item.get("backorder", 0.0) or 0.0) for item in items)
    total_inv = sum(float(item.get("inventory_position", 0.0) or 0.0) for item in items)
    total_lt_demand = sum(float(item.get("lt_demand", 0.0) or 0.0) for item in items)
    avg_daily = sum(float(item.get("daily_avg_demand", 0.0) or 0.0) for item in items)
    days_cover = total_inv / avg_daily if avg_daily > 0 else 0.0
    lead_time_days = int(round(np.mean([item.get("lead_time_days", 0) or 0 for item in items])))
    return {
        "available": total_available,
        "on_po": total_on_po,
        "backorder": total_backorder,
        "inventory_position": total_inv,
        "lt_demand": total_lt_demand,
        "daily_avg_demand": avg_daily,
        "days_of_cover": days_cover,
        "lead_time_days": lead_time_days,
        "vendor_number": items[0].get("vendor_number", "--"),
        "vendor_name": items[0].get("vendor_name", "--"),
    }

def _render_demand_graph_html(fig: go.Figure) -> str:
    fig_html = fig.to_html(include_plotlyjs=False, full_html=False, config={"displayModeBar": False})
    return f"""
    <html>
      <head>
        <script src="https://cdn.plot.ly/plotly-2.30.0.min.js"></script>
        <style>
          :root {{
            --panel-strong: #f1f5fb;
            --panel: #ffffff;
            --border: #e2e8f0;
            --text: #1a1a2e;
          }}
          body {{
            margin: 0;
            background: transparent;
            color: var(--text);
            font-family: "Manrope", sans-serif;
          }}
          .demand-card {{
            background: transparent;
            border-radius: 16px;
            overflow: hidden;
            border: 1px solid var(--border);
          }}
          .demand-body {{
            background: var(--panel);
            padding: 12px 16px 8px 16px;
          }}
          .demand-body .plotly-graph-div {{
            margin: 0 !important;
          }}
          /* Scrollbar styling */
          .demand-body::-webkit-scrollbar {{
            width: 8px;
          }}
          .demand-body::-webkit-scrollbar-track {{
            background: #f1f5fb;
            border-radius: 4px;
          }}
          .demand-body::-webkit-scrollbar-thumb {{
            background: #c7d2fe;
            border-radius: 4px;
          }}
          .demand-body::-webkit-scrollbar-thumb:hover {{
            background: #a5b4fc;
          }}
        </style>
      </head>
      <body>
        <div class="demand-card">
          <div class="demand-body">
            {fig_html}
          </div>
        </div>
      </body>
    </html>
    """

def _series_from_monthly_rows(rows: List[Dict], sku: str) -> Dict:
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    if df.empty or "SKU" not in df.columns or "Month" not in df.columns:
        return {}
    df = df[df["SKU"].astype(str) == str(sku)]
    if df.empty:
        return {}
    df["Month"] = pd.to_datetime(df["Month"], errors="coerce")
    df = df[df["Month"].notna()]
    if df.empty:
        return {}
    row_type_col = "Row_Type" if "Row_Type" in df.columns else None
    hist = df[df[row_type_col].astype(str).str.upper().eq("HIST")] if row_type_col else df[df["Historical Demand"].notna()]
    fc = df[df[row_type_col].astype(str).str.upper().isin(["FCST", "CATCHUP"])] if row_type_col else df[df["Forecast"].notna()]
    hist = hist.sort_values("Month")
    fc = fc.sort_values("Month")
    series: Dict[str, List[float]] = {}
    if not hist.empty and "Historical Demand" in hist.columns:
        series["hist_x"] = [d.strftime("%Y-%m-%d") for d in hist["Month"]]
        series["hist_y"] = [float(v) for v in hist["Historical Demand"]]
    if not fc.empty and "Forecast" in fc.columns:
        fc_series = fc.groupby("Month")["Forecast"].sum().sort_index()
        fc_series = _filter_forecast_months(fc_series)
        series["fc_x"] = [d.strftime("%Y-%m-%d") for d in fc_series.index]
        series["fc_y"] = [float(v) for v in fc_series.values]
        series["x"] = series["fc_x"]
        series["y"] = series["fc_y"]
    return series

def _series_from_monthly_rows_vendor(rows: List[Dict], vendor: str, key: str = "vendor_name") -> Dict:
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    if "Month" not in df.columns:
        return {}
    # If key is None, aggregate all rows (no filtering)
    if key is not None:
        vendor_col = key if key in df.columns else ("vendor_name" if "vendor_name" in df.columns else "collection")
        if df.empty or vendor_col not in df.columns:
            return {}
        df = df[df[vendor_col].astype(str) == str(vendor)]
        if df.empty:
            return {}
    df["Month"] = pd.to_datetime(df["Month"], errors="coerce")
    df = df[df["Month"].notna()]
    if df.empty:
        return {}
    row_type_col = "Row_Type" if "Row_Type" in df.columns else None
    hist = df[df[row_type_col].astype(str).str.upper().eq("HIST")] if row_type_col else df[df["Historical Demand"].notna()]
    fc = df[df[row_type_col].astype(str).str.upper().isin(["FCST", "CATCHUP"])] if row_type_col else df[df["Forecast"].notna()]
    hist = hist.sort_values("Month")
    fc = fc.sort_values("Month")
    series: Dict[str, List[float]] = {}
    if not hist.empty and "Historical Demand" in hist.columns:
        hist_series = hist.groupby("Month")["Historical Demand"].sum().sort_index()
        series["hist_x"] = [d.strftime("%Y-%m-%d") for d in hist_series.index]
        series["hist_y"] = [float(v) for v in hist_series.values]
    if not fc.empty and "Forecast" in fc.columns:
        fc_series = fc.groupby("Month")["Forecast"].sum().sort_index()
        fc_series = _filter_forecast_months(fc_series)
        series["fc_x"] = [d.strftime("%Y-%m-%d") for d in fc_series.index]
        series["fc_y"] = [float(v) for v in fc_series.values]
        series["x"] = series["fc_x"]
        series["y"] = series["fc_y"]
    return series

def _build_spending_chart(spending: Dict) -> go.Figure:
    rng = np.random.default_rng(7)
    x_vals = spending.get("x") or [str(d) for d in range(1, 31)]
    this_month = spending.get("this") or []
    last_month = spending.get("last") or []

    if not this_month or not last_month:
        base = np.linspace(500, 16000, len(x_vals))
        noise = rng.normal(0, 260, len(x_vals)).cumsum()
        last_month = np.clip(base + noise, 0, None)
        this_month = np.clip(last_month * 0.68 + rng.normal(0, 220, len(x_vals)), 0, None)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x_vals,
            y=this_month,
            mode="lines",
            name="This month",
            line=dict(color="#f29a3a", width=4),
            fill="tozeroy",
            fillcolor="rgba(242,154,58,0.25)",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_vals,
            y=last_month,
            mode="lines",
            name="Last month",
            line=dict(color="#dfe8c6", width=4),
        )
    )
    title_text = spending.get("title", "Spending")
    subtitle = spending.get("subtitle", "This month vs. last month")
    fig.update_layout(
        title={
            "text": f"{title_text}<br><span style='font-size:0.8rem;color:rgba(241,244,228,0.75)'>{subtitle}</span>",
            "x": 0.02,
            "y": 0.94,
        },
        margin=dict(l=40, r=20, t=60, b=30),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="rgba(241,244,228,0.9)", family="Palatino Linotype, Book Antiqua, Palatino, serif"),
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0.2, font=dict(size=12)),
        showlegend=True,
        height=320,
    )
    show_ticks = bool(x_vals) and isinstance(x_vals[0], str)
    fig.update_xaxes(showgrid=False, ticks="", showticklabels=show_ticks)
    fig.update_yaxes(
        showgrid=True,
        gridcolor="rgba(255,255,255,0.18)",
        zeroline=False,
        ticks="",
        tickprefix="$",
        tickformat=",.0f",
    )
    return fig

def _build_cashflow_chart(cashflow: Dict) -> go.Figure:
    months = cashflow.get("x") or ["Apr '25", "May '25", "Jun '25", "Jul '25"]
    inflow = np.array(cashflow.get("inflow") or [12000, 15000, 18000, 9000])
    outflow = np.array(cashflow.get("outflow") or [-17000, -16000, -14000, -9000])
    net = cashflow.get("net")
    if not net:
        net = (inflow + outflow).tolist()

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=months,
            y=inflow,
            name="Inflow",
            marker_color="#3f7e57",
            opacity=0.9,
        )
    )
    fig.add_trace(
        go.Bar(
            x=months,
            y=outflow,
            name="Outflow",
            marker_color="#b94f45",
            opacity=0.9,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=months,
            y=net,
            name="Net",
            mode="lines+markers",
            line=dict(color="#e6f0b1", width=3),
            marker=dict(size=6),
        )
    )
    fig.update_layout(
        title={"text": cashflow.get("title", "Cash flow trends"), "x": 0.04, "y": 0.95},
        margin=dict(l=40, r=20, t=60, b=30),
        barmode="relative",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="rgba(241,244,228,0.9)", family="Palatino Linotype, Book Antiqua, Palatino, serif"),
        showlegend=False,
        height=320,
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="rgba(255,255,255,0.18)",
        zeroline=False,
        tickprefix="$",
        tickformat=",.0f",
    )
    fig.update_xaxes(showgrid=False, ticks="", showline=False)
    return fig

def _build_transactions_html(transactions: List[Dict]) -> str:
    items = "\n".join(
        "<div class='list-row'>"
        f"<div class='list-left'><span class='list-icon'>{item.get('code','--')}</span>{item.get('name','--')}</div>"
        f"<div class='list-right'>{_format_currency(float(item.get('amount', 0.0)), 2)}</div>"
        "</div>"
        for item in transactions
    )
    return f"""
    <div class="card">
      <div class="card-title">Transactions</div>
      <div class="list">{items}</div>
    </div>
    """

def _build_categories_html(categories: List[Dict]) -> str:
    bars = []
    for item in categories:
        label = item.get("label", "--")
        value = _format_currency(float(item.get("value", 0.0)), 2)
        pct = float(item.get("pct", 0.0))
        color = item.get("color", "#5f8a4a")
        bars.append(
            f"""
            <div class="bar-row">
              <div class="bar-label">{label}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width:{pct*100:.0f}%;background:{color};"></div>
              </div>
              <div class="bar-value">{value}</div>
            </div>
            """
        )
    return f"""
    <div class="card">
      <div class="card-title">Top expense categories</div>
      {''.join(bars)}
    </div>
    """

def _build_grocery_card_html(title: str, budget: float, actual: float, remaining: float, progress: float) -> str:
    progress_pct = max(0.0, min(1.0, progress))
    return f"""
    <div class="card">
      <div class="card-header-row">
        <div class="card-title">{title}</div>
        <div class="ring" style="--value:{progress_pct};"></div>
      </div>
      <div class="metric-grid">
        <div class="metric-block">
          <div class="metric-label">Budget</div>
          <div class="metric-value">{_format_currency(budget, 0)}</div>
        </div>
        <div class="metric-block">
          <div class="metric-label">Actual</div>
          <div class="metric-value">{_format_currency(actual, 0)}</div>
        </div>
        <div class="metric-block">
          <div class="metric-label">Remaining</div>
          <div class="metric-value positive">{_format_currency(remaining, 0)}</div>
        </div>
      </div>
    </div>
    """

def render_webapp(data: Optional[Dict] = None) -> None:
    st.set_page_config(page_title="OMP Purchasing Dashboard", layout="wide")
    if data is None:
        data = _load_webapp_payload()

    if "active_view" not in st.session_state:
        st.session_state.active_view = "Ilsy"

    # Get current active view for styling
    current_view = st.session_state.active_view

    # CSS to style the tab buttons - active tab is much darker
    # Use shared styles if available, otherwise fallback to inline
    tab_names = ["Ilsy", "Niko", "Carlos", "Dilan"]
    if get_tab_button_css is not None:
        st.markdown(get_tab_button_css(current_view, tab_names), unsafe_allow_html=True)
    else:
        # Fallback inline CSS if shared styles not available
        st.markdown(f"""
        <style>
        div.row-widget.stButton > button[kind="secondary"] {{
            background-color: #f1f5fb !important;
            color: #374151 !important;
            border: 1px solid #d1d9e6 !important;
        }}
        div.row-widget.stButton > button[kind="secondary"]:hover {{
            background-color: #e4eaf4 !important;
            border: 1px solid #b8c5d6 !important;
        }}
        div[data-testid="stHorizontalBlock"] > div:nth-child(1) button[data-testid="stBaseButton-secondary"] {{
            background-color: {"#1e3a5f" if current_view == "Ilsy" else "#f1f5fb"} !important;
            color: {"#ffffff" if current_view == "Ilsy" else "#374151"} !important;
            {"border: none !important; font-weight: 700 !important;" if current_view == "Ilsy" else ""}
        }}
        div[data-testid="stHorizontalBlock"] > div:nth-child(2) button[data-testid="stBaseButton-secondary"] {{
            background-color: {"#1e3a5f" if current_view == "Niko" else "#f1f5fb"} !important;
            color: {"#ffffff" if current_view == "Niko" else "#374151"} !important;
            {"border: none !important; font-weight: 700 !important;" if current_view == "Niko" else ""}
        }}
        div[data-testid="stHorizontalBlock"] > div:nth-child(3) button[data-testid="stBaseButton-secondary"] {{
            background-color: {"#1e3a5f" if current_view == "Carlos" else "#f1f5fb"} !important;
            color: {"#ffffff" if current_view == "Carlos" else "#374151"} !important;
            {"border: none !important; font-weight: 700 !important;" if current_view == "Carlos" else ""}
        }}
        div[data-testid="stHorizontalBlock"] > div:nth-child(4) button[data-testid="stBaseButton-secondary"] {{
            background-color: {"#1e3a5f" if current_view == "Dilan" else "#f1f5fb"} !important;
            color: {"#ffffff" if current_view == "Dilan" else "#374151"} !important;
            {"border: none !important; font-weight: 700 !important;" if current_view == "Dilan" else ""}
        }}
        </style>
        """, unsafe_allow_html=True)

    # Tab buttons
    tab_cols = st.columns([1, 1, 1, 1], gap="medium")
    with tab_cols[0]:
        if st.button("Ilsy", key="tab_ilsy", use_container_width=True):
            st.session_state.active_view = "Ilsy"
            st.rerun()
    with tab_cols[1]:
        if st.button("Veronica", key="tab_niko", use_container_width=True):
            st.session_state.active_view = "Niko"
            st.rerun()
    with tab_cols[2]:
        if st.button("Carlos", key="tab_carlos", use_container_width=True):
            st.session_state.active_view = "Carlos"
            st.rerun()
    with tab_cols[3]:
        if st.button("Dilan", key="tab_dilan", use_container_width=True):
            st.session_state.active_view = "Dilan"
            st.rerun()

    # Run Forecast button
    current_tab = st.session_state.active_view
    current_tab_display = TAB_DISPLAY_NAMES.get(current_tab, current_tab)
    forecast_btn_col, progress_col = st.columns([1, 3])

    with forecast_btn_col:
        # Determine the forecast type label
        if current_tab in ("Ilsy", "Niko"):
            forecast_label = "Run Flooring Forecast"
        elif current_tab == "Carlos":
            forecast_label = "Run Sundries Forecast"
        elif current_tab == "Dilan":
            forecast_label = "Run Moulding Forecast"
        else:
            forecast_label = "Run Forecast"

        run_forecast_clicked = st.button(
            forecast_label,
            key="run_forecast_btn",
            use_container_width=True,
            help=f"Run the forecast for {current_tab_display} tab and refresh data"
        )

    with progress_col:
        if run_forecast_clicked:
            with st.spinner("Running forecast..."):
                success = _run_forecast_with_progress(current_tab)
            if success:
                st.success("Forecast completed! Refreshing...")
                time.sleep(1)
                st.rerun()

    is_sundries = st.session_state.active_view == "Carlos"
    is_niko = st.session_state.active_view == "Niko"
    is_moulding = st.session_state.active_view == "Dilan"
    if is_sundries:
        data = _load_sundries_payload()
    elif is_niko:
        data = _load_veronica_payload()
    elif is_moulding:
        data = _load_moulding_payload()
    else:
        data = _load_webapp_payload()

    # ── Last-refreshed indicator ───────────────────────────────────────────────
    _gen_at = data.get("generated_at", "")
    if _gen_at:
        try:
            _gen_dt  = datetime.fromisoformat(_gen_at)
            _age_sec = int((datetime.now() - _gen_dt).total_seconds())
            if _age_sec < 3600:
                _age_str = f"{_age_sec // 60}m ago"
            elif _age_sec < 86400:
                _h, _m = divmod(_age_sec // 60, 60)
                _age_str = f"{_h}h {_m}m ago"
            else:
                _age_str = _gen_dt.strftime("%b %d")
            _ts_label  = _gen_dt.strftime("%b %d, %Y at %-I:%M %p") if sys.platform != "win32" \
                         else _gen_dt.strftime("%b %d, %Y at %#I:%M %p")
            _auto_on   = bool(_load_refresh_status())
            _prefix    = "Auto-refreshed" if _auto_on else "Last run"
            _stale     = _age_sec > 10800  # more than 3 hours
            if _stale:
                st.markdown(
                    f"<span style='color:#cc0000;font-weight:700;font-size:0.85rem;'>"
                    f"⚠️ {_prefix}: {_ts_label} ({_age_str})</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.caption(f"{_prefix}: {_ts_label}  ({_age_str})")
        except Exception:
            pass

    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');

        :root {
          --bg1: #f0f2f6;
          --bg2: #e8eaf0;
          --bg3: #dde1ea;
          --panel: #ffffff;
          --panel-strong: #f1f5fb;
          --panel-border: #e2e8f0;
          --text: #1a1a2e;
          --muted: #6b7a90;
          --accent: #1e3a5f;
          --blue: #2f6fdd;
          --shadow: 0 2px 8px rgba(0,0,0,0.06);
          --shadow-soft: 0 1px 4px rgba(0,0,0,0.05);
          --section-header-size: 0.9rem;
        }

        html, body, [class*="css"]  {
          font-family: "Manrope", sans-serif;
          color: var(--text);
        }

        .stApp {
          background: var(--bg1);
        }

        .block-container {
          padding-top: 3rem;
          padding-bottom: 2rem;
        }

        section[data-testid="stSidebar"] {
          background: #ffffff;
          border-right: 1px solid var(--panel-border);
        }

        section[data-testid="stSidebar"] .stMarkdown {
          color: var(--text);
        }

        section[data-testid="stSidebar"] h3 {
          font-size: var(--section-header-size) !important;
        }

        section[data-testid="stSidebar"] label {
          color: var(--text) !important;
        }

        section[data-testid="stSidebar"] .stCheckbox label span {
          color: var(--text) !important;
        }

        section[data-testid="stSidebar"] .stCheckbox label p {
          color: var(--text) !important;
        }

        section[data-testid="stSidebar"] .stCheckbox div[role="checkbox"] + div span {
          color: var(--text) !important;
        }

        .jump-anchor {
          display: block;
          height: 0;
          position: relative;
          top: -84px;
          visibility: hidden;
        }

        section[data-testid="stSidebar"] .sidebar-jump-button {
          display: flex;
          align-items: center;
          justify-content: center;
          width: 100%;
          min-height: 38px;
          margin: 8px 0 18px 0;
          padding: 8px 12px;
          border-radius: 12px;
          border: 1px solid #d1d9e6;
          background: var(--panel-strong);
          color: var(--accent) !important;
          font-weight: 700;
          font-size: 0.84rem;
          text-decoration: none !important;
          box-shadow: var(--shadow-soft);
        }

        section[data-testid="stSidebar"] .sidebar-jump-button:hover {
          background: #e4eaf4;
          border-color: #b8c5d6;
        }

        button[data-testid="collapsedControl"] {
          opacity: 1 !important;
          visibility: visible !important;
          display: inline-flex !important;
        }

        .hero-card {
          background: var(--panel);
          border: 1px solid var(--panel-border);
          border-radius: 18px;
          padding: 18px 20px;
          box-shadow: var(--shadow);
          margin-bottom: 18px;
        }

        .hero-top {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          margin-bottom: 10px;
        }

        .pill {
          display: inline-flex;
          padding: 6px 12px;
          border-radius: 999px;
          border: 1px solid #c7d2fe;
          background: #eef2ff;
          font-size: 0.75rem;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          color: var(--accent);
        }

        .export-btn {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 10px 14px;
          border-radius: 14px;
          border: 1px solid #c7d2fe;
          background: #eef2ff;
          color: var(--accent);
          font-weight: 700;
          font-size: 0.8rem;
          text-decoration: none;
        }

        .hero-title {
          font-size: 1.7rem;
          font-weight: 800;
          margin: 8px 0;
          letter-spacing: 0.02em;
          color: var(--accent);
        }

        .hero-sub {
          color: var(--muted);
          font-size: 0.9rem;
          margin-top: 6px;
        }

        .hero-meta {
          text-align: right;
          color: var(--muted);
          font-size: 0.85rem;
          line-height: 1.35;
          max-width: 360px;
        }

        .build-stamp {
          font-size: 0.72rem;
          letter-spacing: 0.06em;
          color: var(--muted);
          margin-top: 6px;
        }

        .toggle-wrap {
          display: flex;
          gap: 12px;
          margin-bottom: 14px;
        }

        .toggle-wrap--spaced {
          margin-top: 32px;
        }

        .stButton button {
          background: var(--panel-strong) !important;
          color: var(--text) !important;
          border: 1px solid var(--panel-border) !important;
          border-radius: 12px !important;
          font-weight: 600 !important;
        }

        .stButton button:hover {
          background: #e4eaf4 !important;
          border-color: #b8c5d6 !important;
        }

        .stButton button:focus {
          box-shadow: none !important;
        }

        .queue-card {
          background: var(--panel-strong);
          border-radius: 16px;
          padding: 0;
          margin-bottom: 18px;
          border: 1px solid var(--panel-border);
          box-shadow: var(--shadow);
        }

        .demand-body .plotly-html {
          margin: 0;
        }

        .queue-header {
          padding: 10px 16px;
          font-size: var(--section-header-size);
          letter-spacing: 0.12em;
          font-weight: 700;
          color: #ffffff;
          background: var(--accent);
          border-radius: 16px 16px 0 0;
        }

        .reorder-header {
          display: grid;
          grid-template-columns: repeat(var(--col-count, 5), minmax(0, 1fr));
          column-gap: 18px;
          align-items: center;
        }

        .reorder-desc {
          white-space: normal;
        }

        .queue-table {
          padding: 10px 12px 14px 12px;
        }

        .queue-row {
          display: grid;
          grid-template-columns: var(--col-template, repeat(var(--col-count, 5), minmax(0, 1fr)));
          column-gap: 18px;
          row-gap: 6px;
          font-size: 0.82rem;
          padding: 6px 8px;
          border-radius: 10px;
          color: var(--text);
        }

        .queue-head {
          font-weight: 700;
          font-size: 0.74rem;
          letter-spacing: 0.03em;
          color: var(--muted);
          border-bottom: 1px solid var(--panel-border);
          margin-bottom: 6px;
          padding-bottom: 8px;
          text-transform: none;
        }

        .queue-row:not(.queue-head) {
          background: var(--panel-strong);
        }

        .queue-row:not(.queue-head):hover {
          background: #e8edf5;
        }

        /* Purchasing group color coding (Niko / Strip tab) */
        .queue-row.row-group {
          background: #9DC3E6 !important;
          border-left: 3px solid #2E75B6;
          font-weight: 600;
          color: #1F4E79;
        }
        .queue-row.row-group:hover {
          background: #86B4DC !important;
        }
        .queue-row.row-member {
          background: #FFD0D0 !important;
          border-left: 3px solid #C00000;
        }
        .queue-row.row-member:hover {
          background: #FFBBBB !important;
        }
        .queue-row.row-standalone {
          background: #EDF3FB !important;
        }
        .queue-row.row-standalone:hover {
          background: #DCE9F5 !important;
        }

        .queue-empty {
          padding: 12px 16px 16px 16px;
          color: var(--muted);
          font-size: 0.85rem;
        }

        .optimizer-separator {
          height: 2px;
          background: var(--panel-border);
          border-radius: 999px;
          margin: 8px 4px 10px 4px;
        }

        div[data-testid="stVerticalBlock"]:has(.optimizer-anchor) .stMarkdown {
          margin-bottom: 4px;
        }

        div[data-testid="stVerticalBlock"]:has(.optimizer-anchor) input[aria-label="Description"] {
          font-size: 0.72rem;
        }

        div[data-testid="stVerticalBlock"]:has(.optimizer-anchor) div[data-testid="stCheckbox"] label {
          color: var(--text);
          font-size: 0.92rem;
          font-weight: 600;
        }

        div[data-testid="stVerticalBlock"]:has(.optimizer-anchor) div[data-testid="stTextInput"],
        div[data-testid="stVerticalBlock"]:has(.optimizer-anchor) div[data-testid="stNumberInput"],
        div[data-testid="stVerticalBlock"]:has(.optimizer-anchor) div[data-testid="stSelectbox"] {
          margin-bottom: 6px;
        }

        .niko-opt-card {
          overflow-x: auto;
        }

        .niko-opt-summary {
          font-size: 0.82rem;
          color: var(--muted);
          margin-bottom: 8px;
        }

        .niko-opt-policy-note {
          font-size: 0.74rem;
          color: #475569;
          margin: 0 0 10px 2px;
        }

        .niko-opt-sheet-title {
          font-size: 0.8rem;
          font-weight: 700;
          color: #1e3a5f;
          margin: 4px 0 8px 2px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }

        .niko-opt-table {
          width: 100%;
          border-collapse: collapse;
          min-width: 920px;
          table-layout: fixed;
        }

        .niko-opt-table th,
        .niko-opt-table td {
          border: 1px solid #90a4bf;
          padding: 7px 8px;
          text-align: center;
          font-size: 0.82rem;
          color: var(--text);
        }

        .niko-opt-table thead th,
        .niko-opt-table tfoot th,
        .niko-opt-table tfoot td {
          background: #bdd7ee;
          color: #1f4e79;
          font-weight: 700;
        }

        .niko-opt-table tbody th {
          text-align: left;
          background: #f8fafc;
          min-width: 180px;
        }

        .niko-opt-item {
          font-weight: 700;
          color: var(--text);
        }

        .niko-opt-item-sub {
          font-size: 0.72rem;
          color: var(--muted);
          margin-top: 2px;
        }

        .niko-opt-price-cell {
          background: #ffffff;
        }

        .niko-opt-choice-cell {
          font-weight: 700;
          box-shadow: inset 0 0 0 2px #86efac;
        }

        .niko-opt-cell-main {
          color: var(--text);
        }

        .niko-opt-cell-sub {
          font-size: 0.68rem;
          color: #475569;
          margin-top: 3px;
          font-weight: 600;
        }

        .niko-opt-empty {
          color: #94a3b8;
          background: #ffffff;
        }

        .niko-opt-footer-cell {
          font-weight: 700;
        }

        .niko-opt-legend {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin-top: 12px;
        }

        .niko-opt-legend-chip {
          display: inline-flex;
          align-items: center;
          border-radius: 999px;
          padding: 4px 10px;
          font-size: 0.72rem;
          font-weight: 700;
          color: #1f2937;
          border: 1px solid rgba(30, 58, 95, 0.15);
        }


        .text-clip {
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        .metric-stack {
          background: var(--panel);
          border-radius: 18px;
          padding: 14px 16px 12px 16px;
          border: 1px solid var(--panel-border);
          box-shadow: var(--shadow-soft);
        }

        .metric-stack .metric-title {
          margin-bottom: 6px;
        }

        .metric-row {
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          padding: 6px 0;
          border-bottom: 1px solid var(--panel-border);
          font-size: 0.9rem;
        }

        .metric-row:last-child {
          border-bottom: none;
        }

        .metric-row span {
          color: var(--muted);
          font-size: 0.82rem;
        }

        .metric-card {
          background: var(--panel);
          border-radius: 18px;
          padding: 14px 16px 12px 16px;
          border: 1px solid var(--panel-border);
          box-shadow: var(--shadow-soft);
          margin-bottom: 14px;
        }

        .metric-title {
          font-size: 0.72rem;
          letter-spacing: 0.12em;
          color: var(--muted);
          text-transform: uppercase;
        }

        .metric-value {
          font-size: 1.55rem;
          font-weight: 750;
          margin-top: 6px;
          color: var(--text);
        }

        .metric-sub {
          font-size: 0.78rem;
          color: var(--muted);
          margin-top: 6px;
        }

        .seg-card {
          background: var(--panel);
          color: var(--text);
          border-radius: 14px;
          padding: 12px 14px;
          box-shadow: var(--shadow);
          border: 1px solid var(--panel-border);
        }

        .seg-title {
          font-size: 0.75rem;
          font-weight: 800;
          color: var(--accent);
          letter-spacing: 0.1em;
          margin-bottom: 10px;
        }

        .seg-row {
          display: grid;
          grid-template-columns: 1fr 1fr;
          padding: 6px 0;
          border-bottom: 1px solid var(--panel-border);
          font-size: 0.8rem;
        }

        .seg-row:last-child {
          border-bottom: none;
        }

        div[data-testid="stPlotlyChart"] {
          background: transparent;
          border: 0;
          border-radius: 0;
          padding: 12px 0 4px 0;
          box-shadow: none;
          overflow: hidden;
        }

        .demand-header + div[data-testid="stPlotlyChart"] {
          padding-right: 0;
        }

        /* Data editor dropdown text color fix - Glide Data Grid */
        #portal,
        #portal div,
        #portal span,
        .dvn-scroller,
        .dvn-scroller div,
        .dvn-scroller span,
        .click-outside-ignore,
        .click-outside-ignore div,
        .click-outside-ignore span,
        [data-testid="portal"],
        [data-testid="portal"] div,
        [data-testid="portal"] span {
          color: #000000 !important;
        }

        /* Dropdown menu container */
        #portal .dvn-scroller {
          background-color: #ffffff !important;
        }

        /* Individual dropdown options */
        .dvn-scroller > div,
        #portal > div > div {
          color: #000000 !important;
          background-color: #ffffff !important;
        }

        /* Hover state for dropdown options */
        .dvn-scroller > div:hover,
        #portal > div > div:hover {
          background-color: #e0e0e0 !important;
          color: #000000 !important;
        }

        /* Expander styling to match card headers */
        div[data-testid="stExpander"] {
          background: var(--panel) !important;
          border-radius: 18px !important;
          border: 1px solid var(--panel-border) !important;
          box-shadow: var(--shadow-soft) !important;
          margin-bottom: 14px;
        }

        div[data-testid="stExpander"] > details {
          background: var(--panel) !important;
          border: none !important;
          border-radius: 18px !important;
        }

        div[data-testid="stExpander"] summary {
          font-size: 0.72rem;
          letter-spacing: 0.12em;
          color: var(--text) !important;
          text-transform: uppercase;
          font-weight: 700;
          padding: 14px 16px;
          background: var(--panel-strong) !important;
          border-radius: 18px !important;
        }

        div[data-testid="stExpander"] summary:hover {
          background: #e4eaf4 !important;
          color: var(--accent) !important;
        }

        div[data-testid="stExpander"] summary svg {
          fill: var(--muted) !important;
          stroke: var(--muted) !important;
        }

        div[data-testid="stExpander"] > div[data-testid="stExpanderDetails"] {
          padding: 0 16px 14px 16px;
          background: var(--panel) !important;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown("### Item Filters")

    items = data.get("items", []) or _demo_webapp_payload().get("items", [])
    if not items:
        items = _demo_webapp_payload().get("items", [])

    # Apply SKU consolidation to items list
    items = _consolidate_items_list(items)

    vendor_names = sorted({str(item.get("vendor_name", "")).strip() for item in items if item.get("vendor_name")})
    if not vendor_names:
        vendor_names = ["--"]
    vendor_choices = ["All Vendors"] + vendor_names
    selected_vendor = st.sidebar.selectbox("Vendor name", vendor_choices, index=0)

    if selected_vendor == "All Vendors":
        vendor_items = items
    else:
        vendor_items = [item for item in items if str(item.get("vendor_name", "")).strip() == selected_vendor]

    if is_sundries:
        selected_collection = "All Collections"
        collection_items = vendor_items
    else:
        collection_names = sorted({str(item.get("collection", "")).strip() for item in vendor_items if item.get("collection")})
        if not collection_names:
            collection_names = ["--"]
        collection_choices = ["All Collections"] + collection_names
        selected_collection = st.sidebar.selectbox("Collection", collection_choices, index=0)

        if selected_collection == "All Collections":
            collection_items = vendor_items
        else:
            collection_items = [
                item for item in vendor_items if str(item.get("collection", "")).strip() == selected_collection
            ]

    if not collection_items:
        collection_items = vendor_items
    item_number_options = ["All items"] + [
        str(item.get("item_number", "")).strip()
        for item in collection_items
        if str(item.get("item_number", "")).strip()
    ]
    selected_item_number = st.sidebar.selectbox("Item number", item_number_options, index=0)

    if selected_item_number != "All items":
        collection_items = [
            item for item in collection_items if str(item.get("item_number", "")).strip() == selected_item_number
        ]

    item_options = ["All items"] + [
        (str(item.get("description", "")).strip() or str(item.get("item_number", "")).strip())
        for item in collection_items
    ]
    selected_desc = st.sidebar.selectbox("Item description", item_options, index=0)

    if selected_desc != "All items":
        selected_item = next(
            (item for item in collection_items if str(item.get("description", "")) == selected_desc),
            collection_items[0],
        )
    elif selected_item_number != "All items":
        selected_item = collection_items[0] if collection_items else None
    else:
        selected_item = None
    vendor_items = collection_items
    aggregate_graphs = st.sidebar.checkbox(
        "Aggregate Graphs",
        value=True,
        help="Checked = aggregate demand by vendor. Unchecked = show one line per SKU.",
    )

    run_meta = data.get("run_meta", {})
    if selected_item:
        header_title = f"{selected_item.get('item_number','')} | {selected_item.get('description','')}"
        vendor_label = selected_item.get("vendor_name") or "--"
        vendor_number = selected_item.get("vendor_number") or "--"
        vendor_summary = selected_item
    else:
        coll_part = selected_collection if selected_collection != "All Collections" else "All items"
        header_title = f"{selected_vendor} | {coll_part}"
        vendor_label = selected_vendor or "--"
        vendor_summary = _aggregate_items(vendor_items)
        vendor_number = vendor_summary.get("vendor_number", "--")
    _jump_vendor_number = str(vendor_number).strip().lstrip("0") if str(vendor_number).strip() != "--" else ""
    _render_sidebar_jump_control(
        _jump_cards_for_tab(current_tab, include_3m=(is_sundries and _jump_vendor_number == "3")),
        current_tab,
    )
    horizon_days = run_meta.get("forecast_horizon_days", FUTURE_FORECAST_DAYS)
    run_stamp = run_meta.get("run_timestamp_local", "--")

    if is_sundries:
        title_text = "OMP Sundries Purchasing Dashboard"
    elif is_niko:
        title_text = "OMP Unfinished Flooring Purchasing Dashboard"
    elif is_moulding:
        title_text = "OMP Moulding Purchasing Dashboard"
    else:
        title_text = "OMP Engineered Floor Purchasing Dashboard"
    _jump_anchor("top")
    st.markdown(
        f"""
        <div class="hero-card">
          <div class="hero-top">
            <div class="pill">{title_text}</div>
            <div class="hero-meta">
              <div>{vendor_label} (Vendor {vendor_number})</div>
              <div>Horizon: {horizon_days} days | Run: {run_stamp}</div>
              <div>UI build: {UI_BUILD}</div>
            </div>
          </div>
          <div class="hero-title">{header_title}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Collection shipping / pallet details (flooring tabs only, when a collection is selected)
    if not is_sundries and not is_moulding and selected_collection != "All Collections":
        coll_details = _get_collection_details_for(selected_collection)
        if coll_details:
            st.markdown(_render_collection_details_html(coll_details), unsafe_allow_html=True)

    metrics_rows = data.get("Inventory_Metrics", [])
    # Niko tab shows individual SKU rows; all other tabs use consolidated groups.
    if not is_niko:
        metrics_rows = _consolidate_inventory_metrics(metrics_rows)
    if selected_collection != "All Collections":
        metrics_rows = [
            row for row in metrics_rows if str(row.get("collection", "")).strip() == selected_collection
        ]
    if selected_item:
        item_id = str(selected_item.get("item_number", "")).strip()
        if item_id:
            metrics_rows = [
                row for row in metrics_rows
                if str(row.get("sku", row.get("item_number", ""))).strip() == item_id
            ]
    _raw_mp: List[Dict] = []
    if is_niko:
        _raw_mp = data.get("Monthly_Projections", [])
        _niko_metrics = metrics_rows
        if selected_vendor != "All Vendors":
            _niko_metrics = [
                row for row in _niko_metrics
                if str(row.get("vendor_name", row.get("collection", ""))).strip() == selected_vendor
            ]
        queue_df = _build_niko_glance_df(_niko_metrics, _raw_mp, member_snapshots=data.get("member_snapshots", []))
        if selected_item and not queue_df.empty:
            queue_df = queue_df[queue_df["Item #"].astype(str).str.strip() == str(selected_item.get("item_number", "")).strip()]
    else:
        queue_df = _build_queue_df_from_inventory(metrics_rows)
        if not queue_df.empty and selected_vendor != "All Vendors":
            queue_df = queue_df[queue_df["Vendor"].astype(str).str.strip() == selected_vendor]
        if selected_item and not queue_df.empty and "Item" in queue_df.columns:
            queue_df = queue_df[queue_df["Item"].astype(str).str.strip() == str(selected_item.get("item_number", "")).strip()]
        if queue_df.empty:
            queue_df = _build_queue_df_from_inventory(_demo_webapp_payload().get("Inventory_Metrics", []))

    monthly_rows = data.get("Monthly_Projections", [])
    arrivals_rows = data.get("arrivals", [])

    # Strip lawsuit-hold vendors from the arrivals display regardless of cache age.
    arrivals_rows = [
        r for r in arrivals_rows
        if str(r.get("VENDOR_NUMBER", "")).strip() not in EXCLUDED_PO_VENDORS
    ]

    # Apply SKU consolidation for Niko (Strip) tab - combine vendor SKUs into consolidated items
    if is_niko:
        monthly_rows = _consolidate_monthly_projections(monthly_rows)
        arrivals_rows = _consolidate_arrivals(arrivals_rows)

    if selected_collection != "All Collections":
        monthly_rows = [
            row for row in monthly_rows if str(row.get("collection", "")).strip() == selected_collection
        ]
    if selected_item:
        series = selected_item.get("forecast_series", {}) or {}
        if not series.get("hist_y"):
            series = {**series, **_series_from_monthly_rows(monthly_rows, selected_item.get("item_number", ""))}
        forecast_fig = _build_forecast_chart(series)
    else:
        if selected_vendor != "All Vendors":
            if aggregate_graphs:
                series_key = "collection" if selected_collection != "All Collections" else "vendor_name"
                series_value = selected_collection if series_key == "collection" else selected_vendor
                vendor_series = _series_from_monthly_rows_vendor(monthly_rows, series_value, key=series_key)
                if vendor_series.get("hist_y") or vendor_series.get("fc_y"):
                    forecast_fig = _build_forecast_chart(vendor_series)
                else:
                    # Fallback: aggregate from item forecast_series with historical data
                    series_list = []
                    for item in vendor_items:
                        sku = item.get("item_number", "")
                        item_series = _series_from_monthly_rows(monthly_rows, sku)
                        fallback_series = item.get("forecast_series", {}) or {}
                        if not item_series.get("hist_y") and fallback_series.get("hist_y"):
                            item_series["hist_x"] = fallback_series.get("hist_x")
                            item_series["hist_y"] = fallback_series.get("hist_y")
                        if not item_series.get("fc_y") and (fallback_series.get("fc_y") or fallback_series.get("y")):
                            item_series["fc_x"] = fallback_series.get("fc_x") or fallback_series.get("x")
                            item_series["fc_y"] = fallback_series.get("fc_y") or fallback_series.get("y")
                    series_list.append({
                        "label": item.get("item_number", item.get("description", "")),
                        "series": item_series,
                    })
                    forecast_fig = _build_forecast_chart_multi(series_list)
                    forecast_fig = _apply_legend_padding(forecast_fig, len(series_list))
            else:
                series_list = []
                for item in vendor_items:
                    sku = item.get("item_number", "")
                    series = _series_from_monthly_rows(monthly_rows, sku)
                    fallback_series = item.get("forecast_series", {}) or {}
                    if not series.get("hist_y") and fallback_series.get("hist_y"):
                        series["hist_x"] = fallback_series.get("hist_x")
                        series["hist_y"] = fallback_series.get("hist_y")
                    if not series.get("fc_y") and (fallback_series.get("fc_y") or fallback_series.get("y")):
                        series["fc_x"] = fallback_series.get("fc_x") or fallback_series.get("x")
                        series["fc_y"] = fallback_series.get("fc_y") or fallback_series.get("y")
                    series_list.append(
                        {
                            "label": item.get("item_number", item.get("description", "")),
                            "series": series,
                        }
                    )
                forecast_fig = _build_forecast_chart_multi(series_list)
                forecast_fig = _apply_legend_padding(forecast_fig, len(series_list))
        else:
            # All Vendors selected
            if aggregate_graphs:
                if selected_collection != "All Collections":
                    # Aggregate by collection when only collection is selected
                    collection_series = _series_from_monthly_rows_vendor(monthly_rows, selected_collection, key="collection")
                else:
                    # Aggregate all items (for Niko tab with All Vendors / All Collections)
                    collection_series = _series_from_monthly_rows_vendor(monthly_rows, None, key=None)
                if collection_series.get("hist_y") or collection_series.get("fc_y"):
                    forecast_fig = _build_forecast_chart(collection_series)
                else:
                    # Fallback: aggregate from item forecast_series
                    series_list = []
                    for item in vendor_items:
                        sku = item.get("item_number", "")
                        item_series = _series_from_monthly_rows(monthly_rows, sku)
                        fallback_series = item.get("forecast_series", {}) or {}
                        if not item_series.get("hist_y") and fallback_series.get("hist_y"):
                            item_series["hist_x"] = fallback_series.get("hist_x")
                            item_series["hist_y"] = fallback_series.get("hist_y")
                        if not item_series.get("fc_y") and (fallback_series.get("fc_y") or fallback_series.get("y")):
                            item_series["fc_x"] = fallback_series.get("fc_x") or fallback_series.get("x")
                            item_series["fc_y"] = fallback_series.get("fc_y") or fallback_series.get("y")
                        series_list.append({
                            "label": item.get("item_number", item.get("description", "")),
                            "series": item_series,
                        })
                    forecast_fig = _build_forecast_chart_multi(series_list)
                    forecast_fig = _apply_legend_padding(forecast_fig, len(series_list))
            else:
                # Show each item with historical data
                series_list = []
                for item in vendor_items:
                    sku = item.get("item_number", "")
                    item_series = _series_from_monthly_rows(monthly_rows, sku)
                    fallback_series = item.get("forecast_series", {}) or {}
                    if not item_series.get("hist_y") and fallback_series.get("hist_y"):
                        item_series["hist_x"] = fallback_series.get("hist_x")
                        item_series["hist_y"] = fallback_series.get("hist_y")
                    if not item_series.get("fc_y") and (fallback_series.get("fc_y") or fallback_series.get("y")):
                        item_series["fc_x"] = fallback_series.get("fc_x") or fallback_series.get("x")
                        item_series["fc_y"] = fallback_series.get("fc_y") or fallback_series.get("y")
                    series_list.append({
                        "label": item.get("item_number", item.get("description", "")),
                        "series": item_series,
                    })
                forecast_fig = _build_forecast_chart_multi(series_list)
                forecast_fig = _apply_legend_padding(forecast_fig, len(series_list))

    fig_html = forecast_fig.to_html(include_plotlyjs="cdn", full_html=False)
    demand_html = _render_demand_graph_html(forecast_fig)
    # Fixed height for demand graph - 1128px total (1008 graph + 120 for legend area)
    _jump_anchor("demand")
    with st.expander("DEMAND", expanded=True):
        components.html(demand_html, height=1128, scrolling=False)

    arrivals_df_all = pd.DataFrame(arrivals_rows)
    selected_sku_for_schedule = (
        str(selected_item.get("item_number", "")).strip()
        if selected_item else None
    )
    schedule_months = _projection_schedule_months(monthly_rows, selected_sku_for_schedule)
    arrivals_all_out = _prepare_arrivals_df(
        arrivals_df_all,
        selected_vendor,
        selected_collection,
        schedule_months=schedule_months,
    )
    if selected_item and not arrivals_all_out.empty and "Item#" in arrivals_all_out.columns:
        arrivals_all_out = arrivals_all_out[
            arrivals_all_out["Item#"].astype(str).str.strip()
            == str(selected_item.get("item_number", "")).strip()
        ]
    _jump_anchor("arrivals")
    if not arrivals_all_out.empty:
        _arr_cols = list(arrivals_all_out.columns)
        _arr_default = "Due in Inventory" if "Due in Inventory" in _arr_cols else _arr_cols[0]
        _arr_idx = _arr_cols.index(_arr_default) if _arr_default in _arr_cols else 0
        _asc1, _asc2, _asc3 = st.columns([0.08, 0.28, 0.64])
        with _asc1:
            st.caption("Sort by")
        with _asc2:
            _arr_sort_col = st.selectbox("arr_sort_col", _arr_cols, index=_arr_idx,
                                         key="arrivals_sort_col", label_visibility="collapsed")
        with _asc3:
            _arr_sort_asc = st.radio("arr_sort_dir", ["↑ Asc", "↓ Desc"], index=1,
                                     horizontal=True, key="arrivals_sort_dir",
                                     label_visibility="collapsed") == "↑ Asc"
        try:
            arrivals_all_out = arrivals_all_out.sort_values(
                _arr_sort_col, ascending=_arr_sort_asc, na_position="last"
            )
        except Exception:
            pass
    with st.expander("ARRIVALS", expanded=True):
        st.markdown(_render_arrivals_table_html(arrivals_all_out), unsafe_allow_html=True)

    # Placeholder: for the Carlos tab the Reorder Now card renders here (above the Glance card).
    _rn_placeholder = st.container() if is_sundries else None
    _jump_anchor("ungrouped-demand" if is_niko else "inventory-glance")
    if not queue_df.empty:
        _q_cols = list(queue_df.columns)
        _q_default = "Available" if "Available" in _q_cols else _q_cols[0]
        _q_idx = _q_cols.index(_q_default) if _q_default in _q_cols else 0
        _qsc1, _qsc2, _qsc3 = st.columns([0.08, 0.28, 0.64])
        with _qsc1:
            st.caption("Sort by")
        with _qsc2:
            _q_sort_col = st.selectbox("q_sort_col", _q_cols, index=_q_idx,
                                       key="queue_sort_col", label_visibility="collapsed")
        with _qsc3:
            _q_sort_asc = st.radio("q_sort_dir", ["↑ Asc", "↓ Desc"], index=1,
                                   horizontal=True, key="queue_sort_dir",
                                   label_visibility="collapsed") == "↑ Asc"
        try:
            queue_df = queue_df.sort_values(
                _q_sort_col, ascending=_q_sort_asc, na_position="last"
            )
        except Exception:
            pass
    _glance_title = "UNGROUPED DEMAND" if is_niko else "INVENTORY AT A GLANCE"
    with st.expander(_glance_title, expanded=True):
        if is_niko:
            st.markdown(_render_niko_glance_table_html(queue_df), unsafe_allow_html=True)
        else:
            st.markdown(_render_queue_table_html(queue_df), unsafe_allow_html=True)
        if not queue_df.empty:
            try:
                from openpyxl.styles import Font, PatternFill, Border, Side
                from openpyxl.utils import get_column_letter
                _glance_buf = io.BytesIO()
                with pd.ExcelWriter(_glance_buf, engine="openpyxl") as _writer:
                    queue_df.to_excel(_writer, sheet_name="Inventory At a Glance", index=False)
                    _ws = _writer.sheets["Inventory At a Glance"]
                    _thin = Border(
                        left=Side(style="thin"), right=Side(style="thin"),
                        top=Side(style="thin"), bottom=Side(style="thin"),
                    )
                    _hdr_font = Font(bold=True, color="1F4E79")
                    _hdr_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
                    # Row highlight fills matching dashboard CSS
                    _fill_group      = PatternFill(start_color="9DC3E6", end_color="9DC3E6", fill_type="solid")
                    _fill_group_font = Font(bold=True, color="1F4E79")
                    _fill_member     = PatternFill(start_color="FFD0D0", end_color="FFD0D0", fill_type="solid")
                    _fill_standalone = PatternFill(start_color="EDF3FB", end_color="EDF3FB", fill_type="solid")
                    # Determine which column holds the item key
                    _item_col = "Item #" if "Item #" in queue_df.columns else "Item"
                    _item_col_idx = list(queue_df.columns).index(_item_col) if _item_col in queue_df.columns else -1
                    # Header row
                    for _ci in range(1, len(queue_df.columns) + 1):
                        _cell = _ws.cell(row=1, column=_ci)
                        _cell.font = _hdr_font
                        _cell.fill = _hdr_fill
                        _cell.border = _thin
                        _ws.column_dimensions[get_column_letter(_ci)].width = 16
                    # Data rows with per-row highlighting
                    for _ri, (_, _drow) in enumerate(queue_df.iterrows(), start=2):
                        _sku_val = ""
                        if _item_col_idx >= 0:
                            _sku_val = str(_drow.iloc[_item_col_idx]).strip().upper()
                        if _sku_val in SKU_GROUP_KEYS or _sku_val in SKU_TO_CONSOLIDATED:
                            _row_fill = _fill_group
                            _row_font = _fill_group_font
                        elif _sku_val in SKU_TO_CONSOLIDATED:
                            _row_fill = _fill_member
                            _row_font = None
                        else:
                            _row_fill = _fill_standalone
                            _row_font = None
                        for _ci in range(1, len(queue_df.columns) + 1):
                            _cell = _ws.cell(row=_ri, column=_ci)
                            _cell.fill = _row_fill
                            _cell.border = _thin
                            if _row_font:
                                _cell.font = _row_font
                _glance_buf.seek(0)
                st.download_button(
                    label="Export to Excel",
                    data=_glance_buf,
                    file_name=f"inventory_at_a_glance_{pd.Timestamp.today().strftime('%Y-%m-%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="glance_export",
                )
            except Exception:
                pass

    # Reorder Now section
    reorder_now_df = pd.DataFrame()
    optimizer_source_df = pd.DataFrame()

    if is_sundries and metrics_rows:
        # Sundries: build directly from metrics_rows.
        # Order Up To = 90-day total sales + LT demand  (simple, purchaser-approved formula)
        # ROQ = max(0, Order Up To − IP)
        def _sundries_metric_map(field):
            return {
                str(m.get("item_number", "")).strip().upper(): m.get(field)
                for m in metrics_rows
            }

        _vnum_map  = {str(m.get("item_number", "")).strip().upper(): str(m.get("vendor_number", "")).strip() for m in metrics_rows}
        _vname_map = {str(m.get("item_number", "")).strip().upper(): str(m.get("vendor_name",   "")).strip() for m in metrics_rows}
        _avail_map = _sundries_metric_map("available")
        _bo_map    = _sundries_metric_map("backorder")
        _onpo_map  = _sundries_metric_map("on_order")
        _s30d_map  = _sundries_metric_map("units_30d")
        _r90_map   = _sundries_metric_map("rate_90")
        _r365_map  = _sundries_metric_map("rate_365")
        _lt_map    = _sundries_metric_map("lead_time_days")
        _lt_dem_map = _sundries_metric_map("reorder_point")  # SS=0 so ROP ≈ LT demand
        _ip_map    = _sundries_metric_map("inventory_position")

        _rn_rows = []
        for _m in metrics_rows:
            _inum = str(_m.get("item_number", "")).strip().upper()
            if not _inum:
                continue
            if selected_vendor != "All Vendors" and str(_m.get("vendor_name", "")).strip() != selected_vendor:
                continue
            if selected_collection != "All Collections" and str(_m.get("collection", "")).strip() != selected_collection:
                continue
            if selected_item and _inum != str(selected_item.get("item_number", "")).strip().upper():
                continue
            _units_90d   = float(_r90_map.get(_inum) or 0) * 3  # rate_90 is monthly avg; ×3 = 90-day total
            _lt_demand   = float(_lt_dem_map.get(_inum) or 0)
            _oul         = _units_90d + _lt_demand
            _roq = max(0.0, _oul - float(_ip_map.get(_inum) or 0))
            if _roq <= 0:
                continue
            _rn_rows.append({
                "Item Number":      _inum,
                "Description":      str(_m.get("description", "")),
                "Vendor #":         _vnum_map.get(_inum, ""),
                "LT":               float(_lt_map.get(_inum) or 0),
                "LT Demand":        _lt_demand,
                "30D Sales":        float(_s30d_map.get(_inum) or 0),
                "90D Avg":          float(_r90_map.get(_inum) or 0),
                "365D Avg":         float(_r365_map.get(_inum) or 0),
                "Available":        float(_avail_map.get(_inum) or 0),
                "Back Orders":      float(_bo_map.get(_inum) or 0),
                "On PO":            float(_onpo_map.get(_inum) or 0),
                "Order Up To":      round(_oul, 1),
                "IP":               float(_ip_map.get(_inum) or 0),
                "Reorder Quantity": round(_roq, 1),
            })

        if _rn_rows:
            reorder_now_df = pd.DataFrame(_rn_rows)
            _rn_total = {
                "Item Number":      "Total",
                "Description":      "",
                "Vendor #":         "",
                "LT":               "",
                "LT Demand":        reorder_now_df["LT Demand"].sum(skipna=True),
                "30D Sales":        reorder_now_df["30D Sales"].sum(skipna=True),
                "90D Avg":          reorder_now_df["90D Avg"].sum(skipna=True),
                "365D Avg":         reorder_now_df["365D Avg"].sum(skipna=True),
                "Available":        reorder_now_df["Available"].sum(skipna=True),
                "Back Orders":      reorder_now_df["Back Orders"].sum(skipna=True),
                "On PO":            reorder_now_df["On PO"].sum(skipna=True),
                "Order Up To":      reorder_now_df["Order Up To"].sum(skipna=True),
                "IP":               reorder_now_df["IP"].sum(skipna=True),
                "Reorder Quantity": reorder_now_df["Reorder Quantity"].sum(skipna=True),
            }
            reorder_now_df = pd.concat([reorder_now_df, pd.DataFrame([_rn_total])], ignore_index=True)

        # Render Reorder Now into the placeholder above the Inventory At a Glance card.
        _month_label_rn = pd.Timestamp.today().strftime("%B %Y")
        with _rn_placeholder:
            _jump_anchor("reorder-now")
            if reorder_now_df.empty or "Item Number" not in reorder_now_df.columns:
                st.info("No items require reordering at this time.")
            else:
                _skip_sort_s = {"Price", "Freight", "Additional Charges"}
                _rn_data_s  = reorder_now_df[reorder_now_df["Item Number"] != "Total"]
                _rn_total_s = reorder_now_df[reorder_now_df["Item Number"] == "Total"]
                _rn_cols_s  = [c for c in _rn_data_s.columns if c not in _skip_sort_s]
                _rn_def_s   = "Reorder Quantity" if "Reorder Quantity" in _rn_cols_s else (_rn_cols_s[0] if _rn_cols_s else None)
                if _rn_cols_s and _rn_def_s:
                    _rnsc1, _rnsc2, _rnsc3 = st.columns([0.08, 0.28, 0.64])
                    with _rnsc1:
                        st.caption("Sort by")
                    with _rnsc2:
                        _rn_sort_col = st.selectbox("rn_sort_col_s", _rn_cols_s,
                                                    index=_rn_cols_s.index(_rn_def_s),
                                                    key="reorder_sort_col", label_visibility="collapsed")
                    with _rnsc3:
                        _rn_sort_asc = st.radio("rn_sort_dir_s", ["↑ Asc", "↓ Desc"], index=1,
                                                horizontal=True, key="reorder_sort_dir",
                                                label_visibility="collapsed") == "↑ Asc"
                    try:
                        _rn_data_s = _rn_data_s.sort_values(_rn_sort_col, ascending=_rn_sort_asc, na_position="last")
                        reorder_now_df = pd.concat([_rn_data_s, _rn_total_s], ignore_index=True)
                    except Exception:
                        pass
                with st.expander(f"REORDER NOW: {_month_label_rn}", expanded=True):
                    if not reorder_now_df.empty:
                        from openpyxl.styles import Font, PatternFill, Border, Side
                        from openpyxl.utils import get_column_letter
                        _rn_buf = io.BytesIO()
                        _rn_export = reorder_now_df[reorder_now_df["Item Number"] != "Total"].copy()
                        _vendor_pos = (
                            list(_rn_export.columns).index("Vendor #") + 1
                            if "Vendor #" in _rn_export.columns
                            else list(_rn_export.columns).index("Description") + 1
                        )
                        if "Vendor #" not in _rn_export.columns:
                            _rn_export.insert(
                                _vendor_pos,
                                "Vendor #",
                                _rn_export["Item Number"].map(lambda s: _vnum_map.get(str(s).strip().upper(), "")),
                            )
                            _vendor_pos += 1
                        _rn_export.insert(
                            _vendor_pos,
                            "Vendor Name",
                            _rn_export["Item Number"].map(lambda s: _vname_map.get(str(s).strip().upper(), "")),
                        )
                        with pd.ExcelWriter(_rn_buf, engine="openpyxl") as _rn_writer:
                            _rn_export.to_excel(_rn_writer, sheet_name="Reorder Now", index=False)
                            _rn_ws = _rn_writer.sheets["Reorder Now"]
                            _rn_thin = Border(left=Side(style="thin"), right=Side(style="thin"),
                                              top=Side(style="thin"), bottom=Side(style="thin"))
                            _rn_hfont = Font(bold=True, color="1F4E79")
                            _rn_hfill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
                            for _ci in range(1, len(_rn_export.columns) + 1):
                                _rn_ws.column_dimensions[get_column_letter(_ci)].width = 14
                                _c = _rn_ws.cell(row=1, column=_ci)
                                _c.font = _rn_hfont
                                _c.fill = _rn_hfill
                                _c.border = _rn_thin
                            for _ri in range(2, len(_rn_export) + 2):
                                for _ci in range(1, len(_rn_export.columns) + 1):
                                    _rn_ws.cell(row=_ri, column=_ci).border = _rn_thin
                        _rn_buf.seek(0)
                        st.download_button(
                            label="Export Reorder Now to Excel",
                            data=_rn_buf,
                            file_name=f"reorder_now_{pd.Timestamp.today().strftime('%Y-%m-%d')}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key="rn_export_sundries",
                        )
                    st.markdown(_render_reorder_now_table_html(reorder_now_df, _month_label_rn), unsafe_allow_html=True)

    elif monthly_rows:
        df_monthly_all = pd.DataFrame(monthly_rows)
        if "Month" in df_monthly_all.columns and "Order Quantity" in df_monthly_all.columns:
            df_monthly_all["Month"] = pd.to_datetime(df_monthly_all["Month"], errors="coerce")
            # Derive the reference month from the stored run timestamp so that
            # stale payloads viewed after month-end still show the correct orders.
            # Fall back to today() only if the payload pre-dates this field.
            _run_ts_str = run_stamp if (run_stamp and run_stamp != "--") else ""
            if _run_ts_str:
                try:
                    current_month = pd.Timestamp(_run_ts_str).to_period("M").to_timestamp()
                except Exception:
                    current_month = pd.Timestamp.today().to_period("M").to_timestamp()
            else:
                current_month = pd.Timestamp.today().to_period("M").to_timestamp()
            df_current = df_monthly_all[df_monthly_all["Month"] == current_month]
            df_current = df_current[pd.to_numeric(df_current["Order Quantity"], errors="coerce") > 0]
            if selected_vendor != "All Vendors" and "vendor_name" in df_current.columns:
                df_current = df_current[df_current["vendor_name"].astype(str).str.strip() == selected_vendor]
            if selected_collection != "All Collections" and "collection" in df_current.columns:
                df_current = df_current[df_current["collection"].astype(str).str.strip() == selected_collection]
            if selected_item and "SKU" in df_current.columns:
                df_current = df_current[df_current["SKU"].astype(str) == str(selected_item.get("item_number", ""))]

            if not df_current.empty:
                optimizer_source_df = df_current.copy()
                reorder_sf = pd.to_numeric(df_current["Order Quantity"], errors="coerce")
                if is_niko:
                    # Build lookup maps from Inventory_Metrics
                    ver_item_numbers = df_current.get("SKU", pd.Series(dtype=str)).astype(str).str.strip().str.upper()

                    def _make_metric_map(field):
                        return {
                            str(m.get("item_number", m.get("sku", ""))).strip().upper(): m.get(field)
                            for m in metrics_rows
                        }

                    ip_map    = _make_metric_map("inventory_position")
                    wkly_map  = _make_metric_map("avg_weekly_demand")
                    lt_dem_map = _make_metric_map("lead_time_mean_demand")
                    ss_map    = _make_metric_map("safety_stock")
                    s_map     = _make_metric_map("order_up_to_level")

                    def _get_ip(s):
                        return ip_map.get(s, np.nan)

                    def _get_s(s):
                        return s_map.get(s, np.nan)

                    ip_col  = ver_item_numbers.map(_get_ip)
                    s_col   = ver_item_numbers.map(_get_s)
                    gap_col = s_col - ip_col

                    # Gap > 0 means we need to order; negative gaps mean we're above S already.
                    reorder_qty_col = np.where(gap_col.values > 0, gap_col.values, 0.0)
                    reorder_now_df = pd.DataFrame(
                        {
                            "Item Number":           df_current.get("SKU", ""),
                            "Description":           df_current.get("description", ""),
                            "Inventory Position":    ip_col.values,
                            "Avg Weekly Demand":     ver_item_numbers.map(lambda s: wkly_map.get(s, np.nan)).values,
                            "LT Demand (SF)":        ver_item_numbers.map(lambda s: lt_dem_map.get(s, np.nan)).values,
                            "Safety Stock (SF)":     ver_item_numbers.map(lambda s: ss_map.get(s, np.nan)).values,
                            "S (Order Up to Level)": s_col.values,
                            "ROQ (S \u2212 IP)":     gap_col.values,
                        }
                    )
                    total_row = {
                        "Item Number":           "Total",
                        "Description":           "",
                        "Inventory Position":    reorder_now_df["Inventory Position"].sum(skipna=True),
                        "Avg Weekly Demand":     reorder_now_df["Avg Weekly Demand"].sum(skipna=True),
                        "LT Demand (SF)":        reorder_now_df["LT Demand (SF)"].sum(skipna=True),
                        "Safety Stock (SF)":     reorder_now_df["Safety Stock (SF)"].sum(skipna=True),
                        "S (Order Up to Level)": reorder_now_df["S (Order Up to Level)"].sum(skipna=True),
                        # ROQ total = sum of positive gaps only (negative = already above S)
                        "ROQ (S \u2212 IP)":     float(reorder_qty_col.sum()),
                    }
                    reorder_now_df = pd.concat(
                        [reorder_now_df, pd.DataFrame([total_row])], ignore_index=True
                    )
                else:
                    sf_per_pallet_col = df_current.get("sf_per_pallet")
                    pallets_per_container_col = df_current.get("pallets_per_container")

                    # Handle case where columns may not exist
                    if sf_per_pallet_col is not None:
                        sf_per_pallet = pd.to_numeric(sf_per_pallet_col, errors="coerce").replace(0, np.nan)
                    else:
                        sf_per_pallet = pd.Series([np.nan] * len(df_current), index=df_current.index)

                    if pallets_per_container_col is not None:
                        pallets_per_container = pd.to_numeric(pallets_per_container_col, errors="coerce").replace(0, np.nan)
                    else:
                        pallets_per_container = pd.Series([np.nan] * len(df_current), index=df_current.index)

                    # Available SF, S, and IP — from Inventory_Metrics
                    # Flooring records use "sku" as the item key; sundries/moulding use "item_number"
                    item_numbers = df_current.get("SKU", pd.Series(dtype=str)).astype(str).str.strip().str.upper()
                    avail_map = {
                        str(m.get("item_number", m.get("sku", ""))).strip().upper(): float(
                            m.get("available_sf", m.get("available", 0)) or 0
                        )
                        for m in metrics_rows
                    }
                    s_map = {
                        str(m.get("item_number", m.get("sku", ""))).strip().upper(): float(
                            m.get("order_up_to_level", 0) or 0
                        )
                        for m in metrics_rows
                    }
                    ip_map = {
                        str(m.get("item_number", m.get("sku", ""))).strip().upper(): float(
                            m.get("inventory_position", 0) or 0
                        )
                        for m in metrics_rows
                    }
                    lt_days_map = {
                        str(m.get("item_number", m.get("sku", ""))).strip().upper(): float(
                            m.get("lead_time_days", 0) or 0
                        )
                        for m in metrics_rows
                    }
                    avail_col = item_numbers.map(lambda s: avail_map.get(s, np.nan))

                    # Reorder Quantity = max(0, S − IP_today).
                    # Directly answers "what do we need to order right now to return to
                    # good shape?"  S (order_up_to_level) and IP (inventory_position) are
                    # the final post-budget metrics values — not the simulation's monthly
                    # Order Quantity sum, which can fire on a future day at a lower IP.
                    reorder_sf = item_numbers.map(
                        lambda s: max(0.0, s_map.get(s, 0.0) - ip_map.get(s, 0.0))
                    )
                    reorder_pallets = reorder_sf / sf_per_pallet
                    reorder_pct = reorder_pallets / pallets_per_container

                    reorder_now_df = pd.DataFrame(
                        {
                            "Item Number":                       df_current.get("SKU", ""),
                            "Collection":                        df_current.get("collection", ""),
                            "Description":                       df_current.get("description", ""),
                            "Available (SF)":                    avail_col.values,
                            "Reorder Quantity (SF)":             reorder_sf,
                            "Reorder Quantity (Pallets)":        reorder_pallets,
                            "Reorder Quantity (% of Container)": reorder_pct,
                            "Lead Time (days)":                  item_numbers.map(lambda s: lt_days_map.get(s, np.nan)).values,
                            "Inventory Position":                item_numbers.map(lambda s: ip_map.get(s, np.nan)).values,
                        }
                    )
                    total_sf      = reorder_now_df["Reorder Quantity (SF)"].sum(skipna=True)
                    total_pallets = reorder_now_df["Reorder Quantity (Pallets)"].sum(skipna=True)
                    total_pct     = reorder_now_df["Reorder Quantity (% of Container)"].sum(skipna=True)
                    total_avail   = reorder_now_df["Available (SF)"].sum(skipna=True)
                    total_row = {
                        "Item Number":                       "Total",
                        "Collection":                        "",
                        "Description":                       "",
                        "Available (SF)":                    total_avail,
                        "Reorder Quantity (SF)":             total_sf,
                        "Reorder Quantity (Pallets)":        total_pallets,
                        "Reorder Quantity (% of Container)": total_pct,
                        "Lead Time (days)":                  "",
                        "Inventory Position":                reorder_now_df["Inventory Position"].sum(skipna=True),
                    }
                    reorder_now_df = pd.concat(
                        [reorder_now_df, pd.DataFrame([total_row])], ignore_index=True
                    )
    month_label = pd.Timestamp.today().strftime("%B %Y")

    if is_niko:
        _jump_anchor("grouped-demand")
        # ── GROUPED DEMAND card ────────────────────────────────────────────────
        # Same columns as Ungrouped Demand but keeps consolidated group rows intact.
        _grp_metrics = metrics_rows
        if selected_vendor != "All Vendors":
            _grp_metrics = [
                row for row in _grp_metrics
                if str(row.get("vendor_name", row.get("collection", ""))).strip() == selected_vendor
            ]
        if selected_item:
            _grp_item_id = str(selected_item.get("item_number", "")).strip()
            if _grp_item_id:
                _grp_metrics = [
                    row for row in _grp_metrics
                    if str(row.get("sku", row.get("item_number", ""))).strip() == _grp_item_id
                ]
        niko_grouped_df = _build_niko_grouped_df(_grp_metrics, _raw_mp)
        if not niko_grouped_df.empty:
            _gd_cols = list(niko_grouped_df.columns)
            _gd_default = "Available" if "Available" in _gd_cols else _gd_cols[0]
            _gd_idx = _gd_cols.index(_gd_default) if _gd_default in _gd_cols else 0
            _gdsc1, _gdsc2, _gdsc3 = st.columns([0.08, 0.28, 0.64])
            with _gdsc1:
                st.caption("Sort by")
            with _gdsc2:
                _gd_sort_col = st.selectbox("gd_sort_col", _gd_cols, index=_gd_idx,
                                            key="grouped_sort_col", label_visibility="collapsed")
            with _gdsc3:
                _gd_sort_asc = st.radio("gd_sort_dir", ["↑ Asc", "↓ Desc"], index=1,
                                        horizontal=True, key="grouped_sort_dir",
                                        label_visibility="collapsed") == "↑ Asc"
            try:
                niko_grouped_df = niko_grouped_df.sort_values(
                    _gd_sort_col, ascending=_gd_sort_asc, na_position="last"
                )
            except Exception:
                pass
        with st.expander("GROUPED DEMAND", expanded=True):
            st.markdown(_render_niko_glance_table_html(niko_grouped_df), unsafe_allow_html=True)
            if not niko_grouped_df.empty:
                try:
                    from openpyxl.styles import Font, PatternFill, Border, Side
                    from openpyxl.utils import get_column_letter
                    _gd_buf = io.BytesIO()
                    with pd.ExcelWriter(_gd_buf, engine="openpyxl") as _writer:
                        niko_grouped_df.to_excel(_writer, sheet_name="Grouped Demand", index=False)
                        _ws = _writer.sheets["Grouped Demand"]
                        _thin = Border(
                            left=Side(style="thin"), right=Side(style="thin"),
                            top=Side(style="thin"), bottom=Side(style="thin"),
                        )
                        _hdr_font = Font(bold=True, color="1F4E79")
                        _hdr_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
                        for _ci in range(1, len(niko_grouped_df.columns) + 1):
                            _cell = _ws.cell(row=1, column=_ci)
                            _cell.font = _hdr_font
                            _cell.fill = _hdr_fill
                            _cell.border = _thin
                            _ws.column_dimensions[get_column_letter(_ci)].width = 16
                        for _ri, (_, _drow) in enumerate(niko_grouped_df.iterrows(), start=2):
                            _sku_val = str(_drow.iloc[0]).strip().upper()
                            if _sku_val in SKU_GROUP_KEYS:
                                _row_fill = PatternFill(start_color="9DC3E6", end_color="9DC3E6", fill_type="solid")
                                _row_font = Font(bold=True, color="1F4E79")
                            else:
                                _row_fill = PatternFill(start_color="EDF3FB", end_color="EDF3FB", fill_type="solid")
                                _row_font = None
                            for _ci in range(1, len(niko_grouped_df.columns) + 1):
                                _cell = _ws.cell(row=_ri, column=_ci)
                                _cell.fill = _row_fill
                                _cell.border = _thin
                                if _row_font:
                                    _cell.font = _row_font
                    _gd_buf.seek(0)
                    st.download_button(
                        label="Export to Excel",
                        data=_gd_buf,
                        file_name=f"grouped_demand_{pd.Timestamp.today().strftime('%Y-%m-%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="grouped_export",
                    )
                except Exception:
                    pass
    elif not is_sundries:
        # ── REORDER NOW card (non-Niko, non-Carlos tabs) ───────────────────────
        _jump_anchor("reorder-now")
        _skip_sort_cols = {"Price", "Freight", "Additional Charges"}
        if not reorder_now_df.empty:
            _rn_data = reorder_now_df[reorder_now_df["Item Number"] != "Total"]
            _rn_total = reorder_now_df[reorder_now_df["Item Number"] == "Total"]
            _rn_cols = [c for c in _rn_data.columns if c not in _skip_sort_cols]
            _rn_default = "Description" if "Description" in _rn_cols else (_rn_cols[0] if _rn_cols else None)
            if _rn_cols and _rn_default:
                _rn_idx = _rn_cols.index(_rn_default) if _rn_default in _rn_cols else 0
                _rnsc1, _rnsc2, _rnsc3 = st.columns([0.08, 0.28, 0.64])
                with _rnsc1:
                    st.caption("Sort by")
                with _rnsc2:
                    _rn_sort_col = st.selectbox("rn_sort_col", _rn_cols, index=_rn_idx,
                                                key="reorder_sort_col", label_visibility="collapsed")
                with _rnsc3:
                    _rn_sort_asc = st.radio("rn_sort_dir", ["↑ Asc", "↓ Desc"], index=0,
                                            horizontal=True, key="reorder_sort_dir",
                                            label_visibility="collapsed") == "↑ Asc"
                try:
                    _rn_data = _rn_data.sort_values(
                        _rn_sort_col, ascending=_rn_sort_asc, na_position="last"
                    )
                    reorder_now_df = pd.concat([_rn_data, _rn_total], ignore_index=True)
                except Exception:
                    pass
        with st.expander(f"REORDER NOW: {month_label}", expanded=True):
            st.markdown(_render_reorder_now_table_html(reorder_now_df, month_label), unsafe_allow_html=True)

            # Export Reorder Now to Excel button
            if not reorder_now_df.empty:
                from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
                from openpyxl.utils import get_column_letter

                # Create Excel file in memory
                excel_buffer = io.BytesIO()
                # Remove the Total row for export
                export_df = reorder_now_df[reorder_now_df["Item Number"] != "Total"].copy()

                # Round up Quantity Needed to next decimal point (1 decimal place)
                if "Quantity Needed" in export_df.columns:
                    export_df["Quantity Needed"] = export_df["Quantity Needed"].apply(
                        lambda x: math.ceil(x * 10) / 10 if pd.notna(x) else x
                    )

                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                    export_df.to_excel(writer, sheet_name='Reorder Now', index=False)
                    workbook = writer.book
                    worksheet = writer.sheets['Reorder Now']

                    # Define column widths
                    col_widths = {
                        "Item Number": 20,
                        "Thickness": 10,
                        "Width": 8,
                        "Species": 15,
                        "Grade/Cut": 15,
                        "Edge": 12,
                        "Bundles": 10,
                        "Quantity Needed": 20,
                        "Price": 8,
                        "Freight": 8,
                        "Additional Charges": 18,
                    }

                    # Define styles
                    thin_border = Border(
                        left=Side(style='thin'),
                        right=Side(style='thin'),
                        top=Side(style='thin'),
                        bottom=Side(style='thin')
                    )
                    # Header: dark blue text, light blue fill (60% lighter)
                    header_font = Font(bold=True, color="1F4E79")  # Dark blue text
                    header_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")  # Light blue fill

                    # Apply column widths
                    for col_idx, col_name in enumerate(export_df.columns, start=1):
                        col_letter = get_column_letter(col_idx)
                        width = col_widths.get(col_name, 12)
                        worksheet.column_dimensions[col_letter].width = width

                    # Apply header formatting (row 1)
                    for col_idx in range(1, len(export_df.columns) + 1):
                        cell = worksheet.cell(row=1, column=col_idx)
                        cell.font = header_font
                        cell.fill = header_fill
                        cell.border = thin_border

                    # Apply borders to all data cells
                    for row_idx in range(2, len(export_df) + 2):
                        for col_idx in range(1, len(export_df.columns) + 1):
                            cell = worksheet.cell(row=row_idx, column=col_idx)
                            cell.border = thin_border

                excel_buffer.seek(0)
                file_date = pd.Timestamp.today().strftime("%Y-%m-%d")
                st.download_button(
                    label="Export Reorder Now to Excel",
                    data=excel_buffer,
                    file_name=f"reorder_now_{file_date}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

    # ── 3M ORDERING PROCESSOR (Carlos tab, vendor 3 only) ───────────────────
    # Transfer Report (Carlos tab): Loc 1 sends to locations 3, 4, 5, 6, 8, and 9.
    if is_sundries:
        _jump_anchor("transfer-report")
        with st.expander("TRANSFER REPORT", expanded=True):
            with st.spinner("Loading transfer report data..."):
                try:
                    _as_of_str = pd.Timestamp.today().normalize().isoformat()
                    _raw_transfer = _fetch_transfer_report_data(_as_of_str)
                    _transfer_df = _build_transfer_report_df(_raw_transfer)
                    if not _transfer_df.empty:
                        if selected_vendor != "All Vendors" and "Vendor Name" in _transfer_df.columns:
                            _transfer_df = _transfer_df[
                                _transfer_df["Vendor Name"].astype(str).str.strip() == str(selected_vendor).strip()
                            ]
                        if selected_item and "Item Number" in _transfer_df.columns:
                            _selected_item_number = str(selected_item.get("item_number", "")).strip().upper()
                            _transfer_df = _transfer_df[
                                _transfer_df["Item Number"].astype(str).str.strip().str.upper() == _selected_item_number
                            ]
                    _render_transfer_report_card(_transfer_df)
                except Exception as _transfer_err:
                    st.error(f"Transfer Report error: {_transfer_err}")

    _vnum_clean = str(vendor_number).strip().lstrip("0") if str(vendor_number).strip() != "--" else ""
    if is_sundries and _vnum_clean == "3":
        _jump_anchor("3m-ordering-processor")
        with st.expander("3M ORDERING PROCESSOR", expanded=True):
            with st.spinner("Loading 3M ordering data…"):
                try:
                    _as_of_str = pd.Timestamp.today().normalize().isoformat()
                    _raw_3m = _fetch_3m_ordering_data(_as_of_str)
                    if _raw_3m:
                        _df_3m, _loc_vals, _bundled = _build_3m_order_df(_raw_3m)
                        _render_3m_ordering_card(_df_3m, _loc_vals, _bundled)
                    else:
                        st.info("No active 3M sundries items found in the database.")
                except Exception as _3m_err:
                    st.error(f"3M Ordering Processor error: {_3m_err}")

    # VENDOR QUOTED COSTS card - only show for Niko (Strip/Veronica) tab
    if is_niko:
        _jump_anchor("vendor-quoted-costs")
        with st.expander("Vendor Quoted Costs", expanded=True):
            st.caption(
                "Tracks the price each vendor has quoted for every item. Edit a cell to update it — "
                "every change is logged below with a timestamp so a mistake can be traced and reversed."
            )

            _vq_pricing_inputs = _load_niko_pricing_inputs()
            _vq_vendor_names = _vq_pricing_inputs.get("vendor_names") or list(dict.fromkeys(STRIP_VENDOR_COLUMNS))
            _seed_vendor_quotes_from_workbook(_vq_pricing_inputs.get("prices_by_sku", {}))

            _vq_group_toggle = st.checkbox(
                "Group item numbers",
                value=st.session_state.get("vendor_quotes_group_toggle", False),
                key="vendor_quotes_group_toggle",
                help="Collapse consolidated SKUs (e.g. RH112QOSNB-15/RH112QOSNB-24) into a single row, "
                     "same as the Grouped Demand table above. Editing a collapsed row updates every "
                     "member SKU behind it.",
            )

            _vq_quotes = _load_vendor_quotes()
            _vq_sku_list = sorted(_load_strip_sku_list())
            _vq_sku_details = _load_strip_sku_details()

            _vq_row_members: Dict[str, List[str]] = {}
            _vq_rows: List[str] = []
            if _vq_group_toggle:
                _vq_grouped_seen: set = set()
                for _vq_group_key, _vq_members in SKU_CONSOLIDATION_GROUPS.items():
                    _vq_members_in_tab = [m for m in _vq_members if m in _vq_sku_list]
                    if not _vq_members_in_tab:
                        continue
                    _vq_grouped_seen.update(_vq_members_in_tab)
                    _vq_row_members[_vq_group_key] = _vq_members_in_tab
                    _vq_rows.append(_vq_group_key)
                for _vq_sku in _vq_sku_list:
                    if _vq_sku not in _vq_grouped_seen:
                        _vq_row_members[_vq_sku] = [_vq_sku]
                        _vq_rows.append(_vq_sku)
            else:
                for _vq_sku in _vq_sku_list:
                    _vq_row_members[_vq_sku] = [_vq_sku]
                    _vq_rows.append(_vq_sku)
            _vq_rows.sort()

            def _vq_description_for(row_key: str) -> str:
                for _m in _vq_row_members.get(row_key, [row_key]):
                    _desc = _vq_sku_details.get(_m, {}).get("Description", "")
                    if _desc:
                        return _desc
                return ""

            def _vq_resolved_prices_for(row_key: str) -> Dict[str, float]:
                # Some legacy workbook rows store a quote directly under the combined
                # group key rather than under each physical member SKU, so resolve
                # through the same merge helper the Optimizer uses for pricing lookups.
                return _lookup_prices_for_sku_core(
                    row_key,
                    _vq_quotes,
                    sku_to_consolidated=SKU_TO_CONSOLIDATED,
                    consolidation_groups=SKU_CONSOLIDATION_GROUPS,
                )

            _vq_table_rows = []
            for _row_key in _vq_rows:
                _row = {"Item #": _row_key, "Description": _vq_description_for(_row_key)}
                _vq_row_prices = _vq_resolved_prices_for(_row_key)
                for _vendor in _vq_vendor_names:
                    _val = _vq_row_prices.get(_vendor)
                    _row[_vendor] = float(_val) if _val is not None else None
                _vq_table_rows.append(_row)
            _vq_base_df = pd.DataFrame(_vq_table_rows, columns=["Item #", "Description"] + _vq_vendor_names)

            _vq_column_config = {
                "Item #": st.column_config.TextColumn("Item #", width="medium", disabled=True),
                "Description": st.column_config.TextColumn("Description", width="large", disabled=True),
            }
            for _vendor in _vq_vendor_names:
                _vq_column_config[_vendor] = st.column_config.NumberColumn(
                    _vendor, width="small", format="$%.2f", min_value=0.0, step=0.01
                )

            _vq_editor_key = "vendor_quotes_editor_grouped" if _vq_group_toggle else "vendor_quotes_editor_ungrouped"
            # Size the grid to fit every row with no internal scrollbar, so a purchasing
            # agent scanning the list can never mistake an off-screen row for a missing item.
            _vq_calculated_height = (len(_vq_base_df) * 35) + 35 + 10
            _vq_edited_df = st.data_editor(
                _vq_base_df,
                key=_vq_editor_key,
                column_config=_vq_column_config,
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                height=_vq_calculated_height,
            )

            _vq_changes: List[Tuple[str, str, Optional[float], Optional[float]]] = []
            _vq_base_indexed = _vq_base_df.set_index("Item #")
            _vq_edited_indexed = _vq_edited_df.set_index("Item #")
            for _row_key in _vq_rows:
                for _vendor in _vq_vendor_names:
                    _old_val = _vq_base_indexed.at[_row_key, _vendor]
                    _new_val = _vq_edited_indexed.at[_row_key, _vendor]
                    _old_norm = None if pd.isna(_old_val) else round(float(_old_val), 4)
                    _new_norm = None if pd.isna(_new_val) else round(float(_new_val), 4)
                    if _old_norm == _new_norm:
                        continue
                    for _member in _vq_row_members.get(_row_key, [_row_key]):
                        _member_old = _vq_quotes.get(_member, {}).get(_vendor)
                        _member_old = None if _member_old is None else round(float(_member_old), 4)
                        if _member_old == _new_norm:
                            continue
                        _vq_changes.append((_member, _vendor, _member_old, _new_norm))

            if _vq_changes:
                _apply_vendor_quote_changes(_vq_changes)
                st.success(f"Saved {len(_vq_changes)} price update(s).")
                st.rerun()

            with st.expander("Price Change Log", expanded=False):
                _vq_history_df = _load_vendor_quote_history(limit=300)
                if _vq_history_df.empty:
                    st.caption("No price changes recorded yet.")
                else:
                    _vq_display_df = _vq_history_df.rename(columns={
                        "id": "Log #", "sku": "Item #", "vendor": "Vendor",
                        "old_price": "Old Price", "new_price": "New Price", "changed_at": "Changed At (UTC)",
                    }).copy()
                    _vq_display_df["Old Price"] = _vq_display_df["Old Price"].map(
                        lambda v: "" if pd.isna(v) else f"${v:,.2f}"
                    )
                    _vq_display_df["New Price"] = _vq_display_df["New Price"].map(
                        lambda v: "" if pd.isna(v) else f"${v:,.2f}"
                    )
                    st.dataframe(_vq_display_df, width="stretch", hide_index=True)

                    _vq_revert_options = {
                        f"#{r.id} — {r.sku} / {r.vendor}: "
                        f"{'—' if pd.isna(r.old_price) else f'${r.old_price:,.2f}'} -> "
                        f"{'—' if pd.isna(r.new_price) else f'${r.new_price:,.2f}'} ({r.changed_at})": r.id
                        for r in _vq_history_df.itertuples()
                    }
                    _vq_revert_choice = st.selectbox(
                        "Revert a change",
                        options=["—"] + list(_vq_revert_options.keys()),
                        key="vendor_quotes_revert_choice",
                    )
                    if _vq_revert_choice != "—" and st.button("Revert selected change", key="vendor_quotes_revert_btn"):
                        _vq_reverted = _revert_vendor_quote_history_entry(_vq_revert_options[_vq_revert_choice])
                        if _vq_reverted:
                            st.success(f"Reverted {_vq_reverted[0]} / {_vq_reverted[1]} back to its prior value.")
                            st.rerun()
                        else:
                            st.error("Could not find that history entry.")

    # OPTIMIZER section - only show for Niko (Strip) tab
    if is_niko:
        optimizer_rows = []
        strip_details = _load_strip_sku_details()
        niko_pricing_inputs = _load_niko_pricing_inputs()
        vendor_names = niko_pricing_inputs.get("vendor_names") or list(dict.fromkeys(STRIP_VENDOR_COLUMNS))
        # Intermodal Freight.xlsx / Current Month is the authoritative source for freight
        # rates; it overrides any FREIGHT row pasted into Unfinished Pricing.xlsx.
        _intermodal_freight = _load_intermodal_freight_costs()
        freight_defaults = {**niko_pricing_inputs.get("freight_costs", {}), **_intermodal_freight}
        # Prices come from the tracked/editable Vendor Quoted Costs card (vendor_quotes.db),
        # not the workbook — that card seeds itself from the workbook on every load, so this
        # is always at least as complete, and reflects manual edits the workbook never sees.
        pricing_by_sku = _load_vendor_quotes()
        pricing_warnings = niko_pricing_inputs.get("warnings", [])
        # Build sf_per_pallet lookup from monthly projection data (carries
        # values loaded from the lead-times spreadsheet at forecast-run time).
        _sfpp_lookup: Dict[str, float] = {}
        if not optimizer_source_df.empty:
            for _, _sim_row in optimizer_source_df.iterrows():
                _sim_sku = str(_sim_row.get("SKU", "")).strip().upper()
                _sfpp = _coerce_positive_float(_sim_row.get("sf_per_pallet"))
                if _sim_sku and _sfpp:
                    _sfpp_lookup[_sim_sku] = _sfpp

        # Use the same metrics source and formula as the Grouped Demand ROQ:
        #   ROQ = max(0, order_up_to_level − inventory_position)
        # This replaces the previous approach of reading Order Quantity from the
        # monthly simulation, which used a larger S value and a stale inventory
        # reference point — causing the Optimizer to overstate reorder quantities.
        for m in _grp_metrics:
            sku = str(m.get("sku", m.get("item_number", ""))).strip().upper()
            if not sku:
                continue
            inv_available = float(m.get("available_sf", 0) or 0)
            inv_on_po     = float(m.get("on_po_sf", 0) or 0)
            inv_backorder = float(m.get("backorder_sf", 0) or 0)
            inv_position  = inv_available + inv_on_po - inv_backorder
            order_up_to   = float(m.get("order_up_to_level", 0) or 0)
            roq = max(0.0, order_up_to - inv_position)
            if roq <= 0:
                continue
            desc = str(m.get("description", "")).strip()
            if not desc:
                desc = strip_details.get(sku, {}).get("Description", "")
            sfpp = _sfpp_lookup.get(sku) or NIKO_DEFAULT_PALLET_SF
            row_prices = _lookup_strip_prices(sku, pricing_by_sku)
            optimizer_rows.append(
                {
                    "sku": sku,
                    "description": desc,
                    "quantity": math.ceil(roq),
                    "sf_per_pallet": sfpp,
                    "prices": row_prices,
                }
            )

        optimizer_rows.sort(key=lambda r: r["sku"])

        _jump_anchor("optimizer")
        with st.expander("OPTIMIZER", expanded=True):
            st.markdown('<div class="optimizer-anchor"></div>', unsafe_allow_html=True)
            st.caption(
                f"Prices are auto-filled from {UNFINISHED_PRICING_FILE} / Current Pricing. "
                f"Container freight rates come from {INTERMODAL_FREIGHT_FILE.name} / Current Month. "
                f"Freight is modeled per {NIKO_TRUCK_CAPACITY_SF:,.0f} SF truckload, "
                f"lines under {NIKO_MIN_PURCHASABLE_QTY_SF:,.0f} SF are omitted, "
                f"lines between {NIKO_MIN_PURCHASABLE_QTY_SF:,.0f} and {NIKO_MIN_FULL_PALLET_QTY_SF:,.0f} SF are rounded up to one pallet, "
                f"and pallet fill is capped at about {NIKO_MAX_PALLET_UPLIFT_PCT * 100:.0f}% above the suggested reorder quantity."
            )
            if not _intermodal_freight:
                st.warning(
                    f"No intermodal rates were loaded from {INTERMODAL_FREIGHT_FILE.name} / Current Month. "
                    "Verify the file exists in the Dashboard Files folder and the 'Current Month' sheet "
                    "has a Rate column with a Vendor name column to its right. "
                    "Freight values can still be typed manually into the Freight row below."
                )
            for warning_text in pricing_warnings:
                st.info(warning_text)

            if optimizer_rows:
                missing_quotes = [row["sku"] for row in optimizer_rows if not row.get("prices")]
                if missing_quotes:
                    st.warning(
                        "No vendor pricing was found for: " + ", ".join(sorted(missing_quotes))
                    )

                optimizer_df_data = []
                freight_row = {
                    "Include": False,
                    "SKU": "Freight",
                    "Description": f"Truck freight per {NIKO_TRUCK_CAPACITY_SF:,.0f} SF",
                    "Qty Needed": "",
                }
                for vname in vendor_names:
                    freight_row[vname] = freight_defaults.get(vname, "")
                optimizer_df_data.append(freight_row)

                for row in optimizer_rows:
                    row_data = {
                        "Include": bool(row.get("prices")),
                        "SKU": row["sku"],
                        "Description": row["description"],
                        "Qty Needed": row["quantity"],
                    }
                    for vname in vendor_names:
                        row_data[vname] = row.get("prices", {}).get(vname, "")
                    optimizer_df_data.append(row_data)

                optimizer_df = pd.DataFrame(optimizer_df_data)
                st.session_state["optimizer_edited_data"] = optimizer_df

                # SKU-level pallet-SF fallback from the lead-times spreadsheet.
                # Not shown in the UI — only used in optimizer calculations.
                _sfpp_fallback = {
                    normalize_item_key(r["sku"]): r.get("sf_per_pallet", NIKO_DEFAULT_PALLET_SF)
                    for r in optimizer_rows
                }

                selected_skus = tuple(sorted({
                    normalize_item_key(row["sku"])
                    for row in optimizer_rows
                    if row.get("prices")
                }))
                baseline_payload = _load_niko_cost_baselines(selected_skus)
                if baseline_payload.get("error"):
                    st.warning(baseline_payload["error"])

                # Build per-SKU, per-vendor container constraint data from the
                # pricing spreadsheet columns.  These are NOT displayed in the
                # optimizer card — they only feed the optimizer calculations.
                _sf_per_pallet_by_sku = niko_pricing_inputs.get("sf_per_pallet_by_sku", {})
                _pallets_per_cont_by_sku = niko_pricing_inputs.get("pallets_per_container_by_sku", {})
                _vendor_capacity_data: Dict[str, Dict[str, Dict[str, float]]] = {}
                for _sk in set(list(_sf_per_pallet_by_sku.keys()) + list(_pallets_per_cont_by_sku.keys())):
                    _sfpp_map = _sf_per_pallet_by_sku.get(_sk, {})
                    _ppc_map = _pallets_per_cont_by_sku.get(_sk, {})
                    _entry: Dict[str, Dict[str, float]] = {}
                    for _v in set(list(_sfpp_map.keys()) + list(_ppc_map.keys())):
                        _sfpp = _sfpp_map.get(_v)
                        _ppc = _ppc_map.get(_v)
                        if _sfpp and _ppc:
                            _entry[_v] = {"pallet_sf": _sfpp, "container_sf": _sfpp * _ppc}
                        elif _sfpp:
                            _entry[_v] = {"pallet_sf": _sfpp}
                    if _entry:
                        _vendor_capacity_data[_sk] = _entry

                optimization_result = _calculate_niko_vendor_mix(
                    optimizer_df,
                    vendor_names,
                    baseline_payload.get("baselines", {}),
                    vendor_capacity_data=_vendor_capacity_data,
                    sfpp_fallback=_sfpp_fallback,
                )

                if optimization_result["errors"]:
                    for err in optimization_result["errors"]:
                        st.error(err)

                if optimization_result["optimal_assignments"]:
                    st.markdown(
                        _render_niko_optimizer_tables(optimizer_rows, vendor_names, optimization_result),
                        unsafe_allow_html=True,
                    )

                    # ── Export Optimizer to Excel ───────────────────────────
                    _opt_item_costs = {
                        normalize_item_key(sku): val
                        for sku, val in (optimization_result.get("item_costs") or {}).items()
                    }
                    _opt_vendor_summary = optimization_result.get("vendor_summary") or {}
                    _opt_omitted = optimization_result.get("omitted_items") or []
                    _sku_row_map = {normalize_item_key(r["sku"]): r for r in optimizer_rows}

                    if _opt_item_costs:
                        from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
                        from openpyxl.utils import get_column_letter

                        # ── Build items detail rows ─────────────────────────
                        _items_data = []
                        for _nk, _itm in _opt_item_costs.items():
                            _src = _sku_row_map.get(_nk, {})
                            _tl = _itm.get("truck_labels") or []
                            _truck_str = " / ".join(f"T{int(t)}" for t in _tl) if _tl else ""
                            _qty = float(_itm.get("qty", 0.0) or 0.0)
                            _price = float(_itm.get("price", 0.0) or 0.0)
                            _items_data.append({
                                "Item Number":      _src.get("sku", _nk),
                                "Description":      _src.get("description", ""),
                                "Vendor":           _itm.get("vendor", ""),
                                "Truck":            _truck_str,
                                "Quantity (SF)":    _qty,
                                "Material Price/SF": _price,
                                "Landed Cost/SF":   float(_itm.get("landed_cost", 0.0) or 0.0),
                                "Material Total":   _qty * _price,
                                "Landed Total":     float(_itm.get("line_total", 0.0) or 0.0),
                            })
                        # Sort by vendor then truck so groups are together
                        _items_data.sort(key=lambda r: (r["Vendor"], r["Truck"]))

                        # Include omitted items at the bottom
                        for _om in _opt_omitted:
                            _src = _sku_row_map.get(normalize_item_key(_om.get("sku", "")), {})
                            _items_data.append({
                                "Item Number":      _src.get("sku", _om.get("sku", "")),
                                "Description":      _src.get("description", ""),
                                "Vendor":           "(omitted)",
                                "Truck":            "",
                                "Quantity (SF)":    float(_om.get("original_qty", 0.0) or 0.0),
                                "Material Price/SF": "",
                                "Landed Cost/SF":   "",
                                "Material Total":   "",
                                "Landed Total":     "",
                            })

                        _items_df = pd.DataFrame(_items_data)

                        # ── Build vendor summary rows ───────────────────────
                        _summary_data = []
                        for _vname, _vs in _opt_vendor_summary.items():
                            _summary_data.append({
                                "Vendor":           _vname,
                                "Total SF":         float(_vs.get("qty_sf", 0.0) or 0.0),
                                "Added SF":         float(_vs.get("added_qty_sf", 0.0) or 0.0),
                                "Truckloads":       int(_vs.get("truckloads", 0) or 0),
                                "Freight/Truck":    float(_vs.get("freight_per_truck", 0.0) or 0.0),
                                "Total Freight":    float(_vs.get("freight_total", 0.0) or 0.0),
                                "Material Total":   float(_vs.get("material_total", 0.0) or 0.0),
                                "Landed Total":     float(_vs.get("landed_total", 0.0) or 0.0),
                                "Avg Landed/SF":    float(_vs.get("avg_landed_cost", 0.0) or 0.0),
                            })
                        _summary_df = pd.DataFrame(_summary_data) if _summary_data else pd.DataFrame()

                        # ── Shared Excel styles ─────────────────────────────
                        _thin = Border(
                            left=Side(style="thin"), right=Side(style="thin"),
                            top=Side(style="thin"), bottom=Side(style="thin"),
                        )
                        _hdr_font  = Font(bold=True, color="1F4E79")
                        _hdr_fill  = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
                        _omit_font = Font(italic=True, color="888888")

                        def _vendor_fill(vendor_str: str, truck_str: str) -> PatternFill:
                            """Return an openpyxl fill matching the Optimizer table cell color."""
                            _vk = normalize_vendor_name(vendor_str).upper()
                            _shades = NIKO_VENDOR_TRUCK_COLORS.get(_vk)
                            if _shades is None:
                                _off = sum(ord(c) for c in _vk) % len(_NIKO_FALLBACK_VENDOR_COLORS)
                                _shades = _NIKO_FALLBACK_VENDOR_COLORS[_off]
                            # Parse truck number from e.g. "T1" or "T1 / T2"
                            _t_nums = []
                            for _part in str(truck_str).split("/"):
                                _part = _part.strip()
                                if _part.upper().startswith("T") and _part[1:].isdigit():
                                    _t_nums.append(int(_part[1:]))
                            _tnum = min(_t_nums) if _t_nums else 1
                            _hex = _shades[max(0, _tnum - 1) % len(_shades)].lstrip("#")
                            return PatternFill(start_color=_hex, end_color=_hex, fill_type="solid")

                        _excel_buf_opt = io.BytesIO()
                        with pd.ExcelWriter(_excel_buf_opt, engine="openpyxl") as _writer:
                            _items_df.to_excel(_writer, sheet_name="Optimizer", index=False)
                            if not _summary_df.empty:
                                _summary_df.to_excel(_writer, sheet_name="Vendor Summary", index=False)

                            _wb = _writer.book

                            # ── Style: Optimizer sheet ──────────────────────
                            _ws = _writer.sheets["Optimizer"]
                            _opt_col_widths = {
                                "Item Number": 22, "Description": 34, "Vendor": 14,
                                "Truck": 10, "Quantity (SF)": 14, "Material Price/SF": 16,
                                "Landed Cost/SF": 14, "Material Total": 15, "Landed Total": 13,
                            }
                            for _ci, _cn in enumerate(_items_df.columns, start=1):
                                _ws.column_dimensions[get_column_letter(_ci)].width = _opt_col_widths.get(_cn, 13)
                                _cell = _ws.cell(row=1, column=_ci)
                                _cell.font  = _hdr_font
                                _cell.fill  = _hdr_fill
                                _cell.border = _thin

                            for _ri, _row_data in enumerate(_items_data, start=2):
                                _is_omit = _row_data["Vendor"] == "(omitted)"
                                _row_fill = _vendor_fill(_row_data["Vendor"], _row_data["Truck"]) if not _is_omit else None
                                for _ci in range(1, len(_items_df.columns) + 1):
                                    _cell = _ws.cell(row=_ri, column=_ci)
                                    _cell.border = _thin
                                    if _is_omit:
                                        _cell.font = _omit_font
                                    elif _row_fill:
                                        _cell.fill = _row_fill

                            # Number formats for currency / quantity columns
                            _qty_col = list(_items_df.columns).index("Quantity (SF)") + 1
                            _price_col = list(_items_df.columns).index("Material Price/SF") + 1
                            _lc_col = list(_items_df.columns).index("Landed Cost/SF") + 1
                            _mt_col = list(_items_df.columns).index("Material Total") + 1
                            _lt_col = list(_items_df.columns).index("Landed Total") + 1
                            for _ri in range(2, len(_items_data) + 2):
                                _ws.cell(_ri, _qty_col).number_format  = '#,##0'
                                _ws.cell(_ri, _price_col).number_format = '$#,##0.00'
                                _ws.cell(_ri, _lc_col).number_format   = '$#,##0.00'
                                _ws.cell(_ri, _mt_col).number_format   = '$#,##0.00'
                                _ws.cell(_ri, _lt_col).number_format   = '$#,##0.00'

                            # ── Style: Vendor Summary sheet ─────────────────
                            if not _summary_df.empty:
                                _ws2 = _writer.sheets["Vendor Summary"]
                                _sum_col_widths = {
                                    "Vendor": 14, "Total SF": 12, "Added SF": 10,
                                    "Truckloads": 11, "Freight/Truck": 13, "Total Freight": 13,
                                    "Material Total": 15, "Landed Total": 13, "Avg Landed/SF": 14,
                                }
                                for _ci, _cn in enumerate(_summary_df.columns, start=1):
                                    _ws2.column_dimensions[get_column_letter(_ci)].width = _sum_col_widths.get(_cn, 13)
                                    _cell = _ws2.cell(row=1, column=_ci)
                                    _cell.font  = _hdr_font
                                    _cell.fill  = _hdr_fill
                                    _cell.border = _thin
                                for _ri, _sd in enumerate(_summary_data, start=2):
                                    _vfill = _vendor_fill(_sd["Vendor"], "T1")
                                    for _ci in range(1, len(_summary_df.columns) + 1):
                                        _cell = _ws2.cell(row=_ri, column=_ci)
                                        _cell.border = _thin
                                        _cell.fill = _vfill
                                # Currency formats on summary sheet
                                _s_cols = {"Freight/Truck": 5, "Total Freight": 6,
                                           "Material Total": 7, "Landed Total": 8, "Avg Landed/SF": 9}
                                for _cn, _ci in _s_cols.items():
                                    if _cn in _summary_df.columns:
                                        for _ri in range(2, len(_summary_data) + 2):
                                            _ws2.cell(_ri, _ci).number_format = '$#,##0.00'
                                _sf_cols = {"Total SF": 2, "Added SF": 3}
                                for _cn, _ci in _sf_cols.items():
                                    if _cn in _summary_df.columns:
                                        for _ri in range(2, len(_summary_data) + 2):
                                            _ws2.cell(_ri, _ci).number_format = '#,##0'

                        _excel_buf_opt.seek(0)
                        _file_date = pd.Timestamp.today().strftime("%Y-%m-%d")
                        st.download_button(
                            label="Export Optimizer to Excel",
                            data=_excel_buf_opt,
                            file_name=f"optimizer_{_file_date}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    # ── End Export Optimizer ────────────────────────────────

                if optimization_result["cost_alerts"]:
                    alert_rows = []
                    for alert in optimization_result["cost_alerts"]:
                        last_po_cost = alert.get("last_received_po_cost")
                        avg_inventory_cost = alert.get("avg_inventory_cost")
                        reasons = []
                        for comparison in alert.get("comparisons", []):
                            reasons.append(
                                f"{comparison['source']}: {comparison['diff_pct'] * 100:+.1f}% ({comparison['diff_value']:+.2f}/SF)"
                            )
                        alert_rows.append({
                            "SKU": alert["sku"],
                            "Vendor": alert["vendor"],
                            "Proposed Landed Cost": f"${alert['proposed_landed_cost']:.2f}",
                            "Last Received PO": f"${last_po_cost:.2f}" if last_po_cost else "",
                            "Diff vs Last PO": (
                                f"{((alert['proposed_landed_cost'] - last_po_cost) / last_po_cost) * 100:+.1f}%"
                                if last_po_cost else ""
                            ),
                            "Last PO Date": alert.get("last_received_date", "") or "",
                            "Avg Inventory Cost": f"${avg_inventory_cost:.2f}" if avg_inventory_cost else "",
                            "Diff vs Avg Inv": (
                                f"{((alert['proposed_landed_cost'] - avg_inventory_cost) / avg_inventory_cost) * 100:+.1f}%"
                                if avg_inventory_cost else ""
                            ),
                            "Alert Reason": "; ".join(reasons),
                        })
                    if alert_rows:
                        st.markdown('<h3 style="color: #1a1a2e;">Cost Alerts</h3>', unsafe_allow_html=True)
                        st.caption(
                            "Alerts compare proposed landed cost against the last received PO landed cost "
                            "and the average landed cost of current inventory."
                        )
                        alert_df = pd.DataFrame(alert_rows)
                        st.dataframe(alert_df, use_container_width=True, hide_index=True)

            else:
                st.markdown('<div class="queue-empty">No reorder quantities for this month.</div>', unsafe_allow_html=True)

    # OPTIMIZER section - only show for Dilan (Moulding) tab
    if is_moulding:
        moulding_optimizer_rows = []
        if not reorder_now_df.empty:
            source_rows = reorder_now_df[reorder_now_df["Item Number"] != "Total"].copy()
            for _, row in source_rows.iterrows():
                sku = str(row.get("Item Number", "")).strip()
                desc = str(row.get("Description", "")).strip()
                qty_value = None
                for col in (
                    "Quantity Needed",
                    "Reorder Quantity (SF)",
                    "Reorder Quantity",
                    "Reorder Quantity (Pallets)",
                ):
                    if col in source_rows.columns:
                        qty_value = row.get(col)
                        break
                qty_text = ""
                if isinstance(qty_value, (int, float)) and not pd.isna(qty_value):
                    qty_text = _format_number(float(qty_value), 2)
                elif isinstance(qty_value, str):
                    qty_text = qty_value
                moulding_optimizer_rows.append(
                    {
                        "sku": sku,
                        "description": desc,
                        "quantity": qty_text,
                    }
                )

        _jump_anchor("optimizer")
        with st.expander("OPTIMIZER", expanded=True):
            st.markdown('<div class="optimizer-anchor"></div>', unsafe_allow_html=True)

            if moulding_optimizer_rows:
                # Build DataFrame for data_editor
                moulding_optimizer_df_data = []

                # Named vendor columns for Dilan tab
                moulding_vendor_names = ["Dayspring", "GLC", "Royal", "Other"]

                # First row is always "Freight" (for freight cost entry)
                freight_row = {
                    "Include": False,
                    "SKU": "Freight",
                    "Description": "",
                    "Qty Needed": "",
                }
                for vname in moulding_vendor_names:
                    freight_row[vname] = ""
                moulding_optimizer_df_data.append(freight_row)

                # Add item rows
                for row in moulding_optimizer_rows:
                    row_data = {
                        "Include": False,
                        "SKU": row["sku"],
                        "Description": row["description"],
                        "Qty Needed": row["quantity"],
                    }
                    for vname in moulding_vendor_names:
                        row_data[vname] = ""
                    moulding_optimizer_df_data.append(row_data)

                moulding_optimizer_df = pd.DataFrame(moulding_optimizer_df_data)

                # Configure column types for data_editor
                moulding_column_config = {
                    "Include": st.column_config.CheckboxColumn("Include", default=False, width="small"),
                    "SKU": st.column_config.TextColumn("SKU", width="medium", disabled=True),
                    "Description": st.column_config.TextColumn("Description", width="large", disabled=True),
                    "Qty Needed": st.column_config.TextColumn("Qty Needed", width="small", disabled=True),
                }
                for vname in moulding_vendor_names:
                    moulding_column_config[vname] = st.column_config.NumberColumn(vname, width="small", format="%.2f")

                # Calculate height based on number of rows
                num_rows = len(moulding_optimizer_df)
                calculated_height = (num_rows * 35) + 35 + 10

                # Load margins data for landed cost comparison
                moulding_margins_data = _load_moulding_margins()

                # Centered "Optimize" button above the data editor
                col1, col2, col3 = st.columns([1, 1, 1])
                with col2:
                    moulding_optimize_clicked = st.button("🎯 Optimize Vendor Mix", key="moulding_optimize_vendor_mix_btn", width='stretch')

                # Use data_editor for efficient tabular editing
                edited_moulding_optimizer_df = st.data_editor(
                    moulding_optimizer_df,
                    column_config=moulding_column_config,
                    width='stretch',
                    hide_index=True,
                    num_rows="fixed",
                    key="moulding_optimizer_data_editor",
                    height=calculated_height,
                )

                # Store edited data in session state
                st.session_state["moulding_optimizer_edited_data"] = edited_moulding_optimizer_df

                # Run optimization when button clicked
                if moulding_optimize_clicked:
                    moulding_optimization_result = _calculate_optimal_vendor_mix(
                        edited_moulding_optimizer_df,
                        moulding_margins_data,
                        moulding_vendor_names,
                    )

                    # Display errors if any
                    if moulding_optimization_result["errors"]:
                        for err in moulding_optimization_result["errors"]:
                            st.error(f"⚠️ {err}")

                    # Display optimal vendor assignments
                    if moulding_optimization_result["optimal_assignments"]:
                        st.markdown('<h3 style="color: #1a1a2e;">Optimal Vendor Mix</h3>', unsafe_allow_html=True)

                        # Build results table
                        moulding_results_data = []
                        for sku, cost_info in moulding_optimization_result["item_costs"].items():
                            moulding_results_data.append({
                                "SKU": sku,
                                "Recommended Vendor": cost_info["vendor"],
                                "Material Price": f"${cost_info['price']:.2f}",
                                "Freight/LF": f"${cost_info['freight_share']:.4f}" if cost_info['freight_share'] > 0 else "Included",
                                "Landed Cost": f"${cost_info['landed_cost']:.2f}",
                                "Qty (LF)": f"{cost_info['qty']:,.0f}",
                                "Total Cost": f"${cost_info['landed_cost'] * cost_info['qty']:,.2f}",
                            })

                        if moulding_results_data:
                            moulding_results_df = pd.DataFrame(moulding_results_data)
                            st.dataframe(moulding_results_df, use_container_width=True, hide_index=True)

                            # Show total cost
                            st.markdown(f'<p style="color: #1a1a2e;"><strong>Total Order Cost: ${moulding_optimization_result["total_cost"]:,.2f}</strong></p>', unsafe_allow_html=True)

                    # Display margin alerts
                    if moulding_optimization_result["margin_alerts"]:
                        st.markdown('<h3 style="color: #1a1a2e;">⚠️ Margin Alerts</h3>', unsafe_allow_html=True)
                        st.markdown('<p style="color: #1a1a2e;">The following items have proposed landed costs that differ significantly from current inventory:</p>', unsafe_allow_html=True)

                        for alert in moulding_optimization_result["margin_alerts"]:
                            diff_pct = alert["margin_diff"] * 100
                            direction = "higher" if diff_pct > 0 else "lower"
                            color = "green" if diff_pct > 0 else "red"

                            st.markdown(f"""
<div style="border: 2px solid {color}; padding: 10px; margin: 5px 0; border-radius: 5px; color: #1a1a2e;">
<strong>{alert['sku']}</strong> from <strong>{alert['vendor']}</strong><br>
Sale Price: ${alert['sale_price']:.2f}<br>
Current Landed Cost: ${alert['current_landed_cost']:.2f} (Margin: {alert['current_margin']*100:.1f}%)<br>
Proposed Landed Cost: ${alert['proposed_landed_cost']:.2f} (Margin: {alert['proposed_margin']*100:.1f}%)<br>
<strong style="color: {color};">Margin is {abs(diff_pct):.1f}% {direction} than current inventory</strong>
</div>
""", unsafe_allow_html=True)

            else:
                st.markdown('<div class="queue-empty">No reorder quantities for this month.</div>', unsafe_allow_html=True)

    # Monthly detail / History and Margins section
    detail_items = [selected_item] if selected_item else vendor_items
    if is_sundries:
        # ── HISTORY AND MARGINS (Carlos tab) ────────────────────────────────
        _jump_anchor("history-margins")
        with st.expander("HISTORY AND MARGINS", expanded=True):
            _hm_items = detail_items if detail_items else vendor_items
            _hm_skus = tuple(sorted({
                str(item.get("item_number", "")).strip().upper()
                for item in _hm_items
                if item.get("item_number")
            }))
            _hm_db = _load_carlos_history_margins(_hm_skus) if _hm_skus else pd.DataFrame()
            _hm_cost_map: Dict[str, Any] = {}
            if not _hm_db.empty:
                for _, _r in _hm_db.iterrows():
                    _inum = str(_r.get("ITEM_NUMBER", "")).strip().upper()
                    if _inum:
                        _hm_cost_map[_inum] = _r
            _hm_rows = []
            for _item in _hm_items:
                _inum = str(_item.get("item_number", "")).strip().upper()
                if not _inum:
                    continue
                _db = _hm_cost_map.get(_inum)
                _sale  = float(_db.get("SALE_PRICE")   or 0) if _db is not None else 0.0
                _acost = float(_db.get("AVG_INV_COST")  or 0) if _db is not None else 0.0
                _pcost = float(_db.get("LAST_PO_COST")  or 0) if _db is not None else 0.0
                _cost_30d = float(_db.get("COST_30D_AGO") or 0) if _db is not None else 0.0
                _pdate = _db.get("LAST_PO_DATE")             if _db is not None else None
                _margin_avg = (_sale - _acost) / _sale if _sale > 0 and _acost > 0 else 0.0
                _margin_po  = (_sale - _pcost) / _sale if _sale > 0 and _pcost > 0 else 0.0
                _margin_30d = (_sale - _cost_30d) / _sale if _sale > 0 and _cost_30d > 0 else 0.0
                # Match existing margin-alert convention: show margin movement in percentage points.
                _margin_30d_chg = _margin_point_change(_margin_avg, _margin_30d)
                _margin_po_chg = _margin_point_change(_margin_avg, _margin_po)
                _hm_rows.append({
                    "Item Number":      _inum,
                    "Description":      str(_item.get("description", "")),
                    "Vendor Name":      str(_item.get("vendor_name", "")),
                    "Selling Price":    _sale,
                    "Cost (Avg Inv)":   _acost,
                    "Margin (Avg Inv)": _margin_avg,
                    "Margin Chg 30D":   _margin_30d_chg,
                    "Cost (Last PO)":   _pcost,
                    "Margin (Last PO)": _margin_po,
                    "Margin Chg Last PO": _margin_po_chg,
                    "Last PO Date":     _pdate,
                })
            _hm_df = pd.DataFrame(_hm_rows) if _hm_rows else pd.DataFrame()
            if not _hm_df.empty:
                _hm_cols = list(_hm_df.columns)
                _hm_default = "Item Number" if "Item Number" in _hm_cols else _hm_cols[0]
                _hm_idx = _hm_cols.index(_hm_default) if _hm_default in _hm_cols else 0
                _hmsc1, _hmsc2, _hmsc3 = st.columns([0.08, 0.28, 0.64])
                with _hmsc1:
                    st.caption("Sort by")
                with _hmsc2:
                    _hm_sort_col = st.selectbox(
                        "history_margins_sort_col",
                        _hm_cols,
                        index=_hm_idx,
                        key="history_margins_sort_col",
                        label_visibility="collapsed",
                    )
                with _hmsc3:
                    _hm_sort_asc = st.radio(
                        "history_margins_sort_dir",
                        ["Asc", "Desc"],
                        index=0,
                        horizontal=True,
                        key="history_margins_sort_dir",
                        label_visibility="collapsed",
                    ) == "Asc"
                _hm_df = _sort_history_margins_df(_hm_df, _hm_sort_col, _hm_sort_asc)
                try:
                    from openpyxl.styles import Font, PatternFill, Border, Side
                    from openpyxl.utils import get_column_letter

                    _hm_buf = io.BytesIO()
                    _hm_export = _hm_df.copy()
                    _currency_cols = {"Selling Price", "Cost (Avg Inv)", "Cost (Last PO)"}
                    _margin_cols = {"Margin (Avg Inv)", "Margin (Last PO)"}
                    _change_cols = {"Margin Chg 30D", "Margin Chg Last PO"}
                    for _col in _currency_cols | _margin_cols | _change_cols:
                        if _col in _hm_export.columns:
                            _hm_export[_col] = pd.to_numeric(_hm_export[_col], errors="coerce")
                    for _col in _currency_cols | _margin_cols:
                        if _col in _hm_export.columns:
                            _hm_export.loc[_hm_export[_col].abs() < 1e-9, _col] = np.nan
                    if "Last PO Date" in _hm_export.columns:
                        _hm_export["Last PO Date"] = pd.to_datetime(_hm_export["Last PO Date"], errors="coerce")

                    with pd.ExcelWriter(_hm_buf, engine="openpyxl") as _hm_writer:
                        _hm_export.to_excel(_hm_writer, sheet_name="History and Margins", index=False)
                        _hm_ws = _hm_writer.sheets["History and Margins"]
                        _hm_thin = Border(
                            left=Side(style="thin"), right=Side(style="thin"),
                            top=Side(style="thin"), bottom=Side(style="thin"),
                        )
                        _hm_hfont = Font(bold=True, color="1F4E79")
                        _hm_hfill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
                        _hm_widths = {
                            "Item Number": 16,
                            "Description": 36,
                            "Vendor Name": 24,
                            "Selling Price": 14,
                            "Cost (Avg Inv)": 14,
                            "Margin (Avg Inv)": 15,
                            "Margin Chg 30D": 15,
                            "Cost (Last PO)": 14,
                            "Margin (Last PO)": 15,
                            "Margin Chg Last PO": 18,
                            "Last PO Date": 14,
                        }
                        for _ci, _cn in enumerate(_hm_export.columns, start=1):
                            _hm_col_letter = get_column_letter(_ci)
                            _hm_ws.column_dimensions[_hm_col_letter].width = _hm_widths.get(_cn, 14)
                            _cell = _hm_ws.cell(row=1, column=_ci)
                            _cell.font = _hm_hfont
                            _cell.fill = _hm_hfill
                            _cell.border = _hm_thin
                        for _ri in range(2, len(_hm_export) + 2):
                            for _ci, _cn in enumerate(_hm_export.columns, start=1):
                                _cell = _hm_ws.cell(row=_ri, column=_ci)
                                _cell.border = _hm_thin
                                if _cn in _currency_cols:
                                    _cell.number_format = '$#,##0.00'
                                elif _cn in _margin_cols or _cn in _change_cols:
                                    _cell.number_format = '0.0%'
                                elif _cn == "Last PO Date":
                                    _cell.number_format = 'mm/dd/yyyy'
                    _hm_buf.seek(0)
                    st.download_button(
                        label="Export History and Margins to Excel",
                        data=_hm_buf,
                        file_name=f"history_and_margins_{pd.Timestamp.today().strftime('%Y-%m-%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="history_margins_export_sundries",
                    )
                except Exception:
                    pass
            st.markdown(
                _render_history_margins_table_html(
                    _hm_df,
                    sort_col=_hm_sort_col if not _hm_df.empty else None,
                    sort_ascending=_hm_sort_asc if not _hm_df.empty else True,
                ),
                unsafe_allow_html=True,
            )
    elif detail_items:
        # ── REORDER SCHEDULE (non-Carlos tabs) ──────────────────────────────
        _jump_anchor("reorder-schedule")
        with st.expander("REORDER SCHEDULE", expanded=True):
            df_monthly_all = pd.DataFrame(monthly_rows)
            for item in detail_items:
                if not item:
                    continue
                sku_label = item.get("item_number", "")
                desc_label = item.get("description", "")
                df_monthly = df_monthly_all.copy()
                if not df_monthly.empty and "SKU" in df_monthly.columns:
                    df_monthly = df_monthly[df_monthly["SKU"].astype(str) == str(sku_label)]
                # Filter out HIST rows - only show CATCHUP and FCST rows with forecast data
                if not df_monthly.empty and "Row_Type" in df_monthly.columns:
                    df_monthly = df_monthly[df_monthly["Row_Type"].astype(str).str.upper().isin(["FCST", "CATCHUP"])]
                if not df_monthly.empty and "Month" in df_monthly.columns:
                    df_monthly["Month"] = pd.to_datetime(df_monthly["Month"], errors="coerce")
                    df_monthly = df_monthly.sort_values("Month")
                    df_monthly["Month"] = df_monthly["Month"].dt.strftime("%m/%d/%Y")
                col_map = {
                    "Month": "Month",
                    "Beginning Inventory": "Beginning Inventory",
                    "Forecast": "Forecast",
                    "Expected Arrivals": "Expected Arrivals",
                    "Order Quantity": "Reorder Quantity",
                    "Ending Inventory": "Ending Inventory",
                }
                existing = [col for col in col_map if col in df_monthly.columns]
                if existing:
                    out = df_monthly[existing].rename(columns=col_map)
                    if "Beginning Inventory" in out.columns:
                        out["Beginning Inventory"] = pd.to_numeric(out["Beginning Inventory"], errors="coerce").fillna(0.0)
                    if "Ending Inventory" in out.columns:
                        out["Ending Inventory"] = pd.to_numeric(out["Ending Inventory"], errors="coerce").fillna(0.0)
                    st.markdown(
                        _render_reorder_table_html(out, f"Reorder Schedule:   {sku_label} {desc_label}"),
                        unsafe_allow_html=True,
                    )

    # Removed metric/segmentation sections below Purchasing Queue per request.
if __name__ == "__main__":
    if _is_streamlit_runtime():
        render_webapp()
        raise SystemExit
    print("="*70)
    print("INVENTORY PLANNING - DEMAND-AWARE FORECASTING")
    print("="*70)
    print(f"Mode: {'Single SKU - ' + FOCUS_SKU if FOCUS_SKU else 'All Active SKUs'}")
    print(f"Date Filter: Only SKUs with sales since {CUTOFF_DATE}")
    print(f"Data Aggregation: WEEKLY (reduces intermittency, improves accuracy)")
    print(f"Historical Data: Each SKU uses data from its first sale month forward")
    print(f"\nService Level: {SERVICE_LEVEL*100:.0f}%")
    print(f"\nService Level: {SERVICE_LEVEL*100:.0f}%")
    print(f"Safety Stock Cap: Maximum 3 months of average demand")
    print(f"\nForecast Method Selection:")
    print(f"  ALL 9+ models tested per SKU (Neural Network, XGBoost, Random Forest,")
    print(f"  Holt Damped, SARIMA, TSB, SBA, Croston, Hurdle, + Global models)")
    print(f"  Best method selected using composite score (35% wMAPE, 20% MASE, 20% RMSSE, 15% UF, 10% Bias)")
    print(f"\nDemand Classification (ADI/CV^2): SMOOTH, INTERMITTENT, ERRATIC, LUMPY")
    print(f"\nABC Uplift Scalars: A={SS_UPLIFT_SCALAR['A']:.1f}, B={SS_UPLIFT_SCALAR['B']:.1f}, C={SS_UPLIFT_SCALAR['C']:.1f}")
    print(f"Global Uplift Budget: {GLOBAL_UPLIFT_BUDGET_PCT*100:.0f}% of trailing 12m volume")
    
    if ENABLE_GLOBAL_INV_CAP:
        print(f"\n{'='*70}")
        print("GLOBAL INVENTORY CAP ENABLED")
        print(f"{'='*70}")
        print(f"  Max Total Ending Inventory: {GLOBAL_INV_CAP_SF:,.0f} SF")
        print(f"  ABC MOI Caps: A={ABC_MOI_CAPS['A']:.1f}, B={ABC_MOI_CAPS['B']:.1f}, C={ABC_MOI_CAPS['C']:.1f} months")
        print(f"  Strategy: Min (lead time demand) + Flex (scaled to fit capacity)")
    
    print("="*70)
    run_inventory_planning()
