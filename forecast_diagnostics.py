"""
Forecasting Model Diagnostics - Comprehensive Model Comparison
Compares 20+ forecasting models on Division 1 items from Gartman database
Evaluates using 8 accuracy metrics with weighted scoring
"""

from __future__ import annotations
import warnings
from pathlib import Path
from typing import Dict, Tuple
import numpy as np
import pandas as pd
import pyodbc
from scipy import stats
from statsmodels.tsa.holtwinters import ExponentialSmoothing, SimpleExpSmoothing, Holt
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.statespace.sarimax import SARIMAX
from sklearn.neural_network import MLPRegressor
from sklearn.ensemble import RandomForestRegressor
from xgboost import XGBRegressor
from datetime import datetime
import json

warnings.filterwarnings("ignore")

# ============================================================
# CONFIGURATION
# ============================================================
DSN_NAME = "Gartman"
CUTOFF_DATE = "2020-06-01"  # Only items with sales since this date
MIN_HISTORY_MONTHS = 12  # Minimum months of history required
TEST_MONTHS = 3  # Hold out last 3 months for testing
FORECAST_SKU_LIST_FILE = "Forecast_List_Copy.xlsx"  # Excel file with SKUs to process

# Metric weights (must sum to 100%)
METRIC_WEIGHTS = {
    'wMAPE': 0.26,
    'RMSE': 0.26,
    'MAE': 0.08,
    'MAPE': 0.08,
    'sMAPE': 0.08,
    'MPE': 0.08,
    'MASE': 0.08,
    'WAPE': 0.08
}

# ============================================================
# DATABASE CONNECTION
# ============================================================

def _load_credentials():
    """Load database credentials from OMP_secrets.py"""
    import os
    secrets_path = Path(__file__).resolve().parent / "OMP_secrets.py"
    
    uid_env = os.getenv("GARTMAN_UID", "").strip()
    pwd_env = os.getenv("GARTMAN_PWD", "").strip()
    
    if secrets_path.exists():
        namespace = {}
        with open(secrets_path, 'r', encoding='utf-8') as f:
            exec(f.read(), namespace, namespace)
        uid_file = str(namespace.get("GARTMAN_UID", "")).strip()
        pwd_file = str(namespace.get("GARTMAN_PWD", "")).strip()
    else:
        uid_file = ""
        pwd_file = ""
    
    if uid_env and pwd_env:
        return uid_env, pwd_env
    elif uid_file and pwd_file:
        return uid_file, pwd_file
    else:
        raise RuntimeError("Missing credentials")

def connect_to_database():
    """Connect to Gartman database"""
    uid, pwd = _load_credentials()
    conn_str = f"DSN={DSN_NAME};UID={uid};PWD={pwd};"
    return pyodbc.connect(conn_str, autocommit=True, timeout=60)

# ============================================================
# DATA LOADING
# ============================================================

def load_forecast_sku_list():
    """Load SKU list from Excel file"""
    try:
        output_dir = Path(__file__).parent
        filepath = output_dir / FORECAST_SKU_LIST_FILE
        
        print(f"\nLoading SKU list from {FORECAST_SKU_LIST_FILE}...")
        df = pd.read_excel(filepath)
        
        # Get first column (should be SKUs)
        sku_col = df.columns[0]
        sku_list = df[sku_col].astype(str).str.strip().str.upper().tolist()
        
        # Remove duplicates and empty values
        sku_list = [sku for sku in sku_list if sku and sku != 'NAN']
        sku_list = list(set(sku_list))
        
        print(f"✓ Loaded {len(sku_list)} SKUs from forecast list")
        return sku_list
    except Exception as e:
        print(f"ERROR loading SKU list: {e}")
        return []

def get_skus_from_database(conn, sku_list):
    """Get SKU details from database for items in the forecast list"""
    print("\n" + "="*70)
    print("LOADING SKU DETAILS FROM DATABASE")
    print("="*70)
    
    # Create IN clause for SQL
    sku_list_str = "'" + "','".join(sku_list) + "'"
    
    sql = f"""
    WITH FIRSTREC AS (
        SELECT TRIM(R.IRITEM) AS ITEM_NUMBER, 
               MIN(R.IRDATE) AS FIRST_RECEIPT_DATE
        FROM GSFL2K.ITEMRECH R
        WHERE R.IRLOC NOT IN (90, 17, 41, 46)
          AND TRIM(R.IRITEM) IN ({sku_list_str})
        GROUP BY TRIM(R.IRITEM)
    )
    SELECT 
        TRIM(M.IMITEM) AS ITEM_NUMBER,
        TRIM(M.IMDESC) AS DESCRIPTION,
        TRIM(M.IMVEND) AS VENDOR_NUMBER,
        FR.FIRST_RECEIPT_DATE
    FROM GSFL2K.ITEMMAST M
    LEFT JOIN FIRSTREC FR ON FR.ITEM_NUMBER = TRIM(M.IMITEM)
    WHERE TRIM(M.IMITEM) IN ({sku_list_str})
    ORDER BY TRIM(M.IMITEM)
    """
    
    df = pd.read_sql_query(sql, conn)
    df['FIRST_RECEIPT_DATE'] = pd.to_datetime(df['FIRST_RECEIPT_DATE'], errors='coerce')
    
    print(f"Found {len(df)} SKUs in database (from {len(sku_list)} in list)")
    
    if len(df) < len(sku_list):
        missing = len(sku_list) - len(df)
        print(f"  Note: {missing} SKUs from list not found in database")
    
    return df

def load_sales_history(conn, sku):
    """Load complete sales history for an item"""
    sql = """
    SELECT 
        H.SHIDAT AS SALES_DATE,
        SUM(COALESCE(L.SLBLUS, 0)) AS QTY_SF
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H ON H.SHCO = L.SLCO AND H.SHLOC = L.SLLOC AND H.SHORD# = L.SLORD#
    WHERE L.SLUM2 LIKE '%SF%'
      AND TRIM(L.SLITEM) = ?
      AND H.SHIDAT >= DATE('1990-01-01')
      AND H.SHIDAT <= CURRENT_DATE
    GROUP BY H.SHIDAT
    ORDER BY H.SHIDAT
    """
    
    df = pd.read_sql_query(sql, conn, params=[sku])
    df['SALES_DATE'] = pd.to_datetime(df['SALES_DATE'], errors='coerce')
    df['QTY_SF'] = pd.to_numeric(df['QTY_SF'], errors='coerce').fillna(0.0)
    
    return df

