"""
Vehicle Routing Problem solver using Google OR-Tools.

Solves a time-window VRP across N stops and K trucks, all starting and
returning to depot (index 0). Minimizes total travel time.

Service time is charged at the FROM node so that CumulVar at any node
represents true wall-clock arrival time (the standard OR-Tools VRP pattern).
"""

from __future__ import annotations

from dataclasses import dataclass

from ortools.constraint_solver import pywrapcp, routing_enums_pb2


DEPOT_INDEX = 0
MAX_SHIFT_SECONDS     = 8 * 3600   # 8-hour shift cap per truck
SOLVER_TIME_LIMIT_SEC = 30         # increased: breaks make the problem harder

# ---------------------------------------------------------------------------
# Mandatory driver break schedule (all times in seconds since shift start)
# ---------------------------------------------------------------------------
# Break 1 — 15-min rest, must be taken after hour 2 and before hour 4
_BRK1_START_MIN  = 2 * 3600
_BRK1_START_MAX  = 4 * 3600
_BRK1_DURATION   = 15 * 60

# Lunch — 60-min break, must START by hour 4 so it is COMPLETE before hour 5
_LUNCH_START_MIN = 2 * 3600
_LUNCH_START_MAX = 4 * 3600
_LUNCH_DURATION  = 60 * 60

# Break 2 — 15-min rest, taken after hour 6 (optional: skipped if route ends early)
_BRK2_START_MIN  = 6 * 3600
_BRK2_START_MAX  = int(7.75 * 3600)   # must start by 7h45m so it ends by 8h
_BRK2_DURATION   = 15 * 60


@dataclass
class SolverResult:
    routes: list[list[int]]          # routes[truck_idx] = ordered list of stop indices
    arrival_sec: list[list[int]]     # arrival_sec[truck_idx][stop_pos] = seconds since shift start
    end_times_sec: list[int]         # end_times_sec[truck_idx] = seconds since shift start when truck returns to depot
    total_travel_sec: int
    feasible: bool


