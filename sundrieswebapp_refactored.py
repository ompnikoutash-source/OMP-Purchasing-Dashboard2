"""
Sundries Inventory Planning Web Application (Refactored)

This version uses shared modules from core/ for database, forecasting,
and inventory management functions.

Designed for sundries buyers to manage non-flooring items (EA, BOX, CASE, etc.)
"""

from __future__ import annotations
import json
import os
import warnings
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Suppress all warnings BEFORE importing numpy/pandas/sklearn
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", module="sklearn")
warnings.filterwarnings("ignore", module="statsmodels")
warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable",
    category=UserWarning,
)
os.environ['PYTHONWARNINGS'] = 'ignore::UserWarning'
os.environ['LOKY_MAX_CPU_COUNT'] = '4'

import numpy as np
import pandas as pd

# Fix Windows console encoding for unicode characters
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# Import shared modules
from core.db_connection import connect, _resolve_as_of_date, sql_escape
from core.config import (
    FUTURE_FORECAST_DAYS,
    FUTURE_FORECAST_WEEKS,
    DAYS_PER_WEEK,
    DAYS_PER_MONTH,
    WEEKS_PER_MONTH,
    SERVICE_LEVEL,
    Z_SCORE,
    CUTOFF_DATE,
    ABC_BREAK_A_DEFAULT as ABC_BREAK_A,
    ABC_BREAK_B_DEFAULT as ABC_BREAK_B,
    ABC_MOI_CAPS,
    SS_UPLIFT_SCALAR,
    LUMPY_PCTL_NONZERO,
    LUMPY_LT_BUFFER_FRAC,
    ENABLE_GLOBAL_UPLIFT_BUDGET,
    GLOBAL_UPLIFT_BUDGET_PCT,
    ENABLE_GLOBAL_INV_CAP,
    GLOBAL_INV_CAP_UNITS,
    COVERAGE_HORIZON_DEFAULT,
    MIN_LEAD_TIME_DAYS,
)
from core.data_loader import (
    load_sku_list_from_excel,
    load_trailing_12m_volume,
    fetch_sales_history,
    build_in_list,
)
from core.forecasting import (
    aggregate_to_weekly,
    build_rolling_origin_splits,
    classify_demand_pattern,
    cap_outliers,
    wmape,
    forecast_holt_damped,
    forecast_croston,
    forecast_tsb,
    forecast_sba,
    forecast_prophet,
    is_prophet_available,
    forecast_random_forest,
    forecast_xgboost,
    forecast_random_forest_full,
    forecast_xgboost_full,
    build_features,
    validate_forecast_reasonableness,
    select_best_forecast,
    compute_safety_stock,
    compute_reorder_metrics,
)
from core.inventory import (
    compute_abc_classification,
    simulate_monthly_projection,
)

# ============================================================
# SUNDRIES-SPECIFIC CONFIGURATION
# ============================================================
FOCUS_SKU = ""  # Leave blank "" to process all SKUs
USE_GLOBAL_MODEL = True
SHAREPOINT_DIR = Path(r"C:\Users\niko\OneDrive - Old Master Products\Purchasing - Flooring Reports\Dashboard Files")
MOULDING_LIST_FILE = SHAREPOINT_DIR / "MouldingSKUList.xlsx"
LEAD_TIMES_FILE = SHAREPOINT_DIR / "Lead Times.xlsx"
WEBAPP_JSON_PATH = Path(__file__).resolve().parent / "sundrieswebappJSON"
PROGRESS_FILE_PATH = Path(__file__).resolve().parent / ".sundries_forecast_progress.json"
UI_BUILD = "2026-01-23-sundries-refactored"

# ABC-tiered coverage horizons: A items (high volume) get largest coverage, C items smallest
COVERAGE_HORIZON_DAYS = {"A": 90, "B": 60, "C": 60}

AS_OF_DATE: Optional[pd.Timestamp] = None
AS_OF_DATE_OVERRIDE = ""


# ============================================================
# SUNDRIES-SPECIFIC DATA LOADING
# ============================================================
def discover_active_sundries_skus(conn, days: int = 365, as_of_date: Optional[pd.Timestamp] = None) -> set:
    """
    Discover sundries SKUs directly from the database.
    Finds all distinct items with non-SF sales in the last N days, then
    subtracts any item also present in the moulding list so moulding items
    don't bleed through.
    """
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    cutoff = (as_of_date - pd.Timedelta(days=days)).date()

    print(f"\nDiscovering active sundries SKUs with sales since {cutoff}...")
    query = """
    SELECT DISTINCT TRIM(L.SLITEM) AS SKU
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC
     AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    JOIN GSFL2K.ITEMMAST M ON M.IMITEM = L.SLITEM
    WHERE H.SHIDAT >= ?
      AND TRIM(L.SLUM2) NOT LIKE '%SF%'
      AND COALESCE(L.SLBLUO, 0) > 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
      AND M.IMDIV IN (2, 3)
    """
    df = pd.read_sql(query, conn, params=[cutoff])
    discovered = set(df['SKU'].str.strip().str.upper())
    print(f"  Found {len(discovered)} non-flooring SKUs with sales in last {days} days")

    # Exclude moulding items so only true sundries remain
    if MOULDING_LIST_FILE.exists():
        moulding_skus = load_sku_list_from_excel(MOULDING_LIST_FILE, product_type="moulding exclusion")
        before = len(discovered)
        discovered -= moulding_skus
        print(f"  Excluded {before - len(discovered)} moulding items → {len(discovered)} sundries SKUs")
    else:
        print(f"  WARNING: Moulding list not found at {MOULDING_LIST_FILE} — skipping moulding exclusion")

    return discovered