def aggregate_to_monthly(sales_df):
    """Aggregate daily sales to monthly totals"""
    if sales_df.empty:
        return None
    
    df = sales_df.copy()
    df['MONTH'] = df['SALES_DATE'].dt.to_period('M')
    monthly = df.groupby('MONTH')['QTY_SF'].sum().reset_index()
    monthly['MONTH'] = monthly['MONTH'].dt.to_timestamp()
    monthly.set_index('MONTH', inplace=True)
    
    # Fill missing months with zeros
    if len(monthly) > 0:
        full_range = pd.date_range(monthly.index.min(), monthly.index.max(), freq='MS')
        monthly = monthly.reindex(full_range, fill_value=0.0)
    
    return monthly['QTY_SF']

def has_long_gap(monthly_series, max_gap_months=36):
    """
    Check if there's a 3+ year gap (36 months) with no sales
    Between FIRST ACTUAL SALE and TODAY
    
    This catches:
    - Discontinued items (no sales in last 3+ years)
    - Items we stopped carrying years ago
    - Old demand patterns that won't reflect current reality
    """
    if monthly_series is None or len(monthly_series) == 0:
        return True
    
    # Find all non-zero sales
    non_zero_mask = monthly_series > 0
    non_zero_indices = np.where(non_zero_mask)[0]
    
    if len(non_zero_indices) == 0:
        return True  # No sales at all
    
    # Check gaps between consecutive sales from FIRST SALE TO TODAY
    # The monthly_series already goes to today (or near it)
    # So we check all gaps including the gap from last sale to end of series
    
    gaps = np.diff(non_zero_indices)
    
    # Also check the gap from last sale to end of series (today)
    last_sale_idx = non_zero_indices[-1]
    gap_to_end = len(monthly_series) - 1 - last_sale_idx
    
    # Combine all gaps
    all_gaps = list(gaps) + [gap_to_end]
    
    # If any gap is >= 36 months, item has been discontinued or inactive
    max_gap = max(all_gaps) if all_gaps else 0
    
    if max_gap >= max_gap_months:
        print(f"    (Found {max_gap}-month gap in sales - likely discontinued)")
        return True
    
    return False

