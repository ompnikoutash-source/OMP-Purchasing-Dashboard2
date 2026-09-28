from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.vendor_optimizer import normalize_item_key, normalize_vendor_name


DEFAULT_STRIP_VENDOR_COLUMNS = [
    "Indiana",
    "Dayspring",
    "Mullican",
    "Macon",
    "Anthony",
    "Orillia",
    "Lebanon",
    "Merrick",
]
DEFAULT_NIKO_VENDOR_ALIASES = {
    "MERRICK FROM SHEET": "MERRICK",
}
_FREIGHT_COLUMN_CANDIDATES = ["Freight", "FREIGHT", "Rate", "RATE"]


def get_first_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def coerce_positive_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        value = stripped.replace(",", "").replace("$", "")
    try:
        numeric = float(value)
    except Exception:
        return None
    if not np.isfinite(numeric) or numeric <= 0:
        return None
    return numeric


def canonical_niko_vendor_name(
    value: Any,
    preferred_vendors: List[str],
    vendor_aliases: Optional[Dict[str, str]] = None,
) -> str:
    vendor = normalize_vendor_name(value)
    if not vendor:
        return ""

    vendor_key = vendor.upper()
    if vendor_aliases:
        vendor_key = vendor_aliases.get(vendor_key, vendor_key)

    preferred = {name.upper(): name for name in preferred_vendors}
    preferred["APPALACHIAN"] = "Appalachian"
    preferred["MERRICK"] = "Merrick"
    return preferred.get(vendor_key, vendor)


def read_vendor_freight_from_row(row: pd.Series, vendor_column_map: Dict[str, str]) -> Dict[str, float]:
    freight_costs: Dict[str, float] = {}
    for source_col, vendor in vendor_column_map.items():
        value = coerce_positive_float(row.get(source_col))
        if value is not None:
            freight_costs[vendor] = value
    return freight_costs


def _merge_vendor_prices(
    target: Dict[str, float],
    source: Dict[str, Any],
    fill_missing_only: bool,
) -> None:
    for vendor, value in (source or {}).items():
        numeric = coerce_positive_float(value)
        if numeric is None:
            continue
        if vendor not in target:
            target[vendor] = numeric
        elif not fill_missing_only:
            target[vendor] = min(float(target[vendor]), numeric)


def lookup_prices_for_sku(
    sku: Any,
    prices_by_sku: Dict[str, Dict[str, float]],
    sku_to_consolidated: Optional[Dict[str, str]] = None,
    consolidation_groups: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, float]:
    sku_key = normalize_item_key(sku)
    if not sku_key:
        return {}

    resolved: Dict[str, float] = {}
    _merge_vendor_prices(resolved, prices_by_sku.get(sku_key, {}), fill_missing_only=True)

    consolidated_key = normalize_item_key((sku_to_consolidated or {}).get(sku_key, ""))
    if consolidated_key and consolidated_key != sku_key:
        _merge_vendor_prices(resolved, prices_by_sku.get(consolidated_key, {}), fill_missing_only=True)

    group_members = (consolidation_groups or {}).get(sku_key, [])
    if group_members:
        member_prices: Dict[str, float] = {}
        for member_sku in group_members:
            _merge_vendor_prices(
                member_prices,
                prices_by_sku.get(normalize_item_key(member_sku), {}),
                fill_missing_only=False,
            )
        _merge_vendor_prices(resolved, member_prices, fill_missing_only=True)

    return resolved


