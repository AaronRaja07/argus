"""
loadgen — traffic with a shape.

Flat synthetic load makes anomaly detection look easy, which is exactly why
most portfolio projects use it. Real traffic has a daily cycle, a weekly cycle,
bursts, and a long tail of quiet periods. A detector tuned on flat traffic
falls apart the first morning ramp it sees.

A `--compress` factor lets a full simulated day run in minutes so experiments
are practical: --compress 288 turns 24h into 5 minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import time

import aiohttp
from prometheus_client import Counter, Gauge, start_http_server

TARGET = os.getenv("TARGET_URL", "http://checkout-api:8000/checkout")

SENT = Counter("loadgen_requests_sent_total", "Requests issued", ["outcome"])
RATE = Gauge("loadgen_target_rps", "Requested RPS at this instant")
PHASE = Gauge("loadgen_simulated_hour", "Simulated hour of day (0-24)")


def diurnal_rps(sim_hour: float, base: float, peak: float) -> float:
    """Two-humped daily curve: a morning ramp and a larger evening peak."""
    morning = math.exp(-((sim_hour - 10.0) ** 2) / (2 * 2.1 ** 2))
    evening = math.exp(-((sim_hour - 20.0) ** 2) / (2 * 2.6 ** 2))
    shape = 0.65 * morning + 1.0 * evening
    night = 0.12 + 0.05 * math.sin(sim_hour / 24 * 2 * math.pi)
    return base + (peak - base) * max(shape, night)


async def worker(session: aiohttp.ClientSession, queue: asyncio.Queue) -> None:
    while True:
        await queue.get()
        try:
            async with session.post(TARGET, timeout=aiohttp.ClientTimeout(total=10)) as r:
                SENT.labels(outcome=("ok" if r.status == 200 else str(r.status))).inc()
        except asyncio.TimeoutError:
            SENT.labels(outcome="timeout").inc()
        except Exception:
            SENT.labels(outcome="error").inc()
        finally:
            queue.task_done()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-rps", type=float, default=float(os.getenv("BASE_RPS", "6")))
    ap.add_argument("--peak-rps", type=float, default=float(os.getenv("PEAK_RPS", "38")))
    ap.add_argument("--compress", type=float, default=float(os.getenv("COMPRESS", "120")),
                    help="simulated seconds per real second")
    ap.add_argument("--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "80")))
    ap.add_argument("--metrics-port", type=int, default=9109)
    ap.add_argument("--start-hour", type=float, default=float(os.getenv("START_HOUR", "0")))
    args = ap.parse_args()

    start_http_server(args.metrics_port)
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)

    started = time.time()
    async with aiohttp.ClientSession() as session:
        workers = [asyncio.create_task(worker(session, queue))
                   for _ in range(args.concurrency)]
        try:
            while True:
                elapsed = time.time() - started
                sim_hour = (args.start_hour + elapsed * args.compress / 3600.0) % 24.0
                PHASE.set(sim_hour)

                rps = diurnal_rps(sim_hour, args.base_rps, args.peak_rps)
                # bursty arrivals, not a metronome
                rps *= random.lognormvariate(0, 0.16)
                RATE.set(rps)

                tick = 0.25
                for _ in range(max(1, int(rps * tick))):
                    try:
                        queue.put_nowait(1)
                    except asyncio.QueueFull:
                        SENT.labels(outcome="shed").inc()
                await asyncio.sleep(tick)
        finally:
            for w in workers:
                w.cancel()


if __name__ == "__main__":
    asyncio.run(main())