def calculate_demand_characteristics(y_series):
    """
    Calculate ADI (Average Demand Interval) and CV² (Coefficient of Variation²)
    Used to classify demand patterns
    
    Returns:
    - adi: Average Demand Interval (avg months between non-zero demands)
    - cv2: Coefficient of Variation² (variance in demand SIZE)
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
    
    # Calculate CV² (Coefficient of Variation squared)
    # Measures variability in demand SIZE (when non-zero)
    mean_demand = non_zero_values.mean()
    std_demand = non_zero_values.std()
    cv = std_demand / mean_demand if mean_demand > 0 else 0
    cv2 = cv ** 2
    
    # Classify demand pattern (Syntetos et al. 2005)
    # ADI threshold: 1.32 (about monthly vs less frequent)
    # CV² threshold: 0.49 (consistent size vs variable)
    
    if adi < 1.32 and cv2 < 0.49:
        demand_class = 'SMOOTH'
    elif adi >= 1.32 and cv2 < 0.49:
        demand_class = 'INTERMITTENT'
    elif adi < 1.32 and cv2 >= 0.49:
        demand_class = 'ERRATIC'
    else:  # adi >= 1.32 and cv2 >= 0.49
        demand_class = 'LUMPY'
    
    return adi, cv2, demand_class

def find_first_sale_month(monthly_series):
    """Find first month with non-zero sales"""
    if monthly_series is None or len(monthly_series) == 0:
        return None
    
    non_zero = monthly_series[monthly_series > 0]
    if len(non_zero) == 0:
        return None
    
    return non_zero.index[0]

# ============================================================
# GLOBAL MODELS - Train on all SKUs together
# ============================================================

def build_global_training_data(all_sku_data):
    """
    Build training dataset for global models
    all_sku_data: list of dicts with {'sku': str, 'y_train': Series}
    """
    print("\n" + "="*70)
    print("BUILDING GLOBAL MODEL TRAINING DATA")
    print("="*70)
    
    all_records = []
    sku_encodings = {}
    
    for idx, sku_data in enumerate(all_sku_data):
        sku = sku_data['sku']
        y_train = sku_data['y_train']
        
        # Assign SKU encoding
        sku_encodings[sku] = idx
        sku_id = idx
        
        # Calculate SKU-level statistics
        avg_demand = y_train.mean()
        std_demand = y_train.std()
        zero_pct = (y_train == 0).sum() / len(y_train)
        
        # Create lagged features for each time point
        lags = [1, 3, 6, 12]
        for i in range(max(lags), len(y_train)):
            record = {
                'sku_id': sku_id,
                'target': y_train.iloc[i],
                'lag_1': y_train.iloc[i-1],
                'lag_3': y_train.iloc[i-3],
                'lag_6': y_train.iloc[i-6],
                'lag_12': y_train.iloc[i-12],
                'roll_3': y_train.iloc[i-3:i].mean(),
                'roll_6': y_train.iloc[i-6:i].mean(),
                'roll_12': y_train.iloc[i-12:i].mean(),
                'month': y_train.index[i].month,
                'quarter': y_train.index[i].quarter,
                'sku_avg_demand': avg_demand,
                'sku_std_demand': std_demand,
                'sku_zero_pct': zero_pct,
            }
            all_records.append(record)
    
    df = pd.DataFrame(all_records)
    print(f"✓ Built global dataset: {len(df):,} records from {len(sku_encodings)} SKUs")
    
    return df, sku_encodings

def train_global_models(df_global):
    """Train global XGBoost, Neural Network, and Random Forest"""
    print("\nTraining global models...")
    
    feature_cols = ['sku_id', 'lag_1', 'lag_3', 'lag_6', 'lag_12',
                    'roll_3', 'roll_6', 'roll_12', 'month', 'quarter',
                    'sku_avg_demand', 'sku_std_demand', 'sku_zero_pct']
    
    X = df_global[feature_cols].values
    y = df_global['target'].values
    
    # Split for validation
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    
    models = {}
    
    # 1. Global XGBoost
    print("  Training Global XGBoost...")
    xgb_model = XGBRegressor(n_estimators=200, learning_rate=0.05, 
                             max_depth=6, random_state=42, n_jobs=-1)
    xgb_model.fit(X_train, y_train)
    models['xgboost'] = xgb_model
    
    # 2. Global Neural Network
    print("  Training Global Neural Network...")
    nn_model = MLPRegressor(hidden_layer_sizes=(100, 50), max_iter=500, 
                           random_state=42)
    nn_model.fit(X_train, y_train)
    models['neural_network'] = nn_model
    
    # 3. Global Random Forest
    print("  Training Global Random Forest...")
    rf_model = RandomForestRegressor(n_estimators=200, max_depth=10, 
                                    random_state=42, n_jobs=-1)
    rf_model.fit(X_train, y_train)
    models['random_forest'] = rf_model
    
    print("✓ Global models trained")
    
    return models, feature_cols

def forecast_global_model(model, model_type, feature_cols, y_train, sku_id, 
                         sku_avg, sku_std, sku_zero_pct, horizon):
    """Generate forecast using a global model"""
    try:
        preds = []
        hist = y_train.copy()
        
        for i in range(horizon):
            # Get next month
            if len(hist) > 0:
                next_month = hist.index[-1] + pd.DateOffset(months=1)
            else:
                return None, "Empty history"
            
            # Build feature vector
            features = {
                'sku_id': sku_id,
                'lag_1': hist.iloc[-1] if len(hist) >= 1 else 0,
                'lag_3': hist.iloc[-3] if len(hist) >= 3 else 0,
                'lag_6': hist.iloc[-6] if len(hist) >= 6 else 0,
                'lag_12': hist.iloc[-12] if len(hist) >= 12 else 0,
                'roll_3': hist.iloc[-3:].mean() if len(hist) >= 3 else hist.mean(),
                'roll_6': hist.iloc[-6:].mean() if len(hist) >= 6 else hist.mean(),
                'roll_12': hist.iloc[-12:].mean() if len(hist) >= 12 else hist.mean(),
                'month': next_month.month,
                'quarter': next_month.quarter,
                'sku_avg_demand': sku_avg,
                'sku_std_demand': sku_std,
                'sku_zero_pct': sku_zero_pct,
            }
            
            X_new = np.array([[features[col] for col in feature_cols]])
            pred = max(0.0, model.predict(X_new)[0])
            preds.append(pred)
            
            # Add to history
            new_row = pd.Series([pred], index=[next_month])
            hist = pd.concat([hist, new_row])
        
        return np.array(preds), None
    except Exception as e:
        return None, str(e)

# ============================================================
# ACCURACY METRICS
# ============================================================

def calculate_metrics(y_true, y_pred, y_train=None):
    """Calculate all 8 accuracy metrics"""
    y_true = np.array(y_true, dtype=float)
    y_pred = np.array(y_pred, dtype=float)
    
    # Ensure same length
    min_len = min(len(y_true), len(y_pred))
    y_true = y_true[:min_len]
    y_pred = y_pred[:min_len]
    
    # Remove any NaN or inf values
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    y_true = y_true[mask]
    y_pred = y_pred[mask]
    
    if len(y_true) == 0:
        return {k: np.nan for k in METRIC_WEIGHTS.keys()}
    
    metrics = {}
    
    # 1. wMAPE (Weighted Mean Absolute Percentage Error)
    denom = np.sum(np.abs(y_true))
    metrics['wMAPE'] = (np.sum(np.abs(y_true - y_pred)) / denom * 100) if denom > 0 else np.nan
    
    # 2. RMSE (Root Mean Squared Error)
    metrics['RMSE'] = np.sqrt(np.mean((y_true - y_pred) ** 2))
    
    # 3. MAE (Mean Absolute Error)
    metrics['MAE'] = np.mean(np.abs(y_true - y_pred))
    
    # 4. MAPE (Mean Absolute Percentage Error)
    # Only for non-zero actuals
    non_zero_mask = y_true != 0
    if np.sum(non_zero_mask) > 0:
        metrics['MAPE'] = np.mean(np.abs((y_true[non_zero_mask] - y_pred[non_zero_mask]) / y_true[non_zero_mask])) * 100
    else:
        metrics['MAPE'] = np.nan
    
    # 5. sMAPE (Symmetric MAPE)
    denominator = (np.abs(y_true) + np.abs(y_pred)) / 2
    valid = denominator > 0
    if np.sum(valid) > 0:
        metrics['sMAPE'] = np.mean(np.abs(y_true[valid] - y_pred[valid]) / denominator[valid]) * 100
    else:
        metrics['sMAPE'] = np.nan
    
    # 6. MPE (Mean Percentage Error)
    if np.sum(non_zero_mask) > 0:
        metrics['MPE'] = np.mean((y_true[non_zero_mask] - y_pred[non_zero_mask]) / y_true[non_zero_mask]) * 100
    else:
        metrics['MPE'] = np.nan
    
    # 7. MASE (Mean Absolute Scaled Error)
    if y_train is not None and len(y_train) > 1:
        naive_mae = np.mean(np.abs(np.diff(y_train)))
        if naive_mae > 0:
            metrics['MASE'] = metrics['MAE'] / naive_mae
        else:
            metrics['MASE'] = np.nan
    else:
        metrics['MASE'] = np.nan
    
    # 8. WAPE (Weighted Absolute Percentage Error)
    metrics['WAPE'] = metrics['wMAPE']  # Same as wMAPE
    
    return metrics

def calculate_weighted_score(metrics):
    """Calculate weighted composite score (lower is better)"""
    score = 0.0
    total_weight = 0.0
    
    for metric, weight in METRIC_WEIGHTS.items():
        if not np.isnan(metrics.get(metric, np.nan)):
            score += metrics[metric] * weight
            total_weight += weight
    
    if total_weight > 0:
        return score / total_weight
    else:
        return np.nan

# ============================================================
# FORECASTING MODELS
# ============================================================

def forecast_arima(y_train, horizon, order):
    """ARIMA models"""
    try:
        model = ARIMA(y_train, order=order)
        fitted = model.fit()
        forecast = fitted.forecast(steps=horizon)
        return np.maximum(0, forecast.values), None
    except Exception as e:
        return None, str(e)

def forecast_sarima(y_train, horizon, order, seasonal_order=(0,0,0,12)):
    """SARIMA model"""
    try:
        model = SARIMAX(y_train, order=order, seasonal_order=seasonal_order)
        fitted = model.fit(disp=False)
        forecast = fitted.forecast(steps=horizon)
        return np.maximum(0, forecast.values), None
    except Exception as e:
        return None, str(e)

def forecast_ses(y_train, horizon):
    """Simple Exponential Smoothing"""
    try:
        model = SimpleExpSmoothing(y_train)
        fitted = model.fit()
        forecast = fitted.forecast(steps=horizon)
        return np.maximum(0, forecast.values), None
    except Exception as e:
        return None, str(e)

def forecast_holt(y_train, horizon, damped=False):
    """Holt's Linear Trend"""
    try:
        model = Holt(y_train, damped_trend=damped)
        fitted = model.fit()
        forecast = fitted.forecast(steps=horizon)
        return np.maximum(0, forecast.values), None
    except Exception as e:
        return None, str(e)

