"""
Build an N×N travel-time matrix using the OSRM public table endpoint.

OSRM (Open Source Routing Machine) is free and uses OpenStreetMap data.
One HTTP request fetches the full matrix for all stops in a single call.

IMPORTANT: OSRM uses longitude,latitude order (opposite of most geocoding APIs).
"""

from __future__ import annotations

import requests

OSRM_BASE = "http://router.project-osrm.org"
REQUEST_TIMEOUT = 45  # seconds — larger matrix can take a moment on the public server


def build_travel_matrix(
    coords: list[tuple[float, float]],
) -> list[list[int]]:
    """
    Build an N×N travel-time matrix (in seconds) between all coordinates.

    Args:
        coords: List of (lat, lon) tuples. Index 0 MUST be the depot.

    Returns:
        N×N list of integers. matrix[i][j] = travel time in seconds from i to j.
        Unreachable pairs are set to 999_999 (should not occur in greater LA area).

    Raises:
        RuntimeError: If the OSRM API returns an error status.
        requests.RequestException: On network failure.
    """
    if len(coords) < 2:
        raise ValueError(f"Need at least 2 coordinates (depot + 1 stop), got {len(coords)}")

    # OSRM expects lon,lat pairs separated by semicolons
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in coords)
    url = f"{OSRM_BASE}/table/v1/driving/{coord_str}"
    params = {
        "annotations":   "duration",
        "sources":       "all",
        "destinations":  "all",
    }

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"OSRM request failed: {e}\n"
            "If the public endpoint is unreliable, consider running a local OSRM "
            "Docker container: https://github.com/Project-OSRM/osrm-backend"
        ) from e

    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(
            f"OSRM error: {data.get('message', data.get('code', 'unknown'))}"
        )

    raw: list[list[float | None]] = data["durations"]
    n = len(coords)

    matrix: list[list[int]] = []
    for row in raw:
        int_row: list[int] = []
        for val in row:
            if val is None:
                int_row.append(999_999)
            else:
                int_row.append(int(round(val)))
        matrix.append(int_row)

    # Sanity check
    if len(matrix) != n or any(len(r) != n for r in matrix):
        raise RuntimeError(
            f"OSRM returned unexpected matrix shape. Expected {n}×{n}."
        )

    return matrix
