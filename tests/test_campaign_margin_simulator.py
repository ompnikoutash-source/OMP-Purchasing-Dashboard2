import pandas as pd
import pytest

from campaign_margin_simulator import (
    OfferConfig,
    apply_ui_filters,
    build_bundle_order_summary,
    calculate_offer_margin_impact,
    lowest_margin_items,
    top_selling_items,
)


def _sample_items() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ITEM_NUMBER": "GFEN1001",
                "DESCRIPTION": "Engineered Oak",
                "DIVISION": "1",
                "FAMILY_CODE": "EN",
                "FAMILY_NAME": "ENGINEERED",
                "COLLECTION": "EURO OAK",
                "VENDOR_NUMBER": "100",
                "VENDOR_NAME": "Vendor A",
                "SALES_UOM": "SF",
                "SALE_PRICE": 100.0,
                "MOST_RECENT_PO_COST": 60.0,
                "AVG_INVENTORY_COST": 50.0,
                "CURRENT_AVAILABLE": 500.0,
                "UNITS_SOLD_365D": 20.0,
                "REVENUE_365D": 2000.0,
                "ORDERS_365D": 2,
                "DIVISION_KEY": "1",
                "FAMILY_KEY": "EN",
                "COLLECTION_KEY": "EURO OAK",
                "VENDOR_KEY": "100",
                "ITEM_KEY": "GFEN1001",
            },
            {
                "ITEM_NUMBER": "GFMO2001",
                "DESCRIPTION": "Reducer",
                "DIVISION": "3",
                "FAMILY_CODE": "MO",
                "FAMILY_NAME": "MOULDING",
                "COLLECTION": "MOULDING",
                "VENDOR_NUMBER": "200",
                "VENDOR_NAME": "Vendor B",
                "SALES_UOM": "EA",
                "SALE_PRICE": 30.0,
                "MOST_RECENT_PO_COST": 10.0,
                "AVG_INVENTORY_COST": 12.0,
                "CURRENT_AVAILABLE": 50.0,
                "UNITS_SOLD_365D": 100.0,
                "REVENUE_365D": 3000.0,
                "ORDERS_365D": 15,
                "DIVISION_KEY": "3",
                "FAMILY_KEY": "MO",
                "COLLECTION_KEY": "MOULDING",
                "VENDOR_KEY": "200",
                "ITEM_KEY": "GFMO2001",
            },
        ]
    )


def test_percentage_offer_calculates_margin_impact_on_both_cost_bases():
    result = calculate_offer_margin_impact(
        _sample_items().iloc[[0]],
        OfferConfig(discount_type="Percentage Off", magnitude=20),
    ).iloc[0]

    assert result["Discounted Sale Price"] == pytest.approx(80.0)
    assert result["Price Decrease $"] == pytest.approx(20.0)
    assert result["Price Decrease %"] == pytest.approx(20.0)
    assert result["Original Margin % - Recent PO"] == pytest.approx(40.0)
    assert result["Offer Margin % - Recent PO"] == pytest.approx(25.0)
    assert result["Margin Point Decrease - Recent PO"] == pytest.approx(15.0)
    assert result["Original Margin % - Avg Inventory"] == pytest.approx(50.0)
    assert result["Offer Margin % - Avg Inventory"] == pytest.approx(37.5)


def test_bogo_uses_effective_unit_price_for_one_free_unit():
    result = calculate_offer_margin_impact(
        _sample_items().iloc[[1]],
        OfferConfig(discount_type="BOGO", magnitude=1),
    ).iloc[0]

    assert result["Discounted Sale Price"] == pytest.approx(15.0)
    assert result["Price Decrease %"] == pytest.approx(50.0)
    assert result["Offer Gross Profit - Recent PO"] == pytest.approx(5.0)
    assert result["Offer Margin % - Recent PO"] == pytest.approx(33.333333)


def test_bundle_order_summary_includes_sample_flooring_threshold_and_sale_item():
    impact = calculate_offer_margin_impact(
        _sample_items().iloc[[0]],
        OfferConfig(discount_type="Bundle", magnitude=100),
    )
    summary = build_bundle_order_summary(
        impact.iloc[0],
        OfferConfig(
            discount_type="Bundle",
            magnitude=100,
            sample_flooring_sqft=100,
            sample_flooring_sale_per_sf=5,
            sample_flooring_margin_pct=40,
        ),
    )

    recent = summary[summary["Cost Basis"] == "Most Recent PO"].iloc[0]
    assert recent["Threshold SQFT"] == pytest.approx(100)
    assert recent["Flooring Revenue"] == pytest.approx(500)
    assert recent["Flooring Cost"] == pytest.approx(300)
    assert recent["Flooring Gross Profit"] == pytest.approx(200)
    assert recent["Sale Item Offer Price"] == pytest.approx(0)
    assert recent["Sale Item Cost"] == pytest.approx(60)
    assert recent["Example Order Revenue"] == pytest.approx(500)
    assert recent["Example Order Gross Profit"] == pytest.approx(140)
    assert recent["Example Order Margin %"] == pytest.approx(28)


def test_filters_and_result_windows_select_expected_rows():
    filtered = apply_ui_filters(
        _sample_items(),
        divisions=["1"],
        families=["EN - ENGINEERED"],
        collections=[],
        vendors=[],
        items=[],
        text_search="oak",
        priced_only=True,
    )
    assert filtered["ITEM_NUMBER"].tolist() == ["GFEN1001"]

    impact = calculate_offer_margin_impact(
        _sample_items(),
        OfferConfig(discount_type="Price Drop", magnitude=20),
    )
    assert top_selling_items(impact, 1)["Item Number"].tolist() == ["GFMO2001"]
    assert lowest_margin_items(impact, 1)["Item Number"].tolist() == ["GFMO2001"]
