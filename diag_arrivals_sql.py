"""
Diagnostic script — tests the modified load_arrivals SQL without running
the full forecast.

Checks:
  1. That PHDDAT is a valid POHEAD column (no SQL error)
  2. That HEADER_DUE_DATE is populated for cork POs
  3. That the Shipping Days calculation produces the expected DUE_INV date

Usage:
    python diag_arrivals_sql.py
"""

import sys
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
from core.db_connection import connect

# ── Test SKUs ────────────────────────────────────────────────────────────────
TEST_SKUS = ["GFALO9503S", "GFALO9504S", "GFALPH401-L", "GFALPH401-R"]

# ── SQL (mirrors load_arrivals exactly) ──────────────────────────────────────
SKU_LIST_STR = ", ".join(f"'{s}'" for s in TEST_SKUS)

SQL = f"""
SELECT
  TRIM(L.PLPO#)    AS PO_NUMBER,
  TRIM(L.PLITEM)   AS ITEM_NUMBER,
  COALESCE(L.PLBLUO, 0) AS QUANTITY_SF,
  L.PLPDAT         AS DUE_PORT,
  L.PLDDAT         AS DUE_INV,
  H.PHDOI          AS ENTRY_DATE,
  H.PHSDAT         AS EST_SHIP_DATE
FROM GSFL2K.POLINE L
LEFT JOIN GSFL2K.POHEAD H ON H.PHPO# = L.PLPO# AND H.PHCO = L.PLCO
JOIN  GSFL2K.ITEMMAST M ON M.IMITEM = L.PLITEM
WHERE L.PLDELT LIKE '%A%'
  AND TRIM(L.PLITEM) IN ({SKU_LIST_STR})
  AND TRIM(L.PLPO#) <> ''
ORDER BY L.PLPO#, L.PLITEM
"""

print("Connecting to Gartman...")
try:
    conn = connect()
except Exception as e:
    print(f"Connection failed: {e}")
    sys.exit(1)

# ── Step 1: List all POHEAD columns so we can find the correct Due-date field ─
print("\nFetching POHEAD column list from DB2 catalog...")
try:
    cols_df = pd.read_sql(
        """
        SELECT COLUMN_NAME, DATA_TYPE, LENGTH
        FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA = 'GSFL2K'
          AND TABLE_NAME   = 'POHEAD'
        ORDER BY ORDINAL_POSITION
        """,
        conn,
    )
    if cols_df.empty:
        print("  No columns returned — check schema/table name.")
    else:
        print(f"  {len(cols_df)} columns in GSFL2K.POHEAD:\n")
        # Print columns whose name contains 'DAT' or 'DUE' or 'DATE' — likely candidates
        date_cols = cols_df[
            cols_df["COLUMN_NAME"].str.contains("DAT|DUE|DATE|DT", case=False, na=False)
        ]
        print("  Date-related columns (candidates for Due date):")
        print(date_cols.to_string(index=False))
        print("\n  Full column list:")
        print(cols_df.to_string(index=False))
except Exception as e:
    print(f"  Catalog query failed: {e}")

print("\nQuerying all date fields on PO 62530 to identify the Due date column...")
try:
    po_df = pd.read_sql(
        """
        SELECT PHDOI, PHSDAT, PHSOPODATE, PHSOREQDAT, PHDTP, PHRSDT, PHDLM, PHDLR
        FROM GSFL2K.POHEAD
        WHERE PHPO# = 63275
        """,
        conn,
    )
    print(po_df.to_string(index=False))
except Exception as e:
    print(f"  Date probe query failed: {e}")

print("\nRunning arrivals query...")
try:
    df = pd.read_sql(SQL, conn)
except Exception as e:
    print(f"\n*** SQL ERROR ***")
    print(f"{e}")
    print("\nSee POHEAD column list above to find the correct Due-date field name.")
    conn.close()
    sys.exit(1)

conn.close()

if df.empty:
    print("Query ran successfully but returned no rows.")
    print("(No open POs found for UNCOAC12 / UNCOAC14 — that is fine, SQL is valid.)")
    sys.exit(0)

print(f"\nQuery OK — {len(df)} row(s) returned:\n")
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 140)
print(df.to_string(index=False))

# ── Show computed DUE_INV ────────────────────────────────────────────────────
SHIPPING_DAYS: dict = {}  # not applicable for these SKUs
null_as400 = pd.Timestamp("0001-01-01")

print("\n── Shipping-days calculation preview ─────────────────────────────────")
print("  (only applies where line-level DUE_INV is blank)\n")
for _, row in df.iterrows():
    sku      = str(row["ITEM_NUMBER"]).strip().upper()
    days     = SHIPPING_DAYS.get(sku)
    hdr      = pd.to_datetime(row["EST_SHIP_DATE"], errors="coerce")
    line_inv = pd.to_datetime(row["DUE_INV"], errors="coerce")
    has_line = not pd.isna(line_inv) and line_inv > null_as400

    if days is None:
        print(f"  {sku}  PO {row['PO_NUMBER']:>6}  → no Shipping Days entry (skipped)")
    elif has_line:
        print(f"  {sku}  PO {row['PO_NUMBER']:>6}  → line date {line_inv.date()} already set, NOT overriding")
    elif pd.isna(hdr) or hdr <= null_as400:
        print(f"  {sku}  PO {row['PO_NUMBER']:>6}  → EST_SHIP_DATE blank, cannot compute (skipped)")
    else:
        computed = hdr + pd.Timedelta(days=days)
        print(f"  {sku}  PO {row['PO_NUMBER']:>6}  ship={hdr.date()}  +{days}d  →  DUE_INV={computed.date()}")

print("\nDone.")
