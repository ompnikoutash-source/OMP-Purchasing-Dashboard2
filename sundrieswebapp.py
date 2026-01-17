"""
Sundries Inventory Planning Web Application
This version is specifically tailored for items that DO NOT have "SF" as their sales unit of measurement.
Designed for sundries buyers to manage non-flooring items (EA, BOX, CASE, etc.)

Based on OMPsundries1.py with JSON output and Streamlit webapp interface.
"""

from __future__ import annotations
import json
import math
import os
import warnings
import sys
from datetime import datetime, date
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
os.environ['PYTHONWARNINGS'] = 'ignore::UserWarning'
os.environ['LOKY_MAX_CPU_COUNT'] = '4'

# ============================================================
# CONFIGURATION
# ============================================================
FOCUS_SKU = "ADBOA38910"  # Leave blank "" to process all SKUs in the Sundries List
USE_GLOBAL_MODEL = True
SUNDRIES_LIST_FILE = "Sundries List.xlsx"
WEBAPP_JSON_PATH = Path(__file__).resolve().parent / "sundrieswebappJSON"
UI_BUILD = "2026-01-16-sundries"

# ============================================================
# GLOBAL ENDING INVENTORY CAP
# ============================================================
ENABLE_GLOBAL_INV_CAP = True
GLOBAL_INV_CAP_UNITS = 350000

ABC_MOI_CAPS = {
    "A": 3.0,
    "B": 2.0,
    "C": 1.0
}

# ============================================================
# SAFETY STOCK TUNING (ABC-based uplift)
# ============================================================
ABC_BREAK_A = 0.80
ABC_BREAK_B = 0.90
SS_UPLIFT_SCALAR = {"A": 1.0, "B": 0.35, "C": 0.00}
LUMPY_PCTL_NONZERO = {"A": 0.95, "B": 0.90, "C": 0.85}
LUMPY_LT_BUFFER_FRAC = 0.10
ENABLE_GLOBAL_UPLIFT_BUDGET = True
GLOBAL_UPLIFT_BUDGET_PCT = 0.02

DSN_NAME = "Gartman"
FUTURE_FORECAST_DAYS = 365
FUTURE_FORECAST_WEEKS = 52
DAYS_PER_WEEK = 7
DAYS_PER_MONTH = 30.4
WEEKS_PER_MONTH = DAYS_PER_MONTH / DAYS_PER_WEEK
SERVICE_LEVEL = 0.95
Z_SCORE = stats.norm.ppf(SERVICE_LEVEL)
CUTOFF_DATE = "2020-06-01"
AS_OF_DATE: Optional[pd.Timestamp] = None
AS_OF_DATE_OVERRIDE = ""

# ============================================================
# DATABASE AND CREDENTIALS
# ============================================================
def _get_db_as_of_date(conn) -> pd.Timestamp:
    try:
        df = pd.read_sql("SELECT CURRENT DATE AS TODAY FROM SYSIBM.SYSDUMMY1", conn)
        if not df.empty and pd.notna(df.loc[0, "TODAY"]):
            return pd.Timestamp(df.loc[0, "TODAY"]).normalize()
    except Exception as e:
        print(f"  WARNING: Failed to read DB current date, using local clock. Error: {e}")
    return pd.Timestamp.now().normalize()

def _resolve_as_of_date(conn) -> pd.Timestamp:
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
    uid_env = os.getenv("GARTMAN_UID", "").strip()
    pwd_env = os.getenv("GARTMAN_PWD", "").strip()
    secrets_path = Path(__file__).resolve().parent / "OMP_secrets.py"
    uid_file = ""
    pwd_file = ""
    if secrets_path.exists():
        namespace = {}
        with open(secrets_path, 'r', encoding='utf-8') as f:
            exec(f.read(), namespace, namespace)
        uid_file = str(namespace.get("GARTMAN_UID", "")).strip()
        pwd_file = str(namespace.get("GARTMAN_PWD", "")).strip()
    if uid_env and pwd_env:
        if uid_file and pwd_file:
            if uid_env == pwd_file and pwd_env == uid_file:
                print(f"  WARNING: Environment variables appear SWAPPED!")
                print(f"  Using credentials from secrets file instead")
                uid, pwd = uid_file, pwd_file
            else:
                uid, pwd = uid_env, pwd_env
        else:
            uid, pwd = uid_env, pwd_env
    elif uid_file and pwd_file:
        uid, pwd = uid_file, pwd_file
    else:
        raise RuntimeError("Missing credentials")
    if uid == pwd:
        raise RuntimeError(f"ERROR: Username and password are identical ('{uid}'). This is incorrect!")
    return uid, pwd

def _connect():
    uid, pwd = _load_credentials()
    conn_str = f"DSN={DSN_NAME};UID={uid};PWD={pwd};"
    print(f"  Attempting database connection...")
    try:
        conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
        print(f"  Database connection successful!")
        return conn
    except pyodbc.Error as e:
        print(f"  Database connection failed!")
        print(f"  Error: {e}")
        raise

def _sql_escape(value: str) -> str:
    return value.replace("'", "''")

# ============================================================
# DATA LOADING FUNCTIONS
# ============================================================
def load_sundries_list(xlsx_path: Path) -> set:
    print(f"\nLoading sundries SKU list from {xlsx_path}...")
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Required file not found: {xlsx_path}")
    try:
        df = pd.read_excel(xlsx_path, header=0)
        if df.shape[0] == 0 or df.shape[1] < 1:
            raise ValueError(f"File is empty: {xlsx_path}")
        sku_col = df.columns[0]
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        sku_list = set(df[sku_col].dropna())
        sku_list = {sku for sku in sku_list if sku and sku != 'NAN' and len(sku) > 0}
        if len(sku_list) == 0:
            raise ValueError(f"No valid SKUs found in: {xlsx_path}")
        print(f"  Loaded {len(sku_list)} sundries SKUs from list")
        return sku_list
    except Exception as e:
        if isinstance(e, (FileNotFoundError, ValueError)):
            raise
        raise

