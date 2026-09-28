import pandas as pd
import pytest

from core.rewards_program import (
    RewardProgramInputs,
    calculate_breakeven_quantity_lift_pct,
    calculate_customer_order_audit_projection,
    calculate_rewards_projection,
    normalize_thresholds,
    summarize_order_audit,
    summarize_projection,
)


def test_normalize_thresholds_keeps_tiers_monotonic():
    assert normalize_thresholds(50000, 40000, 45000) == (50000, 50000, 50000)


def test_rewards_projection_discounts_division_1_before_diamond_cash_back():
    sales = pd.DataFrame(
        [
            {
                "Tier": "Diamond",
                "Customer Number": "C001",
                "Customer Name": "Diamond Customer",
                "Customer Class": "100",
                "Outside Sales Rep": "2",
                "Customer 365-Day Sales": 100000,
                "Branch Location": "1",
                "Item Number": "ITEM1",
                "Description": "Division 1 Item",
                "Division": "1",
                "Sales UOM": "SF",
                "Units Sold": 10,
                "Status Quo Sales": 100,
                "Orders Count": 1,
            },
            {
                "Tier": "Platinum",
                "Customer Number": "C002",
                "Customer Name": "Platinum Customer",
                "Customer Class": "100",
                "Outside Sales Rep": "2",
                "Customer 365-Day Sales": 60000,
                "Branch Location": "1",
                "Item Number": "ITEM2",
                "Description": "Division 2 Item",
                "Division": "2",
                "Sales UOM": "EA",
                "Units Sold": 20,
                "Status Quo Sales": 200,
                "Orders Count": 1,
            },
            {
                "Tier": "Gold",
                "Customer Number": "C003",
                "Customer Name": "Gold Customer",
                "Customer Class": "100",
                "Outside Sales Rep": "2",
                "Customer 365-Day Sales": 45000,
                "Branch Location": "1",
                "Item Number": "ITEM3",
                "Description": "Gold Division 1 Item",
                "Division": "1",
                "Sales UOM": "SF",
                "Units Sold": 5,
                "Status Quo Sales": 50,
                "Orders Count": 1,
            },
        ]
    )
    inputs = RewardProgramInputs(
        gold_threshold=40000,
        platinum_threshold=50000,
        diamond_threshold=75000,
        gold_discount_per_sf=0.50,
        platinum_discount_per_sf=2.00,
        diamond_discount_per_sf=1.00,
        platinum_div3_discount_pct=0.0,
        diamond_div3_discount_pct=0.0,
        diamond_cash_back_pct=10.0,
        quantity_lift_pct=10.0,
        waive_delivery_fees=False,
    )

    projected = calculate_rewards_projection(sales, inputs)
    diamond = projected[projected["Tier"] == "Diamond"].iloc[0]
    platinum = projected[projected["Tier"] == "Platinum"].iloc[0]
    gold = projected[projected["Tier"] == "Gold"].iloc[0]

    assert diamond["Projected Gross Sales"] == pytest.approx(110.00)
    assert diamond["Square Foot Discount Cost"] == pytest.approx(11.00)
    assert diamond["Program Revenue After Discount"] == pytest.approx(99.00)
    assert diamond["Cash Back Cost"] == pytest.approx(9.90)
    assert diamond["Program Revenue"] == pytest.approx(89.10)

    assert platinum["Square Foot Discount Cost"] == pytest.approx(0.00)
    assert platinum["Program Revenue"] == pytest.approx(220.00)

    assert gold["Square Foot Discount Cost"] == pytest.approx(2.75)
    assert gold["Program Revenue"] == pytest.approx(52.25)

    summary = summarize_projection(projected)
    total = summary[summary["Tier"] == "Total"].iloc[0]
    assert total["Status Quo Sales"] == pytest.approx(350.00)
    assert total["Program Revenue"] == pytest.approx(361.35)
    assert total["Incremental Benefit"] == pytest.approx(11.35)

    breakeven = calculate_breakeven_quantity_lift_pct(sales, inputs)
    assert breakeven == pytest.approx(6.544901, rel=1e-6)


