"""
Regression Triage (Layer 1). Ported from the ai-loop sandbox 2026-09-02,
originally written 2026-08-31.

The "what's worth investigating and how urgent" half of a two-layer
root-cause process: cheap, deterministic, no simulation. Reads what
forecast_audit_agent.py and forecast_accuracy_report.py already produce and
merges them into one ranked investigation queue. Layer 2 (actually
investigating a queued item -- forming a hypothesis, testing it against
real data, proposing a scoped fix) is a manual/agent-driven process, not
automated here; see the ai-loop sandbox's root_cause_playbook.md for that
process if picking up a queued item.

Two input streams:
  1. forecast_audit_agent's latest audit_findings_*.json -- point-in-time,
     rule-based, per-SKU issues with an assigned severity.
  2. forecast_accuracy_report's latest forecast_accuracy_details_*.csv,
     "by_week_product" rows specifically -- a real week-by-week accuracy
     series per product_type. This script does the one thing that report
     doesn't: compare the most recent weeks against the earlier baseline and
     flag a product_type as regressing if median_ape got meaningfully worse.
     (median_ape, not mean_ape -- mean_ape can blow up from near-zero-actual
     items even after the report's own materiality filter; see that script's
     MIN_MATERIALITY_UNITS comment.)

Output: one ranked queue, most urgent first, written to
agents/reports/triage_queue_<timestamp>.json and printed as a table. Each
item includes a "layer2_prompt" -- a self-contained investigation brief
ready to hand to a Layer 2 detective agent (see root_cause_playbook.md),
so triage and investigation are two separate, composable steps.

Usage:
    .venv\\Scripts\\python.exe -m agents.regression_triage --base-dir .
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

REPORTS_DIR = Path(__file__).resolve().parent / "reports"

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# How much worse (relative) median_ape has to get, recent vs baseline weeks,
# before it's worth a human/agent looking at it. Below MILD it's noise.
REGRESSION_SEVERE_PCT = 40.0   # -> "high"
REGRESSION_MILD_PCT = 15.0    # -> "medium"
RECENT_WEEKS = 3


def _latest(pattern: str) -> Optional[Path]:
    matches = sorted(REPORTS_DIR.glob(pattern))
    return matches[-1] if matches else None


GARTMAN_CSV_PATH = REPORTS_DIR.parent.parent / "gartman_item_master_full.csv"


def _load_active_item_set() -> Optional[set]:
    """SANDBOX-ONLY (2026-09-02): forecast_audit_agent scans the raw,
    unfiltered flooringwebappJSON/sundrieswebappJSON snapshots -- it has no
    concept of "actively purchased" the way param_optimizer.py's datasets
    now do (see [[feedback_flooring_scope_rules]]: rolling 24mo PO-receipt,
    Drop Status blank, not a SPECIAL ORDER catch-all code). Confirmed this
    matters, not hypothetical: cross-checking a fresh triage run found
    RH112SRSNB (a "high_backorder_ratio"/"negative_inventory_position"
    finding) is a dead SKU with no PO since 2023-06-13 -- pure noise a human
    would have to manually recognize and discard every single run. Returns
    None (meaning "don't filter") if the Gartman pull isn't available,
    rather than silently passing everything through as if it were checked."""
    if not GARTMAN_CSV_PATH.exists():
        return None
    gart = pd.read_csv(GARTMAN_CSV_PATH, low_memory=False)
    gart["Last FOB Date"] = pd.to_datetime(gart["Last FOB Date"], errors="coerce")
    cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(months=24)
    is_dropped = gart["Drop Status"].fillna("").str.strip() != ""
    is_special_order = gart["Description"].str.contains("SPECIAL ORDER", case=False, na=False)
    active = (gart["Last FOB Date"] >= cutoff) & (~is_dropped) & (~is_special_order)
    return set(gart.loc[active, "Item Number"])


def load_audit_findings() -> List[Dict[str, Any]]:
    path = _latest("audit_findings_*.json")
    if path is None:
        return []
    findings = json.loads(path.read_text(encoding="utf-8"))
    active_items = _load_active_item_set()
    n_excluded_inactive = 0
    queue_items = []
    for f in findings:
        sku = f.get("sku")
        if active_items is not None and sku is not None and sku not in active_items:
            n_excluded_inactive += 1
            continue
        queue_items.append({
            "source": "audit_finding",
            "severity": f.get("severity", "medium"),
            "title": f"[{f.get('dataset')}] {f.get('sku')}: {f.get('issue_id')}",
            "description": f.get("issue_description", ""),
            "recommendation": f.get("recommendation", ""),
            "evidence": f.get("evidence", {}),
            "likely_code_locations": f.get("likely_code_locations", []),
            "layer2_prompt": (
                f"Investigate audit finding '{f.get('issue_id')}' on SKU {f.get('sku')} "
                f"({f.get('dataset')}, demand_class={f.get('demand_class')}, abc_class={f.get('abc_class')}, "
                f"forecast_method={f.get('method')}). Issue: {f.get('issue_description', '')} "
                f"Evidence: {json.dumps(f.get('evidence', {}))}. "
                f"Likely code: {', '.join(f.get('likely_code_locations', [])) or 'unknown'}. "
                f"Form a hypothesis for the mechanism, test it against real data (the ai-loop sandbox "
                f"is the safe place to do this -- never modify live behavior directly), and if confirmed, "
                f"propose a scoped, off-by-default fix: a module-level flag, gated as narrowly as the "
                f"evidence supports, tested before/after, never applied by default without separate review."
            ),
            "_source_path": str(path),
        })
    if active_items is None:
        print("  (Gartman pull not found -- active-population filter skipped, findings NOT cross-checked)")
    elif n_excluded_inactive:
        print(f"  Excluded {n_excluded_inactive} finding(s) on inactive/dead SKUs (not in the active-purchasing population)")
    return queue_items


def load_accuracy_regressions() -> List[Dict[str, Any]]:
    path = _latest("forecast_accuracy_details_*.csv")
    if path is None:
        return []
    df = pd.read_csv(path)
    wp = df[df["table"] == "by_week_product"].dropna(subset=["median_ape"]).copy()
    if wp.empty:
        return []
    wp["snapshot_week"] = pd.to_datetime(wp["snapshot_week"])

    queue_items = []
    for product_type, g in wp.groupby("product_type"):
        g = g.sort_values("snapshot_week")
        if len(g) < RECENT_WEEKS + 2:
            continue  # not enough history to call a trend, one way or the other
        recent = g.tail(RECENT_WEEKS)["median_ape"].mean()
        baseline = g.head(len(g) - RECENT_WEEKS)["median_ape"].mean()
        if baseline <= 0:
            continue
        pct_change = (recent - baseline) / baseline * 100.0
        if pct_change < REGRESSION_MILD_PCT:
            continue  # flat or improving -- not a triage item

        severity = "high" if pct_change >= REGRESSION_SEVERE_PCT else "medium"
        queue_items.append({
            "source": "accuracy_regression",
            "severity": severity,
            "title": f"[{product_type}] forecast accuracy regressing",
            "description": (
                f"median_ape rose {pct_change:.0f}% (baseline {baseline:.3f} -> "
                f"recent {RECENT_WEEKS}-wk avg {recent:.3f}) over {len(g)} tracked weeks."
            ),
            "recommendation": "Root-cause which SKUs/methods/demand classes are driving the regression.",
            "evidence": {
                "product_type": product_type, "baseline_median_ape": round(baseline, 4),
                "recent_median_ape": round(recent, 4), "pct_change": round(pct_change, 1),
                "weeks_tracked": len(g),
            },
            "likely_code_locations": [],
            "layer2_prompt": (
                f"Forecast accuracy for product_type={product_type} is regressing: median_ape rose "
                f"{pct_change:.0f}% (baseline {baseline:.3f} -> recent {RECENT_WEEKS}-week average "
                f"{recent:.3f}) across {len(g)} tracked weeks in "
                f"agents/reports/forecast_accuracy_details_*.csv. Break this down by forecast_method "
                f"and demand_class (see the 'by_method'/'by_demand_class' rows in the same CSV, or "
                f"agents/forecast_history_db.py directly) to find which segment is actually driving it -- "
                f"a portfolio-level average can hide one bad method/class dragging the rest, the same way "
                f"portfolio_gmroi hid LUMPY vs SMOOTH differences this session. Form a hypothesis, test it "
                f"against the real forecast_history.db, and if confirmed, propose a scoped fix."
            ),
            "_source_path": str(path),
        })
    return queue_items


def main() -> None:
    parser = argparse.ArgumentParser(description="Layer 1 regression triage: merge audit findings + accuracy trend into one ranked queue.")
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()

    queue = load_audit_findings() + load_accuracy_regressions()
    queue.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 9), item["title"]))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = REPORTS_DIR / f"triage_queue_{timestamp}.json"
    out_path.write_text(json.dumps(queue, indent=2, default=str), encoding="utf-8")

    print(f"=== Regression Triage (Layer 1) ===")
    print(f"Audit findings: {sum(1 for q in queue if q['source'] == 'audit_finding')}")
    print(f"Accuracy regressions flagged: {sum(1 for q in queue if q['source'] == 'accuracy_regression')}")
    print(f"Total queue: {len(queue)}")
    print(f"Written to: {out_path}\n")

    for i, item in enumerate(queue[: args.top_n], 1):
        print(f"{i}. [{item['severity'].upper()}] ({item['source']}) {item['title']}")
        print(f"   {item['description']}")
    print()


if __name__ == "__main__":
    main()
