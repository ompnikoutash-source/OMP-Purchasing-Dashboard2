"""
Shared configuration constants for OMP Forecasting applications.
"""

from scipy import stats

# ============================================================
# TIME CONSTANTS
# ============================================================
FUTURE_FORECAST_DAYS = 365
FUTURE_FORECAST_WEEKS = 52
DAYS_PER_WEEK = 7
DAYS_PER_MONTH = 30.4
WEEKS_PER_MONTH = DAYS_PER_MONTH / DAYS_PER_WEEK

# ============================================================
# SERVICE LEVEL & SAFETY STOCK
# ============================================================
SERVICE_LEVEL = 0.95
Z_SCORE = stats.norm.ppf(SERVICE_LEVEL)
CUTOFF_DATE = "2020-06-01"

# ============================================================
# ABC CLASSIFICATION - DEFAULT (used by sundries/moulding)
# ============================================================
ABC_BREAK_A_DEFAULT = 0.80
ABC_BREAK_B_DEFAULT = 0.90

# ABC CLASSIFICATION - FLOORING (different thresholds)
ABC_BREAK_A_FLOORING = 0.70
ABC_BREAK_B_FLOORING = 0.80

# ============================================================
# MONTHS OF INVENTORY CAPS BY ABC CLASS
# ============================================================
# Set to 99 to effectively disable caps (was A=3, B=2, C=1)
ABC_MOI_CAPS = {
    "A": 99.0,
    "B": 99.0,
    "C": 99.0
}

# ============================================================
# SAFETY STOCK TUNING (ABC-based uplift)
# ============================================================
# Optimized via Monte Carlo simulation (param_optimizer.py, 2026-02-19)
# MED_HIGH profile: ~42% inventory reduction, A-class stockout <0.3 days/yr
SS_UPLIFT_SCALAR = {"A": 3.0, "B": 1.5, "C": 0.5}
LUMPY_PCTL_NONZERO = {"A": 0.95, "B": 0.90, "C": 0.85}
LUMPY_LT_BUFFER_FRAC = 0.25
SS_CAP_MONTHS = 4  # Cap safety stock at N months of mean demand (0 = disabled)
ENABLE_GLOBAL_UPLIFT_BUDGET = True
GLOBAL_UPLIFT_BUDGET_PCT = 0.02

# ============================================================
# GLOBAL INVENTORY CAP
# ============================================================
ENABLE_GLOBAL_INV_CAP = True
GLOBAL_INV_CAP_UNITS = 350000  # For sundries/moulding (units)
GLOBAL_INV_CAP_SF = 1_000_000  # For flooring (square feet)

# ============================================================
# CONTINUOUS REVIEW PARAMETERS
# ============================================================
# Coverage horizon T (days) - how far beyond lead time to cover when ordering
# Longer horizon = less frequent orders, higher average inventory
# Shorter horizon = more frequent orders, lower average inventory
COVERAGE_HORIZON_FLOORING = 60  # Container freight - longer horizon
COVERAGE_HORIZON_DEFAULT = 60   # Sundries/moulding - increased for higher inventory levels

# Minimum lead time floor (days) - prevents undersized order-up-to levels
# when lead time is 0 or missing in the data. Increased to ensure adequate
# buffer for all items regardless of actual lead time.
MIN_LEAD_TIME_DAYS = 7

# Safety stock floor (minimum SS as days of average demand on non-zero days)
SS_FLOOR_DAYS = 10  # ~1.5 weeks of demand minimum


# ============================================================
# PRODUCT-SPECIFIC CONFIGURATION
# ============================================================
class ProductConfig:
    """Base configuration for product types."""

    def __init__(
        self,
        product_type: str,
        abc_break_a: float = ABC_BREAK_A_DEFAULT,
        abc_break_b: float = ABC_BREAK_B_DEFAULT,
        global_inv_cap: float = GLOBAL_INV_CAP_UNITS,
        unit_of_measure: str = "UNITS",
        coverage_horizon_days: int = COVERAGE_HORIZON_DEFAULT,
        service_level: float = SERVICE_LEVEL,
        global_uplift_budget_pct: float = GLOBAL_UPLIFT_BUDGET_PCT,
        enable_global_inv_cap: bool = ENABLE_GLOBAL_INV_CAP,
    ):
        self.product_type = product_type
        self.abc_break_a = abc_break_a
        self.abc_break_b = abc_break_b
        self.global_inv_cap = global_inv_cap
        self.unit_of_measure = unit_of_measure
        self.coverage_horizon_days = coverage_horizon_days
        self.service_level = service_level
        self.global_uplift_budget_pct = global_uplift_budget_pct
        self.enable_global_inv_cap = enable_global_inv_cap


# Pre-defined configurations for each product type
SUNDRIES_CONFIG = ProductConfig(
    product_type="sundries",
    abc_break_a=ABC_BREAK_A_DEFAULT,
    abc_break_b=ABC_BREAK_B_DEFAULT,
    global_inv_cap=GLOBAL_INV_CAP_UNITS,
    unit_of_measure="UNITS",
)

MOULDING_CONFIG = ProductConfig(
    product_type="moulding",
    abc_break_a=ABC_BREAK_A_DEFAULT,
    abc_break_b=ABC_BREAK_B_DEFAULT,
    global_inv_cap=GLOBAL_INV_CAP_UNITS,
    unit_of_measure="LF",
)

FLOORING_CONFIG = ProductConfig(
    product_type="flooring",
    abc_break_a=ABC_BREAK_A_FLOORING,
    abc_break_b=ABC_BREAK_B_FLOORING,
    global_inv_cap=GLOBAL_INV_CAP_SF,
    unit_of_measure="SF",
    coverage_horizon_days=COVERAGE_HORIZON_FLOORING,
    service_level=0.90,           # Flooring uses 90% vs 95% for sundries/moulding
    global_uplift_budget_pct=0.05,  # Flooring allows 5% uplift vs 2% for others
    enable_global_inv_cap=False,    # Flooring has no global inventory cap
)

STRIP_CONFIG = ProductConfig(
    product_type="strip",
    abc_break_a=ABC_BREAK_A_FLOORING,
    abc_break_b=ABC_BREAK_B_FLOORING,
    global_inv_cap=GLOBAL_INV_CAP_SF,
    unit_of_measure="SF",
    coverage_horizon_days=COVERAGE_HORIZON_FLOORING,
)
