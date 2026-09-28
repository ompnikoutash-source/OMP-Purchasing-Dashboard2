"""
Shared inventory management functions for OMP Forecasting applications.

This module contains ABC classification, inventory position calculations,
and monthly projection simulation logic.
"""

from __future__ import annotations
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from .config import (
    ABC_BREAK_A_DEFAULT,
    ABC_BREAK_B_DEFAULT,
    ABC_MOI_CAPS,
    DAYS_PER_WEEK,
    DAYS_PER_MONTH,
    WEEKS_PER_MONTH,
)


def compute_abc_classification(
    df_master: pd.DataFrame,
    shipped_12m_map: Dict[str, float],
    abc_break_a: float = ABC_BREAK_A_DEFAULT,
    abc_break_b: float = ABC_BREAK_B_DEFAULT,
) -> Dict[str, str]:
    """
    Compute ABC classification based on 12-month shipped volume.

    Args:
        df_master: DataFrame with 'sku' column
        shipped_12m_map: Dict mapping SKU -> 12-month volume
        abc_break_a: Cumulative percentage threshold for A class (default 0.80)
        abc_break_b: Cumulative percentage threshold for B class (default 0.90)

    Returns:
        Dict mapping SKU -> ABC class ('A', 'B', or 'C')
    """
    print("\nComputing ABC classification...")

    if df_master.empty:
        return {}

    # Build sorted list by volume
    volumes = []
    for sku in df_master['sku']:
        vol = shipped_12m_map.get(sku, 0.0)
        volumes.append((sku, vol))
    volumes.sort(key=lambda x: x[1], reverse=True)

    total_volume = sum(v for _, v in volumes)
    if total_volume == 0:
        return {sku: 'C' for sku, _ in volumes}

    # Assign classes based on cumulative percentage
    cumulative = 0.0
    abc_map = {}
    for sku, vol in volumes:
        cumulative += vol
        pct = cumulative / total_volume
        if pct <= abc_break_a:
            abc_map[sku] = 'A'
        elif pct <= abc_break_b:
            abc_map[sku] = 'B'
        else:
            abc_map[sku] = 'C'

    a_count = sum(1 for c in abc_map.values() if c == 'A')
    b_count = sum(1 for c in abc_map.values() if c == 'B')
    c_count = sum(1 for c in abc_map.values() if c == 'C')
    print(f"  A: {a_count} SKUs, B: {b_count} SKUs, C: {c_count} SKUs")

    return abc_map


