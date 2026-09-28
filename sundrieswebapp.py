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
)
from core.data_loader import (
    load_sku_list_from_excel,
    fetch_last_sale_dates_for_skus,
    filter_skus_to_active_last_n_days,
    load_trailing_12m_volume,
    fetch_sales_history,
    build_in_list,
)
from core.forecasting import (
    aggregate_to_weekly,
    classify_demand_pattern,
    cap_outliers,
    wmape,
    forecast_holt_damped,
    forecast_croston,
    forecast_tsb,
    forecast_sba,
    forecast_random_forest,
    forecast_xgboost,
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
FOCUS_SKU = ""  # Leave blank "" to process all SKUs in the Sundries List
USE_GLOBAL_MODEL = True
SHAREPOINT_DIR = Path(r"C:\Users\niko\OneDrive - Old Master Products\Purchasing - Flooring Reports\Dashboard Files")
SUNDRIES_LIST_FILE = SHAREPOINT_DIR / "Sundries List.xlsx"
WEBAPP_JSON_PATH = Path(__file__).resolve().parent / "sundrieswebappJSON"
UI_BUILD = "2026-01-23-sundries-refactored"

AS_OF_DATE: Optional[pd.Timestamp] = None
AS_OF_DATE_OVERRIDE = ""


# ============================================================
# SUNDRIES-SPECIFIC DATA LOADING
# ============================================================
def load_sundries_list(xlsx_path: Path) -> set:
    """Load sundries SKU list from Excel file."""
    return load_sku_list_from_excel(
        xlsx_path,
        product_type="sundries"
    )


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
      ((COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0)) * COALESCE(M.IMFACT, 1)) AS AVAILABLE_QTY,
      COALESCE(PO.ON_PO_QTY, 0) AS ON_PO_QTY, COALESCE(BO.BO_QTY, 0) AS BACKORDER_QTY,
      COALESCE(M.IMLT, 30) AS LEAD_TIME_IMLT
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
        'ON_PO_QTY': 'on_order', 'BACKORDER_QTY': 'backorder', 'LEAD_TIME_IMLT': 'lead_time'
    })

    df['inventory_position'] = df['available'] + df['on_order'] - df['backorder']
    df['sku'] = df['sku'].astype(str).str.strip().str.upper()
    df['available'] = pd.to_numeric(df['available'], errors='coerce').fillna(0.0)
    df['on_order'] = pd.to_numeric(df['on_order'], errors='coerce').fillna(0.0)
    df['backorder'] = pd.to_numeric(df['backorder'], errors='coerce').fillna(0.0)
    df['inventory_position'] = pd.to_numeric(df['inventory_position'], errors='coerce').fillna(0.0)
    df['lead_time'] = pd.to_numeric(df['lead_time'], errors='coerce').fillna(30.0)

    print(f"  Loaded {len(df)} sundries SKUs from master data")
    return df


def _fetch_sales_history(conn, sku: str, cutoff_date_str: str = CUTOFF_DATE) -> pd.DataFrame:
    """Fetch sales history for a single SKU (sundries-specific, excludes SF)."""
    return fetch_sales_history(conn, sku, cutoff_date_str, exclude_sf=True)


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
    """Load trailing 12-month volume for sundries (excludes SF)."""
    return load_trailing_12m_volume(conn, sku_list, exclude_sf=True, as_of_date=as_of_date)


