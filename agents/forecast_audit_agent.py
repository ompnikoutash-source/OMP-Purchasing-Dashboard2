from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

from agents.utils import SkuSnapshot, WEEKS_PER_MONTH, load_config, load_snapshots


FALLBACK_METHOD_TOKENS = (
    "HISTORICAL_MEAN",
    "SAFE_MEAN",
    "RECENT_AVG",
    "FALLBACK",
)
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass
class Finding:
    dataset: str
    sku: str
    severity: str
    issue_id: str
    issue_description: str
    method: str
    demand_class: str
    abc_class: str
    evidence: Dict
    recommendation: str
    likely_code_locations: List[str]
    source_file: str


def _sample_by_dataset(
    snapshots: List[SkuSnapshot],
    sample_size: int,
    seed: int,
) -> List[SkuSnapshot]:
    if sample_size <= 0:
        return snapshots

    rng = random.Random(seed)
    by_ds: Dict[str, List[SkuSnapshot]] = {}
    for row in snapshots:
        by_ds.setdefault(row.dataset, []).append(row)

    sampled: List[SkuSnapshot] = []
    for dataset, rows in by_ds.items():
        if len(rows) <= sample_size:
            sampled.extend(rows)
            continue
        sampled.extend(rng.sample(rows, sample_size))
    return sampled


