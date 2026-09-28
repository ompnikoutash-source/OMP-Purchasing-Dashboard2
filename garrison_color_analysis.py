from __future__ import annotations

import argparse
import math
import shutil
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import openpyxl
from openpyxl.styles import PatternFill
from PIL import Image

from refresh_garrison_color_rollups import refresh_buckets, refresh_summary


DEFAULT_WORKBOOK = Path(r"H:\2025\MISC Reports\For Marketing\Garrison Color Analysis 2.12.2026.xlsx")

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
    "ImageWidthPx",
    "ImageHeightPx",
]

COLUMN_WIDTHS = {
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
    "ImageAnalysisSource": 70,
    "ImageWidthPx": 14,
    "ImageHeightPx": 15,
}

BUCKET_LABELS = {
    2: ("Neutral Whitewash", "Cool/Neutral"),
    3: ("Honey Blonde", "Golden/Honey"),
    4: ("Warm Blonde", "Golden/Honey"),
    5: ("Golden Oak", "Golden/Honey"),
    6: ("Amber", "Amber/Caramel"),
    7: ("Caramel", "Amber/Caramel"),
    8: ("Walnut Brown", "Brown"),
    9: ("Deep Espresso", "Brown"),
    10: ("Smoky Espresso", "Brown"),
}

REQUIRED_PRODUCT_HEADERS = [
    "INDEX",
    "Collection",
    "Product",
    "Width_in",
    "HueBucket",
    "BucketName",
    "HueFamily",
    "Swatch",
    "Undertone",
    "Notes",
    "MeanHex",
    "MedianHex",
    "MeanRGB_R",
    "MeanRGB_G",
    "MeanRGB_B",
    "L_mean",
    "a_mean",
    "b_mean",
    "HueDeg",
    "Chroma",
    "Sat_mean",
    "Value_mean",
    "Contrast_L_std",
    "EdgeDensity",
    "WarmthScore",
]


def header_map(ws) -> dict[str, int]:
    return {str(cell.value).strip(): idx for idx, cell in enumerate(ws[1], start=1) if cell.value is not None}


def copy_cell_style(src, dst) -> None:
    dst.font = copy(src.font)
    dst.fill = copy(src.fill)
    dst.border = copy(src.border)
    dst.alignment = copy(src.alignment)
    dst.number_format = src.number_format
    dst.protection = copy(src.protection)


def rgb_fill(hex_color: str) -> PatternFill:
    return PatternFill(fill_type="solid", fgColor=f"FF{hex_color.lstrip('#').upper()}")


def srgb_to_lab(rgb255: np.ndarray) -> np.ndarray:
    rgb = rgb255.astype(np.float64) / 255.0
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    matrix = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ]
    )
    xyz = linear @ matrix.T
    white = np.array([0.95047, 1.00000, 1.08883])
    xyz = xyz / white
    delta = 6 / 29
    f = np.where(xyz > delta**3, np.cbrt(xyz), xyz / (3 * delta**2) + 4 / 29)
    lab = np.empty_like(f)
    lab[:, 0] = 116 * f[:, 1] - 16
    lab[:, 1] = 500 * (f[:, 0] - f[:, 1])
    lab[:, 2] = 200 * (f[:, 1] - f[:, 2])
    return lab


def rgb_to_hsv_stats(rgb255: np.ndarray) -> tuple[float, float]:
    rgb = rgb255.astype(np.float64) / 255.0
    maxc = rgb.max(axis=1)
    minc = rgb.min(axis=1)
    delta = maxc - minc
    saturation = np.where(maxc == 0, 0, delta / maxc)
    return float(saturation.mean()), float(maxc.mean())


def edge_density(gray: np.ndarray) -> float:
    g = gray.astype(np.float64)
    dx = np.zeros_like(g)
    dy = np.zeros_like(g)
    dx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    dy[1:-1, :] = g[2:, :] - g[:-2, :]
    mag = np.hypot(dx, dy)
    return float((mag > 30).mean())


