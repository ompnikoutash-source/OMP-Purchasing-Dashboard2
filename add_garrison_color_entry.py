from __future__ import annotations

from copy import copy
from datetime import datetime
from pathlib import Path
import shutil

import openpyxl
from openpyxl.styles import PatternFill


WORKBOOK = Path(r"H:\2025\MISC Reports\For Marketing\Garrison Color Analysis 2.12.2026.xlsx")
BACKUP = WORKBOOK.with_name(
    f"{WORKBOOK.stem} backup before GFALO7508 {datetime.now():%Y%m%d_%H%M%S}{WORKBOOK.suffix}"
)


PRODUCT_EXTRA_HEADERS = [
    "ProductNumber",
    "Species",
    "Construction",
    "Grade",
    "Finish",
    "SurfaceTreatment",
    "JankaRating",
    "Thickness",
    "TopLayer",
    "Length",
    "InstallationMethods",
    "EdgeDetail",
    "Warranty",
    "ImageAnalysisSource",
]


def header_map(ws):
    return {cell.value: idx for idx, cell in enumerate(ws[1], start=1)}


def copy_cell_style(src, dst):
    dst.font = copy(src.font)
    dst.fill = copy(src.fill)
    dst.border = copy(src.border)
    dst.alignment = copy(src.alignment)
    dst.number_format = src.number_format
    dst.protection = copy(src.protection)


def rgb_fill(hex_color: str) -> PatternFill:
    return PatternFill(fill_type="solid", fgColor=f"FF{hex_color.lstrip('#').upper()}")


def add_product_columns(ws):
    headers = header_map(ws)
    for name in PRODUCT_EXTRA_HEADERS:
        if name in headers:
            continue
        new_col = ws.max_column + 1
        src = ws.cell(1, new_col - 1)
        dst = ws.cell(1, new_col)
        copy_cell_style(src, dst)
        dst.value = name
        width = {
            "ProductNumber": 16,
            "Species": 18,
            "Construction": 26,
            "Grade": 20,
            "Finish": 18,
            "SurfaceTreatment": 22,
            "JankaRating": 14,
            "Thickness": 14,
            "TopLayer": 14,
            "Length": 20,
            "InstallationMethods": 28,
            "EdgeDetail": 18,
            "Warranty": 70,
            "ImageAnalysisSource": 46,
        }.get(name, 16)
        ws.column_dimensions[dst.column_letter].width = width
    return header_map(ws)


def set_row(ws, row_idx, values_by_header):
    headers = header_map(ws)
    template_row = row_idx - 1
    for col_idx in range(1, ws.max_column + 1):
        copy_cell_style(ws.cell(template_row, col_idx), ws.cell(row_idx, col_idx))
    for header, value in values_by_header.items():
        if header in headers:
            ws.cell(row_idx, headers[header]).value = value


def update_bucket(ws, bucket_number, new_metrics):
    headers = header_map(ws)
    row_idx = None
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, headers["HueBucket"]).value == bucket_number:
            row_idx = r
            break
    if row_idx is None:
        raise RuntimeError(f"HueBucket {bucket_number} not found")

    old_count = ws.cell(row_idx, headers["Count"]).value or 0
    new_count = old_count + 1

    weighted_fields = [
        ("Avg_L", "L_mean"),
        ("Avg_Hue", "HueDeg"),
        ("Avg_Chroma", "Chroma"),
        ("Avg_Sat", "Sat_mean"),
    ]
    for bucket_col, metric_key in weighted_fields:
        old_avg = ws.cell(row_idx, headers[bucket_col]).value or 0
        ws.cell(row_idx, headers[bucket_col]).value = (
            (old_avg * old_count) + new_metrics[metric_key]
        ) / new_count

    ws.cell(row_idx, headers["Count"]).value = new_count
    ws.cell(row_idx, headers["RepresentativeHex"]).value = "#AA8052"
    swatch = ws.cell(row_idx, headers["Swatch"])
    swatch.fill = rgb_fill("#AA8052")


