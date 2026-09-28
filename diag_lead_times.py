"""
Diagnostic: Lead Times file resolution
Shows which Lead Times.xlsx each app actually reads, whether the file exists,
and what values it returns for key SKUs.
"""

from pathlib import Path
import pandas as pd

SKUS_TO_CHECK = [
    "GFLEO9502O", "GFLEO7503O", "GFLEPH501O-L", "GFLEPH501O-R",  # NAT.OIL (problem items)
    "GFLEO9502",  "GFLEO7503",                                     # originals (should be 112)
]

# ── Paths each app resolves ────────────────────────────────────────────────────
SHAREPOINT_DIR   = Path(r"C:\Users\niko\OneDrive - Old Master Products\Purchasing - Flooring Reports\Dashboard Files")
APP_DIR          = Path(__file__).resolve().parent

paths = {
    "flooringwebapp  (LEADTIMES_XLSX)":       SHAREPOINT_DIR / "Lead Times.xlsx",
    "OMPforecasting5 (local project copy)":   APP_DIR / "Lead Times.xlsx",
}

# ── Helper ─────────────────────────────────────────────────────────────────────
def read_lead_times(path: Path):
    """Return (dict[sku->lt], error_str). SKUs are uppercased."""
    try:
        df = pd.read_excel(path, sheet_name="Final", header=0)
        for col in ["SKU", "Item Number", "ITEM_NUMBER"]:
            if col in df.columns:
                sku_col = col
                break
        else:
            return None, "No SKU/Item Number column found"
        for col in ["FINAL", "Final", "final"]:
            if col in df.columns:
                lt_col = col
                break
        else:
            return None, "No FINAL column found"
        df[sku_col] = df[sku_col].astype(str).str.strip().str.upper()
        df[lt_col]  = pd.to_numeric(df[lt_col], errors="coerce")
        df = df.dropna(subset=[sku_col, lt_col])
        return dict(zip(df[sku_col], df[lt_col])), None
    except Exception as e:
        return None, str(e)

# ── Run ────────────────────────────────────────────────────────────────────────
SEP = "-" * 72

print(SEP)
print("LEAD TIMES FILE DIAGNOSTIC")
print(SEP)

for label, path in paths.items():
    print(f"\n[{label}]")
    print(f"  Path   : {path}")

    exists = path.exists()
    print(f"  Exists : {'YES' if exists else 'NO  <-- FILE NOT FOUND'}")

    if not exists:
        # Show what IS in the parent dir (helps spot the right folder)
        parent = path.parent
        if parent.exists():
            xlsx_files = [f.name for f in parent.iterdir() if f.suffix.lower() == ".xlsx"]
            print(f"  .xlsx files in {parent.name}/: {xlsx_files or '(none)'}")
        continue

    import os
    mtime = os.path.getmtime(path)
    import datetime
    print(f"  Modified: {datetime.datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M:%S}")

    lt_map, err = read_lead_times(path)
    if err:
        print(f"  Read error: {err}")
        continue

    print(f"  Total SKUs loaded: {len(lt_map)}")
    print(f"  {'SKU':<20}  {'Lead Time (days)':>18}  {'Status'}")
    print(f"  {'-'*20}  {'-'*18}  {'-'*20}")
    for sku in SKUS_TO_CHECK:
        key = sku.upper()
        if key in lt_map:
            val = lt_map[key]
            status = "OK" if val > 0 else "ZERO - will break forecast!"
        else:
            val = "NOT FOUND"
            status = "Missing - fallback used"
        print(f"  {sku:<20}  {str(val):>18}  {status}")

print(f"\n{SEP}")
print("SUMMARY")
print(SEP)
found = [lbl for lbl, p in paths.items() if p.exists()]
missing = [lbl for lbl, p in paths.items() if not p.exists()]
print(f"  Files found   : {len(found)}")
for lbl in found:
    print(f"    + {lbl}")
print(f"  Files missing : {len(missing)}")
for lbl in missing:
    print(f"    - {lbl}")
