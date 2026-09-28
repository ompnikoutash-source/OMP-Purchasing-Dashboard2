"""
Compile all catalog_intelligence outputs for one family into a single JSON blob
for the dashboard artifact (self-contained HTML needs the data inlined, not
fetched, so this flattens everything into plain arrays/numbers up front).

Usage:
    .venv\\Scripts\\python.exe catalog_intelligence\\compile_dashboard_data.py --family EN
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from correlation_hmm_analysis import (  # noqa: E402
    build_pivot,
    correlation_matrix,
    family_seasonal_index,
    load_or_pull,
    run_hmm_for_all,
)
from catalog_similarity import (  # noqa: E402
    dont_use_item_keys,
    is_dont_use_description,
    load_family_items,
    load_color_products,
    load_master_specs,
    norm_item,
    normalize_width,
)
from active_items import DEFAULT_PO_SINCE  # noqa: E402
from core.db_connection import connect, sql_escape  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
HMM_LABEL_MAP = {
    "Growing": "Positive",
    "Stable": "Neutral",
    "Declining": "Negative",
}


def read_csv_maybe(path: Path) -> pd.DataFrame:
    pending = path.with_stem(path.stem + "_pending")
    use = pending if pending.exists() and pending.stat().st_mtime > (path.stat().st_mtime if path.exists() else 0) else path
    return pd.read_csv(use) if use.exists() else pd.DataFrame()


def load_current_item_prices(items) -> dict[str, float | None]:
    """Current selling price from Gartman ITEMMAST.IMP1, keyed by normalized item."""
    item_keys = sorted({norm_item(item) for item in items if norm_item(item)})
    if not item_keys:
        return {}
    quoted = ", ".join(f"'{sql_escape(item)}'" for item in item_keys)
    query = f"""
    SELECT
        TRIM(IM.IMITEM) AS ITEM_KEY,
        DECIMAL(COALESCE(IM.IMP1, 0), 18, 5) AS CURRENT_SELL_PRICE
    FROM GSFL2K.ITEMMAST IM
    WHERE TRIM(IM.IMITEM) IN ({quoted})
    """
    conn = connect()
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    if df.empty:
        return {}
    df.columns = [c.upper() for c in df.columns]
    prices: dict[str, float | None] = {}
    for _, row in df.iterrows():
        key = norm_item(row.get("ITEM_KEY"))
        price = row.get("CURRENT_SELL_PRICE")
        if price is None or pd.isna(price) or float(price) <= 0:
            prices[key] = None
        else:
            prices[key] = round(float(price), 2)
    return prices


def trailing_momentum(pivot: pd.DataFrame) -> dict:
    """Trailing square-foot momentum by item.

    The dashboard displays average monthly SF over each trailing window, which
    lets a 1-month, 3-month, 6-month, and 12-month view sit on the same scale.
    Change-versus-prior fields are still kept in the payload for action labels.
    """
    out = {c: {} for c in pivot.columns}

    if len(pivot) >= 6:
        last3 = pivot.iloc[-3:].sum(min_count=1)
        prior3 = pivot.iloc[-6:-3].sum(min_count=1)
        for c in pivot.columns:
            l3 = last3[c]
            p3 = prior3[c]
            out[c]["momentumPct"] = round(float((l3 - p3) / p3 * 100), 1) if l3 is not None and not pd.isna(l3) and p3 is not None and not pd.isna(p3) and p3 else None
    else:
        for c in pivot.columns:
            out[c]["momentumPct"] = None

    for months in (1, 3, 6, 12):
        has_window = len(pivot) >= months * 2
        recent = pivot.iloc[-months:].sum(min_count=1) if has_window else pd.Series(dtype=float)
        prior = pivot.iloc[-months * 2:-months].sum(min_count=1) if has_window else pd.Series(dtype=float)
        for c in pivot.columns:
            current_raw = recent.get(c) if has_window else None
            prior_raw = prior.get(c) if has_window else None
            current_missing = current_raw is None or pd.isna(current_raw)
            prior_missing = prior_raw is None or pd.isna(prior_raw)
            if not has_window or current_missing:
                out[c][f"momentum{months}Sqft"] = None
                out[c][f"momentum{months}AvgSqft"] = None
                out[c][f"momentum{months}PriorAvgSqft"] = None if prior_missing else round(float(prior_raw) / months, 1)
                out[c][f"momentum{months}AvgDeltaSqft"] = None
                out[c][f"momentum{months}RecentSqft"] = None
                out[c][f"momentum{months}PriorSqft"] = None if prior_missing else round(float(prior_raw), 1)
                continue
            current_sf = float(current_raw)
            prior_sf = None if prior_missing else float(prior_raw)
            current_avg = current_sf / months
            prior_avg = None if prior_sf is None else prior_sf / months
            out[c][f"momentum{months}Sqft"] = None if prior_sf is None else round(current_sf - prior_sf, 1)
            out[c][f"momentum{months}AvgSqft"] = round(current_avg, 1)
            out[c][f"momentum{months}PriorAvgSqft"] = None if prior_avg is None else round(prior_avg, 1)
            out[c][f"momentum{months}AvgDeltaSqft"] = None if prior_avg is None else round(current_avg - prior_avg, 1)
            out[c][f"momentum{months}RecentSqft"] = round(current_sf, 1)
            out[c][f"momentum{months}PriorSqft"] = None if prior_sf is None else round(prior_sf, 1)
    return out


def momentum_cutoffs(item_momentum: dict) -> tuple[float, float]:
    """Dynamic 6-month SF thresholds for action labels."""
    values = [
        m.get("momentum6Sqft")
        for m in item_momentum.values()
        if m.get("momentum6Sqft") is not None
    ]
    if not values:
        return -1000.0, 1000.0
    series = pd.Series(values, dtype=float)
    decline = min(float(series.quantile(0.25)), -1000.0)
    growth = max(float(series.quantile(0.75)), 1000.0)
    return decline, growth


def positive_momentum_regime(momentum: dict | None) -> dict:
    """Regime from 10 visible momentum comparisons.

    Six comparisons read current-window acceleration (1mo vs 3/6/12, 3mo vs
    6/12, and 6mo vs 12). Four comparisons read each current trailing average
    against the prior same-length window. All 10 are required; otherwise the
    item is marked Insufficient.
    """
    if not momentum:
        return {"label": "Insufficient", "positive": None, "total": 0}

    def value(key: str):
        v = momentum.get(key)
        return None if v is None or pd.isna(v) else float(v)

    current = {m: value(f"momentum{m}AvgSqft") for m in (1, 3, 6, 12)}
    prior = {m: value(f"momentum{m}PriorAvgSqft") for m in (1, 3, 6, 12)}
    checks = [
        (current[1], current[3]),
        (current[1], current[6]),
        (current[1], current[12]),
        (current[3], current[6]),
        (current[3], current[12]),
        (current[6], current[12]),
        (current[1], prior[1]),
        (current[3], prior[3]),
        (current[6], prior[6]),
        (current[12], prior[12]),
    ]
    available = [(a, b) for a, b in checks if a is not None and b is not None]
    if len(available) < 10:
        return {"label": "Insufficient", "positive": None, "total": len(available)}

    positive = sum(1 for a, b in available if a > b)
    if positive >= 7:
        label = "Growing"
    elif positive >= 4:
        label = "Stable"
    else:
        label = "Declining"
    return {"label": label, "positive": positive, "total": len(available)}


def hmm_dashboard_label(label: str | None) -> str:
    text = clean_text(label)
    if not text:
        return "Insufficient"
    return HMM_LABEL_MAP.get(text, text)


def item_pair_summary(pair_df: pd.DataFrame, col_a: str, col_b: str, value_col: str, higher_is_better: bool = True) -> dict:
    """For a pairs table (too-similar, substitute-effect, ...), returns
    {item: {"count", "partner", "value"}} for every item that appears as
    either side of a row -- how many redundancy flags it has, and the single
    most relevant one to look at first (highest color/spec similarity, or
    most negative correlation for substitute pairs -- `higher_is_better`
    controls which). Lets the item action table show a concrete partner to
    drill into, not just a count."""
    if pair_df.empty:
        return {}
    counts = pd.concat([pair_df[col_a], pair_df[col_b]]).value_counts().to_dict()
    order = pair_df[value_col].sort_values(ascending=not higher_is_better).index
    best_partner: dict = {}
    for idx in order:
        row = pair_df.loc[idx]
        v = row[value_col]
        if pd.isna(v):
            continue
        a, b = row[col_a], row[col_b]
        if a not in best_partner:
            best_partner[a] = (b, float(v))
        if b not in best_partner:
            best_partner[b] = (a, float(v))
    return {item: {"count": int(counts.get(item, 0)), "partner": p, "value": round(v, 3)} for item, (p, v) in best_partner.items()}


def recommend_action(momentum_pct: float | None, similar_count: int, substitute_count: int, photo_gap: int) -> str:
    """Simple, transparent rule for the item action table. Momentum
    (validated against the user's own pivot) is the primary driver, not the
    HMM regime -- shown alongside for context, not folded into this rule."""
    if momentum_pct is None:
        return "Watch — not enough recent history"
    if momentum_pct <= -10 and (similar_count > 0 or substitute_count > 0):
        return "Drop candidate"
    if momentum_pct <= -10:
        return "Phase-out watch"
    if momentum_pct >= 10 and photo_gap > 0:
        return "Expand — needs photo analysis"
    if momentum_pct >= 10:
        return "Expand candidate"
    return "Watch"


def recommend_action_sf(
    momentum: dict | None,
    similar_count: int,
    substitute_count: int,
    photo_gap: int,
    decline_sf: float,
    growth_sf: float,
    momentum_regime: str = "",
    hmm_regime: str = "",
) -> str:
    """Action labels based on six-month square-foot movement."""
    if not momentum or momentum.get("momentum6Sqft") is None:
        return "Watch - not enough recent history"
    m6 = float(momentum["momentum6Sqft"])
    momentum_label = clean_text(momentum_regime)
    hmm_label = clean_text(hmm_regime)
    positive_consensus = momentum_label == "Growing" and hmm_label == "Positive"
    negative_signal = momentum_label == "Declining" or hmm_label == "Negative"
    if m6 <= decline_sf and (similar_count > 0 or substitute_count > 0):
        return "Watch - mixed signals" if positive_consensus else "Drop candidate"
    if m6 <= decline_sf:
        return "Watch - mixed signals" if positive_consensus else ("Phase-out watch" if negative_signal else "Watch")
    if m6 >= growth_sf and photo_gap > 0:
        return "Expand - needs photo analysis"
    if m6 >= growth_sf:
        return "Expand candidate"
    return "Watch"


def clean_text(value, default: str = "") -> str:
    if value is None or pd.isna(value):
        return default
    text = " ".join(str(value).strip().split())
    if not text or text.upper() in {"NONE", "NAN", "NA", "N/A", "??"}:
        return default
    return text


def first_text(*values, default: str = "") -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return default


def clean_num(value, digits: int | None = None):
    if value is None or pd.isna(value):
        return None
    n = float(value)
    return round(n, digits) if digits is not None else n


def clean_int(value):
    if value is None or pd.isna(value):
        return None
    return int(value)


def format_width(value) -> str:
    return normalize_width(value)


def price_tier(avg_price: float | None, good_max: float | None, better_max: float | None) -> str:
    if avg_price is None or avg_price <= 0 or good_max is None or better_max is None:
        return "Unpriced"
    if avg_price <= good_max:
        return "Good"
    if avg_price <= better_max:
        return "Better"
    return "Best"


def pair_key(a: str, b: str) -> tuple[str, str]:
    aa, bb = norm_item(a), norm_item(b)
    return (aa, bb) if aa <= bb else (bb, aa)


def filter_item_refs(d: pd.DataFrame, allowed_keys: set[str], item_cols: list[str]) -> pd.DataFrame:
    if d.empty or not allowed_keys:
        return d
    cols = [c for c in item_cols if c in d.columns]
    if not cols:
        return d
    mask = pd.Series(True, index=d.index)
    for col in cols:
        mask &= d[col].map(lambda v: norm_item(v) in allowed_keys)
    return d[mask].copy()


def same_present_text(a, b) -> bool:
    aa = clean_text(a).upper()
    bb = clean_text(b).upper()
    return bool(aa and bb and aa == bb)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default="EN")
    parser.add_argument("--color-workbook", type=Path, default=None)
    parser.add_argument("--po-since", type=str, default=DEFAULT_PO_SINCE)
    args = parser.parse_args()
    fam = args.family.lower()
    po_since = args.po_since or None
    color_kwargs = {"workbook": args.color_workbook} if args.color_workbook else {}

    color_products_all = load_color_products(include_dont_use=True, **color_kwargs)
    color_placeholder_keys = dont_use_item_keys(color_products_all)
    color_products = color_products_all[~color_products_all["ITEM_KEY"].isin(color_placeholder_keys)].copy()
    master_specs = load_master_specs()
    master_placeholder_keys = dont_use_item_keys(master_specs)
    placeholder_keys = color_placeholder_keys | master_placeholder_keys
    if placeholder_keys:
        color_products = color_products[~color_products["ITEM_KEY"].isin(placeholder_keys)].copy()
        master_specs = master_specs[~master_specs["ITEM_KEY"].isin(placeholder_keys)].copy()

    df = load_or_pull(args.family, 36, refresh=False, po_since=po_since)
    if "DESCRIPTION" in df.columns:
        df = df[~df["DESCRIPTION"].map(is_dont_use_description)].copy()
    if placeholder_keys:
        df = df[~df["ITEM"].map(lambda v: norm_item(v) in placeholder_keys)].copy()
    item_pivot = build_pivot(df, "ITEM")
    current_price_by_key = load_current_item_prices(item_pivot.columns)

    consolidation = read_csv_maybe(DATA_DIR / f"{fam}_consolidation_review.csv")
    highly_corr = read_csv_maybe(DATA_DIR / f"{fam}_highly_correlated_pairs.csv")
    substitute = read_csv_maybe(DATA_DIR / f"{fam}_substitute_effect_pairs.csv")
    too_similar = read_csv_maybe(DATA_DIR / f"{fam}_too_similar_flagged.csv")
    needs_photo = read_csv_maybe(DATA_DIR / f"{fam}_needs_color_photo_review.csv")
    photo_priority = read_csv_maybe(DATA_DIR / f"{fam}_photo_priority.csv")
    similarity_pairs = read_csv_maybe(DATA_DIR / f"{fam}_similarity_pairs.csv")

    def df_records(d: pd.DataFrame, cols: list[str], limit: int | None = None) -> list:
        if d.empty:
            return []
        sub = d[[c for c in cols if c in d.columns]].copy()
        if limit:
            sub = sub.head(limit)
        return json.loads(sub.to_json(orient="records"))

    total_family_items = int(df["ITEM"].nunique())
    full_family = load_family_items(args.family, po_since)
    if placeholder_keys:
        full_family = full_family[~full_family["ITEM_KEY"].isin(placeholder_keys)].copy()
    allowed_keys = set(full_family["ITEM_KEY"]) | {norm_item(item) for item in item_pivot.columns}
    consolidation = filter_item_refs(consolidation, allowed_keys, ["A", "B"])
    highly_corr = filter_item_refs(highly_corr, allowed_keys, ["A", "B"])
    substitute = filter_item_refs(substitute, allowed_keys, ["A", "B"])
    too_similar = filter_item_refs(too_similar, allowed_keys, ["ITEM_A", "ITEM_B"])
    needs_photo = filter_item_refs(needs_photo, allowed_keys, ["ITEM_A", "ITEM_B"])
    photo_priority = filter_item_refs(photo_priority, allowed_keys, ["ITEM"])
    similarity_pairs = filter_item_refs(similarity_pairs, allowed_keys, ["ITEM_A", "ITEM_B"])
    analyzed_keys = set(color_products["ITEM_KEY"])
    total_family_all = len(full_family)
    total_analyzed_in_family = full_family["ITEM_KEY"].isin(analyzed_keys).sum()

    item_collection = dict(zip(full_family["ITEM_KEY"], full_family["COLLECTION"]))
    item_momentum = trailing_momentum(item_pivot)
    momentum_regimes = {item: positive_momentum_regime(item_momentum.get(item, {})) for item in item_pivot.columns}
    regime_counts = pd.Series([r["label"] for r in momentum_regimes.values()]).value_counts().to_dict() if momentum_regimes else {}
    seasonal = family_seasonal_index(df)
    item_hmm_regimes = run_hmm_for_all(item_pivot, seasonal)

    def hmm_record(item: str) -> dict:
        if item_hmm_regimes.empty or item not in item_hmm_regimes.index:
            return {
                "hmmRegime": "Insufficient",
                "hmmSourceRegime": None,
                "hmmMonthsInRegime": None,
                "hmmPersistence": None,
                "hmmMonthsHistory": None,
            }
        row = item_hmm_regimes.loc[item]
        source = clean_text(row.get("current_regime"))
        return {
            "hmmRegime": hmm_dashboard_label(source),
            "hmmSourceRegime": source or None,
            "hmmMonthsInRegime": clean_int(row.get("months_in_regime")),
            "hmmPersistence": clean_num(row.get("regime_persistence"), 3),
            "hmmMonthsHistory": clean_int(row.get("months_of_history")),
        }

    hmm_by_item = {item: hmm_record(item) for item in item_pivot.columns}
    hmm_counts = pd.Series([r["hmmRegime"] for r in hmm_by_item.values()]).value_counts().to_dict() if hmm_by_item else {}
    decline_sf, growth_sf = momentum_cutoffs(item_momentum)
    similar_summary = item_pair_summary(too_similar, "ITEM_A", "ITEM_B", "COMBINED_SIMILARITY", higher_is_better=True)
    substitute_summary = item_pair_summary(substitute, "A", "B", "CORRELATION", higher_is_better=False)

    sales_summary = (
        df.groupby("ITEM", as_index=False)
        .agg({
            "SQFT_SOLD": "sum",
            "SALES_DOLLARS": "sum",
            "DESCRIPTION": "first",
            "COLOR": "first",
            "COLLECTION": "first",
        })
    )
    sales_summary["AVG_SELL_PRICE"] = sales_summary.apply(
        lambda r: float(r["SALES_DOLLARS"]) / float(r["SQFT_SOLD"]) if float(r["SQFT_SOLD"] or 0) else None,
        axis=1,
    )
    prices = pd.Series([p for p in current_price_by_key.values() if p is not None], dtype=float)
    good_max = float(prices.quantile(1 / 3)) if len(prices) else None
    better_max = float(prices.quantile(2 / 3)) if len(prices) else None

    sales_by_item = {clean_text(r["ITEM"]): r for _, r in sales_summary.iterrows()}
    family_by_key = full_family.set_index("ITEM_KEY").to_dict("index") if not full_family.empty else {}
    color_by_key = color_products.set_index("ITEM_KEY").to_dict("index") if not color_products.empty else {}
    master_by_key = master_specs.set_index("ITEM_KEY").to_dict("index") if not master_specs.empty else {}

    def catalog_item_record(item: str) -> dict:
        key = norm_item(item)
        sales = sales_by_item.get(item, {})
        family_row = family_by_key.get(key, {})
        color_row = color_by_key.get(key, {})
        master_row = master_by_key.get(key, {})
        current_price = clean_num(current_price_by_key.get(key), 2)
        realized_avg_price = clean_num(sales.get("AVG_SELL_PRICE"), 2)
        collection = first_text(
            family_row.get("COLLECTION"),
            sales.get("COLLECTION"),
            color_row.get("Collection"),
            master_row.get("MASTER_COLLECTION"),
        )
        color_bucket = first_text(
            color_row.get("BucketName"),
            master_row.get("catalog_color"),
            color_row.get("Product"),
            sales.get("COLOR"),
        )
        product_name = first_text(
            color_row.get("Product"),
            master_row.get("catalog_color"),
            family_row.get("DESCRIPTION"),
            sales.get("DESCRIPTION"),
            item,
        )
        return {
            "item": item,
            "itemKey": key,
            "description": first_text(family_row.get("DESCRIPTION"), sales.get("DESCRIPTION")),
            "collection": collection,
            "productName": product_name,
            "hueBucket": clean_text(color_row.get("HueBucket")),
            "hasHueBucket": bool(clean_text(color_row.get("BucketName"))),
            "species": first_text(master_row.get("species"), color_row.get("Species")),
            "width": first_text(format_width(master_row.get("width")), format_width(color_row.get("Width_in"))),
            "thickness": first_text(master_row.get("thickness"), color_row.get("Thickness")),
            "wearLayer": first_text(master_row.get("wear_layer"), color_row.get("TopLayer")),
            "lengths": first_text(master_row.get("lengths"), color_row.get("Length")),
            "cut": first_text(master_row.get("cut"), color_row.get("SurfaceTreatment")),
            "bevel": first_text(master_row.get("edge_detail"), color_row.get("EdgeDetail")),
            "colorBucket": color_bucket,
            "colorFamily": clean_text(color_row.get("HueFamily")),
            "colorHex": clean_text(color_row.get("MeanHex")),
            "grade": first_text(master_row.get("grade"), color_row.get("Grade")),
            "finish": first_text(master_row.get("finish"), color_row.get("Finish")),
            "texture": first_text(master_row.get("texture"), color_row.get("SurfaceTreatment")),
            "currentSellPrice": current_price,
            "avgSellPrice": current_price,
            "realizedAvgSellPrice": realized_avg_price,
            "priceSource": "GSFL2K.ITEMMAST.IMP1",
            "priceTier": price_tier(current_price, good_max, better_max),
            "sqft36mo": clean_num(sales.get("SQFT_SOLD"), 1),
            "salesDollars36mo": clean_num(sales.get("SALES_DOLLARS"), 2),
            "photoAnalyzed": bool(key in analyzed_keys),
        }

    catalog_items_by_item = {item: catalog_item_record(item) for item in item_pivot.columns}

    item_actions = []
    for item in item_pivot.columns:
        key = norm_item(item)
        m = item_momentum.get(item, {})
        sim = similar_summary.get(item)
        sub = substitute_summary.get(item)
        sim_count = sim["count"] if sim else 0
        sub_count = sub["count"] if sub else 0
        photo_analyzed = key in analyzed_keys
        regime_score = momentum_regimes.get(item, {"label": "Insufficient", "positive": None, "total": 0})
        hmm_signal = hmm_by_item.get(item, {"hmmRegime": "Insufficient"})
        item_actions.append({
            "item": item,
            "collection": item_collection.get(key, ""),
            "momentumPct": m.get("momentumPct"),
            "momentum1Sqft": m.get("momentum1Sqft"),
            "momentum1AvgSqft": m.get("momentum1AvgSqft"),
            "momentum1PriorAvgSqft": m.get("momentum1PriorAvgSqft"),
            "momentum1AvgDeltaSqft": m.get("momentum1AvgDeltaSqft"),
            "momentum1RecentSqft": m.get("momentum1RecentSqft"),
            "momentum1PriorSqft": m.get("momentum1PriorSqft"),
            "momentum3Sqft": m.get("momentum3Sqft"),
            "momentum3AvgSqft": m.get("momentum3AvgSqft"),
            "momentum3PriorAvgSqft": m.get("momentum3PriorAvgSqft"),
            "momentum3AvgDeltaSqft": m.get("momentum3AvgDeltaSqft"),
            "momentum3RecentSqft": m.get("momentum3RecentSqft"),
            "momentum3PriorSqft": m.get("momentum3PriorSqft"),
            "momentum6Sqft": m.get("momentum6Sqft"),
            "momentum6AvgSqft": m.get("momentum6AvgSqft"),
            "momentum6PriorAvgSqft": m.get("momentum6PriorAvgSqft"),
            "momentum6AvgDeltaSqft": m.get("momentum6AvgDeltaSqft"),
            "momentum6RecentSqft": m.get("momentum6RecentSqft"),
            "momentum6PriorSqft": m.get("momentum6PriorSqft"),
            "momentum12Sqft": m.get("momentum12Sqft"),
            "momentum12AvgSqft": m.get("momentum12AvgSqft"),
            "momentum12PriorAvgSqft": m.get("momentum12PriorAvgSqft"),
            "momentum12AvgDeltaSqft": m.get("momentum12AvgDeltaSqft"),
            "momentum12RecentSqft": m.get("momentum12RecentSqft"),
            "momentum12PriorSqft": m.get("momentum12PriorSqft"),
            "regime": regime_score["label"],
            "regimePositiveComparisons": regime_score["positive"],
            "regimeTotalComparisons": regime_score["total"],
            "hmmRegime": hmm_signal.get("hmmRegime"),
            "hmmSourceRegime": hmm_signal.get("hmmSourceRegime"),
            "hmmMonthsInRegime": hmm_signal.get("hmmMonthsInRegime"),
            "hmmPersistence": hmm_signal.get("hmmPersistence"),
            "hmmMonthsHistory": hmm_signal.get("hmmMonthsHistory"),
            "similarPartner": sim["partner"] if sim else None,
            "similarValue": sim["value"] if sim else None,
            "similarCount": sim_count,
            "substitutePartner": sub["partner"] if sub else None,
            "substituteValue": sub["value"] if sub else None,
            "substituteCount": sub_count,
            "photoAnalyzed": bool(photo_analyzed),
            "action": recommend_action_sf(
                m,
                sim_count,
                sub_count,
                0 if photo_analyzed else 1,
                decline_sf,
                growth_sf,
                regime_score["label"],
                hmm_signal.get("hmmRegime"),
            ),
        })
        item_actions[-1].update(catalog_items_by_item.get(item, {}))
    item_actions.sort(key=lambda r: (r["momentum6Sqft"] is None, r["momentum6Sqft"] if r["momentum6Sqft"] is not None else 0))

    sim_lookup = {}
    if not similarity_pairs.empty:
        for _, row in similarity_pairs.iterrows():
            sim_lookup[pair_key(row["ITEM_A"], row["ITEM_B"])] = row

    item_corr = correlation_matrix(item_pivot)
    interference_pairs = []
    cannibalization_candidates = []
    items = list(item_pivot.columns)
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            sim_row = sim_lookup.get(pair_key(a, b))
            corr = item_corr.loc[a, b] if a in item_corr.index and b in item_corr.columns else None
            if (corr is None or pd.isna(corr)) and sim_row is None:
                continue
            similarity = sim_row.get("COMBINED_SIMILARITY") if sim_row is not None else None
            abs_corr = abs(float(corr)) if corr is not None and not pd.isna(corr) else None
            sim_float = float(similarity) if similarity is not None and not pd.isna(similarity) else None
            pair_score = None
            if abs_corr is not None or sim_float is not None:
                pair_score = (abs_corr if abs_corr is not None else 0.25) * (sim_float if sim_float is not None else 0.35)
            interference_pairs.append({
                "a": a,
                "b": b,
                "correlation": clean_num(corr, 3),
                "absCorrelation": clean_num(abs_corr, 3),
                "similarity": clean_num(sim_float, 3),
                "interferenceScore": clean_num(pair_score, 3),
                "deltaE": clean_num(sim_row.get("DELTA_E") if sim_row is not None else None, 2),
                "specMatch": clean_num(sim_row.get("SPEC_MATCH_SCORE") if sim_row is not None else None, 3),
                "hasColorComparison": bool(sim_row.get("HAS_COLOR_COMPARISON")) if sim_row is not None and "HAS_COLOR_COMPARISON" in sim_row else False,
                "sameCollection": bool(sim_row.get("SAME_COLLECTION")) if sim_row is not None and "SAME_COLLECTION" in sim_row else False,
            })
            rec_a = catalog_items_by_item.get(a, {})
            rec_b = catalog_items_by_item.get(b, {})
            price_a = rec_a.get("currentSellPrice")
            price_b = rec_b.get("currentSellPrice")
            price_diff = abs(float(price_a) - float(price_b)) if price_a is not None and price_b is not None else None
            if (
                corr is not None and not pd.isna(corr) and float(corr) < -0.20
                and rec_a.get("hasHueBucket") and rec_b.get("hasHueBucket")
                and same_present_text(rec_a.get("colorBucket"), rec_b.get("colorBucket"))
                and same_present_text(rec_a.get("species"), rec_b.get("species"))
                and price_diff is not None and price_diff <= 0.50
            ):
                cannibalization_candidates.append({
                    "ITEM_A": a,
                    "ITEM_B": b,
                    "DESC_A": clean_text(rec_a.get("description")),
                    "DESC_B": clean_text(rec_b.get("description")),
                    "COLLECTION_A": clean_text(rec_a.get("collection")),
                    "COLLECTION_B": clean_text(rec_b.get("collection")),
                    "COLOR_BUCKET": clean_text(rec_a.get("colorBucket")),
                    "SPECIES": clean_text(rec_a.get("species")),
                    "PRICE_A": clean_num(price_a, 2),
                    "PRICE_B": clean_num(price_b, 2),
                    "PRICE_DIFF": clean_num(price_diff, 2),
                    "CORRELATION": clean_num(corr, 3),
                    "COMBINED_SIMILARITY": clean_num(sim_float, 3),
                    "DELTA_E": clean_num(sim_row.get("DELTA_E") if sim_row is not None else None, 2),
                    "SPEC_MATCH_SCORE": clean_num(sim_row.get("SPEC_MATCH_SCORE") if sim_row is not None else None, 3),
                })

    cannibalization_candidates.sort(key=lambda r: (r["CORRELATION"], r["PRICE_DIFF"], r["ITEM_A"], r["ITEM_B"]))

    catalog_items = list(catalog_items_by_item.values())
    spec_keys = ["species", "width", "thickness", "wearLayer", "lengths", "cut", "bevel", "colorBucket"]
    spec_coverage = sum(1 for r in catalog_items if any(clean_text(r.get(k)) for k in spec_keys))
    photo_priority_display = photo_priority.copy()
    if not photo_priority_display.empty and "current_regime" in photo_priority_display.columns:
        photo_priority_display["current_regime"] = photo_priority_display["current_regime"].map(hmm_dashboard_label)

    payload = {
        "family": args.family.upper(),
        "generatedNote": f"{args.family.upper()} family, 36 months of Gartman sales history, momentum regimes scored from 10 comparisons; HMM signal retained as model context"
            + (f" — active items only (PO received on/after {po_since})" if po_since else " — all items, no PO-activity filter"),
        "kpi": {
            "totalItemsWithSales": total_family_items,
            "growing": int(regime_counts.get("Growing", 0)),
            "stable": int(regime_counts.get("Stable", 0)),
            "declining": int(regime_counts.get("Declining", 0)),
            "insufficient": int(regime_counts.get("Insufficient", 0)),
            "hmmPositive": int(hmm_counts.get("Positive", 0)),
            "hmmNeutral": int(hmm_counts.get("Neutral", 0)),
            "hmmNegative": int(hmm_counts.get("Negative", 0)),
            "hmmInsufficient": int(hmm_counts.get("Insufficient", 0)),
            "tooSimilarFlagged": len(cannibalization_candidates),
            "needsPhotoReview": len(needs_photo),
            "totalFamilyItems": int(total_family_all),
            "colorAnalyzedInFamily": int(total_analyzed_in_family),
            "catalogItemsWithSpecs": int(spec_coverage),
            "priceTieredItems": int(sum(1 for r in catalog_items if r["priceTier"] != "Unpriced")),
        },
        "priceTierThresholds": {
            "goodMax": clean_num(good_max, 2),
            "betterMax": clean_num(better_max, 2),
            "basis": "current GSFL2K.ITEMMAST.IMP1 selling price per square foot",
        },
        "momentumBasis": {
            "unit": "square feet",
            "windows": "Average monthly square feet inside each trailing window",
            "actionWindow": "6-month SF change vs prior 6 months",
            "declineThresholdSqft": clean_num(decline_sf, 1),
            "growthThresholdSqft": clean_num(growth_sf, 1),
        },
        "regimeBasis": {
            "method": "10 positive momentum comparisons",
            "growingMinPositive": 7,
            "stableMinPositive": 4,
            "stableMaxPositive": 6,
            "decliningMaxPositive": 3,
            "requiredComparisons": 10,
        },
        "hmmBasis": {
            "method": "3-state Gaussian HMM over seasonality-adjusted log sales level and slope",
            "positiveLabel": "Former HMM Growing state",
            "neutralLabel": "Former HMM Stable state",
            "negativeLabel": "Former HMM Declining state",
            "minimumMonths": 12,
            "smoothing": "Current label is a 3-month majority vote across the decoded HMM path",
        },
        "catalogItems": catalog_items,
        "interferencePairs": interference_pairs,
        "itemActions": item_actions,
        "topCorrelatedPairs": df_records(highly_corr, ["A", "B", "CORRELATION", "COMBINED_SIMILARITY", "SAME_COLLECTION"], 15),
        "topSubstitutePairs": df_records(substitute, ["A", "B", "CORRELATION", "COMBINED_SIMILARITY", "SAME_COLLECTION"], 15),
        "consolidationReview": df_records(consolidation, ["A", "B", "CORRELATION", "COMBINED_SIMILARITY", "DELTA_E", "SAME_COLLECTION"]),
        "tooSimilar": cannibalization_candidates,
        "photoPriority": df_records(photo_priority_display, ["ITEM", "TOTAL_SQFT_36MO", "current_regime"]),
        "needsPhotoReviewTop": df_records(needs_photo, ["ITEM_A", "ITEM_B", "COLLECTION_A", "SPEC_MATCH_SCORE"], 20),
    }

    out = DATA_DIR / f"{fam}_dashboard_data.json"
    out.write_text(json.dumps(payload, indent=None, allow_nan=False), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