def _impute_lead_time_days(df: pd.DataFrame, default_days: float = 30.0) -> pd.DataFrame:
    """
    Replace hard default lead times with hierarchical fallback:
    SKU value -> vendor median -> global median -> default.
    """
    if df.empty or "lead_time" not in df.columns:
        return df

    lt_raw = pd.to_numeric(df["lead_time"], errors="coerce")
    vendor_key = df["vendor_number"].astype(str).str.strip().str.upper()
    valid = lt_raw.notna() & (lt_raw > 0)
    # Treat ~30-day values as default-like placeholders when better evidence exists.
    non_default = valid & (~lt_raw.between(default_days - 0.5, default_days + 0.5))

    vendor_medians = lt_raw[non_default].groupby(vendor_key[non_default]).median()
    if non_default.any():
        global_median = float(lt_raw[non_default].median())
    elif valid.any():
        global_median = float(lt_raw[valid].median())
    else:
        global_median = float(default_days)

    resolved_vals: List[float] = []
    resolved_src: List[str] = []
    for lt, vendor in zip(lt_raw, vendor_key):
        if np.isfinite(lt) and lt > 0 and not (
            (default_days - 0.5) <= lt <= (default_days + 0.5) and vendor in vendor_medians.index
        ):
            chosen = float(lt)
            source = "MASTER"
        elif vendor in vendor_medians.index and np.isfinite(vendor_medians[vendor]):
            chosen = float(vendor_medians[vendor])
            source = "VENDOR_MEDIAN"
        elif np.isfinite(global_median) and global_median > 0:
            chosen = float(global_median)
            source = "GLOBAL_MEDIAN"
        elif np.isfinite(lt) and lt > 0:
            chosen = float(lt)
            source = "MASTER"
        else:
            chosen = float(default_days)
            source = "DEFAULT_30D"

        resolved_vals.append(max(float(MIN_LEAD_TIME_DAYS), chosen))
        resolved_src.append(source)

    df["lead_time"] = resolved_vals
    df["lead_time_source"] = resolved_src
    return df


def _get_first_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _load_excel_lead_times(xlsx_path: Path) -> Dict[str, float]:
    """Load per-SKU FINAL lead times from Lead Times.xlsx."""
    if not xlsx_path.exists():
        fallback = Path(__file__).resolve().parent / "Lead Times.xlsx"
        xlsx_path = fallback if fallback.exists() else xlsx_path
    if not xlsx_path.exists():
        return {}
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Final", header=0)
        sku_col = _get_first_column(df, ["SKU", "Item Number", "ITEM_NUMBER"])
        lt_col = _get_first_column(df, ["FINAL", "Final", "final"])
        if not sku_col or not lt_col:
            return {}
        work = df[[sku_col, lt_col]].copy()
        work[sku_col] = work[sku_col].astype(str).str.strip().str.upper()
        work[lt_col] = pd.to_numeric(work[lt_col], errors="coerce")
        work = work.dropna(subset=[sku_col, lt_col])
        work = work[(work[sku_col] != "") & (work[lt_col] > 0)]
        return dict(zip(work[sku_col], work[lt_col].astype(float)))
    except Exception as exc:
        print(f"  WARNING: Could not load lead times from {xlsx_path}: {exc}")
        return {}


def _apply_excel_lead_times(df: pd.DataFrame, xlsx_path: Path = LEAD_TIMES_FILE) -> pd.DataFrame:
    """Override sundries master lead times with Lead Times.xlsx Final values."""
    if df.empty or "sku" not in df.columns:
        return df
    lead_times = _load_excel_lead_times(xlsx_path)
    if not lead_times:
        return df

    out = df.copy()
    sku_key = out["sku"].astype(str).str.strip().str.upper()
    excel_lt = sku_key.map(lead_times)
    mask = excel_lt.notna()
    if mask.any():
        out.loc[mask, "lead_time"] = excel_lt[mask].astype(float)
        out.loc[mask, "lead_time_source"] = "Lead Times.xlsx:Final"
        print(f"  Applied Lead Times.xlsx FINAL overrides to {int(mask.sum())} sundries SKU(s)")
    return out


def _filter_skus_by_last_received(
    conn,
    sku_list: set,
    days: int = 365,
    as_of_date: Optional[pd.Timestamp] = None,
) -> Tuple[set, Dict[str, pd.Timestamp]]:
    """Return only SKUs that have a PO receipt recorded within the last N days.

    Uses GSFL2K.ITEMRECH (IRSRC='P' = purchase receipt, IRQTY>0 = actual receipt).
    SKUs with no receipt row at all are treated as never received and are dropped.
    """
    if not sku_list:
        return set(), {}

    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    cutoff = as_of_date - pd.Timedelta(days=days)

    sku_list_clean = [s.strip().upper() for s in sku_list]
    sku_list_str = "'" + "','".join(sku_list_clean) + "'"

    sql = f"""
    SELECT TRIM(IRITEM) AS ITEM_NUMBER, MAX(IRDATE) AS LAST_RECEIVED_DATE
    FROM GSFL2K.ITEMRECH
    WHERE IRCO = 1
      AND TRIM(IRSRC) = 'P'
      AND IRQTY > 0
      AND TRIM(IRITEM) IN ({sku_list_str})
    GROUP BY TRIM(IRITEM)
    """

    df = pd.read_sql(sql, conn)
    if df.empty:
        return set(), {}

    last_received_map: Dict[str, pd.Timestamp] = {}
    for _, row in df.iterrows():
        sku = str(row["ITEM_NUMBER"]).strip().upper()
        dt = row["LAST_RECEIVED_DATE"]
        if pd.notna(dt):
            last_received_map[sku] = pd.Timestamp(dt)

    active = {sku for sku, dt in last_received_map.items() if dt >= cutoff}
    return active, last_received_map


