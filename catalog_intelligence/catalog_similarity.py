"""
Cross-reference the Garrison Color Analysis workbook (perceptual color metrics
from product images) with the MASTER Garrison Product Information List (physical
specs: species, grade, finish, construction, dimensions...) to flag item pairs
that are "too similar" -- i.e. hard for a customer (or a rep) to tell apart --
within a given Gartman product family.

Usage:
    .venv\\Scripts\\python.exe catalog_intelligence\\catalog_similarity.py --family EN
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.db_connection import connect  # noqa: E402
from item_pairing import is_companion_pair  # noqa: E402
from active_items import active_item_conditions, retired_collection_clause, DEFAULT_PO_SINCE  # noqa: E402

COLOR_WORKBOOK = Path(r"H:\2025\MISC Reports\For Marketing\Garrison Color Analysis 2.12.2026.xlsx")
MASTER_LIST_WORKBOOK = Path(r"C:\Users\niko\Downloads\MASTER Garrison Product Information List AS OF 5.7.26.xlsx")
OUT_DIR = Path(__file__).resolve().parent / "data"

# Master list categorical spec fields to compare directly (Everything sheet header -> our field name)
MASTER_SPEC_FIELDS = {
    "SPECIES": "species",
    "GRADE": "grade",
    "FINISH": "finish",
    "TEXTURE": "texture",
    "EDGE DETAIL": "edge_detail",
    "CONSTRUCTION": "construction",
    "WEAR LAYER": "wear_layer",
    "THICKNESS": "thickness",
    "WIDTH": "width",
    "LENGTH": "lengths",
    "VENEER CUT": "cut",
    "COLOR": "catalog_color",
    "INSTALLATION METHODS": "installation_methods",
}
CATEGORICAL_SPEC_FIELDS = [
    "species", "grade", "finish", "texture", "edge_detail",
    "construction", "wear_layer", "thickness", "width", "lengths", "cut", "installation_methods",
]

XLSX_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XLSX_NS_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_NS = {
    "a": XLSX_NS_MAIN,
    "r": XLSX_NS_OFFICE_REL,
    "rel": XLSX_NS_REL,
}
ROW_TAG = f"{{{XLSX_NS_MAIN}}}row"
CELL_TAG = f"{{{XLSX_NS_MAIN}}}c"
VALUE_TAG = f"{{{XLSX_NS_MAIN}}}v"
TEXT_TAG = f"{{{XLSX_NS_MAIN}}}t"


def norm_item(value) -> str:
    return " ".join(str(value or "").strip().upper().split())


def clean_value(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return " ".join(str(value).strip().split())


def is_dont_use_description(value) -> bool:
    text = clean_value(value).upper().replace("’", "'").replace("`", "'")
    no_apostrophe = text.replace("'", "")
    return "DONT USE" in no_apostrophe or "DO NOT USE" in text


def has_dont_use_text(*values) -> bool:
    return any(is_dont_use_description(value) for value in values)


DONT_USE_TEXT_COLUMNS = [
    "Product", "BucketName", "Notes", "Description", "Item Description",
    "description", "productName", "colorBucket", "catalog_color", "MASTER_COLOR",
]


def dont_use_item_keys(df: pd.DataFrame) -> set[str]:
    if df.empty or "ITEM_KEY" not in df.columns:
        return set()
    text_cols = [c for c in DONT_USE_TEXT_COLUMNS if c in df.columns]
    if not text_cols:
        return set()
    return set(df.loc[
        df[text_cols].apply(lambda row: has_dont_use_text(*row), axis=1),
        "ITEM_KEY",
    ])


def _parse_inches(value) -> float | None:
    text = clean_value(value)
    if not text:
        return None
    normalized = (
        text.upper()
        .replace("“", '"')
        .replace("”", '"')
        .replace("″", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    for glyph, fraction in {
        "¼": " 1/4",
        "½": " 1/2",
        "¾": " 3/4",
        "⅛": " 1/8",
        "⅜": " 3/8",
        "⅝": " 5/8",
        "⅞": " 7/8",
    }.items():
        normalized = normalized.replace(glyph, fraction)
    normalized = normalized.replace('"', " ")
    normalized = re.sub(r"\b(INCHES|INCH|IN\.?)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    try:
        return float(normalized)
    except ValueError:
        pass

    mixed = re.search(r"(\d+(?:\.\d+)?)\s*[- ]\s*(\d+)\s*/\s*(\d+)", normalized)
    if mixed:
        whole, num, den = mixed.groups()
        den_float = float(den)
        return float(whole) + (float(num) / den_float if den_float else 0.0)

    fraction = re.search(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)", normalized)
    if fraction:
        num, den = fraction.groups()
        den_float = float(den)
        return float(num) / den_float if den_float else None

    number = re.search(r"\d+(?:\.\d+)?", normalized)
    return float(number.group(0)) if number else None


def normalize_width(value) -> str:
    width = _parse_inches(value)
    if width is None:
        return norm_item(value) if clean_value(value) else ""
    if abs(width - round(width)) < 1e-9:
        return f'{int(round(width))}"'
    return f'{width:.4f}'.rstrip("0").rstrip(".") + '"'


def _xlsx_col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in str(cell_ref) if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch.upper()) - 64
    return max(n, 1)


@lru_cache(maxsize=8)
def _xlsx_shared_strings(workbook: str) -> tuple[str, ...]:
    """Read shared strings without opening the whole workbook through openpyxl.

    These Garrison workbooks are large enough that openpyxl can spend minutes
    initializing objects just to get one sheet. The dashboard compiler only
    needs raw cell values, so a small XML reader keeps refreshes practical.
    """
    path = Path(workbook)
    with zipfile.ZipFile(path) as zf:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return ()
        strings: list[str] = []
        with zf.open("xl/sharedStrings.xml") as fh:
            for _, elem in ET.iterparse(fh, events=("end",)):
                if elem.tag != f"{{{XLSX_NS_MAIN}}}si":
                    continue
                strings.append("".join(t.text or "" for t in elem.iter(TEXT_TAG)))
                elem.clear()
    return tuple(strings)


@lru_cache(maxsize=16)
def _xlsx_sheet_member(workbook: str, sheet_name: str) -> str:
    path = Path(workbook)
    with zipfile.ZipFile(path) as zf:
        workbook_xml = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_xml = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rels = {
            rel.attrib["Id"]: rel.attrib["Target"].lstrip("/")
            for rel in rels_xml.findall("rel:Relationship", XLSX_NS)
        }
        sheets = workbook_xml.find("a:sheets", XLSX_NS)
        if sheets is None:
            raise RuntimeError(f"No sheets found in {path}")
        for sheet in sheets.findall("a:sheet", XLSX_NS):
            if sheet.attrib.get("name") != sheet_name:
                continue
            rel_id = sheet.attrib[f"{{{XLSX_NS_OFFICE_REL}}}id"]
            target = rels[rel_id]
            return target if target.startswith("xl/") else f"xl/{target}"
    raise KeyError(f"Sheet {sheet_name!r} not found in {path}")


def _xlsx_cell_value(cell: ET.Element, shared_strings: tuple[str, ...]) -> str | None:
    cell_type = cell.attrib.get("t")
    value = cell.find(VALUE_TAG)
    if cell_type == "s":
        if value is None or value.text is None:
            return None
        idx = int(value.text)
        return shared_strings[idx] if 0 <= idx < len(shared_strings) else None
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(TEXT_TAG)) or None
    if value is None:
        return None
    return value.text


def read_xlsx_sheet(workbook: Path, sheet_name: str) -> pd.DataFrame:
    shared_strings = _xlsx_shared_strings(str(workbook))
    member = _xlsx_sheet_member(str(workbook), sheet_name)
    rows: list[list[str | None]] = []
    with zipfile.ZipFile(workbook) as zf:
        with zf.open(member) as fh:
            for _, elem in ET.iterparse(fh, events=("end",)):
                if elem.tag != ROW_TAG:
                    continue
                values: list[str | None] = []
                for cell in elem:
                    if cell.tag != CELL_TAG:
                        continue
                    idx = _xlsx_col_index(cell.attrib.get("r", "A1"))
                    while len(values) < idx - 1:
                        values.append(None)
                    values.append(_xlsx_cell_value(cell, shared_strings))
                rows.append(values)
                elem.clear()

    if not rows:
        return pd.DataFrame()
    headers = [str(v or "").strip() for v in rows[0]]
    width = len(headers)
    records = []
    for row in rows[1:]:
        padded = (row + [None] * width)[:width]
        if any(v not in (None, "") for v in padded):
            records.append(padded)
    return pd.DataFrame(records, columns=headers)


def load_color_products(workbook: Path = COLOR_WORKBOOK, include_dont_use: bool = False) -> pd.DataFrame:
    df = read_xlsx_sheet(workbook, "Products")
    # "Item Number" is populated for every row; "ProductNumber" only for rows that have
    # been run through the upsert tool's extra-spec columns. Prefer ProductNumber when
    # present, otherwise fall back to Item Number so all 171 analyzed images are usable.
    df["ITEM_KEY"] = df["ProductNumber"].where(df["ProductNumber"].notna(), df["Item Number"])
    df = df[df["ITEM_KEY"].notna()].copy()
    df["ITEM_KEY"] = df["ITEM_KEY"].map(norm_item)
    if not include_dont_use:
        blocked_keys = dont_use_item_keys(df)
        if blocked_keys:
            df = df[~df["ITEM_KEY"].isin(blocked_keys)].copy()
    df = df.drop_duplicates(subset="ITEM_KEY", keep="first")
    return df


def load_master_specs() -> pd.DataFrame:
    df = read_xlsx_sheet(MASTER_LIST_WORKBOOK, "Everything")
    df = df[df["PRODUCT NUMBER"].notna() & (df["PRODUCT NUMBER"].astype(str).str.strip().str.upper() != "NA")].copy()
    df["ITEM_KEY"] = df["PRODUCT NUMBER"].map(norm_item)
    out = pd.DataFrame({"ITEM_KEY": df["ITEM_KEY"]})
    for src, dst in MASTER_SPEC_FIELDS.items():
        if src not in df.columns:
            out[dst] = ""
        elif dst == "width":
            out[dst] = df[src].map(normalize_width)
        else:
            out[dst] = df[src].map(lambda v: norm_item(v) if v is not None else "")
    out["MASTER_COLLECTION"] = df["COLLECTION"].map(norm_item) if "COLLECTION" in df.columns else ""
    out["MASTER_COLOR"] = df["COLOR"].map(norm_item) if "COLOR" in df.columns else ""
    out = out.drop_duplicates(subset="ITEM_KEY", keep="first")
    return out


def load_family_items(family: str, po_since: str | None = DEFAULT_PO_SINCE) -> pd.DataFrame:
    """Active-only item universe for a family: not flagged Dropped (IMDROP='D')
    and, if po_since is set, has received a PO (real physical arrival,
    IRSRC='P') on or after that date (YYYY-MM-DD). Excludes old/discontinued/
    dropped colors that still carry historical sales or spec rows but aren't
    current offerings -- pass po_since=None to skip the PO-recency check (the
    IMDROP check always applies)."""
    conn = connect()
    try:
        active_clause = f"AND {active_item_conditions('IM', po_since=po_since)}"
        retired_clause = retired_collection_clause("TRIM(IMX.IMCOLLECT)")
        query = f"""
        SELECT DISTINCT
            TRIM(IM.IMITEM) AS ITEM_KEY_RAW,
            TRIM(IM.IMDESC) AS DESCRIPTION,
            TRIM(IMX.IMCOLLECT) AS COLLECTION
        FROM GSFL2K.ITEMMAST IM
        LEFT JOIN GSFL2K.ITEMXTRA IMX
            ON IM.IMITEM = IMX.IMXITM
        WHERE
            IM.IMFMCD = '{family.upper()}'
            AND {retired_clause}
            {active_clause}
        """
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    df.columns = [c.upper() for c in df.columns]
    df = df[~df["DESCRIPTION"].map(is_dont_use_description)].copy()
    df["ITEM_KEY"] = df["ITEM_KEY_RAW"].map(norm_item)
    return df[["ITEM_KEY", "DESCRIPTION", "COLLECTION"]]


def build_merged(family: str, color_workbook: Path = COLOR_WORKBOOK, po_since: str | None = DEFAULT_PO_SINCE) -> pd.DataFrame:
    family_items = load_family_items(family, po_since)
    color_df_all = load_color_products(color_workbook, include_dont_use=True)
    master_df = load_master_specs()
    blocked_keys = dont_use_item_keys(color_df_all)
    blocked_keys |= dont_use_item_keys(master_df)
    if blocked_keys:
        family_items = family_items[~family_items["ITEM_KEY"].isin(blocked_keys)].copy()
        color_df_all = color_df_all[~color_df_all["ITEM_KEY"].isin(blocked_keys)].copy()
        master_df = master_df[~master_df["ITEM_KEY"].isin(blocked_keys)].copy()
    color_df = color_df_all.drop_duplicates(subset="ITEM_KEY", keep="first")

    merged = family_items.merge(color_df, on="ITEM_KEY", how="left", suffixes=("", "_color"))
    merged = merged.merge(master_df, on="ITEM_KEY", how="left", suffixes=("", "_master"))
    merged["COLLECTION"] = merged["COLLECTION"].fillna(merged.get("Collection")).fillna(merged.get("MASTER_COLLECTION"))
    return merged


def pairwise_similarity(merged: pd.DataFrame, color_weight: float = 0.5, delta_e_scale: float = 20.0) -> pd.DataFrame:
    """Vectorized over all i<j pairs: pairwise Lab color distance (Delta-E76) plus a
    categorical spec match fraction across CATEGORICAL_SPEC_FIELDS, combined into one
    0-1 similarity score. O(n^2) memory, not O(n^2) Python-level iterations -- keeps
    this usable at full-family scale (thousands of items) instead of minutes-long loops."""
    merged = merged.reset_index(drop=True)
    n = len(merged)
    keys = merged["ITEM_KEY"].to_numpy()
    collections = merged["COLLECTION"].fillna("").astype(str).to_numpy()

    if "L_mean" in merged.columns:
        L = pd.to_numeric(merged["L_mean"], errors="coerce").to_numpy()
        A = pd.to_numeric(merged["a_mean"], errors="coerce").to_numpy()
        Bv = pd.to_numeric(merged["b_mean"], errors="coerce").to_numpy()
        color_valid = ~np.isnan(L) & ~np.isnan(A) & ~np.isnan(Bv)
        delta_e = np.sqrt(
            (L[:, None] - L[None, :]) ** 2
            + (A[:, None] - A[None, :]) ** 2
            + (Bv[:, None] - Bv[None, :]) ** 2
        )
        color_pair_valid = color_valid[:, None] & color_valid[None, :]
    else:
        delta_e = np.full((n, n), np.nan)
        color_pair_valid = np.zeros((n, n), dtype=bool)

    spec_matches = np.zeros((n, n), dtype=int)
    spec_totals = np.zeros((n, n), dtype=int)
    for f in CATEGORICAL_SPEC_FIELDS:
        if f not in merged.columns:
            continue
        vals = merged[f].fillna("").astype(str).to_numpy()
        present_pair = (vals != "")[:, None] & (vals != "")[None, :]
        spec_totals += present_pair
        spec_matches += present_pair & (vals[:, None] == vals[None, :])

    spec_score = np.divide(
        spec_matches, spec_totals, out=np.zeros((n, n)), where=spec_totals > 0
    )
    color_score = np.clip(1.0 - (delta_e / delta_e_scale), 0.0, 1.0)
    combined = np.where(color_pair_valid, color_weight * color_score + (1 - color_weight) * spec_score, spec_score)

    iu = np.triu_indices(n, k=1)
    keep = (color_pair_valid[iu] | (spec_totals[iu] > 0)) & (keys[iu[0]] != keys[iu[1]])
    i_idx, j_idx = iu[0][keep], iu[1][keep]

    df = pd.DataFrame({
        "ITEM_A": keys[i_idx], "ITEM_B": keys[j_idx],
        "COLLECTION_A": collections[i_idx], "COLLECTION_B": collections[j_idx],
        "SAME_COLLECTION": (collections[i_idx] != "") & (collections[i_idx] == collections[j_idx]),
        "DELTA_E": delta_e[i_idx, j_idx],
        "COLOR_SCORE": np.where(color_pair_valid[i_idx, j_idx], color_score[i_idx, j_idx], np.nan),
        "HAS_COLOR_COMPARISON": color_pair_valid[i_idx, j_idx],
        "SPEC_MATCH_SCORE": spec_score[i_idx, j_idx],
        "SPEC_FIELDS_COMPARED": spec_totals[i_idx, j_idx],
        "COMBINED_SIMILARITY": combined[i_idx, j_idx],
    })
    df = df[~df.apply(lambda r: is_companion_pair(r["ITEM_A"], r["ITEM_B"]), axis=1)]
    return df.sort_values("COMBINED_SIMILARITY", ascending=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Flag too-similar item pairs within a product family.")
    parser.add_argument("--family", default="EN")
    parser.add_argument("--threshold", type=float, default=0.80, help="Combined similarity flag threshold (0-1)")
    parser.add_argument("--color-weight", type=float, default=0.5)
    parser.add_argument("--color-workbook", type=Path, default=COLOR_WORKBOOK, help="Override path (e.g. a pending copy while the live file is locked open)")
    parser.add_argument("--po-since", type=str, default=DEFAULT_PO_SINCE, help="Only include items with a received PO on/after this date (YYYY-MM-DD). Empty string disables the filter.")
    args = parser.parse_args()

    merged = build_merged(args.family, args.color_workbook, args.po_since or None)
    has_color = merged["L_mean"].notna().sum() if "L_mean" in merged.columns else 0
    has_spec = merged[CATEGORICAL_SPEC_FIELDS[0]].notna().sum() if CATEGORICAL_SPEC_FIELDS[0] in merged.columns else 0
    po_note = f"active items (PO received on/after {args.po_since})" if args.po_since else "ALL items (no PO-activity filter)"
    print(f"Family {args.family}: {len(merged)} {po_note}, {has_color} with color metrics, {has_spec} with master specs.")

    sim = pairwise_similarity(merged, color_weight=args.color_weight)
    OUT_DIR.mkdir(exist_ok=True)
    out_all = OUT_DIR / f"{args.family.lower()}_similarity_pairs.csv"
    sim.to_csv(out_all, index=False)

    # Spec text (species/grade/finish/thickness/...) is set at the collection level, so
    # same-collection color variants share it near-100% of the time regardless of how
    # visually distinct they are. A spec-only match is not evidence of visual confusion --
    # require an actual measured color comparison (Delta-E) for the headline "too similar"
    # flag; spec-only high-matches are surfaced separately as photo-review candidates.
    color_confirmed = sim[sim["HAS_COLOR_COMPARISON"]]
    flagged = color_confirmed[color_confirmed["COMBINED_SIMILARITY"] >= args.threshold]
    out_flag = OUT_DIR / f"{args.family.lower()}_too_similar_flagged.csv"
    flagged.to_csv(out_flag, index=False)

    needs_photo_review = sim[
        (~sim["HAS_COLOR_COMPARISON"]) & sim["SAME_COLLECTION"] & (sim["SPEC_MATCH_SCORE"] >= 0.9)
    ]
    out_review = OUT_DIR / f"{args.family.lower()}_needs_color_photo_review.csv"
    needs_photo_review.to_csv(out_review, index=False)

    print(f"{len(sim)} pairs scored ({len(color_confirmed)} with a measured color comparison).")
    print(f"{len(flagged)} flagged as too similar (color + spec) at threshold {args.threshold}.")
    print(f"{len(needs_photo_review)} same-collection pairs match on spec but have no color photo yet.")
    print(f"All pairs:          {out_all}")
    print(f"Flagged:            {out_flag}")
    print(f"Needs photo review: {out_review}")
    if len(flagged):
        print(flagged.head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
