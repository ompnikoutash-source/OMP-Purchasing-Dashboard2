"""
Generate a print-ready Excel workbook with one route sheet per truck + a summary tab.
"""

from __future__ import annotations

from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins


# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_HEADER_FONT  = Font(bold=True, color="FFFFFF", size=10, name="Calibri")
_HEADER_FILL  = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
_TITLE_FONT   = Font(bold=True, size=14, color="1F4E79", name="Calibri")
_SUBTIT_FONT  = Font(italic=True, size=9, color="595959", name="Calibri")
_DATA_FONT    = Font(size=10, name="Calibri")
_BOLD_FONT    = Font(bold=True, size=10, name="Calibri")
_WARN_FILL    = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
_ALT_FILL     = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
_TOTAL_FILL   = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
_BRANCH_FILL    = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")  # light green
_BRANCH_FONT    = Font(bold=True, size=10, color="375623", name="Calibri")  # dark green text
_BREAK_FILL     = PatternFill(start_color="FFD966", end_color="FFD966", fill_type="solid")  # amber
_BREAK_FONT     = Font(bold=True, size=10, color="7F6000", name="Calibri")  # dark gold text
_XFER_FILL      = PatternFill(start_color="DAEEF3", end_color="DAEEF3", fill_type="solid")  # light teal
_XFER_FONT      = Font(bold=True, size=10, color="17375E", name="Calibri")  # dark navy text

# Truck accent colors for the summary sheet
_TRUCK_COLORS = ["4472C4", "ED7D31", "70AD47", "FFC000", "9B59B6"]
_TRUCK_FILLS  = [
    PatternFill(start_color=c, end_color=c, fill_type="solid")
    for c in _TRUCK_COLORS
]

_ROUTE_COLUMNS = [
    ("Stop #",          5),
    ("Order #",         11),
    ("Customer",        28),
    ("Ship-To Address", 32),
    ("City / ST / Zip", 22),
    ("In Time",         10),
    ("Out Time",        10),
    ("Svc (min)",       9),
    ("Delivery Notes",  44),
    ("Geocode",         11),
]

DEPOT_LABEL = "7751 Hayvenhurst Ave, Van Nuys CA 91406"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_route_workbook(
    result_stops: list[list[dict]],
    date_obj: datetime,
    depart_hour: int = 7,
    depart_minute: int = 0,
    total_travel_sec: int = 0,
    end_times_sec: list[int] | None = None,
    all_orders: list[dict] | None = None,
) -> Workbook:
    """
    Build and return an openpyxl Workbook.

    Args:
        result_stops: List of K lists (one per truck), each containing stop dicts.
          Each stop dict must have:
            order_num, cust_name, addr1, city, state, zip,
            eta_str, svc_min, notes_flags, geocode_ok (bool)
        date_obj: The delivery date (used for sheet titles).
        depart_hour/depart_minute: Departure time for subtitle.
        total_travel_sec: Combined drive time across all trucks (for summary).
        end_times_sec: Seconds-since-shift-start when each truck returns to depot.
        all_orders: Flat list of every order/stop/transfer for the master tab.
    """
    wb = Workbook()
    # Remove the default sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    date_label = _fmt_date(date_obj) if hasattr(date_obj, "strftime") else str(date_obj)
    depart_str = f"{depart_hour}:{depart_minute:02d} {'AM' if depart_hour < 12 else 'PM'}"
    end_times  = end_times_sec or [0] * len(result_stops)

    # Summary sheet first
    _write_summary_sheet(wb, result_stops, date_label, end_times, depart_hour, depart_minute)

    # One sheet per truck
    for truck_idx, stops in enumerate(result_stops):
        truck_num = truck_idx + 1
        _write_truck_sheet(wb, truck_num, stops, date_label, depart_str, truck_idx)

    # All-orders master tab at far right
    if all_orders is not None:
        _write_all_orders_sheet(wb, all_orders, date_label)

    return wb


# ---------------------------------------------------------------------------
# Summary sheet
# ---------------------------------------------------------------------------