def _fetch_sku_master(conn, focus_sku: str = "", cutoff_date_str: str = CUTOFF_DATE,
                      sundries_sku_list: set = None) -> pd.DataFrame:
    """Fetch sundries SKU master data from database."""
    print("\nFetching sundries SKU master...")
    if not sundries_sku_list or len(sundries_sku_list) == 0:
        raise ValueError("Sundries SKU list is required but was not provided or is empty")

    if focus_sku:
        sku_filter = f"AND TRIM(M.IMITEM) = '{focus_sku.strip().upper()}'"
        lpv_filter = f"AND TRIM(L.PLITEM) = '{focus_sku.strip().upper()}'"
        print(f"  Filtering for single SKU: {focus_sku}")
    else:
        sku_list_clean = [s.strip().upper() for s in sundries_sku_list]
        sku_list_str = "'" + "','".join(sku_list_clean) + "'"
        sku_filter = f"AND TRIM(M.IMITEM) IN ({sku_list_str})"
        lpv_filter = f"AND TRIM(L.PLITEM) IN ({sku_list_str})"
        print(f"  Filtering for {len(sundries_sku_list)} SKUs from Sundries List")

    query = f"""
    WITH
    PO_MAX_DATE AS (
      SELECT TRIM(L.PLITEM) AS ITEM_NUMBER, MAX(H.PHDOI) AS LATEST_DATE
      FROM GSFL2K.POLINE L
      JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
      WHERE TRIM(H.PHVEND) <> ''
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
      GROUP BY PMD.ITEM_NUMBER
    ),
    INV AS (
      SELECT B.IBITEM, SUM(B.IBQOH) AS QTY_ON_HAND, SUM(B.IBQOO) AS QTY_COMMITTED
      FROM GSFL2K.ITEMBAL B WHERE B.IBLOC NOT IN (90, 17, 41, 46) GROUP BY B.IBITEM
    ),
    PO AS (
      SELECT PLITEM, SUM(PLBLUO) AS ON_PO_QTY FROM GSFL2K.POLINE WHERE PLDELT LIKE '%A%' GROUP BY PLITEM
    ),
    BO AS (
      SELECT OLITEM, SUM(OLBLUB) AS BO_QTY FROM GSFL2K.OOLINE
      WHERE OLCUST NOT LIKE '%TRANSFER%' AND OLCUST NOT LIKE '%OMP000%' AND OLCUST NOT LIKE '%INV000%'
        AND OLCUST NOT LIKE '%OLD001%' AND OLLOC <> 90 AND OLBO LIKE 'Y' GROUP BY OLITEM
    )
    SELECT
      TRIM(M.IMITEM) AS ITEM_NUMBER, TRIM(M.IMDESC) AS DESCRIPTION, TRIM(M.IMUM2) AS UNIT_OF_MEASURE,
      COALESCE(TRIM(LPV.VENDOR_NUMBER), TRIM(M.IMVEND)) AS VENDOR_NUMBER,
      TRIM(V.VMNAME) AS VENDOR_NAME, TRIM(X.IMCOLLECT) AS COLLECTION,
      (
        CASE
          WHEN COALESCE(M.IMFACT, 0) = 0 OR TRIM(M.IMUM1) = TRIM(M.IMUM2)
            THEN (COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0))
          ELSE (COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0)) * M.IMFACT
        END
      ) AS AVAILABLE_QTY,
      COALESCE(PO.ON_PO_QTY, 0) AS ON_PO_QTY, COALESCE(BO.BO_QTY, 0) AS BACKORDER_QTY,
      M.IMLT AS LEAD_TIME_IMLT, TRIM(M.IMSKEY) AS VENDOR_PART_NUMBER,
      COALESCE(M.IMORDQ, 0) AS MIN_ORDER_QTY
    FROM GSFL2K.ITEMMAST M
    LEFT JOIN LATEST_PO_VENDOR LPV ON LPV.ITEM_NUMBER = TRIM(M.IMITEM)
    LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
    LEFT JOIN GSFL2K.VENDMAST V ON TRIM(V.VMVEND) = COALESCE(TRIM(LPV.VENDOR_NUMBER), TRIM(M.IMVEND))
    LEFT JOIN INV ON INV.IBITEM = M.IMITEM
    LEFT JOIN PO ON PO.PLITEM = M.IMITEM
    LEFT JOIN BO ON BO.OLITEM = M.IMITEM
    WHERE M.IMUM2 NOT LIKE '%SF%'
      AND TRIM(M.IMSI) = 'Y'
      AND COALESCE(TRIM(M.IMDROP), '') = ''
      AND TRIM(M.IMDELT) = 'A'
      AND M.IMDIV IN (2, 3)
      {sku_filter}
    ORDER BY TRIM(M.IMITEM)
    """

    df = pd.read_sql(query, conn)
    if df.empty:
        print("  No sundries SKUs found matching criteria")
        return pd.DataFrame()

    df = df.rename(columns={
        'ITEM_NUMBER': 'sku', 'UNIT_OF_MEASURE': 'uom', 'VENDOR_NUMBER': 'vendor_number',
        'VENDOR_NAME': 'vendor_name', 'COLLECTION': 'collection', 'AVAILABLE_QTY': 'available',
        'ON_PO_QTY': 'on_order', 'BACKORDER_QTY': 'backorder', 'LEAD_TIME_IMLT': 'lead_time',
        'VENDOR_PART_NUMBER': 'vendor_part_number', 'MIN_ORDER_QTY': 'min_order_qty',
    })

    df['inventory_position'] = df['available'] + df['on_order'] - df['backorder']
    df['sku'] = df['sku'].astype(str).str.strip().str.upper()
    df['available'] = pd.to_numeric(df['available'], errors='coerce').fillna(0.0)
    df['on_order'] = pd.to_numeric(df['on_order'], errors='coerce').fillna(0.0)
    df['backorder'] = pd.to_numeric(df['backorder'], errors='coerce').fillna(0.0)
    df['inventory_position'] = pd.to_numeric(df['inventory_position'], errors='coerce').fillna(0.0)
    df['min_order_qty'] = pd.to_numeric(df['min_order_qty'], errors='coerce').fillna(0.0)
    df = _impute_lead_time_days(df, default_days=30.0)
    df = _apply_excel_lead_times(df)

    print(f"  Loaded {len(df)} sundries SKUs from master data")
    return df


def _fetch_sales_history(conn, sku: str, cutoff_date_str: str = CUTOFF_DATE) -> pd.DataFrame:
    """Fetch sales history for a single SKU (sundries-specific)."""
    return fetch_sales_history(conn, sku, cutoff_date_str, exclude_sf=False)


def load_arrivals(conn, sku_list: List[str]) -> pd.DataFrame:
    """Load open PO arrivals with container notes for sundries."""
    if not sku_list:
        return pd.DataFrame()

    placeholders, params = build_in_list(sku_list)

    sql = f"""
    SELECT
      TRIM(L.PLPO#) AS PO_NUMBER,
      TRIM(L.PLITEM) AS ITEM_NUMBER,
      TRIM(L.PLDESC) AS DESCRIPTION,
      COALESCE(L.PLBLUO, 0) AS QUANTITY_SF,
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
      AND TRIM(L.PLPO#) <> ''
      AND TRIM(L.PLITEM) <> ''
      AND TRIM(L.PLITEM) IN ({placeholders})
    GROUP BY
      TRIM(L.PLPO#),
      TRIM(L.PLITEM),
      TRIM(L.PLDESC),
      COALESCE(L.PLBLUO, 0),
      TRIM(H.PHVEND),
      TRIM(V.VMNAME),
      TRIM(X.IMCOLLECT),
      L.PLPDAT,
      L.PLDDAT,
      H.PHDOI,
      H.PHSDAT
    """

    df = pd.read_sql_query(sql, conn, params=params)
    df["CONTAINER"] = df["PTCMT1"].fillna("").astype(str).str.strip()
    df["CONTAINER"] = np.where(
        df["CONTAINER"] != "",
        df["CONTAINER"],
        df["PTCMT2"].fillna("").astype(str).str.strip(),
    )
    df["CONTAINER"] = df["CONTAINER"].replace("", np.nan)
    return df


def load_trailing_12m_volume_sundries(conn, sku_list: List[str],
                                       as_of_date: Optional[pd.Timestamp] = None) -> Tuple[Dict[str, float], float]:
    """Load trailing 12-month volume for sundries."""
    return load_trailing_12m_volume(conn, sku_list, exclude_sf=False, as_of_date=as_of_date)


# ============================================================
# FORECASTING HELPERS
# ============================================================
def _cold_start_weekly_forecast(y_values: np.ndarray, h_future: int) -> Tuple[np.ndarray, str, float, Dict[str, float]]:
    """
    Conservative fallback for sparse history.

    Keeps forecasts intentionally modest to avoid runaway recommendations.
    """
    y = np.asarray(y_values, dtype=float)
    if y.size == 0:
        level = 0.0
        method = "COLD_START_ZERO"
    else:
        recent = y[-min(4, len(y)):]
        if float(np.sum(recent)) == 0.0:
            level = 0.0
            method = "COLD_START_RECENT_ZERO"
        else:
            nonzero = y[y > 0]
            overall_mean = float(np.mean(y))
            nonzero_mean = float(np.mean(nonzero)) if len(nonzero) > 0 else overall_mean
            p_nonzero = float(np.mean(y > 0)) if len(y) > 0 else 0.0
            conservative_mean = min(overall_mean, nonzero_mean * p_nonzero)
            level = min(conservative_mean, float(np.max(recent)))
            method = "COLD_START_CONSERVATIVE_MEAN"

    fc_future = np.full(h_future, max(level, 0.0), dtype=float)
    nan = float("nan")
    metrics = {
        "wape": nan,
        "mase": nan,
        "rmsse": nan,
        "bias_pct": nan,
        "uf_share": nan,
        "composite_score": 0.0,
    }
    return fc_future, method, nan, metrics


def _build_methods_with_rolling_backtest(
    df_weekly: pd.DataFrame,
    demand_class: str,
    h_future: int,
) -> Tuple[Dict[str, Tuple[np.ndarray, np.ndarray, float]], np.ndarray]:
    """Create method candidates using rolling-origin CV and full-history retraining."""
    y_values = df_weekly["quantity"].to_numpy(dtype=float)
    week_values = pd.to_datetime(df_weekly["week"]).to_numpy()
    splits = build_rolling_origin_splits(len(y_values), demand_class=demand_class, max_folds=4)

    methods_dict: Dict[str, Tuple[np.ndarray, np.ndarray, float]] = {}
    if not splits:
        return methods_dict, np.array([])

    cv_actual_ref: Optional[np.ndarray] = None

    def _store_method(method_name: str, fold_preds: List[np.ndarray], fold_actuals: List[np.ndarray], fc_future: Optional[np.ndarray]) -> None:
        nonlocal cv_actual_ref
        if not fold_preds or not fold_actuals or fc_future is None:
            return

        y_cv_pred = np.concatenate(fold_preds).astype(float)
        y_cv_true = np.concatenate(fold_actuals).astype(float)
        fc_future = np.asarray(fc_future, dtype=float)

        if len(y_cv_pred) == 0 or len(y_cv_true) == 0 or len(y_cv_pred) != len(y_cv_true):
            return
        if len(fc_future) != h_future:
            return

        if cv_actual_ref is None:
            cv_actual_ref = y_cv_true
        else:
            if len(cv_actual_ref) != len(y_cv_true):
                return
            if not np.allclose(cv_actual_ref, y_cv_true, rtol=0.0, atol=1e-8):
                return

        cv_wape = wmape(y_cv_true, y_cv_pred)
        methods_dict[method_name] = (y_cv_pred, fc_future, cv_wape)

    # Traditional / intermittent methods
    base_methods = {
        "HOLT_DAMPED": forecast_holt_damped,
        "CROSTON": forecast_croston,
        "TSB": forecast_tsb,
        "SBA": forecast_sba,
    }
    for method_name, method_fn in base_methods.items():
        fold_preds: List[np.ndarray] = []
        fold_actuals: List[np.ndarray] = []
        valid = True
        for train_end, val_end in splits:
            y_train = y_values[:train_end]
            y_val = y_values[train_end:val_end]
            fc_val, _, _ = method_fn(y_train, y_val, len(y_val))
            if fc_val is None:
                valid = False
                break
            fc_arr = np.asarray(fc_val, dtype=float)
            if len(fc_arr) != len(y_val):
                valid = False
                break
            fold_preds.append(fc_arr)
            fold_actuals.append(np.asarray(y_val, dtype=float))

        if not valid:
            continue

        _, fc_future, _ = method_fn(y_values, np.array([], dtype=float), h_future)
        _store_method(method_name, fold_preds, fold_actuals, fc_future)

    # Prophet
    if is_prophet_available() and len(df_weekly) >= 20:
        fold_preds = []
        fold_actuals = []
        valid = True
        for train_end, val_end in splits:
            y_train = y_values[:train_end]
            y_val = y_values[train_end:val_end]
            train_weeks = week_values[:train_end]
            fc_val, _, _ = forecast_prophet(
                y_train,
                y_val,
                len(y_val),
                weekly_dates_train=train_weeks,
            )
            if fc_val is None:
                valid = False
                break
            fc_arr = np.asarray(fc_val, dtype=float)
            if len(fc_arr) != len(y_val):
                valid = False
                break
            fold_preds.append(fc_arr)
            fold_actuals.append(np.asarray(y_val, dtype=float))

        if valid:
            _, fc_future, _ = forecast_prophet(
                y_values,
                np.array([], dtype=float),
                h_future,
                weekly_dates_train=week_values,
            )
            _store_method("PROPHET", fold_preds, fold_actuals, fc_future)

    # ML methods
    if len(df_weekly) >= 12:
        ml_methods = {
            "RANDOM_FOREST": ("rf", forecast_random_forest_full),
            "XGBOOST": ("xgb", forecast_xgboost_full),
        }
        for method_name, (ml_type, full_fn) in ml_methods.items():
            fold_preds = []
            fold_actuals = []
            valid = True

            for train_end, val_end in splits:
                df_fold = df_weekly.iloc[:val_end].copy()
                val_h = val_end - train_end
                X_train, y_train_ml, X_val, y_val_ml, future_state, _ = build_features(
                    df_fold,
                    val_h,
                    split_idx=train_end,
                )
                if len(X_train) < 4 or len(X_val) == 0 or len(y_val_ml) == 0:
                    valid = False
                    break

                if ml_type == "rf":
                    fc_val, _, _ = forecast_random_forest(
                        X_train, y_train_ml, X_val, y_val_ml, future_state, val_h
                    )
                else:
                    fc_val, _, _ = forecast_xgboost(
                        X_train, y_train_ml, X_val, y_val_ml, future_state, val_h
                    )

                if fc_val is None:
                    valid = False
                    break

                fc_arr = np.asarray(fc_val, dtype=float)
                y_actual = np.asarray(y_val_ml, dtype=float)
                if len(fc_arr) != len(y_actual):
                    valid = False
                    break
                fold_preds.append(fc_arr)
                fold_actuals.append(y_actual)

            if not valid:
                continue

            fc_future = full_fn(df_weekly, h_future)
            _store_method(method_name, fold_preds, fold_actuals, fc_future)

    return methods_dict, (cv_actual_ref if cv_actual_ref is not None else np.array([]))


