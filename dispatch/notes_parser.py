"""
Parse AS400 order text/notes to estimate stop service time and delivery time windows.

Notes come from the Sales Text Inquiry (SH81002) and look like:
    DELIVERY AFTER 9:00AM
    GATE CODE #7621
    EC//MD
    1 PLT, 2 BCKTS

Service time is volume-scaled:
    base = max(1, ceil(sf / 1000)) * 30 minutes
    Each 1,000 SF = 1 unit.  Modifiers (forklift, liftgate, floor delivery)
    also scale by unit count.  Pallets and buckets are count-based and do not
    scale with SF units (they already express quantity explicitly).
    Gate code is flat (opening a gate takes the same time regardless of order size).
"""

import math
import re


BASE_STOP_MINUTES = 30   # minutes per 1,000-SF unit (also the minimum base)
SF_PER_UNIT       = 1000 # SF threshold per unit


def parse_stop_duration(notes_text: str, quantity_sf: int = 0) -> dict:
    """
    Parse raw order notes text and return a dict with:
      service_minutes  : int  — estimated stop duration in minutes
      time_window_open : int|None — earliest arrival in minutes since midnight
      time_window_close: int|None — latest arrival in minutes since midnight
      flags            : list[str] — human-readable adjustments applied

    quantity_sf: total square footage for this order (from order lines).
                 0 or missing → treated as 1 unit (30-min base, unscaled modifiers).
    """
    text = (notes_text or "").upper()
    flags: list[str] = []

    # --- Volume-based base time -------------------------------------------
    # 1 unit  = up to 1,000 SF  → 30 min base
    # 2 units = up to 2,000 SF  → 60 min base
    # 3 units = up to 3,000 SF  → 90 min base  … etc.
    units = max(1, math.ceil(quantity_sf / SF_PER_UNIT)) if quantity_sf > 0 else 1
    minutes = BASE_STOP_MINUTES * units

    if units > 1:
        flags.append(f"{quantity_sf:,} SF → {units} units, base {minutes}min")

    # --- Count-based modifiers (not scaled by SF units) -------------------

    # Pallets: "1 PLT", "2 PLTS", "3 PALLETS", "1PLT"
    pallet_match = re.search(r"(\d+)\s*(?:PLT[S]?|PALLET[S]?)", text)
    if pallet_match:
        count = int(pallet_match.group(1))
        add = count * 15
        minutes += add
        flags.append(f"{count} pallet(s) +{add}min")

    # Buckets: "2 BCKTS", "3 BUCKETS", "2BCKTS"
    bucket_match = re.search(r"(\d+)\s*(?:BCKT[S]?|BUCKET[S]?)", text)
    if bucket_match:
        count = int(bucket_match.group(1))
        add = count * 2
        minutes += add
        flags.append(f"{count} bucket(s) +{add}min")

    # Gate code: flat regardless of order size (same time to punch in a code)
    if re.search(r"GATE\s*(?:CODE|#|:)", text):
        minutes += 5
        flags.append("gate code +5min")

    # --- SF-scaled modifiers (per 1,000-SF unit) --------------------------

    # Forklift available: reduces unload time, scaled by SF units
    if re.search(r"FORKLIFT|F\/L\s+(?:AVAIL|ON\s*SITE|ACCESS)", text):
        adj = 10 * units
        minutes -= adj
        flags.append(f"forklift -{adj}min")

    # Liftgate required: adds time, scaled by SF units
    if re.search(r"LIFT\s*GATE", text):
        adj = 10 * units
        minutes += adj
        flags.append(f"liftgate +{adj}min")

    # Floor delivery / hand unload (no equipment): scaled by SF units
    if re.search(r"FLOOR\s+DELIVERY|HAND\s+UNLOAD", text):
        adj = 15 * units
        minutes += adj
        flags.append(f"floor delivery +{adj}min")

    minutes = max(10, minutes)

    # --- Time windows (unchanged) -----------------------------------------

    open_window:  int | None = None
    close_window: int | None = None

    open_match = re.search(
        r"(?:DELIVERY\s+AFTER|DO\s+NOT\s+(?:DELIVER|ARRIVE)\s+BEFORE)\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?",
        text,
    )
    if open_match:
        open_window = _to_minutes(
            int(open_match.group(1)),
            int(open_match.group(2) or 0),
            open_match.group(3) or "",
        )

    close_match = re.search(
        r"(?:DELIVER\s+BY|MUST\s+(?:DELIVER|ARRIVE)\s+BEFORE)\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?",
        text,
    )
    if close_match:
        close_window = _to_minutes(
            int(close_match.group(1)),
            int(close_match.group(2) or 0),
            close_match.group(3) or "",
        )

    return {
        "service_minutes":   minutes,
        "time_window_open":  open_window,
        "time_window_close": close_window,
        "flags":             flags,
    }


def _to_minutes(hour: int, minute: int, ampm: str) -> int:
    """Convert hour/minute/ampm to minutes since midnight."""
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute
