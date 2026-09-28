# Forecast Accuracy Report (20260823_201009)

Trailing 26 weeks of reconciled predictions.

## Trend by product line (mean realized absolute % error, weekly)

- **flooring**: improving (201.2% -> 118.6%)
- **moulding**: improving (117.9% -> 98.8%)
- **sundries**: improving (92.9% -> 69.7%)

## Best/worst forecast method by realized accuracy (materiality-weighted)

Ranked by wMAPE (aggregate error / aggregate actual -- the same metric flooringwebapp.py's own method selection uses internally), restricted to groups where fewer than half the rows are near-zero-volume (actual_demand < 1.0 units) so a method that's merely easy on trivial demand can't win the ranking. `scoring_method` is the method that actually won cross-validated scoring; `used_fallback_average=True` means its own forecast was discarded in favor of a historical-average fallback, so the error shown reflects that fallback, not the method's own forecast.

- **flooring**: best = `Croston` (22.9% wMAPE, n=500.0, volume=9366115), worst = `Holt (Damped) + drift_cap` (193.1% wMAPE, n=31.0, volume=42326)
- **moulding**: best = `PROPHET` (38.0% wMAPE, n=739.0, volume=528469), worst = `RANDOM_FOREST + drift_cap` (141.0% wMAPE, n=86.0, volume=4518)
- **sundries**: best = `CROSTON` (24.9% wMAPE, n=23864.0, volume=4719154), worst = `SBA + drift_cap` (401.9% wMAPE, n=62.0, volume=233)

### Naive/unweighted best-worst by forecast_method (for contrast, not recommended for decisions)

- **flooring**: best = `Historical Avg (fallback capped)` (8.3% mean APE, n=48198), worst = `XGBoost (Global) (fallback to avg)` (844.0% mean APE, n=472)
- **moulding**: best = `RECENT_ZERO_FALLBACK` (14.1% mean APE, n=64), worst = `RANDOM_FOREST + drift_cap` (263.4% mean APE, n=86)
- **sundries**: best = `COLD_START_RECENT_ZERO` (6.2% mean APE, n=658), worst = `SAFE_MEAN + drift_cap` (235.8% mean APE, n=1005)

## By demand class

- flooring / ERRATIC: 442.5% mean APE (n=7473)
- flooring / INSUFFICIENT_DATA: 35.2% mean APE (n=7974)
- flooring / INTERMITTENT: 49.1% mean APE (n=8963)
- flooring / LUMPY: 149.4% mean APE (n=85233)
- flooring / SMOOTH: 20.1% mean APE (n=1146)
- moulding / ERRATIC: 90.8% mean APE (n=4524)
- moulding / INTERMITTENT: 94.5% mean APE (n=134)
- moulding / LUMPY: 121.4% mean APE (n=6294)
- moulding / SMOOTH: 31.9% mean APE (n=713)
- sundries / ERRATIC: 104.5% mean APE (n=25702)
- sundries / INTERMITTENT: 62.4% mean APE (n=59643)
- sundries / LUMPY: 93.3% mean APE (n=44006)
- sundries / MOVING_AVG: 68.7% mean APE (n=38592)
- sundries / NO_DEMAND: 5.9% mean APE (n=204)
- sundries / SMOOTH: 85.0% mean APE (n=26102)

Full detail: `forecast_accuracy_details_20260823_201009.csv`