def image_metrics(path: Path) -> dict[str, float | str]:
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img)
    rgb = arr.reshape(-1, 3)
    mean_rgb = rgb.mean(axis=0)
    median_rgb = np.median(rgb, axis=0)
    lab = srgb_to_lab(rgb)
    mean_lab = lab.mean(axis=0)
    hue = (math.degrees(math.atan2(float(mean_lab[2]), float(mean_lab[1]))) + 360) % 360
    chroma = math.hypot(float(mean_lab[1]), float(mean_lab[2]))
    sat, val = rgb_to_hsv_stats(rgb)
    gray = np.dot(arr[..., :3], [0.299, 0.587, 0.114])
    contrast = float(lab[:, 0].std())
    warm_score = float((mean_rgb[0] - mean_rgb[2]) / 255.0)

    return {
        "MeanHex": "#{:02X}{:02X}{:02X}".format(*np.rint(mean_rgb).astype(int)),
        "MedianHex": "#{:02X}{:02X}{:02X}".format(*np.rint(median_rgb).astype(int)),
        "MeanRGB_R": float(mean_rgb[0]),
        "MeanRGB_G": float(mean_rgb[1]),
        "MeanRGB_B": float(mean_rgb[2]),
        "L_mean": float(mean_lab[0]),
        "a_mean": float(mean_lab[1]),
        "b_mean": float(mean_lab[2]),
        "HueDeg": hue,
        "Chroma": chroma,
        "Sat_mean": sat,
        "Value_mean": val,
        "Contrast_L_std": contrast,
        "EdgeDensity": edge_density(gray),
        "WarmthScore": warm_score,
        "ImageWidthPx": img.width,
        "ImageHeightPx": img.height,
    }


def bucket_for_metrics(metrics: dict[str, float | str]) -> tuple[int, str, str]:
    hue = float(metrics["HueDeg"])
    light = float(metrics["L_mean"])
    chroma = float(metrics["Chroma"])
    if light >= 82 and chroma < 4:
        bucket = 2
    elif light >= 80 and hue >= 75:
        bucket = 3
    elif light >= 70:
        bucket = 4
    elif light >= 60:
        bucket = 5
    elif light >= 50:
        bucket = 6
    elif light >= 40:
        bucket = 7
    elif light >= 30:
        bucket = 8
    elif chroma < 16:
        bucket = 9
    else:
        bucket = 10
    name, family = BUCKET_LABELS[bucket]
    return bucket, name, family


def ensure_product_columns(ws) -> dict[str, int]:
    headers = header_map(ws)
    for name in PRODUCT_EXTRA_HEADERS:
        if name in headers:
            continue
        col = ws.max_column + 1
        copy_cell_style(ws.cell(1, col - 1), ws.cell(1, col))
        ws.cell(1, col).value = name
        ws.column_dimensions[ws.cell(1, col).column_letter].width = COLUMN_WIDTHS.get(name, 18)
        for row in range(2, ws.max_row + 1):
            copy_cell_style(ws.cell(row, col - 1), ws.cell(row, col))
        headers = header_map(ws)

    missing = [name for name in REQUIRED_PRODUCT_HEADERS if name not in headers]
    if missing:
        raise RuntimeError("Products sheet is missing required columns: " + ", ".join(missing))
    return headers


def find_product_row(ws, headers: dict[str, int], product_number: str | None, collection: str, product: str) -> int | None:
    def norm(value: Any) -> str:
        return " ".join(str(value or "").strip().upper().split())

    if product_number and "ProductNumber" in headers:
        wanted = product_number.strip().upper()
        for row in range(2, ws.max_row + 1):
            value = str(ws.cell(row, headers["ProductNumber"]).value or "").strip().upper()
            if value == wanted:
                return row

    wanted_collection = norm(collection)
    wanted_product = norm(product)
    candidates: list[tuple[int, str]] = []
    for row in range(2, ws.max_row + 1):
        collection_value = norm(ws.cell(row, headers["Collection"]).value)
        product_value = norm(ws.cell(row, headers["Product"]).value)
        if collection_value == wanted_collection:
            candidates.append((row, product_value))

    for row, product_value in candidates:
        if product_value == wanted_product:
            return row
    for row, product_value in candidates:
        if product_value.endswith(wanted_product):
            return row
    for row, product_value in candidates:
        if wanted_product in product_value:
            return row
    return None


