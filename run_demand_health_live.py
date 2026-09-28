"""
run_demand_health_live.py

One command, no PowerQuery, no CSV export, no manual file-drop:
connects to Gartman directly, runs both demand_health_extract.sql
queries, scores every item (same logic as demand_health_scoring.py),
writes scored.xlsx + quadrant.png, and opens the dashboard in your
browser with the fresh numbers already loaded.

ONE-TIME SETUP:
  1. pip install pyodbc pandas numpy matplotlib openpyxl
  2. Edit gartman_connection.py (same folder) to match how your other
     OMPforecasting5 scripts connect to Gartman.
  3. Keep these files together in the same folder:
       run_demand_health_live.py   (this file)
       gartman_connection.py
       demand_health_scoring.py
       demand_health_dashboard.html   (the dashboard delivered earlier --
                                        must have the /*DATA_START*/ marker;
                                        re-download it if this script
                                        complains that it's an old version)

USAGE:
    python run_demand_health_live.py

Every run overwrites scored.xlsx, quadrant.png, and
demand_health_dashboard_live.html, then opens the dashboard for you.
Nothing here changes demand_health_extract.sql or demand_health_scoring.py
-- if you edit the SQL logic in one place, mirror the change in
PANEL_SQL / SNAPSHOT_SQL below (or in the .sql file, if you keep using
PowerQuery for anything else) so the two stay in sync.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import warnings
import webbrowser
from pathlib import Path

import pandas as pd

# pandas' pyodbc-vs-SQLAlchemy warning is harmless here (we connect via
# pyodbc directly on purpose, same as every other OMPforecasting5 script) --
# but it prints to stderr, and the scheduled-task wrapper script treats
# stderr output as a sign something went wrong. Silencing it here keeps the
# log file (and console, when run by hand) free of a scary-looking line
# that isn't actually a problem.
warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable.*",
    category=UserWarning,
)

from gartman_connection import get_connection
from demand_health_scoring import score_items, make_chart
from purchasing_plan import load_plan

HERE = Path(__file__).resolve().parent
DASHBOARD_TEMPLATE = HERE / "demand_health_dashboard.html"
DASHBOARD_OUT = HERE / "demand_health_dashboard_live.html"
SCORED_XLSX = HERE / "scored.xlsx"
CHART_PNG = HERE / "quadrant.png"
# Produced by the Purchasing Dashboard refresh (run_webapp_update.ps1).
# Supplies lead time, safety stock, reorder point, order-up-to level and the
# forward forecast. Absent/stale -> we fall back to sales history only.
PLAN_XLSX = HERE / "inventory_plan_all.xlsx"
MARKETING_BLAST_XLSX = Path(r"H:\2025\MISC Reports\For Marketing\Blast Impact Analysis.xlsx")

MIN_MONTHS = 6
MIN_FAMILY_SIZE = 5

MARKETING_SUMMARY_COLS = [
    "INDEX",
    "Index Sort",
    "Send Count",
    "Effective Start",
    "Effective End",
    "Effective Days",
    "Single-Day Effective Period",
    "Breadth",
    "Timeline Item Filter Type",
    "Timeline Item Filter",
    "Discount Type",
    "Discount Amount/Unit",
    "Card Names",
    "Date Warning",
    "Target Set Type",
    "Target Set Source",
    "Target Item Count",
    "Comparison Set Type",
    "Comparison Filters",
    "Comparison Item Count",
    "Data Availability Note",
    "Units During vs Before 30D %",
    "Units After vs Before 30D %",
    "Units Net During Lift 30D pp",
    "Units Net After Lift 30D pp",
    "Revenue During vs Before 30D %",
    "Revenue After vs Before 30D %",
    "Revenue Net During Lift 30D pp",
    "Revenue Net After Lift 30D pp",
    "Orders During vs Before 30D %",
    "Orders After vs Before 30D %",
    "Orders Net During Lift 30D pp",
    "Orders Net After Lift 30D pp",
]
MARKETING_HIGHLIGHT_COLS = [
    "Highlight",
    "INDEX",
    "Card Names",
    "Effective Start",
    "Effective End",
    "Promoted During vs Before %",
    "Comparison During vs Before %",
    "Net During Lift vs Comparison (pp)",
    "Promoted After vs Before %",
    "Comparison After vs Before %",
    "Net After Lift vs Comparison (pp)",
    "Target Set Source",
    "Comparison Filters",
    "Baseline Note",
]
MARKETING_IMPACT_COLS = [
    "INDEX",
    "Window Days",
    "Metric",
    "Promoted Before Avg/Day",
    "Promoted During Avg/Day",
    "Promoted After Avg/Day",
    "Comparison Before Avg/Day",
    "Comparison During Avg/Day",
    "Comparison After Avg/Day",
    "Promoted During vs Before %",
    "Promoted After vs Before %",
    "Comparison During vs Before %",
    "Comparison After vs Before %",
    "Net During Lift vs Comparison (pp)",
    "Net After Lift vs Comparison (pp)",
    "Baseline Note",
]

# Same as EXTRACT A in demand_health_extract.sql -- keep in sync if you
# edit the .sql file.
PANEL_SQL = """
WITH
Params AS (
    SELECT (CURRENT_DATE - (DAY(CURRENT_DATE) - 1) DAYS) AS MSTART
    FROM SYSIBM.SYSDUMMY1
)
SELECT
    TRIM(L.SLITEM) AS ITEM_NUMBER,
    YEAR(H.SHIDAT)  AS YR,
    MONTH(H.SHIDAT) AS MO,
    SUM(COALESCE(L.SLBLUO, 0)) AS UNITS,
    DECIMAL(SUM(COALESCE(L.SLENET, 0)), 18, 2) AS REVENUE,
    COUNT(DISTINCT
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))
    ) AS ORDERS,
    COUNT(DISTINCT TRIM(L.SLCUST)) AS DISTINCT_CUSTOMERS
