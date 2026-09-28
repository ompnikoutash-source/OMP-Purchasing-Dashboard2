"""
OMP Delivery Route Optimizer
=============================
Pulls OUR TRUCK orders from Gartman for a given ship date, geocodes ship-to
addresses, builds a drive-time matrix via OSRM, solves a 5-truck VRP with
OR-Tools, and exports a print-ready Excel route sheet + HTML map.

Usage
-----
  # First run: discover actual SHHEAD column names
  python dispatch_routes.py --discover-schema

  # Daily use
  python dispatch_routes.py --date 2026-03-04
  python dispatch_routes.py --date 2026-03-04 --trucks 4 --depart 07:30

Output
------
  dispatch_routes_20260304.xlsx   (route sheets, one tab per truck + summary)
  dispatch_routes_20260304.html   (colour-coded folium map)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from core.db_connection import connect
from dispatch.db import (
    DEFAULT_COL_CONFIG,
    discover_schema,
    fetch_order_notes,
    fetch_order_quantities,
    fetch_orders,
    fetch_transfer_orders,
)
from dispatch.geocoder import geocode_address, init_geocode_db
from dispatch.notes_parser import parse_stop_duration
from dispatch.osrm import build_travel_matrix
from dispatch.solver import SolverResult, seconds_to_wallclock, solve_vrp
from dispatch.excel_export import build_route_workbook

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEPOT_ADDRESS    = "7751 Hayvenhurst Ave, Van Nuys, CA 91406"
DEPOT_LAT        = 34.19890   # pre-verified via Nominatim
DEPOT_LON        = -118.49120
DEFAULT_DEPART   = "06:00"
DEFAULT_TRUCKS   = 5

# Satellite branches — every branch must be visited by exactly one truck each day
BRANCH_SVC_MIN = 45  # minutes spent at each branch stop
BRANCH_STOPS = [
    {"name": "Robertson Branch", "full_addr": "3351 S Robertson Blvd, Los Angeles, CA 90034",
     "addr1": "3351 S Robertson Blvd", "city": "Los Angeles", "state": "CA", "zip": "90034"},
    {"name": "Gardena Branch",   "full_addr": "534 W 182nd St, Gardena, CA 90248",
     "addr1": "534 W 182nd St",        "city": "Gardena",     "state": "CA", "zip": "90248"},
    {"name": "Santa Ana Branch", "full_addr": "2211 S Wright St, Santa Ana, CA 92705",
     "addr1": "2211 S Wright St",      "city": "Santa Ana",   "state": "CA", "zip": "92705"},
    {"name": "Vernon Branch",    "full_addr": "5051 Slauson Ave, Vernon, CA 90058",
     "addr1": "5051 Slauson Ave",      "city": "Vernon",      "state": "CA", "zip": "90058"},
    {"name": "Glendale Branch",  "full_addr": "617 Ruberta Ave, Glendale, CA 91201",
     "addr1": "617 Ruberta Ave",       "city": "Glendale",    "state": "CA", "zip": "91201"},
    # Irwindale (code 51) is an overflow warehouse, NOT a mandatory daily stop.
    # It is kept in TRANSFER_LOCATION_MAP so transfer orders to/from 51 are handled correctly.
]

# ---------------------------------------------------------------------------
# Branch-to-branch transfer configuration
# ---------------------------------------------------------------------------

# Maps the 2-digit TRANSFER## suffix to branch address info.
# Code "01" = Van Nuys (the depot itself).
TRANSFER_LOCATION_MAP: dict[str, dict] = {
    "01": {"name": "Van Nuys",   "full_addr": DEPOT_ADDRESS,
           "addr1": "7751 Hayvenhurst Ave", "city": "Van Nuys",     "state": "CA", "zip": "91406"},
    "03": {"name": "Robertson",  "full_addr": "3351 S Robertson Blvd, Los Angeles, CA 90034",
           "addr1": "3351 S Robertson Blvd", "city": "Los Angeles", "state": "CA", "zip": "90034"},
    "04": {"name": "Gardena",    "full_addr": "534 W 182nd St, Gardena, CA 90248",
           "addr1": "534 W 182nd St",        "city": "Gardena",     "state": "CA", "zip": "90248"},
    "06": {"name": "Glendale",   "full_addr": "617 Ruberta Ave, Glendale, CA 91201",
           "addr1": "617 Ruberta Ave",       "city": "Glendale",    "state": "CA", "zip": "91201"},
    "08": {"name": "Vernon",     "full_addr": "5051 Slauson Ave, Vernon, CA 90058",
           "addr1": "5051 Slauson Ave",      "city": "Vernon",      "state": "CA", "zip": "90058"},
    "09": {"name": "Santa Ana",  "full_addr": "2211 S Wright St, Santa Ana, CA 92705",
           "addr1": "2211 S Wright St",      "city": "Santa Ana",   "state": "CA", "zip": "92705"},
    "51": {"name": "Irwindale",  "full_addr": "5313 3rd St, Irwindale, CA 91706",
           "addr1": "5313 3rd St",           "city": "Irwindale",   "state": "CA", "zip": "91706"},
}

# Maps pick-ticket printer name → source location code.
# PICKTICK / PICKTICK1 = Van Nuys (confirmed by user).
# Others follow the same numeric suffix pattern — update if Gartman uses different names.
PRINTER_SOURCE_MAP: dict[str, str] = {
    "PICKTICK":   "01",
    "PICKTICK1":  "01",
    "PICKTICK3":  "03",
    "PICKTICK4":  "04",
    "PICKTICK6":  "06",
    "PICKTICK8":  "08",
    "PICKTICK9":  "09",
    "PICKTICK51": "51",
}

# Maps branch code → the exact "cust_name" used on BRANCH_STOPS.
# "01" (Van Nuys) is the depot — it has no entry here because it is not a route stop.
# "51" (Irwindale) is omitted because it is not a mandatory daily branch stop.
_BRANCH_CODE_TO_STOP_NAME: dict[str, str] = {
    "03": "Robertson Branch",
    "04": "Gardena Branch",
    "06": "Glendale Branch",
    "08": "Vernon Branch",
    "09": "Santa Ana Branch",
}

# Folium truck colors (one per truck, cycles if > 5)
_TRUCK_COLORS    = ["red", "blue", "green", "orange", "purple"]

# ---------------------------------------------------------------------------
# Break schedule (compressed/productive seconds = wall-clock minus break time)
# ---------------------------------------------------------------------------
_BREAK_REST1_COMPRESSED     = 2 * 3600          # 15-min rest:  trigger after 2h of productive time
_BREAK_REST1_SEC            = 15 * 60
_BREAK_LUNCH_COMPRESSED     = 3 * 3600          # 60-min lunch: trigger after 3h of productive time
_BREAK_LUNCH_SEC            = 60 * 60           #   (separate from rest so driver can make stops between them)
_BREAK_MANDATORY_SEC        = _BREAK_REST1_SEC + _BREAK_LUNCH_SEC   # 75 min total — kept for summary
_BREAK_OPTIONAL_COMPRESSED  = 17_100            # 4h 45m compressed → 6h wall-clock threshold
_BREAK_OPTIONAL_SEC         = 15 * 60           # 15-min optional 2nd rest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OMP Delivery Route Optimizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date", type=str,
        help="Ship date in YYYY-MM-DD format (e.g. 2026-03-04)",
    )
    parser.add_argument(
        "--trucks", type=int, default=DEFAULT_TRUCKS,
        help=f"Number of trucks (default {DEFAULT_TRUCKS})",
    )
    parser.add_argument(
        "--depart", type=str, default=DEFAULT_DEPART,
        help=f"Departure time HH:MM, 24-hour (default {DEFAULT_DEPART})",
    )
    parser.add_argument(
        "--discover-schema", action="store_true",
        help="Print SHHEAD column names and exit (run once to find ship-via/address columns)",
    )
    args = parser.parse_args()

    print("\nConnecting to Gartman database...")
    conn = connect()

    # ---- Schema discovery mode ----------------------------------------
    if args.discover_schema:
        discover_schema(conn)
        return

    # ---- Validate date ---------------------------------------------------
    if not args.date:
        parser.error("--date is required unless using --discover-schema")

    try:
        ship_date = datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error(f"Invalid date format '{args.date}'. Use YYYY-MM-DD.")

    ship_date_iso = ship_date.strftime("%Y-%m-%d")   # "2026-03-04" - DB2 DATE literal

    try:
        depart_h, depart_m = map(int, args.depart.split(":"))
    except ValueError:
        parser.error(f"Invalid departure time '{args.depart}'. Use HH:MM (e.g. 07:30).")

    depart_offset_sec = depart_h * 3600 + depart_m * 60
    max_shift_sec     = 8 * 3600   # 8-hour shift

    date_label = ship_date.strftime("%A, %B ") + str(ship_date.day) + ship_date.strftime(", %Y")

    print(f"\n{'='*60}")
    print(f"Route Optimizer -- {date_label}")
    print(f"Trucks: {args.trucks}  |  Depart: {args.depart}  |  SHIDAT={ship_date_iso}")
    print(f"{'='*60}")

    # ---- 1. Fetch orders -------------------------------------------------
    print(f"\n[1/7] Fetching OUR TRUCK orders for {args.date}...")
    orders_df = fetch_orders(conn, ship_date_iso, col_config=DEFAULT_COL_CONFIG)

    if orders_df.empty:
        print(
            "  No orders found.\n"
            "  Verify the date has deliveries and that DEFAULT_COL_CONFIG in\n"
            "  dispatch/db.py matches your SHHEAD column names.\n"
            "  Tip: run --discover-schema to check column names.\n"
        )
        sys.exit(0)

    print(f"  Found {len(orders_df)} order(s)")

    # ---- 1b. Fetch branch-to-branch transfer orders ----------------------
    print(f"\n[1b/7] Fetching branch-to-branch transfer orders for {args.date}...")
    transfer_rows = fetch_transfer_orders(conn, ship_date_iso)
    if transfer_rows:
        print(f"  Found {len(transfer_rows)} transfer order(s) — routing constraints will be applied.")
    else:
        print(f"  No transfer orders found.")

    # ---- 2. Fetch order notes -------------------------------------------
    print("\n[2/7] Fetching order text/notes...")
    order_nums  = orders_df["ORDER_NUM"].tolist()
    notes_dict  = fetch_order_notes(conn, order_nums)

    # ---- 2b. Fetch SF quantities for volume-based service time ----------
    print("\n[2b/7] Fetching order SF quantities for service-time scaling...")
    qty_dict = fetch_order_quantities(conn, order_nums)

    # ---- 3. Build stop list with parsed notes ---------------------------
    print("\n[3/7] Parsing notes for stop durations and time windows...")
    stops: list[dict] = []
    for _, row in orders_df.iterrows():
        order_key = str(row["ORDER_NUM"]).strip()
        notes_raw = notes_dict.get(order_key, "")
        qty_sf    = qty_dict.get(order_key, 0)
        parsed    = parse_stop_duration(notes_raw, quantity_sf=qty_sf)

        # Convert wall-clock time windows to seconds-since-shift-start
        tw_open  = _wall_to_shift(parsed["time_window_open"],  depart_offset_sec, max_shift_sec, default=0)
        tw_close = _wall_to_shift(parsed["time_window_close"], depart_offset_sec, max_shift_sec, default=max_shift_sec)

        stops.append({
            "order_num":  str(row["ORDER_NUM"]).strip(),
            "cust_name":  str(row["CUST_NAME"]).strip(),
            "addr1":      str(row["SHIP_ADDR1"]).strip(),
            "city":       str(row["SHIP_CITY"]).strip(),
            "state":      str(row["SHIP_STATE"]).strip(),
            "zip":        str(row["SHIP_ZIP"]).strip(),
            "full_addr":  str(row["FULL_ADDRESS"]).strip(),
            "notes_raw":  notes_raw,
            "svc_min":    parsed["service_minutes"],
            "tw_open":    tw_open,
            "tw_close":   tw_close,
            "notes_flags": ", ".join(parsed["flags"]) if parsed["flags"] else "",
            "geocode_ok": True,
            "is_branch":  False,
            "lat":        None,
            "lon":        None,
            "eta_str":    "",
            "out_time_str": "",
        })

    # ---- 4. Geocode all stops -------------------------------------------
    print(f"\n[4/7] Geocoding {len(stops)} address(es) (cached results are instant)...")
    geocache_conn = init_geocode_db()
    geocode_failures = 0

    for s in stops:
        coords = geocode_address(s["full_addr"], geocache_conn)

        # Fallback: if full address fails, retry without zip (bad zip codes in data are common)
        if coords is None and s["zip"]:
            fallback = f"{s['addr1']}, {s['city']}, {s['state']}".strip(", ")
            coords = geocode_address(fallback, geocache_conn)
            if coords:
                print(f"  NOTE: Geocoded order {s['order_num']} via fallback (no zip): '{fallback}'")

        if coords:
            s["lat"], s["lon"] = coords
        else:
            print(f"  WARNING: Geocode failed for order {s['order_num']} at '{s['full_addr']}'")
            print(f"           -> Assigned depot coordinates; review stop placement manually.")
            s["lat"], s["lon"] = DEPOT_LAT, DEPOT_LON
            s["geocode_ok"] = False
            geocode_failures += 1

    if geocode_failures:
        print(f"  {geocode_failures} geocode failure(s) - these stops will be marked red in Excel.")

    # ---- 4b. Geocode and inject satellite branch stops ------------------
    print(f"\n[4b/7] Geocoding {len(BRANCH_STOPS)} satellite branch(es)...")
    for b in BRANCH_STOPS:
        coords = geocode_address(b["full_addr"], geocache_conn)
        if coords is None:
            print(f"  WARNING: Could not geocode branch '{b['name']}' — using depot coords.")
            coords = (DEPOT_LAT, DEPOT_LON)
        else:
            print(f"  Branch: {b['name']} geocoded OK")
        stops.append({
            "order_num":    "BRANCH",
            "cust_name":    b["name"],
            "addr1":        b["addr1"],
            "city":         b["city"],
            "state":        b["state"],
            "zip":          b["zip"],
            "full_addr":    b["full_addr"],
            "notes_raw":    "",
            "svc_min":      BRANCH_SVC_MIN,
            "tw_open":      0,
            "tw_close":     max_shift_sec,
            "notes_flags":  "BRANCH STOP",
            "geocode_ok":   True,
            "is_branch":    True,
            "lat":          coords[0],
            "lon":          coords[1],
            "eta_str":      "",
            "out_time_str": "",
        })

    geocache_conn.close()

    # ---- 4c. Build transfer pickup-delivery constraints -----------------
    # Transfers are NOT separate stops.  They are handled during the existing
    # mandatory branch visits.  We just need to tell the solver:
    #   • source branch must be visited BEFORE destination branch
    #   • both must be on the SAME truck
    # The 45-min BRANCH_SVC_MIN already covers the loading/unloading time.
    transfer_pairs: list[tuple[int, int]] = []

    if transfer_rows:
        print(f"\n[4c/7] Building transfer routing constraints...")
        # name → stops-list index (0-based); matrix node = index + 1
        stop_name_to_idx: dict[str, int] = {s["cust_name"]: i for i, s in enumerate(stops)}
        seen_pairs: set[tuple[str, str]] = set()

        for xfer in transfer_rows:
            src_code  = PRINTER_SOURCE_MAP.get(xfer["printer"].upper(), "")
            dest_code = xfer["dest_code"]

            # If the printer field didn't resolve to a known branch, warn and skip
            if not src_code:
                print(
                    f"  WARNING: Transfer order {xfer['order_num']} has unknown printer "
                    f"'{xfer['printer']}' — cannot determine source branch. "
                    f"Run --discover-schema to find the printer column name and add it to "
                    f"PRINTER_SOURCE_MAP in dispatch_routes.py."
                )
                continue

            pair_key  = (src_code, dest_code)
            if pair_key in seen_pairs or src_code == dest_code:
                continue
            seen_pairs.add(pair_key)

            src_name  = _BRANCH_CODE_TO_STOP_NAME.get(src_code)   # None for depot (01)
            dest_name = _BRANCH_CODE_TO_STOP_NAME.get(dest_code)  # None when dest is depot (01)
            src_label  = src_name  or TRANSFER_LOCATION_MAP.get(src_code,  {}).get("name", f"loc-{src_code}")
            dest_label = dest_name or TRANSFER_LOCATION_MAP.get(dest_code, {}).get("name", f"loc-{dest_code}")
            print(f"  Transfer: {src_label} → {dest_label}")

            # Annotate destination branch stop (skipped when dest is depot — not a route stop)
            if dest_name and dest_name in stop_name_to_idx:
                s = stops[stop_name_to_idx[dest_name]]
                note = f"Receives transfer from {src_label}"
                s["notes_flags"] = (
                    f"BRANCH STOP | {note}" if s["notes_flags"] == "BRANCH STOP"
                    else f"{s['notes_flags']} | {note}"
                )
            elif dest_code == "01":
                print(f"    NOTE: Destination is Van Nuys depot — all trucks return there; no depot annotation needed.")

            # Annotate source branch stop (if not the depot)
            if src_name and src_name in stop_name_to_idx:
                s = stops[stop_name_to_idx[src_name]]
                note = f"PICKUP: Transfer items to {dest_label}"
                s["notes_flags"] = (
                    f"BRANCH STOP | {note}" if s["notes_flags"] == "BRANCH STOP"
                    else f"{s['notes_flags']} | {note}"
                )
            elif src_code == "01":
                print(f"    NOTE: Source is Van Nuys depot — items will be loaded at departure; no branch annotation needed.")

            # Solver constraint only needed when both ends are non-depot branch stops.
            # If destination is the depot (01), every truck already returns there — no constraint needed.
            # If source is a branch, it is already a mandatory stop — the truck will naturally pick up.
            if src_name and dest_name:
                src_idx  = stop_name_to_idx.get(src_name)
                dest_idx = stop_name_to_idx.get(dest_name)
                if src_idx is not None and dest_idx is not None:
                    # +1 because matrix node 0 = depot
                    transfer_pairs.append((src_idx + 1, dest_idx + 1))
                    print(f"    → Solver: {src_name} (node {src_idx+1}) before {dest_name} (node {dest_idx+1}), same truck")
                else:
                    print(f"    WARNING: Stop index not found for {src_name!r} or {dest_name!r} — constraint skipped")
            elif dest_code == "01":
                print(f"    → {src_label} is a mandatory branch stop; truck will pick up transfer and return to depot automatically.")

    # ---- 5. Build travel-time matrix via OSRM ---------------------------
    n_stops = len(stops)
    print(f"\n[5/7] Building {n_stops + 1}x{n_stops + 1} travel-time matrix via OSRM...")
    coords = [(DEPOT_LAT, DEPOT_LON)] + [(s["lat"], s["lon"]) for s in stops]

    try:
        matrix_sec = build_travel_matrix(coords)
    except Exception as e:
        print(f"\nERROR building travel matrix: {e}")
        sys.exit(1)

    print(f"  Matrix built ({(n_stops + 1) ** 2} cells).")

    # ---- 5b. Pre-solver diagnostics ------------------------------------
    print("\n  --- STOP DIAGNOSTICS ---")
    print(f"  {'#':<4} {'Order':<10} {'Customer':<28} {'Address':<35} {'TW Open':<10} {'TW Close':<10} {'Svc':<6} {'OSRM max'}")
    unreachable_nodes = []
    for i, s in enumerate(stops):
        node = i + 1  # 1-based in matrix
        depot_to_stop = matrix_sec[0][node]
        stop_to_depot = matrix_sec[node][0]
        if depot_to_stop >= 999_000 or stop_to_depot >= 999_000:
            osrm_flag = " *** UNREACHABLE (no road route) ***"
        elif depot_to_stop > max_shift_sec:
            osrm_flag = f" *** TOO FAR ({depot_to_stop // 3600:.0f}h drive from depot) ***"
        else:
            osrm_flag = ""
        if osrm_flag:
            unreachable_nodes.append(i)
        tw_open_str  = seconds_to_wallclock(s["tw_open"],  depart_h, depart_m) if s["tw_open"]  > 0             else "any"
        tw_close_str = seconds_to_wallclock(s["tw_close"], depart_h, depart_m) if s["tw_close"] < max_shift_sec else "any"
        print(
            f"  {i+1:<4} {s['order_num']:<10} {s['cust_name'][:27]:<28} "
            f"{s['full_addr'][:34]:<35} {tw_open_str:<10} {tw_close_str:<10} "
            f"{s['svc_min']}min{osrm_flag}"
        )
    if unreachable_nodes:
        print(f"\n  *** {len(unreachable_nodes)} stop(s) unreachable by OSRM - these will block the solver ***")
    print("  --- END DIAGNOSTICS ---")

    # ---- 6. Solve VRP ---------------------------------------------------
    print(f"\n[6/7] Solving VRP ({n_stops} stops, {args.trucks} trucks)...")

    service_sec  = [0] + [s["svc_min"] * 60 for s in stops]
    time_windows = [(0, max_shift_sec)] + [(s["tw_open"], s["tw_close"]) for s in stops]

    result: SolverResult = solve_vrp(
        travel_matrix_sec=matrix_sec,
        service_times_sec=service_sec,
        time_windows=time_windows,
        num_vehicles=args.trucks,
        max_shift_seconds=max_shift_sec,
        pickup_delivery_pairs=transfer_pairs if transfer_pairs else None,
    )

    if not result.feasible:
        print(
            "\nERROR: VRP solver could not find a feasible solution.\n"
            "Possible causes:\n"
            "  - Too many stops with tight time windows\n"
            "  - 8-hour shift cap exceeded -- try adding a truck (--trucks)\n"
            "  - OSRM unreachable locations (check for geocode failures above)\n"
        )
        sys.exit(1)

    total_stops_assigned = sum(len(r) for r in result.routes)
    print(f"  Solution found. {total_stops_assigned}/{n_stops} stops assigned.")

    # Annotate stops with their assigned truck, insert break rows, and set wall-clock ETAs.
    # OR-Tools arrival times are in "compressed" seconds (productive time, no breaks).
    # _insert_break_rows() converts them to real wall-clock times and splices in break rows.
    result_stops: list[list[dict]] = [[] for _ in range(args.trucks)]

    for truck_idx, (route, arrivals) in enumerate(zip(result.routes, result.arrival_sec)):
        raw_stops = [stops[node - 1] for node in route]   # node indices are 1-based (0=depot)
        end_compressed = result.end_times_sec[truck_idx]
        result_stops[truck_idx] = _insert_break_rows(
            raw_stops, list(arrivals), end_compressed, depart_h, depart_m,
        )

    # ---- 7. Export outputs ----------------------------------------------
    print(f"\n[7/7] Exporting Excel and HTML map...")

    out_stem = f"dispatch_routes_{ship_date.strftime('%Y%m%d')}"
    out_xlsx = Path(out_stem + ".xlsx")
    out_html = Path(out_stem + ".html")

    # Convert compressed solver end times to actual wall-clock end times for the Excel summary
    wall_end_times = [
        _compressed_to_wall_sec(
            result.end_times_sec[i],
            result.end_times_sec[i] > _BREAK_OPTIONAL_COMPRESSED,
        )
        for i in range(args.trucks)
    ]

    # Build flat all-orders list for the master "All Orders" tab.
    # Deliveries and branch stops come from result_stops (have truck + ETA info).
    # Transfer orders come from transfer_rows (handled within branch visits, no truck assignment).
    all_orders: list[dict] = []
    for truck_idx, truck_stops in enumerate(result_stops):
        sn = 0
        for stop in truck_stops:
            if stop.get("is_break"):
                continue
            sn += 1
            all_orders.append({
                "order_num":    stop["order_num"],
                "cust_name":    stop["cust_name"],
                "addr1":        stop["addr1"],
                "city":         stop["city"],
                "state":        stop["state"],
                "zip":          stop["zip"],
                "row_type":     "BRANCH" if stop.get("is_branch") else "DELIVERY",
                "truck_num":    truck_idx + 1,
                "stop_num":     sn,
                "eta_str":      stop.get("eta_str", ""),
                "out_time_str": stop.get("out_time_str", ""),
                "svc_min":      stop.get("svc_min", 0),
                "notes_flags":  stop.get("notes_flags", ""),
                "printer":      "",
                "geocode_ok":   stop.get("geocode_ok", True),
            })
    for xfer in transfer_rows:
        src_code  = PRINTER_SOURCE_MAP.get(xfer["printer"].upper(), "")
        dest_code = xfer["dest_code"]
        dest_info = TRANSFER_LOCATION_MAP.get(dest_code, {})
        src_name  = TRANSFER_LOCATION_MAP.get(src_code, {}).get("name", f"loc-{src_code}") if src_code else f"unknown (printer='{xfer['printer']}')"
        all_orders.append({
            "order_num":    xfer["order_num"],
            "cust_name":    f"TRANSFER → {dest_info.get('name', dest_code)}",
            "addr1":        dest_info.get("addr1", ""),
            "city":         dest_info.get("city", ""),
            "state":        dest_info.get("state", ""),
            "zip":          dest_info.get("zip", ""),
            "row_type":     "TRANSFER",
            "truck_num":    None,
            "stop_num":     None,
            "eta_str":      "",
            "out_time_str": "",
            "svc_min":      None,
            "notes_flags":  f"Pickup: {src_name}  →  Drop: {dest_info.get('name', dest_code)}",
            "printer":      xfer["printer"],
            "geocode_ok":   True,
        })

    wb = build_route_workbook(
        result_stops=result_stops,
        date_obj=ship_date,
        depart_hour=depart_h,
        depart_minute=depart_m,
        total_travel_sec=result.total_travel_sec,
        end_times_sec=wall_end_times,
        all_orders=all_orders,
    )
    try:
        wb.save(str(out_xlsx))
        print(f"  Saved: {out_xlsx}")
    except PermissionError:
        ts = datetime.now().strftime("%H%M%S")
        out_xlsx = Path(f"{out_stem}_{ts}.xlsx")
        wb.save(str(out_xlsx))
        print(f"  NOTE: {out_stem}.xlsx was open in Excel - saved as: {out_xlsx}")

    try:
        _build_folium_map(result_stops, coords, out_html, date_label)
        print(f"  Saved: {out_html}")
    except Exception as e:
        print(f"  WARNING: Map export failed ({e}) - Excel route sheet is complete.")

    # Summary
    print(f"\n{'='*84}")
    print(f"  {'Truck':<8} {'Stops':<7} {'Drive':<10} {'Service':<10} {'Breaks':<8} {'Total Shift':<13} {'Returns'}")
    print(f"  {'-'*7} {'-'*6} {'-'*9} {'-'*9} {'-'*7} {'-'*12} {'-'*10}")
    for truck_idx, truck_stops in enumerate(result_stops):
        end_compressed = result.end_times_sec[truck_idx]
        has_optional   = end_compressed > _BREAK_OPTIONAL_COMPRESSED
        wall_end_sec   = _compressed_to_wall_sec(end_compressed, has_optional)
        svc_sec        = sum(s["svc_min"] * 60 for s in truck_stops if not s.get("is_break"))
        brk_sec        = (
            (_BREAK_REST1_SEC  if end_compressed >= _BREAK_REST1_COMPRESSED  else 0)
            + (_BREAK_LUNCH_SEC if end_compressed >= _BREAK_LUNCH_COMPRESSED else 0)
            + (_BREAK_OPTIONAL_SEC if has_optional else 0)
        )
        drv_sec        = max(0, end_compressed - svc_sec)
        drv_str        = _fmt_duration(drv_sec)
        svc_str        = _fmt_duration(svc_sec)
        brk_str        = _fmt_duration(brk_sec)
        tot_str        = _fmt_duration(wall_end_sec)
        has_real_stops = any(not s.get("is_break") for s in truck_stops)
        ret_str        = seconds_to_wallclock(wall_end_sec, depart_h, depart_m) if has_real_stops else "--"
        n_deliveries   = sum(1 for s in truck_stops if not s.get("is_break") and not s.get("is_branch"))
        branches       = [s["cust_name"] for s in truck_stops if s.get("is_branch")]
        branch_str     = f"  [Branches: {', '.join(branches)}]" if branches else ""
        print(f"  Truck {truck_idx + 1:<2}  {n_deliveries} deliv + {len(branches)} branch  {drv_str:<10} {svc_str:<10} {brk_str:<8} {tot_str:<13} {ret_str}{branch_str}")
    print(f"{'='*84}")
    if geocode_failures:
        print(f"\n  *** {geocode_failures} geocode failure(s) - review red cells in Excel ***")
    print(f"\nDone.\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: int) -> str:
    """Format seconds as 'Xh Ym'."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _compressed_to_wall_sec(compressed_sec: int, has_optional_break: bool) -> int:
    """Add accumulated break time to a compressed (no-break) second count.

    Each break type is added only once the compressed cursor has passed its
    individual trigger point, so a stop between the rest break and the lunch
    break gets only the 15-min rest offset (not the full 75 min).
    """
    offset = 0
    if compressed_sec >= _BREAK_REST1_COMPRESSED:
        offset += _BREAK_REST1_SEC
    if compressed_sec >= _BREAK_LUNCH_COMPRESSED:
        offset += _BREAK_LUNCH_SEC
    if has_optional_break and compressed_sec >= _BREAK_OPTIONAL_COMPRESSED:
        offset += _BREAK_OPTIONAL_SEC
    return compressed_sec + offset


