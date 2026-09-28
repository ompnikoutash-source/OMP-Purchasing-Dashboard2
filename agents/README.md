# Forecast Audit Agent

This package adds two local agents:

- `forecast_audit_agent.py`: rule-based "does this make sense?" audit over JSON outputs.
- `experiment_runner.py`: runs policy-toggle experiments on random SKU samples and scores outcomes.

## Commands

Audit all SKUs:

```bash
python -m agents.forecast_audit_agent --base-dir .
```

Audit sample (50 per dataset):

```bash
python -m agents.forecast_audit_agent --base-dir . --sample-size 50 --seed 20260223

CI gate (fail if any `critical` findings exist):

```bash
python -m agents.forecast_audit_agent --base-dir . --fail-on-severity critical
```
```

Run experiments (baseline + candidate toggles):

```bash
python -m agents.experiment_runner --base-dir . --sample-size 50 --seed 20260223
```

## Config files

- `agents/rules.yaml`: issue rules, thresholds, severities.
- `agents/fix_map.yaml`: rule -> likely code locations + recommendation + experiment presets.

These files are JSON-compatible YAML so they work without adding PyYAML.

## Outputs

Written to `agents/reports/`:

- `audit_details_*.csv`
- `audit_summary_*.csv`
- `audit_report_*.md`
- `audit_findings_*.json`
- `experiment_details_*.csv`
- `experiment_summary_*.csv`

## CI integration

A workflow is included at `.github/workflows/forecast-audit-gate.yml`.
It runs the audit automatically and fails if critical findings are detected.