FROM Params P
JOIN GSFL2K.SHHEAD H
  ON H.SHIDAT >= (P.MSTART - 12 MONTHS)
 AND H.SHIDAT <  P.MSTART
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO AND L.SLLOC = H.SHLOC
 AND L.SLORD# = H.SHORD# AND L.SLINV# = H.SHINV#
WHERE TRIM(L.SLITEM) NOT LIKE 'PROMO%'
GROUP BY TRIM(L.SLITEM), YEAR(H.SHIDAT), MONTH(H.SHIDAT)
ORDER BY ITEM_NUMBER, YR, MO
"""

# Same as EXTRACT B in demand_health_extract.sql -- keep in sync if you
# edit the .sql file.
SNAPSHOT_SQL = """
WITH
RecentPoReceipts AS (
    SELECT DISTINCT TRIM(R.IRITEM) AS ITEM_NUMBER
    FROM GSFL2K.ITEMRECH R
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRRECNBR > 0
      AND R.IRDATE >= ADD_MONTHS(CURRENT_DATE, -24)
),
InvEnd AS (
    SELECT
        IB.IBITEM AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0))
                ELSE (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT
            END
        ) AS END_AVAIL
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY IB.IBITEM
),
AvgInvCost AS (
    SELECT
        TRIM(D.IDITEM) AS ITEM_NUMBER,
        DECIMAL(
            DECIMAL(SUM(DECIMAL(D.IDCOST, 18, 6) * DECIMAL(D.IDQOH, 18, 6)), 18, 6) /
            NULLIF(DECIMAL(SUM(DECIMAL(D.IDQOH, 18, 6)), 18, 6), 0),
            18, 4
        ) AS AVG_INVENTORY_COST
    FROM GSFL2K.ITEMDETL D
    WHERE D.IDCO = 1
      AND D.IDLOC IN ('001', '003', '004', '005', '006', '008', '009', '051')
      AND COALESCE(D.IDDELT, '') <> 'D'
      AND D.IDQOH > 0
      AND D.IDCOST IS NOT NULL
      AND D.IDCOST <> 0
    GROUP BY TRIM(D.IDITEM)
)
SELECT
    TRIM(IM.IMITEM) AS ITEM_NUMBER,
    TRIM(IM.IMDESC) AS DESCRIPTION,
    TRIM(IM.IMFMCD) AS FAMILY_CODE,
    DECIMAL(COALESCE(IE.END_AVAIL, 0), 18, 2)          AS AVAIL_QTY,
    DECIMAL(COALESCE(AC.AVG_INVENTORY_COST, 0), 18, 4) AS AVG_COST,
    DECIMAL(COALESCE(IE.END_AVAIL, 0) * COALESCE(AC.AVG_INVENTORY_COST, 0), 18, 2) AS DOLLARS_TIED_UP,
    CASE WHEN RP.ITEM_NUMBER IS NOT NULL THEN 1 ELSE 0 END AS HAD_PO_RECEIPT_24MO
