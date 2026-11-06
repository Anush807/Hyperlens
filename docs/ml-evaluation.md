# ML Evaluation

Generated 2026-10-19 13:32 UTC by `python -m ml.evaluate`. Regenerate after every retrain.

## 1. Anomaly detection (Isolation Forest)

| Metric | Value |
|---|---|
| Windows scored | 61200 |
| Windows flagged | 80 |
| Merged incidents | 13 |
| Score threshold | -0.084791 |
| Scored period | 2026-09-19T14:00 → 2026-10-19T13:00 |
| Anomalous windows (ground truth) | 84 |
| True positives | 80 |
| False positives | 0 |
| False negatives | 4 |
| **Precision** | **1.0** |
| **Recall** | **0.9524** |
| **F1** | **0.9756** |

Window level, as spec 9.2 defines it: each hourly window is scored against whether it falls inside an injected anomaly.

Ablation - the detector is an Isolation Forest *and* a minimum-deviation gate, so here is what each contributes:

| Decision rule | Flagged | Precision | Recall | F1 |
|---|---|---|---|---|
| Isolation Forest alone | 496 | 0.1613 | 0.9524 | 0.2759 |
| Deviation gate alone | 81 | 1.0 | 0.9643 | 0.9818 |
| Both (shipped) | 80 | 1.0 | 0.9524 | 0.9756 |

Read that honestly: on **this synthetic data** the gate is doing the discriminating and the forest adds nothing, because normal synthetic variation is well-behaved Gaussian noise that never deviates ten robust standard deviations from its own same-hour baseline. The forest stays in the pipeline because real metrics are not that tidy - it is what catches a multivariate pattern that is unremarkable on every axis taken alone - but the headline F1 above should not be read as evidence that it works.

Incident level, after merging consecutive flagged hours - this is what alert volume feels like:

| Metric | Value |
|---|---|
| Injected incidents in period | 20 |
| Incidents raised | 13 |
| Injected incidents matched | 14 |
| Incident precision | 1.0 |
| Incident recall | 0.7 |

✅ F1 0.9756 meets the 0.70 acceptance bar from spec 14, Phase 5.

## 2. Forecasting (LSTM vs seasonal naive)

Horizon: 30 days. Lookback: 28 days. Test window: the final 30 days, never seen in training.

| Model | MAE | sMAPE |
|---|---|---|
| LSTM | 1.3965 | 12.116% |
| Seasonal naive | 0.8876 | 2.068% |

⚠️ **The LSTM does not beat the seasonal-naive baseline overall.** Per spec 16 this is an acceptable outcome and is reported rather than hidden: the naive forecast is the one to trust until more history or a smaller model closes the gap. Both are stored and both are rendered in the capacity screens.

### By synthetic profile

| Profile | LSTM MAE | Naive MAE | LSTM sMAPE | Naive sMAPE | Winner |
|---|---|---|---|---|---|
| anomalous | 3.6271 | 2.9973 | 7.189% | 4.8532% | naive |
| diurnal | 0.8175 | 0.6826 | 26.7931% | 1.5204% | naive |
| growing | 10.5305 | 4.6405 | 15.3779% | 6.802% | naive |
| hot | 0.3921 | 0.4192 | 11.5274% | 0.446% | LSTM |
| idle | 0.056 | 0.0808 | 1.7149% | 2.4736% | LSTM |
| overprovisioned | 0.2138 | 0.2975 | 1.3127% | 1.812% | LSTM |
| steady | 0.2947 | 0.4164 | 12.5611% | 1.1262% | LSTM |

#### Reading this table

The LSTM wins where the series is near-stationary and loses where it is not, which is the opposite of what one would hope for from a sequence model. Two things explain most of it, and neither is flattering:

- **Seasonal naive is close to the generating process here.** These series are a weekly sine plus small Gaussian noise, so repeating last week is very nearly optimal. Its sMAPE of ~1-2% on the stationary profiles is the ceiling, not a baseline to clear. A real estate's irregularity is where a learned model would earn its place.
- **`growing` is the hardest case and the LSTM's worst.** The synthetic trend is a 180-day sawtooth - at ~0.28%/day it cannot climb much longer before saturating - so the training window contains one discontinuity whose position moves with the calendar. That is a property of the synthetic data, not of the model, and it makes this profile's number noisier across runs than the others.

What this table is *for* is the decision in the next line: the model the UI leads with is whichever one actually won, read from these measurements at runtime rather than assumed.

## 3. Assistant tool selection

Twenty questions from `services/api/tests/assistant_eval.yaml`, each with the tool call it should make. Two things are scored separately: whether the model reached for the right tool, and whether its arguments were right given that tool. An argument the model omits counts as correct when the tool's own default already matches - the question is whether the call does the right thing, not whether the model restated a default.

### `qwen2.5-3b-instruct`

- Cases: 20
- **Tool-selection accuracy: 100%**
- **Argument accuracy (given the right tool): 95%**
- Argument accuracy overall: 95%
- Wall clock: 24.6s for 20 questions

✅ Meets the 80% tool-selection bar from spec 14, Phase 6.

Where it got it wrong:

| Question | Expected | Called | Problem |
|---|---|---|---|
| Which VMs need more memory? | list_recommendations | list_recommendations | kind: expected 'upsize_mem', got 'downsize_mem' |


Twenty questions from `services/api/tests/assistant_eval.yaml`, each with the tool call it should make. Two things are scored separately: whether the model reached for the right tool, and whether its arguments were right given that tool. An argument the model omits counts as correct when the tool's own default already matches - the question is whether the call does the right thing, not whether the model restated a default.

### `qwen2.5-3b-instruct`

- Cases: 20
- **Tool-selection accuracy: 100%**
- **Argument accuracy (given the right tool): 95%**
- Argument accuracy overall: 95%
- Wall clock: 21.4s for 20 questions

✅ Meets the 80% tool-selection bar from spec 14, Phase 6.

Where it got it wrong:

| Question | Expected | Called | Problem |
|---|---|---|---|
| Which VMs need more memory? | list_recommendations | list_recommendations | kind: expected 'upsize_mem', got 'downsize_mem' |


## 4. How to reproduce

```bash
make up && make seed          # 180 days of synthetic history
make train                    # isolation forest + LSTM + recommendations
make evaluate                 # regenerates this file
```