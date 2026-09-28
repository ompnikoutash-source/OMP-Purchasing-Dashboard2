"""
demand_health_scoring.py

Scores items on two SEPARATE axes instead of one blended number:

    1. Demand Health  -- is this item's demand (units, revenue, order
       count, distinct customers) trending up or down relative to its
       own product family peers, using outlier-resistant statistics?
       Low percentile = concerning decline. High = healthy/growing.

    2. Materiality     -- how much does this item matter financially
       right now (dollars tied up in on-hand inventory)? Scored
       company-wide, not family-relative, because $200k tied up
       matters the same regardless of what family it's in.

Why two axes and not one score: collapsing "declining" and "big"
into a single number forces an arbitrary, invisible trade-off between
them. Keeping them separate lets you actually see WHY an item landed
where it did, and lets different quadrants map to different actions
(discontinue quietly vs. fund a real campaign vs. leave alone).

METHOD (per your stated preferences):
  - Percentile rank, not z-score, for combining differently-scaled
    metrics -- robust to the single-large-outlier problem inherent
    in project-based B2B demand.
  - Trend is computed as a linear regression slope across the
    trailing 12 monthly buckets (not a two-point 90-vs-90 delta),
    which is far less sensitive to which exact window you draw.
  - Coefficient of variation (CV = stdev/mean) is computed per metric
    per item, as a lumpiness/reliability flag -- NOT used to score,
    just reported so you can see which "declines" are actually just
    noisy/project-driven data you should trust less.
  - The four percentile-ranked trend metrics are blended with fixed
    business weights: revenue 40%, units 35%, orders 20%, and distinct
    customers 5%. These weights can be changed in DEMAND_METRIC_WEIGHTS
    when the score needs to reflect a different business priority.
  - Items with too few months of actual sales activity (new items,
    or very intermittent ones) are EXCLUDED from percentile ranking
    and scoring, and reported separately -- comparing a 2-month-old item's
    "trend" to mature items' trends is not a fair fight, per your
    point about cold-start items needing different treatment.

INPUTS:
  --panel     CSV/XLSX export of Extract A (MonthlyPanel) from
              demand_health_extract.sql: ITEM_NUMBER, YR, MO, UNITS,
              REVENUE, ORDERS, DISTINCT_CUSTOMERS
  --snapshot  CSV/XLSX export of Extract B (ItemSnapshot):
              ITEM_NUMBER, FAMILY_CODE, AVAIL_QTY, AVG_COST,
              DOLLARS_TIED_UP
  --min-months   Minimum months with nonzero sales activity required
                 to be scored (default 6 of the trailing 12).
  --min-family-size  Minimum items in a family for percentile ranks
                 within that family to be considered reliable
                 (default 5; smaller families get ranked against the
                 whole item base instead, flagged in output).

OUTPUT:
  --out xlsx workbook with three tabs:
    "Scored"        - one row per item scored: the 4 raw trend slopes,
                      4 CVs, 4 within-family percentile ranks, the
                      business-weighted Demand Health percentile, the
                      Materiality percentile, and a quadrant label.
    "PCA Weights"   - the demand-health weight used for each of the
                      4 trend metrics.
    "Insufficient History" - items excluded from scoring, with a
                      reason, so nothing silently disappears.
  --chart PNG scatter: Materiality (x) vs Demand Health (y), colored
                      by quadrant.

USAGE:
    python demand_health_scoring.py --panel panel.csv --snapshot snapshot.csv --out scored.xlsx --chart quadrant.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

METRICS = ["UNITS", "REVENUE", "ORDERS", "DISTINCT_CUSTOMERS"]

# Fixed business weights for the trend component of Demand Health.
# Stored by metric name, then emitted in METRICS order wherever a numpy
# vector is needed for the percentile blend.
DEMAND_METRIC_WEIGHTS = {
    "UNITS": 0.35,
    "REVENUE": 0.40,
    "ORDERS": 0.20,
    "DISTINCT_CUSTOMERS": 0.05,
}

# How much of Demand Health is "which direction is it going" (trend) vs
# "how much does it sell at all" (level). Trend alone was the original
# design and it had a blind spot: an item that has ALREADY collapsed to
# near-zero has a flat slope, and a flat slope ranks mid-pack -- so
# chronically dead items scored ~30-55 instead of near the bottom, while
# items in the middle of a fresh decline scored ~3. Blending in an
# absolute-level percentile fixes that: you cannot look healthy while
# selling almost nothing relative to your family peers.
TREND_WEIGHT = 0.65
LEVEL_WEIGHT = 0.35

# An item with this many months of stock on hand (at its trailing-12-month
# run rate) AND at least this many dollars tied up is an inventory-coverage
# problem regardless of which way its trend is pointing -- it gets its own
# quadrant so it can't hide behind a flat or even rising trend line.
#
# NOTE: this flat 24-month rule is now only the FALLBACK, used for items the
# Purchasing Dashboard doesn't plan. Where planning data exists we use that
# item's own lead-time-aware order-up-to level instead (see
# OVERSTOCK_COVER_RATIO), which is strictly better: 24 months means something
# very different for a 60-day-lead-time domestic item than for a 120-day
# import.
OVERSTOCK_MOS_THRESHOLD = 24.0
OVERSTOCK_MIN_DOLLARS = 10_000.0

# Plan-based overstock trigger: stock on hand versus the order-up-to level
# the purchasing model itself computed (lead-time demand + safety stock +
# cycle stock). Simply being *above* order-up-to is far too common to be
# useful -- roughly half of all planned items are, because the implied target
# is only ~4 months of cover -- so the trigger is a multiple of it. 2x means
# "you hold more than twice what your own replenishment model would ever
# order up to."
OVERSTOCK_COVER_RATIO = 2.0

# Sentinel used for MONTHS_OF_SUPPLY when an item has stock but no positive
# trailing-12-month sales (i.e. coverage is effectively infinite). A real
# number keeps sorting/JSON sane; anything at this value means "no sales".
MOS_INFINITE = 999.0

QUADRANT_PRIMARY = "Primary sale candidate (declining + significant $)"
QUADRANT_LOW_PRIORITY = "Low priority (declining but small $ impact)"
QUADRANT_CORE = "Core business - monitor only (healthy + significant $)"
QUADRANT_LEAVE_ALONE = "Leave alone (healthy + small $ impact)"
QUADRANT_OVERSTOCK = "Overstocked - clear inventory (excess coverage)"
QUADRANT_UNDERSTOCK = "Understocked - below reorder point (stockout risk)"


def _read_table(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p)
    return pd.read_csv(p)


def _trend_and_cv(months: pd.DataFrame, metric: str) -> tuple[float, float, float, int, float]:
    """
    Linear-regression slope of `metric` across the item's trailing 12
    monthly buckets (already sorted chronologically), plus coefficient
    of variation, plus how many of those months had nonzero activity
    at all (for the min-months data-sufficiency check).
    Missing months are treated as zero -- a month with no sales is a
    real data point (zero demand that month), not a missing one.

    Returns (slope, rel_slope, cv, months_active, total).

    rel_slope is the slope expressed as a fraction of the item's own
    average monthly level (so -0.10 means "shedding about 10% of its own
    typical month, every month"). This is what actually gets percentile-
    ranked, because the RAW slope is not comparable between items of
    different size: an item averaging 1.5 orders/month that drifts from 3
    orders to 1 has a raw slope near -0.06, which is indistinguishable
    from a healthy 20-orders/month item that lost a fraction of an order
    -- both look "flat". Dividing by the item's own mean separates
    "flat because stable" from "flat because already dead", which was the
    single biggest scoring blind spot in the original version.
    """
    y = months[metric].to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    months_active = int((y > 0).sum())
    total = float(y.sum())

    if len(y) < 2 or np.all(y == y[0]):
        slope = 0.0
    else:
        # np.polyfit degree-1: slope, intercept
        slope, _ = np.polyfit(x, y, 1)
    slope = float(slope)

    mean = y.mean()
    if mean > 0:
        cv = float(y.std(ddof=0) / mean)
        rel_slope = slope / mean
    else:
        # No positive average demand at all over the window (nothing sold,
        # or net returns exceeded sales). There is no meaningful ratio to
        # take, and this is unambiguously the worst case -- pin it to the
        # bottom rather than letting it rank as "flat".
        cv = np.nan
        rel_slope = -1.0

    return slope, float(rel_slope), cv, months_active, total


def build_trend_table(panel: pd.DataFrame, min_months: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Collapses the long monthly panel into one row per item: a trend
    slope + CV per metric, plus a MONTHS_ACTIVE data-sufficiency count.
    Returns (scored_candidates, insufficient_history).
    """
    panel = panel.sort_values(["ITEM_NUMBER", "YR", "MO"])
    rows = []
    for item, grp in panel.groupby("ITEM_NUMBER", sort=False):
        rec = {"ITEM_NUMBER": item}
        min_active_seen = None
        for metric in METRICS:
            slope, rel_slope, cv, months_active, total = _trend_and_cv(grp, metric)
            rec[f"{metric}_SLOPE"] = slope
            rec[f"{metric}_REL_SLOPE"] = rel_slope
            rec[f"{metric}_CV"] = cv
            rec[f"{metric}_MONTHS_ACTIVE"] = months_active
            rec[f"{metric}_12MO_TOTAL"] = total
            min_active_seen = (
                months_active if min_active_seen is None else min(min_active_seen, months_active)
            )
        rec["MIN_MONTHS_ACTIVE"] = min_active_seen
        rows.append(rec)

    trend_df = pd.DataFrame(rows)
    sufficient = trend_df[trend_df["MIN_MONTHS_ACTIVE"] >= min_months].copy()
    insufficient = trend_df[trend_df["MIN_MONTHS_ACTIVE"] < min_months].copy()
    insufficient["REASON"] = (
        "Fewer than "
        + str(min_months)
        + " of the trailing 12 months had activity in at least one metric "
        "-- likely a new or very intermittently-ordered item. Excluded from "
        "peer comparison; evaluate against launch/ramp expectations instead."
    )
    return sufficient, insufficient


def percentile_rank_within_family(
    df: pd.DataFrame, family_col: str, value_col: str, min_family_size: int
) -> pd.Series:
    """
    Percentile rank (0-100, higher = higher raw value) of value_col,
    computed within each family. Families smaller than min_family_size
    fall back to a company-wide percentile rank instead, since a
    "percentile" within 2 items isn't a meaningful comparison.
    """
    family_sizes = df.groupby(family_col)[value_col].transform("size")
    within_family = df.groupby(family_col)[value_col].rank(pct=True) * 100
    company_wide = df[value_col].rank(pct=True) * 100
    return within_family.where(family_sizes >= min_family_size, company_wide)


def demand_metric_weights() -> np.ndarray:
    """
    Fixed weights for the 4 within-family percentile-ranked trend slopes.
    Higher weighted scores mean healthier/growing demand relative to peers,
    matching the direction of the input percentiles.
    """
    weights = np.array([DEMAND_METRIC_WEIGHTS[metric] for metric in METRICS], dtype=float)
    if not np.isclose(weights.sum(), 1.0):
        raise ValueError(f"DEMAND_METRIC_WEIGHTS must sum to 1.0, got {weights.sum():.4f}")
    return weights


def run_pca_weights(pctl_matrix: pd.DataFrame) -> tuple[np.ndarray, float]:
    """Backward-compatible wrapper for older callers."""
    return demand_metric_weights(), 0.0


def _attach_plan(merged: pd.DataFrame, plan: pd.DataFrame | None) -> pd.DataFrame:
    """
    Joins the Purchasing Dashboard's planning columns on and derives the
    coverage findings from them, falling back to the history-only rule for
    any item the plan doesn't cover.

    Three deliberate choices worth knowing about:

    * COVER_RATIO and FORWARD_MONTHS_OF_SUPPLY are computed entirely from
      plan-side numbers (plan SF over plan SF, plan SF over plan demand), so
      they stay valid even for the handful of items where the plan's SF and
      our billing-UOM quantities disagree. Only the dollar conversion is
      UOM-sensitive, and that is handled by working in FRACTIONS.

    * EXCESS_DOLLARS is DOLLARS_TIED_UP x (excess SF / available SF). Going
      through a unitless fraction rather than multiplying excess SF by a unit
      cost means the result is automatically both UOM-independent and capped
      at the money actually on the floor -- the earlier naive version summed
      to more than the total inventory value, which is impossible.

    * Excess is measured against AVAILABLE stock, not inventory position.
      Position includes inbound POs, which is right for a buying decision but
      wrong for "how much money is sitting in my warehouse". Inbound is
      carried separately as PLAN_ON_PO_SF so an item that is already drowning
      AND has another container on the water is visible as such.
    """
    n = len(merged)
    if plan is None or not len(plan):
        merged["HAS_PLAN"] = 0
    else:
        cols = [c for c in plan.columns if c.startswith("PLAN_") or c == "ITEM_NUMBER"]
        merged = merged.merge(plan[cols], on="ITEM_NUMBER", how="left")
        merged["HAS_PLAN"] = merged.get(
            "PLAN_ORDER_UP_TO", pd.Series(np.nan, index=merged.index)
        ).notna().astype(int)

    # Guarantee every downstream column exists even in history-only mode.
    for col in (
        "PLAN_AVAILABLE_SF", "PLAN_ON_PO_SF", "PLAN_INV_POSITION", "PLAN_ORDER_UP_TO",
        "PLAN_REORDER_POINT", "PLAN_SAFETY_STOCK", "PLAN_AVG_MONTHLY_DEMAND",
        "PLAN_LEAD_TIME_DAYS", "PLAN_FORECAST_WMAPE", "PLAN_ADI", "PLAN_CV2",
    ):
        if col not in merged.columns:
            merged[col] = np.nan
    for col in ("PLAN_DEMAND_CLASS", "PLAN_FORECAST_CONFIDENCE", "PLAN_ABC",
                "PLAN_FIRST_REORDER", "PLAN_FORECAST_METHOD", "PLAN_SKU"):
        if col not in merged.columns:
            merged[col] = None
    if "PLAN_SHARED_ROW" not in merged.columns:
        merged["PLAN_SHARED_ROW"] = 0
    merged["PLAN_SHARED_ROW"] = merged["PLAN_SHARED_ROW"].fillna(0).astype(int)
    merged["PLAN_FORECAST_CONFIDENCE"] = merged["PLAN_FORECAST_CONFIDENCE"].fillna("unknown")

    has_plan = merged["HAS_PLAN"] == 1
    avail_sf = pd.to_numeric(merged["PLAN_AVAILABLE_SF"], errors="coerce")
    outl = pd.to_numeric(merged["PLAN_ORDER_UP_TO"], errors="coerce")
    rop = pd.to_numeric(merged["PLAN_REORDER_POINT"], errors="coerce")
    fc_rate = pd.to_numeric(merged["PLAN_AVG_MONTHLY_DEMAND"], errors="coerce")

    # How many times over the plan's own maximum stocking level are we?
    merged["COVER_RATIO"] = np.where(
        has_plan & (outl > 0), avail_sf / outl.replace(0, np.nan), np.nan
    )

    # Forward-looking coverage: the same question as MONTHS_OF_SUPPLY but
    # answered with FORECAST demand instead of trailing actuals. For a
    # declining item this is the harsher (and more honest) of the two.
    fwd = np.where(has_plan & (fc_rate > 0), avail_sf / fc_rate.replace(0, np.nan), np.nan)
    merged["FORWARD_MONTHS_OF_SUPPLY"] = np.minimum(
        np.where(np.isnan(fwd), np.nan, fwd), MOS_INFINITE
    )
    merged["TRAILING_MONTHS_OF_SUPPLY"] = merged["MONTHS_OF_SUPPLY"]
    # Positive => the forecast says this is worse than history implies.
    merged["COVERAGE_DISAGREEMENT"] = (
        merged["FORWARD_MONTHS_OF_SUPPLY"] - merged["TRAILING_MONTHS_OF_SUPPLY"]
    )

    excess_sf = (avail_sf - outl).clip(lower=0)
    merged["EXCESS_SF"] = np.where(has_plan, excess_sf, np.nan)
    excess_frac = np.where(
        has_plan & (avail_sf > 0), (excess_sf / avail_sf.replace(0, np.nan)).clip(0, 1), np.nan
    )
    merged["EXCESS_FRACTION"] = excess_frac

    dollars = merged["DOLLARS_TIED_UP"].fillna(0)
    plan_excess_dollars = dollars * pd.Series(excess_frac, index=merged.index).fillna(0)

    # History-only fallback for unplanned items: dollars above what a
    # 6-month cover would justify, derived from trailing run rate.
    trail_mos = merged["MONTHS_OF_SUPPLY"]
    hist_frac = ((trail_mos - 6.0).clip(lower=0) / trail_mos.replace(0, np.nan)).clip(0, 1)
    hist_excess_dollars = dollars * hist_frac.fillna(0)

    merged["EXCESS_DOLLARS"] = np.where(has_plan, plan_excess_dollars, hist_excess_dollars)
    merged["EXCESS_BASIS"] = np.where(
        has_plan, "vs plan order-up-to", "vs 6-month cover (no plan data)"
    )

    merged["NO_REORDER_EXPECTED"] = (
        has_plan & merged["PLAN_FIRST_REORDER"].isna()
    ).astype(int)

    # ---- the two coverage verdicts ----
    plan_over = has_plan & (merged["COVER_RATIO"] >= OVERSTOCK_COVER_RATIO) & (
        merged["EXCESS_DOLLARS"] >= OVERSTOCK_MIN_DOLLARS
    )
    hist_over = (~has_plan) & (trail_mos >= OVERSTOCK_MOS_THRESHOLD) & (
        dollars >= OVERSTOCK_MIN_DOLLARS
    )
    merged["OVERSTOCKED"] = (plan_over | hist_over).fillna(False).astype(int)
    merged["OVERSTOCK_BASIS"] = np.where(
        plan_over.fillna(False), "plan order-up-to",
        np.where(hist_over.fillna(False), "24-month history rule", ""),
    )

    # Below the reorder point is the opposite failure and only the plan can
    # tell us about it -- there is no way to derive a reorder point from
    # sales history alone. Never fires at the same time as OVERSTOCKED.
    merged["UNDERSTOCKED"] = (
        has_plan & (avail_sf < rop) & (rop > 0) & (merged["OVERSTOCKED"] == 0)
    ).fillna(False).astype(int)

    assert len(merged) == n, f"plan join changed row count {n} -> {len(merged)}"
    return merged


def score_items(
    panel: pd.DataFrame,
    snapshot: pd.DataFrame,
    min_months: int,
    min_family_size: int,
    plan: pd.DataFrame | None = None,
):
    sufficient, insufficient = build_trend_table(panel, min_months)

    merged = sufficient.merge(snapshot, on="ITEM_NUMBER", how="left")
    merged["FAMILY_CODE"] = merged["FAMILY_CODE"].fillna("UNKNOWN")

    # Exclude items with no PO receipt in the trailing 24 months (see
    # HAD_PO_RECEIPT_24MO in demand_health_extract.sql / ItemSnapshot).
    # An item nobody is restocking is a purchasing/discontinuation
    # decision already, not a "should this go on sale" decision -- it
    # doesn't belong mixed in with items that are actively being
    # replenished. Reported separately, same pattern as the
    # insufficient-history bucket, so nothing silently disappears.
    if "HAD_PO_RECEIPT_24MO" in merged.columns:
        no_po_mask = merged["HAD_PO_RECEIPT_24MO"].fillna(0).astype(int) == 0
        no_recent_po = merged[no_po_mask].copy()
        no_recent_po["REASON"] = (
            "No purchase-order receipt in the trailing 24 months -- likely "
            "discontinued or already being phased out rather than an active "
            "sale candidate. Excluded from demand-health scoring; review "
            "purchasing/discontinuation status directly instead."
        )
        merged = merged[~no_po_mask].copy()
    else:
        no_recent_po = merged.iloc[0:0].copy()
        no_recent_po["REASON"] = pd.Series(dtype=str)

    # ---- trend component: percentile-rank the RELATIVE slopes ----
    # (relative, not raw -- see _trend_and_cv's docstring for why)
    pctl_cols = []
    for metric in METRICS:
        col = f"{metric}_TREND_PCTL"
        merged[col] = percentile_rank_within_family(
            merged, "FAMILY_CODE", f"{metric}_REL_SLOPE", min_family_size
        )
        pctl_cols.append(col)

    weights = demand_metric_weights()
    merged["DEMAND_TREND_PCTL"] = merged[pctl_cols].to_numpy() @ weights

    # ---- level component: how much does it sell at all, vs its peers ----
    # Units and revenue are averaged so neither a UOM quirk nor a pricing
    # change alone decides it. Orders/customers are deliberately excluded
    # here -- they're small integers and already feed the trend side.
    level_cols = []
    for metric in ("UNITS", "REVENUE"):
        col = f"{metric}_LEVEL_PCTL"
        merged[col] = percentile_rank_within_family(
            merged, "FAMILY_CODE", f"{metric}_12MO_TOTAL", min_family_size
        )
        level_cols.append(col)
    merged["DEMAND_LEVEL_PCTL"] = merged[level_cols].mean(axis=1)

    # ---- blend, then re-rank so the number is a true percentile again ----
    # (a weighted average of percentiles is not itself uniformly
    # distributed; re-ranking makes "30" mean "30% of scored items are
    # worse than this", which is how anyone reading it will interpret it)
    merged["DEMAND_HEALTH_SCORE"] = (
        TREND_WEIGHT * merged["DEMAND_TREND_PCTL"] + LEVEL_WEIGHT * merged["DEMAND_LEVEL_PCTL"]
    )
    merged["DEMAND_HEALTH_PCTL"] = merged["DEMAND_HEALTH_SCORE"].rank(pct=True) * 100

    merged["MATERIALITY_PCTL"] = merged["DOLLARS_TIED_UP"].fillna(0).rank(pct=True) * 100

    # ---- inventory coverage: months of supply at the trailing run rate ----
    run_rate = merged["UNITS_12MO_TOTAL"] / 12.0
    merged["MONTHLY_RUN_RATE"] = run_rate
    avail = merged["AVAIL_QTY"].fillna(0)
    mos = pd.Series(np.nan, index=merged.index, dtype=float)
    mos[avail <= 0] = 0.0
    has_stock_no_sales = (avail > 0) & (run_rate <= 0)
    mos[has_stock_no_sales] = MOS_INFINITE
    normal = (avail > 0) & (run_rate > 0)
    mos[normal] = (avail[normal] / run_rate[normal]).clip(upper=MOS_INFINITE)
    merged["MONTHS_OF_SUPPLY"] = mos

    # ---- fold in the Purchasing Dashboard's planning numbers ----
    merged = _attach_plan(merged, plan)

    health_median = merged["DEMAND_HEALTH_PCTL"].median()
    materiality_median = merged["MATERIALITY_PCTL"].median()

    def quadrant(row) -> str:
        # Inventory-coverage findings come FIRST, because they override the
        # trend read entirely: whether demand is ticking up or down this
        # quarter is secondary to owning four years of the stuff (or being
        # about to run out of a good seller).
        if row["OVERSTOCKED"] == 1:
            return QUADRANT_OVERSTOCK
        if row["UNDERSTOCKED"] == 1:
            return QUADRANT_UNDERSTOCK
        low_health = row["DEMAND_HEALTH_PCTL"] < health_median
        high_materiality = row["MATERIALITY_PCTL"] >= materiality_median
        if low_health and high_materiality:
            return QUADRANT_PRIMARY
        if low_health and not high_materiality:
            return QUADRANT_LOW_PRIORITY
        if not low_health and high_materiality:
            return QUADRANT_CORE
        return QUADRANT_LEAVE_ALONE

    merged["QUADRANT"] = merged.apply(quadrant, axis=1)

    weights_df = pd.DataFrame(
        {
            "Metric": METRICS,
            "Demand-health weight": weights,
        }
    )

    return merged, weights_df, insufficient, no_recent_po


def make_chart(scored: pd.DataFrame, out_path: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Same validated palette + marker shapes as the HTML dashboard, so the
    # PNG and the interactive chart can't disagree about what a color means.
    # Shapes carry identity alongside hue (red/green is the colorblindness
    # confusion axis, so hue alone is never the only cue).
    quadrant_colors = {
        QUADRANT_PRIMARY: "#d03b3b",
        QUADRANT_LOW_PRIORITY: "#e38989",
        QUADRANT_CORE: "#0ca30c",
        QUADRANT_LEAVE_ALONE: "#75cb75",
        QUADRANT_OVERSTOCK: "#4a3aa7",
    }
    quadrant_markers = {
        QUADRANT_PRIMARY: "^",
        QUADRANT_LOW_PRIORITY: "D",
        QUADRANT_CORE: "o",
        QUADRANT_LEAVE_ALONE: "s",
        QUADRANT_OVERSTOCK: "*",
    }

    fig, ax = plt.subplots(figsize=(9, 7))
    for label, group in scored.groupby("QUADRANT"):
        ax.scatter(
            group["MATERIALITY_PCTL"],
            group["DEMAND_HEALTH_PCTL"],
            s=26 if label == QUADRANT_OVERSTOCK else 18,
            alpha=0.6,
            label=label,
            color=quadrant_colors.get(label, "#333333"),
            marker=quadrant_markers.get(label, "o"),
        )

    ax.axvline(scored["MATERIALITY_PCTL"].median(), color="gray", linestyle="--", linewidth=1)
    ax.axhline(scored["DEMAND_HEALTH_PCTL"].median(), color="gray", linestyle="--", linewidth=1)
    ax.set_xlabel("Materiality percentile ($ tied up, company-wide)")
    ax.set_ylabel("Demand Health percentile (within family)")
    ax.set_title("Demand Health vs. Materiality")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=1, fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", required=True, help="Path to MonthlyPanel export (csv/xlsx)")
    ap.add_argument("--snapshot", required=True, help="Path to ItemSnapshot export (csv/xlsx)")
    ap.add_argument("--out", default="demand_health_scored.xlsx")
    ap.add_argument("--chart", default="demand_health_quadrant.png")
    ap.add_argument("--min-months", type=int, default=6)
    ap.add_argument("--min-family-size", type=int, default=5)
    ap.add_argument(
        "--plan",
        default="inventory_plan_all.xlsx",
        help="Purchasing Dashboard planning workbook (Inventory_Metrics + "
        "Monthly_Projections sheets). Supplies lead time, safety stock, "
        "reorder point, order-up-to level and forecast demand. If it's "
        "missing, scoring falls back to sales history only.",
    )
    args = ap.parse_args()

    panel = _read_table(args.panel)
    snapshot = _read_table(args.snapshot)

    missing_panel_cols = {"ITEM_NUMBER", "YR", "MO", *METRICS} - set(panel.columns)
    if missing_panel_cols:
        sys.exit(f"Panel file is missing columns: {sorted(missing_panel_cols)}")
    missing_snap_cols = {"ITEM_NUMBER", "FAMILY_CODE", "DOLLARS_TIED_UP"} - set(snapshot.columns)
    if missing_snap_cols:
        sys.exit(f"Snapshot file is missing columns: {sorted(missing_snap_cols)}")

    try:
        from purchasing_plan import load_plan

        plan_df, _projections, plan_meta = load_plan(args.plan)
        if plan_meta["available"]:
            print(
                f"Planning data: {plan_meta['n_plan_skus']} plan SKUs from "
                f"{args.plan} (updated {plan_meta['modified']})"
            )
        else:
            print(f"Planning data: {plan_meta['note']}")
    except ImportError:
        plan_df = None
        print("Planning data: purchasing_plan.py not found -- history-only mode.")

    scored, weights_df, insufficient, no_recent_po = score_items(
        panel, snapshot, args.min_months, args.min_family_size, plan=plan_df
    )

    with pd.ExcelWriter(args.out, engine="openpyxl") as writer:
        scored.sort_values("DEMAND_HEALTH_PCTL").to_excel(writer, sheet_name="Scored", index=False)
        weights_df.to_excel(writer, sheet_name="PCA Weights", index=False)
        insufficient.to_excel(writer, sheet_name="Insufficient History", index=False)
        no_recent_po.to_excel(writer, sheet_name="No Recent PO", index=False)
        panel.to_excel(writer, sheet_name="Monthly Panel", index=False)

    make_chart(scored, args.chart)

    print(
        f"Scored {len(scored):,} items ({len(insufficient):,} excluded for insufficient history, "
        f"{len(no_recent_po):,} excluded for no PO receipt in 24 months)."
    )
    print("\nDemand-health metric weights:")
    print(weights_df.to_string(index=False))
    print(f"\nWrote {args.out} and {args.chart}")


if __name__ == "__main__":
    main()