def forecast_holtwinters(y_train, horizon):
    """Holt-Winters (Exponential Smoothing with Trend and Seasonality)"""
    try:
        if len(y_train) < 24:  # Need at least 2 years
            return None, "Insufficient data for Holt-Winters"
        
        model = ExponentialSmoothing(y_train, seasonal_periods=12, 
                                     trend='add', seasonal='add')
        fitted = model.fit()
        forecast = fitted.forecast(steps=horizon)
        return np.maximum(0, forecast.values), None
    except Exception as e:
        return None, str(e)

def forecast_neural_network(y_train, horizon, lags=[1, 3, 6, 12]):
    """Neural Network (MLP) with lagged features"""
    try:
        if len(y_train) < max(lags) + 10:
            return None, "Insufficient data"
        
        X, y = [], []
        for i in range(max(lags), len(y_train)):
            X.append([y_train.iloc[i-lag] if i >= lag else 0 for lag in lags])
            y.append(y_train.iloc[i])
        
        model = MLPRegressor(hidden_layer_sizes=(50, 25), max_iter=500, random_state=42)
        model.fit(np.array(X), np.array(y))
        
        # Forecast
        preds = []
        hist = y_train.values.tolist()
        for _ in range(horizon):
            x_new = [hist[-lag] if lag <= len(hist) else 0 for lag in lags]
            pred = max(0.0, model.predict([x_new])[0])
            preds.append(pred)
            hist.append(pred)
        
        return np.array(preds), None
    except Exception as e:
        return None, str(e)

def forecast_random_forest(y_train, horizon, lags=[1, 3, 6, 12]):
    """Random Forest with lagged features"""
    try:
        if len(y_train) < max(lags) + 10:
            return None, "Insufficient data"
        
        X, y = [], []
        for i in range(max(lags), len(y_train)):
            X.append([y_train.iloc[i-lag] if i >= lag else 0 for lag in lags])
            y.append(y_train.iloc[i])
        
        model = RandomForestRegressor(n_estimators=100, max_depth=10, random_state=42)
        model.fit(np.array(X), np.array(y))
        
        # Forecast
        preds = []
        hist = y_train.values.tolist()
        for _ in range(horizon):
            x_new = [hist[-lag] if lag <= len(hist) else 0 for lag in lags]
            pred = max(0.0, model.predict([x_new])[0])
            preds.append(pred)
            hist.append(pred)
        
        return np.array(preds), None
    except Exception as e:
        return None, str(e)

def forecast_xgboost(y_train, horizon, lags=[1, 3, 6, 12]):
    """XGBoost with lagged features"""
    try:
        if len(y_train) < max(lags) + 10:
            return None, "Insufficient data"
        
        X, y = [], []
        for i in range(max(lags), len(y_train)):
            X.append([y_train.iloc[i-lag] if i >= lag else 0 for lag in lags])
            y.append(y_train.iloc[i])
        
        model = XGBRegressor(n_estimators=100, learning_rate=0.1, max_depth=6, random_state=42)
        model.fit(np.array(X), np.array(y))
        
        # Forecast
        preds = []
        hist = y_train.values.tolist()
        for _ in range(horizon):
            x_new = [hist[-lag] if lag <= len(hist) else 0 for lag in lags]
            pred = max(0.0, model.predict([x_new])[0])
            preds.append(pred)
            hist.append(pred)
        
        return np.array(preds), None
    except Exception as e:
        return None, str(e)

def forecast_croston(y_train, horizon, alpha=0.1):
    """Croston's Method"""
    try:
        y = y_train.values.astype(float)
        non_zero_idx = np.where(y > 0)[0]
        
        if len(non_zero_idx) < 2:
            return np.array([y_train.mean()] * horizon), None
        
        demand_sizes = y[non_zero_idx]
        intervals = np.diff(non_zero_idx)
        
        z = demand_sizes[0]
        p = intervals[0] if len(intervals) > 0 else 1
        
        for i in range(1, len(non_zero_idx)):
            z = alpha * demand_sizes[i] + (1 - alpha) * z
            if i < len(intervals):
                p = alpha * intervals[i] + (1 - alpha) * p
        
        forecast_value = z / p if p > 0 else z
        return np.array([forecast_value] * horizon), None
    except Exception as e:
        return None, str(e)

def forecast_sba(y_train, horizon, alpha=0.1):
    """Syntetos-Boylan Approximation"""
    try:
        y = y_train.values.astype(float)
        non_zero_idx = np.where(y > 0)[0]
        
        if len(non_zero_idx) < 2:
            return np.array([y_train.mean()] * horizon), None
        
        demand_sizes = y[non_zero_idx]
        intervals = np.diff(non_zero_idx)
        
        z = demand_sizes[0]
        p = intervals[0] if len(intervals) > 0 else 1
        
        for i in range(1, len(non_zero_idx)):
            z = alpha * demand_sizes[i] + (1 - alpha) * z
            if i < len(intervals):
                p = alpha * intervals[i] + (1 - alpha) * p
        
        correction = 1 - (alpha / 2)
        forecast_value = (z / p) * correction if p > 0 else z
        return np.array([forecast_value] * horizon), None
    except Exception as e:
        return None, str(e)

def forecast_tsb(y_train, horizon):
    """Teunter-Syntetos-Babai"""
    try:
        y = y_train.values.astype(float)
        non_zero_mask = y > 0
        non_zero_count = np.sum(non_zero_mask)
        
        if non_zero_count == 0:
            return np.zeros(horizon), None
        
        p = non_zero_count / len(y)
        z = np.mean(y[non_zero_mask])
        level = p * z
        
        return np.array([level] * horizon), None
    except Exception as e:
        return None, str(e)

def forecast_bootstrap(y_train, horizon, n_simulations=1000):
    """Bootstrap simulation"""
    try:
        non_zero = y_train[y_train > 0].values
        
        if len(non_zero) == 0:
            return np.zeros(horizon), None
        
        if len(non_zero) < 3:
            return np.array([y_train.mean()] * horizon), None
        
        p_nonzero = len(non_zero) / len(y_train)
        
        simulations = []
        for _ in range(n_simulations):
            forecast_sim = []
            for _ in range(horizon):
                if np.random.random() < p_nonzero:
                    demand = np.random.choice(non_zero)
                else:
                    demand = 0.0
                forecast_sim.append(demand)
            simulations.append(forecast_sim)
        
        forecast_values = np.array(simulations).mean(axis=0)
        return forecast_values, None
    except Exception as e:
        return None, str(e)

