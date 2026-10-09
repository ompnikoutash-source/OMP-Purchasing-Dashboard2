# Forecast Accuracy Report (20261006_082757)

Trailing 26 weeks of reconciled predictions.

## Trend by product line (mean realized absolute % error, weekly)

- **flooring**: improving (110.6% -> 91.4%)
- **moulding**: improving (91.1% -> 78.6%)
- **sundries**: roughly flat (73.1% -> 73.7%)

## Best/worst forecast method by realized accuracy (materiality-weighted)

Ranked by wMAPE (aggregate error / aggregate actual -- the same metric flooringwebapp.py's own method selection uses internally), restricted to groups where fewer than half the rows are near-zero-volume (actual_demand < 1.0 units) so a method that's merely easy on trivial demand can't win the ranking. `scoring_method` is the method that actually won cross-validated scoring; `used_fallback_average=True` means its own forecast was discarded in favor of a historical-average fallback, so the error shown reflects that fallback, not the method's own forecast.

- **flooring**: best = `Croston` (18.7% wMAPE, n=826.0, volume=14002602), worst = `Croston + drift_cap` (475.0% wMAPE, n=2.0, volume=-640)
- **moulding**: best = `PROPHET` (26.8% wMAPE, n=1268.0, volume=719892), worst = `RANDOM_FOREST + drift_cap` (383.4% wMAPE, n=117.0, volume=3636)
- **sundries**: best = `TSB` (21.1% wMAPE, n=8205.0, volume=10548297), worst = `SBA + drift_cap` (603.2% wMAPE, n=104.0, volume=316)

### Naive/unweighted best-worst by forecast_method (for contrast, not recommended for decisions)

- **flooring**: best = `Historical Avg (fallback capped)` (7.9% mean APE, n=89514), worst = `Croston (fallback to avg)` (557.4% mean APE, n=814)
- **moulding**: best = `RECENT_ZERO_FALLBACK` (21.5% mean APE, n=149), worst = `RANDOM_FOREST + drift_cap` (231.8% mean APE, n=117)
- **sundries**: best = `COLD_START_RECENT_ZERO` (10.5% mean APE, n=629), worst = `SBA + drift_cap` (508.5% mean APE, n=104)

## By demand class

- flooring / ERRATIC: 180.7% mean APE (n=10882)
- flooring / INSUFFICIENT_DATA: 20.6% mean APE (n=13198)
- flooring / INTERMITTENT: 54.7% mean APE (n=15285)
- flooring / LUMPY: 110.7% mean APE (n=136000)
- flooring / SMOOTH: 25.1% mean APE (n=1691)
- moulding / ERRATIC: 58.4% mean APE (n=6082)
- moulding / INTERMITTENT: 118.0% mean APE (n=116)
- moulding / LUMPY: 107.8% mean APE (n=10559)
- moulding / SMOOTH: 27.2% mean APE (n=909)
- sundries / ERRATIC: 81.6% mean APE (n=16151)
- sundries / INTERMITTENT: 61.9% mean APE (n=51615)
- sundries / LUMPY: 89.2% mean APE (n=40769)
- sundries / MOVING_AVG: 73.6% mean APE (n=144079)
- sundries / NO_DEMAND: 17.4% mean APE (n=155)
- sundries / SMOOTH: 54.4% mean APE (n=15302)

Full detail: `forecast_accuracy_details_20261006_082757.csv`