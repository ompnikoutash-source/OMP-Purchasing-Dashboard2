"""
Parameter Optimizer for OMP Forecasting Safety Stock & Reorder Quantities.

Loads existing JSON forecast data (no database needed), re-computes safety stock
and order-up-to levels under different parameter sets, then runs stochastic
daily inventory simulations to find optimal parameters that:
  1. Never run out of stock (near-zero stockout rate)
  2. Don't produce unreasonably large/small reorder quantities
  3. Cut at least 20% of inventory by year-end
"""

import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

# ============================================================
# PATHS
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
SUNDRIES_JSON = BASE_DIR / "sundrieswebappJSON"
MOULDING_JSON = BASE_DIR / "mouldingwebappJSON"
FLOORING_JSON = BASE_DIR / "flooringwebappJSON"

# ============================================================
# FIXED CONSTANTS (not being tuned)
# ============================================================
DAYS_PER_WEEK = 7
WEEKS_PER_MONTH = 30.4 / 7
SERVICE_LEVEL = 0.95
Z_SCORE = stats.norm.ppf(SERVICE_LEVEL)  # ~1.6449
MIN_LEAD_TIME_DAYS = 7
DEFAULT_LEAD_TIME_DAYS = 30  # Fallback when not in JSON

# Coverage horizons (already tuned in prior session)
COVERAGE_HORIZON = {"A": 90, "B": 60, "C": 30}

# Monte Carlo settings
N_REPLICATIONS = 100
SIMULATION_DAYS = 365
SIMULATION_MONTHS = 12
RANDOM_SEED = 2027


# ============================================================
# PARAMETER SET DEFINITION
# ============================================================
@dataclass
class ParamSet:
    """A set of tunable parameters for safety stock computation."""
    name: str
    ss_uplift_A: float
    ss_uplift_B: float
    ss_uplift_C: float
    ss_floor_days: float
    lumpy_buffer_frac: float
    ss_cap_months: float  # 0 = no cap, else cap SS at N months of demand
    # Lumpy percentiles (less impactful, keep at original values)
    lumpy_pctl: Dict[str, float] = field(default_factory=lambda: {"A": 0.95, "B": 0.90, "C": 0.85})

    def uplift(self, abc: str) -> float:
        return {"A": self.ss_uplift_A, "B": self.ss_uplift_B, "C": self.ss_uplift_C}.get(abc, 0.0)


# ============================================================
# SKU DATA CONTAINER
# ============================================================
@dataclass
class SKUData:
    """Extracted data for one SKU from the JSON payload."""
    item_number: str
    abc_class: str
    demand_class: str
    forecast_method: str
    inventory_position: float
    available: float
    on_order: float
    hist_y: np.ndarray       # Weekly historical demand
    fc_y: np.ndarray         # 52-week forecast
    lead_time_days: float
    uom: str
    product_type: str        # "sundries", "moulding", or "flooring"
    base_safety_stock: float = 0.0
    base_reorder_point: float = 0.0
    base_reorder_qty: float = 0.0
    daily_mean_demand: float = 0.0
    weekly_std_demand: float = 0.0


# ============================================================
# LOAD & SAMPLE SKUs
# ============================================================
def _as_float(value, default: float = 0.0) -> float:
    """Best-effort float conversion for mixed JSON fields."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_skus(json_path: Path, product_type: str) -> List[SKUData]:
    """Load SKU data from a forecast JSON file."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Flooring keeps forecast_series in "items"; sundries/moulding use Inventory_Metrics.
    if product_type == "flooring":
        items = data.get("items", [])
    else:
        items = data.get("Inventory_Metrics", data.get("items", []))

    metrics_idx = {}
    for metric in data.get("Inventory_Metrics", []):
        sku_key = str(metric.get("sku") or metric.get("item_number") or "").strip()
        if sku_key:
            metrics_idx[sku_key] = metric

    skus = []
    for item in items:
        sku_key = str(item.get("item_number") or item.get("sku") or item.get("SKU") or "").strip()
        if not sku_key:
            continue

        metric = metrics_idx.get(sku_key, {})
        segmentation = item.get("segmentation") if isinstance(item.get("segmentation"), dict) else {}
        fs = item.get("forecast_series", {}) or {}
        hist_y = np.array(fs.get("hist_y", []), dtype=float)
        fc_y = np.array(fs.get("fc_y", []), dtype=float)

        if len(hist_y) < 8 or len(fc_y) == 0:
            continue

        abc_class = str(
            item.get("abc_class")
            or item.get("sku_abc")
            or metric.get("sku_abc")
            or segmentation.get("maturity")
            or "C"
        ).upper()
        if abc_class not in {"A", "B", "C"}:
            abc_class = "C"

        demand_class = str(
            item.get("demand_class")
            or metric.get("demand_class")
            or segmentation.get("demand_pattern")
            or "SMOOTH"
        ).upper()

        forecast_method = str(
            item.get("forecast_method")
            or metric.get("forecast_method")
            or "?"
        )

        inventory_position = _as_float(item.get("inventory_position", metric.get("inventory_position", 0)))
        available = _as_float(item.get("available", item.get("available_sf", metric.get("available_sf", 0))))
        on_order = _as_float(item.get("on_order", item.get("on_po", item.get("on_po_sf", metric.get("on_po_sf", 0)))))

        lt_days = _as_float(item.get("lead_time_days", metric.get("lead_time_days", DEFAULT_LEAD_TIME_DAYS)), DEFAULT_LEAD_TIME_DAYS)
        daily_demand = _as_float(
            item.get("daily_demand", item.get("daily_avg_demand", metric.get("daily_mean_demand", 0))),
            0.0,
        )
        if lt_days <= 0:
            rop = _as_float(item.get("reorder_point", metric.get("reorder_point", 0)))
            ss = _as_float(item.get("safety_stock", metric.get("safety_stock", 0)))
            mu_L = rop - ss
            if daily_demand > 0 and mu_L > 0:
                lt_days = mu_L / daily_demand
            else:
                lt_days = DEFAULT_LEAD_TIME_DAYS
        lt_days = max(lt_days, MIN_LEAD_TIME_DAYS)

        skus.append(SKUData(
            item_number=sku_key,
            abc_class=abc_class,
            demand_class=demand_class,
            forecast_method=forecast_method,
            inventory_position=inventory_position,
            available=available,
            on_order=on_order,
            hist_y=hist_y,
            fc_y=fc_y,
            lead_time_days=lt_days,
            uom=str(item.get("uom", metric.get("uom", "EA"))),
            product_type=product_type,
            base_safety_stock=_as_float(item.get("safety_stock", metric.get("safety_stock", 0))),
            base_reorder_point=_as_float(item.get("reorder_point", metric.get("reorder_point", 0))),
            base_reorder_qty=_as_float(item.get("reorder_quantity", metric.get("reorder_quantity", 0))),
            daily_mean_demand=daily_demand,
            weekly_std_demand=_as_float(item.get("weekly_std_demand", metric.get("weekly_std_demand", 0))),
        ))
    return skus


