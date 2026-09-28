"""
generate_rep_account_lists.py
Pulls customer data from Gartman and writes one Excel file per sales rep.
Sales reps with multiple rep numbers are consolidated under a canonical name.
"""

from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyodbc

# ---------------------------------------------------------------------------
# Sales rep number → canonical rep name
# Numbers with no canonical rep (house accounts, special) are grouped under
# their own label; set to None to skip them entirely.
# ---------------------------------------------------------------------------
REP_MAP: dict[str, str | None] = {
    "20":  "Andy Aguilar",
    "14":  "Andy Aguilar",
    "15":  "Andy Aguilar",
    "13":  "Brad Ornellas",
    "105": "Brad Ornellas",
    "106": "Chris Phillips",
    "601": "Dave Courtney",
    "2":   "Dave Courtney",
    "36":  "Dave Courtney",
    "38":  "Dave Courtney",
    "44":  "Dave Courtney",
    "46":  "Dave Courtney",
    "47":  "Dave Courtney",
    "19":  "Jim McConnell",
    "11":  "Jim McConnell",
    "10":  "Jose Zaldivar",
    "12":  "Jose Zaldivar",
    "555": None,   # Nathalie Dina — excluded
    "18":  "Warren Carmichael",
    "33":  "Warren Carmichael",
    # House / special accounts — set to None to exclude from output files
    "997": None,   # Bad Debt / Collection
    "888": None,   # House Account
    "818": None,   # Revival - House Account
    "880": None,   # VIP House Acct
}

DSN_NAME = "Gartman"

# ---------------------------------------------------------------------------
# Customer class number → display name
# ---------------------------------------------------------------------------
CUSTOMER_CLASS_MAP: dict[str, str] = {
    "0":   "UNASSIGNED",
    "10":  "SAMPLES ONLY",
    "100": "C-15 LIC.",
    "200": "RETAILERS",
    "300": "INSTALLERS",
    "400": "GENERAL CONTRACTORS",
    "410": "CARPENTERS",
    "500": "DESIGNERS",
    "600": "DISTRIBUTORS",
    "700": "CASH ACCOUNTS",
    "800": "COMMERCIAL ACCOUNTS",
    "900": "ETZ & STEEL",
}

OUTPUT_DIR = Path(__file__).parent / "rep_account_lists"

# ---------------------------------------------------------------------------
# Credentials (mirrors flooringwebapp.py pattern)
# ---------------------------------------------------------------------------

def _load_credentials() -> tuple[str, str]:
    secrets_path = Path(__file__).resolve().parent / "OMP_secrets.py"
    if secrets_path.exists():
        ns: dict = {}
        with open(secrets_path, "r", encoding="utf-8") as f:
            exec(f.read(), ns, ns)
        uid = str(ns.get("GARTMAN_UID", "")).strip()
        pwd = str(ns.get("GARTMAN_PWD", "")).strip()
        if uid and pwd and uid != pwd:
            return uid, pwd
    uid = os.getenv("GARTMAN_UID", "").strip()
    pwd = os.getenv("GARTMAN_PWD", "").strip()
    if uid and pwd:
        return uid, pwd
    raise RuntimeError("Missing Gartman credentials (OMP_secrets.py or env vars)")


def _connect() -> pyodbc.Connection:
    uid, pwd = _load_credentials()
    conn_str = f"DSN={DSN_NAME};UID={uid};PWD={pwd};"
    print("Connecting to Gartman…")
    conn = pyodbc.connect(conn_str, autocommit=True, timeout=60)
    print("Connected.")
    return conn


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

QUERY = """
WITH EMAILS AS (
    SELECT
        X.CCNXACCT                AS ACCT,
        MIN(C.CNCE_MAIL)          AS EMAIL
    FROM GSFL2K.CONTXREF X
    JOIN GSFL2K.CONTCONT C
      ON C.CNCCONTID = X.CCNXCONTID
    GROUP BY X.CCNXACCT
)
SELECT
    TRIM(C.CMCUST)              AS ACCOUNT_NUMBER,
    TRIM(C.CMNAME)              AS ACCOUNT_NAME,
    TRIM(C.CMADR1)              AS ADDRESS,
    TRIM(C.CMADR2)              AS CITY,
    TRIM(C.CMADR3)              AS STATE,
    TRIM(C.CMZIP)               AS ZIP_CODE,
    TRIM(C.CMATTN)              AS CONTACT,
    TRIM(C.CMPHON)              AS PHONE_NUMBER,
    TRIM(COALESCE(E.EMAIL, '')) AS EMAIL,
    C.CMSDAT                    AS SETUP_DATE,
    TRIM(C.CMCLAS)              AS CUSTOMER_CLASS,
    TRIM(C.CMSLMN)              AS SALES_REP_NUMBER
FROM GSFL2K.CUSTMAST C
LEFT JOIN EMAILS E
  ON E.ACCT = C.CMCUST
WHERE C.CMSLMN IS NOT NULL
  AND TRIM(C.CMSLMN) <> ''
ORDER BY TRIM(C.CMNAME)
"""