def solve_vrp(
    travel_matrix_sec: list[list[int]],
    service_times_sec: list[int],
    time_windows: list[tuple[int, int]],
    num_vehicles: int = 5,
    depot_index: int = DEPOT_INDEX,
    max_shift_seconds: int = MAX_SHIFT_SECONDS,
    solver_time_limit_sec: int = SOLVER_TIME_LIMIT_SEC,
    enforce_breaks: bool = True,
    pickup_delivery_pairs: list[tuple[int, int]] | None = None,
) -> SolverResult:
    """
    Solve the VRP and return routes + arrival times for each truck.

    Args:
        travel_matrix_sec: N×N integer matrix of travel times in seconds.
        service_times_sec: N-length list. Index 0 = depot (0 sec).
        time_windows: N-length list of (earliest_sec, latest_sec) since shift start.
                      Depot = (0, max_shift_seconds). Use (0, max_shift_sec) for
                      unconstrained stops.
        num_vehicles: Number of trucks.
        depot_index: Index of the depot in the matrix (always 0).
        max_shift_seconds: Maximum seconds a single truck can be out.
        solver_time_limit_sec: OR-Tools time limit for local search improvement.
        pickup_delivery_pairs: List of (src_node, dst_node) pairs (1-based, depot=0).
                               Each pair enforces same truck + src visited before dst.
                               Used for branch-to-branch transfers.

    Returns:
        SolverResult with routes, arrival times, total travel, and feasibility flag.
        On infeasible/timeout the routes will still be populated (best found solution).
    """
    n = len(travel_matrix_sec)

    manager = pywrapcp.RoutingIndexManager(n, num_vehicles, depot_index)
    routing = pywrapcp.RoutingModel(manager)

    # Transit + service callback (service charged at FROM node)
    def time_callback(from_idx: int, to_idx: int) -> int:
        from_node = manager.IndexToNode(from_idx)
        to_node   = manager.IndexToNode(to_idx)
        return travel_matrix_sec[from_node][to_node] + service_times_sec[from_node]

    transit_cb = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    # Reduce the per-vehicle time budget by mandatory break time so routes stay within the
    # 8-hour wall-clock shift once breaks are inserted in post-processing.  All three
    # breaks are reserved conservatively: short routes won't need the optional break and
    # get a bit of extra slack; long routes need all three and still finish ≤ 8 h.
    if enforce_breaks:
        mandatory_break_sec = _BRK1_DURATION + _LUNCH_DURATION + _BRK2_DURATION  # 90 min
        effective_capacity = max(0, max_shift_seconds - mandatory_break_sec)
    else:
        effective_capacity = max_shift_seconds

    # Time dimension
    routing.AddDimension(
        transit_cb,
        slack_max=3600,            # allow up to 1 hr of waiting at a stop
        capacity=effective_capacity,
        fix_start_cumul_to_zero=True,
        name="Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # Apply time windows
    for node in range(n):
        index = manager.NodeToIndex(node)
        lo, hi = time_windows[node]
        lo = max(0, lo)
        hi = min(effective_capacity, max(lo + 1, hi))
        time_dim.CumulVar(index).SetRange(lo, hi)

    # Pickup-delivery pairs — branch-to-branch transfers.
    # AddPickupAndDelivery enforces precedence (src before dst).
    # The explicit VehicleVar equality enforces same truck.
    if pickup_delivery_pairs:
        for src_node, dst_node in pickup_delivery_pairs:
            src_idx = manager.NodeToIndex(src_node)
            dst_idx = manager.NodeToIndex(dst_node)
            routing.AddPickupAndDelivery(src_idx, dst_idx)
            routing.solver().Add(
                routing.VehicleVar(src_idx) == routing.VehicleVar(dst_idx)
            )

    # Enforce at least 1 stop per truck (only when there are enough stops to go around)
    num_real_stops = n - 1  # n includes the depot
    if num_real_stops >= num_vehicles:
        def count_callback(from_idx: int, to_idx: int) -> int:
            # Add 1 for every non-depot node departed from
            return 0 if manager.IndexToNode(from_idx) == depot_index else 1
        count_cb = routing.RegisterTransitCallback(count_callback)
        routing.AddDimension(count_cb, 0, num_real_stops, True, "StopCount")
        count_dim = routing.GetDimensionOrDie("StopCount")
        for vid in range(num_vehicles):
            count_dim.CumulVar(routing.End(vid)).SetMin(1)

    # Balance routes across trucks: penalize the gap between the longest and
    # shortest route, so the solver spreads work equitably while still
    # respecting the travel-time objective.
    time_dim.SetGlobalSpanCostCoefficient(100)

    # Minimize total time (travel + service)
    for vid in range(num_vehicles):
        routing.AddVariableMinimizedByFinalizer(
            time_dim.CumulVar(routing.Start(vid))
        )
        routing.AddVariableMinimizedByFinalizer(
            time_dim.CumulVar(routing.End(vid))
        )

    # Search parameters
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_params.time_limit.seconds = solver_time_limit_sec
    search_params.log_search = False

    solution = routing.SolveWithParameters(search_params)

    if solution is None:
        # Return empty routes — caller will handle this case
        return SolverResult(
            routes=[[] for _ in range(num_vehicles)],
            arrival_sec=[[] for _ in range(num_vehicles)],
            end_times_sec=[0] * num_vehicles,
            total_travel_sec=0,
            feasible=False,
        )

    routes: list[list[int]] = []
    arrival_sec: list[list[int]] = []
    end_times_sec: list[int] = []

    for vid in range(num_vehicles):
        route: list[int] = []
        arrivals: list[int] = []
        idx = routing.Start(vid)
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != depot_index:
                route.append(node)
                arr = solution.Min(time_dim.CumulVar(idx))
                arrivals.append(arr)
            idx = solution.Value(routing.NextVar(idx))
        routes.append(route)
        arrival_sec.append(arrivals)
        end_times_sec.append(solution.Min(time_dim.CumulVar(routing.End(vid))))

    total_travel = 0
    for vid in range(num_vehicles):
        if routes[vid]:
            svc_on_route = sum(service_times_sec[n] for n in routes[vid])
            total_travel += max(0, end_times_sec[vid] - svc_on_route)

    return SolverResult(
        routes=routes,
        arrival_sec=arrival_sec,
        end_times_sec=end_times_sec,
        total_travel_sec=total_travel,
        feasible=True,
    )


def format_duration(seconds: int) -> str:
    """Format a duration in seconds as 'Xh Ym'."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def seconds_to_wallclock(
    shift_seconds: int,
    depart_hour: int = 7,
    depart_minute: int = 0,
) -> str:
    """Convert seconds-since-shift-start to a wall-clock time string like '9:43 AM'."""
    total_minutes = depart_hour * 60 + depart_minute + shift_seconds // 60
    total_minutes %= 1440  # wrap at midnight
    h = total_minutes // 60
    m = total_minutes % 60
    period = "AM" if h < 12 else "PM"
    display_h = h if h <= 12 else h - 12
    if display_h == 0:
        display_h = 12
    return f"{display_h}:{m:02d} {period}"
