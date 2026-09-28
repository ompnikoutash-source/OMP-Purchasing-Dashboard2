"""
OMP Purchasing Dashboard - Streamlit Web UI

Unified purchasing dashboard for all product types:
  - Ilsy tab:     Engineered flooring    (flooringwebappJSON)
  - Veronica tab: Strip/unfinished floor (flooringwebappJSON, filtered by StripSKUList)
  - Carlos tab:   Sundries               (sundrieswebappJSON)
  - Dilan tab:    Moulding               (mouldingwebappJSON)

Run forecasts separately to refresh the JSON data before opening the webapp:
    python flooringforecast_refactored.py   # Flooring + Veronica tabs
    python sundrieswebapp_refactored.py     # Carlos tab
    python mouldingwebapp_refactored.py     # Dilan tab

Usage:
    streamlit run webapp.py
"""

from flooringwebapp import render_webapp

if __name__ == "__main__":
    render_webapp()
