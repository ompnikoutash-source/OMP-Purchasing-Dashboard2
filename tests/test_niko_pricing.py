from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.niko_pricing import load_niko_pricing_inputs, lookup_prices_for_sku


def test_load_niko_pricing_inputs_reads_rate_column_from_freight_sheet(tmp_path: Path):
    workbook_path = tmp_path / "Unfinished Pricing.xlsx"

    current_pricing = pd.DataFrame(
        {
            "Item Number": ["RQ112SRSNB", "RQ214FMS"],
            "Macon": [3.25, 2.05],
            "Indiana": [3.55, None],
            "Merrick From Sheet": [None, 2.15],
        }
    )
    freight = pd.DataFrame(
        {
            "Vendor": ["Macon", "Indiana", "Merrick"],
            "Rate": [1200, 2500, 3300],
        }
    )

    with pd.ExcelWriter(workbook_path, engine="openpyxl") as writer:
        current_pricing.to_excel(writer, sheet_name="Current Pricing", index=False)
        freight.to_excel(writer, sheet_name="Freight", index=False)

    result = load_niko_pricing_inputs(workbook_path)

    assert result["warnings"] == []
    assert result["freight_costs"] == {
        "Macon": 1200.0,
        "Indiana": 2500.0,
        "Merrick": 3300.0,
    }
    assert result["prices_by_sku"]["RQ112SRSNB"] == {
        "Macon": 3.25,
        "Indiana": 3.55,
    }
    assert result["prices_by_sku"]["RQ214FMS"] == {
        "Macon": 2.05,
        "Merrick": 2.15,
    }


def test_lookup_prices_for_sku_falls_back_to_member_prices_for_group_rows():
    prices_by_sku = {
        "RH112SOSNB-24": {"Dayspring": 3.95},
        "RH112SOSNB/RH112SOSNB-15/RH112SOSNB-24": {"Macon": 3.75},
    }
    groups = {
        "RH112SOSNB/RH112SOSNB-15/RH112SOSNB-24": [
            "RH112SOSNB",
            "RH112SOSNB-15",
            "RH112SOSNB-24",
        ]
    }
    sku_to_consolidated = {
        "RH112SOSNB": "RH112SOSNB/RH112SOSNB-15/RH112SOSNB-24",
        "RH112SOSNB-15": "RH112SOSNB/RH112SOSNB-15/RH112SOSNB-24",
        "RH112SOSNB-24": "RH112SOSNB/RH112SOSNB-15/RH112SOSNB-24",
    }

    prices = lookup_prices_for_sku(
        "RH112SOSNB/RH112SOSNB-15/RH112SOSNB-24",
        prices_by_sku,
        sku_to_consolidated=sku_to_consolidated,
        consolidation_groups=groups,
    )

    assert prices == {
        "Macon": 3.75,
        "Dayspring": 3.95,
    }
