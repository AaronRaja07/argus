"""
checkout-api — the service Argus watches.

This is a simulation, but not a trivial one. The behaviours that make anomaly
detection genuinely hard in production are modelled deliberately:

  * Latency is queue-coupled. Service time rises non-linearly with concurrency
    (an M/M/c-style approximation), so a traffic surge raises latency *without*
    anything being wrong. A detector that just thresholds p99 will cry wolf
    every weekday morning.
  * Errors are partly load-induced. Under saturation the service sheds load,
    so error rate and latency move together during healthy peaks as well as
    during real incidents.
  * A downstream dependency has its own latency distribution and failure mode,
    so some incidents originate outside the service.

Faults are injected through /admin/faults and every injection is appended to a
ground-truth log. That log is what lets us actually score the detector instead
of eyeballing a dashboard. See services/detector/evaluate.py.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

SERVICE = os.getenv("SERVICE_NAME", "checkout-api")
WORKERS = int(os.getenv("WORKER_POOL", "24"))          # concurrent slots
BASE_SERVICE_MS = float(os.getenv("BASE_SERVICE_MS", "38"))
BASE_ERROR_RATE = float(os.getenv("BASE_ERROR_RATE", "0.004"))
GROUND_TRUTH = Path(os.getenv("GROUND_TRUTH_PATH", "/data/ground_truth.jsonl"))

# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

REQUESTS = Counter(
    "checkout_requests_total", "Requests handled", ["endpoint", "status"]
)
LATENCY = Histogram(
    "checkout_request_duration_seconds",
    "End-to-end request latency",
    ["endpoint"],
    # tuned for a 20ms-3s range; default buckets are useless at this scale
    buckets=(.005, .01, .025, .05, .075, .1, .15, .25, .4, .6, 1.0, 1.5, 2.5, 5.0),
)
DEP_LATENCY = Histogram(
    "checkout_dependency_duration_seconds",
    "Downstream dependency latency",
    ["dependency"],
    buckets=(.005, .01, .025, .05, .1, .25, .5, 1.0, 2.5, 5.0),
)
INFLIGHT = Gauge("checkout_inflight_requests", "Requests currently in flight")
QUEUE_DEPTH = Gauge("checkout_queue_depth", "Requests waiting for a worker slot")
POOL_SATURATION = Gauge("checkout_pool_saturation_ratio", "In-flight / worker pool")
HEAP_BYTES = Gauge("checkout_heap_bytes", "Simulated heap in use")
DEP_ERRORS = Counter(
    "checkout_dependency_errors_total", "Downstream dependency failures", ["dependency"]
)
FAULT_ACTIVE = Gauge("checkout_fault_active", "1 while a fault is injected", ["kind"])

# --------------------------------------------------------------------------
# fault injection
# --------------------------------------------------------------------------

FaultKind = Literal[
    "latency_spike",          # service-side slowdown
    "error_burst",            # elevated 5xx independent of load
    "memory_leak",            # slow heap growth -> GC pressure -> latency creep
    "dependency_degradation", # downstream gets slow and flaky
    "partial_outage",         # a fraction of requests fail hard
]


@dataclass
class Fault:
    kind: str
    magnitude: float
    started_at: float
    ends_at: float
    fault_id: str

    def active(self, now: float) -> bool:
        return self.started_at <= now < self.ends_at


class FaultRegistry:
    """Tracks live faults and records every injection as labelled ground truth.

    The ground-truth file is the whole point. Without it this project can show
    a pretty dashboard but cannot answer "does the detector actually work".
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._faults: list[Fault] = []

    def inject(self, kind: str, magnitude: float, duration_s: float) -> Fault:
        now = time.time()
        fault = Fault(
            kind=kind,
            magnitude=magnitude,
            started_at=now,
            ends_at=now + duration_s,
            fault_id=uuid.uuid4().hex[:12],
        )
        self._faults.append(fault)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(fault)) + "\n")
        return fault

    def live(self) -> list[Fault]:
        now = time.time()
        self._faults = [f for f in self._faults if f.ends_at > now - 3600]
        return [f for f in self._faults if f.active(now)]

    def magnitude_of(self, kind: str) -> float:
        return sum(f.magnitude for f in self.live() if f.kind == kind)

    def clear(self) -> int:
        n = len(self.live())
        now = time.time()
        for f in self._faults:
            if f.active(now):
                f.ends_at = now
        return n


faults = FaultRegistry(GROUND_TRUTH)

# --------------------------------------------------------------------------
# the simulation itself
# --------------------------------------------------------------------------

_inflight = 0
_heap_base = 220_000_000.0
_leak_started: float | None = None


def queue_delay_ms(inflight: int) -> float:
    """Approximate queueing delay for `inflight` requests over WORKERS slots.

    Below saturation this is ~0. Past it, wait time grows sharply. This single
    function is why the detector cannot just threshold latency: a legitimate
    traffic peak produces the same latency signature as a slowdown.
    """
    rho = inflight / WORKERS
    if rho < 0.7:
        return random.gauss(1.5, 0.6)
    # blow-up term, clamped so the sim stays bounded
    rho = min(rho, 3.5)
    return BASE_SERVICE_MS * (rho ** 2.4) * random.uniform(0.75, 1.3)