def test_division_3_discount_and_delivery_waiver_apply_by_tier():
    sales = pd.DataFrame(
        [
            {
                "Tier": "Diamond",
                "Customer Number": "C001",
                "Customer Name": "Diamond Customer",
                "Customer 365-Day Sales": 100000,
                "Applicable Delivery Orders": 1,
                "Customer Delivery Fee Revenue": 20,
                "Division": "3",
                "Units Sold": 10,
                "Status Quo Sales": 100,
            },
            {
                "Tier": "Platinum",
                "Customer Number": "C002",
                "Customer Name": "Platinum Customer",
                "Customer 365-Day Sales": 60000,
                "Applicable Delivery Orders": 2,
                "Customer Delivery Fee Revenue": 30,
                "Division": "3",
                "Units Sold": 20,
                "Status Quo Sales": 200,
            },
            {
                "Tier": "Gold",
                "Customer Number": "C003",
                "Customer Name": "Gold Customer",
                "Customer 365-Day Sales": 45000,
                "Applicable Delivery Orders": 3,
                "Customer Delivery Fee Revenue": 40,
                "Division": "3",
                "Units Sold": 5,
                "Status Quo Sales": 50,
            },
        ]
    )
    inputs = RewardProgramInputs(
        gold_threshold=40000,
        platinum_threshold=50000,
        diamond_threshold=75000,
        gold_discount_per_sf=0.0,
        platinum_discount_per_sf=0.0,
        diamond_discount_per_sf=0.0,
        platinum_div3_discount_pct=5.0,
        diamond_div3_discount_pct=4.0,
        diamond_cash_back_pct=0.0,
        quantity_lift_pct=0.0,
        waive_delivery_fees=True,
    )

    projected = calculate_rewards_projection(sales, inputs)
    diamond = projected[projected["Tier"] == "Diamond"].iloc[0]
    platinum = projected[projected["Tier"] == "Platinum"].iloc[0]
    gold = projected[projected["Tier"] == "Gold"].iloc[0]

    assert diamond["Division 3 Discount %"] == pytest.approx(5.0)
    assert diamond["Division 3 Discount Cost"] == pytest.approx(5.0)
    assert diamond["Delivery Fee Waiver Cost"] == pytest.approx(20.0)
    assert diamond["Program Revenue"] == pytest.approx(75.0)

    assert platinum["Division 3 Discount Cost"] == pytest.approx(10.0)
    assert platinum["Delivery Fee Waiver Cost"] == pytest.approx(30.0)
    assert platinum["Program Revenue"] == pytest.approx(160.0)

    assert gold["Division 3 Discount Cost"] == pytest.approx(0.0)
    assert gold["Delivery Fee Waiver Cost"] == pytest.approx(40.0)
    assert gold["Program Revenue"] == pytest.approx(10.0)

    summary = summarize_projection(projected)
    total = summary[summary["Tier"] == "Total"].iloc[0]
    assert total["Applicable Delivery Orders"] == pytest.approx(6.0)
    assert total["Customer Delivery Fee Revenue"] == pytest.approx(90.0)
    assert total["Delivery Fee Waiver Cost"] == pytest.approx(90.0)
    assert total["Program Revenue"] == pytest.approx(245.0)


def test_order_line_audit_allocates_delivery_waiver_once_per_order():
    order_lines = pd.DataFrame(
        [
            {
                "Tier": "Diamond",
                "Customer Number": "WHO103",
                "Customer Name": "WHOLESALE CUSTOMER",
                "Order Key": "1|1|100",
                "Invoice Date": "2026-08-01",
                "Branch Location": "1",
                "Order Number": "500",
                "Invoice Number": "100",
                "Ship Via": "OUR TRUCK",
                "Order Delivery Fee Revenue": 50.0,
                "Line Sequence": 1,
                "Line Number": 1,
                "Item Number": "DIV1",
                "Description": "Division 1 Flooring",
                "Division": "1",
                "Sales UOM": "SF",
                "Units Sold": 10,
                "Status Quo Sales": 100,
            },
            {
                "Tier": "Diamond",
                "Customer Number": "WHO103",
                "Customer Name": "WHOLESALE CUSTOMER",
                "Order Key": "1|1|100",
                "Invoice Date": "2026-08-01",
                "Branch Location": "1",
                "Order Number": "500",
                "Invoice Number": "100",
                "Ship Via": "OUR TRUCK",
                "Order Delivery Fee Revenue": 50.0,
                "Line Sequence": 2,
                "Line Number": 2,
                "Item Number": "DIV3",
                "Description": "Division 3 Item",
                "Division": "3",
                "Sales UOM": "EA",
                "Units Sold": 1,
                "Status Quo Sales": 150,
            },
        ]
    )
    inputs = RewardProgramInputs(
        gold_threshold=40000,
        platinum_threshold=50000,
        diamond_threshold=75000,
        gold_discount_per_sf=0.0,
        platinum_discount_per_sf=0.0,
        diamond_discount_per_sf=1.0,
        platinum_div3_discount_pct=5.0,
        diamond_div3_discount_pct=10.0,
        diamond_cash_back_pct=0.0,
        quantity_lift_pct=10.0,
        waive_delivery_fees=True,
    )

    projected = calculate_customer_order_audit_projection(order_lines, inputs)
    div1 = projected[projected["Item Number"] == "DIV1"].iloc[0]
    div3 = projected[projected["Item Number"] == "DIV3"].iloc[0]

    assert div1["Square Foot Discount Cost"] == pytest.approx(11.0)
    assert div1["Line Delivery Fee Waiver Allocation"] == pytest.approx(22.0)
    assert div3["Division 3 Discount Cost"] == pytest.approx(16.5)
    assert div3["Line Delivery Fee Waiver Allocation"] == pytest.approx(33.0)
    assert projected["Line Delivery Fee Waiver Allocation"].sum() == pytest.approx(55.0)
    assert set(projected["Delivery Fee Waiver Applied"]) == {"Yes"}

    order_summary = summarize_order_audit(projected)
    assert len(order_summary) == 1
    order = order_summary.iloc[0]
    assert order["Order Delivery Fee Revenue"] == pytest.approx(50.0)
    assert order["Order Delivery Fee Waiver Cost"] == pytest.approx(55.0)
    assert order["Line Delivery Fee Waiver Allocation"] == pytest.approx(55.0)
    assert order["Delivery Fee Waiver Applied"] == "Yes"