def extract_workbook_freight_costs(
    workbook_path: Path,
    current_pricing_df: pd.DataFrame,
    item_col: Optional[str],
    vendor_column_map: Dict[str, str],
    preferred_vendors: List[str],
    vendor_aliases: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, float], List[str]]:
    freight_costs: Dict[str, float] = {}
    warnings_out: List[str] = []
    workbook_name = workbook_path.name

    if item_col and item_col in current_pricing_df.columns:
        item_keys = current_pricing_df[item_col].map(normalize_item_key)
        freight_row = current_pricing_df[item_keys == "FREIGHT"]
        if not freight_row.empty:
            freight_costs.update(read_vendor_freight_from_row(freight_row.iloc[0], vendor_column_map))
            if freight_costs:
                return freight_costs, warnings_out

    try:
        workbook = pd.ExcelFile(workbook_path)
    except Exception as exc:
        return {}, [f"Could not open {workbook_name} to read Freight data: {exc}"]

    for sheet_name in workbook.sheet_names:
        try:
            df = pd.read_excel(workbook_path, sheet_name=sheet_name, header=0)
        except Exception:
            continue
        if df.empty:
            continue

        freight_col = get_first_column(df, _FREIGHT_COLUMN_CANDIDATES)
        vendor_col = get_first_column(df, ["Vendor Name", "Vendor", "VendorName", "VENDOR NAME", "VENDOR"])
        item_number_col = get_first_column(df, ["Item Number", "ITEM NUMBER", "ITEM_NUMBER"])

        if freight_col and vendor_col:
            for _, row in df.iterrows():
                vendor = canonical_niko_vendor_name(row.get(vendor_col), preferred_vendors, vendor_aliases)
                value = coerce_positive_float(row.get(freight_col))
                if vendor and value is not None:
                    freight_costs[vendor] = value
            if freight_costs:
                return freight_costs, warnings_out

        if freight_col and item_number_col:
            freight_row = df[df[item_number_col].map(normalize_item_key) == "FREIGHT"]
            if not freight_row.empty:
                freight_costs.update(read_vendor_freight_from_row(freight_row.iloc[0], vendor_column_map))
                if freight_costs:
                    return freight_costs, warnings_out

        if freight_col and not vendor_col and len(df.columns) >= 2:
            first_col = df.columns[0]
            vendor_matches = 0
            candidate_map: Dict[str, float] = {}
            for _, row in df.iterrows():
                vendor = canonical_niko_vendor_name(row.get(first_col), preferred_vendors, vendor_aliases)
                value = coerce_positive_float(row.get(freight_col))
                if vendor and value is not None:
                    vendor_matches += 1
                    candidate_map[vendor] = value
            if vendor_matches >= 2:
                freight_costs.update(candidate_map)
                return freight_costs, warnings_out

    warnings_out.append(
        f"No vendor Freight data was found in {workbook_name}. "
        "You can still type freight into the Freight row before optimizing."
    )
    return freight_costs, warnings_out