def _compressed_to_wallclock(
    compressed_sec: int,
    has_optional_break: bool,
    depart_h: int,
    depart_m: int,
) -> str:
    """Convert a compressed OR-Tools arrival time to a wall-clock string."""
    return seconds_to_wallclock(
        _compressed_to_wall_sec(compressed_sec, has_optional_break),
        depart_h,
        depart_m,
    )


def _make_break_row(
    name: str,
    desc: str,
    duration_min: int,
    notes: str,
    eta: str,
    out_time: str,
    btype: str,
) -> dict:
    return {
        "order_num": "", "cust_name": name,
        "addr1": desc, "city": "", "state": "", "zip": "",
        "full_addr": "", "notes_raw": "", "notes_flags": notes,
        "eta_str": eta, "out_time_str": out_time, "svc_min": duration_min,
        "geocode_ok": True, "is_branch": False, "is_break": True,
        "break_type": btype, "lat": None, "lon": None,
    }


def _insert_break_rows(
    stops: list[dict],
    arrivals_compressed: list[int],
    end_compressed: int,
    depart_h: int,
    depart_m: int,
) -> list[dict]:
    """
    Walk the ordered stop list, insert break pseudo-rows at the correct
    positions, and set wall-clock eta_str on every delivery/branch row.

    OR-Tools arrival times are "compressed" (productive time, no breaks).
    Breaks are inserted separately at their own thresholds so the driver can
    make additional stops between the rest break and the lunch break:

      • After 2h productive  — 15-min rest
      • After 3h productive  — 60-min lunch  (separate trigger, not back-to-back)
      • After 4h 45m productive (= 6h wall-clock) — 15-min optional rest
    """
    if not stops:
        return []

    needs_rest1   = end_compressed >= _BREAK_REST1_COMPRESSED
    needs_lunch   = end_compressed >= _BREAK_LUNCH_COMPRESSED
    has_optional  = end_compressed > _BREAK_OPTIONAL_COMPRESSED

    rest1_done    = False
    lunch_done    = False
    optional_done = False
    result: list[dict] = []

    # Pre-compute wall-clock times for each break start so they are consistent
    # regardless of which stop they are inserted before.
    # At the rest1 trigger point no breaks have occurred yet, so wall = compressed.
    _rest1_wall_start  = _BREAK_REST1_COMPRESSED                              # e.g. 2h → 8:00 AM
    _rest1_wall_end    = _rest1_wall_start + _BREAK_REST1_SEC                 # 8:15 AM
    # At the lunch trigger point rest1 has already happened (+15 min offset).
    _lunch_wall_start  = _BREAK_LUNCH_COMPRESSED + _BREAK_REST1_SEC           # e.g. 3h+15m → 9:15 AM
    _lunch_wall_end    = _lunch_wall_start + _BREAK_LUNCH_SEC                 # 10:15 AM

    for stop, arr in zip(stops, arrivals_compressed):
        # 15-min rest — insert before the first stop whose compressed arrival ≥ 2h.
        if needs_rest1 and not rest1_done and arr >= _BREAK_REST1_COMPRESSED:
            result.append(_make_break_row(
                "REST BREAK", "15-min rest", 15,
                "DOT required — after hour 2",
                seconds_to_wallclock(_rest1_wall_start, depart_h, depart_m),
                seconds_to_wallclock(_rest1_wall_end,   depart_h, depart_m),
                "rest1",
            ))
            rest1_done = True

        # 60-min lunch — insert before the first stop whose compressed arrival ≥ 3h.
        # This is intentionally a separate threshold so the driver can make stops
        # between the rest break and the lunch break.
        if needs_lunch and not lunch_done and arr >= _BREAK_LUNCH_COMPRESSED:
            result.append(_make_break_row(
                "LUNCH BREAK", "60-min lunch", 60,
                "Required — complete before hour 5",
                seconds_to_wallclock(_lunch_wall_start, depart_h, depart_m),
                seconds_to_wallclock(_lunch_wall_end,   depart_h, depart_m),
                "lunch",
            ))
            lunch_done = True

        # Optional 2nd rest (15 min) before any stop past the 4h45m compressed mark
        # (= 6h wall-clock once the 75-min mandatory block is counted).
        if has_optional and not optional_done and arr >= _BREAK_OPTIONAL_COMPRESSED:
            result.append(_make_break_row(
                "REST BREAK", "15-min rest", 15,
                "DOT required — after hour 6",
                seconds_to_wallclock(6 * 3600, depart_h, depart_m),
                seconds_to_wallclock(6 * 3600 + 15 * 60, depart_h, depart_m),
                "rest2",
            ))
            optional_done = True

        wall_arr_sec = _compressed_to_wall_sec(arr, has_optional)
        stop["eta_str"]      = seconds_to_wallclock(wall_arr_sec, depart_h, depart_m)
        stop["out_time_str"] = seconds_to_wallclock(wall_arr_sec + stop.get("svc_min", 0) * 60, depart_h, depart_m)
        result.append(stop)

    # Any breaks that fall after the last delivery but before returning to depot
    if needs_rest1 and not rest1_done:
        result.append(_make_break_row(
            "REST BREAK", "15-min rest", 15,
            "DOT required — after hour 2",
            seconds_to_wallclock(_rest1_wall_start, depart_h, depart_m),
            seconds_to_wallclock(_rest1_wall_end,   depart_h, depart_m),
            "rest1",
        ))
    if needs_lunch and not lunch_done:
        result.append(_make_break_row(
            "LUNCH BREAK", "60-min lunch", 60,
            "Required — complete before hour 5",
            seconds_to_wallclock(_lunch_wall_start, depart_h, depart_m),
            seconds_to_wallclock(_lunch_wall_end,   depart_h, depart_m),
            "lunch",
        ))
    if has_optional and not optional_done:
        result.append(_make_break_row(
            "REST BREAK", "15-min rest", 15,
            "DOT required — after hour 6",
            seconds_to_wallclock(6 * 3600, depart_h, depart_m),
            seconds_to_wallclock(6 * 3600 + 15 * 60, depart_h, depart_m),
            "rest2",
        ))

    return result


