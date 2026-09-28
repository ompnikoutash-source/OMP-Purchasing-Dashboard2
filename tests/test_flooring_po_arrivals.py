from __future__ import annotations

import pathlib
import sys

import pandas as pd


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np

from flooringwebapp import (
    _apply_shipping_days_overrides,
    _prepare_arrivals_df,
    _projection_schedule_months,
    _resolve_po_projection_arrival_date,
)


def _make_arrivals_df(due_port, due_inv):
    """Build a minimal arrivals DataFrame with one row for display tests."""
    return pd.DataFrame([{
        "PO_NUMBER": "063831",
        "ITEM_NUMBER": "GFBHO9506",
        "DESCRIPTION": "EURO OAK 9.5 WALDEN",
        "QUANTITY_SF": 896.0,
        "VENDOR_NAME": "EVERGREEN WOOD INDUSTRY",
        "VENDOR_NUMBER": "560",
        "COLLECTION": "EURO OAK",
        "DUE_PORT": due_port,
        "DUE_INV": due_inv,
        "ENTRY_DATE": None,
        "CONTAINER": None,
    }])


class TestFlooringPoArrivalDisplay:
    """Regression tests for date display in _prepare_arrivals_df.

    The dashboard uses Due in Inventory (ETW / DUE_INV) as the source of truth
    for both the Arrivals card and the reorder projection.
    """

    def test_due_inventory_shows_formatted_date_and_schedule_status(self):
        df = _make_arrivals_df("2026-10-15", "2026-11-05")
        out = _prepare_arrivals_df(
            df,
            "All Vendors",
            "All Collections",
            schedule_months={"2026-11-01"},
        )
        assert out["Due in Inventory"].iloc[0] == "11/05/2026"
        assert out["Reorder Schedule"].iloc[0] == "In Schedule"

    def test_due_port_only_is_not_used_as_inventory_date(self):
        df = _make_arrivals_df("2026-10-01", None)
        out = _prepare_arrivals_df(df, "All Vendors", "All Collections")
        assert out["Due in Inventory"].iloc[0] == "No Date"

    def test_as400_sentinel_eta_treated_as_no_date(self):
        """The AS400 null sentinel date (0001-01-01) must not appear."""
        df = _make_arrivals_df("0001-01-01", "2026-11-16")
        out = _prepare_arrivals_df(df, "All Vendors", "All Collections")
        assert out["Due in Inventory"].iloc[0] == "11/16/2026"

    def test_no_dates_shows_no_date_both_columns(self):
        df = _make_arrivals_df(None, None)
        out = _prepare_arrivals_df(df, "All Vendors", "All Collections")
        assert out["Due in Inventory"].iloc[0] == "No Date"

    def test_arrival_outside_schedule_window_is_flagged(self):
        df = _make_arrivals_df(None, "2027-09-02")
        out = _prepare_arrivals_df(
            df,
            "All Vendors",
            "All Collections",
            schedule_months={"2026-09-01"},
        )
        assert out["Due in Inventory"].iloc[0] == "09/02/2027"
        assert out["Reorder Schedule"].iloc[0] == "Outside Window"

    def test_schedule_column_is_omitted_without_schedule_months(self):
        df = _make_arrivals_df(None, "2026-11-16")
        out = _prepare_arrivals_df(df, "All Vendors", "All Collections")
        assert "Reorder Schedule" not in out.columns

    def test_projection_schedule_months_skip_historical_rows(self):
        monthly_rows = [
            {"SKU": "UNCOAC12", "Row_Type": "HIST", "Month": "2026-05-01"},
            {"SKU": "UNCOAC12", "Row_Type": "FCST", "Month": "2026-09-01"},
            {"SKU": "UNCOAC14", "Row_Type": "FCST", "Month": "2026-10-01"},
        ]
        assert _projection_schedule_months(monthly_rows, "UNCOAC12") == {"2026-09-01"}


class TestFlooringPoArrivalResolution:
    """Regression tests for how flooring projections treat open POs."""

    def test_shipping_days_override_uses_line_date_when_header_conflicts(self):
        df = pd.DataFrame([{
            "PO_NUMBER": "62534",
            "ITEM_NUMBER": "UNCOAC12",
            "DUE_PORT": "2026-09-04",
            "DUE_INV": "0001-01-01",
            "EST_SHIP_DATE": "2027-07-01",
        }])

        out = _apply_shipping_days_overrides(df, {"UNCOAC12": 63})

        assert pd.Timestamp(out["DUE_INV"].iloc[0]) == pd.Timestamp("2026-09-04")

    def test_shipping_days_override_keeps_normal_header_derived_date(self):
        df = pd.DataFrame([{
            "PO_NUMBER": "63265",
            "ITEM_NUMBER": "UNCOAC12",
            "DUE_PORT": "2026-10-02",
            "DUE_INV": "0001-01-01",
            "EST_SHIP_DATE": "2026-07-27",
        }])

        out = _apply_shipping_days_overrides(df, {"UNCOAC12": 63})

        assert pd.Timestamp(out["DUE_INV"].iloc[0]) == pd.Timestamp("2026-09-28")

    def test_undated_po_is_excluded_from_projection(self):
        row = {
            "DUE_PORT": "1999-05-22",
            "DUE_INV": "1999-06-12",
            "ENTRY_DATE": "2025-05-12",
        }

        result = _resolve_po_projection_arrival_date(row)
        assert pd.isna(result), "Undated PO should not be scheduled into the projection"

    def test_due_port_does_not_estimate_inventory_arrival(self):
        row = {
            "DUE_PORT": "2026-04-20",
            "DUE_INV": None,
        }

        result = _resolve_po_projection_arrival_date(row)
        assert pd.isna(result)

    def test_due_inventory_takes_precedence_over_due_port(self):
        row = {
            "DUE_PORT": "2026-04-20",
            "DUE_INV": "2026-04-28",
        }

        result = _resolve_po_projection_arrival_date(row)
        assert pd.Timestamp(result) == pd.Timestamp("2026-04-28")
