"""
Sales rewards program query and projection helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import numpy as np
import pandas as pd

from core.db_connection import connect, sql_escape


DB_CACHE_TTL_SECONDS = 300
TIER_ORDER = ("Diamond", "Platinum", "Gold")
TIER_SORT = {"Diamond": 1, "Platinum": 2, "Gold": 3}


@dataclass(frozen=True)
class RewardProgramInputs:
    gold_threshold: float
    platinum_threshold: float
    diamond_threshold: float
    gold_discount_per_sf: float
    platinum_discount_per_sf: float
    diamond_discount_per_sf: float
    platinum_div3_discount_pct: float
    diamond_div3_discount_pct: float
    diamond_cash_back_pct: float
    quantity_lift_pct: float
    waive_delivery_fees: bool = False


def normalize_thresholds(
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
) -> tuple[float, float, float]:
    """Keep the tier cutoffs monotonic: Gold <= Platinum <= Diamond."""
    gold = max(0.0, float(gold_threshold))
    platinum = max(gold, float(platinum_threshold))
    diamond = max(platinum, float(diamond_threshold))
    return gold, platinum, diamond


def _money_literal(value: float) -> str:
    """Return a DB2-safe decimal literal for generated SQL."""
    return f"{max(0.0, float(value)):.2f}"


def build_rewards_sales_sql(
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
) -> str:
    """
    Build the Gartman SQL for eligible customers and their 365-day item sales.

    The historical window is the 365 days before today, excluding today. Customer
    tiering is based on total billed sales across all divisions in that window.
    Item-level detail is then returned for those same customers and same dates.
    """
    gold, platinum, diamond = normalize_thresholds(
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )
    gold_sql = _money_literal(gold)
    platinum_sql = _money_literal(platinum)
    diamond_sql = _money_literal(diamond)

    return f"""
