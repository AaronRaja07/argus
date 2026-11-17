"""
Detection models.

The central problem this file solves: in the simulated service (and in any real
one) latency is *coupled to load*. Request rate climbs every morning and latency
climbs with it, entirely healthily. A detector that thresholds p99 latency, or
even z-scores it against its own recent history, fires on every peak.

So instead of asking "is latency unusual?", we ask "is latency unusual **for
this level of load**?" We keep a rolling population of (load, latency)
observations, bin them by load, and score a new observation against the
distribution of its own bin. A traffic surge moves you into a higher bin where
higher latency is normal; a genuine slowdown makes you an outlier inside
whatever bin you are in.

Robust statistics throughout (median/MAD rather than mean/std) because the
anomalies we are hunting would otherwise poison the baseline we score against.
"""

from __future__ import annotations

import heapq
import math
import statistics
from collections import deque
from dataclasses import dataclass, field

MAD_TO_SIGMA = 1.4826  # makes MAD a consistent estimator of sigma for normals


def median_abs_deviation(values: list[float], med: float | None = None) -> float:
    if not values:
        return 0.0
    med = statistics.median(values) if med is None else med
    return statistics.median([abs(v - med) for v in values])


Z_CAP = 12.0  # no single signal may dominate fusion without bound


def iqr_scale(values: list[float]) -> float:
    """Spread estimated from the interquartile range, normalised to sigma."""
    if len(values) < 4:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    q1 = ordered[n // 4]
    q3 = ordered[(3 * n) // 4]
    return (q3 - q1) / 1.349


def robust_z(value: float, population: list[float], min_scale: float = 0.0) -> float:
    """Outlier-resistant z-score, hardened against near-constant signals.

    A signal that sits at a constant value has MAD == 0, and dividing by that
    produces astronomically large scores on the first tiny deviation. This bit
    me in testing: pool saturation is 0.0 almost always, so a routine reading
    of 0.25 scored 2.5e8 and swamped every other signal in fusion.

    The fix is a scale ladder -- MAD, then IQR, then an explicit per-signal
    floor describing the smallest deviation that is actually meaningful for
    that metric. The result is capped so no signal can dominate without bound.
    """
    if len(population) < 8:
        return 0.0
    med = statistics.median(population)

    scale = MAD_TO_SIGMA * median_abs_deviation(population, med)
    if scale <= 0:
        scale = iqr_scale(population)
    # min_scale is the real safeguard: below it, differences are noise
    scale = max(scale, min_scale)
    if scale <= 0:
        return 0.0

    z = (value - med) / scale
    return max(-Z_CAP, min(Z_CAP, z))


@dataclass
class LoadConditionedBaseline:
    """Scores a signal against its own history *at a comparable load level*.

    Implemented as k-nearest-neighbours in load space: to score an observation
    taken at load L, we compare it against the `neighbours` historical samples
    whose load was closest to L.

    An earlier version pre-binned observations into quantile buckets at
    observe() time. That was subtly wrong -- the quantile edges are estimated
    from data that arrives over time, so early samples were filed against
    edges that no longer existed, and nothing re-binned them. In testing, the
    lowest-load bucket ended up holding peak-hour latencies, its spread
    inflated accordingly, and a real slowdown at low load scored 1.1 instead
    of ~6. Choosing the comparison set at *score* time removes the failure
    mode entirely and is simpler.
    """

    window: int = 2400
    neighbours: int = 60
    min_samples: int = 12
    _obs: deque = field(default_factory=lambda: deque(maxlen=2400))

    def observe(self, load: float, value: float) -> None:
        self._obs.append((load, value))

    def population(self, load: float) -> list[float]:
        """The `neighbours` samples recorded at the most similar load."""
        if len(self._obs) < self.min_samples:
            return []
        k = min(self.neighbours, len(self._obs))
        nearest = heapq.nsmallest(k, self._obs, key=lambda lv: abs(lv[0] - load))
        return [v for _, v in nearest]

    def score(self, load: float, value: float, min_scale: float = 0.0) -> float:
        return robust_z(value, self.population(load), min_scale)

    def ready(self) -> bool:
        return len(self._obs) >= self.min_samples * 3


@dataclass
class PersistenceGate:
    """Hysteresis. The single most effective anti-noise mechanism there is.

    An alert opens only after the score has held above `enter` for
    `enter_for` consecutive evaluations, and closes only after it has sat
    below `exit_at` for `exit_for`. Without this, any threshold on a noisy
    signal produces a storm of one-sample alerts and nobody trusts the pager.
    """

    enter: float = 3.5
    exit_at: float = 2.0
    enter_for: int = 3
    exit_for: int = 4

    _above: int = 0
    _below: int = 0
    _open: bool = False

    def update(self, score: float) -> tuple[bool, bool, bool]:
        """Returns (is_open, just_opened, just_closed)."""
        opened = closed = False
        if score >= self.enter:
            self._above += 1
            self._below = 0
        elif score <= self.exit_at:
            self._below += 1
            self._above = 0
        else:
            self._above = self._below = 0

        if not self._open and self._above >= self.enter_for:
            self._open, opened = True, True
            self._above = 0
        elif self._open and self._below >= self.exit_for:
            self._open, closed = False, True
            self._below = 0
        return self._open, opened, closed

    @property
    def is_open(self) -> bool:
        return self._open


@dataclass
class SignalSpec:
    name: str
    query: str
    weight: float = 1.0
    load_conditioned: bool = True
    direction: str = "up"  # "up" = only high values are anomalous
    min_scale: float = 0.0  # smallest deviation that counts as real for this metric


class CompositeDetector:
    """Fuses per-signal scores into one health score.

    Fusion is a weighted soft-max rather than a mean: one signal screaming is
    the common shape of a real incident, and averaging would bury it under
    three calm signals. But we add a correlation bonus, because several
    signals moving together is much stronger evidence than one moving alone.
    """

    def __init__(self, specs: list[SignalSpec]) -> None:
        self.specs = {s.name: s for s in specs}
        self.baselines = {
            s.name: LoadConditionedBaseline() for s in specs if s.load_conditioned
        }
        self.plain: dict[str, deque] = {
            s.name: deque(maxlen=600) for s in specs if not s.load_conditioned
        }

    def observe(self, load: float, values: dict[str, float]) -> None:
        for name, value in values.items():
            if name in self.baselines:
                self.baselines[name].observe(load, value)
            elif name in self.plain:
                self.plain[name].append(value)

    def score_signals(self, load: float, values: dict[str, float]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for name, value in values.items():
            spec = self.specs.get(name)
            if spec is None:
                continue
            if name in self.baselines:
                z = self.baselines[name].score(load, value, spec.min_scale)
            else:
                z = robust_z(value, list(self.plain.get(name, ())), spec.min_scale)
            if spec.direction == "up":
                z = max(0.0, z)
            scores[name] = z
        return scores

    @staticmethod
    def fuse(scores: dict[str, float], specs: dict[str, SignalSpec]) -> float:
        if not scores:
            return 0.0
        weighted = [(specs[n].weight * s) for n, s in scores.items() if n in specs]
        if not weighted:
            return 0.0
        peak = max(weighted)
        # how many signals are meaningfully elevated at the same time
        corroborating = sum(1 for w in weighted if w >= 2.0)
        bonus = 1.0 + 0.18 * max(0, corroborating - 1)
        return peak * bonus

    def ready(self) -> bool:
        return all(b.ready() for b in self.baselines.values()) if self.baselines else True