def _wall_to_shift(
    wall_minutes: int | None,
    depart_offset_sec: int,
    max_shift_sec: int,
    default: int,
) -> int:
    """Convert a wall-clock minute value to seconds-since-shift-start."""
    if wall_minutes is None:
        return default
    wall_sec  = wall_minutes * 60
    shift_sec = wall_sec - depart_offset_sec
    return max(0, min(max_shift_sec, shift_sec))


def _build_folium_map(
    result_stops: list[list[dict]],
    coords: list[tuple[float, float]],
    out_path: Path,
    date_label: str,
) -> None:
    """Generate a colour-coded interactive HTML map with folium."""
    import folium

    depot_lat, depot_lon = coords[0]
    m = folium.Map(location=[depot_lat, depot_lon], zoom_start=11, tiles="OpenStreetMap")

    # Depot marker
    folium.Marker(
        location=[depot_lat, depot_lon],
        popup=folium.Popup(f"<b>DEPOT</b><br>{DEPOT_ADDRESS}", max_width=250),
        icon=folium.Icon(color="black", icon="home", prefix="fa"),
        tooltip="Depot",
    ).add_to(m)

    for truck_idx, stops in enumerate(result_stops):
        color = _TRUCK_COLORS[truck_idx % len(_TRUCK_COLORS)]
        prev  = (depot_lat, depot_lon)

        for stop_num, stop in enumerate(stops):
            if stop.get("is_break"):
                continue
            loc = (stop["lat"], stop["lon"])

            geo_warning = (
                '<br><span style="color:red;font-weight:bold">⚠ GEOCODE FAILED — verify address</span>'
                if not stop.get("geocode_ok", True) else ""
            )
            popup_html = (
                f"<b>Truck {truck_idx + 1} — Stop {stop_num + 1}</b><br>"
                f"Order: {stop['order_num']}<br>"
                f"Customer: {stop['cust_name']}<br>"
                f"Address: {stop['addr1']}, {stop['city']}<br>"
                f"ETA: {stop.get('eta_str', '?')}  |  Svc: {stop['svc_min']} min<br>"
                f"{stop.get('notes_flags', '')}"
                f"{geo_warning}"
            )

            folium.CircleMarker(
                location=loc,
                radius=14,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.85,
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=f"T{truck_idx + 1} #{stop_num + 1}: {stop['cust_name']}",
            ).add_to(m)

            # Stop number label
            folium.Marker(
                location=loc,
                icon=folium.DivIcon(
                    html=f'<div style="'
                         f'font-size:11px;font-weight:bold;color:white;'
                         f'text-align:center;line-height:18px;'
                         f'">{stop_num + 1}</div>',
                    icon_size=(18, 18),
                    icon_anchor=(9, 9),
                ),
            ).add_to(m)

            # Route line
            folium.PolyLine(
                locations=[prev, loc],
                color=color,
                weight=3,
                opacity=0.75,
                tooltip=f"Truck {truck_idx + 1}",
            ).add_to(m)
            prev = loc

        # Return line to depot (if truck has stops)
        if stops:
            folium.PolyLine(
                locations=[prev, (depot_lat, depot_lon)],
                color=color,
                weight=2,
                opacity=0.4,
                dash_array="6 4",
            ).add_to(m)

    # Legend
    legend_html = (
        f'<div style="position:fixed;bottom:30px;left:30px;z-index:1000;'
        f'background:white;padding:10px;border:1px solid #ccc;border-radius:4px;'
        f'font-size:13px;">'
        f'<b>OMP Routes — {date_label}</b><br>'
    )
    for i, color in enumerate(_TRUCK_COLORS[: len(result_stops)]):
        real_stops = [s for s in result_stops[i] if not s.get("is_break")]
        if real_stops:
            legend_html += (
                f'<span style="color:{color}">&#9632;</span> '
                f'Truck {i + 1} ({len(real_stops)} stops)<br>'
            )
    legend_html += "</div>"
    m.get_root().html.add_child(folium.Element(legend_html))

    m.save(str(out_path))


if __name__ == "__main__":
    main()
