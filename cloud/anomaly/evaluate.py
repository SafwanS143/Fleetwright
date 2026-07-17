#!/usr/bin/env python3
"""Chunk 21 — evaluate the two Chunk 20 detectors and PICK ONE for the alerting path.

The interview-valuable artifact here is the *decision*, not the estimator: on identical data, which
detector should feed alerting, backed by numbers instead of a hunch. Two questions decide it:

  1. False-positive rate on clean baseline data — every false flag is a page that trains the on-call to
     ignore the channel (alert fatigue). This is the dominant cost on a 10 Hz stream: even a 1% FP rate
     is 0.1 false flags/sec = 6/min/channel.
  2. Detection + latency on injected faults — does it catch the fault, and how fast (MTTD).

Why this can run offline with no Pi and no recorded baseline: measuring a FP rate and a detection
latency needs *labels* — you must know which samples are clean and which are fault. Real telemetry off
the Pi's sensors carries no labels; it's just numbers, so there's nothing to score against. So we
generate SYNTHETIC data — a stand-in for the sensor signal — where we decide by construction which
samples are clean and which are fault. It comes from the same `fake_telemetry.py` generator used
elsewhere (imported, not re-implemented), so the synthetic shape stays consistent with the rest of the
dev tooling. The detectors under test, though, are the exact production estimators from detectors.py.
Fabricating labelled data like this is the standard way to evaluate an unsupervised detector when a real
fleet has no labelled faults — which is exactly why Chunk 20 stood up two detectors instead of trusting
one.

    python evaluate.py            # full sweep + decision, printed as tables
    python evaluate.py --seed 7   # different draw of the synthetic noise

Numbers quoted in docs/detector-evaluation.md come from the default seed.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys

import numpy as np

# Reuse the production ESTIMATORS (detectors.py) so the evaluation tests the code that actually ships,
# and the shared synthetic GENERATOR (fake_telemetry.py) so the fake signal matches the rest of the dev
# tooling — neither is a parallel re-implementation that could quietly diverge. The signal is synthetic;
# only the detectors are production.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from detectors import IsolationForestDetector, ZScoreDetector  # noqa: E402
from fake_telemetry import ANOMALY_OFFSETS, build_sample  # noqa: E402

PERIOD = 0.1  # 10 Hz, the real operating point
BASELINE_N = 300  # warm-up window, identical to the production default
CLEAN_N = 200  # clean samples between fault episodes (the FP-rate population)
FAULT_N = 50  # samples per fault episode
EPISODES = 8  # fault onsets, so detection latency is a mean over repeats, not one lucky draw
SUBTLE = 0.35  # a "subtle" fault = this fraction of the production offset, to expose the miss/FP tradeoff

# Same channel mapping the service uses: accel is the vector magnitude, the rest are scalar fields.
CHANNELS = ["temp", "humidity", "pressure", "accel"]

# Sweep grids: the sensitivity knob for each detector. Lower sigma / higher contamination = more
# sensitive = more true detections but more false positives. This is the one tradeoff that matters.
SIGMA_GRID = [2.5, 3.0, 3.5, 4.0, 5.0]
CONTAM_GRID = [0.005, 0.01, 0.02, 0.05]
DEFAULT_SIGMA = 3.5
DEFAULT_CONTAM = 0.01


def channel_value(sample: dict, target: str) -> float:
    """Pull one channel's scalar out of a generated sample, matching anomaly_service._channels."""
    if target == "accel":
        return math.sqrt(sample["ax"] ** 2 + sample["ay"] ** 2 + sample["az"] ** 2)
    return float(sample["temp" if target == "temp" else target])


def _generate(target: str, n: int, off: float, t0: float) -> tuple[np.ndarray, float]:
    """n samples of `target`'s value; `off` is the fault offset (0.0 = clean). Returns (values, t_end)."""
    vals = []
    t = t0
    for _ in range(n):
        vals.append(channel_value(build_sample(t, target, off), target))
        t += PERIOD
    return np.asarray(vals, dtype=float), t


def make_stream(target: str, off: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int]]:
    """Build a labelled evaluation stream for one channel: a clean baseline to fit on, then alternating
    clean / fault windows. Returns (baseline, values, labels, fault_start_indices)."""
    baseline, t = _generate(target, BASELINE_N, 0.0, 0.0)
    values: list[float] = []
    labels: list[int] = []
    fault_starts: list[int] = []
    for _ in range(EPISODES):
        clean, t = _generate(target, CLEAN_N, 0.0, t)
        values.extend(clean)
        labels.extend([0] * len(clean))
        fault_starts.append(len(values))  # fault begins at the next appended index
        fault, t = _generate(target, FAULT_N, off, t)
        values.extend(fault)
        labels.extend([1] * len(fault))
    return baseline, np.asarray(values), np.asarray(labels), fault_starts


