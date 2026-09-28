"""
DB queries for the route optimizer.

Source table: GSFL2K.OOHEAD (Open Order Header) — orders that have not yet been
shipped or invoiced.  These are the orders that need to be loaded onto trucks.
Columns use the "OH" prefix (confirmed via --discover-schema on 2026-03-03).

Column configuration (confirmed from --discover-schema):
  - date_col = OHSDAT  (requested ship / delivery date, DATE type)
  - via_col  = OHVIA   (ship-via, CHAR 15; filter = 'OUR TRUCK')
  - addr1    = OHSTA1, addr2 = OHSTA2, city = OHSTA3  (ship-to address lines, CHAR 25)
  - state    = ""  (state embedded in OHSTA3, parsed in Python)
  - zip_col  = OHZIP   (NUMERIC 9, e.g. 902650000 → "90265")
  - notes    = GSFL2K.OOTEXT  (confirmed EXISTS; OTORD#, OTCMT1, OTCMT2, OTSEQ#)

No invoice-number filter is needed — OOHEAD contains only open orders by definition.
"""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd
import pyodbc


# ---------------------------------------------------------------------------
# Column configuration
# ---------------------------------------------------------------------------

class ColumnConfig(NamedTuple):
    date_col: str  # requested ship/delivery date: OHSDAT
    via_col:  str  # ship-via field:  OHVIA
    addr1:    str  # address line 1:  OHSTA1
    addr2:    str  # address line 2:  OHSTA2
    city:     str  # city/state combined: OHSTA3 (e.g. "MALIBU CA" or "MALIBU")
    state:    str  # separate state col: "" means extract from city field above
    zip_col:  str  # zip (may be NUMERIC): OHZIP


DEFAULT_COL_CONFIG = ColumnConfig(
    date_col="OHSDAT",  # ship/delivery date (DATE) — confirmed via --discover-schema
    via_col="OHVIA",    # ship-via method (CHAR 15)
    addr1="OHSTA1",     # ship-to address line 1 (CHAR 25)
    addr2="OHSTA2",     # ship-to address line 2 (CHAR 25)
    city="OHSTA3",      # contains "CITY  ST" or just "CITY" — parsed below (CHAR 25)
    state="",           # no dedicated state column; extracted from OHSTA3 in Python
    zip_col="OHZIP",    # NUMERIC 9 e.g. 902650000 → "90265"
)


# ---------------------------------------------------------------------------
# Schema discovery (run with --discover-schema)
# ---------------------------------------------------------------------------

_ROLE_PATTERNS: dict[str, list[str]] = {
    "date_col (ship/delivery date)": ["OHSDAT", "OOSDAT", "OOREQD", "OOSHPD", "OOSHIP", "OOSCHD"],
    "via_col  (ship via)":           ["OHVIA",  "OOVIA",  "OOVIAC", "OOCARR", "OOSHPV", "OOSVIA"],
    "addr1    (ship addr 1)":        ["OHSTA1", "OOSTA1", "OOADR1", "OOAD1",  "OOADDR"],
    "addr2    (ship addr 2)":        ["OHSTA2", "OOSTA2", "OOADR2", "OOAD2",  "OOADDR2"],
    "city     (ship city)":          ["OHSTA3", "OOSTA3", "OOCITY", "OOSCTY", "OOSTCT"],
    "state    (ship state)":         ["OHST",   "OOST",   "OOSST",  "OOSHST", "(embedded in city)"],
    "zip_col  (ship zip)":           ["OHZIP",  "OOZIP",  "OOPSTL", "OOZP5",  "OOPOST"],
    "order#":                        ["OHORD#", "OOORD#", "OOORDNO","OOORNO", "OOOR#"],
    "customer code":                 ["OHCUST", "OOCUST", "OOCUS",  "OOCNO",  "OOCUSTNO"],
    "company code":                  ["OHCO",   "OOCO",   "OOCONO", "OOCOMP"],
    "ship-to name":                  ["OHSTNM", "OOSTNM", "OOSNM",  "OOSHINM"],
}

_NOTE_TABLE_CANDIDATES = [
    "GSFL2K.OOTEXT",    # open order text (primary candidate for OOHEAD notes)
    "GSFL2K.OOXTRA",    # open order extra text (secondary candidate)
    "GSFL2K.SHTEXT",    # shipped order text (fallback — same order# range)
    "GSFL2K.SOTEXT",
    "GSFL2K.ORTEXT",
    "GSFL2K.SHNOTE",
    "GSFL2K.SHTXT",
    "GSFL2K.ORDNOTE",
    "GSFL2K.NOTEHEAD",
    "GSFL2K.NOTETEXT",
]


def discover_schema(conn: pyodbc.Connection) -> None:
    """Print all OOHEAD columns with role guesses, probe notes tables, show their columns."""
    print("\n" + "=" * 70)
    print("OOHEAD COLUMN DISCOVERY  (Open Order Header)")
    print("=" * 70)

    sql = """
        SELECT COLUMN_NAME, DATA_TYPE, LENGTH
        FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA = 'GSFL2K'
          AND TABLE_NAME = 'OOHEAD'
        ORDER BY ORDINAL_POSITION
    """
    try:
        df = pd.read_sql(sql, conn)
    except Exception as e:
        print(f"ERROR reading QSYS2.SYSCOLUMNS for OOHEAD: {e}")
        return

    if df.empty:
        print("  OOHEAD table not found in GSFL2K — verify the table name.")
        return

    col_names_upper = [c.upper() for c in df["COLUMN_NAME"].tolist()]

    print(f"\nAll {len(col_names_upper)} OOHEAD columns:\n")
    for _, row in df.iterrows():
        print(f"  {row['COLUMN_NAME']:<20}  {row['DATA_TYPE']:<10}  len={row['LENGTH']}")

    print("\n" + "-" * 70)
    print("SUGGESTED COLUMN MAPPINGS  (update DEFAULT_COL_CONFIG in dispatch/db.py)")
    print("-" * 70)
    for role, candidates in _ROLE_PATTERNS.items():
        found = next((c for c in candidates if c in col_names_upper), None)
        status = f"  FOUND -> {found}" if found else "  *** NOT FOUND -- check manually"
        print(f"  {role:<38}{status}")

    print("\n" + "=" * 70)
    print("NOTES TABLE DISCOVERY")
    print("=" * 70)
    for tbl in _NOTE_TABLE_CANDIDATES:
        probe = f"SELECT COUNT(*) AS N FROM {tbl}"
        try:
            result = pd.read_sql(probe, conn)
            count  = result["N"].iloc[0]
            print(f"\n  {tbl}  EXISTS  (rows ~ {count})")
            _print_table_columns(conn, tbl)
        except Exception:
            print(f"  {tbl:<40}  not found")

    print("\nDone. Update DEFAULT_COL_CONFIG then run --date to verify orders load.\n")


def _print_table_columns(conn: pyodbc.Connection, full_table: str) -> None:
    """Print column names for a discovered table (helps identify notes column names)."""
    schema, tbl_name = full_table.split(".")
    sql = f"""
        SELECT COLUMN_NAME, DATA_TYPE, LENGTH
        FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{tbl_name}'
        ORDER BY ORDINAL_POSITION
    """
    try:
        df = pd.read_sql(sql, conn)
        print(f"  {'Column':<22} {'Type':<12} Len")
        print(f"  {'-'*22} {'-'*12} ---")
        for _, row in df.iterrows():
            print(f"  {row['COLUMN_NAME']:<22} {row['DATA_TYPE']:<12} {row['LENGTH']}")
    except Exception as e:
        print(f"    (could not read columns: {e})")


# ---------------------------------------------------------------------------
# Orders query
# ---------------------------------------------------------------------------