def next_index(ws, headers: dict[str, int]) -> int:
    values = [
        ws.cell(row, headers["INDEX"]).value
        for row in range(2, ws.max_row + 1)
        if isinstance(ws.cell(row, headers["INDEX"]).value, (int, float))
    ]
    return int(max(values, default=0)) + 1


def set_product_row(ws, row_idx: int, values: dict[str, Any]) -> None:
    headers = header_map(ws)
    template_row = max(2, row_idx - 1)
    for col_idx in range(1, ws.max_column + 1):
        copy_cell_style(ws.cell(template_row, col_idx), ws.cell(row_idx, col_idx))
    for header, value in values.items():
        if header in headers:
            ws.cell(row_idx, headers[header]).value = value
    ws.cell(row_idx, headers["Swatch"]).fill = rgb_fill(str(values["MeanHex"]))


def save_workbook(wb, workbook: Path, pending_label: str) -> Path:
    try:
        wb.save(workbook)
        return workbook
    except PermissionError:
        pending = workbook.with_name(f"{workbook.stem} pending {pending_label}{workbook.suffix}")
        wb.save(pending)
        return pending


def build_values(args: argparse.Namespace, metrics: dict[str, float | str], row_index: int) -> dict[str, Any]:
    if args.bucket:
        bucket = int(args.bucket)
        bucket_name, hue_family = BUCKET_LABELS.get(bucket, ("Custom", "Custom"))
    else:
        bucket, bucket_name, hue_family = bucket_for_metrics(metrics)

    if args.bucket_name:
        bucket_name = args.bucket_name
    if args.hue_family:
        hue_family = args.hue_family

    notes = args.notes or f"Metrics computed from source image: {args.image.name}"
    values: dict[str, Any] = {
        "INDEX": row_index,
        "Collection": args.collection,
        "Product": args.product,
        "Width_in": args.width,
        "HueBucket": bucket,
        "BucketName": bucket_name,
        "HueFamily": hue_family,
        "Undertone": args.undertone,
        "Notes": notes,
        "ProductNumber": args.product_number,
        "Species": args.species,
        "Construction": args.construction,
        "Grade": args.grade,
        "Finish": args.finish,
        "SurfaceTreatment": args.surface_treatment,
        "JankaRating": args.janka_rating,
        "Thickness": args.thickness,
        "TopLayer": args.top_layer,
        "Length": args.length,
        "InstallationMethods": args.installation_methods,
        "EdgeDetail": args.edge_detail,
        "Warranty": args.warranty,
        "ImageAnalysisSource": str(args.image),
    }
    values.update(metrics)
    return values


def add_upsert_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", type=Path, required=True, help="Source product image to analyze.")
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK, help="Garrison Color Analysis workbook.")
    parser.add_argument("--product-number", required=True, help="Gartman product/item number.")
    parser.add_argument("--collection", required=True, help="Collection name for the Products sheet.")
    parser.add_argument("--product", required=True, help="Product/color name for the Products sheet.")
    parser.add_argument("--width", type=float, default=7.5, help="Product width in inches.")
    parser.add_argument("--bucket", type=int, choices=sorted(BUCKET_LABELS), help="Override auto bucket assignment.")
    parser.add_argument("--bucket-name", help="Override bucket name.")
    parser.add_argument("--hue-family", help="Override hue family.")
    parser.add_argument("--undertone", default="Warm", help="Undertone value.")
    parser.add_argument("--notes", help="Notes value. Defaults to the image path.")
    parser.add_argument("--species", default="European Oak")
    parser.add_argument("--construction", default="Engineered (Birch Plywood)")
    parser.add_argument("--grade", default="Select Character")
    parser.add_argument("--finish", default="Matte UV Lacquer")
    parser.add_argument("--surface-treatment", default="Light Wire-Brush")
    parser.add_argument("--janka-rating", type=int, default=1360)
    parser.add_argument("--thickness", default='5/8"')
    parser.add_argument("--top-layer", default="4mm")
    parser.add_argument("--length", default='Mostly 7\'8 1/2"')
    parser.add_argument("--installation-methods", default="Glue Down, Floating")
    parser.add_argument("--edge-detail", default="Micro-Bevel")
    parser.add_argument(
        "--warranty",
        default=(
            "Residential: Limited Lifetime Structure, Limited 25 Year Finish | "
            "Commercial: Limited 25 Year Structure, Limited 10 Year Finish"
        ),
    )
    parser.add_argument("--no-rollups", action="store_true", help="Skip Buckets and Summary refresh.")
    parser.add_argument("--write", action="store_true", help="Actually save the workbook. Without this, only preview.")