def random_sample(skus: List[SKUData], n: int, seed: int) -> List[SKUData]:
    """Uniform random sample of SKUs."""
    rng = random.Random(seed)
    if len(skus) <= n:
        return skus.copy()
    return rng.sample(skus, n)


# ============================================================
# SAFETY STOCK & REORDER COMPUTATION (mirrors core/forecasting.py)
# ============================================================
def compute_safety_stock(sku: SKUData, params: ParamSet) -> Dict[str, float]:
    """Compute safety stock for a SKU under a given parameter set."""
    mean_weekly = float(np.mean(sku.fc_y))
    std_weekly = float(np.std(sku.hist_y)) if len(sku.hist_y) > 0 else 0.0

    lead_time_days = max(sku.lead_time_days, MIN_LEAD_TIME_DAYS)
    lead_time_weeks = lead_time_days / DAYS_PER_WEEK

    std_daily = std_weekly / DAYS_PER_WEEK
    sigma_L = std_daily * math.sqrt(lead_time_days) if lead_time_days > 0 else 0.0

    # Base safety stock
    ss_base = Z_SCORE * sigma_L

    # ABC uplift
    uplift_scalar = params.uplift(sku.abc_class)
    ss_uplift = ss_base * uplift_scalar

    # Lumpy buffer
    lumpy_buffer = 0.0
    if sku.demand_class == "LUMPY":
        nonzero = sku.hist_y[sku.hist_y > 0]
        if len(nonzero) > 0:
            pctl = params.lumpy_pctl.get(sku.abc_class, 0.90)
            pctl_demand = float(np.percentile(nonzero, pctl * 100))
            lumpy_buffer = pctl_demand * lead_time_weeks * params.lumpy_buffer_frac

    total_uplift = ss_uplift + lumpy_buffer

    # Safety stock floor
    ss_floor = 0.0
    if params.ss_floor_days > 0:
        nonzero = sku.hist_y[sku.hist_y > 0]
        if len(nonzero) > 0:
            avg_nz_daily = float(np.mean(nonzero)) / DAYS_PER_WEEK
            ss_floor = params.ss_floor_days * avg_nz_daily

    calculated_ss = ss_base + total_uplift

    # 3-month cap (if enabled)
    if params.ss_cap_months > 0 and mean_weekly > 0:
        max_ss = mean_weekly * WEEKS_PER_MONTH * params.ss_cap_months
        calculated_ss = min(calculated_ss, max_ss)

    final_ss = max(ss_floor, calculated_ss)

    return {
        "ss_base": ss_base,
        "safety_stock": final_ss,
        "ss_floor": ss_floor,
    }


def compute_reorder_metrics(sku: SKUData, params: ParamSet) -> Dict[str, float]:
    """Compute ROP and order-up-to level for a SKU under a given parameter set."""
    mean_weekly = float(np.mean(sku.fc_y))
    std_weekly = float(np.std(sku.hist_y)) if len(sku.hist_y) > 0 else 0.0
    mean_daily = mean_weekly / DAYS_PER_WEEK
    std_daily = std_weekly / DAYS_PER_WEEK

    lead_time_days = max(sku.lead_time_days, MIN_LEAD_TIME_DAYS)

    # Expected demand over lead time
    mu_L = mean_daily * lead_time_days

    # Safety stock
    ss = compute_safety_stock(sku, params)

    # Reorder point
    rop = mu_L + ss["safety_stock"]

    # Order-up-to level
    coverage_days = COVERAGE_HORIZON.get(sku.abc_class, 60)
    total_horizon = lead_time_days + coverage_days
    mu_LT = mean_daily * total_horizon
    sigma_LT = std_daily * math.sqrt(total_horizon) if total_horizon > 0 else 0.0

    uplift_scalar = params.uplift(sku.abc_class)
    buffer_LT = Z_SCORE * sigma_LT * (1 + uplift_scalar)
    order_up_to = mu_LT + buffer_LT

    # Floor for order-up-to
    if params.ss_floor_days > 0:
        nonzero = sku.hist_y[sku.hist_y > 0]
        if len(nonzero) > 0:
            avg_nz_daily = float(np.mean(nonzero)) / DAYS_PER_WEEK
            s_floor = params.ss_floor_days * avg_nz_daily + mu_LT
            order_up_to = max(s_floor, order_up_to)

    # Cap order-up-to if SS cap is active (to keep consistency)
    if params.ss_cap_months > 0 and mean_daily > 0:
        max_s = mu_LT + mean_weekly * WEEKS_PER_MONTH * params.ss_cap_months
        order_up_to = min(order_up_to, max_s)

    nominal_q = max(0, order_up_to - rop)

    return {
        "reorder_point": rop,
        "order_up_to_level": order_up_to,
        "nominal_q": nominal_q,
        "safety_stock": ss["safety_stock"],
        "mean_daily": mean_daily,
        "lead_time_days": lead_time_days,
    }