# Friendly column headers that match the screenshot
RENAME_COLS = {
    "ACCOUNT_NUMBER":  "Account Number",
    "ACCOUNT_NAME":    "Account Name",
    "ADDRESS":         "Address",
    "CITY":            "Address 2",
    "STATE":           "State",
    "ZIP_CODE":        "Zip Code",
    "CONTACT":         "Contact",
    "PHONE_NUMBER":    "Phone Number",
    "EMAIL":           "Email",
    "SETUP_DATE":      "Setup Date",
    "CUSTOMER_CLASS":  "Customer Class",
}


# ---------------------------------------------------------------------------
# Excel formatting helpers
# ---------------------------------------------------------------------------

def _write_rep_excel(df: pd.DataFrame, rep_name: str, output_path: Path) -> None:
    """Write a single rep's customer list to a formatted Excel file."""
    display_cols = [c for c in RENAME_COLS.values() if c in df.columns]
    out_df = df[display_cols].copy()

    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        out_df.to_excel(writer, index=False, sheet_name="Accounts")
        ws = writer.sheets["Accounts"]

        # Auto-fit column widths
        for col_cells in ws.columns:
            max_len = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in col_cells
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 3, 50)

        # Auto-filter on all columns
        ws.auto_filter.ref = ws.dimensions

        # Freeze header row
        ws.freeze_panes = "A2"

        # Bold header row
        from openpyxl.styles import Font, PatternFill, Alignment
        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_font = Font(bold=True, color="FFFFFF")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        # Format Setup Date column as date
        date_col_idx = None
        for i, col in enumerate(display_cols, start=1):
            if col == "Setup Date":
                date_col_idx = i
                break
        if date_col_idx:
            col_letter = ws.cell(row=1, column=date_col_idx).column_letter
            for row in ws.iter_rows(min_row=2, min_col=date_col_idx, max_col=date_col_idx):
                for cell in row:
                    if cell.value:
                        cell.number_format = "MM/DD/YYYY"

    print(f"  Wrote {len(out_df):>4} accounts → {output_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = _connect()
    print("Pulling customer data…")
    df = pd.read_sql(QUERY, conn)
    conn.close()
    print(f"Fetched {len(df)} customer rows.")

    # Normalize rep number to strip whitespace
    df["SALES_REP_NUMBER"] = df["SALES_REP_NUMBER"].astype(str).str.strip()

    # Map rep number → canonical rep name
    df["CANONICAL_REP"] = df["SALES_REP_NUMBER"].map(REP_MAP)

    # Drop accounts whose rep maps to None (house/special accounts)
    df = df[df["CANONICAL_REP"].notna()].copy()

    print(f"{len(df)} accounts after excluding house/special accounts.")

    # Replace customer class numbers with display names
    df["CUSTOMER_CLASS"] = (
        df["CUSTOMER_CLASS"].astype(str).str.strip()
        .map(CUSTOMER_CLASS_MAP)
        .fillna(df["CUSTOMER_CLASS"])
    )

    # Rename display columns
    df.rename(columns=RENAME_COLS, inplace=True)

    # Create output directory
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Write one file per canonical rep
    reps = sorted(df["CANONICAL_REP"].unique())
    print(f"\nWriting {len(reps)} rep files to: {OUTPUT_DIR}\n")

    for rep in reps:
        rep_df = df[df["CANONICAL_REP"] == rep].copy()
        rep_df = rep_df.sort_values("Account Name")

        # Safe filename
        safe_name = rep.replace(" ", "_").replace("/", "-")
        filename = f"{safe_name}_{timestamp}.xlsx"
        output_path = OUTPUT_DIR / filename

        _write_rep_excel(rep_df, rep, output_path)

    print(f"\nDone. Files saved to: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