def forecast_garma(y_train, horizon):
    """
    GARMA (Generalized Autoregressive Moving Average) for Count Data
    Uses Poisson-like regression with AR structure for intermittent demand
    """
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX
        
        # Transform to ensure positive values
        y_transform = np.maximum(y_train.values, 0.1)
        
        model = SARIMAX(y_transform, order=(1, 0, 1), 
                       enforce_stationarity=False, enforce_invertibility=False)
        fitted = model.fit(disp=False, maxiter=200)
        forecast = fitted.forecast(steps=horizon)
        
        return np.maximum(0, forecast.values), None
    except Exception as e:
        return None, str(e)

def forecast_inarma(y_train, horizon):
    """
    INARMA (Integer-valued ARMA) for Count Data
    Simplified implementation using rounded ARIMA predictions
    """
    try:
        # Use ARIMA but round to integers (count data)
        model = ARIMA(y_train, order=(1, 0, 1))
        fitted = model.fit()
        forecast = fitted.forecast(steps=horizon)
        
        # Round to nearest integer and ensure non-negative
        forecast_int = np.maximum(0, np.round(forecast.values))
        
        return forecast_int, None
    except Exception as e:
        return None, str(e)

def forecast_zero_inflated(y_train, horizon):
    """
    Zero-Inflated Model
    Separately models: (1) probability of zero, (2) non-zero amount
    Similar to Croston but uses logistic for zero probability
    """
    try:
        y = y_train.values.astype(float)
        
        # Calculate probability of zero
        zero_count = np.sum(y == 0)
        p_zero = zero_count / len(y)
        
        # Get non-zero values
        non_zero = y[y > 0]
        
        if len(non_zero) == 0:
            return np.zeros(horizon), None
        
        # Model non-zero values with exponential smoothing
        if len(non_zero) >= 3:
            non_zero_mean = non_zero.mean()
        else:
            non_zero_mean = non_zero[0]
        
        # Forecast: probability of non-zero × expected non-zero value
        forecast_value = (1 - p_zero) * non_zero_mean
        
        return np.array([forecast_value] * horizon), None
    except Exception as e:
        return None, str(e)

def forecast_hurdle(y_train, horizon):
    """
    Hurdle Regression Model
    Two-part model: (1) Binary model for zero vs non-zero, (2) Truncated model for positive values
    """
    try:
        y = y_train.values.astype(float)
        
        # Part 1: Model probability of non-zero (hurdle)
        non_zero_mask = y > 0
        p_nonzero = np.mean(non_zero_mask)
        
        # Part 2: Model expected value given non-zero
        non_zero_values = y[non_zero_mask]
        
        if len(non_zero_values) == 0:
            return np.zeros(horizon), None
        
        # Use exponential smoothing for non-zero values
        if len(non_zero_values) >= 5:
            # Fit simple exponential smoothing to non-zero values
            alpha = 0.3
            smoothed = non_zero_values[0]
            for val in non_zero_values[1:]:
                smoothed = alpha * val + (1 - alpha) * smoothed
            expected_nonzero = smoothed
        else:
            expected_nonzero = non_zero_values.mean()
        
        # Combine: P(Y>0) × E(Y|Y>0)
        forecast_value = p_nonzero * expected_nonzero
        
        return np.array([forecast_value] * horizon), None
    except Exception as e:
        return None, str(e)

# ============================================================
# GLOBAL MODELS - Train on all SKUs together
# ============================================================

def build_global_training_data(all_sku_data):
    """
    Build training dataset for global models
    all_sku_data: list of dicts with {'sku': str, 'y_train': Series}
    """
    print("\n" + "="*70)
    print("BUILDING GLOBAL MODEL TRAINING DATA")
    print("="*70)
    
    all_records = []
    sku_encodings = {}
    
    for idx, sku_data in enumerate(all_sku_data):
        sku = sku_data['sku']
        y_train = sku_data['y_train']
        
        # Assign SKU encoding
        sku_encodings[sku] = idx
        sku_id = idx
        
        # Calculate SKU-level statistics
        avg_demand = y_train.mean()
        std_demand = y_train.std()
        zero_pct = (y_train == 0).sum() / len(y_train)
        
        # Create lagged features for each time point
        lags = [1, 3, 6, 12]
        for i in range(max(lags), len(y_train)):
            record = {
                'sku_id': sku_id,
                'target': y_train.iloc[i],
                'lag_1': y_train.iloc[i-1],
                'lag_3': y_train.iloc[i-3],
                'lag_6': y_train.iloc[i-6],
                'lag_12': y_train.iloc[i-12],
                'roll_3': y_train.iloc[i-3:i].mean(),
                'roll_6': y_train.iloc[i-6:i].mean(),
                'roll_12': y_train.iloc[i-12:i].mean(),
                'month': y_train.index[i].month,
                'quarter': y_train.index[i].quarter,
                'sku_avg_demand': avg_demand,
                'sku_std_demand': std_demand,
                'sku_zero_pct': zero_pct,
            }
            all_records.append(record)
    
    df = pd.DataFrame(all_records)
    print(f"✓ Built global dataset: {len(df):,} records from {len(sku_encodings)} SKUs")
    
    return df, sku_encodings

def train_global_models(df_global):
    """Train global XGBoost and Neural Network"""
    print("\nTraining global models...")
    
    feature_cols = ['sku_id', 'lag_1', 'lag_3', 'lag_6', 'lag_12',
                    'roll_3', 'roll_6', 'roll_12', 'month', 'quarter',
                    'sku_avg_demand', 'sku_std_demand', 'sku_zero_pct']
    
    X = df_global[feature_cols].values
    y = df_global['target'].values
    
    # Split for validation
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    
    models = {}
    
    # 1. Global XGBoost
    print("  Training Global XGBoost...")
    xgb_model = XGBRegressor(n_estimators=200, learning_rate=0.05, 
                             max_depth=6, random_state=42, n_jobs=-1)
    xgb_model.fit(X_train, y_train)
    models['xgboost'] = xgb_model
    
    # 2. Global Neural Network
    print("  Training Global Neural Network...")
    nn_model = MLPRegressor(hidden_layer_sizes=(100, 50), max_iter=500, 
                           random_state=42)
    nn_model.fit(X_train, y_train)
    models['neural_network'] = nn_model
    
    print("✓ Global models trained")
    
    return models, feature_cols

