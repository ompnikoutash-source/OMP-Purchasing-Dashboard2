"""
Standalone Niko optimizer export.

Loads the saved flooringwebappJSON payload and the current Unfinished Pricing.xlsx,
applies SKU consolidation groups, runs the vendor cost optimizer, and writes a
multi-sheet Excel workbook you can review without rerunning the full forecast.

Usage:
    python export_optimizer_results.py [output_path]

    output_path defaults to  optimizer_results_YYYYMMDD_HHMM.xlsx
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from core.niko_pricing import load_niko_pricing_inputs
from core.vendor_optimizer import (
    PurchaseRequest,
    VendorQuote,
    normalize_item_key,
    optimize_vendor_mix,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO = Path(__file__).parent
PAYLOAD_PATH = REPO / "flooringwebappJSON"
PRICING_PATH = REPO / "Unfinished Pricing.xlsx"
STRIP_SKU_LIST_PATH = REPO / "StripSKUList.xlsx"
TRUCK_CAPACITY_SF = 18_000.0
ALLOW_PALLET_UPLIFT = False
DEFAULT_PALLET_SF = 900.0
# Items (after consolidation) with reorder qty below this threshold are omitted —
# rounding up to a full pallet on a tiny order isn't cost-effective.
MIN_REORDER_QTY_SF = 500.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_strip_skus_and_groups() -> tuple[set[str], Dict[str, List[str]]]:
    """Return (strip_skus, groups) where groups maps 'SKU1/SKU2' -> [SKU1, SKU2]."""
    who = pd.read_excel(STRIP_SKU_LIST_PATH, sheet_name="Who Produces")
    strip_skus = set(who["Item Number"].astype(str).str.strip().str.upper())

    combo = pd.read_excel(STRIP_SKU_LIST_PATH, sheet_name="Strip SKU Combination")
    groups: Dict[str, List[str]] = {}
    for _idx, grp in combo.groupby("INDEX"):
        skus = [
            str(v).strip().upper()
            for v in grp["Item Number"]
            if str(v).strip() and str(v).strip().upper() != "NAN"
        ]
        if len(skus) >= 2:
            key = "/".join(skus)
            groups[key] = skus
    return strip_skus, groups


def _resolve_prices(
    sku_or_group: str,
    members: List[str],
    prices_by_sku: Dict[str, Dict[str, float]],
) -> Dict[str, float]:
    """
    Look up vendor prices for a SKU or group key.

    Priority:
    1. Exact group / standalone key
    2. Union of individual member prices (take first encountered price per vendor)
    """
    key = normalize_item_key(sku_or_group)
    direct = prices_by_sku.get(key, {})
    if direct:
        return dict(direct)

    # Try each member individually
    merged: Dict[str, float] = {}
    for member in members:
        mkey = normalize_item_key(member)
        for vendor, price in prices_by_sku.get(mkey, {}).items():
            if vendor not in merged:
                merged[vendor] = price
    return merged


def _build_purchase_requests(
    reorder_rows: pd.DataFrame,
    groups: Dict[str, List[str]],
    pricing: Dict[str, Any],
) -> tuple[List[PurchaseRequest], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Consolidate member SKUs into group rows, look up prices, and apply the
    minimum-qty threshold.  Returns (requests_with_prices, no_price_items, too_small_items).

    too_small_items: consolidated items whose total qty < MIN_REORDER_QTY_SF.
    no_price_items:  items that meet the qty threshold but have no vendor quotes.
    """
    prices_by_sku = pricing["prices_by_sku"]
    freight_costs = pricing["freight_costs"]

    # Map individual SKU -> group key (for members)
    sku_to_group: Dict[str, str] = {
        sku: gkey for gkey, members in groups.items() for sku in members
    }

    # Aggregate quantities: standalone + consolidated groups
    consolidated: Dict[str, Dict[str, Any]] = {}
    for _, row in reorder_rows.iterrows():
        sku = str(row["SKU"]).strip().upper()
        qty = float(row["OrderQty"])
        desc = str(row.get("description", "")).strip()

        group_key = sku_to_group.get(sku)
        if group_key:
            if group_key not in consolidated:
                consolidated[group_key] = {"qty": 0.0, "description": desc, "members": groups[group_key]}
            consolidated[group_key]["qty"] += qty
        else:
            if sku not in consolidated:
                consolidated[sku] = {"qty": 0.0, "description": desc, "members": [sku]}
            consolidated[sku]["qty"] += qty

    requests: List[PurchaseRequest] = []
    no_price: List[Dict[str, Any]] = []
    too_small: List[Dict[str, Any]] = []

    for sku_key, info in consolidated.items():
        qty = info["qty"]
        members = info["members"]
        desc = info["description"]

        # Drop items below the minimum order threshold — rounding up to a full
        # pallet on a tiny order isn't cost-effective.
        if qty < MIN_REORDER_QTY_SF:
            too_small.append({
                "SKU / Group": sku_key,
                "Description": desc,
                "Qty Needed (SF)": round(qty),
                "Members": ", ".join(m for m in members if m != sku_key),
                "Note": f"Omitted — qty {qty:.0f} SF is below {MIN_REORDER_QTY_SF:.0f} SF minimum",
            })
            continue

        raw_prices = _resolve_prices(sku_key, members, prices_by_sku)
        if not raw_prices:
            no_price.append({
                "SKU / Group": sku_key,
                "Description": desc,
                "Qty Needed (SF)": round(qty),
                "Members": ", ".join(m for m in members if m != sku_key),
                "Note": "No vendor quotes — excluded from optimization",
            })
            continue

        vendor_quotes: Dict[str, VendorQuote] = {}
        for vendor, price in raw_prices.items():
            vendor_quotes[vendor] = VendorQuote(
                vendor=vendor,
                material_price=price,
                freight_per_truck=float(freight_costs.get(vendor, 0.0)),
            )

        requests.append(
            PurchaseRequest(
                sku=sku_key,
                qty_sf=qty,
                vendor_quotes=vendor_quotes,
                description=desc,
                pallet_sf=DEFAULT_PALLET_SF,
            )
        )

    return requests, no_price, too_small


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def _write_workbook(
    out_path: Path,
    result: Dict[str, Any],
    requests: List[PurchaseRequest],
    no_price: List[Dict[str, Any]],
    too_small: List[Dict[str, Any]],
    pricing: Dict[str, Any],
    run_ts: str,
) -> None:
    with pd.ExcelWriter(str(out_path), engine="openpyxl") as writer:
        _write_summary_sheet(writer, result, run_ts, len(no_price), len(too_small), pricing)
        _write_item_results_sheet(writer, result, requests)
        _write_vendor_summary_sheet(writer, result)
        _write_inputs_sheet(writer, requests, pricing)
        if no_price:
            pd.DataFrame(no_price).to_excel(writer, sheet_name="Missing Prices", index=False)
        if too_small:
            pd.DataFrame(too_small).to_excel(writer, sheet_name="Below Min Qty", index=False)


