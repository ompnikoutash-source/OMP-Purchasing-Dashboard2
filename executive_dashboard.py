"""
Executive Dashboard

Streamlit webapp for upper-management analysis across Gartman data. The first
implemented workspace is Product Analysis: live product data, spreadsheet-style
filtering, and configurable graphing.
"""

from __future__ import annotations

import contextlib
import html
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

from core.db_connection import connect, sql_escape


APP_TITLE = "Executive Dashboard"
DB_CACHE_TTL_SECONDS = 300
INVENTORY_LOCS = (1, 3, 4, 5, 6, 8, 9, 51)
BREAKOUT_NONE = "No breakout"
LEGACY_BREAKOUT_NONE = "(none)"
ITEM_DESCRIPTION_COLUMN = "Item / Description"
NO_MATCH_FILTER_VALUE = "__EXEC_DASHBOARD_NO_MATCH__"
BLANK_FILTER_LABEL = "(Blank)"
REPO_ROOT = Path(__file__).resolve().parent
FINANCIAL_TRIAL_BALANCE_PATH = REPO_ROOT / "TrialBalance_12312025_v2.xlsx"
FINANCIAL_ACCOUNTING_PACKET_PATH = Path(r"H:\2025\MISC Reports\For Accounting")

# Floor & Decor Holdings, Inc. FY2025 10-K (fiscal year ended 12/25/2025), figures read directly from
# the filing's Consolidated Balance Sheets, Statements of Operations, and Statements of Stockholders'
# Equity (Item 8) and Note 1/Note 3 disaggregation. Floor & Decor is a large, healthy, ongoing public
# company -- a big-box hard-surface-flooring *retailer* selling directly to homeowners/Pros, roughly
# 100x OMP's revenue, versus OMP's wholesale distribution model. Every dollar of its balance sheet is
# either mapped to the comparable OMP line below or intentionally left unmapped (goodwill, intangibles,
# deferred taxes, asset retirement obligations -- items OMP's trial balance has no equivalent for).
FINANCIAL_BENCHMARK_LABEL = "Floor & Decor (FY25 10-K)"
FLOOR_DECOR_FY2025 = {
    "total_assets": 5_469_358_000.0,
    "cash": 249_296_000.0,
    "accounts_receivable": 94_068_000.0,
    "inventory": 1_133_083_000.0,
    "prepaid_other_current": 51_484_000.0,  # income taxes receivable + prepaid expenses/other current assets
    "total_current_assets": 1_527_931_000.0,
    "ppe_net": 1_856_127_000.0,  # "Fixed assets, net"
    "rou_asset": 1_617_772_000.0,  # "Right-of-use assets", reported as its own line, same as OMP
    "other_lt_assets": 43_754_000.0,  # "Other assets" only; excludes goodwill/intangibles/deferred tax assets
    "accounts_payable": 683_675_000.0,  # "Trade accounts payable"
    "payroll_tax_other_accrued": 298_740_000.0,  # "Accrued expenses and other current liabilities" (Note 3)
    "customer_deposits": 10_685_000.0,  # "Deferred revenue"
    "current_lease_liability": 155_661_000.0,
    "notes_loans_payable": 196_218_000.0,  # current + long-term term loan, net of issuance costs
    "long_term_lease_liability": 1_639_598_000.0,
    "total_liabilities": 3_060_522_000.0,
    "total_equity": 2_408_836_000.0,
    "common_stock_apic": 577_894_000.0,  # common stock ($108K) + additional paid-in capital
    "retained_earnings": 1_830_942_000.0,  # retained earnings + $22K accumulated other comprehensive income
    # No "treasury_stock" key: F&D paid no dividends and repurchased no shares in fiscal 2025 -- no
    # capital-return line exists to map to OMP's "Less shareholder draws", so that cell stays blank.
    "net_sales": 4_684_088_000.0,
    "cogs": 2_640_180_000.0,
    "gross_profit": 2_043_908_000.0,
    "advertising_promotion": 103_600_000.0,  # disclosed separately in Note 1
    "total_operating_expense": 1_773_838_000.0,  # "Selling, general and administrative expenses"
    "operating_income": 270_070_000.0,
    "interest_expense": 3_409_000.0,  # "Interest expense, net"
    "net_income": 208_647_000.0,
    # No "other_income_net" key: F&D's income statement has no separate other-income line to map to
    # OMP's "Other income, net" (interest income/FX/restocking/freight-out rolled together).
}


def _financial_benchmark_pct(key: str, basis_key: str) -> float | None:
    """Floor & Decor's own common-size % (its $ value / its own basis total) -- independent of OMP's numbers."""
    value = FLOOR_DECOR_FY2025.get(key)
    basis = FLOOR_DECOR_FY2025.get(basis_key)
    if value is None or not basis:
        return None
    return value / basis


def _loc_list_sql() -> str:
    return ", ".join(str(loc) for loc in INVENTORY_LOCS)


def _is_no_breakout(value: str) -> bool:
    return value in {BREAKOUT_NONE, LEGACY_BREAKOUT_NONE, "", None}


def _sanitize_months(value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = 24
    return max(1, min(parsed, 120))


def _date_sql_literal(value: date | datetime | str | None) -> str:
    if value is None or value == "":
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


def _active_item_clause(include_discontinued: bool) -> str:
    if include_discontinued:
        return "TRIM(IM.IMITEM) <> ''"
    return "TRIM(IM.IMITEM) <> '' AND COALESCE(TRIM(IM.IMDROP), '') = ''"


def _product_family_sql_expr(item_alias: str = "IM", family_alias: str = "FM") -> str:
    code_expr = f"TRIM({item_alias}.IMFMCD)"
    desc_expr = f"TRIM({family_alias}.FMDESC)"
    return (
        f"CASE "
        f"WHEN COALESCE({code_expr}, '') = '' THEN '' "
        f"WHEN COALESCE({desc_expr}, '') = '' THEN {code_expr} "
        f"ELSE {code_expr} || ' / ' || {desc_expr} "
        f"END"
    )


def _product_family_join_sql(item_alias: str = "IM", family_alias: str = "FM") -> str:
    return (
        f"LEFT JOIN GSFL2K.FAMILY {family_alias}\n"
        f"  ON TRIM({family_alias}.FMFMCD) = TRIM({item_alias}.IMFMCD)"
    )


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{sql_escape(value)}'" for value in values if str(value).strip())


def _blank_filter_selected(values: tuple[str, ...]) -> bool:
    return any(str(value).strip() == BLANK_FILTER_LABEL for value in values)


def _nonblank_filter_values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for value in values if str(value).strip() and str(value).strip() != BLANK_FILTER_LABEL)


def _sql_filter_clause(expression: str, values: tuple[str, ...]) -> str:
    values = tuple(str(value).strip() for value in values if str(value).strip())
    nonblank_values = _nonblank_filter_values(values)
    clauses = []
    if nonblank_values:
        clauses.append(f"{expression} IN ({_sql_list(nonblank_values)})")
    if _blank_filter_selected(values):
        clauses.append(f"COALESCE({expression}, '') = ''")
    return "(" + " OR ".join(clauses) + ")" if clauses else ""


def _extra_filter_clause(
    filters: dict[str, tuple[str, ...]] | None,
    *,
    item_expr: str = "TRIM(IM.IMITEM)",
    division_expr: str = "RTRIM(CHAR(IM.IMDIV))",
    product_family_expr: str | None = None,
    collection_expr: str = "TRIM(IX.IMCOLLECT)",
    branch_expr: str | None = None,
    vendor_expr: str = "TRIM(IM.IMVEND)",
) -> str:
    if not filters:
        return ""
    product_family_expr = product_family_expr or _product_family_sql_expr()

    clauses: list[str] = []
    if filters.get("item_numbers"):
        clauses.append(_sql_filter_clause(item_expr, filters["item_numbers"]))
    if filters.get("divisions"):
        clauses.append(_sql_filter_clause(division_expr, filters["divisions"]))
    if filters.get("product_families"):
        clauses.append(_sql_filter_clause(product_family_expr, filters["product_families"]))
    if filters.get("collections"):
        clauses.append(_sql_filter_clause(collection_expr, filters["collections"]))
    if branch_expr and filters.get("branch_locations"):
        clauses.append(_sql_filter_clause(branch_expr, filters["branch_locations"]))
    if filters.get("vendor_numbers"):
        clauses.append(_sql_filter_clause(vendor_expr, filters["vendor_numbers"]))

    clauses = [clause for clause in clauses if clause]
    if not clauses:
        return ""
    return "\n  AND " + "\n  AND ".join(clauses)


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if "Date" in col or col == "Month":
            df[col] = pd.to_datetime(df[col], errors="coerce")

    numeric_keywords = (
        "$",
        "Amount",
        "Available",
        "Average",
        "Backorders",
        "Cost",
        "Count",
        "Gross Margin",
        "Inventory Value",
        "Landed",
        "On Hand",
        "Orders",
        "Price",
        "Qty",
        "Receipt",
        "Sales",
        "Units",
    )
    protected_dimensions = {
        "Branch Location",
        "Collection",
        "Customer Name",
        "Customer Number",
        "Description",
        "Division",
        "Item Number",
        "Product Family",
        "Sales UOM",
        "Vendor Name",
        "Vendor Number",
    }
    for col in df.columns:
        if col in protected_dimensions:
            continue
        if "Date" in col or col == "Month":
            continue
        if any(keyword in col for keyword in numeric_keywords):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "Gross Margin %" in col:
            df[col] = pd.to_numeric(df[col], errors="coerce") * 100
    return df


def _read_gartman(sql: str) -> tuple[pd.DataFrame, datetime]:
    queried_at = datetime.now()
    with connect() as conn:
        df = pd.read_sql(sql, conn)
    return _clean_dataframe(df), queried_at


def _product_snapshot_sql(
    include_discontinued: bool,
    filters: dict[str, tuple[str, ...]] | None = None,
    start_date: date | datetime | str | None = None,
    end_date: date | datetime | str | None = None,
) -> str:
    locs = _loc_list_sql()
    start_literal = _date_sql_literal(start_date)
    end_literal = _date_sql_literal(end_date)
    active_clause = _active_item_clause(include_discontinued)
    filter_clause = _extra_filter_clause(filters)
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    if start_literal and end_literal:
        sales_date_clause = f"H.SHIDAT BETWEEN DATE('{start_literal}') AND DATE('{end_literal}')"
    else:
        sales_date_clause = "H.SHIDAT >= CURRENT_DATE - 365 DAYS"
    return f"""
WITH
OrderItemSales AS (
    SELECT
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        H.SHIDAT AS SHIDAT,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#))) AS INVKEY,
        SUM(COALESCE(L.SLENET, 0)) AS INV_ITEM_SALES,
        SUM(COALESCE(L.SLBLUO, 0)) AS INV_ITEM_UNITS
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
      ON L.SLCO   = H.SHCO
     AND L.SLLOC  = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    WHERE {sales_date_clause}
      AND TRIM(L.SLITEM) <> ''
    GROUP BY
        TRIM(L.SLITEM),
        H.SHIDAT,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))
),
SalesAgg AS (
    SELECT
        ITEM_NUMBER,
        SUM(INV_ITEM_SALES) AS SALES_SELECTED_RANGE,
        SUM(INV_ITEM_UNITS) AS UNITS_SELECTED_RANGE,
        COUNT(DISTINCT INVKEY) AS ORDERS_SELECTED_RANGE
    FROM OrderItemSales
    GROUP BY ITEM_NUMBER
),
InvEnd AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN IB.IBQOH
                ELSE IB.IBQOH * IM.IMFACT
            END
        ) AS CURRENT_ON_HAND,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0))
                ELSE (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT
            END
        ) AS CURRENT_AVAILABLE
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN ({locs})
    GROUP BY TRIM(IB.IBITEM)
),
Backorders AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN COALESCE(IB.IBQBO, 0)
                ELSE COALESCE(IB.IBQBO, 0) * IM.IMFACT
            END
        ) AS TOTAL_BACKORDER
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN ({locs})
    GROUP BY TRIM(IB.IBITEM)
),
QtyOnPO AS (
    SELECT
        TRIM(PL.PLITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN DECIMAL(PL.PLQORD - COALESCE(PL.PLQREC, 0), 18, 6)
                ELSE DECIMAL(PL.PLQORD - COALESCE(PL.PLQREC, 0), 18, 6) * IM.IMFACT
            END
        ) AS TOTAL_QTY_ON_PO
    FROM GSFL2K.POLINE PL
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = TRIM(PL.PLITEM)
    WHERE PL.PLCO = 1
      AND COALESCE(TRIM(PL.PLDELT), '') <> 'D'
      AND PL.PLQORD > COALESCE(PL.PLQREC, 0)
    GROUP BY TRIM(PL.PLITEM)
),
LatestLandedCost AS (
    SELECT
        TRIM(R.IRITEM) AS ITEM_NUMBER,
        R.IRDATE AS MOST_RECENT_RECEIVED_DATE,
        DECIMAL(R.IRCOST, 18, 5) AS MOST_RECENT_LANDED_COST
    FROM GSFL2K.ITEMRECH R
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRCOST > 0
      AND R.IRRECNBR > 0
      AND R.IRRECNBR = (
          SELECT MAX(R2.IRRECNBR)
          FROM GSFL2K.ITEMRECH R2
          WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
            AND R2.IRCO = 1
            AND TRIM(R2.IRSRC) = 'P'
            AND R2.IRQTY > 0
            AND R2.IRCOST > 0
            AND R2.IRRECNBR > 0
      )
),
VipProductPrice AS (
    SELECT
        PP.PPPRCD AS PRICE_CODE,
        MAX(PP.PPP1) AS VIP_PRICE
    FROM GSFL2K.PRODPCOL PP
    WHERE TRIM(PP.PPPMCD) = 'WOOD-A'
    GROUP BY PP.PPPRCD
),
VipItemPrice AS (
    SELECT
        TRIM(IP.IPITEM) AS ITEM_NUMBER,
        MAX(IP.IPP1) AS VIP_PRICE
    FROM GSFL2K.ITEMPRIC IP
    WHERE TRIM(IP.IPPMCD) = 'WOOD-A'
    GROUP BY TRIM(IP.IPITEM)
)
SELECT
    TRIM(IM.IMITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    {family_expr} AS "Product Family",
    TRIM(IM.IMVEND) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    IM.IMP1 AS "Preferred Price",
    COALESCE(VIP_ITEM.VIP_PRICE, VIP_PRODUCT.VIP_PRICE) AS "VIP Price",
    DECIMAL(COALESCE(IEN.CURRENT_ON_HAND, 0), 18, 2) AS "Current Inventory On Hand",
    DECIMAL(COALESCE(IEN.CURRENT_AVAILABLE, 0), 18, 2) AS "Current Inventory Available",
    DECIMAL(COALESCE(BO.TOTAL_BACKORDER, 0), 18, 2) AS "Backorders",
    DECIMAL(COALESCE(PO.TOTAL_QTY_ON_PO, 0), 18, 2) AS "Qty on PO",
    TRIM(IM.IMUM2) AS "Sales UOM",
    COALESCE(SA.SALES_SELECTED_RANGE, 0) AS "Sales $ - Selected Range",
    COALESCE(SA.UNITS_SELECTED_RANGE, 0) AS "Units Sold - Selected Range",
    COALESCE(SA.ORDERS_SELECTED_RANGE, 0) AS "Orders - Selected Range",
    DECIMAL(COALESCE(LC.MOST_RECENT_LANDED_COST, 0), 18, 5) AS "Landed Cost - Most Recent PO",
    DECIMAL(COALESCE(IM.IMACST, 0), 18, 5) AS "Landed Cost - Avg Inventory",
    CASE
        WHEN COALESCE(IM.IMP1, 0) = 0 OR LC.MOST_RECENT_LANDED_COST IS NULL THEN NULL
        ELSE DECIMAL((IM.IMP1 - LC.MOST_RECENT_LANDED_COST) / IM.IMP1, 18, 6)
    END AS "Gross Margin % - Most Recent PO",
    CASE
        WHEN COALESCE(IM.IMP1, 0) = 0 OR IM.IMACST IS NULL THEN NULL
        ELSE DECIMAL((IM.IMP1 - IM.IMACST) / IM.IMP1, 18, 6)
    END AS "Gross Margin % - Avg Inventory",
    LC.MOST_RECENT_RECEIVED_DATE AS "Last FOB Date"
FROM GSFL2K.ITEMMAST IM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
{family_join}
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
LEFT JOIN SalesAgg SA
  ON SA.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN InvEnd IEN
  ON IEN.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN Backorders BO
  ON BO.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN QtyOnPO PO
  ON PO.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN LatestLandedCost LC
  ON LC.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN VipProductPrice VIP_PRODUCT
  ON VIP_PRODUCT.PRICE_CODE = IM.IMPRCD
LEFT JOIN VipItemPrice VIP_ITEM
  ON VIP_ITEM.ITEM_NUMBER = TRIM(IM.IMITEM)
WHERE {active_clause}{filter_clause}
ORDER BY COALESCE(SA.SALES_SELECTED_RANGE, 0) DESC, TRIM(IM.IMITEM)
"""


def _sales_over_time_sql(
    include_discontinued: bool,
    months: int,
    filters: dict[str, tuple[str, ...]] | None = None,
    start_date: date | datetime | str | None = None,
    end_date: date | datetime | str | None = None,
    separate_by_branch: bool = False,
) -> str:
    months = _sanitize_months(months)
    start_literal = _date_sql_literal(start_date)
    end_literal = _date_sql_literal(end_date)
    active_clause = _active_item_clause(include_discontinued)
    filter_clause = _extra_filter_clause(
        filters,
        branch_expr="RTRIM(CHAR(H.SHLOC))",
    )
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    if start_literal and end_literal:
        date_clause = f"H.SHIDAT BETWEEN DATE('{start_literal}') AND DATE('{end_literal}')"
    else:
        date_clause = f"H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -{months})"
    branch_select = '    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",\n' if separate_by_branch else ""
    branch_group = "    RTRIM(CHAR(H.SHLOC)),\n" if separate_by_branch else ""
    return f"""
SELECT
    H.SHIDAT - (DAY(H.SHIDAT) - 1) DAYS AS "Month",
    TRIM(L.SLITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    {family_expr} AS "Product Family",
{branch_select}    TRIM(IM.IMVEND) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    SUM(COALESCE(L.SLENET, 0)) AS "Sales Amount",
    SUM(COALESCE(L.SLBLUO, 0)) AS "Units Sold",
    COUNT(DISTINCT (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))) AS "Orders Count"
FROM GSFL2K.SHHEAD H
JOIN GSFL2K.SHLINE L
  ON L.SLCO   = H.SHCO
 AND L.SLLOC  = H.SHLOC
 AND L.SLINV# = H.SHINV#
 AND L.SLORD# = H.SHORD#
JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = L.SLITEM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
{family_join}
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
WHERE {date_clause}
  AND {active_clause}{filter_clause}
GROUP BY
    H.SHIDAT - (DAY(H.SHIDAT) - 1) DAYS,
    TRIM(L.SLITEM),
    TRIM(IM.IMDESC),
    TRIM(IX.IMCOLLECT),
    RTRIM(CHAR(IM.IMDIV)),
    {family_expr},
{branch_group}    TRIM(IM.IMVEND),
    TRIM(VM.VMNAME)
ORDER BY "Month" DESC, "Sales Amount" DESC
"""


def _sales_summary_sql(
    include_discontinued: bool,
    months: int,
    filters: dict[str, tuple[str, ...]] | None = None,
    start_date: date | datetime | str | None = None,
    end_date: date | datetime | str | None = None,
    separate_by_branch: bool = False,
) -> str:
    months = _sanitize_months(months)
    start_literal = _date_sql_literal(start_date)
    end_literal = _date_sql_literal(end_date)
    active_clause = _active_item_clause(include_discontinued)
    filter_clause = _extra_filter_clause(
        filters,
        branch_expr="RTRIM(CHAR(H.SHLOC))",
    )
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    if start_literal and end_literal:
        date_clause = f"H.SHIDAT BETWEEN DATE('{start_literal}') AND DATE('{end_literal}')"
    else:
        date_clause = f"H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -{months})"
    branch_select = '    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",\n' if separate_by_branch else ""
    branch_group = "    RTRIM(CHAR(H.SHLOC)),\n" if separate_by_branch else ""
    return f"""
SELECT
    TRIM(L.SLITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    {family_expr} AS "Product Family",
{branch_select}    TRIM(IM.IMVEND) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    IM.IMP1 AS "Preferred Price",
    COALESCE(VIP_ITEM.VIP_PRICE, VIP_PRODUCT.VIP_PRICE) AS "VIP Price",
    TRIM(IM.IMUM2) AS "Sales UOM",
    SUM(COALESCE(L.SLENET, 0)) AS "Sales Amount",
    SUM(COALESCE(L.SLBLUO, 0)) AS "Units Sold",
    COUNT(DISTINCT (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))) AS "Orders Count"
FROM GSFL2K.SHHEAD H
JOIN GSFL2K.SHLINE L
  ON L.SLCO   = H.SHCO
 AND L.SLLOC  = H.SHLOC
 AND L.SLINV# = H.SHINV#
 AND L.SLORD# = H.SHORD#
JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = L.SLITEM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
{family_join}
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
LEFT JOIN (
    SELECT
        PPPRCD AS PRICE_CODE,
        MAX(PPP1) AS VIP_PRICE
    FROM GSFL2K.PRODPCOL
    WHERE TRIM(PPPMCD) = 'WOOD-A'
    GROUP BY PPPRCD
) VIP_PRODUCT
  ON VIP_PRODUCT.PRICE_CODE = IM.IMPRCD
LEFT JOIN (
    SELECT
        TRIM(IPITEM) AS ITEM_NUMBER,
        MAX(IPP1) AS VIP_PRICE
    FROM GSFL2K.ITEMPRIC
    WHERE TRIM(IPPMCD) = 'WOOD-A'
    GROUP BY TRIM(IPITEM)
) VIP_ITEM
  ON VIP_ITEM.ITEM_NUMBER = TRIM(IM.IMITEM)
WHERE {date_clause}
  AND {active_clause}{filter_clause}
GROUP BY
    TRIM(L.SLITEM),
    TRIM(IM.IMDESC),
    TRIM(IX.IMCOLLECT),
    RTRIM(CHAR(IM.IMDIV)),
    {family_expr},
{branch_group}    TRIM(IM.IMVEND),
    TRIM(VM.VMNAME),
    IM.IMP1,
    COALESCE(VIP_ITEM.VIP_PRICE, VIP_PRODUCT.VIP_PRICE),
    TRIM(IM.IMUM2)
ORDER BY "Sales Amount" DESC, "Item Number"
"""


TREND_INTERVALS: dict[str, int] = {
    "Last 30 vs Prior 30 Days": 30,
    "Last 90 vs Prior 90 Days": 90,
    "Last 180 vs Prior 180 Days": 180,
    "Last 365 vs Prior 365 Days": 365,
}