def fetch_orders(
    conn: pyodbc.Connection,
    ship_date: str,
    col_config: ColumnConfig = DEFAULT_COL_CONFIG,
) -> pd.DataFrame:
    """
    Return all open OUR TRUCK orders in OOHEAD scheduled for the given ship date.

    ship_date: ISO date string 'YYYY-MM-DD' (e.g. '2026-03-04').
               The date column (OHSDAT) is compared as a DB2 DATE literal.

    OOHEAD contains only open/unshipped orders by definition — no invoice-number
    filter is needed.  These are the orders that will physically go on trucks.

    Returns DataFrame with columns:
        ORDER_NUM, CUST_CODE, CUST_NAME,
        SHIP_ADDR1, SHIP_ADDR2, SHIP_CITY, SHIP_STATE, SHIP_ZIP, FULL_ADDRESS
    """
    c = col_config

    # OHSTA3 contains "CITY  ST" combined — selected as SHIP_CITY, state parsed in Python
    state_expr = f"TRIM(H.{c.state})" if c.state else "''"

    sql = f"""
        SELECT
            TRIM(CHAR(H.OHORD#))            AS ORDER_NUM,
            TRIM(H.OHCUST)                  AS CUST_CODE,
            COALESCE(TRIM(C.CMNAME),
                     TRIM(H.OHSTNM),
                     TRIM(H.OHCUST))        AS CUST_NAME,
            TRIM(H.{c.addr1})               AS SHIP_ADDR1,
            TRIM(H.{c.addr2})               AS SHIP_ADDR2,
            TRIM(H.{c.city})                AS SHIP_CITY,
            {state_expr}                    AS SHIP_STATE,
            H.{c.zip_col}                   AS SHIP_ZIP_RAW
        FROM GSFL2K.OOHEAD H
        LEFT JOIN GSFL2K.CUSTMAST C
            ON C.CMCO = H.OHCO
           AND TRIM(C.CMCUST) = TRIM(H.OHCUST)
        WHERE H.{c.date_col} = DATE('{ship_date}')
          AND TRIM(H.{c.via_col}) = 'OUR TRUCK'
          AND H.OHCUST NOT LIKE '%OMP000%'
          AND H.OHCUST NOT LIKE '%INV000%'
          AND H.OHCUST NOT LIKE 'TRANSFER%'
        ORDER BY H.OHORD#
    """
    try:
        df = pd.read_sql(sql, conn)
    except Exception as e:
        _column_error_hint(e, col_config)
        raise

    # Post-process ZIP: NUMERIC(9) → 5-digit string
    df["SHIP_ZIP"] = df["SHIP_ZIP_RAW"].apply(_format_zip)
    df.drop(columns=["SHIP_ZIP_RAW"], inplace=True)

    # If state is empty, extract city and state from the combined SHSTA3 field
    if not c.state:
        parsed_cs = df["SHIP_CITY"].apply(lambda v: _parse_city_state(str(v or "")))
        df["SHIP_CITY"]  = [p[0] for p in parsed_cs]
        df["SHIP_STATE"] = [p[1] for p in parsed_cs]

    # Backfill blank ship-to addresses from CUSTMAST billing address
    df = _backfill_from_custmast(df, conn)

    # Full address string for geocoding (Nominatim works well with addr + zip)
    # Use list comprehension to avoid pandas apply() type-inference issues
    df["FULL_ADDRESS"] = [_build_full_address(row) for _, row in df.iterrows()]

    return df.reset_index(drop=True)


def _build_full_address(row: pd.Series) -> str:
    """Assemble a geocoding-friendly address string."""
    parts = []
    if row["SHIP_ADDR1"].strip():
        parts.append(row["SHIP_ADDR1"].strip())
    if row["SHIP_CITY"].strip():
        parts.append(row["SHIP_CITY"].strip())
    if row["SHIP_STATE"].strip():
        parts.append(row["SHIP_STATE"].strip())
    if row["SHIP_ZIP"].strip():
        parts.append(row["SHIP_ZIP"].strip())
    return ", ".join(parts)


def _format_zip(val) -> str:
    """Convert NUMERIC zip field to a 5-digit string. Handles 90265 and 902650000."""
    if val is None or val == 0 or val == "":
        return ""
    try:
        s = str(int(val))
        # 9-digit form: 902650000 → "90265"  |  5-digit form: 90265 → "90265"
        return s[:5] if len(s) >= 5 else s.zfill(5)
    except (ValueError, TypeError):
        return str(val)[:5]