# ============================================================
# SKU PROCESSING (uses shared forecasting functions)
# ============================================================
def process_single_sku(conn, sku: str, row: pd.Series, abc_class: str,
                       as_of_date: pd.Timestamp) -> Optional[Dict[str, Any]]:
    """Process a single SKU: fetch history, forecast, compute metrics."""
    print(f"\n  Processing SKU: {sku}")

    # Fetch sales history
    sales_df = _fetch_sales_history(conn, sku)
    if sales_df.empty:
        print(f"    No sales history found")
        return None

    # Aggregate to weekly
    df_weekly = aggregate_to_weekly(sales_df)
    if df_weekly.empty or len(df_weekly) < 8:
        print(f"    Insufficient weekly data ({len(df_weekly)} weeks)")
        return None

    # Cap outliers
    df_weekly['quantity'] = cap_outliers(df_weekly['quantity'])

    # Classify demand pattern
    demand_class, adi, cv2 = classify_demand_pattern(df_weekly)
    print(f"    Demand pattern: {demand_class} (ADI={adi:.2f}, CV²={cv2:.2f})")

    # Prepare train/validation split
    y_values = df_weekly['quantity'].values
    split_idx = int(len(y_values) * 0.7)
    if split_idx < 4:
        split_idx = max(4, len(y_values) - 4)

    y_train = y_values[:split_idx]
    y_val = y_values[split_idx:]
    h_future = FUTURE_FORECAST_WEEKS

    # Run forecasting methods
    methods_dict = {}

    # Traditional methods
    fc_val, fc_future, error = forecast_holt_damped(y_train, y_val, h_future)
    if fc_val is not None:
        methods_dict['HOLT_DAMPED'] = (fc_val, fc_future, error)

    fc_val, fc_future, error = forecast_croston(y_train, y_val, h_future)
    if fc_val is not None:
        methods_dict['CROSTON'] = (fc_val, fc_future, error)

    fc_val, fc_future, error = forecast_tsb(y_train, y_val, h_future)
    if fc_val is not None:
        methods_dict['TSB'] = (fc_val, fc_future, error)

    fc_val, fc_future, error = forecast_sba(y_train, y_val, h_future)
    if fc_val is not None:
        methods_dict['SBA'] = (fc_val, fc_future, error)

    # ML methods
    if len(df_weekly) >= 12:
        X_train, y_train_ml, X_val, y_val_ml, future_state, _ = build_features(df_weekly, h_future)
        if len(X_train) >= 4:
            fc_val, fc_future, error = forecast_random_forest(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
            if fc_val is not None:
                methods_dict['RANDOM_FOREST'] = (fc_val, fc_future, error)

            fc_val, fc_future, error = forecast_xgboost(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
            if fc_val is not None:
                methods_dict['XGBOOST'] = (fc_val, fc_future, error)

    # Select best method
    if not methods_dict:
        print(f"    No valid forecasts produced")
        return None

    best_method, weekly_forecast, best_error, best_metrics = select_best_forecast(
        methods_dict, y_train, y_val, demand_class, h_future
    )
    composite = best_metrics.get('composite_score', 0)
    print(f"    Best method: {best_method} (Score={composite:.1f}, wMAPE={best_error:.1f}%)")

    # Compute reorder metrics
    lead_time_weeks = float(row.get('lead_time', 30)) / DAYS_PER_WEEK
    metrics = compute_reorder_metrics(weekly_forecast, lead_time_weeks, demand_class, abc_class, y_values)

    # Build result
    inventory_position = float(row.get('inventory_position', 0))
    monthly_rows = simulate_monthly_projection(
        inventory_position=inventory_position,
        weekly_forecast=weekly_forecast,
        reorder_point=metrics['reorder_point'],
        reorder_qty=metrics.get('reorder_quantity', 0),
        sales_df=sales_df,
        as_of_date=as_of_date,
        safety_stock=metrics['safety_stock'],
        months_ahead=12,
        abc_class=abc_class,
        order_up_to_level=metrics.get('order_up_to_level'),
        lead_time_weeks=lead_time_weeks,
    )

    # Build forecast series for charting
    hist_dates = df_weekly['week'].dt.strftime('%Y-%m-%d').tolist()
    hist_values = df_weekly['quantity'].tolist()

    last_date = df_weekly['week'].max()
    fc_dates = [(last_date + pd.Timedelta(weeks=i+1)).strftime('%Y-%m-%d') for i in range(len(weekly_forecast))]
    fc_values = weekly_forecast.tolist()

    # Add SKU details to monthly projections (to match original format)
    for mp in monthly_rows:
        mp['SKU'] = sku
        mp['vendor_number'] = str(row.get('vendor_number', ''))
        mp['collection'] = str(row.get('collection', ''))
        mp['description'] = str(row.get('DESCRIPTION', ''))
        mp['Safety Stock'] = metrics['safety_stock']

    result = {
        'sku': sku,
        'description': str(row.get('DESCRIPTION', '')),
        'vendor_number': str(row.get('vendor_number', '')),
        'vendor_name': str(row.get('vendor_name', '')),
        'collection': str(row.get('collection', '')),
        'uom': str(row.get('uom', '')),
        'abc_class': abc_class,
        'demand_class': demand_class,
        'adi': adi,
        'cv2': cv2,
        'forecast_method': best_method,
        'forecast_wmape': best_error,
        'forecast_wape': best_metrics.get('wape'),
        'forecast_mase': best_metrics.get('mase'),
        'forecast_rmsse': best_metrics.get('rmsse'),
        'forecast_bias_pct': best_metrics.get('bias_pct'),
        'forecast_uf_share': best_metrics.get('uf_share'),
        'forecast_composite_score': best_metrics.get('composite_score'),
        'inventory_position': inventory_position,
        'available': float(row.get('available', 0)),
        'on_order': float(row.get('on_order', 0)),
        'backorder': float(row.get('backorder', 0)),
        'lead_time_days': float(row.get('lead_time', 30)),
        'lead_time_weeks': lead_time_weeks,
        'mean_weekly_demand': float(np.mean(weekly_forecast)),
        # Include all reorder metrics
        'lead_time_mean_demand': metrics['lead_time_mean_demand'],
        'ss_base': metrics['ss_base'],
        'ss_uplift_raw': metrics['ss_uplift_raw'],
        'ss_uplift_applied_pre_budget': metrics['ss_uplift_applied_pre_budget'],
        'safety_stock': metrics['safety_stock'],
        'reorder_point': metrics['reorder_point'],
        'order_up_to_level': metrics.get('order_up_to_level', 0),
        'reorder_quantity': metrics.get('reorder_quantity', 0),
        'coverage_horizon_days': metrics.get('coverage_horizon_days', 60),
        'daily_mean_demand': metrics['daily_mean_demand'],
        'monthly_projections': monthly_rows,
        'forecast_series': {
            'hist_x': hist_dates,
            'hist_y': hist_values,
            'fc_x': fc_dates,
            'fc_y': fc_values,
        },
    }

    return result


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

    # Load sundries SKU list
    sundries_skus = load_sundries_list(SUNDRIES_LIST_FILE)

    # Filter to active SKUs
    print("\n" + "=" * 70)
    print("FILTERING ACTIVE SKUS")
    print("=" * 70)
    active_skus, last_sale_map = filter_skus_to_active_last_n_days(
        conn, sundries_skus, days=365, exclude_sf=True, as_of_date=AS_OF_DATE
    )
    print(f"  Active SKUs (sold in last 365 days): {len(active_skus)}")

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

    # Process each SKU
    print("\n" + "=" * 70)
    print("PROCESSING SKUS")
    print("=" * 70)
    results = []
    monthly_projections = []

    for idx, row in df_master.iterrows():
        sku = row['sku']
        abc_class = abc_map.get(sku, 'C')

        result = process_single_sku(conn, sku, row, abc_class, AS_OF_DATE)
        if result:
            results.append(result)
            # Add monthly projections with SKU
            for mp in result.get('monthly_projections', []):
                mp['SKU'] = sku
                monthly_projections.append(mp)

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
            'collection': r['collection'],
            'uom': r['uom'],
            'abc_class': r['abc_class'],
            'demand_class': r['demand_class'],
            'forecast_method': r['forecast_method'],
            'inventory_position': r['inventory_position'],
            'available': r['available'],
            'on_order': r['on_order'],
            'backorder': r['backorder'],
            'safety_stock': r['safety_stock'],
            'reorder_point': r['reorder_point'],
            'order_up_to_level': r.get('order_up_to_level', 0),
            'reorder_quantity': r.get('reorder_quantity', 0),
            'daily_demand': r['daily_mean_demand'],
            'forecast_series': r['forecast_series'],
        })

    payload = {
        'run_meta': {
            'title': 'OMP Sundries Purchasing Dashboard',
            'run_timestamp_local': run_ts,
            'forecast_horizon_days': FUTURE_FORECAST_DAYS,
            'source': SUNDRIES_LIST_FILE,
        },
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
        df_month = df_month[df_month['Row_Type'] != 'HIST'] if 'Row_Type' in df_month.columns else df_month
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
        print(f"Mode: {'Single SKU - ' + FOCUS_SKU if FOCUS_SKU else 'Sundries List SKUs'}")
        print(f"Input File: {SUNDRIES_LIST_FILE}")
        print(f"Output JSON: {WEBAPP_JSON_PATH}")
        print("=" * 70)
        run_inventory_planning()
