# Forecast Accuracy Report (20260819_083116)

Trailing 26 weeks of reconciled predictions.

## Trend by product line (mean realized absolute % error, weekly)

- **flooring**: improving (157.2% -> 141.3%)
- **moulding**: worsening (122.5% -> 182.9%)
- **sundries**: worsening (83.3% -> 97.1%)

## Best/worst forecast method by realized accuracy

- **flooring**: best = `Historical Avg (fallback capped)` (7.7% mean APE, n=78702), worst = `XGBoost (Global) (fallback to avg)` (868.5% mean APE, n=414)
- **moulding**: best = `RECENT_ZERO_FALLBACK` (8.8% mean APE, n=102), worst = `XGBOOST` (300.3% mean APE, n=632)
- **sundries**: best = `COLD_START_RECENT_ZERO` (5.4% mean APE, n=884), worst = `PROPHET + drift_cap` (237.2% mean APE, n=33)

## By demand class

- flooring / ERRATIC: 388.6% mean APE (n=10953)
- flooring / INSUFFICIENT_DATA: 30.0% mean APE (n=12643)
- flooring / INTERMITTENT: 41.2% mean APE (n=14265)
- flooring / LUMPY: 155.7% mean APE (n=131373)
- flooring / SMOOTH: 38.0% mean APE (n=1682)
- moulding / ERRATIC: 100.3% mean APE (n=6070)
- moulding / INTERMITTENT: 129.5% mean APE (n=178)
- moulding / LUMPY: 182.3% mean APE (n=10045)
- moulding / SMOOTH: 52.3% mean APE (n=1002)
- sundries / ERRATIC: 106.9% mean APE (n=27862)
- sundries / INTERMITTENT: 64.6% mean APE (n=78750)
- sundries / LUMPY: 97.9% mean APE (n=58513)
- sundries / MOVING_AVG: 100.2% mean APE (n=63927)
- sundries / NO_DEMAND: 6.7% mean APE (n=269)
- sundries / SMOOTH: 82.6% mean APE (n=27712)

Full detail: `forecast_accuracy_details_20260819_083116.csv`