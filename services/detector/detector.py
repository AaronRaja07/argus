"""
Argus detector — evaluation loop.

Every SCRAPE_INTERVAL it pulls the current signal vector from Prometheus,
scores each signal against a load-conditioned baseline, fuses the scores,
runs the result through a persistence gate, and emits alerts.

Alerts are written to a JSONL file so evaluate.py can score them against the
fault ground truth the service records when a fault is injected.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from prometheus_client import Counter, Gauge, start_http_server

from models import CompositeDetector, PersistenceGate, SignalSpec
from promql import PrometheusClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("argus")

PROM_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
INTERVAL = float(os.getenv("EVAL_INTERVAL_S", "10"))
WARMUP_S = float(os.getenv("WARMUP_S", "120"))
ALERTS_PATH = Path(os.getenv("ALERTS_PATH", "/data/alerts.jsonl"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/scores.jsonl"))

# The load signal everything is conditioned on.
LOAD_QUERY = 'sum(rate(checkout_requests_total[1m]))'

SIGNALS = [
    SignalSpec(
        name="latency_p95",
        query=(
            'histogram_quantile(0.95, sum by (le) '
            '(rate(checkout_request_duration_seconds_bucket[1m])))'
        ),
        weight=1.0,
        min_scale=0.004,      # 4ms — below this, latency noise
    ),
    SignalSpec(
        name="error_ratio",
        query=(
            '(sum(rate(checkout_requests_total{status=~"5.."}[1m])) or vector(0)) '
            '/ clamp_min(sum(rate(checkout_requests_total[1m])), 0.001)'
        ),
        weight=1.15,
        min_scale=0.0015,     # 0.15pp of error rate
    ),
    SignalSpec(
        name="dependency_p95",
        query=(
            'histogram_quantile(0.95, sum by (le) '
            '(rate(checkout_dependency_duration_seconds_bucket[1m])))'
        ),
        weight=0.9,
        min_scale=0.003,
    ),
    SignalSpec(
        name="saturation",
        query='max(checkout_pool_saturation_ratio)',
        weight=0.55,
        min_scale=0.05,       # saturation sits at 0 most of the time
    ),
    SignalSpec(
        name="heap_bytes",
        query='max(checkout_heap_bytes)',
        weight=0.7,
        load_conditioned=False,  # a leak is not a function of load
        min_scale=4_000_000,  # 4MB — smaller than one GC sawtooth
    ),
]

HEALTH = Gauge("argus_health_score", "Fused anomaly score (0 = healthy)")
SIGNAL_SCORE = Gauge("argus_signal_score", "Per-signal robust z-score", ["signal"])
SIGNAL_VALUE = Gauge("argus_signal_value", "Raw signal value", ["signal"])
ALERT_OPEN = Gauge("argus_alert_open", "1 while an alert is open")
ALERTS_FIRED = Counter("argus_alerts_fired_total", "Alerts opened")
BASELINE_READY = Gauge("argus_baseline_ready", "1 once baselines have warmed up")
BASELINE_SAMPLES = Gauge(
    "argus_baseline_samples", "Observations held per signal baseline", ["signal"]
)
EVAL_ERRORS = Counter("argus_evaluation_errors_total", "Failed evaluation cycles")
OBSERVED_LOAD = Gauge("argus_observed_load_rps", "Load the scores are conditioned on")


class Detector:
    def __init__(self) -> None:
        self.prom = PrometheusClient(PROM_URL)
        self.composite = CompositeDetector(SIGNALS)
        self.gate = PersistenceGate(
            enter=float(os.getenv("ENTER_THRESHOLD", "3.5")),
            exit_at=float(os.getenv("EXIT_THRESHOLD", "2.0")),
            enter_for=int(os.getenv("ENTER_FOR", "3")),
            exit_for=int(os.getenv("EXIT_FOR", "4")),
        )
        self.started = time.time()
        self.current_alert: dict | None = None
        for p in (ALERTS_PATH, STATE_PATH):
            p.parent.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------- bootstrap
    def bootstrap(self, minutes: int = 90, step: str = "15s") -> int:
        """Backfill baselines from Prometheus history instead of cold-starting.

        Learning from scratch means the detector is blind for its first ten
        minutes, which is exactly when you have just restarted it after an
        incident. Prometheus already holds the history, so we replay it.
        """
        load_series = {int(s.ts): s.value for s in self.prom.range(LOAD_QUERY, minutes, step)}
        if not load_series:
            return 0
        per_signal: dict[str, dict[int, float]] = {}
        for spec in SIGNALS:
            try:
                per_signal[spec.name] = {
                    int(s.ts): s.value for s in self.prom.range(spec.query, minutes, step)
                }
            except Exception as exc:
                log.warning("bootstrap: %s unavailable (%s)", spec.name, exc)
                per_signal[spec.name] = {}

        replayed = 0
        for ts, load in sorted(load_series.items()):
            values = {n: ser[ts] for n, ser in per_signal.items() if ts in ser}
            if values:
                self.composite.observe(load, values)
                replayed += 1
        return replayed

    # ---------------------------------------------------------------- io
    def collect(self) -> tuple[float, dict[str, float]]:
        load = self.prom.instant(LOAD_QUERY) or 0.0
        values: dict[str, float] = {}
        for spec in SIGNALS:
            v = self.prom.instant(spec.query)
            if v is not None:
                values[spec.name] = v
        return load, values

    def emit(self, record: dict, path: Path) -> None:
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------- cycle
    def step(self) -> None:
        load, values = self.collect()
        if not values:
            return

        OBSERVED_LOAD.set(load)
        for name, v in values.items():
            SIGNAL_VALUE.labels(signal=name).set(v)

        for name, baseline in self.composite.baselines.items():
            BASELINE_SAMPLES.labels(signal=name).set(len(baseline._obs))

        warming = (time.time() - self.started) < WARMUP_S or not self.composite.ready()
        BASELINE_READY.set(0 if warming else 1)

        scores = self.composite.score_signals(load, values)
        for name, s in scores.items():
            SIGNAL_SCORE.labels(signal=name).set(s)

        fused = self.composite.fuse(scores, self.composite.specs)
        HEALTH.set(fused)

        # Learn only from data we are not currently calling anomalous, so a
        # sustained incident does not quietly become the new normal.
        if fused < self.gate.enter:
            self.composite.observe(load, values)

        self.emit(
            {
                "ts": time.time(),
                "load": load,
                "fused": fused,
                "warming": warming,
                "values": values,
                "scores": scores,
            },
            STATE_PATH,
        )

        if warming:
            return

        is_open, opened, closed = self.gate.update(fused)
        ALERT_OPEN.set(1 if is_open else 0)

        if opened:
            top = max(scores.items(), key=lambda kv: kv[1]) if scores else ("?", 0.0)
            self.current_alert = {
                "opened_at": time.time(),
                "peak_score": fused,
                "primary_signal": top[0],
                "scores_at_open": scores,
                "load_at_open": load,
            }
            ALERTS_FIRED.inc()
            log.warning(
                "ALERT OPEN  score=%.2f  primary=%s  load=%.1f rps", fused, top[0], load
            )
        elif is_open and self.current_alert:
            self.current_alert["peak_score"] = max(
                self.current_alert["peak_score"], fused
            )
        elif closed and self.current_alert:
            self.current_alert["closed_at"] = time.time()
            self.current_alert["duration_s"] = round(
                self.current_alert["closed_at"] - self.current_alert["opened_at"], 1
            )
            self.emit(self.current_alert, ALERTS_PATH)
            log.info(
                "alert closed  after %.0fs  peak=%.2f",
                self.current_alert["duration_s"],
                self.current_alert["peak_score"],
            )
            self.current_alert = None

    def run(self) -> None:
        log.info("waiting for prometheus at %s", PROM_URL)
        while not self.prom.ready():
            time.sleep(2)
        replayed = self.bootstrap()
        if replayed:
            log.info("bootstrapped %d historical samples from prometheus", replayed)
            if self.composite.ready():
                self.started = 0.0  # history is sufficient; skip the cold warm-up
                log.info("baselines ready from history — detecting immediately")
        else:
            log.info("no history available; warming baselines for %.0fs", WARMUP_S)

        while True:
            cycle = time.time()
            try:
                self.step()
            except Exception as exc:  # keep the loop alive; a bad scrape is not fatal
                EVAL_ERRORS.inc()
                log.error("evaluation cycle failed: %s", exc)
            time.sleep(max(0.0, INTERVAL - (time.time() - cycle)))


def main() -> None:
    start_http_server(int(os.getenv("METRICS_PORT", "9110")))
    det = Detector()

    def bye(*_):
        log.info("shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    det.run()


if __name__ == "__main__":
    main()
