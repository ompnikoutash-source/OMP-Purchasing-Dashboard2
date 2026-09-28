"""
Demand adjustment helpers.

These utilities convert transactional net demand into a forecasting-friendly
signal by pairing returns (negative quantities) against prior sales.
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd


def net_returns_against_prior_sales(values: np.ndarray) -> np.ndarray:
    """
    Reconcile negative demand by offsetting prior positive demand (LIFO).

    Rationale:
    - Returns usually correspond to recently shipped material.
    - For demand forecasting, a return should erase prior demand rather than
      create a negative-demand spike in the return period.

    Behavior:
    - Output is always >= 0 per period.
    - Unmatched returns (no prior positive demand in the window) are ignored.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr

    adjusted = np.zeros_like(arr, dtype=float)
    positive_stack: List[List[float]] = []  # [index, remaining_positive_qty]

    for i, qty in enumerate(arr):
        if not np.isfinite(qty):
            qty = 0.0

        if qty > 0:
            adjusted[i] = qty
            positive_stack.append([float(i), float(qty)])
            continue

        if qty >= 0:
            # qty == 0 path
            adjusted[i] = 0.0
            continue

        # Negative quantity => return. Apply against prior positives.
        adjusted[i] = 0.0
        to_offset = -qty

        while to_offset > 1e-12 and positive_stack:
            idx_f, remaining = positive_stack[-1]
            idx = int(idx_f)
            applied = min(remaining, to_offset)
            adjusted[idx] = max(0.0, adjusted[idx] - applied)
            remaining -= applied
            to_offset -= applied
            if remaining <= 1e-12:
                positive_stack.pop()
            else:
                positive_stack[-1][1] = remaining

        # Unmatched remainder is ignored: it likely belongs to sales before
        # the history window, and should not create negative demand now.

    return np.maximum(adjusted, 0.0)


def make_return_aware_daily_series(df: pd.DataFrame, date_col: str, qty_col: str) -> pd.DataFrame:
    """
    Aggregate to daily quantities and net returns against prior daily sales.

    Returns a DataFrame with columns:
        - date_col
        - qty_col (non-negative, return-aware)
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=[date_col, qty_col])

    tmp = df.copy()
    tmp[date_col] = pd.to_datetime(tmp[date_col], errors="coerce")
    tmp[qty_col] = pd.to_numeric(tmp[qty_col], errors="coerce").fillna(0.0)
    tmp = tmp.dropna(subset=[date_col])
    if tmp.empty:
        return pd.DataFrame(columns=[date_col, qty_col])

    daily = (
        tmp.groupby(date_col, as_index=False)[qty_col]
        .sum()
        .sort_values(date_col)
        .reset_index(drop=True)
    )
    daily[qty_col] = net_returns_against_prior_sales(daily[qty_col].to_numpy(dtype=float))
    return daily