def _rule_triggered(rule_id: str, sku: SkuSnapshot, thresholds: Dict) -> Tuple[bool, Dict]:
    fc_w = sku.weekly_fc_mean
    recent_w = sku.weekly_recent_mean
    recent_sum_26w = sku.weekly_recent_sum
    ss = sku.safety_stock
    rop = sku.reorder_point
    s_level = sku.order_up_to_level

    evidence = {
        "weekly_fc_mean": round(fc_w, 4),
        "weekly_recent_mean": round(recent_w, 4),
        "weekly_recent_sum_26w": round(recent_sum_26w, 4),
        "safety_stock": round(ss, 4),
        "reorder_point": round(rop, 4),
        "order_up_to_level": round(s_level, 4),
        "on_order": round(sku.on_order, 4),
        "catchup_order_qty": round(sku.catchup_order_qty, 4),
        "trailing_zero_weeks": sku.trailing_zero_weeks,
    }

    if rule_id == "low_fc_high_ss":
        ok = (
            fc_w <= float(thresholds.get("max_fc_weekly", 0.25))
            and recent_sum_26w <= float(thresholds.get("max_recent_weekly_sum", 1.0))
            and ss >= float(thresholds.get("min_safety_stock", 0.5))
        )
        return ok, evidence

    if rule_id == "future_level_drift":
        ratio_threshold = float(thresholds.get("ratio_recent_nonzero", 2.5))
        min_when_zero = float(thresholds.get("min_fc_when_recent_zero", 0.25))
        ratio = (fc_w / recent_w) if recent_w > 0 else np.inf
        evidence["future_to_recent_ratio"] = None if not np.isfinite(ratio) else round(float(ratio), 4)
        ok = (recent_w > 0 and ratio > ratio_threshold) or (recent_w == 0 and fc_w > min_when_zero)
        return ok, evidence

    if rule_id == "weekly_monthly_mismatch":
        if sku.weekly_fc.size == 0 or sku.monthly_fc.size == 0:
            return False, evidence
        sum_weekly = float(np.nansum(sku.weekly_fc))
        sum_monthly = float(np.nansum(sku.monthly_fc))
        if sum_weekly <= 0 or sum_monthly <= 0:
            return False, evidence
        # Materiality guard: if both totals are sub-threshold (near-zero demand),
        # the absolute difference is operationally irrelevant.
        min_material = float(thresholds.get("min_material_units", 1.0))
        if sum_weekly < min_material and sum_monthly < min_material:
            return False, evidence
        ratio = sum_monthly / sum_weekly
        evidence["sum_weekly_fc"] = round(sum_weekly, 4)
        evidence["sum_monthly_fc"] = round(sum_monthly, 4)
        evidence["monthly_to_weekly_ratio"] = round(ratio, 4)
        ok = ratio < float(thresholds.get("min_ratio", 0.8)) or ratio > float(thresholds.get("max_ratio", 1.25))
        return ok, evidence

    if rule_id == "default_lead_time_used":
        src = str(getattr(sku, "lead_time_source", "") or "").strip().upper()
        lt_days = float(getattr(sku, "lead_time_days", 0.0) or 0.0)
        default_lt = float(thresholds.get("default_lt_days", 30.0))
        tol = float(thresholds.get("tolerance_days", 1.0))
        evidence["lead_time_source"] = src
        if lt_days > 0:
            evidence["lead_time_days"] = round(lt_days, 4)

        # Source-aware check: only flag explicit default/fallback sources.
        default_sources = {"DEFAULT30", "DEFAULT_30D", "DEFAULT", "IMPUTED_DEFAULT_30D"}
        if src:
            if src in default_sources:
                if lt_days > 0:
                    return abs(lt_days - default_lt) <= tol, evidence
                # Source says default but value missing: still flag.
                return True, evidence
            # Non-default source should not trigger this rule.
            return False, evidence

        # Backward-compatible inference path for snapshots without source metadata.
        if sku.daily_demand <= 0:
            return False, evidence
        lead_time_mean = max(0.0, rop - ss)
        if lead_time_mean <= 0:
            return False, evidence
        inferred_lt_days = lead_time_mean / max(sku.daily_demand, 1e-9)
        evidence["inferred_lead_time_days"] = round(inferred_lt_days, 4)
        ok = abs(inferred_lt_days - default_lt) <= tol
        return ok, evidence

    if rule_id == "inventory_position_identity":
        expected = sku.available + sku.on_order - sku.backorder
        delta = sku.inventory_position - expected
        evidence["inventory_position_expected"] = round(expected, 4)
        evidence["inventory_position_delta"] = round(delta, 4)
        ok = abs(delta) > float(thresholds.get("max_abs_error", 0.01))
        return ok, evidence

    if rule_id == "on_order_duplicate_risk":
        max_fc = float(thresholds.get("max_fc_weekly", 0.5))
        # Only flag when on_order alone already covers most of the ordering need.
        # on_order >= s_level means the in-flight PO already meets or exceeds the
        # order-up-to target — any additional catchup is a true duplicate.
        # Also suppress when active backorders explain why both coexist.
        backorder_exceeds_on_order = sku.backorder > sku.on_order
        on_order_covers_target = s_level > 0 and sku.on_order >= s_level
        evidence["backorder"] = round(sku.backorder, 4)
        evidence["on_order_covers_target"] = on_order_covers_target
        ok = (
            sku.on_order > 0
            and sku.catchup_order_qty > 0
            and fc_w <= max_fc
            and not backorder_exceeds_on_order
            and on_order_covers_target
        )
        return ok, evidence

    if rule_id == "negative_inventory_position":
        threshold = float(thresholds.get("min_ip_vs_ss_ratio", -1.0))
        ip = sku.inventory_position
        evidence["inventory_position"] = round(ip, 4)
        evidence["safety_stock"] = round(ss, 4)
        if ss > 0:
            ratio = ip / ss
            evidence["ip_to_ss_ratio"] = round(ratio, 4)
            return ratio < threshold, evidence
        return ip < 0, evidence

    if rule_id == "high_backorder_ratio":
        threshold = float(thresholds.get("min_backorder_to_ss_ratio", 2.0))
        backorder = sku.backorder
        evidence["backorder"] = round(backorder, 4)
        evidence["safety_stock"] = round(ss, 4)
        if backorder <= 0:
            return False, evidence
        if ss > 0:
            ratio = backorder / ss
            evidence["backorder_to_ss_ratio"] = round(ratio, 4)
            return ratio >= threshold, evidence
        # No safety stock but has backorders — always flag.
        return True, evidence

    if rule_id == "negative_history":
        negative_weekly = bool(np.any(sku.weekly_hist < 0)) if sku.weekly_hist.size else False
        negative_monthly = bool(np.any(sku.monthly_hist < 0)) if sku.monthly_hist.size else False
        evidence["negative_weekly_present"] = negative_weekly
        evidence["negative_monthly_present"] = negative_monthly
        return negative_weekly or negative_monthly, evidence

    if rule_id == "fallback_without_recent_demand":
        method_upper = sku.method.upper()
        is_fallback = any(tok in method_upper for tok in FALLBACK_METHOD_TOKENS)
        max_recent_sum = float(thresholds.get("max_recent_weekly_sum", 0.0))
        ok = is_fallback and recent_sum_26w <= max_recent_sum and fc_w > 0
        evidence["is_fallback"] = is_fallback
        return ok, evidence

    if rule_id == "zero_fc_nonzero_policy":
        # Only treat true zero (not negative/invalid) as this specific issue.
        ok = 0.0 <= fc_w <= 1e-9 and (ss > 1e-9 or rop > 1e-9 or s_level > 1e-9)
        return ok, evidence

    if rule_id == "s_below_rop":
        if s_level <= 0:
            return False, evidence
        return s_level + 1e-9 < rop, evidence

    return False, evidence