def _flags(det, values: np.ndarray) -> np.ndarray:
    """Vectorized equivalent of calling det.flagged() sample-by-sample — same verdict, far faster, so
    the sweep re-runs in under a second. The equivalences are the ones documented in detectors.py:
      - z-score:  flagged  <=>  (|x-median| / robust_std) / sigma > 1.0
      - IF:       flagged  <=>  decision_function(x) < 0  (ratio>1.0 iff native score>0)."""
    if isinstance(det, ZScoreDetector):
        return (np.abs(values - det.median) / det.robust_std) / det.sigma > 1.0
    assert isinstance(det, IsolationForestDetector) and det.model is not None
    return det.model.decision_function(values.reshape(-1, 1)) < 0.0


def score(det, baseline: np.ndarray, values: np.ndarray, labels: np.ndarray,
          fault_starts: list[int]) -> dict:
    """Fit a detector on the baseline and score the stream. Returns FP rate (on clean-labelled samples),
    per-episode detection rate, and mean detection latency in seconds (time to the first flag inside a
    fault window)."""
    det.fit(baseline)
    flags = _flags(det, values)

    fp_rate = float(flags[labels == 0].mean())

    detected = 0
    latencies: list[int] = []
    for start in fault_starts:
        window = flags[start:start + FAULT_N]
        if window.any():
            detected += 1
            latencies.append(int(np.argmax(window)))  # index of first True = samples until detection
    detection_rate = detected / len(fault_starts)
    mean_latency_s = float(np.mean(latencies)) * PERIOD if latencies else float("nan")
    return {"fp": fp_rate, "detection": detection_rate, "latency_s": mean_latency_s}


def _fmt(v: float) -> str:
    return "  n/a " if math.isnan(v) else f"{v:6.3f}"


def sweep(magnitude: str, off_factor: float) -> dict:
    """Run the full sweep for one fault magnitude across all channels, printing per-channel tables and
    returning the aggregate FP/detection/latency for the default config of each detector."""
    print(f"\n{'=' * 78}\n{magnitude.upper()} FAULT  (offset = {off_factor:g}x production)\n{'=' * 78}")

    agg = {"zscore": [], "iforest": []}
    for target in CHANNELS:
        off = ANOMALY_OFFSETS[target] * off_factor
        baseline, values, labels, starts = make_stream(target, off)
        print(f"\n  {target}  (fault offset {off:+.3g} in channel units)")
        print(f"    {'z-score':<24}{'FP rate':>10}{'detect':>10}{'latency s':>12}")
        for sigma in SIGMA_GRID:
            r = score(ZScoreDetector(sigma=sigma), baseline, values, labels, starts)
            mark = "  <- default" if sigma == DEFAULT_SIGMA else ""
            print(f"      sigma={sigma:<17.1f}{_fmt(r['fp']):>10}{_fmt(r['detection']):>10}"
                  f"{_fmt(r['latency_s']):>12}{mark}")
            if sigma == DEFAULT_SIGMA:
                agg["zscore"].append(r)
        print(f"    {'isolation forest':<24}{'FP rate':>10}{'detect':>10}{'latency s':>12}")
        for contam in CONTAM_GRID:
            r = score(IsolationForestDetector(contamination=contam), baseline, values, labels, starts)
            mark = "  <- default" if contam == DEFAULT_CONTAM else ""
            print(f"      contam={contam:<15.3f}{_fmt(r['fp']):>10}{_fmt(r['detection']):>10}"
                  f"{_fmt(r['latency_s']):>12}{mark}")
            if contam == DEFAULT_CONTAM:
                agg["iforest"].append(r)
    return agg


def summarize(agg_full: dict, agg_subtle: dict) -> None:
    """Roll the default-config per-channel results up into the one comparison the decision rests on."""
    print(f"\n{'=' * 78}\nDECISION SUMMARY  (default configs: z-score sigma={DEFAULT_SIGMA}, "
          f"IF contamination={DEFAULT_CONTAM})\n{'=' * 78}")
    for name, det in (("z-score / MAD", "zscore"), ("isolation forest", "iforest")):
        fp = np.mean([r["fp"] for r in agg_full[det] + agg_subtle[det]])
        det_full = np.mean([r["detection"] for r in agg_full[det]])
        det_sub = np.mean([r["detection"] for r in agg_subtle[det]])
        lat = np.nanmean([r["latency_s"] for r in agg_full[det]])
        print(f"  {name:<20}  mean FP={fp:6.3f}   detect(clear)={det_full:5.2f}   "
              f"detect(subtle)={det_sub:5.2f}   latency={lat:5.2f}s")
    print("\n  Every false positive on a 10 Hz stream is a page. FP rate is the deciding axis; both "
          "detect clear\n  faults at ~one-sample latency, so detection does not separate them. See "
          "docs/detector-evaluation.md.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Chunk 21 detector evaluation")
    ap.add_argument("--seed", type=int, default=0, help="seed for the synthetic noise draw")
    args = ap.parse_args()
    # Seed both RNGs the generator and IF touch, so the quoted numbers are reproducible.
    random.seed(args.seed)
    np.random.seed(args.seed)

    agg_full = sweep("clear", 1.0)
    agg_subtle = sweep("subtle", SUBTLE)
    summarize(agg_full, agg_subtle)


if __name__ == "__main__":
    main()
