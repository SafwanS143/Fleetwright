"""Per-channel anomaly detectors for Chunk 20.

Two detectors run in parallel on *every channel* — deliberately not one global model across all
channels, because temperature, humidity, pressure and motion have completely different scales and
"normal" shapes; a single model would let a normal pressure swamp a real temperature excursion.

  1. ZScoreDetector          — a robust statistical baseline (median + MAD). Cheap, explainable, and
                               expressed in the channel's own units, so it yields a concrete normal
                               band you can shade under the raw signal.
  2. IsolationForestDetector — scikit-learn IsolationForest, the estimator reused from the SRE Monitor.

Both are *unsupervised*: there is no labelled fault data on a fleet, so all you can define is "normal"
from a clean warm-up window and score how far each live sample departs from it.

Neither detector is wired to alerting. Chunk 20 only exposes both so they can be compared side by side.
The evaluation that PICKS one for the alerting path — on false-positive rate and detection latency — is
Chunk 21. So the raw scores here live on very different scales (z in robust-sigma units; IF's
decision_function in ~[-0.5, 0.5]). To make them comparable on one axis we export a *normalized* score
where 1.0 is each detector's own trip line: >= 1.0 means "past threshold" for either. That common scale
is a display convenience, not the real comparison — quantifying which detector is actually better is
exactly the Chunk 21 evaluation.
"""

from __future__ import annotations

import numpy as np
from sklearn.ensemble import IsolationForest

MAD_TO_STD = 1.4826  # scales the median-absolute-deviation to a normal-consistent std estimate
_EPS = 1e-9


class ZScoreDetector:
    """Robust statistical baseline: median + MAD, scored in robust-sigma units.

    Robust (median / MAD) rather than mean / std so a few stray samples in the warm-up window can't
    inflate the spread and blind the detector. The trip threshold is `sigma`, so the normalized score
    is (distance in robust-sigma) / sigma and 1.0 is the trip line.
    """

    def __init__(self, sigma: float = 3.5):
        self.sigma = sigma
        self.median = 0.0
        self.robust_std = _EPS
        self.fitted = False

    def fit(self, samples: np.ndarray) -> None:
        self.median = float(np.median(samples))
        mad = float(np.median(np.abs(samples - self.median)))
        rstd = MAD_TO_STD * mad
        if rstd < _EPS:
            # Degenerate (near-constant) warm-up window: fall back to ordinary std, then a hard floor,
            # so a dead-flat baseline can't make every later sample look infinitely anomalous.
            rstd = float(np.std(samples))
        self.robust_std = max(rstd, _EPS)
        self.fitted = True

    def ratio(self, x: float) -> float:
        # Distance from the baseline centre in robust-sigma units, divided by the trip threshold.
        return (abs(x - self.median) / self.robust_std) / self.sigma

    def flagged(self, x: float) -> bool:
        return self.ratio(x) > 1.0

    @property
    def band(self) -> tuple[float, float]:
        # The normal range in the channel's own units: centre ± sigma * robust_std. A raw value outside
        # this band is exactly what `ratio > 1.0` means, drawn on the signal instead of in score space.
        d = self.sigma * self.robust_std
        return self.median - d, self.median + d


class IsolationForestDetector:
    """The reused SRE-Monitor estimator: sklearn IsolationForest, one instance per channel.

    IF isolates points with random axis-aligned splits; an outlier needs *few* splits to be cut off, so
    a short average path length = easy to isolate = anomalous. sklearn's `decision_function` bakes the
    contamination-defined boundary in at exactly 0 (>0 inlier, <0 outlier). We negate it so higher =
    more anomalous, then normalize by the spread of the training scores so 1.0 lands on that boundary:
    `ratio > 1.0` is identical to `decision_function < 0`, i.e. the model's own outlier verdict.
    """

    def __init__(self, contamination: float = 0.01, n_estimators: int = 100, random_state: int = 0):
        self.contamination = contamination
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.model: IsolationForest | None = None
        self._scale = _EPS  # spread of training anomaly scores, set at fit time

    @property
    def fitted(self) -> bool:
        return self.model is not None

    def fit(self, samples: np.ndarray) -> None:
        model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
        )
        X = samples.reshape(-1, 1)
        model.fit(X)
        # Native anomaly score = -decision_function; on the (mostly inlier) training set this spread
        # sets the unit that maps the boundary (0) to a normalized 1.0.
        train_scores = -model.decision_function(X)
        self._scale = max(float(np.std(train_scores)), _EPS)
        self.model = model

    def ratio(self, x: float) -> float:
        assert self.model is not None
        native = float(-self.model.decision_function([[x]])[0])  # >0 = more anomalous, boundary at 0
        return 1.0 + native / self._scale

    def flagged(self, x: float) -> bool:
        # Equivalent to decision_function(x) < 0, i.e. IsolationForest.predict == -1.
        return self.ratio(x) > 1.0


class ChannelDetectors:
    """Both detectors for one (device, channel), sharing a single warm-up baseline window.

    Collects the first `baseline` samples as assumed-normal, fits both detectors once on them, then
    scores every later sample. The baseline is frozen after fitting (Chunk 20 keeps it simple; when a
    legitimate regime change should or shouldn't re-baseline is part of the Chunk 21 limitations story).
    """

    def __init__(self, baseline: int = 300, sigma: float = 3.5,
                 contamination: float = 0.01, n_estimators: int = 100):
        self.baseline = baseline
        self._warmup: list[float] = []
        self.zscore = ZScoreDetector(sigma=sigma)
        self.iforest = IsolationForestDetector(contamination=contamination, n_estimators=n_estimators)

    @property
    def ready(self) -> bool:
        return self.zscore.fitted and self.iforest.fitted

    def update(self, x: float) -> dict | None:
        """Feed one live value.

        Returns per-detector normalized scores + flags and the statistical band once fitted; returns
        None while still warming up (including the sample that completes and triggers the fit).
        """
        if not self.ready:
            self._warmup.append(x)
            if len(self._warmup) >= self.baseline:
                arr = np.asarray(self._warmup, dtype=float)
                self.zscore.fit(arr)
                self.iforest.fit(arr)
                self._warmup = []  # baseline is frozen; free the buffer
            return None

        lower, upper = self.zscore.band
        return {
            "zscore": {"score": self.zscore.ratio(x), "flag": self.zscore.flagged(x)},
            "iforest": {"score": self.iforest.ratio(x), "flag": self.iforest.flagged(x)},
            "band_lower": lower,
            "band_upper": upper,
        }