def fetch_last_sale_dates_for_skus(conn, sku_list: set, chunk_size: int = 800, as_of_date: Optional[pd.Timestamp] = None) -> Dict[str, pd.Timestamp]:
    if not sku_list:
        return {}
    skus = sorted({str(s).strip().upper() for s in sku_list if str(s).strip()})
    results: Dict[str, pd.Timestamp] = {}
    for i in range(0, len(skus), chunk_size):
        chunk = skus[i:i + chunk_size]
        in_list = ",".join([f"'{_sql_escape(s)}'" for s in chunk])
        query = f"""
            SELECT TRIM(L.SLITEM) AS SKU, MAX(H.SHIDAT) AS LAST_SALE_YYYYMMDD
            FROM GSFL2K.SHLINE L
            JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
            WHERE TRIM(L.SLITEM) IN ({in_list})
              AND L.SLUM2 NOT LIKE '%SF%'
              AND COALESCE(L.SLBLUO, 0) > 0
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

def filter_skus_to_active_last_365_days(conn, sku_list: set, days: int = 365, as_of_date: Optional[pd.Timestamp] = None) -> Tuple[set, Dict[str, pd.Timestamp]]:
    last_sale_map = fetch_last_sale_dates_for_skus(conn, sku_list, as_of_date=as_of_date)
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    cutoff = as_of_date - pd.Timedelta(days=days)
    active = {sku for sku, dt in last_sale_map.items() if pd.notna(dt) and dt >= cutoff}
    return active, last_sale_map

def _fetch_sku_master(conn, focus_sku: str = "", cutoff_date_str: str = CUTOFF_DATE, sundries_sku_list: set = None) -> pd.DataFrame:
    print("\nFetching sundries SKU master...")
    if not sundries_sku_list or len(sundries_sku_list) == 0:
        raise ValueError("Sundries SKU list is required but was not provided or is empty")
    if focus_sku:
        sku_filter = f"AND TRIM(M.IMITEM) = '{focus_sku.strip().upper()}'"
        print(f"  Filtering for single SKU: {focus_sku}")
    else:
        sku_list_clean = [s.strip().upper() for s in sundries_sku_list]
        sku_list_str = "'" + "','".join(sku_list_clean) + "'"
        sku_filter = f"AND TRIM(M.IMITEM) IN ({sku_list_str})"
        print(f"  Filtering for {len(sundries_sku_list)} SKUs from Sundries List")
    query = f"""
    WITH
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
      TRIM(M.IMVEND) AS VENDOR_NUMBER, TRIM(V.VMNAME) AS VENDOR_NAME, TRIM(X.IMCOLLECT) AS COLLECTION,
      ((COALESCE(INV.QTY_ON_HAND,0) - COALESCE(INV.QTY_COMMITTED,0)) * COALESCE(M.IMFACT, 1)) AS AVAILABLE_QTY,
      COALESCE(PO.ON_PO_QTY, 0) AS ON_PO_QTY, COALESCE(BO.BO_QTY, 0) AS BACKORDER_QTY,
      COALESCE(M.IMLT, 30) AS LEAD_TIME_IMLT
    FROM GSFL2K.ITEMMAST M
    LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
    LEFT JOIN GSFL2K.VENDMAST V ON V.VMVEND = M.IMVEND
    LEFT JOIN INV ON INV.IBITEM = M.IMITEM
    LEFT JOIN PO ON PO.PLITEM = M.IMITEM
    LEFT JOIN BO ON BO.OLITEM = M.IMITEM
    WHERE M.IMUM2 NOT LIKE '%SF%' {sku_filter}
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
    sku_clean = sku.strip().upper()
    query = f"""
    SELECT H.SHIDAT AS SALES_DATE, COALESCE(L.SLBLUO, 0) AS QTY_SOLD
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE TRIM(L.SLITEM) = '{_sql_escape(sku_clean)}'
      AND L.SLUM2 NOT LIKE '%SF%'
      AND COALESCE(L.SLBLUO, 0) > 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
    """
    try:
        df = pd.read_sql_query(query, conn)
        if df.empty:
            return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])
        raw = df['SALES_DATE']
        raw_str = raw.astype(str).str.strip()
        is_yyyymmdd = raw_str.str.fullmatch(r"\d{8}")
        dt = pd.to_datetime(raw_str.where(is_yyyymmdd), format='%Y%m%d', errors='coerce')
        dt = dt.fillna(pd.to_datetime(raw_str, errors='coerce'))
        df['SALES_DATE'] = dt
        df = df.dropna(subset=['SALES_DATE'])
        if df.empty:
            return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])
        cutoff_dt = pd.to_datetime(cutoff_date_str)
        df = df[df['SALES_DATE'] >= cutoff_dt]
        df = df.groupby('SALES_DATE', as_index=False).agg({'QTY_SOLD': 'sum'})
        df = df.rename(columns={'SALES_DATE': 'transaction_date', 'QTY_SOLD': 'quantity_shipped'})
        df['uom'] = 'UNITS'
        return df.sort_values('transaction_date')
    except Exception as e:
        print(f"ERROR fetching sales for {sku}: {e}")
        return pd.DataFrame(columns=['transaction_date', 'quantity_shipped'])

def _build_in_list(values: List[str]) -> Tuple[str, List[str]]:
    safe = [str(v).strip().upper() for v in values if v]
    if not safe:
        return "''", []
    placeholders = ",".join(["?"] * len(safe))
    return placeholders, safe

