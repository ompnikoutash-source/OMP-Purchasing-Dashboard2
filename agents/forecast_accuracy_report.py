"""
Forecast Accuracy History -- trend report.

Aggregates realized_abs_pct_error (populated by forecast_reconciler.py) into
weekly buckets by product line / forecast method / demand class, so you can
see whether forecast accuracy is trending better or worse over time, and
which method actually wins in hindsight -- not just in each script's own
in-sample backtest.

Outputs, written to agents/reports/ (same convention as the existing
audit_report_*.md / audit_details_*.csv):
    forecast_accuracy_details_<timestamp>.csv   -- one row per week x product x method x demand_class
    forecast_accuracy_report_<timestamp>.md     -- human-readable summary + trend read

Usage:
    .venv\\Scripts\\python.exe -m agents.forecast_accuracy_report
    .venv\\Scripts\\python.exe -m agents.forecast_accuracy_report --weeks 12
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from agents import forecast_history_db as db

REPORTS_DIR = Path(__file__).resolve().parent / "reports"

# Rows with actual_demand below this are near-zero-volume -- trivially easy
# to "predict" as ~0, and letting them dominate a best/worst-method ranking
# is the same class of ratio-blowup problem param_optimizer.py's
# MIN_MATERIALITY_DOLLARS guards against for GMROI. Unit-based here since no
# cost/price join is wired into the tracker.
MIN_MATERIALITY_UNITS = 1.0


def load_reconciled(conn, weeks: int) -> pd.DataFrame:
    sql = """
    SELECT snapshot_date, product_type, item_number, forecast_method,
           scoring_method, used_fallback_average, demand_class,
           abc_class, period_granularity, target_period_start, predicted_demand,
           actual_demand, realized_abs_pct_error
    FROM forecast_accuracy
    WHERE actual_demand IS NOT NULL
    """
    df = pd.read_sql(sql, conn)
    if df.empty:
        return df
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    cutoff = df["snapshot_date"].max() - pd.Timedelta(weeks=weeks)
    df = df[df["snapshot_date"] >= cutoff].copy()
    df["snapshot_week"] = df["snapshot_date"].dt.to_period("W").dt.start_time
    return df


def _wmape(g: pd.DataFrame) -> float:
    """Aggregate |error| / aggregate |actual| -- the same metric
    flooringwebapp.py's own select_best_forecast() uses internally, because
    (unlike per-point APE) it stays well-behaved when actual_demand is near
    zero, which is the norm for LUMPY/sparse series."""
    denom = g["actual_demand"].abs().sum()
    if not denom:
        return float("nan")
    return (g["actual_demand"] - g["predicted_demand"]).abs().sum() / denom


def build_trend_tables(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "by_week_product": pd.DataFrame(), "by_method": pd.DataFrame(),
            "by_scoring_method": pd.DataFrame(), "by_demand_class": pd.DataFrame(),
        }

    by_week_product = (
        df.groupby(["snapshot_week", "product_type"])["realized_abs_pct_error"]
        .agg(mean_ape="mean", median_ape="median", n="count")
        .reset_index()
        .sort_values(["product_type", "snapshot_week"])
    )
    by_method = (
        df.groupby(["product_type", "forecast_method"])["realized_abs_pct_error"]
        .agg(mean_ape="mean", median_ape="median", n="count")
        .reset_index()
        .sort_values(["product_type", "mean_ape"])
    )
    df = df.assign(low_materiality=df["actual_demand"].abs() < MIN_MATERIALITY_UNITS)
    by_scoring_method = (
        df.groupby(["product_type", "scoring_method", "used_fallback_average"])
        .apply(
            lambda g: pd.Series({
                "mean_ape": g["realized_abs_pct_error"].mean(),
                "median_ape": g["realized_abs_pct_error"].median(),
                "wmape": _wmape(g),
                "n": len(g),
                "total_actual_volume": g["actual_demand"].sum(),
                "low_materiality_share": g["low_materiality"].mean(),
            }),
            include_groups=False,
        )
        .reset_index()
        .sort_values(["product_type", "wmape"])
    )
    by_demand_class = (
        df.groupby(["product_type", "demand_class"])["realized_abs_pct_error"]
        .agg(mean_ape="mean", median_ape="median", n="count")
        .reset_index()
        .sort_values(["product_type", "demand_class"])
    )
    return {
        "by_week_product": by_week_product, "by_method": by_method,
        "by_scoring_method": by_scoring_method, "by_demand_class": by_demand_class,
    }


def _trend_direction(by_week_product: pd.DataFrame, product_type: str) -> str:
    """Simple first-half-vs-second-half comparison -- enough to say
    'improving' / 'worsening' / 'flat' without overfitting a trend line to
    what's still a fairly short history."""
    sub = by_week_product[by_week_product["product_type"] == product_type].sort_values("snapshot_week")
    if len(sub) < 4:
        return "not enough weeks of reconciled history yet to call a trend"
    mid = len(sub) // 2
    first_half = sub.iloc[:mid]["mean_ape"].mean()
    second_half = sub.iloc[mid:]["mean_ape"].mean()
    delta = second_half - first_half
    if abs(delta) < 0.02:
        return f"roughly flat ({first_half:.1%} -> {second_half:.1%})"
    direction = "improving" if delta < 0 else "worsening"
    return f"{direction} ({first_half:.1%} -> {second_half:.1%})"


