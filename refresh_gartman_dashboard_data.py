"""
Refresh current Gartman inventory and PO data in flooringwebappJSON.

This is intentionally lighter than a full forecast refresh:
  - Preserves demand model outputs, safety stock, reorder point, and S level.
  - Refreshes available inventory, backorders, on-PO quantities, and arrivals.
  - Regenerates the monthly reorder projection from the preserved policy values
    so the browser dashboard reflects the new current inventory picture.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

os.environ.setdefault("STREAMLIT_LOG_LEVEL", "error")

import flooringwebapp as fw
from core.inventory import simulate_monthly_projection as simulate_monthly


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_JSON_PATH = fw.WEBAPP_JSON_PATH
STATUS_FILE = BASE_DIR / "_refresh_status.json"


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _group_map() -> Dict[str, List[str]]:
    return {
        _norm(group): [_norm(member) for member in members if _norm(member)]
        for group, members in fw.SKU_CONSOLIDATION_GROUPS.items()
        if _norm(group)
    }


def _payload_physical_skus(payload: Dict[str, Any], groups: Dict[str, List[str]]) -> List[str]:
    skus: Set[str] = set()
    for row in payload.get("Inventory_Metrics", []) or []:
        sku = _norm(row.get("sku") or row.get("item_number"))
        if not sku:
            continue
        if sku in groups:
            skus.update(groups[sku])
        else:
            skus.add(sku)

    for row in payload.get("member_snapshots", []) or []:
        sku = _norm(row.get("sku") or row.get("item_number"))
        if sku:
            skus.add(sku)

    return sorted(skus)


def _load_payload(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Dashboard JSON not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _backup_json(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup)
    return backup


def _load_current_gartman_data(physical_skus: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    conn = fw._connect()
    try:
        sku_master = fw.get_active_skus(conn, sku_list=physical_skus, ensure_skus=set(physical_skus))
        arrivals = fw.load_arrivals(conn, physical_skus)
    finally:
        conn.close()

    if not sku_master.empty:
        sku_master["ITEM_NUMBER"] = sku_master["ITEM_NUMBER"].astype(str).str.strip().str.upper()
    if arrivals is None:
        arrivals = pd.DataFrame()
    if not arrivals.empty:
        arrivals["ITEM_NUMBER"] = arrivals["ITEM_NUMBER"].astype(str).str.strip().str.upper()

    return _remove_excluded_po_vendors(sku_master, arrivals)


def _remove_excluded_po_vendors(sku_master: pd.DataFrame, arrivals: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if arrivals.empty or "VENDOR_NUMBER" not in arrivals.columns:
        return sku_master, arrivals

    arrivals = arrivals.copy()
    arrivals["VENDOR_NUMBER"] = arrivals["VENDOR_NUMBER"].astype(str).str.strip()
    arrivals["QUANTITY_SF"] = pd.to_numeric(arrivals.get("QUANTITY_SF", 0), errors="coerce").fillna(0.0)
    excluded = arrivals["VENDOR_NUMBER"].isin(fw.EXCLUDED_PO_VENDORS)

    if excluded.any() and not sku_master.empty:
        excluded_qty = arrivals.loc[excluded].groupby("ITEM_NUMBER")["QUANTITY_SF"].sum()
        sku_master = sku_master.copy()
        for sku, qty in excluded_qty.items():
            mask = sku_master["ITEM_NUMBER"].astype(str).str.strip().str.upper() == _norm(sku)
            if not mask.any():
                continue
            sku_master.loc[mask, "ON_PO_SF"] = (
                pd.to_numeric(sku_master.loc[mask, "ON_PO_SF"], errors="coerce")
                .fillna(0.0)
                .sub(float(qty))
                .clip(lower=0.0)
            )
        sku_master["INVENTORY_POSITION"] = (
            pd.to_numeric(sku_master["AVAILABLE_SF"], errors="coerce").fillna(0.0)
            + pd.to_numeric(sku_master["ON_PO_SF"], errors="coerce").fillna(0.0)
            - pd.to_numeric(sku_master["BACKORDER_SF"], errors="coerce").fillna(0.0)
        )
        arrivals = arrivals.loc[~excluded].reset_index(drop=True)

    return sku_master, arrivals


def _master_lookup(sku_master: pd.DataFrame) -> Dict[str, pd.Series]:
    if sku_master.empty:
        return {}
    return {
        _norm(row.get("ITEM_NUMBER")): row
        for _, row in sku_master.iterrows()
        if _norm(row.get("ITEM_NUMBER"))
    }


def _aggregate_group_metrics(members: Iterable[str], master: Dict[str, pd.Series]) -> Optional[Dict[str, float]]:
    rows = [master[m] for m in members if m in master]
    if not rows:
        return None
    available = sum(_to_float(r.get("AVAILABLE_SF")) for r in rows)
    on_po = sum(_to_float(r.get("ON_PO_SF")) for r in rows)
    backorder = sum(_to_float(r.get("BACKORDER_SF")) for r in rows)
    return {
        "available_sf": available,
        "on_po_sf": on_po,
        "backorder_sf": backorder,
        "inventory_position": available + on_po - backorder,
    }


def _update_inventory_metrics(
    df_results: pd.DataFrame,
    sku_master: pd.DataFrame,
    groups: Dict[str, List[str]],
) -> pd.DataFrame:
    master = _master_lookup(sku_master)
    out = df_results.copy()

    for idx, row in out.iterrows():
        sku = _norm(row.get("sku") or row.get("item_number"))
        if not sku:
            continue

        if sku in master:
            current = master[sku]
            out.at[idx, "description"] = str(current.get("DESCRIPTION", row.get("description", "")) or "")
            out.at[idx, "vendor_number"] = str(current.get("VENDOR_NUMBER", row.get("vendor_number", "")) or "")
            out.at[idx, "vendor_name"] = str(current.get("VENDOR_NAME", row.get("vendor_name", "")) or "")
            out.at[idx, "collection"] = str(current.get("COLLECTION", row.get("collection", "")) or "")
            out.at[idx, "available_sf"] = _to_float(current.get("AVAILABLE_SF"))
            out.at[idx, "on_po_sf"] = _to_float(current.get("ON_PO_SF"))
            out.at[idx, "backorder_sf"] = _to_float(current.get("BACKORDER_SF"))
            out.at[idx, "inventory_position"] = _to_float(current.get("INVENTORY_POSITION"))
        elif sku in groups:
            agg = _aggregate_group_metrics(groups[sku], master)
            if agg:
                for col, val in agg.items():
                    out.at[idx, col] = val

    return out


def _update_member_snapshots(member_snapshots: List[Dict[str, Any]], sku_master: pd.DataFrame) -> List[Dict[str, Any]]:
    master = _master_lookup(sku_master)
    updated: List[Dict[str, Any]] = []
    for snapshot in member_snapshots or []:
        row = dict(snapshot)
        sku = _norm(row.get("sku") or row.get("item_number"))
        current = master.get(sku)
        if current is not None:
            row["description"] = str(current.get("DESCRIPTION", row.get("description", "")) or "")
            row["vendor_name"] = str(current.get("VENDOR_NAME", row.get("vendor_name", "")) or "")
            row["vendor_number"] = str(current.get("VENDOR_NUMBER", row.get("vendor_number", "")) or "")
            row["available_sf"] = _to_float(current.get("AVAILABLE_SF"))
            row["on_po_sf"] = _to_float(current.get("ON_PO_SF"))
            row["backorder_sf"] = _to_float(current.get("BACKORDER_SF"))
        updated.append(row)
    return updated


def _rows_for_projection_sku(sku: str, arrivals: pd.DataFrame, groups: Dict[str, List[str]]) -> pd.DataFrame:
    if arrivals.empty or "ITEM_NUMBER" not in arrivals.columns:
        return pd.DataFrame()
    if sku in groups:
        return arrivals[arrivals["ITEM_NUMBER"].isin(groups[sku])].copy()
    return arrivals[arrivals["ITEM_NUMBER"] == sku].copy()


def _inbound_schedule(arrivals_for_sku: pd.DataFrame, as_of_date: pd.Timestamp) -> Dict[int, float]:
    schedule: Dict[int, float] = {}
    if arrivals_for_sku.empty:
        return schedule
    for _, po in arrivals_for_sku.iterrows():
        qty = _to_float(po.get("QUANTITY_SF"))
        if qty <= 0:
            continue
        arrival_date = fw._resolve_po_projection_arrival_date(po)
        if pd.isna(arrival_date):
            continue
        day_offset = max(0, int((pd.Timestamp(arrival_date).normalize() - as_of_date).days))
        schedule[day_offset] = schedule.get(day_offset, 0.0) + qty
    return schedule


def _weekly_forecast_mean(row: pd.Series) -> float:
    for col in ("weekly_fc_mean", "avg_weekly_demand"):
        val = _to_float(row.get(col), default=float("nan"))
        if not math.isnan(val):
            return val
    daily = _to_float(row.get("daily_mean_demand"))
    return daily * 7.0


def _regenerate_monthly_projection(
    df_results: pd.DataFrame,
    old_monthly: pd.DataFrame,
    arrivals: pd.DataFrame,
    groups: Dict[str, List[str]],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    as_of_date = pd.Timestamp.now().normalize()
    out_results = df_results.copy()
    monthly_parts: List[pd.DataFrame] = []

    if old_monthly is None or old_monthly.empty:
        old_monthly = pd.DataFrame()
    else:
        old_monthly = old_monthly.copy()
        if "SKU" in old_monthly.columns:
            old_monthly["SKU"] = old_monthly["SKU"].astype(str).str.strip().str.upper()

    for idx, row in out_results.iterrows():
        sku = _norm(row.get("sku") or row.get("item_number"))
        if not sku:
            continue

        sku_old = old_monthly[old_monthly["SKU"] == sku].copy() if "SKU" in old_monthly.columns else pd.DataFrame()
        hist = pd.DataFrame()
        if not sku_old.empty and "Row_Type" in sku_old.columns:
            hist = sku_old[sku_old["Row_Type"].astype(str).str.upper() == "HIST"].copy()

        arrivals_for_sku = _rows_for_projection_sku(sku, arrivals, groups)
        inbound = _inbound_schedule(arrivals_for_sku, as_of_date)
        weekly_mean = _weekly_forecast_mean(row)
        lead_time_weeks = _to_float(row.get("lead_time_weeks"), default=0.0)
        if lead_time_weeks <= 0:
            lead_time_weeks = max(_to_float(row.get("lead_time_days"), default=30.0) / 7.0, 0.0)

        sim_rows = simulate_monthly(
            inventory_position=_to_float(row.get("inventory_position")),
            weekly_forecast=np.array([weekly_mean], dtype=float),
            reorder_point=_to_float(row.get("reorder_point")),
            reorder_qty=_to_float(row.get("reorder_quantity")),
            sales_df=pd.DataFrame(columns=["transaction_date", "quantity_shipped"]),
            as_of_date=as_of_date,
            safety_stock=_to_float(row.get("safety_stock")),
            months_ahead=12,
            abc_class=str(row.get("sku_abc", "C") or "C"),
            order_up_to_level=_to_float(row.get("order_up_to_level")),
            lead_time_weeks=lead_time_weeks,
            available=_to_float(row.get("available_sf")),
            backorder=_to_float(row.get("backorder_sf")),
            on_order=0.0 if inbound else _to_float(row.get("on_po_sf")),
            inbound_schedule=inbound if inbound else None,
        )

        sim = pd.DataFrame(sim_rows)
        if sim.empty:
            continue
        sim["Month"] = pd.to_datetime(sim["Month"], errors="coerce")
        sim["Historical Demand"] = np.nan
        sim["SKU"] = sku
        sim["vendor_number"] = row.get("vendor_number", "")
        sim["vendor_name"] = row.get("vendor_name", "")
        sim["collection"] = row.get("collection", "")
        sim["description"] = row.get("description", "")
        if "sf_per_pallet" in row.index:
            sim["sf_per_pallet"] = row.get("sf_per_pallet")
        if "pallets_per_container" in row.index:
            sim["pallets_per_container"] = row.get("pallets_per_container")

        combined = pd.concat([hist, sim], ignore_index=True, sort=False)
        if "Month" in combined.columns:
            combined["Month"] = pd.to_datetime(combined["Month"], errors="coerce")
            combined = combined.sort_values("Month")

        orders = combined[pd.to_numeric(combined.get("Order Quantity", 0), errors="coerce").fillna(0) > 0]
        out_results.at[idx, "first_reorder_month"] = (
            orders.iloc[0]["Month"] if not orders.empty and "Month" in orders.columns else "None"
        )
        monthly_parts.append(combined)

    if not monthly_parts:
        return out_results, old_monthly
    return out_results, pd.concat(monthly_parts, ignore_index=True, sort=False)


def _write_status(result: bool, message: str, updated_count: int, arrivals_count: int) -> None:
    status = {
        "last_run": datetime.now().isoformat(),
        "results": {"FlooringGartmanOnly": bool(result)},
        "all_ok": bool(result),
        "mode": "gartman_only",
        "message": message,
        "updated_items": updated_count,
        "arrival_rows": arrivals_count,
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")


def refresh(json_path: Path, dry_run: bool = False, backup: bool = True) -> None:
    payload = _load_payload(json_path)
    groups = _group_map()
    physical_skus = _payload_physical_skus(payload, groups)
    if not physical_skus:
        raise RuntimeError("No SKUs found in dashboard JSON.")

    df_results = pd.DataFrame(payload.get("Inventory_Metrics", []))
    if df_results.empty:
        raise RuntimeError("Inventory_Metrics is empty in dashboard JSON.")
    if "sku" not in df_results.columns and "item_number" in df_results.columns:
        df_results = df_results.rename(columns={"item_number": "sku"})
    df_results["sku"] = df_results["sku"].astype(str).str.strip().str.upper()

    old_monthly = pd.DataFrame(payload.get("Monthly_Projections", []))

    print(f"Loading current Gartman data for {len(physical_skus)} physical SKU(s)...")
    sku_master, arrivals = _load_current_gartman_data(physical_skus)
    print(f"Loaded {len(sku_master)} inventory row(s) and {len(arrivals)} open PO arrival row(s).")

    df_results = _update_inventory_metrics(df_results, sku_master, groups)
    member_snapshots = _update_member_snapshots(payload.get("member_snapshots", []) or [], sku_master)
    df_results, df_monthly = _regenerate_monthly_projection(df_results, old_monthly, arrivals, groups)

    new_payload = fw._build_webapp_payload(df_results, df_monthly, arrivals, member_snapshots=member_snapshots)
    new_payload["sql_sources"] = payload.get("sql_sources", [])
    new_payload["run_meta"]["refresh_mode"] = "Gartman inventory/PO only"
    new_payload["run_meta"]["forecast_policy_source"] = payload.get("generated_at", "")

    print(f"Updated dashboard payload for {len(df_results)} item row(s).")
    if dry_run:
        print("Dry run complete. JSON was not written.")
        return

    backup_path: Optional[Path] = None
    if backup:
        backup_path = _backup_json(json_path)
        print(f"Backup written: {backup_path}")

    fw._write_webapp_json(new_payload, json_path)
    _write_status(True, "Gartman inventory and PO data refreshed without refitting forecasts.", len(df_results), len(arrivals))
    print(f"Dashboard JSON updated: {json_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh current Gartman inventory/PO data in flooringwebappJSON.")
    parser.add_argument("--json-path", default=str(DEFAULT_JSON_PATH), help="Path to flooringwebappJSON.")
    parser.add_argument("--dry-run", action="store_true", help="Build the refreshed payload without writing JSON.")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a timestamped JSON backup before writing.")
    args = parser.parse_args()

    try:
        refresh(Path(args.json_path), dry_run=args.dry_run, backup=not args.no_backup)
        return 0
    except Exception as exc:
        _write_status(False, f"Gartman-only refresh failed: {exc}", 0, 0)
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