def heap_bytes() -> float:
    """Sawtooth heap with GC, plus monotonic growth while a leak is active."""
    global _leak_started
    leak = faults.magnitude_of("memory_leak")
    now = time.time()
    if leak > 0 and _leak_started is None:
        _leak_started = now
    elif leak == 0:
        _leak_started = None

    gc_cycle = 90.0
    sawtooth = (now % gc_cycle) / gc_cycle * 60_000_000
    grown = 0.0
    if _leak_started is not None:
        grown = (now - _leak_started) * leak * 900_000
    return _heap_base + sawtooth + grown + random.gauss(0, 3_000_000)


async def call_dependency(name: str = "payments") -> float:
    """Simulated downstream call. Returns latency in seconds; may raise."""
    degradation = faults.magnitude_of("dependency_degradation")

    base = random.lognormvariate(math.log(0.018), 0.55)
    latency = base * (1.0 + degradation * 9.0)
    latency = min(latency, 6.0)

    failure_p = 0.0015 + degradation * 0.06
    await asyncio.sleep(latency)
    DEP_LATENCY.labels(dependency=name).observe(latency)

    if random.random() < failure_p:
        DEP_ERRORS.labels(dependency=name).inc()
        raise RuntimeError(f"{name} unavailable")
    return latency


@asynccontextmanager
async def track_inflight():
    global _inflight
    _inflight += 1
    INFLIGHT.set(_inflight)
    QUEUE_DEPTH.set(max(0, _inflight - WORKERS))
    POOL_SATURATION.set(_inflight / WORKERS)
    try:
        yield _inflight
    finally:
        _inflight -= 1
        INFLIGHT.set(_inflight)
        QUEUE_DEPTH.set(max(0, _inflight - WORKERS))
        POOL_SATURATION.set(_inflight / WORKERS)


app = FastAPI(title="checkout-api", version="1.0.0")


class FaultRequest(BaseModel):
    kind: FaultKind
    magnitude: float = Field(1.0, gt=0, le=10, description="severity multiplier")
    duration_s: float = Field(300, gt=0, le=7200)


@app.post("/admin/faults")
async def inject_fault(req: FaultRequest):
    fault = faults.inject(req.kind, req.magnitude, req.duration_s)
    FAULT_ACTIVE.labels(kind=req.kind).set(1)
    return {"injected": asdict(fault)}


@app.delete("/admin/faults")
async def clear_faults():
    n = faults.clear()
    for kind in ("latency_spike", "error_burst", "memory_leak",
                 "dependency_degradation", "partial_outage"):
        FAULT_ACTIVE.labels(kind=kind).set(0)
    return {"cleared": n}


@app.get("/admin/faults")
async def list_faults():
    return {"active": [asdict(f) for f in faults.live()]}


@app.get("/health")
async def health():
    return {"status": "ok", "inflight": _inflight, "pool": WORKERS}


@app.get("/metrics")
async def metrics():
    HEAP_BYTES.set(heap_bytes())
    for kind in ("latency_spike", "error_burst", "memory_leak",
                 "dependency_degradation", "partial_outage"):
        FAULT_ACTIVE.labels(kind=kind).set(
            1 if faults.magnitude_of(kind) > 0 else 0
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/checkout")
async def checkout():
    endpoint = "/checkout"
    started = time.perf_counter()

    async with track_inflight() as inflight:
        # hard failures first — these don't consume the full request path
        if random.random() < faults.magnitude_of("partial_outage") * 0.22:
            REQUESTS.labels(endpoint=endpoint, status="503").inc()
            LATENCY.labels(endpoint=endpoint).observe(time.perf_counter() - started)
            raise HTTPException(503, "service unavailable")

        # queueing + base service time + injected slowdown
        wait_ms = queue_delay_ms(inflight)
        service_ms = random.lognormvariate(math.log(BASE_SERVICE_MS), 0.4)
        service_ms *= 1.0 + faults.magnitude_of("latency_spike") * 3.2

        # heap pressure bleeds into latency, which is how a leak actually presents
        heap_pressure = max(0.0, (heap_bytes() - _heap_base - 60_000_000) / 400_000_000)
        service_ms *= 1.0 + heap_pressure * 1.8

        await asyncio.sleep((wait_ms + service_ms) / 1000.0)

        status = "200"
        try:
            await call_dependency()
        except RuntimeError:
            status = "502"

        load_induced_error_p = max(0.0, (inflight / WORKERS - 1.1)) * 0.10
        injected_error_p = faults.magnitude_of("error_burst") * 0.18
        if status == "200" and random.random() < (
            BASE_ERROR_RATE + load_induced_error_p + injected_error_p
        ):
            status = "500"

    elapsed = time.perf_counter() - started
    LATENCY.labels(endpoint=endpoint).observe(elapsed)
    REQUESTS.labels(endpoint=endpoint, status=status).inc()

    if status != "200":
        raise HTTPException(int(status), f"checkout failed ({status})")
    return {"order_id": uuid.uuid4().hex[:16], "latency_ms": round(elapsed * 1000, 1)}