def _parse_city_state(text: str) -> tuple[str, str]:
    """
    Extract city and 2-letter US state from a combined field.
    Examples: "MALIBU CA" → ("MALIBU", "CA")
              "LOS ANGELES CA" → ("LOS ANGELES", "CA")
              "MALIBU" → ("MALIBU", "")
    """
    text = text.strip()
    if not text:
        return "", ""
    parts = text.split()
    # If last token is exactly 2 alpha chars it's likely a state code
    if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
        return " ".join(parts[:-1]), parts[-1].upper()
    return text, ""


def _backfill_from_custmast(df: pd.DataFrame, conn: pyodbc.Connection) -> pd.DataFrame:
    """
    Where the ship-to address is blank on the order, fall back to the customer's
    billing address from CUSTMAST.  Non-fatal if CUSTMAST address columns differ.
    """
    needs_fill = df["SHIP_ADDR1"].str.strip().eq("")
    if not needs_fill.any():
        return df

    custs = df.loc[needs_fill, "CUST_CODE"].unique().tolist()
    if not custs:
        return df

    escaped = ", ".join(f"'{c}'" for c in custs)
    # Try common CUSTMAST address column names; ignore if they don't exist
    for addr1_col, city_col, state_col, zip_col in [
        ("CMAD1", "CMCITY", "CMST",   "CMZIP"),
        ("CMAD1", "CMSTA3", "CMST",   "CMZIP"),
        ("CMSTA1","CMSTA3", "",       "CMZIP"),
    ]:
        sql = f"""
            SELECT TRIM(C.CMCUST)      AS CUST_CODE,
                   TRIM(C.{addr1_col}) AS CM_ADDR1,
                   TRIM(C.{city_col})  AS CM_CITY,
                   {'TRIM(C.' + state_col + ')' if state_col else "''"} AS CM_STATE,
                   C.{zip_col}         AS CM_ZIP_RAW
            FROM GSFL2K.CUSTMAST C
            WHERE TRIM(C.CMCUST) IN ({escaped})
        """
        try:
            cust_df = pd.read_sql(sql, conn)
            cust_df["CM_ZIP"] = cust_df["CM_ZIP_RAW"].apply(_format_zip)
            cust_df = cust_df.set_index("CUST_CODE")
            break
        except Exception:
            cust_df = None
            continue

    if cust_df is None:
        return df

    for idx, row in df[needs_fill].iterrows():
        cust = row["CUST_CODE"]
        if cust in cust_df.index:
            cdata = cust_df.loc[cust]
            # .loc returns a DataFrame when there are duplicate index entries; take first row
            if isinstance(cdata, pd.DataFrame):
                cdata = cdata.iloc[0]
            df.at[idx, "SHIP_ADDR1"] = str(cdata.get("CM_ADDR1", "") or "")
            df.at[idx, "SHIP_CITY"]  = str(cdata.get("CM_CITY",  "") or "")
            df.at[idx, "SHIP_STATE"] = str(cdata.get("CM_STATE", "") or "")
            df.at[idx, "SHIP_ZIP"]   = str(cdata.get("CM_ZIP",   "") or "")
    return df


# ---------------------------------------------------------------------------
# Order notes query
# ---------------------------------------------------------------------------

# Column name candidates for the notes order-number field
_NOTE_ORD_CANDIDATES = [
    "OTORD#",   # GSFL2K.OOTEXT (confirmed — open order text)
    "OOORD#",   # alternative OO order# field name
    "STORD#",   # GSFL2K.SHTEXT (shipped order text — fallback)
    "SOCO", "SONO", "SFORD#", "SFCO", "SNORD#",
    "TXORD#", "NTORD#", "ORDNO", "SHORD#",
]

# Column name candidates for the PRIMARY text content (first/only text field)
_NOTE_TEXT1_CANDIDATES = [
    "OTCMT1",  # GSFL2K.OOTEXT primary comment field (confirmed)
    "STCMT1",  # GSFL2K.SHTEXT primary comment field (confirmed)
    "STTXT1", "STTXT", "STTEXT", "STTXTL", "STTXTLIN", "ST1",
    "SOTEXT", "SFTXT1", "SNTXT", "TXTLIN", "NOTELIN", "SOTXT",
    "TEXT",   "TXTEXT", "NTTXT", "CMT",    "COMMENT", "TXTLINE",
]