def _fmt_currency(val: float) -> str:
    return f"${val:,.2f}"


def _write_summary_sheet(
    writer: pd.ExcelWriter,
    result: Dict[str, Any],
    run_ts: str,
    n_missing: int,
    n_too_small: int,
    pricing: Dict[str, Any],
) -> None:
    freight_costs = pricing.get("freight_costs", {})
    vendor_summary = result.get("vendor_summary", {})

    # Build a per-vendor freight audit so the user can see exactly where each
    # truckload rate comes from (and which vendors have no rate yet).
    freight_audit_rows = []
    for vendor in sorted(set(list(vendor_summary.keys()) + list(freight_costs.keys()))):
        rate = freight_costs.get(vendor)
        in_solution = vendor in vendor_summary
        trucks = vendor_summary.get(vendor, {}).get("truckloads", 0)
        freight_total = vendor_summary.get(vendor, {}).get("freight_total", 0.0)
        if rate is None or rate == 0:
            note = "No rate — freight charged as $0.  Add to Freight tab to include."
        else:
            note = f"${rate:,.0f}/truck x {trucks} truck(s) = ${freight_total:,.0f}"
        freight_audit_rows.append({
            "Vendor":           vendor,
            "Rate ($/truck)":   f"${rate:,.0f}" if (rate and rate > 0) else "$0 (missing)",
            "Trucks in solution": trucks if in_solution else "—",
            "Freight charged":  _fmt_currency(freight_total) if in_solution else "—",
            "Note":             note,
        })

    rows = [
        {"Metric": "Forecast run date",                  "Value": run_ts},
        {"Metric": "Export generated",                   "Value": datetime.now().strftime("%Y-%m-%d %H:%M")},
        {"Metric": "",                                   "Value": ""},
        {"Metric": "Items optimized",                    "Value": len(result.get("item_costs", {}))},
        {"Metric": f"Below {MIN_REORDER_QTY_SF:.0f} SF minimum (omitted)", "Value": n_too_small},
        {"Metric": "No vendor prices (excluded)",        "Value": n_missing},
        {"Metric": "",                                   "Value": ""},
        {"Metric": "Truck capacity (SF)",                "Value": TRUCK_CAPACITY_SF},
        {"Metric": "Min reorder qty (SF)",               "Value": MIN_REORDER_QTY_SF},
        {"Metric": "",                                   "Value": ""},
        {"Metric": "Total Material Cost",                "Value": _fmt_currency(result.get("total_material_cost", 0.0))},
        {"Metric": "Total Freight Cost",                 "Value": _fmt_currency(result.get("total_freight_cost", 0.0))},
        {"Metric": "Grand Total",                        "Value": _fmt_currency(result.get("total_cost", 0.0))},
        {"Metric": "",                                   "Value": ""},
        {"Metric": "--- Freight Rate Audit ---",         "Value": ""},
    ]
    for fa in freight_audit_rows:
        rows.append({"Metric": fa["Vendor"], "Value": fa["Note"]})

    pd.DataFrame(rows).to_excel(writer, sheet_name="Summary", index=False)


