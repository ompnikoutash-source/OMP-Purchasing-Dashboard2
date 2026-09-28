from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

WEEKS_PER_MONTH = 30.4 / 7.0


@dataclass
class SkuSnapshot:
    dataset: str
    sku: str
    description: str
    method: str
    demand_class: str
    abc_class: str
    uom: str
    inventory_position: float
    available: float
    on_order: float
    backorder: float
    safety_stock: float
    reorder_point: float
    order_up_to_level: float
    daily_demand: float
    lead_time_days: float
    lead_time_source: str
    weekly_hist: np.ndarray
    weekly_fc: np.ndarray
    monthly_hist: np.ndarray
    monthly_fc: np.ndarray
    catchup_order_qty: float
    source_file: str
    weekly_fc_dates: List[str] = None       # ISO dates aligned to weekly_fc, when available
    monthly_fc_dates: List[str] = None      # ISO dates aligned to monthly_fc, when available

    def __post_init__(self):
        if self.weekly_fc_dates is None:
            self.weekly_fc_dates = []
        if self.monthly_fc_dates is None:
            self.monthly_fc_dates = []

    @property
    def weekly_fc_mean(self) -> float:
        if self.weekly_fc.size:
            return float(np.nanmean(self.weekly_fc))
        if self.monthly_fc.size:
            return float(np.nanmean(self.monthly_fc) / WEEKS_PER_MONTH)
        return 0.0

    @property
    def weekly_recent_mean(self) -> float:
        if self.weekly_hist.size:
            n = min(26, self.weekly_hist.size)
            return float(np.nanmean(self.weekly_hist[-n:]))
        if self.monthly_hist.size:
            n = min(6, self.monthly_hist.size)
            return float(np.nanmean(self.monthly_hist[-n:]) / WEEKS_PER_MONTH)
        return 0.0

    @property
    def weekly_recent_sum(self) -> float:
        if self.weekly_hist.size:
            n = min(26, self.weekly_hist.size)
            return float(np.nansum(self.weekly_hist[-n:]))
        if self.monthly_hist.size:
            n = min(6, self.monthly_hist.size)
            # Approximate 26-week sum from recent 6 months.
            monthly_sum = float(np.nansum(self.monthly_hist[-n:]))
            return monthly_sum * (26.0 / (n * WEEKS_PER_MONTH))
        return 0.0

    @property
    def trailing_zero_weeks(self) -> int:
        if self.weekly_hist.size == 0:
            return 0
        count = 0
        for val in self.weekly_hist[::-1]:
            if float(val) == 0.0:
                count += 1
            else:
                break
        return count


def load_config(config_path: Path) -> Dict:
    """
    Load a config file written as JSON (also valid YAML 1.2 syntax).
    Falls back to PyYAML if available.
    """
    text = config_path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                f"Failed to parse {config_path}. Install pyyaml or keep config JSON-compatible."
            ) from exc
        return yaml.safe_load(text)


def _to_float(value: object) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _to_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _get_row_value(row: Dict, keys: Iterable[str]) -> object:
    for key in keys:
        if key in row:
            return row.get(key)
    return None


_FALLBACK_SUFFIX_RE = re.compile(r"^(.*?) \(fallback to avg\)(?: \+ cap)?(?: \+ drift_cap)?$")


def split_fallback_method(method: Optional[str]) -> Tuple[str, bool]:
    """Split flooringwebapp.py's `select_best_forecast()` naming convention
    (`"{winning_method} (fallback to avg)[ + cap][ + drift_cap]"`, applied at
    the point where the CV-winning method's own forecast is discarded in
    favor of a capped historical average, lines ~2463-2500) into the method
    that actually WON cross-validated scoring vs. whether its own forecast
    values were used or replaced by the fallback average.

    Without this split, e.g. "XGBoost (Global) (fallback to avg)" reads as an
    XGBoost forecast in accuracy reports when the values behind it are
    actually a historical average -- misattributing that error to XGBoost.

    Native fallback paths that never had a winning candidate at all (e.g.
    "Historical Avg (fallback)", "Historical Avg (fallback capped)", set at a
    separate code path when no candidate survives CV) don't match this
    pattern and pass through unchanged with used_fallback_average=False --
    there's no other method being mislabeled in that case."""
    method = method or ""
    m = _FALLBACK_SUFFIX_RE.match(method)
    if m:
        return m.group(1), True
    return method, False


def _floor_to_month_start(date_str: str) -> str:
    """Floor an ISO-ish date string to its month-start (YYYY-MM-01). Tolerates
    a wrong day-of-month (e.g. a snapshot day carried forward into future
    months by an upstream date-arithmetic bug) since only the year-month is
    ever meaningful as a forecasted period key."""
    if not date_str or len(date_str) < 7:
        return date_str
    return f"{date_str[:7]}-01"