# ============================================================
# STOCHASTIC DAILY SIMULATION
# ============================================================
def simulate_daily_stochastic(
    starting_inventory: float,
    reorder_point: float,
    order_up_to_level: float,
    lead_time_days: int,
    daily_demands: np.ndarray,  # Pre-sampled daily demand array
) -> Dict[str, float]:
    """Run one replication of stochastic daily inventory simulation."""
    on_hand = starting_inventory
    backorders = 0.0
    inbound = {}
    total_orders = 0
    total_order_qty = 0.0
    stockout_days = 0
    max_inventory = starting_inventory
    order_sizes = []

    for day in range(len(daily_demands)):
        # Receive arrivals
        if day in inbound:
            on_hand += inbound[day]
            del inbound[day]

        # Inventory position
        total_inbound = sum(inbound.values())
        ip = on_hand + total_inbound - backorders

        # Reorder check
        if ip < reorder_point and order_up_to_level > 0:
            oq = max(0, order_up_to_level - ip)
            if oq > 0:
                total_orders += 1
                total_order_qty += oq
                order_sizes.append(oq)
                if lead_time_days == 0:
                    on_hand += oq
                else:
                    arrival = day + lead_time_days
                    inbound[arrival] = inbound.get(arrival, 0) + oq

        # Apply demand
        demand = daily_demands[day]
        if on_hand >= demand:
            on_hand -= demand
        else:
            shortfall = demand - on_hand
            on_hand = 0.0
            backorders += shortfall

        # Fill backorders
        if on_hand > 0 and backorders > 0:
            filled = min(on_hand, backorders)
            on_hand -= filled
            backorders -= filled

        if backorders > 0:
            stockout_days += 1

        max_inventory = max(max_inventory, on_hand)

    # End-of-year inventory
    ending_inv = on_hand - backorders

    return {
        "stockout_days": stockout_days,
        "ending_inventory": ending_inv,
        "on_hand_end": on_hand,
        "backorders_end": backorders,
        "total_orders": total_orders,
        "total_order_qty": total_order_qty,
        "max_inventory": max_inventory,
        "avg_order_size": np.mean(order_sizes) if order_sizes else 0,
    }