def simulate_daily_inventory(
    starting_inventory: float,
    daily_forecast: float,
    reorder_point: float,
    order_up_to_level: float,
    lead_time_days: int,
    simulation_days: int = 365,
    starting_on_hand: Optional[float] = None,
    starting_backorders: float = 0.0,
    initial_on_order: float = 0.0,
    inbound_schedule: Optional[Dict[int, float]] = None,
) -> List[Dict]:
    """
    Simulate daily inventory using continuous review (s, S) policy.

    Uses order-up-to logic:
        - When IP drops below ROP, order Q = max(0, S - IP)
        - Orders arrive after lead_time_days
        - Backorders are tracked explicitly

    Args:
        starting_inventory: Current inventory position (on-hand + on_order - backorders).
            Used only when starting_on_hand is None.
        daily_forecast: Expected daily demand (constant rate forecast)
        reorder_point: Reorder point (ROP)
        order_up_to_level: Order-up-to level (S)
        lead_time_days: Days until order arrives
        simulation_days: Number of days to simulate
        starting_on_hand: Actual physical on-hand units (preferred over starting_inventory).
            When provided, the simulation tracks on_hand and backorders accurately.
        starting_backorders: Units already owed to customers at simulation start.
        initial_on_order: Units already on open POs at simulation start.
            Used as fallback when inbound_schedule is not provided; arrives at lead_time_days.
        inbound_schedule: Mapping of {day_offset: quantity} for existing open POs, keyed
            by days from simulation day-0 until arrival.  When provided, replaces the
            single-lump initial_on_order approach so each PO arrives on its real date.
            Arrivals from this schedule are tracked separately as 'arrived_from_po' in
            each daily record, enabling the "Expected Arrivals" display column.

    Returns:
        List of daily records with inventory state.  Each record includes:
            day, on_hand, backorders, inbound, inventory_position, demand,
            order_qty, arrived_from_po.
    """
    # Prefer explicit on_hand/backorders over the combined IP approach.
    if starting_on_hand is not None:
        on_hand = max(0.0, float(starting_on_hand))
        backorders = max(0.0, float(starting_backorders))
    else:
        # Legacy path: IP used as on_hand, no pre-existing backorders.
        on_hand = float(starting_inventory)
        backorders = 0.0

    # Two separate inbound dicts so we can distinguish existing-PO arrivals
    # (visible as "Expected Arrivals" in the display) from new orders placed
    # by the simulation's reorder logic.
    po_inbound: Dict[int, float] = {}    # arrivals from database open POs
    order_inbound: Dict[int, float] = {} # arrivals from simulation-generated orders

    if inbound_schedule:
        # Per-PO schedule: load each line at its actual remaining days to arrival.
        for day_offset, qty in inbound_schedule.items():
            if qty <= 0:
                continue
            if day_offset <= 0:
                on_hand += qty          # already arrived / arrives today
            else:
                po_inbound[day_offset] = po_inbound.get(day_offset, 0) + qty
    elif initial_on_order > 0:
        # Legacy fallback: lump all on-order at lead_time_days.
        if lead_time_days > 0:
            po_inbound[lead_time_days] = initial_on_order
        else:
            on_hand += initial_on_order

    daily_records = []

    for day in range(simulation_days):
        # Receive existing-PO arrivals scheduled for today.
        arrived_from_po = 0.0
        if day in po_inbound:
            arrived_from_po = po_inbound[day]
            on_hand += arrived_from_po
            del po_inbound[day]

        # Receive simulation-generated order arrivals scheduled for today.
        arrived_from_sim_order = 0.0
        if day in order_inbound:
            arrived_from_sim_order = order_inbound[day]
            on_hand += arrived_from_sim_order
            del order_inbound[day]

        # Inventory position: on-hand + all inbound (both sources) - backorders.
        total_inbound = sum(po_inbound.values()) + sum(order_inbound.values())
        inventory_position = on_hand + total_inbound - backorders

        # Check if we need to order (continuous review).
        order_qty = 0.0
        if inventory_position < reorder_point and order_up_to_level > 0:
            order_qty = max(0, order_up_to_level - inventory_position)
            if order_qty > 0:
                if lead_time_days == 0:
                    # Zero lead time: order arrives immediately (same day).
                    on_hand += order_qty
                    inventory_position = on_hand + sum(po_inbound.values()) + sum(order_inbound.values()) - backorders
                else:
                    arrival_day = day + lead_time_days
                    order_inbound[arrival_day] = order_inbound.get(arrival_day, 0) + order_qty

        # Apply demand.
        demand = daily_forecast
        if on_hand >= demand:
            on_hand -= demand
        else:
            shortfall = demand - on_hand
            on_hand = 0.0
            backorders += shortfall

        # Fill backorders from on-hand if possible.
        if on_hand > 0 and backorders > 0:
            filled = min(on_hand, backorders)
            on_hand -= filled
            backorders -= filled

        daily_records.append({
            'day': day,
            'on_hand': on_hand,
            'backorders': backorders,
            'inbound': total_inbound,
            'inventory_position': inventory_position,
            'demand': demand,
            'order_qty': order_qty,
            'arrived_from_po': arrived_from_po,
            'arrived_from_sim_order': arrived_from_sim_order,
        })

    return daily_records


