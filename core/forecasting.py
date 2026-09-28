"""
Shared forecasting algorithms for OMP Forecasting applications.

This module contains demand classification, forecasting methods, and model selection
logic that is shared across sundries, moulding, and other non-flooring products.
"""

from __future__ import annotations
import math
import os
import sys
import warnings
from typing import Any, Dict, List, Optional, Tuple

# Suppress sklearn deprecation warnings about joblib/delayed
warnings.filterwarnings("ignore", message=".*sklearn.utils._joblib.*")
warnings.filterwarnings("ignore", message=".*delayed.*Parallel.*")
warnings.filterwarnings("ignore", category=FutureWarning, module="sklearn")

import numpy as np
import pandas as pd

# Prophet (and its Stan backend) is one of the slowest packages to import, and is only
# needed during an actual forecast run — so it's loaded lazily on first use instead of
# unconditionally at module import time (which would tax every dashboard page view).
_prophet_class = None
_prophet_checked = False


def _get_prophet_class():
    global _prophet_class, _prophet_checked
    if not _prophet_checked:
        try:
            from prophet import Prophet as _Prophet
        except ImportError:
            try:
                from fbprophet import Prophet as _Prophet
            except ImportError:
                _Prophet = None
        _prophet_class = _Prophet
        _prophet_checked = True
    return _prophet_class

from .config import (
    FUTURE_FORECAST_WEEKS,
    DAYS_PER_WEEK,
    WEEKS_PER_MONTH,
    Z_SCORE,
    SS_UPLIFT_SCALAR,
    LUMPY_PCTL_NONZERO,
    LUMPY_LT_BUFFER_FRAC,
    SS_FLOOR_DAYS,
    SS_CAP_MONTHS,
    COVERAGE_HORIZON_DEFAULT,
    MIN_LEAD_TIME_DAYS,
)
from .demand_adjustment import make_return_aware_daily_series