# Outside sales reps who carry multiple SHSLSM rep numbers (e.g. territory-specific splits).
# When "combine" is enabled, all numbers in a group are reported as one row.
REP_NUMBER_GROUPS: dict[str, list[int]] = {
    "DAVE COURTNEY COMBINED": [2, 47, 38, 46, 36, 44],
    "JOSE ZALDIVAR COMBINED": [10, 12],
    "WARREN CARMICHAEL COMBINED": [18, 33],
}


def _rep_group_case_sql(column_expr: str) -> str:
    """CASE expression mapping a SHSLSM-style column to its combined-rep label, else NULL."""
    branches = "\n        ".join(
        f"WHEN {column_expr} IN ({', '.join(str(n) for n in numbers)}) THEN '{label}'"
        for label, numbers in REP_NUMBER_GROUPS.items()
    )
    return f"CASE\n        {branches}\n        ELSE NULL\n      END"


def _customer_sales_detail_sql(months: int) -> str:
    """Customer x Branch x Class x Division grain. Powers every Customer-tab view."""
    months = _sanitize_months(months)
    return f"""
SELECT
    TRIM(H.SHCUST) AS "Customer Number",
    COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)') AS "Customer Name",
    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",
    RTRIM(CHAR(CM.CMCLAS)) AS "Customer Class",
    COALESCE(TRIM(CL.CLSDES), '(Unclassified)') AS "Customer Class Description",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    SUM(COALESCE(L.SLENET, 0)) AS "Sales Amount",
    SUM(COALESCE(L.SLBLUO, 0)) AS "Units Sold",
    COUNT(DISTINCT (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))) AS "Orders Count"
FROM GSFL2K.SHHEAD H
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC AND L.SLINV# = H.SHINV# AND L.SLORD# = H.SHORD#
JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = L.SLITEM
LEFT JOIN GSFL2K.CUSTMAST CM
  ON CM.CMCUST = H.SHCUST AND CM.CMCO = H.SHCO
LEFT JOIN GSFL2K.CUSTCLAS CL
  ON CL.CLSCO = CM.CMCO AND CL.CLSLOC = CM.CMLOC AND CL.CLSCOD = CM.CMCLAS
WHERE H.SHCO = 1
  AND H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -{months})
GROUP BY
    TRIM(H.SHCUST),
    COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)'),
    RTRIM(CHAR(H.SHLOC)),
    RTRIM(CHAR(CM.CMCLAS)),
    COALESCE(TRIM(CL.CLSDES), '(Unclassified)'),
    RTRIM(CHAR(IM.IMDIV))
ORDER BY "Sales Amount" DESC
"""


def _customer_item_sales_sql(
    months: int,
    include_discontinued: bool,
    filters: dict[str, tuple[str, ...]] | None = None,
    top_customers: int = 25,
    items_per_customer: int = 10,
    group_by: str = "Item",
) -> str:
    """Customer sales by item or collection, ranked inside the selected product filter."""
    months = _sanitize_months(months)
    active_clause = _active_item_clause(include_discontinued)
    filter_clause = _extra_filter_clause(filters)
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    top_customers = max(1, min(int(top_customers), 100))
    details_per_customer = max(1, min(int(items_per_customer), 50))
    group_by_collection = str(group_by).strip().lower().startswith("collection")
    detail_rank_alias = "Collection Rank" if group_by_collection else "Item Rank"
    if group_by_collection:
        detail_sales_cte = """
CustomerDetailSales AS (
    SELECT
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        CAST('' AS VARCHAR(30)) AS ITEM_NUMBER,
        CAST('' AS VARCHAR(80)) AS DESCRIPTION,
        COLLECTION,
        CASE WHEN COUNT(DISTINCT PRODUCT_FAMILY) = 1 THEN MIN(PRODUCT_FAMILY) ELSE '(Multiple)' END AS PRODUCT_FAMILY,
        CASE WHEN COUNT(DISTINCT DIVISION) = 1 THEN MIN(DIVISION) ELSE '(Multiple)' END AS DIVISION,
        CASE WHEN COUNT(DISTINCT VENDOR_NUMBER) = 1 THEN MIN(VENDOR_NUMBER) ELSE '(Multiple)' END AS VENDOR_NUMBER,
        CASE WHEN COUNT(DISTINCT VENDOR_NAME) = 1 THEN MIN(VENDOR_NAME) ELSE '(Multiple)' END AS VENDOR_NAME,
        SUM(SALES_AMOUNT) AS SALES_AMOUNT,
        SUM(UNITS_SOLD) AS UNITS_SOLD,
        COUNT(DISTINCT INVKEY) AS ORDERS_COUNT,
        COUNT(DISTINCT ITEM_NUMBER) AS DETAIL_ITEM_COUNT
    FROM FilteredSales
    GROUP BY
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        COLLECTION
),
"""
        detail_order_expr = "CI.SALES_AMOUNT DESC, CI.COLLECTION"
    else:
        detail_sales_cte = """
CustomerDetailSales AS (
    SELECT
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        ITEM_NUMBER,
        DESCRIPTION,
        COLLECTION,
        PRODUCT_FAMILY,
        DIVISION,
        VENDOR_NUMBER,
        VENDOR_NAME,
        SUM(SALES_AMOUNT) AS SALES_AMOUNT,
        SUM(UNITS_SOLD) AS UNITS_SOLD,
        COUNT(DISTINCT INVKEY) AS ORDERS_COUNT,
        COUNT(DISTINCT ITEM_NUMBER) AS DETAIL_ITEM_COUNT
    FROM FilteredSales
    GROUP BY
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        ITEM_NUMBER,
        DESCRIPTION,
        COLLECTION,
        PRODUCT_FAMILY,
        DIVISION,
        VENDOR_NUMBER,
        VENDOR_NAME
),
"""
        detail_order_expr = "CI.SALES_AMOUNT DESC, CI.ITEM_NUMBER"
    return f"""
WITH
FilteredSales AS (
    SELECT
        TRIM(H.SHCUST) AS CUSTOMER_NUMBER,
        COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)') AS CUSTOMER_NAME,
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        TRIM(IM.IMDESC) AS DESCRIPTION,
        COALESCE(NULLIF(TRIM(IX.IMCOLLECT), ''), '(Unclassified)') AS COLLECTION,
        RTRIM(CHAR(IM.IMDIV)) AS DIVISION,
        {family_expr} AS PRODUCT_FAMILY,
        TRIM(IM.IMVEND) AS VENDOR_NUMBER,
        TRIM(VM.VMNAME) AS VENDOR_NAME,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#))) AS INVKEY,
        COALESCE(L.SLENET, 0) AS SALES_AMOUNT,
        COALESCE(L.SLBLUO, 0) AS UNITS_SOLD
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
      ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC AND L.SLINV# = H.SHINV# AND L.SLORD# = H.SHORD#
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = L.SLITEM
    LEFT JOIN GSFL2K.ITEMXTRA IX
      ON IX.IMXITM = IM.IMITEM
    {family_join}
    LEFT JOIN GSFL2K.VENDMAST VM
      ON VM.VMVEND = IM.IMVEND
    LEFT JOIN GSFL2K.CUSTMAST CM
      ON CM.CMCUST = H.SHCUST AND CM.CMCO = H.SHCO
    WHERE H.SHCO = 1
      AND H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -{months})
      AND {active_clause}{filter_clause}
),
{detail_sales_cte}
CustomerTotals AS (
    SELECT
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        SUM(SALES_AMOUNT) AS CUSTOMER_FILTER_SALES_AMOUNT,
        SUM(UNITS_SOLD) AS CUSTOMER_FILTER_UNITS_SOLD,
        COUNT(DISTINCT INVKEY) AS CUSTOMER_FILTER_ORDERS_COUNT,
        COUNT(DISTINCT ITEM_NUMBER) AS CUSTOMER_FILTER_ITEM_COUNT
    FROM FilteredSales
    GROUP BY
        CUSTOMER_NUMBER,
        CUSTOMER_NAME
),
RankedCustomers AS (
    SELECT
        CT.*,
        ROW_NUMBER() OVER (
            ORDER BY CT.CUSTOMER_FILTER_SALES_AMOUNT DESC, CT.CUSTOMER_NUMBER
        ) AS CUSTOMER_RANK
    FROM CustomerTotals CT
),
RankedDetail AS (
    SELECT
        RC.CUSTOMER_RANK,
        CI.CUSTOMER_NUMBER,
        CI.CUSTOMER_NAME,
        CI.ITEM_NUMBER,
        CI.DESCRIPTION,
        CI.COLLECTION,
        CI.PRODUCT_FAMILY,
        CI.DIVISION,
        CI.VENDOR_NUMBER,
        CI.VENDOR_NAME,
        CI.SALES_AMOUNT,
        CI.UNITS_SOLD,
        CI.ORDERS_COUNT,
        CI.DETAIL_ITEM_COUNT,
        RC.CUSTOMER_FILTER_SALES_AMOUNT,
        RC.CUSTOMER_FILTER_UNITS_SOLD,
        RC.CUSTOMER_FILTER_ORDERS_COUNT,
        RC.CUSTOMER_FILTER_ITEM_COUNT,
        ROW_NUMBER() OVER (
            PARTITION BY CI.CUSTOMER_NUMBER
            ORDER BY {detail_order_expr}
        ) AS DETAIL_RANK
    FROM CustomerDetailSales CI
    JOIN RankedCustomers RC
      ON RC.CUSTOMER_NUMBER = CI.CUSTOMER_NUMBER
     AND RC.CUSTOMER_NAME = CI.CUSTOMER_NAME
    WHERE RC.CUSTOMER_RANK <= {top_customers}
)
SELECT
    CUSTOMER_RANK AS "Customer Rank",
    CUSTOMER_NUMBER AS "Customer Number",
    CUSTOMER_NAME AS "Customer Name",
    DETAIL_RANK AS "{detail_rank_alias}",
    ITEM_NUMBER AS "Item Number",
    DESCRIPTION AS "Description",
    COLLECTION AS "Collection",
    PRODUCT_FAMILY AS "Product Family",
    DIVISION AS "Division",
    VENDOR_NUMBER AS "Vendor Number",
    VENDOR_NAME AS "Vendor Name",
    DECIMAL(SALES_AMOUNT, 18, 2) AS "Sales Amount",
    DECIMAL(UNITS_SOLD, 18, 2) AS "Units Sold",
    ORDERS_COUNT AS "Orders Count",
    DETAIL_ITEM_COUNT AS "Item Count",
    DECIMAL(CUSTOMER_FILTER_SALES_AMOUNT, 18, 2) AS "Customer Filter Sales Amount",
    DECIMAL(CUSTOMER_FILTER_UNITS_SOLD, 18, 2) AS "Customer Filter Units Sold",
    CUSTOMER_FILTER_ORDERS_COUNT AS "Customer Filter Orders Count",
    CUSTOMER_FILTER_ITEM_COUNT AS "Customer Filter Item Count"
FROM RankedDetail
WHERE DETAIL_RANK <= {details_per_customer}
ORDER BY
    "Customer Rank",
    "{detail_rank_alias}"
"""


def _entity_trend_sql(days: int, grain: str) -> str:
    """Period-over-period sales comparison. grain='customer', 'item', or 'collection'."""
    days = max(1, min(int(days), 730))
    if grain == "item":
        id_select = 'TRIM(L.SLITEM) AS "Entity Number",\n    TRIM(IM.IMDESC) AS "Entity Name",'
        id_group = "TRIM(L.SLITEM),\n    TRIM(IM.IMDESC)"
        extra_join = "JOIN GSFL2K.ITEMMAST IM\n  ON IM.IMITEM = L.SLITEM\n"
    elif grain == "collection":
        collection_expr = "COALESCE(NULLIF(TRIM(IX.IMCOLLECT), ''), '(Unclassified)')"
        id_select = f'{collection_expr} AS "Entity Number",\n    {collection_expr} AS "Entity Name",'
        id_group = collection_expr
        extra_join = (
            "JOIN GSFL2K.ITEMMAST IM\n  ON IM.IMITEM = L.SLITEM\n"
            "LEFT JOIN GSFL2K.ITEMXTRA IX\n  ON IX.IMXITM = IM.IMITEM\n"
        )
    else:
        id_select = (
            'TRIM(H.SHCUST) AS "Entity Number",\n'
            "    COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)') AS \"Entity Name\",\n"
            '    RTRIM(CHAR(H.SHSLSM)) AS "Employee Number",\n'
            "    COALESCE(TRIM(SM.SMNAME), '(Unassigned)') AS \"Employee Name\","
        )
        id_group = (
            "TRIM(H.SHCUST),\n"
            "    COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)'),\n"
            "    RTRIM(CHAR(H.SHSLSM)),\n"
            "    COALESCE(TRIM(SM.SMNAME), '(Unassigned)')"
        )
        extra_join = (
            "LEFT JOIN GSFL2K.CUSTMAST CM\n  ON CM.CMCUST = H.SHCUST AND CM.CMCO = H.SHCO\n"
            "LEFT JOIN GSFL2K.SALESMAN SM\n  ON SM.SMNO = H.SHSLSM AND SM.SMCO = H.SHCO\n"
        )
    return f"""
SELECT
    {id_select}
    SUM(CASE WHEN H.SHIDAT >= CURRENT_DATE - {days} DAYS THEN COALESCE(L.SLENET, 0) ELSE 0 END) AS "Current Period Sales",
    SUM(CASE WHEN H.SHIDAT < CURRENT_DATE - {days} DAYS THEN COALESCE(L.SLENET, 0) ELSE 0 END) AS "Prior Period Sales"
FROM GSFL2K.SHHEAD H
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC AND L.SLINV# = H.SHINV# AND L.SLORD# = H.SHORD#
{extra_join}WHERE H.SHCO = 1
  AND H.SHIDAT >= CURRENT_DATE - {days * 2} DAYS
GROUP BY
    {id_group}
"""


def _branch_summary_sql(months: int) -> str:
    months = _sanitize_months(months)
    return f"""
SELECT
    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",
    SUM(COALESCE(L.SLENET, 0)) AS "Sales Amount",
    SUM(COALESCE(L.SLBLUO, 0)) AS "Units Sold",
    COUNT(DISTINCT (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))) AS "Orders Count",
    COUNT(DISTINCT TRIM(H.SHCUST)) AS "Customer Count"
FROM GSFL2K.SHHEAD H
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC AND L.SLINV# = H.SHINV# AND L.SLORD# = H.SHORD#
WHERE H.SHCO = 1
  AND H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -{months})
GROUP BY RTRIM(CHAR(H.SHLOC))
ORDER BY "Sales Amount" DESC
"""


def _employee_sales_sql(months: int, combine_reps: bool = False) -> str:
    months = _sanitize_months(months)
    if combine_reps:
        group_case = _rep_group_case_sql("H.SHSLSM")
        # Numbers combine into one label; everyone else keeps their own number/name.
        number_expr = f"COALESCE({group_case}, RTRIM(CHAR(H.SHSLSM)))"
        name_expr = f"COALESCE({group_case}, TRIM(SM.SMNAME), '(Unassigned)')"
    else:
        number_expr = "RTRIM(CHAR(H.SHSLSM))"
        name_expr = "COALESCE(TRIM(SM.SMNAME), '(Unassigned)')"
    return f"""
SELECT
    {number_expr} AS "Employee Number",
    {name_expr} AS "Employee Name",
    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",
    SUM(COALESCE(L.SLENET, 0)) AS "Sales Amount",
    SUM(COALESCE(L.SLBLUO, 0)) AS "Units Sold",
    COUNT(DISTINCT (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))) AS "Orders Count",
    COUNT(DISTINCT TRIM(H.SHCUST)) AS "Customer Count"
FROM GSFL2K.SHHEAD H
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC AND L.SLINV# = H.SHINV# AND L.SLORD# = H.SHORD#
LEFT JOIN GSFL2K.SALESMAN SM
  ON SM.SMNO = H.SHSLSM AND SM.SMCO = H.SHCO
WHERE H.SHCO = 1
  AND H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -{months})
GROUP BY {number_expr}, {name_expr}, RTRIM(CHAR(H.SHLOC))
ORDER BY "Sales Amount" DESC
"""


def _orders_per_day_sql(months: int) -> str:
    """Orders per day by inside rep. OHUSER is the Gartman login that keyed the order in
    (an inside/order-entry user id) — distinct from OHSLSM, the outside salesman code, which
    is blank on most orders and isn't a reliable "who created this" signal."""
    months = _sanitize_months(months)
    return f"""
SELECT
    OH.OHODAT AS "Order Date",
    COALESCE(TRIM(OH.OHUSER), '(Unknown)') AS "Inside Rep User ID",
    COUNT(DISTINCT (RTRIM(CHAR(OH.OHCO)) || '|' || RTRIM(CHAR(OH.OHLOC)) || '|' || RTRIM(CHAR(OH.OHORD#)))) AS "Orders Count"
FROM GSFL2K.OOHEAD OH
WHERE OH.OHCO = 1
  AND OH.OHODAT >= ADD_MONTHS(CURRENT_DATE, -{months})
  AND TRIM(OH.OHOTYP) <> 'TR'
  AND TRIM(OH.OHCUST) NOT LIKE '%TRANSFER%'
  AND TRIM(OH.OHCUST) NOT LIKE '%OMP000%'
  AND TRIM(OH.OHCUST) NOT LIKE '%INV000%'
  AND TRIM(OH.OHCUST) NOT LIKE '%OLD001%'
GROUP BY OH.OHODAT, COALESCE(TRIM(OH.OHUSER), '(Unknown)')
ORDER BY "Order Date"
"""


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_customer_sales_detail(months: int) -> tuple[pd.DataFrame, datetime]:
    return _read_gartman(_customer_sales_detail_sql(months))


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_customer_item_sales(
    months: int,
    include_discontinued: bool,
    item_numbers: tuple[str, ...] = (),
    divisions: tuple[str, ...] = (),
    product_families: tuple[str, ...] = (),
    collections: tuple[str, ...] = (),
    vendor_numbers: tuple[str, ...] = (),
    top_customers: int = 25,
    items_per_customer: int = 10,
    group_by: str = "Item",
) -> tuple[pd.DataFrame, datetime]:
    filters = {
        "item_numbers": item_numbers,
        "divisions": divisions,
        "product_families": product_families,
        "collections": collections,
        "vendor_numbers": vendor_numbers,
    }
    return _read_gartman(
        _customer_item_sales_sql(
            months,
            include_discontinued,
            filters,
            top_customers,
            items_per_customer,
            group_by,
        )
    )


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_entity_trend(days: int, grain: str) -> tuple[pd.DataFrame, datetime]:
    return _read_gartman(_entity_trend_sql(days, grain))


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_branch_summary(months: int) -> tuple[pd.DataFrame, datetime]:
    return _read_gartman(_branch_summary_sql(months))


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_employee_sales(months: int, combine_reps: bool = False) -> tuple[pd.DataFrame, datetime]:
    return _read_gartman(_employee_sales_sql(months, combine_reps))


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_orders_per_day(months: int) -> tuple[pd.DataFrame, datetime]:
    return _read_gartman(_orders_per_day_sql(months))