def aggregate_daily_to_monthly(
    daily_records: List[Dict],
    as_of_date: pd.Timestamp,
    months_ahead: int = 12,
    safety_stock: float = 0.0,
) -> List[Dict]:
    """
    Aggregate daily simulation results to monthly summary for display.

    Args:
        daily_records: List of daily inventory records from simulate_daily_inventory
        as_of_date: Reference date for simulation start
        months_ahead: Number of months to aggregate
        safety_stock: Safety stock level for display

    Returns:
        List of monthly projection dicts for webapp display
    """
    if not daily_records:
        return []

    df = pd.DataFrame(daily_records)
    df['date'] = pd.date_range(start=as_of_date, periods=len(df), freq='D')
    df['month'] = df['date'].dt.to_period('M').dt.to_timestamp()

    # Populate arrived_from_po / arrived_from_sim_order if not present.
    if 'arrived_from_po' not in df.columns:
        df['arrived_from_po'] = 0.0
    if 'arrived_from_sim_order' not in df.columns:
        df['arrived_from_sim_order'] = 0.0

    # Group by month
    monthly = df.groupby('month').agg({
        'on_hand': ['first', 'last'],
        'demand': 'sum',
        'order_qty': 'sum',
        'backorders': 'last',
        'arrived_from_po': 'sum',
        'arrived_from_sim_order': 'sum',
    }).reset_index()

    # Flatten column names
    monthly.columns = ['Month', 'Beginning Inventory', 'Ending Inventory',
                       'Forecast', 'Order Quantity', 'Backorders',
                       'PO Arrivals', 'Sim Order Arrivals']

    rows = []
    running_inv = None  # propagated ending inventory between months
    for i, row in monthly.iterrows():
        # Use the simulation's on_hand.first for month 0 (actual starting inventory),
        # then chain from the previous month's computed ending inventory onward.
        beg_inv = running_inv if running_inv is not None else float(row['Beginning Inventory'])

        # Expected Arrivals = real in-transit PO arrivals + arrivals from simulation-
        # recommended orders that were placed in a prior month and now arrive.
        po_arrivals = float(row['PO Arrivals'])
        sim_arrivals = float(row['Sim Order Arrivals'])
        total_arrivals = po_arrivals + sim_arrivals
        demand = float(row['Forecast'])
        backorders = float(row['Backorders'])

        # Ending inventory includes all arrivals (real POs + simulation orders).
        net_ending = max(0.0, beg_inv + total_arrivals - demand - backorders)

        running_inv = net_ending
        row_type = 'CATCHUP' if i == 0 else 'FCST'
        rows.append({
            "Row_Type": row_type,
            "Month": row['Month'].strftime("%Y-%m-%d"),
            "Safety Stock": round(safety_stock, 2),
            "Beginning Inventory": round(beg_inv, 2),
            "Forecast": round(demand, 2),
            "Expected Arrivals": round(total_arrivals, 2),
            "PO Arrivals": round(po_arrivals, 2),
            "Order Quantity": round(row['Order Quantity'], 2),
            "Ending Inventory": round(net_ending, 2),
            "Backorders": round(backorders, 2),
        })

    return rows[:months_ahead + 1]  # Include current month + months_ahead