def evaluate_rules(
    snapshots: List[SkuSnapshot],
    rule_defs: List[Dict],
    fix_map: Dict[str, Dict],
) -> List[Finding]:
    findings: List[Finding] = []
    for snapshot in snapshots:
        for rule in rule_defs:
            if not bool(rule.get("enabled", True)):
                continue
            rule_id = str(rule.get("id"))
            hit, evidence = _rule_triggered(rule_id, snapshot, rule.get("thresholds", {}))
            if not hit:
                continue
            fix_meta = fix_map.get(rule_id, {})
            findings.append(
                Finding(
                    dataset=snapshot.dataset,
                    sku=snapshot.sku,
                    severity=str(rule.get("severity", "medium")),
                    issue_id=rule_id,
                    issue_description=str(rule.get("description", "")),
                    method=snapshot.method,
                    demand_class=snapshot.demand_class,
                    abc_class=snapshot.abc_class,
                    evidence=evidence,
                    recommendation=str(fix_meta.get("recommendation", "")),
                    likely_code_locations=[str(x) for x in fix_meta.get("likely_code_locations", [])],
                    source_file=snapshot.source_file,
                )
            )
    return findings


def _write_reports(findings: List[Finding], output_dir: Path, run_stamp: str) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    details_path = output_dir / f"audit_details_{run_stamp}.csv"
    summary_path = output_dir / f"audit_summary_{run_stamp}.csv"
    markdown_path = output_dir / f"audit_report_{run_stamp}.md"
    json_path = output_dir / f"audit_findings_{run_stamp}.json"

    detail_rows = []
    for finding in findings:
        row = asdict(finding)
        row["evidence"] = json.dumps(row["evidence"], sort_keys=True)
        row["likely_code_locations"] = "; ".join(row["likely_code_locations"])
        detail_rows.append(row)

    details_df = pd.DataFrame(detail_rows)
    if details_df.empty:
        details_df = pd.DataFrame(
            columns=[
                "dataset",
                "sku",
                "severity",
                "issue_id",
                "issue_description",
                "method",
                "demand_class",
                "abc_class",
                "evidence",
                "recommendation",
                "likely_code_locations",
                "source_file",
            ]
        )
    details_df.to_csv(details_path, index=False)

    summary_df = (
        details_df.groupby(["severity", "issue_id", "dataset"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["severity", "count", "issue_id"], ascending=[True, False, True])
    )
    summary_df.to_csv(summary_path, index=False)

    payload = [asdict(finding) for finding in findings]
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = ["# Forecast Audit Report", "", f"Run timestamp: `{run_stamp}`", ""]
    if summary_df.empty:
        lines.append("No rule violations found.")
    else:
        lines.append("## Summary")
        lines.append("")
        for _, row in summary_df.iterrows():
            lines.append(
                f"- `{row['issue_id']}` ({row['severity']}) in `{row['dataset']}`: {int(row['count'])} finding(s)"
            )
        lines.append("")
        lines.append("## Top Examples")
        lines.append("")
        for issue_id in summary_df["issue_id"].drop_duplicates().tolist():
            issue_rows = details_df[details_df["issue_id"] == issue_id].head(3)
            lines.append(f"### `{issue_id}`")
            for _, row in issue_rows.iterrows():
                lines.append(
                    f"- `{row['dataset']}` / `{row['sku']}` / `{row['method']}` -> {row['issue_description']}"
                )
            lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "details_csv": details_path,
        "summary_csv": summary_path,
        "report_md": markdown_path,
        "findings_json": json_path,
    }