# Column name candidates for the SECONDARY text field (continuation line)
_NOTE_TEXT2_CANDIDATES = [
    "OTCMT2",  # GSFL2K.OOTEXT secondary comment field (confirmed)
    "STCMT2",  # GSFL2K.SHTEXT secondary comment field (confirmed)
    "STTXT2", "STTEXT2", "CMT2", "COMMENT2",
]

# Keep the old name as an alias for the primary field (used in _try_notes_table)
_NOTE_TEXT_CANDIDATES = _NOTE_TEXT1_CANDIDATES

# Column name candidates for the notes sequence/line number
_NOTE_SEQ_CANDIDATES = [
    "OTSEQ#",  # GSFL2K.OOTEXT (confirmed — note the # suffix)
    "STSEQ#",  # GSFL2K.SHTEXT (confirmed — note the # suffix)
    "OTSEQ", "STSEQ",
    "SOSEQ", "SFSEQ", "SNSEQ", "TXTSEQ", "SEQ", "LINENO", "NTSEQ", "TXSEQ",
]


def fetch_order_notes(
    conn: pyodbc.Connection,
    order_nums: list[str],
) -> dict[str, str]:
    """
    Return {order_num: concatenated_notes_text} for the given order numbers.
    Returns empty dict if no notes table is found (non-fatal).
    """
    if not order_nums:
        return {}

    escaped = ", ".join(f"'{n}'" for n in order_nums)

    for tbl in _NOTE_TABLE_CANDIDATES:
        result = _try_notes_table(conn, tbl, escaped)
        if result is not None:
            return result

    print("  WARNING: No order notes table found — stop durations will use defaults.")
    return {}


def _try_notes_table(
    conn: pyodbc.Connection,
    table: str,
    escaped_order_list: str,
) -> dict[str, str] | None:
    """
    Try to read notes from a candidate table. Returns None if table doesn't exist
    or if required columns (order#, text) cannot be identified.

    Handles two-field rows (e.g. SHTEXT's STCMT1 + STCMT2) by concatenating them.
    """
    schema, tbl_name = table.split(".")
    schema_sql = f"""
        SELECT COLUMN_NAME FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{tbl_name}'
    """
    try:
        cols_df = pd.read_sql(schema_sql, conn)
    except Exception:
        return None

    if cols_df.empty:
        return None

    cols_upper = [c.upper() for c in cols_df["COLUMN_NAME"].tolist()]

    ord_col  = next((c for c in _NOTE_ORD_CANDIDATES   if c in cols_upper), None)
    txt_col1 = next((c for c in _NOTE_TEXT1_CANDIDATES if c in cols_upper), None)
    txt_col2 = next((c for c in _NOTE_TEXT2_CANDIDATES if c in cols_upper), None)
    seq_col  = next((c for c in _NOTE_SEQ_CANDIDATES   if c in cols_upper), None)

    if not ord_col or not txt_col1:
        print(
            f"  Notes table {table}: found cols {cols_upper[:12]}...\n"
            f"    ord_col={ord_col!r}  txt_col1={txt_col1!r}  (add to candidates if wrong)"
        )
        return None

    # Build SELECT with optional second text field
    txt2_select = f", TRIM(T.{txt_col2}) AS NOTE_LINE2" if txt_col2 else ""
    order_by    = f"TRIM(T.{ord_col}), T.{seq_col}" if seq_col else f"TRIM(T.{ord_col})"

    sql = f"""
        SELECT TRIM(T.{ord_col}) AS ORDER_NUM,
               TRIM(T.{txt_col1}) AS NOTE_LINE1
               {txt2_select}
        FROM {table} T
        WHERE TRIM(T.{ord_col}) IN ({escaped_order_list})
        ORDER BY {order_by}
    """
    try:
        df = pd.read_sql(sql, conn)
    except Exception as e:
        print(f"  Notes query failed on {table}: {e}")
        return None

    notes: dict[str, str] = {}
    for _, row in df.iterrows():
        key   = str(row["ORDER_NUM"]).strip()
        line1 = str(row["NOTE_LINE1"]).strip()
        line2 = str(row.get("NOTE_LINE2", "")).strip() if txt_col2 else ""
        # Combine both comment fields; deduplicate whitespace
        combined = (line1 + " " + line2).strip()
        if key and combined:
            notes[key] = notes.get(key, "") + " " + combined

    print(f"  Notes loaded from {table} ({len(notes)} orders with text)")
    return notes