def _series_is_monthly(fs: Dict) -> bool:
    """
    Detect whether forecast_series date axis appears monthly (not weekly).
    """
    if not isinstance(fs, dict):
        return False

    for key in ("fc_x", "x", "hist_x"):
        raw_dates = fs.get(key)
        if not isinstance(raw_dates, list) or len(raw_dates) < 3:
            continue
        parsed = []
        for value in raw_dates:
            try:
                parsed.append(np.datetime64(str(value), "D"))
            except Exception:
                continue
        if len(parsed) < 3:
            continue

        arr = np.asarray(parsed, dtype="datetime64[D]")
        if arr.size < 3:
            continue

        deltas = np.diff(arr).astype("timedelta64[D]").astype(float)
        if deltas.size == 0:
            continue
        median_delta = float(np.nanmedian(deltas))
        if median_delta >= 20.0:
            return True
        if median_delta <= 10.0:
            return False
    return False


def _extract_monthly_by_sku(monthly_rows: List[Dict]) -> Dict[str, List[Dict]]:
    by_sku: Dict[str, List[Dict]] = {}
    for row in monthly_rows:
        sku = _to_str(_get_row_value(row, ("SKU", "sku", "item_number"))).upper()
        if not sku:
            continue
        by_sku.setdefault(sku, []).append(row)
    return by_sku


def _normalize_item_row(dataset: str, row: Dict) -> Dict:
    sku = _to_str(_get_row_value(row, ("item_number", "sku"))).upper()
    desc = _to_str(_get_row_value(row, ("description", "Description")))
    method = _to_str(_get_row_value(row, ("forecast_method", "Forecast Method")))
    demand_class = _to_str(_get_row_value(row, ("demand_class",))).upper()
    abc = _to_str(_get_row_value(row, ("abc_class", "sku_abc"))).upper()
    uom = _to_str(_get_row_value(row, ("uom", "Sales Unit of Measurement", "Sales UOM"))).upper()

    available = _to_float(_get_row_value(row, ("available", "available_sf")))
    on_order = _to_float(_get_row_value(row, ("on_order", "on_po_sf")))
    backorder = _to_float(_get_row_value(row, ("backorder", "backorder_sf")))
    inv_pos = _to_float(_get_row_value(row, ("inventory_position",)))
    ss = _to_float(_get_row_value(row, ("safety_stock",)))
    rop = _to_float(_get_row_value(row, ("reorder_point",)))
    # Flooring often has reorder_quantity instead of order_up_to_level.
    s_level = _to_float(_get_row_value(row, ("order_up_to_level",)))
    reorder_qty = _to_float(_get_row_value(row, ("reorder_quantity",)))
    if s_level <= 0.0 and rop > 0.0 and reorder_qty > 0.0:
        s_level = rop + reorder_qty

    daily = _to_float(_get_row_value(row, ("daily_demand", "daily_mean_demand")))
    lead_time_days = _to_float(_get_row_value(row, ("lead_time_days", "LEAD_TIME_DAYS")))
    lead_time_source = _to_str(_get_row_value(row, ("lead_time_source", "Lead Time Source")))

    fs = row.get("forecast_series") if isinstance(row.get("forecast_series"), dict) else {}
    weekly_hist = np.asarray(fs.get("hist_y", []), dtype=float) if fs else np.asarray([], dtype=float)
    weekly_fc = np.asarray(fs.get("fc_y", []), dtype=float) if fs else np.asarray([], dtype=float)
    weekly_fc_dates: List[str] = list(fs.get("fc_x", [])) if fs else []
    if fs and _series_is_monthly(fs):
        # Flooring payload often stores monthly series in forecast_series keys.
        # Keep weekly arrays empty so downstream logic relies on monthly rows.
        weekly_hist = np.asarray([], dtype=float)
        weekly_fc = np.asarray([], dtype=float)
        weekly_fc_dates = []
    if len(weekly_fc_dates) != weekly_fc.size:
        # Dates/values must line up 1:1 for accuracy tracking; drop dates rather
        # than risk misaligning a value with the wrong target period.
        weekly_fc_dates = []

    return {
        "sku": sku,
        "description": desc,
        "method": method,
        "demand_class": demand_class,
        "abc_class": abc,
        "uom": uom,
        "inventory_position": inv_pos,
        "available": available,
        "on_order": on_order,
        "backorder": backorder,
        "safety_stock": ss,
        "reorder_point": rop,
        "order_up_to_level": s_level,
        "daily_demand": daily,
        "lead_time_days": lead_time_days,
        "lead_time_source": lead_time_source,
        "weekly_hist": weekly_hist,
        "weekly_fc": weekly_fc,
        "weekly_fc_dates": weekly_fc_dates,
        "dataset": dataset,
    }


