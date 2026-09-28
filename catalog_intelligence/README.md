# Catalog Intelligence Toolkit

Sales correlation, Hidden-Markov regime detection, and color/spec "too similar"
analysis for a Gartman flooring product family. Built for family `EN`
(engineered hardwood) but every script takes `--family` and works for any
other family code (`PF`, `SF`, `DF`, `VI`, `VF`, `SP`, `RF`, `CF`, `LA`).

## Active-item filter (all scripts, `--po-since`, default 2025-01-01)

A Gartman item number carrying historical sales or spec rows does NOT mean
it's a current color — old, discontinued, and dropped colors keep their
history forever. `active_items.py` requires BOTH:

1. **`ITEMMAST.IMDROP <> 'D'`** — Gartman's own "Dropped?" status flag.
   Confirmed against real examples: items reported as discontinued Villa
   Gialla/Canyon Crest colors (GFVGO902, GFVGO909, GFCYO1005) all carry
   `IMDROP='D'`. Always applied, regardless of `--po-since`.
2. A received PO on or after `--po-since` (a fixed calendar date, default
   `2025-01-01`): `EXISTS` against `GSFL2K.ITEMRECH` with `IRSRC='P'` (real
   physical purchase receipts, not a last-touched date — see
   `inventory_received_365days.sql`).

Signal 2 alone under-excludes: a dropped item can carry one last closeout
receipt inside the window — signal 1 is what actually catches those. This
was originally a rolling 24-month window, but per user direction (2026-08-15)
a rolling window still let through items/collections with a stray PO receipt
from 2+ years back, so it's now a fixed cutoff date that has to be bumped
forward manually over time rather than silently drifting. `IMDROP` also takes
values `'G'` (261 EN items, meaning unconfirmed — some are literally
described "DISCONTINUED", others look ordinary) and `'X'` (12 items, several
recently-maintained and look current) — per user direction (2026-08-14) only
`'D'` is excluded until those are confirmed; don't extend the exclusion to
`G`/`X` without checking first. Pass `--po-since ""` (empty string) to skip
the PO-recency check (IMDROP still applies).

A collection can still appear after the per-item filter even if some of its
colors were dropped, since the filter operates per-item, not per-collection —
e.g. Villa Gialla and Canyon Crest each had a couple of colors (GFVGO903,
GFVGO914, GFCYO1008) with `IMDROP=' '` and recent PO receipts, so they passed
the item-level check even though the collections themselves are retired lines.
No Gartman table encodes collection-level status — `GSFL2K.ITEMXTRA.IMXCOLLECT`
is a plain text field — so `active_items.RETIRED_COLLECTIONS` is a manually
maintained, user-confirmed set (currently `{VILLA GIALLA, CANYON CREST}`,
confirmed 2026-08-14) applied in both `pull_family_sales.py` and
`catalog_similarity.py`'s `load_family_items`. Extend it only after confirming
with the user which collections are actually retired at the business level.

## Why hand-rolled HMM

`hmmlearn` has no prebuilt wheel for Python 3.14 and this machine has no C++
build toolchain, so `gaussian_hmm.py` reimplements the small slice of it this
project needs (diagonal-covariance Gaussian HMM, scaled forward-backward
Baum-Welch fit, Viterbi decode) in plain numpy. No other dependency changes.

## Scripts

1. **`pull_family_sales.py`** — pulls monthly SF/$ sales by item, family, and
   collection straight from Gartman (`core.db_connection.connect()`), caches
   to `data/<family>_monthly_sales.csv`. The pull window always ends on the
   last day of the most recently *completed* calendar month
   (`last_complete_month_end()`), never today — including the current
   in-progress month (e.g. 17 of 31 days) makes every trailing-window
   comparison look like a decline regardless of real momentum, since the
   partial month is always short of a full one. Confirmed (2026-08-17)
   against a user-provided last-3-months-vs-prior-3 pivot that excluded the
   current month: once the pull window matched, every collection's percentage
   lined up with theirs to within rounding — this had been silently
   distorting every regime, correlation, and sparkline in the toolkit.

   ```
   .venv\Scripts\python.exe catalog_intelligence\pull_family_sales.py --family EN --months 36
   ```

2. **`catalog_similarity.py`** — cross-references the
   *Garrison Color Analysis* workbook (perceptual Lab color metrics from
   product photos) against the *MASTER Garrison Product Information List*
   (species/grade/finish/construction/dimensions) for every item in a family,
   and flags pairs that are hard to tell apart.

   ```
   .venv\Scripts\python.exe catalog_intelligence\catalog_similarity.py --family EN --threshold 0.80
   ```

   Key design point: spec text (species, finish, thickness...) is set at the
   *collection* level, so two different named colors in the same collection
   share nearly all spec fields almost by construction — a spec-only match is
   not evidence of visual confusion. The headline **too-similar flag requires
   a real color-photo comparison** (Delta-E76 in Lab space) in addition to the
   spec match; same-collection pairs that match on spec but have no photo yet
   land in `<family>_needs_color_photo_review.csv` instead of being flagged.

   Outputs (`data/`): `<family>_similarity_pairs.csv` (everything scored),
   `<family>_too_similar_flagged.csv` (color+spec confirmed), 
   `<family>_needs_color_photo_review.csv` (spec-only, no photo yet).

