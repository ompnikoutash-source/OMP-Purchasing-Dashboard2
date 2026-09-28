"""
OMP Flooring Forecast - Headless Entry Point

Generates inventory forecasts for engineered/unfinished flooring products
and writes the result to flooringwebappJSON for the webapp to consume.

This file is a headless CLI runner. It imports run_inventory_planning()
from flooringwebapp.py and executes it without starting the Streamlit server.
Streamlit is imported by flooringwebapp.py but never activated since no
st.* rendering calls are made during forecast execution.

Key flooring-specific parameters (from core.config.FLOORING_CONFIG):
    service_level           = 0.90  (vs 0.95 for sundries/moulding)
    abc_break_a             = 0.70  (vs 0.80)
    abc_break_b             = 0.80  (vs 0.90)
    global_uplift_budget_pct = 0.05 (vs 0.02)
    enable_global_inv_cap   = False (vs True)

Usage:
    python flooringforecast_refactored.py

Output:
    flooringwebappJSON      — consumed by the webapp dashboard
    inventory_plan_all.xlsx — Excel summary (or _list.xlsx / _single.xlsx)
"""

from __future__ import annotations
import os
import sys

# Suppress Streamlit telemetry and prevent server auto-start
os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

# Import the flooring forecast engine from the main webapp module.
# Streamlit is imported as a dependency but no rendering functions are called
# during run_inventory_planning(), so this is safe to run headlessly.
from flooringwebapp import run_inventory_planning

if __name__ == "__main__":
    run_inventory_planning()