def load_arrivals(conn, sku_list: List[str]) -> pd.DataFrame:
    """Load open PO arrivals with container notes for sundries."""
    if not sku_list:
        return pd.DataFrame()
    placeholders, params = _build_in_list(sku_list)
    sql = f"""
    SELECT
      TRIM(L.PLPO#) AS PO_NUMBER,
      TRIM(L.PLITEM) AS ITEM_NUMBER,
      TRIM(L.PLDESC) AS DESCRIPTION,
      COALESCE(L.PLBLUO, 0) AS QUANTITY_SF,
      TRIM(M.IMVEND) AS VENDOR_NUMBER,
      TRIM(V.VMNAME) AS VENDOR_NAME,
      TRIM(X.IMCOLLECT) AS COLLECTION,
      L.PLPDAT AS DUE_PORT,
      L.PLDDAT AS DUE_INV,
      H.PHDOI AS ENTRY_DATE,
      H.PHSDAT AS EST_SHIP_DATE,
      MAX(TRIM(P.PTCMT1)) AS PTCMT1,
      MAX(TRIM(P.PTCMT2)) AS PTCMT2
    FROM GSFL2K.POLINE L
    LEFT JOIN GSFL2K.POTEXT P ON P.PTPO# = L.PLPO#
    LEFT JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO#
    JOIN GSFL2K.ITEMMAST M ON M.IMITEM = L.PLITEM
    LEFT JOIN GSFL2K.ITEMXTRA X ON X.IMXITM = M.IMITEM
    LEFT JOIN GSFL2K.VENDMAST V ON V.VMVEND = M.IMVEND
    WHERE L.PLDELT LIKE '%A%'
      AND TRIM(L.PLPO#) <> ''
      AND TRIM(L.PLITEM) <> ''
      AND TRIM(L.PLITEM) IN ({placeholders})
    GROUP BY
      TRIM(L.PLPO#),
      TRIM(L.PLITEM),
      TRIM(L.PLDESC),
      COALESCE(L.PLBLUO, 0),
      TRIM(M.IMVEND),
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

def load_trailing_12m_volume_sundries(conn, sku_list: List[str], as_of_date: Optional[pd.Timestamp] = None) -> Tuple[Dict[str, float], float]:
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
    SELECT TRIM(L.SLITEM) AS ITEM_NUMBER, SUM(COALESCE(L.SLBLUO,0)) AS VOL_12M
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD# AND H.SHINV# = L.SLINV#
    WHERE L.SLUM2 NOT LIKE '%SF%' AND H.SHIDAT >= ? AND H.SHIDAT <= ? {sku_filter}
    GROUP BY TRIM(L.SLITEM)
    """
    df = pd.read_sql_query(sql, conn, params=params)
    df["ITEM_NUMBER"] = df["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    df["VOL_12M"] = pd.to_numeric(df["VOL_12M"], errors="coerce").fillna(0.0)
    vol_map = dict(zip(df["ITEM_NUMBER"], df["VOL_12M"]))
    total_vol = float(df["VOL_12M"].sum()) if not df.empty else 0.0
    print(f"  Loaded volume data for {len(df)} SKUs")
    return vol_map, total_vol

def compute_abc_classification(df_master: pd.DataFrame, shipped_12m_map: Dict[str, float]) -> Dict[str, str]:
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
    print(f"  A: {a_count} SKUs, B: {b_count} SKUs, C: {c_count} SKUs")
    return abc_map

# ============================================================
# FORECASTING FUNCTIONS
# ============================================================
def aggregate_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df['week'] = df['transaction_date'].dt.to_period('W').dt.to_timestamp()
    weekly = df.groupby('week').agg({'quantity_shipped': 'sum'}).reset_index()
    weekly.columns = ['week', 'quantity']
    return weekly

def classify_demand_pattern(df_weekly: pd.DataFrame) -> Tuple[str, float, float]:
    if df_weekly.empty or len(df_weekly) < 4:
        return 'UNKNOWN', 0.0, 0.0
    demand = df_weekly['quantity'].values
    nonzero_demand = demand[demand > 0]
    if len(nonzero_demand) == 0:
        return 'NO_DEMAND', 0.0, 0.0
    nonzero_indices = np.where(demand > 0)[0]
    if len(nonzero_indices) <= 1:
        adi = len(demand)
    else:
        intervals = np.diff(nonzero_indices)
        adi = float(np.mean(intervals))
    if len(nonzero_demand) < 2:
        cv2 = 0.0
    else:
        mean_demand = float(np.mean(nonzero_demand))
        std_demand = float(np.std(nonzero_demand, ddof=1))
        if mean_demand > 0:
            cv2 = (std_demand / mean_demand) ** 2
        else:
            cv2 = 0.0
    ADI_THRESHOLD = 1.32
    CV2_THRESHOLD = 0.49
    if adi < ADI_THRESHOLD and cv2 < CV2_THRESHOLD:
        demand_class = 'SMOOTH'
    elif adi >= ADI_THRESHOLD and cv2 < CV2_THRESHOLD:
        demand_class = 'INTERMITTENT'
    elif adi < ADI_THRESHOLD and cv2 >= CV2_THRESHOLD:
        demand_class = 'ERRATIC'
    else:
        demand_class = 'LUMPY'
    return demand_class, adi, cv2

def safe_exponential_smoothing(y_train, seasonal_periods=None, trend=None, seasonal=None, damped_trend=False, max_attempts=3):
    if len(y_train) < 3:
        return None
    nonzero_count = np.sum(y_train > 0)
    if nonzero_count < 2:
        return None
    for attempt in range(max_attempts):
        try:
            if attempt == 0:
                model = ExponentialSmoothing(y_train, seasonal_periods=seasonal_periods, trend=trend, seasonal=seasonal, damped_trend=damped_trend, initialization_method="estimated")
            elif attempt == 1:
                model = ExponentialSmoothing(y_train, trend=trend, seasonal=None, damped_trend=damped_trend, initialization_method="heuristic")
            else:
                model = SimpleExpSmoothing(y_train, initialization_method="heuristic")
            if attempt == 0:
                fit = model.fit(optimized=True, use_brute=False)
            else:
                fit = model.fit(optimized=False)
            return fit
        except Exception:
            if attempt == max_attempts - 1:
                return None
            continue
    return None

def wmape(y_true, y_pred):
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0 if np.sum(np.abs(y_pred)) == 0 else 1000.0
    return float(np.sum(np.abs(y_true - y_pred)) / denom) * 100

def forecast_holt_damped(y_train, y_val, h_future):
    try:
        fit = safe_exponential_smoothing(y_train, trend='add', seasonal=None, damped_trend=True)
        if fit is None:
            return None, None, None
        total_steps = len(y_val) + h_future
        fc_all = fit.forecast(total_steps)
        fc_val = fc_all[:len(y_val)]
        fc_future = fc_all[len(y_val):]
        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except Exception:
        return None, None, None

def forecast_croston(y_train, y_val, h_future, alpha=0.1):
    try:
        demand = y_train.copy()
        nonzero_indices = np.where(demand > 0)[0]
        if len(nonzero_indices) < 2:
            return None, None, None
        z = demand[nonzero_indices[0]]
        p = nonzero_indices[0] + 1
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
    except Exception:
        return None, None, None

def forecast_tsb(y_train, y_val, h_future, alpha=0.1, beta=0.1):
    try:
        demand = y_train.copy()
        n = len(demand)
        if n < 2:
            return None, None, None
        if demand[0] > 0:
            z_t = demand[0]
            p_t = 1.0
        else:
            first_nonzero = np.where(demand > 0)[0]
            if len(first_nonzero) == 0:
                return None, None, None
            z_t = demand[first_nonzero[0]]
            p_t = 0.5
        for t in range(1, n):
            if demand[t] > 0:
                z_t = alpha * demand[t] + (1 - alpha) * z_t
                p_t = alpha * 1.0 + (1 - alpha) * p_t
            else:
                p_t = alpha * 0.0 + (1 - alpha) * p_t
        z_final = z_t
        p_final = min(max(p_t, 0.05), 0.95)
        forecast_value = z_final * p_final
        mean_all = np.mean(demand)
        if mean_all > 0 and (forecast_value > 3 * mean_all or forecast_value < 0.1 * mean_all):
            forecast_value = mean_all
        fc_val = np.full(len(y_val), forecast_value)
        fc_future = np.full(h_future, forecast_value)
        error = wmape(y_val, fc_val)
        if error > 200:
            return None, None, None
        return fc_val, fc_future, error
    except Exception:
        return None, None, None

def forecast_sba(y_train, y_val, h_future, alpha=0.1):
    try:
        demand = y_train.copy()
        nonzero_demand = demand[demand > 0]
        if len(nonzero_demand) < 2:
            return None, None, None
        z = np.mean(nonzero_demand)
        nonzero_indices = np.where(demand > 0)[0]
        if len(nonzero_indices) < 2:
            return None, None, None
        intervals = np.diff(nonzero_indices)
        p = np.mean(intervals) if len(intervals) > 0 else 1
        forecast_value = (z / p) * (1 - alpha / 2) if p > 0 else 0
        fc_val = np.full(len(y_val), forecast_value)
        fc_future = np.full(h_future, forecast_value)
        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except Exception:
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

def forecast_random_forest(X_train, y_train, X_val, y_val, future_state, h_future):
    try:
        model = RandomForestRegressor(n_estimators=100, max_depth=10, min_samples_split=5, random_state=42, n_jobs=1)
        model.fit(X_train, y_train)
        fc_val = model.predict(X_val)
        fc_future = _recursive_forecast(model, future_state, h_future)
        fc_val = np.maximum(fc_val, 0)
        fc_future = np.maximum(fc_future, 0)
        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except Exception:
        return None, None, None

def forecast_xgboost(X_train, y_train, X_val, y_val, future_state, h_future):
    try:
        model = XGBRegressor(n_estimators=100, max_depth=6, learning_rate=0.1, random_state=42, n_jobs=1, verbosity=0)
        model.fit(X_train, y_train, verbose=False)
        fc_val = model.predict(X_val)
        fc_future = _recursive_forecast(model, future_state, h_future)
        fc_val = np.maximum(fc_val, 0)
        fc_future = np.maximum(fc_future, 0)
        error = wmape(y_val, fc_val)
        return fc_val, fc_future, error
    except Exception:
        return None, None, None

def build_features(df_weekly: pd.DataFrame, h_future: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, Dict[str, Any], np.ndarray]:
    df = df_weekly.copy()
    df = df.sort_values('week').reset_index(drop=True)
    df['time_idx'] = range(len(df))
    df['lag_1'] = df['quantity'].shift(1)
    df['lag_2'] = df['quantity'].shift(2)
    df['lag_3'] = df['quantity'].shift(3)
    df['lag_4'] = df['quantity'].shift(4)
    df['rolling_mean_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).mean()
    df['rolling_std_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).std()
    df = df.fillna(0)
    feature_cols = ['time_idx', 'lag_1', 'lag_2', 'lag_3', 'lag_4', 'rolling_mean_4', 'rolling_std_4']
    split_idx = int(len(df) * 0.7)
    if split_idx < 4:
        split_idx = max(4, len(df) - 4)
    train_df = df.iloc[:split_idx]
    val_df = df.iloc[split_idx:]
    X_train = train_df[feature_cols].values
    y_train = train_df['quantity'].values
    X_val = val_df[feature_cols].values
    y_val = val_df['quantity'].values
    last_idx = int(df['time_idx'].iloc[-1])
    last_values = df['quantity'].iloc[-4:].tolist()
    future_state = {"last_idx": last_idx, "last_values": last_values}
    return X_train, y_train, X_val, y_val, future_state, val_df['quantity'].values

def validate_forecast_reasonableness(fc_future: np.ndarray, y_train: np.ndarray, method_name: str) -> Tuple[bool, str]:
    if fc_future is None or len(fc_future) == 0:
        return False, "No forecast produced"
    fc_future = np.asarray(fc_future)
    y_train = np.asarray(y_train)
    historical_max = float(np.max(y_train))
    historical_mean = float(np.mean(y_train))
    nonzero_data = y_train[y_train > 0]
    historical_nonzero_mean = float(np.mean(nonzero_data)) if len(nonzero_data) > 0 else 0
    recent_weeks = min(26, len(y_train) // 2)
    recent_sales = y_train[-recent_weeks:]
    recent_total = float(np.sum(recent_sales))
    recent_max = float(np.max(recent_sales))
    forecast_max = float(np.max(fc_future))
    forecast_mean = float(np.mean(fc_future))
    forecast_min = float(np.min(fc_future))
    if forecast_max > 100_000:
        return False, f"Forecast exceeds 100K ({forecast_max:,.0f})"
    if recent_total == 0:
        if forecast_mean > 0.5:
            return False, f"No sales in last {recent_weeks} weeks but forecasting {forecast_mean:.1f} per week"
    if recent_total > 0 and recent_total < 5:
        if forecast_mean > 2:
            return False, f"Only {recent_total:.0f} units in last {recent_weeks} weeks but forecasting {forecast_mean:.1f} per week"
    if historical_max > 0 and forecast_max > (historical_max * 10):
        return False, f"Forecast max ({forecast_max:.0f}) > 10x historical max ({historical_max:.0f})"
    if historical_mean > 0 and forecast_mean > (historical_mean * 10):
        return False, f"Forecast mean ({forecast_mean:.1f}) > 10x historical mean ({historical_mean:.1f})"
    if historical_nonzero_mean > 0 and historical_nonzero_mean < 10:
        if forecast_mean > (historical_nonzero_mean * 3):
            return False, f"Slow mover: forecast ({forecast_mean:.1f}) > 3x non-zero mean ({historical_nonzero_mean:.1f})"
    if forecast_min < 0:
        return False, f"Negative forecast detected: {forecast_min:.1f}"
    if recent_max > 0 and forecast_max > (recent_max * 5):
        return False, f"Forecast max ({forecast_max:.0f}) > 5x recent max ({recent_max:.0f})"
    return True, "Valid"

def select_best_forecast(methods_dict: Dict, y_train_series: pd.Series, demand_class: str) -> Tuple[str, np.ndarray, float]:
    y_train = y_train_series.values
    valid_methods = {}
    rejected_methods = {}
    for method_name, (fc_val, fc_future, error_metric) in methods_dict.items():
        if fc_val is None or fc_future is None or error_metric is None:
            rejected_methods[method_name] = "Failed to produce forecast"
            continue
        if error_metric >= 200:
            rejected_methods[method_name] = f"wMAPE too high ({error_metric:.0f}%)"
            continue
        is_valid, reason = validate_forecast_reasonableness(fc_future, y_train, method_name)
        if not is_valid:
            rejected_methods[method_name] = reason
            continue
        valid_methods[method_name] = (fc_val, fc_future, error_metric)
    if not valid_methods:
        recent_weeks = min(26, len(y_train) // 2)
        recent_sales = y_train[-recent_weeks:]
        recent_total = float(np.sum(recent_sales))
        h_future = FUTURE_FORECAST_WEEKS
        if recent_total == 0:
            nonzero = y_train[y_train > 0]
            if len(nonzero) > 0:
                fallback = float(np.mean(nonzero))
                fc_future = np.full(h_future, fallback)
                return 'HISTORICAL_MEAN', fc_future, 0.0
            fc_future = np.zeros(h_future)
            return 'ZERO_FORECAST', fc_future, 0.0
        if recent_total < 10:
            recent_avg = recent_total / recent_weeks
            fc_future = np.full(h_future, recent_avg)
            return 'RECENT_AVG', fc_future, 0.0
        mean_demand = float(np.mean(y_train))
        historical_max = float(np.max(y_train))
        safe_forecast = min(mean_demand, historical_max)
        fc_future = np.full(h_future, safe_forecast)
        return 'SAFE_MEAN', fc_future, 0.0
    historical_mean = float(np.mean(y_train))
    methods_with_scores = []
    for method_name, (fc_val, fc_future, wmape_score) in valid_methods.items():
        forecast_mean = float(np.mean(fc_future))
        forecast_bias = forecast_mean / historical_mean if historical_mean > 0 else 1.0
        if forecast_bias < 0.5:
            adjusted_score = wmape_score * 1.5
        elif forecast_bias < 0.8:
            adjusted_score = wmape_score * 1.2
        elif forecast_bias > 2.0:
            adjusted_score = wmape_score * 1.1
        else:
            adjusted_score = wmape_score
        methods_with_scores.append({
            'name': method_name, 'fc_val': fc_val, 'fc_future': fc_future, 'wmape': wmape_score, 'adjusted_score': adjusted_score
        })
    best_method = min(methods_with_scores, key=lambda x: x['adjusted_score'])
    return best_method['name'], best_method['fc_future'], best_method['wmape']

# ============================================================
# SAFETY STOCK AND REORDER METRICS
# ============================================================
def compute_safety_stock(mean_demand: float, std_demand: float, lead_time_weeks: float,
                        demand_class: str, abc_class: str, y_train: np.ndarray) -> Dict[str, float]:
    if std_demand > 0 and lead_time_weeks > 0:
        ss_base = Z_SCORE * std_demand * math.sqrt(lead_time_weeks)
    else:
        ss_base = 0.0
    uplift_scalar = SS_UPLIFT_SCALAR.get(abc_class, 0.0)
    ss_uplift = ss_base * uplift_scalar
    lumpy_buffer = 0.0
    if demand_class == 'LUMPY':
        nonzero_demand = y_train[y_train > 0]
        if len(nonzero_demand) > 0:
            pctl = LUMPY_PCTL_NONZERO.get(abc_class, 0.90)
            percentile_demand = float(np.percentile(nonzero_demand, pctl * 100))
            lumpy_buffer = percentile_demand * lead_time_weeks * LUMPY_LT_BUFFER_FRAC
    total_uplift = ss_uplift + lumpy_buffer
    max_ss = mean_demand * WEEKS_PER_MONTH * 3
    ss_base = min(ss_base, max_ss)
    total_uplift = min(total_uplift, max_ss - ss_base)
    return {
        'ss_base': ss_base,
        'ss_uplift_raw': total_uplift,
        'ss_uplift_applied_pre_budget': total_uplift,
        'safety_stock': ss_base + total_uplift
    }

def compute_reorder_metrics(weekly_forecast: np.ndarray, lead_time_weeks: float,
                           demand_class: str, abc_class: str, y_train: np.ndarray) -> Dict[str, float]:
    mean_weekly = np.mean(weekly_forecast)
    std_weekly = np.std(y_train) if len(y_train) > 0 else 0.0
    lead_time_mean_demand = mean_weekly * lead_time_weeks
    ss_dict = compute_safety_stock(mean_weekly, std_weekly, lead_time_weeks, demand_class, abc_class, y_train)
    reorder_point = lead_time_mean_demand + ss_dict['safety_stock']
    reorder_quantity = mean_weekly * WEEKS_PER_MONTH * 2
    return {
        'lead_time_mean_demand': lead_time_mean_demand,
        'reorder_point': reorder_point,
        'reorder_quantity': reorder_quantity,
        'daily_mean_demand': mean_weekly / DAYS_PER_WEEK,
        **ss_dict
    }

# ============================================================
# MONTHLY PROJECTION SIMULATION
# ============================================================
def simulate_monthly_projection(inventory_position, weekly_forecast, reorder_point, reorder_qty, sales_df, as_of_date, safety_stock, months_ahead=12):
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    else:
        as_of_date = pd.Timestamp(as_of_date).normalize()
    month_start = as_of_date.replace(day=1)
    month_end = (month_start + pd.offsets.MonthEnd(0)).normalize()
    as_of_end = as_of_date + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    sales_df = sales_df.copy()
    sales_df['transaction_date'] = pd.to_datetime(sales_df['transaction_date'], errors='coerce')
    sales_df = sales_df[sales_df['transaction_date'].notna()]
    sales_df = sales_df[sales_df['transaction_date'] <= as_of_end]
    mtd_mask = (sales_df['transaction_date'] >= month_start) & (sales_df['transaction_date'] <= as_of_end)
    mtd_actual = float(sales_df.loc[mtd_mask, 'quantity_shipped'].sum())
    remaining_days = max(0, (month_end - as_of_date).days)
    remainder_fc = float(weekly_forecast.mean()) * (remaining_days / DAYS_PER_WEEK)
    total_month_demand = mtd_actual + remainder_fc
    daily_idx = pd.date_range(weekly_forecast.index[0], weekly_forecast.index[-1] + pd.Timedelta(days=6), freq='D')
    daily_forecast = pd.Series(index=daily_idx, dtype=float)
    for week_end, week_demand in weekly_forecast.items():
        week_start = week_end - pd.Timedelta(days=6)
        mask = (daily_idx >= week_start) & (daily_idx <= week_end)
        daily_forecast.loc[mask] = week_demand / DAYS_PER_WEEK
    df_forecast = pd.DataFrame({'Date': daily_forecast.index, 'Demand': daily_forecast.values})
    df_forecast['Month'] = pd.to_datetime(df_forecast['Date']).dt.to_period('M').dt.to_timestamp()
    monthly_demand = df_forecast.groupby('Month')['Demand'].sum().reset_index()
    start_fc_month = (month_start + pd.offsets.MonthBegin(1)).normalize()
    monthly_demand = monthly_demand[monthly_demand['Month'] >= start_fc_month]
    monthly_demand = monthly_demand.sort_values('Month').head(months_ahead)
    if len(monthly_demand) < months_ahead:
        avg_month_demand = float(weekly_forecast.mean()) * WEEKS_PER_MONTH if len(weekly_forecast) else 0.0
        last_month = monthly_demand['Month'].max() if not monthly_demand.empty else (start_fc_month - pd.offsets.MonthBegin(1))
        extra_months = pd.date_range(last_month + pd.offsets.MonthBegin(1), periods=months_ahead - len(monthly_demand), freq="MS")
        extra = pd.DataFrame({"Month": extra_months, "Demand": avg_month_demand})
        monthly_demand = pd.concat([monthly_demand, extra], ignore_index=True)
    projections = []
    inventory_level = inventory_position
    beginning_inv = inventory_level
    reorder_triggered = (beginning_inv - total_month_demand) < reorder_point
    if reorder_triggered:
        inventory_level += reorder_qty
    ending_inv = max(0, inventory_level - total_month_demand)
    projections.append({
        'Month': month_start,
        'Historical Demand': mtd_actual,
        'Safety Stock': safety_stock,
        'Beginning Inventory': beginning_inv,
        'Forecast': remainder_fc,
        'Order Quantity': reorder_qty if reorder_triggered else 0,
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
            inventory_level += reorder_qty
            reorder_qty_applied = reorder_qty
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

# ============================================================
# GLOBAL INVENTORY CAP
# ============================================================
def apply_global_inv_cap(df_results: pd.DataFrame, df_monthly: pd.DataFrame, global_cap_units: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    print(f"\n{'='*70}")
    print("APPLYING GLOBAL INVENTORY CAP")
    print(f"{'='*70}")
    if 'Ending Inventory' not in df_monthly.columns:
        print("  No Ending Inventory column found, skipping cap")
        return df_results, df_monthly
    total_ending_inv = df_monthly.groupby('Month')['Ending Inventory'].sum().max()
    print(f"  Current max total ending inventory: {total_ending_inv:,.0f} units")
    print(f"  Global cap: {global_cap_units:,.0f} units")
    if total_ending_inv <= global_cap_units:
        print(f"  Under capacity - no adjustment needed")
        return df_results, df_monthly
    print(f"  OVER CAPACITY by {total_ending_inv - global_cap_units:,.0f} units")
    df_monthly = df_monthly.copy()
    sku_abc_map = dict(zip(df_results['sku'], df_results['abc_class']))
    df_monthly['abc_class'] = df_monthly['SKU'].map(sku_abc_map)
    sku_avg_demand = df_monthly.groupby('SKU')['Forecast'].mean().to_dict()
    df_monthly['avg_monthly_demand'] = df_monthly['SKU'].map(sku_avg_demand)
    df_monthly['moi_cap_months'] = df_monthly['abc_class'].map(ABC_MOI_CAPS)
    df_monthly['moi_cap_inventory'] = df_monthly['avg_monthly_demand'] * df_monthly['moi_cap_months']
    sku_lt_demand_map = dict(zip(df_results['sku'], df_results['lead_time_mean_demand']))
    df_monthly['lead_time_demand'] = df_monthly['SKU'].map(sku_lt_demand_map)
    df_monthly['min_inventory'] = df_monthly['lead_time_demand']
    df_monthly['flex_inventory'] = np.maximum(0, np.minimum(df_monthly['Ending Inventory'] - df_monthly['min_inventory'], df_monthly['moi_cap_inventory'] - df_monthly['min_inventory']))
    df_monthly['capped_ending_inventory'] = df_monthly['min_inventory'] + df_monthly['flex_inventory']
    total_after_moi_cap = df_monthly.groupby('Month')['capped_ending_inventory'].sum().max()
    print(f"  After ABC MOI caps: {total_after_moi_cap:,.0f} units")
    if total_after_moi_cap <= global_cap_units:
        df_monthly['Ending Inventory'] = df_monthly['capped_ending_inventory']
        return df_results, df_monthly
    month_totals = df_monthly.groupby('Month').agg({'min_inventory': 'sum', 'flex_inventory': 'sum'}).reset_index()
    month_totals['total_inventory'] = month_totals['min_inventory'] + month_totals['flex_inventory']
    peak_month_idx = month_totals['total_inventory'].idxmax()
    peak_month = month_totals.loc[peak_month_idx]
    total_min = peak_month['min_inventory']
    total_flex = peak_month['flex_inventory']
    available_for_flex = global_cap_units - total_min
    if available_for_flex < 0:
        scale_factor = 0.5
    else:
        scale_factor = available_for_flex / total_flex if total_flex > 0 else 1.0
    scale_factor = min(scale_factor, 1.0)
    df_monthly['scaled_flex_inventory'] = df_monthly['flex_inventory'] * scale_factor
    df_monthly['Ending Inventory'] = df_monthly['min_inventory'] + df_monthly['scaled_flex_inventory']
    final_total = df_monthly.groupby('Month')['Ending Inventory'].sum().max()
    print(f"  Final max total ending inventory: {final_total:,.0f} units")
    print(f"{'='*70}")
    return df_results, df_monthly

# ============================================================
# SKU PROCESSING
# ============================================================
def process_sku(conn, sku_row: pd.Series, abc_map: Dict[str, str], global_model=None, global_feature_cols=None, sku_encodings=None) -> Optional[Tuple[Dict, pd.DataFrame]]:
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
    lead_time_days = float(sku_row.get('lead_time', 30))
    lead_time_weeks = lead_time_days / DAYS_PER_WEEK
    print(f"  Lead Time: {lead_time_days:.1f} days ({lead_time_weeks:.1f} weeks)")
    print(f"  Vendor: {vendor} - {vendor_name}")
    print(f"  Inventory Position: {inventory_position:,.0f}")
    df_sales = _fetch_sales_history(conn, sku, CUTOFF_DATE)
    if df_sales.empty:
        print(f"  No sales history - SKIPPING")
        return None
    today = AS_OF_DATE or pd.Timestamp.now()
    df_sales['transaction_date'] = pd.to_datetime(df_sales['transaction_date'], errors='coerce')
    df_sales = df_sales.dropna(subset=['transaction_date'])
    df_sales = df_sales[df_sales['transaction_date'] <= today]
    if df_sales.empty:
        print(f"  No valid sales data after cleaning - SKIPPING")
        return None
    most_recent_sale = df_sales['transaction_date'].max()
    days_since_last_sale = (today - most_recent_sale).days
    print(f"  Sales Records: {len(df_sales)}")
    print(f"  Most Recent Sale: {most_recent_sale.date()} ({days_since_last_sale} days ago)")
    if days_since_last_sale > 365:
        print(f"  SKIPPING - Last sale more than 365 days ago")
        return None
    df_weekly = aggregate_to_weekly(df_sales)
    if len(df_weekly) < 8:
        print(f"  Insufficient weekly data ({len(df_weekly)} weeks)")
        return None
    demand_class, adi, cv2 = classify_demand_pattern(df_weekly)
    print(f"  Demand Pattern: {demand_class} (ADI={adi:.2f}, CV2={cv2:.2f})")
    abc_class = abc_map.get(sku, 'C')
    print(f"  ABC Class: {abc_class}")
    split_idx = int(len(df_weekly) * 0.7)
    if split_idx < 4:
        split_idx = max(4, len(df_weekly) - 4)
    y_train = df_weekly['quantity'].values[:split_idx]
    y_val = df_weekly['quantity'].values[split_idx:]
    h_future = FUTURE_FORECAST_WEEKS
    X_train, y_train_ml, X_val, y_val_ml, future_state, _ = build_features(df_weekly, h_future)
    methods = {}
    methods['Holt_Damped'] = forecast_holt_damped(pd.Series(y_train), pd.Series(y_val), h_future)
    methods['Croston'] = forecast_croston(y_train, y_val, h_future)
    methods['TSB'] = forecast_tsb(y_train, y_val, h_future)
    methods['SBA'] = forecast_sba(y_train, y_val, h_future)
    if len(X_train) >= 10:
        methods['Random_Forest'] = forecast_random_forest(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
        methods['XGBoost'] = forecast_xgboost(X_train, y_train_ml, X_val, y_val_ml, future_state, h_future)
    best_method, weekly_forecast, best_mape = select_best_forecast(methods, pd.Series(y_train), demand_class)
    print(f"  Selected Method: {best_method} (wMAPE: {best_mape:.2f}%)")
    reorder_metrics = compute_reorder_metrics(weekly_forecast, lead_time_weeks, demand_class, abc_class, y_train)
    print(f"  Reorder Point: {reorder_metrics['reorder_point']:.0f}, ROQ: {reorder_metrics['reorder_quantity']:.0f}")
    weekly_fc_series = pd.Series(weekly_forecast, index=pd.date_range(df_weekly['week'].max() + pd.Timedelta(days=7), periods=len(weekly_forecast), freq='W'))
    monthly_proj = simulate_monthly_projection(inventory_position, weekly_fc_series, reorder_metrics['reorder_point'], reorder_metrics['reorder_quantity'], df_sales, AS_OF_DATE, reorder_metrics['safety_stock'], months_ahead=12)
    if not df_sales.empty:
        df_hist = df_sales.copy()
        df_hist['Month'] = pd.to_datetime(df_hist['transaction_date']).dt.to_period('M').dt.to_timestamp()
        df_hist = df_hist.groupby('Month', as_index=False)['quantity_shipped'].sum().sort_values('Month')
        current_month = AS_OF_DATE.replace(day=1) if AS_OF_DATE else pd.Timestamp.now().replace(day=1)
        df_hist = df_hist[df_hist['Month'] < current_month]
        hist_rows = []
        for _, r in df_hist.iterrows():
            hist_rows.append({
                'Month': r['Month'],
                'Historical Demand': r['quantity_shipped'],
                'Safety Stock': reorder_metrics['safety_stock'],
                'Beginning Inventory': np.nan,
                'Forecast': np.nan,
                'Order Quantity': np.nan,
                'Ending Inventory': np.nan,
                'Row_Type': 'HIST'
            })
        if hist_rows:
            monthly_proj = pd.concat([pd.DataFrame(hist_rows), monthly_proj], ignore_index=True)
    monthly_proj['SKU'] = sku
    monthly_proj['vendor_number'] = vendor
    monthly_proj['vendor_name'] = vendor_name
    monthly_proj['collection'] = collection
    monthly_proj['description'] = sku_row.get('DESCRIPTION', '')
    metrics = {
        'sku': sku,
        'description': sku_row.get('DESCRIPTION', ''),
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
    return metrics, monthly_proj

# ============================================================
# JSON OUTPUT FUNCTIONS
# ============================================================
def _df_to_records(df: pd.DataFrame) -> List[Dict]:
    if df is None or df.empty:
        return []
    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].dt.strftime("%Y-%m-%d")
    df_out = df_out.replace({pd.NA: None, np.nan: None, np.inf: None, -np.inf: None})
    return df_out.to_dict(orient="records")

def _filter_forecast_months(series: pd.Series) -> pd.Series:
    if series is None or series.empty:
        return series
    if not isinstance(series.index, pd.DatetimeIndex):
        return series
    current_month_start = pd.Timestamp.today().to_period("M").to_timestamp()
    next_month_start = current_month_start + pd.offsets.MonthBegin(1)
    end_exclusive = current_month_start + pd.DateOffset(months=12)
    return series[(series.index >= next_month_start) & (series.index < end_exclusive)]

def _get_first_column(df: Optional[pd.DataFrame], candidates: List[str]) -> Optional[str]:
    if df is None:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None

def _build_webapp_payload(df_results: pd.DataFrame, df_monthly: pd.DataFrame, df_arrivals: Optional[pd.DataFrame] = None) -> Dict:
    payload = {
        "run_meta": {},
        "items": [],
        "queue": [],
        "Inventory_Metrics": [],
        "Monthly_Projections": [],
        "generated_at": datetime.now().isoformat()
    }
    if df_results is None or df_results.empty:
        return payload
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload["run_meta"] = {
        "title": "OMP Sundries Purchasing Dashboard",
        "run_timestamp_local": run_ts,
        "forecast_horizon_days": FUTURE_FORECAST_DAYS,
        "source": f"{SUNDRIES_LIST_FILE}",
    }
    col_month = _get_first_column(df_monthly, ["Month", "month"])
    col_forecast = _get_first_column(df_monthly, ["Forecast", "forecast"])
    col_hist = _get_first_column(df_monthly, ["Historical Demand", "Historical", "history"])
    col_sku = _get_first_column(df_monthly, ["SKU", "sku", "Item Number", "ITEM_NUMBER"])
    row_type_col = _get_first_column(df_monthly, ["Row_Type", "row_type"])
    sku_series: Dict[str, Dict[str, List[float]]] = {}
    sku_first_dates: Dict[str, str] = {}
    sku_history_days: Dict[str, int] = {}
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
            series_payload = {
                "fc_x": [d.strftime("%Y-%m-%d") for d in series.index],
                "fc_y": [float(v) for v in series.values],
                "x": [d.strftime("%Y-%m-%d") for d in series.index],
                "y": [float(v) for v in series.values],
            }
            hist_series = None
            if row_type_col and row_type_col in sku_df.columns:
                hist_mask = sku_df[row_type_col].astype(str).str.upper().eq("HIST")
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
            sku_series[str(sku)] = series_payload
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
            "maturity": str(row.get("abc_class", "")) or "C",
            "demand_pattern": str(row.get("demand_class", "")),
            "adi": float(row.get("adi", 0.0) or 0.0),
            "cv2": float(row.get("cv2", 0.0) or 0.0),
            "first_sales_date": sku_first_dates.get(sku),
            "history_days": sku_history_days.get(sku, 0),
        }
        items.append({
            "item_number": sku,
            "description": str(row.get("description", "")),
            "vendor_name": str(row.get("vendor_name", "")) or str(row.get("collection", "")),
            "vendor_number": str(row.get("vendor_number", "")),
            "collection": str(row.get("collection", "")),
            "uom": str(row.get("uom", "UNITS")),
            "available": float(row.get("available", 0.0) or 0.0),
            "on_po": float(row.get("on_order", 0.0) or 0.0),
            "backorder": float(row.get("backorder", 0.0) or 0.0),
            "inventory_position": inv_pos,
            "lead_time_days": int(row.get("lead_time_days", 0) or 0),
            "lt_demand": float(row.get("lead_time_mean_demand", 0.0) or 0.0),
            "suggested_order_qty": float(row.get("reorder_quantity", 0.0) or 0.0),
            "forecast_series": sku_series.get(sku, {}),
            "segmentation": segmentation,
            "daily_avg_demand": daily_avg,
            "days_of_cover": float(days_cover),
        })
    df_queue = df_results.copy()
    if "reorder_point" in df_queue.columns and "inventory_position" in df_queue.columns:
        df_queue["risk_gap"] = df_queue["inventory_position"] - df_queue["reorder_point"]
        df_queue = df_queue.sort_values("risk_gap")
    queue_rows: List[Dict] = []
    for _, row in df_queue.head(8).iterrows():
        queue_rows.append({
            "item_number": str(row.get("sku", "")),
            "description": str(row.get("description", "")),
            "vendor_name": str(row.get("vendor_name", "")) or str(row.get("collection", "")),
            "vendor_number": str(row.get("vendor_number", "")),
            "collection": str(row.get("collection", "")),
            "available": float(row.get("available", 0.0) or 0.0),
            "on_po": float(row.get("on_order", 0.0) or 0.0),
            "backorder": float(row.get("backorder", 0.0) or 0.0),
            "inventory_position": float(row.get("inventory_position", 0.0) or 0.0),
            "lead_time_days": int(row.get("lead_time_days", 0) or 0),
            "lt_demand": float(row.get("lead_time_mean_demand", 0.0) or 0.0),
        })
    payload["items"] = items
    payload["queue"] = queue_rows
    payload["Inventory_Metrics"] = _df_to_records(df_results)
    payload["Monthly_Projections"] = _df_to_records(df_monthly)
    if df_arrivals is not None and not df_arrivals.empty:
        payload["arrivals"] = _df_to_records(df_arrivals)
    else:
        payload["arrivals"] = []
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

def _load_webapp_payload() -> Dict:
    if WEBAPP_JSON_PATH.exists():
        try:
            with open(WEBAPP_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"items": [], "queue": [], "Inventory_Metrics": [], "Monthly_Projections": [], "run_meta": {}}
    return {"items": [], "queue": [], "Inventory_Metrics": [], "Monthly_Projections": [], "run_meta": {}}

# ============================================================
# MAIN INVENTORY PLANNING FUNCTION
# ============================================================
def run_inventory_planning():
    output_dir = Path(__file__).resolve().parent
    sku_list_path = output_dir / SUNDRIES_LIST_FILE
    sundries_sku_list = load_sundries_list(sku_list_path)
    conn = _connect()
    global AS_OF_DATE
    AS_OF_DATE = _resolve_as_of_date(conn)
    single_sku = bool(FOCUS_SKU)
    if single_sku:
        focus = FOCUS_SKU.strip().upper()
        active_set, last_sale_map = filter_skus_to_active_last_365_days(conn, {focus}, days=365, as_of_date=AS_OF_DATE)
        if focus not in active_set:
            print(f"\nSKIPPING {focus}: No recent sales")
            conn.close()
            return
        sundries_sku_list = {focus}
    else:
        before_ct = len(sundries_sku_list)
        active_set, last_sale_map = filter_skus_to_active_last_365_days(conn, sundries_sku_list, days=365, as_of_date=AS_OF_DATE)
        sundries_sku_list = active_set
        after_ct = len(sundries_sku_list)
        print(f"\nActive SKU Filter: {before_ct} -> {after_ct} SKUs")
        if after_ct == 0:
            print("\nNo active SKUs found")
            conn.close()
            return
    sku_master = _fetch_sku_master(conn, FOCUS_SKU, CUTOFF_DATE, sundries_sku_list)
    if sku_master.empty:
        print("\nNo sundries SKUs to process")
        conn.close()
        return
    sku_list_for_volume = sku_master["sku"].astype(str).str.upper().tolist()
    shipped_12m_map, total_vol_12m = load_trailing_12m_volume_sundries(conn, sku_list_for_volume, as_of_date=AS_OF_DATE)
    abc_map = compute_abc_classification(sku_master, shipped_12m_map)
    uplift_budget = GLOBAL_UPLIFT_BUDGET_PCT * total_vol_12m if ENABLE_GLOBAL_UPLIFT_BUDGET else None
    results = []
    all_monthly_projs = []
    for _, row in sku_master.iterrows():
        try:
            result = process_sku(conn, row, abc_map, None, None, None)
            if result:
                metrics, monthly_proj = result
                results.append(metrics)
                all_monthly_projs.append(monthly_proj)
        except Exception as e:
            print(f"  ERROR: {str(e)}")
            import traceback
            traceback.print_exc()
    if not results:
        print("\nNo SKUs processed")
        conn.close()
        return
    df_results = pd.DataFrame(results)
    df_monthly = pd.concat(all_monthly_projs, ignore_index=True) if all_monthly_projs else pd.DataFrame()
    arrivals_df = pd.DataFrame()
    if not df_results.empty and "sku" in df_results.columns:
        arrivals_df = load_arrivals(conn, df_results["sku"].astype(str).unique().tolist())
        if not arrivals_df.empty:
            arrivals_df = arrivals_df.merge(
                df_results[["sku", "lead_time_days"]].rename(columns={"sku": "ITEM_NUMBER"}),
                on="ITEM_NUMBER",
                how="left",
            )
    if ENABLE_GLOBAL_UPLIFT_BUDGET and uplift_budget is not None and uplift_budget > 0 and not df_results.empty:
        total_uplift = float(df_results["ss_uplift_applied_pre_budget"].sum())
        if total_uplift > uplift_budget:
            k = uplift_budget / total_uplift
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"] * k
            df_results["safety_stock"] = df_results["ss_base"] + df_results["ss_uplift_applied"]
            df_results["reorder_point"] = df_results["lead_time_mean_demand"] + df_results["safety_stock"]
        else:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]
    else:
        if not df_results.empty and "ss_uplift_applied_pre_budget" in df_results.columns:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]
    if ENABLE_GLOBAL_INV_CAP and not df_results.empty and not df_monthly.empty:
        df_results, df_monthly = apply_global_inv_cap(df_results, df_monthly, GLOBAL_INV_CAP_UNITS)
    # Build and write webapp JSON
    payload = _build_webapp_payload(df_results, df_monthly, arrivals_df)
    _write_webapp_json(payload, WEBAPP_JSON_PATH)
    # Also save Excel output
    if single_sku:
        base_filename = "inventory_plan_sundries_single.xlsx"
    else:
        base_filename = "inventory_plan_sundries_list.xlsx"
    output_file = output_dir / base_filename
    try:
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            df_results.to_excel(writer, sheet_name='Inventory_Metrics', index=False)
            if not df_monthly.empty:
                df_monthly.to_excel(writer, sheet_name='Monthly_Projections', index=False)
        print(f"\nSaved: {output_file}")
        print(f"  Processed: {len(results)} SKUs")
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        timestamped_filename = base_filename.replace('.xlsx', f'_{timestamp}.xlsx')
        output_file = output_dir / timestamped_filename
        try:
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                df_results.to_excel(writer, sheet_name='Inventory_Metrics', index=False)
                if not df_monthly.empty:
                    df_monthly.to_excel(writer, sheet_name='Monthly_Projections', index=False)
            print(f"\nSaved: {output_file}")
        except Exception:
            print(f"\nERROR: Could not save file")
    except Exception:
        print(f"\nERROR: Could not save file")
    conn.close()

# ============================================================
# STREAMLIT WEBAPP
# ============================================================
def _is_streamlit_runtime() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False

def _aggregate_items(items: List[Dict]) -> Dict:
    if not items:
        return {}
    total_available = sum(float(i.get("available", 0) or 0) for i in items)
    total_on_po = sum(float(i.get("on_po", 0) or 0) for i in items)
    total_backorder = sum(float(i.get("backorder", 0) or 0) for i in items)
    total_inv_pos = sum(float(i.get("inventory_position", 0) or 0) for i in items)
    total_lt_demand = sum(float(i.get("lt_demand", 0) or 0) for i in items)
    return {
        "available": total_available,
        "on_po": total_on_po,
        "backorder": total_backorder,
        "inventory_position": total_inv_pos,
        "lt_demand": total_lt_demand,
        "vendor_number": items[0].get("vendor_number", "--") if items else "--",
    }

def _build_queue_df_from_inventory(metrics_rows: List[Dict]) -> pd.DataFrame:
    if not metrics_rows:
        return pd.DataFrame()
    rows = []
    for r in metrics_rows:
        inv_pos = float(r.get("inventory_position", 0) or 0)
        rop = float(r.get("reorder_point", 0) or 0)
        risk_gap = inv_pos - rop
        rows.append({
            "SKU": str(r.get("sku", "")),
            "Description": str(r.get("description", "")),
            "Vendor": str(r.get("vendor_name", "")),
            "On Hand": float(r.get("available", 0) or 0),
            "On PO": float(r.get("on_order", 0) or 0),
            "Backorder": float(r.get("backorder", 0) or 0),
            "Inv Position": inv_pos,
            "ROP": rop,
            "Risk Gap": risk_gap,
        })
    df = pd.DataFrame(rows)
    return df.sort_values("Risk Gap")

def run_streamlit_app():
    import streamlit as st
    import plotly.graph_objects as go
    st.set_page_config(page_title="OMP Sundries Dashboard", layout="wide", initial_sidebar_state="expanded")
    data = _load_webapp_payload()
    st.markdown(
        """
        <style>
        :root {
          --panel-strong: rgba(30, 52, 16, 0.55);
          --panel: rgba(40, 63, 22, 0.55);
          --shadow: 0 1px 4px rgba(0,0,0,0.25);
        }
        .hero-card {
          background: var(--panel-strong);
          border-radius: 16px;
          padding: 18px 24px;
          margin-bottom: 18px;
          box-shadow: var(--shadow);
        }
        .hero-title {
          font-size: 1.5rem;
          font-weight: 800;
          color: #ffffff;
        }
        .hero-meta {
          font-size: 0.85rem;
          color: rgba(255,255,255,0.8);
        }
        .pill {
          display: inline-block;
          background: rgba(255,255,255,0.15);
          border-radius: 12px;
          padding: 4px 12px;
          font-size: 0.75rem;
          color: #ffffff;
          margin-bottom: 8px;
        }
        .queue-card {
          background: var(--panel-strong);
          border-radius: 16px;
          padding: 0;
          margin-bottom: 18px;
          border: 1px solid rgba(255,255,255,0.08);
        }
        .queue-header {
          padding: 10px 16px;
          font-size: 0.9rem;
          font-weight: 700;
          color: #ffffff;
          background: rgba(0,0,0,0.2);
          border-radius: 16px 16px 0 0;
        }
        .queue-table {
          padding: 10px 12px 14px 12px;
        }
        .queue-row {
          display: grid;
          grid-template-columns: repeat(5, 1fr);
          font-size: 0.82rem;
          padding: 6px 8px;
          border-bottom: 1px solid rgba(255,255,255,0.08);
          color: #ffffff;
        }
        .queue-row:last-child {
          border-bottom: none;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        st.markdown("### Item Filters")
    items = data.get("items", [])
    if not items:
        items = []
    vendor_names = sorted({str(item.get("vendor_name", "")).strip() for item in items if item.get("vendor_name")})
    if not vendor_names:
        vendor_names = ["--"]
    vendor_choices = ["All Vendors"] + vendor_names
    selected_vendor = st.sidebar.selectbox("Vendor name", vendor_choices, index=0)
    if selected_vendor == "All Vendors":
        vendor_items = items
    else:
        vendor_items = [item for item in items if str(item.get("vendor_name", "")).strip() == selected_vendor]
    collection_names = sorted({str(item.get("collection", "")).strip() for item in vendor_items if item.get("collection")})
    if not collection_names:
        collection_names = ["--"]
    collection_choices = ["All Collections"] + collection_names
    selected_collection = st.sidebar.selectbox("Collection", collection_choices, index=0)
    if selected_collection == "All Collections":
        collection_items = vendor_items
    else:
        collection_items = [item for item in vendor_items if str(item.get("collection", "")).strip() == selected_collection]
    item_options = ["All items"] + [str(item.get("description", "")) for item in collection_items]
    selected_desc = st.sidebar.selectbox("Item description", item_options, index=0)
    if selected_desc != "All items":
        selected_item = next((item for item in collection_items if str(item.get("description", "")) == selected_desc), collection_items[0] if collection_items else None)
    else:
        selected_item = None
    vendor_items = collection_items
    run_meta = data.get("run_meta", {})
    if selected_item:
        header_title = f"{selected_item.get('item_number','')} | {selected_item.get('description','')}"
        vendor_label = selected_item.get("vendor_name") or "--"
        vendor_number = selected_item.get("vendor_number") or "--"
    else:
        header_title = f"{selected_vendor} | All items"
        vendor_label = selected_vendor or "--"
        vendor_summary = _aggregate_items(vendor_items)
        vendor_number = vendor_summary.get("vendor_number", "--")
    horizon_days = run_meta.get("forecast_horizon_days", FUTURE_FORECAST_DAYS)
    run_stamp = run_meta.get("run_timestamp_local", "--")
    st.markdown(
        f"""
        <div class="hero-card">
          <div class="pill">{run_meta.get('title','OMP Sundries Dashboard')}</div>
          <div class="hero-meta">
            <div>{vendor_label} (Vendor {vendor_number})</div>
            <div>Horizon: {horizon_days} days | Run: {run_stamp}</div>
            <div>UI build: {UI_BUILD}</div>
          </div>
          <div class="hero-title">{header_title}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    metrics_rows = data.get("Inventory_Metrics", [])
    if selected_collection != "All Collections":
        metrics_rows = [row for row in metrics_rows if str(row.get("collection", "")).strip() == selected_collection]
    queue_df = _build_queue_df_from_inventory(metrics_rows)
    if not queue_df.empty and selected_vendor != "All Vendors":
        queue_df = queue_df[queue_df["Vendor"].astype(str).str.strip() == selected_vendor]
    monthly_rows = data.get("Monthly_Projections", [])
    if selected_collection != "All Collections":
        monthly_rows = [row for row in monthly_rows if str(row.get("collection", "")).strip() == selected_collection]
    # Display forecast chart if item selected
    if selected_item:
        series = selected_item.get("forecast_series", {}) or {}
        hist_x = series.get("hist_x") or []
        hist_y = series.get("hist_y") or []
        fc_x = series.get("fc_x") or []
        fc_y = series.get("fc_y") or []
        fig = go.Figure()
        if hist_x and hist_y:
            fig.add_trace(go.Scatter(x=hist_x, y=hist_y, mode='lines+markers', name='Historical', line=dict(color='#7cb342')))
        if fc_x and fc_y:
            fig.add_trace(go.Scatter(x=fc_x, y=fc_y, mode='lines+markers', name='Forecast', line=dict(color='#ffb74d', dash='dash')))
        fig.update_layout(
            title="Demand Forecast",
            xaxis_title="Date",
            yaxis_title="Units",
            template="plotly_dark",
            height=350,
            margin=dict(l=40, r=40, t=60, b=40),
        )
        st.plotly_chart(fig, use_container_width=True)
    # Display purchasing queue
    if not queue_df.empty:
        st.markdown("### Purchasing Queue (Highest Risk)")
        st.dataframe(queue_df.head(10), use_container_width=True)
    # Display monthly projections table for selected item
    if selected_item and monthly_rows:
        sku_label = selected_item.get("item_number", "")
        df_monthly_all = pd.DataFrame(monthly_rows)
        if not df_monthly_all.empty and "SKU" in df_monthly_all.columns:
            df_monthly = df_monthly_all[df_monthly_all["SKU"].astype(str) == str(sku_label)]
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
                st.markdown(f"### Reorder Schedule: {sku_label}")
                st.dataframe(out, use_container_width=True)

# ============================================================
# MAIN ENTRY POINT
# ============================================================
if __name__ == "__main__":
    if _is_streamlit_runtime():
        run_streamlit_app()
    else:
        print("="*70)
        print("SUNDRIES INVENTORY PLANNING WEB APPLICATION")
        print("="*70)
        print(f"Mode: {'Single SKU - ' + FOCUS_SKU if FOCUS_SKU else 'Sundries List SKUs'}")
        print(f"Input File: {SUNDRIES_LIST_FILE}")
        print(f"Output JSON: {WEBAPP_JSON_PATH}")
        print("="*70)
        run_inventory_planning()
