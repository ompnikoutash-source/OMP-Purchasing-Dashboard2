from __future__ import annotations

import math

import numpy as np
import pytest

from core.config import DAYS_PER_WEEK, Z_SCORE
from core.forecasting import (
    _weekly_std_to_horizon_std,
    compute_reorder_metrics,
    compute_safety_stock,
)


class TestWeeklyStdToHorizonStd:
    def test_matches_direct_weekly_scaling(self):
        std_weekly = 14.0
        horizon_days = 28.0

        sigma = _weekly_std_to_horizon_std(std_weekly, horizon_days)

        assert sigma == pytest.approx(std_weekly * math.sqrt(horizon_days / DAYS_PER_WEEK))

    def test_matches_correct_daily_equivalent(self):
        std_weekly = 21.0
        horizon_days = 35.0

        sigma = _weekly_std_to_horizon_std(std_weekly, horizon_days)
        daily_std = std_weekly / math.sqrt(DAYS_PER_WEEK)

        assert sigma == pytest.approx(daily_std * math.sqrt(horizon_days))


class TestSafetyStockRegression:
    def test_compute_safety_stock_uses_correct_weekly_to_horizon_conversion(self):
        mean_weekly = 70.0
        std_weekly = 14.0
        lead_time_weeks = 4.0
        y_train = np.array([0.0, 28.0], dtype=float)

        metrics = compute_safety_stock(
            mean_demand=mean_weekly,
            std_demand=std_weekly,
            lead_time_weeks=lead_time_weeks,
            demand_class="SMOOTH",
            abc_class="",
            y_train=y_train,
        )

        expected_sigma_l = std_weekly * math.sqrt(lead_time_weeks)
        expected_ss = Z_SCORE * expected_sigma_l

        assert metrics["ss_base"] == pytest.approx(expected_ss)
        assert metrics["safety_stock"] == pytest.approx(expected_ss)


class TestReorderMetricsRegression:
    def test_compute_reorder_metrics_uses_correct_horizon_std(self):
        weekly_forecast = np.array([70.0, 70.0], dtype=float)
        y_train = np.array([0.0, 28.0], dtype=float)
        lead_time_weeks = 4.0
        coverage_horizon_days = 14

        metrics = compute_reorder_metrics(
            weekly_forecast=weekly_forecast,
            lead_time_weeks=lead_time_weeks,
            demand_class="SMOOTH",
            abc_class="",
            y_train=y_train,
            coverage_horizon_days=coverage_horizon_days,
        )

        mean_weekly = 70.0
        std_weekly = float(np.std(y_train))
        mean_daily = mean_weekly / DAYS_PER_WEEK
        lead_time_days = lead_time_weeks * DAYS_PER_WEEK
        total_horizon_days = lead_time_days + coverage_horizon_days

        expected_ss = Z_SCORE * std_weekly * math.sqrt(lead_time_weeks)
        expected_rop = mean_daily * lead_time_days + expected_ss
        expected_s = mean_daily * total_horizon_days + Z_SCORE * std_weekly * math.sqrt(
            total_horizon_days / DAYS_PER_WEEK
        )

        # Non-zero-demand floor should not bind for this fixture.
        assert metrics["safety_stock"] == pytest.approx(expected_ss)
        assert metrics["reorder_point"] == pytest.approx(expected_rop)
        assert metrics["order_up_to_level"] == pytest.approx(expected_s)