def forecast_global_model(model, model_type, feature_cols, y_train, sku_id, 
                         sku_avg, sku_std, sku_zero_pct, horizon):
    """Generate forecast using a global model"""
    try:
        preds = []
        hist = y_train.copy()
        
        for i in range(horizon):
            # Get next month
            if len(hist) > 0:
                next_month = hist.index[-1] + pd.DateOffset(months=1)
            else:
                return None, "Empty history"
            
            # Build feature vector
            features = {
                'sku_id': sku_id,
                'lag_1': hist.iloc[-1] if len(hist) >= 1 else 0,
                'lag_3': hist.iloc[-3] if len(hist) >= 3 else 0,
                'lag_6': hist.iloc[-6] if len(hist) >= 6 else 0,
                'lag_12': hist.iloc[-12] if len(hist) >= 12 else 0,
                'roll_3': hist.iloc[-3:].mean() if len(hist) >= 3 else hist.mean(),
                'roll_6': hist.iloc[-6:].mean() if len(hist) >= 6 else hist.mean(),
                'roll_12': hist.iloc[-12:].mean() if len(hist) >= 12 else hist.mean(),
                'month': next_month.month,
                'quarter': next_month.quarter,
                'sku_avg_demand': sku_avg,
                'sku_std_demand': sku_std,
                'sku_zero_pct': sku_zero_pct,
            }
            
            X_new = np.array([[features[col] for col in feature_cols]])
            pred = max(0.0, model.predict(X_new)[0])
            preds.append(pred)
            
            # Add to history
            new_row = pd.Series([pred], index=[next_month])
            hist = pd.concat([hist, new_row])
        
        return np.array(preds), None
    except Exception as e:
        return None, str(e)

# ============================================================
# MODEL EVALUATION
# ============================================================

def evaluate_models(y_train, y_test, global_models=None, feature_cols=None, 
                   sku_id=None, sku_avg=None, sku_std=None, sku_zero_pct=None):
    """Evaluate the 9 local models + 2 global models (if available)"""
    horizon = len(y_test)
    
    # 9 local models
    models = {
        'Holt (Damped)': lambda: forecast_holt(y_train, horizon, damped=True),
        'Neural Network (Local)': lambda: forecast_neural_network(y_train, horizon),
        'SARIMA(0,1,1)': lambda: forecast_sarima(y_train, horizon, (0,1,1)),
        'XGBoost (Local)': lambda: forecast_xgboost(y_train, horizon),
        'Random Forest (Local)': lambda: forecast_random_forest(y_train, horizon),
        'Croston': lambda: forecast_croston(y_train, horizon),
        'SBA': lambda: forecast_sba(y_train, horizon),
        'Bootstrap': lambda: forecast_bootstrap(y_train, horizon),
        'TSB': lambda: forecast_tsb(y_train, horizon),
        'Hurdle': lambda: forecast_hurdle(y_train, horizon),
    }
    
    # Add global models if available
    if global_models and feature_cols and sku_id is not None:
        models['XGBoost (Global)'] = lambda: forecast_global_model(
            global_models['xgboost'], 'xgboost', feature_cols, y_train,
            sku_id, sku_avg, sku_std, sku_zero_pct, horizon
        )
        models['Neural Network (Global)'] = lambda: forecast_global_model(
            global_models['neural_network'], 'neural_network', feature_cols, y_train,
            sku_id, sku_avg, sku_std, sku_zero_pct, horizon
        )
    
    results = {}
    
    for model_name, model_func in models.items():
        try:
            forecast, error = model_func()
            
            if forecast is None:
                results[model_name] = {
                    'status': 'FAILED',
                    'error': error,
                    **{k: np.nan for k in METRIC_WEIGHTS.keys()},
                    'weighted_score': np.nan
                }
            else:
                # Calculate metrics
                metrics = calculate_metrics(y_test.values, forecast, y_train.values)
                weighted_score = calculate_weighted_score(metrics)
                
                results[model_name] = {
                    'status': 'SUCCESS',
                    'error': None,
                    **metrics,
                    'weighted_score': weighted_score
                }
        except Exception as e:
            results[model_name] = {
                'status': 'ERROR',
                'error': str(e),
                **{k: np.nan for k in METRIC_WEIGHTS.keys()},
                'weighted_score': np.nan
            }
    
    return results

# ============================================================
# MAIN PROCESSING
# ============================================================