def _write_item_results_sheet(
    writer: pd.ExcelWriter,
    result: Dict[str, Any],
    requests: List[PurchaseRequest],
) -> None:
    item_costs = result.get("item_costs", {})
    if not item_costs:
        pd.DataFrame(columns=["SKU / Group", "Vendor", "Qty (SF)", "Material Price ($/SF)",
                               "Freight Share ($/SF)", "Landed Cost ($/SF)", "Line Total ($)"]).to_excel(
            writer, sheet_name="Item Results", index=False)
        return

    request_map = {req.sku: req for req in requests}
    rows = []
    for sku, item in sorted(item_costs.items(), key=lambda x: x[0]):
        req = request_map.get(sku)
        all_quotes = {}
        if req:
            all_quotes = {v: q.material_price for v, q in req.vendor_quotes.items()}
        price_comparison = " | ".join(
            f"{v}: ${p:.2f}" + (" ✓" if v == item.get("vendor") else "")
            for v, p in sorted(all_quotes.items(), key=lambda x: x[1])
        )
        rows.append({
            "SKU / Group":           item.get("sku", sku),
            "Description":           item.get("description", ""),
            "Chosen Vendor":         item.get("vendor", ""),
            "Qty Needed (SF)":       round(item.get("base_qty", 0.0)),
            "Final Qty (SF)":        round(item.get("qty", 0.0)),
            "Pallets Added":         item.get("pallets_added", 0),
            "Material ($/SF)":       item.get("price", 0.0),
            "Freight Share ($/SF)":  round(item.get("freight_share", 0.0), 4),
            "Landed ($/SF)":         round(item.get("landed_cost", 0.0), 4),
            "Line Total ($)":        round(item.get("line_total", 0.0), 2),
            "Truck #":               ", ".join(str(t) for t in item.get("truck_labels", [])),
            "All Vendor Quotes":     price_comparison,
        })

    df = pd.DataFrame(rows)
    df.to_excel(writer, sheet_name="Item Results", index=False)