# ============================================================
# SKU PROCESSING — simple 30 / 90 / 365-day moving averages
# ============================================================

def _fetch_sales_aggregates_batch(
    conn,
    sku_list: List[str],
    as_of_date: Optional[pd.Timestamp] = None,
) -> Dict[str, Dict[str, float]]:
    """Fetch 30/90/365-day sales totals for all SKUs in a single query.

    Returns dict keyed by SKU → {units_30d, units_90d, units_365d}.
    Replaces per-SKU sales-history fetches with one batch round-trip.
    """
    if not sku_list:
        return {}
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()

    cutoff_365 = (as_of_date - pd.Timedelta(days=365)).strftime('%Y-%m-%d')
    cutoff_90  = (as_of_date - pd.Timedelta(days=90)).strftime('%Y-%m-%d')
    cutoff_30  = (as_of_date - pd.Timedelta(days=30)).strftime('%Y-%m-%d')

    sku_list_str = "'" + "','".join(s.strip().upper() for s in sku_list) + "'"

    sql = f"""
    SELECT
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        SUM(COALESCE(L.SLBLUO, 0)) AS UNITS_365D,
        SUM(CASE WHEN H.SHIDAT >= '{cutoff_90}'
                 THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_90D,
        SUM(CASE WHEN H.SHIDAT >= '{cutoff_30}'
                 THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_30D
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
        ON H.SHCO   = L.SLCO
       AND H.SHLOC  = L.SLLOC
       AND H.SHORD# = L.SLORD#
       AND H.SHINV# = L.SLINV#
    WHERE H.SHIDAT >= '{cutoff_365}'
      AND TRIM(L.SLUM2) NOT LIKE '%SF%'
      AND COALESCE(L.SLBLUO, 0) <> 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
      AND TRIM(L.SLITEM) IN ({sku_list_str})
    GROUP BY TRIM(L.SLITEM)
    """

    print(f"  Fetching 30/90/365-day sales aggregates for {len(sku_list)} SKUs...")
    df = pd.read_sql(sql, conn)
    if df.empty:
        return {}

    result: Dict[str, Dict[str, float]] = {}
    for _, row in df.iterrows():
        sku = str(row['ITEM_NUMBER']).strip().upper()
        result[sku] = {
            'units_30d':  max(0.0, float(row.get('UNITS_30D')  or 0)),
            'units_90d':  max(0.0, float(row.get('UNITS_90D')  or 0)),
            'units_365d': max(0.0, float(row.get('UNITS_365D') or 0)),
        }
    print(f"  Loaded sales aggregates for {len(result)} SKUs")
    return result


