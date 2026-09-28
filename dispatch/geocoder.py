"""
Address geocoding with a persistent SQLite cache.

Geocoder chain (each tried in order until one succeeds):
  1. ArcGIS World Geocoding Service — free, no API key, very accurate for US addresses
  2. Nominatim (OpenStreetMap) — free, 1 req/sec rate limit (fallback)

The cache means each unique address is only looked up once across all geocoders.
For a delivery operation with recurring customers the cost quickly approaches zero.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import requests

CACHE_DB_PATH  = Path(__file__).resolve().parent.parent / "geocode_cache.db"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
ARCGIS_URL     = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
USER_AGENT     = "OMP-Dispatch-RouteOptimizer/1.0 (internal logistics tool)"
RATE_LIMIT_SEC = 1.1   # Nominatim ToS: max 1 req/sec
FAILURE_RETRY_DAYS = 7  # Re-try a previously failed address after this many days
ARCGIS_MIN_SCORE   = 80  # ArcGIS match score threshold (0-100); 80+ is a solid match


def init_geocode_db(db_path: Path = CACHE_DB_PATH) -> sqlite3.Connection:
    """Open (or create) the SQLite geocode cache. Returns an open connection."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address_key  TEXT PRIMARY KEY,
            lat          REAL,
            lon          REAL,
            display_name TEXT,
            failed       INTEGER DEFAULT 0,
            queried_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def geocode_address(
    full_address: str,
    cache_conn: sqlite3.Connection,
) -> tuple[float, float] | None:
    """
    Return (lat, lon) for the given address string, or None on failure.

    Hits the SQLite cache first; only calls geocoding APIs on a cache miss (or
    expired failure). Caches both successes and failures to avoid repeat lookups.
    """
    if not full_address.strip():
        return None  # blank address — caller will assign depot coords and flag the stop

    key = full_address.strip().upper()

    row = cache_conn.execute(
        "SELECT lat, lon, failed, queried_at FROM geocode_cache WHERE address_key = ?",
        (key,),
    ).fetchone()

    if row:
        lat, lon, failed, queried_at = row
        if not failed:
            return (lat, lon)
        # Cached failure: only retry after FAILURE_RETRY_DAYS
        try:
            age_days = _age_days(queried_at)
        except Exception:
            age_days = FAILURE_RETRY_DAYS + 1  # force retry if date is corrupt
        if age_days < FAILURE_RETRY_DAYS:
            return None

    # 1. Try ArcGIS first (more reliable for US commercial addresses)
    coords = _arcgis_geocode(full_address)
    if coords:
        lat, lon = coords
        cache_conn.execute(
            """
            INSERT OR REPLACE INTO geocode_cache
                (address_key, lat, lon, display_name, failed, queried_at)
            VALUES (?, ?, ?, ?, 0, datetime('now'))
            """,
            (key, lat, lon, "ArcGIS"),
        )
        cache_conn.commit()
        return (lat, lon)

    # 2. Fall back to Nominatim
    print(f"    ArcGIS found nothing for '{full_address}' — trying Nominatim...")
    time.sleep(RATE_LIMIT_SEC)  # Nominatim ToS: max 1 req/sec

    params = {
        "q":            full_address,
        "format":       "json",
        "limit":        1,
        "addressdetails": 0,
        "countrycodes": "us",
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        print(f"    Nominatim request failed for '{full_address}': {e}")
        _cache_failure(cache_conn, key)
        return None

    if not results:
        print(f"    Nominatim: no result either — address will be flagged.")
        _cache_failure(cache_conn, key)
        return None

    lat = float(results[0]["lat"])
    lon = float(results[0]["lon"])
    display = results[0].get("display_name", "")

    cache_conn.execute(
        """
        INSERT OR REPLACE INTO geocode_cache
            (address_key, lat, lon, display_name, failed, queried_at)
        VALUES (?, ?, ?, ?, 0, datetime('now'))
        """,
        (key, lat, lon, display),
    )
    cache_conn.commit()
    return (lat, lon)


def _arcgis_geocode(full_address: str) -> tuple[float, float] | None:
    """
    Geocode via ArcGIS World Geocoding Service (free, no API key required).
    Returns (lat, lon) if a confident match is found, else None.
    """
    params = {
        "SingleLine": full_address,
        "f":          "json",
        "outFields":  "",
        "maxLocations": 1,
        "countryCode":  "USA",
    }
    try:
        resp = requests.get(ARCGIS_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ArcGIS request failed for '{full_address}': {e}")
        return None

    candidates = data.get("candidates", [])
    if not candidates:
        return None

    best = candidates[0]
    score = best.get("score", 0)
    if score < ARCGIS_MIN_SCORE:
        print(f"    ArcGIS low-confidence match (score {score:.0f}) for '{full_address}' — skipping")
        return None

    loc = best.get("location", {})
    lon = loc.get("x")
    lat = loc.get("y")
    if lat is None or lon is None:
        return None

    print(f"    ArcGIS resolved '{full_address}' (score {score:.0f})")
    return (float(lat), float(lon))


def _cache_failure(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO geocode_cache
            (address_key, lat, lon, display_name, failed, queried_at)
        VALUES (?, NULL, NULL, NULL, 1, datetime('now'))
        """,
        (key,),
    )
    conn.commit()


def _age_days(queried_at: str) -> float:
    """Return the age of a cached entry in days."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(queried_at).replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        return (now - ts).total_seconds() / 86400
    except Exception:
        return 0.0
