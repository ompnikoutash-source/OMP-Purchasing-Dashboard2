"""
Shared "is this item still current" filter for the catalog_intelligence toolkit.

Two independent signals, both required:

1. ITEMMAST.IMDROP <> 'D' -- Gartman's own "Dropped?" status flag. Confirmed
   against real examples (GFVGO902, GFVGO909, GFCYO1005 -- reported as
   discontinued Villa Gialla/Canyon Crest colors -- all carry IMDROP='D').
   IMDROP also takes values 'G', 'X', 'E' with unconfirmed meaning (some 'G'
   items are literally described "DISCONTINUED", others look ordinary; some
   'X' items were maintained within the past year and look current) -- per
   user direction (2026-08-14) only 'D' is excluded until those are confirmed.
   Blank/space IMDROP is the normal active value.

2. A purchase-order RECEIPT on or after `po_since`: GSFL2K.ITEMRECH with
   IRSRC = 'P' is purchase receipts only (see inventory_received_365days.sql),
   and IRDATE is the actual physical arrival date, not a last-touched date.
   On its own this under-excludes -- a dropped item can carry one last
   closeout receipt inside the window -- which is why signal 1 exists; a
   sales row alone is even weaker evidence since sales/returns can still post
   against a dead item.

Default cutoff is a fixed calendar date (2025-01-01), not a rolling window --
per user direction (2026-08-15) a rolling 24-month window was still letting
through old collections/items with a stray PO receipt from 2+ years back.
A fixed date needs manual bumping over time but won't silently widen.
"""

from __future__ import annotations

DEFAULT_PO_SINCE = "2025-01-01"

# Collections retired at the business level even though 1-2 residual colors
# still post sales/PO activity within the item-level window (e.g. Villa Gialla's
# GFVGO903/GFVGO914 and Canyon Crest's GFCYO1008 all carry IMDROP=' ' and recent
# receipts, but the collections themselves are discontinued lines). No Gartman
# table encodes collection-level status -- GSFL2K.ITEMXTRA.IMXCOLLECT is a plain
# text field -- so this list is user-confirmed (2026-08-14), not derived.
RETIRED_COLLECTIONS = {"VILLA GIALLA", "CANYON CREST"}


def retired_collection_clause(collection_alias: str) -> str:
    """Boolean SQL fragment, true when `collection_alias` (a TRIM'd collection
    name column/expression) is NOT one of RETIRED_COLLECTIONS."""
    excluded = ", ".join(f"'{name}'" for name in sorted(RETIRED_COLLECTIONS))
    return f"(UPPER({collection_alias}) NOT IN ({excluded}) OR {collection_alias} IS NULL)"


def active_item_conditions(table_alias: str = "IM", item_alias: str | None = None, po_since: str | None = DEFAULT_PO_SINCE) -> str:
    """Boolean SQL fragment for a WHERE clause, true when `table_alias` (an
    alias for GSFL2K.ITEMMAST or a table with the same columns) is not
    Dropped and, if po_since is set, has a purchase receipt on or after that
    date (YYYY-MM-DD). Pass po_since=None to apply only the IMDROP check."""
    item_ref = item_alias or f"{table_alias}.IMITEM"
    dropped_check = f"{table_alias}.IMDROP <> 'D'"
    if not po_since:
        return dropped_check
    return f"""{dropped_check}
            AND EXISTS (
                SELECT 1 FROM GSFL2K.ITEMRECH IR
                WHERE IR.IRITEM = {item_ref}
                  AND IR.IRSRC = 'P'
                  AND IR.IRDATE >= '{po_since}'
            )"""