def process_single_sku(
    sku: str,
    row: pd.Series,
    abc_class: str,
    sales_agg: Dict[str, float],
    as_of_date: pd.Timestamp,
) -> Optional[Dict[str, Any]]:
    """Compute reorder metrics from 30/90/365-day moving averages.

    Demand basis: 90-day monthly rate (units_90d / 3).
    Fallback chain: 365-day monthly rate → 30-day rate if 90-day is zero.

    Target stock: 3 months = total units sold in the past 90 days.
    Reorder point: lead-time demand only (no safety stock).
    """
    units_30d  = float(sales_agg.get('units_30d',  0) or 0)
    units_90d  = float(sales_agg.get('units_90d',  0) or 0)
    units_365d = float(sales_agg.get('units_365d', 0) or 0)

    if units_365d <= 0:
        print(f"    {sku}: no 365-day sales — skipping")
        return None

    rate_30  = units_30d
    rate_90  = units_90d  / 3.0
    rate_365 = units_365d / 12.0

    # Primary: 90-day monthly rate; fallback to 365-day, then 30-day.
    if rate_90 > 0:
        monthly_demand = rate_90
    elif rate_365 > 0:
        monthly_demand = rate_365
    else:
        monthly_demand = rate_30
    if monthly_demand <= 0:
        return None

    daily_demand   = monthly_demand / 30.0
    weekly_demand  = daily_demand * 7.0
    lead_time_days = float(row.get('lead_time', 30) or 30)

    lead_time_demand  = daily_demand * lead_time_days
    safety_stock      = 0.0
    reorder_point     = lead_time_demand
    coverage_days     = COVERAGE_HORIZON_DAYS.get(abc_class, 60)
    # Target = 3 months on hand AFTER receipt: order up to 90-day units sold
    # plus lead time demand so that once the order arrives (consuming LT demand
    # in transit), physical stock lands at exactly units_90d = 3 months.
    order_up_to_level = (units_90d if units_90d > 0 else monthly_demand * 3.0) + lead_time_demand
    # Cap at 4 months so long-LT items don't drift past a lean stocking target.
    order_up_to_level = min(order_up_to_level, monthly_demand * 4)
    # Structural guardrail: S must never be below ROP.
    order_up_to_level = max(order_up_to_level, reorder_point)

    available          = float(row.get('available',          0) or 0)
    on_order           = float(row.get('on_order',           0) or 0)
    backorder          = float(row.get('backorder',          0) or 0)
    inventory_position = float(row.get('inventory_position', 0) or 0)
    min_order_qty      = float(row.get('min_order_qty',      0) or 0)
    # Suppress reorder if the MOQ alone would exceed 6 months of demand — ordering
    # the minimum would cause overstock and tie up cash unnecessarily.
    moq_overstock = (min_order_qty > 0 and min_order_qty > monthly_demand * 6)

    print(f"    {sku}: 30d={units_30d:.0f}  90d/3={rate_90:.1f}  365d/12={rate_365:.1f}"
          f"  → monthly={monthly_demand:.1f}  ROP={reorder_point:.0f}  S={order_up_to_level:.0f}")

    # ── Monthly projections ───────────────────────────────────────────────
    # Three HIST rows (one per demand window) then forward FCST simulation.
    common = {
        'vendor_number': str(row.get('vendor_number', '')),
        'vendor_name':   str(row.get('vendor_name',   '')),
        'collection':    str(row.get('collection',    '')),
        'description':   str(row.get('DESCRIPTION',   '')),
        'SKU':           sku,
    }
    monthly_projections: List[Dict[str, Any]] = []
    as_of_month_start = as_of_date.replace(day=1)
    for months_back, rate in [(11, rate_365), (2, rate_90), (1, rate_30)]:
        mdate = as_of_month_start - pd.DateOffset(months=months_back)
        monthly_projections.append({
            **common,
            'Row_Type':          'HIST',
            'Month':             mdate.strftime('%Y-%m-%d'),
            'Historical Demand': round(rate, 1),
            'Safety Stock':      round(safety_stock, 1),
            'Beginning Inventory': np.nan,
            'Forecast':          np.nan,
            'Order Quantity':    np.nan,
            'Ending Inventory':  np.nan,
        })

    inv = inventory_position
    for i in range(max(3, int(np.ceil(coverage_days / 30)))):
        mdate = as_of_month_start + pd.DateOffset(months=i)
        beg   = inv
        order = round(max(0.0, order_up_to_level - inv), 1) if inv <= reorder_point else 0.0
        if order > 0:
            if moq_overstock:
                order = 0.0  # suppress: MOQ would result in >6 months of stock
            elif min_order_qty > 0:
                order = round(max(order, min_order_qty), 1)
        end   = max(0.0, beg + order - monthly_demand)
        monthly_projections.append({
            **common,
            'Row_Type':            'FCST',
            'Month':               mdate.strftime('%Y-%m-%d'),
            'Historical Demand':   np.nan,
            'Safety Stock':        round(safety_stock, 1),
            'Beginning Inventory': round(beg, 1),
            'Forecast':            round(monthly_demand, 1),
            'Order Quantity':      order if order > 0 else np.nan,
            'Ending Inventory':    round(end, 1),
        })
        inv = end

    # ── Forecast series for chart (flat weekly-demand reference line) ─────
    hist_x = [(as_of_date - pd.Timedelta(weeks=w)).strftime('%Y-%m-%d') for w in range(52, 0, -1)]
    hist_y = [round(weekly_demand, 2)] * 52
    fc_x   = [(as_of_date + pd.Timedelta(weeks=w)).strftime('%Y-%m-%d') for w in range(1, 14)]
    fc_y   = [round(weekly_demand, 2)] * 13

    return {
        'sku':                      sku,
        'description':              str(row.get('DESCRIPTION',        '')),
        'vendor_number':            str(row.get('vendor_number',      '')),
        'vendor_name':              str(row.get('vendor_name',        '')),
        'collection':               str(row.get('collection',         '')),
        'uom':                      str(row.get('uom',                '')),
        'vendor_part_number':       str(row.get('vendor_part_number', '')),
        'abc_class':                abc_class,
        'demand_class':             'MOVING_AVG',
        'adi':                      0.0,
        'cv2':                      0.0,
        'forecast_method':          'MAVG_30_90_365',
        'forecast_wmape':           None,
        'forecast_wape':            None,
        'forecast_mase':            None,
        'forecast_rmsse':           None,
        'forecast_bias_pct':        None,
        'forecast_uf_share':        None,
        'forecast_composite_score': None,
        'inventory_position':       round(inventory_position, 1),
        'available':                round(available,          1),
        'on_order':                 round(on_order,           1),
        'backorder':                round(backorder,          1),
        'lead_time_days':           lead_time_days,
        'lead_time_source':         str(row.get('lead_time_source', '')),
        'lead_time_weeks':          lead_time_days / 7.0,
        'mean_weekly_demand':       round(weekly_demand,       4),
        'lead_time_mean_demand':    round(lead_time_demand,    1),
        'ss_base':                  round(safety_stock,        1),
        'ss_uplift_raw':            0.0,
        'ss_uplift_applied_pre_budget': 0.0,
        'ss_floor':                 0.0,
        'safety_stock':             round(safety_stock,        1),
        'reorder_point':            round(reorder_point,       1),
        'order_up_to_level':        round(order_up_to_level,   1),
        'min_order_qty':            round(min_order_qty,       1),
        'moq_overstock':            moq_overstock,
        'coverage_horizon_days':    coverage_days,
        'daily_mean_demand':        round(daily_demand,        4),
        'units_30d':                round(units_30d,           1),
        'units_90d':                round(units_90d,           1),
        'units_365d':               round(units_365d,          1),
        'rate_30':                  round(rate_30,             2),
        'rate_90':                  round(rate_90,             2),
        'rate_365':                 round(rate_365,            2),
        'monthly_projections':      monthly_projections,
        'forecast_series': {
            'hist_x': hist_x,
            'hist_y': hist_y,
            'fc_x':   fc_x,
            'fc_y':   fc_y,
        },
    }


# ============================================================
# MAIN INVENTORY PLANNING FUNCTION
# ============================================================
def run_inventory_planning():
    """Main function to run the inventory planning process."""
    global AS_OF_DATE

    print("\n" + "=" * 70)
    print("CONNECTING TO DATABASE")
    print("=" * 70)
    conn = connect()

    # Resolve as-of date
    AS_OF_DATE = _resolve_as_of_date(conn, AS_OF_DATE_OVERRIDE)

    # Discover active sundries SKUs directly from the database
    print("\n" + "=" * 70)
    print("DISCOVERING ACTIVE SUNDRIES SKUS")
    print("=" * 70)
    active_skus = discover_active_sundries_skus(conn, days=365, as_of_date=AS_OF_DATE)
    print(f"  Active sundries SKUs (sold in last 365 days): {len(active_skus)}")

    # Filter to SKUs received via PO within the last 365 days.
    # Items that were never received, or whose last receipt is over a year old,
    # are excluded — they are either dead stock or discontinued in practice.
    received_skus, last_received_map = _filter_skus_by_last_received(
        conn, active_skus, days=365, as_of_date=AS_OF_DATE
    )
    dropped_by_receipt = active_skus - received_skus
    if dropped_by_receipt:
        print(f"  Dropped {len(dropped_by_receipt)} SKUs with no receipt in 365 days: "
              + ", ".join(sorted(dropped_by_receipt)))
    active_skus = received_skus
    print(f"  Active SKUs after last-received filter: {len(active_skus)}")

    # Fetch SKU master
    print("\n" + "=" * 70)
    print("FETCHING SKU MASTER DATA")
    print("=" * 70)
    df_master = _fetch_sku_master(conn, FOCUS_SKU, CUTOFF_DATE, active_skus)
    if df_master.empty:
        print("No SKUs to process")
        conn.close()
        return

    # Load volume for ABC classification
    print("\n" + "=" * 70)
    print("COMPUTING ABC CLASSIFICATION")
    print("=" * 70)
    vol_map, total_vol = load_trailing_12m_volume_sundries(conn, list(df_master['sku']), AS_OF_DATE)
    abc_map = compute_abc_classification(df_master, vol_map, ABC_BREAK_A, ABC_BREAK_B)

    # Fetch 30/90/365-day sales aggregates for all SKUs in one batch query
    print("\n" + "=" * 70)
    print("FETCHING SALES AGGREGATES (30 / 90 / 365 DAYS)")
    print("=" * 70)
    sales_agg_map = _fetch_sales_aggregates_batch(conn, list(df_master['sku']), AS_OF_DATE)

    # Process each SKU
    print("\n" + "=" * 70)
    print("PROCESSING SKUS")
    print("=" * 70)
    results = []
    monthly_projections = []
    total_skus = len(df_master)

    for idx, row in df_master.iterrows():
        sku = row['sku']
        abc_class = abc_map.get(sku, 'C')

        # Write progress to file for webapp to read
        progress_data = {
            "current": idx + 1,
            "total": total_skus,
            "sku": sku,
            "status": "running",
            "product_type": "sundries"
        }
        try:
            with open(PROGRESS_FILE_PATH, 'w') as f:
                json.dump(progress_data, f)
        except Exception:
            pass  # Don't let progress file errors stop the forecast

        result = process_single_sku(sku, row, abc_class, sales_agg_map.get(sku, {}), AS_OF_DATE)
        if result:
            results.append(result)
            for mp in result.get('monthly_projections', []):
                monthly_projections.append(mp)

    # Mark progress as complete
    try:
        with open(PROGRESS_FILE_PATH, 'w') as f:
            json.dump({"current": total_skus, "total": total_skus, "status": "complete", "product_type": "sundries"}, f)
    except Exception:
        pass

    # Load arrivals (open POs) before closing connection
    print("\n" + "=" * 70)
    print("LOADING ARRIVALS (OPEN POs)")
    print("=" * 70)
    df_results_tmp = pd.DataFrame(results)
    arrivals_df = pd.DataFrame()
    if not df_results_tmp.empty and "sku" in df_results_tmp.columns:
        arrivals_df = load_arrivals(conn, df_results_tmp["sku"].astype(str).unique().tolist())
        if not arrivals_df.empty:
            arrivals_df = arrivals_df.merge(
                df_results_tmp[["sku", "lead_time_days"]].rename(columns={"sku": "ITEM_NUMBER"}),
                on="ITEM_NUMBER",
                how="left",
            )
            print(f"  Loaded {len(arrivals_df)} arrival rows")
        else:
            print("  No open POs found")
    else:
        print("  No results to query arrivals for")

    conn.close()

    # Build output payload
    print("\n" + "=" * 70)
    print("BUILDING OUTPUT")
    print("=" * 70)

    payload = _build_webapp_payload(results, monthly_projections, arrivals_df)

    # Write JSON output
    print(f"\nWriting output to: {WEBAPP_JSON_PATH}")
    with open(WEBAPP_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, default=str)

    # Build DataFrames for Excel export
    df_results = pd.DataFrame(results)
    # Drop nested columns that are only needed for JSON (not suitable for Excel)
    cols_to_drop = ['monthly_projections', 'forecast_series']
    df_results = df_results.drop(columns=[c for c in cols_to_drop if c in df_results.columns])
    df_monthly = pd.DataFrame(monthly_projections)
    df_summary = _build_inventory_summary(df_results, df_monthly)

    # Save Excel output
    output_dir = Path(__file__).resolve().parent
    single_sku = bool(FOCUS_SKU)
    if single_sku:
        base_filename = "inventory_plan_sundries_single.xlsx"
    else:
        base_filename = "inventory_plan_sundries_list.xlsx"
    output_file = output_dir / base_filename

    try:
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            df_results.to_excel(writer, sheet_name='Inventory_Metrics', index=False)
            if not df_summary.empty:
                df_summary.to_excel(writer, sheet_name='Inventory_Summary', index=False)
            if not df_monthly.empty:
                df_monthly.to_excel(writer, sheet_name='Monthly_Projections', index=False)
        print(f"\nSaved spreadsheet: {output_file}")
        print(f"  Processed: {len(results)} SKUs")
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        timestamped_filename = base_filename.replace('.xlsx', f'_{timestamp}.xlsx')
        output_file = output_dir / timestamped_filename
        try:
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                df_results.to_excel(writer, sheet_name='Inventory_Metrics', index=False)
                if not df_summary.empty:
                    df_summary.to_excel(writer, sheet_name='Inventory_Summary', index=False)
                if not df_monthly.empty:
                    df_monthly.to_excel(writer, sheet_name='Monthly_Projections', index=False)
            print(f"\nSaved spreadsheet: {output_file}")
        except Exception as e:
            print(f"\nERROR: Could not save spreadsheet: {e}")
    except Exception as e:
        print(f"\nERROR: Could not save spreadsheet: {e}")

    print("\n" + "=" * 70)
    print("COMPLETE")
    print("=" * 70)
    print(f"Processed {len(results)} SKUs")


