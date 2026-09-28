"""
Diagnostic: Find the AS400 field names that control discontinued / dropped /
nonstocking status in GSFL2K.ITEMMAST.

Run once, paste the output back so the filter can be written correctly.

    python diag_itemmast_status_fields.py
"""
from __future__ import annotations
import pandas as pd
from core.db_connection import connect

conn = connect()

# ── 1. Show every column in ITEMMAST for the sample item from the screenshot ──
# ARSLP14 is a known active stocking item (Stocking Item = Y on screen).
# This lets us see what value a LIVE item has in every field.
print("=" * 70)
print("ALL ITEMMAST COLUMNS FOR ARSLP14  (known active stocking item)")
print("=" * 70)
df_sample = pd.read_sql(
    "SELECT * FROM GSFL2K.ITEMMAST WHERE TRIM(IMITEM) = 'ARSLP14'", conn
)
if df_sample.empty:
    print("  Item not found — check item number spelling")
else:
    for col in df_sample.columns:
        print(f"  {col:30s} = {repr(df_sample[col].iloc[0])}")

# ── 2. Show the distinct values of every short/flag column ──────────────────
# Focus on columns whose name contains common status-related substrings.
print()
print("=" * 70)
print("CANDIDATE STATUS / FLAG COLUMNS IN ITEMMAST  (distinct values)")
print("=" * 70)
keywords = ["STKI", "STAT", "DROP", "DISC", "INACT", "TYPE", "CLASS", "FLAG",
            "ACTV", "STATUS", "ACTIVE", "OBSOLET"]
if not df_sample.empty:
    for col in df_sample.columns:
        col_upper = col.upper()
        if any(k in col_upper for k in keywords):
            try:
                vals = pd.read_sql(
                    f"SELECT DISTINCT TRIM({col}) AS VAL FROM GSFL2K.ITEMMAST "
                    f"ORDER BY TRIM({col})",
                    conn,
                )["VAL"].tolist()
            except Exception as e:
                vals = [f"<error: {e}>"]
            print(f"  {col:30s} distinct values: {vals}")

# ── 3. Show all distinct values of IMSTKI (the Stocking Item field) ─────────
# The screen shows "Stocking Item Y ( /Y/M/A/T/V)" — so IMSTKI is the likely
# field name.  If it doesn't exist the query will error and tell us the real name.
print()
print("=" * 70)
print("IMSTKI (Stocking Item field) — distinct values across all items")
print("=" * 70)
try:
    df_stki = pd.read_sql(
        "SELECT TRIM(IMSTKI) AS VAL, COUNT(*) AS CNT "
        "FROM GSFL2K.ITEMMAST GROUP BY TRIM(IMSTKI) ORDER BY CNT DESC",
        conn,
    )
    print(df_stki.to_string(index=False))
except Exception as e:
    print(f"  IMSTKI not found or error: {e}")

# ── 4. Look for any column that contains 'D' or blank for ARSLP14 neighbours──
# Pull 20 items near ARSLP14 alphabetically so we can compare stocking vs
# non-stocking side-by-side if any show up.
print()
print("=" * 70)
print("SAMPLE OF 20 ITEMS NEAR ARSLP14 — all flag-like columns")
print("=" * 70)
try:
    df_near = pd.read_sql(
        "SELECT TRIM(IMITEM) AS ITEM, TRIM(IMDESC) AS DESC, "
        "TRIM(IMSTKI) AS STKI "
        "FROM GSFL2K.ITEMMAST "
        "WHERE TRIM(IMITEM) BETWEEN 'ARSLP10' AND 'ARSLP20' "
        "ORDER BY TRIM(IMITEM)",
        conn,
    )
    print(df_near.to_string(index=False))
except Exception as e:
    print(f"  Error: {e}")

conn.close()
print()
print("Done. Paste output above back into the chat.")