WITH
DateBounds AS (
    SELECT
        CURRENT_DATE - 365 DAYS AS START_DATE,
        CURRENT_DATE - 1 DAYS AS END_DATE,
        CURRENT_DATE AS AS_OF_DATE
    FROM SYSIBM.SYSDUMMY1
),
CustomerTotals AS (
    SELECT
        TRIM(H.SHCUST) AS CUSTOMER_NUMBER,
        COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)') AS CUSTOMER_NAME,
        RTRIM(CHAR(CM.CMCLAS)) AS CUSTOMER_CLASS,
        RTRIM(CHAR(CM.CMSLMN)) AS OUTSIDE_SALES_REP,
        SUM(COALESCE(L.SLENET, 0)) AS CUSTOMER_SALES
    FROM DateBounds D
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = 1
    JOIN GSFL2K.SHLINE L
      ON L.SLCO = H.SHCO
     AND L.SLLOC = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    LEFT JOIN GSFL2K.CUSTMAST CM
      ON CM.CMCUST = H.SHCUST
     AND CM.CMCO = H.SHCO
    WHERE H.SHIDAT BETWEEN D.START_DATE AND D.END_DATE
      AND TRIM(H.SHCUST) <> ''
    GROUP BY
        TRIM(H.SHCUST),
        COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)'),
        RTRIM(CHAR(CM.CMCLAS)),
        RTRIM(CHAR(CM.CMSLMN))
    HAVING SUM(COALESCE(L.SLENET, 0)) >= DECIMAL({gold_sql}, 18, 2)
),
TieredCustomers AS (
    SELECT
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        CUSTOMER_CLASS,
        OUTSIDE_SALES_REP,
        CUSTOMER_SALES,
        CASE
            WHEN CUSTOMER_SALES >= DECIMAL({diamond_sql}, 18, 2) THEN 'Diamond'
            WHEN CUSTOMER_SALES >= DECIMAL({platinum_sql}, 18, 2) THEN 'Platinum'
            ELSE 'Gold'
        END AS TIER,
        CASE
            WHEN CUSTOMER_SALES >= DECIMAL({diamond_sql}, 18, 2) THEN 1
            WHEN CUSTOMER_SALES >= DECIMAL({platinum_sql}, 18, 2) THEN 2
            ELSE 3
        END AS TIER_SORT
    FROM CustomerTotals
),
DeliveryFees AS (
    SELECT
        TRIM(H.SHCUST) AS CUSTOMER_NUMBER,
        COUNT(DISTINCT CASE
            WHEN UPPER(TRIM(H.SHVIA)) LIKE '%TRUCK%'
             AND COALESCE(H.SHSPC2, 0) > 0
            THEN (
                RTRIM(CHAR(H.SHCO)) || '|'
                || RTRIM(CHAR(H.SHLOC)) || '|'
                || RTRIM(CHAR(H.SHINV#))
            )
        END) AS DELIVERY_ORDER_COUNT,
        SUM(CASE
            WHEN UPPER(TRIM(H.SHVIA)) LIKE '%TRUCK%'
             AND COALESCE(H.SHSPC2, 0) > 0
            THEN COALESCE(H.SHSPC2, 0)
            ELSE 0
        END) AS DELIVERY_FEE_REVENUE
    FROM DateBounds D
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = 1
    JOIN TieredCustomers TC
      ON TRIM(H.SHCUST) = TC.CUSTOMER_NUMBER
    WHERE H.SHIDAT BETWEEN D.START_DATE AND D.END_DATE
    GROUP BY TRIM(H.SHCUST)
)
SELECT
    D.START_DATE AS "Window Start",
    D.END_DATE AS "Window End",
    D.AS_OF_DATE AS "As Of Date",
    TC.TIER AS "Tier",
    TC.TIER_SORT AS "Tier Sort",
    TC.CUSTOMER_NUMBER AS "Customer Number",
    TC.CUSTOMER_NAME AS "Customer Name",
    TC.CUSTOMER_CLASS AS "Customer Class",
    TC.OUTSIDE_SALES_REP AS "Outside Sales Rep",
    DECIMAL(TC.CUSTOMER_SALES, 18, 2) AS "Customer 365-Day Sales",
    COALESCE(DF.DELIVERY_ORDER_COUNT, 0) AS "Applicable Delivery Orders",
    DECIMAL(COALESCE(DF.DELIVERY_FEE_REVENUE, 0), 18, 2) AS "Customer Delivery Fee Revenue",
    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",
    TRIM(L.SLITEM) AS "Item Number",
    TRIM(IM.IMDESC) AS "Description",
    RTRIM(CHAR(IM.IMDIV)) AS "Division",
    TRIM(IM.IMUM2) AS "Sales UOM",
    DECIMAL(SUM(COALESCE(L.SLBLUO, 0)), 18, 2) AS "Units Sold",
    DECIMAL(SUM(COALESCE(L.SLENET, 0)), 18, 2) AS "Status Quo Sales",
    COUNT(DISTINCT (
        RTRIM(CHAR(H.SHCO)) || '|'
        || RTRIM(CHAR(H.SHLOC)) || '|'
        || RTRIM(CHAR(H.SHINV#))
    )) AS "Orders Count"
FROM DateBounds D
JOIN TieredCustomers TC
  ON 1 = 1
JOIN GSFL2K.SHHEAD H
  ON H.SHCO = 1
 AND TRIM(H.SHCUST) = TC.CUSTOMER_NUMBER
LEFT JOIN DeliveryFees DF
  ON DF.CUSTOMER_NUMBER = TC.CUSTOMER_NUMBER
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO
 AND L.SLLOC = H.SHLOC
 AND L.SLINV# = H.SHINV#
 AND L.SLORD# = H.SHORD#
JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = L.SLITEM
WHERE H.SHIDAT BETWEEN D.START_DATE AND D.END_DATE
  AND TRIM(L.SLITEM) <> ''
GROUP BY
    D.START_DATE,
    D.END_DATE,
    D.AS_OF_DATE,
    TC.TIER,
    TC.TIER_SORT,
    TC.CUSTOMER_NUMBER,
    TC.CUSTOMER_NAME,
    TC.CUSTOMER_CLASS,
    TC.OUTSIDE_SALES_REP,
    TC.CUSTOMER_SALES,
    COALESCE(DF.DELIVERY_ORDER_COUNT, 0),
    DECIMAL(COALESCE(DF.DELIVERY_FEE_REVENUE, 0), 18, 2),
    RTRIM(CHAR(H.SHLOC)),
    TRIM(L.SLITEM),
    TRIM(IM.IMDESC),
    RTRIM(CHAR(IM.IMDIV)),
    TRIM(IM.IMUM2)
ORDER BY
    TC.TIER_SORT,
    TC.CUSTOMER_SALES DESC,
    SUM(COALESCE(L.SLENET, 0)) DESC,
    TC.CUSTOMER_NUMBER,
    TRIM(L.SLITEM)
FOR READ ONLY
"""


def load_rewards_sales_data(
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
) -> tuple[pd.DataFrame, datetime]:
    """Query Gartman for the eligible customer control group."""
    sql = build_rewards_sales_sql(
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )
    queried_at = datetime.now()
    with connect() as conn:
        df = pd.read_sql(sql, conn)
    return clean_rewards_sales_data(df), queried_at


def build_customer_order_audit_sql(
    customer_number: str,
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
) -> str:
    """Build an order-line Gartman query for auditing one customer's reward math."""
    customer_sql = sql_escape(str(customer_number).strip())
    gold, platinum, diamond = normalize_thresholds(
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )
    gold_sql = _money_literal(gold)
    platinum_sql = _money_literal(platinum)
    diamond_sql = _money_literal(diamond)

    return f"""
WITH
DateBounds AS (
    SELECT
        CURRENT_DATE - 365 DAYS AS START_DATE,
        CURRENT_DATE - 1 DAYS AS END_DATE,
        CURRENT_DATE AS AS_OF_DATE
    FROM SYSIBM.SYSDUMMY1
),
CustomerTotals AS (
    SELECT
        TRIM(H.SHCUST) AS CUSTOMER_NUMBER,
        COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)') AS CUSTOMER_NAME,
        RTRIM(CHAR(CM.CMCLAS)) AS CUSTOMER_CLASS,
        RTRIM(CHAR(CM.CMSLMN)) AS OUTSIDE_SALES_REP,
        SUM(COALESCE(L.SLENET, 0)) AS CUSTOMER_SALES
    FROM DateBounds D
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = 1
    JOIN GSFL2K.SHLINE L
      ON L.SLCO = H.SHCO
     AND L.SLLOC = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    LEFT JOIN GSFL2K.CUSTMAST CM
      ON CM.CMCUST = H.SHCUST
     AND CM.CMCO = H.SHCO
    WHERE H.SHIDAT BETWEEN D.START_DATE AND D.END_DATE
      AND TRIM(H.SHCUST) = '{customer_sql}'
    GROUP BY
        TRIM(H.SHCUST),
        COALESCE(TRIM(CM.CMNAME), '(Unknown Customer)'),
        RTRIM(CHAR(CM.CMCLAS)),
        RTRIM(CHAR(CM.CMSLMN))
),
TieredCustomer AS (
    SELECT
        CUSTOMER_NUMBER,
        CUSTOMER_NAME,
        CUSTOMER_CLASS,
        OUTSIDE_SALES_REP,
        CUSTOMER_SALES,
        CASE
            WHEN CUSTOMER_SALES >= DECIMAL({diamond_sql}, 18, 2) THEN 'Diamond'
            WHEN CUSTOMER_SALES >= DECIMAL({platinum_sql}, 18, 2) THEN 'Platinum'
            WHEN CUSTOMER_SALES >= DECIMAL({gold_sql}, 18, 2) THEN 'Gold'
            ELSE 'Below Gold'
        END AS TIER,
        CASE
            WHEN CUSTOMER_SALES >= DECIMAL({diamond_sql}, 18, 2) THEN 1
            WHEN CUSTOMER_SALES >= DECIMAL({platinum_sql}, 18, 2) THEN 2
            WHEN CUSTOMER_SALES >= DECIMAL({gold_sql}, 18, 2) THEN 3
            ELSE 99
        END AS TIER_SORT
    FROM CustomerTotals
)
SELECT
    D.START_DATE AS "Window Start",
    D.END_DATE AS "Window End",
    D.AS_OF_DATE AS "As Of Date",
    TC.TIER AS "Tier",
    TC.TIER_SORT AS "Tier Sort",
    TC.CUSTOMER_NUMBER AS "Customer Number",
    TC.CUSTOMER_NAME AS "Customer Name",
    TC.CUSTOMER_CLASS AS "Customer Class",
    TC.OUTSIDE_SALES_REP AS "Outside Sales Rep",
    DECIMAL(TC.CUSTOMER_SALES, 18, 2) AS "Customer 365-Day Sales",
    (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#))) AS "Order Key",
    H.SHIDAT AS "Invoice Date",
    RTRIM(CHAR(H.SHLOC)) AS "Branch Location",
    TRIM(CHAR(H.SHORD#)) AS "Order Number",
    TRIM(H.SHINV#) AS "Invoice Number",
    TRIM(H.SHVIA) AS "Ship Via",
    DECIMAL(COALESCE(H.SHSPC1, 0), 18, 2) AS "Header Special Charge 1",
    DECIMAL(COALESCE(H.SHSPC2, 0), 18, 2) AS "Header Special Charge 2",
    DECIMAL(COALESCE(H.SHSPC3, 0), 18, 2) AS "Header Special Charge 3",
    DECIMAL(COALESCE(H.SHSPC4, 0), 18, 2) AS "Header Special Charge 4",
    DECIMAL(COALESCE(H.SHSPC5, 0), 18, 2) AS "Header Special Charge 5",
    DECIMAL(COALESCE(H.SHADDA, 0), 18, 2) AS "Header Additional Amount",
    DECIMAL(
        CASE
            WHEN UPPER(TRIM(H.SHVIA)) LIKE '%TRUCK%'
             AND COALESCE(H.SHSPC2, 0) > 0
            THEN COALESCE(H.SHSPC2, 0)
            ELSE 0
        END,
        18,
        2
    ) AS "Order Delivery Fee Revenue",
    L.SLSEQ# AS "Line Sequence",
    L.SLLINE AS "Line Number",
    TRIM(L.SLITEM) AS "Item Number",
    COALESCE(NULLIF(TRIM(IM.IMDESC), ''), NULLIF(TRIM(L.SLDESC), ''), '') AS "Description",
    RTRIM(CHAR(COALESCE(IM.IMDIV, L.SLDIV, 0))) AS "Division",
    COALESCE(NULLIF(TRIM(IM.IMUM2), ''), NULLIF(TRIM(L.SLUM2), ''), NULLIF(TRIM(L.SLUM1), ''), '') AS "Sales UOM",
    DECIMAL(COALESCE(L.SLBLUO, 0), 18, 2) AS "Units Sold",
    DECIMAL(COALESCE(L.SLENET, 0), 18, 2) AS "Status Quo Sales"
FROM DateBounds D
JOIN TieredCustomer TC
  ON 1 = 1
JOIN GSFL2K.SHHEAD H
  ON H.SHCO = 1
 AND TRIM(H.SHCUST) = TC.CUSTOMER_NUMBER
JOIN GSFL2K.SHLINE L
  ON L.SLCO = H.SHCO
 AND L.SLLOC = H.SHLOC
 AND L.SLINV# = H.SHINV#
 AND L.SLORD# = H.SHORD#
LEFT JOIN GSFL2K.ITEMMAST IM
  ON IM.IMITEM = L.SLITEM
WHERE H.SHIDAT BETWEEN D.START_DATE AND D.END_DATE
ORDER BY
    H.SHIDAT DESC,
    RTRIM(CHAR(H.SHLOC)),
    TRIM(H.SHINV#),
    L.SLSEQ#,
    L.SLLINE
FOR READ ONLY
"""


def load_customer_order_audit_data(
    customer_number: str,
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
) -> tuple[pd.DataFrame, datetime]:
    """Query order-line detail for one customer over the reward lookback window."""
    sql = build_customer_order_audit_sql(
        customer_number,
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )
    queried_at = datetime.now()
    with connect() as conn:
        df = pd.read_sql(sql, conn)
    return clean_customer_order_audit_data(df), queried_at


def clean_rewards_sales_data(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    date_columns = ("Window Start", "Window End", "As Of Date")
    for column in date_columns:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], errors="coerce")

    numeric_columns = (
        "Tier Sort",
        "Customer 365-Day Sales",
        "Applicable Delivery Orders",
        "Customer Delivery Fee Revenue",
        "Units Sold",
        "Status Quo Sales",
        "Orders Count",
    )
    for column in numeric_columns:
        if column not in out.columns:
            out[column] = 0.0
        out[column] = pd.to_numeric(out[column], errors="coerce").fillna(0.0)

    string_columns = (
        "Tier",
        "Customer Number",
        "Customer Name",
        "Customer Class",
        "Outside Sales Rep",
        "Branch Location",
        "Item Number",
        "Description",
        "Division",
        "Sales UOM",
    )
    for column in string_columns:
        if column in out.columns:
            out[column] = out[column].fillna("").astype(str).str.strip()
    return out


def clean_customer_order_audit_data(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    date_columns = ("Window Start", "Window End", "As Of Date", "Invoice Date")
    for column in date_columns:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], errors="coerce")

    numeric_columns = (
        "Tier Sort",
        "Customer 365-Day Sales",
        "Header Special Charge 1",
        "Header Special Charge 2",
        "Header Special Charge 3",
        "Header Special Charge 4",
        "Header Special Charge 5",
        "Header Additional Amount",
        "Order Delivery Fee Revenue",
        "Line Sequence",
        "Line Number",
        "Units Sold",
        "Status Quo Sales",
    )
    for column in numeric_columns:
        if column not in out.columns:
            out[column] = 0.0
        out[column] = pd.to_numeric(out[column], errors="coerce").fillna(0.0)

    string_columns = (
        "Tier",
        "Customer Number",
        "Customer Name",
        "Customer Class",
        "Outside Sales Rep",
        "Order Key",
        "Branch Location",
        "Order Number",
        "Invoice Number",
        "Ship Via",
        "Item Number",
        "Description",
        "Division",
        "Sales UOM",
    )
    for column in string_columns:
        if column in out.columns:
            out[column] = out[column].fillna("").astype(str).str.strip()
    return out


def calculate_rewards_projection(
    sales_df: pd.DataFrame,
    inputs: RewardProgramInputs,
) -> pd.DataFrame:
    """Apply projected unit lift, Division 1 discounts, and Diamond cash back."""
    if sales_df.empty:
        return sales_df.copy()

    out = clean_rewards_sales_data(sales_df)
    discount_by_tier = {
        "Gold": max(0.0, float(inputs.gold_discount_per_sf)),
        "Platinum": max(0.0, float(inputs.platinum_discount_per_sf)),
        "Diamond": max(0.0, float(inputs.diamond_discount_per_sf)),
    }
    div3_discount_by_tier = {
        "Gold": 0.0,
        "Platinum": max(0.0, float(inputs.platinum_div3_discount_pct)) / 100.0,
        "Diamond": max(
            float(inputs.platinum_div3_discount_pct),
            float(inputs.diamond_div3_discount_pct),
            0.0,
        )
        / 100.0,
    }
    lift_rate = max(-1.0, float(inputs.quantity_lift_pct) / 100.0)
    cash_back_rate = max(0.0, float(inputs.diamond_cash_back_pct) / 100.0)

    out["Per SF Discount"] = out["Tier"].map(discount_by_tier).fillna(0.0)
    out["Division 3 Discount %"] = out["Tier"].map(div3_discount_by_tier).fillna(0.0) * 100.0
    out["Projected Units"] = out["Units Sold"] * (1.0 + lift_rate)
    out["Projected Gross Sales"] = out["Status Quo Sales"] * (1.0 + lift_rate)

    division_1 = out["Division"].astype(str).str.strip() == "1"
    positive_units = out["Projected Units"].clip(lower=0.0)
    raw_discount = np.where(division_1, positive_units * out["Per SF Discount"], 0.0)
    max_discount = out["Projected Gross Sales"].clip(lower=0.0)
    out["Square Foot Discount Cost"] = np.minimum(raw_discount, max_discount)
    remaining_after_sf_discount = (
        out["Projected Gross Sales"] - out["Square Foot Discount Cost"]
    ).clip(lower=0.0)

    division_3 = out["Division"].astype(str).str.strip() == "3"
    raw_div3_discount = np.where(
        division_3,
        remaining_after_sf_discount * (out["Division 3 Discount %"] / 100.0),
        0.0,
    )
    out["Division 3 Discount Cost"] = np.minimum(raw_div3_discount, remaining_after_sf_discount)

    out["Program Revenue After Discount"] = (
        out["Projected Gross Sales"] - out["Square Foot Discount Cost"] - out["Division 3 Discount Cost"]
    )
    cash_back_base = out["Program Revenue After Discount"].clip(lower=0.0)
    out["Cash Back Cost"] = np.where(out["Tier"] == "Diamond", cash_back_base * cash_back_rate, 0.0)

    customer_sales_total = out.groupby("Customer Number")["Status Quo Sales"].transform("sum")
    delivery_fee_revenue = out.groupby("Customer Number")["Customer Delivery Fee Revenue"].transform("max")
    allocation_ratio = (
        out["Status Quo Sales"]
        .div(customer_sales_total.where(customer_sales_total > 0))
        .fillna(0.0)
    )
    out["Delivery Fee Waiver Cost"] = np.where(
        bool(inputs.waive_delivery_fees),
        delivery_fee_revenue * allocation_ratio * (1.0 + lift_rate),
        0.0,
    )

    out["Program Revenue"] = (
        out["Program Revenue After Discount"] - out["Cash Back Cost"] - out["Delivery Fee Waiver Cost"]
    )
    out["Incremental Benefit"] = out["Program Revenue"] - out["Status Quo Sales"]
    out["Division 1 Discounted Units"] = np.where(division_1, positive_units, 0.0)
    return out


def calculate_customer_order_audit_projection(
    order_df: pd.DataFrame,
    inputs: RewardProgramInputs,
) -> pd.DataFrame:
    """Apply reward projection math at invoice/order line grain."""
    if order_df.empty:
        return order_df.copy()

    out = clean_customer_order_audit_data(order_df)
    eligible = out["Tier"].isin(TIER_SORT)
    discount_by_tier = {
        "Gold": max(0.0, float(inputs.gold_discount_per_sf)),
        "Platinum": max(0.0, float(inputs.platinum_discount_per_sf)),
        "Diamond": max(0.0, float(inputs.diamond_discount_per_sf)),
    }
    div3_discount_by_tier = {
        "Gold": 0.0,
        "Platinum": max(0.0, float(inputs.platinum_div3_discount_pct)) / 100.0,
        "Diamond": max(
            float(inputs.platinum_div3_discount_pct),
            float(inputs.diamond_div3_discount_pct),
            0.0,
        )
        / 100.0,
    }
    lift_rate = max(-1.0, float(inputs.quantity_lift_pct) / 100.0)
    multiplier = 1.0 + lift_rate
    cash_back_rate = max(0.0, float(inputs.diamond_cash_back_pct) / 100.0)

    out["Per SF Discount"] = np.where(
        eligible,
        out["Tier"].map(discount_by_tier).fillna(0.0),
        0.0,
    )
    out["Division 3 Discount %"] = np.where(
        eligible,
        out["Tier"].map(div3_discount_by_tier).fillna(0.0) * 100.0,
        0.0,
    )
    out["Projected Units"] = out["Units Sold"] * multiplier
    out["Projected Gross Sales"] = out["Status Quo Sales"] * multiplier

    division_1 = out["Division"].astype(str).str.strip() == "1"
    division_3 = out["Division"].astype(str).str.strip() == "3"
    positive_units = out["Projected Units"].clip(lower=0.0)
    projected_positive_sales = out["Projected Gross Sales"].clip(lower=0.0)

    raw_sf_discount = np.where(
        eligible & division_1,
        positive_units * out["Per SF Discount"],
        0.0,
    )
    out["Square Foot Discount Cost"] = np.minimum(raw_sf_discount, projected_positive_sales)
    remaining_after_sf_discount = (
        out["Projected Gross Sales"] - out["Square Foot Discount Cost"]
    ).clip(lower=0.0)
    raw_div3_discount = np.where(
        eligible & division_3,
        remaining_after_sf_discount * (out["Division 3 Discount %"] / 100.0),
        0.0,
    )
    out["Division 3 Discount Cost"] = np.minimum(raw_div3_discount, remaining_after_sf_discount)
    out["Program Revenue After Discount"] = (
        out["Projected Gross Sales"] - out["Square Foot Discount Cost"] - out["Division 3 Discount Cost"]
    )
    cash_back_base = out["Program Revenue After Discount"].clip(lower=0.0)
    out["Cash Back Cost"] = np.where(eligible & (out["Tier"] == "Diamond"), cash_back_base * cash_back_rate, 0.0)

    order_sales_total = out.groupby("Order Key")["Status Quo Sales"].transform("sum")
    order_delivery_fee = out.groupby("Order Key")["Order Delivery Fee Revenue"].transform("max")
    order_delivery_waiver = np.where(
        bool(inputs.waive_delivery_fees) & eligible,
        order_delivery_fee * multiplier,
        0.0,
    )
    line_delivery_ratio = (
        out["Status Quo Sales"]
        .div(order_sales_total.where(order_sales_total > 0))
        .fillna(0.0)
    )
    out["Order Delivery Fee Waiver Cost"] = order_delivery_waiver
    out["Line Delivery Fee Waiver Allocation"] = order_delivery_waiver * line_delivery_ratio
    out["Delivery Fee Waiver Applied"] = np.where(order_delivery_waiver > 0.005, "Yes", "No")
    out["Program Revenue"] = (
        out["Program Revenue After Discount"] - out["Cash Back Cost"] - out["Line Delivery Fee Waiver Allocation"]
    )
    out["Incremental Benefit"] = out["Program Revenue"] - out["Status Quo Sales"]
    out["Item Discount Type"] = np.select(
        [
            out["Square Foot Discount Cost"].abs() > 0.005,
            out["Division 3 Discount Cost"].abs() > 0.005,
        ],
        ["Division 1 per-SF", "Division 3 percent"],
        default="",
    )
    return out


def calculate_breakeven_quantity_lift_pct(
    sales_df: pd.DataFrame,
    inputs: RewardProgramInputs,
) -> float | None:
    """
    Return the unit quantity lift where program revenue equals status quo.

    This uses the same projection math with lift set to zero to find the net
    program revenue produced by the existing unit volume after discounts and
    cash back. Since all projected units and sales scale by the same multiplier,
    breakeven is status quo sales divided by that no-lift program revenue.
    """
    if sales_df.empty:
        return None

    no_lift_inputs = replace(inputs, quantity_lift_pct=0.0)
    no_lift_projection = calculate_rewards_projection(sales_df, no_lift_inputs)
    status_quo_sales = float(no_lift_projection["Status Quo Sales"].sum())
    no_lift_program_revenue = float(no_lift_projection["Program Revenue"].sum())

    if status_quo_sales <= 0 or no_lift_program_revenue <= 0:
        return None
    return ((status_quo_sales / no_lift_program_revenue) - 1.0) * 100.0


def summarize_order_audit(projected_order_df: pd.DataFrame) -> pd.DataFrame:
    """Return one row per audited invoice/order."""
    if projected_order_df.empty:
        return pd.DataFrame()

    grouped = (
        projected_order_df.groupby(
            [
                "Order Key",
                "Invoice Date",
                "Branch Location",
                "Order Number",
                "Invoice Number",
                "Ship Via",
                "Tier",
                "Customer Number",
                "Customer Name",
            ],
            dropna=False,
        )
        .agg(
            **{
                "Line Count": ("Line Sequence", "count"),
                "Status Quo Sales": ("Status Quo Sales", "sum"),
                "Projected Gross Sales": ("Projected Gross Sales", "sum"),
                "Square Foot Discount Cost": ("Square Foot Discount Cost", "sum"),
                "Division 3 Discount Cost": ("Division 3 Discount Cost", "sum"),
                "Cash Back Cost": ("Cash Back Cost", "sum"),
                "Line Delivery Fee Waiver Allocation": ("Line Delivery Fee Waiver Allocation", "sum"),
                "Order Delivery Fee Revenue": ("Order Delivery Fee Revenue", "max"),
                "Order Delivery Fee Waiver Cost": ("Order Delivery Fee Waiver Cost", "max"),
                "Header Special Charge 1": ("Header Special Charge 1", "max"),
                "Header Special Charge 2": ("Header Special Charge 2", "max"),
                "Header Special Charge 3": ("Header Special Charge 3", "max"),
                "Header Special Charge 4": ("Header Special Charge 4", "max"),
                "Header Special Charge 5": ("Header Special Charge 5", "max"),
                "Header Additional Amount": ("Header Additional Amount", "max"),
                "Program Revenue": ("Program Revenue", "sum"),
                "Incremental Benefit": ("Incremental Benefit", "sum"),
            }
        )
        .reset_index()
    )
    grouped["Delivery Fee Waiver Applied"] = np.where(
        grouped["Order Delivery Fee Waiver Cost"] > 0.005,
        "Yes",
        "No",
    )
    return grouped.sort_values(["Invoice Date", "Invoice Number"], ascending=[False, False])


def summarize_projection(projected_df: pd.DataFrame) -> pd.DataFrame:
    """Return tier-level financial summary rows plus an all-tier total."""
    columns = [
        "Tier",
        "Customers",
        "Units Sold",
        "Projected Units",
        "Division 1 Discounted Units",
        "Status Quo Sales",
        "Projected Gross Sales",
        "Square Foot Discount Cost",
        "Division 3 Discount Cost",
        "Program Revenue After Discount",
        "Cash Back Cost",
        "Delivery Fee Waiver Cost",
        "Program Revenue",
        "Incremental Benefit",
        "Applicable Delivery Orders",
        "Customer Delivery Fee Revenue",
    ]
    if projected_df.empty:
        return pd.DataFrame(columns=columns)

    delivery_by_tier = (
        projected_df.groupby(["Tier", "Customer Number"], dropna=False)
        .agg(
            **{
                "Applicable Delivery Orders": ("Applicable Delivery Orders", "max"),
                "Customer Delivery Fee Revenue": ("Customer Delivery Fee Revenue", "max"),
            }
        )
        .reset_index()
        .groupby("Tier", dropna=False)
        .agg(
            **{
                "Applicable Delivery Orders": ("Applicable Delivery Orders", "sum"),
                "Customer Delivery Fee Revenue": ("Customer Delivery Fee Revenue", "sum"),
            }
        )
        .reset_index()
    )

    grouped = (
        projected_df.groupby("Tier", dropna=False)
        .agg(
            Customers=("Customer Number", "nunique"),
            **{
                "Units Sold": ("Units Sold", "sum"),
                "Projected Units": ("Projected Units", "sum"),
                "Division 1 Discounted Units": ("Division 1 Discounted Units", "sum"),
                "Status Quo Sales": ("Status Quo Sales", "sum"),
                "Projected Gross Sales": ("Projected Gross Sales", "sum"),
                "Square Foot Discount Cost": ("Square Foot Discount Cost", "sum"),
                "Division 3 Discount Cost": ("Division 3 Discount Cost", "sum"),
                "Program Revenue After Discount": ("Program Revenue After Discount", "sum"),
                "Cash Back Cost": ("Cash Back Cost", "sum"),
                "Delivery Fee Waiver Cost": ("Delivery Fee Waiver Cost", "sum"),
                "Program Revenue": ("Program Revenue", "sum"),
                "Incremental Benefit": ("Incremental Benefit", "sum"),
            },
        )
        .reset_index()
    )
    grouped = grouped.merge(delivery_by_tier, on="Tier", how="left")
    grouped["Tier Sort"] = grouped["Tier"].map(TIER_SORT).fillna(99)
    grouped = grouped.sort_values("Tier Sort").drop(columns=["Tier Sort"])

    totals = {
        "Tier": "Total",
        "Customers": projected_df["Customer Number"].nunique(),
        "Units Sold": projected_df["Units Sold"].sum(),
        "Projected Units": projected_df["Projected Units"].sum(),
        "Division 1 Discounted Units": projected_df["Division 1 Discounted Units"].sum(),
        "Status Quo Sales": projected_df["Status Quo Sales"].sum(),
        "Projected Gross Sales": projected_df["Projected Gross Sales"].sum(),
        "Square Foot Discount Cost": projected_df["Square Foot Discount Cost"].sum(),
        "Division 3 Discount Cost": projected_df["Division 3 Discount Cost"].sum(),
        "Program Revenue After Discount": projected_df["Program Revenue After Discount"].sum(),
        "Cash Back Cost": projected_df["Cash Back Cost"].sum(),
        "Delivery Fee Waiver Cost": projected_df["Delivery Fee Waiver Cost"].sum(),
        "Program Revenue": projected_df["Program Revenue"].sum(),
        "Incremental Benefit": projected_df["Incremental Benefit"].sum(),
        "Applicable Delivery Orders": (
            projected_df.groupby("Customer Number")["Applicable Delivery Orders"].max().sum()
        ),
        "Customer Delivery Fee Revenue": (
            projected_df.groupby("Customer Number")["Customer Delivery Fee Revenue"].max().sum()
        ),
    }
    return pd.concat([grouped, pd.DataFrame([totals])], ignore_index=True)[columns]


def summarize_customers(projected_df: pd.DataFrame) -> pd.DataFrame:
    """Return customer-level projection detail."""
    if projected_df.empty:
        return pd.DataFrame()

    grouped = (
        projected_df.groupby(
            [
                "Tier",
                "Customer Number",
                "Customer Name",
                "Customer Class",
                "Outside Sales Rep",
            ],
            dropna=False,
        )
        .agg(
            **{
                "Customer 365-Day Sales": ("Customer 365-Day Sales", "max"),
                "Status Quo Sales": ("Status Quo Sales", "sum"),
                "Projected Gross Sales": ("Projected Gross Sales", "sum"),
                "Square Foot Discount Cost": ("Square Foot Discount Cost", "sum"),
                "Division 3 Discount Cost": ("Division 3 Discount Cost", "sum"),
                "Cash Back Cost": ("Cash Back Cost", "sum"),
                "Delivery Fee Waiver Cost": ("Delivery Fee Waiver Cost", "sum"),
                "Program Revenue": ("Program Revenue", "sum"),
                "Incremental Benefit": ("Incremental Benefit", "sum"),
                "Applicable Delivery Orders": ("Applicable Delivery Orders", "max"),
                "Customer Delivery Fee Revenue": ("Customer Delivery Fee Revenue", "max"),
                "Item Count": ("Item Number", "nunique"),
            },
        )
        .reset_index()
    )
    grouped["Tier Sort"] = grouped["Tier"].map(TIER_SORT).fillna(99)
    return grouped.sort_values(
        ["Tier Sort", "Customer 365-Day Sales", "Customer Number"],
        ascending=[True, False, True],
    ).drop(columns=["Tier Sort"])
