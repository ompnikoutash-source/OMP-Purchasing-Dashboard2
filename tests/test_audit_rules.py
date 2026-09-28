"""
Unit tests for agents/forecast_audit_agent.py _rule_triggered() logic
and agents/utils.py helpers.

Run with:
    pytest tests/test_audit_rules.py -v
"""
from __future__ import annotations

import numpy as np
import pytest

from agents.forecast_audit_agent import _rule_triggered
from agents.utils import SkuSnapshot, _series_is_monthly


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(**kwargs) -> SkuSnapshot:
    """Build a SkuSnapshot with sensible defaults; override via kwargs."""
    defaults = dict(
        dataset="test",
        sku="TEST001",
        description="Test SKU",
        method="RANDOM_FOREST",
        demand_class="SMOOTH",
        abc_class="B",
        uom="EA",
        inventory_position=100.0,
        available=100.0,
        on_order=0.0,
        backorder=0.0,
        safety_stock=20.0,
        reorder_point=30.0,
        order_up_to_level=80.0,
        daily_demand=1.0,
        lead_time_days=30.0,
        lead_time_source="IMLT",
        weekly_hist=np.ones(52, dtype=float),
        weekly_fc=np.ones(52, dtype=float),
        monthly_hist=np.ones(12, dtype=float) * 4.33,
        monthly_fc=np.ones(12, dtype=float) * 4.33,
        catchup_order_qty=0.0,
        source_file="test.json",
    )
    defaults.update(kwargs)
    return SkuSnapshot(**defaults)


# ---------------------------------------------------------------------------
# low_fc_high_ss
# ---------------------------------------------------------------------------

