"""
Streamlit visualizer for the proposed sales rewards program.

Run:
    .venv\\Scripts\\python.exe -m streamlit run rewards_program_visualizer.py --server.port 8506
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.rewards_program import (
    RewardProgramInputs,
    calculate_breakeven_quantity_lift_pct,
    calculate_customer_order_audit_projection,
    calculate_rewards_projection,
    load_customer_order_audit_data,
    load_rewards_sales_data,
    normalize_thresholds,
    summarize_order_audit,
    summarize_customers,
    summarize_projection,
)


REWARDS_QUERY_VERSION = 3
THRESHOLD_MIN = 10_000
THRESHOLD_MAX = 100_000
THRESHOLD_STEP = 5_000
AUDIT_EXPORT_DIR = Path(".analysis")


st.set_page_config(
    page_title="Sales Rewards Program Visualizer",
    layout="wide",
    initial_sidebar_state="collapsed",
)


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');

html, body, [class*="main"] {
    font-family: 'Manrope', sans-serif !important;
    background: #f4f6f8;
    color: #1f2933;
}

.block-container {
    max-width: 1480px;
    padding-top: 1.1rem;
    padding-bottom: 2rem;
}

h1, h2, h3 {
    letter-spacing: 0;
}

div[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #d8dee7;
    border-radius: 8px;
    padding: 12px 14px;
}

div[data-testid="stMetric"] label {
    color: #657282 !important;
    font-weight: 700;
}

div[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #202938;
    font-size: 1.45rem;
    font-weight: 800;
}

.tier-strip {
    border-left: 5px solid #7c8b9e;
    padding-left: 12px;
    margin-bottom: 10px;
}

.tier-strip.diamond { border-left-color: #0f766e; }
.tier-strip.platinum { border-left-color: #586174; }
.tier-strip.gold { border-left-color: #a16207; }

.tier-name {
    font-size: 0.92rem;
    font-weight: 800;
    color: #202938;
    text-transform: uppercase;
}

.tier-meta {
    color: #697586;
    font-size: 0.82rem;
    font-weight: 600;
}

.section-label {
    color: #3b4656;
    font-weight: 800;
    font-size: 1rem;
    margin: 0.4rem 0 0.25rem 0;
}

.stButton button {
    border-radius: 7px !important;
    font-weight: 700 !important;
}
</style>
"""


CURRENCY_COLUMNS = {
    "Status Quo Sales",
    "Projected Gross Sales",
    "Square Foot Discount Cost",
    "Division 3 Discount Cost",
    "Program Revenue After Discount",
    "Cash Back Cost",
    "Delivery Fee Waiver Cost",
    "Customer Delivery Fee Revenue",
    "Program Revenue",
    "Incremental Benefit",
    "Customer 365-Day Sales",
    "Per SF Discount",
    "Header Special Charge 1",
    "Header Special Charge 2",
    "Header Special Charge 3",
    "Header Special Charge 4",
    "Header Special Charge 5",
    "Header Additional Amount",
    "Order Delivery Fee Revenue",
    "Order Delivery Fee Waiver Cost",
    "Line Delivery Fee Waiver Allocation",
}

NUMERIC_COLUMNS = {
    "Units Sold",
    "Projected Units",
    "Division 1 Discounted Units",
}

PERCENT_COLUMNS = {
    "Division 3 Discount %",
}

INTEGER_COLUMNS = {
    "Customers",
    "Orders Count",
    "Applicable Delivery Orders",
    "Item Count",
    "Line Count",
    "Line Sequence",
    "Line Number",
}


@st.cache_data(ttl=300, show_spinner=False)
def _cached_load_rewards_sales_data(
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
    query_version: int,
) -> tuple[pd.DataFrame, datetime]:
    _ = query_version
    return load_rewards_sales_data(
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )


@st.cache_data(ttl=300, show_spinner=False)
def _cached_load_customer_order_audit_data(
    customer_number: str,
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
    query_version: int,
) -> tuple[pd.DataFrame, datetime]:
    _ = query_version
    return load_customer_order_audit_data(
        customer_number,
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _fmt_rate(value: float) -> str:
    return f"{value:,.2f}%"


def _column_config(df: pd.DataFrame) -> dict:
    config = {}
    for column in df.columns:
        if column in CURRENCY_COLUMNS:
            config[column] = st.column_config.NumberColumn(column, format="$%,.2f")
        elif column in NUMERIC_COLUMNS:
            config[column] = st.column_config.NumberColumn(column, format="%,.2f")
        elif column in PERCENT_COLUMNS:
            config[column] = st.column_config.NumberColumn(column, format="%,.2f%%")
        elif column in INTEGER_COLUMNS:
            config[column] = st.column_config.NumberColumn(column, format="%,d")
    return config


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in str(value).strip())
    return cleaned.strip("_") or "download"


def _download_dataframe(df: pd.DataFrame, label: str, file_name: str, key: str) -> None:
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label,
        data=csv_bytes,
        file_name=file_name,
        mime="text/csv",
        key=key,
        disabled=df.empty,
        on_click="ignore",
        width="stretch",
    )


def _write_audit_export(df: pd.DataFrame, file_name: str) -> Path | None:
    if df.empty:
        return None

    AUDIT_EXPORT_DIR.mkdir(exist_ok=True)
    export_path = AUDIT_EXPORT_DIR / file_name
    df.to_csv(export_path, index=False, encoding="utf-8-sig")
    return export_path.resolve()


def _ensure_threshold_state() -> None:
    defaults = {
        "reward_gold_threshold": 40000,
        "reward_platinum_threshold": 50000,
        "reward_diamond_threshold": 75000,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)

    st.session_state["reward_gold_threshold"] = min(
        THRESHOLD_MAX,
        max(THRESHOLD_MIN, int(st.session_state["reward_gold_threshold"])),
    )
    st.session_state["reward_platinum_threshold"] = min(
        THRESHOLD_MAX,
        max(
            int(st.session_state["reward_gold_threshold"]),
            int(st.session_state["reward_platinum_threshold"]),
        ),
    )
    st.session_state["reward_diamond_threshold"] = min(
        THRESHOLD_MAX,
        max(
            int(st.session_state["reward_platinum_threshold"]),
            int(st.session_state["reward_diamond_threshold"]),
        ),
    )