3. **`correlation_hmm_analysis.py`** — the main analysis:
   - Collection-vs-collection and item-vs-item (within the family) monthly
     sales correlation matrices, pairwise-complete over each pair's
     overlapping active window.
   - A 3-state Gaussian HMM per collection/item over
     `[standardized log-level, standardized month-over-month slope]` —
     ranking uses the *slope* feature to label Declining/Stable/Growing, not
     raw level, so a big item that's turning around isn't mistaken for a
     small item that's merely still large. Requires `MIN_MONTHS_FOR_HMM`
     (12, lowered from 15 on 2026-08-17 per user request — a catalog shakeup
     left many collections without 15mo of history yet) months of sales
     history to fit at all; items below that show up with no regime
     ("insufficient history" in the dashboard) rather than a noisy fit.
   - **`current_regime` is a 3-month majority vote**, not just the single most
     recent month (`SMOOTH_WINDOW = 3`, added 2026-08-17) — a single noisy
     month can otherwise flip the label on a collection that's actually
     recovering off a trough (see GII Distressed below).
   - **Deseasonalized first** (`family_seasonal_index()`, added 2026-08-17):
     EN flooring sales have a known calendar-month seasonal pattern (August is
     the deepest trough, ~-22% in log-space; October/July run high) —
     without removing that, a shared seasonal dip gets fit as a "Declining"
     regime across nearly every collection simultaneously, which is a
     seasonality artifact, not a per-collection signal. The seasonal index is
     estimated once from the family-wide total (summed across all items, so
     it has ~3 years of cycles even though most individual series don't),
     then subtracted (in log space, by calendar month) from every
     collection/item series before the HMM ever sees it.
   - Cross-references `catalog_similarity.py`'s output: pairs that are BOTH
     highly correlated in sales AND color/spec-confirmed similar go to
     `consolidation_review` (true redundancy candidates); highly correlated
     pairs that aren't visually similar are left in
     `highly_correlated_pairs` (shared demand driver, not redundancy);
     negatively correlated + similar pairs go to `substitute_effect_pairs`
     (one item's growth is coming at the other's expense).
   - `-L`/`-R` companion SKUs (stair-nose, herringbone/parquet piece sets sold
     together as one install) are excluded everywhere — they're supposed to
     move together and look alike; that's not redundancy.
   - `photo_priority`: sales-active items with no color analysis yet, ranked
     by volume, so color-analysis effort goes where it'll actually sharpen a
     consolidation call next.

   ```
   .venv\Scripts\python.exe catalog_intelligence\correlation_hmm_analysis.py --family EN
   ```

   Outputs (`data/`): `<family>_collection_correlation.csv`,
   `<family>_item_correlation.csv`, `<family>_collection_regimes.csv`,
   `<family>_item_regimes.csv`, `<family>_phase_out_candidates.csv`,
   `<family>_expansion_candidates.csv`, `<family>_consolidation_review.csv`,
   `<family>_highly_correlated_pairs.csv`, `<family>_substitute_effect_pairs.csv`,
   `<family>_photo_priority.csv`. (These per-collection CSVs are still written
   for ad hoc inspection, but the dashboard itself no longer displays anything
   at the collection level — see point 4.)

4. **`compile_dashboard_data.py`** builds an **item action table**
   (`DATA.itemActions` in the dashboard, rendered as the "Item action plan"
   card at the very top) — one row per *item* (color), not per collection.
   Per user direction (2026-08-18): individual colors get dropped, kept, or
   reshuffled into a differently-named collection far more often than whole
   collections disappear, so collection membership is too unstable to be the
   dashboard's organizing unit — it's shown only as a context column on each
   item row. Each row has: 3-month-vs-prior-3-month sales momentum, the HMM
   regime shown alongside for a longer-run read, the single best
   color/spec-similar item and the single strongest sales-substitute item
   (with their score, via `item_pair_summary()` — not just a count, so you
   can drill straight into the specific redundancy), whether a color photo
   has been analyzed, and a recommended action (`Drop candidate` /
   `Phase-out watch` / `Expand candidate` / `Expand — needs photo analysis` /
   `Watch`). A text filter box above the table searches item, collection, and
   partner columns at once. **Momentum drives the recommendation, not the HMM
   regime** — this rule predates the item-level rework (added 2026-08-17 at
   the collection level after the user's own last-3-vs-prior-3 Excel pivot
   disagreed with the dashboard; see memory for the full diagnosis: a
   partial-current-month bug, now fixed, explained most of it, but GII
   Distressed's regime-vs-momentum disagreement was a real, distinct case).
   `recommend_action()` in `compile_dashboard_data.py` is a plain, transparent
   threshold rule (±10% momentum), not a hidden score — read it before
   trusting/tuning it. The collection-level regime table, correlation
   heatmap, and separate phase-out/expansion tables were removed from the
   dashboard as part of this change (superseded by the item action table);
   the underlying collection-level CSVs are unaffected if you need them.

## Known coverage gap (EN, as of 2026-08-14)

Of the 191 EN items with a PO received in the last 24 months, 121 have been
through color analysis and 135 have a match in the master spec list (a
current-offering catalog, not a full historical item master) — so
`consolidation_review` will stay thin until more of the remainder get
photographed. `photo_priority` output tells you which high-volume gaps to
close first.

## Extending to another family

Every script takes `--family`. To add PF (or several at once for the pull):

```
.venv\Scripts\python.exe catalog_intelligence\pull_family_sales.py --family PF
.venv\Scripts\python.exe catalog_intelligence\catalog_similarity.py --family PF
.venv\Scripts\python.exe catalog_intelligence\correlation_hmm_analysis.py --family PF
```