def _df_to_records(df: pd.DataFrame) -> List[Dict]:
    """Convert a DataFrame to a list of dicts, formatting dates as strings."""
    if df is None or df.empty:
        return []
    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].dt.strftime("%Y-%m-%d")
    df_out = df_out.replace({pd.NA: None, np.nan: None, np.inf: None, -np.inf: None})
    return df_out.to_dict(orient="records")


def _build_webapp_payload(results: List[Dict], monthly_projections: List[Dict],
                          df_arrivals: Optional[pd.DataFrame] = None) -> Dict:
    """Build the JSON payload for the webapp."""
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build inventory metrics list
    inventory_metrics = []
    for r in results:
        inventory_metrics.append({
            'item_number': r['sku'],
            'description': r['description'],
            'vendor_number': r['vendor_number'],
            'vendor_name': r['vendor_name'],
            'vendor_part_number': r.get('vendor_part_number', ''),
            'collection': r['collection'],
            'uom': r['uom'],
            'abc_class': r['abc_class'],
            'demand_class': r['demand_class'],
            'forecast_method': r['forecast_method'],
            'inventory_position': r['inventory_position'],
            'available': r['available'],
            'on_order': r['on_order'],
            'backorder': r['backorder'],
            'lead_time_days': r.get('lead_time_days'),
            'lead_time_source': r.get('lead_time_source', ''),
            'safety_stock': r['safety_stock'],
            'reorder_point': r['reorder_point'],
            'order_up_to_level': r['order_up_to_level'],
            'min_order_qty': r.get('min_order_qty', 0),
            'moq_overstock': r.get('moq_overstock', False),
            'daily_demand': r['daily_mean_demand'],
            'units_30d': r.get('units_30d', 0),
            'rate_90': r.get('rate_90', 0),
            'rate_365': r.get('rate_365', 0),
            'forecast_series': r['forecast_series'],
        })

    payload = {
        'run_meta': {
            'title': 'OMP Sundries Purchasing Dashboard',
            'run_timestamp_local': run_ts,
            'forecast_horizon_days': FUTURE_FORECAST_DAYS,
            'source': 'DB-driven: non-SF items with sales in last 365 days',
        },
        'items': inventory_metrics,
        'Inventory_Metrics': inventory_metrics,
        'Monthly_Projections': monthly_projections,
        'arrivals': _df_to_records(df_arrivals) if df_arrivals is not None else [],
    }

    return payload


def _build_inventory_summary(df_results: pd.DataFrame, df_monthly: pd.DataFrame) -> pd.DataFrame:
    """Build a simple apples-to-apples summary table for Excel export."""
    if df_results.empty:
        return pd.DataFrame()

    total_now_inventory_position = float(df_results['inventory_position'].sum()) if 'inventory_position' in df_results.columns else 0.0

    total_year_end = 0.0
    total_year_end_backorders = 0.0
    year_end_month = ""

    if not df_monthly.empty and 'Month' in df_monthly.columns:
        df_month = df_monthly.copy()
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


# ============================================================
# STREAMLIT APP (simplified - uses main webapp for display)
# ============================================================
def _is_streamlit_runtime() -> bool:
    """Check if running in Streamlit."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except ImportError:
        return False


def run_streamlit_app():
    """Run the Streamlit web application."""
    import streamlit as st
    import plotly.graph_objects as go

    st.set_page_config(page_title="OMP Sundries Dashboard", layout="wide")
    st.title("OMP Sundries Purchasing Dashboard")
    st.info("This is a standalone viewer. For the full dashboard, use flooringwebapp.py")

    # Load data
    if WEBAPP_JSON_PATH.exists():
        with open(WEBAPP_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        st.error(f"No data file found at {WEBAPP_JSON_PATH}. Run the inventory planning first.")
        return

    # Display basic info
    run_meta = data.get('run_meta', {})
    st.write(f"**Last Run:** {run_meta.get('run_timestamp_local', 'Unknown')}")
    st.write(f"**Source:** {run_meta.get('source', 'Unknown')}")

    # Display metrics
    metrics = data.get('Inventory_Metrics', [])
    if metrics:
        df = pd.DataFrame(metrics)
        st.dataframe(df, use_container_width=True)
    else:
        st.warning("No inventory metrics found in data file.")


# ============================================================
# MAIN ENTRY POINT
# ============================================================
if __name__ == "__main__":
    if _is_streamlit_runtime():
        run_streamlit_app()
    else:
        print("=" * 70)
        print("SUNDRIES INVENTORY PLANNING WEB APPLICATION (REFACTORED)")
        print("=" * 70)
        print(f"Mode: {'Single SKU - ' + FOCUS_SKU if FOCUS_SKU else 'DB-driven (non-SF, last 365 days, moulding excluded)'}")
        print(f"Output JSON: {WEBAPP_JSON_PATH}")
        print("=" * 70)
        run_inventory_planning()
