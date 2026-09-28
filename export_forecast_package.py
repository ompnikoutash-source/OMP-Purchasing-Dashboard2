from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import OMPforecasting5 as sf
import sundrieswebapp_refactored as sundries


def _iso_date(value: Any) -> Optional[str]:
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return None


def _monthly_forecast_map(
    df: pd.DataFrame,
    month_col: str,
    demand_col: str,
) -> Dict[str, float]:
    if df is None or df.empty:
        return {}

    df = df[[month_col, demand_col]].copy()
    df = df.dropna(subset=[demand_col])
    df[month_col] = pd.to_datetime(df[month_col], errors="coerce")
    df = df.dropna(subset=[month_col])
    if df.empty:
        return {}

    df = df.sort_values(month_col)
    df = df.groupby(month_col, as_index=False)[demand_col].sum()

    out: Dict[str, float] = {}
    for _, row in df.iterrows():
        key = _iso_date(row[month_col])
        if key is None:
            continue
        out[key] = float(row[demand_col])
    return out


def _compute_target_position(rop: float, order_qty: float) -> float:
    try:
        return float(rop) + float(order_qty)
    except Exception:
        return float(rop or 0) + float(order_qty or 0)


def _build_sf_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    output_dir = Path(__file__).resolve().parent
    lead_times_excel = sf.load_lead_times(output_dir / sf.LEADTIMES_XLSX)

    forecast_sku_list = None
    if sf.USE_SKU_LIST and not sf.FOCUS_SKU:
        forecast_sku_list = sf.load_forecast_sku_list(output_dir / sf.FORECAST_SKU_LIST_FILE)

    conn = sf._connect()
    single_sku = sf.FOCUS_SKU.strip().upper() if sf.FOCUS_SKU else None
    sku_master = sf.get_active_skus(conn, single_sku, forecast_sku_list)

    if sku_master.empty:
        conn.close()
        return [], [], {"source": "sf", "processed": 0}

    sku_list_for_volume = sku_master["ITEM_NUMBER"].astype(str).str.upper().tolist()
    vol12_df = sf.load_trailing_12m_volume(conn, sku_list_for_volume)
    abc_map = sf.build_abc_map(vol12_df)

    total_vol_12m = float(vol12_df["VOL_12M"].sum()) if not vol12_df.empty else 0.0
    uplift_budget = sf.GLOBAL_UPLIFT_BUDGET_PCT * total_vol_12m if sf.ENABLE_GLOBAL_UPLIFT_BUDGET else None

    global_model = None
    global_feature_cols = None
    sku_encodings = None

    if sf.USE_GLOBAL_MODEL and not single_sku:
        cutoff_date = pd.Timestamp(sf.CUTOFF_DATE)
        df_global, sku_encodings = sf.build_global_training_data(conn, sku_master, cutoff_date)
        if len(df_global) > 1000:
            global_model, global_feature_cols = sf.train_global_xgboost(df_global)

    results: List[Dict[str, Any]] = []
    all_monthly: List[pd.DataFrame] = []

    for _, row in sku_master.iterrows():
        result = sf.process_sku(conn, row, lead_times_excel, abc_map, global_model, global_feature_cols, sku_encodings)
        if result:
            metrics, monthly_proj = result
            results.append(metrics)
            monthly_proj["SKU"] = metrics["sku"]
            all_monthly.append(monthly_proj)

    conn.close()

    if not results:
        return [], [], {"source": "sf", "processed": 0}

    df_results = pd.DataFrame(results)
    df_monthly = pd.concat(all_monthly, ignore_index=True) if all_monthly else pd.DataFrame()

    if sf.ENABLE_GLOBAL_UPLIFT_BUDGET and uplift_budget is not None and uplift_budget > 0 and not df_results.empty:
        total_uplift = float(df_results["ss_uplift_applied_pre_budget"].sum())
        if total_uplift > uplift_budget:
            k = uplift_budget / total_uplift
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"] * k
            df_results["safety_stock"] = df_results["ss_base"] + df_results["ss_uplift_applied"]
            df_results["reorder_point"] = df_results["lead_time_mean_demand"] + df_results["safety_stock"]
        else:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]
    else:
        if not df_results.empty and "ss_uplift_applied_pre_budget" in df_results.columns:
            df_results["ss_uplift_applied"] = df_results["ss_uplift_applied_pre_budget"]

    if sf.ENABLE_GLOBAL_INV_CAP and not df_results.empty and not df_monthly.empty:
        df_results, df_monthly = sf.apply_global_inv_cap(df_results, df_monthly, sf.GLOBAL_INV_CAP_SF)

    skus: List[Dict[str, Any]] = []
    weights: List[Dict[str, Any]] = []

    for row in df_results.to_dict(orient="records"):
        sku = row.get("sku", "")
        sku_monthly = df_monthly[df_monthly.get("SKU") == sku] if not df_monthly.empty else pd.DataFrame()
        fc_map = _monthly_forecast_map(sku_monthly, "Month", "Monthly_Demand")

        rop = float(row.get("reorder_point", 0.0) or 0.0)
        order_qty = float(row.get("reorder_quantity", 0.0) or 0.0)
        lead_time_days = float(row.get("lead_time_days", 0.0) or 0.0)

        skus.append(
            {
                "item_number": sku,
                "description": row.get("description", ""),
                "vendor_number": row.get("vendor_number", ""),
                "vendor_name": row.get("vendor_name", ""),
                "available_sf": float(row.get("available_sf", 0.0) or 0.0),
                "on_po_sf": float(row.get("on_po_sf", 0.0) or 0.0),
                "backorder_sf": float(row.get("backorder_sf", 0.0) or 0.0),
                "lead_time_days": lead_time_days,
                "mean_hist_monthly": float(row.get("avg_monthly_demand", 0.0) or 0.0),
                "sd_hist_monthly": float(row.get("sd_demand_monthly", 0.0) or 0.0),
                "ensemble_forecast": fc_map,
                "planning": {
                    "inventory_position": float(row.get("inventory_position", 0.0) or 0.0),
                    "rop": rop,
                    "target_position": _compute_target_position(rop, order_qty),
                    "order_qty": order_qty,
                    "lead_time_months": lead_time_days / sf.DAYS_PER_MONTH if sf.DAYS_PER_MONTH else 0.0,
                },
                "source": "sf",
            }
        )

        method = row.get("forecast_method", "")
        if method:
            weights.append(
                {
                    "item_number": sku,
                    "weights": {method: 1.0},
                    "rmse": {},
                }
            )

    counts = {
        "source": "sf",
        "processed": len(skus),
    }

    return skus, weights, counts


def _build_sundries_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    output_dir = Path(__file__).resolve().parent

    sku_list_path = output_dir / sundries.SUNDRIES_LIST_FILE
    sundries_sku_list = sundries.load_sundries_list(sku_list_path)

    conn = sundries.connect()
    as_of = sundries._resolve_as_of_date(conn, sundries.AS_OF_DATE_OVERRIDE)
    sundries.AS_OF_DATE = as_of

    single_sku = bool(sundries.FOCUS_SKU)

    if single_sku:
        focus = sundries.FOCUS_SKU.strip().upper()
        active_set, _ = sundries.filter_skus_to_active_last_n_days(
            conn, {focus}, days=365, exclude_sf=True, as_of_date=as_of
        )
        if focus not in active_set:
            conn.close()
            return [], [], {"source": "sundries", "processed": 0}
        sundries_sku_list = {focus}
    else:
        active_set, _ = sundries.filter_skus_to_active_last_n_days(
            conn, sundries_sku_list, days=365, exclude_sf=True, as_of_date=as_of
        )
        sundries_sku_list = active_set
        if not sundries_sku_list:
            conn.close()
            return [], [], {"source": "sundries", "processed": 0}

    sku_master = sundries._fetch_sku_master(conn, sundries.FOCUS_SKU, sundries.CUTOFF_DATE, sundries_sku_list)
    if sku_master.empty:
        conn.close()
        return [], [], {"source": "sundries", "processed": 0}

    sku_list_for_volume = sku_master["sku"].astype(str).str.upper().tolist()
    vol_map, _ = sundries.load_trailing_12m_volume_sundries(
        conn, sku_list_for_volume, as_of_date=as_of
    )
    abc_map = sundries.compute_abc_classification(sku_master, vol_map)

    results: List[Dict[str, Any]] = []
    all_monthly: List[pd.DataFrame] = []

    for _, row in sku_master.iterrows():
        sku = row["sku"]
        abc_class = abc_map.get(sku, "C")
        result = sundries.process_single_sku(conn, sku, row, abc_class, as_of)
        if result:
            mp_rows = result.get("monthly_projections", [])
            results.append({k: v for k, v in result.items() if k not in ("monthly_projections", "forecast_series")})
            if mp_rows:
                mp_df = pd.DataFrame(mp_rows)
                mp_df["SKU"] = sku
                all_monthly.append(mp_df)

    conn.close()

    if not results:
        return [], [], {"source": "sundries", "processed": 0}

    df_results = pd.DataFrame(results)
    df_monthly = pd.concat(all_monthly, ignore_index=True) if all_monthly else pd.DataFrame()

    skus: List[Dict[str, Any]] = []
    weights: List[Dict[str, Any]] = []

    for row in df_results.to_dict(orient="records"):
        sku = row.get("sku", "")
        if not df_monthly.empty and "SKU" in df_monthly.columns:
            sku_monthly = df_monthly[df_monthly["SKU"] == sku]
        else:
            sku_monthly = pd.DataFrame()
        fc_map = _monthly_forecast_map(sku_monthly, "Month", "Forecast")

        rop = float(row.get("reorder_point", 0.0) or 0.0)
        order_up_to = float(row.get("order_up_to_level", 0.0) or 0.0)
        lead_time_days = float(row.get("lead_time_days", 0.0) or 0.0)

        skus.append(
            {
                "item_number": sku,
                "description": row.get("description", ""),
                "vendor_number": row.get("vendor_number", ""),
                "vendor_name": row.get("vendor_name", ""),
                "available_sf": float(row.get("available", 0.0) or 0.0),
                "on_po_sf": float(row.get("on_order", 0.0) or 0.0),
                "backorder_sf": float(row.get("backorder", 0.0) or 0.0),
                "lead_time_days": lead_time_days,
                "mean_hist_monthly": float(row.get("mean_weekly_demand", 0.0) or 0.0) * sundries.WEEKS_PER_MONTH,
                "sd_hist_monthly": 0.0,
                "ensemble_forecast": fc_map,
                "planning": {
                    "inventory_position": float(row.get("inventory_position", 0.0) or 0.0),
                    "rop": rop,
                    "target_position": _compute_target_position(rop, order_up_to - rop),
                    "order_qty": order_up_to,
                    "lead_time_months": lead_time_days / sundries.DAYS_PER_MONTH if sundries.DAYS_PER_MONTH else 0.0,
                },
                "source": "sundries",
                "uom": row.get("uom", "UNITS"),
            }
        )

        method = row.get("forecast_method", "")
        if method:
            weights.append(
                {
                    "item_number": sku,
                    "weights": {method: 1.0},
                    "rmse": {},
                }
            )

    counts = {
        "source": "sundries",
        "processed": len(skus),
    }

    return skus, weights, counts