def _tier_header(name: str, threshold: float, class_name: str) -> None:
    st.markdown(
        f"""
        <div class="tier-strip {class_name}">
          <div class="tier-name">{name}</div>
          <div class="tier-meta">Minimum 365-day billed sales: {_fmt_money(threshold)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _input_model(
    gold_threshold: float,
    platinum_threshold: float,
    diamond_threshold: float,
    gold_discount: float,
    platinum_discount: float,
    diamond_discount: float,
    platinum_div3_discount_pct: float,
    diamond_div3_discount_pct: float,
    diamond_cash_back_pct: float,
    quantity_lift_pct: float,
    waive_delivery_fees: bool,
) -> RewardProgramInputs:
    return RewardProgramInputs(
        gold_threshold=gold_threshold,
        platinum_threshold=platinum_threshold,
        diamond_threshold=diamond_threshold,
        gold_discount_per_sf=gold_discount,
        platinum_discount_per_sf=platinum_discount,
        diamond_discount_per_sf=diamond_discount,
        platinum_div3_discount_pct=platinum_div3_discount_pct,
        diamond_div3_discount_pct=diamond_div3_discount_pct,
        diamond_cash_back_pct=diamond_cash_back_pct,
        quantity_lift_pct=quantity_lift_pct,
        waive_delivery_fees=waive_delivery_fees,
    )


def _render_waterfall(total_row: pd.Series) -> None:
    status_quo = float(total_row["Status Quo Sales"])
    growth = float(total_row["Projected Gross Sales"] - total_row["Status Quo Sales"])
    discount = -float(total_row["Square Foot Discount Cost"])
    div3_discount = -float(total_row["Division 3 Discount Cost"])
    cash_back = -float(total_row["Cash Back Cost"])
    delivery_waiver = -float(total_row["Delivery Fee Waiver Cost"])
    program_revenue = float(total_row["Program Revenue"])

    fig = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=["absolute", "relative", "relative", "relative", "relative", "relative", "total"],
            x=[
                "Status Quo",
                "Unit Lift",
                "Div 1 SF",
                "Div 3 %",
                "Cash Back",
                "Delivery",
                "Program Revenue",
            ],
            y=[status_quo, growth, discount, div3_discount, cash_back, delivery_waiver, program_revenue],
            connector={"line": {"color": "#a9b4c2"}},
            increasing={"marker": {"color": "#0f766e"}},
            decreasing={"marker": {"color": "#b42318"}},
            totals={"marker": {"color": "#202938"}},
        )
    )
    fig.update_layout(
        height=360,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        yaxis_tickprefix="$",
        yaxis_tickformat=",",
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        font={"family": "Manrope, sans-serif", "color": "#202938"},
        showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")


def _render_customer_order_audit(inputs: RewardProgramInputs) -> None:
    st.markdown('<div class="section-label">Customer Order Audit</div>', unsafe_allow_html=True)
    audit_cols = st.columns([1, 1, 2])
    with audit_cols[0]:
        customer_number = st.text_input(
            "Customer number",
            value="WHO103",
            key="reward_audit_customer_number",
        ).strip()
    with audit_cols[1]:
        load_audit = st.button("Load Customer Audit", key="reward_load_customer_audit", width="stretch")
    with audit_cols[2]:
        st.caption(
            "Shows every billed order line for the previous 365 days, plus line-level item discounts "
            "and order-level delivery fee waiver math."
        )

    current_audit_key = (
        customer_number.upper(),
        inputs.gold_threshold,
        inputs.platinum_threshold,
        inputs.diamond_threshold,
        REWARDS_QUERY_VERSION,
    )

    if load_audit and customer_number:
        with st.spinner(f"Loading order-line audit for {customer_number.upper()}..."):
            audit_df, audit_queried_at = _cached_load_customer_order_audit_data(
                customer_number,
                inputs.gold_threshold,
                inputs.platinum_threshold,
                inputs.diamond_threshold,
                REWARDS_QUERY_VERSION,
            )
        st.session_state["reward_order_audit_df"] = audit_df
        st.session_state["reward_order_audit_queried_at"] = audit_queried_at
        st.session_state["reward_order_audit_key"] = current_audit_key

    audit_df = st.session_state.get("reward_order_audit_df")
    audit_queried_at = st.session_state.get("reward_order_audit_queried_at")
    loaded_audit_key = st.session_state.get("reward_order_audit_key")

    if audit_df is None or audit_queried_at is None:
        st.info("Load the customer audit to review order-line reward math. Default customer is WHO103.")
        return

    if loaded_audit_key != current_audit_key:
        st.warning("Audit customer or thresholds changed. Load the customer audit again for the current inputs.")
        return

    projected_audit_df = calculate_customer_order_audit_projection(audit_df, inputs)
    if projected_audit_df.empty:
        st.info(f"No billed order lines found for {customer_number.upper()} in the previous 365-day window.")
        return

    order_summary_df = summarize_order_audit(projected_audit_df)
    audit_total_cols = st.columns(5)
    audit_total_cols[0].metric("Audit Orders", f"{len(order_summary_df):,}")
    audit_total_cols[1].metric("Audit Lines", f"{len(projected_audit_df):,}")
    audit_total_cols[2].metric("Item Discounts", _fmt_money(
        float(projected_audit_df["Square Foot Discount Cost"].sum())
        + float(projected_audit_df["Division 3 Discount Cost"].sum())
    ))
    audit_total_cols[3].metric(
        "Order Delivery Waivers",
        _fmt_money(float(order_summary_df["Order Delivery Fee Waiver Cost"].sum())),
    )
    audit_total_cols[4].metric("Cash Back", _fmt_money(float(projected_audit_df["Cash Back Cost"].sum())))

    st.caption(f"Customer audit last queried from Gartman: {audit_queried_at:%Y-%m-%d %I:%M:%S %p}.")

    audit_label = customer_number.upper()
    with st.expander(f"{audit_label} Order Delivery Fee Audit", expanded=True):
        st.caption(
            "Delivery fee waiver rule: ship-via contains TRUCK and Header Special Charge 2 is positive. "
            "Other header charge buckets are shown for audit visibility but are not waived by the current rule."
        )
        order_columns = [
            "Invoice Date",
            "Branch Location",
            "Order Number",
            "Invoice Number",
            "Ship Via",
            "Tier",
            "Line Count",
            "Status Quo Sales",
            "Projected Gross Sales",
            "Header Special Charge 1",
            "Header Special Charge 2",
            "Header Special Charge 3",
            "Header Special Charge 4",
            "Header Special Charge 5",
            "Header Additional Amount",
            "Order Delivery Fee Revenue",
            "Delivery Fee Waiver Applied",
            "Order Delivery Fee Waiver Cost",
            "Line Delivery Fee Waiver Allocation",
            "Square Foot Discount Cost",
            "Division 3 Discount Cost",
            "Cash Back Cost",
            "Program Revenue",
            "Incremental Benefit",
        ]
        order_display_df = order_summary_df[
            [column for column in order_columns if column in order_summary_df.columns]
        ]
        st.dataframe(
            order_display_df,
            width="stretch",
            hide_index=True,
            column_config=_column_config(order_display_df),
        )
        _download_dataframe(
            order_display_df,
            "Download order audit CSV",
            f"rewards_order_audit_{_safe_filename_part(customer_number)}.csv",
            "download_order_audit",
        )
        order_export_path = _write_audit_export(
            order_display_df,
            f"rewards_order_audit_{_safe_filename_part(customer_number)}.csv",
        )
        if order_export_path is not None:
            st.caption(f"Local CSV export: {order_export_path}")

    with st.expander(f"{audit_label} Order Line Audit", expanded=True):
        line_columns = [
            "Invoice Date",
            "Branch Location",
            "Order Number",
            "Invoice Number",
            "Line Sequence",
            "Item Number",
            "Description",
            "Division",
            "Sales UOM",
            "Tier",
            "Item Discount Type",
            "Units Sold",
            "Projected Units",
            "Status Quo Sales",
            "Projected Gross Sales",
            "Per SF Discount",
            "Division 3 Discount %",
            "Square Foot Discount Cost",
            "Division 3 Discount Cost",
            "Program Revenue After Discount",
            "Cash Back Cost",
            "Ship Via",
            "Delivery Fee Waiver Applied",
            "Order Delivery Fee Revenue",
            "Order Delivery Fee Waiver Cost",
            "Line Delivery Fee Waiver Allocation",
            "Program Revenue",
            "Incremental Benefit",
        ]
        line_display_df = projected_audit_df[
            [column for column in line_columns if column in projected_audit_df.columns]
        ]
        st.dataframe(
            line_display_df,
            width="stretch",
            hide_index=True,
            column_config=_column_config(line_display_df),
        )
        _download_dataframe(
            line_display_df,
            "Download order-line audit CSV",
            f"rewards_order_line_audit_{_safe_filename_part(customer_number)}.csv",
            "download_order_line_audit",
        )
        line_export_path = _write_audit_export(
            line_display_df,
            f"rewards_order_line_audit_{_safe_filename_part(customer_number)}.csv",
        )
        if line_export_path is not None:
            st.caption(f"Local CSV export: {line_export_path}")


def _render_results(raw_df: pd.DataFrame, inputs: RewardProgramInputs, queried_at: datetime) -> None:
    projected_df = calculate_rewards_projection(raw_df, inputs)
    summary_df = summarize_projection(projected_df)
    breakeven_lift_pct = calculate_breakeven_quantity_lift_pct(raw_df, inputs)

    if summary_df.empty:
        st.info("No customers met the current Gold threshold in the previous 365-day window.")
        return

    total_row = summary_df[summary_df["Tier"] == "Total"].iloc[0]
    incremental = float(total_row["Incremental Benefit"])
    delta_label = "profit" if incremental >= 0 else "loss"
    breakeven_label = "N/A" if breakeven_lift_pct is None else _fmt_rate(breakeven_lift_pct)
    total_rewards_cost = (
        float(total_row["Square Foot Discount Cost"])
        + float(total_row["Division 3 Discount Cost"])
        + float(total_row["Cash Back Cost"])
        + float(total_row["Delivery Fee Waiver Cost"])
    )

    st.markdown('<div class="section-label">Financial Projection</div>', unsafe_allow_html=True)
    metric_cols = st.columns(6)
    metric_cols[0].metric("Status Quo Sales", _fmt_money(float(total_row["Status Quo Sales"])))
    metric_cols[1].metric("Projected Gross Sales", _fmt_money(float(total_row["Projected Gross Sales"])))
    metric_cols[2].metric("Total Rewards Cost", _fmt_money(total_rewards_cost))
    metric_cols[3].metric("Delivery Waiver Cost", _fmt_money(float(total_row["Delivery Fee Waiver Cost"])))
    metric_cols[4].metric(
        "Net Program Revenue",
        _fmt_money(float(total_row["Program Revenue"])),
        delta=f"{_fmt_money(abs(incremental))} {delta_label}",
    )
    metric_cols[5].metric("Breakeven Unit Lift", breakeven_label)

    chart_col, table_col = st.columns([1.05, 1])
    with chart_col:
        _render_waterfall(total_row)
    with table_col:
        line_items = pd.DataFrame(
            [
                ("Status Quo Sales", total_row["Status Quo Sales"]),
                ("Projected Gross Sales", total_row["Projected Gross Sales"]),
                ("Division 1 SF Discount Cost", -total_row["Square Foot Discount Cost"]),
                ("Division 3 Discount Cost", -total_row["Division 3 Discount Cost"]),
                ("Program Revenue After Discount", total_row["Program Revenue After Discount"]),
                ("Diamond Cash Back Cost", -total_row["Cash Back Cost"]),
                ("Delivery Fee Waiver Cost", -total_row["Delivery Fee Waiver Cost"]),
                ("Net Program Revenue", total_row["Program Revenue"]),
                ("Benefit / Cost vs Status Quo", total_row["Incremental Benefit"]),
            ],
            columns=["Line Item", "Amount"],
        )
        st.dataframe(
            line_items,
            width="stretch",
            hide_index=True,
            column_config={"Amount": st.column_config.NumberColumn("Amount", format="$%,.2f")},
        )
        _download_dataframe(
            line_items,
            "Download financial line items CSV",
            "rewards_financial_line_items.csv",
            "download_financial_line_items",
        )

    if "Window Start" in raw_df.columns and not raw_df.empty:
        start = pd.to_datetime(raw_df["Window Start"].iloc[0]).date()
        end = pd.to_datetime(raw_df["Window End"].iloc[0]).date()
        st.caption(
            f"Control group window: {start:%B %d, %Y} through {end:%B %d, %Y}. "
            f"Last queried from Gartman: {queried_at:%Y-%m-%d %I:%M:%S %p}."
        )

    st.markdown('<div class="section-label">Tier Summary</div>', unsafe_allow_html=True)
    st.dataframe(
        summary_df,
        width="stretch",
        hide_index=True,
        column_config=_column_config(summary_df),
    )
    _download_dataframe(
        summary_df,
        "Download tier summary CSV",
        "rewards_tier_summary.csv",
        "download_tier_summary",
    )

    customer_df = summarize_customers(projected_df)
    with st.expander("Customer Detail", expanded=False):
        st.dataframe(
            customer_df,
            width="stretch",
            hide_index=True,
            column_config=_column_config(customer_df),
        )
        _download_dataframe(
            customer_df,
            "Download customer detail CSV",
            "rewards_customer_detail.csv",
            "download_customer_detail",
        )

    with st.expander("Customer / Item Detail", expanded=False):
        visible_columns = [
            "Tier",
            "Customer Number",
            "Customer Name",
            "Branch Location",
            "Item Number",
            "Description",
            "Division",
            "Sales UOM",
            "Units Sold",
            "Projected Units",
            "Status Quo Sales",
            "Projected Gross Sales",
            "Per SF Discount",
            "Division 3 Discount %",
            "Square Foot Discount Cost",
            "Division 3 Discount Cost",
            "Cash Back Cost",
            "Delivery Fee Waiver Cost",
            "Program Revenue",
            "Incremental Benefit",
        ]
        detail_df = projected_df[[column for column in visible_columns if column in projected_df.columns]]
        st.dataframe(
            detail_df,
            width="stretch",
            hide_index=True,
            column_config=_column_config(detail_df),
        )
        _download_dataframe(
            detail_df,
            "Download customer/item detail CSV",
            "rewards_customer_item_detail.csv",
            "download_customer_item_detail",
        )

    _render_customer_order_audit(inputs)


def main() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    _ensure_threshold_state()

    st.title("Sales Rewards Program Visualizer")
    st.caption(
        "Thresholds are based on total billed customer sales. Division 1 discounts are per SF; "
        "Division 3 discounts are percentage-based for Platinum and Diamond."
    )

    st.markdown('<div class="section-label">Reward Tiers</div>', unsafe_allow_html=True)
    tier_cols = st.columns(3)

    with tier_cols[2]:
        gold_threshold = st.slider(
            "Gold minimum sales",
            min_value=THRESHOLD_MIN,
            max_value=THRESHOLD_MAX,
            step=THRESHOLD_STEP,
            key="reward_gold_threshold",
            format="$%,d",
        )
        _tier_header("Gold", gold_threshold, "gold")
        gold_discount = st.number_input(
            "Gold Division 1 discount per SF",
            min_value=0.0,
            max_value=25.0,
            value=0.25,
            step=0.05,
            format="%.2f",
            key="reward_gold_discount",
        )
        waive_delivery_fees = st.checkbox(
            "Waive applicable delivery fees",
            value=False,
            key="reward_waive_delivery_fees",
        )

    if st.session_state["reward_platinum_threshold"] < gold_threshold:
        st.session_state["reward_platinum_threshold"] = gold_threshold
    with tier_cols[1]:
        platinum_threshold = st.slider(
            "Platinum minimum sales",
            min_value=int(gold_threshold),
            max_value=THRESHOLD_MAX,
            step=THRESHOLD_STEP,
            key="reward_platinum_threshold",
            format="$%,d",
        )
        _tier_header("Platinum", platinum_threshold, "platinum")
        platinum_discount = st.number_input(
            "Platinum Division 1 discount per SF",
            min_value=0.0,
            max_value=25.0,
            value=0.50,
            step=0.05,
            format="%.2f",
            key="reward_platinum_discount",
        )
        platinum_div3_discount_pct = st.number_input(
            "Platinum Division 3 discount %",
            min_value=0.0,
            max_value=100.0,
            value=0.0,
            step=0.25,
            format="%.2f",
            key="reward_platinum_div3_discount_pct",
        )

    if st.session_state["reward_diamond_threshold"] < platinum_threshold:
        st.session_state["reward_diamond_threshold"] = platinum_threshold
    if st.session_state.get("reward_diamond_div3_discount_pct", 0.0) < platinum_div3_discount_pct:
        st.session_state["reward_diamond_div3_discount_pct"] = platinum_div3_discount_pct
    with tier_cols[0]:
        diamond_threshold = st.slider(
            "Diamond minimum sales",
            min_value=int(platinum_threshold),
            max_value=THRESHOLD_MAX,
            step=THRESHOLD_STEP,
            key="reward_diamond_threshold",
            format="$%,d",
        )
        _tier_header("Diamond", diamond_threshold, "diamond")
        diamond_discount = st.number_input(
            "Diamond Division 1 discount per SF",
            min_value=0.0,
            max_value=25.0,
            value=0.75,
            step=0.05,
            format="%.2f",
            key="reward_diamond_discount",
        )
        diamond_div3_discount_pct = st.number_input(
            "Diamond Division 3 discount %",
            min_value=float(platinum_div3_discount_pct),
            max_value=100.0,
            value=max(float(platinum_div3_discount_pct), 0.0),
            step=0.25,
            format="%.2f",
            key="reward_diamond_div3_discount_pct",
        )
        diamond_cash_back_pct = st.number_input(
            "Diamond cash back %",
            min_value=0.0,
            max_value=100.0,
            value=1.00,
            step=0.25,
            format="%.2f",
            key="reward_diamond_cash_back_pct",
        )

    gold_threshold, platinum_threshold, diamond_threshold = normalize_thresholds(
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
    )

    st.markdown('<div class="section-label">Projections and Assumptions</div>', unsafe_allow_html=True)
    assumption_cols = st.columns([1, 1, 2])
    with assumption_cols[0]:
        quantity_lift_pct = st.number_input(
            "Unit quantity increase %",
            min_value=-100.0,
            max_value=500.0,
            value=10.0,
            step=0.25,
            format="%.2f",
            key="reward_quantity_lift_pct",
        )
    with assumption_cols[1]:
        st.metric("Projected unit multiplier", f"{1 + quantity_lift_pct / 100:,.3f}x")
    with assumption_cols[2]:
        st.caption(
            f"Current scenario: Gold >= {_fmt_money(gold_threshold)}, "
            f"Platinum >= {_fmt_money(platinum_threshold)}, "
            f"Diamond >= {_fmt_money(diamond_threshold)}; "
            f"Diamond cash back {_fmt_rate(diamond_cash_back_pct)}; "
            f"delivery waiver {'on' if waive_delivery_fees else 'off'}."
        )

    inputs = _input_model(
        gold_threshold,
        platinum_threshold,
        diamond_threshold,
        gold_discount,
        platinum_discount,
        diamond_discount,
        platinum_div3_discount_pct,
        diamond_div3_discount_pct,
        diamond_cash_back_pct,
        quantity_lift_pct,
        waive_delivery_fees,
    )

    run_col, status_col = st.columns([1, 3])
    with run_col:
        run_projection = st.button("Run Projection", type="primary", width="stretch")
    with status_col:
        st.caption("Gartman is queried for the previous 365 days excluding today.")

    if run_projection:
        with st.spinner("Querying Gartman and calculating the rewards projection..."):
            raw_df, queried_at = _cached_load_rewards_sales_data(
                gold_threshold,
                platinum_threshold,
                diamond_threshold,
                REWARDS_QUERY_VERSION,
            )
        st.session_state["rewards_raw_df"] = raw_df
        st.session_state["rewards_queried_at"] = queried_at
        st.session_state["rewards_query_version"] = REWARDS_QUERY_VERSION
        st.session_state["rewards_loaded_thresholds"] = (
            gold_threshold,
            platinum_threshold,
            diamond_threshold,
        )

    raw_df = st.session_state.get("rewards_raw_df")
    queried_at = st.session_state.get("rewards_queried_at")
    loaded_thresholds = st.session_state.get("rewards_loaded_thresholds")
    loaded_query_version = st.session_state.get("rewards_query_version")

    if raw_df is None or queried_at is None:
        st.info("Run the projection to load the eligible customer control group from Gartman.")
        return

    if loaded_query_version != REWARDS_QUERY_VERSION:
        st.warning("The rewards query has been updated. Run the projection again to reload delivery-fee data.")
        return

    current_thresholds = (gold_threshold, platinum_threshold, diamond_threshold)
    if loaded_thresholds != current_thresholds:
        st.warning("Thresholds changed since the last Gartman pull. Run the projection again to retier the control group.")

    _render_results(raw_df, inputs, queried_at)


if __name__ == "__main__":
    main()