def run_diagnostics():
    """Main diagnostic routine"""
    print("="*70)
    print("FORECASTING MODEL DIAGNOSTICS")
    print("="*70)
    print(f"SKU List: {FORECAST_SKU_LIST_FILE}")
    print(f"Sales since: {CUTOFF_DATE}")
    print(f"Min history: {MIN_HISTORY_MONTHS} months")
    print(f"Test period: {TEST_MONTHS} months")
    print(f"Models to evaluate: 10 local + 2 global = 12 total")
    print(f"3-year gap filter: ENABLED (excludes discontinued items)")
    print(f"Demand classification: ADI/CV² (SMOOTH, INTERMITTENT, ERRATIC, LUMPY)")
    print("="*70)
    
    # Load SKU list from Excel
    sku_list = load_forecast_sku_list()
    
    if not sku_list:
        print("ERROR: No SKUs loaded from forecast list!")
        return
    
    # Connect to database
    print("\nConnecting to database...")
    conn = connect_to_database()
    print("✓ Connected")
    
    # Get SKU details from database
    sku_master = get_skus_from_database(conn, sku_list)
    
    if sku_master.empty:
        print("ERROR: No items found in database!")
        conn.close()
        return
    
    # ========================================================================
    # PHASE 1: BUILD GLOBAL MODELS
    # ========================================================================
    print("\n" + "="*70)
    print("PHASE 1: BUILDING GLOBAL MODELS")
    print("="*70)
    
    valid_skus_for_global = []
    
    for idx, row in sku_master.iterrows():
        try:
            sku = row['ITEM_NUMBER']
            first_receipt_date = row.get('FIRST_RECEIPT_DATE')
            
            if pd.isna(first_receipt_date):
                continue
            
            first_receipt_month = pd.Timestamp(year=first_receipt_date.year, 
                                              month=first_receipt_date.month, 
                                              day=1)
            
            sales_df = load_sales_history(conn, sku)
            if sales_df.empty:
                continue
            
            monthly = aggregate_to_monthly(sales_df)
            if monthly is None or len(monthly) == 0:
                continue
            
            today = pd.Timestamp.today().normalize()
            today_month = pd.Timestamp(year=today.year, month=today.month, day=1)
            full_date_range = pd.date_range(first_receipt_month, today_month, freq='MS')
            monthly_full = monthly.reindex(full_date_range, fill_value=0.0)
            
            if has_long_gap(monthly_full, max_gap_months=36):
                continue
            
            if len(monthly_full) < MIN_HISTORY_MONTHS + TEST_MONTHS:
                continue
            
            train_size = len(monthly_full) - TEST_MONTHS
            y_train = monthly_full.iloc[:train_size]
            
            valid_skus_for_global.append({
                'sku': sku,
                'y_train': y_train
            })
            
            if len(valid_skus_for_global) % 50 == 0:
                print(f"  Loaded {len(valid_skus_for_global)} valid SKUs for global training...")
        except:
            continue
    
    print(f"\n✓ Found {len(valid_skus_for_global)} valid SKUs for global model training")
    
    # Train global models
    global_models = None
    feature_cols = None
    sku_encodings = None
    
    if len(valid_skus_for_global) >= 10:
        df_global, sku_encodings = build_global_training_data(valid_skus_for_global)
        
        if len(df_global) >= 500:
            global_models, feature_cols = train_global_models(df_global)
        else:
            print("⚠ Insufficient data for global models (need 500+ records)")
    else:
        print("⚠ Insufficient SKUs for global models (need 10+)")
    
    # ========================================================================
    # PHASE 2: EVALUATE ALL MODELS PER SKU
    # ========================================================================
    print("\n" + "="*70)
    print("PHASE 2: EVALUATING MODELS PER SKU")
    print("="*70)
    
    all_results = []
    skipped_3yr_gap = 0
    skipped_insufficient = 0
    
    for idx, row in sku_master.iterrows():
        try:
            sku = row['ITEM_NUMBER']
            
            # Get first receipt date from database
            first_receipt_date = row.get('FIRST_RECEIPT_DATE')
            
            if pd.isna(first_receipt_date):
                print(f"\n{sku}: SKIPPED - No first receipt date")
                continue
            
            # Convert to first day of that month
            first_receipt_month = pd.Timestamp(year=first_receipt_date.year, 
                                              month=first_receipt_date.month, 
                                              day=1)
            
            # Load sales history
            sales_df = load_sales_history(conn, sku)
            if sales_df.empty:
                print(f"\n{sku}: SKIPPED - No sales history")
                continue
            
            # Aggregate to monthly
            monthly = aggregate_to_monthly(sales_df)
            if monthly is None or len(monthly) == 0:
                print(f"\n{sku}: SKIPPED - No monthly data")
                continue
            
            # Create complete monthly series from FIRST RECEIPT to TODAY
            today = pd.Timestamp.today().normalize()
            today_month = pd.Timestamp(year=today.year, month=today.month, day=1)
            
            # Create full date range from first receipt to today
            full_date_range = pd.date_range(first_receipt_month, today_month, freq='MS')
            
            # Reindex monthly data to include all months from first receipt to today
            # Fill missing months with 0
            monthly_full = monthly.reindex(full_date_range, fill_value=0.0)
            
            # Get date range for diagnostics
            date_range_months = len(monthly_full)
            
            # Check for 3-year gaps from FIRST RECEIPT to TODAY
            print(f"\n{sku}: Sales window from {first_receipt_month.strftime('%Y-%m')} (first receipt) to {today_month.strftime('%Y-%m')} ({date_range_months} months)")
            
            if has_long_gap(monthly_full, max_gap_months=36):
                skipped_3yr_gap += 1
                print(f"  → SKIPPED: 3+ year gap in sales history")
                continue
            
            if len(monthly_full) < MIN_HISTORY_MONTHS + TEST_MONTHS:
                skipped_insufficient += 1
                print(f"  → SKIPPED: Insufficient history ({len(monthly_full)} months, need {MIN_HISTORY_MONTHS + TEST_MONTHS})")
                continue
            
            # Split train/test
            train_size = len(monthly_full) - TEST_MONTHS
            y_train = monthly_full.iloc[:train_size]
            y_test = monthly_full.iloc[train_size:]
            
            print(f"  ✓ Valid for forecasting")
            print(f"\n{'='*70}")
            print(f"Processing: {sku}")
            print(f"Description: {row['DESCRIPTION']}")
            print(f"Vendor: {row['VENDOR_NUMBER']}")
            print(f"First Receipt: {first_receipt_month.strftime('%Y-%m')}")
            print(f"{'='*70}")
            print(f"  Train: {len(y_train)} months, Test: {len(y_test)} months")
            print(f"  Avg monthly demand: {y_train.mean():.2f} SF")
            print(f"  Non-zero months: {(y_train > 0).sum()} ({100*(y_train > 0).sum()/len(y_train):.1f}%)")
            
            # Calculate demand characteristics
            adi, cv2, demand_class = calculate_demand_characteristics(y_train)
            print(f"  Demand Class: {demand_class} (ADI={adi:.2f}, CV²={cv2:.2f})")
            
            # Get SKU ID and stats for global models
            sku_id = sku_encodings.get(sku) if sku_encodings else None
            sku_avg = y_train.mean()
            sku_std = y_train.std()
            sku_zero_pct = (y_train == 0).sum() / len(y_train)
            
            # Evaluate all models (local + global)
            num_models = 10 + (2 if global_models else 0)
            print(f"  Evaluating {num_models} models...")
            model_results = evaluate_models(y_train, y_test, global_models, feature_cols,
                                           sku_id, sku_avg, sku_std, sku_zero_pct)
            
            # Find best model
            valid_scores = {k: v['weighted_score'] for k, v in model_results.items() 
                            if not np.isnan(v['weighted_score'])}
            
            if valid_scores:
                best_model = min(valid_scores, key=valid_scores.get)
                best_score = valid_scores[best_model]
                print(f"  Best Model: {best_model} (Score: {best_score:.2f})")
            else:
                best_model = None
                best_score = np.nan
                print(f"  All models failed")
            
            result = {
                'sku': sku,
                'description': row['DESCRIPTION'],
                'vendor_number': row['VENDOR_NUMBER'],
                'first_receipt_date': first_receipt_month.strftime('%Y-%m'),
                'demand_class': demand_class,
                'adi': adi,
                'cv2': cv2,
                'train_months': len(y_train),
                'test_months': len(y_test),
                'avg_monthly_demand': y_train.mean(),
                'non_zero_pct': 100 * (y_train > 0).sum() / len(y_train),
                'best_model': best_model,
                'best_score': best_score,
                'model_results': model_results
            }
            
            all_results.append(result)
            
        except Exception as e:
            print(f"\n{sku}: ERROR - {e}")
            import traceback
            traceback.print_exc()
    
    conn.close()
    
    if not all_results:
        print("\nNo SKUs successfully processed")
        print(f"  Skipped (3-year gap): {skipped_3yr_gap}")
        print(f"  Skipped (insufficient history): {skipped_insufficient}")
        return
    
    # Generate summary report
    print("\n" + "="*70)
    print("GENERATING SUMMARY REPORT")
    print("="*70)
    print(f"  Successfully processed: {len(all_results)} SKUs")
    print(f"  Skipped (3-year gap): {skipped_3yr_gap}")
    print(f"  Skipped (insufficient history): {skipped_insufficient}")
    
    # Save detailed results
    output_dir = Path(__file__).parent
    
    # 1. SKU-level summary
    summary_data = []
    for result in all_results:
        summary_data.append({
            'sku': result['sku'],
            'description': result['description'],
            'vendor_number': result['vendor_number'],
            'first_receipt_date': result['first_receipt_date'],
            'demand_class': result['demand_class'],
            'adi': result['adi'],
            'cv2': result['cv2'],
            'train_months': result['train_months'],
            'avg_monthly_demand': result['avg_monthly_demand'],
            'non_zero_pct': result['non_zero_pct'],
            'best_model': result['best_model'],
            'best_score': result['best_score']
        })
    
    df_summary = pd.DataFrame(summary_data)
    
    # 2. Model performance aggregation
    model_stats = {}
    all_model_names = set()
    for result in all_results:
        for model_name in result['model_results'].keys():
            all_model_names.add(model_name)
    
    for model_name in all_model_names:
        scores = []
        metrics_list = {k: [] for k in METRIC_WEIGHTS.keys()}
        
        for result in all_results:
            if model_name in result['model_results']:
                mr = result['model_results'][model_name]
                if mr['status'] == 'SUCCESS' and not np.isnan(mr['weighted_score']):
                    scores.append(mr['weighted_score'])
                    for metric in METRIC_WEIGHTS.keys():
                        if not np.isnan(mr[metric]):
                            metrics_list[metric].append(mr[metric])
        
        model_stats[model_name] = {
            'count_success': len(scores),
            'avg_score': np.mean(scores) if scores else np.nan,
            'median_score': np.median(scores) if scores else np.nan,
            'min_score': np.min(scores) if scores else np.nan,
            'max_score': np.max(scores) if scores else np.nan,
            **{f'avg_{k}': np.mean(v) if v else np.nan for k, v in metrics_list.items()}
        }
    
    df_model_stats = pd.DataFrame(model_stats).T
    df_model_stats = df_model_stats.sort_values('avg_score')
    
    # 3. Detailed results per SKU per model
    detail_data = []
    for result in all_results:
        for model_name, model_result in result['model_results'].items():
            row = {
                'sku': result['sku'],
                'model': model_name,
                'status': model_result['status'],
                'weighted_score': model_result['weighted_score'],
                **{k: model_result[k] for k in METRIC_WEIGHTS.keys()}
            }
            detail_data.append(row)
    
    df_details = pd.DataFrame(detail_data)
    
    # 4. Best model by demand class
    demand_class_best = {}
    for demand_class in ['SMOOTH', 'INTERMITTENT', 'ERRATIC', 'LUMPY']:
        class_skus = [r for r in all_results if r['demand_class'] == demand_class]
        
        if not class_skus:
            continue
        
        # Aggregate model performance for this demand class
        class_model_stats = {}
        for model_name in all_model_names:
            scores = []
            for result in class_skus:
                if model_name in result['model_results']:
                    mr = result['model_results'][model_name]
                    if mr['status'] == 'SUCCESS' and not np.isnan(mr['weighted_score']):
                        scores.append(mr['weighted_score'])
            
            if scores:
                class_model_stats[model_name] = {
                    'demand_class': demand_class,
                    'count_skus': len(class_skus),
                    'count_success': len(scores),
                    'avg_score': np.mean(scores),
                    'median_score': np.median(scores),
                    'min_score': np.min(scores),
                    'max_score': np.max(scores)
                }
        
        demand_class_best[demand_class] = class_model_stats
    
    # Convert to DataFrame
    class_best_data = []
    for demand_class, models in demand_class_best.items():
        for model_name, stats in models.items():
            class_best_data.append({
                'demand_class': demand_class,
                'model': model_name,
                **stats
            })
    
    df_class_best = pd.DataFrame(class_best_data)
    if not df_class_best.empty:
        df_class_best = df_class_best.sort_values(['demand_class', 'avg_score'])
    
    # Save to Excel
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f'forecast_diagnostics_{timestamp}.xlsx'
    
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        df_summary.to_excel(writer, sheet_name='SKU_Summary', index=False)
        df_model_stats.to_excel(writer, sheet_name='Model_Performance')
        if not df_class_best.empty:
            df_class_best.to_excel(writer, sheet_name='Best_By_Demand_Class', index=False)
        df_details.to_excel(writer, sheet_name='Detailed_Results', index=False)
    
    print(f"\n✓ Results saved: {output_file}")
    print(f"\nTop 5 Models by Average Score:")
    print(df_model_stats[['count_success', 'avg_score', 'median_score']].head())
    print(f"\nBottom 5 Models by Average Score:")
    print(df_model_stats[['count_success', 'avg_score', 'median_score']].tail())
    
    # Print demand class distribution
    if not df_summary.empty and 'demand_class' in df_summary.columns:
        print(f"\n{'='*70}")
        print("DEMAND CLASS DISTRIBUTION")
        print(f"{'='*70}")
        demand_counts = df_summary['demand_class'].value_counts()
        for demand_class, count in demand_counts.items():
            pct = 100 * count / len(df_summary)
            print(f"  {demand_class}: {count} SKUs ({pct:.1f}%)")
        
        # Print best model by demand class
        if not df_class_best.empty:
            print(f"\n{'='*70}")
            print("BEST MODEL BY DEMAND CLASS")
            print(f"{'='*70}")
            for demand_class in ['SMOOTH', 'INTERMITTENT', 'ERRATIC', 'LUMPY']:
                class_data = df_class_best[df_class_best['demand_class'] == demand_class]
                if not class_data.empty:
                    best_row = class_data.iloc[0]
                    print(f"\n{demand_class}:")
                    print(f"  Best Model: {best_row['model']}")
                    print(f"  Avg Score: {best_row['avg_score']:.2f}")
                    print(f"  Success Rate: {best_row['count_success']}/{best_row['count_skus']} SKUs")


if __name__ == "__main__":
    run_diagnostics()