# ============================================================
# DATA PREPROCESSING
# ============================================================
def aggregate_to_weekly(df: pd.DataFrame, end_date: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Aggregate daily transaction data to weekly totals, filling zero-demand weeks."""
    if df.empty:
        return df
    # Defensive normalization: ensure returns are paired against prior sales
    # before weekly aggregation (prevents negative-demand artifacts).
    daily = make_return_aware_daily_series(df, date_col='transaction_date', qty_col='quantity_shipped')
    if daily.empty:
        return pd.DataFrame(columns=['week', 'quantity'])

    daily['week'] = daily['transaction_date'].dt.to_period('W').dt.to_timestamp()
    weekly = daily.groupby('week').agg({'quantity_shipped': 'sum'}).reset_index()
    weekly.columns = ['week', 'quantity']

    # Reindex to include ALL weeks between first sale and requested end week.
    # This keeps trailing zero-demand weeks so models can see recent inactivity.
    if len(weekly) >= 1:
        start_week = pd.Timestamp(weekly['week'].min())
        if end_date is not None:
            end_ts = pd.Timestamp(end_date).normalize()
            end_week = end_ts.to_period('W').start_time
        else:
            end_week = pd.Timestamp(weekly['week'].max())
        if end_week < start_week:
            end_week = start_week
        full_weeks = pd.date_range(
            start=start_week,
            end=end_week,
            freq='W-MON',
        )
        weekly = (
            weekly.set_index('week')
            .reindex(full_weeks, fill_value=0.0)
            .rename_axis('week')
            .reset_index()
        )

    return weekly


def build_rolling_origin_splits(
    n_points: int,
    demand_class: str = "",
    max_folds: int = 4,
) -> List[Tuple[int, int]]:
    """Build rolling-origin train/validation split indices [(train_end, val_end), ...]."""
    if n_points < 6:
        return []

    intermittent_classes = {"INTERMITTENT", "ERRATIC", "LUMPY", "NO_DEMAND"}
    base_val = 4 if str(demand_class).upper() in intermittent_classes else 8
    val_size = max(2, min(base_val, n_points // 4 if n_points >= 8 else 2))
    min_train = max(6, val_size * 2)

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
    median = series.median()
    mad = np.median(np.abs(series - median))
    # Convert MAD to std-equivalent (for normal distribution, std ~ 1.4826 * MAD)
    std_est = 1.4826 * mad
    if std_est == 0:
        return series
    upper_bound = median + n_mad * std_est
    capped = series.clip(upper=upper_bound)
    n_capped = (series > upper_bound).sum()
    if n_capped > 0:
        print(f"    Capped {n_capped} outlier(s) exceeding {upper_bound:,.0f} (median + {n_mad} MAD)")
    return capped


# ============================================================
# DEMAND CLASSIFICATION
# ============================================================
def classify_demand_pattern(df_weekly: pd.DataFrame) -> Tuple[str, float, float]:
    """
    Classify demand pattern using ADI and CV² metrics.

    Returns:
        Tuple of (demand_class, adi, cv2) where demand_class is one of:
        SMOOTH, INTERMITTENT, ERRATIC, LUMPY, UNKNOWN, NO_DEMAND
    """
    if df_weekly.empty or len(df_weekly) < 4:
        return 'UNKNOWN', 0.0, 0.0

    demand = df_weekly['quantity'].values
    nonzero_demand = demand[demand > 0]

    if len(nonzero_demand) == 0:
        return 'NO_DEMAND', 0.0, 0.0

    # Calculate Average Demand Interval (ADI)
    nonzero_indices = np.where(demand > 0)[0]
    if len(nonzero_indices) <= 1:
        adi = len(demand)
    else:
        intervals = np.diff(nonzero_indices)
        adi = float(np.mean(intervals))

    # Calculate Coefficient of Variation Squared (CV²)
    if len(nonzero_demand) < 2:
        cv2 = 0.0
    else:
        mean_demand = float(np.mean(nonzero_demand))
        std_demand = float(np.std(nonzero_demand, ddof=1))
        if mean_demand > 0:
            cv2 = (std_demand / mean_demand) ** 2
        else:
            cv2 = 0.0

    # Classify based on thresholds
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


# ============================================================
# ERROR METRICS
# ============================================================
def wmape(y_true, y_pred) -> float:
    """Weighted Mean Absolute Percentage Error."""
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0 if np.sum(np.abs(y_pred)) == 0 else 1000.0
    return float(np.sum(np.abs(y_true - y_pred)) / denom) * 100


def wape(y_true, y_pred) -> float:
    """Weighted Absolute Percentage Error (alias of wMAPE)."""
    return wmape(y_true, y_pred)


def _naive_scale(y_train: np.ndarray, seasonal_period: int = 1) -> Tuple[float, float]:
    """Return mean abs diff and mean squared diff for a seasonal naive baseline."""
    y_train = np.asarray(y_train, dtype=float)
    n = len(y_train)
    m = int(seasonal_period) if seasonal_period and seasonal_period > 0 else 1
    if n <= m:
        return 0.0, 0.0
    diffs = y_train[m:] - y_train[:-m]
    mae_scale = float(np.mean(np.abs(diffs))) if len(diffs) > 0 else 0.0
    mse_scale = float(np.mean(diffs ** 2)) if len(diffs) > 0 else 0.0
    return mae_scale, mse_scale


def mase(y_true, y_pred, y_train, seasonal_period: int = 1) -> float:
    """Mean Absolute Scaled Error."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mae = float(np.mean(np.abs(y_true - y_pred))) if len(y_true) > 0 else 0.0
    mae_scale, _ = _naive_scale(y_train, seasonal_period)
    if mae_scale == 0:
        return float("inf") if mae > 0 else 0.0
    return mae / mae_scale


def rmsse(y_true, y_pred, y_train, seasonal_period: int = 1) -> float:
    """Root Mean Squared Scaled Error."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mse = float(np.mean((y_true - y_pred) ** 2)) if len(y_true) > 0 else 0.0
    _, mse_scale = _naive_scale(y_train, seasonal_period)
    if mse_scale == 0:
        return float("inf") if mse > 0 else 0.0
    return math.sqrt(mse / mse_scale)


def bias_pct(y_true, y_pred) -> float:
    """Signed bias as a percent of total actual volume."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0
    return float(np.sum(y_pred - y_true)) / denom


def under_forecast_share(y_true, y_pred) -> float:
    """Share of demand under-forecasted (missed demand)."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0
    missed = np.maximum(0.0, y_true - y_pred)
    return float(np.sum(missed)) / denom


def compute_forecast_metrics(y_true, y_pred, y_train, seasonal_period: int = 1) -> Dict[str, float]:
    """Compute a bundle of accuracy and guardrail metrics."""
    return {
        "wape": wape(y_true, y_pred),
        "mase": mase(y_true, y_pred, y_train, seasonal_period),
        "rmsse": rmsse(y_true, y_pred, y_train, seasonal_period),
        "bias_pct": bias_pct(y_true, y_pred),
        "uf_share": under_forecast_share(y_true, y_pred),
    }


# ============================================================
# EXPONENTIAL SMOOTHING HELPERS
# ============================================================
def safe_exponential_smoothing(y_train, seasonal_periods=None, trend=None, seasonal=None,
                                damped_trend=False, max_attempts=3):
    """Safely fit an exponential smoothing model with fallbacks."""
    from statsmodels.tsa.holtwinters import ExponentialSmoothing, SimpleExpSmoothing
    if len(y_train) < 3:
        return None
    nonzero_count = np.sum(y_train > 0)
    if nonzero_count < 2:
        return None

    for attempt in range(max_attempts):
        try:
            if attempt == 0:
                model = ExponentialSmoothing(
                    y_train,
                    seasonal_periods=seasonal_periods,
                    trend=trend,
                    seasonal=seasonal,
                    damped_trend=damped_trend,
                    initialization_method="estimated"
                )
            elif attempt == 1:
                model = ExponentialSmoothing(
                    y_train,
                    trend=trend,
                    seasonal=None,
                    damped_trend=damped_trend,
                    initialization_method="heuristic"
                )
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


# ============================================================
# FORECASTING METHODS
# ============================================================
def forecast_holt_damped(y_train, y_val, h_future):
    """Holt's damped trend exponential smoothing."""
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
    """Croston's method for intermittent demand."""
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
    """Teunter-Syntetos-Babai method for intermittent demand."""
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
    """Syntetos-Boylan Approximation method."""
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


def forecast_prophet(y_train, y_val, h_future, weekly_dates_train=None, freq='W'):
    """
    Prophet forecasting method.

    Prophet is well-suited for:
    - Data with strong seasonal patterns (weekly, yearly)
    - Data with trend changes
    - Data with missing values or outliers

    Args:
        y_train: Training data (numpy array of demand values)
        y_val: Validation data (numpy array)
        h_future: Number of future periods to forecast
        weekly_dates_train: Optional dates for training data. If None, generates dates.
        freq: Frequency of data ('W' for weekly, 'D' for daily)

    Returns:
        Tuple of (fc_val, fc_future, error) or (None, None, None) on failure
    """
    Prophet = _get_prophet_class()
    if Prophet is None:
        return None, None, None

    try:
        import logging
        # Suppress Prophet's verbose logging
        logging.getLogger('prophet').setLevel(logging.WARNING)
        logging.getLogger('cmdstanpy').setLevel(logging.WARNING)

        # Minimum data requirement for Prophet
        if len(y_train) < 10:
            return None, None, None

        # Create date index if not provided
        if weekly_dates_train is not None:
            dates = pd.to_datetime(weekly_dates_train)
        else:
            # Generate weekly dates ending today
            end_date = pd.Timestamp.now().normalize()
            dates = pd.date_range(end=end_date, periods=len(y_train), freq=freq)

        # Prepare Prophet DataFrame format
        df_prophet = pd.DataFrame({
            'ds': dates,
            'y': y_train
        })

        # Initialize Prophet with settings optimized for demand forecasting
        model = Prophet(
            yearly_seasonality=True,      # Capture annual patterns
            weekly_seasonality=False,     # Already weekly aggregated
            daily_seasonality=False,      # Not daily data
            seasonality_mode='multiplicative',  # Better for demand that scales
            changepoint_prior_scale=0.05,  # Regularize trend changes
            interval_width=0.80,           # 80% uncertainty interval
        )

        # Fit model (suppress stdout)
        with open(os.devnull, 'w') as devnull:
            import sys
            old_stdout = sys.stdout
            sys.stdout = devnull
            try:
                model.fit(df_prophet)
            finally:
                sys.stdout = old_stdout

        # Create future dataframe for validation + future periods
        total_periods = len(y_val) + h_future
        future = model.make_future_dataframe(periods=total_periods, freq=freq)

        # Generate forecast
        forecast = model.predict(future)

        # Extract predictions (yhat column)
        # The last (len(y_val) + h_future) rows are the out-of-sample predictions
        predictions = forecast['yhat'].values

        # Split into validation and future forecasts
        # Prophet predictions include historical fitted values, so we take the tail
        n_train = len(y_train)
        fc_val = predictions[n_train:n_train + len(y_val)]
        fc_future = predictions[n_train + len(y_val):]

        # Ensure non-negative forecasts (demand can't be negative)
        fc_val = np.maximum(fc_val, 0)
        fc_future = np.maximum(fc_future, 0)

        # Calculate wMAPE on validation set
        error = wmape(y_val, fc_val)

        # Sanity check - reject if error is too high
        if error > 200:
            return None, None, None

        return fc_val, fc_future, error

    except Exception as e:
        # Prophet can fail for various reasons (convergence, data issues)
        return None, None, None


def is_prophet_available() -> bool:
    """Check if Prophet is available for use."""
    return _get_prophet_class() is not None


# ============================================================
# ML FORECASTING HELPERS
# ============================================================
def _feature_row(time_idx: int, buffer: List[float]) -> List[float]:
    """Build a feature row for ML models."""
    lags = [0.0, 0.0, 0.0, 0.0]
    for i in range(1, 5):
        if len(buffer) >= i:
            lags[i - 1] = float(buffer[-i])
    recent = buffer[-4:] if buffer else []
    rolling_mean = float(np.mean(recent)) if recent else 0.0
    rolling_std = float(np.std(recent)) if recent else 0.0
    return [time_idx, lags[0], lags[1], lags[2], lags[3], rolling_mean, rolling_std]


def _recursive_forecast(model, future_state: Dict[str, Any], h_future: int) -> np.ndarray:
    """Generate recursive multi-step forecasts using ML model."""
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
    """Random Forest regression forecasting."""
    try:
        from sklearn.ensemble import RandomForestRegressor
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
    except Exception:
        return None, None, None


def forecast_xgboost(X_train, y_train, X_val, y_val, future_state, h_future):
    """XGBoost regression forecasting."""
    try:
        from xgboost import XGBRegressor
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
    except Exception:
        return None, None, None


def build_features(
    df_weekly: pd.DataFrame,
    h_future: int,
    split_idx: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, Dict[str, Any], np.ndarray]:
    """Build feature matrices for ML models from weekly data."""
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

    n_rows = len(df)
    if n_rows < 2:
        empty_X = np.empty((0, len(feature_cols)))
        empty_y = np.empty((0,))
        return empty_X, empty_y, empty_X, empty_y, {"last_idx": 0, "last_values": []}, empty_y

    if split_idx is None:
        split_idx = int(n_rows * 0.7)
        if split_idx < 4:
            split_idx = max(4, n_rows - 4)
    split_idx = int(split_idx)
    split_idx = max(1, min(split_idx, n_rows - 1))

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


def forecast_random_forest_full(df_weekly: pd.DataFrame, h_future: int) -> Optional[np.ndarray]:
    """Train Random Forest on full weekly history and forecast future horizon."""
    try:
        from sklearn.ensemble import RandomForestRegressor
        if df_weekly is None or df_weekly.empty or len(df_weekly) < 4:
            return None
        df = df_weekly.copy().sort_values('week').reset_index(drop=True)
        df['time_idx'] = range(len(df))
        df['lag_1'] = df['quantity'].shift(1)
        df['lag_2'] = df['quantity'].shift(2)
        df['lag_3'] = df['quantity'].shift(3)
        df['lag_4'] = df['quantity'].shift(4)
        df['rolling_mean_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).mean()
        df['rolling_std_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).std()
        df = df.fillna(0)

        feature_cols = ['time_idx', 'lag_1', 'lag_2', 'lag_3', 'lag_4', 'rolling_mean_4', 'rolling_std_4']
        X = df[feature_cols].values
        y = df['quantity'].values
        if len(X) < 4:
            return None

        model = RandomForestRegressor(
            n_estimators=100,
            max_depth=10,
            min_samples_split=5,
            random_state=42,
            n_jobs=1
        )
        model.fit(X, y)

        future_state = {
            "last_idx": int(df['time_idx'].iloc[-1]),
            "last_values": df['quantity'].iloc[-4:].tolist(),
        }
        fc_future = _recursive_forecast(model, future_state, h_future)
        return np.maximum(fc_future, 0.0)
    except Exception:
        return None


def forecast_xgboost_full(df_weekly: pd.DataFrame, h_future: int) -> Optional[np.ndarray]:
    """Train XGBoost on full weekly history and forecast future horizon."""
    try:
        from xgboost import XGBRegressor
        if df_weekly is None or df_weekly.empty or len(df_weekly) < 4:
            return None
        df = df_weekly.copy().sort_values('week').reset_index(drop=True)
        df['time_idx'] = range(len(df))
        df['lag_1'] = df['quantity'].shift(1)
        df['lag_2'] = df['quantity'].shift(2)
        df['lag_3'] = df['quantity'].shift(3)
        df['lag_4'] = df['quantity'].shift(4)
        df['rolling_mean_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).mean()
        df['rolling_std_4'] = df['quantity'].shift(1).rolling(window=4, min_periods=1).std()
        df = df.fillna(0)

        feature_cols = ['time_idx', 'lag_1', 'lag_2', 'lag_3', 'lag_4', 'rolling_mean_4', 'rolling_std_4']
        X = df[feature_cols].values
        y = df['quantity'].values
        if len(X) < 4:
            return None

        model = XGBRegressor(
            n_estimators=100,
            max_depth=6,
            learning_rate=0.1,
            random_state=42,
            n_jobs=1,
            verbosity=0
        )
        model.fit(X, y, verbose=False)

        future_state = {
            "last_idx": int(df['time_idx'].iloc[-1]),
            "last_values": df['quantity'].iloc[-4:].tolist(),
        }
        fc_future = _recursive_forecast(model, future_state, h_future)
        return np.maximum(fc_future, 0.0)
    except Exception:
        return None


# ============================================================
# FORECAST VALIDATION AND SELECTION
# ============================================================
def validate_forecast_reasonableness(fc_future: np.ndarray, y_train: np.ndarray,
                                      method_name: str) -> Tuple[bool, str]:
    """Validate that a forecast is reasonable given historical data."""
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

    # Validation checks
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


def _percentile_scores(values: List[float]) -> List[float]:
    """Convert metric values (lower is better) to 0-100 scores."""
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


def _resolve_lead_time_window_weeks(
    demand_class: str,
    abc_class: str = "C",
    lead_time_weeks: Optional[float] = None,
    n_val_points: int = 0,
) -> int:
    """Resolve evaluation window for lead-time cumulative demand error."""
    if lead_time_weeks is not None and np.isfinite(lead_time_weeks):
        window = int(round(float(lead_time_weeks)))
    else:
        base_by_class = {
            "SMOOTH": 4,
            "ERRATIC": 6,
            "INTERMITTENT": 8,
            "LUMPY": 8,
            "NO_DEMAND": 4,
            "UNKNOWN": 6,
        }
        window = base_by_class.get(str(demand_class).upper(), 6)

    window += {"A": 2, "B": 1, "C": 0}.get(str(abc_class).upper(), 0)
    max_window = max(2, n_val_points)
    return max(2, min(window, max_window))


def _lead_time_wape(y_true: np.ndarray, y_pred: np.ndarray, window: int) -> float:
    """WAPE on rolling cumulative demand (proxy for lead-time demand error)."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if len(y_true) == 0 or len(y_pred) == 0 or len(y_true) != len(y_pred):
        return float("inf")
    if window <= 1 or len(y_true) < window:
        return wape(y_true, y_pred)

    kernel = np.ones(window, dtype=float)
    true_roll = np.convolve(y_true, kernel, mode="valid")
    pred_roll = np.convolve(y_pred, kernel, mode="valid")
    return wape(true_roll, pred_roll)


def _split_by_lengths(values: np.ndarray, lengths: List[int]) -> List[np.ndarray]:
    """Split a 1D array into chunks by provided lengths."""
    chunks: List[np.ndarray] = []
    idx = 0
    for n in lengths:
        n_int = int(n)
        if n_int <= 0:
            continue
        end = idx + n_int
        if end > len(values):
            break
        chunks.append(np.asarray(values[idx:end], dtype=float))
        idx = end
    if idx < len(values):
        chunks.append(np.asarray(values[idx:], dtype=float))
    return [c for c in chunks if len(c) > 0]


def _scale_ratio_threshold(demand_class: str, abc_class: str, trend_lift: float) -> float:
    """Class-aware threshold for future-vs-recent scale ratio guardrail."""
    base = {
        "SMOOTH": 2.0,
        "ERRATIC": 2.3,
        "INTERMITTENT": 2.4,
        "LUMPY": 2.3,
        "NO_DEMAND": 1.0,
        "UNKNOWN": 2.2,
    }.get(str(demand_class).upper(), 2.2)
    base += {"A": 0.3, "B": 0.1, "C": 0.0}.get(str(abc_class).upper(), 0.0)
    if trend_lift >= 1.8:
        base *= 1.15
    elif trend_lift <= 0.7:
        base *= 0.90
    return base


def _fallback_recent_cap(y_train: np.ndarray, demand_class: str, abc_class: str) -> float:
    """Upper cap for fallback level based on trailing demand evidence."""
    y = np.clip(np.asarray(y_train, dtype=float), 0.0, None)
    if len(y) == 0:
        return 0.0

    recent_n = min(52, len(y))
    recent = y[-recent_n:]
    recent_sum = float(np.sum(recent))
    recent_mean = float(np.mean(recent)) if recent_n > 0 else 0.0
    recent_nonzero = recent[recent > 0]
    p_recent_nonzero = float(np.mean(recent > 0)) if recent_n > 0 else 0.0
    recent_nonzero_mean = float(np.mean(recent_nonzero)) if len(recent_nonzero) > 0 else 0.0
    recent_implied = p_recent_nonzero * recent_nonzero_mean

    full_nonzero = y[y > 0]
    full_nonzero_mean = float(np.mean(full_nonzero)) if len(full_nonzero) > 0 else 0.0

    demand_class_upper = str(demand_class).upper()
    abc_class_upper = str(abc_class).upper()
    ratio_cap = {
        "SMOOTH": 2.0,
        "ERRATIC": 2.5,
        "INTERMITTENT": 3.0,
        "LUMPY": 3.0,
        "NO_DEMAND": 1.0,
        "UNKNOWN": 2.5,
    }.get(demand_class_upper, 2.5)
    ratio_cap += {"A": 0.5, "B": 0.2, "C": 0.0}.get(abc_class_upper, 0.0)

    baseline = max(recent_mean, recent_implied)
    if baseline > 0:
        return baseline * ratio_cap

    # No recent demand: keep only a small residual carry-over.
    carry_frac = {"A": 0.25, "B": 0.15, "C": 0.10}.get(abc_class_upper, 0.10)
    cap = full_nonzero_mean * carry_frac
    if demand_class_upper in {"INTERMITTENT", "LUMPY", "NO_DEMAND"}:
        cap = min(cap, 0.25)
    return max(0.0, cap)


def _apply_fallback_cap(raw_level: float, y_train: np.ndarray, demand_class: str, abc_class: str) -> float:
    """Cap a fallback level by trailing-demand evidence."""
    raw = max(float(raw_level), 0.0)
    cap = _fallback_recent_cap(y_train, demand_class=demand_class, abc_class=abc_class)
    if cap <= 0:
        return 0.0 if raw > 0 else raw
    return min(raw, cap)


def _cap_forecast_to_recent_evidence(
    fc_future: np.ndarray,
    y_train: np.ndarray,
    demand_class: str,
    abc_class: str,
) -> Tuple[np.ndarray, bool]:
    """
    Clamp forecast level to recent demand evidence.
    This is a final guardrail applied to the selected winner/fallback.
    """
    fc = np.maximum(np.asarray(fc_future, dtype=float), 0.0)
    if fc.size == 0:
        return fc, False

    future_mean = float(np.mean(fc))
    if not np.isfinite(future_mean) or future_mean <= 0:
        return fc, False

    y = np.clip(np.asarray(y_train, dtype=float), 0.0, None)
    if y.size == 0:
        return np.zeros_like(fc), True

    recent_n = min(26, y.size)
    recent = y[-recent_n:]
    recent_mean = float(np.mean(recent)) if recent_n > 0 else 0.0
    recent_sum = float(np.sum(recent))
    recent_nonzero_weeks = int(np.sum(recent > 0))

    long_mean = float(np.mean(y)) if y.size > 0 else 0.0
    trend_lift = (recent_mean / (long_mean + 1e-6)) if long_mean > 0 else 1.0
    ratio_cap = _scale_ratio_threshold(demand_class, abc_class, trend_lift)

    if recent_nonzero_weeks <= 1 or recent_sum <= 1.0:
        ratio_cap = min(ratio_cap, 2.2 if str(abc_class).upper() == "A" else 2.0)

    adjusted = False
    if recent_mean <= 0:
        target_mean = _fallback_recent_cap(y, demand_class=demand_class, abc_class=abc_class)
        target_mean = max(0.0, float(target_mean))
    else:
        target_mean = max(0.0, recent_mean * ratio_cap)

    if future_mean > target_mean + 1e-9:
        if target_mean <= 0:
            fc = np.zeros_like(fc)
        else:
            fc = fc * (target_mean / (future_mean + 1e-9))
        adjusted = True

    return np.maximum(fc, 0.0), adjusted


def select_best_forecast(
    methods_dict: Dict,
    y_train: np.ndarray,
    y_val: np.ndarray,
    demand_class: str,
    h_future: int = FUTURE_FORECAST_WEEKS,
    abc_class: str = "C",
    lead_time_weeks: Optional[float] = None,
) -> Tuple[str, np.ndarray, float, Dict[str, float]]:
    """Select the best method using cost-aware, rolling-CV composite scoring."""
    y_train = np.asarray(y_train, dtype=float)
    y_val = np.asarray(y_val, dtype=float)
    valid_methods = {}
    rejected_methods = {}
    demand_class_upper = str(demand_class).upper()
    abc_class_upper = str(abc_class).upper()

    split_defs = build_rolling_origin_splits(len(y_train), demand_class=demand_class_upper, max_folds=4)
    fold_lengths = [int(val_end - train_end) for train_end, val_end in split_defs]
    if not fold_lengths or sum(fold_lengths) != len(y_val):
        fold_lengths = [len(y_val)] if len(y_val) > 0 else []

    lt_window = _resolve_lead_time_window_weeks(
        demand_class=demand_class_upper,
        abc_class=abc_class_upper,
        lead_time_weeks=lead_time_weeks,
        n_val_points=len(y_val),
    )

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

    # Fallback if no valid methods
    if not valid_methods:
        recent_weeks = min(26, len(y_train) // 2)
        recent_sales = y_train[-recent_weeks:]
        recent_total = float(np.sum(recent_sales))

        if recent_total == 0:
            # No demand in the recent validation window: force inert fallback.
            fc_future = np.zeros(h_future)
            return 'RECENT_ZERO_FALLBACK', fc_future, 0.0, {
                "wape": 0.0, "mase": 0.0, "rmsse": 0.0, "bias_pct": 0.0, "uf_share": 0.0, "composite_score": 0.0
            }

        if recent_total < 10:
            recent_avg_raw = recent_total / recent_weeks
            recent_avg = _apply_fallback_cap(
                recent_avg_raw, y_train, demand_class=demand_class_upper, abc_class=abc_class_upper
            )
            fc_future = np.full(h_future, recent_avg, dtype=float)
            fc_future, drift_capped = _cap_forecast_to_recent_evidence(
                fc_future, y_train, demand_class=demand_class_upper, abc_class=abc_class_upper
            )
            method_name = 'RECENT_AVG_CAPPED' if recent_avg < recent_avg_raw else 'RECENT_AVG'
            if drift_capped:
                method_name = f"{method_name} + drift_cap"
            return method_name, fc_future, 0.0, {
                "wape": 0.0, "mase": 0.0, "rmsse": 0.0, "bias_pct": 0.0, "uf_share": 0.0, "composite_score": 0.0
            }

        mean_demand = float(np.mean(y_train))
        historical_max = float(np.max(y_train))
        safe_forecast_raw = min(mean_demand, historical_max)
        safe_forecast = _apply_fallback_cap(
            safe_forecast_raw, y_train, demand_class=demand_class_upper, abc_class=abc_class_upper
        )
        fc_future = np.full(h_future, safe_forecast, dtype=float)
        fc_future, drift_capped = _cap_forecast_to_recent_evidence(
            fc_future, y_train, demand_class=demand_class_upper, abc_class=abc_class_upper
        )
        method_name = 'SAFE_MEAN_CAPPED' if safe_forecast < safe_forecast_raw else 'SAFE_MEAN'
        if drift_capped:
            method_name = f"{method_name} + drift_cap"
        return method_name, fc_future, 0.0, {
            "wape": 0.0, "mase": 0.0, "rmsse": 0.0, "bias_pct": 0.0, "uf_share": 0.0, "composite_score": 0.0
        }

    # Compute metrics for all candidates
    candidate_rows = []
    for method_name, (fc_val, fc_future, wmape_score) in valid_methods.items():
        fc_val_arr = np.asarray(fc_val, dtype=float)
        metrics = compute_forecast_metrics(y_val, fc_val_arr, y_train, seasonal_period=1)

        # Lead-time cumulative demand accuracy (primary metric).
        lt_wape = _lead_time_wape(y_val, fc_val_arr, lt_window)

        # Fold stability: lower variation of fold errors is better.
        fold_pred_chunks = _split_by_lengths(fc_val_arr, fold_lengths)
        fold_true_chunks = _split_by_lengths(y_val, fold_lengths)
        fold_wapes: List[float] = []
        for y_true_fold, y_pred_fold in zip(fold_true_chunks, fold_pred_chunks):
            fold_wapes.append(wape(y_true_fold, y_pred_fold))
        fold_wapes = [f for f in fold_wapes if np.isfinite(f)]
        if fold_wapes:
            fold_mean = float(np.mean(fold_wapes))
            fold_std = float(np.std(fold_wapes))
            stability_cv = fold_std / (fold_mean + 1e-6)
        else:
            stability_cv = float("inf")

        # Soft cost-aware bias penalty (no hard drop on under-forecast).
        bias = float(metrics["bias_pct"])
        uf_share = float(metrics["uf_share"])
        under = max(0.0, -bias)
        over = max(0.0, bias)
        under_w, over_w = {
            "A": (1.8, 0.8),
            "B": (1.4, 1.0),
            "C": (1.1, 1.2),
        }.get(abc_class_upper, (1.2, 1.1))
        if demand_class_upper in {"INTERMITTENT", "LUMPY"}:
            over_w *= 1.25
        elif demand_class_upper == "SMOOTH":
            under_w *= 1.15
        bias_cost = (under_w * under) + (over_w * over) + (0.35 * uf_share)

        # Scale guardrail: reject extreme future drift when trend does not justify it.
        recent_n = min(52, len(y_train))
        long_mean = float(np.mean(y_train)) if len(y_train) > 0 else 0.0
        recent_mean = float(np.mean(y_train[-recent_n:])) if recent_n > 0 else long_mean
        baseline_mean = recent_mean if recent_mean > 0 else long_mean
        future_mean = float(np.mean(fc_future)) if len(fc_future) > 0 else 0.0
        trend_lift = (recent_mean / (long_mean + 1e-6)) if long_mean > 0 else 1.0
        scale_ratio = (future_mean / (baseline_mean + 1e-6)) if baseline_mean > 0 else (0.0 if future_mean == 0 else float("inf"))
        ratio_threshold = _scale_ratio_threshold(demand_class_upper, abc_class_upper, trend_lift)
        if future_mean > 0.75 and scale_ratio > ratio_threshold:
            rejected_methods[method_name] = (
                f"Scale drift: future/recent ratio {scale_ratio:.2f} > {ratio_threshold:.2f}"
            )
            continue

        metrics["lead_time_wape"] = lt_wape
        metrics["stability_cv"] = stability_cv
        metrics["bias_cost"] = bias_cost
        metrics["scale_ratio"] = scale_ratio
        metrics["lead_time_window_weeks"] = float(lt_window)
        candidate_rows.append({
            "name": method_name,
            "fc_val": fc_val_arr,
            "fc_future": fc_future,
            "metrics": metrics,
        })

    if not candidate_rows:
        return 'SAFE_MEAN', np.full(h_future, 0.0), 0.0, {
            "wape": 0.0, "mase": 0.0, "rmsse": 0.0, "bias_pct": 0.0, "uf_share": 0.0, "composite_score": 0.0
        }

    scored_rows = candidate_rows

    lt_scores = _percentile_scores([r["metrics"]["lead_time_wape"] for r in scored_rows])
    rmsse_scores = _percentile_scores([r["metrics"]["rmsse"] for r in scored_rows])
    bias_cost_scores = _percentile_scores([r["metrics"]["bias_cost"] for r in scored_rows])
    stability_scores = _percentile_scores([r["metrics"]["stability_cv"] for r in scored_rows])

    for idx, row in enumerate(scored_rows):
        # Cost-aware composite:
        # 40% lead-time cumulative error, 25% RMSSE, 20% bias/UF cost, 15% stability.
        composite = (
            0.40 * lt_scores[idx]
            + 0.25 * rmsse_scores[idx]
            + 0.20 * bias_cost_scores[idx]
            + 0.15 * stability_scores[idx]
        )
        row["metrics"]["composite_score"] = composite

    best_row = max(
        scored_rows,
        key=lambda x: (x["metrics"]["composite_score"], -x["metrics"]["lead_time_wape"]),
    )
    best_metrics = best_row["metrics"]
    best_fc_future, drift_capped = _cap_forecast_to_recent_evidence(
        best_row["fc_future"], y_train, demand_class=demand_class_upper, abc_class=abc_class_upper
    )
    best_metrics["drift_capped"] = 1.0 if drift_capped else 0.0
    best_name = f"{best_row['name']} + drift_cap" if drift_capped else best_row["name"]
    return best_name, best_fc_future, best_metrics["wape"], best_metrics


# ============================================================
# SAFETY STOCK AND REORDER METRICS
# ============================================================
def _weekly_std_to_horizon_std(std_weekly: float, horizon_days: float) -> float:
    """
    Convert weekly demand std dev to std dev over an arbitrary day horizon.

    If weekly demand has standard deviation σ_week, then over `horizon_days`
    the equivalent horizon std is:

        σ_h = σ_week * sqrt(horizon_days / 7)

    This is equivalent to converting weekly std to daily std via
    σ_day = σ_week / sqrt(7) and then scaling by sqrt(horizon_days).
    """
    horizon_days = max(0.0, float(horizon_days))
    std_weekly = max(0.0, float(std_weekly))
    if horizon_days <= 0.0 or std_weekly <= 0.0:
        return 0.0
    return std_weekly * math.sqrt(horizon_days / DAYS_PER_WEEK)


def compute_safety_stock(mean_demand: float, std_demand: float, lead_time_weeks: float,
                         demand_class: str, abc_class: str, y_train: np.ndarray) -> Dict[str, float]:
    """
    Compute safety stock using continuous review formula with floor.

    Clean formula: SS = z × σ_L (no double counting with ROP)
    Floor: max(SS_floor, z × σ_L) where SS_floor = SS_FLOOR_DAYS × daily demand on non-zero days
    """
    # Compute standard deviation over lead time (σ_L)
    lead_time_days = lead_time_weeks * DAYS_PER_WEEK
    sigma_L = _weekly_std_to_horizon_std(std_demand, lead_time_days)

    # Base safety stock: z × σ_L
    ss_base = Z_SCORE * sigma_L if sigma_L > 0 else 0.0

    # ABC-based uplift for higher service level on top sellers
    uplift_scalar = SS_UPLIFT_SCALAR.get(abc_class, 0.0)
    ss_uplift = ss_base * uplift_scalar

    y_train_arr = np.clip(np.asarray(y_train, dtype=float), 0.0, None)
    recent_for_buffers = y_train_arr[-min(52, len(y_train_arr)):] if len(y_train_arr) > 0 else y_train_arr

    # Lumpy demand buffer - extra protection for erratic items
    lumpy_buffer = 0.0
    if demand_class == 'LUMPY':
        nonzero_demand = recent_for_buffers[recent_for_buffers > 0]
        if len(nonzero_demand) > 0:
            pctl = LUMPY_PCTL_NONZERO.get(abc_class, 0.90)
            percentile_demand = float(np.percentile(nonzero_demand, pctl * 100))
            lumpy_buffer = percentile_demand * lead_time_weeks * LUMPY_LT_BUFFER_FRAC

    total_uplift = ss_uplift + lumpy_buffer

    # Calculate safety stock floor (prevents SS=0 for slow movers)
    # Use average daily demand on non-zero days
    nonzero_demand = recent_for_buffers[recent_for_buffers > 0]
    if len(nonzero_demand) > 0:
        avg_nonzero_weekly = float(np.mean(nonzero_demand))
        avg_nonzero_daily = avg_nonzero_weekly / DAYS_PER_WEEK
        ss_floor = SS_FLOOR_DAYS * avg_nonzero_daily
    else:
        ss_floor = 0.0

    # Apply floor first, then enforce cap on the final value so floor/uplifts
    # cannot bypass the cap for inactive or near-zero-demand SKUs.
    calculated_ss = ss_base + total_uplift
    final_ss = max(ss_floor, calculated_ss)
    if SS_CAP_MONTHS > 0:
        max_ss = max(0.0, float(mean_demand)) * WEEKS_PER_MONTH * SS_CAP_MONTHS
        final_ss = min(final_ss, max_ss)

    # Inactivity suppressor: if there is no demand in recent history and
    # forecast scale is effectively zero, keep SS at zero.
    recent_n = min(26, len(y_train_arr))
    recent_26 = y_train_arr[-recent_n:] if recent_n > 0 else np.array([], dtype=float)
    recent_sum = float(np.sum(recent_26)) if recent_n > 0 else 0.0
    recent_nonzero_weeks = int(np.sum(recent_26 > 0)) if recent_n > 0 else 0
    if recent_n > 0 and float(mean_demand) <= 0.25:
        if recent_sum <= 0.0:
            final_ss = 0.0
        elif recent_sum <= 1.0:
            final_ss = min(final_ss, 0.25)
        elif recent_nonzero_weeks <= 1:
            final_ss = min(final_ss, 0.45)

    return {
        'ss_base': ss_base,
        'ss_uplift_raw': total_uplift,
        'ss_uplift_applied_pre_budget': total_uplift,
        'ss_floor': ss_floor,
        'safety_stock': final_ss
    }


def compute_reorder_metrics(weekly_forecast: np.ndarray, lead_time_weeks: float,
                            demand_class: str, abc_class: str, y_train: np.ndarray,
                            coverage_horizon_days: int = COVERAGE_HORIZON_DEFAULT) -> Dict[str, float]:
    """
    Compute reorder point and order-up-to level using continuous review formulas.

    Key formulas (no double counting):
        ROP = μ_L + z × σ_L          (reorder point)
        S = μ_(L+T) + z × σ_(L+T)    (order-up-to level)
        Q = max(0, S - IP)            (order quantity, computed at reorder time)

    Args:
        weekly_forecast: Array of weekly forecast values
        lead_time_weeks: Lead time in weeks
        demand_class: Demand classification (SMOOTH, INTERMITTENT, etc.)
        abc_class: ABC classification (A, B, C)
        y_train: Historical weekly demand data
        coverage_horizon_days: Days beyond lead time to cover (T parameter)

    Returns:
        Dict with reorder metrics including order_up_to_level
    """
    mean_weekly = np.mean(weekly_forecast)
    std_weekly = np.std(y_train) if len(y_train) > 0 else 0.0
    mean_daily = mean_weekly / DAYS_PER_WEEK
    # Apply minimum lead time floor to prevent undersized order-up-to levels
    # when lead time is 0 or missing in the data
    lead_time_days_raw = lead_time_weeks * DAYS_PER_WEEK
    lead_time_days = max(lead_time_days_raw, MIN_LEAD_TIME_DAYS)
    lead_time_weeks_floored = lead_time_days / DAYS_PER_WEEK

    # μ_L = expected demand over lead time
    mu_L = mean_daily * lead_time_days

    # σ_L = std deviation of demand over lead time
    sigma_L = _weekly_std_to_horizon_std(std_weekly, lead_time_days)

    # Safety stock (also computes floor) - use floored lead time
    ss_dict = compute_safety_stock(mean_weekly, std_weekly, lead_time_weeks_floored, demand_class, abc_class, y_train)

    # Clean ROP formula: ROP = μ_L + SS (where SS = z × σ_L + uplifts)
    reorder_point = mu_L + ss_dict['safety_stock']

    # Order-up-to level S = μ_(L+T) + z × σ_(L+T)
    # T = coverage horizon (how far beyond lead time to cover)
    total_horizon_days = lead_time_days + coverage_horizon_days
    mu_LT = mean_daily * total_horizon_days
    sigma_LT = _weekly_std_to_horizon_std(std_weekly, total_horizon_days)

    # Apply same ABC uplift logic for order-up-to level
    uplift_scalar = SS_UPLIFT_SCALAR.get(abc_class, 0.0)
    buffer_LT = Z_SCORE * sigma_LT * (1 + uplift_scalar)

    order_up_to_level = mu_LT + buffer_LT

    max_s = None
    if SS_CAP_MONTHS > 0:
        # Keep S-level tied to current demand scale; allow zero when forecast is zero.
        max_s = mu_LT + max(0.0, float(mean_weekly)) * WEEKS_PER_MONTH * SS_CAP_MONTHS
        order_up_to_level = min(order_up_to_level, max_s)

    # Also compute floor for order-up-to level using non-zero demand
    y_train_arr = np.clip(np.asarray(y_train, dtype=float), 0.0, None)
    recent_for_floor = y_train_arr[-min(52, len(y_train_arr)):] if len(y_train_arr) > 0 else y_train_arr
    nonzero_demand = recent_for_floor[recent_for_floor > 0]
    if len(nonzero_demand) > 0:
        avg_nonzero_weekly = float(np.mean(nonzero_demand))
        avg_nonzero_daily = avg_nonzero_weekly / DAYS_PER_WEEK
        s_floor = SS_FLOOR_DAYS * avg_nonzero_daily + mu_LT  # At least floor + expected demand
        order_up_to_level = max(s_floor, order_up_to_level)

    # Re-apply cap so historical non-zero floor cannot force runaway S-level.
    if max_s is not None:
        order_up_to_level = min(order_up_to_level, max_s)

    # Structural guardrail: S-level must never be below ROP.
    # When caps/floors interact, this can otherwise flip the policy and suppress ordering.
    if order_up_to_level < reorder_point:
        order_up_to_level = reorder_point

    # Inactivity guardrail: keep S close to ROP when demand is near-zero recently.
    recent_n = min(26, len(y_train_arr))
    recent_sum = float(np.sum(y_train_arr[-recent_n:])) if recent_n > 0 else 0.0
    if recent_n > 0 and float(mean_weekly) <= 0.25 and recent_sum <= 1.0:
        order_up_to_level = min(order_up_to_level, reorder_point + 0.25)

    return {
        'lead_time_mean_demand': mu_L,
        'reorder_point': reorder_point,
        'order_up_to_level': order_up_to_level,
        'coverage_horizon_days': coverage_horizon_days,
        'daily_mean_demand': mean_daily,
        **ss_dict
    }
