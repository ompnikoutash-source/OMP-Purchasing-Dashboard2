"""
Pull monthly item-level sales for one or more flooring family codes from Gartman
and cache the result as CSV for the correlation / HMM analysis.

Usage:
    .venv\\Scripts\\python.exe catalog_intelligence\\pull_family_sales.py --family EN
    .venv\\Scripts\\python.exe catalog_intelligence\\pull_family_sales.py --family EN PF SF --months 36
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.db_connection import connect  # noqa: E402
from active_items import active_item_conditions, retired_collection_clause, DEFAULT_PO_SINCE  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "data"

QUERY_TEMPLATE = """
SELECT
    TRIM(SL.SLITEM) AS ITEM,
    TRIM(IM.IMDESC) AS DESCRIPTION,
    TRIM(IM.IMCOLR) AS COLOR,
    TRIM(IMX.IMCOLLECT) AS COLLECTION,
    TRIM(IM.IMFMCD) AS FAMILY_CODE,
    YEAR(SL.SLDATE) AS SALE_YEAR,
    MONTH(SL.SLDATE) AS SALE_MONTH,
    SUM(SL.SLBLUS) AS SQFT_SOLD,
    ROUND(SUM(SL.SLBLUS * SL.SLPRIC), 2) AS SALES_DOLLARS
FROM GSFL2K.SHLINE SL
LEFT JOIN GSFL2K.ITEMMAST IM
    ON SL.SLITEM = IM.IMITEM
LEFT JOIN GSFL2K.ITEMXTRA IMX
    ON IM.IMITEM = IMX.IMXITM
WHERE
    IM.IMFMCD IN ({family_list}) AND
    SL.SLDATE BETWEEN '{start_date}' AND '{end_date}' AND
    TRIM(IM.IMDESC) NOT LIKE '%SAMPLE%' AND
    TRIM(IM.IMDESC) NOT LIKE '%SPECIAL%' AND
    TRIM(IM.IMDESC) NOT LIKE '%CUSTOM%' AND
    TRIM(IM.IMDESC) NOT LIKE 'DISC%NUED' AND
    TRIM(IM.IMDESC) NOT LIKE '%DO NOT USE%' AND
    TRIM(IM.IMDESC) NOT LIKE '%DON''T USE%' AND
    TRIM(IM.IMITEM) != 'GNTEST' AND
    {retired_collection_clause}
    {active_clause}
GROUP BY
    TRIM(SL.SLITEM),
    TRIM(IM.IMDESC),
    TRIM(IM.IMCOLR),
    TRIM(IMX.IMCOLLECT),
    TRIM(IM.IMFMCD),
    YEAR(SL.SLDATE),
    MONTH(SL.SLDATE)
ORDER BY
    ITEM,
    SALE_YEAR,
    SALE_MONTH
"""


def last_complete_month_end(today: date | None = None) -> date:
    """The last day of the most recently fully-completed calendar month.
    Sales analysis must never include the current, still-in-progress month --
    it's always partial (e.g. 17 of 31 days), so any month-over-month or
    trailing-window comparison that includes it looks artificially like a
    decline regardless of real momentum. Confirmed against a user-provided
    last-3-months-vs-prior-3 pivot (2026-08-17) that excluded the current
    month -- once matched here, every collection's percentage lined up with
    theirs to within rounding."""
    today = today or date.today()
    return today.replace(day=1) - pd.Timedelta(days=1)


def pull(families: list[str], months: int = 36, po_since: str | None = DEFAULT_PO_SINCE) -> pd.DataFrame:
    """po_since: only include items that are not flagged Dropped (IMDROP='D')
    and, if set, received a PO (real physical receipt, not just a sales row)
    on or after this date (YYYY-MM-DD) -- excludes old/discontinued/dropped
    colors that still carry historical sales. Pass None to skip the
    PO-recency check (the IMDROP check always applies)."""
    family_list = ", ".join(f"'{f.upper()}'" for f in families)
    active_clause = f"AND {active_item_conditions('IM', po_since=po_since)}"
    retired_clause = retired_collection_clause("TRIM(IMX.IMCOLLECT)")
    end_date = last_complete_month_end()
    start_date = (end_date.replace(day=1) - pd.DateOffset(months=months - 1)).date()
    query = QUERY_TEMPLATE.format(family_list=family_list, active_clause=active_clause, retired_collection_clause=retired_clause, start_date=start_date, end_date=end_date)
    conn = connect()
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    df.columns = [c.upper() for c in df.columns]
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull monthly family sales from Gartman.")
    parser.add_argument("--family", nargs="+", default=["EN"], help="Family code(s), e.g. EN PF SF DF")
    parser.add_argument("--months", type=int, default=36, help="Trailing months of history to pull")
    parser.add_argument("--po-since", type=str, default=DEFAULT_PO_SINCE, help="Only include items with a received PO on/after this date (YYYY-MM-DD). Empty string disables the filter.")
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path")
    args = parser.parse_args()

    df = pull(args.family, args.months, args.po_since or None)
    CACHE_DIR.mkdir(exist_ok=True)
    out = args.out or CACHE_DIR / f"{'_'.join(args.family).lower()}_monthly_sales.csv"
    df.to_csv(out, index=False)
    print(f"Pulled {len(df)} rows across {df['ITEM'].nunique()} items, {df['COLLECTION'].nunique()} collections.")
    print(f"Saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