def _forecast_meta(all_skus: List[Dict[str, Any]]) -> Tuple[Optional[str], int]:
    if not all_skus:
        return None, 0

    min_date: Optional[pd.Timestamp] = None
    max_len = 0
    for sku in all_skus:
        fc = sku.get("ensemble_forecast", {}) or {}
        if not fc:
            continue
        dates = [pd.Timestamp(d) for d in fc.keys()]
        if dates:
            sku_min = min(dates)
            if min_date is None or sku_min < min_date:
                min_date = sku_min
            max_len = max(max_len, len(dates))

    return _iso_date(min_date) if min_date is not None else None, max_len


def build_package(include_sf: bool, include_sundries: bool) -> Dict[str, Any]:
    all_skus: List[Dict[str, Any]] = []
    all_weights: List[Dict[str, Any]] = []
    counts: Dict[str, Any] = {}
    sources: List[str] = []

    if include_sf:
        skus, weights, sf_counts = _build_sf_data()
        all_skus.extend(skus)
        all_weights.extend(weights)
        counts["sf"] = sf_counts.get("processed", 0)
        sources.append("Lead Times.xlsx:Final")

    if include_sundries:
        skus, weights, sundries_counts = _build_sundries_data()
        all_skus.extend(skus)
        all_weights.extend(weights)
        counts["sundries"] = sundries_counts.get("processed", 0)
        sources.append("IMLT")

    forecast_start_date, horizon_months = _forecast_meta(all_skus)

    run_meta = {
        "run_timestamp_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "forecast_start_date": forecast_start_date or "",
        "forecast_horizon_months": horizon_months,
        "service_level": sf.SERVICE_LEVEL if include_sf else sundries.SERVICE_LEVEL,
        "lead_time_source": ", ".join(sources),
    }

    return {
        "run_meta": run_meta,
        "counts": {
            "total_skus": len(all_skus),
            **counts,
        },
        "skus": all_skus,
        "ensemble_weights": all_weights,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export forecast outputs to forecast_package.json for Streamlit."
    )
    parser.add_argument(
        "--output",
        default="forecast_package.json",
        help="Output JSON filename (default: forecast_package.json)",
    )
    parser.add_argument(
        "--sf",
        action="store_true",
        help="Include OMPforecasting5 (SF) output",
    )
    parser.add_argument(
        "--sundries",
        action="store_true",
        help="Include sundries (sundrieswebapp_refactored) output",
    )
    args = parser.parse_args()

    include_sf = args.sf or (not args.sf and not args.sundries)
    include_sundries = args.sundries or (not args.sf and not args.sundries)

    package = build_package(include_sf=include_sf, include_sundries=include_sundries)

    out_path = Path(args.output).resolve()
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(package, f, indent=2)

    print(f"Saved JSON: {out_path}")


if __name__ == "__main__":
    main()
