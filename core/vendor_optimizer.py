from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from ortools.linear_solver import pywraplp
except Exception:
    pywraplp = None


TRUCKLOAD_CAPACITY_SF = 15_000.0
DEFAULT_PALLET_SF = 900.0
MAX_PALLET_UPLIFT_PCT = 0.20
PRICE_ALERT_PCT_THRESHOLD = 0.10
PRICE_ALERT_ABS_THRESHOLD = 0.15
MIN_PURCHASABLE_QTY_SF = 500.0
MIN_FULL_PALLET_QTY_SF = 900.0
_TRUCKLOAD_TIEBREAKER = 1e-6


def normalize_item_key(value: Any) -> str:
    """Normalize workbook and dashboard SKU/group keys into a common form."""
    if value is None:
        return ""
    text = str(value).strip().upper()
    if not text or text == "NAN":
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in text.replace("\n", "/").split("/") if part.strip() and part.strip().upper() != "NAN"]
    return "/".join(parts)


def normalize_vendor_name(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def _sanitize_pallet_sf(value: Any, default: float = DEFAULT_PALLET_SF) -> float:
    try:
        pallet_sf = float(value)
    except Exception:
        pallet_sf = default
    if not math.isfinite(pallet_sf) or pallet_sf <= 0:
        return float(default)
    return float(pallet_sf)


@dataclass(frozen=True)
class VendorQuote:
    vendor: str
    material_price: float
    freight_per_truck: float = 0.0


@dataclass(frozen=True)
class PurchaseRequest:
    sku: str
    qty_sf: float
    vendor_quotes: Dict[str, VendorQuote]
    description: str = ""
    pallet_sf: float = DEFAULT_PALLET_SF
    original_qty_sf: Optional[float] = None
    # Per-vendor constraint data from the pricing spreadsheet.
    # Keys are canonical vendor names; fall back to scalar pallet_sf /
    # global truck_capacity_sf when a vendor is absent.
    pallet_sf_by_vendor: Optional[Dict[str, float]] = None
    container_sf_by_vendor: Optional[Dict[str, float]] = None


def apply_minimum_order_policy(
    requests: List[PurchaseRequest],
    omit_below_sf: float = MIN_PURCHASABLE_QTY_SF,
    round_up_above_sf: float = MIN_PURCHASABLE_QTY_SF,
    minimum_order_sf: float = MIN_FULL_PALLET_QTY_SF,
) -> tuple[List[PurchaseRequest], List[Dict[str, Any]]]:
    """
    Apply a simple purchasing policy before optimization.

    - Lines below ``omit_below_sf`` are omitted from the optimizer entirely.
    - Lines above ``round_up_above_sf`` but below ``minimum_order_sf`` are
      rounded up to the minimum pallet buy.

    Returns the normalized requests plus a list of omitted-line diagnostics.
    """
    normalized: List[PurchaseRequest] = []
    omitted_items: List[Dict[str, Any]] = []

    for request in requests:
        raw_qty = getattr(request, "original_qty_sf", None)
        try:
            original_qty = float(raw_qty if raw_qty is not None else request.qty_sf)
        except Exception:
            original_qty = float(request.qty_sf or 0.0)
        if not math.isfinite(original_qty) or original_qty <= 0:
            continue

        if omit_below_sf > 0 and original_qty < omit_below_sf:
            omitted_items.append(
                {
                    "sku": normalize_item_key(request.sku),
                    "description": request.description,
                    "original_qty": original_qty,
                    "reason": f"Below {omit_below_sf:,.0f} SF minimum",
                }
            )
            continue

        effective_qty = original_qty
        if (
            minimum_order_sf > 0
            and round_up_above_sf > 0
            and original_qty > round_up_above_sf
            and original_qty < minimum_order_sf
        ):
            effective_qty = float(minimum_order_sf)

        normalized.append(
            PurchaseRequest(
                sku=request.sku,
                qty_sf=effective_qty,
                vendor_quotes=request.vendor_quotes,
                description=request.description,
                pallet_sf=request.pallet_sf,
                original_qty_sf=original_qty,
            )
        )

    return normalized, omitted_items


def _clean_requests(requests: List[PurchaseRequest], default_pallet_sf: float) -> List[PurchaseRequest]:
    cleaned_requests: List[PurchaseRequest] = []
    for request in requests:
        sku = normalize_item_key(request.sku)
        qty = float(request.qty_sf or 0.0)
        if not sku or qty <= 0:
            continue
        raw_original_qty = getattr(request, "original_qty_sf", None)
        try:
            original_qty = float(raw_original_qty if raw_original_qty is not None else qty)
        except Exception:
            original_qty = qty
        if not math.isfinite(original_qty) or original_qty <= 0:
            original_qty = qty
        quotes: Dict[str, VendorQuote] = {}
        for vendor_name, quote in (request.vendor_quotes or {}).items():
            vendor = normalize_vendor_name(vendor_name)
            if not vendor:
                continue
            try:
                material_price = float(quote.material_price)
            except Exception:
                continue
            if not math.isfinite(material_price) or material_price <= 0:
                continue
            freight_per_truck = float(max(0.0, quote.freight_per_truck or 0.0))
            quotes[vendor] = VendorQuote(
                vendor=vendor,
                material_price=material_price,
                freight_per_truck=freight_per_truck,
            )
        if quotes:
            cleaned_requests.append(
                PurchaseRequest(
                    sku=sku,
                    qty_sf=qty,
                    vendor_quotes=quotes,
                    description=request.description,
                    pallet_sf=_sanitize_pallet_sf(getattr(request, "pallet_sf", default_pallet_sf), default_pallet_sf),
                    original_qty_sf=original_qty,
                    pallet_sf_by_vendor=getattr(request, "pallet_sf_by_vendor", None),
                    container_sf_by_vendor=getattr(request, "container_sf_by_vendor", None),
                )
            )
    return cleaned_requests


def _solve_vendor_assignments(
    requests: List[PurchaseRequest],
    truck_capacity_sf: float,
    vendor_penalties: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    if pywraplp is None:
        raise RuntimeError("ortools is not available, so the vendor optimizer cannot run.")
    if truck_capacity_sf <= 0:
        raise ValueError("truck_capacity_sf must be positive.")

    solver = pywraplp.Solver.CreateSolver("SCIP")
    if solver is None:
        raise RuntimeError("Failed to initialize the mixed-integer solver.")

    all_vendors = sorted({vendor for req in requests for vendor in req.vendor_quotes})

    assign_vars: Dict[tuple[int, str], Any] = {}
    truck_vars: Dict[str, Any] = {}

    # Per-vendor upper bound: worst case is all items assigned to that vendor.
    # Use per-SKU container sizes where available; fall back to global default.
    vendor_max_trucks: Dict[str, int] = {}
    for vendor in all_vendors:
        vendor_max_trucks[vendor] = max(
            1,
            sum(
                int(math.ceil(
                    req.qty_sf / (
                        (req.container_sf_by_vendor or {}).get(vendor) or truck_capacity_sf
                    )
                ))
                for req in requests
                if vendor in req.vendor_quotes
            ),
        )

    for vendor in all_vendors:
        truck_vars[vendor] = solver.IntVar(0, vendor_max_trucks[vendor], f"truckloads_{vendor}")

    for idx, request in enumerate(requests):
        row_vars = []
        for vendor in request.vendor_quotes:
            var = solver.BoolVar(f"assign_{idx}_{vendor}")
            assign_vars[(idx, vendor)] = var
            row_vars.append(var)
        solver.Add(solver.Sum(row_vars) == 1)

    # LP constraint: fractional-container relaxation.  For each SKU the
    # contribution to vendor V's container count is qty / container_sf_for_that_sku.
    # This correctly prices freight for items with non-standard container sizes.
    for vendor in all_vendors:
        fractional_containers = solver.Sum(
            (request.qty_sf / (
                (request.container_sf_by_vendor or {}).get(vendor) or truck_capacity_sf
            )) * assign_vars[(idx, vendor)]
            for idx, request in enumerate(requests)
            if (idx, vendor) in assign_vars
        )
        solver.Add(fractional_containers <= truck_vars[vendor])

    objective = solver.Objective()
    _penalties = vendor_penalties or {}
    for idx, request in enumerate(requests):
        for vendor, quote in request.vendor_quotes.items():
            penalty = float(_penalties.get(vendor, 0.0))
            objective.SetCoefficient(assign_vars[(idx, vendor)], request.qty_sf * (quote.material_price + penalty))
    for vendor in all_vendors:
        sample_quote = next(
            request.vendor_quotes[vendor]
            for request in requests
            if vendor in request.vendor_quotes
        )
        objective.SetCoefficient(
            truck_vars[vendor],
            float(sample_quote.freight_per_truck) + _TRUCKLOAD_TIEBREAKER,
        )
    objective.SetMinimization()

    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return {
            "status": "error",
            "errors": ["The vendor optimizer could not find a feasible solution."],
            "assignments": {},
        }

    assignments: Dict[str, str] = {}
    for idx, request in enumerate(requests):
        chosen_vendor: Optional[str] = None
        for vendor in request.vendor_quotes:
            if assign_vars[(idx, vendor)].solution_value() > 0.5:
                chosen_vendor = vendor
                break
        if chosen_vendor is None:
            return {
                "status": "error",
                "errors": [f"Solver returned no vendor assignment for {request.sku}."],
                "assignments": {},
            }
        assignments[request.sku] = chosen_vendor

    return {
        "status": "ok",
        "errors": [],
        "assignments": assignments,
    }


def _build_rollups(
    requests: List[PurchaseRequest],
    assignments: Dict[str, str],
    qty_by_sku: Dict[str, float],
    truck_capacity_sf: float,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], float, float]:
    request_map = {request.sku: request for request in requests}
    all_vendors = sorted({vendor for vendor in assignments.values()})
    vendor_qty: Dict[str, float] = {vendor: 0.0 for vendor in all_vendors}
    vendor_material_total: Dict[str, float] = {vendor: 0.0 for vendor in all_vendors}
    vendor_freight_per_truck: Dict[str, float] = {}
    vendor_added_qty: Dict[str, float] = {vendor: 0.0 for vendor in all_vendors}

    item_costs: Dict[str, Dict[str, Any]] = {}
    for sku, vendor in assignments.items():
        request = request_map[sku]
        quote = request.vendor_quotes[vendor]
        final_qty = float(qty_by_sku.get(sku, request.qty_sf))
        base_qty = float(request.qty_sf)
        try:
            original_qty = float(request.original_qty_sf if request.original_qty_sf is not None else base_qty)
        except Exception:
            original_qty = base_qty
        if not math.isfinite(original_qty) or original_qty <= 0:
            original_qty = base_qty
        rounded_qty = max(0.0, base_qty - original_qty)
        added_qty = max(0.0, final_qty - base_qty)
        pallet_sf = _sanitize_pallet_sf(
            (request.pallet_sf_by_vendor or {}).get(vendor) or request.pallet_sf
        )
        container_sf_for_vendor = (
            (request.container_sf_by_vendor or {}).get(vendor) or truck_capacity_sf
        )
        vendor_qty[vendor] += final_qty
        vendor_material_total[vendor] += final_qty * quote.material_price
        vendor_added_qty[vendor] += added_qty
        vendor_freight_per_truck[vendor] = quote.freight_per_truck
        item_costs[sku] = {
            "sku": request.sku,
            "description": request.description,
            "vendor": vendor,
            "price": quote.material_price,
            "original_qty": original_qty,
            "base_qty": base_qty,
            "rounded_qty": rounded_qty,
            "qty": final_qty,
            "added_qty": added_qty,
            "pallet_sf": pallet_sf,
            "container_sf_for_vendor": container_sf_for_vendor,
            "pallets_added": int(round(added_qty / pallet_sf)) if pallet_sf > 0 else 0,
            "rounded_to_min_pallet": rounded_qty > 1e-9,
        }

    # Count containers per vendor as the sum of per-SKU container counts.
    # Items with vendor-specific container data use their actual container size;
    # others fall back to the global truck_capacity_sf.
    vendor_containers: Dict[str, int] = {vendor: 0 for vendor in all_vendors}
    for sku, vendor in assignments.items():
        request = request_map.get(sku)
        if request is None:
            continue
        item_qty = float(qty_by_sku.get(sku, request.qty_sf))
        if item_qty <= 0:
            continue
        c_sf = (request.container_sf_by_vendor or {}).get(vendor) or truck_capacity_sf
        vendor_containers[vendor] += max(1, int(math.ceil(item_qty / c_sf)))

    vendor_summary: Dict[str, Dict[str, Any]] = {}
    total_material_cost = 0.0
    total_freight_cost = 0.0
    for vendor in all_vendors:
        qty = vendor_qty[vendor]
        if qty <= 0:
            continue
        truckloads = vendor_containers[vendor]
        freight_per_truck = float(vendor_freight_per_truck.get(vendor, 0.0))
        freight_total = truckloads * freight_per_truck
        freight_per_sf = freight_total / qty if qty > 0 else 0.0
        material_total = vendor_material_total[vendor]
        landed_total = material_total + freight_total
        vendor_summary[vendor] = {
            "vendor": vendor,
            "qty_sf": qty,
            "truckloads": truckloads,
            "truck_fill_pct": qty / (truckloads * truck_capacity_sf) if truckloads > 0 else 0.0,
            "freight_per_truck": freight_per_truck,
            "freight_total": freight_total,
            "freight_per_sf": freight_per_sf,
            "material_total": material_total,
            "added_qty_sf": vendor_added_qty[vendor],
            "avg_material_price": material_total / qty if qty > 0 else 0.0,
            "landed_total": landed_total,
            "avg_landed_cost": landed_total / qty if qty > 0 else 0.0,
        }
        total_material_cost += material_total
        total_freight_cost += freight_total

    for sku, item in item_costs.items():
        vendor_rollup = vendor_summary[item["vendor"]]
        landed_cost = float(item["price"]) + vendor_rollup["freight_per_sf"]
        item["freight_share"] = vendor_rollup["freight_per_sf"]
        item["landed_cost"] = landed_cost
        item["line_total"] = landed_cost * float(item["qty"])
        item["truckloads"] = vendor_rollup["truckloads"]
        item["vendor_qty_sf"] = vendor_rollup["qty_sf"]

    return vendor_summary, item_costs, total_material_cost, total_freight_cost


def _round_to_pallet_multiples(
    requests: List[PurchaseRequest],
    assignments: Dict[str, str],
    default_pallet_sf: float,
) -> Dict[str, float]:
    """Round each SKU's quantity UP to the nearest full pallet for its assigned vendor.

    Uses pallet_sf_by_vendor[vendor] when available, falls back to request.pallet_sf,
    then default_pallet_sf.
    """
    qty_by_sku: Dict[str, float] = {}
    for request in requests:
        qty = float(request.qty_sf)
        vendor = assignments.get(request.sku)
        if vendor and qty > 0:
            pallet_sf = _sanitize_pallet_sf(
                (request.pallet_sf_by_vendor or {}).get(vendor) or request.pallet_sf,
                default_pallet_sf,
            )
            n_pallets = math.ceil(qty / pallet_sf)
            qty_by_sku[request.sku] = float(n_pallets) * pallet_sf
        else:
            qty_by_sku[request.sku] = qty
    return qty_by_sku


def _apply_optional_pallet_uplift(
    requests: List[PurchaseRequest],
    assignments: Dict[str, str],
    vendor_summary: Dict[str, Dict[str, Any]],
    truck_capacity_sf: float,
    max_uplift_pct: float,
    default_pallet_sf: float,
    base_qty_by_sku: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Fill slack space in each item's last container with additional pallets.

    For items with per-vendor container data the slack is the unused space in
    the last (partially-filled) container for that specific SKU.  For items
    without container data the slack is the unused space across the vendor's
    globally-sized trucks (original behaviour).

    base_qty_by_sku: if provided, use these as starting quantities (e.g. already
    rounded to pallet multiples) instead of request.qty_sf.
    """
    qty_by_sku = (
        dict(base_qty_by_sku)
        if base_qty_by_sku is not None
        else {request.sku: float(request.qty_sf) for request in requests}
    )
    if max_uplift_pct <= 0:
        return qty_by_sku

    for request in requests:
        vendor = assignments.get(request.sku)
        if not vendor:
            continue
        quote = request.vendor_quotes.get(vendor)
        if quote is None:
            continue

        pallet_sf = _sanitize_pallet_sf(
            (request.pallet_sf_by_vendor or {}).get(vendor) or request.pallet_sf,
            default_pallet_sf,
        )
        container_sf = (request.container_sf_by_vendor or {}).get(vendor) or truck_capacity_sf

        current_qty = qty_by_sku[request.sku]
        if current_qty <= 0 or container_sf <= 0:
            continue

        n_containers = math.ceil(current_qty / container_sf)
        slack_sf = n_containers * container_sf - current_qty
        max_extra_sf = min(slack_sf, current_qty * max_uplift_pct)

        if max_extra_sf < pallet_sf - 1e-9:
            continue

        extra_pallets = int(max_extra_sf / pallet_sf)
        if extra_pallets > 0:
            qty_by_sku[request.sku] += extra_pallets * pallet_sf

    return qty_by_sku


def _annotate_truckloads(
    item_costs: Dict[str, Dict[str, Any]],
    vendor_summary: Dict[str, Dict[str, Any]],
    truck_capacity_sf: float,
) -> None:
    """Assign items to numbered containers (trucks) for display.

    Each item fills its own container(s) using its specific container capacity.
    Items without per-vendor container data share trucks at ``truck_capacity_sf``
    (original behaviour).
    """
    items_by_vendor: Dict[str, List[Dict[str, Any]]] = {}
    for item in item_costs.values():
        items_by_vendor.setdefault(item["vendor"], []).append(item)

    for vendor, vendor_items in items_by_vendor.items():
        vendor_items_sorted = sorted(vendor_items, key=lambda row: (-float(row["qty"]), str(row["sku"])))
        truckloads = int(vendor_summary.get(vendor, {}).get("truckloads", 0) or 0)
        if truckloads <= 0:
            continue

        truck_fill_sf: List[float] = []
        truck_capacity_per_truck: List[float] = []
        current_truck = 1

        for item in vendor_items_sorted:
            item_container_sf = float(item.get("container_sf_for_vendor") or truck_capacity_sf)
            qty_remaining = float(item["qty"])
            segments: List[Dict[str, float]] = []
            labels: List[int] = []

            while qty_remaining > 1e-9 and current_truck <= truckloads:
                # Ensure the per-truck tracking lists are long enough.
                while len(truck_fill_sf) < current_truck:
                    truck_fill_sf.append(0.0)
                    truck_capacity_per_truck.append(item_container_sf)

                remaining_in_container = item_container_sf - truck_fill_sf[current_truck - 1]
                if remaining_in_container <= 1e-9:
                    current_truck += 1
                    continue

                take = min(qty_remaining, remaining_in_container)
                truck_fill_sf[current_truck - 1] += take
                segments.append({"truck": current_truck, "qty": take})
                labels.append(current_truck)
                qty_remaining -= take

                if truck_fill_sf[current_truck - 1] >= item_container_sf - 1e-9:
                    current_truck += 1

            item["truck_segments"] = segments
            item["truck_labels"] = sorted(set(labels))
            item["truck_display_labels"] = [f"{vendor} T{truck}" for truck in sorted(set(labels))]
            item["primary_truck"] = labels[0] if labels else 1
            item["primary_truck_label"] = f"{vendor} T{item['primary_truck']}"

        # Pad to truckloads length in case fewer trucks were actually used.
        while len(truck_fill_sf) < truckloads:
            truck_fill_sf.append(0.0)
            truck_capacity_per_truck.append(truck_capacity_sf)

        vendor_summary[vendor]["truck_fill_sf"] = truck_fill_sf
        vendor_summary[vendor]["truck_unused_sf"] = [
            max(0.0, cap - fill)
            for cap, fill in zip(truck_capacity_per_truck, truck_fill_sf)
        ]


def optimize_vendor_mix(
    requests: List[PurchaseRequest],
    truck_capacity_sf: float = TRUCKLOAD_CAPACITY_SF,
    allow_pallet_uplift: bool = False,
    max_uplift_pct: float = MAX_PALLET_UPLIFT_PCT,
    default_pallet_sf: float = DEFAULT_PALLET_SF,
    round_to_pallet_multiples: bool = False,
    vendor_penalties: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Solve the vendor-assignment problem with fixed truck freight.

    Each SKU is assigned to exactly one vendor. Vendor freight is charged per
    truckload, and each truck can carry up to ``truck_capacity_sf`` square feet.
    Optional pallet uplift can fill spare space on already-purchased trucks
    without exceeding a per-item percent cap.
    """
    cleaned_requests = _clean_requests(requests, default_pallet_sf)
    if not cleaned_requests:
        return {
            "status": "error",
            "errors": ["No purchasable items were supplied to the optimizer."],
            "item_costs": {},
            "vendor_summary": {},
            "total_cost": 0.0,
            "total_material_cost": 0.0,
            "total_freight_cost": 0.0,
        }

    unavailable = [r.sku for r in cleaned_requests if not r.vendor_quotes]
    if unavailable:
        return {
            "status": "error",
            "errors": [f"No valid vendor quotes found for: {', '.join(sorted(unavailable))}"],
            "item_costs": {},
            "vendor_summary": {},
            "total_cost": 0.0,
            "total_material_cost": 0.0,
            "total_freight_cost": 0.0,
        }

    solved = _solve_vendor_assignments(cleaned_requests, truck_capacity_sf, vendor_penalties=vendor_penalties)
    if solved.get("status") != "ok":
        return {
            "status": "error",
            "errors": solved.get("errors", ["The vendor optimizer could not find a feasible solution."]),
            "item_costs": {},
            "vendor_summary": {},
            "total_cost": 0.0,
            "total_material_cost": 0.0,
            "total_freight_cost": 0.0,
        }
    assignments = solved["assignments"]

    # Round each quantity to the nearest full pallet multiple for its assigned vendor.
    # Uses per-vendor pallet SF from the pricing spreadsheet where available; falls back
    # to request.pallet_sf then default_pallet_sf.
    if round_to_pallet_multiples:
        base_qty_by_sku = _round_to_pallet_multiples(cleaned_requests, assignments, default_pallet_sf)
    else:
        base_qty_by_sku = {request.sku: float(request.qty_sf) for request in cleaned_requests}

    vendor_summary, _, _, _ = _build_rollups(cleaned_requests, assignments, base_qty_by_sku, truck_capacity_sf)
    qty_by_sku = base_qty_by_sku
    if allow_pallet_uplift and max_uplift_pct > 0:
        qty_by_sku = _apply_optional_pallet_uplift(
            cleaned_requests,
            assignments,
            vendor_summary,
            truck_capacity_sf,
            max_uplift_pct,
            default_pallet_sf,
            base_qty_by_sku=base_qty_by_sku,
        )

    vendor_summary, item_costs, total_material_cost, total_freight_cost = _build_rollups(
        cleaned_requests,
        assignments,
        qty_by_sku,
        truck_capacity_sf,
    )
    _annotate_truckloads(item_costs, vendor_summary, truck_capacity_sf)

    total_added_qty = sum(float(item.get("added_qty", 0.0) or 0.0) for item in item_costs.values())
    return {
        "status": "ok",
        "errors": [],
        "item_costs": item_costs,
        "vendor_summary": vendor_summary,
        "optimal_assignments": assignments,
        "total_material_cost": total_material_cost,
        "total_freight_cost": total_freight_cost,
        "total_cost": total_material_cost + total_freight_cost,
        "total_added_qty": total_added_qty,
        "truck_capacity_sf": truck_capacity_sf,
    }


def build_cost_alerts(
    item_costs: Dict[str, Dict[str, Any]],
    baseline_costs: Dict[str, Dict[str, Any]],
    pct_threshold: float = PRICE_ALERT_PCT_THRESHOLD,
    abs_threshold: float = PRICE_ALERT_ABS_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Compare proposed landed costs against baseline cost sources."""
    alerts: List[Dict[str, Any]] = []
    for sku, item in (item_costs or {}).items():
        baseline = baseline_costs.get(normalize_item_key(sku), {})
        if not baseline:
            continue

        proposed_landed = float(item.get("landed_cost") or 0.0)
        comparisons: List[Dict[str, Any]] = []
        for source_key, label in (
            ("last_received_po_cost", "Last Received PO"),
            ("avg_inventory_cost", "Avg Inventory Cost"),
        ):
            reference_cost = baseline.get(source_key)
            if reference_cost is None:
                continue
            try:
                reference_cost = float(reference_cost)
            except Exception:
                continue
            if reference_cost <= 0:
                continue

            diff_value = proposed_landed - reference_cost
            diff_pct = diff_value / reference_cost if reference_cost else None
            if diff_pct is None:
                continue
            if abs(diff_value) >= abs_threshold and abs(diff_pct) >= pct_threshold:
                comparisons.append(
                    {
                        "source": label,
                        "reference_cost": reference_cost,
                        "diff_value": diff_value,
                        "diff_pct": diff_pct,
                    }
                )

        if not comparisons:
            continue

        alerts.append(
            {
                "sku": sku,
                "vendor": item.get("vendor", ""),
                "qty": float(item.get("qty") or 0.0),
                "material_price": float(item.get("price") or 0.0),
                "proposed_landed_cost": proposed_landed,
                "last_received_po_cost": baseline.get("last_received_po_cost"),
                "avg_inventory_cost": baseline.get("avg_inventory_cost"),
                "last_received_date": baseline.get("last_received_date"),
                "comparisons": comparisons,
                "max_abs_pct_diff": max(abs(comp["diff_pct"]) for comp in comparisons),
            }
        )

    alerts.sort(key=lambda row: row.get("max_abs_pct_diff", 0.0), reverse=True)
    return alerts
