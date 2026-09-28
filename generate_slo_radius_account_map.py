"""
Generate an interactive account map from the SLO 93401 radius export workbook.

Markers are colored by sales rep. Hovering or clicking a marker shows the
account name, address, sales rep, and trailing-365 sales.
"""

from __future__ import annotations

import argparse
import html
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import folium
import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "account_radius_lists"
GEOCODE_DB_PATH = BASE_DIR / "geocode_cache.db"
ORIGIN_LAT_LON = (35.2645356, -120.6563423)
ORIGIN_LABEL = "93401, San Luis Obispo, CA"
RADIUS_MILES = 30.0

REP_COLORS = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#17becf",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SLO account map HTML.")
    parser.add_argument(
        "workbook",
        nargs="?",
        type=Path,
        help="Path to radius export workbook. Defaults to latest account_radius_lists export.",
    )
    args = parser.parse_args()

    workbook_path = args.workbook or _latest_radius_workbook()
    if not workbook_path.exists():
        raise FileNotFoundError(workbook_path)

    accounts = pd.read_excel(workbook_path, sheet_name="Accounts")
    accounts = _attach_coordinates(accounts)
    missing = accounts[accounts["LATITUDE"].isna() | accounts["LONGITUDE"].isna()]
    if not missing.empty:
        missing_names = ", ".join(missing["Account Name"].astype(str).head(5))
        raise RuntimeError(
            f"Missing coordinates for {len(missing)} accounts. Examples: {missing_names}"
        )

    map_obj = _build_map(accounts)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = INPUT_DIR / f"slo_93401_30mi_account_map_{timestamp}.html"
    map_obj.save(str(output_path))

    print(f"Map accounts: {len(accounts)}")
    print(f"HTML map: {output_path}")