def _compute_trend_deltas(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["Current Period Sales"] = pd.to_numeric(out["Current Period Sales"], errors="coerce").fillna(0.0)
    out["Prior Period Sales"] = pd.to_numeric(out["Prior Period Sales"], errors="coerce").fillna(0.0)

    has_employee = "Employee Number" in out.columns
    if has_employee:
        active = out[(out["Current Period Sales"] != 0) | (out["Prior Period Sales"] != 0)]
        rep_counts = active.groupby("Entity Number")["Employee Number"].nunique()
        # The one employee name/number, for entities whose active rows all share a single rep.
        single_rep_entities = rep_counts[rep_counts == 1].index
        single_rep_rows = active[active["Entity Number"].isin(single_rep_entities)]
        single_rep_name = single_rep_rows.groupby("Entity Number")["Employee Name"].first()
        single_rep_number = single_rep_rows.groupby("Entity Number")["Employee Number"].first()

    agg = (
        out.groupby(["Entity Number", "Entity Name"], as_index=False)
        .agg({"Current Period Sales": "sum", "Prior Period Sales": "sum"})
    )

    if has_employee:
        agg["Employee Rep Count"] = agg["Entity Number"].map(rep_counts).fillna(0).astype(int)
        agg["Assigned Employee"] = agg["Entity Number"].map(single_rep_name).fillna("")
        agg["Assigned Employee Number"] = agg["Entity Number"].map(single_rep_number).fillna("")

    agg["Change $"] = agg["Current Period Sales"] - agg["Prior Period Sales"]
    agg["Change %"] = agg.apply(
        lambda r: (r["Change $"] / r["Prior Period Sales"] * 100.0) if r["Prior Period Sales"] else pd.NA,
        axis=1,
    )
    agg = agg[(agg["Current Period Sales"] != 0) | (agg["Prior Period Sales"] != 0)]
    return agg


def render_trend_section(
    title: str, help_text: str, grain: str, key_prefix: str, show_header: bool = True
) -> None:
    container = st.expander(title, expanded=True) if show_header else contextlib.nullcontext()
    with container:
        st.caption(help_text)
        interval_label = st.selectbox(
            "Comparison window", list(TREND_INTERVALS.keys()), key=f"{key_prefix}_interval"
        )
        days = TREND_INTERVALS[interval_label]
        with st.spinner("Loading trend data..."):
            df, queried_at = load_entity_trend(days, grain)
        deltas = _compute_trend_deltas(df)
        st.caption(f"Last queried: {queried_at.strftime('%b %d, %Y at %#I:%M %p')}")
        if deltas.empty:
            st.info("No sales activity found for this window.")
            return

        if "Assigned Employee" in deltas.columns:
            rep_lookup = (
                deltas.loc[deltas["Employee Rep Count"] == 1, ["Assigned Employee", "Assigned Employee Number"]]
                .drop_duplicates()
            )
            rep_labels = {
                f"{name} / {number}": number
                for name, number in zip(rep_lookup["Assigned Employee"], rep_lookup["Assigned Employee Number"])
                if name and number
            }
            selected_labels = st.multiselect(
                "Filter to accounts unique to selected sales rep(s)",
                options=sorted(rep_labels.keys()),
                key=f"{key_prefix}_unique_rep_pick",
                help="Restricts the list to accounts whose sales in this window were handled by "
                     "exactly one sales rep, limited to whichever rep(s) you pick here. Leave empty to "
                     "show all accounts, including house accounts and accounts split across reps.",
            )
            if selected_labels:
                selected_numbers = {rep_labels[label] for label in selected_labels}
                deltas = deltas[
                    (deltas["Employee Rep Count"] == 1) & (deltas["Assigned Employee Number"].isin(selected_numbers))
                ]
            if deltas.empty:
                st.info("No accounts match this filter.")
                return

        top_n = st.slider("Show top N", min_value=5, max_value=50, value=15, key=f"{key_prefix}_topn")
        gainers = deltas.sort_values("Change $", ascending=False).head(top_n)
        decliners = deltas.sort_values("Change $", ascending=True).head(top_n)

        display_cols = ["Entity Number", "Entity Name", "Prior Period Sales", "Current Period Sales", "Change $", "Change %"]
        money_column_config = {
            col: st.column_config.NumberColumn(col, format="$%,.2f")
            for col in ("Prior Period Sales", "Current Period Sales", "Change $")
        }
        money_column_config["Change %"] = st.column_config.NumberColumn("Change %", format="%.2f%%")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Biggest Gainers**")
            st.dataframe(
                gainers[display_cols],
                use_container_width=True,
                hide_index=True,
                column_config=money_column_config,
            )
        with col2:
            st.markdown("**Biggest Decliners**")
            st.dataframe(
                decliners[display_cols],
                use_container_width=True,
                hide_index=True,
                column_config=money_column_config,
            )

        chart_df = pd.concat([gainers, decliners]).drop_duplicates(subset=["Entity Number"])
        chart_df = chart_df.sort_values("Change $", ascending=True)
        fig = px.bar(
            chart_df,
            x="Change $",
            y="Entity Name",
            orientation="h",
            color="Change $",
            color_continuous_scale=["#c0392b", "#e0e0e0", "#1e824c"],
            color_continuous_midpoint=0,
            hover_data={"Entity Number": True, "Change $": ":,.2f"},
            title=f"{title}: Change in Sales $ ({interval_label})",
        )
        fig.update_layout(height=max(400, 24 * len(chart_df)), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)


def _landed_cost_history_sql(
    include_discontinued: bool,
    months: int,
    filters: dict[str, tuple[str, ...]] | None = None,
) -> str:
    months = _sanitize_months(months)
    active_clause = _active_item_clause(include_discontinued)
    filter_clause = _extra_filter_clause(
        filters,
        vendor_expr="TRIM(CHAR(R.IRVEND))",
    )
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    return f"""
SELECT
    R.IRDATE - (DAY(R.IRDATE) - 1) DAYS AS "Month",
    R.IRDATE AS "Receipt Date",
    TRIM(R.IRITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    {family_expr} AS "Product Family",
    TRIM(CHAR(R.IRVEND)) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    DECIMAL(R.IRCOST, 18, 5) AS "Landed Cost",
    SUM(
        CASE
            WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                THEN DECIMAL(R.IRQTY, 18, 6)
            ELSE DECIMAL(R.IRQTY, 18, 6) * IM.IMFACT
        END
    ) AS "Receipt Qty",
    COUNT(*) AS "Receipt Count"
FROM GSFL2K.ITEMRECH R
JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = R.IRITEM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
{family_join}
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = R.IRVEND
WHERE R.IRCO = 1
  AND TRIM(R.IRSRC) = 'P'
  AND R.IRQTY > 0
  AND R.IRCOST > 0
  AND R.IRDATE >= ADD_MONTHS(CURRENT_DATE, -{months})
  AND {active_clause}{filter_clause}
GROUP BY
    R.IRDATE - (DAY(R.IRDATE) - 1) DAYS,
    R.IRDATE,
    TRIM(R.IRITEM),
    TRIM(IM.IMDESC),
    TRIM(IX.IMCOLLECT),
    RTRIM(CHAR(IM.IMDIV)),
    {family_expr},
    TRIM(CHAR(R.IRVEND)),
    TRIM(VM.VMNAME),
    DECIMAL(R.IRCOST, 18, 5)
ORDER BY "Receipt Date" DESC, "Item Number"
"""


def _average_inventory_cost_sql(
    include_discontinued: bool,
    filters: dict[str, tuple[str, ...]] | None = None,
) -> str:
    locs = _loc_list_sql()
    active_clause = _active_item_clause(include_discontinued)
    filter_clause = _extra_filter_clause(
        filters,
        branch_expr="RTRIM(CHAR(D.IDLOC))",
    )
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    return f"""
SELECT
    TRIM(D.IDITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    {family_expr} AS "Product Family",
    RTRIM(CHAR(D.IDLOC)) AS "Branch Location",
    TRIM(IM.IMVEND) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    SUM(D.IDQOH) AS "On Hand Qty",
    DECIMAL(
        DECIMAL(SUM(DECIMAL(D.IDCOST, 18, 6) * DECIMAL(D.IDQOH, 18, 6)), 18, 6)
        /
        NULLIF(DECIMAL(SUM(DECIMAL(D.IDQOH, 18, 6)), 18, 6), 0),
        18,
        5
    ) AS "Average Inventory Cost",
    DECIMAL(SUM(DECIMAL(D.IDCOST, 18, 6) * DECIMAL(D.IDQOH, 18, 6)), 18, 2) AS "Inventory Value"
FROM GSFL2K.ITEMDETL D
JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = D.IDITEM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
{family_join}
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
WHERE D.IDCO = 1
  AND D.IDLOC IN ({locs})
  AND COALESCE(D.IDDELT, '') <> 'D'
  AND D.IDQOH > 0
  AND D.IDCOST IS NOT NULL
  AND D.IDCOST <> 0
  AND {active_clause}{filter_clause}
GROUP BY
    TRIM(D.IDITEM),
    TRIM(IM.IMDESC),
    TRIM(IX.IMCOLLECT),
    RTRIM(CHAR(IM.IMDIV)),
    {family_expr},
    RTRIM(CHAR(D.IDLOC)),
    TRIM(IM.IMVEND),
    TRIM(VM.VMNAME)
ORDER BY "Inventory Value" DESC, "Item Number"
"""


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_product_dataset(
    dataset: str,
    include_discontinued: bool,
    months: int,
    item_numbers: tuple[str, ...] = (),
    divisions: tuple[str, ...] = (),
    product_families: tuple[str, ...] = (),
    collections: tuple[str, ...] = (),
    branch_locations: tuple[str, ...] = (),
    vendor_numbers: tuple[str, ...] = (),
    date_start: str = "",
    date_end: str = "",
    separate_sales_by_branch: bool = False,
) -> tuple[pd.DataFrame, datetime]:
    filters = {
        "item_numbers": item_numbers,
        "divisions": divisions,
        "product_families": product_families,
        "collections": collections,
        "branch_locations": branch_locations,
        "vendor_numbers": vendor_numbers,
    }
    if dataset == "Sales Over Time":
        sql = _sales_over_time_sql(
            include_discontinued,
            months,
            filters,
            date_start,
            date_end,
            separate_sales_by_branch,
        )
    elif dataset == "Sales Summary":
        sql = _sales_summary_sql(
            include_discontinued,
            months,
            filters,
            date_start,
            date_end,
            separate_sales_by_branch,
        )
    elif dataset == "Landed Cost History":
        sql = _landed_cost_history_sql(include_discontinued, months, filters)
    elif dataset == "Average Inventory Cost":
        sql = _average_inventory_cost_sql(include_discontinued, filters)
    else:
        sql = _product_snapshot_sql(include_discontinued, filters, date_start, date_end)
    return _read_gartman(sql)


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_product_filter_lookup(include_discontinued: bool) -> pd.DataFrame:
    active_clause = _active_item_clause(include_discontinued)
    family_expr = _product_family_sql_expr()
    family_join = _product_family_join_sql()
    sql = f"""
SELECT
    TRIM(IM.IMITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    {family_expr} AS "Product Family",
    TRIM(IM.IMVEND) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name"
FROM GSFL2K.ITEMMAST IM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
{family_join}
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
WHERE {active_clause}
ORDER BY
    RTRIM(CHAR(IM.IMDIV)),
    {family_expr},
    TRIM(IX.IMCOLLECT),
    TRIM(IM.IMITEM)
"""
    df, _ = _read_gartman(sql)
    return df


def apply_dashboard_css() -> None:
    st.markdown(
        """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');

:root {
  --exec-bg: #f2f5f9;
  --exec-panel: #ffffff;
  --exec-panel-strong: #f1f5fb;
  --exec-border: #cbd5e1;
  --exec-text: #142033;
  --exec-muted: #58677b;
  --exec-navy: #1e3a5f;
  --exec-green: #e7f4d7;
  --exec-blue: #d8ebfb;
  --exec-shadow: 0 8px 22px rgba(15, 23, 42, 0.07);
}

html, body, [data-testid="stAppViewContainer"] {
  background: var(--exec-bg);
  color: var(--exec-text);
  font-family: 'Manrope', sans-serif;
}

.block-container {
  max-width: 1600px;
  padding-top: 4.75rem;
  padding-bottom: 2rem;
}

header[data-testid="stHeader"] {
  background: rgba(242, 245, 249, 0.96);
}

.exec-hero {
  background: var(--exec-panel);
  border: 1px solid var(--exec-border);
  border-radius: 14px;
  padding: 18px 22px;
  box-shadow: var(--exec-shadow);
  margin-bottom: 16px;
}

.exec-hero-top {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  align-items: flex-start;
}

.exec-pill {
  display: inline-flex;
  padding: 5px 11px;
  border: 1px solid #c7d2fe;
  border-radius: 999px;
  background: #eef2ff;
  color: var(--exec-navy);
  font-size: 0.78rem;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.exec-title {
  color: var(--exec-navy);
  font-size: 2rem;
  line-height: 1.1;
  font-weight: 800;
  margin-top: 10px;
}

.exec-subtitle {
  color: var(--exec-muted);
  font-size: 0.95rem;
  margin-top: 6px;
}

.exec-meta {
  color: var(--exec-muted);
  text-align: right;
  font-size: 0.85rem;
  line-height: 1.45;
  white-space: nowrap;
}

.exec-stat-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
  margin: 10px 0 14px 0;
}

.exec-stat {
  background: var(--exec-panel);
  border: 1px solid var(--exec-border);
  border-radius: 8px;
  padding: 10px 12px;
}

.exec-stat-label {
  color: var(--exec-muted);
  font-size: 0.73rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.exec-stat-value {
  color: var(--exec-text);
  font-size: 1.1rem;
  font-weight: 800;
  margin-top: 4px;
}

.financial-stat-row {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}

.financial-grid {
  display: grid;
  grid-template-columns: minmax(360px, 1.05fr) minmax(360px, 1.1fr) minmax(300px, 0.85fr);
  gap: 12px;
  align-items: start;
}

.financial-panel {
  background: var(--exec-panel);
  border: 1px solid var(--exec-border);
  border-radius: 8px;
  box-shadow: var(--exec-shadow);
  overflow: hidden;
}

.financial-panel-header {
  background: var(--exec-navy);
  color: #ffffff;
  padding: 10px 12px;
}

.financial-panel-title {
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-size: 0.86rem;
}

.financial-panel-subtitle {
  color: rgba(255, 255, 255, 0.78);
  font-size: 0.76rem;
  margin-top: 2px;
}

.financial-table-scroll {
  overflow-x: auto;
}

.financial-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.78rem;
  table-layout: fixed;
}

.financial-table th {
  background: #eef3f9;
  color: var(--exec-muted);
  font-size: 0.68rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 7px 9px;
  border-bottom: 1px solid var(--exec-border);
  text-align: right;
}

.financial-table th:first-child {
  text-align: left;
}

.financial-table td {
  color: var(--exec-text);
  padding: 5px 9px;
  border-bottom: 1px solid #edf2f7;
  text-align: right;
  white-space: nowrap;
}

.financial-table td:first-child {
  text-align: left;
  white-space: normal;
  overflow-wrap: break-word;
}

.financial-table tr.is-emphasis td {
  font-weight: 800;
  background: #f8fafc;
  border-top: 1px solid var(--exec-border);
}

.financial-table .negative {
  color: #9f1239;
}

.financial-table th.benchmark-col,
.financial-table td.benchmark-col {
  background: #f7f5ff;
  border-left: 1px solid var(--exec-border);
  font-style: italic;
}

.financial-table tr.is-emphasis td.benchmark-col {
  background: #efeaff;
}

.financial-notes {
  color: var(--exec-muted);
  font-size: 0.78rem;
  line-height: 1.45;
  padding: 10px 12px 12px 12px;
}

.financial-table tr.has-detail > td {
  padding: 0;
}

.financial-detail {
  width: 100%;
}

.financial-detail > summary {
  display: grid;
  grid-template-columns: 1fr auto auto auto;
  align-items: center;
  gap: 0;
  padding: 0;
  cursor: pointer;
  list-style: none;
  color: var(--exec-text);
  position: relative;
}

.financial-detail > summary::-webkit-details-marker {
  display: none;
}

.financial-detail > summary::before {
  content: "▸";
  position: absolute;
  left: 9px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--exec-muted);
  font-size: 0.7rem;
}

.financial-detail[open] > summary::before {
  content: "▾";
}

.financial-detail .fd-line,
.financial-detail .fd-value,
.financial-detail .fd-pct,
.financial-detail .fd-benchmark {
  box-sizing: border-box;
  min-width: 0;
  padding: 5px 7px;
}

.financial-detail .fd-line {
  padding-left: 22px;
  overflow-wrap: break-word;
}

.financial-detail .fd-value,
.financial-detail .fd-pct,
.financial-detail .fd-benchmark {
  text-align: right;
  white-space: nowrap;
}

.financial-detail .fd-benchmark {
  color: var(--exec-muted);
  font-style: italic;
}

.financial-detail .fd-value.negative,
.financial-detail .fd-pct.negative,
.financial-detail .fd-benchmark.negative {
  color: #9f1239;
}

.financial-detail-body {
  padding: 2px 9px 8px 22px;
  background: #f8fafc;
  max-height: 320px;
  overflow-y: auto;
}

.financial-detail-body table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.72rem;
}

.financial-detail-body td {
  padding: 3px 6px;
  border-bottom: 1px solid #edf2f7;
  text-align: right;
  color: var(--exec-muted);
  white-space: nowrap;
}

.financial-detail-body td:first-child {
  text-align: left;
  white-space: normal;
}

div[data-testid="stExpander"] {
  background: var(--exec-panel);
  border: 1px solid var(--exec-border);
  border-radius: 10px;
  box-shadow: var(--exec-shadow);
  overflow: hidden;
}

div[data-testid="stExpander"] details summary {
  background: var(--exec-navy);
  color: #ffffff;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 0.75rem 1rem;
}

div[data-testid="stExpander"] details[open] summary {
  border-bottom: 1px solid var(--exec-border);
}

div[data-testid="stExpander"] div[data-testid="stVerticalBlock"] {
  gap: 0.75rem;
}

.stButton button {
  border-radius: 8px !important;
  border: 1px solid var(--exec-border) !important;
  background: var(--exec-panel-strong) !important;
  color: var(--exec-text) !important;
  font-weight: 800 !important;
}

.stButton button:hover {
  border-color: #94a3b8 !important;
  background: #e5edf6 !important;
}

div[class*="st-key-exec_lazy_header_"] {
  margin-top: 0.75rem;
}

div[class*="st-key-exec_lazy_header_"] button {
  display: flex !important;
  align-items: center !important;
  justify-content: flex-start !important;
  text-align: left !important;
  background: var(--exec-navy) !important;
  color: #ffffff !important;
  border: 1px solid var(--exec-navy) !important;
  border-radius: 8px !important;
  font-weight: 800 !important;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 0.75rem 1rem !important;
  min-height: 52px;
  transition: background 0.15s ease, border-color 0.15s ease;
}

div[class*="st-key-exec_lazy_header_"] button > div {
  width: 100% !important;
  display: flex !important;
  flex-direction: row !important;
  align-items: center !important;
  justify-content: flex-start !important;
  gap: 0.55rem !important;
  text-align: left !important;
}

div[class*="st-key-exec_lazy_header_"] button span[data-has-shortcut] {
  width: auto !important;
  display: inline-flex !important;
  align-items: center !important;
  flex: 0 1 auto !important;
  text-align: left !important;
}

div[class*="st-key-exec_lazy_header_"] button div[data-testid="stMarkdownContainer"] {
  width: auto !important;
  display: inline-flex !important;
  text-align: left !important;
}

div[class*="st-key-exec_lazy_header_"] button p {
  width: auto !important;
  margin: 0 !important;
  text-align: left !important;
  font-size: 0.97rem;
  white-space: nowrap !important;
}

div[class*="st-key-exec_lazy_header_"] button:hover {
  background: #17385f !important;
  border-color: #17385f !important;
  color: #ffffff !important;
}

div[data-testid="stDataFrame"] {
  border: 1px solid var(--exec-border);
  border-radius: 8px;
  overflow: hidden;
}

label, .stSelectbox label, .stMultiSelect label, .stTextInput label, .stNumberInput label {
  color: var(--exec-text) !important;
  font-weight: 800 !important;
}

@media (max-width: 900px) {
  .exec-hero-top {
    display: block;
  }

  .exec-meta {
    margin-top: 12px;
    text-align: left;
    white-space: normal;
  }

  .exec-stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .financial-grid {
    grid-template-columns: 1fr;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )


def render_tab_buttons() -> str:
    tabs = ["Product", "Inventory", "Customer", "Order", "Location", "Financial"]
    if "executive_tab" not in st.session_state:
        st.session_state.executive_tab = "Product"
    if st.session_state.executive_tab not in tabs:
        st.session_state.executive_tab = "Product"

    cols = st.columns(len(tabs), gap="medium")
    for idx, tab_name in enumerate(tabs):
        active = st.session_state.executive_tab == tab_name
        button_label = f"{tab_name}" if active else tab_name
        with cols[idx]:
            if st.button(button_label, key=f"exec_tab_{tab_name.lower()}", use_container_width=True):
                st.session_state.executive_tab = tab_name
                st.rerun()

    active_idx = tabs.index(st.session_state.executive_tab) + 1
    st.markdown(
        f"""
<style>
div[data-testid="stHorizontalBlock"] > div:nth-child({active_idx}) button[data-testid="stBaseButton-secondary"] {{
  background: #1e3a5f !important;
  color: #ffffff !important;
  border-color: #1e3a5f !important;
}}
</style>
        """,
        unsafe_allow_html=True,
    )
    return st.session_state.executive_tab


def render_hero(queried_at: datetime | None = None, meta_lines: list[str] | None = None) -> None:
    if meta_lines is None:
        stamp = queried_at.strftime("%b %d, %Y at %#I:%M %p") if queried_at else "Not queried yet"
        meta_lines = [
            "Product tab: live Gartman query",
            f"Cache window: {DB_CACHE_TTL_SECONDS // 60} minutes",
            f"Last queried: {stamp}",
        ]
    meta_html = "".join(f"<div>{html.escape(line)}</div>" for line in meta_lines)
    st.markdown(
        f"""
<div class="exec-hero">
  <div class="exec-hero-top">
    <div>
      <div class="exec-pill">Gartman Executive View</div>
      <div class="exec-title">{APP_TITLE}</div>
      <div class="exec-subtitle">A single workspace for product, inventory, customer, order, location, and financial analysis.</div>
    </div>
    <div class="exec-meta">
      {meta_html}
    </div>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )


def _format_number(value: float, decimals: int = 0, prefix: str = "") -> str:
    if pd.isna(value):
        return ""
    return f"{prefix}{value:,.{decimals}f}"


def render_summary_stats(df: pd.DataFrame, dataset: str) -> None:
    total_rows = len(df)
    if dataset in {"Sales Over Time", "Sales Summary"}:
        stats = [
            ("Rows", f"{total_rows:,}"),
            ("Sales", _format_number(df.get("Sales Amount", pd.Series(dtype=float)).sum(), 0, "$")),
            ("Units", _format_number(df.get("Units Sold", pd.Series(dtype=float)).sum(), 0)),
            ("Orders", _format_number(df.get("Orders Count", pd.Series(dtype=float)).sum(), 0)),
            ("Items", f"{df.get('Item Number', pd.Series(dtype=object)).nunique():,}"),
        ]
    elif dataset == "Landed Cost History":
        landed = df.get("Landed Cost", pd.Series(dtype=float))
        stats = [
            ("Rows", f"{total_rows:,}"),
            ("Avg Landed Cost", _format_number(landed.mean(), 2, "$")),
            ("Receipt Qty", _format_number(df.get("Receipt Qty", pd.Series(dtype=float)).sum(), 0)),
            ("Vendors", f"{df.get('Vendor Number', pd.Series(dtype=object)).nunique():,}"),
            ("Items", f"{df.get('Item Number', pd.Series(dtype=object)).nunique():,}"),
        ]
    elif dataset == "Average Inventory Cost":
        stats = [
            ("Rows", f"{total_rows:,}"),
            ("Inventory Value", _format_number(df.get("Inventory Value", pd.Series(dtype=float)).sum(), 0, "$")),
            ("On Hand Qty", _format_number(df.get("On Hand Qty", pd.Series(dtype=float)).sum(), 0)),
            ("Avg Cost", _format_number(df.get("Average Inventory Cost", pd.Series(dtype=float)).mean(), 2, "$")),
            ("Items", f"{df.get('Item Number', pd.Series(dtype=object)).nunique():,}"),
        ]
    else:
        stats = [
            ("Rows", f"{total_rows:,}"),
            ("Sales Range", _format_number(df.get("Sales $ - Selected Range", pd.Series(dtype=float)).sum(), 0, "$")),
            ("Available", _format_number(df.get("Current Inventory Available", pd.Series(dtype=float)).sum(), 0)),
            ("Backorders", _format_number(df.get("Backorders", pd.Series(dtype=float)).sum(), 0)),
            ("Items", f"{df.get('Item Number', pd.Series(dtype=object)).nunique():,}"),
        ]

    html = ['<div class="exec-stat-row">']
    for label, value in stats:
        html.append(
            f'<div class="exec-stat"><div class="exec-stat-label">{label}</div>'
            f'<div class="exec-stat-value">{value}</div></div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def _financial_statement_row(
    line: str,
    value: float | None,
    denominator: float,
    *,
    emphasis: bool = False,
    benchmark: float | None = None,
) -> dict[str, object]:
    common_size = None
    if value is not None and abs(denominator) > 0.0001:
        common_size = float(value) / float(denominator)
    return {
        "Line": line,
        "Value": value,
        "Common Size": common_size,
        "Emphasis": emphasis,
        "Benchmark": benchmark,
    }


def _format_financial_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return ""
    value = float(value)
    if value < -0.005:
        return f"(${abs(value):,.0f})"
    return f"${value:,.0f}"


def _format_financial_percent(value: float | None) -> str:
    if value is None or pd.isna(value):
        return ""
    value = float(value)
    if value < -0.00005:
        return f"({abs(value):.1%})"
    return f"{value:.1%}"


def _sum_financial_accounts(tb: pd.DataFrame, mask: pd.Series) -> float:
    return float(tb.loc[mask, "Balance"].sum())


def _credit_financial_accounts(tb: pd.DataFrame, mask: pd.Series) -> float:
    return -1.0 * _sum_financial_accounts(tb, mask)


def _financial_detail_rows(
    tb: pd.DataFrame,
    mask: pd.Series,
    denominator: float,
    *,
    flip_sign: bool = False,
) -> list[dict[str, object]]:
    """Non-zero GL accounts behind a statement line, biggest balance first."""
    subset = tb.loc[mask, ["Account", "Description", "Balance"]]
    subset = subset.loc[subset["Balance"].abs() > 0.005]
    subset = subset.reindex(subset["Balance"].abs().sort_values(ascending=False).index)
    sign = -1.0 if flip_sign else 1.0
    rows: list[dict[str, object]] = []
    for _, r in subset.iterrows():
        label = f"{int(r['Account'])} · {str(r['Description']).strip()}"
        rows.append(_financial_statement_row(label, float(r["Balance"]) * sign, denominator))
    return rows


def _financial_manual_detail_rows(
    items: list[tuple[str, float | None]],
    denominator: float,
) -> list[dict[str, object]]:
    """Detail rows built from already-computed sub-totals rather than a raw account mask."""
    rows: list[dict[str, object]] = []
    for label, value in items:
        if value is None or abs(value) < 0.005:
            continue
        rows.append(_financial_statement_row(label, value, denominator))
    rows.sort(key=lambda r: abs(r["Value"]) if r["Value"] is not None else 0.0, reverse=True)
    return rows


def _financial_with_detail(row: dict[str, object], detail: list[dict[str, object]]) -> dict[str, object]:
    if detail:
        row["Detail"] = detail
    return row


@st.cache_data(ttl=DB_CACHE_TTL_SECONDS, show_spinner=False)
def load_financial_statement_report(source_path: str) -> dict[str, object]:
    path = Path(source_path)
    raw = pd.read_excel(path, sheet_name=0, header=4)
    raw.columns = [str(column).strip() for column in raw.columns]
    raw["Account"] = pd.to_numeric(raw.get("Account #"), errors="coerce")
    raw["Balance"] = pd.to_numeric(raw.get("Balance"), errors="coerce").fillna(0.0)
    descriptions = raw.get("Description", pd.Series("", index=raw.index)).fillna("").astype(str).str.strip()
    is_company_total = descriptions.str.upper().eq("COMPANY TOTAL")
    suspense_mask = (raw["Account"].isna() | raw["Account"].gt(999999)) & ~is_company_total
    suspense = float(raw.loc[suspense_mask, "Balance"].sum())

    tb = raw.loc[raw["Account"].notna() & raw["Account"].le(999999), ["Account", "Description", "Balance"]].copy()
    tb["Account"] = tb["Account"].astype(int)
    acct = tb["Account"]

    cash = _sum_financial_accounts(tb, acct.between(1000, 1099))
    ar_net = _sum_financial_accounts(tb, acct.isin([1110, 1120]))
    inventory = _sum_financial_accounts(tb, acct.between(1200, 1300) | acct.between(12000, 12999))
    prepaids_other_current = _sum_financial_accounts(tb, acct.between(1310, 1499) | acct.eq(1840))
    current_assets = cash + ar_net + inventory + prepaids_other_current

    note_debit_reclass = _sum_financial_accounts(tb, acct.isin([2593, 2598]))
    ppe_net = _sum_financial_accounts(tb, acct.between(1510, 1530)) + _sum_financial_accounts(tb, acct.between(1555, 1575))
    rou_asset = _sum_financial_accounts(tb, acct.eq(1700))
    deposits = _sum_financial_accounts(tb, acct.eq(1650))
    total_assets = _sum_financial_accounts(tb, acct.between(1000, 1999) | acct.between(12000, 12999)) + note_debit_reclass

    ap_accrued = _credit_financial_accounts(tb, acct.isin([2150, 2155, 2157, 2160]))
    payroll_tax_other = _credit_financial_accounts(tb, acct.isin([2158, 2159, 2322, 2323, 2324, 2330, 2370, 2375, 2377]))
    customer_deposits = _credit_financial_accounts(tb, acct.eq(2390))
    current_lease = _credit_financial_accounts(tb, acct.eq(2200))
    notes_loans = _credit_financial_accounts(tb, acct.isin([2404, 2500, 2594]))
    long_term_lease = _credit_financial_accounts(tb, acct.eq(2600))
    total_liabilities = ap_accrued + payroll_tax_other + customer_deposits + current_lease + notes_loans + long_term_lease

    common_stock = _credit_financial_accounts(tb, acct.eq(2719))
    retained_earnings = _credit_financial_accounts(tb, acct.eq(2720))
    shareholder_draws = -1.0 * _sum_financial_accounts(tb, acct.isin([2736, 2737]))
    equity_before_income = common_stock + retained_earnings + shareholder_draws

    gross_sales = _credit_financial_accounts(tb, acct.between(3100, 3399) | acct.between(31000, 31999))
    discounts_returns = _sum_financial_accounts(tb, acct.isin([3400, 3500]))
    net_sales = gross_sales - discounts_returns
    cogs = _sum_financial_accounts(tb, acct.between(3550, 4999) | acct.between(40000, 40999))
    gross_profit = net_sales - cogs

    salaries_benefits = _sum_financial_accounts(tb, acct.isin([6000, 7010, 7040, 7760, 8465, 8467]))
    commissions = _sum_financial_accounts(tb, acct.isin([7609, 7610]))
    occupancy = _sum_financial_accounts(tb, acct.between(8545, 8559) | acct.between(8560, 8568) | acct.eq(8800))
    freight_auto = _sum_financial_accounts(tb, acct.isin([7450, 7451, 7452, 7455, 7460, 8580]))
    card_bank = _sum_financial_accounts(tb, acct.eq(7560))
    professional = _sum_financial_accounts(tb, acct.isin([7615, 8310, 8460]))
    insurance = _sum_financial_accounts(tb, acct.between(8060, 8073))
    advertising_promotion = _sum_financial_accounts(tb, acct.isin([7400, 8520]))
    all_operating_expense = _sum_financial_accounts(tb, acct.between(6000, 8999) & ~acct.eq(8160))
    known_operating_expense = (
        salaries_benefits
        + commissions
        + occupancy
        + freight_auto
        + card_bank
        + professional
        + insurance
        + advertising_promotion
    )
    other_operating_expense = all_operating_expense - known_operating_expense + suspense
    total_operating_expense = all_operating_expense + suspense
    operating_income = gross_profit - total_operating_expense
    interest_expense = _sum_financial_accounts(tb, acct.eq(8160))
    other_income_net = _credit_financial_accounts(tb, acct.between(9000, 9299))
    net_income = operating_income - interest_expense + other_income_net
    ending_equity = equity_before_income + net_income

    known_opex_mask = (
        acct.isin([6000, 7010, 7040, 7760, 8465, 8467])
        | acct.isin([7609, 7610])
        | acct.between(8545, 8559)
        | acct.between(8560, 8568)
        | acct.eq(8800)
        | acct.isin([7450, 7451, 7452, 7455, 7460, 8580])
        | acct.eq(7560)
        | acct.isin([7615, 8310, 8460])
        | acct.between(8060, 8073)
        | acct.isin([7400, 8520])
    )
    other_opex_mask = acct.between(6000, 8999) & ~acct.eq(8160) & ~known_opex_mask
    other_opex_detail = _financial_detail_rows(tb, other_opex_mask, net_sales)
    other_opex_detail.extend(
        _financial_manual_detail_rows([("Suspense/unsupported balance", suspense)], net_sales)
    )
    other_opex_detail.sort(key=lambda r: abs(r["Value"]) if r["Value"] is not None else 0.0, reverse=True)

    cash_row = _financial_with_detail(
        _financial_statement_row("Cash and equivalents", cash, total_assets, benchmark=_financial_benchmark_pct("cash", "total_assets")),
        _financial_detail_rows(tb, acct.between(1000, 1099), total_assets),
    )
    ar_row = _financial_with_detail(
        _financial_statement_row(
            "Accounts receivable, net", ar_net, total_assets,
            benchmark=_financial_benchmark_pct("accounts_receivable", "total_assets"),
        ),
        _financial_detail_rows(tb, acct.isin([1110, 1120]), total_assets),
    )
    inventory_row = _financial_with_detail(
        _financial_statement_row(
            "Inventory, net", inventory, total_assets, benchmark=_financial_benchmark_pct("inventory", "total_assets")
        ),
        _financial_detail_rows(tb, acct.between(1200, 1300) | acct.between(12000, 12999), total_assets),
    )
    prepaids_row = _financial_with_detail(
        _financial_statement_row(
            "Prepaids and other current", prepaids_other_current, total_assets,
            benchmark=_financial_benchmark_pct("prepaid_other_current", "total_assets"),
        ),
        _financial_detail_rows(tb, acct.between(1310, 1499) | acct.eq(1840), total_assets),
    )
    note_reclass_row = _financial_with_detail(
        _financial_statement_row("Related-party note reclass", note_debit_reclass, total_assets),
        _financial_detail_rows(tb, acct.isin([2593, 2598]), total_assets),
    )
    ppe_row = _financial_with_detail(
        _financial_statement_row(
            "Property and equipment, net", ppe_net, total_assets,
            benchmark=_financial_benchmark_pct("ppe_net", "total_assets"),
        ),
        _financial_detail_rows(tb, acct.between(1510, 1530) | acct.between(1555, 1575), total_assets),
    )
    ap_row = _financial_with_detail(
        _financial_statement_row(
            "AP and accrued purchases", ap_accrued, total_assets,
            benchmark=_financial_benchmark_pct("accounts_payable", "total_assets"),
        ),
        _financial_detail_rows(tb, acct.isin([2150, 2155, 2157, 2160]), total_assets, flip_sign=True),
    )
    payroll_accrual_row = _financial_with_detail(
        _financial_statement_row(
            "Payroll/tax/other accruals", payroll_tax_other, total_assets,
            benchmark=_financial_benchmark_pct("payroll_tax_other_accrued", "total_assets"),
        ),
        _financial_detail_rows(
            tb, acct.isin([2158, 2159, 2322, 2323, 2324, 2330, 2370, 2375, 2377]), total_assets, flip_sign=True
        ),
    )
    notes_loans_row = _financial_with_detail(
        _financial_statement_row(
            "Notes/loans payable", notes_loans, total_assets,
            benchmark=_financial_benchmark_pct("notes_loans_payable", "total_assets"),
        ),
        _financial_detail_rows(tb, acct.isin([2404, 2500, 2594]), total_assets, flip_sign=True),
    )
    equity_bs_row = _financial_with_detail(
        _financial_statement_row(
            "Stockholders' equity", ending_equity, total_assets, emphasis=True,
            benchmark=_financial_benchmark_pct("total_equity", "total_assets"),
        ),
        _financial_manual_detail_rows(
            [
                ("Common stock", common_stock),
                ("Retained earnings", retained_earnings),
                ("Less shareholder draws", shareholder_draws),
                ("FY 2025 net income", net_income),
            ],
            total_assets,
        ),
    )

    balance_sheet_rows = [
        cash_row,
        ar_row,
        inventory_row,
        prepaids_row,
        _financial_statement_row(
            "Total operating current assets", current_assets, total_assets, emphasis=True,
            benchmark=_financial_benchmark_pct("total_current_assets", "total_assets"),
        ),
        note_reclass_row,
        ppe_row,
        _financial_statement_row(
            "Operating lease ROU assets", rou_asset, total_assets,
            benchmark=_financial_benchmark_pct("rou_asset", "total_assets"),
        ),
        _financial_statement_row(
            "Deposits and other assets", deposits, total_assets,
            benchmark=_financial_benchmark_pct("other_lt_assets", "total_assets"),
        ),
        _financial_statement_row("TOTAL ASSETS", total_assets, total_assets, emphasis=True, benchmark=1.0),
        _financial_statement_row("", None, total_assets),
        ap_row,
        payroll_accrual_row,
        _financial_statement_row(
            "Customer deposits", customer_deposits, total_assets,
            benchmark=_financial_benchmark_pct("customer_deposits", "total_assets"),
        ),
        _financial_statement_row(
            "Current lease liability", current_lease, total_assets,
            benchmark=_financial_benchmark_pct("current_lease_liability", "total_assets"),
        ),
        notes_loans_row,
        _financial_statement_row(
            "Long-term lease liability", long_term_lease, total_assets,
            benchmark=_financial_benchmark_pct("long_term_lease_liability", "total_assets"),
        ),
        _financial_statement_row(
            "TOTAL LIABILITIES", total_liabilities, total_assets, emphasis=True,
            benchmark=_financial_benchmark_pct("total_liabilities", "total_assets"),
        ),
        equity_bs_row,
        _financial_statement_row(
            "TOTAL LIABILITIES AND EQUITY", total_liabilities + ending_equity, total_assets, emphasis=True,
            benchmark=1.0,
        ),
    ]

    gross_sales_row = _financial_with_detail(
        _financial_statement_row("Gross sales", gross_sales, net_sales),
        _financial_detail_rows(tb, acct.between(3100, 3399) | acct.between(31000, 31999), net_sales, flip_sign=True),
    )
    discounts_row = _financial_with_detail(
        _financial_statement_row("Less discounts/returns", -discounts_returns, net_sales),
        _financial_detail_rows(tb, acct.isin([3400, 3500]), net_sales, flip_sign=True),
    )
    cogs_row = _financial_with_detail(
        _financial_statement_row(
            "Cost of goods sold", cogs, net_sales, benchmark=_financial_benchmark_pct("cogs", "net_sales")
        ),
        _financial_detail_rows(tb, acct.between(3550, 4999) | acct.between(40000, 40999), net_sales),
    )
    salaries_row = _financial_with_detail(
        _financial_statement_row("Salaries/wages/benefits", salaries_benefits, net_sales),
        _financial_detail_rows(tb, acct.isin([6000, 7010, 7040, 7760, 8465, 8467]), net_sales),
    )
    commissions_row = _financial_with_detail(
        _financial_statement_row("Commissions", commissions, net_sales),
        _financial_detail_rows(tb, acct.isin([7609, 7610]), net_sales),
    )
    occupancy_row = _financial_with_detail(
        _financial_statement_row("Occupancy/utilities/repairs", occupancy, net_sales),
        _financial_detail_rows(
            tb, acct.between(8545, 8559) | acct.between(8560, 8568) | acct.eq(8800), net_sales
        ),
    )
    freight_row = _financial_with_detail(
        _financial_statement_row("Freight and auto", freight_auto, net_sales),
        _financial_detail_rows(tb, acct.isin([7450, 7451, 7452, 7455, 7460, 8580]), net_sales),
    )
    professional_row = _financial_with_detail(
        _financial_statement_row("Professional/outside services", professional, net_sales),
        _financial_detail_rows(tb, acct.isin([7615, 8310, 8460]), net_sales),
    )
    insurance_row = _financial_with_detail(
        _financial_statement_row("Insurance", insurance, net_sales),
        _financial_detail_rows(tb, acct.between(8060, 8073), net_sales),
    )
    advertising_row = _financial_with_detail(
        _financial_statement_row(
            "Advertising and promotion", advertising_promotion, net_sales,
            benchmark=_financial_benchmark_pct("advertising_promotion", "net_sales"),
        ),
        _financial_detail_rows(tb, acct.isin([7400, 8520]), net_sales),
    )
    other_opex_row = _financial_with_detail(
        _financial_statement_row("Other operating expense", other_operating_expense, net_sales),
        other_opex_detail,
    )
    other_income_row = _financial_with_detail(
        _financial_statement_row("Other income, net", other_income_net, net_sales),
        _financial_detail_rows(tb, acct.between(9000, 9299), net_sales, flip_sign=True),
    )

    income_statement_rows = [
        gross_sales_row,
        discounts_row,
        _financial_statement_row("NET SALES", net_sales, net_sales, emphasis=True, benchmark=1.0),
        cogs_row,
        _financial_statement_row(
            "GROSS PROFIT", gross_profit, net_sales, emphasis=True,
            benchmark=_financial_benchmark_pct("gross_profit", "net_sales"),
        ),
        _financial_statement_row("", None, net_sales),
        salaries_row,
        commissions_row,
        occupancy_row,
        freight_row,
        _financial_statement_row("Card and bank charges", card_bank, net_sales),
        professional_row,
        insurance_row,
        advertising_row,
        other_opex_row,
        _financial_statement_row(
            "TOTAL OPERATING EXPENSE", total_operating_expense, net_sales, emphasis=True,
            benchmark=_financial_benchmark_pct("total_operating_expense", "net_sales"),
        ),
        _financial_statement_row(
            "OPERATING INCOME", operating_income, net_sales, emphasis=True,
            benchmark=_financial_benchmark_pct("operating_income", "net_sales"),
        ),
        _financial_statement_row(
            "Interest expense", interest_expense, net_sales,
            benchmark=_financial_benchmark_pct("interest_expense", "net_sales"),
        ),
        other_income_row,
        _financial_statement_row(
            "NET INCOME", net_income, net_sales, emphasis=True,
            benchmark=_financial_benchmark_pct("net_income", "net_sales"),
        ),
    ]

    draws_row = _financial_with_detail(
        _financial_statement_row(
            "Less shareholder draws", shareholder_draws, ending_equity,
            benchmark=_financial_benchmark_pct("treasury_stock", "total_equity"),
        ),
        _financial_detail_rows(tb, acct.isin([2736, 2737]), ending_equity, flip_sign=True),
    )

    equity_rows = [
        _financial_statement_row(
            "Common stock", common_stock, ending_equity,
            benchmark=_financial_benchmark_pct("common_stock_apic", "total_equity"),
        ),
        _financial_statement_row(
            "Retained earnings", retained_earnings, ending_equity,
            benchmark=_financial_benchmark_pct("retained_earnings", "total_equity"),
        ),
        draws_row,
        _financial_statement_row("Equity before 2025 income", equity_before_income, ending_equity, emphasis=True),
        _financial_statement_row("FY 2025 net income", net_income, ending_equity),
        _financial_statement_row("ENDING EQUITY", ending_equity, ending_equity, emphasis=True, benchmark=1.0),
    ]

    metrics = [
        ("Net Sales", _format_financial_money(net_sales)),
        ("Gross Profit", f"{_format_financial_money(gross_profit)} / {_format_financial_percent(gross_profit / net_sales)}"),
        ("Net Income", f"{_format_financial_money(net_income)} / {_format_financial_percent(net_income / net_sales)}"),
        ("Total Assets", _format_financial_money(total_assets)),
        ("Debt + Equity", _format_financial_money(total_liabilities + ending_equity)),
    ]

    return {
        "balance_sheet": balance_sheet_rows,
        "income_statement": income_statement_rows,
        "equity_statement": equity_rows,
        "metrics": metrics,
        "source_path": str(path),
        "source_mtime": datetime.fromtimestamp(path.stat().st_mtime),
        "suspense": suspense,
        "note_debit_reclass": note_debit_reclass,
        "total_assets": total_assets,
        "net_sales": net_sales,
    }


def render_financial_metrics(metrics: list[tuple[str, str]]) -> None:
    html_parts = ['<div class="exec-stat-row financial-stat-row">']
    for label, value in metrics:
        html_parts.append(
            f'<div class="exec-stat"><div class="exec-stat-label">{html.escape(label)}</div>'
            f'<div class="exec-stat-value">{html.escape(value)}</div></div>'
        )
    html_parts.append("</div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def _financial_is_negative(value: float | None, threshold: float) -> bool:
    return value is not None and float(value) < threshold


def _financial_negative_class(value: float | None, threshold: float) -> str:
    return ' class="negative"' if _financial_is_negative(value, threshold) else ""


def render_financial_statement_panel(
    title: str,
    subtitle: str,
    rows: list[dict[str, object]],
    notes: list[str] | None = None,
    benchmark_label: str | None = None,
) -> None:
    colspan = 4 if benchmark_label else 3
    line_col_width = 148
    num_col_width = 96
    colgroup = (
        f'<colgroup><col style="width:{line_col_width}px">'
        + f'<col style="width:{num_col_width}px">' * (colspan - 1)
        + "</colgroup>"
    )
    summary_grid_style = (
        f' style="grid-template-columns: {line_col_width}px {" ".join([f"{num_col_width}px"] * (colspan - 1))};"'
    )
    table_min_width = line_col_width + num_col_width * (colspan - 1)
    table_rows: list[str] = []
    for row in rows:
        line = str(row.get("Line", ""))
        if not line:
            table_rows.append(f'<tr><td colspan="{colspan}">&nbsp;</td></tr>')
            continue
        value = row.get("Value")
        common_size = row.get("Common Size")
        benchmark = row.get("Benchmark") if benchmark_label else None
        row_class = ' class="is-emphasis"' if bool(row.get("Emphasis")) else ""
        value_class = _financial_negative_class(value, -0.005)
        pct_class = _financial_negative_class(common_size, -0.00005)
        benchmark_cell = (
            f'<td class="benchmark-col{" negative" if _financial_is_negative(benchmark, -0.00005) else ""}">'
            f"{html.escape(_format_financial_percent(benchmark))}</td>"
            if benchmark_label
            else ""
        )
        detail = row.get("Detail")
        if detail:
            detail_body = "".join(
                f"<tr><td>{html.escape(str(d.get('Line', '')))}</td>"
                f"<td{_financial_negative_class(d.get('Value'), -0.005)}>{html.escape(_format_financial_money(d.get('Value')))}</td>"
                f"<td{_financial_negative_class(d.get('Common Size'), -0.00005)}>{html.escape(_format_financial_percent(d.get('Common Size')))}</td>"
                "</tr>"
                for d in detail
            )
            value_mod = " negative" if _financial_is_negative(value, -0.005) else ""
            pct_mod = " negative" if _financial_is_negative(common_size, -0.00005) else ""
            benchmark_mod = " negative" if _financial_is_negative(benchmark, -0.00005) else ""
            benchmark_span = (
                f'<span class="fd-benchmark{benchmark_mod}">{html.escape(_format_financial_percent(benchmark))}</span>'
                if benchmark_label
                else ""
            )
            table_rows.append(
                f'<tr class="has-detail"{row_class}><td colspan="{colspan}">'
                f'<details class="financial-detail"><summary{summary_grid_style}>'
                f'<span class="fd-line">{html.escape(line)}</span>'
                f'<span class="fd-value{value_mod}">{html.escape(_format_financial_money(value))}</span>'
                f'<span class="fd-pct{pct_mod}">{html.escape(_format_financial_percent(common_size))}</span>'
                f"{benchmark_span}"
                "</summary>"
                f'<div class="financial-detail-body"><table><tbody>{detail_body}</tbody></table></div>'
                "</details></td></tr>"
            )
            continue
        table_rows.append(
            f"<tr{row_class}>"
            f"<td>{html.escape(line)}</td>"
            f"<td{value_class}>{html.escape(_format_financial_money(value))}</td>"
            f"<td{pct_class}>{html.escape(_format_financial_percent(common_size))}</td>"
            f"{benchmark_cell}"
            "</tr>"
        )

    note_html = ""
    if notes:
        note_html = '<div class="financial-notes">' + "<br>".join(html.escape(note) for note in notes) + "</div>"

    benchmark_header = (
        f'<th class="benchmark-col">{html.escape(benchmark_label)}</th>' if benchmark_label else ""
    )

    st.markdown(
        f"""
<div class="financial-panel">
  <div class="financial-panel-header">
    <div class="financial-panel-title">{html.escape(title)}</div>
    <div class="financial-panel-subtitle">{html.escape(subtitle)}</div>
  </div>
  <div class="financial-table-scroll">
  <table class="financial-table" style="min-width:{table_min_width}px">
    {colgroup}
    <thead>
      <tr><th>Line</th><th>Value</th><th>Common Size</th>{benchmark_header}</tr>
    </thead>
    <tbody>
      {''.join(table_rows)}
    </tbody>
  </table>
  </div>
  {note_html}
</div>
        """,
        unsafe_allow_html=True,
    )


def render_financial_tab() -> None:
    if not FINANCIAL_TRIAL_BALANCE_PATH.exists():
        render_hero(
            None,
            meta_lines=[
                "Financial tab: source file missing",
                f"Expected: {FINANCIAL_TRIAL_BALANCE_PATH}",
            ],
        )
        st.error(f"Could not find financial source file: {FINANCIAL_TRIAL_BALANCE_PATH}")
        return

    source_mtime = datetime.fromtimestamp(FINANCIAL_TRIAL_BALANCE_PATH.stat().st_mtime)
    with st.spinner("Loading 2025 financial statements..."):
        report = load_financial_statement_report(str(FINANCIAL_TRIAL_BALANCE_PATH))

    render_hero(
        source_mtime,
        meta_lines=[
            "Financial tab: 2025 statement draft",
            f"Source updated: {source_mtime:%b %d, %Y at %#I:%M %p}",
            f"Accounting packet: {FINANCIAL_ACCOUNTING_PACKET_PATH}",
        ],
    )

    control_cols = st.columns([4, 1])
    with control_cols[1]:
        if st.button("Refresh financials", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    render_financial_metrics(report["metrics"])

    balance_col, income_col, equity_col = st.columns([1.15, 1.2, 1.1], gap="medium")
    with balance_col:
        render_financial_statement_panel(
            "Balance Sheet",
            "As of 12/31/2025 | common size = % of total assets",
            report["balance_sheet"],
            benchmark_label=FINANCIAL_BENCHMARK_LABEL,
            notes=[
                f"Benchmark: {FINANCIAL_BENCHMARK_LABEL}, common-size % of F&D's own total assets.",
                "F&D is a big-box hard-surface-flooring retailer (~100x OMP's revenue) vs. OMP's "
                "wholesale distribution model -- scale and channel differ, only the mix is comparable.",
                "Blank benchmark cells = no clean line-item match in F&D's disclosures (e.g. F&D paid "
                "no dividends and repurchased no shares in FY2025, so shareholder draws has no analog).",
            ],
        )
    with income_col:
        render_financial_statement_panel(
            "Income Statement",
            "FY 2025 | common size = % of net sales",
            report["income_statement"],
            benchmark_label=FINANCIAL_BENCHMARK_LABEL,
        )
    with equity_col:
        render_financial_statement_panel(
            "Stockholders' Equity",
            "FY 2025 | common size = % of ending equity",
            report["equity_statement"],
            benchmark_label=FINANCIAL_BENCHMARK_LABEL,
            notes=[
                "Source: TrialBalance_12312025_v2.xlsx.",
                "Debit-balance note payable accounts shown as asset reclasses.",
                f"Suspense/unsupported balance included in other operating expense: {_format_financial_money(report['suspense'])}.",
            ],
        )


def _options_for(df: pd.DataFrame, column: str, limit: int | None = 800) -> list[str]:
    if column not in df.columns:
        return []
    raw = df[column]
    cleaned = raw.fillna("").astype(str).str.strip()
    has_blank = raw.isna().any() or cleaned.eq("").any()
    values = (
        cleaned[cleaned.ne("")]
        .drop_duplicates()
        .sort_values()
        .tolist()
    )
    options = values if limit is None else values[:limit]
    return ([BLANK_FILTER_LABEL] if has_blank else []) + options


def _item_description_options(df: pd.DataFrame, limit: int | None = None) -> tuple[list[str], dict[str, str]]:
    if "Item Number" not in df.columns:
        return [], {}

    description_series = df["Description"] if "Description" in df.columns else pd.Series("", index=df.index)
    item_numbers = df["Item Number"].fillna("").astype(str).str.strip()
    has_blank_item = df["Item Number"].isna().any() or item_numbers.eq("").any()
    item_df = (
        pd.DataFrame(
            {
                "Item Number": item_numbers,
                "Description": description_series.astype(str).str.strip(),
            }
        )
        .replace("", pd.NA)
        .dropna(subset=["Item Number"])
        .drop_duplicates(subset=["Item Number"])
        .sort_values("Item Number")
    )

    labels: list[str] = []
    lookup: dict[str, str] = {}
    if has_blank_item:
        labels.append(BLANK_FILTER_LABEL)
        lookup[BLANK_FILTER_LABEL] = BLANK_FILTER_LABEL
    for row in item_df.itertuples(index=False):
        item_number = row[0]
        description = row[1]
        label = f"{item_number} - {description}" if pd.notna(description) and description else item_number
        labels.append(label)
        lookup[label] = item_number
        if limit is not None and len(labels) >= limit:
            break
    return labels, lookup


def _vendor_options(df: pd.DataFrame, limit: int = 1200) -> tuple[list[str], dict[str, str]]:
    if "Vendor Number" not in df.columns:
        return [], {}

    vendor_name_series = df["Vendor Name"] if "Vendor Name" in df.columns else pd.Series("", index=df.index)
    vendor_numbers = df["Vendor Number"].fillna("").astype(str).str.strip()
    has_blank_vendor = df["Vendor Number"].isna().any() or vendor_numbers.eq("").any()
    vendor_df = (
        pd.DataFrame(
            {
                "Vendor Number": vendor_numbers,
                "Vendor Name": vendor_name_series.fillna("").astype(str).str.strip(),
            }
        )
        .replace("", pd.NA)
        .dropna(subset=["Vendor Number"])
        .drop_duplicates(subset=["Vendor Number"])
        .sort_values("Vendor Number")
    )

    labels: list[str] = []
    lookup: dict[str, str] = {}
    if has_blank_vendor:
        labels.append(BLANK_FILTER_LABEL)
        lookup[BLANK_FILTER_LABEL] = BLANK_FILTER_LABEL
    for row in vendor_df.itertuples(index=False):
        vendor_number = row[0]
        vendor_name = row[1]
        label = f"{vendor_number} / {vendor_name}" if pd.notna(vendor_name) and vendor_name else vendor_number
        labels.append(label)
        lookup[label] = vendor_number
        if len(labels) >= limit:
            break
    return labels, lookup


def _clear_filter_keys(*keys: str) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def _clean_multiselect_state(key: str, options: list[str]) -> None:
    if key not in st.session_state:
        return
    current = st.session_state.get(key, [])
    if isinstance(current, str):
        current_values = [current]
    else:
        current_values = list(current or [])
    allowed = set(options)
    cleaned = [value for value in current_values if value in allowed]
    if cleaned != current_values:
        st.session_state[key] = cleaned


def _filter_by_selected(df: pd.DataFrame, column: str, selected: list[str]) -> pd.DataFrame:
    if selected and column in df.columns:
        selected_values = {str(value).strip() for value in selected if str(value).strip()}
        cleaned = df[column].fillna("").astype(str).str.strip()
        mask = cleaned.isin(_nonblank_filter_values(tuple(selected_values)))
        if BLANK_FILTER_LABEL in selected_values:
            mask = mask | cleaned.eq("")
        return df[mask]
    return df


def _column_format_groups() -> dict[str, set[str]]:
    return {
        "text": {
        "Branch Location",
        "Collection",
        "Description",
        "Division",
        "Item Number",
        "Product Family",
        "Sales UOM",
        "Vendor Name",
        "Vendor Number",
        ITEM_DESCRIPTION_COLUMN,
        },
        "currency": {
        "Preferred Price",
        "VIP Price",
        "Sales Amount",
        "Sales $ - Selected Range",
        "Customer Filter Sales Amount",
        "Inventory Value",
        },
        "cost": {
        "Landed Cost - Most Recent PO",
        "Landed Cost - Avg Inventory",
        "Landed Cost",
        "Average Inventory Cost",
        },
        "quantity_two_decimal": {
        "Current Inventory On Hand",
        "Current Inventory Available",
        "Backorders",
        "Qty on PO",
        "On Hand Qty",
        "Receipt Qty",
        "Units Sold",
        "Units Sold - Selected Range",
        "Customer Filter Units Sold",
        },
        "whole_number": {
        "Customer Filter Item Count",
        "Customer Filter Orders Count",
        "Customer Rank",
        "Collection Rank",
        "Item Count",
        "Item Rank",
        "Orders - Selected Range",
        "Orders Count",
        "Receipt Count",
        },
    }


def _column_config(df: pd.DataFrame, pinned_columns: set[str] | None = None) -> dict:
    config: dict = {}
    pinned_columns = pinned_columns or set()
    groups = _column_format_groups()

    for col in df.columns:
        pinned = col in pinned_columns
        label = _display_column_label(col)
        if "Date" in col or col == "Month":
            config[col] = st.column_config.DateColumn(label, format="MM/DD/YYYY", pinned=pinned)
        elif col in groups["text"]:
            config[col] = st.column_config.TextColumn(label, pinned=pinned)
        elif col in groups["currency"] or col in groups["cost"]:
            config[col] = st.column_config.NumberColumn(label, format="$%,.2f", pinned=pinned)
        elif col in groups["quantity_two_decimal"]:
            config[col] = st.column_config.NumberColumn(label, format="%,.2f", pinned=pinned)
        elif col in groups["whole_number"]:
            config[col] = st.column_config.NumberColumn(label, format="%,d", pinned=pinned)
        elif col in {"Gross Margin % - Most Recent PO", "Gross Margin % - Avg Inventory"}:
            config[col] = st.column_config.NumberColumn(label, format="%.2f%%", pinned=pinned)
        elif pd.api.types.is_numeric_dtype(df[col]):
            config[col] = st.column_config.NumberColumn(label, format="%,.0f", pinned=pinned)
        elif pinned:
            config[col] = st.column_config.Column(label, pinned=True)
    return config


def _display_column_label(column: str) -> str:
    labels = {
        "Current Inventory On Hand": "On Hand",
        "Current Inventory Available": "Available",
        "Customer Filter Item Count": "Filter Items",
        "Customer Filter Orders Count": "Filter Orders",
        "Customer Filter Sales Amount": "Filter Sales $",
        "Customer Filter Units Sold": "Filter Units",
        "Landed Cost - Most Recent PO": "Recent PO Cost",
        "Landed Cost - Avg Inventory": "Avg Inv Cost",
        "Gross Margin % - Most Recent PO": "GM % Recent PO",
        "Gross Margin % - Avg Inventory": "GM % Avg Inv",
        "Average Inventory Cost": "Avg Inv Cost",
        "Inventory Value": "Inv Value",
        "Most Recent Received Date": "Last Receipt",
        "Branch Location": "Branch",
        "Orders Count": "Orders",
        "Receipt Count": "Receipts",
        "Receipt Qty": "Receipt Qty",
        "Sales Amount": "Sales $",
        "Vendor Number": "Vendor #",
        "Vendor Name": "Vendor",
    }
    return labels.get(column, column)


def _table_column_type(df: pd.DataFrame, column: str) -> str:
    if column == "Month" or "Date" in column:
        return "date"
    if pd.api.types.is_numeric_dtype(df[column]):
        return "number"
    return "text"


def _smart_filter_prefix(dataset: str) -> str:
    return f"exec_smart_filter_{_safe_key(dataset)}_"


def _clear_smart_filter_state(dataset: str) -> None:
    prefix = _smart_filter_prefix(dataset)
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefix):
            st.session_state.pop(key, None)


def _clear_graph_filter_state() -> None:
    for key in list(st.session_state.keys()):
        if str(key).startswith("exec_graph_filter_"):
            st.session_state.pop(key, None)


def _smart_filter_key(dataset: str, column: str, suffix: str = "") -> str:
    suffix_part = f"_{suffix}" if suffix else ""
    return f"{_smart_filter_prefix(dataset)}{_safe_key(column)}{suffix_part}"


def _numeric_step(column: str) -> float:
    groups = _column_format_groups()
    if column in groups["quantity_two_decimal"] or column in groups["currency"] or column in groups["cost"]:
        return 0.01
    if column in {"Gross Margin % - Most Recent PO", "Gross Margin % - Avg Inventory"}:
        return 0.1
    return 1.0


def _reset_number_range_if_stale(min_key: str, max_key: str, min_default: float, max_default: float) -> None:
    if min_key not in st.session_state and max_key not in st.session_state:
        return
    current_min = pd.to_numeric(st.session_state.get(min_key, min_default), errors="coerce")
    current_max = pd.to_numeric(st.session_state.get(max_key, max_default), errors="coerce")
    if pd.isna(current_min) or pd.isna(current_max):
        st.session_state[min_key] = min_default
        st.session_state[max_key] = max_default
        return
    low = min(float(current_min), float(current_max))
    high = max(float(current_min), float(current_max))
    if high < min_default or low > max_default:
        st.session_state[min_key] = min_default
        st.session_state[max_key] = max_default
        return
    st.session_state[min_key] = max(low, min_default)
    st.session_state[max_key] = min(high, max_default)


def _reset_date_range_if_stale(start_key: str, end_key: str, min_default: date, max_default: date) -> None:
    if start_key not in st.session_state and end_key not in st.session_state:
        return
    current_start = pd.to_datetime(st.session_state.get(start_key, min_default), errors="coerce")
    current_end = pd.to_datetime(st.session_state.get(end_key, max_default), errors="coerce")
    if pd.isna(current_start) or pd.isna(current_end):
        st.session_state[start_key] = min_default
        st.session_state[end_key] = max_default
        return
    low = min(current_start.date(), current_end.date())
    high = max(current_start.date(), current_end.date())
    if high < min_default or low > max_default:
        st.session_state[start_key] = min_default
        st.session_state[end_key] = max_default
        return
    st.session_state[start_key] = max(low, min_default)
    st.session_state[end_key] = min(high, max_default)


def _compact_filter_values(values: list[str] | tuple[str, ...], limit: int = 4) -> str:
    cleaned = [str(value) for value in values if str(value).strip()]
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    return f"{', '.join(cleaned[:limit])} +{len(cleaned) - limit} more"


def _ordered_smart_text_columns(text_columns: list[str]) -> list[str]:
    preferred = [
        "Division",
        "Product Family",
        "Collection",
        "Vendor Number",
        "Item Number",
        "Description",
        "Vendor Name",
        "Branch Location",
    ]
    ordered = [column for column in preferred if column in text_columns]
    ordered_set = set(ordered)
    return ordered + [column for column in text_columns if column not in ordered_set]


def _apply_smart_column_filters(
    df: pd.DataFrame,
    dataset: str,
    lookup_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    filtered = df.copy()
    text_columns = _ordered_smart_text_columns([col for col in df.columns if _table_column_type(df, col) == "text"])
    number_columns = [col for col in df.columns if _table_column_type(df, col) == "number"]
    date_columns = [col for col in df.columns if _table_column_type(df, col) == "date"]
    active_filter_labels: list[str] = []

    with st.expander("Smart Column Filters", expanded=True):
        intro_col, reset_col = st.columns([4, 1])
        with intro_col:
            st.caption("These Product Data filters control the table and feed the Graph Builder. They do not reset date range, dataset, or Graph Builder-only filters.")
        with reset_col:
            if st.button("Clear Product Data filters", use_container_width=True, key=f"{_smart_filter_prefix(dataset)}clear"):
                _clear_smart_filter_state(dataset)
                st.rerun()

        if filtered.empty:
            st.warning("No rows match the current filter combination. Use **Clear Product Data filters** above to reset.")

        tab_labels = []
        if text_columns:
            tab_labels.append("Text / Categories")
        if number_columns:
            tab_labels.append("Numbers")
        if date_columns:
            tab_labels.append("Dates")

        if not tab_labels:
            st.info("No filterable columns are available.")
            return filtered

        tabs = st.tabs(tab_labels)
        tab_index = 0

        if text_columns:
            with tabs[tab_index]:
                cols = st.columns(3)
                smart_option_source = (
                    lookup_df.copy() if lookup_df is not None and not lookup_df.empty else filtered.copy()
                )
                for idx, column in enumerate(text_columns):
                    option_source = (
                        smart_option_source if column in smart_option_source.columns else filtered
                    )
                    option_limit = None if column in {"Item Number", "Description", ITEM_DESCRIPTION_COLUMN} else 5000
                    options = _options_for(option_source, column, limit=option_limit)
                    key = _smart_filter_key(dataset, column, "values")
                    _clean_multiselect_state(key, options)
                    with cols[idx % 3]:
                        selected = st.multiselect(
                            _display_column_label(column),
                            options,
                            key=key,
                            placeholder="All values",
                        )
                    filtered = _filter_by_selected(filtered, column, selected)
                    smart_option_source = _filter_by_selected(smart_option_source, column, selected)
                    if selected:
                        active_filter_labels.append(f"{_display_column_label(column)}: {_compact_filter_values(selected)}")
            tab_index += 1

        if number_columns:
            with tabs[tab_index]:
                for column in number_columns:
                    numeric = pd.to_numeric(filtered[column], errors="coerce").dropna()
                    if numeric.empty:
                        continue
                    min_default = float(numeric.min())
                    max_default = float(numeric.max())
                    label = _display_column_label(column)
                    min_key = _smart_filter_key(dataset, column, "min")
                    max_key = _smart_filter_key(dataset, column, "max")
                    enabled_key = _smart_filter_key(dataset, column, "range_enabled")
                    _reset_number_range_if_stale(min_key, max_key, min_default, max_default)
                    range_enabled = bool(st.session_state.get(enabled_key, False))
                    if not range_enabled:
                        st.session_state[min_key] = min_default
                        st.session_state[max_key] = max_default
                    row_cols = st.columns([0.55, 1.1, 1, 1])
                    with row_cols[0]:
                        range_enabled = st.checkbox(
                            "Apply",
                            key=enabled_key,
                            help=f"Enable the {_display_column_label(column)} numeric range filter.",
                        )
                    with row_cols[1]:
                        st.markdown(f"**{label}**")
                    with row_cols[2]:
                        min_value = st.number_input(
                            f"{label} min",
                            value=min_default,
                            step=_numeric_step(column),
                            key=min_key,
                            label_visibility="collapsed",
                            disabled=not range_enabled,
                        )
                    with row_cols[3]:
                        max_value = st.number_input(
                            f"{label} max",
                            value=max_default,
                            step=_numeric_step(column),
                            key=max_key,
                            label_visibility="collapsed",
                            disabled=not range_enabled,
                        )
                    if range_enabled:
                        low = min(float(min_value), float(max_value))
                        high = max(float(min_value), float(max_value))
                        filtered = filtered[pd.to_numeric(filtered[column], errors="coerce").between(low, high, inclusive="both")]
                        active_filter_labels.append(
                            f"{label}: {_format_number(low, 2)} to {_format_number(high, 2)}"
                        )
            tab_index += 1

        if date_columns:
            with tabs[tab_index]:
                for column in date_columns:
                    dates = pd.to_datetime(filtered[column], errors="coerce").dropna()
                    if dates.empty:
                        continue
                    min_default = dates.min().date()
                    max_default = dates.max().date()
                    label = _display_column_label(column)
                    start_key = _smart_filter_key(dataset, column, "start")
                    end_key = _smart_filter_key(dataset, column, "end")
                    enabled_key = _smart_filter_key(dataset, column, "date_enabled")
                    _reset_date_range_if_stale(start_key, end_key, min_default, max_default)
                    date_enabled = bool(st.session_state.get(enabled_key, False))
                    if not date_enabled:
                        st.session_state[start_key] = min_default
                        st.session_state[end_key] = max_default
                    row_cols = st.columns([0.55, 1.1, 1, 1])
                    with row_cols[0]:
                        date_enabled = st.checkbox(
                            "Apply",
                            key=enabled_key,
                            help=f"Enable the {_display_column_label(column)} date range filter.",
                        )
                    with row_cols[1]:
                        st.markdown(f"**{label}**")
                    with row_cols[2]:
                        start_value = st.date_input(
                            f"{label} start",
                            value=min_default,
                            key=start_key,
                            label_visibility="collapsed",
                            disabled=not date_enabled,
                        )
                    with row_cols[3]:
                        end_value = st.date_input(
                            f"{label} end",
                            value=max_default,
                            key=end_key,
                            label_visibility="collapsed",
                            disabled=not date_enabled,
                        )
                    if date_enabled:
                        start_date = min(start_value, end_value)
                        end_date = max(start_value, end_value)
                        parsed = pd.to_datetime(filtered[column], errors="coerce").dt.date
                        filtered = filtered[parsed.between(start_date, end_date, inclusive="both")]
                        active_filter_labels.append(f"{label}: {start_date:%m/%d/%Y} to {end_date:%m/%d/%Y}")

        if active_filter_labels:
            preview = "; ".join(active_filter_labels[:6])
            if len(active_filter_labels) > 6:
                preview += f"; +{len(active_filter_labels) - 6} more"
            st.warning(f"Active Product Data filters are narrowing the rows used by the Graph Builder: {preview}")

        if filtered.empty:
            st.warning("No rows match the current filter combination. Use **Clear Product Data filters** above to reset.")

    return filtered


def _format_table_value(value, column: str) -> str:
    if pd.isna(value):
        return ""

    groups = _column_format_groups()
    if column == "Month" or "Date" in column:
        parsed = pd.to_datetime(value, errors="coerce")
        return "" if pd.isna(parsed) else parsed.strftime("%m/%d/%Y")

    if column in groups["currency"] or column in groups["cost"]:
        return f"${float(value):,.2f}"
    if column in groups["quantity_two_decimal"]:
        return f"{float(value):,.2f}"
    if column in groups["whole_number"]:
        return f"{float(value):,.0f}"
    if column in {"Gross Margin % - Most Recent PO", "Gross Margin % - Avg Inventory"}:
        return f"{float(value):,.2f}%"
    if pd.api.types.is_numeric_dtype(type(value)) or isinstance(value, (int, float)):
        return f"{float(value):,.0f}"
    return str(value)


def _sort_value(value, column_type: str) -> str:
    if pd.isna(value):
        return ""
    if column_type == "date":
        parsed = pd.to_datetime(value, errors="coerce")
        return "" if pd.isna(parsed) else parsed.isoformat()
    if column_type == "number":
        try:
            return str(float(value))
        except Exception:
            return ""
    return str(value).lower()


def _is_negative_number(value, column_type: str) -> bool:
    if column_type != "number" or pd.isna(value):
        return False
    try:
        return float(value) < 0
    except Exception:
        return False


def _column_width(column: str) -> int:
    label = _display_column_label(column)
    if column == "Item Number":
        return 180
    if column in {"Description", ITEM_DESCRIPTION_COLUMN}:
        return 320
    if column == "Collection":
        return 220
    if column == "Product Family":
        return 240
    if column == "Vendor Name":
        return 260
    if column == "Vendor Number":
        return 150
    if column in {"Branch Location", "Division", "Sales UOM"}:
        return 130
    if "Gross Margin" in column:
        return 210
    if "$" in column or "Cost" in column or "Inventory" in column:
        return 185
    return max(130, min(240, len(label) * 9 + 44))


def _render_spreadsheet_table(
    df: pd.DataFrame,
    dataset: str,
    pinned_columns: set[str] | None = None,
) -> None:
    if df.empty:
        if dataset in {"Sales Over Time", "Sales Summary"}:
            st.info("No sales rows match the current filters and date range. Try widening the date range or clearing a collection/item filter.")
        else:
            st.info("No rows match the current filters.")
        return

    pinned_columns = pinned_columns or set()
    columns = list(df.columns)
    widths = {column: _column_width(column) for column in columns}
    left_offsets: dict[str, int] = {}
    running_left = 0
    for column in columns:
        if column in pinned_columns:
            left_offsets[column] = running_left
            running_left += widths[column]

    table_min_width = sum(widths.values())
    colgroup = "".join(f'<col style="width:{widths[column]}px;">' for column in columns)

    header_cells = []
    for idx, column in enumerate(columns):
        column_type = _table_column_type(df, column)
        sticky_style = ""
        sticky_class = ""
        if column in left_offsets:
            sticky_style = f"left:{left_offsets[column]}px;"
            sticky_class = " sticky-col"
        display_label = _display_column_label(column)
        escaped_column = html.escape(column)
        escaped_label = html.escape(display_label)
        header_cells.append(
            f'<th class="sortable{sticky_class}" data-col="{idx}" data-type="{column_type}" '
            f'style="{sticky_style}" title="Click to sort {escaped_column}">'
            f'<span class="header-label">{escaped_label}</span><span class="sort-indicator"></span></th>'
        )

    body_rows = []
    for row in df.itertuples(index=False, name=None):
        cells = []
        for idx, value in enumerate(row):
            column = columns[idx]
            column_type = _table_column_type(df, column)
            display = _format_table_value(value, column)
            raw_sort = _sort_value(value, column_type)
            raw_filter = f"{display} {value}".lower() if not pd.isna(value) else display.lower()
            class_names = []
            if column_type == "number":
                class_names.append("numeric")
            if _is_negative_number(value, column_type):
                class_names.append("negative")
            sticky_style = ""
            sticky_class = ""
            if column in left_offsets:
                sticky_style = f"left:{left_offsets[column]}px;"
                sticky_class = " sticky-col"
            class_attr = " ".join(class_names) + sticky_class
            escaped_display = html.escape(display)
            cells.append(
                f'<td class="{class_attr}" style="{sticky_style}" '
                f'data-sort="{html.escape(raw_sort)}" data-filter="{html.escape(raw_filter)}" '
                f'title="{escaped_display}">{escaped_display}</td>'
            )
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    table_id = f"exec-table-{_safe_key(dataset)}"
    table_html = f"""
<style>
  body {{
    margin: 0;
    font-family: Manrope, Arial, sans-serif;
    color: #142033;
    background: #ffffff;
  }}

  .exec-table-toolbar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 8px 10px;
    border: 1px solid #cbd5e1;
    border-bottom: none;
    background: #f8fafc;
    font-size: 13px;
    font-weight: 700;
  }}

  .exec-table-toolbar button {{
    border: 1px solid #cbd5e1;
    background: #ffffff;
    color: #1e3a5f;
    border-radius: 6px;
    padding: 6px 10px;
    font-weight: 800;
    cursor: pointer;
  }}

  .exec-table-wrap {{
    height: 600px;
    overflow: auto;
    border: 1px solid #cbd5e1;
    background: #ffffff;
  }}

  table.exec-grid {{
    border-collapse: separate;
    border-spacing: 0;
    table-layout: fixed;
    min-width: {table_min_width}px;
    width: {table_min_width}px;
    font-size: 13px;
  }}

  .exec-grid th,
  .exec-grid td {{
    border-right: 1px solid #dbe3ee;
    border-bottom: 1px solid #e2e8f0;
    padding: 7px 9px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    box-sizing: border-box;
  }}

  .exec-grid thead tr.header-row th {{
    position: sticky;
    top: 0;
    z-index: 5;
    height: 48px;
    background: #e8edf5;
    color: #1e3a5f;
    font-size: 10.5px;
    line-height: 1.15;
    font-weight: 900;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    cursor: pointer;
    user-select: none;
    white-space: normal;
    text-overflow: clip;
    vertical-align: middle;
  }}

  .exec-grid thead tr.filter-row th {{
    position: sticky;
    top: 48px;
    z-index: 5;
    height: 38px;
    background: #f8fafc;
  }}

  .exec-grid .header-label {{
    display: inline-block;
    max-width: calc(100% - 28px);
    white-space: normal;
    overflow-wrap: anywhere;
  }}

  .exec-grid input {{
    width: 100%;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 5px 6px;
    font-size: 12px;
    box-sizing: border-box;
    outline: none;
  }}

  .exec-grid input:focus {{
    border-color: #1e3a5f;
    box-shadow: 0 0 0 1px #1e3a5f;
  }}

  .exec-grid td {{
    background: #ffffff;
  }}

  .exec-grid tbody tr:hover td {{
    background: #f1f5fb;
  }}

  .exec-grid .numeric {{
    text-align: right;
    font-variant-numeric: tabular-nums;
  }}

  .exec-grid td.negative {{
    color: #c0392b;
    font-weight: 800;
  }}

  .exec-grid .sticky-col {{
    position: sticky;
    z-index: 4;
    box-shadow: 1px 0 0 #cbd5e1;
  }}

  .exec-grid thead tr.header-row th.sticky-col {{
    z-index: 8;
  }}

  .exec-grid tbody .sticky-col {{
    background: #ffffff;
  }}

  .exec-grid tbody tr:hover .sticky-col {{
    background: #f1f5fb;
  }}

  .sort-indicator {{
    float: right;
    color: #ef4444;
    font-size: 10px;
    margin-left: 4px;
  }}
</style>

<div class="exec-table-toolbar">
  <div>{len(df):,} rows</div>
  <div>Click any header to sort</div>
</div>
<div class="exec-table-wrap">
  <table id="{table_id}" class="exec-grid">
    <colgroup>{colgroup}</colgroup>
    <thead>
      <tr class="header-row">{''.join(header_cells)}</tr>
    </thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
</div>

<script>
(function() {{
  const table = document.getElementById("{table_id}");
  const tbody = table.querySelector("tbody");
  const rows = Array.from(tbody.querySelectorAll("tr"));
  const headers = Array.from(table.querySelectorAll("thead tr.header-row th"));
  let sortColumn = null;
  let sortDirection = 1;

  function sortRows(col, type) {{
    if (sortColumn === col) {{
      sortDirection *= -1;
    }} else {{
      sortColumn = col;
      sortDirection = -1;
    }}

    headers.forEach(header => header.querySelector(".sort-indicator").textContent = "");
    headers[col].querySelector(".sort-indicator").textContent = sortDirection === 1 ? "ASC" : "DESC";

    const sortedRows = rows.slice().sort((a, b) => {{
      let av = a.cells[col].dataset.sort || "";
      let bv = b.cells[col].dataset.sort || "";
      if (type === "number") {{
        av = av === "" ? Number.NEGATIVE_INFINITY : Number(av);
        bv = bv === "" ? Number.NEGATIVE_INFINITY : Number(bv);
      }} else if (type === "date") {{
        av = av === "" ? 0 : Date.parse(av);
        bv = bv === "" ? 0 : Date.parse(bv);
      }}
      if (av < bv) return -1 * sortDirection;
      if (av > bv) return 1 * sortDirection;
      return 0;
    }});

    sortedRows.forEach(row => tbody.appendChild(row));
  }}

  headers.forEach(header => {{
    header.addEventListener("click", () => sortRows(Number(header.dataset.col), header.dataset.type));
  }});
}})();
</script>
"""

    components.html(table_html, height=670, scrolling=False)


def _display_table_dataframe(df: pd.DataFrame, dataset: str) -> pd.DataFrame:
    if dataset != "Landed Cost History":
        return df

    leading_columns = [column for column in ["Item Number", "Description"] if column in df.columns]
    if not leading_columns:
        return df

    remaining_columns = [column for column in df.columns if column not in leading_columns]
    return df[leading_columns + remaining_columns]


def render_data_card(df: pd.DataFrame, dataset: str, lookup_df: pd.DataFrame | None = None) -> pd.DataFrame:
    with st.expander("Product Data", expanded=True):
        filtered = _apply_smart_column_filters(df, dataset, lookup_df)
        render_summary_stats(filtered, dataset)
        display_df = _display_table_dataframe(filtered, dataset)
        # Note: Streamlit's grid renders pinned columns in a deliberately dimmed "faded" text
        # style (a hardcoded behavior in the underlying component, not something column_config
        # can override) — so columns aren't pinned here, since a hard-to-read "always visible"
        # column isn't actually more useful than a normal-contrast one you scroll back to.
        st.caption(f"{len(display_df):,} rows — click any column header to sort.")
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=600,
            column_config=_column_config(display_df, pinned_columns=set()),
        )
        csv = filtered.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download filtered rows",
            data=csv,
            file_name=f"executive_dashboard_{dataset.lower().replace(' ', '_')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    return filtered


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in df.columns if pd.api.types.is_numeric_dtype(df[col])]


def _dimension_columns(df: pd.DataFrame) -> list[str]:
    dims = []
    for col in df.columns:
        if col not in _numeric_columns(df):
            dims.append(col)
    return dims


def _pick_existing(columns: list[str], candidates: list[str], fallback: str | None = None) -> str:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    if fallback and fallback in columns:
        return fallback
    return columns[0] if columns else ""


def _preset_defaults(dataset: str, preset: str, columns: list[str]) -> tuple[str, str, str, str, str]:
    if preset == "Sales by Division":
        return "Bar", "Division", _pick_existing(columns, ["Sales Amount", "Sales $ - Selected Range"]), "Collection", "Sum"
    if preset == "Sales by Collection":
        return "Bar", "Collection", _pick_existing(columns, ["Sales Amount", "Sales $ - Selected Range"]), "Division", "Sum"
    if preset == "Sales by Branch Location":
        return "Bar", "Branch Location", _pick_existing(columns, ["Sales Amount", "Sales $ - Selected Range"]), "Division", "Sum"
    if preset == "Landed Cost Changes by Vendor Over Time":
        return "Line", "Month", _pick_existing(columns, ["Landed Cost", "Landed Cost - Most Recent PO"]), "Vendor Name", "Average"
    if preset == "Average Landed Cost of Inventory by Item":
        return "Bar", _pick_existing(columns, [ITEM_DESCRIPTION_COLUMN, "Item Number"]), _pick_existing(columns, ["Average Inventory Cost", "Landed Cost - Avg Inventory"]), "Collection", "Average"
    if dataset == "Sales Over Time":
        return "Line", "Month", _pick_existing(columns, ["Sales Amount"]), "Collection", "Sum"
    if dataset == "Sales Summary":
        return "Bar", "Collection", _pick_existing(columns, ["Sales Amount"]), "Division", "Sum"
    if dataset == "Landed Cost History":
        return "Line", "Month", _pick_existing(columns, ["Landed Cost"]), "Vendor Name", "Average"
    if dataset == "Average Inventory Cost":
        return "Bar", _pick_existing(columns, [ITEM_DESCRIPTION_COLUMN, "Item Number"]), _pick_existing(columns, ["Average Inventory Cost"]), "Collection", "Average"
    return "Bar", "Collection", _pick_existing(columns, ["Sales $ - Selected Range", "Current Inventory Available"]), "Division", "Sum"


def _aggregate(df: pd.DataFrame, x_axis: str, y_metric: str, color_by: str, aggregation: str) -> pd.DataFrame:
    group_cols = [x_axis]
    if not _is_no_breakout(color_by) and color_by in df.columns and color_by != x_axis:
        group_cols.append(color_by)

    working = df[group_cols + ([y_metric] if y_metric in df.columns else [])].copy()
    working = working.dropna(subset=[x_axis])
    if y_metric in working.columns:
        working[y_metric] = pd.to_numeric(working[y_metric], errors="coerce").fillna(0)

    if aggregation == "Count":
        grouped = working.groupby(group_cols, dropna=False).size().reset_index(name="Value")
    else:
        agg_map: dict[str, Callable] = {
            "Sum": "sum",
            "Average": "mean",
            "Median": "median",
            "Minimum": "min",
            "Maximum": "max",
        }
        grouped = (
            working.groupby(group_cols, dropna=False)[y_metric]
            .agg(agg_map.get(aggregation, "sum"))
            .reset_index(name="Value")
        )
    if pd.api.types.is_datetime64_any_dtype(grouped[x_axis]):
        grouped = grouped.sort_values(group_cols)
    else:
        grouped = grouped.sort_values("Value", ascending=False)
    return grouped


def _safe_key(value: str) -> str:
    return (
        value.lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace("$", "dollars")
        .replace("%", "pct")
        .replace("(", "")
        .replace(")", "")
    )


def _toggle_lazy_analysis_card(state_key: str) -> None:
    st.session_state[state_key] = not bool(st.session_state.get(state_key, False))


def render_lazy_analysis_card(title: str, key: str, expanded: bool = False) -> bool:
    """Render a lightweight expandable header and return True only when open."""
    state_key = f"{key}_expanded"
    if state_key not in st.session_state:
        st.session_state[state_key] = bool(expanded or st.session_state.get(key, False))

    is_open = bool(st.session_state[state_key])
    icon = ":material/keyboard_arrow_down:" if is_open else ":material/keyboard_arrow_right:"
    with st.container(key=f"exec_lazy_header_{_safe_key(key)}"):
        st.button(
            title.upper(),
            key=f"{key}_toggle",
            icon=icon,
            icon_position="left",
            use_container_width=True,
            on_click=_toggle_lazy_analysis_card,
            args=(state_key,),
        )
    return is_open


def _normal_tuple(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not values:
        return ()
    cleaned = sorted({str(value).strip() for value in values if str(value).strip()})
    return tuple(cleaned)


def _item_numbers_from_labels(labels: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not labels:
        return ()
    items = []
    for label in labels:
        item = str(label).split(" - ", 1)[0].strip()
        if item:
            items.append(item)
    return _normal_tuple(items)


def _vendor_numbers_from_labels(labels: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not labels:
        return ()
    vendors = []
    for label in labels:
        vendor = str(label).split(" / ", 1)[0].strip()
        if vendor:
            vendors.append(vendor)
    return _normal_tuple(vendors)


def _combined_state_values(*keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        current = st.session_state.get(key, [])
        if isinstance(current, str):
            values.append(current)
        else:
            values.extend(list(current or []))
    return _normal_tuple(values)


def _smart_state_values(dataset: str, column: str) -> tuple[str, ...]:
    return _combined_state_values(_smart_filter_key(dataset, column, "values"))


def _intersect_nonempty(*groups: tuple[str, ...]) -> tuple[str, ...]:
    populated = [set(group) for group in groups if group]
    if not populated:
        return ()
    values = populated[0]
    for group in populated[1:]:
        values = values.intersection(group)
    if not values:
        return (NO_MATCH_FILTER_VALUE,)
    return tuple(sorted(values))


def _lookup_item_numbers_by_description(lookup_df: pd.DataFrame | None, descriptions: tuple[str, ...]) -> tuple[str, ...]:
    if lookup_df is None or lookup_df.empty or not descriptions:
        return ()
    if "Description" not in lookup_df.columns or "Item Number" not in lookup_df.columns:
        return ()
    selected_descriptions = set(_nonblank_filter_values(descriptions))
    description_values = lookup_df["Description"].fillna("").astype(str).str.strip()
    mask = description_values.isin(selected_descriptions)
    if _blank_filter_selected(descriptions):
        mask = mask | description_values.eq("")
    matches = lookup_df[mask]
    return _normal_tuple(matches["Item Number"].astype(str).str.strip().tolist())


def _lookup_vendor_numbers_by_name(lookup_df: pd.DataFrame | None, vendor_names: tuple[str, ...]) -> tuple[str, ...]:
    if lookup_df is None or lookup_df.empty or not vendor_names:
        return ()
    if "Vendor Name" not in lookup_df.columns or "Vendor Number" not in lookup_df.columns:
        return ()
    selected_vendor_names = set(_nonblank_filter_values(vendor_names))
    vendor_name_values = lookup_df["Vendor Name"].fillna("").astype(str).str.strip()
    mask = vendor_name_values.isin(selected_vendor_names)
    if _blank_filter_selected(vendor_names):
        mask = mask | vendor_name_values.eq("")
    matches = lookup_df[mask]
    return _normal_tuple(matches["Vendor Number"].astype(str).str.strip().tolist())


def _collect_query_filters(
    dataset: str,
    lookup_df: pd.DataFrame | None = None,
    include_branch_filters: bool = True,
) -> dict[str, tuple[str, ...]]:
    smart_items = _normal_tuple(
        list(_smart_state_values(dataset, "Item Number"))
        + list(_item_numbers_from_labels(_smart_state_values(dataset, ITEM_DESCRIPTION_COLUMN)))
        + list(_lookup_item_numbers_by_description(lookup_df, _smart_state_values(dataset, "Description")))
    )

    smart_vendors = _normal_tuple(
        list(_smart_state_values(dataset, "Vendor Number"))
        + list(_lookup_vendor_numbers_by_name(lookup_df, _smart_state_values(dataset, "Vendor Name")))
    )

    return {
        "item_numbers": smart_items,
        "divisions": _smart_state_values(dataset, "Division"),
        "product_families": _smart_state_values(dataset, "Product Family"),
        "collections": _smart_state_values(dataset, "Collection"),
        "branch_locations": _smart_state_values(dataset, "Branch Location") if include_branch_filters else (),
        "vendor_numbers": smart_vendors,
    }


def _customer_item_sales_column_config() -> dict:
    return {
        "Collection Rank": st.column_config.NumberColumn("Collection Rank", format="%,d"),
        "Customer Rank": st.column_config.NumberColumn("Customer Rank", format="%,d"),
        "Item Count": st.column_config.NumberColumn("Item Count", format="%,d"),
        "Item Rank": st.column_config.NumberColumn("Item Rank", format="%,d"),
        "Sales Amount": st.column_config.NumberColumn("Sales Amount", format="$%,.2f"),
        "Units Sold": st.column_config.NumberColumn("Units Sold", format="%,.2f"),
        "Orders Count": st.column_config.NumberColumn("Orders Count", format="%,d"),
        "Customer Filter Sales Amount": st.column_config.NumberColumn("Customer Filter Sales Amount", format="$%,.2f"),
        "Customer Filter Units Sold": st.column_config.NumberColumn("Customer Filter Units Sold", format="%,.2f"),
        "Customer Filter Orders Count": st.column_config.NumberColumn("Customer Filter Orders Count", format="%,d"),
        "Customer Filter Item Count": st.column_config.NumberColumn("Customer Filter Item Count", format="%,d"),
    }


def render_customer_item_product_filters(
    lookup_df: pd.DataFrame,
    key_prefix: str,
) -> tuple[dict[str, tuple[str, ...]], pd.DataFrame]:
    filtered = lookup_df.copy()
    option_source = lookup_df.copy()

    division_key = f"{key_prefix}_division"
    product_family_key = f"{key_prefix}_product_family"
    collection_key = f"{key_prefix}_collection"
    vendor_key = f"{key_prefix}_vendor"
    item_key = f"{key_prefix}_item"

    filter_header_cols = st.columns([4, 1])
    with filter_header_cols[0]:
        st.markdown("**Product Filter**")
    with filter_header_cols[1]:
        if st.button("Clear filters", key=f"{key_prefix}_clear", use_container_width=True):
            _clear_filter_keys(division_key, product_family_key, collection_key, vendor_key, item_key)
            st.rerun()

    cols = st.columns([0.9, 1.3, 1.1, 1.3, 2])
    with cols[0]:
        division_options = _options_for(option_source, "Division", limit=5000)
        _clean_multiselect_state(division_key, division_options)
        selected_divisions = st.multiselect(
            "Division",
            division_options,
            key=division_key,
            placeholder="All divisions",
            on_change=_clear_filter_keys,
            args=(product_family_key, collection_key, vendor_key, item_key),
        )
    filtered = _filter_by_selected(filtered, "Division", selected_divisions)
    option_source = _filter_by_selected(option_source, "Division", selected_divisions)

    with cols[1]:
        family_options = _options_for(option_source, "Product Family", limit=5000)
        _clean_multiselect_state(product_family_key, family_options)
        selected_product_families = st.multiselect(
            "Product Family",
            family_options,
            key=product_family_key,
            placeholder="All families",
            on_change=_clear_filter_keys,
            args=(collection_key, vendor_key, item_key),
        )
    filtered = _filter_by_selected(filtered, "Product Family", selected_product_families)
    option_source = _filter_by_selected(option_source, "Product Family", selected_product_families)

    with cols[2]:
        collection_options = _options_for(option_source, "Collection", limit=5000)
        _clean_multiselect_state(collection_key, collection_options)
        selected_collections = st.multiselect(
            "Collection",
            collection_options,
            key=collection_key,
            placeholder="All collections",
            on_change=_clear_filter_keys,
            args=(vendor_key, item_key),
        )
    filtered = _filter_by_selected(filtered, "Collection", selected_collections)
    option_source = _filter_by_selected(option_source, "Collection", selected_collections)

    with cols[3]:
        vendor_options, vendor_lookup = _vendor_options(option_source)
        _clean_multiselect_state(vendor_key, vendor_options)
        selected_vendors = st.multiselect(
            "Vendor # / Name",
            vendor_options,
            key=vendor_key,
            placeholder="All vendors",
            on_change=_clear_filter_keys,
            args=(item_key,),
        )
    selected_vendor_numbers = tuple(vendor_lookup[label] for label in selected_vendors if label in vendor_lookup)
    filtered = _filter_by_selected(filtered, "Vendor Number", list(selected_vendor_numbers))
    option_source = _filter_by_selected(option_source, "Vendor Number", list(selected_vendor_numbers))

    with cols[4]:
        item_options, item_lookup = _item_description_options(option_source)
        _clean_multiselect_state(item_key, item_options)
        selected_items = st.multiselect(
            "Item # / Description",
            item_options,
            key=item_key,
            placeholder="All items",
        )
    selected_item_numbers = tuple(item_lookup[label] for label in selected_items if label in item_lookup)
    filtered = _filter_by_selected(filtered, "Item Number", list(selected_item_numbers))

    filters = {
        "item_numbers": _normal_tuple(list(selected_item_numbers)),
        "divisions": _normal_tuple(list(selected_divisions)),
        "product_families": _normal_tuple(list(selected_product_families)),
        "collections": _normal_tuple(list(selected_collections)),
        "vendor_numbers": _normal_tuple(list(selected_vendor_numbers)),
    }
    return filters, filtered


def render_customer_item_sales_report(months: int) -> None:
    st.caption(
        "Rank customers by what they purchased inside a product filter. "
        "Uses the Customer tab sales history window."
    )

    control_cols = st.columns([1.1, 1, 1, 1.1])
    with control_cols[0]:
        detail_grain = st.segmented_control(
            "Group detail by",
            ["Item", "Collection"],
            default="Item",
            key="cust_item_sales_detail_grain",
        )
        detail_grain = detail_grain or "Item"
    with control_cols[1]:
        include_discontinued = st.checkbox(
            "Include discontinued items",
            value=False,
            key="cust_item_sales_include_discontinued",
        )
    with control_cols[2]:
        top_customers = st.slider(
            "Top customers",
            min_value=5,
            max_value=100,
            value=25,
            step=5,
            key="cust_item_sales_top_customers",
        )
    with control_cols[3]:
        items_per_customer = st.slider(
            "Rows per customer",
            min_value=1,
            max_value=25,
            value=10,
            step=1,
            key="cust_item_sales_items_per_customer",
        )

    with st.spinner("Loading product filter options..."):
        lookup_df = load_product_filter_lookup(bool(include_discontinued))
    filters, filtered_lookup = render_customer_item_product_filters(
        lookup_df,
        key_prefix="cust_item_sales_product_filter",
    )

    matching_item_count = (
        filtered_lookup["Item Number"].nunique()
        if "Item Number" in filtered_lookup.columns and not filtered_lookup.empty
        else 0
    )
    if matching_item_count == 0:
        st.info("No items match the current product filters.")
        return

    st.caption(f"{matching_item_count:,} item(s) match the current product filter.")

    with st.spinner("Loading customer/item sales..."):
        customer_item_df, queried_at = load_customer_item_sales(
            int(months),
            bool(include_discontinued),
            filters["item_numbers"],
            filters["divisions"],
            filters["product_families"],
            filters["collections"],
            filters["vendor_numbers"],
            int(top_customers),
            int(items_per_customer),
            str(detail_grain),
        )

    st.caption(f"Last queried: {queried_at.strftime('%b %d, %Y at %#I:%M %p')}")
    if customer_item_df.empty:
        st.info("No customer sales found for the current product filter and history window.")
        return

    column_config = _customer_item_sales_column_config()
    summary_columns = [
        "Customer Rank",
        "Customer Number",
        "Customer Name",
        "Customer Filter Sales Amount",
        "Customer Filter Units Sold",
        "Customer Filter Orders Count",
        "Customer Filter Item Count",
    ]
    customer_summary = (
        customer_item_df[summary_columns]
        .drop_duplicates(subset=["Customer Rank", "Customer Number", "Customer Name"])
        .sort_values("Customer Rank")
    )

    st.markdown("**Top Customers for Current Product Filter**")
    st.dataframe(
        customer_summary,
        use_container_width=True,
        hide_index=True,
        column_config=column_config,
    )

    chart_df = customer_summary.sort_values("Customer Filter Sales Amount")
    fig = px.bar(
        chart_df,
        x="Customer Filter Sales Amount",
        y="Customer Name",
        orientation="h",
        title="Top Customers for Current Product Filter",
    )
    fig.update_traces(
        customdata=chart_df[[
            "Customer Number",
            "Customer Filter Units Sold",
            "Customer Filter Orders Count",
            "Customer Filter Item Count",
        ]],
        hovertemplate=(
            "Filter Sales=%{x:$,.2f}<br>"
            "Customer Name=%{y}<br>"
            "Account Number=%{customdata[0]}<br>"
            "Filter Units=%{customdata[1]:,.2f}<br>"
            "Filter Orders=%{customdata[2]:,.0f}<br>"
            "Filter Items=%{customdata[3]:,.0f}"
            "<extra></extra>"
        ),
    )
    fig.update_layout(height=max(400, 24 * len(chart_df)))
    st.plotly_chart(fig, use_container_width=True)

    if str(detail_grain) == "Collection":
        detail_columns = [
            "Customer Rank",
            "Customer Number",
            "Customer Name",
            "Collection Rank",
            "Collection",
            "Item Count",
            "Product Family",
            "Division",
            "Vendor Number",
            "Vendor Name",
            "Sales Amount",
            "Units Sold",
            "Orders Count",
            "Customer Filter Sales Amount",
            "Customer Filter Units Sold",
            "Customer Filter Orders Count",
        ]
        detail_title = "**Customer / Collection Detail**"
    else:
        detail_columns = [
            "Customer Rank",
            "Customer Number",
            "Customer Name",
            "Item Rank",
            "Item Number",
            "Description",
            "Collection",
            "Product Family",
            "Division",
            "Vendor Number",
            "Vendor Name",
            "Sales Amount",
            "Units Sold",
            "Orders Count",
            "Customer Filter Sales Amount",
            "Customer Filter Units Sold",
            "Customer Filter Orders Count",
        ]
        detail_title = "**Customer / Item Detail**"
    st.markdown(detail_title)
    st.dataframe(
        customer_item_df[_available_columns(customer_item_df, detail_columns)],
        use_container_width=True,
        hide_index=True,
        column_config=column_config,
    )


def _label_for_measure(column: str) -> str:
    labels = {
        "Sales Amount": "Sales $",
        "Sales $ - Selected Range": "Sales $ - selected range",
        "Units Sold": "Units sold",
        "Units Sold - Selected Range": "Units sold - selected range",
        "Orders Count": "Orders",
        "Orders - Selected Range": "Orders - selected range",
        "Current Inventory On Hand": "On hand",
        "Current Inventory Available": "Available",
        "Backorders": "Backorders",
        "Qty on PO": "Qty on PO",
        "Landed Cost": "Landed cost",
        "Landed Cost - Most Recent PO": "Landed cost - most recent PO",
        "Landed Cost - Avg Inventory": "Landed cost - avg inventory",
        "Average Inventory Cost": "Average inventory cost",
        "Inventory Value": "Inventory value",
        "On Hand Qty": "On hand qty",
        "Receipt Qty": "Receipt qty",
        "Receipt Count": "Receipt count",
    }
    return labels.get(column, column)


def _with_item_description_column(df: pd.DataFrame) -> pd.DataFrame:
    if ITEM_DESCRIPTION_COLUMN in df.columns:
        return df
    if "Item Number" not in df.columns:
        return df

    working = df.copy()
    item_numbers = working["Item Number"].astype(str).str.strip()
    if "Description" in working.columns:
        descriptions = working["Description"].fillna("").astype(str).str.strip()
        working[ITEM_DESCRIPTION_COLUMN] = item_numbers.where(
            descriptions.eq(""),
            item_numbers + " - " + descriptions,
        )
    else:
        working[ITEM_DESCRIPTION_COLUMN] = item_numbers
    return working


def _available_columns(df: pd.DataFrame, candidates: list[str]) -> list[str]:
    return [col for col in candidates if col in df.columns]


def _first_available(df: pd.DataFrame, candidates: list[str], fallback: str = "") -> str:
    options = _available_columns(df, candidates)
    if options:
        return options[0]
    return fallback


def _breakout_options(columns: list[str]) -> list[str]:
    return [BREAKOUT_NONE] + [col for col in columns if col not in {BREAKOUT_NONE, LEGACY_BREAKOUT_NONE}]


def _normalize_selectbox_state(key: str, options: list[str]) -> None:
    if key not in st.session_state:
        return
    current = st.session_state[key]
    if current == LEGACY_BREAKOUT_NONE:
        st.session_state[key] = BREAKOUT_NONE
    elif current == "Current inventory position" and "Inventory availability" in options:
        st.session_state[key] = "Inventory availability"
    elif current not in options:
        st.session_state[key] = options[0] if options else None


def _apply_graph_filters(
    df: pd.DataFrame,
    key_base: str,
    lookup_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    filtered = df.copy()
    option_source = lookup_df.copy() if lookup_df is not None and not lookup_df.empty else df.copy()
    st.markdown("**Filter**")
    division_key = f"exec_graph_filter_division_{key_base}"
    product_family_key = f"exec_graph_filter_product_family_{key_base}"
    collection_key = f"exec_graph_filter_collection_{key_base}"
    location_key = f"exec_graph_filter_branch_{key_base}"
    vendor_key = f"exec_graph_filter_vendor_{key_base}"
    item_key = f"exec_graph_filter_item_{key_base}"
    show_branch_filter = "Branch Location" in filtered.columns

    cols = st.columns([0.9, 1.2, 1, 1.1, 1.7] if not show_branch_filter else [0.9, 1.2, 1, 1, 1.1, 1.7])
    with cols[0]:
        division_options = _options_for(option_source, "Division")
        _clean_multiselect_state(division_key, division_options)
        selected_divisions = st.multiselect(
            "Division",
            division_options,
            key=division_key,
            on_change=_clear_filter_keys,
            args=(product_family_key, collection_key, location_key, vendor_key, item_key),
        )
    filtered = _filter_by_selected(filtered, "Division", selected_divisions)
    option_source = _filter_by_selected(option_source, "Division", selected_divisions)

    with cols[1]:
        family_options = _options_for(option_source, "Product Family")
        _clean_multiselect_state(product_family_key, family_options)
        selected_product_families = st.multiselect(
            "Product Family",
            family_options,
            key=product_family_key,
            on_change=_clear_filter_keys,
            args=(collection_key, location_key, vendor_key, item_key),
        )
    filtered = _filter_by_selected(filtered, "Product Family", selected_product_families)
    option_source = _filter_by_selected(option_source, "Product Family", selected_product_families)

    with cols[2]:
        collection_options = _options_for(option_source, "Collection")
        _clean_multiselect_state(collection_key, collection_options)
        selected_collections = st.multiselect(
            "Collection",
            collection_options,
            key=collection_key,
            on_change=_clear_filter_keys,
            args=(location_key, vendor_key, item_key),
        )
    filtered = _filter_by_selected(filtered, "Collection", selected_collections)
    option_source = _filter_by_selected(option_source, "Collection", selected_collections)

    if show_branch_filter:
        with cols[3]:
            location_options = _options_for(filtered, "Branch Location")
            _clean_multiselect_state(location_key, location_options)
            selected_locations = st.multiselect(
                "Branch",
                location_options,
                key=location_key,
                on_change=_clear_filter_keys,
                args=(vendor_key, item_key),
            )
        filtered = _filter_by_selected(filtered, "Branch Location", selected_locations)
        vendor_col_index = 4
        item_col_index = 5
    else:
        _clean_multiselect_state(location_key, [])
        vendor_col_index = 3
        item_col_index = 4

    with cols[vendor_col_index]:
        vendor_options, vendor_lookup = _vendor_options(option_source)
        _clean_multiselect_state(vendor_key, vendor_options)
        selected_vendors = st.multiselect(
            "Vendor",
            vendor_options,
            key=vendor_key,
            on_change=_clear_filter_keys,
            args=(item_key,),
        )
    selected_vendor_numbers = [vendor_lookup[label] for label in selected_vendors if label in vendor_lookup]
    filtered = _filter_by_selected(filtered, "Vendor Number", selected_vendor_numbers)
    option_source = _filter_by_selected(option_source, "Vendor Number", selected_vendor_numbers)

    item_options, item_lookup = _item_description_options(option_source)
    _clean_multiselect_state(item_key, item_options)
    with cols[item_col_index]:
        selected_items = st.multiselect(
            "Item / Description",
            item_options,
            key=item_key,
            help="Graph-only item filter. The table filters above still apply first.",
        )

    if selected_items and "Item Number" in filtered.columns:
        selected_item_numbers = [item_lookup[label] for label in selected_items if label in item_lookup]
        filtered = _filter_by_selected(filtered, "Item Number", selected_item_numbers)
    return filtered


def _add_time_period(df: pd.DataFrame, grain: str, date_column: str = "Month") -> tuple[pd.DataFrame, str]:
    if date_column not in df.columns:
        date_candidates = [col for col in df.columns if "Date" in col or col == "Month"]
        date_column = date_candidates[0] if date_candidates else ""
    if not date_column:
        return df, ""

    working = df.copy()
    dates = pd.to_datetime(working[date_column], errors="coerce")
    if grain == "Year":
        working["Time Period"] = dates.dt.to_period("Y").dt.to_timestamp()
    elif grain == "Quarter":
        working["Time Period"] = dates.dt.to_period("Q").dt.to_timestamp()
    else:
        working["Time Period"] = dates.dt.to_period("M").dt.to_timestamp()
    return working, "Time Period"


def _question_config(question: str, df: pd.DataFrame) -> dict:
    if question == "Sales over time":
        return {
            "time_grain": True,
            "x_options": [],
            "x_default": "",
            "measure_options": _available_columns(
                df,
                [
                    "Sales Amount",
                    "Sales $ - Selected Range",
                    "Units Sold",
                    "Units Sold - Selected Range",
                    "Orders Count",
                    "Orders - Selected Range",
                ],
            ),
            "measure_default": _first_available(df, ["Sales Amount", "Sales $ - Selected Range"]),
            "breakout_options": _available_columns(
                df,
                [
                    "Branch Location",
                    "Collection",
                    "Division",
                    "Product Family",
                    "Vendor Name",
                    "Vendor Number",
                    ITEM_DESCRIPTION_COLUMN,
                    "Item Number",
                ],
            ),
            "breakout_default": BREAKOUT_NONE,
            "aggregation_default": "Sum",
            "chart_default": "Line",
        }
    if question == "Sales by category":
        return {
            "time_grain": False,
            "x_options": _available_columns(
                df,
                [
                    "Division",
                    "Product Family",
                    "Collection",
                    "Branch Location",
                    "Vendor Name",
                    "Vendor Number",
                    ITEM_DESCRIPTION_COLUMN,
                    "Item Number",
                ],
            ),
            "x_default": _first_available(df, ["Division", "Product Family", "Collection"]),
            "measure_options": _available_columns(
                df,
                [
                    "Sales Amount",
                    "Sales $ - Selected Range",
                    "Units Sold",
                    "Units Sold - Selected Range",
                    "Orders Count",
                    "Orders - Selected Range",
                ],
            ),
            "measure_default": _first_available(df, ["Sales Amount", "Sales $ - Selected Range"]),
            "breakout_options": _available_columns(
                df,
                ["Collection", "Division", "Product Family", "Branch Location", "Vendor Name", ITEM_DESCRIPTION_COLUMN, "Item Number"],
            ),
            "breakout_default": BREAKOUT_NONE,
            "aggregation_default": "Sum",
            "chart_default": "Bar",
        }
    if question == "Inventory availability":
        return {
            "time_grain": False,
            "x_options": _available_columns(
                df,
                ["Collection", "Division", "Product Family", "Vendor Name", "Vendor Number", ITEM_DESCRIPTION_COLUMN, "Item Number"],
            ),
            "x_default": _first_available(df, ["Collection", "Division", "Product Family", ITEM_DESCRIPTION_COLUMN, "Item Number"]),
            "measure_options": _available_columns(
                df,
                [
                    "Current Inventory Available",
                    "Current Inventory On Hand",
                    "Backorders",
                    "Qty on PO",
                    "Sales $ - Selected Range",
                    "Units Sold - Selected Range",
                ],
            ),
            "measure_default": _first_available(df, ["Current Inventory Available"]),
            "breakout_options": _available_columns(
                df,
                ["Division", "Product Family", "Collection", "Vendor Name", "Vendor Number", ITEM_DESCRIPTION_COLUMN, "Item Number"],
            ),
            "breakout_default": BREAKOUT_NONE,
            "aggregation_default": "Sum",
            "chart_default": "Bar",
        }
    if question == "Inventory by location":
        return {
            "time_grain": False,
            "x_options": _available_columns(
                df,
                ["Branch Location", "Collection", "Division", "Product Family", "Vendor Name", ITEM_DESCRIPTION_COLUMN, "Item Number"],
            ),
            "x_default": _first_available(df, ["Branch Location"]),
            "measure_options": _available_columns(df, ["On Hand Qty", "Inventory Value", "Average Inventory Cost"]),
            "measure_default": _first_available(df, ["On Hand Qty"]),
            "breakout_options": _available_columns(
                df,
                ["Collection", "Division", "Product Family", "Vendor Name", "Vendor Number", ITEM_DESCRIPTION_COLUMN, "Item Number"],
            ),
            "breakout_default": BREAKOUT_NONE,
            "aggregation_default": "Sum",
            "chart_default": "Bar",
        }
    if question == "Landed cost over time":
        return {
            "time_grain": True,
            "x_options": [],
            "x_default": "",
            "measure_options": _available_columns(df, ["Landed Cost", "Receipt Qty", "Receipt Count"]),
            "measure_default": _first_available(df, ["Landed Cost"]),
            "breakout_options": _available_columns(
                df,
                ["Vendor Name", "Vendor Number", ITEM_DESCRIPTION_COLUMN, "Item Number", "Collection", "Division", "Product Family"],
            ),
            "breakout_default": BREAKOUT_NONE,
            "aggregation_default": "Average",
            "chart_default": "Line",
        }
    if question == "Inventory cost by item":
        return {
            "time_grain": False,
            "x_options": _available_columns(
                df,
                [ITEM_DESCRIPTION_COLUMN, "Item Number", "Collection", "Product Family", "Branch Location", "Vendor Name", "Division"],
            ),
            "x_default": _first_available(df, [ITEM_DESCRIPTION_COLUMN, "Item Number"]),
            "measure_options": _available_columns(df, ["Average Inventory Cost", "Inventory Value", "On Hand Qty"]),
            "measure_default": _first_available(df, ["Average Inventory Cost"]),
            "breakout_options": _available_columns(
                df,
                ["Branch Location", "Collection", "Product Family", "Vendor Name", "Division", ITEM_DESCRIPTION_COLUMN, "Item Number"],
            ),
            "breakout_default": BREAKOUT_NONE,
            "aggregation_default": "Average",
            "chart_default": "Bar",
        }
    return {}


def _question_is_supported(question: str, df: pd.DataFrame) -> bool:
    config = _question_config(question, df)
    if not config or not config.get("measure_options"):
        return False
    if config.get("time_grain"):
        # Both time-series questions (Sales over time, Landed cost over time) are only
        # meaningful against a true one-row-per-period "Month" column. Datasets like Product
        # Snapshot have no such column but do have unrelated single-value date fields (e.g.
        # "Last FOB Date"); falling back to those silently buckets a trailing-window total by
        # an arbitrary date and produces a meaningless spike, not a real monthly trend.
        return "Month" in df.columns
    return bool(config.get("x_options"))


def _available_questions(df: pd.DataFrame) -> list[str]:
    candidates = [
        "Sales over time",
        "Sales by category",
        "Inventory availability",
        "Inventory by location",
        "Landed cost over time",
        "Inventory cost by item",
    ]
    supported = [question for question in candidates if _question_is_supported(question, df)]
    supported.append("Custom / advanced")
    return supported


def _value_axis_range(chart_df: pd.DataFrame) -> list[float] | None:
    if "Value" not in chart_df.columns:
        return None
    values = pd.to_numeric(chart_df["Value"], errors="coerce").dropna()
    if values.empty:
        return None

    min_value = float(values.min())
    max_value = float(values.max())
    low = min(0.0, min_value)
    high = max(0.0, max_value)
    span = high - low
    padding = span * 0.08 if span else max(abs(high), 1.0) * 0.1

    if low < 0:
        low -= padding
    if high > 0:
        high += padding
    if low == high:
        high = low + 1
    return [low, high]


def _x_axis_config(chart_df: pd.DataFrame, x_axis: str) -> dict:
    if x_axis not in chart_df.columns:
        return {}

    values = chart_df[x_axis].dropna()
    if values.empty:
        return {}

    is_date_axis = pd.api.types.is_datetime64_any_dtype(values) or x_axis in {"Month", "Time Period"} or "Date" in x_axis
    if is_date_axis:
        dates = pd.to_datetime(values, errors="coerce").dropna()
        if dates.empty:
            return {}
        low = dates.min()
        high = dates.max()
        if low == high:
            low = low - pd.Timedelta(days=1)
            high = high + pd.Timedelta(days=1)
        else:
            padding = (high - low) * 0.03
            low = low - padding
            high = high + padding
        return {"range": [low, high], "autorange": False}

    if pd.api.types.is_numeric_dtype(values):
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        if numeric.empty:
            return {}
        low = float(numeric.min())
        high = float(numeric.max())
        span = high - low
        padding = span * 0.03 if span else max(abs(high), 1.0) * 0.1
        return {"range": [low - padding, high + padding], "autorange": False}

    categories = values.astype(str).drop_duplicates().tolist()
    return {"type": "category", "categoryorder": "array", "categoryarray": categories}


def _render_chart(
    chart_df: pd.DataFrame,
    chart_type: str,
    x_axis: str,
    y_metric: str,
    color_by: str,
    aggregation: str,
) -> None:
    color_arg = None if _is_no_breakout(color_by) else color_by
    title = f"{aggregation} of {_label_for_measure(y_metric)} by {x_axis}"
    if color_arg:
        title += f", broken out by {color_arg}"

    y_range = _value_axis_range(chart_df)
    x_config = _x_axis_config(chart_df, x_axis)

    if chart_type == "Line":
        fig = px.line(chart_df, x=x_axis, y="Value", color=color_arg, markers=True, title=title)
    elif chart_type == "Area":
        fig = px.area(chart_df, x=x_axis, y="Value", color=color_arg, title=title)
    elif chart_type == "Scatter":
        fig = px.scatter(chart_df, x=x_axis, y="Value", color=color_arg, title=title)
    else:
        fig = px.bar(chart_df, x=x_axis, y="Value", color=color_arg, title=title, barmode="group")

    fig.update_layout(
        template="plotly_white",
        height=560,
        margin=dict(l=10, r=10, t=64, b=10),
        font=dict(family="Manrope", size=13, color="#142033"),
        title_font=dict(size=19, color="#1e3a5f"),
        legend_title_text=color_arg or "",
    )
    fig.update_xaxes(title=x_axis, showgrid=False, **x_config)
    fig.update_yaxes(
        title=_label_for_measure(y_metric),
        gridcolor="#e2e8f0",
        range=y_range,
        autorange=False if y_range else True,
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_advanced_graph_builder(df: pd.DataFrame, dataset: str) -> None:
    numeric_cols = _numeric_columns(df)
    dimension_cols = _dimension_columns(df)
    all_cols = list(df.columns)
    if not numeric_cols or not dimension_cols:
        st.info("The selected dataset does not have enough dimensions and numeric measures to graph.")
        return

    presets = [
        "Sales by Division",
        "Sales by Collection",
        "Sales by Branch Location",
        "Landed Cost Changes by Vendor Over Time",
        "Average Landed Cost of Inventory by Item",
        "Custom",
    ]
    preset = st.selectbox("Analysis preset", presets, key="exec_graph_preset")
    chart_type_default, x_default, y_default, color_default, agg_default = _preset_defaults(dataset, preset, all_cols)
    control_key_base = (
        f"{dataset}_{preset}"
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .lower()
    )

    control_cols = st.columns([1, 1, 1, 1, 1])
    with control_cols[0]:
        chart_type = st.selectbox(
            "Chart",
            ["Bar", "Line", "Area", "Scatter"],
            index=["Bar", "Line", "Area", "Scatter"].index(chart_type_default)
            if chart_type_default in ["Bar", "Line", "Area", "Scatter"]
            else 0,
            key=f"exec_chart_type_{control_key_base}",
        )
    with control_cols[1]:
        x_axis = st.selectbox(
            "First field",
            dimension_cols,
            index=dimension_cols.index(x_default) if x_default in dimension_cols else 0,
            key=f"exec_x_axis_{control_key_base}",
        )
    with control_cols[2]:
        y_metric = st.selectbox(
            "Measure",
            numeric_cols,
            index=numeric_cols.index(y_default) if y_default in numeric_cols else 0,
            key=f"exec_y_metric_{control_key_base}",
        )
    with control_cols[3]:
        color_options = _breakout_options([col for col in dimension_cols if col != x_axis])
        color_key = f"exec_color_by_{control_key_base}"
        _normalize_selectbox_state(color_key, color_options)
        color_by = st.selectbox(
            "Break out by",
            color_options,
            index=color_options.index(color_default) if color_default in color_options else 0,
            key=color_key,
        )
    with control_cols[4]:
        aggregation = st.selectbox(
            "Aggregation",
            ["Sum", "Average", "Median", "Minimum", "Maximum", "Count"],
            index=["Sum", "Average", "Median", "Minimum", "Maximum", "Count"].index(agg_default)
            if agg_default in ["Sum", "Average", "Median", "Minimum", "Maximum", "Count"]
            else 0,
            key=f"exec_aggregation_{control_key_base}",
        )

    chart_df = _aggregate(df, x_axis, y_metric, color_by, aggregation)
    _render_chart(chart_df, chart_type, x_axis, y_metric, color_by, aggregation)

    with st.expander("Pivoted chart data", expanded=False):
        st.dataframe(chart_df, use_container_width=True, hide_index=True, height=260)


def render_graph_card(df: pd.DataFrame, dataset: str, lookup_df: pd.DataFrame | None = None) -> None:
    with st.expander("Graph Builder", expanded=True):
        if df.empty:
            st.info("No rows are available for the current filters.")
            return

        graph_source_df = _with_item_description_column(df)
        questions = _available_questions(graph_source_df)
        _normalize_selectbox_state("exec_guided_question", questions)
        question_col, reset_col = st.columns([4, 1])
        with question_col:
            question = st.selectbox("Question", questions, key="exec_guided_question")
        with reset_col:
            if st.button("Clear Graph filters", use_container_width=True, key="exec_clear_graph_filters"):
                _clear_graph_filter_state()
                st.rerun()
        key_base = _safe_key(f"{dataset}_{question}")
        if question == "Sales over time":
            st.caption("This view shows monthly trend points. For a date-range total by item, collection, or vendor, use the Sales Summary dataset.")

        if question == "Custom / advanced":
            _render_advanced_graph_builder(graph_source_df, dataset)
            return

        graph_df = _apply_graph_filters(graph_source_df, key_base, lookup_df)
        if graph_df.empty:
            st.info("No rows match the graph filters.")
            return

        config = _question_config(question, graph_df)
        if not config or not config.get("measure_options"):
            st.info("This dataset does not have the fields needed for that question yet.")
            return

        chart_options = ["Bar", "Line", "Area", "Scatter"]
        aggregation_options = ["Sum", "Average", "Median", "Minimum", "Maximum", "Count"]
        control_cols = st.columns([1, 1, 1, 1, 1])
        with control_cols[0]:
            chart_type = st.selectbox(
                "Chart",
                chart_options,
                index=chart_options.index(config["chart_default"]),
                key=f"exec_guided_chart_{key_base}",
            )
        with control_cols[1]:
            if config["time_grain"]:
                time_grain = st.selectbox(
                    "Time grain",
                    ["Month", "Quarter", "Year"],
                    key=f"exec_guided_time_grain_{key_base}",
                )
                working_df, x_axis = _add_time_period(graph_df, time_grain)
            else:
                x_options = config.get("x_options", [])
                if not x_options:
                    st.info("This question needs at least one grouping field.")
                    return
                default_x = config["x_default"] if config["x_default"] in x_options else x_options[0]
                x_axis = st.selectbox(
                    "Group by",
                    x_options,
                    index=x_options.index(default_x),
                    key=f"exec_guided_x_{key_base}",
                )
                working_df = graph_df
        with control_cols[2]:
            measure_options = config["measure_options"]
            default_measure = config["measure_default"] if config["measure_default"] in measure_options else measure_options[0]
            y_metric = st.selectbox(
                "Measure",
                measure_options,
                index=measure_options.index(default_measure),
                format_func=_label_for_measure,
                key=f"exec_guided_measure_{key_base}",
            )
        with control_cols[3]:
            breakout_options = _breakout_options([col for col in config.get("breakout_options", []) if col != x_axis])
            default_breakout = config["breakout_default"] if config["breakout_default"] in breakout_options else BREAKOUT_NONE
            breakout_key = f"exec_guided_breakout_{key_base}"
            _normalize_selectbox_state(breakout_key, breakout_options)
            color_by = st.selectbox(
                "Break out by",
                breakout_options,
                index=breakout_options.index(default_breakout),
                key=breakout_key,
            )
        with control_cols[4]:
            default_aggregation = config["aggregation_default"]
            if y_metric in {
                "Sales Amount",
                "Sales $ - Selected Range",
                "Units Sold",
                "Units Sold - Selected Range",
                "Orders Count",
                "Orders - Selected Range",
                "Inventory Value",
                "On Hand Qty",
                "Receipt Qty",
                "Receipt Count",
            }:
                default_aggregation = "Sum"
            if y_metric in {"Landed Cost", "Average Inventory Cost", "Landed Cost - Avg Inventory", "Landed Cost - Most Recent PO"}:
                default_aggregation = "Average"
            aggregation = st.selectbox(
                "Aggregation",
                aggregation_options,
                index=aggregation_options.index(default_aggregation),
                key=f"exec_guided_aggregation_{key_base}_{_safe_key(y_metric)}",
            )

        if not x_axis:
            st.info("Choose a time grain or grouping field to build the chart.")
            return

        chart_df = _aggregate(working_df, x_axis, y_metric, color_by, aggregation)
        _render_chart(chart_df, chart_type, x_axis, y_metric, color_by, aggregation)

        with st.expander("Pivoted chart data", expanded=False):
            st.dataframe(chart_df, use_container_width=True, hide_index=True, height=260)


def render_product_tab() -> None:
    dataset_options = ["Product Snapshot", "Sales Summary", "Sales Over Time", "Landed Cost History", "Average Inventory Cost"]

    today = datetime.now().date()
    default_sales_start = today - timedelta(days=365)
    date_range_datasets = {"Product Snapshot", "Sales Summary", "Sales Over Time"}
    hero_slot = st.empty()
    controls = st.columns([1.3, 0.95, 0.95, 0.95, 0.8, 0.75])
    with controls[0]:
        dataset = st.selectbox(
            "Product dataset",
            dataset_options,
            key="exec_product_dataset",
        )
    with controls[1]:
        if dataset in date_range_datasets:
            if dataset == "Product Snapshot":
                date_key_prefix = "exec_product_snapshot"
            elif dataset == "Sales Summary":
                date_key_prefix = "exec_sales_summary"
            else:
                date_key_prefix = "exec_sales"
            start_date = st.date_input(
                "Start date",
                value=st.session_state.get(f"{date_key_prefix}_start_date", default_sales_start),
                key=f"{date_key_prefix}_start_date",
            )
            months = 24
        else:
            months = st.number_input(
                "History months",
                min_value=1,
                max_value=120,
                value=24,
                step=1,
                disabled=dataset in {"Product Snapshot", "Average Inventory Cost"},
                key="exec_history_months",
            )
            start_date = None
    with controls[2]:
        if dataset in date_range_datasets:
            end_date = st.date_input(
                "End date",
                value=st.session_state.get(f"{date_key_prefix}_end_date", today),
                key=f"{date_key_prefix}_end_date",
            )
        else:
            end_date = None
    with controls[3]:
        if dataset in {"Sales Summary", "Sales Over Time"}:
            separate_sales_by_branch = st.checkbox(
                "Separate sales by branch",
                value=False,
                key="exec_sales_split_by_branch",
            )
        else:
            separate_sales_by_branch = False
    with controls[4]:
        include_discontinued = st.checkbox(
            "Include discontinued",
            value=False,
            key="exec_include_discontinued",
        )
    with controls[5]:
        if st.button("Refresh Gartman", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    if dataset in date_range_datasets and start_date and end_date and start_date > end_date:
        st.warning("Start date is after end date, so I am using the dates in chronological order.")
        start_date, end_date = end_date, start_date

    with st.spinner("Querying Gartman..."):
        lookup_df = load_product_filter_lookup(bool(include_discontinued))
        query_filters = _collect_query_filters(
            dataset,
            lookup_df,
            include_branch_filters=dataset not in {"Sales Summary", "Sales Over Time"} or bool(separate_sales_by_branch),
        )
        df, queried_at = load_product_dataset(
            dataset,
            bool(include_discontinued),
            int(months),
            query_filters["item_numbers"],
            query_filters["divisions"],
            query_filters["product_families"],
            query_filters["collections"],
            query_filters["branch_locations"],
            query_filters["vendor_numbers"],
            _date_sql_literal(start_date),
            _date_sql_literal(end_date),
            bool(separate_sales_by_branch),
        )
    with hero_slot.container():
        render_hero(queried_at)
    filtered = render_data_card(df, dataset, lookup_df)
    if dataset in date_range_datasets and start_date and end_date:
        st.caption(f"{dataset} date window: {start_date:%m/%d/%Y} to {end_date:%m/%d/%Y}")
    render_graph_card(filtered, dataset, lookup_df)

    if render_lazy_analysis_card("Biggest Item Trends", "item_trend_show"):
        with st.container(border=True):
            render_trend_section(
                "Biggest Item Trends",
                "Ranks items by the change in sales dollars between two equal-length periods, "
                "so you can spot what's suddenly taking off or falling off a cliff.",
                grain="item",
                key_prefix="item_trend",
                show_header=False,
            )

    if render_lazy_analysis_card("Biggest Collection Trends", "collection_trend_show"):
        with st.container(border=True):
            render_trend_section(
                "Biggest Collection Trends",
                "Ranks collections by the change in sales dollars between two equal-length periods.",
                grain="collection",
                key_prefix="collection_trend",
                show_header=False,
            )


def render_customer_tab() -> None:
    hero_slot = st.empty()
    with hero_slot.container():
        render_hero(None)

    months = st.slider(
        "Sales history window (months)", min_value=1, max_value=36, value=12, key="customer_tab_months"
    )
    with st.spinner("Loading customer sales..."):
        detail, queried_at = load_customer_sales_detail(months)
    with hero_slot.container():
        render_hero(queried_at)

    if detail.empty:
        st.info("No customer sales found for this window.")
        return

    branches = sorted(detail["Branch Location"].dropna().unique().tolist())
    classes = sorted(detail["Customer Class Description"].dropna().unique().tolist())
    customer_metric_column_config = {
        "Sales Amount": st.column_config.NumberColumn("Sales Amount", format="$%,.2f"),
        "Units Sold": st.column_config.NumberColumn("Units Sold", format="%,.2f"),
        "Orders Count": st.column_config.NumberColumn("Orders Count", format="%,d"),
    }

    # ---- Sales by Customer ----
    with st.expander("Sales by Customer", expanded=True):
        st.caption("Total sales, units, and order count per customer across the selected window.")
        by_cust = (
            detail.groupby(["Customer Number", "Customer Name"], as_index=False)
            .agg({"Sales Amount": "sum", "Units Sold": "sum", "Orders Count": "sum"})
            .sort_values("Sales Amount", ascending=False)
        )
        top_n = st.slider("Show top N customers", 5, 100, 25, key="cust_sales_topn")
        top_cust = by_cust.head(top_n)
        st.dataframe(
            top_cust,
            use_container_width=True,
            hide_index=True,
            column_config=customer_metric_column_config,
        )
        top_cust_chart = top_cust.sort_values("Sales Amount")
        fig = px.bar(
            top_cust_chart,
            x="Sales Amount", y="Customer Name", orientation="h",
            title="Top Customers by Sales",
        )
        fig.update_traces(
            customdata=top_cust_chart[["Customer Number"]],
            hovertemplate=(
                "Sales Amount=%{x:$,.2f}<br>"
                "Customer Name=%{y}<br>"
                "Account Number=%{customdata[0]}"
                "<extra></extra>"
            ),
        )
        fig.update_layout(height=max(400, 22 * len(top_cust)))
        st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "The sections below are opt-in. Expand a section to run that analysis, "
        "so the tab doesn't load every chart at once."
    )

    # ---- Sales by Item by Customer ----
    if render_lazy_analysis_card("Sales by Item by Customer", "cust_show_item_sales"):
        with st.container(border=True):
            render_customer_item_sales_report(int(months))

    # ---- Sales by Division per Customer ----
    if render_lazy_analysis_card("Sales by Division per Customer", "cust_show_division"):
        with st.container(border=True):
            st.caption("Pick a customer to see how their spend splits across product divisions.")
            cust_options = by_cust["Customer Name"] + " (" + by_cust["Customer Number"] + ")"
            chosen = st.selectbox("Customer", cust_options.tolist(), key="cust_div_pick")
            chosen_number = chosen.split("(")[-1].rstrip(")") if chosen else None
            if chosen_number:
                cust_div = (
                    detail[detail["Customer Number"] == chosen_number]
                    .groupby("Division", as_index=False)
                    .agg({"Sales Amount": "sum", "Units Sold": "sum", "Orders Count": "sum"})
                    .sort_values("Sales Amount", ascending=False)
                )
                st.dataframe(
                    cust_div,
                    use_container_width=True,
                    hide_index=True,
                    column_config=customer_metric_column_config,
                )
                fig = px.pie(cust_div, names="Division", values="Sales Amount", title=f"{chosen}: Sales by Division")
                st.plotly_chart(fig, use_container_width=True)

    # ---- Sales by Customer Class ----
    if render_lazy_analysis_card("Sales by Customer Class", "cust_show_class"):
        with st.container(border=True):
            st.caption("Total sales rolled up by customer class (e.g. Retailers, Installers, Licensed Contractors).")
            by_class = (
                detail.groupby("Customer Class Description", as_index=False)
                .agg({"Sales Amount": "sum", "Units Sold": "sum", "Orders Count": "sum"})
                .sort_values("Sales Amount", ascending=False)
            )
            st.dataframe(
                by_class,
                use_container_width=True,
                hide_index=True,
                column_config=customer_metric_column_config,
            )
            fig = px.bar(by_class, x="Customer Class Description", y="Sales Amount", title="Sales by Customer Class")
            st.plotly_chart(fig, use_container_width=True)

    # ---- Sales by Customer Class per Branch ----
    if render_lazy_analysis_card("Sales by Customer Class per Branch", "cust_show_class_branch"):
        with st.container(border=True):
            st.caption("Cross-tab of customer class against branch location.")
            pivot = detail.pivot_table(
                index="Customer Class Description", columns="Branch Location",
                values="Sales Amount", aggfunc="sum", fill_value=0,
            )
            pivot_display = pivot.reset_index()
            pivot_column_config = {
                column: st.column_config.NumberColumn(str(column), format="$%,.2f")
                for column in pivot.columns
            }
            st.dataframe(
                pivot_display,
                use_container_width=True,
                hide_index=True,
                column_config=pivot_column_config,
            )
            pivot_long = pivot.reset_index().melt(
                id_vars="Customer Class Description", var_name="Branch Location", value_name="Sales Amount"
            )
            fig = px.bar(
                pivot_long, x="Customer Class Description", y="Sales Amount", color="Branch Location",
                barmode="group", title="Customer Class by Branch",
            )
            st.plotly_chart(fig, use_container_width=True)

    # ---- Sales by Branch by Customer ----
    if render_lazy_analysis_card("Sales by Branch by Customer", "cust_show_branch_cust"):
        with st.container(border=True):
            st.caption("Pick a branch to see its top customers.")
            branch_pick = st.selectbox("Branch Location", branches, key="cust_branch_pick")
            branch_cust = (
                detail[detail["Branch Location"] == branch_pick]
                .groupby(["Customer Number", "Customer Name"], as_index=False)
                .agg({"Sales Amount": "sum", "Units Sold": "sum", "Orders Count": "sum"})
                .sort_values("Sales Amount", ascending=False)
                .head(25)
            )
            st.dataframe(
                branch_cust,
                use_container_width=True,
                hide_index=True,
                column_config=customer_metric_column_config,
            )
            branch_cust_chart = branch_cust.sort_values("Sales Amount")
            fig = px.bar(
                branch_cust_chart,
                x="Sales Amount", y="Customer Name", orientation="h",
                title=f"Branch {branch_pick}: Top Customers",
            )
            fig.update_traces(
                customdata=branch_cust_chart[["Customer Number"]],
                hovertemplate=(
                    "Sales Amount=%{x:$,.2f}<br>"
                    "Customer Name=%{y}<br>"
                    "Account Number=%{customdata[0]}"
                    "<extra></extra>"
                ),
            )
            fig.update_layout(height=max(400, 22 * len(branch_cust)))
            st.plotly_chart(fig, use_container_width=True)

    if render_lazy_analysis_card("Biggest Customer Trends", "cust_show_trend"):
        with st.container(border=True):
            render_trend_section(
                "Biggest Customer Trends",
                "Ranks customers by the change in sales dollars between two equal-length periods, "
                "so you can spot who's ramping up or pulling back.",
                grain="customer",
                key_prefix="cust_trend",
                show_header=False,
            )


def render_location_tab() -> None:
    hero_slot = st.empty()
    with hero_slot.container():
        render_hero(None)

    months = st.slider(
        "Sales history window (months)", min_value=1, max_value=36, value=12, key="location_tab_months"
    )
    with st.spinner("Loading branch sales..."):
        branch_df, queried_at = load_branch_summary(months)
    with hero_slot.container():
        render_hero(queried_at)

    if branch_df.empty:
        st.info("No branch sales found for this window.")
        return

    branch_column_config = {
        "Sales Amount": st.column_config.NumberColumn("Sales Amount", format="$%,.2f"),
        "Units Sold": st.column_config.NumberColumn("Units Sold", format="%,.2f"),
        "Orders Count": st.column_config.NumberColumn("Orders Count", format="%,d"),
        "Customer Count": st.column_config.NumberColumn("Customer Count", format="%,d"),
    }

    with st.expander("Sales per Branch", expanded=True):
        st.caption("Total sales and units shipped per branch location over the selected window.")
        st.dataframe(
            branch_df, use_container_width=True, hide_index=True, column_config=branch_column_config
        )
        fig = px.bar(
            branch_df.sort_values("Sales Amount"),
            x="Sales Amount", y="Branch Location", orientation="h",
            title="Sales by Branch",
        )
        fig.update_yaxes(type="category")
        st.plotly_chart(fig, use_container_width=True)

    if render_lazy_analysis_card("Orders per Branch", "orders_per_branch_show"):
        with st.container(border=True):
            st.caption("Distinct order/invoice count per branch location over the selected window.")
            orders_sorted = branch_df.sort_values("Orders Count", ascending=False)
            st.dataframe(
                orders_sorted[["Branch Location", "Orders Count", "Customer Count"]],
                use_container_width=True, hide_index=True,
                column_config=branch_column_config,
            )
            fig = px.bar(
                orders_sorted.sort_values("Orders Count"),
                x="Orders Count", y="Branch Location", orientation="h",
                title="Orders by Branch",
            )
            fig.update_yaxes(type="category")
            st.plotly_chart(fig, use_container_width=True)


def render_order_tab() -> None:
    hero_slot = st.empty()
    with hero_slot.container():
        render_hero(None)

    months = st.slider(
        "History window (months)", min_value=1, max_value=36, value=12, key="order_tab_months"
    )
    combine_reps = st.checkbox(
        "Combine sales reps with multiple rep numbers",
        value=False,
        key="emp_combine_reps",
        help='Merges known multi-number outside reps (Dave Courtney, Jose Zaldivar, Warren Carmichael) '
             'into a single "<Name> COMBINED" row on the Sales per Employee table below.',
    )
    with st.spinner("Loading employee sales..."):
        emp_df, emp_queried_at = load_employee_sales(months, combine_reps)
    with hero_slot.container():
        render_hero(emp_queried_at)

    with st.expander("Sales per Employee", expanded=True):
        st.caption("Total sales, units, orders, and distinct customers handled per employee/salesman.")
        if emp_df.empty:
            st.info("No employee sales found for this window.")
        else:
            by_emp = (
                emp_df.groupby(["Employee Number", "Employee Name"], as_index=False)
                .agg({"Sales Amount": "sum", "Units Sold": "sum", "Orders Count": "sum", "Customer Count": "sum"})
                .sort_values("Sales Amount", ascending=False)
            )
            top_n = st.slider("Show top N employees", 5, 50, 20, key="emp_sales_topn")
            top_emp = by_emp.head(top_n)
            st.dataframe(
                top_emp,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Sales Amount": st.column_config.NumberColumn("Sales Amount", format="$%,.2f"),
                    "Units Sold": st.column_config.NumberColumn("Units Sold", format="%,.2f"),
                    "Orders Count": st.column_config.NumberColumn("Orders Count", format="%,d"),
                    "Customer Count": st.column_config.NumberColumn("Customer Count", format="%,d"),
                },
            )
            fig = px.bar(
                top_emp.sort_values("Sales Amount"),
                x="Sales Amount", y="Employee Name", orientation="h",
                title="Sales by Employee",
            )
            fig.update_layout(height=max(400, 24 * len(top_emp)))
            st.plotly_chart(fig, use_container_width=True)

    if render_lazy_analysis_card("Orders per Day", "orders_per_day_show"):
        with st.container(border=True):
            with st.spinner("Loading daily order activity..."):
                orders_df, orders_queried_at = load_orders_per_day(months)
            with hero_slot.container():
                render_hero(max(t for t in [emp_queried_at, orders_queried_at] if t))
            st.caption(
                "Daily order-entry volume by inside sales rep (the Gartman user who keyed the order in), "
                "with an optional breakout by rep. Internal warehouse transfers are excluded."
            )
            if orders_df.empty:
                st.info("No order activity found for this window.")
            else:
                breakout = st.checkbox("Break out by inside sales rep", value=False, key="orders_per_day_breakout")
                if breakout:
                    # Cap the legend to the top reps by volume; a chart with 40+ colored lines is
                    # both unreadable and heavy to render, so the long tail gets bucketed as "Other".
                    top_reps = (
                        orders_df.groupby("Inside Rep User ID")["Orders Count"].sum()
                        .sort_values(ascending=False).head(15).index
                    )
                    capped = orders_df.copy()
                    capped["Inside Rep User ID"] = capped["Inside Rep User ID"].where(
                        capped["Inside Rep User ID"].isin(top_reps), "Other"
                    )
                    daily = capped.groupby(["Order Date", "Inside Rep User ID"], as_index=False)["Orders Count"].sum()
                    fig = px.line(
                        daily, x="Order Date", y="Orders Count", color="Inside Rep User ID",
                        title="Orders per Day by Inside Sales Rep (top 15, rest grouped as Other)",
                    )
                else:
                    daily = orders_df.groupby("Order Date", as_index=False)["Orders Count"].sum()
                    fig = px.line(daily, x="Order Date", y="Orders Count", title="Orders per Day")
                st.plotly_chart(fig, use_container_width=True)
                avg_per_day = daily.groupby("Order Date")["Orders Count"].sum().mean() if not breakout else (
                    orders_df.groupby("Order Date")["Orders Count"].sum().mean()
                )
                st.caption(f"Average orders per day over this window: {avg_per_day:,.1f}")


def render_placeholder(tab_name: str) -> None:
    render_hero(None)
    st.info(
        f"{tab_name} analysis will live here. I started with Product first so we can lock the data model, "
        "chart controls, and executive layout before cloning the pattern into the other views."
    )


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=":bar_chart:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    apply_dashboard_css()
    active_tab = render_tab_buttons()

    if active_tab == "Product":
        render_product_tab()
    elif active_tab == "Customer":
        render_customer_tab()
    elif active_tab == "Location":
        render_location_tab()
    elif active_tab == "Order":
        render_order_tab()
    elif active_tab == "Financial":
        render_financial_tab()
    else:
        render_placeholder(active_tab)


if __name__ == "__main__":
    main()
