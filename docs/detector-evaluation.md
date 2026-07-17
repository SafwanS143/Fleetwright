# Fleetwright — Detector Evaluation (Chunk 21)

Chunk 20 stood up **two** anomaly detectors per channel — a robust statistical baseline (median + MAD
z-score) and an Isolation Forest — and wired **neither** to alerting. This is the evaluation that picks
one for the alerting path, and the decision is the artifact: *which detector, on what evidence, and what
it costs.* The estimator is off-the-shelf; the defensible part is the choice.

Reproduce: `python cloud/anomaly/evaluate.py` (offline, no Pi, no broker — numbers below are the default
seed).

## Why the evaluation is offline and synthetic

To measure a **false-positive rate** and a **detection latency** you need *labels*: you must know which
samples are clean and which are fault. A real fleet has no labelled faults — that is the whole reason
Chunk 20 went unsupervised. Real telemetry off the Pi's sensors is just unlabelled numbers, so there is
nothing to score correctness against. So the harness generates **synthetic** data — a stand-in for the
sensor signal, from the same `fake_telemetry.py` generator used elsewhere (imported, not re-implemented,
so the fake shape stays consistent with the dev tooling) — where clean vs. fault is known by
construction. The **detectors** under test are the exact production estimators from `detectors.py`; only
the **signal** is synthetic. Fabricating labelled data this way is standard practice for evaluating an
unsupervised detector.

**Method.** Per channel: fit both detectors on a 300-sample clean warm-up (identical to the production
default), then score a labelled stream of alternating clean windows (the FP population) and 8 injected
fault episodes (the detection population). Faults are tested at two magnitudes — the production offset
("clear") and 0.35× it ("subtle") — to expose the sensitivity tradeoff. Metrics: FP rate on
clean-labelled samples, per-episode detection rate, and detection latency (time to the first flag inside
a fault window).

## Results (default configs: z-score σ=3.5, IF contamination=0.01)

| Detector | mean FP rate | detect (clear) | detect (subtle) | latency |
| --- | --- | --- | --- | --- |
| **z-score / MAD** | **0.000** | 0.91 | 0.59 | ~0.01 s |
| Isolation Forest | **0.056** | 1.00 | 0.88 | ~0.00 s |

Per-channel FP rate is where they split hardest. The z-score baseline is **0.000 on every channel**. The
Isolation Forest is nonzero everywhere and **0.174 on humidity** — and IF's floor is structural, not
tuning: `contamination` *tells* the model to call ~1% of data outliers, so a nonzero FP rate is
guaranteed by construction, and humidity makes it worse because the 300-sample (~30 s) baseline covers
less than one period of its ~44 s sine, so held-out clean data visits regions the training window barely
saw and IF flags them.

## The decision: the z-score / MAD baseline enters the alerting path

**On a 10 Hz continuous stream the false-positive rate is the deciding axis, and it is not close.** IF's
5.6% mean FP (17% on humidity) is ~0.56 false flags/second/channel — thousands of false pages an hour.
An alerting path that cries wolf that often trains the on-call to ignore it, which is worse than no
alert at all (Chunk 23). The z-score baseline's 0.000 FP is the property that makes it *safe to wire to
paging*. Detection does **not** separate them — both catch clear faults at ~one-sample latency — so the
tie breaks entirely on false positives, and on cost/explainability besides:

- **Explainable, in real units.** The z-score's threshold is a band (`median ± σ·robust_std`) you can
  shade under the raw signal and write a runbook against ("temperature left its 20–24 °C band"). IF
  emits an opaque score with no physical meaning.
- **Deterministic.** IF depends on `random_state`; the same data can flip a borderline flag between
  seeds. A statistical band is fully reproducible — you want that on the paging path.
- **Cheap.** O(1) arithmetic per sample vs. traversing 100 trees.

## When I would *not* pick the baseline — i.e. when IF earns its place

The baseline wins **here** because every channel is **1-D and unimodal** — one sensor, one number, a
single normal band describes it. That is precisely the case where IF's strengths are wasted and its
costs (FP floor, nondeterminism, opacity) are paid for nothing. IF is the right tool when the anomaly
is **multivariate**: a *combination* of channels that is abnormal while each channel alone looks normal
(e.g. temperature up **and** pressure down together — a signature no per-channel band can see). It also
beats a single MAD band on **multimodal or non-Gaussian** distributions where "normal" isn't one blob.
Move Fleetwright to joint multi-channel detection and I would revisit IF; for independent per-channel
scalars, the baseline is correct.

## Limitations of the chosen detector, and mitigations

**1. Periodic channels bury moderate faults.** The frozen MAD band folds a channel's legitimate
oscillation into its spread: temperature's ±2 °C sine and humidity's ±5 %RH sine inflate `robust_std`,
so the band widens and a *subtle* fault hides inside it. In the results, temperature's subtle fault is
missed at every σ, and humidity's subtle detection is only 0.375 at σ=3.5. *Mitigation:* detrend the
periodic component before scoring (or size the baseline to a whole number of periods) so the band
reflects noise, not the swing — this recovers sensitivity without paying IF's FP price.

**2. Legitimate regime change reads as a fault (the key false positive).** Concrete scenario: the BME280
genuinely warms — the enclosure heats up, ambient rises — and temperature climbs out of a band that was
frozen on a cooler warm-up. That is a real, benign trend, not a fault, but a single-sample threshold
flags it. *Mitigation:* (a) require a **sustained** breach (N-of-M / dwell) before alerting, so a slow
legitimate drift doesn't page on one sample; and (b) **re-baseline** on a rolling window so a new steady
state re-anchors the band. The tension worth naming: re-baseline too eagerly and you mask the *slow real
faults* you actually want to catch — a sensor drifting out of calibration looks exactly like a benign
regime change, so this knob trades false positives against missed slow faults directly.

## Contamination / threshold — the one knob, and why σ stays at 3.5

Both detectors expose the same sensitivity dial under different names: **σ** for the baseline,
**contamination** for IF. Turning it toward *more sensitive* (lower σ, higher contamination) catches
subtler faults but raises false positives; toward *less sensitive* does the reverse. It is the classic
sensitivity-vs-specificity trade, and you set it from the **FP budget the paging path can tolerate**
(alert fatigue) balanced against **MTTD**.

The sweep shows headroom: lowering σ from 3.5 to 3.0 lifts temperature's clear-fault detection from
0.625 to 1.000 with FP still 0.000 in this data. I am **keeping the shipped default at σ=3.5** anyway —
the synthetic noise here (±0.1 units) is unrealistically clean, so a threshold tuned tight against it
would under-provision for real sensor data's heavier tails. σ=3.5 is the robust-stats convention and
leaves margin; the honest fix for the periodic channels is detrending (limitation 1), not shaving the
threshold against synthetic noise.
