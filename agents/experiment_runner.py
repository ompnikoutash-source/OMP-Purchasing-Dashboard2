from __future__ import annotations

import argparse
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import numpy as np
import pandas as pd

from core.inventory import simulate_daily_inventory
from agents.utils import SkuSnapshot, WEEKS_PER_MONTH, load_config, load_snapshots


def _sample_by_dataset(snapshots: List[SkuSnapshot], sample_size: int, seed: int) -> List[SkuSnapshot]:
    if sample_size <= 0:
        return snapshots
    rng = random.Random(seed)
    by_ds: Dict[str, List[SkuSnapshot]] = {}
    for row in snapshots:
        by_ds.setdefault(row.dataset, []).append(row)

    sampled: List[SkuSnapshot] = []
    for rows in by_ds.values():
        if len(rows) <= sample_size:
            sampled.extend(rows)
            continue
        sampled.extend(rng.sample(rows, sample_size))
    return sampled


def _infer_lead_time_days(sku: SkuSnapshot) -> int:
    if sku.daily_demand > 0 and sku.reorder_point > sku.safety_stock:
        lt = (sku.reorder_point - sku.safety_stock) / max(sku.daily_demand, 1e-9)
        return int(max(7, min(180, round(lt))))
    return 30


def _simulate_policy(
    sku: SkuSnapshot,
    ss_scale: float,
    rop_offset_days: float,
    s_scale: float,
    forecast_scale: float,
    inbound_credit: bool,
    simulation_days: int = 365,
) -> Dict:
    fc_weekly = max(0.0, sku.weekly_fc_mean) * max(0.0, forecast_scale)
    daily_forecast = fc_weekly / 7.0
    lead_time_days = _infer_lead_time_days(sku)

    lead_time_demand = max(0.0, sku.reorder_point - sku.safety_stock)
    ss_new = max(0.0, sku.safety_stock * max(0.0, ss_scale))
    rop_new = max(0.0, lead_time_demand + ss_new + daily_forecast * rop_offset_days)
    s_new = max(rop_new, sku.order_up_to_level * max(0.0, s_scale))
    if s_new <= 0.0:
        s_new = rop_new

    starting_inventory = sku.inventory_position
    if inbound_credit:
        starting_inventory += max(0.0, sku.on_order)

    daily_rows = simulate_daily_inventory(
        starting_inventory=starting_inventory,
        daily_forecast=daily_forecast,
        reorder_point=rop_new,
        order_up_to_level=s_new,
        lead_time_days=lead_time_days,
        simulation_days=simulation_days,
    )
    if not daily_rows:
        return {
            "stockout_days": 0,
            "ending_inventory": starting_inventory,
            "total_order_qty": 0.0,
            "orders_count": 0,
            "rop_new": rop_new,
            "s_new": s_new,
            "ss_new": ss_new,
            "daily_forecast": daily_forecast,
            "lead_time_days": lead_time_days,
        }

    ending_on_hand = float(daily_rows[-1]["on_hand"])
    ending_backorders = float(daily_rows[-1]["backorders"])
    ending_inventory = ending_on_hand - ending_backorders
    stockout_days = int(sum(1 for row in daily_rows if float(row["backorders"]) > 0))
    total_order_qty = float(sum(float(row["order_qty"]) for row in daily_rows))
    orders_count = int(sum(1 for row in daily_rows if float(row["order_qty"]) > 0))

    return {
        "stockout_days": stockout_days,
        "ending_inventory": ending_inventory,
        "total_order_qty": total_order_qty,
        "orders_count": orders_count,
        "rop_new": rop_new,
        "s_new": s_new,
        "ss_new": ss_new,
        "daily_forecast": daily_forecast,
        "lead_time_days": lead_time_days,
    }


def _score_experiment(sku_rows: List[Dict]) -> Dict:
    if not sku_rows:
        return {
            "sku_count": 0,
            "stockout_sku_rate_pct": 0.0,
            "avg_stockout_days": 0.0,
            "inventory_change_pct": 0.0,
            "avg_orders_per_sku": 0.0,
        }
    stockout_skus = sum(1 for r in sku_rows if r["stockout_days"] > 0)
    avg_stockout_days = float(np.mean([r["stockout_days"] for r in sku_rows]))
    start_total = float(np.sum([r["inventory_position"] for r in sku_rows]))
    end_total = float(np.sum([r["ending_inventory"] for r in sku_rows]))
    inv_change_pct = ((end_total - start_total) / start_total * 100.0) if start_total > 0 else 0.0
    avg_orders = float(np.mean([r["orders_count"] for r in sku_rows]))

    return {
        "sku_count": len(sku_rows),
        "stockout_sku_rate_pct": round(stockout_skus / len(sku_rows) * 100.0, 4),
        "avg_stockout_days": round(avg_stockout_days, 4),
        "inventory_change_pct": round(inv_change_pct, 4),
        "avg_orders_per_sku": round(avg_orders, 4),
    }


