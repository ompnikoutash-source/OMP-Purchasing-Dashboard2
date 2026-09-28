"""
OMP Sales Dashboard — Flooring Collection Browser

Sales reps select a collection, click an item row to highlight it, then
click a section button (General / Arrivals / …) to expand that item's detail.

Run:
    .venv\\Scripts\\python.exe -m streamlit run sales_dashboard_v2.py --server.port 8503
"""

from __future__ import annotations
import html
import warnings
warnings.filterwarnings("ignore")

import calendar as _cal
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core.db_connection import connect, sql_escape

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="OMP Sales Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_LOCS     = [1, 3, 4, 5, 6, 8, 9, 51]
_SECTIONS = ["General", "Arrivals", "Sales", "Specs"]
_READY    = {"General", "Arrivals", "Sales", "Specs"}
_EXCLUDED_ARRIVAL_VENDOR_NUMBERS = (
    3046, 323, 3067, 3956, 1059, 1027, 1244, 1451, 2512, 1379
)
_EXCLUDED_ARRIVAL_VENDOR_SQL = ", ".join(str(v) for v in _EXCLUDED_ARRIVAL_VENDOR_NUMBERS)

_SPECS_DIR = Path(
    r"C:\Users\niko\OneDrive - Old Master Products"
    r"\Purchasing - Flooring Reports\Dashboard Files"
)
_QUERY_LOG = Path(__file__).with_name("sales_dashboard_gartman_queries.log")

_SPEC_GROUPS = {
    "Physical": [
        ("THICKNESS",         "Thickness"),
        ("WIDTH",             "Width"),
        ("LENGTH",            "Length"),
        ("WEAR LAYER",        "Wear Layer"),
        ("SPECIES",           "Species"),
        ("GRADE",             "Grade"),
        ("FINISH",            "Finish"),
        ("TEXTURE",           "Texture"),
        ("FILLER COLOR",      "Filler Color"),
        ("EDGE DETAIL",       "Edge Detail"),
        ("CONSTRUCTION",      "Construction"),
    ],
    "Performance": [
        ("JANKA RATING",      "Janka Rating"),
        ("TREATMENT PROCESS", "Treatment Process"),
        ("VENEER CUT",        "Veneer Cut"),
        ("INSTALLATION METHODS", "Installation Methods"),
    ],
    "Package": [
        ("SQF / BOX",         "SF / Box"),
        ("LBS / BOX",         "Lbs / Box"),
        ("PIECES/BOX",        "Pieces / Box"),
    ],
    "Warranty": [
        ("STRUCTURE RESIDENTIAL", "Structure — Residential"),
        ("FINISH RESIDENTIAL",    "Finish — Residential"),
        ("STRUCTURE COMMERCIAL",  "Structure — Commercial"),
        ("FINISH COMMERICAL",     "Finish — Commercial"),
    ],
}


def _gartman_query_stamp(section: str, item_num: str) -> str:
    queried_at = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    try:
        with _QUERY_LOG.open("a", encoding="utf-8") as log:
            log.write(f"{queried_at} | {section} | {item_num}\n")
    except OSError:
        pass
    return queried_at


def _query_stamp_html(queried_at: str) -> str:
    return f'<div class="gartman-query-stamp">Last queried from Gartman: {queried_at}</div>'