def update_summary(ws, bucket_number, collection):
    headers = header_map(ws)
    if collection not in headers:
        new_col = ws.max_column + 1
        copy_cell_style(ws.cell(1, new_col - 1), ws.cell(1, new_col))
        ws.cell(1, new_col).value = collection
        ws.column_dimensions[ws.cell(1, new_col).column_letter].width = max(12, len(collection) + 2)
        for r in range(2, ws.max_row + 1):
            copy_cell_style(ws.cell(r, new_col - 1), ws.cell(r, new_col))
            ws.cell(r, new_col).value = 0
        headers = header_map(ws)

    bucket_row = None
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, headers["HueBucket"]).value == bucket_number:
            bucket_row = r
            break
    if bucket_row is None:
        raise RuntimeError(f"Summary row for HueBucket {bucket_number} not found")
    cell = ws.cell(bucket_row, headers[collection])
    cell.value = (cell.value or 0) + 1


def main():
    shutil.copy2(WORKBOOK, BACKUP)

    wb = openpyxl.load_workbook(WORKBOOK)
    ws = wb["Products"]
    add_product_columns(ws)

    # Estimated from the pasted flooring image in chat. Source file was not
    # available locally, so these can be recomputed later if a file is supplied.
    metrics = {
        "MeanHex": "#B9814A",
        "MedianHex": "#BA824A",
        "MeanRGB_R": 185.0,
        "MeanRGB_G": 129.0,
        "MeanRGB_B": 74.0,
        "L_mean": 58.9243,
        "a_mean": 15.2130,
        "b_mean": 34.2190,
        "HueDeg": 66.0,
        "Chroma": 37.7350,
        "Sat_mean": 0.6000,
        "Value_mean": 0.7255,
        "Contrast_L_std": 6.2000,
        "EdgeDensity": 0.0280,
        "WarmthScore": 0.1900,
    }

    product_number = "GFALO7508"
    existing_rows = []
    headers = header_map(ws)
    if "ProductNumber" in headers:
        for r in range(2, ws.max_row + 1):
            if str(ws.cell(r, headers["ProductNumber"]).value or "").strip().upper() == product_number:
                existing_rows.append(r)

    values = {
        "INDEX": max(
            cell.value for cell in ws["A"][1:] if isinstance(cell.value, (int, float))
        ) + 1,
        "Collection": "Allora",
        "Product": "Volto",
        "Width_in": 7.5,
        "HueBucket": 6,
        "BucketName": "Amber",
        "HueFamily": "Amber/Caramel",
        "Swatch": None,
        "Undertone": "Warm",
        "Notes": 'Gartman item description: EURO OAK 7.5" VOLTO. Metrics estimated from pasted image.',
        "ProductNumber": product_number,
        "Species": "European Oak",
        "Construction": "Engineered (Birch Plywood)",
        "Grade": "Select Character",
        "Finish": "Matte UV Lacquer",
        "SurfaceTreatment": "Light Wire-Brush",
        "JankaRating": 1360,
        "Thickness": '5/8"',
        "TopLayer": "4mm",
        "Length": "Mostly 7'8 1/2\"",
        "InstallationMethods": "Glue Down, Floating",
        "EdgeDetail": "Micro-Bevel",
        "Warranty": "Residential: Limited Lifetime Structure, Limited 25 Year Finish | Commercial: Limited 25 Year Structure, Limited 10 Year Finish",
        "ImageAnalysisSource": "Chat-pasted image; exact pixel stats pending source file",
    }
    values.update(metrics)

    if existing_rows:
        row_idx = existing_rows[0]
    else:
        row_idx = ws.max_row + 1
    set_row(ws, row_idx, values)
    ws.cell(row_idx, header_map(ws)["Swatch"]).fill = rgb_fill(metrics["MeanHex"])

    if not existing_rows:
        update_bucket(wb["Buckets"], 6, metrics)
        update_summary(wb["Summary"], 6, "Allora")

    wb.save(WORKBOOK)
    print(f"Updated: {WORKBOOK}")
    print(f"Backup:  {BACKUP}")
    print(f"Row:     {row_idx}")


if __name__ == "__main__":
    main()