def _latest_radius_workbook() -> Path:
    matches = sorted(
        INPUT_DIR.glob("slo_93401_30mi_accounts_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError("No slo_93401_30mi_accounts_*.xlsx workbook found.")
    return matches[0]


def _attach_coordinates(accounts: pd.DataFrame) -> pd.DataFrame:
    out = accounts.copy()
    latitudes = []
    longitudes = []

    conn = sqlite3.connect(str(GEOCODE_DB_PATH))
    try:
        for _, row in out.iterrows():
            key = _address_key(row)
            coords = _lookup_geocode(conn, key)
            latitudes.append(coords[0] if coords else None)
            longitudes.append(coords[1] if coords else None)
    finally:
        conn.close()

    out["LATITUDE"] = latitudes
    out["LONGITUDE"] = longitudes
    return out


def _lookup_geocode(
    conn: sqlite3.Connection,
    address_key: str,
) -> tuple[float, float] | None:
    row = conn.execute(
        """
        SELECT lat, lon
        FROM geocode_cache
        WHERE address_key = ?
          AND failed = 0
          AND lat IS NOT NULL
          AND lon IS NOT NULL
        """,
        (address_key,),
    ).fetchone()
    if row:
        return float(row[0]), float(row[1])
    return None


def _address_key(row) -> str:
    parts = [
        row.get("Address", ""),
        row.get("City", ""),
        row.get("State", ""),
        str(row.get("Zipcode", "")).split(".")[0],
    ]
    return ", ".join(str(part).strip() for part in parts if str(part).strip()).upper()


def _build_map(accounts: pd.DataFrame) -> folium.Map:
    accounts = _with_marker_offsets(accounts)
    center_lat = float(accounts["LATITUDE"].mean())
    center_lon = float(accounts["LONGITUDE"].mean())
    map_obj = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=10,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    folium.Marker(
        location=list(ORIGIN_LAT_LON),
        tooltip=folium.Tooltip(html.escape(ORIGIN_LABEL), sticky=True),
        popup=folium.Popup(html.escape(ORIGIN_LABEL), max_width=260),
        icon=folium.Icon(color="red", icon="home", prefix="fa"),
    ).add_to(map_obj)

    folium.Circle(
        location=list(ORIGIN_LAT_LON),
        radius=RADIUS_MILES * 1609.344,
        color="#d62728",
        weight=2,
        fill=True,
        fill_color="#d62728",
        fill_opacity=0.04,
        tooltip=f"{RADIUS_MILES:.0f}-mile radius",
    ).add_to(map_obj)

    rep_values = sorted(accounts["Sales Rep Number"].astype(str).unique())
    rep_to_color = {
        rep: REP_COLORS[idx % len(REP_COLORS)]
        for idx, rep in enumerate(rep_values)
    }

    for rep in rep_values:
        group = folium.FeatureGroup(name=f"Sales Rep {rep}", show=True)
        rep_df = accounts[accounts["Sales Rep Number"].astype(str) == rep]
        color = rep_to_color[rep]

        for _, row in rep_df.iterrows():
            sales = float(row.get("Sales Revenue Past 365 Days", 0.0) or 0.0)
            tooltip_html = _tooltip_html(row, sales)
            popup_html = _popup_html(row, sales)
            size_sales = max(sales, 0.0)
            radius = max(6, min(18, 6 + (size_sales / 10000) ** 0.5 * 3))

            folium.CircleMarker(
                location=[float(row["MAP_LATITUDE"]), float(row["MAP_LONGITUDE"])],
                radius=radius,
                color="#222222",
                weight=1,
                fill=True,
                fill_color=color,
                fill_opacity=0.86,
                tooltip=folium.Tooltip(tooltip_html, sticky=True),
                popup=folium.Popup(popup_html, max_width=360),
            ).add_to(group)

        group.add_to(map_obj)

    folium.LayerControl(collapsed=False).add_to(map_obj)
    _add_legend(map_obj, rep_to_color)

    bounds = [
        [float(row["MAP_LATITUDE"]), float(row["MAP_LONGITUDE"])]
        for _, row in accounts.iterrows()
    ]
    bounds.append(list(ORIGIN_LAT_LON))
    map_obj.fit_bounds(bounds, padding=(24, 24))
    return map_obj


def _with_marker_offsets(accounts: pd.DataFrame) -> pd.DataFrame:
    out = accounts.copy()
    out["MAP_LATITUDE"] = out["LATITUDE"].astype(float)
    out["MAP_LONGITUDE"] = out["LONGITUDE"].astype(float)

    grouped = out.groupby(["LATITUDE", "LONGITUDE"], dropna=False).groups
    for (_, _), indexes in grouped.items():
        indexes = list(indexes)
        if len(indexes) <= 1:
            continue

        base_lat = float(out.loc[indexes[0], "LATITUDE"])
        base_lon = float(out.loc[indexes[0], "LONGITUDE"])
        offset_degrees = 0.00018

        for order, idx in enumerate(indexes):
            angle = 2 * math.pi * order / len(indexes)
            lat_offset = math.sin(angle) * offset_degrees
            lon_scale = max(math.cos(math.radians(base_lat)), 0.2)
            lon_offset = math.cos(angle) * offset_degrees / lon_scale
            out.loc[idx, "MAP_LATITUDE"] = base_lat + lat_offset
            out.loc[idx, "MAP_LONGITUDE"] = base_lon + lon_offset

    return out


def _tooltip_html(row, sales: float) -> str:
    account_name = html.escape(str(row.get("Account Name", "")))
    account_number = html.escape(str(row.get("Account Number", "")))
    address = html.escape(_full_display_address(row))
    rep = html.escape(str(row.get("Sales Rep Number", "")))
    return (
        "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;'>"
        f"<strong>{account_name}</strong><br>"
        f"Acct: {account_number}<br>"
        f"{address}<br>"
        f"Sales rep: {rep}<br>"
        f"365-day sales: ${sales:,.2f}"
        "</div>"
    )


def _popup_html(row, sales: float) -> str:
    return (
        "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;line-height:1.35;'>"
        f"<h4 style='margin:0 0 6px 0;'>{html.escape(str(row.get('Account Name', '')))}</h4>"
        f"<strong>Account:</strong> {html.escape(str(row.get('Account Number', '')))}<br>"
        f"<strong>Address:</strong> {html.escape(_full_display_address(row))}<br>"
        f"<strong>Sales rep:</strong> {html.escape(str(row.get('Sales Rep Number', '')))}<br>"
        f"<strong>365-day sales:</strong> ${sales:,.2f}"
        "</div>"
    )


def _full_display_address(row) -> str:
    parts = [
        row.get("Address", ""),
        row.get("City", ""),
        row.get("State", ""),
        str(row.get("Zipcode", "")).split(".")[0],
    ]
    return ", ".join(str(part).strip() for part in parts if str(part).strip())


def _add_legend(map_obj: folium.Map, rep_to_color: dict[str, str]) -> None:
    items = "".join(
        "<div style='display:flex;align-items:center;gap:7px;margin:3px 0;'>"
        f"<span style='width:11px;height:11px;border-radius:50%;background:{color};"
        "border:1px solid #222;display:inline-block;'></span>"
        f"<span>Sales Rep {html.escape(rep)}</span>"
        "</div>"
        for rep, color in rep_to_color.items()
    )
    legend = f"""
    <div style="
        position: fixed;
        bottom: 24px;
        left: 24px;
        z-index: 9999;
        background: white;
        border: 1px solid #b8b8b8;
        border-radius: 6px;
        padding: 10px 12px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.18);
        font-family: Segoe UI, Arial, sans-serif;
        font-size: 13px;
    ">
        <div style="font-weight:600;margin-bottom:5px;">Account Map</div>
        {items}
    </div>
    """
    map_obj.get_root().html.add_child(folium.Element(legend))


if __name__ == "__main__":
    main()
