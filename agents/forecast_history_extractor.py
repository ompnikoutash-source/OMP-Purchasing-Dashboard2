"""
Forecast Accuracy History -- extraction from git history.

Walks the git history of each product line's forecast JSON, dedupes to one
snapshot per calendar day (recent history runs the forecast ~3x/day; one
snapshot/day is the right granularity for a trend), and reads each historical
version via `git show <sha>:<path>` (no working-tree checkout -- this never
touches the live repo state). Each snapshot is parsed with
agents.utils.parse_snapshot_payload() -- the same logic forecast_audit_agent.py
already uses for live files -- and every (item, forecasted period) pair
becomes one row in forecast_accuracy via forecast_history_db.upsert_predictions().

Idempotent: re-running over the same git history just re-inserts rows the
UNIQUE constraint already has, which upsert_predictions() silently skips.

Usage:
    .venv\\Scripts\\python.exe -m agents.forecast_history_extractor
    .venv\\Scripts\\python.exe -m agents.forecast_history_extractor --dataset flooring
    .venv\\Scripts\\python.exe -m agents.forecast_history_extractor --since 2026-06-01
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agents import forecast_history_db as db
from agents.utils import parse_snapshot_payload, split_fallback_method

REPO_DIR = Path(__file__).resolve().parent.parent

DATASET_PATHS = {
    "flooring": "flooringwebappJSON",
    "sundries": "sundrieswebappJSON",
    "moulding": "mouldingwebappJSON",
}

BATCH_COMMIT_SIZE = 10  # insert every N processed commits, bound memory on a full backfill


def _git(args: List[str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _commits_for_file(rel_path: str, since: Optional[str] = None) -> List[Tuple[str, str]]:
    """[(sha, commit_date_iso), ...], newest first (git log's default order)."""
    args = ["log", "--format=%H|%cI", "--", rel_path]
    if since:
        args = ["log", "--format=%H|%cI", f"--since={since}", "--", rel_path]
    out = _git(args)
    pairs = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        sha, iso = line.split("|", 1)
        pairs.append((sha, iso))
    return pairs


def _dedupe_one_per_day(commits: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """git log is newest-first, so the first commit seen for a given calendar
    day is already the last (most recent) one that day -- keep only that."""
    seen_days = set()
    result = []
    for sha, iso in commits:
        day = iso[:10]  # ISO date prefix, e.g. "2026-08-18"
        if day in seen_days:
            continue
        seen_days.add(day)
        result.append((sha, iso))
    return result


def _show_json(sha: str, rel_path: str) -> Optional[Dict]:
    try:
        text = _git(["show", f"{sha}:{rel_path}"])
    except RuntimeError:
        return None  # file didn't exist at this commit (renamed/added later), skip
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None  # corrupted/partial historical commit, skip rather than crash the backfill


def _snapshot_to_rows(dataset: str, sha: str, snapshot_date: str, snap) -> List[Dict]:
    rows = []
    scoring_method, used_fallback_average = split_fallback_method(snap.method)
    if snap.weekly_fc.size and len(snap.weekly_fc_dates) == snap.weekly_fc.size:
        for idx, (target_date, value) in enumerate(zip(snap.weekly_fc_dates, snap.weekly_fc)):
            rows.append({
                "snapshot_date": snapshot_date, "commit_sha": sha, "product_type": dataset,
                "item_number": snap.sku, "forecast_method": snap.method,
                "scoring_method": scoring_method, "used_fallback_average": int(used_fallback_average),
                "demand_class": snap.demand_class,
                "abc_class": snap.abc_class, "period_granularity": "week", "period_index": idx,
                "target_period_start": target_date[:10], "predicted_demand": float(value),
            })
    if snap.monthly_fc.size and len(snap.monthly_fc_dates) == snap.monthly_fc.size:
        for idx, (target_date, value) in enumerate(zip(snap.monthly_fc_dates, snap.monthly_fc)):
            if not target_date:
                continue
            rows.append({
                "snapshot_date": snapshot_date, "commit_sha": sha, "product_type": dataset,
                "item_number": snap.sku, "forecast_method": snap.method,
                "scoring_method": scoring_method, "used_fallback_average": int(used_fallback_average),
                "demand_class": snap.demand_class,
                "abc_class": snap.abc_class, "period_granularity": "month", "period_index": idx,
                "target_period_start": target_date[:10], "predicted_demand": float(value),
            })
    return rows


def extract_dataset(conn, dataset: str, rel_path: str, since: Optional[str] = None) -> Dict[str, int]:
    commits = _dedupe_one_per_day(_commits_for_file(rel_path, since))
    print(f"  {dataset}: {len(commits)} calendar-day snapshots to process")

    stats = {"commits_processed": 0, "commits_skipped": 0, "rows_inserted": 0}
    batch: List[Dict] = []

    for i, (sha, iso) in enumerate(commits):
        payload = _show_json(sha, rel_path)
        if payload is None:
            stats["commits_skipped"] += 1
            continue
        snapshot_date = iso[:10]
        try:
            snapshots = parse_snapshot_payload(dataset, payload, source_file=f"git:{sha}:{rel_path}")
        except Exception as e:
            print(f"    WARNING: failed to parse {sha[:8]} ({snapshot_date}): {e}")
            stats["commits_skipped"] += 1
            continue

        for snap in snapshots:
            batch.extend(_snapshot_to_rows(dataset, sha, snapshot_date, snap))
        stats["commits_processed"] += 1

        if (i + 1) % BATCH_COMMIT_SIZE == 0 or i == len(commits) - 1:
            stats["rows_inserted"] += db.upsert_predictions(conn, batch)
            batch = []
            print(f"    ...{i + 1}/{len(commits)} commits, {stats['rows_inserted']} rows inserted so far")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract forecast history from git into forecast_history.db")
    parser.add_argument("--dataset", choices=list(DATASET_PATHS.keys()), default=None,
                         help="Limit to one dataset (default: all three)")
    parser.add_argument("--since", default=None, help="Only commits since this date (YYYY-MM-DD), e.g. for incremental runs")
    args = parser.parse_args()

    conn = db.connect()
    datasets = {args.dataset: DATASET_PATHS[args.dataset]} if args.dataset else DATASET_PATHS

    print(f"Extracting forecast history into {db.DB_PATH}")
    for dataset, rel_path in datasets.items():
        stats = extract_dataset(conn, dataset, rel_path, args.since)
        print(f"  {dataset}: {stats}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
