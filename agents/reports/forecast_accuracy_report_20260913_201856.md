# Forecast Accuracy Report (20260913_201856)

Trailing 26 weeks of reconciled predictions.

## Trend by product line (mean realized absolute % error, weekly)

- **flooring**: improving (125.5% -> 106.4%)
- **moulding**: worsening (92.1% -> 99.7%)
- **sundries**: improving (71.7% -> 65.1%)

## Best/worst forecast method by realized accuracy (materiality-weighted)

Ranked by wMAPE (aggregate error / aggregate actual -- the same metric flooringwebapp.py's own method selection uses internally), restricted to groups where fewer than half the rows are near-zero-volume (actual_demand < 1.0 units) so a method that's merely easy on trivial demand can't win the ranking. `scoring_method` is the method that actually won cross-validated scoring; `used_fallback_average=True` means its own forecast was discarded in favor of a historical-average fallback, so the error shown reflects that fallback, not the method's own forecast.

- **flooring**: best = `Croston` (19.7% wMAPE, n=658.0, volume=12042959), worst = `SARIMA(0,1,1)` (fallback average used) (103.4% wMAPE, n=7053.0, volume=8169576)
- **moulding**: best = `PROPHET` (30.6% wMAPE, n=1047.0, volume=719517), worst = `RANDOM_FOREST + drift_cap` (238.8% wMAPE, n=135.0, volume=4580)
- **sundries**: best = `TSB` (22.6% wMAPE, n=10438.0, volume=10355340), worst = `SBA + drift_cap` (545.4% wMAPE, n=86.0, volume=264)

### Naive/unweighted best-worst by forecast_method (for contrast, not recommended for decisions)

- **flooring**: best = `Historical Avg (fallback capped)` (8.0% mean APE, n=76596), worst = `XGBoost (Global) (fallback to avg)` (1350.4% mean APE, n=180)
- **moulding**: best = `RECENT_ZERO_FALLBACK` (17.6% mean APE, n=102), worst = `RANDOM_FOREST + drift_cap` (206.9% mean APE, n=135)
- **sundries**: best = `COLD_START_RECENT_ZERO` (6.3% mean APE, n=806), worst = `SAFE_MEAN + drift_cap` (215.8% mean APE, n=1322)

## By demand class

- flooring / ERRATIC: 321.0% mean APE (n=9609)
- flooring / INSUFFICIENT_DATA: 23.9% mean APE (n=11443)
- flooring / INTERMITTENT: 35.0% mean APE (n=12957)
- flooring / LUMPY: 121.6% mean APE (n=116967)
- flooring / SMOOTH: 10.6% mean APE (n=1478)
- moulding / ERRATIC: 62.3% mean APE (n=5122)
- moulding / INTERMITTENT: 128.7% mean APE (n=154)
- moulding / LUMPY: 119.3% mean APE (n=8989)
- moulding / SMOOTH: 29.4% mean APE (n=846)
- sundries / ERRATIC: 76.6% mean APE (n=21850)
- sundries / INTERMITTENT: 61.4% mean APE (n=68394)
- sundries / LUMPY: 91.1% mean APE (n=51181)
- sundries / MOVING_AVG: 67.6% mean APE (n=87932)
- sundries / NO_DEMAND: 8.8% mean APE (n=239)
- sundries / SMOOTH: 51.6% mean APE (n=21364)

Full detail: `forecast_accuracy_details_20260913_201856.csv`