def parse_snapshot_payload(dataset: str, payload: Dict, source_file: str = "") -> List[SkuSnapshot]:
    """Normalize one already-loaded JSON payload (flooring/sundries/moulding
    shape) into SkuSnapshot rows. Factored out of load_snapshots() so both a
    live file on disk and an in-memory payload (e.g. from `git show
    <sha>:<path>` for historical snapshots) can share the same parsing --
    used by agents/forecast_history_extractor.py to mine git history."""
    snapshots: List[SkuSnapshot] = []
    item_rows = payload.get("Inventory_Metrics", payload.get("items", []))
    payload_items = payload.get("items", [])
    monthly_rows = payload.get("Monthly_Projections", [])
    monthly_by_sku = _extract_monthly_by_sku(monthly_rows)
    items_by_sku: Dict[str, Dict] = {}
    if isinstance(payload_items, list):
        for item in payload_items:
            if not isinstance(item, dict):
                continue
            sku_item = _to_str(_get_row_value(item, ("item_number", "sku"))).upper()
            if sku_item:
                items_by_sku[sku_item] = item

    for raw in item_rows:
        norm = _normalize_item_row(dataset, raw)
        sku = norm["sku"]
        if not sku:
            continue

        # Flooring payload keeps richer forecast series in `items`; reuse it when
        # Inventory_Metrics rows are compact and lack series arrays.
        item_fallback = items_by_sku.get(sku, {})
        if (norm["weekly_hist"].size == 0 or norm["weekly_fc"].size == 0) and isinstance(item_fallback, dict):
            fs_fallback = item_fallback.get("forecast_series", {})
            if isinstance(fs_fallback, dict):
                # Only hydrate weekly arrays when fallback series is actually weekly.
                if not _series_is_monthly(fs_fallback):
                    if norm["weekly_hist"].size == 0:
                        norm["weekly_hist"] = np.asarray(fs_fallback.get("hist_y", []), dtype=float)
                    if norm["weekly_fc"].size == 0:
                        norm["weekly_fc"] = np.asarray(fs_fallback.get("fc_y", []), dtype=float)
                        fc_x_fallback = list(fs_fallback.get("fc_x", []))
                        if len(fc_x_fallback) == norm["weekly_fc"].size:
                            norm["weekly_fc_dates"] = fc_x_fallback
            if norm["daily_demand"] <= 0:
                norm["daily_demand"] = _to_float(
                    _get_row_value(item_fallback, ("daily_avg_demand", "daily_demand", "daily_mean_demand"))
                )
            if norm["lead_time_days"] <= 0:
                norm["lead_time_days"] = _to_float(_get_row_value(item_fallback, ("lead_time_days",)))
            if not norm["lead_time_source"]:
                norm["lead_time_source"] = _to_str(_get_row_value(item_fallback, ("lead_time_source",)))

        sku_monthly = monthly_by_sku.get(sku, [])
        monthly_hist_vals: List[float] = []
        monthly_fc_vals: List[float] = []
        monthly_fc_dates: List[str] = []
        for r in sku_monthly:
            row_type = _to_str(r.get("Row_Type")).upper()
            if row_type in {"HIST", "CATCHUP"}:
                val = _to_float(r.get("Historical Demand"))
                if np.isfinite(val):
                    monthly_hist_vals.append(val)
            if row_type == "FCST":
                val = _to_float(r.get("Forecast"))
                if np.isfinite(val):
                    monthly_fc_vals.append(val)
                    monthly_fc_dates.append(_floor_to_month_start(_to_str(r.get("Month"))))

        monthly_hist = np.asarray(monthly_hist_vals, dtype=float)
        monthly_fc = np.asarray(monthly_fc_vals, dtype=float)
        catchup_order = 0.0
        for row in sku_monthly:
            if _to_str(row.get("Row_Type")).upper() == "CATCHUP":
                catchup_order = _to_float(row.get("Order Quantity"))
                break

        snapshots.append(
            SkuSnapshot(
                dataset=dataset,
                sku=sku,
                description=norm["description"],
                method=norm["method"],
                demand_class=norm["demand_class"],
                abc_class=norm["abc_class"],
                uom=norm["uom"],
                inventory_position=norm["inventory_position"],
                available=norm["available"],
                on_order=norm["on_order"],
                backorder=norm["backorder"],
                safety_stock=norm["safety_stock"],
                reorder_point=norm["reorder_point"],
                order_up_to_level=norm["order_up_to_level"],
                daily_demand=norm["daily_demand"],
                lead_time_days=norm["lead_time_days"],
                lead_time_source=norm["lead_time_source"],
                weekly_hist=norm["weekly_hist"],
                weekly_fc=norm["weekly_fc"],
                monthly_hist=monthly_hist,
                monthly_fc=monthly_fc,
                catchup_order_qty=catchup_order,
                source_file=source_file,
                weekly_fc_dates=norm.get("weekly_fc_dates") or [],
                monthly_fc_dates=monthly_fc_dates,
            )
        )

    return snapshots


def load_snapshots(
    base_dir: Path,
    dataset_paths: Dict[str, str],
    datasets: Optional[Iterable[str]] = None,
) -> Tuple[List[SkuSnapshot], Dict[str, Dict]]:
    selected = set(datasets) if datasets else set(dataset_paths.keys())
    snapshots: List[SkuSnapshot] = []
    run_meta: Dict[str, Dict] = {}

    for dataset, rel_path in dataset_paths.items():
        if dataset not in selected:
            continue
        path = (base_dir / rel_path).resolve()
        if not path.exists():
            continue

        payload = json.loads(path.read_text(encoding="utf-8"))
        run_meta[dataset] = payload.get("run_meta", {})
        snapshots.extend(parse_snapshot_payload(dataset, payload, source_file=str(path)))

    return snapshots, run_meta
