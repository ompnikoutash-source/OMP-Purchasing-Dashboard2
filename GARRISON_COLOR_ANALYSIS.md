# Garrison Color Analysis Tool

Use `garrison_color_analysis.py` to analyze product images and optionally update the
Garrison Color Analysis workbook.

## Analyze an Image

This only prints color metrics and does not touch the workbook.

```powershell
.\.venv\Scripts\python.exe garrison_color_analysis.py analyze --image "H:\2025\MISC Reports\For Marketing\Overheads for Color Analysis\European Oak Altura 7.5.jpg"
```

## Preview an Update

This opens the workbook, finds the matching product row, and shows what would be
updated. It still does not save anything.

```powershell
.\.venv\Scripts\python.exe garrison_color_analysis.py upsert --image "H:\2025\MISC Reports\For Marketing\Overheads for Color Analysis\European Oak Altura 7.5.jpg" --product-number GFALO7501 --collection Allora --product Altura --bucket 5 --undertone Neutral
```

## Save an Update

Add `--write` to save. The tool creates a timestamped workbook backup first. If
the workbook is open/locked, it saves a pending copy beside the workbook instead.

```powershell
.\.venv\Scripts\python.exe garrison_color_analysis.py upsert --image "H:\2025\MISC Reports\For Marketing\Overheads for Color Analysis\European Oak Altura 7.5.jpg" --product-number GFALO7501 --collection Allora --product Altura --bucket 5 --undertone Neutral --write
```

By default, save mode also refreshes the `Buckets` and `Summary` sheets. Use
`--no-rollups` only when you intentionally want to skip those rollups.