def _write_vendor_summary_sheet(
    writer: pd.ExcelWriter,
    result: Dict[str, Any],
) -> None:
    vendor_summary = result.get("vendor_summary", {})
    if not vendor_summary:
        pd.DataFrame().to_excel(writer, sheet_name="Vendor Summary", index=False)
        return

    rows = []
    for vendor, vs in sorted(vendor_summary.items()):
        rows.append({
            "Vendor":               vendor,
            "Total Qty (SF)":       round(vs.get("qty_sf", 0.0)),
            "Truckloads":           vs.get("truckloads", 0),
            "Truck Fill %":         f"{vs.get('truck_fill_pct', 0.0):.1%}",
            "Freight / Truck ($)":  _fmt_currency(vs.get("freight_per_truck", 0.0)),
            "Total Freight ($)":    _fmt_currency(vs.get("freight_total", 0.0)),
            "Freight / SF ($)":     round(vs.get("freight_per_sf", 0.0), 4),
            "Material Total ($)":   _fmt_currency(vs.get("material_total", 0.0)),
            "Avg Material ($/SF)":  round(vs.get("avg_material_price", 0.0), 4),
            "Landed Total ($)":     _fmt_currency(vs.get("landed_total", 0.0)),
            "Avg Landed ($/SF)":    round(vs.get("avg_landed_cost", 0.0), 4),
            "Added Qty (SF)":       round(vs.get("added_qty_sf", 0.0)),
        })

    # Totals row
    total_material = result.get("total_material_cost", 0.0)
    total_freight = result.get("total_freight_cost", 0.0)
    rows.append({
        "Vendor": "TOTAL",
        "Total Qty (SF)": round(sum(v.get("qty_sf", 0.0) for v in vendor_summary.values())),
        "Truckloads": sum(v.get("truckloads", 0) for v in vendor_summary.values()),
        "Truck Fill %": "",
        "Freight / Truck ($)": "",
        "Total Freight ($)": _fmt_currency(total_freight),
        "Freight / SF ($)": "",
        "Material Total ($)": _fmt_currency(total_material),
        "Avg Material ($/SF)": "",
        "Landed Total ($)": _fmt_currency(total_material + total_freight),
        "Avg Landed ($/SF)": "",
        "Added Qty (SF)": "",
    })

    pd.DataFrame(rows).to_excel(writer, sheet_name="Vendor Summary", index=False)