def run_experiments(
    base_dir: Path,
    rules_path: Path,
    fix_map_path: Path,
    datasets: Iterable[str],
    sample_size: int,
    seed: int,
    experiments: Iterable[str],
    output_dir: Path,
) -> Dict[str, Path]:
    rules_cfg = load_config(rules_path)
    fix_cfg = load_config(fix_map_path)
    dataset_paths = rules_cfg.get("dataset_paths", {})
    experiment_defs = fix_cfg.get("experiments", {})

    snapshots, _ = load_snapshots(base_dir, dataset_paths, datasets=datasets)
    snapshots = _sample_by_dataset(snapshots, sample_size=sample_size, seed=seed)

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = output_dir / f"experiment_details_{run_stamp}.csv"
    summary_path = output_dir / f"experiment_summary_{run_stamp}.csv"

    requested = list(experiments) if experiments else ["baseline"]
    if "baseline" not in requested:
        requested = ["baseline"] + requested

    detail_rows: List[Dict] = []
    summary_rows: List[Dict] = []

    for exp_name in requested:
        if exp_name not in experiment_defs:
            continue
        cfg = experiment_defs[exp_name]
        exp_rows: List[Dict] = []
        for sku in snapshots:
            sim = _simulate_policy(
                sku=sku,
                ss_scale=float(cfg.get("ss_scale", 1.0)),
                rop_offset_days=float(cfg.get("rop_offset_days", 0.0)),
                s_scale=float(cfg.get("s_scale", 1.0)),
                forecast_scale=float(cfg.get("forecast_scale", 1.0)),
                inbound_credit=bool(cfg.get("inbound_credit", False)),
            )
            row = {
                "experiment": exp_name,
                "dataset": sku.dataset,
                "sku": sku.sku,
                "method": sku.method,
                "abc_class": sku.abc_class,
                "demand_class": sku.demand_class,
                "inventory_position": round(sku.inventory_position, 4),
                **{k: round(v, 4) if isinstance(v, float) else v for k, v in sim.items()},
            }
            detail_rows.append(row)
            exp_rows.append(row)

        by_dataset: Dict[str, List[Dict]] = {}
        for row in exp_rows:
            by_dataset.setdefault(str(row["dataset"]), []).append(row)
        for dataset, rows in by_dataset.items():
            score = _score_experiment(rows)
            summary_rows.append({"experiment": exp_name, "dataset": dataset, **score})

    pd.DataFrame(detail_rows).to_csv(detail_path, index=False)
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    return {"details_csv": detail_path, "summary_csv": summary_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="Experiment runner for forecast policy toggles.")
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    parser.add_argument("--rules", type=Path, default=Path("agents/rules.yaml"))
    parser.add_argument("--fix-map", type=Path, default=Path("agents/fix_map.yaml"))
    parser.add_argument("--sample-size", type=int, default=50, help="Per-dataset sample size.")
    parser.add_argument("--seed", type=int, default=20260223)
    parser.add_argument(
        "--datasets",
        nargs="*",
        default=["sundries", "moulding", "flooring"],
        help="Datasets to include.",
    )
    parser.add_argument(
        "--experiments",
        nargs="*",
        default=["baseline", "lean_ss", "tight_inactivity", "drift_guard", "shorter_lead_time"],
        help="Experiment names from agents/fix_map.yaml.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("agents/reports"))
    args = parser.parse_args()

    paths = run_experiments(
        base_dir=args.base_dir,
        rules_path=(args.base_dir / args.rules).resolve(),
        fix_map_path=(args.base_dir / args.fix_map).resolve(),
        datasets=args.datasets,
        sample_size=args.sample_size,
        seed=args.seed,
        experiments=args.experiments,
        output_dir=(args.base_dir / args.output_dir).resolve(),
    )
    print("=== Experiment Runner ===")
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()

