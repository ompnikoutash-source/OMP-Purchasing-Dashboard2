# Flooring Forecasting Refactoring Plan

## Objective
Separate the flooring/unfinished forecasting logic from `OMPforecasting5.py` into a new refactored file that uses the shared `core/` modules, and create a standalone webapp UI file.

## Current State

| File | Purpose | Lines |
|------|---------|-------|
| `OMPforecasting5.py` | Flooring forecasting + embedded UI | ~2100 |
| `flooringwebapp.py` | Main webapp UI (all 4 tabs) | ~5200 |
| `sundrieswebapp_refactored.py` | Sundries forecasting (uses core/) | ~620 |
| `mouldingwebapp_refactored.py` | Moulding forecasting (uses core/) | ~620 |

## Target State

| File | Purpose |
|------|---------|
| `flooringforecast_refactored.py` | Flooring forecasting → `flooringwebappJSON` |
| `sundrieswebapp_refactored.py` | Sundries forecasting → `sundrieswebappJSON` (unchanged) |
| `mouldingwebapp_refactored.py` | Moulding forecasting → `mouldingwebappJSON` (unchanged) |
| `webapp.py` | UI only - reads all 3 JSONs, displays dashboard |

---

## CRITICAL DIFFERENCES: Flooring vs Sundries/Moulding

These must be preserved exactly in the refactored flooring file:

### 1. Service Level (CRITICAL)
| Setting | Flooring (OMPforecasting5) | Sundries/Moulding (core/config) |
|---------|---------------------------|--------------------------------|
| SERVICE_LEVEL | **0.90 (90%)** | 0.95 (95%) |
| Z_SCORE | **1.2816** | 1.6449 |

**Impact:** Safety stock is ~22% lower for flooring due to lower service level.

### 2. ABC Classification Thresholds
| Setting | Flooring | Sundries/Moulding |
|---------|----------|-------------------|
| ABC_BREAK_A | **0.70** | 0.80 |
| ABC_BREAK_B | **0.80** | 0.90 |

**Impact:** More SKUs classified as A-class in flooring (top 70% vs 80%).

### 3. Global Uplift Budget
| Setting | Flooring | Sundries/Moulding |
|---------|----------|-------------------|
| GLOBAL_UPLIFT_BUDGET_PCT | **0.05 (5%)** | 0.02 (2%) |

**Impact:** Flooring allows 2.5x more safety stock uplift for lumpy demand.

### 4. Global Inventory Cap
| Setting | Flooring | Sundries/Moulding |
|---------|----------|-------------------|
| ENABLE_GLOBAL_INV_CAP | **False** | True |
| Cap Value | 6,000,000 SF (if enabled) | 350,000 units |

**Impact:** Flooring currently has no global cap; sundries/moulding are capped.

### 5. Unit of Measure & SQL Queries
| Aspect | Flooring | Sundries/Moulding |
|--------|----------|-------------------|
| UOM Filter | `SLUM2 LIKE '%SF%'` | `SLUM2 NOT LIKE '%SF%'` |
| Quantity Field | `SLBLUS` (SF) | `SLBLUO` (units) |
| Output Units | Square Feet | Units / Linear Feet |

### 6. Flooring-Specific Output Fields
The flooring JSON includes additional pallet/container metrics not in sundries/moulding:
- `sf_per_pallet`
- `pallets_per_container`

---

## Implementation Plan

### Phase 1: Update core/config.py
Add flooring-specific constants:

```python
# SERVICE LEVEL - PRODUCT SPECIFIC
SERVICE_LEVEL_DEFAULT = 0.95  # Sundries/Moulding
SERVICE_LEVEL_FLOORING = 0.90  # Flooring (90%)

# Update ProductConfig class to include service_level
class ProductConfig:
    def __init__(
        self,
        ...
        service_level: float = SERVICE_LEVEL_DEFAULT,
        global_uplift_budget_pct: float = 0.02,
        enable_global_inv_cap: bool = True,
    ):
        ...

# Update FLOORING_CONFIG
FLOORING_CONFIG = ProductConfig(
    product_type="flooring",
    abc_break_a=0.70,
    abc_break_b=0.80,
    global_inv_cap=6_000_000,
    unit_of_measure="SF",
    service_level=0.90,
    global_uplift_budget_pct=0.05,
    enable_global_inv_cap=False,
)
```

### Phase 2: Update core/data_loader.py
Add flooring-specific query function (or parameterize existing):

