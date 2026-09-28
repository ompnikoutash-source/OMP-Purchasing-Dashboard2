from __future__ import annotations

import pytest

from core.vendor_optimizer import (
    DEFAULT_PALLET_SF,
    MIN_FULL_PALLET_QTY_SF,
    MIN_PURCHASABLE_QTY_SF,
    TRUCKLOAD_CAPACITY_SF,
    PurchaseRequest,
    VendorQuote,
    apply_minimum_order_policy,
    build_cost_alerts,
    normalize_item_key,
    optimize_vendor_mix,
)


class TestNormalizeItemKey:
    def test_normalizes_multiline_group_keys(self):
        raw = "RH112SRSNB/\nRH112SRSNB-15/\nRH112SRSNB-24"
        assert normalize_item_key(raw) == "RH112SRSNB/RH112SRSNB-15/RH112SRSNB-24"


class TestOptimizeVendorMix:
    def test_split_assignment_minimizes_total_cost(self):
        # Each SKU fits in one truck independently (9,000 < 15,000 SF), so splitting
        # across vendors still requires two trucks.  The optimizer picks cheapest-material
        # vendor per SKU: SKU1 → Vendor B ($1.90 < $2.00), SKU2 → Vendor A ($2.00 < $2.20).
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=9000,
                vendor_quotes={
                    "Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=1000),
                    "Vendor B": VendorQuote("Vendor B", material_price=1.90, freight_per_truck=1000),
                },
            ),
            PurchaseRequest(
                sku="SKU2",
                qty_sf=9000,
                vendor_quotes={
                    "Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=1000),
                    "Vendor B": VendorQuote("Vendor B", material_price=2.20, freight_per_truck=1000),
                },
            ),
        ]

        result = optimize_vendor_mix(requests)

        assert result["status"] == "ok"
        # Optimal: SKU1 → B (cheaper), SKU2 → A (cheaper) — each on their own truck
        assert result["optimal_assignments"] == {"SKU1": "Vendor B", "SKU2": "Vendor A"}
        assert result["total_freight_cost"] == pytest.approx(2000.0)
        # material: 9000*1.90 + 9000*2.00 = 17100 + 18000 = 35100
        assert result["total_cost"] == pytest.approx(37_100.0)

    def test_high_freight_favors_consolidation_to_one_vendor(self):
        # Very high freight makes it cheaper to consolidate to Vendor A ($2.00/SF for
        # both SKUs) rather than split across two vendors and pay two truck costs.
        # Note: the rollup counts one container per SKU, so two SKUs → two truck charges
        # even when combined volume would fit in one physical truck.  The LP correctly
        # prices at fractional containers, so the optimizer still chooses consolidation
        # because the material savings on SKU2 ($2.20→$2.00) outweigh the extra truck.
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=7_000,
                vendor_quotes={
                    "Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=5000),
                    "Vendor B": VendorQuote("Vendor B", material_price=1.90, freight_per_truck=5000),
                },
            ),
            PurchaseRequest(
                sku="SKU2",
                qty_sf=7_000,
                vendor_quotes={
                    "Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=5000),
                    "Vendor B": VendorQuote("Vendor B", material_price=2.20, freight_per_truck=5000),
                },
            ),
        ]

        result = optimize_vendor_mix(requests)

        assert result["status"] == "ok"
        assert result["optimal_assignments"] == {"SKU1": "Vendor A", "SKU2": "Vendor A"}

    def test_truckloads_scale_with_vendor_volume(self):
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=12_000,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=900)},
            ),
            PurchaseRequest(
                sku="SKU2",
                qty_sf=15_000,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=900)},
            ),
        ]

        result = optimize_vendor_mix(requests)

        assert result["status"] == "ok"
        assert result["vendor_summary"]["Vendor A"]["truckloads"] == 2
        assert result["vendor_summary"]["Vendor A"]["freight_total"] == pytest.approx(1800.0)
        assert result["vendor_summary"]["Vendor A"]["freight_per_sf"] == pytest.approx(1800.0 / 27_000.0)

    def test_optional_pallet_uplift_fills_slack_without_adding_truck(self):
        # 12,000 SF in a 15,000 SF truck → 3,000 SF slack.
        # max_extra = min(3000, 12000 * 0.20 = 2400) = 2400 SF → 2 full pallets (1,800 SF).
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=12_000,
                pallet_sf=DEFAULT_PALLET_SF,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=900)},
            ),
        ]

        result = optimize_vendor_mix(requests, allow_pallet_uplift=True, max_uplift_pct=0.20)

        assert result["status"] == "ok"
        assert result["vendor_summary"]["Vendor A"]["truckloads"] == 1
        assert result["item_costs"]["SKU1"]["qty"] == pytest.approx(13_800.0)
        assert result["item_costs"]["SKU1"]["added_qty"] == pytest.approx(1_800.0)
        assert result["total_added_qty"] == pytest.approx(1_800.0)

    def test_optional_pallet_uplift_skips_item_when_no_slack_fits_a_pallet(self):
        # SKU1 has ample slack; SKU2 fills its container exactly — no room for uplift.
        requests = [
            PurchaseRequest(
                sku="SLACK",
                qty_sf=12_000,
                pallet_sf=DEFAULT_PALLET_SF,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=900)},
            ),
            PurchaseRequest(
                sku="FULL",
                qty_sf=TRUCKLOAD_CAPACITY_SF,
                pallet_sf=DEFAULT_PALLET_SF,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00, freight_per_truck=900)},
            ),
        ]

        result = optimize_vendor_mix(requests, allow_pallet_uplift=True, max_uplift_pct=0.20)

        assert result["status"] == "ok"
        assert result["item_costs"]["SLACK"]["added_qty"] == pytest.approx(1_800.0)
        assert result["item_costs"]["FULL"]["added_qty"] == pytest.approx(0.0)

    def test_pallet_rounding_rounds_up_to_full_pallet_using_default(self):
        # 1,100 SF / 900 SF per pallet = 1.22 → ceil to 2 pallets = 1,800 SF.
        # 1,800 SF is already an exact multiple → stays 1,800 SF.
        requests = [
            PurchaseRequest(
                sku="ODD",
                qty_sf=1_100,
                pallet_sf=DEFAULT_PALLET_SF,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
            PurchaseRequest(
                sku="EVEN",
                qty_sf=1_800,
                pallet_sf=DEFAULT_PALLET_SF,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
        ]

        result = optimize_vendor_mix(requests, round_to_pallet_multiples=True)

        assert result["status"] == "ok"
        assert result["item_costs"]["ODD"]["qty"] == pytest.approx(1_800.0)
        assert result["item_costs"]["EVEN"]["qty"] == pytest.approx(1_800.0)

    def test_pallet_rounding_uses_vendor_specific_pallet_sf(self):
        # Vendor A pallet = 1,050 SF.  1,100 / 1,050 = 1.048 → ceil to 2 → 2,100 SF.
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=1_100,
                pallet_sf=DEFAULT_PALLET_SF,
                pallet_sf_by_vendor={"Vendor A": 1_050},
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
        ]

        result = optimize_vendor_mix(requests, round_to_pallet_multiples=True)

        assert result["status"] == "ok"
        assert result["item_costs"]["SKU1"]["qty"] == pytest.approx(2_100.0)

    def test_pallet_rounding_falls_back_to_default_when_vendor_specific_missing(self):
        # No vendor-specific pallet SF → falls back to DEFAULT_PALLET_SF (900).
        # 1,100 / 900 = 1.22 → ceil to 2 → 1,800 SF.
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=1_100,
                pallet_sf_by_vendor={},  # empty — no per-vendor data
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
        ]

        result = optimize_vendor_mix(requests, round_to_pallet_multiples=True)

        assert result["status"] == "ok"
        assert result["item_costs"]["SKU1"]["qty"] == pytest.approx(1_800.0)

    def test_pallet_rounding_off_by_default(self):
        # Without round_to_pallet_multiples, quantities are NOT rounded.
        requests = [
            PurchaseRequest(
                sku="SKU1",
                qty_sf=1_100,
                pallet_sf=DEFAULT_PALLET_SF,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
        ]

        result = optimize_vendor_mix(requests)

        assert result["status"] == "ok"
        assert result["item_costs"]["SKU1"]["qty"] == pytest.approx(1_100.0)


class TestMinimumOrderPolicy:
    def test_omits_lines_below_minimum_and_rounds_small_pallet_lines(self):
        requests = [
            PurchaseRequest(
                sku="DROP",
                qty_sf=125.0,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
            PurchaseRequest(
                sku="ROUND",
                qty_sf=650.0,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
            PurchaseRequest(
                sku="KEEP",
                qty_sf=1050.0,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            ),
        ]

        normalized, omitted = apply_minimum_order_policy(requests)

        assert [req.sku for req in normalized] == ["ROUND", "KEEP"]
        assert omitted == [
            {
                "sku": "DROP",
                "description": "",
                "original_qty": 125.0,
                "reason": f"Below {MIN_PURCHASABLE_QTY_SF:,.0f} SF minimum",
            }
        ]
        assert normalized[0].qty_sf == pytest.approx(MIN_FULL_PALLET_QTY_SF)
        assert normalized[0].original_qty_sf == pytest.approx(650.0)
        assert normalized[1].qty_sf == pytest.approx(1050.0)

    def test_rollups_preserve_original_qty_for_rounding_note(self):
        requests = [
            PurchaseRequest(
                sku="ROUND",
                qty_sf=650.0,
                vendor_quotes={"Vendor A": VendorQuote("Vendor A", material_price=2.00)},
            )
        ]

        normalized, omitted = apply_minimum_order_policy(requests)
        result = optimize_vendor_mix(normalized)

        assert omitted == []
        assert result["status"] == "ok"
        item = result["item_costs"]["ROUND"]
        assert item["original_qty"] == pytest.approx(650.0)
        assert item["base_qty"] == pytest.approx(MIN_FULL_PALLET_QTY_SF)
        assert item["rounded_qty"] == pytest.approx(MIN_FULL_PALLET_QTY_SF - 650.0)
        assert item["rounded_to_min_pallet"] is True


class TestBuildCostAlerts:
    def test_alerts_when_proposed_landed_cost_is_materially_higher(self):
        item_costs = {
            "SKU1": {
                "vendor": "Vendor A",
                "qty": 5000,
                "price": 2.35,
                "landed_cost": 2.50,
            }
        }
        baselines = {
            "SKU1": {
                "last_received_po_cost": 2.00,
                "avg_inventory_cost": 2.10,
                "last_received_date": "2026-04-01",
            }
        }

        alerts = build_cost_alerts(item_costs, baselines, pct_threshold=0.10, abs_threshold=0.15)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["sku"] == "SKU1"
        assert {comp["source"] for comp in alert["comparisons"]} == {"Last Received PO", "Avg Inventory Cost"}

    def test_ignores_small_cost_deltas(self):
        item_costs = {
            "SKU1": {
                "vendor": "Vendor A",
                "qty": 5000,
                "price": 2.04,
                "landed_cost": 2.08,
            }
        }
        baselines = {
            "SKU1": {
                "last_received_po_cost": 2.00,
                "avg_inventory_cost": 2.05,
            }
        }

        alerts = build_cost_alerts(item_costs, baselines, pct_threshold=0.10, abs_threshold=0.15)

        assert alerts == []
