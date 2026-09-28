"""
OMP Sales Dashboard — Flooring Collection Browser

Sales reps select a collection, click an item row to highlight it, then
click a section button (General / Arrivals / …) to expand that item's detail.

Run:
    .venv\Scripts\python.exe -m streamlit run sales_dashboard.py --server.port 8502
"""

from __future__ import annotations
import warnings
warnings.filterwarnings("ignore")

import calendar as _cal
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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


if __name__ == "__main__":
    render_sales_dashboard()