class TestLowFcHighSs:
    RULE = "low_fc_high_ss"
    THRESHOLDS = {"max_fc_weekly": 0.25, "max_recent_weekly_sum": 1.0, "min_safety_stock": 0.5}

    def test_triggers_when_low_fc_and_high_ss(self):
        sku = _snap(
            weekly_fc=np.full(52, 0.1),         # fc_mean = 0.1 ≤ 0.25
            weekly_hist=np.full(52, 0.0),        # recent_sum_26w = 0 ≤ 1.0
            safety_stock=10.0,                   # ss = 10 ≥ 0.5
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_fc_above_threshold(self):
        sku = _snap(
            weekly_fc=np.full(52, 1.0),          # fc_mean = 1.0 > 0.25
            weekly_hist=np.zeros(52),
            safety_stock=10.0,
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_ss_below_threshold(self):
        sku = _snap(
            weekly_fc=np.full(52, 0.1),
            weekly_hist=np.zeros(52),
            safety_stock=0.1,                    # ss = 0.1 < 0.5
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_recent_sum_above_threshold(self):
        sku = _snap(
            weekly_fc=np.full(52, 0.1),
            weekly_hist=np.full(52, 0.1),        # recent_sum_26w ≈ 2.6 > 1.0
            safety_stock=10.0,
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# future_level_drift
# ---------------------------------------------------------------------------

class TestFutureLevelDrift:
    RULE = "future_level_drift"
    THRESHOLDS = {"ratio_recent_nonzero": 2.5, "min_fc_when_recent_zero": 0.25}

    def test_triggers_when_ratio_above_threshold(self):
        # recent_mean = 1.0, fc_mean = 10.0 → ratio = 10.0 > 2.5
        sku = _snap(
            weekly_fc=np.full(52, 10.0),
            weekly_hist=np.ones(52),
        )
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["future_to_recent_ratio"] > 2.5

    def test_triggers_when_recent_zero_fc_nonzero(self):
        sku = _snap(
            weekly_fc=np.full(52, 1.0),
            weekly_hist=np.zeros(52),            # recent_mean = 0
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_normal_ratio(self):
        # recent_mean = 5.0, fc_mean = 6.0 → ratio = 1.2 < 2.5
        sku = _snap(
            weekly_fc=np.full(52, 6.0),
            weekly_hist=np.full(52, 5.0),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_recent_zero_fc_also_zero(self):
        sku = _snap(
            weekly_fc=np.zeros(52),              # fc_mean = 0 ≤ 0.25
            weekly_hist=np.zeros(52),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# weekly_monthly_mismatch
# ---------------------------------------------------------------------------

class TestWeeklyMonthlyMismatch:
    RULE = "weekly_monthly_mismatch"
    THRESHOLDS = {"min_ratio": 0.8, "max_ratio": 1.25, "min_material_units": 1.0}

    def test_triggers_when_monthly_much_larger(self):
        # weekly sum = 52, monthly sum = 100 → ratio = ~1.92 > 1.25
        sku = _snap(
            weekly_fc=np.ones(52),
            monthly_fc=np.full(12, 100.0 / 12),
        )
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["monthly_to_weekly_ratio"] > 1.25

    def test_triggers_when_monthly_much_smaller(self):
        # weekly sum = 52, monthly sum = 10 → ratio ≈ 0.19 < 0.8
        sku = _snap(
            weekly_fc=np.ones(52),
            monthly_fc=np.full(12, 10.0 / 12),
        )
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["monthly_to_weekly_ratio"] < 0.8

    def test_no_trigger_when_ratios_match(self):
        # weekly sum ≈ 52, monthly sum ≈ 52 → ratio ≈ 1.0
        sku = _snap(
            weekly_fc=np.ones(52),
            monthly_fc=np.full(12, 52.0 / 12),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_materiality_guard_near_zero(self):
        # Both totals < 1.0 → operationally irrelevant even if ratio differs
        sku = _snap(
            weekly_fc=np.full(52, 0.01),         # sum = 0.52
            monthly_fc=np.full(12, 0.05),         # sum = 0.6
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_empty_arrays(self):
        sku = _snap(weekly_fc=np.array([]), monthly_fc=np.array([]))
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# default_lead_time_used
# ---------------------------------------------------------------------------

class TestDefaultLeadTimeUsed:
    RULE = "default_lead_time_used"
    THRESHOLDS = {"default_lt_days": 30.0, "tolerance_days": 1.0}

    def test_triggers_when_source_is_default30(self):
        sku = _snap(lead_time_source="DEFAULT30", lead_time_days=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_when_source_is_imlt(self):
        sku = _snap(lead_time_source="IMLT", lead_time_days=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_source_is_excel(self):
        sku = _snap(lead_time_source="EXCEL", lead_time_days=45.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_triggers_imputed_default_source(self):
        sku = _snap(lead_time_source="IMPUTED_DEFAULT_30D", lead_time_days=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True


# ---------------------------------------------------------------------------
# inventory_position_identity
# ---------------------------------------------------------------------------

class TestInventoryPositionIdentity:
    RULE = "inventory_position_identity"
    THRESHOLDS = {"max_abs_error": 0.01}

    def test_triggers_when_ip_does_not_match(self):
        # IP = 100, but avail=80 + on_order=10 - bo=0 = 90 → delta = 10
        sku = _snap(inventory_position=100.0, available=80.0, on_order=10.0, backorder=0.0)
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert abs(ev["inventory_position_delta"]) > 0.01

    def test_no_trigger_when_ip_matches(self):
        # IP = 90, avail=80 + on_order=10 - bo=0 = 90
        sku = _snap(inventory_position=90.0, available=80.0, on_order=10.0, backorder=0.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_handles_backorders_in_identity(self):
        # IP = 70, avail=80 + on_order=10 - bo=20 = 70
        sku = _snap(inventory_position=70.0, available=80.0, on_order=10.0, backorder=20.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# on_order_duplicate_risk
# ---------------------------------------------------------------------------

class TestOnOrderDuplicateRisk:
    RULE = "on_order_duplicate_risk"
    THRESHOLDS = {"max_fc_weekly": 0.5}

    def test_triggers_classic_duplicate(self):
        # on_order=100 ≥ s_level=80, catchup=50, fc=0.1, no active backorders
        sku = _snap(
            on_order=100.0,
            order_up_to_level=80.0,
            catchup_order_qty=50.0,
            backorder=0.0,
            weekly_fc=np.full(52, 0.1),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_when_backorder_exceeds_on_order(self):
        # Backorders (50) > on_order (10) → catchup is legitimate
        sku = _snap(
            on_order=10.0,
            order_up_to_level=80.0,
            catchup_order_qty=70.0,
            backorder=50.0,
            weekly_fc=np.full(52, 0.1),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_on_order_does_not_cover_target(self):
        # on_order=20 < s_level=80 → PO doesn't cover target, catchup is needed
        sku = _snap(
            on_order=20.0,
            order_up_to_level=80.0,
            catchup_order_qty=50.0,
            backorder=0.0,
            weekly_fc=np.full(52, 0.1),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_fc_too_high(self):
        # fc_mean = 2.0 > 0.5 → active demand, not a duplicate
        sku = _snap(
            on_order=100.0,
            order_up_to_level=80.0,
            catchup_order_qty=50.0,
            backorder=0.0,
            weekly_fc=np.full(52, 2.0),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_no_catchup(self):
        sku = _snap(
            on_order=100.0,
            order_up_to_level=80.0,
            catchup_order_qty=0.0,              # No catchup → no duplicate
            backorder=0.0,
            weekly_fc=np.full(52, 0.1),
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# negative_inventory_position
# ---------------------------------------------------------------------------

class TestNegativeInventoryPosition:
    RULE = "negative_inventory_position"
    THRESHOLDS = {"min_ip_vs_ss_ratio": -1.0}

    def test_triggers_when_ip_deeply_negative(self):
        # IP = -50, SS = 20 → ratio = -2.5 < -1.0
        sku = _snap(inventory_position=-50.0, safety_stock=20.0)
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["ip_to_ss_ratio"] < -1.0

    def test_no_trigger_when_ip_slightly_negative(self):
        # IP = -10, SS = 20 → ratio = -0.5 > -1.0
        sku = _snap(inventory_position=-10.0, safety_stock=20.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_ip_positive(self):
        sku = _snap(inventory_position=50.0, safety_stock=20.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_triggers_when_no_ss_and_ip_negative(self):
        # SS = 0, IP = -10 → any negative IP fires
        sku = _snap(inventory_position=-10.0, safety_stock=0.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True


# ---------------------------------------------------------------------------
# high_backorder_ratio
# ---------------------------------------------------------------------------

class TestHighBackorderRatio:
    RULE = "high_backorder_ratio"
    THRESHOLDS = {"min_backorder_to_ss_ratio": 2.0}

    def test_triggers_when_backorder_exceeds_2x_ss(self):
        # backorder=50, SS=20 → ratio=2.5 ≥ 2.0
        sku = _snap(backorder=50.0, safety_stock=20.0)
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["backorder_to_ss_ratio"] >= 2.0

    def test_no_trigger_when_backorder_small(self):
        # backorder=10, SS=20 → ratio=0.5 < 2.0
        sku = _snap(backorder=10.0, safety_stock=20.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_no_backorder(self):
        sku = _snap(backorder=0.0, safety_stock=20.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_triggers_when_no_ss_but_has_backorders(self):
        # SS=0, backorder=5 → always flag
        sku = _snap(backorder=5.0, safety_stock=0.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_when_no_ss_no_backorder(self):
        sku = _snap(backorder=0.0, safety_stock=0.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# negative_history
# ---------------------------------------------------------------------------

class TestNegativeHistory:
    RULE = "negative_history"
    THRESHOLDS = {}

    def test_triggers_on_negative_weekly_hist(self):
        hist = np.array([1.0, -0.5, 2.0, 3.0])
        sku = _snap(weekly_hist=hist, monthly_hist=np.ones(12))
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["negative_weekly_present"] is True

    def test_triggers_on_negative_monthly_hist(self):
        sku = _snap(weekly_hist=np.ones(52), monthly_hist=np.array([1.0, -2.0, 3.0]))
        hit, ev = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True
        assert ev["negative_monthly_present"] is True

    def test_no_trigger_all_positive(self):
        sku = _snap(weekly_hist=np.ones(52), monthly_hist=np.ones(12))
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_empty_arrays(self):
        sku = _snap(weekly_hist=np.array([]), monthly_hist=np.array([]))
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# zero_fc_nonzero_policy
# ---------------------------------------------------------------------------

class TestZeroFcNonzeroPolicy:
    RULE = "zero_fc_nonzero_policy"
    THRESHOLDS = {}

    def test_triggers_when_fc_zero_but_ss_nonzero(self):
        sku = _snap(weekly_fc=np.zeros(52), safety_stock=10.0, reorder_point=15.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_when_fc_nonzero(self):
        sku = _snap(weekly_fc=np.ones(52), safety_stock=10.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_all_zero(self):
        sku = _snap(
            weekly_fc=np.zeros(52),
            safety_stock=0.0,
            reorder_point=0.0,
            order_up_to_level=0.0,
        )
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# s_below_rop
# ---------------------------------------------------------------------------

class TestSBelowRop:
    RULE = "s_below_rop"
    THRESHOLDS = {}

    def test_triggers_when_s_below_rop(self):
        sku = _snap(order_up_to_level=20.0, reorder_point=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is True

    def test_no_trigger_when_s_above_rop(self):
        sku = _snap(order_up_to_level=80.0, reorder_point=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_s_is_zero(self):
        # s_level=0 → guard returns False
        sku = _snap(order_up_to_level=0.0, reorder_point=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False

    def test_no_trigger_when_s_equals_rop(self):
        sku = _snap(order_up_to_level=30.0, reorder_point=30.0)
        hit, _ = _rule_triggered(self.RULE, sku, self.THRESHOLDS)
        assert hit is False


# ---------------------------------------------------------------------------
# _series_is_monthly
# ---------------------------------------------------------------------------

class TestSeriesIsMonthly:
    def _monthly_series(self):
        # Dates separated by ~30 days
        import pandas as pd
        dates = pd.date_range("2025-01-01", periods=12, freq="MS")
        return {"fc_x": [d.strftime("%Y-%m-%d") for d in dates]}

    def _weekly_series(self):
        import pandas as pd
        dates = pd.date_range("2025-01-05", periods=52, freq="W-SUN")
        return {"fc_x": [d.strftime("%Y-%m-%d") for d in dates]}

    def test_detects_monthly(self):
        assert _series_is_monthly(self._monthly_series()) is True

    def test_detects_weekly(self):
        assert _series_is_monthly(self._weekly_series()) is False

    def test_empty_dict(self):
        assert _series_is_monthly({}) is False

    def test_non_dict(self):
        assert _series_is_monthly("not a dict") is False

    def test_too_few_dates(self):
        # Only 2 dates — not enough to determine
        assert _series_is_monthly({"fc_x": ["2025-01-01", "2025-02-01"]}) is False

    def test_monthly_in_hist_x(self):
        import pandas as pd
        dates = pd.date_range("2024-01-01", periods=24, freq="MS")
        series = {"hist_x": [d.strftime("%Y-%m-%d") for d in dates]}
        assert _series_is_monthly(series) is True