def simulate_monthly_projection(
    inventory_position: float,
    weekly_forecast: np.ndarray,
    reorder_point: float,
    reorder_qty: float,
    sales_df: pd.DataFrame,
    as_of_date: Optional[pd.Timestamp],
    safety_stock: float,
    months_ahead: int = 12,
    abc_class: str = 'C',
    order_up_to_level: float = None,
    lead_time_weeks: float = 4.0,
    available: Optional[float] = None,
    backorder: float = 0.0,
    on_order: float = 0.0,
    inbound_schedule: Optional[Dict[int, float]] = None,
) -> List[Dict]:
    """
    Simulate inventory projection using continuous review with daily steps.

    Uses order-up-to (s, S) policy:
        - Simulates daily to properly model continuous review
        - Uses Q = max(0, S - IP) when IP < ROP
        - Tracks backorders explicitly
        - Aggregates to monthly for display

    Args:
        inventory_position: Current inventory position (on-hand + on_order - backorders).
        weekly_forecast: Array of weekly forecast values
        reorder_point: Reorder point threshold (ROP)
        reorder_qty: Deprecated - use order_up_to_level instead
        sales_df: DataFrame with transaction_date and quantity_shipped columns
        as_of_date: Reference date for simulation
        safety_stock: Safety stock level
        months_ahead: Number of months to simulate
        abc_class: ABC classification for MOI cap
        order_up_to_level: Order-up-to level (S)
        lead_time_weeks: Lead time in weeks for order arrival
        available: Physical on-hand units (preferred over inventory_position alone).
        backorder: Units currently owed to customers.
        on_order: Units already on open POs (pre-loaded into simulation).

    Returns:
        List of dicts with monthly projection data
    """
    if as_of_date is None:
        as_of_date = pd.Timestamp.now().normalize()
    else:
        as_of_date = pd.Timestamp(as_of_date).normalize()

    # Compute mean daily forecast
    if len(weekly_forecast) > 0:
        mean_weekly_fc = float(np.nanmean(weekly_forecast))
        if pd.isna(mean_weekly_fc):
            mean_weekly_fc = 0.0
    else:
        mean_weekly_fc = 0.0

    daily_forecast = mean_weekly_fc / DAYS_PER_WEEK
    lead_time_days = int(lead_time_weeks * DAYS_PER_WEEK)

    # Use order-up-to level if provided, otherwise fall back to old logic
    if order_up_to_level is not None and order_up_to_level > 0:
        # New continuous review simulation
        simulation_days = (months_ahead + 1) * 31  # Approximate days

        daily_records = simulate_daily_inventory(
            starting_inventory=inventory_position,
            daily_forecast=daily_forecast,
            reorder_point=reorder_point,
            order_up_to_level=order_up_to_level,
            lead_time_days=lead_time_days,
            simulation_days=simulation_days,
            starting_on_hand=available,
            starting_backorders=backorder,
            initial_on_order=on_order,
            inbound_schedule=inbound_schedule,
        )

        return aggregate_daily_to_monthly(
            daily_records=daily_records,
            as_of_date=as_of_date,
            months_ahead=months_ahead,
            safety_stock=safety_stock,
        )

    # Fallback to old monthly logic if order_up_to_level not provided
    # (for backwards compatibility during transition)
    month_start = as_of_date.replace(day=1)
    month_end = (month_start + pd.offsets.MonthEnd(0)).normalize()
    as_of_end = as_of_date + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    # Process sales data
    sales_df = sales_df.copy()
    sales_df['transaction_date'] = pd.to_datetime(sales_df['transaction_date'], errors='coerce')
    sales_df = sales_df[sales_df['transaction_date'].notna()]
    sales_df = sales_df[sales_df['transaction_date'] <= as_of_end]

    # Calculate month-to-date actual sales
    mtd_mask = (sales_df['transaction_date'] >= month_start) & (sales_df['transaction_date'] <= as_of_end)
    mtd_actual = float(sales_df.loc[mtd_mask, 'quantity_shipped'].sum())

    remaining_days = max(0, (month_end - as_of_date).days)
    remainder_fc = mean_weekly_fc * (remaining_days / DAYS_PER_WEEK)
    total_month_demand = mtd_actual + remainder_fc

    # Compute monthly forecasts
    start_fc_month = (month_start + pd.offsets.MonthBegin(1)).normalize()
    monthly_fc = []
    for m_offset in range(months_ahead):
        m_start = start_fc_month + pd.DateOffset(months=m_offset)
        days_in_month = (m_start + pd.offsets.MonthEnd(0)).day
        fc_val = mean_weekly_fc * (days_in_month / DAYS_PER_WEEK)
        monthly_fc.append((m_start, fc_val))

    # MOI cap from ABC class
    moi_cap = ABC_MOI_CAPS.get(abc_class, 1.0)
    monthly_mean = mean_weekly_fc * WEEKS_PER_MONTH
    max_ending_inv = moi_cap * monthly_mean if monthly_mean > 0 else 0

    rows = []
    beg_inv = inventory_position
    row_types = ['CATCHUP'] + ['FCST'] * months_ahead

    for i, (m_start, fc_val) in enumerate([(month_start, total_month_demand)] + monthly_fc):
        demand = fc_val
        order_qty = 0.0

        projected_end_inv = beg_inv - demand
        if projected_end_inv < reorder_point and reorder_qty > 0:
            order_qty = reorder_qty

        end_inv = beg_inv + order_qty - demand

        if end_inv > max_ending_inv and max_ending_inv > 0:
            excess = end_inv - max_ending_inv
            if order_qty >= excess:
                order_qty = max(0, order_qty - excess)
                end_inv = beg_inv + order_qty - demand

        end_inv = max(0, end_inv)

        rows.append({
            "Row_Type": row_types[i],
            "Month": m_start.strftime("%Y-%m-%d"),
            "Safety Stock": round(safety_stock, 2),
            "Beginning Inventory": round(beg_inv, 2),
            "Forecast": round(demand, 2),
            "Order Quantity": round(order_qty, 2),
            "Ending Inventory": round(end_inv, 2),
        })

        beg_inv = end_inv

    return rows


def apply_global_inventory_cap(
    results_df: pd.DataFrame,
    global_cap: float,
    monthly_data: List[Dict],
) -> pd.DataFrame:
    """
    Apply a global inventory cap across all SKUs.

    Args:
        results_df: DataFrame with SKU results including ending inventory
        global_cap: Maximum total ending inventory allowed
        monthly_data: List of monthly projection dicts

    Returns:
        Updated DataFrame with capped order quantities
    """
    # This is a simplified version - the full implementation would need
    # to iterate through months and adjust order quantities
    print("\n" + "=" * 70)
    print("APPLYING GLOBAL INVENTORY CAP")
    print("=" * 70)

    if results_df.empty:
        return results_df

    # Sum up ending inventories
    if 'ending_inventory' in results_df.columns:
        total_ending = results_df['ending_inventory'].sum()
        print(f"  Current max total ending inventory: {total_ending:,.0f} units")
        print(f"  Global cap: {global_cap:,.0f} units")

        if total_ending <= global_cap:
            print("  Under capacity - no adjustment needed")
        else:
            print(f"  Over capacity by {total_ending - global_cap:,.0f} units")
            # Implementation of proportional reduction would go here

    return results_df