def run_audit(
    base_dir: Path,
    rules_path: Path,
    fix_map_path: Path,
    sample_size: int,
    seed: int,
    datasets: Iterable[str],
    output_dir: Path,
) -> Dict[str, object]:
    rules_cfg = load_config(rules_path)
    fix_cfg = load_config(fix_map_path)
    dataset_paths = rules_cfg.get("dataset_paths", {})
    snapshots, run_meta = load_snapshots(base_dir, dataset_paths, datasets=datasets)
    snapshots = _sample_by_dataset(snapshots, sample_size=sample_size, seed=seed)

    findings = evaluate_rules(
        snapshots=snapshots,
        rule_defs=rules_cfg.get("rules", []),
        fix_map=fix_cfg.get("fix_map", {}),
    )
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_paths = _write_reports(findings=findings, output_dir=output_dir, run_stamp=run_stamp)
    return {
        "snapshots_scanned": len(snapshots),
        "findings_count": len(findings),
        "run_meta": run_meta,
        "report_paths": report_paths,
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Forecast output sanity audit agent.")
    parser.add_argument("--base-dir", type=Path, default=Path.cwd(), help="Project root directory.")
    parser.add_argument("--rules", type=Path, default=Path("agents/rules.yaml"))
    parser.add_argument("--fix-map", type=Path, default=Path("agents/fix_map.yaml"))
    parser.add_argument("--sample-size", type=int, default=0, help="Per-dataset sample size (0 = all SKUs).")
    parser.add_argument("--seed", type=int, default=20260223)
    parser.add_argument(
        "--datasets",
        nargs="*",
        default=["sundries", "moulding", "flooring"],
        help="Datasets to scan.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("agents/reports"))
    parser.add_argument(
        "--fail-on-severity",
        type=str,
        default="",
        choices=["", "low", "medium", "high", "critical"],
        help="Exit non-zero if any finding is at or above this severity.",
    )
    args = parser.parse_args()

    result = run_audit(
        base_dir=args.base_dir,
        rules_path=(args.base_dir / args.rules).resolve(),
        fix_map_path=(args.base_dir / args.fix_map).resolve(),
        sample_size=args.sample_size,
        seed=args.seed,
        datasets=args.datasets,
        output_dir=(args.base_dir / args.output_dir).resolve(),
    )

    print("=== Forecast Audit Agent ===")
    print(f"Snapshots scanned: {result['snapshots_scanned']}")
    print(f"Findings: {result['findings_count']}")
    print("Reports:")
    for name, path in result["report_paths"].items():
        print(f"  {name}: {path}")
    print("Run meta (source files):")
    for ds, meta in result["run_meta"].items():
        print(f"  {ds}: {meta.get('run_timestamp_local', 'unknown')}")

    fail_threshold = str(args.fail_on_severity).strip().lower()
    if fail_threshold:
        threshold_rank = SEVERITY_RANK.get(fail_threshold, 99)
        blocked = [
            f
            for f in result["findings"]
            if SEVERITY_RANK.get(str(f.severity).lower(), 0) >= threshold_rank
        ]
        if blocked:
            print(
                f"CI gate failed: {len(blocked)} finding(s) at or above severity '{fail_threshold}'."
            )
            sys.exit(2)


if __name__ == "__main__":
    main()
