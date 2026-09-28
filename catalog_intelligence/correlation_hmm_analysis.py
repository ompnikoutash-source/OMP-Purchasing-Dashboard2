"""
Sales correlation + Hidden Markov regime analysis for a flooring product family.

Pulls (or reuses a cached CSV from pull_family_sales.py), then:
  1. Builds collection-vs-collection and item-vs-item monthly sales correlation matrices.
  2. Fits a 3-state Gaussian HMM (Declining / Stable / Growing) per collection and per
     item with enough history, using [standardized log-level, standardized slope] as
     the observation (ranking uses slope, not raw level growth, since slope is what
     actually distinguishes a turning-around item from one still sliding).
  3. Cross-references catalog_similarity.py output (if present) so redundancy calls
     combine "correlated in sales" with "actually look/spec alike" instead of treating
     any co-moving pair as a duplicate.
  4. Emits a ranked recommendation table: expansion candidates, phase-out candidates,
     consolidation-review pairs, and substitute-effect pairs.

Usage:
    .venv\\Scripts\\python.exe catalog_intelligence\\correlation_hmm_analysis.py --family EN
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gaussian_hmm import GaussianHMM  # noqa: E402
from pull_family_sales import pull  # noqa: E402
from item_pairing import is_companion_pair  # noqa: E402
from catalog_similarity import dont_use_item_keys, is_dont_use_description, load_color_products, load_master_specs, norm_item  # noqa: E402
from active_items import DEFAULT_PO_SINCE  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
MIN_MONTHS_FOR_HMM = 12  # lowered from 15 (2026-08-17) -- a catalog shakeup left many
# collections without 15mo of history yet; a 3-state HMM fit on 12 points is thinner
# but was the user's chosen tradeoff over leaving those items unclassified
STATE_LABELS = ["Declining", "Stable", "Growing"]


def load_or_pull(family: str, months: int, refresh: bool, po_since: str | None = DEFAULT_PO_SINCE) -> pd.DataFrame:
    cache = DATA_DIR / f"{family.lower()}_monthly_sales.csv"
    if cache.exists() and not refresh:
        df = pd.read_csv(cache)
    else:
        df = pull([family], months, po_since)
        DATA_DIR.mkdir(exist_ok=True)
        df.to_csv(cache, index=False)
    if "DESCRIPTION" in df.columns:
        df = df[~df["DESCRIPTION"].map(is_dont_use_description)].copy()
    df["PERIOD"] = pd.PeriodIndex(year=df["SALE_YEAR"], month=df["SALE_MONTH"], freq="M")
    return df


def build_pivot(df: pd.DataFrame, group_col: str, value_col: str = "SQFT_SOLD") -> pd.DataFrame:
    agg = df.groupby([group_col, "PERIOD"], as_index=False)[value_col].sum()
    full_periods = pd.period_range(df["PERIOD"].min(), df["PERIOD"].max(), freq="M")
    pivot = agg.pivot(index="PERIOD", columns=group_col, values=value_col).reindex(full_periods)

    # Fill 0 for months within each series' own active window (first->last observed period);
    # leave NaN outside that window so correlation uses only the overlapping active period.
    for col in pivot.columns:
        s = pivot[col]
        first, last = s.first_valid_index(), s.last_valid_index()
        if first is None:
            continue
        pivot.loc[first:last, col] = s.loc[first:last].fillna(0.0)
    return pivot


def correlation_matrix(pivot: pd.DataFrame, min_overlap_months: int = 6) -> pd.DataFrame:
    corr = pivot.corr(min_periods=min_overlap_months)
    return corr


def family_seasonal_index(df: pd.DataFrame) -> dict[int, float]:
    """Additive, log-space seasonal index by calendar month (1-12), estimated
    from the family-wide total monthly sales (summed across every item, so it
    has ~3 years of cycles even though most individual items/collections
    don't). Detrends with a 12-month centered moving average, averages the
    residual by calendar month, then centers the 12 factors to mean zero so
    they're a pure seasonal deviation, not a level shift. Per user direction
    (2026-08-17): EN flooring sales have a known seasonal pattern, and without
    this, a shared seasonal dip gets mistaken for a per-collection decline
    happening everywhere at once."""
    total = df.groupby("PERIOD")["SQFT_SOLD"].sum().sort_index()
    log_total = np.log1p(total.clip(lower=0))
    trend = log_total.rolling(window=12, center=True, min_periods=6).mean()
    residual = (log_total - trend).dropna()
    seasonal = residual.groupby(residual.index.month).mean()
    seasonal = seasonal - seasonal.mean()
    return seasonal.to_dict()


def series_features(series: pd.Series, seasonal: dict[int, float] | None = None) -> np.ndarray | None:
    s = series.dropna()
    if len(s) < MIN_MONTHS_FOR_HMM:
        return None
    level = np.log1p(s.clip(lower=0).to_numpy())
    if seasonal:
        level = level - np.array([seasonal.get(m, 0.0) for m in s.index.month])
    slope = np.diff(level, prepend=level[0])
    level_z = (level - level.mean()) / (level.std() + 1e-9)
    slope_z = (slope - slope.mean()) / (slope.std() + 1e-9)
    return np.column_stack([level_z, slope_z])


SMOOTH_WINDOW = 3  # added 2026-08-17 -- a single noisy month (e.g. GII Distressed's
# one down-month inside an otherwise up quarter) shouldn't flip the reported regime
# on its own; per user request, "current regime" is now a majority vote over the
# trailing 3 months of the decoded path, not just the single most recent month.


def fit_regime(series: pd.Series, seasonal: dict[int, float] | None = None) -> dict | None:
    X = series_features(series, seasonal)
    if X is None:
        return None
    model = GaussianHMM(n_states=3, n_iter=150, random_state=0).fit([X])
    path = model.decode(X)

    # label states by mean slope (feature index 1): most negative -> Declining, most positive -> Growing
    order = np.argsort(model.means_[:, 1])
    label_of_state = {state: STATE_LABELS[rank] for rank, state in enumerate(order)}

    tail = path[-SMOOTH_WINDOW:]
    values, counts = np.unique(tail, return_counts=True)
    tied = values[counts == counts.max()]
    current_state = tied[0]
    if len(tied) > 1:
        for s in tail[::-1]:  # break ties toward the most recent month
            if s in tied:
                current_state = s
                break
    current_label = label_of_state[current_state]

    run_length = 0
    for s in path[::-1]:
        if s == current_state:
            run_length += 1
        else:
            break
    run_length = max(run_length, 1)

    persistence = model.transmat_[current_state, current_state]
    return {
        "current_regime": current_label,
        "months_in_regime": run_length,
        "regime_persistence": float(persistence),
        "months_of_history": len(series.dropna()),
        "mean_slope_z_by_state": {label_of_state[s]: float(model.means_[s, 1]) for s in range(3)},
    }


def run_hmm_for_all(pivot: pd.DataFrame, seasonal: dict[int, float] | None = None) -> pd.DataFrame:
    rows = []
    for col in pivot.columns:
        result = fit_regime(pivot[col], seasonal)
        if result is None:
            continue
        result["NAME"] = col
        rows.append(result)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("NAME")


def top_pairs(corr: pd.DataFrame, top_n: int = 25, positive: bool = True) -> pd.DataFrame:
    pairs = []
    cols = corr.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.iloc[i, j]
            if pd.isna(v) or is_companion_pair(cols[i], cols[j]):
                continue
            pairs.append((cols[i], cols[j], v))
    df = pd.DataFrame(pairs, columns=["A", "B", "CORRELATION"])
    if positive:
        return df.sort_values("CORRELATION", ascending=False).head(top_n)
    return df.sort_values("CORRELATION").head(top_n)


def load_similarity(family: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{family.lower()}_similarity_pairs.csv"
    if not path.exists():
        return None
    return pd.read_csv(path)


def build_recommendations(item_regimes: pd.DataFrame, item_corr: pd.DataFrame, similarity: pd.DataFrame | None) -> dict:
    recs: dict[str, pd.DataFrame] = {}

    if not item_regimes.empty:
        recs["phase_out_candidates"] = (
            item_regimes[item_regimes["current_regime"] == "Declining"]
            .sort_values(["months_in_regime", "regime_persistence"], ascending=False)
            .reset_index()
        )
        recs["expansion_candidates"] = (
            item_regimes[item_regimes["current_regime"] == "Growing"]
            .sort_values(["months_in_regime", "regime_persistence"], ascending=False)
            .reset_index()
        )

    if not item_corr.empty:
        pos_pairs = top_pairs(item_corr, top_n=40, positive=True)
        neg_pairs = top_pairs(item_corr, top_n=40, positive=False)

        if similarity is not None and not similarity.empty:
            sim_lookup = {}
            for _, r in similarity.iterrows():
                sim_lookup[frozenset((r["ITEM_A"], r["ITEM_B"]))] = r
            def attach_similarity(df):
                sims, des, same_coll = [], [], []
                for _, r in df.iterrows():
                    match = sim_lookup.get(frozenset((r["A"], r["B"])))
                    sims.append(match["COMBINED_SIMILARITY"] if match is not None else np.nan)
                    des.append(match["DELTA_E"] if match is not None else np.nan)
                    same_coll.append(bool(match["SAME_COLLECTION"]) if match is not None else np.nan)
                df = df.copy()
                df["COMBINED_SIMILARITY"], df["DELTA_E"], df["SAME_COLLECTION"] = sims, des, same_coll
                return df
            pos_pairs = attach_similarity(pos_pairs)
            neg_pairs = attach_similarity(neg_pairs)
            consolidation = pos_pairs[
                (pos_pairs["CORRELATION"] >= 0.7) & (pos_pairs["COMBINED_SIMILARITY"] >= 0.75)
            ].sort_values("COMBINED_SIMILARITY", ascending=False)
            recs["consolidation_review"] = consolidation.reset_index(drop=True)

        recs["highly_correlated_pairs"] = pos_pairs.reset_index(drop=True)
        recs["substitute_effect_pairs"] = neg_pairs.reset_index(drop=True)

    return recs


def photo_priority(df: pd.DataFrame, item_regimes: pd.DataFrame, top_n: int = 25, color_workbook=None) -> pd.DataFrame:
    """Rank sales-active items that have NOT yet been through color analysis, so
    coverage gets prioritized toward items that actually matter for the
    consolidation-review cross-reference (high volume, or in a Growing/Declining
    regime where a similarity read would sharpen the phase-out/expand call)."""
    try:
        color_df = load_color_products(color_workbook) if color_workbook else load_color_products()
        analyzed = set(color_df["ITEM_KEY"])
    except FileNotFoundError:
        analyzed = set()

    volume = df.groupby("ITEM", as_index=False)["SQFT_SOLD"].sum().rename(columns={"SQFT_SOLD": "TOTAL_SQFT_36MO"})
    volume["ITEM_KEY"] = volume["ITEM"].map(norm_item)
    volume["ALREADY_ANALYZED"] = volume["ITEM_KEY"].isin(analyzed)
    volume = volume.merge(
        item_regimes[["current_regime"]] if not item_regimes.empty else pd.DataFrame(columns=["current_regime"]),
        left_on="ITEM", right_index=True, how="left",
    )
    missing = volume[~volume["ALREADY_ANALYZED"]].sort_values("TOTAL_SQFT_36MO", ascending=False)
    return missing.drop(columns=["ITEM_KEY", "ALREADY_ANALYZED"]).head(top_n).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Correlation + HMM catalog analysis for a flooring family.")
    parser.add_argument("--family", default="EN")
    parser.add_argument("--months", type=int, default=36)
    parser.add_argument("--refresh", action="store_true", help="Re-pull from Gartman instead of using cached CSV.")
    parser.add_argument("--color-workbook", type=Path, default=None, help="Override color workbook path (e.g. a pending copy)")
    parser.add_argument("--po-since", type=str, default=DEFAULT_PO_SINCE, help="Only include items with a received PO on/after this date (YYYY-MM-DD). Empty string disables the filter.")
    args = parser.parse_args()

    df = load_or_pull(args.family, args.months, args.refresh, args.po_since or None)
    try:
        color_df_all = load_color_products(args.color_workbook, include_dont_use=True) if args.color_workbook else load_color_products(include_dont_use=True)
        blocked_keys = dont_use_item_keys(color_df_all)
    except FileNotFoundError:
        blocked_keys = set()
    try:
        blocked_keys |= dont_use_item_keys(load_master_specs())
    except FileNotFoundError:
        pass
    if blocked_keys:
        df = df[~df["ITEM"].map(lambda v: norm_item(v) in blocked_keys)].copy()
    print(f"Loaded {len(df)} monthly rows, {df['ITEM'].nunique()} items, {df['COLLECTION'].nunique()} collections.")

    collection_pivot = build_pivot(df, "COLLECTION")
    item_pivot = build_pivot(df, "ITEM")
    seasonal = family_seasonal_index(df)
    print(f"Seasonal index (log-space, by month): { {m: round(v, 3) for m, v in sorted(seasonal.items())} }")

    collection_corr = correlation_matrix(collection_pivot)
    item_corr = correlation_matrix(item_pivot, min_overlap_months=9)

    collection_regimes = run_hmm_for_all(collection_pivot, seasonal)
    item_regimes = run_hmm_for_all(item_pivot, seasonal)

    similarity = load_similarity(args.family)
    recs = build_recommendations(item_regimes, item_corr, similarity)
    recs["photo_priority"] = photo_priority(df, item_regimes, color_workbook=args.color_workbook)

    def safe_to_csv(obj, path: Path, **kwargs) -> None:
        try:
            obj.to_csv(path, **kwargs)
        except PermissionError:
            fallback = path.with_stem(path.stem + "_pending")
            obj.to_csv(fallback, **kwargs)
            print(f"  (locked open, wrote {fallback.name} instead)")

    DATA_DIR.mkdir(exist_ok=True)
    safe_to_csv(collection_corr, DATA_DIR / f"{args.family.lower()}_collection_correlation.csv")
    safe_to_csv(item_corr, DATA_DIR / f"{args.family.lower()}_item_correlation.csv")
    safe_to_csv(collection_regimes, DATA_DIR / f"{args.family.lower()}_collection_regimes.csv")
    safe_to_csv(item_regimes, DATA_DIR / f"{args.family.lower()}_item_regimes.csv")
    for name, rec_df in recs.items():
        safe_to_csv(rec_df, DATA_DIR / f"{args.family.lower()}_{name}.csv", index=False)

    print("\n=== Collection regimes ===")
    print(collection_regimes[["current_regime", "months_in_regime", "regime_persistence"]].to_string())

    print(f"\n=== Item regimes: {len(item_regimes)} items with >= {MIN_MONTHS_FOR_HMM} months of history ===")
    print(item_regimes["current_regime"].value_counts().to_string())

    for name, rec_df in recs.items():
        print(f"\n=== {name} ({len(rec_df)}) ===")
        print(rec_df.head(10).to_string(index=False))

    print(f"\nAll outputs written to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