def _apply_arrival_uncommitted(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    attached_bo = out["ATTACHED_BO_SF"].iloc[0]
    remaining_attached = max(0.0, float(attached_bo) if pd.notna(attached_bo) else 0.0)
    uncommitted_values = []
    applied_values = []

    for total_on_po in pd.to_numeric(out["TOTAL_ON_PO"], errors="coerce").fillna(0.0):
        total = max(0.0, float(total_on_po))
        applied = min(total, remaining_attached)
        applied_values.append(applied)
        uncommitted_values.append(max(0.0, total - applied))
        remaining_attached = max(0.0, remaining_attached - applied)

    out["ATTACHED_BO_APPLIED"] = applied_values
    out["UNCOMMITTED"] = uncommitted_values
    return out

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');

html, body {
    font-family: 'Manrope', sans-serif !important;
    background: #f0f2f6 !important;
    overflow-x: hidden;
}
.block-container {
    padding-top: 0.5rem !important;
    padding-bottom: 2rem !important;
    max-width: 100% !important;
}

/* ── Topbar ── */
.omp-topbar {
    background: #fff;
    border-bottom: 1px solid #dde1ea;
    padding: 0 28px;
    height: 52px;
    display: flex;
    align-items: center;
    gap: 14px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    margin: -0.5rem -3rem 1.5rem -3rem;
}
.omp-tb-logo  { font-size: 16px; font-weight: 800; color: #1e3a5f; letter-spacing: .04em; }
.omp-tb-sep   { color: #c8cdd8; font-size: 18px; }
.omp-tb-title { font-size: 14px; color: #5c6880; font-weight: 500; }

/* ── Dropdown label ── */
.dd-label {
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: #6b7a90; margin: 0 0 3px 0;
}

/* ── Collection heading bar ── */
.coll-heading {
    font-size: 13px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: #1e3a5f;
    padding: 11px 18px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px 10px 0 0;
    margin-top: 16px;
}

/* ── Section toolbar (5 buttons above items) ── */
.sec-toolbar-wrap {
    background: #fff;
    border: 1px solid #e2e8f0;
    border-top: none;
    padding: 10px 14px;
}

/* ── All buttons: base reset ── */
div.stButton > button {
    font-family: 'Manrope', sans-serif !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    border-radius: 7px !important;
    transition: background .12s, border-color .12s, color .12s;
}

/* ── Section toolbar buttons (inactive) ── */
div.stButton > button[kind="secondary"] {
    padding: 6px 14px !important;
    border: 1px solid #d1d9e6 !important;
    background: #f1f5fb !important;
    color: #4a5568 !important;
}
div.stButton > button[kind="secondary"]:hover:not(:disabled) {
    background: #e8eef8 !important;
    border-color: #b0c0d8 !important;
    color: #1e3a5f !important;
}

/* ── Section toolbar button ACTIVE + selected item row (both use primary) ── */
div.stButton > button[kind="primary"] {
    padding: 6px 14px !important;
    background: #1e3a5f !important;
    border: 1px solid #1e3a5f !important;
    color: #ffffff !important;
    box-shadow: 0 2px 6px rgba(30,58,95,.25) !important;
}
div.stButton > button[kind="primary"]:hover {
    background: #16304f !important;
    border-color: #16304f !important;
}

/* ── Disabled buttons ── */
div.stButton > button:disabled {
    background: #f5f7fa !important;
    color: #c0cad8 !important;
    border-color: #e8edf5 !important;
    cursor: not-allowed !important;
    opacity: 0.75 !important;
}

/* ── Item row buttons ── */
div.stButton > button[kind="secondary"].item-row-btn {
    text-align: left !important;
    padding: 9px 18px !important;
    border-radius: 0 !important;
    border: none !important;
    border-bottom: 1px solid #f0f2f6 !important;
    background: #ffffff !important;
    color: #374151 !important;
    font-size: 13px !important;
    font-weight: 500 !important;
}

/* ── Expanded section panel ── */
.sec-panel {
    background: #fff;
    border: 1px solid #dde1ea;
    border-top: 3px solid #1e3a5f;
    border-radius: 0 0 8px 8px;
    padding: 16px 20px 20px 20px;
    margin-bottom: 2px;
    box-shadow: 0 3px 10px rgba(0,0,0,0.06);
}
.sec-panel-title {
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .10em; color: #6b7a90; margin-bottom: 14px;
}
.gartman-query-stamp {
    margin: -8px 0 12px 0;
    color: #8a96a8;
    font-size: 11px;
    font-weight: 600;
}

/* ── General inventory table ── */
.gen-tbl-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    background: #ffffff;
}
.gen-tbl {
    width: 100%; min-width: 700px; table-layout: fixed;
    border-collapse: separate; border-spacing: 0; font-size: 14px;
}
.gen-tbl th {
    background: #f1f5fb; padding: 7px 14px; text-align: center;
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: #6b7a90;
    border-bottom: 2px solid #d1d9e6; white-space: nowrap;
}
.gen-tbl th.row-lbl { text-align: left; width: 20%; }
.gen-tbl th.row-lbl, .gen-tbl td.row-lbl {
    position: sticky;
    left: 0;
    z-index: 2;
    box-shadow: 2px 0 4px rgba(0,0,0,0.06);
}
.gen-tbl td {
    padding: 8px 14px; text-align: right;
    border-bottom: 1px solid #edf0f5;
    color: #2d3748; font-variant-numeric: tabular-nums; white-space: nowrap;
}
.gen-tbl td.row-lbl {
    font-weight: 700; text-align: left; color: #1e3a5f;
    text-transform: uppercase; letter-spacing: .04em; font-size: 13px;
    background: #ffffff;
}
.gen-tbl td.avail  { color: #16a34a; font-weight: 700; }
.gen-tbl td.bo     { color: #c0392b; font-weight: 700; }
.gen-tbl td.zero   { color: #cbd5e1; }
.gen-tbl td.price  { color: #1e3a5f; font-weight: 700; text-align: left; }
.gen-tbl td.span-l { text-align: left; }
.gen-tbl tr:last-child td { border-bottom: none; }
.gen-tbl th.total-col { border-right: 2px solid #c8d3e6; color: #1e3a5f; width: 12%; }
.gen-tbl td.total-val {
    font-weight: 700; border-right: 2px solid #e2e8f0;
    background: #f7f9fc;
}

/* ── Arrivals table ── */
.arr-tbl-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    background: #ffffff;
}
.arr-tbl {
    width: 100%; min-width: 480px; table-layout: fixed;
    border-collapse: separate; border-spacing: 0; font-size: 14px;
}
.arr-tbl th {
    background: #f1f5fb; padding: 7px 14px; text-align: left;
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: #6b7a90;
    border-bottom: 2px solid #d1d9e6; white-space: nowrap;
}
.arr-tbl th.num { text-align: right; }
.arr-tbl th.po-col { width: 26%; }
.arr-tbl td {
    padding: 8px 14px; border-bottom: 1px solid #edf0f5;
    color: #2d3748; font-variant-numeric: tabular-nums; white-space: nowrap;
}
.arr-tbl td.po      { font-family: monospace; font-weight: 700; color: #1e3a5f; font-size: 14px; }
.arr-tbl td.num     { text-align: right; }
.arr-tbl td.uncom   { text-align: right; color: #16a34a; font-weight: 700; }
.arr-tbl td.uncom-z { text-align: right; color: #cbd5e1; }
.arr-tbl tr:last-child td { border-bottom: none; }
.arr-tbl th.po-col, .arr-tbl td.po, .arr-tbl td.arr-total-lbl {
    position: sticky;
    left: 0;
    z-index: 2;
    box-shadow: 2px 0 4px rgba(0,0,0,0.06);
}
.arr-tbl td.po { background: #ffffff; }

/* ── Arrivals totals footer row ── */
.arr-tbl tfoot tr.arr-total {
    border-top: 2px solid #d1d9e6;
    background: #f1f5fb;
}
.arr-tbl tfoot td {
    padding: 8px 14px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.arr-tbl .arr-total-lbl {
    font-weight: 700;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: .04em;
    color: #1e3a5f;
    background: #f1f5fb;
}

/* ── Sales metrics table ── */
.sales-tbl-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    background: #ffffff;
}
.sales-tbl {
    width: 100%; min-width: 560px; table-layout: fixed;
    border-collapse: separate; border-spacing: 0; font-size: 14px; margin-bottom: 4px;
}
.sales-tbl tbody tr td { padding: 11px 14px; border-bottom: 1px solid #edf0f5; vertical-align: top; }
.sales-tbl tbody tr:last-child td { border-bottom: none; }
.sales-tbl tr.sm-row-sel td { background: #eef3fb; }
.sm-row-lbl {
    font-weight: 800; font-size: 13px; letter-spacing: .06em;
    text-transform: uppercase; color: #1e3a5f; white-space: nowrap;
    width: 16%; padding-top: 14px !important;
    position: sticky; left: 0; z-index: 2;
    box-shadow: 2px 0 4px rgba(0,0,0,0.06);
    background: #ffffff;
}
.sm-cell { text-align: right; width: 21%; }
.sm-val { font-size: 18px; font-weight: 700; color: #2d3748; font-variant-numeric: tabular-nums; display: block; }
.sm-sub { font-size: 12px; color: #9aa5b8; font-weight: 500; display: block; margin-top: 2px; }

/* ── Specs panel ── */
.specs-section { margin-bottom: 18px; }
.specs-section-lbl {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .10em; color: #6b7a90;
    padding-bottom: 7px; border-bottom: 1px solid #e2e8f0; margin-bottom: 10px;
}
.specs-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.spec-card {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 9px 13px; min-width: 150px; flex: 1 1 150px;
}
.spec-card.wide { flex: 2 1 300px; }
.spec-lbl {
    font-size: 11px; color: #9aa5b8; font-weight: 700;
    text-transform: uppercase; letter-spacing: .06em; margin-bottom: 4px;
}
.spec-val { font-size: 15px; color: #1e3a5f; font-weight: 600; }
.spec-val.empty { color: #cbd5e1; font-weight: 400; font-style: italic; font-size: 14px; }
.spec-notes {
    background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px;
    padding: 10px 14px; font-size: 14px; color: #78350f; margin-top: 4px;
}
.spec-notes-lbl {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: #b45309; margin-bottom: 4px;
}

/* ── Sales chart header ── */
.sales-chart-hdr {
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: #6b7a90; margin: 14px 0 6px 0;
}

/* ── Placeholder / empty states ── */
.sec-placeholder { color: #9aa5b8; font-style: italic; font-size: 14px; padding: 6px 0; }

/* ── "Select an item" nudge ── */
.select-nudge {
    color: #9aa5b8; font-size: 14px; font-style: italic;
    padding: 10px 18px 14px 18px;
    background: #fff;
    border: 1px solid #e2e8f0;
    border-top: none;
}
</style>
"""

# ─────────────────────────────────────────────────────────────────────────────
# DB query helpers  (all cached)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=1800, show_spinner=False)
def _load_collections() -> list[str]:
    conn = connect()
    try:
        df = pd.read_sql(
            "SELECT DISTINCT TRIM(IX.IMCOLLECT) AS COLLECTION "
            "FROM GSFL2K.ITEMMAST IM "
            "JOIN GSFL2K.ITEMXTRA IX ON IX.IMXITM = IM.IMITEM "
            "WHERE IM.IMDIV IN (1, 6) AND TRIM(IX.IMCOLLECT) <> '' "
            "ORDER BY TRIM(IX.IMCOLLECT)",
            conn,
        )
        return df["COLLECTION"].tolist()
    finally:
        conn.close()


@st.cache_data(ttl=1800, show_spinner=False)
def _load_items(collection: str, exclude_discontinued: bool = True) -> pd.DataFrame:
    conn = connect()
    try:
        disc_clause = "AND COALESCE(TRIM(IM.IMDROP), '') = '' " if exclude_discontinued else ""
        return pd.read_sql(
            "SELECT TRIM(IM.IMITEM) AS ITEM_NUMBER, TRIM(IM.IMDESC) AS DESCRIPTION "
            "FROM GSFL2K.ITEMMAST IM "
            "JOIN GSFL2K.ITEMXTRA IX ON IX.IMXITM = IM.IMITEM "
            f"WHERE IM.IMDIV IN (1, 6) AND TRIM(IX.IMCOLLECT) = '{sql_escape(collection)}' "
            f"{disc_clause}"
            "ORDER BY TRIM(IM.IMITEM)",
            conn,
        )
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _load_general(item_num: str) -> tuple[pd.DataFrame, float, float, str]:
    conn = connect()
    try:
        inv = pd.read_sql(
            "SELECT IB.IBLOC, "
            "  DECIMAL(CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2 "
            "               THEN IB.IBQOH "
            "               ELSE IB.IBQOH * IM.IMFACT END, 18, 2) AS ON_HAND, "
            "  DECIMAL(CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2 "
            "               THEN COALESCE(IB.IBQOH, 0) - COALESCE(IB.IBQOO, 0) - COALESCE(IB.IBQAL, 0) "
            "               ELSE (COALESCE(IB.IBQOH, 0) - COALESCE(IB.IBQOO, 0) - COALESCE(IB.IBQAL, 0)) * IM.IMFACT "
            "               END, 18, 2) AS AVAILABLE "
            "FROM GSFL2K.ITEMBAL IB "
            "JOIN GSFL2K.ITEMMAST IM ON IM.IMITEM = IB.IBITEM "
            f"WHERE TRIM(IB.IBITEM) = '{sql_escape(item_num)}' "
            "  AND IB.IBCO = 1 "
            "  AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)",
            conn,
        )
        bo = pd.read_sql(
            "SELECT DECIMAL(COALESCE(SUM( "
            "    CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2 "
            "         THEN COALESCE(IB.IBQBO, 0) "
            "         ELSE COALESCE(IB.IBQBO, 0) * IM.IMFACT END "
            "), 0), 18, 2) AS BACK_ORDER "
            "FROM GSFL2K.ITEMBAL IB "
            "JOIN GSFL2K.ITEMMAST IM ON IM.IMITEM = IB.IBITEM "
            f"WHERE TRIM(IB.IBITEM) = '{sql_escape(item_num)}' AND IB.IBCO = 1",
            conn,
        )
        pr = pd.read_sql(
            "SELECT COALESCE(IMP1, 0) AS PREFERRED_PRICE "
            "FROM GSFL2K.ITEMMAST "
            f"WHERE TRIM(IMITEM) = '{sql_escape(item_num)}'",
            conn,
        )
        return (
            inv,
            float(bo["BACK_ORDER"].iloc[0]) if not bo.empty else 0.0,
            float(pr["PREFERRED_PRICE"].iloc[0]) if not pr.empty else 0.0,
            _gartman_query_stamp("General", item_num),
        )
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _load_arrivals(item_num: str) -> tuple[pd.DataFrame, str]:
    """
    Open PO lines for the item, sorted oldest ETW first.

    TOTAL_ON_PO  = (PLQORD - PLQREC) × IMFACT  — SF remaining to receive on this line.
    UNCOMMITTED  = TOTAL_ON_PO − attached backorders (OLPOR='Y').
    Using (PLQORD-PLQREC)×IMFACT instead of PLBLUO avoids the partial-receipt
    distortion where PLBLUR reduces the apparent uncommitted by already-received qty.
    Attached backorders are allocated once, from the earliest PO forward.
    """
    conn = connect()
    try:
        sql = (
            "WITH BOStatus AS ( "
            "    SELECT TRIM(L.OLITEM) AS ITEM_NUMBER, "
            "           SUM(CASE WHEN COALESCE(TRIM(L.OLPOR), '') = 'Y' "
            "                    THEN COALESCE(L.OLBLUB, 0) ELSE 0 END) AS ATTACHED_BO_SF "
            "    FROM GSFL2K.OOLINE L "
            "    JOIN GSFL2K.OOHEAD H "
            "        ON H.OHCO = L.OLCO AND H.OHLOC = L.OLLOC AND H.OHORD# = L.OLORD# "
            "    WHERE L.OLCO = 1 AND L.OLQBO > 0 "
            "      AND TRIM(H.OHCUST) NOT LIKE '%TRANSFER%' "
            "      AND H.OHOTYP NOT LIKE '%RA%' "
            "    GROUP BY TRIM(L.OLITEM) "
            ") "
            "SELECT "
            "    TRIM(CHAR(PL.PLPO#)) AS PO_NUMBER, "
            "    DECIMAL( "
            "        (PL.PLQORD - COALESCE(PL.PLQREC, 0)) * "
            "        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2 THEN 1 ELSE IM.IMFACT END, "
            "    18, 2) AS TOTAL_ON_PO, "
            "    DECIMAL(COALESCE(BO.ATTACHED_BO_SF, 0), 18, 2) AS ATTACHED_BO_SF, "
            "    PL.PLDDAT AS ETW_DATE "
            "FROM GSFL2K.POLINE PL "
            "JOIN GSFL2K.ITEMMAST IM ON IM.IMITEM = PL.PLITEM "
            "LEFT JOIN BOStatus BO ON BO.ITEM_NUMBER = TRIM(PL.PLITEM) "
            f"WHERE PL.PLCO = 1 AND TRIM(PL.PLITEM) = '{sql_escape(item_num)}' "
            "  AND COALESCE(TRIM(PL.PLDELT), '') <> 'D' "
            "  AND PL.PLQORD > COALESCE(PL.PLQREC, 0) "
            "  AND PL.PLDDAT IS NOT NULL "
            f"  AND COALESCE(PL.PLVEND, 0) NOT IN ({_EXCLUDED_ARRIVAL_VENDOR_SQL}) "
            "ORDER BY PL.PLDDAT ASC, TRIM(CHAR(PL.PLPO#)) ASC "
            "FOR READ ONLY"
        )
        return _apply_arrival_uncommitted(pd.read_sql(sql, conn)), _gartman_query_stamp("Arrivals", item_num)
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _load_sales(item_num: str) -> tuple[pd.DataFrame, str]:
    """Monthly SF sold for the item over the past 60 months (YR, MO, UNITS_SF)."""
    conn = connect()
    try:
        sql = (
            "SELECT YEAR(H.SHIDAT) AS YR, MONTH(H.SHIDAT) AS MO, "
            "    DECIMAL(SUM(COALESCE(L.SLBLUO, 0)), 18, 2) AS UNITS_SF "
            "FROM GSFL2K.SHHEAD H "
            "JOIN GSFL2K.SHLINE L "
            "    ON L.SLCO=H.SHCO AND L.SLLOC=H.SHLOC AND L.SLINV#=H.SHINV# AND L.SLORD#=H.SHORD# "
            "WHERE H.SHCO=1 "
            f"  AND TRIM(L.SLITEM)='{sql_escape(item_num)}' "
            "  AND H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -60) "
            "GROUP BY YEAR(H.SHIDAT), MONTH(H.SHIDAT) "
            "ORDER BY YEAR(H.SHIDAT), MONTH(H.SHIDAT) "
            "FOR READ ONLY"
        )
        return pd.read_sql(sql, conn), _gartman_query_stamp("Sales", item_num)
    finally:
        conn.close()


@st.cache_data(ttl=3600, show_spinner=False)
def _load_specs_db() -> pd.DataFrame:
    """Load the Garrison product info Excel from the SharePoint-synced folder.
    Returns a DataFrame indexed by stripped PRODUCT NUMBER, or empty if file not found."""
    matches = sorted(
        _SPECS_DIR.glob("*Garrison Product Information*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return pd.DataFrame()
    df = pd.read_excel(matches[0], sheet_name="Everything")
    df = df[df["PRODUCT NUMBER"].notna()].copy()
    df["PRODUCT NUMBER"] = df["PRODUCT NUMBER"].astype(str).str.strip()
    return df.set_index("PRODUCT NUMBER")


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    return "—" if val == 0.0 else f"{val:,.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Sales helpers
# ─────────────────────────────────────────────────────────────────────────────

def _shift_month(year: int, month: int, n: int) -> tuple[int, int]:
    """Shift (year, month) by n months; negative n goes backward."""
    m = (month - 1) + n
    return year + m // 12, m % 12 + 1


def _get_mo(df: pd.DataFrame, year: int, month: int) -> float:
    rows = df[(df["YR"] == year) & (df["MO"] == month)]
    return float(rows["UNITS_SF"].sum()) if not rows.empty else 0.0


def _sales_metrics(df: pd.DataFrame) -> dict:
    today = date.today()
    cy, cm = today.year, today.month
    days_in_mo  = _cal.monthrange(cy, cm)[1]
    days_elapsed = max(today.day, 1)

    mtd      = _get_mo(df, cy, cm)
    expected = round(mtd * days_in_mo / days_elapsed, 0)
    m1y, m1m = _shift_month(cy, cm, -1)
    m2y, m2m = _shift_month(cy, cm, -2)

    quarters, q_subs = [], []
    for q in range(4):
        total = sum(_get_mo(df, *_shift_month(cy, cm, -(q * 3 + 1 + i))) for i in range(3))
        quarters.append(total)
        ey, em = _shift_month(cy, cm, -(q * 3 + 1))
        sy, sm = _shift_month(cy, cm, -(q * 3 + 3))
        q_subs.append(f"{_cal.month_abbr[sm]}–{_cal.month_abbr[em]} {ey}")

    ytd = sum(_get_mo(df, cy, m) for m in range(1, cm + 1))
    py  = [sum(_get_mo(df, cy - y, m) for m in range(1, 13)) for y in range(1, 4)]

    return {
        "Months": {
            "values":    [mtd, expected, _get_mo(df, m1y, m1m), _get_mo(df, m2y, m2m)],
            "sublabels": [
                "MTD",
                f"Expected {_cal.month_abbr[cm]}",
                f"{_cal.month_abbr[m1m]} {m1y}",
                f"{_cal.month_abbr[m2m]} {m2y}",
            ],
        },
        "Quarters": {
            "values":    quarters,
            "sublabels": q_subs,
        },
        "Years": {
            "values":    [ytd] + py,
            "sublabels": [f"YTD {cy}", str(cy - 1), str(cy - 2), str(cy - 3)],
        },
    }


def _sales_graph_data(df: pd.DataFrame, section: str) -> tuple[list, list]:
    today = date.today()
    cy, cm = today.year, today.month

    if section == "Months":
        labels, values = [], []
        for i in range(23, -1, -1):
            yr, mo = _shift_month(cy, cm, -i)
            labels.append(f"{_cal.month_abbr[mo]} {yr}")
            values.append(_get_mo(df, yr, mo))
        return labels, values

    if section == "Quarters":
        labels, values = [], []
        for q in range(7, -1, -1):  # oldest → newest
            total = sum(_get_mo(df, *_shift_month(cy, cm, -(q * 3 + 1 + i))) for i in range(3))
            ey, em = _shift_month(cy, cm, -(q * 3 + 1))
            sy, sm = _shift_month(cy, cm, -(q * 3 + 3))
            labels.append(f"{_cal.month_abbr[sm]}–{_cal.month_abbr[em]} {ey}")
            values.append(total)
        return labels, values

    # Years: 3 prior full years + YTD current
    labels, values = [], []
    for y in range(3, 0, -1):
        yr = cy - y
        labels.append(str(yr))
        values.append(sum(_get_mo(df, yr, m) for m in range(1, 13)))
    labels.append(f"YTD {cy}")
    values.append(sum(_get_mo(df, cy, m) for m in range(1, cm + 1)))
    return labels, values


# ─────────────────────────────────────────────────────────────────────────────
# Section renderers
# ─────────────────────────────────────────────────────────────────────────────

def _render_general(item_num: str) -> None:
    with st.spinner("Loading inventory…"):
        inv_df, back_order, price, queried_at = _load_general(item_num)

    loc_data: dict[int, dict[str, float]] = {
        loc: {"on_hand": 0.0, "available": 0.0} for loc in _LOCS
    }
    for _, row in inv_df.iterrows():
        loc = int(row["IBLOC"])
        if loc in loc_data:
            loc_data[loc]["on_hand"]   = float(row["ON_HAND"])
            loc_data[loc]["available"] = float(row["AVAILABLE"])

    loc_headers = "".join(f"<th>Loc&nbsp;{loc}</th>" for loc in _LOCS)
    n_cols = len(_LOCS)

    total_oh    = sum(loc_data[loc]["on_hand"]   for loc in _LOCS)
    total_avail = sum(loc_data[loc]["available"] for loc in _LOCS)

    def _cells(field: str, active_cls: str = "") -> str:
        return "".join(
            f'<td class="{"zero" if loc_data[loc][field] == 0.0 else active_cls}">'
            f'{_fmt(loc_data[loc][field])}</td>'
            for loc in _LOCS
        )

    bo_cls = "bo" if back_order > 0 else "zero"

    st.markdown(f"""
<div class="sec-panel">
  <div class="sec-panel-title">General — Inventory Overview</div>
  {_query_stamp_html(queried_at)}
  <div class="gen-tbl-wrap">
  <table class="gen-tbl">
    <thead><tr>
      <th class="row-lbl"></th>
      <th class="total-col">Total</th>
      {loc_headers}
    </tr></thead>
    <tbody>
      <tr>
        <td class="row-lbl">On Hand</td>
        <td class="total-val">{_fmt(total_oh)}</td>
        {_cells("on_hand")}
      </tr>
      <tr>
        <td class="row-lbl">Available</td>
        <td class="total-val avail">{_fmt(total_avail)}</td>
        {_cells("available", "avail")}
      </tr>
      <tr>
        <td class="row-lbl">Back Order</td>
        <td class="total-val {bo_cls}">{_fmt(back_order)}</td>
        <td colspan="{n_cols}"></td>
      </tr>
      <tr>
        <td class="row-lbl">Preferred Price</td>
        <td class="total-val price">${price:,.2f}</td>
        <td colspan="{n_cols}"></td>
      </tr>
    </tbody>
  </table>
  </div>
</div>""", unsafe_allow_html=True)


def _render_arrivals(item_num: str) -> None:
    with st.spinner("Loading PO arrivals…"):
        df, queried_at = _load_arrivals(item_num)

    if df.empty:
        st.markdown(
            '<div class="sec-panel">'
            '<div class="sec-panel-title">Arrivals — Open Purchase Orders</div>'
            f'{_query_stamp_html(queried_at)}'
            '<p class="sec-placeholder">No open purchase orders for this item.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    rows_html = "".join(
        f'<tr>'
        f'<td class="po">{r["PO_NUMBER"]}</td>'
        f'<td class="num">{_fmt(float(r["TOTAL_ON_PO"]))}</td>'
        f'<td class="{"uncom" if float(r["UNCOMMITTED"]) > 0 else "uncom-z"}">'
        f'{_fmt(float(r["UNCOMMITTED"]))}</td>'
        f'<td>{pd.Timestamp(r["ETW_DATE"]).strftime("%m/%d/%Y") if pd.notna(r["ETW_DATE"]) else "—"}</td>'
        f'</tr>'
        for _, r in df.iterrows()
    )

    total_po     = df["TOTAL_ON_PO"].sum()
    total_uncom  = df["UNCOMMITTED"].sum()
    uncom_cls    = "uncom" if total_uncom > 0 else "uncom-z"

    st.markdown(f"""
<div class="sec-panel">
  <div class="sec-panel-title">Arrivals — Open Purchase Orders</div>
  {_query_stamp_html(queried_at)}
  <div class="arr-tbl-wrap">
  <table class="arr-tbl">
    <thead><tr>
      <th class="po-col">PO #</th>
      <th class="num">Total on PO (SF)</th>
      <th class="num">Uncommitted (SF)</th>
      <th>Arrival Date (ETW)</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
    <tfoot><tr class="arr-total">
      <td class="arr-total-lbl">Total</td>
      <td class="num">{_fmt(float(total_po))}</td>
      <td class="{uncom_cls}">{_fmt(float(total_uncom))}</td>
      <td></td>
    </tr></tfoot>
  </table>
  </div>
</div>""", unsafe_allow_html=True)


def _set_sales_section(sgs_key: str, section: str) -> None:
    st.session_state[sgs_key] = section


def _render_sales(item_num: str) -> None:
    with st.spinner("Loading sales data…"):
        df, queried_at = _load_sales(item_num)

    sgs_key = f"sgs_{item_num}"
    if sgs_key not in st.session_state:
        st.session_state[sgs_key] = "Months"

    active_sub = st.session_state[sgs_key]
    metrics    = _sales_metrics(df)

    def _row_html(section_name: str) -> str:
        is_sel  = active_sub == section_name
        row_cls = "sm-row-sel" if is_sel else ""
        m       = metrics[section_name]
        cells   = "".join(
            f'<td class="sm-cell">'
            f'<span class="sm-val">{_fmt(v)}</span>'
            f'<span class="sm-sub">{lbl}</span>'
            f'</td>'
            for v, lbl in zip(m["values"], m["sublabels"])
        )
        return f'<tr class="{row_cls}"><td class="sm-row-lbl">{section_name}</td>{cells}</tr>'

    table_html = (
        '<div class="sales-tbl-wrap"><table class="sales-tbl"><tbody>'
        + _row_html("Months")
        + _row_html("Quarters")
        + _row_html("Years")
        + "</tbody></table></div>"
    )

    st.markdown(
        f'<div class="sec-panel">'
        f'<div class="sec-panel-title">Sales — Historical Performance</div>'
        f'{_query_stamp_html(queried_at)}'
        f'{table_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Chart sub-section selector
    st.markdown('<p class="sales-chart-hdr">Chart view</p>', unsafe_allow_html=True)
    btn_cols = st.columns([1, 1, 1, 5])
    for i, sub in enumerate(["Months", "Quarters", "Years"]):
        with btn_cols[i]:
            st.button(
                sub,
                key=f"sgs_btn_{item_num}_{sub}",
                on_click=_set_sales_section,
                args=(sgs_key, sub),
                type="primary" if active_sub == sub else "secondary",
                use_container_width=True,
            )

    labels, values = _sales_graph_data(df, active_sub)
    fig = go.Figure(
        go.Scatter(
            x=labels,
            y=values,
            mode="lines+markers",
            line=dict(color="#1e3a5f", width=2.5),
            marker=dict(size=6, color="#1e3a5f"),
            fill="tozeroy",
            fillcolor="rgba(30,58,95,0.07)",
        )
    )
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(
            type="category",
            showgrid=False,
            showline=True,
            linecolor="#dde1ea",
            tickfont=dict(size=12, color="#6b7a90"),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#edf0f5",
            showline=False,
            tickfont=dict(size=12, color="#6b7a90"),
            tickformat=",.0f",
        ),
        showlegend=False,
        font=dict(family="Manrope, sans-serif"),
    )
    st.plotly_chart(
        fig,
        key=f"sales_chart_{item_num}",
        height=260,
        config={"displayModeBar": False},
    )


def _render_specs(item_num: str) -> None:
    with st.spinner("Loading specs…"):
        db = _load_specs_db()

    if db.empty:
        st.markdown(
            '<div class="sec-panel"><div class="sec-panel-title">Specs</div>'
            '<p class="sec-placeholder">Spec file not found in Dashboard Files folder.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    if item_num not in db.index:
        st.markdown(
            '<div class="sec-panel"><div class="sec-panel-title">Specs</div>'
            f'<p class="sec-placeholder">No spec data found for {item_num}.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    row = db.loc[item_num]
    # If multiple rows match (same product number), take the first
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]

    def _val(col: str) -> str:
        v = row.get(col, None)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip()

    def _card(label: str, value: str, wide: bool = False) -> str:
        cls = "spec-card wide" if wide else "spec-card"
        if value:
            return f'<div class="{cls}"><div class="spec-lbl">{label}</div><div class="spec-val">{value}</div></div>'
        return f'<div class="{cls}"><div class="spec-lbl">{label}</div><div class="spec-val empty">—</div></div>'

    sections_html = ""
    for group_name, fields in _SPEC_GROUPS.items():
        cards = ""
        for col, lbl in fields:
            wide = col in ("CONSTRUCTION", "INSTALLATION METHODS",
                           "STRUCTURE RESIDENTIAL", "FINISH RESIDENTIAL",
                           "STRUCTURE COMMERCIAL", "FINISH COMMERICAL")
            cards += _card(lbl, _val(col), wide=wide)
        sections_html += (
            f'<div class="specs-section">'
            f'<div class="specs-section-lbl">{group_name}</div>'
            f'<div class="specs-grid">{cards}</div>'
            f'</div>'
        )

    notes = _val("NOTES")
    notes_html = ""
    if notes:
        notes_html = (
            f'<div class="spec-notes">'
            f'<div class="spec-notes-lbl">Notes</div>'
            f'{notes}'
            f'</div>'
        )

    subtitle = _val("COLOR")

    st.markdown(
        f'<div class="sec-panel">'
        f'<div class="sec-panel-title">Specs'
        f'{"  —  " + subtitle if subtitle else ""}'
        f'</div>'
        f'{sections_html}'
        f'{notes_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_placeholder(section: str) -> None:
    st.markdown(
        f'<div class="sec-panel">'
        f'<div class="sec-panel-title">{section}</div>'
        f'<p class="sec-placeholder">This section is coming soon.</p>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Session-state callbacks
# ─────────────────────────────────────────────────────────────────────────────

def _select_item(num: str) -> None:
    """Click a row: select it, or deselect if already selected."""
    if st.session_state.get("selected_item") == num:
        st.session_state.selected_item = None
    else:
        st.session_state.selected_item = num


def _select_section(section: str) -> None:
    """Click a section button: activate it, or deactivate if already active."""
    if st.session_state.get("active_section") == section:
        st.session_state.active_section = None
    else:
        st.session_state.active_section = section


def _on_collection_change() -> None:
    st.session_state.selected_item  = None
    st.session_state.active_section = None


def _on_filter_change() -> None:
    st.session_state.selected_item  = None
    st.session_state.active_section = None


# ─────────────────────────────────────────────────────────────────────────────
# Main render
# ─────────────────────────────────────────────────────────────────────────────

def render_sales_dashboard() -> None:
    if "selected_item"    not in st.session_state:
        st.session_state.selected_item    = None
    if "active_section"   not in st.session_state:
        st.session_state.active_section   = None

    st.markdown(_CSS, unsafe_allow_html=True)

    # ── Topbar ──────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="omp-topbar">'
        '<span class="omp-tb-logo">OMP</span>'
        '<span class="omp-tb-sep">|</span>'
        '<span class="omp-tb-title">Sales Dashboard</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Dropdowns ───────────────────────────────────────────────────────────
    dd_col, _ = st.columns([1, 1])
    with dd_col:
        st.markdown('<p class="dd-label">Flooring Collection</p>', unsafe_allow_html=True)
        try:
            with st.spinner("Loading collections…"):
                collections = _load_collections()
        except Exception as exc:
            st.error(f"Could not load collections: {exc}")
            return

        collection = st.selectbox(
            label="collection",
            options=["— Select a collection —"] + collections,
            index=0,
            label_visibility="collapsed",
            key="dd_collection",
            on_change=_on_collection_change,
        )

    # ── Nothing selected yet ─────────────────────────────────────────────────
    if collection == "— Select a collection —":
        st.markdown(
            '<div style="margin-top:3.5rem;text-align:center;color:#9aa5b8;font-size:14px;">'
            'Select a collection above to view its items.'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Load items ───────────────────────────────────────────────────────────
    excl_disc = st.session_state.get("excl_discontinued", True)
    try:
        with st.spinner("Loading items…"):
            items_df = _load_items(collection, excl_disc)
    except Exception as exc:
        st.error(f"Could not load items: {exc}")
        return

    if items_df.empty:
        st.warning(f"No items found for collection **{collection}**.")
        return

    selected_item  = st.session_state.get("selected_item")
    active_section = st.session_state.get("active_section")

    n = len(items_df)

    # ── Collection heading ───────────────────────────────────────────────────
    st.markdown(
        f'<div class="coll-heading">'
        f'{collection}&nbsp;&nbsp;·&nbsp;&nbsp;{n} item{"s" if n != 1 else ""}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Section toolbar ──────────────────────────────────────────────────────
    st.markdown('<div class="sec-toolbar-wrap">', unsafe_allow_html=True)
    tb_cols = st.columns(len(_SECTIONS))
    for i, section in enumerate(_SECTIONS):
        is_active  = active_section == section
        is_ready   = section in _READY
        with tb_cols[i]:
            st.button(
                section,
                key=f"sec_{section}",
                on_click=_select_section if is_ready else None,
                args=(section,) if is_ready else None,
                disabled=not is_ready,
                use_container_width=True,
                type="primary" if is_active else "secondary",
            )
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Discontinued filter ──────────────────────────────────────────────────
    st.checkbox(
        "Exclude Discontinued Items",
        value=True,
        key="excl_discontinued",
        on_change=_on_filter_change,
    )

    # ── Items list ───────────────────────────────────────────────────────────
    for _, item in items_df.iterrows():
        num  = item["ITEM_NUMBER"]
        desc = item["DESCRIPTION"]
        is_selected = selected_item == num

        row_col, _ = st.columns([1, 1])
        with row_col:
            st.button(
                f"{num}   —   {desc}",
                key=f"row_{num}",
                on_click=_select_item,
                args=(num,),
                use_container_width=True,
                type="primary" if is_selected else "secondary",
            )

        # Expanded detail panel — rendered right below this item's row, full width
        if is_selected and active_section:
            if active_section == "General":
                _render_general(num)
            elif active_section == "Arrivals":
                _render_arrivals(num)
            elif active_section == "Sales":
                _render_sales(num)
            elif active_section == "Specs":
                _render_specs(num)
            else:
                _render_placeholder(active_section)

    # Nudge when an item is selected but no section button pressed yet
    if selected_item and not active_section:
        st.markdown(
            '<div class="select-nudge">'
            'Click a section button above (General, Arrivals…) to view details.'
            '</div>',
            unsafe_allow_html=True,
        )


_V2_SPEC_FIELDS = [
    ("THICKNESS", "Thickness"),
    ("WIDTH", "Width"),
    ("LENGTH", "Length"),
    ("WEAR LAYER", "Wear Layer"),
    ("SPECIES", "Species"),
    ("GRADE", "Grade"),
    ("FINISH", "Finish"),
    ("TEXTURE", "Texture"),
    ("EDGE DETAIL", "Edge Detail"),
    ("CONSTRUCTION", "Construction"),
    ("SQF / BOX", "SF / Box"),
    ("LBS / BOX", "Lbs / Box"),
    ("PIECES/BOX", "Pieces / Box"),
    ("STRUCTURE RESIDENTIAL", "Structure Warranty"),
    ("FINISH RESIDENTIAL", "Finish Warranty"),
]

_V2_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');

html, body {
    font-family: 'Manrope', sans-serif !important;
    background: #f3f4f6 !important;
    overflow-x: hidden;
    font-size: 18px;
}
.block-container {
    padding-top: 0.35rem !important;
    padding-bottom: 2rem !important;
    max-width: 100% !important;
}
.v2-topbar {
    background: #ffffff;
    border-bottom: 1px solid #d9dee8;
    height: 48px;
    margin: -0.35rem -3rem 1rem -3rem;
    padding: 0 28px;
    display: flex;
    align-items: center;
    gap: 12px;
}
.v2-logo { font-size: 18px; font-weight: 800; letter-spacing: .04em; color: #111827; }
.v2-title { font-size: 15px; color: #596579; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.v2-label {
    font-size: 16px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #5b6575;
    margin: 0 0 4px 0;
}
div[data-testid="stSelectbox"] * {
    font-size: 18px !important;
}
div[data-testid="stCheckbox"] label,
div[data-testid="stCheckbox"] label * {
    font-size: 18px !important;
}
div.stButton > button {
    font-family: 'Manrope', sans-serif !important;
    border-radius: 4px !important;
    font-weight: 800 !important;
    font-size: 17px !important;
}
.v2-report {
    background: #ffffff;
    border: 2px solid #111827;
    margin-top: 12px;
    overflow-x: auto;
}
.v2-table-scroll {
    width: 100%;
    overflow-x: visible;
}
.v2-spec-panel {
    border-bottom: 2px solid #111827;
}
.v2-spec-head {
    display: grid;
    grid-template-columns: minmax(220px, 1.2fr) minmax(420px, 2fr) minmax(90px, .45fr);
    min-height: 82px;
}
.v2-spec-main,
.v2-spec-mid,
.v2-spec-count {
    padding: 12px 18px;
    display: flex;
    flex-direction: column;
    justify-content: center;
}
.v2-spec-main,
.v2-spec-mid {
    border-right: 2px solid #111827;
}
.v2-kicker {
    font-size: 14px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #5b6575;
}
.v2-spec-name {
    font-size: 32px;
    line-height: 1.15;
    margin-top: 5px;
    font-weight: 800;
    color: #111827;
    text-transform: uppercase;
}
.v2-spec-subtitle {
    color: #111827;
    font-size: 21px;
    font-weight: 800;
    line-height: 1.5;
    text-align: center;
}
.v2-spec-count {
    text-align: center;
    font-size: 18px;
    font-weight: 800;
    text-transform: uppercase;
    color: #111827;
}
.v2-spec-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
    border-top: 2px solid #111827;
}
.v2-spec-cell {
    min-height: 66px;
    padding: 11px 13px;
    border-right: 1px solid #111827;
    border-bottom: 1px solid #111827;
}
.v2-spec-cell:last-child { border-right: none; }
.v2-spec-lbl {
    font-size: 13px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: #667085;
}
.v2-spec-val {
    margin-top: 3px;
    color: #111827;
    font-size: 20px;
    font-weight: 800;
}
.v2-controls {
    margin: 10px 0 0 0;
    display: flex;
    gap: 8px;
    align-items: center;
}
.v2-table {
    width: 100%;
    min-width: 860px;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 20px;
    line-height: 1.28;
}
.v2-table th,
.v2-table td {
    border-right: 1px solid #111827;
    border-bottom: 1px solid #111827;
    padding: 11px 12px;
    vertical-align: middle;
}
.v2-table th:last-child,
.v2-table td:last-child { border-right: none; }
.v2-table th {
    background: #d9d9d9;
    color: #111827;
    text-align: center;
    text-transform: uppercase;
    font-size: 17px;
    font-weight: 800;
    letter-spacing: .03em;
}
.v2-table th.v2-bo-head { background: #ff5b5b; }
.v2-table th.v2-sales-head { background: #d8eefd; }
.v2-table th.v2-arr-head { background: #e7f6d5; }
.v2-main-table { min-width: 1180px; }
.v2-item-col { width: 21%; }
.v2-num-col { width: 11%; }
.v2-sales-col { width: 15%; }
.v2-arr-col { width: 31%; }
.v2-collapsed-col { width: 10%; color: #98a2b3; }
.v2-item {
    color: #111827;
    font-weight: 800;
    text-transform: uppercase;
    font-size: 22px;
}
.v2-desc {
    color: #475467;
    font-size: 18px;
    font-weight: 600;
    margin-top: 2px;
}
.v2-num {
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.v2-zero { color: #c0c7d2; }
.v2-on-hand { color: #111827 !important; font-weight: 800; }
.v2-bo { color: #b42318; font-weight: 800; }
.v2-available { color: #087443; font-weight: 800; }
.v2-muted { color: #98a2b3; text-align: center; font-weight: 700; }
.v2-cell-details summary {
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 8px;
    border: 1px solid #cfd6e0;
    border-radius: 3px;
    background: #f8fafc;
    color: #111827;
    font-weight: 800;
    font-size: 14px;
}
.v2-cell-details summary::-webkit-details-marker { display: none; }
.v2-cell-details summary::before {
    content: "+";
    width: 14px;
    height: 14px;
    line-height: 13px;
    text-align: center;
    border: 1px solid #98a2b3;
    border-radius: 2px;
    color: #344054;
    flex: 0 0 14px;
}
.v2-cell-details[open] summary::before { content: "-"; }
.v2-summary-val {
    margin-left: auto;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.v2-summary-label {
    color: #667085;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .04em;
}
.v2-mini-table {
    width: 100%;
    margin-top: 6px;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 18px;
}
.v2-mini-table th,
.v2-mini-table td {
    border: 1px solid #d0d5dd;
    padding: 9px 10px;
}
.v2-mini-table th {
    background: #f2f4f7;
    color: #475467;
    font-size: 16px;
}
.v2-grid {
    min-width: 1230px;
}
.v2-grid .v2-item-col,
.v2-grid .v2-num-col,
.v2-grid .v2-sales-col,
.v2-grid .v2-arr-col {
    width: auto;
}
.v2-grid-head,
.v2-row-grid {
    display: grid;
    grid-template-columns: minmax(250px, 1.15fr) minmax(150px, .72fr) minmax(150px, .72fr) minmax(150px, .72fr) minmax(220px, .95fr) minmax(310px, 1.35fr);
}
.v2-grid-head > div,
.v2-row-grid > div {
    border-right: 1px solid #111827;
    border-bottom: 1px solid #111827;
    padding: 14px 14px;
    min-height: 74px;
    display: flex;
    flex-direction: column;
    justify-content: center;
}
.v2-grid-head > div:last-child,
.v2-row-grid > div:last-child {
    border-right: none;
}
.v2-grid-head > div {
    min-height: 48px;
    background: #d9d9d9;
    color: #111827;
    align-items: center;
    text-align: center;
    text-transform: uppercase;
    font-size: 16px;
    font-weight: 800;
    letter-spacing: .03em;
}
.v2-grid-head .v2-bo-head { background: #ff5b5b; }
.v2-grid-head .v2-sales-head { background: #d8eefd; }
.v2-grid-head .v2-arr-head { background: #e7f6d5; }
.v2-sort-btn {
    appearance: none;
    border: 2px solid transparent;
    border-radius: 6px;
    background: transparent;
    color: inherit;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    font: inherit;
    font-weight: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    padding: 5px 8px;
    min-height: 30px;
}
.v2-sort-btn:hover {
    border-color: #64748b;
    background: rgba(255, 255, 255, .55);
}
.v2-sort-btn:focus-visible {
    outline: 2px solid #1d4ed8;
    outline-offset: 3px;
}
.v2-sort-btn[data-dir] {
    background: #102a4c;
    border-color: #102a4c;
    color: #ffffff;
    box-shadow: 0 0 0 2px #f59e0b inset;
}
.v2-sort-mark {
    width: 24px;
    height: 24px;
    border-radius: 4px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: #eef2f7;
    color: #64748b;
    font-size: 16px;
    font-weight: 900;
    line-height: 1;
}
.v2-sort-mark::after { content: "\\2195"; }
.v2-sort-btn[data-dir] .v2-sort-mark {
    background: #f59e0b;
    color: #111827;
}
.v2-sort-btn[data-dir="asc"] .v2-sort-mark::after { content: "\\25B2"; }
.v2-sort-btn[data-dir="desc"] .v2-sort-mark::after { content: "\\25BC"; }
.v2-row-shell {
    border-bottom: 1px solid #111827;
}
.v2-row-details > summary {
    list-style: none;
    cursor: pointer;
}
.v2-row-details > summary::-webkit-details-marker {
    display: none;
}
.v2-row-details[open] .v2-row-grid > div {
    border-bottom: 2px solid #111827;
}
.v2-item-cell {
    gap: 6px;
}
.v2-expand-line {
    display: flex;
    align-items: center;
    gap: 10px;
}
.v2-expand-badge {
    width: 28px;
    height: 28px;
    border: 2px solid #64748b;
    border-radius: 4px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
    line-height: 1;
    font-weight: 800;
    color: #334155;
    flex: 0 0 28px;
}
.v2-row-details[open] .v2-expand-badge::before { content: "-"; }
.v2-row-details:not([open]) .v2-expand-badge::before { content: "+"; }
.v2-grid-num {
    align-items: flex-end;
    text-align: right;
    font-size: 24px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.v2-grid-sales,
.v2-grid-arrivals {
    background: #fbfdff;
}
.v2-cell-label {
    color: #64748b;
    font-size: 16px;
    text-transform: uppercase;
    letter-spacing: .04em;
    font-weight: 800;
}
.v2-cell-value {
    color: #111827;
    font-size: 25px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
}
.v2-row-expanded {
    padding: 14px;
    background: #ffffff;
    border-bottom: 2px solid #111827;
}
.v2-expanded-grid {
    display: grid;
    grid-template-columns: minmax(320px, .85fr) minmax(620px, 1.65fr);
    gap: 14px;
    align-items: start;
}
.v2-detail-card {
    border: 1px solid #111827;
    background: #ffffff;
    overflow: hidden;
}
.v2-detail-card.inventory {
    grid-column: 1 / -1;
}
.v2-detail-title {
    padding: 10px 12px;
    background: #f2f4f7;
    border-bottom: 1px solid #111827;
    color: #111827;
    font-size: 17px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .05em;
}
.v2-detail-card.sales .v2-detail-title { background: #d8eefd; }
.v2-detail-card.arrivals .v2-detail-title { background: #e7f6d5; }
.v2-detail-empty {
    padding: 12px;
    color: #94a3b8;
    font-size: 18px;
    font-weight: 800;
    text-align: center;
}
.v2-sales-metrics {
    width: 100%;
    display: grid;
    grid-template-columns: 1fr;
}
.v2-sales-metric {
    display: grid;
    grid-template-columns: minmax(0, 58%) minmax(0, 42%);
}
.v2-sales-label,
.v2-sales-value {
    border-right: 1px solid #d0d5dd;
    border-bottom: 1px solid #d0d5dd;
    padding: 11px 12px;
    min-height: 42px;
    display: flex;
    align-items: center;
}
.v2-sales-label {
    background: #f2f4f7;
    color: #475467;
    font-size: 16px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .03em;
}
.v2-sales-value {
    justify-content: flex-end;
    color: #111827;
    font-size: 18px;
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    border-right: none;
    white-space: nowrap;
}
.v2-arrivals-detail-grid {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(92px, .8fr) minmax(120px, 1fr) minmax(130px, 1fr) minmax(130px, 1fr);
}
.v2-arrivals-detail-grid > div {
    border-right: 1px solid #d0d5dd;
    border-bottom: 1px solid #d0d5dd;
    padding: 9px 10px;
    min-height: 42px;
    display: flex;
    align-items: center;
}
.v2-arrivals-detail-grid > div:nth-child(4n) {
    border-right: none;
}
.v2-arr-head-cell {
    justify-content: center;
    background: #f2f4f7;
    color: #475467;
    font-size: 16px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .03em;
    text-align: center;
}
.v2-arr-cell {
    color: #111827;
    font-size: 18px;
    font-variant-numeric: tabular-nums;
}
.v2-arr-po {
    font-size: 20px;
    font-weight: 800;
}
.v2-arr-num {
    justify-content: flex-end;
    text-align: right;
}
.v2-inv-table {
    min-width: 1120px;
}
.v2-detail-card.sales .v2-mini-table,
.v2-detail-card.arrivals .v2-mini-table {
    min-width: 0;
    table-layout: auto;
}
.v2-detail-card.sales .v2-mini-table th {
    width: 58%;
    text-align: left;
}
.v2-detail-card.arrivals .v2-mini-table th,
.v2-detail-card.arrivals .v2-mini-table td {
    white-space: nowrap;
}
.v2-stamp {
    color: #667085;
    font-size: 12px;
    font-weight: 600;
    margin: 6px 0 0 0;
}
.v2-report-stamp {
    padding: 8px 12px;
}
.v2-scroll-stamp {
    display: none;
}
.v2-arrivals {
    background: #ffffff;
    border: 2px solid #111827;
    border-top: none;
    overflow-x: auto;
}
.v2-arrivals-title {
    padding: 9px 12px;
    background: #e7f6d5;
    border-bottom: 2px solid #111827;
    color: #111827;
    font-size: 14px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .06em;
}
.v2-empty {
    padding: 14px 16px;
    color: #667085;
    font-size: 15px;
    font-weight: 600;
}
@media (min-width: 769px) and (max-width: 1400px) {
    .v2-row-expanded {
        position: sticky;
        left: 0;
        width: calc(100vw - 184px);
        max-width: calc(100vw - 184px);
        box-sizing: border-box;
    }
    .v2-expanded-grid {
        width: 100%;
        min-width: 0;
    }
    .v2-detail-card.sales,
    .v2-detail-card.arrivals {
        min-width: 0;
    }
}
@media (min-width: 769px) and (max-width: 1180px) {
    .v2-expanded-grid {
        grid-template-columns: minmax(0, 1fr);
    }
    .v2-detail-card.inventory {
        grid-column: auto;
    }
    .v2-detail-card.sales .v2-mini-table,
    .v2-detail-card.arrivals .v2-mini-table {
        width: 100%;
        table-layout: fixed;
    }
    .v2-detail-card.arrivals .v2-mini-table th,
    .v2-detail-card.arrivals .v2-mini-table td {
        white-space: normal;
        overflow-wrap: normal;
        word-break: normal;
    }
    .v2-detail-card.arrivals .v2-mini-table th {
        font-size: 14px;
        letter-spacing: 0;
    }
    .v2-detail-card.arrivals .v2-mini-table .v2-item {
        font-size: 17px;
    }
    .v2-arrivals-detail-grid {
        grid-template-columns: minmax(82px, .75fr) minmax(110px, 1fr) minmax(118px, 1fr) minmax(118px, 1fr);
    }
    .v2-arr-head-cell {
        font-size: 14px;
        letter-spacing: 0;
    }
    .v2-arr-cell {
        font-size: 16px;
    }
    .v2-arr-po {
        font-size: 17px;
    }
}
@media (max-width: 768px) {
    html, body {
        overflow-x: hidden;
        font-size: 17px;
    }
    .block-container {
        padding: 0.25rem 0.5rem 1.25rem 0.5rem !important;
        max-width: 100vw !important;
    }
    .v2-topbar {
        height: auto;
        min-height: 52px;
        margin: -0.25rem -0.5rem 0.75rem -0.5rem;
        padding: 8px 12px;
        flex-wrap: wrap;
        gap: 4px 10px;
    }
    .v2-logo {
        font-size: 20px;
    }
    .v2-title {
        font-size: 12px;
        line-height: 1.2;
    }
    div[data-testid="stHorizontalBlock"] {
        gap: 0.55rem !important;
        flex-wrap: wrap !important;
    }
    div[data-testid="column"] {
        width: 100% !important;
        min-width: 100% !important;
        flex: 1 1 100% !important;
    }
    .v2-label {
        font-size: 15px;
        margin: 4px 0 5px 0;
    }
    div[data-testid="stSelectbox"] [data-baseweb="select"] {
        min-height: 58px !important;
    }
    div[data-testid="stSelectbox"] [data-baseweb="select"] > div {
        min-height: 58px !important;
        align-items: center !important;
    }
    div[data-testid="stSelectbox"] * {
        font-size: 20px !important;
    }
    div[data-baseweb="popover"] [role="option"],
    [role="listbox"] [role="option"] {
        min-height: 50px !important;
        font-size: 19px !important;
    }
    div[data-testid="stCheckbox"] label,
    div[data-testid="stCheckbox"] label * {
        font-size: 18px !important;
    }
    .v2-report {
        width: calc(100vw - 16px);
        max-width: calc(100vw - 16px);
        margin-top: 10px;
        overflow-x: hidden;
    }
    .v2-table-scroll {
        max-height: none;
        overflow-x: auto;
        overflow-y: visible;
        -webkit-overflow-scrolling: touch;
        overscroll-behavior-x: contain;
        touch-action: pan-x pan-y;
        scrollbar-color: #102a4c #e5e7eb;
        scrollbar-width: auto;
    }
    .v2-table-scroll::-webkit-scrollbar {
        height: 14px;
    }
    .v2-table-scroll::-webkit-scrollbar-track {
        background: #e5e7eb;
    }
    .v2-table-scroll::-webkit-scrollbar-thumb {
        background: #102a4c;
        border-radius: 999px;
        border: 3px solid #e5e7eb;
    }
    .v2-spec-head {
        grid-template-columns: 1fr;
        min-height: 0;
    }
    .v2-spec-main,
    .v2-spec-mid,
    .v2-spec-count {
        padding: 10px 12px;
    }
    .v2-spec-main,
    .v2-spec-mid {
        border-right: none;
        border-bottom: 1px solid #111827;
    }
    .v2-kicker {
        font-size: 12px;
    }
    .v2-spec-name {
        font-size: 25px;
    }
    .v2-spec-subtitle {
        font-size: 17px;
        text-align: left;
        line-height: 1.35;
    }
    .v2-spec-count {
        align-items: flex-start;
        text-align: left;
        font-size: 15px;
    }
    .v2-spec-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .v2-spec-cell {
        min-height: 58px;
        padding: 9px 10px;
    }
    .v2-spec-lbl {
        font-size: 11px;
    }
    .v2-spec-val {
        font-size: 17px;
    }
    .v2-grid,
    .v2-main-table {
        min-width: 1060px;
    }
    .v2-grid-head,
    .v2-row-grid {
        grid-template-columns: 220px 128px 128px 128px 188px 268px;
    }
    .v2-grid-head {
        position: static;
        top: auto;
        z-index: auto;
    }
    .v2-grid-head > div,
    .v2-row-grid > div {
        min-height: 74px;
        padding: 10px 10px;
    }
    .v2-grid-head > div {
        min-height: 48px;
        font-size: 13px;
    }
    .v2-grid-head > div:first-child,
    .v2-row-grid > div:first-child {
        position: sticky;
        left: 0;
        z-index: 4;
        box-shadow: 5px 0 0 rgba(17, 24, 39, .12);
    }
    .v2-grid-head > div:first-child {
        z-index: 6;
        background: #d9d9d9;
    }
    .v2-row-grid > div:first-child {
        background: #ffffff;
    }
    .v2-sort-btn {
        min-height: 38px;
        padding: 6px 8px;
        gap: 6px;
    }
    .v2-sort-mark {
        width: 24px;
        height: 24px;
        flex: 0 0 24px;
    }
    .v2-item {
        font-size: 18px;
    }
    .v2-desc {
        font-size: 15px;
        line-height: 1.25;
    }
    .v2-expand-badge {
        width: 32px;
        height: 32px;
        flex-basis: 32px;
        font-size: 25px;
    }
    .v2-grid-num {
        font-size: 22px;
    }
    .v2-cell-label {
        font-size: 13px;
    }
    .v2-cell-value {
        font-size: 23px;
    }
    .v2-row-expanded {
        position: sticky;
        left: 0;
        width: calc(100vw - 20px);
        max-width: calc(100vw - 20px);
        box-sizing: border-box;
        padding: 10px;
    }
    .v2-expanded-grid {
        grid-template-columns: 1fr;
        gap: 10px;
        width: 100%;
        min-width: 0;
    }
    .v2-detail-card.inventory {
        grid-column: auto;
    }
    .v2-detail-card.sales,
    .v2-detail-card.arrivals {
        width: 100%;
        min-width: 0;
    }
    .v2-detail-title {
        font-size: 15px;
    }
    .v2-mini-table {
        font-size: 16px;
        min-width: 0;
    }
    .v2-mini-table th {
        font-size: 14px;
    }
    .v2-detail-card.sales .v2-mini-table {
        table-layout: fixed;
        width: 100%;
    }
    .v2-detail-card.sales .v2-mini-table th {
        width: 58%;
        font-size: 14px;
        white-space: normal;
    }
    .v2-detail-card.sales .v2-mini-table td {
        width: 42%;
        font-size: 16px;
        text-align: right;
        white-space: nowrap;
    }
    .v2-sales-metric {
        grid-template-columns: minmax(0, 58%) minmax(0, 42%);
    }
    .v2-sales-label,
    .v2-sales-value {
        padding: 9px 10px;
        min-height: 40px;
    }
    .v2-sales-label {
        font-size: 14px;
        letter-spacing: 0;
    }
    .v2-sales-value {
        font-size: 16px;
    }
    .v2-detail-card.arrivals .v2-mini-table {
        table-layout: fixed;
        width: 100%;
    }
    .v2-detail-card.arrivals .v2-mini-table th,
    .v2-detail-card.arrivals .v2-mini-table td {
        padding: 7px 5px;
        font-size: 12px;
        line-height: 1.2;
        white-space: normal;
        overflow-wrap: normal;
        word-break: normal;
    }
    .v2-detail-card.arrivals .v2-mini-table th {
        font-size: 11px;
        letter-spacing: 0;
    }
    .v2-detail-card.arrivals .v2-mini-table td {
        font-size: 13px;
        text-align: right;
    }
    .v2-detail-card.arrivals .v2-mini-table td:first-child {
        text-align: left;
    }
    .v2-detail-card.arrivals .v2-mini-table .v2-item {
        font-size: 14px;
    }
    .v2-arrivals-detail-grid {
        grid-template-columns: minmax(52px, .85fr) minmax(78px, 1.1fr) minmax(86px, 1.15fr) minmax(82px, 1.05fr);
    }
    .v2-arrivals-detail-grid > div {
        padding: 7px 5px;
        min-height: 38px;
    }
    .v2-arr-head-cell {
        font-size: 11px;
        letter-spacing: 0;
        line-height: 1.15;
    }
    .v2-arr-cell {
        font-size: 13px;
    }
    .v2-arr-po {
        font-size: 14px;
    }
    .v2-inv-table {
        min-width: 920px;
    }
    .v2-stamp {
        font-size: 11px;
        line-height: 1.35;
    }
    .v2-report-stamp {
        display: none;
    }
    .v2-scroll-stamp {
        display: block;
        position: sticky;
        left: 0;
        width: calc(100vw - 20px);
        max-width: calc(100vw - 20px);
        box-sizing: border-box;
        margin: 0;
        padding: 10px 12px;
        background: #ffffff;
        border-top: 2px solid #111827;
    }
}
@media print {
    .v2-topbar, .stSelectbox, .stCheckbox, .stButton { display: none !important; }
    html, body, .block-container { background: #ffffff !important; }
    .block-container { padding: 0 !important; }
    .v2-report, .v2-arrivals { margin: 0; border-color: #000000; }
}
</style>
"""


def _v2_h(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return html.escape(str(value).strip())


def _v2_fmt(value: object) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if pd.isna(num):
        return ""
    if abs(num) < 0.005:
        return ""
    return f"{num:,.0f}"


def _v2_fmt_zero(value: object) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    if pd.isna(num):
        num = 0.0
    return f"{num:,.0f}"


def _v2_sort_num(value: object) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    if pd.isna(num):
        num = 0.0
    return f"{num:.6f}"


def _v2_num_cell(value: object, extra_cls: str = "") -> str:
    text = _v2_fmt(value)
    cls = "v2-num"
    if extra_cls:
        cls += f" {extra_cls}"
    if not text:
        cls += " v2-zero"
    return f'<td class="{cls}">{text}</td>'


def _collection_item_cte(collection: str, exclude_discontinued: bool = True) -> str:
    disc_clause = "AND COALESCE(TRIM(IM.IMDROP), '') = '' " if exclude_discontinued else ""
    return (
        "Items AS ( "
        "    SELECT TRIM(IM.IMITEM) AS ITEM_NUMBER, "
        "           TRIM(IM.IMDESC) AS DESCRIPTION, "
        "           IM.IMFACT, IM.IMUM1, IM.IMUM2 "
        "    FROM GSFL2K.ITEMMAST IM "
        "    JOIN GSFL2K.ITEMXTRA IX ON IX.IMXITM = IM.IMITEM "
        f"    WHERE IM.IMDIV IN (1, 6) AND TRIM(IX.IMCOLLECT) = '{sql_escape(collection)}' "
        f"      {disc_clause}"
        ") "
    )


@st.cache_data(ttl=300, show_spinner=False)
def _load_v2_collection_inventory(collection: str, exclude_discontinued: bool = True) -> tuple[pd.DataFrame, str]:
    conn = connect()
    try:
        loc_selects = ""
        for loc in _LOCS:
            loc_selects += (
                f", DECIMAL(COALESCE(SUM(CASE WHEN IB.IBLOC = {loc} "
                "THEN CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 "
                "THEN COALESCE(IB.IBQOH, 0) "
                "ELSE COALESCE(IB.IBQOH, 0) * I.IMFACT END "
                f"ELSE 0 END), 0), 18, 2) AS ON_HAND_LOC_{loc} "
            )
            loc_selects += (
                f", DECIMAL(COALESCE(SUM(CASE WHEN IB.IBLOC = {loc} "
                "THEN CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 "
                "THEN COALESCE(IB.IBQOH, 0) - COALESCE(IB.IBQOO, 0) - COALESCE(IB.IBQAL, 0) "
                "ELSE (COALESCE(IB.IBQOH, 0) - COALESCE(IB.IBQOO, 0) - COALESCE(IB.IBQAL, 0)) * I.IMFACT "
                f"END ELSE 0 END), 0), 18, 2) AS AVAILABLE_LOC_{loc} "
            )
            loc_selects += (
                f", DECIMAL(COALESCE(SUM(CASE WHEN IB.IBLOC = {loc} "
                "THEN CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 "
                "THEN COALESCE(IB.IBQBO, 0) "
                "ELSE COALESCE(IB.IBQBO, 0) * I.IMFACT END "
                f"ELSE 0 END), 0), 18, 2) AS BACK_ORDER_LOC_{loc} "
            )

        sql = (
            f"WITH {_collection_item_cte(collection, exclude_discontinued)} "
            "SELECT "
            "    I.ITEM_NUMBER, "
            "    I.DESCRIPTION, "
            "    DECIMAL(COALESCE(SUM(CASE WHEN IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51) "
            "        THEN CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 "
            "             THEN COALESCE(IB.IBQOH, 0) "
            "             ELSE COALESCE(IB.IBQOH, 0) * I.IMFACT END "
            "        ELSE 0 END), 0), 18, 2) AS ON_HAND, "
            "    DECIMAL(COALESCE(SUM(CASE WHEN IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51) "
            "        THEN CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 "
            "             THEN COALESCE(IB.IBQOH, 0) - COALESCE(IB.IBQOO, 0) - COALESCE(IB.IBQAL, 0) "
            "             ELSE (COALESCE(IB.IBQOH, 0) - COALESCE(IB.IBQOO, 0) - COALESCE(IB.IBQAL, 0)) * I.IMFACT "
            "             END "
            "        ELSE 0 END), 0), 18, 2) AS AVAILABLE, "
            "    DECIMAL(COALESCE(SUM(CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 "
            "        THEN COALESCE(IB.IBQBO, 0) "
            "        ELSE COALESCE(IB.IBQBO, 0) * I.IMFACT END), 0), 18, 2) AS BACK_ORDER "
            f"{loc_selects} "
            "FROM Items I "
            "LEFT JOIN GSFL2K.ITEMBAL IB "
            "    ON IB.IBCO = 1 AND TRIM(IB.IBITEM) = I.ITEM_NUMBER "
            "GROUP BY I.ITEM_NUMBER, I.DESCRIPTION "
            "ORDER BY I.ITEM_NUMBER "
            "FOR READ ONLY"
        )
        return pd.read_sql(sql, conn), _gartman_query_stamp("V2 Inventory", collection)
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _load_v2_collection_sales(collection: str, exclude_discontinued: bool = True) -> tuple[pd.DataFrame, str]:
    conn = connect()
    try:
        sql = (
            f"WITH {_collection_item_cte(collection, exclude_discontinued)}, "
            "SalesAgg AS ( "
            "    SELECT TRIM(L.SLITEM) AS ITEM_NUMBER, "
            "           SUM(CASE WHEN H.SHIDAT >= CURRENT_DATE - 30 DAYS THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS LAST_30D, "
            "           SUM(CASE WHEN H.SHIDAT >= CURRENT_DATE - 90 DAYS THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) / 3 AS THREE_M_AVG, "
            "           SUM(COALESCE(L.SLBLUO, 0)) / 12 AS TWELVE_M_AVG "
            "    FROM GSFL2K.SHHEAD H "
            "    JOIN GSFL2K.SHLINE L "
            "        ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC AND L.SLINV# = H.SHINV# AND L.SLORD# = H.SHORD# "
            "    JOIN Items I ON I.ITEM_NUMBER = TRIM(L.SLITEM) "
            "    WHERE H.SHCO = 1 "
            "      AND H.SHIDAT >= CURRENT_DATE - 365 DAYS "
            "    GROUP BY TRIM(L.SLITEM) "
            ") "
            "SELECT I.ITEM_NUMBER, "
            "       DECIMAL(COALESCE(S.LAST_30D, 0), 18, 2) AS LAST_30D, "
            "       DECIMAL(COALESCE(S.THREE_M_AVG, 0), 18, 2) AS THREE_M_AVG, "
            "       DECIMAL(COALESCE(S.TWELVE_M_AVG, 0), 18, 2) AS TWELVE_M_AVG "
            "FROM Items I "
            "LEFT JOIN SalesAgg S ON S.ITEM_NUMBER = I.ITEM_NUMBER "
            "ORDER BY I.ITEM_NUMBER "
            "FOR READ ONLY"
        )
        return pd.read_sql(sql, conn), _gartman_query_stamp("V2 Sales", collection)
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _load_v2_collection_arrivals(collection: str, exclude_discontinued: bool = True) -> tuple[pd.DataFrame, str]:
    conn = connect()
    try:
        sql = (
            f"WITH {_collection_item_cte(collection, exclude_discontinued)}, "
            "BOStatus AS ( "
            "    SELECT TRIM(L.OLITEM) AS ITEM_NUMBER, "
            "           SUM(CASE WHEN COALESCE(TRIM(L.OLPOR), '') = 'Y' "
            "                    THEN COALESCE(L.OLBLUB, 0) ELSE 0 END) AS ATTACHED_BO_SF "
            "    FROM GSFL2K.OOLINE L "
            "    JOIN GSFL2K.OOHEAD H "
            "        ON H.OHCO = L.OLCO AND H.OHLOC = L.OLLOC AND H.OHORD# = L.OLORD# "
            "    JOIN Items I ON I.ITEM_NUMBER = TRIM(L.OLITEM) "
            "    WHERE L.OLCO = 1 AND L.OLQBO > 0 "
            "      AND TRIM(H.OHCUST) NOT LIKE '%TRANSFER%' "
            "      AND H.OHOTYP NOT LIKE '%RA%' "
            "    GROUP BY TRIM(L.OLITEM) "
            ") "
            "SELECT "
            "    I.ITEM_NUMBER, "
            "    I.DESCRIPTION, "
            "    TRIM(CHAR(PL.PLPO#)) AS PO_NUMBER, "
            "    DECIMAL((PL.PLQORD - COALESCE(PL.PLQREC, 0)) * "
            "        CASE WHEN I.IMFACT = 0 OR I.IMUM1 = I.IMUM2 THEN 1 ELSE I.IMFACT END, 18, 2) AS TOTAL_ON_PO, "
            "    DECIMAL(COALESCE(BO.ATTACHED_BO_SF, 0), 18, 2) AS ATTACHED_BO_SF, "
            "    PL.PLDDAT AS ETW_DATE "
            "FROM Items I "
            "JOIN GSFL2K.POLINE PL ON TRIM(PL.PLITEM) = I.ITEM_NUMBER "
            "LEFT JOIN BOStatus BO ON BO.ITEM_NUMBER = I.ITEM_NUMBER "
            "WHERE PL.PLCO = 1 "
            "  AND COALESCE(TRIM(PL.PLDELT), '') <> 'D' "
            "  AND PL.PLQORD > COALESCE(PL.PLQREC, 0) "
            "  AND PL.PLDDAT IS NOT NULL "
            f"  AND COALESCE(PL.PLVEND, 0) NOT IN ({_EXCLUDED_ARRIVAL_VENDOR_SQL}) "
            "ORDER BY I.ITEM_NUMBER, PL.PLDDAT ASC, TRIM(CHAR(PL.PLPO#)) ASC "
            "FOR READ ONLY"
        )
        df = pd.read_sql(sql, conn)
        if not df.empty:
            df = pd.concat(
                [_apply_arrival_uncommitted(group) for _, group in df.groupby("ITEM_NUMBER", sort=False)],
                ignore_index=True,
            )
        return df, _gartman_query_stamp("V2 Arrivals", collection)
    finally:
        conn.close()


def _v2_specs_rows(items_df: pd.DataFrame) -> list[tuple[str, str]]:
    db = _load_specs_db()
    if db.empty or items_df.empty:
        return []

    rows = []
    for item_num in items_df["ITEM_NUMBER"].astype(str):
        if item_num not in db.index:
            continue
        row = db.loc[item_num]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        rows.append(row)

    if not rows:
        return []

    spec_df = pd.DataFrame(rows)
    out = []
    for col, label in _V2_SPEC_FIELDS:
        if col not in spec_df.columns:
            continue
        values = []
        for value in spec_df[col].dropna().astype(str):
            cleaned = value.strip()
            if (
                cleaned
                and cleaned.lower() != "nan"
                and cleaned.strip(" ?-")
                and cleaned not in values
            ):
                values.append(cleaned)
        if not values:
            continue
        out.append((label, values[0] if len(values) == 1 else "Varies by color"))
    return out


def _v2_specs_html(collection: str, items_df: pd.DataFrame) -> str:
    specs = _v2_specs_rows(items_df)
    if specs:
        top_values = specs[:3]
        mid = "<br>".join(f"{_v2_h(label)}: {_v2_h(value)}" for label, value in top_values)
        grid_specs = specs[3:]
    else:
        mid = "Spec details unavailable for this collection."
        grid_specs = []

    cells = "".join(
        f'<div class="v2-spec-cell"><div class="v2-spec-lbl">{_v2_h(label)}</div>'
        f'<div class="v2-spec-val">{_v2_h(value)}</div></div>'
        for label, value in grid_specs
    )

    return (
        f'<div class="v2-spec-panel">'
        f'<div class="v2-spec-head">'
        f'<div class="v2-spec-main">'
        f'<div class="v2-kicker">Collection</div>'
        f'<div class="v2-spec-name">{_v2_h(collection)}</div>'
        f'</div>'
        f'<div class="v2-spec-mid"><div class="v2-spec-subtitle">{mid}</div></div>'
        f'<div class="v2-spec-count">{len(items_df)}<br>items</div>'
        f'</div>'
        f'<div class="v2-spec-grid">{cells}</div>'
        f'</div>'
    )


def _render_v2_specs(collection: str, items_df: pd.DataFrame) -> None:
    st.markdown(_v2_specs_html(collection, items_df), unsafe_allow_html=True)


def _v2_detail_num(value: object, cls: str = "") -> str:
    text = _v2_fmt(value)
    classes = f"v2-num v2-inv-value {cls}".strip()
    if not text:
        classes += " v2-zero"
    return f'<td class="{classes}" data-v2-value="{_v2_sort_num(value)}">{text}</td>'


def _v2_sales_metric(label: str, field: str, value: object) -> str:
    return (
        f'<div class="v2-sales-metric" data-v2-sales-field="{_v2_h(field)}" '
        f'data-v2-value="{_v2_sort_num(value)}">'
        f'<div class="v2-sales-label">{_v2_h(label)}</div>'
        f'<div class="v2-sales-value">{_v2_fmt_zero(value)}</div>'
        '</div>'
    )


def _v2_inventory_detail(row: pd.Series) -> str:
    loc_headers = "".join(f"<th>Loc {loc}</th>" for loc in _LOCS)

    def _row(label: str, total_col: str, prefix: str, cls: str = "") -> str:
        loc_cells = "".join(
            _v2_detail_num(row.get(f"{prefix}_LOC_{loc}"), cls)
            for loc in _LOCS
        )
        return (
            f'<tr><th>{label}</th>'
            f'{_v2_detail_num(row.get(total_col), cls)}'
            f'{loc_cells}</tr>'
        )

    return (
        '<div class="v2-detail-card inventory">'
        '<div class="v2-detail-title">General - Inventory by Location</div>'
        '<div style="overflow-x:auto;">'
        '<table class="v2-table v2-mini-table v2-inv-table">'
        f'<thead><tr><th></th><th>Total</th>{loc_headers}</tr></thead>'
        '<tbody>'
        f'{_row("On Hand", "ON_HAND", "ON_HAND", "v2-on-hand")}'
        f'{_row("Available", "AVAILABLE", "AVAILABLE", "v2-available")}'
        f'{_row("Back Order", "BACK_ORDER", "BACK_ORDER", "v2-bo")}'
        '</tbody>'
        '</table>'
        '</div>'
        '</div>'
    )


def _v2_sales_detail(row: pd.Series) -> str:
    return (
        '<div class="v2-detail-card sales">'
        '<div class="v2-detail-title">Sales</div>'
        '<div class="v2-sales-metrics">'
        f'{_v2_sales_metric("Last 30 Days", "LAST_30D", row.get("LAST_30D"))}'
        f'{_v2_sales_metric("3 Month Average", "THREE_M_AVG", row.get("THREE_M_AVG"))}'
        f'{_v2_sales_metric("12 Month Average", "TWELVE_M_AVG", row.get("TWELVE_M_AVG"))}'
        '</div>'
        '</div>'
    )


def _v2_arrivals_detail(arrivals: pd.DataFrame | None) -> str:
    if arrivals is None or arrivals.empty:
        return (
            '<div class="v2-detail-card arrivals">'
            '<div class="v2-detail-title">Arrivals</div>'
            '<div class="v2-detail-empty">&nbsp;</div>'
            '</div>'
        )

    rows = []
    for _, row in arrivals.iterrows():
        arrival_date = ""
        if pd.notna(row.get("ETW_DATE")):
            arrival_date = pd.Timestamp(row["ETW_DATE"]).strftime("%m/%d/%Y")
        rows.append(
            f'<div class="v2-arr-cell v2-arr-po" data-v2-field="PO_NUMBER">{_v2_h(row["PO_NUMBER"])}</div>'
            f'<div class="v2-arr-cell v2-arr-num" data-v2-field="TOTAL_ON_PO" '
            f'data-v2-value="{_v2_sort_num(row.get("TOTAL_ON_PO"))}">{_v2_fmt(row.get("TOTAL_ON_PO"))}</div>'
            f'<div class="v2-arr-cell v2-arr-num v2-available" data-v2-field="UNCOMMITTED" '
            f'data-v2-value="{_v2_sort_num(row.get("UNCOMMITTED"))}">{_v2_fmt(row.get("UNCOMMITTED"))}</div>'
            f'<div class="v2-arr-cell" data-v2-field="ARRIVAL_DATE">{_v2_h(arrival_date)}</div>'
        )

    return (
        '<div class="v2-detail-card arrivals">'
        '<div class="v2-detail-title">Arrivals</div>'
        '<div class="v2-arrivals-detail-grid">'
        '<div class="v2-arr-head-cell">PO #</div>'
        '<div class="v2-arr-head-cell">Total on PO</div>'
        '<div class="v2-arr-head-cell">Uncommitted</div>'
        '<div class="v2-arr-head-cell">Arrival Date</div>'
        f'{"".join(rows)}'
        '</div>'
        '</div>'
    )


def _v2_expanded_row(row: pd.Series, arrivals: pd.DataFrame | None) -> str:
    return (
        '<div class="v2-row-expanded">'
        '<div class="v2-expanded-grid">'
        f'{_v2_inventory_detail(row)}'
        f'{_v2_sales_detail(row)}'
        f'{_v2_arrivals_detail(arrivals)}'
        '</div>'
        '</div>'
    )


def _build_v2_main_table(
    items_df: pd.DataFrame,
    sales_df: pd.DataFrame,
    arrivals_df: pd.DataFrame,
) -> str:
    df = items_df.copy()
    if sales_df is not None and not sales_df.empty:
        df = df.merge(sales_df, on="ITEM_NUMBER", how="left")

    arrivals_by_item = {}
    if arrivals_df is not None and not arrivals_df.empty:
        arrivals_by_item = {
            str(item_num): group
            for item_num, group in arrivals_df.groupby("ITEM_NUMBER", sort=False)
        }

    def _sort_header(label: str, key: str, extra_cls: str = "") -> str:
        active = ' data-dir="asc" aria-sort="ascending"' if key == "item" else ' aria-sort="none"'
        classes = f'{extra_cls} '.strip()
        return (
            f'<div class="{classes}">'
            f'<button type="button" class="v2-sort-btn" data-sort-key="{key}" '
            f'title="Sort by {html.escape(label, quote=True)}"{active}>'
            f'{_v2_h(label)}<span class="v2-sort-mark" aria-hidden="true"></span>'
            '</button>'
            '</div>'
        )

    head = (
        '<div class="v2-grid-head">'
        f'{_sort_header("Item / Description", "item", "v2-item-col")}'
        f'{_sort_header("On Hand", "on-hand", "v2-num-col")}'
        f'{_sort_header("Available", "available", "v2-num-col")}'
        f'{_sort_header("Back Order", "back-order", "v2-num-col v2-bo-head")}'
        f'{_sort_header("Sales", "sales", "v2-sales-col v2-sales-head")}'
        f'{_sort_header("Arrivals", "arrivals", "v2-arr-col v2-arr-head")}'
        '</div>'
    )

    body_rows = []
    for _, row in df.iterrows():
        item_num = str(row["ITEM_NUMBER"])
        arrivals = arrivals_by_item.get(item_num)
        arrival_total = arrivals["TOTAL_ON_PO"].sum() if arrivals is not None and not arrivals.empty else 0
        back_order = float(row.get("BACK_ORDER", 0) or 0)
        row_sort_attrs = (
            f'data-sort-item="{html.escape(item_num.casefold(), quote=True)}" '
            f'data-sort-on-hand="{_v2_sort_num(row.get("ON_HAND"))}" '
            f'data-sort-available="{_v2_sort_num(row.get("AVAILABLE"))}" '
            f'data-sort-back-order="{_v2_sort_num(row.get("BACK_ORDER"))}" '
            f'data-sort-sales="{_v2_sort_num(row.get("THREE_M_AVG"))}" '
            f'data-sort-arrivals="{_v2_sort_num(arrival_total)}"'
        )
        item_html = (
            '<div class="v2-item-cell">'
            '<div class="v2-expand-line">'
            '<span class="v2-expand-badge" aria-hidden="true"></span>'
            f'<span class="v2-item">{_v2_h(item_num)}</span>'
            '</div>'
            f'<div class="v2-desc">{_v2_h(row["DESCRIPTION"])}</div>'
            '</div>'
        )
        arrivals_cell = ""
        if arrival_total:
            arrivals_cell = (
                '<span class="v2-cell-label">On PO</span>'
                f'<span class="v2-cell-value">{_v2_fmt(arrival_total)}</span>'
            )
        row_html = (
            f'<div class="v2-row-shell" {row_sort_attrs}>'
            '<details class="v2-row-details">'
            '<summary>'
            '<div class="v2-row-grid">'
            f'<div>{item_html}</div>'
            f'<div class="v2-grid-num v2-on-hand">{_v2_fmt(row.get("ON_HAND"))}</div>'
            f'<div class="v2-grid-num v2-available">{_v2_fmt(row.get("AVAILABLE"))}</div>'
            f'<div class="v2-grid-num {"v2-bo" if back_order > 0 else "v2-zero"}">{_v2_fmt(row.get("BACK_ORDER"))}</div>'
            f'<div class="v2-grid-sales"><span class="v2-cell-label">3 Month Average</span><span class="v2-cell-value" data-v2-sales-summary="{_v2_sort_num(row.get("THREE_M_AVG"))}">{_v2_fmt_zero(row.get("THREE_M_AVG"))}</span></div>'
            f'<div class="v2-grid-arrivals">{arrivals_cell}</div>'
            '</div>'
            '</summary>'
            f'{_v2_expanded_row(row, arrivals)}'
            '</details>'
            '</div>'
        )
        body_rows.append(row_html)

    return f'<div class="v2-grid v2-main-table" data-sort-key="item" data-sort-dir="asc">{head}{"".join(body_rows)}</div>'


def _render_v2_arrivals(arrivals_df: pd.DataFrame, queried_at: str) -> None:
    if arrivals_df.empty:
        st.markdown(
            f'<div class="v2-arrivals">'
            f'<div class="v2-arrivals-title">Arrivals</div>'
            f'<div class="v2-empty">No open purchase orders for this collection.</div>'
            f'<div class="v2-stamp" style="padding:0 16px 14px 16px;">Last queried from Gartman: {_v2_h(queried_at)}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        return

    rows = []
    for _, row in arrivals_df.iterrows():
        arrival_date = ""
        if pd.notna(row.get("ETW_DATE")):
            arrival_date = pd.Timestamp(row["ETW_DATE"]).strftime("%m/%d/%Y")
        rows.append(
            '<tr>'
            f'<td><div class="v2-item">{_v2_h(row["ITEM_NUMBER"])}</div>'
            f'<div class="v2-desc">{_v2_h(row["DESCRIPTION"])}</div></td>'
            f'<td class="v2-item">{_v2_h(row["PO_NUMBER"])}</td>'
            f'{_v2_num_cell(row.get("TOTAL_ON_PO"))}'
            f'{_v2_num_cell(row.get("UNCOMMITTED"), "v2-available")}'
            f'<td>{_v2_h(arrival_date)}</td>'
            '</tr>'
        )

    st.markdown(
        f'<div class="v2-arrivals">'
        f'<div class="v2-arrivals-title">Arrivals</div>'
        f'<table class="v2-table">'
        f'<thead><tr>'
        f'<th class="v2-item-col">Item / Description</th>'
        f'<th>PO #</th>'
        f'<th>Total Qty</th>'
        f'<th>Uncommitted</th>'
        f'<th>Arrival Date</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'<div class="v2-stamp" style="padding:8px 12px;">Last queried from Gartman: {_v2_h(queried_at)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _on_v2_collection_change() -> None:
    st.session_state.v2_collapse_nonce = st.session_state.get("v2_collapse_nonce", 0) + 1


def _render_v2_sort_script() -> None:
    components.html(
        """
        <script>
        (() => {
            const numericKeys = new Set(["on-hand", "available", "back-order", "sales", "arrivals"]);

            function formatWholeNumber(raw, blankZero) {
                const num = Number.parseFloat(raw);
                if (!Number.isFinite(num)) {
                    return blankZero ? "" : "0";
                }
                if (blankZero && Math.abs(num) < 0.005) {
                    return "";
                }
                return Math.round(num).toLocaleString("en-US");
            }

            function hydrateMetricText(parentDoc) {
                parentDoc.querySelectorAll(".v2-sales-value").forEach((valueCell) => {
                    const metric = valueCell.closest(".v2-sales-metric");
                    if (!metric) {
                        return;
                    }
                    valueCell.textContent = formatWholeNumber(metric.getAttribute("data-v2-value"), false);
                });
                parentDoc.querySelectorAll(".v2-cell-value[data-v2-sales-summary]").forEach((valueCell) => {
                    valueCell.textContent = formatWholeNumber(valueCell.getAttribute("data-v2-sales-summary"), false);
                });
                parentDoc.querySelectorAll('.v2-arr-cell[data-v2-field="TOTAL_ON_PO"], .v2-arr-cell[data-v2-field="UNCOMMITTED"]').forEach((valueCell) => {
                    const blankZero = valueCell.getAttribute("data-v2-field") === "UNCOMMITTED";
                    valueCell.textContent = formatWholeNumber(valueCell.getAttribute("data-v2-value"), blankZero);
                });
                parentDoc.querySelectorAll(".v2-inv-value").forEach((valueCell) => {
                    valueCell.textContent = formatWholeNumber(valueCell.getAttribute("data-v2-value"), true);
                });
            }

            function readSortValue(row, key) {
                const raw = row.getAttribute(`data-sort-${key}`) || "";
                if (!numericKeys.has(key)) {
                    return raw;
                }
                const num = Number.parseFloat(raw);
                return Number.isFinite(num) ? num : 0;
            }

            function compareRows(a, b, key, direction) {
                const aVal = readSortValue(a, key);
                const bVal = readSortValue(b, key);
                let result = 0;
                if (numericKeys.has(key)) {
                    result = aVal === bVal ? 0 : (aVal < bVal ? -1 : 1);
                } else {
                    result = String(aVal).localeCompare(String(bVal), undefined, {
                        numeric: true,
                        sensitivity: "base",
                    });
                }
                return direction === "desc" ? -result : result;
            }

            function updateHeaders(table, key, direction) {
                table.querySelectorAll(".v2-sort-btn").forEach((button) => {
                    button.removeAttribute("data-dir");
                    button.setAttribute("aria-sort", "none");
                });
                const active = table.querySelector(`.v2-sort-btn[data-sort-key="${key}"]`);
                if (active) {
                    active.setAttribute("data-dir", direction);
                    active.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");
                }
            }

            function sortTable(table, key, direction) {
                const rows = Array.from(table.children).filter((child) => child.classList.contains("v2-row-shell"));
                rows.sort((a, b) => compareRows(a, b, key, direction));
                rows.forEach((row) => table.appendChild(row));
                table.setAttribute("data-sort-key", key);
                table.setAttribute("data-sort-dir", direction);
                updateHeaders(table, key, direction);
                hydrateMetricText(window.parent.document);
            }

            function bindSorters() {
                const parentDoc = window.parent.document;
                hydrateMetricText(parentDoc);
                parentDoc.querySelectorAll(".v2-main-table").forEach((table) => {
                    if (table.getAttribute("data-sort-bound") === "1") {
                        return;
                    }
                    table.setAttribute("data-sort-bound", "1");
                    table.querySelectorAll(".v2-sort-btn").forEach((button) => {
                        button.addEventListener("click", () => {
                            const key = button.getAttribute("data-sort-key") || "item";
                            const currentKey = table.getAttribute("data-sort-key") || "item";
                            const currentDirection = table.getAttribute("data-sort-dir") || "asc";
                            const firstDirection = key === "item" ? "asc" : "desc";
                            const nextDirection = currentKey === key
                                ? (currentDirection === "asc" ? "desc" : "asc")
                                : firstDirection;
                            sortTable(table, key, nextDirection);
                        });
                    });
                    table.querySelectorAll(".v2-row-details").forEach((detail) => {
                        detail.addEventListener("toggle", () => hydrateMetricText(parentDoc));
                    });
                    updateHeaders(
                        table,
                        table.getAttribute("data-sort-key") || "item",
                        table.getAttribute("data-sort-dir") || "asc"
                    );
                });
            }

            bindSorters();
            setTimeout(bindSorters, 250);
            setTimeout(bindSorters, 1000);
        })();
        </script>
        """,
        height=0,
    )


def render_sales_dashboard_v2() -> None:
    if "v2_collapse_nonce" not in st.session_state:
        st.session_state.v2_collapse_nonce = 0

    st.markdown(_V2_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="v2-topbar">'
        '<span class="v2-logo">OMP</span>'
        '<span class="v2-title">Sales Dashboard V2 - PDF-style collection view</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    dd_col, filter_col, collapse_col, _ = st.columns([1.1, .7, .55, 1.65])
    with dd_col:
        st.markdown('<p class="v2-label">Flooring Collection</p>', unsafe_allow_html=True)
        try:
            with st.spinner("Loading collections..."):
                collections = _load_collections()
        except Exception as exc:
            st.error(f"Could not load collections: {exc}")
            return
        collection = st.selectbox(
            label="collection",
            options=["-- Select a collection --"] + collections,
            index=0,
            label_visibility="collapsed",
            key="v2_dd_collection",
            on_change=_on_v2_collection_change,
        )

    with filter_col:
        st.markdown('<p class="v2-label">&nbsp;</p>', unsafe_allow_html=True)
        exclude_discontinued = st.checkbox(
            "Exclude Discontinued Items",
            value=True,
            key="v2_excl_discontinued",
            on_change=_on_v2_collection_change,
        )

    with collapse_col:
        st.markdown('<p class="v2-label">&nbsp;</p>', unsafe_allow_html=True)
        components.html(
            """
            <style>
            body { margin: 0; font-family: Manrope, Arial, sans-serif; }
            button {
                width: 100%;
                height: 40px;
                border: 1px solid #cfd6e0;
                border-radius: 4px;
                background: #ffffff;
                color: #111827;
                font-size: 15px;
                font-weight: 800;
                cursor: pointer;
            }
            button:hover { background: #f2f4f7; border-color: #98a2b3; }
            </style>
            <button type="button" id="v2-collapse-all">Collapse All</button>
            <script>
            document.getElementById("v2-collapse-all").addEventListener("click", () => {
                const parentDoc = window.parent.document;
                parentDoc
                    .querySelectorAll(".v2-row-details[open]")
                    .forEach((detail) => { detail.open = false; });
            });
            </script>
            """,
            height=44,
        )

    if collection == "-- Select a collection --":
        st.markdown(
            '<div style="margin-top:3rem;text-align:center;color:#667085;font-size:14px;font-weight:600;">'
            'Select a collection to view the PDF-style report.'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    try:
        with st.spinner("Loading collection inventory..."):
            items_df, inventory_ts = _load_v2_collection_inventory(collection, exclude_discontinued)
    except Exception as exc:
        st.error(f"Could not load collection inventory: {exc}")
        return

    if items_df.empty:
        st.warning(f"No items found for collection **{collection}**.")
        return

    try:
        with st.spinner("Loading sales metrics..."):
            sales_df, sales_ts = _load_v2_collection_sales(collection, exclude_discontinued)
    except Exception as exc:
        st.error(f"Could not load sales metrics: {exc}")
        return

    try:
        with st.spinner("Loading arrivals..."):
            arrivals_df, arrivals_ts = _load_v2_collection_arrivals(collection, exclude_discontinued)
    except Exception as exc:
        st.error(f"Could not load arrivals: {exc}")
        return

    stamp = f'Last queried from Gartman: {_v2_h(inventory_ts)}'
    stamp += f' | Sales: {_v2_h(sales_ts)} | Arrivals: {_v2_h(arrivals_ts)}'
    collapse_nonce = int(st.session_state.get("v2_collapse_nonce", 0))
    report_html = (
        f'<div class="v2-report" data-collapse-nonce="{collapse_nonce}">'
        f'{_v2_specs_html(collection, items_df)}'
        f'<div class="v2-table-scroll">{_build_v2_main_table(items_df, sales_df, arrivals_df)}'
        f'<div class="v2-stamp v2-scroll-stamp">{stamp}</div></div>'
        f'<div class="v2-stamp v2-report-stamp">{stamp}</div>'
        '</div>'
    )
    st.markdown(report_html, unsafe_allow_html=True)
    _render_v2_sort_script()


if __name__ == "__main__":
    render_sales_dashboard_v2()