def write_report(tables: dict, weeks: int, timestamp: str) -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    by_week_product = tables["by_week_product"]
    by_method = tables["by_method"]
    by_scoring_method = tables["by_scoring_method"]
    by_demand_class = tables["by_demand_class"]

    details_path = REPORTS_DIR / f"forecast_accuracy_details_{timestamp}.csv"
    combined = pd.concat(
        [t.assign(table=name) for name, t in tables.items() if not t.empty],
        ignore_index=True,
    ) if any(not t.empty for t in tables.values()) else pd.DataFrame()
    combined.to_csv(details_path, index=False)

    lines = [f"# Forecast Accuracy Report ({timestamp})", "", f"Trailing {weeks} weeks of reconciled predictions.", ""]
    if by_week_product.empty:
        lines.append("No reconciled predictions yet -- run forecast_reconciler.py first "
                      "(and give it time: only target periods that have already passed can be reconciled).")
    else:
        lines.append("## Trend by product line (mean realized absolute % error, weekly)")
        lines.append("")
        for product_type in sorted(by_week_product["product_type"].unique()):
            lines.append(f"- **{product_type}**: {_trend_direction(by_week_product, product_type)}")
        lines.append("")
        lines.append("## Best/worst forecast method by realized accuracy (materiality-weighted)")
        lines.append("")
        lines.append(
            f"Ranked by wMAPE (aggregate error / aggregate actual -- the same metric "
            f"flooringwebapp.py's own method selection uses internally), restricted to "
            f"groups where fewer than half the rows are near-zero-volume "
            f"(actual_demand < {MIN_MATERIALITY_UNITS} units) so a method that's merely "
            f"easy on trivial demand can't win the ranking. `scoring_method` is the method "
            f"that actually won cross-validated scoring; `used_fallback_average=True` means "
            f"its own forecast was discarded in favor of a historical-average fallback, so "
            f"the error shown reflects that fallback, not the method's own forecast."
        )
        lines.append("")
        material = by_scoring_method[by_scoring_method["low_materiality_share"] < 0.5]
        for product_type in sorted(by_scoring_method["product_type"].unique()):
            sub = material[material["product_type"] == product_type]
            if sub.empty:
                lines.append(f"- **{product_type}**: no method group clears the materiality bar")
                continue
            best = sub.iloc[0]
            worst = sub.iloc[-1]
            lines.append(
                f"- **{product_type}**: best = `{best['scoring_method']}`"
                f"{' (fallback average used)' if best['used_fallback_average'] else ''} "
                f"({best['wmape']:.1%} wMAPE, n={best['n']}, volume={best['total_actual_volume']:.0f}), "
                f"worst = `{worst['scoring_method']}`"
                f"{' (fallback average used)' if worst['used_fallback_average'] else ''} "
                f"({worst['wmape']:.1%} wMAPE, n={worst['n']}, volume={worst['total_actual_volume']:.0f})"
            )
        lines.append("")
        lines.append("### Naive/unweighted best-worst by forecast_method (for contrast, not recommended for decisions)")
        lines.append("")
        for product_type in sorted(by_method["product_type"].unique()):
            sub = by_method[by_method["product_type"] == product_type]
            best = sub.iloc[0]
            worst = sub.iloc[-1]
            lines.append(
                f"- **{product_type}**: best = `{best['forecast_method']}` "
                f"({best['mean_ape']:.1%} mean APE, n={best['n']}), "
                f"worst = `{worst['forecast_method']}` ({worst['mean_ape']:.1%} mean APE, n={worst['n']})"
            )
        lines.append("")
        lines.append("## By demand class")
        lines.append("")
        for _, row in by_demand_class.iterrows():
            lines.append(f"- {row['product_type']} / {row['demand_class']}: {row['mean_ape']:.1%} mean APE (n={row['n']})")
        lines.append("")
        lines.append(f"Full detail: `{details_path.name}`")

    report_path = REPORTS_DIR / f"forecast_accuracy_report_{timestamp}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {report_path}")
    print(f"Wrote {details_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a forecast accuracy trend report from forecast_history.db.")
    parser.add_argument("--weeks", type=int, default=26, help="Trailing weeks of reconciled history to include")
    args = parser.parse_args()

    conn = db.connect()
    df = load_reconciled(conn, args.weeks)
    conn.close()

    print(f"Loaded {len(df)} reconciled predictions" + (f" over the trailing {args.weeks} weeks" if len(df) else ""))
    tables = build_trend_tables(df)
    write_report(tables, args.weeks, datetime.now().strftime("%Y%m%d_%H%M%S"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
