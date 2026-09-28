# Category Intelligence Dashboard Handoff

Saved on 2026-08-28 for resuming after restart.

## Workspace

- Root: `\\server\home$\niko\2025\NewForecastingModel\OMPForecasting5`
- Dashboard folder: `catalog_intelligence`
- Main dashboard artifact: `catalog_intelligence\en_catalog_dashboard.html`
- Main embedded data file: `catalog_intelligence\data\en_dashboard_data.json`

## Current Dashboard State

- Dashboard title: `EN Catalog Intelligence`
- Active family: `EN`
- Active item rule: active items only, PO received on or after `2025-01-01`
- Current generated data check:
  - `catalogItems`: 137
  - `totalFamilyItems`: 142
  - `totalItemsWithSales`: 137
  - `tooSimilarFlagged`: 8

## Implemented Features

- Added Catalog Family Tree section with selectable hierarchy levels:
  - Species
  - Width
  - Thickness
  - Wear layer
  - Lengths
  - Cut
  - Bevel
  - Color
- Default hierarchy is Color then Width.
- Leaf buckets split products into Good, Better, Best, and Unpriced tiers.
- Family tree nodes show item count, average similarity, average absolute correlation, and max interference score.
- Item cards in the tree show item number, description, color/product context, swatch, and average sell price.
- Text in tree item cards can wrap instead of overflowing.
- Color bucket branches show an average color swatch next to the branch title.
- Width values are normalized so examples like `7-1/2"`, `7 1/2"`, and `7.5"` all bucket as `7.5"`.
- Items marked with `DON'T USE`, `DONT USE`, or `DO NOT USE` are excluded, including old placeholder image rows from master/color descriptive fields.
- The old herringbone placeholder items currently blocked from the EN dashboard are:
  - `GFALPH401-L`
  - `GFALPH401-R`
  - `GFALPH403-L`
  - `GFALPH403-R`
  - `GFALPH405-L`
  - `GFALPH405-R`
- Most Correlated Item Pairs and Substitute-Effect Pairs now show item descriptions next to SKU numbers and wrap cleanly.
- Item Action Plan now uses a Description column instead of Collection.
- Item Action Plan Similar and Substitute columns now show partner SKU plus partner description and wrap cleanly.
- Item Action Plan filter searches item number, description, product name, collection, color bucket, species, width, similar/substitute partner numbers, partner descriptions, and action.

## Too-Similar Items Definition

The Too-Similar Items section is now focused on cannibalization candidates, not generic visual similarity.

Current rule:

- Sales correlation below `-0.20`
- Same hue/color bucket
- Same species
- Average sell price within `$0.50/SF`

The current EN run returns 8 matching pairs. Examples from the last check:

- `GFGLR9502` / `GFGLR9505`: corr `-0.515`, bucket `Golden Oak`, same species, price gap `$0.02/SF`
- `GHCVR538` / `GHCVR538USA`: corr `-0.427`, bucket `Warm Blonde`, same species, price gap `$0.21/SF`
- `GFALO7504` / `GFLEO9501`: corr `-0.405`, bucket `Honey Blonde`, same species, price gap `$0.46/SF`

## Important Source Files

- `catalog_intelligence\render_catalog_dashboard.py`
  - Renders the self-contained HTML dashboard.
  - Contains layout, tree rendering, item/pair labels, wrapping styles, color bucket average swatches, and static table definitions.
- `catalog_intelligence\compile_dashboard_data.py`
  - Compiles all CSV/workbook/Gartman analysis into `data\en_dashboard_data.json`.
  - Contains Too-Similar Items cannibalization rule.
  - Builds `catalogItems`, `interferencePairs`, and `tooSimilar`.
- `catalog_intelligence\catalog_similarity.py`
  - Loads color workbook and master spec workbook.
  - Contains width normalization and `DON'T USE` filtering helpers.
  - Generates `data\en_similarity_pairs.csv`, `data\en_too_similar_flagged.csv`, and `data\en_needs_color_photo_review.csv`.
- `catalog_intelligence\correlation_hmm_analysis.py`
  - Builds item/collection correlations, HMM regimes, and recommendation CSVs.
  - Also excludes color/master `DON'T USE` placeholder keys.
- `catalog_intelligence\pull_family_sales.py`
  - Pulls monthly sales rows from Gartman.
  - SQL excludes sample/special/custom/discontinued/do-not-use rows.

## Data Sources Referenced By Code

- Color workbook:
  - `H:\2025\MISC Reports\For Marketing\Garrison Color Analysis 2.12.2026.xlsx`
- Master spec workbook:
  - `C:\Users\niko\Downloads\MASTER Garrison Product Information List AS OF 5.7.26.xlsx`
- Gartman database connection comes through `core\db_connection.py`.

## Refresh Commands

Run from repo root:

```powershell
& .venv\Scripts\python.exe catalog_intelligence\catalog_similarity.py --family EN
& .venv\Scripts\python.exe catalog_intelligence\correlation_hmm_analysis.py --family EN
& .venv\Scripts\python.exe catalog_intelligence\compile_dashboard_data.py --family EN
& .venv\Scripts\python.exe catalog_intelligence\render_catalog_dashboard.py --family EN --out catalog_intelligence\en_catalog_dashboard.html
```

For only display/template changes, usually only this is needed:

```powershell
& .venv\Scripts\python.exe catalog_intelligence\render_catalog_dashboard.py --family EN --out catalog_intelligence\en_catalog_dashboard.html
```

For code syntax checks:

```powershell
& .venv\Scripts\python.exe -m py_compile catalog_intelligence\catalog_similarity.py catalog_intelligence\correlation_hmm_analysis.py catalog_intelligence\compile_dashboard_data.py catalog_intelligence\pull_family_sales.py catalog_intelligence\render_catalog_dashboard.py
```

## Known Notes

- `git status --short -- catalog_intelligence` currently shows the whole `catalog_intelligence` folder as untracked. Avoid assuming all files are new from the current turn.
- A `FutureWarning` from pandas about `PeriodIndex` appears during data compilation; it has not blocked output.
- Headless Chrome screenshots against UNC `file:` paths appeared stale during one check. The generated HTML payload itself was verified by parsing `/*DATA_START*/.../*DATA_END*/`.
- The dashboard is self-contained HTML; open `catalog_intelligence\en_catalog_dashboard.html` after rendering.

## Last Verification

- Renderer compiled successfully.
- Dashboard HTML regenerated successfully.
- Embedded HTML data parsed successfully.
- Too-Similar Items count in generated HTML: 8.
- Generated HTML contains the updated Action Plan description/partner label helpers.
- Generated HTML contains the updated `correlation below -0.20` Too-Similar rule text.
