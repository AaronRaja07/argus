"""
Drive a scripted incident timeline against checkout-api.

Injects a mix of fault types separated by quiet periods, so the evaluation has
both incidents to catch and healthy stretches in which to not cry wolf. The
quiet periods matter as much as the faults: they are where false positives
come from.

Usage:
  python experiments/run_experiment.py --api http://localhost:8000 --scenario standard
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

SCENARIOS = {
    # (label, kind, magnitude, duration_s, quiet_after_s)
    "smoke": [
        ("warm-up quiet", None, 0, 0, 90),
        ("latency spike", "latency_spike", 1.6, 100, 120),
        ("error burst", "error_burst", 1.2, 90, 120),
    ],
    "standard": [
        ("warm-up quiet", None, 0, 0, 180),
        ("moderate latency spike", "latency_spike", 1.4, 150, 150),
        ("dependency degradation", "dependency_degradation", 1.0, 180, 150),
        ("error burst", "error_burst", 1.3, 120, 180),
        ("memory leak", "memory_leak", 2.0, 300, 150),
        ("partial outage", "partial_outage", 0.8, 120, 150),
        ("subtle latency creep", "latency_spike", 0.5, 240, 180),
    ],
    "hard": [
        ("warm-up quiet", None, 0, 0, 240),
        ("subtle latency creep", "latency_spike", 0.35, 300, 200),
        ("slow dependency drift", "dependency_degradation", 0.4, 300, 200),
        ("low-rate errors", "error_burst", 0.35, 240, 200),
        ("slow memory leak", "memory_leak", 0.8, 420, 200),
    ],
}


def post(api: str, path: str, payload: dict | None = None, method: str = "POST"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{api}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:200]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--scenario", default="standard", choices=sorted(SCENARIOS))
    args = ap.parse_args()

    timeline = SCENARIOS[args.scenario]
    total = sum(step[3] + step[4] for step in timeline)
    print(f"scenario '{args.scenario}' — {len(timeline)} phases, ~{total/60:.1f} min\n")

    post(args.api, "/admin/faults", method="DELETE")
    t0 = time.time()

    for label, kind, magnitude, duration, quiet in timeline:
        stamp = time.strftime("%H:%M:%S")
        if kind:
            res = post(args.api, "/admin/faults",
                       {"kind": kind, "magnitude": magnitude, "duration_s": duration})
            fid = res.get("injected", {}).get("fault_id", "?")
            print(f"[{stamp}] INJECT  {label:<26} mag={magnitude} "
                  f"{duration}s  id={fid}")
            time.sleep(duration)
            print(f"[{stamp}] ...fault window ended")
        if quiet:
            print(f"[{time.strftime('%H:%M:%S')}] quiet   {quiet}s\n")
            time.sleep(quiet)

    post(args.api, "/admin/faults", method="DELETE")
    print(f"\ncomplete in {(time.time()-t0)/60:.1f} min")
    print("score it with:  make evaluate")


if __name__ == "__main__":
    main()