```python
def fetch_sales_history(
    conn,
    sku: str,
    start_date: str,
    exclude_sf: bool = True,  # False for flooring
    use_sf_field: bool = False,  # True for flooring (uses SLBLUS)
) -> pd.DataFrame:
    """
    Fetch sales history for a SKU.

    Args:
        exclude_sf: If True, filter OUT SF items (sundries/moulding)
                   If False, filter TO SF items only (flooring)
        use_sf_field: If True, use SLBLUS (SF); else use SLBLUO (units)
    """
    if exclude_sf:
        sf_filter = "AND L.SLUM2 NOT LIKE '%SF%'"
        qty_field = "COALESCE(L.SLBLUO, 0)"
    else:
        sf_filter = "AND L.SLUM2 LIKE '%SF%'"
        qty_field = "COALESCE(L.SLBLUS, 0)"
    ...
```

### Phase 3: Update core/inventory.py
Parameterize safety stock calculation to accept service level:

```python
def calculate_safety_stock(
    weekly_std: float,
    lead_time_weeks: float,
    avg_monthly_demand: float,
    service_level: float = 0.95,  # Can pass 0.90 for flooring
) -> float:
    z_score = stats.norm.ppf(service_level)
    ...
```

### Phase 4: Create flooringforecast_refactored.py

Structure (modeled after sundrieswebapp_refactored.py):

```python
"""
OMP Flooring Forecasting - Refactored Version

Generates inventory forecasts for engineered/unfinished flooring products.
Outputs to flooringwebappJSON for the webapp UI.

Key differences from sundries/moulding:
- Service level: 90% (vs 95%)
- ABC thresholds: 70%/80% (vs 80%/90%)
- Global uplift budget: 5% (vs 2%)
- Uses SF (square feet) not units
- No global inventory cap
"""

from pathlib import Path
from core.config import FLOORING_CONFIG
from core.data_loader import (
    load_sku_list_from_excel,
    fetch_sales_history,  # with exclude_sf=False
    filter_skus_to_active_last_n_days,
    load_trailing_volume,
)
from core.forecasting import (
    build_weekly_series,
    calculate_demand_characteristics,
    select_best_forecast_method,
    generate_forecast,
)
from core.inventory import (
    calculate_safety_stock,
    calculate_reorder_point,
    calculate_reorder_quantity,
    apply_abc_classification,
    apply_global_uplift_budget,
)

# Flooring-specific constants (override core/config defaults)
FLOORING_SKU_LIST_FILE = "Forecast SKU List.xlsx"
LEAD_TIMES_FILE = "Lead Times.xlsx"
WEBAPP_JSON_PATH = Path(__file__).resolve().parent / "flooringwebappJSON"

# Use FLOORING_CONFIG for all parameters
CONFIG = FLOORING_CONFIG

def main():
    # 1. Load SKU list
    # 2. Connect to database
    # 3. Filter to active SKUs (last 365 days)
    # 4. Load lead times from Excel
    # 5. ABC classification (using CONFIG.abc_break_a/b)
    # 6. For each SKU:
    #    a. Fetch sales history (SF, not units)
    #    b. Build weekly series
    #    c. Calculate demand characteristics (ADI, CV2)
    #    d. Select best forecast method
    #    e. Calculate inventory metrics (using CONFIG.service_level)
    #    f. Generate monthly projections
    # 7. Apply global uplift budget (CONFIG.global_uplift_budget_pct)
    # 8. Output to flooringwebappJSON
```

### Phase 5: Create webapp.py (UI-only)

Extract UI code from `flooringwebapp.py` into a standalone file:

```python
"""
OMP Purchasing Dashboard - Unified Web UI

Displays forecasting results for all product types:
- Ilsy tab: Flooring (from flooringwebappJSON)
- Veronica tab: Strip flooring (from flooringwebappJSON, filtered)
- Carlos tab: Sundries (from sundrieswebappJSON)
- Dilan tab: Moulding (from mouldingwebappJSON)

This file contains UI code only - no forecasting logic.
Run forecasting scripts separately to generate JSON files.
"""

import streamlit as st
from pathlib import Path
import json

# JSON file paths
FLOORING_JSON = Path(__file__).resolve().parent / "flooringwebappJSON"
SUNDRIES_JSON = Path(__file__).resolve().parent / "sundrieswebappJSON"
MOULDING_JSON = Path(__file__).resolve().parent / "mouldingwebappJSON"

def main():
    # All existing UI code from flooringwebapp.py
    # With forecasting logic removed
    ...
```