def _write_summary_sheet(
    wb: Workbook,
    result_stops: list[list[dict]],
    date_label: str,
    end_times_sec: list[int],
    depart_hour: int = 7,
    depart_minute: int = 0,
) -> None:
    ws = wb.create_sheet(title="Summary")

    # Print setup
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth  = 1
    ws.page_setup.paperSize   = ws.PAPERSIZE_LETTER
    ws.page_margins = PageMargins(left=0.5, right=0.5, top=0.75, bottom=0.75)

    # Title
    ws.merge_cells("A1:G1")
    ws["A1"].value     = f"DISPATCH SUMMARY — {date_label}"
    ws["A1"].font      = _TITLE_FONT
    ws["A1"].alignment = Alignment(horizontal="center")

    ws.merge_cells("A2:G2")
    ws["A2"].value     = f"Depot: {DEPOT_LABEL}"
    ws["A2"].font      = _SUBTIT_FONT
    ws["A2"].alignment = Alignment(horizontal="center")

    # Header row 4
    headers = ["Truck", "Driver", "# Stops", "Drive Time", "Svc Time", "Total Shift", "Est. Return"]
    col_widths = [8, 22, 10, 14, 14, 14, 14]
    for col_idx, (hdr, width) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=4, column=col_idx, value=hdr)
        cell.font      = _HEADER_FONT
        cell.fill      = _HEADER_FILL
        cell.border    = _BORDER
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[4].height = 20

    total_stops = 0
    total_svc_min = 0

    for truck_idx, stops in enumerate(result_stops):
        r = 5 + truck_idx
        truck_num = truck_idx + 1
        num_stops = sum(1 for s in stops if not s.get("is_break"))
        svc_min   = sum(s["svc_min"] for s in stops if not s.get("is_break"))
        total_stops   += num_stops
        total_svc_min += svc_min

        end_sec  = end_times_sec[truck_idx] if truck_idx < len(end_times_sec) else 0
        svc_sec  = svc_min * 60
        drv_sec  = max(0, end_sec - svc_sec)
        drv_str  = _fmt_min(drv_sec // 60) if stops else "—"
        tot_str  = _fmt_min(end_sec // 60) if stops else "—"
        ret_str  = _fmt_wallclock(end_sec, depart_hour, depart_minute) if stops else "—"

        truck_fill = PatternFill(
            start_color=_TRUCK_COLORS[truck_idx % len(_TRUCK_COLORS)],
            end_color=_TRUCK_COLORS[truck_idx % len(_TRUCK_COLORS)],
            fill_type="solid",
        )

        values = [
            f"Truck {truck_num}",
            "",              # Driver — dispatcher fills in
            num_stops,
            drv_str,
            _fmt_min(svc_min),
            tot_str,
            ret_str,
        ]
        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col_idx, value=val)
            cell.border    = _BORDER
            cell.alignment = Alignment(horizontal="center" if col_idx != 2 else "left")
            cell.font      = Font(bold=True, color="FFFFFF", name="Calibri", size=10) if col_idx == 1 else _DATA_FONT
            if col_idx == 1:
                cell.fill = truck_fill

    # Totals row
    total_row = 5 + len(result_stops)
    ws.merge_cells(f"A{total_row}:B{total_row}")
    cell = ws.cell(row=total_row, column=1, value="TOTALS")
    cell.font   = _BOLD_FONT
    cell.fill   = _TOTAL_FILL
    cell.border = _BORDER

    ws.cell(row=total_row, column=3, value=total_stops).border = _BORDER
    ws.cell(row=total_row, column=3).font = _BOLD_FONT
    ws.cell(row=total_row, column=3).fill = _TOTAL_FILL

    ws.cell(row=total_row, column=5, value=_fmt_min(total_svc_min)).border = _BORDER
    ws.cell(row=total_row, column=5).font = _BOLD_FONT
    ws.cell(row=total_row, column=5).fill = _TOTAL_FILL

    for col_idx in [2, 4, 6, 7]:
        ws.cell(row=total_row, column=col_idx).border = _BORDER
        ws.cell(row=total_row, column=col_idx).fill   = _TOTAL_FILL


# ---------------------------------------------------------------------------
# Truck route sheet
# ---------------------------------------------------------------------------

def _write_truck_sheet(
    wb: Workbook,
    truck_num: int,
    stops: list[dict],
    date_label: str,
    depart_str: str,
    truck_idx: int,
) -> None:
    ws = wb.create_sheet(title=f"Truck {truck_num}")

    # Print setup: landscape, fit to 1 page wide, letter paper
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth  = 1
    ws.page_setup.fitToHeight = 0       # allow multiple pages vertically
    ws.page_setup.paperSize   = ws.PAPERSIZE_LETTER
    ws.page_margins = PageMargins(left=0.5, right=0.5, top=0.75, bottom=0.75)
    ws.print_title_rows = "4:4"         # repeat header on every printed page

    accent_color = _TRUCK_COLORS[truck_idx % len(_TRUCK_COLORS)]
    accent_fill  = PatternFill(start_color=accent_color, end_color=accent_color, fill_type="solid")

    num_cols = len(_ROUTE_COLUMNS)

    # Row 1: title
    ws.merge_cells(f"A1:{get_column_letter(num_cols)}1")
    ws["A1"].value     = f"DELIVERY ROUTE — TRUCK {truck_num} — {date_label}"
    ws["A1"].font      = Font(bold=True, size=14, color=accent_color, name="Calibri")
    ws["A1"].alignment = Alignment(horizontal="center")

    # Row 2: depot / departure info
    ws.merge_cells(f"A2:{get_column_letter(num_cols)}2")
    ws["A2"].value     = f"Depart: {DEPOT_LABEL}  |  Departure: {depart_str}"
    ws["A2"].font      = _SUBTIT_FONT
    ws["A2"].alignment = Alignment(horizontal="center")

    # Row 3: blank spacer
    ws.row_dimensions[3].height = 6

    # Row 4: column headers
    for col_idx, (hdr, width) in enumerate(_ROUTE_COLUMNS, start=1):
        cell = ws.cell(row=4, column=col_idx, value=hdr)
        cell.font      = _HEADER_FONT
        cell.fill      = accent_fill
        cell.border    = _BORDER
        cell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[4].height = 28

    if not stops:
        ws.merge_cells(f"A5:{get_column_letter(num_cols)}5")
        ws["A5"].value     = "No stops assigned to this truck."
        ws["A5"].font      = Font(italic=True, color="888888", name="Calibri")
        ws["A5"].alignment = Alignment(horizontal="center")
        return

    # Data rows
    delivery_num = 0   # sequential counter for delivery/branch stops (skips break rows)
    alt_counter  = 0   # alternating fill counter (skips break rows)

    for row_offset, stop in enumerate(stops):
        r = 5 + row_offset
        is_break  = stop.get("is_break",  False)
        is_branch = stop.get("is_branch", False)
        geo_fail  = not stop.get("geocode_ok", True)

        if is_break:
            row_fill = _BREAK_FILL
            row_font = _BREAK_FONT
        elif is_branch:
            row_fill = _BRANCH_FILL
            row_font = _BRANCH_FONT
        elif geo_fail:
            row_fill = _WARN_FILL
            row_font = _DATA_FONT
        else:
            row_fill = _ALT_FILL if alt_counter % 2 == 0 else None
            row_font = _DATA_FONT

        if not is_break:
            delivery_num += 1
            alt_counter  += 1

        city_st_zip = (
            f"{stop.get('city','').strip()}, "
            f"{stop.get('state','').strip()} "
            f"{stop.get('zip','').strip()}"
        ).strip(", ")

        values = [
            "" if is_break else delivery_num,
            stop.get("order_num", ""),
            stop.get("cust_name", ""),
            stop.get("addr1", ""),
            city_st_zip,
            stop.get("eta_str", ""),
            stop.get("out_time_str", ""),
            stop.get("svc_min", 0),
            stop.get("notes_flags", ""),
            "" if stop.get("geocode_ok", True) else "CHECK ADDRESS",
        ]

        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col_idx, value=val)
            cell.font      = row_font
            cell.border    = _BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if col_idx in (1, 6, 7, 8) else "left")
            if row_fill:
                cell.fill = row_fill

        ws.row_dimensions[r].height = 30 if is_break else 42

    # Totals row
    total_row = 5 + len(stops)
    ws.merge_cells(f"A{total_row}:G{total_row}")
    cell = ws.cell(row=total_row, column=1, value="TOTALS")
    cell.font   = _BOLD_FONT
    cell.fill   = _TOTAL_FILL
    cell.border = _BORDER
    cell.alignment = Alignment(horizontal="right")

    total_svc = sum(s.get("svc_min", 0) for s in stops if not s.get("is_break"))
    svc_cell = ws.cell(row=total_row, column=8, value=total_svc)
    svc_cell.font   = _BOLD_FONT
    svc_cell.fill   = _TOTAL_FILL
    svc_cell.border = _BORDER
    svc_cell.alignment = Alignment(horizontal="center")

    for col_idx in [9, 10]:
        c = ws.cell(row=total_row, column=col_idx)
        c.border = _BORDER
        c.fill   = _TOTAL_FILL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_all_orders_sheet(
    wb: Workbook,
    all_orders: list[dict],
    date_label: str,
) -> None:
    """
    Write a master 'All Orders' tab listing every delivery stop, branch stop,
    and transfer order for the day — one row each.

    Row colours:
      DELIVERY  — alternating light blue / white
      BRANCH    — light green
      TRANSFER  — light teal
    """
    ws = wb.create_sheet(title="All Orders")

    # Print setup
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth  = 1
    ws.page_setup.fitToHeight = 0
    ws.page_setup.paperSize   = ws.PAPERSIZE_LETTER
    ws.page_margins = PageMargins(left=0.5, right=0.5, top=0.75, bottom=0.75)
    ws.print_title_rows = "4:4"

    # Row 1: title
    num_cols = 13
    ws.merge_cells(f"A1:{get_column_letter(num_cols)}1")
    ws["A1"].value     = f"ALL ORDERS — {date_label}"
    ws["A1"].font      = _TITLE_FONT
    ws["A1"].alignment = Alignment(horizontal="center")

    # Row 2: legend
    ws.merge_cells(f"A2:{get_column_letter(num_cols)}2")
    ws["A2"].value     = "DELIVERY = customer stop  |  BRANCH = satellite branch visit  |  TRANSFER = branch-to-branch (handled during branch visit)"
    ws["A2"].font      = _SUBTIT_FONT
    ws["A2"].alignment = Alignment(horizontal="center")

    # Row 3: spacer
    ws.row_dimensions[3].height = 6

    # Row 4: column headers
    columns = [
        ("#",                    5),
        ("Order #",             11),
        ("Type",                12),
        ("Customer / Destination", 30),
        ("Ship-To Address",     30),
        ("City / ST / Zip",     22),
        ("Truck",                8),
        ("Stop #",               8),
        ("In Time",             10),
        ("Out Time",            10),
        ("Svc (min)",            9),
        ("Notes",               44),
        ("Pick Ticket Printer", 20),
    ]
    for col_idx, (hdr, width) in enumerate(columns, start=1):
        cell = ws.cell(row=4, column=col_idx, value=hdr)
        cell.font      = _HEADER_FONT
        cell.fill      = _HEADER_FILL
        cell.border    = _BORDER
        cell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[4].height = 28

    if not all_orders:
        ws.merge_cells(f"A5:{get_column_letter(num_cols)}5")
        ws["A5"].value     = "No orders for this date."
        ws["A5"].font      = Font(italic=True, color="888888", name="Calibri")
        ws["A5"].alignment = Alignment(horizontal="center")
        return

    alt_counter = 0  # for alternating delivery rows
    for row_offset, order in enumerate(all_orders):
        r        = 5 + row_offset
        row_type = order.get("row_type", "DELIVERY")
        geo_fail = not order.get("geocode_ok", True)

        if row_type == "TRANSFER":
            row_fill = _XFER_FILL
            row_font = _XFER_FONT
        elif row_type == "BRANCH":
            row_fill = _BRANCH_FILL
            row_font = _BRANCH_FONT
        elif geo_fail:
            row_fill = _WARN_FILL
            row_font = _DATA_FONT
        else:
            row_fill = _ALT_FILL if alt_counter % 2 == 0 else None
            row_font = _DATA_FONT

        if row_type == "DELIVERY":
            alt_counter += 1

        city_st_zip = (
            f"{order.get('city','').strip()}, "
            f"{order.get('state','').strip()} "
            f"{order.get('zip','').strip()}"
        ).strip(", ")

        svc_val = order.get("svc_min")
        svc_display = svc_val if svc_val is not None else ""

        values = [
            row_offset + 1,
            order.get("order_num", ""),
            row_type,
            order.get("cust_name", ""),
            order.get("addr1", ""),
            city_st_zip,
            order.get("truck_num") or "",
            order.get("stop_num") or "",
            order.get("eta_str", ""),
            order.get("out_time_str", ""),
            svc_display,
            order.get("notes_flags", ""),
            order.get("printer", ""),
        ]

        _CENTER_COLS = {1, 3, 7, 8, 9, 10, 11}
        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col_idx, value=val)
            cell.font      = row_font
            cell.border    = _BORDER
            cell.alignment = Alignment(
                wrap_text=True, vertical="top",
                horizontal="center" if col_idx in _CENTER_COLS else "left",
            )
            if row_fill:
                cell.fill = row_fill

        ws.row_dimensions[r].height = 36

    # Totals footer
    total_row = 5 + len(all_orders)
    ws.merge_cells(f"A{total_row}:{get_column_letter(num_cols)}{total_row}")
    cell = ws.cell(row=total_row, column=1,
                   value=f"Total rows: {len(all_orders)}")
    cell.font      = _BOLD_FONT
    cell.fill      = _TOTAL_FILL
    cell.border    = _BORDER
    cell.alignment = Alignment(horizontal="left")


def _fmt_date(dt: datetime) -> str:
    """Format a date as 'Tuesday, March 4, 2026' (no leading zero on day, cross-platform)."""
    return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")


def _fmt_min(minutes: int) -> str:
    """Format minutes as 'Xh Ym' or 'Ym'."""
    h = minutes // 60
    m = minutes % 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _fmt_wallclock(shift_seconds: int, depart_hour: int, depart_minute: int) -> str:
    """Convert seconds-since-shift-start to a wall-clock string like '3:42 PM'."""
    total_minutes = depart_hour * 60 + depart_minute + shift_seconds // 60
    total_minutes %= 1440
    h = total_minutes // 60
    m = total_minutes % 60
    period = "AM" if h < 12 else "PM"
    display_h = h if h <= 12 else h - 12
    if display_h == 0:
        display_h = 12
    return f"{display_h}:{m:02d} {period}"