# ---------------------------------------------------------------------------
# Order quantities query (SF volume for service-time scaling)
# ---------------------------------------------------------------------------

# (table, order# col, qty col, uom col)
# OODETL uses OD prefix; OOLINE uses OL prefix — try both.
_OODETL_CANDIDATES = [
    ("GSFL2K.OODETL", "ODORD#", "ODQTY", "ODUOM"),
    ("GSFL2K.OOLINE",  "OLORD#", "OLQTY", "OLUOM"),
    ("GSFL2K.OODETL",  "ODORD",  "ODQTY", "ODUOM"),
    ("GSFL2K.OOLINE",  "OLORD",  "OLQTY", "OLUOM"),
]


def fetch_order_quantities(
    conn: pyodbc.Connection,
    order_nums: list[str],
) -> dict[str, int]:
    """
    Return {order_num: total_sf} summing order-line quantities where UOM='SF'.
    Non-fatal — returns empty dict if the detail table cannot be found or queried.
    The caller uses quantity_sf=0 (30-min base, unscaled modifiers) as fallback.
    """
    if not order_nums:
        return {}

    escaped = ", ".join(f"'{n}'" for n in order_nums)

    for table, ord_col, qty_col, uom_col in _OODETL_CANDIDATES:
        sql = f"""
            SELECT TRIM(CHAR(L.{ord_col})) AS ORDER_NUM,
                   SUM(L.{qty_col})        AS TOTAL_SF
            FROM {table} L
            WHERE TRIM(CHAR(L.{ord_col})) IN ({escaped})
              AND UPPER(TRIM(L.{uom_col})) = 'SF'
            GROUP BY L.{ord_col}
        """
        try:
            df = pd.read_sql(sql, conn)
            result: dict[str, int] = {}
            for _, row in df.iterrows():
                key = str(row["ORDER_NUM"]).strip()
                result[key] = int(row["TOTAL_SF"] or 0)
            print(f"  Order SF quantities loaded from {table} ({len(result)} order(s) with SF lines)")
            return result
        except Exception:
            continue

    print("  WARNING: Could not load order SF quantities — service times will use 30-min base (1-unit default).")
    return {}


# ---------------------------------------------------------------------------
# Transfer orders query
# ---------------------------------------------------------------------------

# The known TRANSFER## customer codes (2-digit suffix = destination branch code)
TRANSFER_CUSTOMERS = [
    "TRANSFER01", "TRANSFER03", "TRANSFER04",
    "TRANSFER06", "TRANSFER08", "TRANSFER09", "TRANSFER51",
]

# Candidate column names for the pick-ticket printer / output queue field in OOHEAD.
# Used to determine the SOURCE branch for each transfer.
# PICKTICK3 is the Robertson print queue — this value lives in one of these columns.
# Update PRINTER_SOURCE_MAP in dispatch_routes.py if the printer names differ.
_PRINTER_COL_CANDIDATES = [
    "OHPICK",   # most common AS/400 pick-ticket printer field
    "OHPICKQ",  # pick queue variant
    "OHPRTQ",   # print queue
    "OHPRNT",   # printer name
    "OHPRT",    # short printer field
    "OHPRNTR",  # printer (longer)
    "OHPOUT",   # output queue
    "OHPRTOUT", # print output queue
    "OHPRTD",   # print destination
    "OHPLT",    # print location
    "OHPRTNAM", # printer name (long)
    "OHPRNTQ",  # alternate print queue spelling
    "OHPRTLOC", # print location
]

# Fallback: branch/location columns that may store the source branch code numerically.
# Used when no printer column is found — maps branch number (e.g. 3) to loc code "03".
_BRANCH_COL_CANDIDATES = [
    "OHBRNCH", "OHBRN", "OHBR", "OHLOC", "OHLOCC", "OHLOCB",
    "OHWHS", "OHWHSE", "OHSTLOC", "OHSRCLOC", "OHSRC",
]