FROM GSFL2K.ITEMMAST IM
LEFT JOIN InvEnd           IE ON IE.ITEM_NUMBER = IM.IMITEM
LEFT JOIN AvgInvCost       AC ON TRIM(AC.ITEM_NUMBER) = TRIM(IM.IMITEM)
LEFT JOIN RecentPoReceipts RP ON RP.ITEM_NUMBER = TRIM(IM.IMITEM)
ORDER BY IM.IMITEM
"""

METADATA_COLS = [
    "FAMILY_NAME",
    "DIVISION",
    "COLLECTION",
    "VENDOR_NUMBER",
    "VENDOR_NAME",
    "SALES_UOM",
]


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''").strip() + "'"


def _chunks(values: list[str], size: int = 400):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def fetch_item_metadata(conn, item_numbers) -> pd.DataFrame:
    """Fetch descriptive grouping fields for only the SKUs shown in this dashboard."""
    items = sorted({str(i).strip().upper() for i in item_numbers if str(i).strip()})
    if not items:
        return pd.DataFrame(columns=["ITEM_NUMBER", *METADATA_COLS])

    frames: list[pd.DataFrame] = []
    for batch in _chunks(items):
        item_filter = ", ".join(_sql_literal(i) for i in batch)
        query = f"""
        SELECT
            TRIM(IM.IMITEM) AS ITEM_NUMBER,
            TRIM(FM.FMDESC) AS FAMILY_NAME,
            RTRIM(CHAR(IM.IMDIV)) AS DIVISION,
            TRIM(IX.IMCOLLECT) AS COLLECTION,
            TRIM(CHAR(IM.IMVEND)) AS VENDOR_NUMBER,
            TRIM(VM.VMNAME) AS VENDOR_NAME,
            TRIM(IM.IMUM2) AS SALES_UOM
        FROM GSFL2K.ITEMMAST IM
        LEFT JOIN GSFL2K.ITEMXTRA IX
          ON IX.IMXITM = IM.IMITEM
        LEFT JOIN GSFL2K.FAMILY FM
          ON TRIM(FM.FMFMCD) = TRIM(IM.IMFMCD)
        LEFT JOIN GSFL2K.VENDMAST VM
          ON VM.VMVEND = IM.IMVEND
        WHERE TRIM(IM.IMITEM) IN ({item_filter})
        """
        frames.append(pd.read_sql(query, conn))

    metadata = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    metadata.columns = [c.upper() for c in metadata.columns]
    for col in ["ITEM_NUMBER", *METADATA_COLS]:
        if col not in metadata.columns:
            metadata[col] = None
        metadata[col] = metadata[col].map(lambda v: str(v).strip() if pd.notna(v) else None)
        metadata[col] = metadata[col].replace({"": None, "nan": None, "None": None})
    return metadata[["ITEM_NUMBER", *METADATA_COLS]].drop_duplicates("ITEM_NUMBER", keep="first")


def attach_item_metadata(frame: pd.DataFrame, metadata: pd.DataFrame) -> pd.DataFrame:
    if frame is None or not len(frame) or metadata is None or not len(metadata):
        return frame
    existing = [c for c in METADATA_COLS if c in frame.columns]
    base = frame.drop(columns=existing) if existing else frame
    return base.merge(metadata, on="ITEM_NUMBER", how="left")


def _marketing_sheet(path: Path, sheet_name: str, columns: list[str]) -> pd.DataFrame:
    frame = pd.read_excel(path, sheet_name=sheet_name)
    frame.columns = [str(c).strip() for c in frame.columns]
    keep = [c for c in columns if c in frame.columns]
    return frame[keep].copy()


def _dashboard_records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    safe = frame.copy()
    for col in safe.columns:
        if pd.api.types.is_datetime64_any_dtype(safe[col]):
            safe[col] = safe[col].dt.strftime("%Y-%m-%d")
    return json.loads(safe.to_json(orient="records"))


def load_marketing_blast(path: Path) -> tuple[dict, dict[str, pd.DataFrame]]:
    meta = {
        "available": False,
        "source_path": str(path),
        "note": "Blast Impact Analysis.xlsx was not found.",
    }
    if not path.exists():
        return {"meta": meta, "summary": [], "highlights": [], "impact30": []}, {}

    summary_raw = pd.read_excel(path, sheet_name="Index Summary")
    summary_raw.columns = [str(c).strip() for c in summary_raw.columns]

    summary = summary_raw[[c for c in MARKETING_SUMMARY_COLS if c in summary_raw.columns]].copy()
    highlights = _marketing_sheet(path, "30D Revenue Highlights", MARKETING_HIGHLIGHT_COLS)
    impact = _marketing_sheet(path, "Impact Long", MARKETING_IMPACT_COLS)
    if "Window Days" in impact.columns:
        impact = impact[pd.to_numeric(impact["Window Days"], errors="coerce") == 30].copy()

    generated = summary_raw["Generated At"].dropna().iloc[0] if "Generated At" in summary_raw.columns and summary_raw["Generated At"].notna().any() else None
    db_today = summary_raw["DB Today"].dropna().iloc[0] if "DB Today" in summary_raw.columns and summary_raw["DB Today"].notna().any() else None
    data_max = summary_raw["Extracted Sales Data Max"].dropna().iloc[0] if "Extracted Sales Data Max" in summary_raw.columns and summary_raw["Extracted Sales Data Max"].notna().any() else None

    def date_text(value) -> str | None:
        if value is None or pd.isna(value):
            return None
        return pd.Timestamp(value).strftime("%Y-%m-%d")

    meta = {
        "available": True,
        "source_path": str(path),
        "modified": dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %I:%M %p"),
        "generated_at": str(generated) if generated is not None and not pd.isna(generated) else None,
        "db_today": date_text(db_today),
        "data_max": date_text(data_max),
        "campaign_count": int(len(summary)),
        "highlight_count": int(len(highlights)),
    }
    data = {
        "meta": meta,
        "summary": _dashboard_records(summary),
        "highlights": _dashboard_records(highlights),
        "impact30": _dashboard_records(impact),
    }
    return data, {
        "Marketing Blast Summary": summary,
        "Marketing Blast Highlights": highlights,
        "Marketing Impact 30D": impact,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--no-browser",
        action="store_true",
        help="Refresh the data and write demand_health_dashboard_live.html, but don't open a "
        "browser tab. Used by the scheduled-task wrapper so an unattended overnight/morning "
        "run doesn't pop a browser window open on your desktop.",
    )
    args = ap.parse_args()

    print("Connecting to Gartman...")
    conn = get_connection()
    try:
        print("Running MonthlyPanel extract (EXTRACT A)...")
        panel = pd.read_sql(PANEL_SQL, conn)
        print(f"  {len(panel):,} item-month rows")

        print("Running ItemSnapshot extract (EXTRACT B)...")
        snapshot = pd.read_sql(SNAPSHOT_SQL, conn)
        print(f"  {len(snapshot):,} items")
    finally:
        conn.close()

    print("Loading purchasing plan...")
    plan_df, projections, plan_meta = load_plan(PLAN_XLSX)
    if plan_meta["available"]:
        age = plan_meta["age_days"]
        stale = "  *** STALE -- re-run the purchasing refresh ***" if age > 1.5 else ""
        print(
            f"  {plan_meta['n_plan_skus']} plan SKUs, updated {plan_meta['modified']} "
            f"({age:.1f} days old){stale}"
        )
    else:
        print(f"  {plan_meta['note']}")

    print("Scoring...")
    scored, weights_df, insufficient, no_recent_po = score_items(
        panel, snapshot, MIN_MONTHS, MIN_FAMILY_SIZE, plan=plan_df
    )

    # Items the purchasing model plans but demand-health had to drop (too
    # little sales history, or no PO receipt in 24 months). They never appear
    # in the scored table, so an overstocked one is completely invisible --
    # surface them separately rather than letting them fall through the gap.
    plan_only = pd.DataFrame()
    if plan_meta["available"] and len(plan_df):
        unscored = plan_df[~plan_df["ITEM_NUMBER"].isin(set(scored["ITEM_NUMBER"]))].copy()
        unscored = unscored.drop_duplicates(subset=["PLAN_SKU"], keep="first")
        avail = pd.to_numeric(unscored["PLAN_AVAILABLE_SF"], errors="coerce")
        outl = pd.to_numeric(unscored["PLAN_ORDER_UP_TO"], errors="coerce")
        over = unscored[(avail > outl) & (outl > 0)].copy()
        if len(over):
            over["COVER_RATIO"] = (
                pd.to_numeric(over["PLAN_AVAILABLE_SF"], errors="coerce")
                / pd.to_numeric(over["PLAN_ORDER_UP_TO"], errors="coerce")
            )
            # Pull description / $ tied up from the snapshot where we have it
            snap_cols = [
                c for c in (
                    "ITEM_NUMBER", "DESCRIPTION", "FAMILY_CODE", "FAMILY_NAME",
                    "DIVISION", "COLLECTION", "VENDOR_NUMBER", "VENDOR_NAME",
                    "SALES_UOM", "DOLLARS_TIED_UP",
                )
                if c in snapshot.columns
            ]
            over = over.merge(snapshot[snap_cols], on="ITEM_NUMBER", how="left")
            excluded = set(insufficient["ITEM_NUMBER"]) | set(no_recent_po["ITEM_NUMBER"])
            over["WHY_UNSCORED"] = [
                "excluded from scoring (insufficient history or no recent PO)"
                if i in excluded else "no sales in the trailing 12 months at all"
                for i in over["ITEM_NUMBER"]
            ]
            # Sort by money first, ratio second. A 380x cover ratio on an
            # item with a 14 SF order-up-to is arithmetically dramatic and
            # financially irrelevant; dollars is what you act on.
            plan_only = over.sort_values(
                ["DOLLARS_TIED_UP", "COVER_RATIO"], ascending=[False, False]
            ) if "DOLLARS_TIED_UP" in over.columns else over.sort_values(
                "COVER_RATIO", ascending=False
            )
        print(f"  {len(plan_only)} planned SKUs are over their order-up-to level but unscored")

    metadata_items = set(scored["ITEM_NUMBER"])
    if len(plan_only):
        metadata_items |= set(plan_only["ITEM_NUMBER"])
    print("Fetching dashboard item metadata...")
    try:
        metadata_conn = get_connection()
        try:
            item_metadata = fetch_item_metadata(metadata_conn, metadata_items)
        finally:
            metadata_conn.close()
        scored = attach_item_metadata(scored, item_metadata)
        plan_only = attach_item_metadata(plan_only, item_metadata)
        filled = {
            col: int(scored[col].notna().sum())
            for col in METADATA_COLS
            if col in scored.columns
        }
        print("  metadata coverage: " + ", ".join(f"{k}={v:,}" for k, v in filled.items()))
    except Exception as exc:
        print(f"  WARNING: item metadata lookup failed; dashboard will use fallback grouping ({exc})")

    print("Loading marketing blast analysis...")
    try:
        marketing_data, marketing_sheets = load_marketing_blast(MARKETING_BLAST_XLSX)
        if marketing_data["meta"].get("available"):
            print(
                f"  {marketing_data['meta']['campaign_count']} campaign index rows, "
                f"sales data through {marketing_data['meta'].get('data_max') or 'unknown'}"
            )
        else:
            print(f"  {marketing_data['meta'].get('note')}")
    except Exception as exc:
        marketing_data = {
            "meta": {
                "available": False,
                "source_path": str(MARKETING_BLAST_XLSX),
                "note": f"Could not load marketing blast analysis: {exc}",
            },
            "summary": [],
            "highlights": [],
            "impact30": [],
        }
        marketing_sheets = {}
        print(f"  WARNING: marketing blast analysis not loaded ({exc})")

    with pd.ExcelWriter(SCORED_XLSX, engine="openpyxl") as writer:
        scored.sort_values("DEMAND_HEALTH_PCTL").to_excel(writer, sheet_name="Scored", index=False)
        weights_df.to_excel(writer, sheet_name="PCA Weights", index=False)
        insufficient.to_excel(writer, sheet_name="Insufficient History", index=False)
        no_recent_po.to_excel(writer, sheet_name="No Recent PO", index=False)
        panel.to_excel(writer, sheet_name="Monthly Panel", index=False)
        if len(plan_only):
            plan_only.to_excel(writer, sheet_name="Planned But Unscored", index=False)
        for sheet_name, frame in marketing_sheets.items():
            frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    make_chart(scored, str(CHART_PNG))
    print(f"Wrote {SCORED_XLSX.name} and {CHART_PNG.name}")

    print(
        f"Scored {len(scored):,} items ({len(insufficient):,} excluded for insufficient history, "
        f"{len(no_recent_po):,} excluded for no PO receipt in 24 months)."
    )
    n_over = int((scored["OVERSTOCKED"] == 1).sum())
    n_under = int((scored["UNDERSTOCKED"] == 1).sum())
    print(
        f"  overstocked: {n_over}  (${scored.loc[scored['OVERSTOCKED'] == 1, 'EXCESS_DOLLARS'].sum():,.0f} excess)"
        f"   |   understocked: {n_under}"
        f"   |   total excess above target: ${scored['EXCESS_DOLLARS'].sum():,.0f}"
    )
    print("Demand-health metric weights:")
    print(weights_df.to_string(index=False))

    # ---- build the fresh data blob and splice it into the dashboard ----
    weight_col = "Demand-health weight"
    pca = {
        row["Metric"]: row[weight_col]
        for _, row in weights_df.iterrows()
        if row.get("Metric") in {"UNITS", "REVENUE", "ORDERS", "DISTINCT_CUSTOMERS"}
    }

    # Monthly detail per item, for the dashboard's click-a-row drill-down --
    # plain Python types throughout (not numpy scalars) so json.dumps below
    # doesn't choke on int64/float64 values coming out of pandas.
    # Only for items that made it into the scored table -- those are the only
    # rows you can click. Carrying the other ~1,600 items' monthly history
    # would roughly double the size of the embedded blob for no benefit.
    scored_items = set(scored["ITEM_NUMBER"])
    monthly_by_item: dict[str, list[dict]] = {}
    for item, grp in panel.groupby("ITEM_NUMBER"):
        if item not in scored_items:
            continue
        g = grp.sort_values(["YR", "MO"])
        monthly_by_item[item] = [
            {
                "YR": int(row.YR),
                "MO": int(row.MO),
                "UNITS": float(row.UNITS) if pd.notna(row.UNITS) else None,
                "REVENUE": float(row.REVENUE) if pd.notna(row.REVENUE) else None,
                "ORDERS": float(row.ORDERS) if pd.notna(row.ORDERS) else None,
                "DISTINCT_CUSTOMERS": float(row.DISTINCT_CUSTOMERS) if pd.notna(row.DISTINCT_CUSTOMERS) else None,
            }
            for row in g.itertuples()
        ]

    data = {
        "items": scored.to_dict(orient="records"),
        "pca": pca,
        "insufficient": insufficient.to_dict(orient="records"),
        "no_recent_po": no_recent_po.to_dict(orient="records"),
        "monthly": monthly_by_item,
        "projections": {k: v for k, v in projections.items() if k in set(scored["ITEM_NUMBER"])},
        "plan_meta": plan_meta,
        "plan_only_overstock": json.loads(plan_only.to_json(orient="records")) if len(plan_only) else [],
        "marketing": marketing_data,
        "meta": {
            "source": "live",
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %I:%M %p"),
        },
    }

    if not DASHBOARD_TEMPLATE.exists():
        raise SystemExit(
            f"Can't find {DASHBOARD_TEMPLATE.name} in this folder -- save the dashboard "
            "file here (same folder as this script) and re-run."
        )
    html = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    if "/*DATA_START*/" not in html or "/*DATA_END*/" not in html:
        raise SystemExit(
            f"{DASHBOARD_TEMPLATE.name} looks like an older version without the live-refresh "
            "markers -- re-download the latest dashboard file and try again."
        )
    before, rest = html.split("/*DATA_START*/", 1)
    _, after = rest.split("/*DATA_END*/", 1)
    new_html = before + "/*DATA_START*/" + json.dumps(data) + "/*DATA_END*/" + after
    DASHBOARD_OUT.write_text(new_html, encoding="utf-8")
    print(f"Wrote {DASHBOARD_OUT.name}")

    if args.no_browser:
        print("--no-browser passed, not opening a browser tab (data is refreshed on disk).")
    else:
        webbrowser.open(DASHBOARD_OUT.resolve().as_uri())
        print("Opened in your browser.")


if __name__ == "__main__":
    main()