def load_niko_pricing_inputs(
    workbook_path: Path,
    default_vendor_names: Optional[List[str]] = None,
    vendor_aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    preferred_vendors = list(dict.fromkeys(default_vendor_names or DEFAULT_STRIP_VENDOR_COLUMNS))
    aliases = dict(DEFAULT_NIKO_VENDOR_ALIASES)
    if vendor_aliases:
        aliases.update({str(key).upper(): str(value).upper() for key, value in vendor_aliases.items()})
    workbook_name = workbook_path.name

    if not workbook_path.exists():
        return {
            "vendor_names": preferred_vendors,
            "prices_by_sku": {},
            "freight_costs": {},
            "warnings": [f"{workbook_name} was not found."],
            "vendor_column_map": {},
        }

    try:
        pricing_df = pd.read_excel(workbook_path, sheet_name="Current Pricing", header=0)
    except Exception as exc:
        return {
            "vendor_names": preferred_vendors,
            "prices_by_sku": {},
            "freight_costs": {},
            "warnings": [f"Could not read Current Pricing from {workbook_name}: {exc}"],
            "vendor_column_map": {},
        }

    if pricing_df.empty:
        return {
            "vendor_names": preferred_vendors,
            "prices_by_sku": {},
            "freight_costs": {},
            "warnings": [f"Current Pricing in {workbook_name} is empty."],
            "vendor_column_map": {},
        }

    item_col = get_first_column(pricing_df, ["Item Number", "ITEM NUMBER", "ITEM_NUMBER"])
    if not item_col:
        return {
            "vendor_names": preferred_vendors,
            "prices_by_sku": {},
            "freight_costs": {},
            "warnings": [f"Current Pricing in {workbook_name} is missing an Item Number column."],
            "vendor_column_map": {},
        }

    metadata_columns = {
        item_col.upper(),
        "THICKNESS",
        "WIDTH",
        "SPECIES",
        "GRADE/CUT",
        "GRADE",
        "EDGE",
        "CATEGORY",
        "DESCRIPTION",
        "LAST UPDATED",
        "FREIGHT",
        "RATE",
    }

    # Detect constraint columns FIRST so they can be excluded from vendor_names.
    # Columns ending with these suffixes carry pallet/container data, not prices.
    _SFPP_SUFFIX = " SF/PALLET"
    _PPC_SUFFIX = " PALLETS/CONTAINER"
    sf_per_pallet_cols: Dict[str, str] = {}
    pallets_per_container_cols: Dict[str, str] = {}
    _constraint_col_names: set = set()

    for col in pricing_df.columns:
        col_name = str(col).strip()
        col_upper = col_name.upper()
        if col_upper.endswith(_SFPP_SUFFIX):
            vendor_part = col_name[: len(col_name) - len(_SFPP_SUFFIX)].strip()
            v = canonical_niko_vendor_name(vendor_part, preferred_vendors, aliases)
            if v:
                sf_per_pallet_cols[col_name] = v
                _constraint_col_names.add(col_name)
        elif col_upper.endswith(_PPC_SUFFIX):
            vendor_part = col_name[: len(col_name) - len(_PPC_SUFFIX)].strip()
            v = canonical_niko_vendor_name(vendor_part, preferred_vendors, aliases)
            if v:
                pallets_per_container_cols[col_name] = v
                _constraint_col_names.add(col_name)

    vendor_column_map: Dict[str, str] = {}
    vendor_names: List[str] = []
    for col in pricing_df.columns:
        col_name = str(col).strip()
        if not col_name or col_name.lower().startswith("unnamed"):
            continue
        if col_name in _constraint_col_names:
            continue
        if col_name.upper() in metadata_columns:
            continue
        vendor = canonical_niko_vendor_name(col_name, preferred_vendors, aliases)
        if not vendor:
            continue
        if vendor not in vendor_names:
            vendor_names.append(vendor)
        vendor_column_map[col_name] = vendor

    for vendor in preferred_vendors:
        if vendor not in vendor_names:
            vendor_names.append(vendor)

    prices_by_sku: Dict[str, Dict[str, float]] = {}
    sf_per_pallet_by_sku: Dict[str, Dict[str, float]] = {}
    pallets_per_container_by_sku: Dict[str, Dict[str, float]] = {}

    for _, row in pricing_df.iterrows():
        sku_key = normalize_item_key(row.get(item_col))
        if not sku_key or sku_key == "FREIGHT":
            continue
        row_prices = prices_by_sku.setdefault(sku_key, {})
        for source_col, vendor in vendor_column_map.items():
            value = coerce_positive_float(row.get(source_col))
            if value is None:
                continue
            if vendor not in row_prices:
                row_prices[vendor] = value
        for source_col, vendor in sf_per_pallet_cols.items():
            value = coerce_positive_float(row.get(source_col))
            if value is not None:
                sf_per_pallet_by_sku.setdefault(sku_key, {})[vendor] = value
        for source_col, vendor in pallets_per_container_cols.items():
            value = coerce_positive_float(row.get(source_col))
            if value is not None:
                pallets_per_container_by_sku.setdefault(sku_key, {})[vendor] = value

    freight_costs, warnings_out = extract_workbook_freight_costs(
        workbook_path,
        pricing_df,
        item_col,
        vendor_column_map,
        preferred_vendors,
        aliases,
    )

    return {
        "vendor_names": vendor_names,
        "prices_by_sku": prices_by_sku,
        "freight_costs": freight_costs,
        "warnings": warnings_out,
        "vendor_column_map": vendor_column_map,
        "sf_per_pallet_by_sku": sf_per_pallet_by_sku,
        "pallets_per_container_by_sku": pallets_per_container_by_sku,
    }
