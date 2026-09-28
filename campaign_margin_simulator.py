"""
Marketing offer margin simulator.

Run:
    .venv\\Scripts\\python.exe -m streamlit run campaign_margin_simulator.py --server.port 8510

This is intentionally standalone for now so it can be folded into the broader
marketing dashboard once the workflow is settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import warnings
from typing import Any

import numpy as np
import pandas as pd


QUERY_VERSION = 1
DISCOUNT_TYPES = ["Percentage Off", "Price Drop", "BOGO", "Bundle"]
DEFAULT_RESULT_LIMIT = 25

MONEY_COLUMNS = {
    "Sale Price",
    "Discounted Sale Price",
    "Price Decrease $",
    "Most Recent PO Cost",
    "Avg Inventory Cost",
    "Original Gross Profit - Recent PO",
    "Offer Gross Profit - Recent PO",
    "Gross Profit Decrease - Recent PO",
    "Original Gross Profit - Avg Inventory",
    "Offer Gross Profit - Avg Inventory",
    "Gross Profit Decrease - Avg Inventory",
    "Revenue 365D",
    "Avg Invoice Price 365D",
    "Flooring Revenue",
    "Flooring Cost",
    "Flooring Gross Profit",
    "Sale Item Original Price",
    "Sale Item Offer Price",
    "Sale Item Cost",
    "Sale Item Gross Profit",
    "Example Order Revenue",
    "Example Order Cost",
    "Example Order Gross Profit",
}

PERCENT_COLUMNS = {
    "Price Decrease %",
    "Original Margin % - Recent PO",
    "Offer Margin % - Recent PO",
    "Margin Point Decrease - Recent PO",
    "Original Margin % - Avg Inventory",
    "Offer Margin % - Avg Inventory",
    "Margin Point Decrease - Avg Inventory",
    "Example Order Margin %",
}

NUMBER_COLUMNS = {
    "Units Sold 365D",
    "Orders 365D",
    "Current Available",
    "Threshold SQFT",
}


@dataclass(frozen=True)
class OfferConfig:
    discount_type: str
    magnitude: float
    sample_flooring_sqft: float = 500.0
    sample_flooring_sale_per_sf: float = 5.0
    sample_flooring_margin_pct: float = 40.0


def clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def norm_key(value: Any) -> str:
    return clean_text(value).upper()


def _to_number(series: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(default).astype(float)


def _safe_margin(price: pd.Series, cost: pd.Series) -> pd.Series:
    price = _to_number(price)
    cost = _to_number(cost)
    return np.where(price > 0, (price - cost) / price * 100.0, np.nan)


def _safe_margin_scalar(price: float, cost: float) -> float:
    if price <= 0:
        return math.nan
    return (price - cost) / price * 100.0


def _clip_discounted_price(price: pd.Series, discount_amount: pd.Series) -> pd.Series:
    return (price - discount_amount).clip(lower=0.0)


def offer_value_label(discount_type: str) -> str:
    if discount_type == "Percentage Off":
        return "Percent off"
    if discount_type == "Price Drop":
        return "Dollar drop per unit"
    if discount_type == "BOGO":
        return "Free units per paid unit"
    if discount_type == "Bundle":
        return "Bundled item percent off"
    return "Discount value"


def default_offer_value(discount_type: str) -> float:
    if discount_type == "Percentage Off":
        return 15.0
    if discount_type == "Price Drop":
        return 5.0
    if discount_type == "BOGO":
        return 1.0
    if discount_type == "Bundle":
        return 100.0
    return 0.0


def _discount_amount(price: pd.Series, config: OfferConfig) -> pd.Series:
    price = _to_number(price)
    magnitude = max(0.0, float(config.magnitude or 0.0))

    if config.discount_type == "Percentage Off":
        rate = min(magnitude, 100.0) / 100.0
        return price * rate

    if config.discount_type == "Price Drop":
        return pd.Series(np.minimum(price, magnitude), index=price.index)

    if config.discount_type == "BOGO":
        free_units = max(0.0, magnitude)
        effective_price = np.where(free_units > 0, price / (1.0 + free_units), price)
        return pd.Series(price - effective_price, index=price.index)

    if config.discount_type == "Bundle":
        rate = min(magnitude, 100.0) / 100.0
        return price * rate

    return pd.Series(np.zeros(len(price)), index=price.index)


def calculate_offer_margin_impact(items: pd.DataFrame, config: OfferConfig) -> pd.DataFrame:
    """Return item-level margin impact for the selected offer."""
    if items.empty:
        return pd.DataFrame()

    required = {
        "ITEM_NUMBER",
        "DESCRIPTION",
        "DIVISION",
        "FAMILY_CODE",
        "FAMILY_NAME",
        "COLLECTION",
        "VENDOR_NUMBER",
        "VENDOR_NAME",
        "SALES_UOM",
        "SALE_PRICE",
        "MOST_RECENT_PO_COST",
        "AVG_INVENTORY_COST",
        "CURRENT_AVAILABLE",
        "UNITS_SOLD_365D",
        "REVENUE_365D",
        "ORDERS_365D",
    }
    missing = sorted(required - set(items.columns))
    if missing:
        raise ValueError(f"items is missing required columns: {', '.join(missing)}")

    out = items.copy()
    price = _to_number(out["SALE_PRICE"])
    recent_cost = _to_number(out["MOST_RECENT_PO_COST"])
    avg_cost = _to_number(out["AVG_INVENTORY_COST"])
    discount = _discount_amount(price, config)
    discounted = _clip_discounted_price(price, discount)

    out["Discount Type"] = config.discount_type
    out["Discount Magnitude"] = float(config.magnitude or 0.0)
    out["Sale Price"] = price
    out["Discounted Sale Price"] = discounted
    out["Price Decrease $"] = price - discounted
    out["Price Decrease %"] = np.where(price > 0, (price - discounted) / price * 100.0, np.nan)

    out["Most Recent PO Cost"] = recent_cost
    out["Avg Inventory Cost"] = avg_cost
    out["Original Gross Profit - Recent PO"] = price - recent_cost
    out["Offer Gross Profit - Recent PO"] = discounted - recent_cost
    out["Gross Profit Decrease - Recent PO"] = out["Original Gross Profit - Recent PO"] - out["Offer Gross Profit - Recent PO"]
    out["Original Margin % - Recent PO"] = _safe_margin(price, recent_cost)
    out["Offer Margin % - Recent PO"] = _safe_margin(discounted, recent_cost)
    out["Margin Point Decrease - Recent PO"] = out["Original Margin % - Recent PO"] - out["Offer Margin % - Recent PO"]

    out["Original Gross Profit - Avg Inventory"] = price - avg_cost
    out["Offer Gross Profit - Avg Inventory"] = discounted - avg_cost
    out["Gross Profit Decrease - Avg Inventory"] = out["Original Gross Profit - Avg Inventory"] - out["Offer Gross Profit - Avg Inventory"]
    out["Original Margin % - Avg Inventory"] = _safe_margin(price, avg_cost)
    out["Offer Margin % - Avg Inventory"] = _safe_margin(discounted, avg_cost)
    out["Margin Point Decrease - Avg Inventory"] = out["Original Margin % - Avg Inventory"] - out["Offer Margin % - Avg Inventory"]

    out["Avg Invoice Price 365D"] = np.where(
        _to_number(out["UNITS_SOLD_365D"]) != 0,
        _to_number(out["REVENUE_365D"]) / _to_number(out["UNITS_SOLD_365D"]).replace(0, np.nan),
        np.nan,
    )

    display_columns = [
        "ITEM_NUMBER",
        "DESCRIPTION",
        "DIVISION",
        "FAMILY_CODE",
        "FAMILY_NAME",
        "COLLECTION",
        "VENDOR_NUMBER",
        "VENDOR_NAME",
        "SALES_UOM",
        "Sale Price",
        "Discounted Sale Price",
        "Price Decrease $",
        "Price Decrease %",
        "Most Recent PO Cost",
        "Avg Inventory Cost",
        "Original Margin % - Recent PO",
        "Offer Margin % - Recent PO",
        "Margin Point Decrease - Recent PO",
        "Original Margin % - Avg Inventory",
        "Offer Margin % - Avg Inventory",
        "Margin Point Decrease - Avg Inventory",
        "Original Gross Profit - Recent PO",
        "Offer Gross Profit - Recent PO",
        "Gross Profit Decrease - Recent PO",
        "Original Gross Profit - Avg Inventory",
        "Offer Gross Profit - Avg Inventory",
        "Gross Profit Decrease - Avg Inventory",
        "CURRENT_AVAILABLE",
        "UNITS_SOLD_365D",
        "REVENUE_365D",
        "ORDERS_365D",
        "Avg Invoice Price 365D",
        "Discount Type",
        "Discount Magnitude",
    ]
    renamed = out[display_columns].rename(
        columns={
            "ITEM_NUMBER": "Item Number",
            "DESCRIPTION": "Description",
            "DIVISION": "Division",
            "FAMILY_CODE": "Family",
            "FAMILY_NAME": "Family Name",
            "COLLECTION": "Collection",
            "VENDOR_NUMBER": "Vendor Number",
            "VENDOR_NAME": "Vendor Name",
            "SALES_UOM": "Sales UOM",
            "CURRENT_AVAILABLE": "Current Available",
            "UNITS_SOLD_365D": "Units Sold 365D",
            "REVENUE_365D": "Revenue 365D",
            "ORDERS_365D": "Orders 365D",
        }
    )
    return renamed


def lowest_margin_items(impact: pd.DataFrame, limit: int) -> pd.DataFrame:
    if impact.empty:
        return impact
    sort_cols = ["Offer Margin % - Recent PO", "Offer Margin % - Avg Inventory", "Revenue 365D"]
    return impact.sort_values(sort_cols, ascending=[True, True, False], na_position="last").head(limit).reset_index(drop=True)


def top_selling_items(impact: pd.DataFrame, limit: int) -> pd.DataFrame:
    if impact.empty:
        return impact
    return (
        impact.sort_values(["Units Sold 365D", "Revenue 365D"], ascending=[False, False], na_position="last")
        .head(limit)
        .reset_index(drop=True)
    )


def build_bundle_order_summary(selected_item: pd.Series, config: OfferConfig) -> pd.DataFrame:
    """Build a one-order example for Bundle/BOGO offers."""
    sqft = max(0.0, float(config.sample_flooring_sqft or 0.0))
    flooring_sale_per_sf = max(0.0, float(config.sample_flooring_sale_per_sf or 0.0))
    flooring_margin_pct = max(0.0, min(float(config.sample_flooring_margin_pct or 0.0), 100.0))
    flooring_revenue = sqft * flooring_sale_per_sf
    flooring_cost = flooring_revenue * (1.0 - flooring_margin_pct / 100.0)
    flooring_gp = flooring_revenue - flooring_cost

    item_number = clean_text(selected_item.get("Item Number", ""))
    description = clean_text(selected_item.get("Description", ""))
    original_price = float(selected_item.get("Sale Price") or 0.0)
    offer_price = float(selected_item.get("Discounted Sale Price") or 0.0)

    rows: list[dict[str, Any]] = []
    for basis, cost_col in [
        ("Most Recent PO", "Most Recent PO Cost"),
        ("Avg Inventory", "Avg Inventory Cost"),
    ]:
        sale_item_cost = float(selected_item.get(cost_col) or 0.0)
        sale_item_gp = offer_price - sale_item_cost
        order_revenue = flooring_revenue + offer_price
        order_cost = flooring_cost + sale_item_cost
        order_gp = order_revenue - order_cost
        rows.append(
            {
                "Offer Type": config.discount_type,
                "Cost Basis": basis,
                "Threshold SQFT": sqft,
                "Sample Flooring Margin %": flooring_margin_pct,
                "Flooring Revenue": flooring_revenue,
                "Flooring Cost": flooring_cost,
                "Flooring Gross Profit": flooring_gp,
                "Sale Item Number": item_number,
                "Sale Item Description": description,
                "Sale Item Original Price": original_price,
                "Sale Item Offer Price": offer_price,
                "Sale Item Cost": sale_item_cost,
                "Sale Item Gross Profit": sale_item_gp,
                "Example Order Revenue": order_revenue,
                "Example Order Cost": order_cost,
                "Example Order Gross Profit": order_gp,
                "Example Order Margin %": _safe_margin_scalar(order_revenue, order_cost),
            }
        )
    return pd.DataFrame(rows)


def _item_query() -> str:
    return """
    WITH
    RecentSales AS (
        SELECT
            TRIM(L.SLITEM) AS ITEM_NUMBER,
            SUM(COALESCE(L.SLBLUS, 0)) AS UNITS_SOLD_365D,
            SUM(COALESCE(L.SLENET, 0)) AS REVENUE_365D,
            COUNT(DISTINCT
                RTRIM(CHAR(H.SHCO))
                || '|' || RTRIM(CHAR(H.SHLOC))
                || '|' || RTRIM(CHAR(H.SHORD#))
                || '|' || RTRIM(CHAR(H.SHINV#))
            ) AS ORDERS_365D
        FROM GSFL2K.SHHEAD H
        JOIN GSFL2K.SHLINE L
          ON L.SLCO = H.SHCO
         AND L.SLLOC = H.SHLOC
         AND L.SLORD# = H.SHORD#
         AND L.SLINV# = H.SHINV#
        WHERE H.SHIDAT >= (CURRENT_DATE - 365 DAYS)
          AND TRIM(L.SLITEM) <> ''
          AND UPPER(TRIM(L.SLITEM)) NOT LIKE 'PROMO%'
          AND (COALESCE(L.SLBLUS, 0) <> 0 OR COALESCE(L.SLENET, 0) <> 0)
          AND UPPER(TRIM(H.SHCUST)) NOT LIKE '%TRANSFER%'
          AND UPPER(TRIM(H.SHCUST)) NOT LIKE '%OMP000%'
          AND UPPER(TRIM(H.SHCUST)) NOT LIKE '%INV000%'
          AND UPPER(TRIM(H.SHCUST)) NOT LIKE '%OLD001%'
        GROUP BY TRIM(L.SLITEM)
    ),
    CurrentAvailable AS (
        SELECT
            TRIM(IB.IBITEM) AS ITEM_NUMBER,
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
          AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
        GROUP BY TRIM(IB.IBITEM)
    ),
    LatestLandedCost AS (
        SELECT
            TRIM(R.IRITEM) AS ITEM_NUMBER,
            DECIMAL(MAX(R.IRCOST), 18, 5) AS MOST_RECENT_PO_COST
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
        GROUP BY TRIM(R.IRITEM)
    ),
    AvgInvCost AS (
        SELECT
            TRIM(D.IDITEM) AS ITEM_NUMBER,
            DECIMAL(
                DECIMAL(SUM(
                    DECIMAL(D.IDCOST, 18, 6) * DECIMAL(D.IDQOH, 18, 6)
                ), 18, 6) /
                NULLIF(
                    DECIMAL(SUM(DECIMAL(D.IDQOH, 18, 6)), 18, 6),
                    0
                ),
                18,
                5
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
        RTRIM(CHAR(IM.IMDIV)) AS DIVISION,
        TRIM(IM.IMFMCD) AS FAMILY_CODE,
        TRIM(FM.FMDESC) AS FAMILY_NAME,
        TRIM(IX.IMCOLLECT) AS COLLECTION,
        TRIM(CHAR(IM.IMVEND)) AS VENDOR_NUMBER,
        TRIM(VM.VMNAME) AS VENDOR_NAME,
        TRIM(IM.IMUM2) AS SALES_UOM,
        DECIMAL(COALESCE(IM.IMP1, 0), 18, 5) AS SALE_PRICE,
        DECIMAL(COALESCE(LC.MOST_RECENT_PO_COST, 0), 18, 5) AS MOST_RECENT_PO_COST,
        DECIMAL(COALESCE(AC.AVG_INVENTORY_COST, 0), 18, 5) AS AVG_INVENTORY_COST,
        DECIMAL(COALESCE(CA.CURRENT_AVAILABLE, 0), 18, 2) AS CURRENT_AVAILABLE,
        DECIMAL(COALESCE(RS.UNITS_SOLD_365D, 0), 18, 2) AS UNITS_SOLD_365D,
        DECIMAL(COALESCE(RS.REVENUE_365D, 0), 18, 2) AS REVENUE_365D,
        COALESCE(RS.ORDERS_365D, 0) AS ORDERS_365D
    FROM GSFL2K.ITEMMAST IM
    LEFT JOIN GSFL2K.ITEMXTRA IX
      ON IX.IMXITM = IM.IMITEM
    LEFT JOIN GSFL2K.FAMILY FM
      ON TRIM(FM.FMFMCD) = TRIM(IM.IMFMCD)
    LEFT JOIN GSFL2K.VENDMAST VM
      ON VM.VMVEND = IM.IMVEND
    LEFT JOIN RecentSales RS
      ON RS.ITEM_NUMBER = TRIM(IM.IMITEM)
    LEFT JOIN CurrentAvailable CA
      ON CA.ITEM_NUMBER = TRIM(IM.IMITEM)
    LEFT JOIN LatestLandedCost LC
      ON LC.ITEM_NUMBER = TRIM(IM.IMITEM)
    LEFT JOIN AvgInvCost AC
      ON AC.ITEM_NUMBER = TRIM(IM.IMITEM)
    WHERE COALESCE(IM.IMDROP, '') <> 'D'
    ORDER BY TRIM(IM.IMITEM)
    """


def fetch_offer_margin_items(conn) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable",
            category=UserWarning,
        )
        df = pd.read_sql(_item_query(), conn)

    df.columns = [str(c).upper() for c in df.columns]
    text_columns = [
        "ITEM_NUMBER",
        "DESCRIPTION",
        "DIVISION",
        "FAMILY_CODE",
        "FAMILY_NAME",
        "COLLECTION",
        "VENDOR_NUMBER",
        "VENDOR_NAME",
        "SALES_UOM",
    ]
    for col in text_columns:
        df[col] = df[col].map(clean_text)

    numeric_columns = [
        "SALE_PRICE",
        "MOST_RECENT_PO_COST",
        "AVG_INVENTORY_COST",
        "CURRENT_AVAILABLE",
        "UNITS_SOLD_365D",
        "REVENUE_365D",
        "ORDERS_365D",
    ]
    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["DIVISION_KEY"] = df["DIVISION"].map(norm_key)
    df["FAMILY_KEY"] = df["FAMILY_CODE"].map(norm_key)
    df["COLLECTION_KEY"] = df["COLLECTION"].map(norm_key)
    df["VENDOR_KEY"] = df["VENDOR_NUMBER"].map(norm_key)
    df["ITEM_KEY"] = df["ITEM_NUMBER"].map(norm_key)
    return df.drop_duplicates(subset=["ITEM_KEY"], keep="first").reset_index(drop=True)


def option_values(df: pd.DataFrame, column: str) -> list[str]:
    values = [clean_text(v) for v in df[column].dropna().unique().tolist()]
    return sorted(v for v in values if v)


def family_options(df: pd.DataFrame) -> list[str]:
    temp = (
        df[["FAMILY_CODE", "FAMILY_NAME"]]
        .drop_duplicates()
        .assign(
            LABEL=lambda x: np.where(
                x["FAMILY_NAME"].map(clean_text) != "",
                x["FAMILY_CODE"].map(clean_text) + " - " + x["FAMILY_NAME"].map(clean_text),
                x["FAMILY_CODE"].map(clean_text),
            )
        )
    )
    return sorted(v for v in temp["LABEL"].map(clean_text).tolist() if v)


def vendor_options(df: pd.DataFrame) -> list[str]:
    temp = (
        df[["VENDOR_NUMBER", "VENDOR_NAME"]]
        .drop_duplicates()
        .assign(
            LABEL=lambda x: np.where(
                x["VENDOR_NAME"].map(clean_text) != "",
                x["VENDOR_NUMBER"].map(clean_text) + " - " + x["VENDOR_NAME"].map(clean_text),
                x["VENDOR_NUMBER"].map(clean_text),
            )
        )
    )
    return sorted(v for v in temp["LABEL"].map(clean_text).tolist() if v)


def item_options(df: pd.DataFrame, limit: int = 2500) -> list[str]:
    ranked = df.sort_values(["REVENUE_365D", "UNITS_SOLD_365D"], ascending=[False, False]).head(limit)
    return [
        f"{row.ITEM_NUMBER} - {row.DESCRIPTION}".strip(" -")
        for row in ranked[["ITEM_NUMBER", "DESCRIPTION"]].itertuples(index=False)
        if clean_text(row.ITEM_NUMBER)
    ]


def _label_key(label: str) -> str:
    return norm_key(str(label).split(" - ", 1)[0])


def apply_ui_filters(
    df: pd.DataFrame,
    divisions: list[str],
    families: list[str],
    collections: list[str],
    vendors: list[str],
    items: list[str],
    text_search: str = "",
    priced_only: bool = True,
) -> pd.DataFrame:
    out = df.copy()
    if priced_only:
        out = out[out["SALE_PRICE"] > 0].copy()
    if divisions:
        out = out[out["DIVISION_KEY"].isin({norm_key(v) for v in divisions})].copy()
    if families:
        out = out[out["FAMILY_KEY"].isin({_label_key(v) for v in families})].copy()
    if collections:
        out = out[out["COLLECTION_KEY"].isin({norm_key(v) for v in collections})].copy()
    if vendors:
        out = out[out["VENDOR_KEY"].isin({_label_key(v) for v in vendors})].copy()
    if items:
        out = out[out["ITEM_KEY"].isin({_label_key(v) for v in items})].copy()
    search = norm_key(text_search)
    if search:
        haystack = (out["ITEM_NUMBER"] + " " + out["DESCRIPTION"]).str.upper()
        out = out[haystack.str.contains(search, regex=False, na=False)].copy()
    return out.reset_index(drop=True)


def _column_config(df: pd.DataFrame, st) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for col in df.columns:
        if col in MONEY_COLUMNS:
            config[col] = st.column_config.NumberColumn(col, format="$%,.2f")
        elif col in PERCENT_COLUMNS or col == "Sample Flooring Margin %":
            config[col] = st.column_config.NumberColumn(col, format="%,.2f%%")
        elif col in NUMBER_COLUMNS:
            config[col] = st.column_config.NumberColumn(col, format="%,.2f")
    return config


def _download_button(df: pd.DataFrame, label: str, file_name: str, key: str, st) -> None:
    st.download_button(
        label,
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=file_name,
        mime="text/csv",
        disabled=df.empty,
        key=key,
        width="stretch",
    )


def _css() -> str:
    return """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');
    html, body, [class*="main"] {
        font-family: 'Manrope', Arial, sans-serif !important;
        background: #f5f7f8;
        color: #1f2933;
    }
    .block-container {
        max-width: 1540px;
        padding-top: 1rem;
        padding-bottom: 2rem;
    }
    h1, h2, h3 {
        letter-spacing: 0;
    }
    div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #d9e1e8;
        border-radius: 8px;
        padding: 12px 14px;
    }
    div[data-testid="stMetric"] label {
        color: #607086 !important;
        font-weight: 700;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #202938;
        font-size: 1.4rem;
        font-weight: 800;
    }
    label, .stSelectbox label, .stMultiSelect label, .stTextInput label, .stNumberInput label, .stSlider label {
        color: #354154 !important;
        font-weight: 800 !important;
    }
    .stButton button {
        border-radius: 7px !important;
        font-weight: 800 !important;
    }
    </style>
    """


def run_streamlit_app() -> None:
    import streamlit as st

    from gartman_connection import get_connection

    st.set_page_config(
        page_title="Marketing Offer Margin Simulator",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(_css(), unsafe_allow_html=True)
    st.title("Marketing Offer Margin Simulator")

    @st.cache_data(ttl=600, show_spinner=False)
    def _cached_items(query_version: int) -> tuple[pd.DataFrame, datetime]:
        _ = query_version
        conn = get_connection()
        try:
            return fetch_offer_margin_items(conn), datetime.now()
        finally:
            conn.close()

    try:
        with st.spinner("Loading Gartman item, cost, and trailing sales data..."):
            items, loaded_at = _cached_items(QUERY_VERSION)
    except Exception as exc:
        st.error(f"Could not load Gartman data: {exc}")
        return

    filter_cols = st.columns([0.9, 1.15, 1.25, 1.45, 1.65])
    with filter_cols[0]:
        selected_divisions = st.multiselect("Division", option_values(items, "DIVISION"), key="campaign_divisions")
    with filter_cols[1]:
        selected_families = st.multiselect("Family", family_options(items), key="campaign_families")
    with filter_cols[2]:
        selected_collections = st.multiselect("Collection", option_values(items, "COLLECTION"), key="campaign_collections")
    with filter_cols[3]:
        selected_vendors = st.multiselect("Vendor Number / Name", vendor_options(items), key="campaign_vendors")
    with filter_cols[4]:
        selected_items = st.multiselect("Item Number / Description", item_options(items), key="campaign_items")

    setup_cols = st.columns([1.2, 1.0, 1.0, 0.95, 0.9, 0.75])
    with setup_cols[0]:
        discount_type = st.selectbox("Discount Type", DISCOUNT_TYPES, key="campaign_discount_type")
    with setup_cols[1]:
        discount_value = st.number_input(
            offer_value_label(discount_type),
            min_value=0.0,
            value=float(default_offer_value(discount_type)),
            step=1.0 if discount_type != "Price Drop" else 0.25,
            key=f"campaign_discount_value_{discount_type}",
        )
    with setup_cols[2]:
        result_limit = st.slider("Displayed Results", min_value=5, max_value=100, value=DEFAULT_RESULT_LIMIT, step=5)
    with setup_cols[3]:
        item_text = st.text_input("Item Text", value="", key="campaign_item_text")
    with setup_cols[4]:
        priced_only = st.checkbox("Priced Items Only", value=True, key="campaign_priced_only")
    with setup_cols[5]:
        st.markdown("<div style='height: 1.85rem;'></div>", unsafe_allow_html=True)
        calculate = st.button("Calculate", type="primary", width="stretch")

    filtered = apply_ui_filters(
        items,
        selected_divisions,
        selected_families,
        selected_collections,
        selected_vendors,
        selected_items,
        text_search=item_text,
        priced_only=priced_only,
    )

    bundle_summary = pd.DataFrame()
    if discount_type in {"Bundle", "BOGO"}:
        bundle_cols = st.columns([1.8, 0.85, 0.85, 0.85])
        bundle_options = item_options(filtered, limit=1000)
        with bundle_cols[0]:
            bundle_item_label = st.selectbox(
                "Bundled Sale Item",
                bundle_options if bundle_options else [""],
                key="campaign_bundle_item",
            )
        with bundle_cols[1]:
            sample_sqft = st.number_input("Threshold SQFT", min_value=0.0, value=500.0, step=50.0)
        with bundle_cols[2]:
            sample_price_per_sf = st.number_input("Sample Flooring $/SF", min_value=0.0, value=5.0, step=0.25)
        with bundle_cols[3]:
            sample_margin_pct = st.number_input("Sample Flooring Margin %", value=40.0, disabled=True)
    else:
        bundle_item_label = ""
        sample_sqft = 500.0
        sample_price_per_sf = 5.0
        sample_margin_pct = 40.0

    if calculate:
        config = OfferConfig(
            discount_type=discount_type,
            magnitude=float(discount_value),
            sample_flooring_sqft=float(sample_sqft),
            sample_flooring_sale_per_sf=float(sample_price_per_sf),
            sample_flooring_margin_pct=float(sample_margin_pct),
        )
        impact = calculate_offer_margin_impact(filtered, config)
        if discount_type in {"Bundle", "BOGO"} and not impact.empty and bundle_item_label:
            selected_key = _label_key(bundle_item_label)
            selected_rows = impact[impact["Item Number"].map(norm_key) == selected_key]
            if not selected_rows.empty:
                bundle_summary = build_bundle_order_summary(selected_rows.iloc[0], config)
        st.session_state["campaign_margin_result"] = impact
        st.session_state["campaign_margin_bundle"] = bundle_summary
        st.session_state["campaign_margin_limit"] = result_limit
        st.session_state["campaign_margin_loaded_at"] = loaded_at

    impact = st.session_state.get("campaign_margin_result", pd.DataFrame())
    bundle_summary = st.session_state.get("campaign_margin_bundle", pd.DataFrame())
    result_limit = int(st.session_state.get("campaign_margin_limit", result_limit))

    metric_cols = st.columns(5)
    metric_cols[0].metric("Matching Items", f"{len(filtered):,}")
    metric_cols[1].metric("Loaded Items", f"{len(items):,}")
    metric_cols[2].metric("Median Sale Price", f"${filtered['SALE_PRICE'].median():,.2f}" if not filtered.empty else "$0.00")
    metric_cols[3].metric("Trailing Revenue", f"${filtered['REVENUE_365D'].sum():,.0f}")
    metric_cols[4].metric("Data Loaded", loaded_at.strftime("%m/%d %I:%M %p"))

    if impact.empty:
        st.info("Choose filters and an offer, then calculate.")
        return

    lowest = lowest_margin_items(impact, result_limit)
    top = top_selling_items(impact, result_limit)
    pane_cols = st.columns(2)
    with pane_cols[0]:
        st.subheader("Lowest Margin if Live")
        st.dataframe(
            lowest,
            hide_index=True,
            width="stretch",
            column_config=_column_config(lowest, st),
        )
        _download_button(lowest, "Download Lowest Margin CSV", "campaign_lowest_margin.csv", "campaign_lowest_download", st)
    with pane_cols[1]:
        st.subheader("Top Selling Included Items")
        st.dataframe(
            top,
            hide_index=True,
            width="stretch",
            column_config=_column_config(top, st),
        )
        _download_button(top, "Download Top Sellers CSV", "campaign_top_sellers.csv", "campaign_top_download", st)

    if not bundle_summary.empty:
        st.subheader("Bundle / BOGO Example Order")
        st.dataframe(
            bundle_summary,
            hide_index=True,
            width="stretch",
            column_config=_column_config(bundle_summary, st),
        )


def main() -> None:
    run_streamlit_app()


if __name__ == "__main__":
    main()