def _write_inputs_sheet(
    writer: pd.ExcelWriter,
    requests: List[PurchaseRequest],
    pricing: Dict[str, Any],
) -> None:
    """Sheet showing the raw inputs fed to the optimizer."""
    all_vendors = sorted({v for req in requests for v in req.vendor_quotes})
    rows = []
    for req in sorted(requests, key=lambda r: r.sku):
        row: Dict[str, Any] = {
            "SKU / Group":    req.sku,
            "Description":    req.description,
            "Qty Needed (SF)": round(req.qty_sf),
        }
        for vendor in all_vendors:
            quote = req.vendor_quotes.get(vendor)
            row[f"{vendor} ($/SF)"] = quote.material_price if quote else ""
        freight_note = " | ".join(
            f"{v}: ${pricing['freight_costs'][v]:,.0f}/truck"
            for v in all_vendors
            if v in pricing["freight_costs"]
        )
        row["Freight Rates"] = freight_note
        rows.append(row)
    pd.DataFrame(rows).to_excel(writer, sheet_name="Optimizer Inputs", index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    # --- Load pricing ---
    print(f"Loading pricing from {PRICING_PATH.name} …")
    pricing = load_niko_pricing_inputs(PRICING_PATH)
    print(f"  Vendors: {pricing['vendor_names']}")
    print(f"  Freight: {pricing['freight_costs']}")
    for w in pricing["warnings"]:
        print(f"  [warn] {w}")

    # --- Load payload ---
    if not PAYLOAD_PATH.exists():
        print(f"ERROR: {PAYLOAD_PATH} not found. Run the forecast first.")
        sys.exit(1)
    print(f"Loading saved payload from {PAYLOAD_PATH.name} …")
    payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    run_ts = payload["run_meta"].get("run_timestamp_local", "unknown")
    print(f"  Forecast run: {run_ts}")

    # --- Strip SKUs and groups ---
    strip_skus, groups = _load_strip_skus_and_groups()
    print(f"  Strip SKUs: {len(strip_skus)}   Consolidation groups: {len(groups)}")

    # --- Monthly projections → reorder rows ---
    run_month = pd.Timestamp(run_ts).to_period("M").to_timestamp()
    monthly = pd.DataFrame(payload["Monthly_Projections"])
    monthly["Month"] = pd.to_datetime(monthly["Month"], errors="coerce")

    # Pull description from Inventory_Metrics
    desc_map = {
        str(m.get("sku", m.get("item_number", ""))).strip().upper(): str(m.get("description", "")).strip()
        for m in payload["Inventory_Metrics"]
    }

    strip_monthly = monthly[monthly["SKU"].astype(str).str.strip().str.upper().isin(strip_skus)].copy()
    strip_monthly["SKU"] = strip_monthly["SKU"].astype(str).str.strip().str.upper()
    strip_monthly["OrderQty"] = pd.to_numeric(strip_monthly["Order Quantity"], errors="coerce").fillna(0.0)
    strip_monthly["description"] = strip_monthly["SKU"].map(desc_map).fillna("")
    strip_monthly = strip_monthly[strip_monthly["Month"] == run_month]
    reorder_rows = strip_monthly[strip_monthly["OrderQty"] > 0].copy()

    print(f"  Run month: {run_month.strftime('%B %Y')}")
    print(f"  Strip SKUs with reorder qty: {reorder_rows['SKU'].nunique()}")

    # --- Build purchase requests ---
    requests, no_price, too_small = _build_purchase_requests(reorder_rows, groups, pricing)
    print(f"  Items for optimizer: {len(requests)}   Below {MIN_REORDER_QTY_SF:.0f} SF min: {len(too_small)}   No prices: {len(no_price)}")
    if too_small:
        print(f"  Omitted (below {MIN_REORDER_QTY_SF:.0f} SF):")
        for item in too_small:
            print(f"    {item['SKU / Group']}: {item['Qty Needed (SF)']:,.0f} SF")
    if no_price:
        print("  Without prices:")
        for item in no_price:
            print(f"    {item['SKU / Group']}: {item['Qty Needed (SF)']:,.0f} SF")

    if not requests:
        print("ERROR: No items have vendor quotes — cannot run optimizer.")
        sys.exit(1)

    # --- Run optimizer ---
    print("Running optimizer …")
    result = optimize_vendor_mix(
        requests,
        truck_capacity_sf=TRUCK_CAPACITY_SF,
        allow_pallet_uplift=ALLOW_PALLET_UPLIFT,
    )

    if result["status"] != "ok":
        print("Optimizer errors:")
        for err in result.get("errors", []):
            print(f"  {err}")
        sys.exit(1)

    print(f"  Optimal solution found.")
    print(f"  Material: ${result['total_material_cost']:,.2f}")
    print(f"  Freight:  ${result['total_freight_cost']:,.2f}")
    print(f"  Grand total: ${result['total_cost']:,.2f}")
    for vendor, vs in sorted(result["vendor_summary"].items()):
        print(f"    {vendor}: {vs['qty_sf']:,.0f} SF, {vs['truckloads']} truck(s), "
              f"${vs['landed_total']:,.2f} landed")

    # --- Export ---
    if out_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = REPO / f"optimizer_results_{ts}.xlsx"

    print(f"Writing {out_path.name} …")
    _write_workbook(out_path, result, requests, no_price, too_small, pricing, run_ts)
    print(f"Done: {out_path}")


if __name__ == "__main__":
    main()