---

## Validation Plan

### Step 1: Before Refactoring - Capture Baseline
Run `OMPforecasting5.py` and save outputs:
```powershell
python OMPforecasting5.py
copy flooringwebappJSON flooringwebappJSON.baseline
```

### Step 2: After Refactoring - Compare Outputs
Run `flooringforecast_refactored.py` and compare:
```powershell
python flooringforecast_refactored.py
python compare_json_outputs.py flooringwebappJSON.baseline flooringwebappJSON
```

### Comparison Script (compare_json_outputs.py)
```python
"""Compare two JSON outputs to verify identical calculations."""
import json
import sys

def compare_outputs(baseline_path, new_path):
    with open(baseline_path) as f:
        baseline = json.load(f)
    with open(new_path) as f:
        new = json.load(f)

    # Compare Inventory_Metrics
    baseline_items = {r['sku']: r for r in baseline.get('Inventory_Metrics', [])}
    new_items = {r['item_number']: r for r in new.get('Inventory_Metrics', [])}

    numeric_fields = [
        'safety_stock', 'reorder_point', 'reorder_quantity',
        'inventory_position', 'available_sf', 'on_po_sf', 'backorder_sf',
        'lead_time_mean_demand', 'avg_monthly_demand', 'daily_mean_demand',
    ]

    mismatches = []
    for sku in baseline_items:
        if sku not in new_items:
            mismatches.append(f"MISSING: {sku}")
            continue
        for field in numeric_fields:
            old_val = baseline_items[sku].get(field, 0)
            new_val = new_items[sku].get(field, 0)
            if abs(old_val - new_val) > 0.01:  # Allow tiny float differences
                mismatches.append(f"{sku}.{field}: {old_val} -> {new_val}")

    if mismatches:
        print("MISMATCHES FOUND:")
        for m in mismatches:
            print(f"  {m}")
        return False
    else:
        print("✓ All values match!")
        return True

if __name__ == "__main__":
    compare_outputs(sys.argv[1], sys.argv[2])
```

### Step 3: Manual Spot Checks
1. Pick 5 random SKUs
2. Compare safety_stock, reorder_point, reorder_quantity values
3. Verify monthly projection values match

---

## Risk Mitigation

### Risk 1: SQL Query Differences
**Mitigation:** Copy exact SQL from OMPforecasting5.py, don't reuse sundries queries.

### Risk 2: Service Level / Z-Score
**Mitigation:** Explicitly pass `service_level=0.90` to all safety stock calculations.

### Risk 3: ABC Thresholds
**Mitigation:** Use `FLOORING_CONFIG.abc_break_a` (0.70) not default (0.80).

### Risk 4: Global Uplift Budget
**Mitigation:** Use `FLOORING_CONFIG.global_uplift_budget_pct` (0.05) not default (0.02).

### Risk 5: Unit Conversion (SF vs Units)
**Mitigation:** Always use `SLBLUS` field, never `SLBLUO` for flooring.

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `core/config.py` | MODIFY | Add service_level, global_uplift_pct to ProductConfig |
| `core/data_loader.py` | MODIFY | Add exclude_sf parameter to sales history functions |
| `core/inventory.py` | MODIFY | Add service_level parameter to safety stock calc |
| `flooringforecast_refactored.py` | CREATE | New flooring forecasting script |
| `webapp.py` | CREATE | UI-only webapp (or rename flooringwebapp.py) |
| `compare_json_outputs.py` | CREATE | Validation script |

---

## Execution Checklist

- [ ] Capture baseline output from OMPforecasting5.py
- [ ] Update core/config.py with flooring-specific parameters
- [ ] Update core/data_loader.py with SF/non-SF query support
- [ ] Update core/inventory.py with service_level parameter
- [ ] Create flooringforecast_refactored.py
- [ ] Run and compare output to baseline
- [ ] Fix any mismatches
- [ ] Re-run and verify 100% match
- [ ] Create webapp.py (extract UI from flooringwebapp.py)
- [ ] Test all 4 tabs in webapp
- [ ] Document changes

---

## Estimated Effort

| Task | Estimate |
|------|----------|
| Update core/ modules | 1-2 hours |
| Create flooringforecast_refactored.py | 2-3 hours |
| Validation and fixes | 1-2 hours |
| Create webapp.py | 1 hour |
| Testing all tabs | 1 hour |
| **Total** | **6-9 hours** |