def fetch_transfer_orders(
    conn: pyodbc.Connection,
    ship_date: str,
) -> list[dict]:
    """
    Return all branch-to-branch transfer orders scheduled for ship_date.

    Each dict has:
      order_num : str   — order number
      dest_code : str   — 2-digit destination code from TRANSFER## (e.g. "06")
      printer   : str   — pick-ticket printer name, or "" if column not found

    The printer name is mapped to a source branch in dispatch_routes.PRINTER_SOURCE_MAP.
    """
    if not TRANSFER_CUSTOMERS:
        return []

    escaped = ", ".join(f"'{c}'" for c in TRANSFER_CUSTOMERS)

    # Discover the printer column once; fall back to branch/location column if not found
    printer_col = _discover_printer_col(conn)
    if printer_col:
        printer_select = f"TRIM(H.{printer_col})"
        printer_label  = printer_col
    else:
        branch_col = _discover_branch_col(conn)
        printer_col    = branch_col  # may still be None
        printer_select = f"TRIM(CHAR(H.{branch_col}))" if branch_col else "''"
        printer_label  = f"{branch_col} (branch fallback)" if branch_col else "(not found — source will default to Van Nuys)"
    print(f"  Printer/source column: {printer_label}")

    sql = f"""
        SELECT
            TRIM(CHAR(H.OHORD#)) AS ORDER_NUM,
            TRIM(H.OHCUST)       AS CUST_CODE,
            {printer_select}     AS PRINTER
        FROM GSFL2K.OOHEAD H
        WHERE H.OHSDAT = DATE('{ship_date}')
          AND H.OHCUST IN ({escaped})
        ORDER BY H.OHORD#
    """
    try:
        df = pd.read_sql(sql, conn)
    except Exception as e:
        print(f"  WARNING: Could not fetch transfer orders: {e}")
        return []

    result: list[dict] = []
    for _, row in df.iterrows():
        cust = str(row["CUST_CODE"]).strip()
        # dest_code = last 2 chars of TRANSFER## (e.g. "06" from "TRANSFER06")
        dest_code = cust[-2:] if len(cust) >= 8 else ""
        result.append({
            "order_num": str(row["ORDER_NUM"]).strip(),
            "dest_code": dest_code,
            "printer":   str(row["PRINTER"]).strip(),
        })

    print(f"  Found {len(result)} transfer order(s) for {ship_date}")
    return result


def _discover_printer_col(conn: pyodbc.Connection) -> str | None:
    """Probe OOHEAD for the first matching printer/output-queue column name."""
    sql = """
        SELECT COLUMN_NAME FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA = 'GSFL2K' AND TABLE_NAME = 'OOHEAD'
    """
    try:
        df = pd.read_sql(sql, conn)
        cols_upper = [c.upper() for c in df["COLUMN_NAME"].tolist()]
        for candidate in _PRINTER_COL_CANDIDATES:
            if candidate in cols_upper:
                return candidate
    except Exception:
        pass
    return None


def _discover_branch_col(conn: pyodbc.Connection) -> str | None:
    """
    Fallback: probe OOHEAD for a branch/location column that stores the source branch
    number (e.g. 3 for Robertson). Used when no printer column is found.
    """
    sql = """
        SELECT COLUMN_NAME FROM QSYS2.SYSCOLUMNS
        WHERE TABLE_SCHEMA = 'GSFL2K' AND TABLE_NAME = 'OOHEAD'
    """
    try:
        df = pd.read_sql(sql, conn)
        cols_upper = [c.upper() for c in df["COLUMN_NAME"].tolist()]
        for candidate in _BRANCH_COL_CANDIDATES:
            if candidate in cols_upper:
                return candidate
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Error hint helper
# ---------------------------------------------------------------------------

def _column_error_hint(exc: Exception, col_config: ColumnConfig) -> None:
    msg = str(exc).upper()
    guessed = [col_config.date_col, col_config.via_col, col_config.addr1, col_config.city, col_config.zip_col]
    if any(col.upper().replace("#", "") in msg for col in guessed if col):
        print(
            "\nHINT: Column not found in OOHEAD. Run:\n"
            "    python dispatch_routes.py --discover-schema\n"
            "to see actual column names, then update DEFAULT_COL_CONFIG in dispatch/db.py\n"
        )