def generate_daily_demands(sku: SKUData, n_days: int, rng: np.random.RandomState) -> np.ndarray:
    """Generate stochastic daily demand from historical weekly distribution."""
    weekly_hist = sku.hist_y
    mean_weekly_fc = float(np.mean(sku.fc_y))

    if sku.demand_class == "LUMPY" or sku.demand_class == "INTERMITTENT":
        # Two-stage: (1) does demand occur this week? (2) if yes, how much?
        nonzero = weekly_hist[weekly_hist > 0]
        p_nonzero = len(nonzero) / len(weekly_hist) if len(weekly_hist) > 0 else 0.5

        if len(nonzero) == 0:
            return np.zeros(n_days)

        # Scale non-zero distribution by forecast/historical ratio
        hist_mean = float(np.mean(weekly_hist))
        scale = mean_weekly_fc / hist_mean if hist_mean > 0 else 1.0
        scale = max(0.1, min(scale, 5.0))  # Clamp to avoid extremes

        daily_demands = np.zeros(n_days)
        for w in range(n_days // 7 + 1):
            # Does demand occur this week?
            if rng.random() < p_nonzero:
                # Sample from non-zero weekly distribution, scaled by forecast
                weekly_demand = rng.choice(nonzero) * scale
                weekly_demand = max(0, weekly_demand)
                # Spread across the 7 days of this week
                start_day = w * 7
                end_day = min(start_day + 7, n_days)
                daily_amount = weekly_demand / 7
                daily_demands[start_day:end_day] = daily_amount

        return daily_demands[:n_days]
    else:
        # SMOOTH or ERRATIC: sample weekly from historical, scale by forecast ratio
        hist_mean = float(np.mean(weekly_hist))
        scale = mean_weekly_fc / hist_mean if hist_mean > 0 else 1.0
        scale = max(0.1, min(scale, 5.0))

        daily_demands = np.zeros(n_days)
        for w in range(n_days // 7 + 1):
            weekly_demand = rng.choice(weekly_hist) * scale
            weekly_demand = max(0, weekly_demand)
            start_day = w * 7
            end_day = min(start_day + 7, n_days)
            daily_demands[start_day:end_day] = weekly_demand / 7

        return daily_demands[:n_days]


def compute_reorder_metrics_flooring(sku: SKUData) -> Dict[str, float]:
    """
    Flooring-specific reorder metrics.

    Flooring planning uses a fixed reorder quantity policy in the monthly
    simulator (not the daily order-up-to policy used by sundries/moulding).
    """
    monthly_fc = np.clip(sku.fc_y, 0, None)
    mean_monthly = float(np.mean(monthly_fc)) if len(monthly_fc) > 0 else 0.0
    mean_daily = sku.daily_mean_demand if sku.daily_mean_demand > 0 else (mean_monthly / 30.4 if mean_monthly > 0 else 0.0)

    lead_time_days = max(sku.lead_time_days, MIN_LEAD_TIME_DAYS)
    lead_time_weeks = lead_time_days / DAYS_PER_WEEK
    weekly_mean = mean_daily * DAYS_PER_WEEK

    weekly_std = sku.weekly_std_demand
    if weekly_std <= 0:
        hist_nonneg = np.clip(sku.hist_y, 0, None)
        weekly_std = float(np.std(hist_nonneg)) if len(hist_nonneg) > 0 else 0.0

    safety_stock = sku.base_safety_stock
    if safety_stock <= 0:
        safety_stock = Z_SCORE * weekly_std * math.sqrt(lead_time_weeks)

    reorder_point = sku.base_reorder_point
    if reorder_point <= 0:
        reorder_point = (weekly_mean * lead_time_weeks) + safety_stock

    reorder_qty = sku.base_reorder_qty
    if reorder_qty <= 0:
        review_period_weeks = 1.0
        reorder_qty = weekly_mean * (lead_time_weeks + review_period_weeks)

    reorder_qty = max(0.0, reorder_qty)
    order_up_to = reorder_point + reorder_qty

    return {
        "reorder_point": reorder_point,
        "order_up_to_level": order_up_to,
        "nominal_q": reorder_qty,
        "safety_stock": max(0.0, safety_stock),
        "mean_daily": max(0.0, mean_daily),
        "lead_time_days": lead_time_days,
    }


def generate_monthly_demands_flooring(sku: SKUData, n_months: int, rng: np.random.RandomState) -> np.ndarray:
    """Generate stochastic monthly demand aligned to flooring's monthly policy."""
    hist = np.clip(sku.hist_y.astype(float), 0, None)
    fc = np.clip(sku.fc_y.astype(float), 0, None)
    if len(fc) == 0:
        fallback_monthly = max(0.0, sku.daily_mean_demand * 30.4)
        fc = np.array([fallback_monthly], dtype=float)

    fc_mean = float(np.mean(fc)) if len(fc) > 0 else 0.0
    hist_mean = float(np.mean(hist)) if len(hist) > 0 else 0.0
    scale = (fc_mean / hist_mean) if hist_mean > 0 else 1.0
    scale = max(0.2, min(scale, 5.0))

    nonzero = hist[hist > 0]
    p_nonzero = (len(nonzero) / len(hist)) if len(hist) > 0 else (1.0 if fc_mean > 0 else 0.0)

    demands = np.zeros(n_months, dtype=float)
    intermittent_classes = {"LUMPY", "INTERMITTENT"}

    for m in range(n_months):
        target = float(fc[m % len(fc)]) if len(fc) > 0 else 0.0

        if len(hist) == 0:
            sampled = target
        elif sku.demand_class in intermittent_classes:
            if rng.random() < p_nonzero and len(nonzero) > 0:
                sampled = float(rng.choice(nonzero)) * scale
            else:
                sampled = 0.0
        else:
            sampled = float(rng.choice(hist)) * scale

        sampled = max(0.0, sampled)

        # Blend random draw with the monthly forecast level to preserve trend.
        if fc_mean > 0:
            seasonal_adj = max(0.3, min(target / fc_mean, 3.0))
            sampled *= seasonal_adj
        demand = (0.5 * sampled) + (0.5 * max(0.0, target))
        demands[m] = max(0.0, demand)

    return demands


def simulate_monthly_stochastic_flooring(
    starting_inventory: float,
    reorder_point: float,
    reorder_qty: float,
    monthly_demands: np.ndarray,
) -> Dict[str, float]:
    """Run one stochastic monthly simulation using flooring's fixed-quantity policy."""
    on_hand = starting_inventory
    total_orders = 0
    total_order_qty = 0.0
    stockout_months = 0
    stockout_units = 0.0
    max_inventory = starting_inventory
    order_sizes = []

    for demand in monthly_demands:
        if on_hand <= reorder_point and reorder_qty > 0:
            on_hand += reorder_qty
            total_orders += 1
            total_order_qty += reorder_qty
            order_sizes.append(reorder_qty)

        if on_hand >= demand:
            on_hand -= demand
        else:
            stockout_units += (demand - on_hand)
            on_hand = 0.0
            stockout_months += 1

        max_inventory = max(max_inventory, on_hand)

    # Keep field name for compatibility with existing summary/scoring code.
    stockout_days_equivalent = float(stockout_months)

    return {
        "stockout_days": stockout_days_equivalent,
        "stockout_months": stockout_months,
        "stockout_units": stockout_units,
        "ending_inventory": on_hand,
        "on_hand_end": on_hand,
        "backorders_end": stockout_units,
        "total_orders": total_orders,
        "total_order_qty": total_order_qty,
        "max_inventory": max_inventory,
        "avg_order_size": np.mean(order_sizes) if order_sizes else 0,
    }


# ============================================================
# RUN SIMULATION FOR ONE PARAMETER SET
# ============================================================
@dataclass
class SKUResult:
    item_number: str
    abc_class: str
    demand_class: str
    product_type: str
    current_inv: float
    mean_daily: float
    safety_stock: float
    rop: float
    order_up_to: float
    nominal_q: float
    stockout_pct: float       # % of replications with any stockout
    avg_stockout_days: float
    avg_ending_inv: float
    avg_total_orders: float
    avg_order_size: float
    inv_reduction_pct: float  # (current - avg_ending) / current * 100
    roq_months: float         # nominal_q in months of demand


def evaluate_sku(sku: SKUData, params: ParamSet, rng_seed: int = RANDOM_SEED) -> SKUResult:
    """Run Monte Carlo simulation for one SKU under one parameter set."""
    if sku.product_type == "flooring":
        metrics = compute_reorder_metrics_flooring(sku)
    else:
        metrics = compute_reorder_metrics(sku, params)

    rop = metrics["reorder_point"]
    s_level = metrics["order_up_to_level"]
    ss = metrics["safety_stock"]
    mean_daily = metrics["mean_daily"]
    lt_days = int(metrics["lead_time_days"])
    nominal_q = metrics["nominal_q"]

    stockout_counts = []
    ending_invs = []
    total_orders_list = []
    order_sizes = []

    for rep in range(N_REPLICATIONS):
        rep_rng = np.random.RandomState(rng_seed + rep)
        if sku.product_type == "flooring":
            monthly_demands = generate_monthly_demands_flooring(sku, SIMULATION_MONTHS, rep_rng)
            result = simulate_monthly_stochastic_flooring(
                starting_inventory=sku.inventory_position,
                reorder_point=rop,
                reorder_qty=nominal_q,
                monthly_demands=monthly_demands,
            )
        else:
            demands = generate_daily_demands(sku, SIMULATION_DAYS, rep_rng)
            result = simulate_daily_stochastic(
                starting_inventory=sku.inventory_position,
                reorder_point=rop,
                order_up_to_level=s_level,
                lead_time_days=lt_days,
                daily_demands=demands,
            )
        stockout_counts.append(result["stockout_days"])
        ending_invs.append(result["ending_inventory"])
        total_orders_list.append(result["total_orders"])
        if result["avg_order_size"] > 0:
            order_sizes.append(result["avg_order_size"])

    stockout_pct = sum(1 for s in stockout_counts if s > 0) / N_REPLICATIONS * 100
    avg_stockout_days = np.mean(stockout_counts)
    avg_ending = np.mean(ending_invs)
    avg_orders = np.mean(total_orders_list)
    avg_osize = np.mean(order_sizes) if order_sizes else 0

    inv_reduction = 0.0
    if sku.inventory_position > 0:
        inv_reduction = (sku.inventory_position - avg_ending) / sku.inventory_position * 100

    monthly_demand = mean_daily * 30.4
    roq_months = nominal_q / monthly_demand if monthly_demand > 0 else 0

    return SKUResult(
        item_number=sku.item_number,
        abc_class=sku.abc_class,
        demand_class=sku.demand_class,
        product_type=sku.product_type,
        current_inv=sku.inventory_position,
        mean_daily=mean_daily,
        safety_stock=ss,
        rop=rop,
        order_up_to=s_level,
        nominal_q=nominal_q,
        stockout_pct=stockout_pct,
        avg_stockout_days=avg_stockout_days,
        avg_ending_inv=avg_ending,
        avg_total_orders=avg_orders,
        avg_order_size=avg_osize,
        inv_reduction_pct=inv_reduction,
        roq_months=roq_months,
    )


def evaluate_param_set(
    skus: List[SKUData], params: ParamSet, label: str = ""
) -> Tuple[Dict, List[SKUResult]]:
    """Evaluate a parameter set across all SKUs. Returns summary + per-SKU details."""
    results = []
    for sku in skus:
        r = evaluate_sku(sku, params)
        results.append(r)

    # Aggregate by ABC class
    abc_summary = {}
    for abc in ["A", "B", "C"]:
        abc_results = [r for r in results if r.abc_class == abc]
        if not abc_results:
            continue
        abc_summary[abc] = {
            "count": len(abc_results),
            "stockout_pct": np.mean([r.stockout_pct for r in abc_results]),
            "avg_stockout_days": np.mean([r.avg_stockout_days for r in abc_results]),
            "avg_inv_reduction": np.mean([r.inv_reduction_pct for r in abc_results]),
            "avg_roq_months": np.mean([r.roq_months for r in abc_results]),
            "total_current_inv": sum(r.current_inv for r in abc_results),
            "total_ending_inv": sum(r.avg_ending_inv for r in abc_results),
        }

    total_current = sum(r.current_inv for r in results)
    total_ending = sum(r.avg_ending_inv for r in results)
    overall_reduction = (total_current - total_ending) / total_current * 100 if total_current > 0 else 0

    product_summary = {}
    for product_type in sorted({r.product_type for r in results}):
        pt_results = [r for r in results if r.product_type == product_type]
        pt_current = sum(r.current_inv for r in pt_results)
        pt_ending = sum(r.avg_ending_inv for r in pt_results)
        pt_weighted_reduction = (pt_current - pt_ending) / pt_current * 100 if pt_current > 0 else 0
        product_summary[product_type] = {
            "count": len(pt_results),
            "avg_stockout_days": np.mean([r.avg_stockout_days for r in pt_results]),
            "avg_stockout_pct": np.mean([r.stockout_pct for r in pt_results]),
            "avg_roq_months": np.mean([r.roq_months for r in pt_results]),
            "total_current_inv": pt_current,
            "total_ending_inv": pt_ending,
            "weighted_inv_reduction_pct": pt_weighted_reduction,
        }

    # Stockout targets: A=0%, B<5%, C<10%
    a_stockout = abc_summary.get("A", {}).get("stockout_pct", 0)
    b_stockout = abc_summary.get("B", {}).get("stockout_pct", 0)
    c_stockout = abc_summary.get("C", {}).get("stockout_pct", 0)

    # Realistic targets for stochastic simulation:
    # "Stockout %" = % of replications where any stockout day occurred.
    # Even 95% service level means ~18 stockout days/year, so any single replication
    # will likely have some. We score on average stockout days instead.
    a_avg_so_days = abc_summary.get("A", {}).get("avg_stockout_days", 0)
    b_avg_so_days = abc_summary.get("B", {}).get("avg_stockout_days", 0)
    c_avg_so_days = abc_summary.get("C", {}).get("avg_stockout_days", 0)

    # Targets: A < 3 days/year avg, B < 7 days, C < 14 days
    stockout_ok = a_avg_so_days < 3 and b_avg_so_days < 7 and c_avg_so_days < 14

    # ROQ reasonableness: flag if any SKU has ROQ > 6 months or < 1 week
    unreasonable = [r for r in results if r.mean_daily > 0 and (r.roq_months > 6 or r.roq_months < 0.25)]

    summary = {
        "name": params.name,
        "total_current_inv": total_current,
        "total_ending_inv": total_ending,
        "overall_reduction_pct": overall_reduction,
        "a_stockout_pct": a_stockout,
        "b_stockout_pct": b_stockout,
        "c_stockout_pct": c_stockout,
        "a_avg_so_days": a_avg_so_days,
        "b_avg_so_days": b_avg_so_days,
        "c_avg_so_days": c_avg_so_days,
        "stockout_ok": stockout_ok,
        "unreasonable_roq_count": len(unreasonable),
        "abc_summary": abc_summary,
        "product_summary": product_summary,
        "meets_target": stockout_ok and overall_reduction >= 20,
    }

    return summary, results


# ============================================================
# PARAMETER GRID
# ============================================================
def build_phase_a_params() -> List[ParamSet]:
    """Phase A: Test each parameter independently with a reasonable baseline."""
    # Baseline: close to the original file values
    base = {"ss_uplift_A": 1.0, "ss_uplift_B": 0.35, "ss_uplift_C": 0.0,
            "ss_floor_days": 7, "lumpy_buffer_frac": 0.15, "ss_cap_months": 3}

    params = []

    # Current (aggressive) config
    params.append(ParamSet(
        name="CURRENT", ss_uplift_A=6.0, ss_uplift_B=4.0, ss_uplift_C=2.0,
        ss_floor_days=21, lumpy_buffer_frac=0.50, ss_cap_months=0))

    # Original config
    params.append(ParamSet(
        name="ORIGINAL", ss_uplift_A=1.0, ss_uplift_B=0.35, ss_uplift_C=0.0,
        ss_floor_days=0, lumpy_buffer_frac=0.10, ss_cap_months=3))

    # Sweep SS_UPLIFT_A
    for v in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        params.append(ParamSet(
            name=f"uplift_A={v}", ss_uplift_A=v, ss_uplift_B=base["ss_uplift_B"],
            ss_uplift_C=base["ss_uplift_C"], ss_floor_days=base["ss_floor_days"],
            lumpy_buffer_frac=base["lumpy_buffer_frac"], ss_cap_months=base["ss_cap_months"]))

    # Sweep SS_UPLIFT_B
    for v in [0.1, 0.2, 0.35, 0.5, 0.75, 1.0]:
        params.append(ParamSet(
            name=f"uplift_B={v}", ss_uplift_A=base["ss_uplift_A"], ss_uplift_B=v,
            ss_uplift_C=base["ss_uplift_C"], ss_floor_days=base["ss_floor_days"],
            lumpy_buffer_frac=base["lumpy_buffer_frac"], ss_cap_months=base["ss_cap_months"]))

    # Sweep SS_UPLIFT_C
    for v in [0.0, 0.05, 0.1, 0.2, 0.35, 0.5]:
        params.append(ParamSet(
            name=f"uplift_C={v}", ss_uplift_A=base["ss_uplift_A"],
            ss_uplift_B=base["ss_uplift_B"], ss_uplift_C=v,
            ss_floor_days=base["ss_floor_days"],
            lumpy_buffer_frac=base["lumpy_buffer_frac"], ss_cap_months=base["ss_cap_months"]))

    # Sweep SS_FLOOR_DAYS
    for v in [0, 3, 5, 7, 10, 14]:
        params.append(ParamSet(
            name=f"floor={v}d", ss_uplift_A=base["ss_uplift_A"],
            ss_uplift_B=base["ss_uplift_B"], ss_uplift_C=base["ss_uplift_C"],
            ss_floor_days=v, lumpy_buffer_frac=base["lumpy_buffer_frac"],
            ss_cap_months=base["ss_cap_months"]))

    # Sweep LUMPY_BUFFER_FRAC
    for v in [0.05, 0.10, 0.15, 0.20, 0.30, 0.40]:
        params.append(ParamSet(
            name=f"lumpy={v}", ss_uplift_A=base["ss_uplift_A"],
            ss_uplift_B=base["ss_uplift_B"], ss_uplift_C=base["ss_uplift_C"],
            ss_floor_days=base["ss_floor_days"], lumpy_buffer_frac=v,
            ss_cap_months=base["ss_cap_months"]))

    # Sweep SS_CAP_MONTHS
    for v in [0, 2, 3, 4, 6]:
        params.append(ParamSet(
            name=f"cap={v}mo", ss_uplift_A=base["ss_uplift_A"],
            ss_uplift_B=base["ss_uplift_B"], ss_uplift_C=base["ss_uplift_C"],
            ss_floor_days=base["ss_floor_days"],
            lumpy_buffer_frac=base["lumpy_buffer_frac"], ss_cap_months=v))

    return params


def build_phase_b_params(phase_a_insights: Dict) -> List[ParamSet]:
    """Phase B: Full combinations spanning from original to current."""
    combos = []

    # Reference: Current ultra-aggressive
    combos.append(ParamSet(
        name="CURRENT", ss_uplift_A=6.0, ss_uplift_B=4.0, ss_uplift_C=2.0,
        ss_floor_days=21, lumpy_buffer_frac=0.50, ss_cap_months=0))

    # Reference: Original conservative
    combos.append(ParamSet(
        name="ORIGINAL", ss_uplift_A=1.0, ss_uplift_B=0.35, ss_uplift_C=0.0,
        ss_floor_days=0, lumpy_buffer_frac=0.10, ss_cap_months=3))

    # ── Intermediate combos between CURRENT and ORIGINAL ──

    # High safety, moderate lean (close to current but with caps)
    combos.append(ParamSet(
        name="HIGH_CAPPED", ss_uplift_A=4.0, ss_uplift_B=2.5, ss_uplift_C=1.0,
        ss_floor_days=14, lumpy_buffer_frac=0.30, ss_cap_months=4))

    # Medium-high
    combos.append(ParamSet(
        name="MED_HIGH", ss_uplift_A=3.0, ss_uplift_B=1.5, ss_uplift_C=0.5,
        ss_floor_days=10, lumpy_buffer_frac=0.25, ss_cap_months=4))

    # Medium
    combos.append(ParamSet(
        name="MEDIUM", ss_uplift_A=2.5, ss_uplift_B=1.0, ss_uplift_C=0.3,
        ss_floor_days=10, lumpy_buffer_frac=0.20, ss_cap_months=3))

    # Medium-lean
    combos.append(ParamSet(
        name="MED_LEAN", ss_uplift_A=2.0, ss_uplift_B=0.75, ss_uplift_C=0.2,
        ss_floor_days=7, lumpy_buffer_frac=0.20, ss_cap_months=3))

    # Lean-plus
    combos.append(ParamSet(
        name="LEAN_PLUS", ss_uplift_A=1.5, ss_uplift_B=0.5, ss_uplift_C=0.1,
        ss_floor_days=7, lumpy_buffer_frac=0.15, ss_cap_months=3))

    # Lean
    combos.append(ParamSet(
        name="LEAN", ss_uplift_A=1.0, ss_uplift_B=0.35, ss_uplift_C=0.05,
        ss_floor_days=5, lumpy_buffer_frac=0.10, ss_cap_months=3))

    # ── Targeted tweaks ──

    # A-items need most protection (fast sellers, risk = lost sales)
    # B/C less critical (slow movers, risk = excess inventory)
    combos.append(ParamSet(
        name="A_HEAVY", ss_uplift_A=3.5, ss_uplift_B=1.0, ss_uplift_C=0.2,
        ss_floor_days=7, lumpy_buffer_frac=0.20, ss_cap_months=3))

    combos.append(ParamSet(
        name="A_HEAVIER", ss_uplift_A=4.0, ss_uplift_B=1.0, ss_uplift_C=0.2,
        ss_floor_days=10, lumpy_buffer_frac=0.20, ss_cap_months=3))

    # Floor-focused (lean uplifts but higher floor for slow movers)
    combos.append(ParamSet(
        name="FLOOR_FOCUSED", ss_uplift_A=2.0, ss_uplift_B=0.75, ss_uplift_C=0.1,
        ss_floor_days=14, lumpy_buffer_frac=0.15, ss_cap_months=3))

    # Lumpy-focused (higher lumpy buffer, moderate uplifts)
    combos.append(ParamSet(
        name="LUMPY_FOCUSED", ss_uplift_A=2.0, ss_uplift_B=0.75, ss_uplift_C=0.2,
        ss_floor_days=7, lumpy_buffer_frac=0.30, ss_cap_months=3))

    # Build from Phase A best individuals
    best_a = phase_a_insights.get("best_uplift_A", 2.0)
    best_b = phase_a_insights.get("best_uplift_B", 0.75)
    best_c = phase_a_insights.get("best_uplift_C", 0.1)
    best_floor = phase_a_insights.get("best_floor", 7)
    best_lumpy = phase_a_insights.get("best_lumpy", 0.15)
    best_cap = phase_a_insights.get("best_cap", 3)

    combos.append(ParamSet(
        name="PHASE_A_BEST", ss_uplift_A=best_a, ss_uplift_B=best_b,
        ss_uplift_C=best_c, ss_floor_days=best_floor,
        lumpy_buffer_frac=best_lumpy, ss_cap_months=best_cap))

    return combos


# ============================================================
# MAIN
# ============================================================
def print_summary(summary: Dict, verbose: bool = False):
    """Pretty-print a parameter set summary."""
    name = summary["name"]
    reduction = summary["overall_reduction_pct"]
    a_so = summary["a_stockout_pct"]
    b_so = summary["b_stockout_pct"]
    c_so = summary["c_stockout_pct"]
    meets = summary["meets_target"]
    unreasonable = summary["unreasonable_roq_count"]

    a_days = summary.get("a_avg_so_days", 0)
    b_days = summary.get("b_avg_so_days", 0)
    c_days = summary.get("c_avg_so_days", 0)
    flag = "OK" if meets else "FAIL"
    print(f"  {name:<25s}  Inv: {reduction:+6.1f}%  "
          f"AvgSOdays A:{a_days:4.1f} B:{b_days:4.1f} C:{c_days:4.1f}  "
          f"BadROQ:{unreasonable:2d}  [{flag}]")

    if verbose:
        for abc, data in summary.get("abc_summary", {}).items():
            print(f"    {abc}: n={data['count']:3d}  "
                  f"stockout={data['stockout_pct']:.1f}%  "
                  f"inv_red={data['avg_inv_reduction']:.1f}%  "
                  f"roq_mo={data['avg_roq_months']:.1f}")
        for product_type, data in summary.get("product_summary", {}).items():
            print(
                f"    {product_type}: n={data['count']:3d}  "
                f"weighted_inv_red={data['weighted_inv_reduction_pct']:+.1f}%  "
                f"avg_so_days={data['avg_stockout_days']:.1f}  "
                f"avg_so%={data['avg_stockout_pct']:.1f}%  "
                f"roq_mo={data['avg_roq_months']:.1f}"
            )


def main():
    print("=" * 80)
    print("OMP PARAMETER OPTIMIZER")
    print("=" * 80)

    # Load data
    print("\nLoading sundries data...")
    sundries_all = load_skus(SUNDRIES_JSON, "sundries")
    print(f"  Loaded {len(sundries_all)} sundries SKUs")

    print("Loading moulding data...")
    moulding_all = load_skus(MOULDING_JSON, "moulding")
    print(f"  Loaded {len(moulding_all)} moulding SKUs")

    print("Loading flooring data...")
    flooring_all = load_skus(FLOORING_JSON, "flooring")
    print(f"  Loaded {len(flooring_all)} flooring SKUs")

    # Sample
    sundries_sample = random_sample(sundries_all, 50, RANDOM_SEED)
    moulding_sample = random_sample(moulding_all, 50, RANDOM_SEED + 1)
    flooring_sample = random_sample(flooring_all, 50, RANDOM_SEED + 2)

    all_skus = sundries_sample + moulding_sample + flooring_sample
    print(
        f"\nTest set: {len(sundries_sample)} sundries + {len(moulding_sample)} moulding + "
        f"{len(flooring_sample)} flooring = {len(all_skus)} SKUs"
    )

    abc_dist = defaultdict(int)
    dc_dist = defaultdict(int)
    pt_dist = defaultdict(int)
    for s in all_skus:
        abc_dist[s.abc_class] += 1
        dc_dist[s.demand_class] += 1
        pt_dist[s.product_type] += 1
    print(f"  Product Types: {dict(pt_dist)}")
    print(f"  ABC: {dict(abc_dist)}")
    print(f"  Demand: {dict(dc_dist)}")

    # ── PHASE A ──────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("PHASE A: Independent Parameter Sweeps")
    print("=" * 80)

    phase_a_params = build_phase_a_params()
    phase_a_results = {}

    for i, ps in enumerate(phase_a_params):
        sys.stdout.write(f"\r  Testing {i+1}/{len(phase_a_params)}: {ps.name:<25s}")
        sys.stdout.flush()
        summary, details = evaluate_param_set(all_skus, ps)
        phase_a_results[ps.name] = summary

    print("\n\nPhase A Results (sorted by inventory reduction, stockout-OK first):")
    print("-" * 100)

    sorted_results = sorted(
        phase_a_results.values(),
        key=lambda x: (not x["meets_target"], -x["overall_reduction_pct"])
    )

    for s in sorted_results:
        print_summary(s)

    # Extract best individual parameter values from Phase A
    # Find the sweep that maximizes reduction while meeting stockout targets
    phase_a_insights = {}

    for prefix, key, default in [
        ("uplift_A=", "best_uplift_A", 2.0),
        ("uplift_B=", "best_uplift_B", 0.75),
        ("uplift_C=", "best_uplift_C", 0.1),
        ("floor=", "best_floor", 7),
        ("lumpy=", "best_lumpy", 0.15),
        ("cap=", "best_cap", 3),
    ]:
        candidates = [(name, s) for name, s in phase_a_results.items()
                       if name.startswith(prefix) and s["stockout_ok"]]
        if candidates:
            # Best = meets stockout targets with most inventory reduction
            best_name, best = max(candidates, key=lambda x: x[1]["overall_reduction_pct"])
            val_str = best_name.split("=")[1].rstrip("dmo")
            try:
                phase_a_insights[key] = float(val_str)
            except ValueError:
                phase_a_insights[key] = default
        else:
            # No candidate met targets — pick the one with lowest avg A stockout days
            all_cands = [(name, s) for name, s in phase_a_results.items()
                         if name.startswith(prefix)]
            if all_cands:
                best_name, best = min(all_cands, key=lambda x: x[1].get("a_avg_so_days", 999))
                val_str = best_name.split("=")[1].rstrip("dmo")
                try:
                    phase_a_insights[key] = float(val_str)
                except ValueError:
                    phase_a_insights[key] = default
            else:
                phase_a_insights[key] = default

    print(f"\nPhase A Best Individual Values: {phase_a_insights}")

    # ── PHASE B ──────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("PHASE B: Combined Parameter Optimization")
    print("=" * 80)

    phase_b_params = build_phase_b_params(phase_a_insights)
    phase_b_results = {}

    for i, ps in enumerate(phase_b_params):
        sys.stdout.write(f"\r  Testing {i+1}/{len(phase_b_params)}: {ps.name:<25s}")
        sys.stdout.flush()
        summary, details = evaluate_param_set(all_skus, ps)
        phase_b_results[ps.name] = (summary, details)

    print("\n\nPhase B Results (sorted by inventory reduction, stockout-OK first):")
    print("-" * 100)

    sorted_b = sorted(
        phase_b_results.values(),
        key=lambda x: (not x[0]["meets_target"], -x[0]["overall_reduction_pct"])
    )

    for summary, _ in sorted_b:
        print_summary(summary, verbose=True)

    # ── WINNER ───────────────────────────────────────────────
    # Find best that meets all targets
    winners = [(s, d) for s, d in phase_b_results.values() if s["meets_target"]]
    if winners:
        winner_summary, winner_details = max(winners, key=lambda x: x[0]["overall_reduction_pct"])
        print(f"\n{'=' * 80}")
        print(f"WINNER: {winner_summary['name']}")
        print(f"{'=' * 80}")
        print_summary(winner_summary, verbose=True)

        # Find the matching ParamSet
        winner_ps = next(ps for ps in phase_b_params if ps.name == winner_summary["name"])
        print(f"\nRecommended core/config.py values:")
        print(f"  SS_UPLIFT_SCALAR = {{'A': {winner_ps.ss_uplift_A}, 'B': {winner_ps.ss_uplift_B}, 'C': {winner_ps.ss_uplift_C}}}")
        print(f"  SS_FLOOR_DAYS = {winner_ps.ss_floor_days}")
        print(f"  LUMPY_LT_BUFFER_FRAC = {winner_ps.lumpy_buffer_frac}")
        print(f"  SS_CAP_MONTHS = {winner_ps.ss_cap_months}  # 0 = disabled")

        # Save per-SKU details
        csv_path = BASE_DIR / "param_optimizer_results.csv"
        rows = []
        for r in winner_details:
            rows.append({
                "item_number": r.item_number,
                "abc_class": r.abc_class,
                "demand_class": r.demand_class,
                "product_type": r.product_type,
                "current_inv": round(r.current_inv, 1),
                "mean_daily_demand": round(r.mean_daily, 2),
                "safety_stock": round(r.safety_stock, 1),
                "reorder_point": round(r.rop, 1),
                "order_up_to": round(r.order_up_to, 1),
                "nominal_q": round(r.nominal_q, 1),
                "roq_months": round(r.roq_months, 1),
                "stockout_pct": round(r.stockout_pct, 1),
                "avg_stockout_days": round(r.avg_stockout_days, 1),
                "avg_ending_inv": round(r.avg_ending_inv, 1),
                "inv_reduction_pct": round(r.inv_reduction_pct, 1),
            })
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        print(f"\nPer-SKU details saved to: {csv_path}")
    else:
        print("\nNo parameter set met all targets (zero stockouts + 20% reduction).")
        print("Showing best stockout-safe options:")
        safe = [(s, d) for s, d in phase_b_results.values() if s["stockout_ok"]]
        if safe:
            best_safe = max(safe, key=lambda x: x[0]["overall_reduction_pct"])
            print_summary(best_safe[0], verbose=True)
        else:
            print("  No parameter set achieved acceptable stockout rates.")
            print("  Consider loosening stockout targets or running with more replications.")


if __name__ == "__main__":
    main()