def command_upsert(args: argparse.Namespace) -> int:
    if not args.image.exists():
        raise FileNotFoundError(f"Image not found: {args.image}")
    if not args.workbook.exists():
        raise FileNotFoundError(f"Workbook not found: {args.workbook}")

    metrics = image_metrics(args.image)
    auto_bucket, auto_name, auto_family = bucket_for_metrics(metrics)
    print(f"Image:      {args.image}")
    print(f"MeanHex:    {metrics['MeanHex']}")
    print(f"L/H/C:      {float(metrics['L_mean']):.2f} / {float(metrics['HueDeg']):.2f} / {float(metrics['Chroma']):.2f}")
    print(f"AutoBucket: {auto_bucket} - {auto_name} ({auto_family})")

    wb = openpyxl.load_workbook(args.workbook)
    ws = wb["Products"]
    headers = ensure_product_columns(ws)
    row_idx = find_product_row(ws, headers, args.product_number, args.collection, args.product)
    is_new = row_idx is None
    if is_new:
        row_idx = ws.max_row + 1
        row_index = next_index(ws, headers)
    else:
        row_index = int(ws.cell(row_idx, headers["INDEX"]).value or next_index(ws, headers))

    values = build_values(args, metrics, row_index)
    print(f"Target row: {row_idx} ({'new' if is_new else 'update'})")
    print(f"Bucket:     {values['HueBucket']} - {values['BucketName']}")

    if not args.write:
        print("Preview only. Add --write to save workbook changes.")
        return 0

    backup = args.workbook.with_name(
        f"{args.workbook.stem} backup before {args.product_number} {datetime.now():%Y%m%d_%H%M%S}{args.workbook.suffix}"
    )
    shutil.copy2(args.workbook, backup)
    set_product_row(ws, row_idx, values)
    if not args.no_rollups:
        refresh_buckets(wb)
        refresh_summary(wb)
    save_target = save_workbook(wb, args.workbook, args.product_number)

    print(f"Updated:    {save_target}")
    if save_target != args.workbook:
        print(f"Original locked, wrote pending copy instead: {args.workbook}")
    print(f"Backup:     {backup}")
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    if not args.image.exists():
        raise FileNotFoundError(f"Image not found: {args.image}")
    metrics = image_metrics(args.image)
    bucket, name, family = bucket_for_metrics(metrics)
    print(f"Image:      {args.image}")
    print(f"MeanHex:    {metrics['MeanHex']}")
    print(f"MedianHex:  {metrics['MedianHex']}")
    print(f"Bucket:     {bucket} - {name} ({family})")
    for key in [
        "MeanRGB_R",
        "MeanRGB_G",
        "MeanRGB_B",
        "L_mean",
        "a_mean",
        "b_mean",
        "HueDeg",
        "Chroma",
        "Sat_mean",
        "Value_mean",
        "Contrast_L_std",
        "EdgeDensity",
        "WarmthScore",
        "ImageWidthPx",
        "ImageHeightPx",
    ]:
        print(f"{key}: {metrics[key]}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze and update the Garrison Color Analysis workbook.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Analyze one image and print color metrics.")
    analyze.add_argument("--image", type=Path, required=True, help="Source product image to analyze.")
    analyze.set_defaults(func=command_analyze)

    upsert = sub.add_parser("upsert", help="Add or update one Products row from an image.")
    add_upsert_args(upsert)
    upsert.set_defaults(func=command_upsert)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
