# Training report

- **Version**: `20261004T130842Z`
- **Trained at**: 2026-10-04T13:08:42.763934+00:00
- **Interval**: ONE_DAY  (enforced at predict time)
- **Feature set**: v1, 81 features
- **Estimator**: sklearn-hgb
- **Universe**: 447 symbols
- **Date range**: 2024-09-02 to 2026-10-02
- **Git**: `0d187b0`

## Feature block coverage

| Block | Populated | Null rate |
|---|---|---|
| technical | yes | 0.0349 |
| cross_sectional | yes | 0.0422 |
| market_context | yes | 0.0142 |
| flow | yes | 0.0076 |
| fundamental | NO | 1.0 |
| sentiment | NO | 1.0 |

> **fundamental, sentiment** had no values in this training window. These blocks are forward-only: point-in-time fundamentals and scored news accumulate from the day capture began, and cannot be reconstructed for historical dates without look-ahead bias. Retrain once the archive is deep enough. This model does not use them.

## Walk-forward results

### 1day

- rows: 51766, folds: 5, test rows: 33651
- positive rate: 0.3873, barrier hit rate: 0.0
- **AUC 0.5027** (sd 0.016)
- IC -0.0195, precision@20% 0.4102
- Brier 0.2597, net mean return -0.0031, net hit rate 0.3851
- calibrated: True

### 1week

- rows: 50865, folds: 5, test rows: 33296
- positive rate: 0.4376, barrier hit rate: 0.1897
- **AUC 0.5057** (sd 0.0536)
- IC 0.0024, precision@20% 0.4205
- Brier 0.2638, net mean return -0.0046, net hit rate 0.4208
- calibrated: True

### 1month

- rows: 47888, folds: 5, test rows: 32853
- positive rate: 0.4606, barrier hit rate: 0.2448
- **AUC 0.5512** (sd 0.0344)
- IC 0.093, precision@20% 0.572
- Brier 0.2725, net mean return 0.027, net hit rate 0.572
- calibrated: True

## How to read this

AUC is measured with purged, embargoed walk-forward folds — training rows whose
label window reaches into a test block are removed, and a gap is left after it.
An AUC near 0.50 means no signal was found in this window; 0.53-0.58 is a real,
usable result for daily Indian equities. A number far above that is almost always
a leak rather than an edge, and should be investigated as a bug.

The fold-level spread matters as much as the mean: a mean of 0.55 made of 0.49
and 0.61 is a much weaker claim than one made of 0.54 and 0.56.